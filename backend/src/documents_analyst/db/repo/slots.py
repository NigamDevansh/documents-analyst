"""Repository: claim slots. One per (agreement, field) — what the register renders.

A slot is the question "what does this agreement say about payment terms?", and its
`state` is the answer's status. The four states each render differently, and two are
the difference between a register that can be trusted and one that can't:

| state | means |
|---|---|
| `settled` | one value, everything agrees |
| `disputed` | sources conflict and nobody has decided |
| `resolved` | a human chose |
| `contested` | new conflicting evidence arrived on a slot someone already resolved |

`resolved` is never silently overwritten, and `contested` is why. A later document
disagreeing with a decision a person made is news, not a correction: it's shown as a
fresh question rather than quietly reverting the slot to `disputed` and discarding who
chose what.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence, LostLease, fenced

LOG = logging.getLogger(__name__)

UPSERT = text(f"""
    INSERT INTO claim_slots (agreement_id, field_key, subject_free)
         SELECT :agreement_id, :field_key, :subject_free
          WHERE {HOLDS_LEASE}
    ON CONFLICT (agreement_id, field_key, subject_free) DO UPDATE
            SET updated_at = now()
      RETURNING id, field_key, subject_free, state, current_fact_id, resolved_by
""")
"""`DO UPDATE`, not `DO NOTHING`, so the row comes back either way — the caller needs the
slot's existing state to read what a new fact means. The conflict target is all three columns
with `NULLS NOT DISTINCT`: without it, every open-subject slot's NULL `field_key` would compare
unequal and each pass would insert another copy."""


def ensure(
    conn: Connection,
    fence: Fence,
    *,
    agreement_id: str,
    field_key: str | None = None,
    subject_free: str | None = None,
) -> dict[str, Any]:
    """The slot for this field OR this free subject, returning its prior state. Exactly one of
    the two, checked here as well as by the schema so a caller passing both fails at the call
    site, not three frames down."""
    if bool(field_key) == bool(subject_free):
        raise ValueError("a slot is keyed by a taxonomy field OR a free subject, not both")

    row = conn.execute(
        UPSERT,
        {
            "agreement_id": agreement_id,
            "field_key": field_key,
            "subject_free": subject_free,
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    ).first()
    if row is None:
        raise LostLease(
            f"claim_slot upsert affected 0 rows — epoch {fence.epoch} is stale "
            f"for pile {fence.pile_id}"
        )
    return dict(row._mapping)


def attach_facts(
    conn: Connection, fence: Fence, slot_id: str, groups: dict[str, list[str]]
) -> None:
    """Point facts at their slot and record each one's value group. `groups` is
    `{value_group_index: [fact_id, …]}` — facts asserting the SAME value share a group, so
    30/45/60 days is one question with three groups, not three pairwise conflicts."""
    for group, fact_ids in groups.items():
        result = conn.execute(
            text(
                "UPDATE facts_and_claims SET slot_id = :slot_id, value_group = :group "
                f"WHERE id = ANY(:ids) AND {HOLDS_LEASE}"
            ),
            {
                "slot_id": slot_id,
                "group": int(group),
                "ids": fact_ids,
                "pile_id": fence.pile_id,
                "epoch": fence.epoch,
            },
        )
        if result.rowcount != len(fact_ids):
            raise LostLease(
                f"fact attachment affected {result.rowcount} of {len(fact_ids)} rows — "
                f"epoch {fence.epoch} is stale for pile {fence.pile_id}"
            )


def set_state(
    conn: Connection,
    fence: Fence,
    slot_id: str,
    *,
    state: str,
    current_fact_id: str | None = None,
) -> bool:
    """Move a slot, refusing the two transitions that would lose a decision. Returns whether it
    moved (`False` = refused, the guard working); a stale lease still raises. `resolved →
    disputed`/`settled` are blocked in SQL so the next scan can't quietly un-decide a slot —
    new evidence instead makes it `contested`, keeping `resolved_by`."""
    params = {
        "id": slot_id,
        "state": state,
        "fact_id": current_fact_id,
        "pile_id": fence.pile_id,
        "epoch": fence.epoch,
    }
    changed = conn.execute(
        text(
            "UPDATE claim_slots SET state = :state, "
            "current_fact_id = COALESCE(CAST(:fact_id AS uuid), current_fact_id), "
            "updated_at = now() "
            "WHERE id = :id "
            "  AND (state <> 'resolved' OR :state IN ('resolved', 'contested')) "
            f"  AND {HOLDS_LEASE}"
        ),
        params,
    ).rowcount
    if changed:
        return True

    # Zero rows has two causes. `fenced` would call both a lost lease, so a refused transition
    # (the guard working) surfaced as "another worker holds this pile". Ask which it was.
    refused = conn.execute(
        text("SELECT state FROM claim_slots WHERE id = :id"), {"id": slot_id}
    ).scalar_one_or_none()
    if refused == "resolved" and state not in ("resolved", "contested"):
        LOG.debug("slot %s is resolved; refusing to move it to %s", slot_id, state)
        return False
    raise LostLease(
        f"lease lost before setting slot {slot_id} to {state} "
        f"(epoch {fence.epoch} is stale for pile {fence.pile_id})"
    )


WINNING_FACTS = text("""
    SELECT id FROM facts_and_claims
     WHERE slot_id = :slot_id AND status = 'active' AND role <> 'superseded'
       AND value_group = :winner
  ORDER BY created_at
