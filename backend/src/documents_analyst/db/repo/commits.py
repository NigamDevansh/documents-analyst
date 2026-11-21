"""What reached the final document, in which version, and why.

Two tables, both written in the same transaction as the document write and the version
bump (`graph/nodes/commit.py`):

- `deliverable_commits` — one row per commit: the decisions it carried and the document
  hash it produced. "Approved but not yet in the document" is a `NOT EXISTS` against this
  table (`decisions.approved_undecided`), so an approval can never be applied twice.
- `deliverable_versions` — one row per section that changed. A section with no row in a
  version was not touched by it, and its hash in `piles.deliverable_sections` proves it.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence, LostLease, fenced

RECORD = text(f"""
    INSERT INTO deliverable_commits (pile_id, decision_ids, slot_ids, version,
                                     document_sha256, lease_epoch)
         SELECT :pile_id, CAST(:decision_ids AS uuid[]), CAST(:slot_ids AS uuid[]),
                :version, :sha, :epoch
          WHERE {HOLDS_LEASE}
      RETURNING id
""")


LOCK = text(f"SELECT id FROM piles WHERE id = :pile_id AND {HOLDS_LEASE} FOR UPDATE")


def lock_for_write(conn: Connection, fence: Fence) -> None:
    """Hold the pile's row lock from here to the end of the transaction, and only while
    the lease is ours. Taken before the document file is written: acquiring a lease is an
    UPDATE of this row, so no successor can take the pile — and write its own document —
    between our file write and our commit. Without it a worker that lost its lease a
    moment after renewing could overwrite a file its successor had already verified."""
    if conn.execute(LOCK, {"pile_id": fence.pile_id, "epoch": fence.epoch}).first() is None:
        raise LostLease(f"lease lost before writing the final document for pile {fence.pile_id}")


def record_commit(
    conn: Connection,
    fence: Fence,
    *,
    decision_ids: list[str],
    slot_ids: list[str],
    version: int,
    document_sha256: str,
) -> str:
    row = conn.execute(
        RECORD,
        {
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
            "decision_ids": decision_ids,
            "slot_ids": slot_ids,
            "version": version,
            "sha": document_sha256,
        },
    ).first()
    if row is None:
        raise LostLease(f"lease lost before recording a commit for pile {fence.pile_id}")
    return str(row.id)


def record_section_change(
    conn: Connection,
    *,
    pile_id: str,
    version: int,
    slot_id: str | None,
    label: str,
    operation: str,
    before_text: str | None,
    after_text: str | None,
    before_sha256: str | None,
    after_sha256: str | None,
    reason: str,
    decision_id: str | None,
) -> None:
    conn.execute(
        text(
            "INSERT INTO deliverable_versions (pile_id, version, changed_slot_id, section_label, "
            "operation, before_text, after_text, before_sha256, after_sha256, reason, decision_id) "
            "VALUES (:p, :v, CAST(:slot AS uuid), :label, :op, :before, :after, :bsha, :asha, "
            ":reason, CAST(:decision AS uuid))"
        ),
        {
            "p": pile_id,
            "v": version,
            "slot": slot_id,
            "label": label,
            "op": operation,
            "before": before_text,
            "after": after_text,
            "bsha": before_sha256,
            "asha": after_sha256,
            "reason": reason,
            "decision": decision_id,
        },
    )


def publish(
    conn: Connection,
    fence: Fence,
    *,
    path: str,
    document_sha256: str,
    sections: list[dict[str, str]],
    bump: bool,
) -> int:
    """Record the document as written, fenced. `bump=False` is Resolve repairing a file to
    an already-recorded version: same content, same version number. Returns the version."""
    row = fenced(
        conn,
        text(
            "UPDATE piles SET deliverable_path = :path, deliverable_sha256 = :sha, "
            "deliverable_sections = CAST(:sections AS jsonb), "
            "deliverable_version = deliverable_version + :inc, updated_at = now() "
            f"WHERE id = :pile_id AND {HOLDS_LEASE} RETURNING deliverable_version"
        ),
        {
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
            "path": path,
            "sha": document_sha256,
            "sections": json.dumps(sections, ensure_ascii=False),
            "inc": 1 if bump else 0,
        },
    ).first()
    assert row is not None  # `fenced` raised otherwise
    return int(row.deliverable_version)


def recorded(conn: Connection, pile_id: str) -> dict[str, Any]:
    row = conn.execute(
        text(
            "SELECT name, deliverable_path, deliverable_sha256, deliverable_sections, "
            "deliverable_version FROM piles WHERE id = :p"
        ),
        {"p": pile_id},
    ).one()
    return dict(row._mapping)


def history(conn: Connection, pile_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every section change, newest first — "what changed, when, and because of which
    decision". The source documents behind a change are on the slot's facts."""
    rows = conn.execute(
        text(
            "SELECT v.version, v.changed_slot_id AS slot_id, v.section_label, v.operation, "
            "v.before_text, v.after_text, v.before_sha256, v.after_sha256, v.reason, "
            "v.decision_id, d.decided_by, v.created_at "
            "FROM deliverable_versions v "
            "LEFT JOIN pending_decisions d ON d.id = v.decision_id "
            "WHERE v.pile_id = :p ORDER BY v.version DESC, v.created_at DESC LIMIT :limit"
        ),
        {"p": pile_id, "limit": limit},
    ).all()
    return [dict(row._mapping) for row in rows]


def counts_for_pile(conn: Connection, pile_id: str) -> dict[str, int]:
    row = conn.execute(
        text(
            "SELECT count(*) AS commits, coalesce(max(version), 0) AS latest_version "
            "FROM deliverable_commits WHERE pile_id = :p"
        ),
        {"p": pile_id},
    ).one()
    return {"commits": int(row.commits), "latest_version": int(row.latest_version)}
