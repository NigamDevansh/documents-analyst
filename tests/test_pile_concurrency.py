"""Several piles at once: one thread per pile folder, files inside a pile in order.

These use fakes for the runner, so they prove the worker's scheduling — that lanes
really overlap, that one failing lane costs only itself, that one lane is the old
sequential loop — without a database. That two lanes never claim the same pile is
`FOR UPDATE SKIP LOCKED` plus the lease, and the graph-spine tests run the real
`drain_runs` (with the configured lanes) against Postgres.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from documents_analyst.models import LoadedModels, _Serialized, shared_across_threads
from documents_analyst.worker import main as worker_main
from documents_analyst.worker.runner import RunOutcome


def _settings(lanes: int) -> Any:
    return SimpleNamespace(pile_concurrency=lanes, worker_id="test-worker")


def _env() -> Any:
    return worker_main.GraphEnv(checkpointer=object(), models=object(), llm=object())  # type: ignore[arg-type]


class _Queue:
    """A stand-in for the claim: hands each pile out once, to whichever lane asks."""

    def __init__(self, piles: list[str]) -> None:
        self._piles = list(piles)
        self._lock = threading.Lock()
        self.owners: dict[str, str] = {}
        self.threads: dict[str, str] = {}

    def claim(self, worker_id: str) -> str | None:
        with self._lock:
            if not self._piles:
                return None
            pile = self._piles.pop(0)
            self.owners[pile] = worker_id
            self.threads[pile] = threading.current_thread().name
            return pile


def test_two_pile_folders_run_at_the_same_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """folder1 and folder2 each get a lane. The barrier only opens when both runs are
    in progress together, so a sequential worker fails here instead of passing slowly."""
    queue = _Queue(["folder1", "folder2"])
    together = threading.Barrier(2, timeout=5)

    def run_pile(*_a: Any, worker_id: str, **_k: Any) -> RunOutcome | None:
        pile = queue.claim(worker_id)
        if pile is None:
            return None
        together.wait()  # raises BrokenBarrierError if the other pile is not running
        return RunOutcome(pile_id=pile, run_id="r" * 8, documents=5, paused=False)

    monkeypatch.setattr(worker_main.runner, "run_pile", run_pile)
    ran = worker_main.drain_runs(object(), _settings(3), _env())  # type: ignore[arg-type]

    assert ran == 2
    assert queue.threads["folder1"] != queue.threads["folder2"]
    assert queue.owners["folder1"] != queue.owners["folder2"], "each lane names itself"
    assert all(owner.startswith("test-worker/lane-") for owner in queue.owners.values())


def test_one_lane_is_strictly_one_pile_after_another(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _Queue(["folder1", "folder2", "folder3"])
    active = 0
    peak = 0
    lock = threading.Lock()

    def run_pile(*_a: Any, worker_id: str, **_k: Any) -> RunOutcome | None:
        nonlocal active, peak
        pile = queue.claim(worker_id)
        if pile is None:
            return None
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return RunOutcome(pile_id=pile, run_id="r" * 8, documents=1, paused=False)

    monkeypatch.setattr(worker_main.runner, "run_pile", run_pile)
    ran = worker_main.drain_runs(object(), _settings(1), _env())  # type: ignore[arg-type]

    assert ran == 3
    assert peak == 1
    assert set(queue.owners.values()) == {"test-worker"}, "one lane keeps the plain worker id"


def test_a_paused_pile_does_not_stop_its_lane_claiming_the_next(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pile waiting for review is out of the claim, so the lane goes on to the next
    folder in the same pass instead of leaving it for the next tick."""
    queue = _Queue(["needs-review", "clean"])

    def run_pile(*_a: Any, worker_id: str, **_k: Any) -> RunOutcome | None:
        pile = queue.claim(worker_id)
        if pile is None:
            return None
        return RunOutcome(pile_id=pile, run_id="r" * 8, documents=2, paused=pile == "needs-review")

    monkeypatch.setattr(worker_main.runner, "run_pile", run_pile)
    assert worker_main.drain_runs(object(), _settings(1), _env()) == 2  # type: ignore[arg-type]


