"""One structured LLM call per section, then Gate 1. The bulk of the spend.

Extract and Ground are one node: two nodes communicate only through the
checkpointed state, which forbids business data, so a batch of candidate facts
would be serialised into Postgres between them — or need a `candidate_facts`
table holding unverified claims one join from the real one. So `provenance/
extract.py` runs the model and grounds in the same call; a candidate arrives here
already a `GroundedFact` or a `Rejection`, never in between.

Rejections are counted, not swallowed: a section that stated nothing and one
whose every claim was refused look identical, and the difference is the only
signal that extraction is working.

See docs/BACKEND_INTERNALS.md § "Nodes — what each reads and writes".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.runnables import RunnableConfig
from sqlalchemy import text as sql

from documents_analyst.db.repo import agreements as agreement_repo
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import findings as finding_repo
from documents_analyst.graph.context import context_of
from documents_analyst.graph.state import PileRunState
from documents_analyst.worker.lease import Fence
from documents_analyst_core.parsing.sections import split_sections
from documents_analyst_core.provenance.extract import extract_facts
from documents_analyst_core.provenance.injection import find_instructions
from documents_analyst_core.provenance.open_subjects import OpenSubjectResult, extract_open_subjects
from documents_analyst_core.telemetry.sink import timed

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _Usage:
    """`extract_facts` returns aggregate totals across its section calls, not a
    `Usage`. This carries them into the same recording path a single call takes,
    rather than a second way of building a `stage_runs` row."""

    model: str
    input_tokens: int
    output_tokens: int
    attempts: int


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)
    fence = Fence(pile_id=state["pile_id"], epoch=state["lease_epoch"])
    grounded = rejected = subjects = 0

    for document_id in state["document_ids"]:
        ctx.renew_lease(fence)
        with ctx.engine.begin() as conn:
            row = conn.execute(
                sql(
                    "SELECT filename, extracted_text FROM documents "
                    "WHERE id = :id AND extracted_text IS NOT NULL"
                ),
                {"id": document_id},
            ).first()
            agreement_id = agreement_repo.for_document(conn, document_id)
            already = conn.execute(
                sql("SELECT count(*) FROM facts_and_claims WHERE source_document_id = :id"),
                {"id": document_id},
            ).scalar_one()

        if row is None or agreement_id is None:
            # No agreement means Ingest skipped this document (removed,
            # superseded, or arrived after the batch). A fact with no agreement is
            # one nothing will ever compare.
            continue

        if already:
            # Already read: re-reading ADDS, because `replace_for_document`
            # deletes only unmatched facts (a matched one may be the register's
            # current value or half of a live decision). So a second pass inserts
            # a copy, and the register starts saying "USD 10,000 (in 2 documents)"
            # about one file. `extracted_text` is write-once, so there is nothing
            # to gain and an LLM bill to pay. Reached by a restored file or a
            # crash that left a matched document at `ingested`.
            with ctx.engine.begin() as conn:
                doc_repo.mark_extracted(conn, fence, document_id)
            LOG.info("%s already has facts; not extracting it again", document_id)
            continue

        body = row.extracted_text
        # Model calls are made OUTSIDE any transaction: one call per section is
        # seconds, and holding a row lock across that is the parse mistake again.
        with timed() as elapsed:
            result = extract_facts(
                document_id=document_id,
                text=body,
                sections=split_sections(body),
                llm=ctx.llm,
                max_sections=ctx.max_sections,
                document_title=row.filename,
                before_section=lambda: ctx.renew_lease(fence),
            )
        # One record per document. The timing wraps the whole loop, so
        # `stage_runs.latency_ms` answers "why is this pile slow" (per-call timings
        # would hide that there were forty sections). `answered` is False when
        # every section failed: `extract_facts` turns an `LLMError` into a
        # `Rejection`, so a quota-exhausted run looks like a contract that stated
        # nothing, and `attempts == 0` over a document with sections separates them.
        answered = result.attempts > 0 or result.sections_seen == 0
        ctx.record(
            "extract",
            _Usage(
                model=str(getattr(ctx.llm, "model", "")),
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                attempts=max(result.attempts, 1),
            ),
            latency_ms=elapsed(),
            ok=answered,
        )
        if not answered:
            LOG.warning(
                "%s: every section call failed — %s",
                row.filename,
                result.rejected[0].reason if result.rejected else "no reason recorded",
            )

        ctx.renew_lease(fence)
        # The second pass: material terms the 28-key taxonomy has no room for. A
        # separate call and schema keeps the taxonomy closed — these land in
        # `subject_free`, matched by embedding, never by key.
        open_result = _open_subjects(ctx, fence, document_id, body, row.filename)

        ctx.renew_lease(fence)
        subject_vectors = _subject_embeddings(ctx, open_result.subjects)
        with ctx.engine.begin() as conn:
            written = fact_repo.replace_for_document(
                conn,
                fence,
                document_id=document_id,
                agreement_id=agreement_id,
                facts=result.facts,
            )
            opened = fact_repo.add_open_subjects(
                conn,
                fence,
                document_id=document_id,
                agreement_id=agreement_id,
                subjects=open_result.subjects,
                embeddings=subject_vectors,
                embedding_model=_embedding_model(ctx),
            )
            # Recorded whichever way it went, and recorded here rather than left
            # in a log line, because the audit stage is what needs it: an
            # `assert: exists` rule concludes from absence, and absence caused by
            # a failed model call is not the same claim as absence in the
            # contract. Written on success too, so a document that failed on an
            # earlier run and succeeded on this one stops being treated as
            # incomplete.
            _report_instructions(
                conn,
                fence,
                agreement_id=agreement_id,
                document_id=document_id,
                filename=row.filename,
                text=body,
            )
            doc_repo.set_extraction_ok(conn, fence, document_id, ok=answered)
            if answered:
                # Only when the model answered. Marking `extracted` after every
                # call failed would retire the document having learned nothing,
                # and the pile would go quiet holding a contract nobody read.
                doc_repo.mark_extracted(conn, fence, document_id)
            event_repo.append(
                conn,
                pile_id=state["pile_id"],
                run_id=state["run_id"],
                type="stage.extract.document",
                payload={
                    "document_id": document_id,
                    "filename": row.filename,
                    "sections": result.sections_seen,
                    "grounded": written,
                    "open_subjects": opened,
                    "rejected": len(result.rejected),
                },
            )
        grounded += written
        subjects += opened
        rejected += len(result.rejected)

        for rejection in result.rejected:
            LOG.info("dropped %s from %s: %s", rejection.field_key, row.filename, rejection.reason)

    with ctx.engine.begin() as conn:
        ctx.telemetry.flush(conn)
        event_repo.append(
            conn,
            pile_id=state["pile_id"],
            run_id=state["run_id"],
            type="stage.extract.done",
            payload={"grounded": grounded, "open_subjects": subjects, "rejected": rejected},
        )
    return {}


def _open_subjects(
    ctx: Any, fence: Fence, document_id: str, body: str, filename: str
) -> OpenSubjectResult:
    """The escape-hatch pass. A failure here costs subjects, never the document —
    taxonomy facts are the deliverable and open subjects the supplement, so an
    "anything else?" rate limit must not lose a contract's grounded terms."""
    with timed() as elapsed:
        result = extract_open_subjects(
            document_id=document_id,
            text=body,
            sections=split_sections(body),
            llm=ctx.llm,
            max_sections=ctx.max_sections,
            document_title=filename,
            before_section=lambda: ctx.renew_lease(fence),
        )
    ctx.record(
        "extract_open",
        _Usage(
            model=str(getattr(ctx.llm, "model", "")),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            attempts=max(result.attempts, 1),
        ),
        latency_ms=elapsed(),
        ok=result.attempts > 0 or result.sections_seen == 0,
    )
    if result.truncated:
        LOG.warning(
            "%s: %d open subjects over the per-document cap were dropped",
            filename,
            result.truncated,
        )
    return result


