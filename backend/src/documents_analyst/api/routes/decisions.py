"""The review queue: one item, and the amendment fast lane.

This is the human gate's only door. `Commit` acts on `pending_decisions.status =
'approved'` and nothing else, so an external edit nobody approved would have to
start with a row written here.

## What approving does NOT do

It does not resume the run. It writes one row and returns. The resume happens in the
worker, on its own sweep, because a paused pile is resumable when its queue is empty
— not when something was approved. The gate re-interrupts while anything is pending
(`human_gate.py`), so resuming per click would take a lease, start a run, pause and
write two events each time; and a reviewer who rejects everything has still finished,
and that run must reach the end or the pile stays `awaiting_review` for ever.

The `NOTIFY` that wakes the worker is fired by a table trigger, not this handler, so
an approval applied with psql while debugging wakes it the same way.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse

from documents_analyst.api.deps import Actor, Db, run
from documents_analyst.api.schemas import BATCHABLE_KINDS, BatchApproveRequest, DecisionRequest
from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import findings as finding_repo
from documents_analyst.db.repo import piles as pile_repo

router = APIRouter(tags=["decisions"])


@router.get("/piles/{pile_id}/decisions")
async def queue(pile_id: str, conn: Db) -> dict[str, Any]:
    """The whole queue at once, grouped, decided in any order. One gate stop shows
    everything, and rejecting item A never affects item B because each is its own
    row. `group_key` puts a rulebook finding next to the prose conflict about the
    same sentence, so a reviewer isn't asked one question twice in two words."""
    items = await run(conn, decision_repo.open_items, pile_id)
    return {"pile_id": pile_id, "pending": len(items), "items": items}


@router.get("/decisions/{decision_id}")
async def one_item(decision_id: str, conn: Db) -> dict[str, Any]:
    """One item by id, without knowing which pile it belongs to. Added for the MCP
    server and what makes its approve tool possible: a caller holding a decision id
    must be able to read the evidence back before agreeing with it. Otherwise
    "re-read the item" means fetching the whole queue and searching it, and a tool
    that had to do that would sooner skip the reading."""
    item = await run(conn, decision_repo.get, decision_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no decision {decision_id}")
    return dict(item)


@router.post("/decisions/{decision_id}/approve")
async def approve(
    decision_id: str, body: DecisionRequest, actor: Actor, conn: Db
) -> dict[str, Any]:
    return await _settle(conn, decision_id, "approved", actor, body)


@router.post("/decisions/{decision_id}/reject")
async def reject(decision_id: str, body: DecisionRequest, actor: Actor, conn: Db) -> dict[str, Any]:
    return await _settle(conn, decision_id, "rejected", actor, body)


def _bad_choice(item: dict[str, Any], body: DecisionRequest) -> str | None:
    """Why this approval can't be acted on, or `None`. Two failures, and the second
    is the dangerous one.

    No choice at all. `Commit` resolves a slot to the group a reviewer picked;
    without one there's no winner, so the slot stays `disputed` and the register
    keeps printing "sources disagree". Refused, not defaulted — picking a side for
    the reviewer is the one thing this system exists not to do. An amendment is
    different: "the later document supersedes" is mechanical, the classifier recorded
    which group that is, and the batch lane depends on not asking.

    A choice not on the list — `999`, a stale option, a number an agent invented.
    Checked against the options stored on the decision, because a value group is a
    small integer and nothing about the type says which ones exist, so a typo is
    indistinguishable from an answer until it reaches the facts."""
    if item["kind"] not in ("conflict", "amendment"):
        return None

    options = (item.get("payload") or {}).get("options") or []
    available = [o.get("value_group") for o in options if o.get("value_group") is not None]
    choices = ", ".join(f"{o.get('value_group')} = {o.get('value_text')!r}" for o in options)

    chosen = body.chosen_value_group
    if chosen is None:
        if item["kind"] == "amendment":
            return None  # falls back to payload.recommended_value_group
        return "approving a conflict requires chosen_value_group — which value wins. " + (
            f"Options: {choices}" if choices else "This item records no options."
        )

    if chosen not in available:
        # `not available` is a refusal too, not a free pass: an item whose payload
        # recorded no options once accepted any number at all. An approval naming a
        # group that doesn't exist can't resolve anything — it sits `approved` for
        # ever and the slot keeps showing the disagreement.
        return f"chosen_value_group {chosen} is not one of this item's values. " + (
            f"Options: {choices}"
            if choices
            else "This item records no options, so no choice can be honoured."
        )
    return None


async def _settle(
    conn: Any, decision_id: str, outcome: str, actor: str, body: DecisionRequest
) -> dict[str, Any]:
    """One item, once. A second attempt on an item somebody already decided returns
    409 with the existing decision, rather than overwriting it. Two reviewers with
    the same queue open isn't rare, and the loser must be told — silently replacing
    the first name on an already-committed approval would leave the register saying
    one thing and the audit trail attributing it to someone else."""
    if outcome == "approved":
        item = await run(conn, decision_repo.get, decision_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"no decision {decision_id}")
        missing = _bad_choice(item, body)
        if missing:
            raise HTTPException(status_code=422, detail=missing)
        if (
            item["status"] == "pending"
            and item.get("ref_id")
            and not await run(conn, decision_repo.evidence_current, item, body.chosen_value_group)
        ):
            # Returned, not raised: an exception would roll back the request's
            # transaction, and with it the withdrawal that lets the item be asked again.
            await run(conn, decision_repo.send_back_for_recheck, item)
            return JSONResponse(  # type: ignore[return-value]
                status_code=409,
                content={
                    "detail": "The sources behind this item changed, so it was sent back to "
                    "be checked again. A fresh item with the current options appears shortly."
                },
            )

    settled = await run(
        conn,
        decision_repo.settle,
        decision_id,
        status=outcome,
        actor=actor,
        note=body.note,
        chosen_value_group=body.chosen_value_group,
    )
    if settled is not None:
        return {"decision": settled, **await _settle_side_effects(conn, settled, actor)}

    existing = await run(conn, decision_repo.get, decision_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"no decision {decision_id}")
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"already {existing['status']}"
            + (f" by {existing['decided_by']}" if existing["decided_by"] else "")
        ),
    )


