"""Claiming work.

    SELECT ... WHERE status = 'ingested' FOR UPDATE SKIP LOCKED

SKIP LOCKED is what lets N workers share one queue with no coordinator and no
double-processing.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

ORPHAN_WORK = """
        EXISTS (
              SELECT 1
                FROM claim_slots s
                JOIN agreements a ON a.id = s.agreement_id
               WHERE a.pile_id = p.id AND s.retired_at IS NULL
                 AND (
                       (    EXISTS (
                              SELECT 1 FROM facts_and_claims f
                               WHERE f.slot_id = s.id AND f.status = 'active'
                                 AND f.role <> 'superseded'
                            )
                        AND NOT EXISTS (
                              SELECT 1 FROM facts_and_claims f
                                JOIN documents d ON d.id = f.source_document_id
                               WHERE f.slot_id = s.id AND f.status = 'active'
                                 AND f.role <> 'superseded' AND d.status <> 'removed'
                            )
                        AND NOT EXISTS (
                              SELECT 1 FROM pending_decisions pd
                               WHERE pd.pile_id = a.pile_id AND pd.kind = 'orphaned_claims'
                                 AND pd.ref_id = s.id
                            )
                       )
                     )
           )
"""
"""A register row whose every source has been deleted, not yet asked about. Deleting a file
is work: without this the pile has nothing `ingested`, no worker claims it, and `Commit`
never removes the row. Terminates because raising (or rejecting) the decision drops the slot
from the predicate; `APPROVED_WORK` then carries the removal out. The middle `NOT EXISTS` is
the "all sources, not any" rule."""


APPROVED_WORK = """
        EXISTS (
              SELECT 1
                FROM pending_decisions d
                JOIN claim_slots s ON s.id = d.ref_id
                JOIN agreements a ON a.id = s.agreement_id
               WHERE d.pile_id = p.id AND a.pile_id = p.id
                 AND d.status = 'approved'
                 AND d.kind IN ('conflict', 'amendment', 'finding',
                                'label_change', 'orphaned_claims')
                 AND s.retired_at IS NULL
                 AND NOT EXISTS (
                       SELECT 1 FROM deliverable_commits c
                        WHERE c.pile_id = d.pile_id AND d.id = ANY(c.decision_ids)
                     )
           )
"""
"""An approval nobody has carried out yet — also work. Normally the gate handles it, but a
decision approved on an idle pile (raised after the gate, or answered out of order) is left
unclaimed. Terminates because the commit that carries it writes a `deliverable_commits`
row, which drops the decision from the predicate."""


CLAIMABLE_SQL = f"""
    SELECT p.id
      FROM piles p
     WHERE (p.lease_expires_at IS NULL OR p.lease_expires_at < now())
       AND p.status NOT IN ('awaiting_review', 'parked', 'error')
       AND (
             EXISTS (
                   SELECT 1 FROM documents d
                    WHERE d.pile_id = p.id AND d.status = 'ingested'
                      AND d.alias_of_document_id IS NULL
                 )
             OR EXISTS (
                 SELECT 1 FROM stage_events changed
                 WHERE changed.pile_id = p.id AND changed.type = 'run.sources_changed'
                 AND changed.id > COALESCE((SELECT max(done.id) FROM stage_events done
                     WHERE done.pile_id = p.id AND done.type = 'run.coverage_updated'), 0)
             )
             OR {ORPHAN_WORK}
             OR {APPROVED_WORK}
           )
     ORDER BY p.updated_at
       FOR UPDATE OF p SKIP LOCKED
     LIMIT 1
"""

CLAIMABLE = text(CLAIMABLE_SQL)
"""One pile with work, not currently held. Kept as a string too so `CLAIM_AND_LEASE` can
embed it as a CTE — one definition of "a pile worth running". `FOR UPDATE OF p SKIP LOCKED`
is the whole coordinator: a locked row is skipped, so N workers never collide; `ORDER BY
updated_at` keeps a busy pile from starving a quiet one. `awaiting_review`/`parked`/`error`
are skipped — a paused graph still has `ingested` docs, and only the approval path may
resume it."""

INGESTED_DOCUMENTS = text("""
    SELECT id
      FROM documents
     WHERE pile_id = :pile_id
       AND status = 'ingested'
       AND alias_of_document_id IS NULL
       AND attempts < :max_attempts
     ORDER BY created_at
""")
"""`alias_of_document_id IS NULL` matters: an alias holds the same extracted text as its
original, so returning both would extract one contract twice and report the copies as
agreeing — the duplicate `text_sha256` exists to prevent."""


CLAIM_AND_LEASE = text(f"""
    WITH candidate AS ({CLAIMABLE_SQL})
    UPDATE piles p
       SET lease_owner_id   = :worker_id,
           lease_epoch      = p.lease_epoch + 1,
           lease_expires_at = now() + make_interval(secs => :ttl_s),
           status           = 'running',
           updated_at       = now()
      FROM candidate c
     WHERE p.id = c.id
 RETURNING p.id, p.lease_epoch
""")
"""Claim a pile, take its lease and mark it running, in one statement. Not a race fix (two
statements are already safe via `lease.ACQUIRE`'s expiry predicate) but it removes two
costs: a claimed-but-unowned tick that reports "nothing to do", and a third `status` write
whose crash window leaves the pile showing `idle` mid-run."""


def claim_and_lease(conn: Connection, worker_id: str, ttl_s: int) -> tuple[str, int] | None:
    """`(pile_id, epoch)` for a pile this worker now owns, or None — None is ordinary, most
    attempts on a shared queue find nothing."""
    row = conn.execute(CLAIM_AND_LEASE, {"worker_id": worker_id, "ttl_s": ttl_s}).first()
    return (str(row.id), int(row.lease_epoch)) if row else None


def claim_pile(conn: Connection, worker_id: str) -> str | None:
    """Which pile has work, WITHOUT taking it. Read-only — the row lock dies with its
    transaction, so anything that runs a pile calls `claim_and_lease`. Used by the health
    endpoint and tests."""
    row = conn.execute(CLAIMABLE).first()
    return str(row.id) if row else None


def ingested_documents(conn: Connection, pile_id: str, *, max_attempts: int = 3) -> list[str]:
    """Every ingested ORIGINAL in the pile, so a 4-document first build is one run with one
    review queue. Aliases excluded (text already read); `attempts < max_attempts` stops a
    poison file retrying for ever."""
    rows = conn.execute(
        INGESTED_DOCUMENTS, {"pile_id": pile_id, "max_attempts": max_attempts}
    ).all()
    return [str(row.id) for row in rows]