def _subject_embeddings(ctx: Any, subjects: list[Any]) -> list[list[float]]:
    """Embed the SUBJECT, not the clause. Net 2 matches "data residency region"
    to "data storage location"; embedding the sentence would score them apart on
    everything the sentences do not share, which is most of it."""
    if not subjects:
        return []
    return _embed(ctx, [s.subject for s in subjects])


def _report_instructions(
    conn: Any,
    fence: Fence,
    *,
    agreement_id: str,
    document_id: str,
    filename: str,
    text: str,
) -> None:
    """Text in this document aimed at us, reported as a finding — never obeyed,
    and it changes nothing. The facts were already extracted from text wrapped in
    `<untrusted-…>` tags; this defence is unchanged. What is new is that the
    reviewer is told: the register carries the sentence verbatim at its offsets,
    like every other claim. Cleared when it stops matching, so a corrected file
    stops being accused."""
    hits = find_instructions(text)
    rule_id = f"instructions_in_document:{filename}"
    if not hits:
        finding_repo.clear(conn, fence, agreement_id=agreement_id, rule_ids=[rule_id])
        return

    LOG.warning(
        "%s contains %d instruction-shaped span(s); reported, not obeyed", filename, len(hits)
    )
    finding_repo.record(
        conn,
        fence,
        agreement_id=agreement_id,
        rule_id=rule_id,
        severity="high",
        evaluated_by="scan",
        message=(
            f"{filename} contains text addressed to this system rather than to a party "
            f"({', '.join(sorted({hit.pattern for hit in hits}))}). It was read as document "
            "content and obeyed nowhere — no fact, decision or register entry came from it. "
            "Check who sent this file."
        ),
        citations=[
            {
                "document_id": str(document_id),
                "filename": filename,
                "char_start": hit.start,
                "char_end": hit.end,
                "quote": hit.quote,
            }
            for hit in hits
        ],
    )


def _embedding_model(ctx: Any) -> str:
    """Stored beside every vector this node writes. Read off the provider, not
    settings, so it names the model that produced the numbers, not the configured
    one — they differ until a restart picks up an edited `.env`, and that window
    is when a mixed-space pile is created."""
    return str(getattr(ctx.models.embeddings, "model_name", "") or "")


def _embed(ctx: Any, texts: list[str]) -> list[list[float]]:
    """Embed, and record what it cost. Embeddings are a billed API call now, and a
    cost report showing a billable call as free is lying, so the batch lands in
    `stage_runs` beside the node's extraction calls. `last_usage` is None for a
    provider that bills nothing."""
    with timed() as elapsed:
        vectors = ctx.models.embeddings.embed_documents(texts)
    usage = getattr(ctx.models.embeddings, "last_usage", None)
    if usage is not None:
        ctx.record("extract", usage, latency_ms=elapsed())
    return vectors