def test_one_failing_pile_costs_only_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _Queue(["broken", "fine"])
    started = threading.Barrier(2, timeout=5)

    def run_pile(*_a: Any, worker_id: str, **_k: Any) -> RunOutcome | None:
        pile = queue.claim(worker_id)
        if pile is None:
            return None
        started.wait()
        if pile == "broken":
            raise RuntimeError("parser exploded")
        return RunOutcome(pile_id=pile, run_id="r" * 8, documents=1, paused=False)

    monkeypatch.setattr(worker_main.runner, "run_pile", run_pile)
    assert worker_main.drain_runs(object(), _settings(2), _env()) == 1  # type: ignore[arg-type]


def test_reviewed_piles_resume_in_parallel(monkeypatch: pytest.MonkeyPatch) -> None:
    together = threading.Barrier(2, timeout=5)
    resumed: list[str] = []

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

    engine = SimpleNamespace(begin=lambda: _Conn())
    monkeypatch.setattr(worker_main.pile_repo, "resumable", lambda _conn: ["folder1", "folder2"])

    def resume(*_a: Any, pile_id: str, **_k: Any) -> RunOutcome:
        together.wait()
        resumed.append(pile_id)
        return RunOutcome(pile_id=pile_id, run_id="r" * 8, documents=0, paused=False)

    monkeypatch.setattr(worker_main.runner, "resume_after_approval", resume)
    assert worker_main.drain_resumes(engine, _settings(2), _env()) == 2  # type: ignore[arg-type]
    assert sorted(resumed) == ["folder1", "folder2"]


def test_shared_local_models_answer_one_thread_at_a_time() -> None:
    """A fast tokenizer raises "Already borrowed" if two threads enter it; the wrapper
    makes the second caller wait instead."""
    inside = 0
    overlapped = False
    guard = threading.Lock()

    class Model:
        is_available = True

        def classify(self, premise: str, hypothesis: str) -> str:
            nonlocal inside, overlapped
            with guard:
                inside += 1
                overlapped = overlapped or inside > 1
            time.sleep(0.005)
            with guard:
                inside -= 1
            return premise + hypothesis

    model = _Serialized(Model())
    threads = [
        threading.Thread(target=lambda: [model.classify("a", "b") for _ in range(5)])
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not overlapped
    assert model.is_available is True, "attributes pass straight through"
    assert model.classify("x", "y") == "xy"


def test_shared_across_threads_wraps_only_the_local_models() -> None:
    embeddings = object()
    models = LoadedModels(embeddings=embeddings, nli=object(), ner=object())  # type: ignore[arg-type]
    shared = shared_across_threads(models)

    assert shared.embeddings is embeddings
    assert isinstance(shared.nli, _Serialized)
    assert isinstance(shared.ner, _Serialized)


def test_embedding_usage_belongs_to_the_thread_that_made_the_call() -> None:
    """Pile A's cost must not carry pile B's tokens when both embed at once."""
    from documents_analyst_core.embeddings.openai_embeddings import OpenAIEmbeddings
    from documents_analyst_core.portable.llm_client import Usage

    client = OpenAIEmbeddings(api_key="test-key")
    seen: dict[str, int] = {}
    ready = threading.Barrier(2, timeout=5)

    def pile(name: str, tokens: int) -> None:
        client.last_usage = Usage(model="m", input_tokens=tokens, output_tokens=0, attempts=1)
        ready.wait()  # both have written before either reads
        usage = client.last_usage
        seen[name] = usage.input_tokens if usage else -1

    a = threading.Thread(target=pile, args=("a", 11))
    b = threading.Thread(target=pile, args=("b", 22))
    a.start()
    b.start()
    a.join()
    b.join()

    assert seen == {"a": 11, "b": 22}
