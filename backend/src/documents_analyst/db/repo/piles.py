"""Repository: piles. A folder is a pile; there is no creation step."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence

UPSERT = text("""
    INSERT INTO piles (name, watch_path, rulebook_path)
         VALUES (:name, :watch_path, :rulebook_path)
    ON CONFLICT (name) DO UPDATE
            SET watch_path    = EXCLUDED.watch_path,
                rulebook_path = EXCLUDED.rulebook_path,
                updated_at    = now()
      RETURNING id
""")
"""Idempotent by name. Several workers scan independently and all find the same new
folder; `ON CONFLICT` collapses that into one row instead of a race — the losing insert
updates rather than raising, so no worker has to coordinate to discover a pile."""


def ensure(conn: Connection, *, name: str, watch_path: Path) -> str:
    """The pile for this folder, creating it on first sight. `rulebook.yaml` is looked
    for here rather than at run time so adding one to an existing pile takes effect on the
    next scan — the alternative is a config reload nobody would remember to trigger."""
    rulebook = watch_path / "rulebook.yaml"
    row = conn.execute(
        UPSERT,
        {
            "name": name,
            "watch_path": str(watch_path),
            "rulebook_path": str(rulebook) if _is_file(rulebook) else None,
        },
    ).one()
    return str(row.id)


def _is_file(path: Path) -> bool:
    """`Path.is_file()` forgives a missing file but raises on an unreadable one. That matters:
    this runs before every scan, so one unreadable folder would abandon every OTHER pile in the
    pass. A rulebook we can't see is one we don't have."""
    try:
        return path.is_file()
    except OSError:
        return False


def set_status(conn: Connection, fence: Fence, status: str) -> bool:
    """Set a status only while this worker still owns the pile. The error handler calls this
    after work failed, when the lease may have expired and a successor may hold the pile — an
    unfenced update would overwrite its state. `False` is the expected stale outcome, not a
    second failure to raise."""
    result = conn.execute(
        text(
            "UPDATE piles SET status = :status, updated_at = now() "
            f"WHERE id = :pile_id AND {HOLDS_LEASE}"
        ),
        {"pile_id": fence.pile_id, "epoch": fence.epoch, "status": status},
    )
    return result.rowcount == 1


def get(conn: Connection, pile_id: str) -> dict[str, object] | None:
    row = conn.execute(
        text(
            "SELECT id, name, watch_path, rulebook_path, status, lease_epoch, "
            "deliverable_path, deliverable_sha256, deliverable_version "
            "FROM piles WHERE id = :id"
        ),
        {"id": pile_id},
    ).first()
    return dict(row._mapping) if row else None


SUMMARIES = text("""
    SELECT p.id, p.name, p.status, p.watch_path, p.rulebook_path IS NOT NULL AS has_rulebook,
           p.deliverable_sha256, p.deliverable_version, p.updated_at,
           (p.lease_expires_at IS NOT NULL AND p.lease_expires_at > now()) AS leased,
           coalesce(d.total, 0)      AS documents,
           coalesce(d.quarantined, 0) AS quarantined,
           coalesce(q.pending, 0)     AS pending_decisions,
           coalesce(s.disputed, 0)    AS open_conflicts,
           coalesce(f.open, 0)        AS open_findings
      FROM piles p
 LEFT JOIN (SELECT pile_id, count(*) AS total,
                   count(*) FILTER (WHERE status = 'quarantined') AS quarantined
              FROM documents WHERE alias_of_document_id IS NULL GROUP BY pile_id) d
        ON d.pile_id = p.id
 LEFT JOIN (SELECT pile_id, count(*) AS pending
              FROM pending_decisions WHERE status = 'pending' GROUP BY pile_id) q
        ON q.pile_id = p.id
 LEFT JOIN (SELECT a.pile_id, count(*) AS disputed
              FROM claim_slots c JOIN agreements a ON a.id = c.agreement_id
             WHERE c.state IN ('disputed', 'contested') GROUP BY a.pile_id) s
        ON s.pile_id = p.id
 LEFT JOIN (SELECT a.pile_id, count(*) AS open
              FROM findings fi JOIN agreements a ON a.id = fi.agreement_id
             WHERE fi.status = 'open' GROUP BY a.pile_id) f
        ON f.pile_id = p.id
  ORDER BY p.name
""")
"""The navigator, in one query. Subqueries, not four joins onto the base rows: counting
documents and decisions in one join would multiply them (two docs × three decisions → six
each). Grouping first keeps each number its own count. `LEFT JOIN` throughout because a pile
with no documents is still a pile."""


def summaries(conn: Connection) -> list[dict[str, object]]:
    """Every pile with the numbers the navigator shows beside it."""
    return [dict(row._mapping) for row in conn.execute(SUMMARIES).all()]


RESUMABLE = text("""
    SELECT p.id
      FROM piles p
     WHERE p.status = 'awaiting_review'
       AND (p.lease_expires_at IS NULL OR p.lease_expires_at < now())
       AND NOT EXISTS (SELECT 1 FROM pending_decisions d
                        WHERE d.pile_id = p.id AND d.status = 'pending')
     ORDER BY p.updated_at
""")
"""A paused pile whose queue is empty — the resume condition, not "something was approved". The
gate re-interrupts while anything is pending, so resuming a half-reviewed pile would just pause
again. Empty, not approved: a reviewer who rejects everything has still finished, and that run
must reach the end. Read-only — `resume_after_approval` takes the lease itself."""


def resumable(conn: Connection) -> list[str]:
    """Piles waiting at the gate with nothing left to decide."""
    return [str(row.id) for row in conn.execute(RESUMABLE).all()]


def notify_work(conn: Connection, pile_id: str) -> None:
    """ "Something changed in this pile — look sooner." The only `NOTIFY` a request handler
    sends directly (the other publisher is a `pending_decisions` trigger). It lives here, not
    the route, so `analyst/api/` holds no SQL — a test enforces that. Delivered on commit, never
    on rollback, so the worker can't wake for a file that wasn't written."""
    conn.execute(text("SELECT pg_notify('analyst_work', :pile_id)"), {"pile_id": pile_id})