async def _settle_side_effects(conn: Any, settled: dict[str, Any], actor: str) -> dict[str, Any]:
    """What a decision means to the row it was raised about. A `finding` carries a
    consequence outside `pending_decisions`: it moves to `acknowledged` or `dismissed`;
    left alone, the findings endpoint kept reporting a violation the reviewer had just
    answered.

    An `unverified_document` needs nothing here: it reports that the file on disk
    didn't match the record, and `Resolve` restores the file at the start of the next
    run whichever way it is answered.

    Resolving a conflict is deliberately NOT here: it belongs to the worker, under a
    lease, in the same transaction that renders the register
    (`Commit._apply_resolutions`). The API records what a human decided; the graph
    acts on it."""
    kind = str(settled.get("kind") or "")
    ref_id = settled.get("ref_id")
    approved = settled.get("status") == "approved"
    if not ref_id:
        return {}

    if kind == "finding":
        moved = await run(
            conn,
            finding_repo.settle,
            str(ref_id),
            status="acknowledged" if approved else "dismissed",
            by=actor,
        )
        return {"finding": "acknowledged" if approved else "dismissed"} if moved else {}

    return {}


@router.post("/piles/{pile_id}/decisions/approve-batch")
async def approve_batch(
    pile_id: str, body: BatchApproveRequest, actor: Actor, conn: Db
) -> dict[str, Any]:
    """The amendment fast lane — a named class, never "everything pending". A pile
    where each contract was later amended produces a dozen items asking the same
    mechanical question, and clicking twelve times teaches a reviewer to click
    without reading. The kinds it settles are a closed list (`BATCHABLE_KINDS`);
    conflicts and rulebook findings aren't on it, or the batch would be an
    approve-all button with a specific-sounding name."""
    if await run(conn, pile_repo.get, pile_id) is None:
        raise HTTPException(status_code=404, detail=f"no pile {pile_id}")

    kinds = [k for k in body.kinds if k in BATCHABLE_KINDS]
    approved = await run(
        conn, decision_repo.settle_many, pile_id, kinds=kinds, actor=actor, note=body.note
    )
    remaining = await run(conn, decision_repo.open_count, pile_id)
    return {
        "approved": approved,
        "count": len(approved),
        "kinds": kinds,
        "still_pending": remaining,
    }
