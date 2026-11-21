"""Repository: pending decisions. What the human gate is waiting on.

Default-deny is a data structure here, not an `if`. `status` defaults to `'pending'`
in the schema, and the only thing that makes a row `approved` is an UPDATE naming who
decided it. No code path turns a batch of items into approvals, so an item nobody
looked at can't become one by omission — the failure mode of every "approve unless
rejected" design. The boundary is the durable checkpoint: a paused run resumes in the
same place without treating an unanswered item as approval.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence

OPEN_ITEMS = text("""
    SELECT id, kind, ref_id, payload, relationship, detected_by, evidence_quote,
           group_key, recommendation, created_at
      FROM pending_decisions
     WHERE pile_id = :pile_id AND status = 'pending'
     ORDER BY COALESCE(group_key, id::text), created_at
""")
"""Grouped, then chronological. `group_key` puts R010 ("policy says termination must be
mutual") next to the prose conflict about the same sentence, so a reviewer isn't asked
the same question twice in two words. Ungrouped items fall back to their own id, which
keeps them stable rather than clustering every one under a single NULL."""


RAISE = text(f"""
    INSERT INTO pending_decisions (pile_id, kind, ref_id, payload, relationship,
                                   detected_by, evidence_quote, group_key, recommendation)
         SELECT :pile_id, :kind, CAST(:ref_id AS uuid), CAST(:payload AS jsonb), :relationship,
                :detected_by, :evidence_quote, :group_key, :recommendation
          WHERE {HOLDS_LEASE}
            AND NOT EXISTS (
                  SELECT 1 FROM pending_decisions d
                   WHERE d.pile_id = :pile_id AND d.kind = :kind
                     AND d.ref_id IS NOT DISTINCT FROM CAST(:ref_id AS uuid)
                     AND (d.status = 'pending' OR NOT :reopen)
                     AND d.status IN ('pending', 'approved', 'rejected')
                )
      RETURNING id
""")
"""Raised once per `(pile, kind, ref)` — the `NOT EXISTS` guard stops the graph re-raising an
undecided conflict on every re-run. `approved`/`rejected` are in the guard too, so a settled
item isn't asked again (a queue, not a nag); `IS NOT DISTINCT FROM` because `ref_id` is NULL
for kinds pointing at no row. `reopen` narrows the guard to `pending` only, for the one case a
resolved slot gets new disagreeing evidence and goes `contested` — the caller sets it only
then, and the prior decision stays as the visible, questioned answer.
"""


def raise_decision(
    conn: Connection,
    fence: Fence,
    *,
    kind: str,
    ref_id: str | None = None,
    payload: dict[str, Any] | None = None,
    relationship: str | None = None,
    detected_by: str | None = None,
    evidence_quote: str | None = None,
    group_key: str | None = None,
    recommendation: str | None = None,
    reopen: bool = False,
) -> str | None:
    """One review item. `None` if it already exists or the lease is gone. The ONLY way
    something reaches a human, so "what can be asked" is one list. `reopen=True` allows a NEW
    question about something already decided (see `RAISE`), set only by Classify."""
    row = conn.execute(
        RAISE,
        {
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
            "kind": kind,
            "ref_id": ref_id,
            "payload": json.dumps(payload or {}),
            "relationship": relationship,
            "detected_by": detected_by,
            "evidence_quote": evidence_quote,
            "group_key": group_key,
            "recommendation": recommendation,
            "reopen": reopen,
        },
    ).first()
    return str(row.id) if row else None


def open_count(conn: Connection, pile_id: str) -> int:
    """Whether the gate has anything to stop for — its entire condition. Counted, not
    fetched: the node only needs whether, and a mid-review pile can hold a large queue."""
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM pending_decisions "
                "WHERE pile_id = :pile_id AND status = 'pending'"
            ),
            {"pile_id": pile_id},
        ).scalar_one()
    )


def open_items(conn: Connection, pile_id: str) -> list[dict[str, Any]]:
    """The whole queue, decided in any order. All of it at once, deliberately: one gate
    stop shows everything, and rejecting item A never affects item B because each is its
    own row."""
    return [dict(row._mapping) for row in conn.execute(OPEN_ITEMS, {"pile_id": pile_id}).all()]


def approved_undecided(conn: Connection, pile_id: str) -> list[dict[str, Any]]:
    """Approved items not committed yet — Commit's input. Read here rather than in
    `commit.py` so "what has a human agreed to" has one definition: an approval Commit
    can't see is one that silently did nothing."""
    # `decision_ids` is an ARRAY — one commit can carry several decisions — so "already
    # committed" is array membership (`= ANY`), not a scalar join.
    rows = conn.execute(
        text(
            "SELECT d.id, d.kind, d.ref_id, d.payload, d.chosen_value_group, d.decided_by "
            "FROM pending_decisions d "
            "WHERE d.pile_id = :pile_id AND d.status = 'approved' "
            "AND NOT EXISTS (SELECT 1 FROM deliverable_commits c "
            "                 WHERE c.pile_id = d.pile_id AND d.id = ANY(c.decision_ids)) "
            "ORDER BY d.decided_at"
        ),
        {"pile_id": pile_id},
    ).all()
    return [dict(row._mapping) for row in rows]


def slots_already_asked(conn: Connection, pile_id: str, kind: str) -> set[str]:
    """Slot ids this pile has already raised `kind` about, in any status. The read half of
    `RAISE`'s guard, exposed so Commit asks about each orphaned row exactly once. Any status: a
    reviewer who rejected "remove this row" said it should stay."""
    rows = conn.execute(
        text(
            "SELECT ref_id FROM pending_decisions "
            "WHERE pile_id = :pile_id AND kind = :kind AND ref_id IS NOT NULL"
        ),
        {"pile_id": pile_id, "kind": kind},
    ).all()
    return {str(row.ref_id) for row in rows}


SETTLE = text("""
    UPDATE pending_decisions
       SET status = :status,
           decided_by = :actor,
           decided_at = now(),
           decided_note = :note,
           chosen_value_group = COALESCE(:chosen, chosen_value_group)
     WHERE id = :id AND status = 'pending'
 RETURNING id, pile_id, kind, ref_id, status, decided_by, decided_at, chosen_value_group
""")
"""`AND status = 'pending'` is the whole concurrency story: two reviewers both click approve,
one UPDATE matches and one gets "already decided by X" instead of overwriting the first name.
Also makes the route idempotent — replaying an approve returns the existing decision."""


def settle(
    conn: Connection,
    decision_id: str,
    *,
    status: str,
    actor: str,
    note: str | None = None,
    chosen_value_group: int | None = None,
) -> dict[str, Any] | None:
    """Approve or reject one item. `None` if it wasn't pending. `actor` is required, not
    defaulted: an approval with a blank `decided_by` is one nobody can be asked about later,
    and this row is Commit's only authority to touch an external document."""
    if status not in ("approved", "rejected"):
        raise ValueError(f"a decision is approved or rejected, not {status!r}")
    row = conn.execute(
        SETTLE,
        {
            "id": decision_id,
            "status": status,
            "actor": actor,
            "note": note,
            "chosen": chosen_value_group,
        },
    ).first()
    return dict(row._mapping) if row else None


def get(conn: Connection, decision_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "SELECT id, pile_id, kind, ref_id, payload, relationship, detected_by, "
            "evidence_quote, group_key, recommendation, status, chosen_value_group, "
            "decided_by, decided_at, decided_note, created_at "
            "FROM pending_decisions WHERE id = :id"
        ),
        {"id": decision_id},
    ).first()
    return dict(row._mapping) if row else None


def settle_many(
    conn: Connection,
    pile_id: str,
    *,
    kinds: list[str],
    actor: str,
    note: str | None = None,
) -> list[str]:
    """The fast lane: settle every pending item of these kinds in one statement. Built for
    amendments, where a dozen contracts produce a dozen identical mechanical questions. Kinds,
    not "everything pending" — a batch sweeping up conflicts and findings would be an
    approve-all button with a specific name."""
    if not kinds:
        return []
    rows = conn.execute(
        text(
            "UPDATE pending_decisions SET status = 'approved', decided_by = :actor, "
            "decided_at = now(), decided_note = :note "
            "WHERE pile_id = :pile_id AND status = 'pending' AND kind = ANY(:kinds) "
            "RETURNING id"
        ),
        {"pile_id": pile_id, "kinds": kinds, "actor": actor, "note": note},
    ).all()
    return [str(row.id) for row in rows]
