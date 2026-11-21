"""Invoke the graph, and resume it after a human decides.

Two entry points: `run_pile` claims a pile with work and drives it from the
start; `resume_after_approval` re-enters a paused one. Both release the lease in a
`finally` — a run paused at the gate may wait hours, and holding a lease across a
human's coffee break blocks the pile and every other worker. The checkpoint makes
that safe: the run's position is a row, so whoever picks it up next need not be
this process.

See docs/WORKER_RUNTIME.md § "Scanner and runner are decoupled through Postgres".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from langgraph.types import Command
from sqlalchemy.engine import Engine

from documents_analyst.db.repo import events as event_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.db.repo.telemetry import StageRunSink
from documents_analyst.graph.build import build_graph
from documents_analyst.graph.context import CONTEXT_KEY, RunContext
from documents_analyst.graph.state import PileRunState
from documents_analyst.models import LoadedModels
from documents_analyst.settings import Settings
from documents_analyst.worker import lease, queue
from documents_analyst.worker.lease import Fence, LostLease
from documents_analyst_core.portable.llm_client import LLMClient

LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """What one invocation did, in the terms a reviewer would ask about."""

    pile_id: str
    run_id: str
    documents: int
    paused: bool
    """True when the run stopped at the human gate. Not a failure — the design
    working. Matters to the caller: a paused pile waits on a person, a finished one
    waits on nothing."""
    progressed: bool = True
    """Whether any document left the queue. A run that retires nothing leaves the
    pile exactly as claimable as it found it, so a caller looping until there's no
    work would claim it again and do the same nothing — e.g. an LLM quota outage
    where every section call 429s and no document can honestly be marked
    extracted."""


def _context(
    engine: Engine, settings: Settings, models: LoadedModels, llm: LLMClient, sink: StageRunSink
) -> RunContext:
    return RunContext(
        engine=engine,
        settings=settings,
        models=models,
        llm=llm,
        telemetry=sink,
        watch_root=settings.watch_root,
    )


def run_pile(
    engine: Engine,
    checkpointer: object,
    settings: Settings,
    models: LoadedModels,
    llm: LLMClient,
    *,
    worker_id: str,
) -> RunOutcome | None:
    """Claim one pile with graph-ready work and run it. None if there is none.
    Claim and lease are one statement (`queue.claim_and_lease`) — not because two
    would race, but because one has no claimed-but-unowned outcome and sets status
    in the same write that takes the lease."""
    with engine.begin() as conn:
        claimed = queue.claim_and_lease(conn, worker_id, settings.lease_ttl_s)
    if claimed is None:
        return None

    pile_id, epoch = claimed
    try:
        return _invoke(
            engine, checkpointer, settings, models, llm, pile_id=pile_id, epoch=epoch, resume=False
        )
    finally:
        with engine.begin() as conn:
            lease.release(conn, pile_id, epoch)


def resume_after_approval(
    engine: Engine,
    checkpointer: object,
    settings: Settings,
    models: LoadedModels,
    llm: LLMClient,
    *,
    pile_id: str,
    worker_id: str,
) -> RunOutcome | None:
    """Re-enter a paused pile. None when its lease is held by someone else. Callable
    knowing only the pile because `thread_id` IS the pile id: the API writes an
    approval and says "pile X has news", without knowing which run paused or where."""
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, worker_id, settings.lease_ttl_s)
    if epoch is None:
        LOG.debug("pile %s is held by another worker; not resuming", pile_id)
        return None

    try:
        with engine.begin() as conn:
            # Resumed graph is running again, but this lease came from the explicit
            # human-approval path, not a normal queue claim.
            pile_repo.set_status(conn, Fence(pile_id=pile_id, epoch=epoch), "running")
        return _invoke(
            engine, checkpointer, settings, models, llm, pile_id=pile_id, epoch=epoch, resume=True
        )
    except Exception:
        # Put the pile back where it actually is before letting the error out. A
        # node that raises leaves `status = 'running'` describing a run that isn't,
        # and for a resumed pile that is a one-way door: a normal claim needs
        # `ingested` documents (a reviewed pile has none) and the resume sweep needs
        # `awaiting_review`, so the checkpoint would strand at the gate with nothing
        # able to reach it. Restored to `awaiting_review` because that is the truth —
        # queue empty, checkpoint at the gate — and it lets the next tick retry once
        # the cause is fixed. Fenced and inside the lease we hold, so it can't
        # overwrite a successor's state.
        with engine.begin() as conn:
            pile_repo.set_status(conn, Fence(pile_id=pile_id, epoch=epoch), "awaiting_review")
        raise
    finally:
        with engine.begin() as conn:
            lease.release(conn, pile_id, epoch)


def _invoke(
    engine: Engine,
    checkpointer: object,
    settings: Settings,
    models: LoadedModels,
    llm: LLMClient,
    *,
    pile_id: str,
    epoch: int,
    resume: bool,
) -> RunOutcome:
    """One `invoke`, with the lease already held. The batch is read here once, at
    run start — every ingested original in the pile — so a four-document first build
    is one run with one review queue, not four trips to the gate."""
    graph = build_graph(checkpointer)
    run_id = str(uuid.uuid4())
    sink = StageRunSink(pile_id=pile_id, run_id=run_id)
    config = {
        "configurable": {
            "thread_id": pile_id,
            CONTEXT_KEY: _context(engine, settings, models, llm, sink),
        }
    }

    with engine.begin() as conn:
        document_ids = queue.ingested_documents(
            conn, pile_id, max_attempts=settings.job_max_attempts
        )
        event_repo.append(
            conn,
            pile_id=pile_id,
            run_id=run_id,
            type="run.resumed" if resume else "run.started",
            payload={"documents": len(document_ids), "epoch": epoch},
        )

    state: PileRunState = {
        "pile_id": pile_id,
        "run_id": run_id,
        "lease_epoch": epoch,
        "document_ids": document_ids,
        "audit_pass": 1,
        "attempt": 0,
    }

    try:
        # A Command resumes from the checkpoint AND updates its operational fields.
        # The prior checkpoint's epoch belonged to the worker that paused; keeping it
        # would fail the first fenced write under a valid new lease. Passing the whole
        # state would instead restart Ingest and re-extract everything under review.
        input_state: PileRunState | Command
        if resume:
            input_state = Command(
                update={"lease_epoch": epoch, "run_id": run_id},
                # The gate consumes no value, but a resume token tells LangGraph to
                # continue from its durable interrupt.
                resume={},
            )
        else:
            input_state = state
        result = graph.invoke(input_state, config=config)
    except LostLease:
        # Another worker legitimately took the pile mid-run. Every write was fenced,
        # so nothing of ours landed after the takeover — nothing to undo.
        with engine.begin() as conn:
            event_repo.append(
                conn, pile_id=pile_id, run_id=run_id, type="run.lost_lease", payload={}
            )
        raise

    paused = bool(_interrupts(result))
    with engine.begin() as conn:
        sink.flush(conn)
        remaining = queue.ingested_documents(conn, pile_id, max_attempts=settings.job_max_attempts)
        pile_repo.set_status(
            conn, Fence(pile_id=pile_id, epoch=epoch), "awaiting_review" if paused else "idle"
        )
        event_repo.append(
            conn,
            pile_id=pile_id,
            run_id=run_id,
            type="run.paused" if paused else "run.finished",
            payload={"documents": len(document_ids), "remaining": len(remaining)},
        )

    return RunOutcome(
        pile_id=pile_id,
        run_id=run_id,
        documents=len(document_ids),
        paused=paused,
        # Measured against the queue, not assumed from the absence of an exception:
        # a run whose every LLM call failed completes normally and writes nothing,
        # indistinguishable from success unless you look at what's left to do.
        progressed=len(remaining) < len(document_ids),
    )


def _interrupts(result: object) -> tuple[object, ...]:
    """Whether `invoke` returned because the graph paused. LangGraph reports an
    interrupt in the returned state under `__interrupt__` rather than by raising, so
    "did it pause" is a lookup; reading it in one place keeps that library detail out
    of the runner's control flow."""
    if isinstance(result, dict):
        return tuple(result.get("__interrupt__") or ())
    return ()
