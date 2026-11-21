"""Repository: findings. Rulebook verdicts, and the dismissals that outlive them.

`UNIQUE (agreement_id, rule_id)` is the whole design of this table. The audit re-runs on
every pass — it has to, because approvals can CREATE violations — so without that
constraint every pass would insert the same finding again and a reviewer would watch a
list they'd cleared grow back.

A dismissal survives re-evaluation. The upsert refreshes a finding's message and evidence
but never resets its `status`, so "I looked at this and it's fine" is a decision the
system remembers. The inverse — a finding that stops being true — is handled by deleting
it, because a resolved violation should leave the list rather than sit there marked open.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence

UPSERT = text(f"""
    INSERT INTO findings (agreement_id, rule_id, severity, evaluated_by, message, citations)
         SELECT :agreement_id, :rule_id, :severity, :evaluated_by, :message,
                CAST(:citations AS jsonb)
          WHERE {HOLDS_LEASE}
    ON CONFLICT (agreement_id, rule_id) DO UPDATE
            SET severity = EXCLUDED.severity,
                message = EXCLUDED.message,
                citations = EXCLUDED.citations,
                updated_at = now()
      RETURNING id, status
""")
"""Note what `DO UPDATE` deliberately leaves alone: `status`. Refreshing message and
citations keeps a standing finding accurate as the pile changes; resetting the status
would resurrect something a person dismissed, on every audit pass, with no way to make it
stop."""


def record(
    conn: Connection,
    fence: Fence,
    *,
    agreement_id: str,
    rule_id: str,
    severity: str,
    evaluated_by: str,
    message: str,
    citations: list[dict[str, Any]],
) -> tuple[str, str] | None:
    """`(finding_id, status)`, or None if the lease is gone. The status distinguishes a NEW
    finding from a standing, already-dismissed one — only the first should reach the review
    queue."""
    row = conn.execute(
        UPSERT,
        {
            "agreement_id": agreement_id,
            "rule_id": rule_id,
            "severity": severity,
            "evaluated_by": evaluated_by,
            "message": message,
            "citations": json.dumps(citations),
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    ).first()
    return (str(row.id), str(row.status)) if row else None


def clear(conn: Connection, fence: Fence, *, agreement_id: str, rule_ids: list[str]) -> int:
    """Delete findings for rules that now pass — deleted, not marked resolved, so the finding
    count stays meaningful. A dismissed finding goes too: the rule passes now, and if it fails
    again it comes back as new."""
    if not rule_ids:
        return 0
    result = conn.execute(
        text(
            "DELETE FROM findings WHERE agreement_id = :a AND rule_id = ANY(:ids) "
            f"AND {HOLDS_LEASE}"
        ),
        {
            "a": agreement_id,
            "ids": rule_ids,
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    )
    return int(result.rowcount or 0)


def settle(conn: Connection, finding_id: str, *, status: str, by: str) -> bool:
    """Move a finding when a reviewer answers the item that raised it — `acknowledged` on
    approval, `dismissed` on rejection. Without it the finding stayed `open` and the endpoint
    kept reporting a problem already answered. Unfenced: the reviewer holds no lease, the API's
    transaction is the boundary."""
    if status not in ("acknowledged", "dismissed"):
        raise ValueError(f"a finding is acknowledged or dismissed, not {status!r}")
    result = conn.execute(
        text(
            "UPDATE findings SET status = :status, updated_at = now() "
            "WHERE id = :id AND status = 'open'"
        ),
        {"id": finding_id, "status": status},
    )
    return result.rowcount == 1


def open_for_pile(conn: Connection, pile_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT f.id, f.agreement_id, f.rule_id, f.severity, f.evaluated_by, "
            "f.message, f.citations, f.status "
            "FROM findings f JOIN agreements a ON a.id = f.agreement_id "
            "WHERE a.pile_id = :p AND f.status = 'open' ORDER BY f.severity DESC, f.rule_id"
        ),
        {"p": pile_id},
    ).all()
    return [dict(row._mapping) for row in rows]


def counts_for_pile(conn: Connection, pile_id: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT f.status, count(*) AS n FROM findings f "
            "JOIN agreements a ON a.id = f.agreement_id "
            "WHERE a.pile_id = :p GROUP BY f.status"
        ),
        {"p": pile_id},
    ).all()
    return {row.status: int(row.n) for row in rows}
