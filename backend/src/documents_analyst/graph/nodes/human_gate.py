"""The interrupt. The checkpoint IS the pause.

Nothing here waits or polls: the node raises LangGraph's `interrupt`, LangGraph
writes the checkpoint, `invoke` returns. The run's position is then a row in
Postgres, not a stack frame — so the lease is released and a different worker on
a different machine can resume it hours later. A paused run holds no memory,
thread or lock; kill the container and the pause survives, because it was never
in the container.

Default-deny: the gate stops while anything is `pending` and returns when nothing
is. No branch treats "the human did not answer" as approval —
`pending_decisions.status` defaults to `'pending'` and only an explicit UPDATE
moves it, so an item nobody looked at can never be approved by omission. The
decision kinds come from Classify and Audit; this node is the single pause/resume
boundary and re-checks after resume.

See docs/BACKEND_INTERNALS.md § "The gate: review everything, then commit once".
"""

from __future__ import annotations

import logging

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.graph.context import context_of
from documents_analyst.graph.state import PileRunState

LOG = logging.getLogger(__name__)


def run(state: PileRunState, config: RunnableConfig) -> dict[str, object]:
    ctx = context_of(config)

    with ctx.engine.begin() as conn:
        waiting = decision_repo.open_count(conn, state["pile_id"])

    if not waiting:
        with ctx.engine.begin() as conn:
            event_repo.append(
                conn,
                pile_id=state["pile_id"],
                run_id=state["run_id"],
                type="stage.gate.clear",
                payload={"pending": 0},
            )
        return {}

    with ctx.engine.begin() as conn:
        items = decision_repo.open_items(conn, state["pile_id"])
        event_repo.append(
            conn,
            pile_id=state["pile_id"],
            run_id=state["run_id"],
            type="stage.gate.waiting",
            payload={"pending": len(items), "kinds": sorted({str(i["kind"]) for i in items})},
        )
    LOG.info("pile %s paused at the gate on %d item(s)", state["pile_id"], len(items))

    # Everything below runs only on RESUME: `interrupt` raises on the first pass
    # and LangGraph checkpoints; on resume it returns and execution continues
    # here. The payload is a summary, not the queue — the acting node reads the
    # decisions from `pending_decisions`, because carrying them through the
    # interrupt would freeze a snapshot taken before the review changed them.
    interrupt({"pending": len(items), "pile_id": state["pile_id"]})

    # The caller's name is not an approval. Re-check after the interrupt so a
    # premature resume cannot cross the boundary just by supplying a resume token.
    with ctx.engine.begin() as conn:
        remaining = decision_repo.open_count(conn, state["pile_id"])
    if remaining:
        interrupt({"pending": remaining, "pile_id": state["pile_id"]})

    with ctx.engine.begin() as conn:
        event_repo.append(
            conn,
            pile_id=state["pile_id"],
            run_id=state["run_id"],
            type="stage.gate.resumed",
            payload={"remaining": 0},
        )
    return {}
