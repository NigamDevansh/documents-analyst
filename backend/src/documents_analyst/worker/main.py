"""Worker entrypoint — the discovery loop.

One process, one loop: scan `WATCH_ROOT` every `SCAN_INTERVAL_S`, ingest settled
files under each pile's lease, then run the graph over what is ready. With no LLM
key it degrades to discovery-and-ingest rather than dying. `notify` makes the
scan wake sooner but only reconciles, so its absence costs latency; `watchdog`
was never built. The heartbeat runs between documents, where the lease can
expire.

SIGTERM requests cancellation: the worker stops before the next document or pile
and releases the live lease in its `finally`, letting an in-flight hash or parse
return first.

See docs/WORKER_RUNTIME.md.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import signal
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from documents_analyst.db.dsn import psycopg_dsn
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.graph.checkpointer import checkpointer
from documents_analyst.models import EmbeddingUnavailable, LoadedModels, cache_status, load_models
from documents_analyst.settings import Settings, get_settings
from documents_analyst.worker import lease, notify, runner
from documents_analyst.worker.ingest import ShutdownRequested, ingest_pile
from documents_analyst.worker.lease import Fence, LostLease
from documents_analyst.worker.scanner import list_pile_dirs
from documents_analyst_core.portable.llm_client import LLMClient, LLMError, build_client

LOG = logging.getLogger("documents_analyst.worker")


@dataclass(frozen=True, slots=True)
class GraphEnv:
    """The expensive things a run needs, opened once per process.

    The checkpointer holds a database connection and the models are gigabytes
    of weights; building either per tick would spend most of a scan interval on
    setup. Passed explicitly rather than kept as module state so a test can
    supply an in-memory checkpointer and a `FakeLLM` without patching anything.
    """

    checkpointer: object
    models: LoadedModels
    llm: LLMClient


@contextlib.contextmanager
def graph_environment(settings: Settings, *, enabled: bool = True) -> Iterator[GraphEnv | None]:
    """Open the graph runtime, or explicitly nothing. `enabled=False` yields None
    and loads no models — what `--scan-only` is for, so watching a folder does not
    need an LLM key and 2GB of weights."""
    if not enabled:
        LOG.info("scan-only: the graph will not run this session")
        yield None
        return

    # Degrade to discovery, do not die. Neither of these has an honest fallback,
    # but refusing to run the graph is not refusing to start: the README promises
    # an untouched `.env` starts and names what is missing, so a traceback and
    # restart loop would read as "this project is broken". The worker becomes what
    # `--scan-only` is — finds piles and ingests, claims none for the graph — and
    # every document waits for a key to appear.
    try:
        # Keys before weights: both are string checks that decide whether the
        # graph runs, so a worker with an empty `.env` says so in a second instead
        # of loading two models it is about to throw away.
        llm = build_client(
            provider=settings.llm_provider,
            api_key=settings.llm_api_key,
            model=settings.resolved_model_extract,
            max_retries=settings.llm_max_retries,
        )
        models = load_models(settings)
        for warning in models.warnings:
            LOG.warning("%s", warning)
    except (EmbeddingUnavailable, LLMError) as exc:
        LOG.error("%s", exc)
        LOG.error("the graph will not run this session; discovery and ingest continue")
        yield None
        return

    with checkpointer(settings.database_url) as saver:
        yield GraphEnv(checkpointer=saver, models=models, llm=llm)


def worker_id(settings: Settings) -> str:
    """Readable, so `lease_owner_id` answers "which process holds this pile"."""
    return settings.worker_id or f"{socket.gethostname()}:{os.getpid()}"


def discover(
    engine: Engine, settings: Settings, *, deep: bool, stop_event: threading.Event | None = None
) -> int:
    """One full pass: find piles, ingest each under its own lease. The lease is
    per pile, not per pass, so two workers can ingest different piles at once and
    a slow pile can't block a fast one."""
    me = worker_id(settings)
    touched = 0

    for pile_dir in list_pile_dirs(settings.watch_root):
        if stop_event is not None and stop_event.is_set():
            LOG.info("shutdown requested; not acquiring another pile")
            break
        with engine.begin() as conn:
            pile_id = pile_repo.ensure(conn, name=pile_dir.name, watch_path=pile_dir)

        with engine.begin() as conn:
            epoch = lease.acquire(conn, pile_id, me, settings.lease_ttl_s)
        if epoch is None:
            LOG.debug("pile %s is held by another worker", pile_dir.name)
            continue

        try:
            result = ingest_pile(
                engine,
                fence=Fence(pile_id=pile_id, epoch=epoch),
                watch_path=pile_dir,
                settle_seconds=settings.settle_seconds,
                deep=deep,
                lease_ttl_s=settings.lease_ttl_s,
                stop_requested=stop_event.is_set if stop_event is not None else None,
                skip_root_names=(settings.deliverable_filename,),
            )
            if result.changed or result.quarantined or result.listing_incomplete:
                LOG.info(
                    "%s: +%d new, %d aliased, %d superseded, %d restored, "
                    "%d quarantined, %d removed",
                    pile_dir.name,
                    result.added,
                    result.aliased,
                    result.superseded,
                    result.restored,
                    result.quarantined,
                    result.removed,
                )
                for note in result.notes:
                    LOG.info("  %s", note)
            touched += 1
        except LostLease as exc:
            # Not an error — another worker legitimately took it. Every write was
            # fenced, so nothing of ours landed after the takeover.
            LOG.info("%s: %s", pile_dir.name, exc)
        except ShutdownRequested:
            LOG.info("%s: shutdown requested; releasing its lease", pile_dir.name)
        except Exception:
            # One pile must not take down the loop. The lease is released in
            # `finally`, so a raising pile is immediately available again.
            LOG.exception("pile %s failed to ingest", pile_dir.name)
            with engine.begin() as conn:
                pile_repo.set_status(conn, Fence(pile_id=pile_id, epoch=epoch), "error")
        finally:
            with engine.begin() as conn:
                lease.release(conn, pile_id, epoch)

    return touched


