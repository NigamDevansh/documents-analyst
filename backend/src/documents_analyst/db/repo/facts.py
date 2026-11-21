"""Repository: facts. Every row here passed Gate 1 before it arrived.

This module deliberately can't write an ungrounded fact: the columns it fills come from
a `GroundedFact`, which only exists on the far side of the verbatim check in
`provenance/extract.py`. There's no "insert a claim without a quote" path to forget to
guard, because there's no such signature.

Replace per document, like chunks. Re-extraction on a resumed run must not double the
facts, and a fact's natural key — document, offsets, field — isn't unique enough to
upsert on: two runs of the same model can legitimately produce the same claim from
overlapping offsets.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence, fenced
from documents_analyst_core.provenance.models import GroundedFact

DELETE_FOR_DOCUMENT = text(f"""
    DELETE FROM facts_and_claims
     WHERE source_document_id = :document_id
       AND slot_id IS NULL
       AND {HOLDS_LEASE}
""")
"""`slot_id IS NULL` makes re-extraction safe: a fact matched into a slot has been reasoned
about — maybe the current value, maybe half of a conflict under review — so deleting it on
re-extract would withdraw a claim someone is mid-decision on. Only unmatched facts go."""

INSERT = text(f"""
    INSERT INTO facts_and_claims (agreement_id, field_key, value, value_norm,
                                  source_document_id, char_start, char_end, source_quote)
         SELECT :agreement_id, :field_key, CAST(:value AS jsonb), :value_norm,
                :document_id, :char_start, :char_end, :source_quote
          WHERE {HOLDS_LEASE}
      RETURNING id
""")
"""No vector. A taxonomy fact is matched by `field_key`, exactly and for free, and nothing
ever compared its embedding — writing one embedded every quote on every run to fill a column
no query reads. Migration 021 nulls the leftovers."""


def replace_for_document(
    conn: Connection,
    fence: Fence,
    *,
    document_id: str,
    agreement_id: str,
    facts: list[GroundedFact],
) -> int:
    """Every unmatched fact for one document, atomically swapped. `value` is JSON, not text,
    because the taxonomy has kinds (money, date, duration); `value_norm` beside it is the
    comparable form, `NULL` for free text — normalising prose invents structure a conflict
    check would later treat as fact."""
    conn.execute(
        DELETE_FOR_DOCUMENT,
        {"document_id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )
    written = 0
    for fact in facts:
        conn.execute(
            INSERT,
            {
                "agreement_id": agreement_id,
                "field_key": fact.field_key,
                "value": json.dumps({"text": fact.value_text, "confidence": fact.confidence}),
                "value_norm": fact.value_normalized,
                "document_id": document_id,
                "char_start": fact.citation.start,
                "char_end": fact.citation.end,
                "source_quote": fact.citation.quote,
                "pile_id": fence.pile_id,
                "epoch": fence.epoch,
            },
        )
        written += 1
    return written


INSERT_OPEN = text(f"""
    INSERT INTO facts_and_claims (agreement_id, subject_free, value, value_norm,
                                  source_document_id, char_start, char_end, source_quote,
                                  fact_key_embedding, embedding_model)
         SELECT :agreement_id, :subject, CAST(:value AS jsonb), NULL,
                :document_id, :char_start, :char_end, :source_quote,
                CAST(:embedding AS vector), :embedding_model
          WHERE {HOLDS_LEASE}
      RETURNING id
""")
"""`value_norm` and `field_key` both NULL on purpose: a subject the taxonomy doesn't cover has
no `ValueKind`, so there's nothing to normalise to. The schema's `CHECK` that one of
`field_key`/`subject_free` is set keeps a fact from being neither."""


def add_open_subjects(
    conn: Connection,
    fence: Fence,
    *,
    document_id: str,
    agreement_id: str,
    subjects: list[Any],
    embeddings: list[list[float]] | None = None,
    embedding_model: str | None = None,
) -> int:
    """Material terms with no taxonomy key, matched by embedding. Written by the same
    `replace_for_document` sweep — the DELETE keys on `slot_id IS NULL`, and an unmatched open
    subject is as re-derivable as an unmatched taxonomy fact."""
    written = 0
    for index, subject in enumerate(subjects):
        vector = embeddings[index] if embeddings and index < len(embeddings) else None
        conn.execute(
            INSERT_OPEN,
            {
                "agreement_id": agreement_id,
                "subject": subject.subject,
                "value": json.dumps({"text": subject.value_text}),
                "document_id": document_id,
                "char_start": subject.citation.start,
                "char_end": subject.citation.end,
                "source_quote": subject.citation.quote,
                "embedding": str(list(vector)) if vector is not None else None,
                "embedding_model": embedding_model if vector is not None else None,
                "pile_id": fence.pile_id,
                "epoch": fence.epoch,
            },
        )
        written += 1
    return written


def unmatched_for_agreement(conn: Connection, agreement_id: str) -> list[dict[str, Any]]:
    """Every fact in this agreement no slot has claimed yet. Ordered by document then offset so
    a run's output is reproducible — which makes a diff of the register meaningful, not
    incidental."""
    rows = conn.execute(
        text(
            "SELECT f.id, f.field_key, f.subject_free, f.value, f.value_norm, "
            "f.source_document_id, f.char_start, f.char_end, f.source_quote, "
            "f.fact_key_embedding, f.embedding_model, "
            "d.filename, d.created_at AS document_created_at "
            "FROM facts_and_claims f JOIN documents d ON d.id = f.source_document_id "
            "WHERE f.agreement_id = :a AND f.status = 'active' "
            "ORDER BY d.created_at, f.char_start"
        ),
        {"a": agreement_id},
    ).all()
    return [dict(row._mapping) for row in rows]


STALE_OPEN_SUBJECTS = text("""
    SELECT id, subject_free
      FROM facts_and_claims
     WHERE agreement_id = :agreement_id
       AND status = 'active'
       AND subject_free IS NOT NULL
       AND fact_key_embedding IS NOT NULL
       AND embedding_model IS DISTINCT FROM :model
  ORDER BY created_at
