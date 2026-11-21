"""Repository: agreements. The key that decides what gets compared.

An agreement is a set of parties, not a document. Two documents belong to the
same agreement when their normalised party sets match, and that grouping is
what makes a conflict a conflict: "Acme says Net 30, Acme says Net 45" is a
disagreement, while "Acme says Net 30, Cobalt says Net 45" is two contracts
doing their jobs.

Getting it wrong fails silently and in both directions — too coarse and unrelated
contracts start conflicting, too fine and a real disagreement is never noticed because
the two documents were never compared. So the normalisation lives in one place
(`entities/parties.normalize`) and is stored as an array Postgres can enforce
uniqueness on.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence, LostLease

UPSERT = text(f"""
    INSERT INTO agreements (pile_id, title, parties_normalized, external_ref, family)
         SELECT :pile_id, :title, CAST(:parties AS text[]), :external_ref, :family
          WHERE {HOLDS_LEASE}
    ON CONFLICT (pile_id, parties_normalized, external_ref, family) DO UPDATE
            SET title = COALESCE(agreements.title, EXCLUDED.title)
      RETURNING id
""")
"""Idempotent on the party set, so a second document joins an existing agreement instead of
starting a rival. `DO UPDATE`, not `DO NOTHING`, so the id comes back either way. `COALESCE`
keeps the FIRST title: a later document is usually an amendment, and its name shouldn't rename
the whole group."""


ADOPTIVE_PARENT = text("""
    SELECT id FROM agreements
     WHERE pile_id = :pile_id AND parties_normalized = CAST(:parties AS text[])
       AND external_ref IS NOT DISTINCT FROM :external_ref
       AND family IS NOT NULL
  ORDER BY created_at
     LIMIT 2
""")
"""The agreement a dependent document should join. An amendment or side letter isn't a kind of
contract; it modifies one, and when its title doesn't name the parent this finds the standalone
agreement for the same parties. `LIMIT 2` to detect ambiguity — two matches (an MSA and an NDA)
means it gets its own unfamilied agreement, not a guess."""


def ensure(
    conn: Connection,
    fence: Fence,
    *,
    parties: tuple[str, ...],
    title: str | None = None,
    external_ref: str | None = None,
    family: str | None = None,
) -> str:
    """The agreement for this party set, creating it on first sight. An empty party set is
    allowed: NER may be off or unreachable, and a document whose parties we couldn't read must
    still be extractable — they land in one per-pile bucket `unassigned_agreement` lets a human
    split later."""
    if family is None:
        # A document that doesn't stand on its own joins its parent. Without this an "Amendment
        # No. 2" that never names its parent would start a rival agreement beside the one it
        # amends.
        candidates = conn.execute(
            ADOPTIVE_PARENT,
            {
                "pile_id": fence.pile_id,
                "parties": list(parties),
                "external_ref": external_ref,
            },
        ).all()
        if len(candidates) == 1:
            return str(candidates[0].id)

    row = conn.execute(
        UPSERT,
        {
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
            "title": title,
            "parties": list(parties),
            "external_ref": external_ref,
            "family": family,
        },
    ).first()
    if row is None:
        raise LostLease(
            f"agreement upsert affected 0 rows — epoch {fence.epoch} is stale "
            f"for pile {fence.pile_id}"
        )
    return str(row.id)


def attach_document(conn: Connection, fence: Fence, document_id: str, agreement_id: str) -> None:
    """Point a document at its agreement. Fenced like every ingestion write, and
    re-runnable: a resumed run sets the same value, which makes the Ingest node safe to
    re-enter after a crash without checking whether it already ran."""
    conn.execute(
        text(
            "UPDATE documents SET agreement_id = :agreement_id, updated_at = now() "
            f"WHERE id = :id AND pile_id = :pile_id AND {HOLDS_LEASE}"
        ),
        {
            "id": document_id,
            "agreement_id": agreement_id,
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    )


def for_document(conn: Connection, document_id: str) -> str | None:
    row = conn.execute(
        text("SELECT agreement_id FROM documents WHERE id = :id"), {"id": document_id}
    ).first()
    return str(row.agreement_id) if row and row.agreement_id else None