def drain_runs(
    engine: Engine,
    settings: Settings,
    graph_env: GraphEnv,
    *,
    stop_event: threading.Event | None = None,
    limit: int = 8,
) -> int:
    """Run every pile that has graph-ready work, until there is none left.

    A loop rather than one pile per tick, because a scan that discovers four
    piles should not take four scan intervals — two minutes of a demo doing
    nothing visible — to process them.

    `limit` is a circuit breaker, not a batch size. A pile that ends its run
    still holding ingested documents (a node that wrote nothing, a bug) would
    otherwise be re-claimed for ever inside one tick, and the loop would never
    return to check the stop flag.
    """
    me = worker_id(settings)
    ran = 0

    while ran < limit:
        if stop_event is not None and stop_event.is_set():
            LOG.info("shutdown requested; not claiming another run")
            break
        try:
            outcome = runner.run_pile(
                engine,
                graph_env.checkpointer,
                settings,
                graph_env.models,
                graph_env.llm,
                worker_id=me,
            )
        except LostLease as exc:
            LOG.info("run lost its lease: %s", exc)
            break
        if outcome is None:
            break

        LOG.info(
            "%s: run %s over %d document(s) — %s",
            outcome.pile_id,
            outcome.run_id[:8],
            outcome.documents,
            "paused at the human gate" if outcome.paused else "finished",
        )
        ran += 1
        if outcome.paused:
            # A paused pile still has ingested documents, but `awaiting_review`
            # excludes it from claim until an approval resumes its checkpoint.
            break
        if not outcome.progressed:
            # The run retired nothing, so the pile is as claimable as before and
            # looping would repeat it verbatim. This is what a provider outage
            # looks like: every call fails, nothing can be marked extracted, so
            # wait for the next tick.
            LOG.warning(
                "%s: a run finished without retiring any document — "
                "pausing the drain until the next scan",
                outcome.pile_id,
            )
            break

    return ran