""")
"""Open subjects holding a vector from a different embedder. `IS DISTINCT FROM`, not
`<>`, because the pre-020 rows the backfill didn't reach carry NULL — exactly the case
that must be repaired rather than skipped."""

REEMBED = text(f"""
    UPDATE facts_and_claims
       SET fact_key_embedding = CAST(:embedding AS vector),
           embedding_model    = :model
     WHERE id = :id AND {HOLDS_LEASE}
""")


def stale_open_subjects(conn: Connection, agreement_id: str, *, model: str) -> list[dict[str, Any]]:
    rows = conn.execute(STALE_OPEN_SUBJECTS, {"agreement_id": agreement_id, "model": model}).all()
    return [dict(row._mapping) for row in rows]


def reembed(
    conn: Connection,
    fence: Fence,
    *,
    updates: list[tuple[str, list[float]]],
    model: str,
) -> int:
    """Replace vectors in place, keeping the fact and everything decided about it. The subject
    text is still in `subject_free`, so the vector rebuilds for a fraction of a cent — where
    re-extracting would spend the whole budget and nulling would lose the matching silently."""
    for fact_id, vector in updates:
        conn.execute(
            REEMBED,
            {
                "id": fact_id,
                "embedding": str(list(vector)),
                "model": model,
                "pile_id": fence.pile_id,
                "epoch": fence.epoch,
            },
        )
    return len(updates)


def set_role(conn: Connection, fence: Fence, fact_ids: list[str], role: str) -> None:
    """`current` · `corroborating` · `competing` · `superseded` · `rejected` — a fact's
    relationship to the slot's answer, not a quality score. `corroborating` is a second
    document agreeing (no edit, no cost); `superseded` is what an approved amendment leaves,
    kept so the register can show where a replaced term came from."""
    if not fact_ids:
        return
    fenced(
        conn,
        text(f"UPDATE facts_and_claims SET role = :role WHERE id = ANY(:ids) AND {HOLDS_LEASE}"),
        {"role": role, "ids": fact_ids, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )


def count_for_pile(conn: Connection, pile_id: str) -> int:
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM facts_and_claims f JOIN documents d "
                "ON d.id = f.source_document_id WHERE d.pile_id = :pile_id"
            ),
            {"pile_id": pile_id},
        ).scalar_one()
    )


CITATIONS = text("""
    SELECT f.id, f.slot_id, s.field_key, s.subject_free, s.label_override, s.state,
           f.value, f.value_norm, f.value_group, f.role,
           f.source_document_id, d.filename, f.char_start, f.char_end, f.source_quote
      FROM facts_and_claims f
      JOIN documents d   ON d.id = f.source_document_id
 LEFT JOIN claim_slots s ON s.id = f.slot_id
     WHERE d.pile_id = :pile_id AND f.status = 'active'
  ORDER BY d.filename, f.char_start
""")
"""Claim → source, the click-a-value-see-the-sentence map. Each row carries `document_id` and
`char_start/char_end` into `documents.extracted_text`, so the highlight is ours — and those
offsets are the ones Gate 1 verified, so a drawn citation is by construction checked. `LEFT
JOIN` keeps a fact matched before Match (no slot yet) from blanking the source pane."""


def citations_for_pile(conn: Connection, pile_id: str) -> list[dict[str, object]]:
    """Every live claim in the pile with the span that proves it."""
    return [dict(row._mapping) for row in conn.execute(CITATIONS, {"pile_id": pile_id}).all()]


def open_subject_counts(conn: Connection, pile_id: str) -> list[dict[str, object]]:
    """Recurring `subject_free` concepts — the taxonomy-gap report. Recording a subject the 28
    keys can't express isn't a failure, but the SAME one across several agreements is evidence
    of a missing field. Ordered by how often it recurs, the order to grow the taxonomy in."""
    rows = conn.execute(
        text(
            "SELECT f.subject_free, count(*) AS facts, "
            "count(DISTINCT f.agreement_id) AS agreements, "
            "count(DISTINCT f.source_document_id) AS documents "
            "FROM facts_and_claims f JOIN documents d ON d.id = f.source_document_id "
            "WHERE d.pile_id = :pile_id AND f.status = 'active' AND f.subject_free IS NOT NULL "
            "GROUP BY f.subject_free ORDER BY agreements DESC, facts DESC, f.subject_free"
        ),
        {"pile_id": pile_id},
    ).all()
    return [dict(row._mapping) for row in rows]


def for_document(conn: Connection, document_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        text(
            "SELECT id, field_key, value, value_norm, char_start, char_end, source_quote "
            "FROM facts_and_claims WHERE source_document_id = :id ORDER BY char_start"
        ),
        {"id": document_id},
    ).all()
    return [dict(row._mapping) for row in rows]
