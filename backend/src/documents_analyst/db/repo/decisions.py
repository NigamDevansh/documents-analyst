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
            "SELECT id FROM pending_decisions WHERE pile_id = :pile_id AND status = 'pending' AND kind = ANY(:kinds)"
        ),
        {"pile_id": pile_id, "kinds": kinds},
    ).all()
    approved = []
    for row in rows:
        item = get(conn, str(row.id))
        if item is None or not evidence_current(conn, item):
            continue
        if settle(conn, str(row.id), status="approved", actor=actor, note=note):
            approved.append(str(row.id))
    return approved


def invalidate_review(conn: Connection, fence: Fence, *, reread: bool = True) -> int:
    """Withdraw the uncommitted review items a source change actually made stale.

    Scoped by evidence, not by pile: an item is withdrawn (`superseded`, its author kept)
    only when the live facts on its field no longer match the sources it showed. Before,
    any added, copied or removed file withdrew every open and approved item in the pile —
    dropping an identical copy into the folder threw away approvals for agreements it
    had nothing to do with.

    `reread` sends a pile paused at the gate back to `idle` so the change is read now and
    its questions join the same review, rather than after it. Decisions still standing
    are kept; Commit re-checks each one against current evidence before applying it.
    Returns how many items were withdrawn."""
    items = conn.execute(
        text(
            "SELECT id, kind, ref_id, payload, chosen_value_group FROM pending_decisions pd "
            "WHERE pile_id = :pile_id AND kind IN ('conflict', 'amendment') "
            "AND status IN ('pending', 'approved') "
            "AND NOT EXISTS (SELECT 1 FROM deliverable_commits c WHERE pd.id = ANY(c.decision_ids))"
        ),
        {"pile_id": fence.pile_id},
    ).all()
    stale = [
        str(row.id)
        for row in items
        if not evidence_current(conn, dict(row._mapping), row.chosen_value_group)
    ]
    if stale:
        conn.execute(
            text(
                "UPDATE pending_decisions SET status = 'superseded' "
                f"WHERE id = ANY(CAST(:ids AS uuid[])) AND {HOLDS_LEASE}"
            ),
            {"ids": stale, "pile_id": fence.pile_id, "epoch": fence.epoch},
        )
    if stale or reread:
        conn.execute(
            text(
                "UPDATE piles SET status = 'idle' WHERE id = :pile_id "
                f"AND status = 'awaiting_review' AND {HOLDS_LEASE}"
            ),
            {"pile_id": fence.pile_id, "epoch": fence.epoch},
        )
    return len(stale)


def send_back_for_recheck(conn: Connection, item: dict[str, Any]) -> None:
    """A pending item whose evidence changed under it: withdraw it and re-check the field.

    Refusing the approval alone left the item approvable by no one — every attempt got
    a 409, discovery saw no file change, and classify will not raise a second item while
    one is pending. Withdrawing it (`superseded`, never deleted: who raised what stays
    on record) and recording a sources change makes the pile run again, and classify
    asks the question afresh with the options the sources hold now.

    A pile paused at the gate goes back to `idle` so it can be claimed; its other
    pending items stay pending and the gate waits for them again. Nothing here runs
    while a worker holds the lease."""
    withdrawn = conn.execute(
        text(
            "UPDATE pending_decisions SET status = 'superseded', "
            "decided_note = 'withdrawn: its source evidence changed before approval' "
            "WHERE id = :id AND status = 'pending' RETURNING pile_id"
        ),
        {"id": str(item["id"])},
    ).first()
    if withdrawn is None:
        return
    pile_id = str(withdrawn.pile_id)
    conn.execute(
        text(
            "UPDATE piles SET status = 'idle', updated_at = now() WHERE id = :id "
            "AND status = 'awaiting_review' "
            "AND (lease_expires_at IS NULL OR lease_expires_at < now())"
        ),
        {"id": pile_id},
    )
    import uuid

    from documents_analyst.db.repo import events

    events.append(
        conn,
        pile_id=pile_id,
        run_id=str(uuid.uuid4()),
        type="run.sources_changed",
        payload={"reason": "review evidence changed", "decision_id": str(item["id"])},
    )
    conn.execute(text("SELECT pg_notify('analyst_work', :pile_id)"), {"pile_id": pile_id})