def drain_resumes(
    engine: Engine,
    settings: Settings,
    graph_env: GraphEnv,
    *,
    stop_event: threading.Event | None = None,
    limit: int = 8,
) -> int:
    """Resume every pile that finished its review. Returns how many ran. The
    condition is an empty queue, not an approval: the gate re-interrupts while
    anything is pending, and a reviewer who rejected everything has still finished
    (that run must reach Reconcile and end). A sweep, not a notification handler —
    `NOTIFY` only makes it sooner; a lost one is picked up next tick."""
    me = worker_id(settings)
    resumed = 0

    with engine.begin() as conn:
        candidates = pile_repo.resumable(conn)

    for pile_id in candidates[:limit]:
        if stop_event is not None and stop_event.is_set():
            LOG.info("shutdown requested; not resuming another pile")
            break
        try:
            outcome = runner.resume_after_approval(
                engine,
                graph_env.checkpointer,
                settings,
                graph_env.models,
                graph_env.llm,
                pile_id=pile_id,
                worker_id=me,
            )
        except LostLease as exc:
            LOG.info("resume lost its lease: %s", exc)
            continue
        except Exception:
            # One pile must not take down the pass. It keeps its `awaiting_review`
            # status and checkpoint, so the next tick tries again.
            LOG.exception("pile %s failed to resume", pile_id)
            continue
        if outcome is None:
            continue
        resumed += 1
        LOG.info(
            "%s: resumed run %s — %s",
            outcome.pile_id,
            outcome.run_id[:8],
            "paused again at the human gate" if outcome.paused else "finished",
        )

    return resumed


def run_once(
    engine: Engine,
    settings: Settings,
    *,
    deep: bool = True,
    stop_event: threading.Event | None = None,
    graph_env: GraphEnv | None = None,
) -> int:
    """One pass: discover, resume what humans decided, run what is ready.
    Discovery first, so a document found this pass is extracted this pass. Resumes
    before new runs — a pile a human finished has someone waiting, a fresh run can
    wait a tick. `graph_env` is optional so discovery stays runnable without 2GB of
    weights."""
    touched = discover(engine, settings, deep=deep, stop_event=stop_event)
    if graph_env is not None:
        drain_resumes(engine, settings, graph_env, stop_event=stop_event)
        drain_runs(engine, settings, graph_env, stop_event=stop_event)
    return touched


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="discover and ingest, but do not run the graph (no models, no LLM key)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(message)s")

    status = cache_status(settings)
    if status["complete"]:
        LOG.info("model cache ready at %s", status["root"])
    else:
        LOG.warning("model cache at %s is incomplete: %s", status["root"], status["missing"])

    engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    LOG.info("worker %s watching %s", worker_id(settings), settings.watch_root)

    stopped = threading.Event()
    # One event, set by three things: a NOTIFY, a signal, and the listener's own
    # connect. The loop waits on it instead of sleeping, so an approval is picked
    # up in about a second — and if the listener never starts, the timer still runs.
    wake = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        LOG.info("stopping after the current filesystem operation; releasing active leases")
        stopped.set()
        wake.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    # The graph environment is opened once and held: the checkpointer owns a
    # connection and the models are gigabytes, so loading them per tick would
    # spend most of a 30-second interval on weights.
    with graph_environment(settings, enabled=not args.scan_only) as graph_env:
        if args.once:
            run_once(engine, settings, deep=True, stop_event=stopped, graph_env=graph_env)
            engine.dispose()
            return 0

        notify.start_listener(psycopg_dsn(settings.database_url), wake, stopped)

        tick = 0
        while not stopped.is_set():
            # Boot is always deep — startup is when we are most likely to have
            # missed something — then every Nth tick, to bound how long an
            # mtime-preserving copy can hide.
            deep = tick == 0 or bool(
                settings.scan_deep_every and tick % settings.scan_deep_every == 0
            )
            # Cleared BEFORE the pass, not between the pass and the wait: an
            # approval landing WHILE a pass runs is too late for it, so the flag
            # must survive into the wait and start the next pass immediately.
            wake.clear()
            try:
                run_once(engine, settings, deep=deep, stop_event=stopped, graph_env=graph_env)
            except Exception:
                LOG.exception("pass failed; retrying next tick")
            tick += 1
            wake.wait(settings.scan_interval_s)

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
