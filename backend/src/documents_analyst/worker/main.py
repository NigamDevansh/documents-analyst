"""Worker entrypoint — the discovery loop.

One process, one loop: scan `WATCH_ROOT` every `SCAN_INTERVAL_S`, ingest settled
files under each pile's lease, then run the graph over what is ready. Every
subfolder of `WATCH_ROOT` is a pile, and up to `PILE_CONCURRENCY` piles run at
once, one thread each — folder 2 does not wait for folder 1's extraction. Inside a
pile the files are still read one at a time, in order. With no LLM
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
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from documents_analyst import deliverable
from documents_analyst.db.dsn import psycopg_dsn
from documents_analyst.db.repo import commits as commit_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.graph.checkpointer import checkpointer
from documents_analyst.graph.nodes import resolve
from documents_analyst.models import (
    EmbeddingUnavailable,
    LoadedModels,
    cache_status,
    load_models,
    shared_across_threads,
)
from documents_analyst.pricing import price_warnings
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
    judge: LLMClient | None = None


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
        judge = (
            llm
            if settings.resolved_model_judge == settings.resolved_model_extract
            else build_client(
                provider=settings.llm_provider,
                api_key=settings.llm_api_key,
                model=settings.resolved_model_judge,
                max_retries=settings.llm_max_retries,
            )
        )
        # One set of weights for every pile thread; the local models take turns.
        models = shared_across_threads(load_models(settings))
        for warning in models.warnings:
            LOG.warning("%s", warning)
    except (EmbeddingUnavailable, LLMError) as exc:
        LOG.error("%s", exc)
        LOG.error("the graph will not run this session; discovery and ingest continue")
        yield None
        return

    with checkpointer(settings.database_url) as saver:
        yield GraphEnv(checkpointer=saver, models=models, llm=llm, judge=judge)


_VERIFIED: dict[str, tuple[int, int, str]] = {}
"""pile id → (mtime_ns, size, recorded sha) of the final document as last verified. Read
and written only by the discovery pass, which runs on the main thread, so a plain dict."""


def check_final_document(engine: Engine, settings: Settings, fence: Fence) -> bool:
    """Restore a pile's `final-document.docx` from the record if it is missing, damaged
    or hand-edited. Returns whether it had to.

    Runs every discovery pass, under the lease discovery already holds, because an idle
    pile is never claimed for a run — and Resolve, which repairs inside a run, would
    otherwise not see a deleted file until something else in the folder changed. Cheap
    when nothing changed: a stat, compared with what was last verified; the file is only
    re-read when its size, mtime or the recorded document differs."""
    with engine.begin() as conn:
        record = commit_repo.recorded(conn, fence.pile_id)
    if int(record["deliverable_version"] or 0) == 0:
        return False
    path = deliverable.path_for(settings, str(record["name"]))

    def stamp() -> tuple[int, int, str] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, str(record["deliverable_sha256"]))

    before = stamp()
    if before is not None and _VERIFIED.get(fence.pile_id) == before:
        return False

    def renew(f: Fence) -> None:
        with engine.begin() as conn:
            if not lease.heartbeat(conn, f.pile_id, f.epoch, ttl_s=settings.lease_ttl_s):
                raise LostLease(f"lease for pile {f.pile_id} was taken before the repair")

    repaired = resolve.repair_from_record(
        engine, settings, fence, run_id=str(uuid.uuid4()), record=record, renew=renew
    )
    if repaired:
        LOG.info("%s: restored final-document.docx from the record", record["name"])
    after = stamp()
    if after is not None:
        _VERIFIED[fence.pile_id] = after
    return repaired


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
            # New text, or text gone: re-read now and withdraw only the review items
            # whose evidence changed. An identical copy (`aliased`) adds no text and
            # changes no answer, so it touches nothing.
            if result.added or result.superseded or result.removed or result.restored:
                from documents_analyst.db.repo import decisions as decision_repo

                with engine.begin() as conn:
                    decision_repo.invalidate_review(conn, Fence(pile_id=pile_id, epoch=epoch))
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
            if stop_event is None or not stop_event.is_set():
                try:
                    check_final_document(engine, settings, Fence(pile_id=pile_id, epoch=epoch))
                except LostLease:
                    raise
                except Exception:
                    # The document is an output; failing to restore it (a read-only
                    # folder, say) must not mark the pile `error` and stop its runs.
                    LOG.exception("%s: could not check final-document.docx", pile_dir.name)
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


def pile_lanes(settings: Settings) -> int:
    """How many piles run at once. `getattr` because tests hand in partial settings."""
    return max(1, int(getattr(settings, "pile_concurrency", 1) or 1))


def _in_parallel(lanes: int, jobs: list[Callable[[], int]], what: str) -> int:
    """Run `jobs` on up to `lanes` threads and add up what they return. One job that
    raises is logged and costs only itself — the other piles carry on, the same rule
    the sequential loop applied to one pile in a pass."""
    if not jobs:
        return 0
    if lanes == 1 or len(jobs) == 1:
        total = 0
        for job in jobs:
            try:
                total += job()
            except Exception:
                LOG.exception("%s failed; the other piles carry on", what)
        return total
    total = 0
    with ThreadPoolExecutor(max_workers=min(lanes, len(jobs)), thread_name_prefix="pile") as pool:
        for future in [pool.submit(job) for job in jobs]:
            try:
                total += future.result()
            except Exception:
                LOG.exception("%s failed; the other piles carry on", what)
    return total


def drain_runs(
    engine: Engine,
    settings: Settings,
    graph_env: GraphEnv,
    *,
    stop_event: threading.Event | None = None,
    limit: int = 8,
) -> int:
    """Run every pile that has graph-ready work, until there is none left.

    `PILE_CONCURRENCY` lanes, each a thread that claims a pile, runs it, and claims
    the next. Claiming is `FOR UPDATE SKIP LOCKED` plus a lease (`queue.py`), so two
    lanes never take the same pile: with folder 1 (5 files) and folder 2 (10 files)
    each gets a lane and they run side by side, while a third lane finds nothing and
    returns. With one lane this is the old loop exactly.
    """
    me = worker_id(settings)
    lanes = pile_lanes(settings)

    def lane(n: int) -> Callable[[], int]:
        # A readable owner per lane, so `lease_owner_id` says which thread holds a
        # pile. Fencing is by epoch, never by owner, so sharing a prefix is safe.
        owner = me if lanes == 1 else f"{me}/lane-{n}"
        return lambda: _drain_lane(
            engine, settings, graph_env, owner, stop_event=stop_event, limit=limit
        )

    return _in_parallel(lanes, [lane(n) for n in range(1, lanes + 1)], "a pile run")


def _drain_lane(
    engine: Engine,
    settings: Settings,
    graph_env: GraphEnv,
    me: str,
    *,
    stop_event: threading.Event | None,
    limit: int,
) -> int:
    """One lane: claim, run, repeat. A loop rather than one pile per tick, because a
    scan that discovers four piles should not take four scan intervals to process them.

    `limit` is a circuit breaker, not a batch size. A pile that ends its run still
    holding ingested documents (a node that wrote nothing, a bug) would otherwise be
    re-claimed for ever inside one tick, and the loop would never return to check the
    stop flag.
    """
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
                judge=graph_env.judge,
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
            # `awaiting_review` excludes the paused pile from claim until an approval
            # resumes its checkpoint, so the lane can go straight on to the next pile
            # instead of leaving it for the next tick.
            continue
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

    with engine.begin() as conn:
        candidates = pile_repo.resumable(conn)

    def resume(pile_id: str) -> Callable[[], int]:
        return lambda: _resume_one(engine, settings, graph_env, pile_id, me, stop_event)

    return _in_parallel(
        pile_lanes(settings), [resume(p) for p in candidates[:limit]], "a pile resume"
    )


def _resume_one(
    engine: Engine,
    settings: Settings,
    graph_env: GraphEnv,
    pile_id: str,
    me: str,
    stop_event: threading.Event | None,
) -> int:
    """Resume one reviewed pile. 1 if it ran, 0 if it did not."""
    if stop_event is not None and stop_event.is_set():
        LOG.info("shutdown requested; not resuming another pile")
        return 0
    try:
        outcome = runner.resume_after_approval(
            engine,
            graph_env.checkpointer,
            settings,
            graph_env.models,
            graph_env.llm,
            pile_id=pile_id,
            worker_id=me,
            judge=graph_env.judge,
        )
    except LostLease as exc:
        LOG.info("resume lost its lease: %s", exc)
        return 0
    except Exception:
        # One pile must not take down the pass. It keeps its `awaiting_review`
        # status and checkpoint, so the next tick tries again.
        LOG.exception("pile %s failed to resume", pile_id)
        return 0
    if outcome is None:
        return 0
    LOG.info(
        "%s: resumed run %s — %s",
        outcome.pile_id,
        outcome.run_id[:8],
        "paused again at the human gate" if outcome.paused else "finished",
    )
    return 1


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


def watch_loop(
    engine: Engine,
    settings: Settings,
    *,
    graph_env: GraphEnv | None,
    stopped: threading.Event,
    wake: threading.Event,
) -> None:
    """Keep discovery/repair responsive while one graph drain owns its pile lanes."""
    from concurrent.futures import ThreadPoolExecutor

    def graphs() -> None:
        assert graph_env is not None
        drain_resumes(engine, settings, graph_env, stop_event=stopped)
        drain_runs(engine, settings, graph_env, stop_event=stopped)

    # A wake (an approval's NOTIFY, a new file) that lands while a drain is running can't
    # start one — only one drain runs at a time. Without remembering it, the approval
    # waited out the rest of `scan_interval_s` after the drain ended. So a missed wake is
    # recorded, and that drain's completion wakes the loop. Only a missed one: waking
    # after every drain would spin, since an empty drain finishes at once.
    missed = threading.Event()

    def drain_finished(_: object) -> None:
        if missed.is_set():
            wake.set()

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph-drain") as pool:
        pending = None
        tick = 0
        woken = False
        try:
            while not stopped.is_set():
                wake.clear()
                deep = tick == 0 or bool(
                    settings.scan_deep_every and tick % settings.scan_deep_every == 0
                )
                try:
                    discover(engine, settings, deep=deep, stop_event=stopped)
                    if pending is not None and pending.done():
                        try:
                            pending.result()
                        except Exception:
                            LOG.exception("graph drain failed; retrying on the next pass")
                        pending = None
                    if graph_env is not None and pending is None and not stopped.is_set():
                        missed.clear()
                        pending = pool.submit(graphs)
                        pending.add_done_callback(drain_finished)
                    elif woken and pending is not None:
                        missed.set()
                        if pending.done():  # finished between the check and the flag
                            wake.set()
                except Exception:
                    LOG.exception("discovery failed; retrying next tick")
                tick += 1
                woken = wake.wait(settings.scan_interval_s)
        finally:
            stopped.set()
            # The executor joins graph threads before the engine and models close.


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
    # Prices never stop a call; this only says where the cost report will be unpriced
    # or using a price past its `valid_until`.
    for warning in price_warnings(settings):
        LOG.warning("pricing: %s", warning["message"])

    engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
    LOG.info(
        "worker %s watching %s — up to %d pile(s) at once",
        worker_id(settings),
        settings.watch_root,
        pile_lanes(settings),
    )

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

        watch_loop(engine, settings, graph_env=graph_env, stopped=stopped, wake=wake)

    engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