def evidence_current(conn: Connection, item: dict[str, Any], chosen: int | None = None) -> bool:
    """Compare source identities, not just reusable group numbers, at both gates."""
    if item.get("kind") not in ("conflict", "amendment") or not item.get("ref_id"):
        return True
    payload = item.get("payload") or {}
    options = payload.get("options") or []
    rows = conn.execute(
        text(
            "SELECT f.id, f.value_group, f.source_document_id, f.source_quote "
            "FROM facts_and_claims f JOIN documents d ON d.id = f.source_document_id "
            "WHERE f.slot_id = :id AND f.status = 'active' AND f.role <> 'superseded' "
            "AND d.status NOT IN ('removed', 'superseded', 'quarantined')"
        ),
        {"id": str(item["ref_id"])},
    ).all()
    expected = {
        (int(option["value_group"]), str(source["document_id"]), str(source.get("quote") or ""))
        for option in options
        for source in option.get("sources", [])
        if source.get("document_id")
    }
    actual = {
        (int(row.value_group or 0), str(row.source_document_id), str(row.source_quote))
        for row in rows
    }
    chosen_group = chosen if chosen is not None else item.get("chosen_value_group")
    if chosen_group is None:
        chosen_group = payload.get("recommended_value_group")
    # A prior successful resolution may already have superseded the other options.
    applied = conn.execute(
        text(
            "SELECT f.source_document_id, f.source_quote FROM claim_slots s "
            "JOIN facts_and_claims f ON f.id = s.current_fact_id "
            "WHERE s.id = :id AND s.state = 'resolved' AND s.resolved_by = :actor "
            "AND f.status = 'active' AND f.value_group = :chosen"
        ),
        {"id": str(item["ref_id"]), "actor": item.get("decided_by"), "chosen": chosen_group},
    ).first()
    if applied and (
        not expected
        or (int(chosen_group or 0), str(applied.source_document_id), str(applied.source_quote))
        in expected
    ):
        return True
    if expected and expected != actual:
        return False
    winner = chosen if chosen is not None else item.get("chosen_value_group")
    if winner is None:
        winner = payload.get("recommended_value_group")
    return winner is not None and any(int(row.value_group or 0) == int(winner) for row in rows)


def refresh_stale(conn: Connection, fence: Fence, item: dict[str, Any]) -> None:
    """Keep the old decision as history and ask a fresh question with live options."""
    conn.execute(
        text(
            f"UPDATE pending_decisions SET status = 'superseded' WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {"id": str(item["id"]), "pile_id": fence.pile_id, "epoch": fence.epoch},
    )
    rows = conn.execute(
        text(
            "SELECT f.value_group, f.value, f.value_norm, f.source_document_id, f.source_quote, "
            "f.char_start, f.char_end, d.filename FROM facts_and_claims f "
            "JOIN documents d ON d.id = f.source_document_id "
            "WHERE f.slot_id = :id AND f.status = 'active' AND f.role <> 'superseded' "
            "AND d.status NOT IN ('removed', 'superseded', 'quarantined') ORDER BY f.value_group"
        ),
        {"id": str(item["ref_id"])},
    ).all()
    options: dict[int, dict[str, Any]] = {}
    for row in rows:
        group = int(row.value_group or 0)
        option = options.setdefault(
            group,
            {
                "value_group": group,
                "value_text": row.value.get("text"),
                "value_norm": row.value_norm,
                "sources": [],
            },
        )
        option["sources"].append(
            {
                "document_id": str(row.source_document_id),
                "filename": row.filename,
                "quote": row.source_quote,
                "char_start": row.char_start,
                "char_end": row.char_end,
            }
        )
    if not options:
        return
    raise_decision(
        conn,
        fence,
        kind="conflict",
        ref_id=str(item["ref_id"]),
        payload={
            "options": list(options.values()),
            "field_key": (item.get("payload") or {}).get("field_key"),
        },
        detected_by="rule",
        recommendation="Source evidence changed after this choice was reviewed. Review the current options.",
        reopen=True,
    )