""")
"""Look before demoting — the whole safety of `resolve`. If it demoted first and checked the
winner after, approving a group not on the list (`999`, a stale option, a typo) would supersede
every value and leave the slot disputed with nothing to choose. `role <> 'superseded'` also
stops an earlier-demoted group being revived by a later decision written before it."""

RESOLVE_FACTS = text(f"""
    UPDATE facts_and_claims
       SET role = CASE WHEN value_group = :winner THEN 'current' ELSE 'superseded' END
     WHERE slot_id = :slot_id AND status = 'active'
       AND {HOLDS_LEASE}
""")
"""The losers become `superseded`, which is what makes the register change:
`register.slots.SLOT_ROWS` joins only non-`superseded` facts, so demoting the rest leaves the
winner as the slot's only value. Moving the state alone would leave `Slot.current` on group 0,
publishing its number under a resolved heading."""


def resolve(
    conn: Connection, fence: Fence, slot_id: str, *, value_group: int, decided_by: str
) -> str | None:
    """Apply an approved choice, returning the winning fact id or None. Nothing is demoted
    until the winner is confirmed to exist: `None` (a stale or since-superseded group) leaves
    the slot exactly as it was. `resolved` is the state nothing else may overwrite; new evidence
    later makes it `contested`, keeping `resolved_by`."""
    winners = [
        str(row.id)
        for row in conn.execute(WINNING_FACTS, {"slot_id": slot_id, "winner": value_group}).all()
    ]
    if not winners:
        return None

    fenced(
        conn,
        RESOLVE_FACTS,
        {
            "slot_id": slot_id,
            "winner": value_group,
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    )
    fenced(
        conn,
        text(
            "UPDATE claim_slots SET state = 'resolved', current_fact_id = CAST(:fact AS uuid), "
            "resolved_by = :by, resolved_at = now(), updated_at = now() "
            f"WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {
            "id": slot_id,
            "fact": winners[0],
            "by": decided_by,
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    )
    return winners[0]


def counts_for_pile(conn: Connection, pile_id: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT s.state, count(*) AS n FROM claim_slots s "
            "JOIN agreements a ON a.id = s.agreement_id "
            "WHERE a.pile_id = :p GROUP BY s.state"
        ),
        {"p": pile_id},
    ).all()
    return {row.state: int(row.n) for row in rows}
