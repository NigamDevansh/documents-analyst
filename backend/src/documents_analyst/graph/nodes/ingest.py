"""Section, chunk, embed, and decide which agreement each document belongs to.

The scanner already hashed the file and stored `extracted_text`. This turns that
into something retrievable and — the load-bearing half — works out which contract
each document is part of. That is why this node resolves parties:
`facts_and_claims.agreement_id` is NOT NULL and an agreement is a normalised party
set, so parties must be known before any fact is written (they cannot live under
`Match`, which reads facts that don't exist yet).

Idempotent by construction: chunks and the agreement link are replaced, not
appended, so re-entering after a crash leaves what a clean run would — the only
guarantee a checkpointed resume needs.

See docs/BACKEND_INTERNALS.md § "Nodes — what each reads and writes".
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy import text as sql

from documents_analyst.db.repo import agreements as agreement_repo
from documents_analyst.db.repo import chunks as chunk_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.graph.context import RunContext, context_of
from documents_analyst.graph.state import PileRunState
from documents_analyst.worker.lease import Fence
from documents_analyst_core.entities.family import detect_family
from documents_analyst_core.entities.parties import (
    extract_party_candidates,
    normalize,
    party_names,
    pick_parties,
)
from documents_analyst_core.parsing.chunks import chunk_sections
from documents_analyst_core.parsing.sections import find_preamble, split_sections

LOG = logging.getLogger(__name__)


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)
    fence = Fence(pile_id=state["pile_id"], epoch=state["lease_epoch"])
    chunked = 0

    for document_id in state["document_ids"]:
        # Chunking and embedding can be the slowest local op in a run. Renew
        # immediately before it, not on a background timer.
        ctx.renew_lease(fence)
        with ctx.engine.begin() as conn:
            row = conn.execute(
                sql(
                    "SELECT id, filename, extracted_text FROM documents "
                    "WHERE id = :id AND extracted_text IS NOT NULL"
                ),
                {"id": document_id},
            ).first()
        if row is None:
            # Removed, superseded or quarantined since the claim. Skipped, not
            # failed: the pile keeps moving under a batch read at run start.
            continue

        body = row.extracted_text
        sections = split_sections(body)
        agreement_id = _agreement_for(ctx, fence, body, sections, title=row.filename)

        with ctx.engine.begin() as conn:
            agreement_repo.attach_document(conn, fence, document_id, agreement_id)
            chunked += _write_chunks(ctx, conn, fence, document_id, body, sections)

    with ctx.engine.begin() as conn:
        event_repo.append(
            conn,
            pile_id=state["pile_id"],
            run_id=state["run_id"],
            type="stage.ingest.done",
            payload={"documents": len(state["document_ids"]), "chunks": chunked},
        )
    return {}


def _write_chunks(
    ctx: RunContext,
    conn: Any,
    fence: Fence,
    document_id: str,
    body: str,
    sections: list[Any],
) -> int:
    """Chunk and replace. No longer embeds: every chunk used to go through the
    embedding model here for a column nothing read (extraction runs per section,
    the hybrid-retrieval query was never written), so the column is gone. The
    chunks stay — they carry `char_start`/`char_end` a citation resolves against,
    and `context` (the section header, in its own column so prepending it does not
    shift every offset)."""
    pieces = chunk_sections(body, sections)
    if not pieces:
        return 0

    rows = [
        {
            "ordinal": piece.ordinal,
            "section_path": piece.context_header or None,
            "char_start": piece.start,
            "char_end": piece.end,
            "raw_text": piece.text,
            "context": piece.context_header,
        }
        for piece in pieces
    ]
    return chunk_repo.replace_for_document(conn, fence, document_id, rows)


def _agreement_for(
    ctx: RunContext, fence: Fence, body: str, sections: list[Any], *, title: str
) -> str:
    """Which contract this document is part of, from its preamble alone (over a
    whole contract the NER picks up every company in an indemnity clause). Both
    degradations land here rather than raise: no NER yields no candidates, an
    unreachable judge yields all of them, and either way the document still gets
    an agreement — `unassigned_agreement` is a real bucket a human can split.
    Refusing to group is worse: an ungrouped document looks like a clean pile."""
    start, end = find_preamble(body, sections)
    preamble = body[start:end]

    candidates = extract_party_candidates(
        preamble, ctx.models.ner, threshold=ctx.settings.gliner_threshold
    )
    if candidates:
        # Party selection is a separate structured LLM call; the document-level
        # renewal may be old after preamble/NER work, so renew before it.
        ctx.renew_lease(fence)
        picked = pick_parties(preamble, candidates, ctx.llm)
    else:
        picked = []
    names = party_names(candidates, picked)
    if not names:
        LOG.info("no parties resolved for %s — grouped as unassigned", title)

    # The KIND of contract, from its own title — party set alone made an MSA and
    # an NDA between the same two companies one agreement. From the head of the
    # document, not an extracted fact, because the agreement must exist before
    # there is anywhere to put a fact.
    family = detect_family(body)

    with ctx.engine.begin() as conn:
        return agreement_repo.ensure(
            conn,
            fence,
            parties=normalize(names),
            title=title if names else None,
            family=family,
        )
