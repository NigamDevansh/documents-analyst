"""StateGraph wiring and the Postgres checkpointer.

`thread_id` is the **pile id**, not the run id. That single choice is what makes
resume-after-approval work: the API knows only which pile a person clicked
approve on, so a worker can call `invoke(None, config={"thread_id": pile_id})`
and land back inside the interrupt without anyone having to look up which run
was paused. It also makes "one active run per pile" true by construction rather
than by convention — a second run on the same thread would resume the first.

**Through Phase 4C, the whole graph:**

    Resolve → Ingest → Extract → Match → Classify → Audit → HumanGate
                                            ▲                    │
                                            │                    ▼
                                            │                 Commit
                                            │                    │
                                            └── Reconcile ◄───────┘
                                              (audit_pass=2)
                                            │
                                          Audit ──(pass 2)──► END

Three things carry the design and each is worth naming:

**`Resolve` runs first, always.** If the final document on disk is missing or no
longer matches what was last recorded as written, it is restored from the record
before anything new is committed on top. It costs one query and one file read
when there is nothing to repair.

**There is no `Commit → HumanGate` loop, and there was going to be.**
`BACKEND_INTERNALS.md` §2.6 draws one so that each approval commits *that* item
without the run ending. It is unnecessary here because `HumanGate` already
loops on itself: it re-checks the queue after every resume and interrupts again
while anything is still pending, so the run only leaves the gate once the whole
queue is decided. A second loop outside it would be a second mechanism for the
same thing, and the two would disagree the first time one of them changed.

The consequence is worth stating plainly: commits are **batched at the end of
review** rather than issued per approval. That costs a reviewer nothing — they
are deciding either way — and five decided items become one document write and
one version instead of five.

**Pass 2 ends the run.** `Reconcile` sets `audit_pass = 2` and routes back to
`Audit`, which is what catches a violation *created by* an approval. On pass 2
`Audit` records what it finds and stops — its findings wait in the queue for the
next run. Without that a fix that creates a finding that creates a fix would
loop for ever.

`Ground` is deliberately absent — see `nodes/ground.py` for why it is a function
inside `Extract` rather than a node beside it.
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from documents_analyst.graph.nodes import (
    audit,
    classify,
    commit,
    extract,
    human_gate,
    ingest,
    match,
    reconcile,
    resolve,
)
from documents_analyst.graph.state import PileRunState

RESOLVE = "resolve"
INGEST = "ingest"
EXTRACT = "extract"
MATCH = "match"
CLASSIFY = "classify"
AUDIT = "audit"
HUMAN_GATE = "human_gate"
COMMIT = "commit"
RECONCILE = "reconcile"

NODES = (RESOLVE, INGEST, EXTRACT, MATCH, CLASSIFY, AUDIT, HUMAN_GATE, COMMIT, RECONCILE)
"""The stages, in the order they run.

`test_resolve_runs_before_a_new_commit_is_opened` asserts against this tuple,
and that is the point of it existing: "resolve comes before commit" is a
load-bearing invariant — a document damaged or lost since the last run is
restored before a new version is written on top — and a tuple can be asserted
where a graph wiring diagram cannot.
"""


def _after_audit(state: PileRunState) -> str:
    """Pass 1 goes to the gate. Pass 2 stops. The only thing preventing an
    infinite loop, and a single comparison rather than a counter: "have we
    committed on this run" is exactly `audit_pass == 2`."""
    if int(state.get("audit_pass", 1)) >= 2:
        return END
    return HUMAN_GATE


def build_graph(checkpointer: Any) -> Any:
    """The compiled graph. The checkpointer is passed in, not constructed here:
    tests use an in-memory saver, the worker Postgres, and a module reaching for a
    connection string at import time could be neither."""
    graph: StateGraph[PileRunState, None, PileRunState, PileRunState] = StateGraph(PileRunState)

    graph.add_node(RESOLVE, resolve.run)
    graph.add_node(INGEST, ingest.run)
    graph.add_node(EXTRACT, extract.run)
    graph.add_node(MATCH, match.run)
    graph.add_node(CLASSIFY, classify.run)
    graph.add_node(AUDIT, audit.run)
    graph.add_node(HUMAN_GATE, human_gate.run)
    graph.add_node(COMMIT, commit.run)
    graph.add_node(RECONCILE, reconcile.run)

    graph.add_edge(START, RESOLVE)
    graph.add_edge(RESOLVE, INGEST)
    graph.add_edge(INGEST, EXTRACT)

    def after_extract(state: PileRunState, config: RunnableConfig) -> str:
        from documents_analyst.graph.context import context_of
        from documents_analyst.worker.queue import ingested_documents

        ctx = context_of(config)
        with ctx.engine.begin() as conn:
            pending = ingested_documents(
                conn, state["pile_id"], max_attempts=ctx.settings.job_max_attempts
            )
        return END if pending else MATCH

    graph.add_conditional_edges(EXTRACT, after_extract, {END: END, MATCH: MATCH})
    graph.add_edge(MATCH, CLASSIFY)
    graph.add_edge(CLASSIFY, AUDIT)

    graph.add_conditional_edges(AUDIT, _after_audit, {HUMAN_GATE: HUMAN_GATE, END: END})
    graph.add_edge(HUMAN_GATE, COMMIT)
    graph.add_conditional_edges(
        COMMIT,
        lambda state: HUMAN_GATE if state.get("review_pending") else RECONCILE,
        {HUMAN_GATE: HUMAN_GATE, RECONCILE: RECONCILE},
    )
    graph.add_edge(RECONCILE, AUDIT)

    return graph.compile(checkpointer=checkpointer)
