"""The API. Endpoints, the gate's only door, and the event stream.

Marked `db`, and against a real Postgres rather than a mocked session, because
almost everything worth asserting here *is* a database property: a NOTIFY that
fires from a trigger, a cursor that has to be monotonic across runs, an UPDATE
that must not match a row somebody already decided.

No LLM and no network. The final-document routes read a file the worker wrote;
the writing itself is covered in `test_commit.py`.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import create_async_engine

from documents_analyst import deliverable
from documents_analyst.api.events import EventBroker, sse_frames
from documents_analyst.api.main import create_app
from documents_analyst.api.routes.documents import safe_filename
from documents_analyst.db import migrate
from documents_analyst.db.dsn import psycopg_dsn
from documents_analyst.db.repo import agreements as agreement_repo
from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import events as event_repo
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import findings as finding_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.settings import Settings
from documents_analyst.worker import lease
from documents_analyst.worker.lease import Fence
from documents_analyst_core.provenance.models import Citation, GroundedFact

pytestmark = pytest.mark.db

SCRATCH = "analyst_api_test"

CONTRACT = """MASTER SERVICES AGREEMENT

Entered into between Acme Corporation and Beta Logistics Inc.

Payment is due Net 30 days from the date of invoice.
"""


# ── fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — needs a live Postgres")

    base = url.rsplit("/", 1)[0]
    admin = create_engine(base + "/postgres", isolation_level="AUTOCOMMIT", future=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
            conn.execute(text(f'CREATE DATABASE "{SCRATCH}"'))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"cannot reach Postgres: {exc}")

    scratch_url = f"{base}/{SCRATCH}"
    migrate.run(scratch_url)
    made = create_engine(scratch_url, future=True)
    yield made

    made.dispose()
    with admin.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :d AND pid <> pg_backend_pid()"
            ),
            {"d": SCRATCH},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{SCRATCH}"'))
    admin.dispose()


def settings_for(engine: Engine, tmp_path: Path, **overrides: Any) -> Settings:
    return Settings(
        database_url=engine.url.render_as_string(hide_password=False),
        watch_root=tmp_path,
        **overrides,
    )


@pytest.fixture
def client(engine: Engine, tmp_path: Path) -> Iterator[TestClient]:
    """A real app with a real lifespan — pool and LISTEN connection included.

    `TestClient` runs the lifespan, which is the point: the event broker is a
    background task, and an app constructed without one would pass tests that
    the deployed app fails.
    """
    with TestClient(create_app(settings_for(engine, tmp_path))) as made:
        yield made


@pytest.fixture(autouse=True)
def clean(engine: Engine) -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE piles CASCADE"))
        conn.execute(text("TRUNCATE stage_events, stage_runs"))
    yield


def make_pile(engine: Engine, tmp_path: Path, name: str = "acme") -> tuple[str, Path]:
    folder = tmp_path / name
    folder.mkdir(parents=True, exist_ok=True)
    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name=name, watch_path=folder)
    return pile_id, folder


@contextmanager
def held(engine: Engine, pile_id: str) -> Iterator[Fence]:
    """A real lease, taken and released.

    Seeding through the fenced repositories rather than around them: a helper
    that inserted rows with raw SQL would set up states the worker cannot
    actually produce, and the test would then be asserting about a database
    nobody's code can create. Released on the way out because a held lease
    changes what `resumable` returns.
    """
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test-worker", 90)
    assert epoch is not None
    try:
        yield Fence(pile_id=pile_id, epoch=epoch)
    finally:
        with engine.begin() as conn:
            lease.release(conn, pile_id, epoch)


def add_document(engine: Engine, pile_id: str, filename: str, body: str = CONTRACT) -> str:
    with held(engine, pile_id) as fence, engine.begin() as conn:
        return doc_repo.insert(
            conn,
            fence,
            filename=filename,
            path=f"/tmp/{filename}",
            sha256=uuid.uuid4().hex,
            stat={"size": len(body), "mtime_ns": 0, "inode": 0},
            extracted_text=body,
            text_sha256=uuid.uuid4().hex,
            status="ingested",
        )


OPTIONS = [
    {"value_group": 0, "value_text": "Net 30", "value_norm": "30 days", "sources": []},
    {"value_group": 1, "value_text": "Net 45", "value_norm": "45 days", "sources": []},
]


def raise_item(engine: Engine, pile_id: str, kind: str, **kwargs: Any) -> str:
    """A review item as the classifier writes it.

    A `conflict` or `amendment` carries its options, because that is the only
    shape the graph produces — and an approval names one of them by number. A
    helper that raised them bare would let a test approve a group the item never
    offered, which the API now refuses precisely because such an approval cannot
    resolve anything.
    """
    if kind in ("conflict", "amendment"):
        kwargs.setdefault("payload", {"field_key": "payment_terms_days", "options": OPTIONS})
    with held(engine, pile_id) as fence, engine.begin() as conn:
        item = decision_repo.raise_decision(
            conn, fence, kind=kind, ref_id=str(uuid.uuid4()), **kwargs
        )
    assert item is not None
    return item


def set_status(engine: Engine, pile_id: str, status: str) -> None:
    with held(engine, pile_id) as fence, engine.begin() as conn:
        pile_repo.set_status(conn, fence, status)


# ── the navigator ─────────────────────────────────────────────────────────


def test_piles_lists_counts_without_multiplying_them(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """Two documents and three decisions is 2 and 3, not 6 and 6.

    The natural way to write this query — four joins onto `piles` — multiplies
    every count by every other, and the numbers stay plausible while being
    wrong, which is the worst kind of wrong for a dashboard.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    add_document(engine, pile_id, "01_msa.md")
    add_document(engine, pile_id, "02_sow.md")
    for _ in range(3):
        raise_item(engine, pile_id, "conflict")

    body = client.get("/piles").json()
    row = next(p for p in body["piles"] if p["id"] == pile_id)
    assert (row["documents"], row["pending_decisions"]) == (2, 3)


def test_a_pile_with_no_documents_still_appears(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """What a folder looks like one second after somebody creates it."""
    pile_id, _ = make_pile(engine, tmp_path, "empty")
    row = next(p for p in client.get("/piles").json()["piles"] if p["id"] == pile_id)
    assert row["documents"] == 0


def test_an_unknown_pile_is_404_not_an_empty_body(client: TestClient) -> None:
    assert client.get(f"/piles/{uuid.uuid4()}").status_code == 404


# ── upload: one ingestion path ────────────────────────────────────────────


def test_upload_writes_a_file_and_deliberately_no_document_row(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """The API does not decide what a document is. Discovery does.

    This is the corrected §3.1 behaviour. Inserting the row here would mean a
    second implementation of hashing, alias detection, quarantine and
    supersession living in a request handler, and the two only have to disagree
    once to put a duplicate contract in somebody's register.
    """
    pile_id, folder = make_pile(engine, tmp_path)

    response = client.post(
        f"/piles/{pile_id}/documents",
        files={"file": ("03_amendment.md", CONTRACT.encode(), "text/markdown")},
    )

    assert response.status_code == 202
    assert (folder / "03_amendment.md").read_text() == CONTRACT
    with engine.begin() as conn:
        assert doc_repo.for_pile(conn, pile_id) == []
    # And no half-written staging file was left behind.
    assert not list(folder.glob(".*upload"))


def test_uploading_a_file_named_like_the_final_document_is_refused(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """The scanner skips that name and the next commit overwrites it — accepting it
    would answer 202 for bytes that are never read and then destroyed."""
    pile_id, folder = make_pile(engine, tmp_path)
    response = client.post(
        f"/piles/{pile_id}/documents",
        files={"file": ("final-document.docx", b"PK\x03\x04", "application/octet-stream")},
    )
    assert response.status_code == 409
    assert not (folder / "final-document.docx").exists()


def test_upload_then_discovery_produces_exactly_one_document(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """The other half: the file the API wrote is ingested like any other.

    Runs the real discovery pass. If the route had inserted its own row, this
    would end with two — one guessed by the API and one found on disk.
    """
    from documents_analyst.worker import main as worker_main

    pile_id, _ = make_pile(engine, tmp_path)
    client.post(
        f"/piles/{pile_id}/documents",
        files={"file": ("04_sow.md", CONTRACT.encode(), "text/markdown")},
    )

    settings = settings_for(engine, tmp_path, settle_seconds=0)
    worker_main.discover(engine, settings, deep=True)

    with engine.begin() as conn:
        documents = doc_repo.for_pile(conn, pile_id)
    assert [d["filename"] for d in documents] == ["04_sow.md"]
    assert documents[0]["status"] == "ingested"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("/absolute/msa.docx", "msa.docx"),
        ("..\\..\\windows\\system32", "system32"),
        (".hidden.md", "hidden.md"),
        ("re; rm -rf *.md", "re_ rm -rf _.md"),
    ],
)
def test_an_uploaded_filename_cannot_escape_the_pile(raw: str, expected: str) -> None:
    """An upload filename is attacker-controlled text used as a path.

    A leading dot is stripped rather than accepted, because the scanner ignores
    dotfiles — keeping it would mean silently storing a file that is never read.
    """
    assert safe_filename(raw) == expected


def test_upload_to_an_unknown_pile_is_refused(client: TestClient) -> None:
    response = client.post(
        f"/piles/{uuid.uuid4()}/documents", files={"file": ("x.md", b"x", "text/markdown")}
    )
    assert response.status_code == 404


# ── the human gate's only door ────────────────────────────────────────────


def test_an_approval_without_a_name_is_refused_and_changes_nothing(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """`decided_by` is the only authority Commit has for an external edit.

    So a nameless approval is rejected rather than filled in with a
    placeholder — "api" in an audit trail is worse than a gap, because it looks
    like an answer.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    item = raise_item(engine, pile_id, "conflict")

    response = client.post(f"/decisions/{item}/approve", json={})

    assert response.status_code == 422
    with engine.begin() as conn:
        assert decision_repo.open_count(conn, pile_id) == 1


def test_approving_records_who_and_when(client: TestClient, engine: Engine, tmp_path: Path) -> None:
    pile_id, _ = make_pile(engine, tmp_path)
    item = raise_item(engine, pile_id, "conflict")

    response = client.post(
        f"/decisions/{item}/approve",
        json={"note": "the amendment governs", "chosen_value_group": 1},
        headers={"X-Actor": "devansh"},
    )

    assert response.status_code == 200
    with engine.begin() as conn:
        stored = decision_repo.get(conn, item)
    assert stored is not None
    assert (stored["status"], stored["decided_by"], stored["chosen_value_group"]) == (
        "approved",
        "devansh",
        1,
    )
    assert stored["decided_at"] is not None


def test_a_second_decision_on_the_same_item_loses_and_is_told_so(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """Two reviewers with the same queue open is not a rare case.

    The loser must be told rather than silently overwriting the winner's name on
    an approval that may already have been committed — which would leave the
    register saying one thing and the trail attributing it to someone else.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    item = raise_item(engine, pile_id, "conflict")
    # A conflict needs a winner named — approving one without a
    # `chosen_value_group` is refused, because it would resolve nothing.
    client.post(
        f"/decisions/{item}/approve",
        json={"chosen_value_group": 0},
        headers={"X-Actor": "first"},
    )

    second = client.post(f"/decisions/{item}/reject", json={}, headers={"X-Actor": "second"})

    assert second.status_code == 409
    assert "first" in second.json()["detail"]
    with engine.begin() as conn:
        stored = decision_repo.get(conn, item)
    assert stored is not None
    assert (stored["status"], stored["decided_by"]) == ("approved", "first")


def test_the_batch_lane_settles_amendments_and_leaves_everything_else(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """A fast lane for one mechanical class — never an approve-all button.

    A batch that also swept up conflicts and rulebook findings would be exactly
    the thing the gate exists to prevent: agreeing to something nobody looked at.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    for _ in range(3):
        raise_item(engine, pile_id, "amendment")
    conflict = raise_item(engine, pile_id, "conflict")
    finding = raise_item(engine, pile_id, "finding")

    body = client.post(
        f"/piles/{pile_id}/decisions/approve-batch",
        json={},
        headers={"X-Actor": "devansh"},
    ).json()

    assert body["count"] == 3
    assert body["still_pending"] == 2
    with engine.begin() as conn:
        assert decision_repo.get(conn, conflict)["status"] == "pending"  # type: ignore[index]
        assert decision_repo.get(conn, finding)["status"] == "pending"  # type: ignore[index]


def test_the_batch_lane_refuses_a_kind_it_does_not_own(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """Asking it to batch conflicts is a 422 from the schema, not a filtered pass.

    Silently ignoring the field would make the caller believe conflicts had been
    approved.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    response = client.post(
        f"/piles/{pile_id}/decisions/approve-batch",
        json={"kinds": ["conflict"]},
        headers={"X-Actor": "devansh"},
    )
    assert response.status_code == 422


# ── resume: empty queue, not approval ─────────────────────────────────────


def test_a_half_reviewed_pile_is_not_resumable(engine: Engine, tmp_path: Path) -> None:
    """One approval out of two does not resume the run.

    The gate re-interrupts while anything is pending, so resuming here would
    take a lease, start a run, pause again and write two events — per click.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    first = raise_item(engine, pile_id, "conflict")
    raise_item(engine, pile_id, "conflict")
    set_status(engine, pile_id, "awaiting_review")
    with engine.begin() as conn:
        decision_repo.settle(conn, first, status="approved", actor="devansh")
        assert pile_repo.resumable(conn) == []


def test_rejecting_every_item_still_makes_the_pile_resumable(
    engine: Engine, tmp_path: Path
) -> None:
    """The condition is an empty queue, NOT an approval.

    A reviewer who rejects everything has still finished reviewing, and that run
    has to reach Reconcile and end. Keyed on "something was approved", the pile
    would sit in `awaiting_review` for ever because nobody agreed to anything.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    items = [raise_item(engine, pile_id, "conflict") for _ in range(2)]
    set_status(engine, pile_id, "awaiting_review")
    with engine.begin() as conn:
        for item in items:
            decision_repo.settle(conn, item, status="rejected", actor="devansh")
        assert pile_repo.resumable(conn) == [pile_id]


def test_a_leased_pile_is_not_offered_for_resume(engine: Engine, tmp_path: Path) -> None:
    """A worker is already inside it. Two resumes of one checkpoint is one too many."""
    pile_id, _ = make_pile(engine, tmp_path)
    item = raise_item(engine, pile_id, "conflict")
    set_status(engine, pile_id, "awaiting_review")
    with engine.begin() as conn:
        decision_repo.settle(conn, item, status="approved", actor="devansh")
        lease.acquire(conn, pile_id, "worker-1", 90)
        assert pile_repo.resumable(conn) == []


def test_settling_a_decision_notifies_the_work_channel(engine: Engine, tmp_path: Path) -> None:
    """The wake-up is a trigger on the table, not a line in the approve handler.

    So an approval applied with psql — which is what happens while debugging a
    stuck pile — wakes the worker exactly the same way the UI does.
    """
    import psycopg

    pile_id, _ = make_pile(engine, tmp_path)
    item = raise_item(engine, pile_id, "conflict")

    dsn = psycopg_dsn(engine.url.render_as_string(hide_password=False))
    with psycopg.connect(dsn, autocommit=True) as listener:
        listener.execute("LISTEN analyst_work")
        with engine.begin() as conn:
            decision_repo.settle(conn, item, status="approved", actor="devansh")
        received = [n.payload for n in listener.notifies(timeout=5.0, stop_after=1)]

    assert received == [pile_id]


# ── the event stream ──────────────────────────────────────────────────────


def test_the_replay_cursor_is_the_table_id_not_the_per_run_seq(
    engine: Engine, tmp_path: Path
) -> None:
    """A reconnect across a run boundary must not skip the new run's start.

    `seq` restarts at 1 for every run. A browser that saw seq 3 of run A and
    reconnects during run B would ask for "everything after 3" and be handed
    nothing until B passed three events — silently losing the three that started
    it. This is that exact scenario, and it fails on any cursor but `id`.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        for i in range(3):
            event_repo.append(conn, pile_id=pile_id, run_id=run_a, type=f"a.{i}")
        last_of_a = event_repo.since_id(conn, pile_id)[-1]["id"]
        for i in range(3):
            event_repo.append(conn, pile_id=pile_id, run_id=run_b, type=f"b.{i}")

        missed = event_repo.since_id(conn, pile_id, int(last_of_a))

    assert [row["type"] for row in missed] == ["b.0", "b.1", "b.2"]
    assert [row["seq"] for row in missed] == [1, 2, 3]  # seq restarted; id did not


def test_an_event_insert_notifies_without_anyone_remembering_to(
    engine: Engine, tmp_path: Path
) -> None:
    """`INSERT INTO stage_events` cannot happen without a notification.

    Eleven places write this table. In Python, the one that forgets is silent:
    the row lands, the timeline is right, and only the live stream is missing an
    event. As a trigger it is a property of the table — including from psql.
    """
    import json

    import psycopg

    pile_id, _ = make_pile(engine, tmp_path)
    dsn = psycopg_dsn(engine.url.render_as_string(hide_password=False))
    with psycopg.connect(dsn, autocommit=True) as listener:
        listener.execute("LISTEN analyst_events")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO stage_events (pile_id, run_id, seq, type) "
                    "VALUES (:p, :r, 1, 'hand.written')"
                ),
                {"p": pile_id, "r": str(uuid.uuid4())},
            )
        payload = json.loads(next(iter(listener.notifies(timeout=5.0, stop_after=1))).payload)

    assert payload["pile_id"] == pile_id
    assert payload["type"] == "hand.written"
    assert "payload" not in payload  # identity only — never the unbounded column


async def test_the_stream_replays_then_delivers_live(engine: Engine, tmp_path: Path) -> None:
    """One code path for both, which is why a lost notification costs a query.

    The stream is fed a row written *before* it connected and one written after,
    and does not distinguish them: both arrive by re-reading from the cursor.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    with engine.begin() as conn:
        event_repo.append(conn, pile_id=pile_id, run_id=str(uuid.uuid4()), type="before")

    url = engine.url.render_as_string(hide_password=False)
    async_engine = create_async_engine(url)
    broker = EventBroker(psycopg_dsn(url))
    await broker.start()
    try:
        frames = sse_frames(
            engine=async_engine,
            broker=broker,
            pile_id=pile_id,
            after_id=0,
            keepalive_s=0.2,
            limit=100,
        )
        first = await asyncio.wait_for(frames.__anext__(), timeout=5)
        assert first["event"] == "before"

        with engine.begin() as conn:
            event_repo.append(conn, pile_id=pile_id, run_id=str(uuid.uuid4()), type="after")

        # Keepalive frames are allowed through; what matters is that the live
        # row arrives without the stream being restarted.
        for _ in range(40):
            frame = await asyncio.wait_for(frames.__anext__(), timeout=5)
            if frame["event"] == "after":
                break
        else:  # pragma: no cover — a stuck stream should fail loudly
            pytest.fail("the live event never arrived")
        await frames.aclose()
    finally:
        await broker.stop()
        await async_engine.dispose()


async def test_a_stalled_subscriber_cannot_grow_the_brokers_memory(
    engine: Engine, tmp_path: Path
) -> None:
    """Backpressure with no bookkeeping: the queue holds one wake-up token.

    A client that stopped reading costs one slot for as long as it lingers,
    rather than one slot per event for as long as the run lasts. Ten thousand
    wake-ups later the queue still holds one.
    """
    broker = EventBroker("postgresql://unused")
    with broker.subscribe("pile-1") as queue:
        for _ in range(10_000):
            broker.wake("pile-1")
        assert queue.qsize() == 1
    assert broker._subscribers == {}  # and it unsubscribes on the way out


# ── authorization ─────────────────────────────────────────────────────────


def test_without_a_token_the_api_is_open(client: TestClient) -> None:
    """The default. A take-home that demands a credential before showing you
    anything is a take-home that gets closed."""
    assert client.get("/piles").status_code == 200


def test_with_a_token_set_the_routes_close_but_health_stays_open(
    engine: Engine, tmp_path: Path
) -> None:
    """`/` and `/health` are deliberately outside the guard.

    The landing page's whole job is naming what is misconfigured, and a health
    check that needs a credential cannot be used by whatever restarts the
    container.
    """
    settings = settings_for(engine, tmp_path, api_token="s3cret")
    with TestClient(create_app(settings)) as guarded:
        assert guarded.get("/piles").status_code == 401
        assert guarded.get("/health").status_code == 200
        assert guarded.get("/").status_code == 200
        ok = guarded.get("/piles", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert guarded.get("/piles", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_health_reports_whether_the_listener_is_up(client: TestClient) -> None:
    """A stream that silently stopped delivering looks exactly like a system
    with nothing to say."""
    body = client.get("/health").json()
    assert body["events"]["listening"] is True
    assert body["auth"]["token_required"] is False


def test_health_tells_the_setup_guide_where_pile_folders_go(engine: Engine, tmp_path: Path) -> None:
    """Inside a container `watch_root` is `/data/watched`, which is not a path anyone
    can put a folder in — so the host side of the mount wins when compose supplies it."""
    plain = settings_for(engine, tmp_path, watch_dir_host="")
    with TestClient(create_app(plain)) as c:
        watch = c.get("/health").json()["watch"]
    assert watch["path"] == str(tmp_path)
    assert watch["deliverable_filename"] == "final-document.docx"
    assert watch["pile_concurrency"] >= 1

    hosted = settings_for(engine, tmp_path, watch_dir_host="/Users/someone/contracts")
    with TestClient(create_app(hosted)) as c:
        assert c.get("/health").json()["watch"]["path"] == "/Users/someone/contracts"


# ── the API has no queries of its own ─────────────────────────────────────


def test_no_route_writes_its_own_sql() -> None:
    """The anti-drift rule, enforced rather than intended.

    Every route reads through `documents_analyst.db.repo.*` — the same functions the graph
    nodes call, fences and all. The day a handler writes its own query, the
    system has two definitions of something, and the one that drifts is the one
    without the test. `run_sync` is what makes that avoidable at all: the sync
    repos work unchanged on an async connection.
    """
    routes = Path(__file__).resolve().parent.parent / "backend/src/documents_analyst/api"
    offenders = [
        path.name
        for path in sorted(routes.rglob("*.py"))
        if "sqlalchemy import text" in path.read_text() or "text(" in _code(path)
    ]
    assert offenders == [], f"these API modules build their own SQL: {offenders}"


def _code(path: Path) -> str:
    """Source with docstrings and comments removed — a docstring that mentions
    `text(` is prose, not a query."""
    import io
    import tokenize

    kept: list[str] = []
    with path.open("rb") as handle:
        for token in tokenize.tokenize(io.BytesIO(handle.read()).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return " ".join(kept)


# ── citations: every offset lands on its quote ────────────────────────────


def test_every_citation_offset_lands_on_its_own_quote(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """The click-a-value-see-the-sentence contract, checked end to end.

    Two endpoints have to agree for a highlight to be correct: `/citations`
    hands out `char_start/char_end`, and `/documents/{id}/text` hands out the
    string those index into. This slices one with the other and compares it to
    the stored quote — which is the same comparison Gate 1 made at extraction,
    re-run across the HTTP boundary where an encoding or a truncation would
    show up.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    document_id = add_document(engine, pile_id, "01_msa.md")
    quote = "Payment is due Net 30 days from the date of invoice."
    start = CONTRACT.index(quote)

    with held(engine, pile_id) as fence, engine.begin() as conn:
        agreement_id = agreement_repo.ensure(
            conn, fence, parties=("acme corporation", "beta logistics inc"), title="MSA"
        )
        conn.execute(
            text("UPDATE documents SET agreement_id = :a WHERE id = :d"),
            {"a": agreement_id, "d": document_id},
        )
        fact_repo.replace_for_document(
            conn,
            fence,
            document_id=document_id,
            agreement_id=agreement_id,
            facts=[
                GroundedFact(
                    field_key="payment_terms_days",
                    value_text="Net 30 days",
                    value_normalized="30 days",
                    citation=Citation(
                        document_id=document_id,
                        chunk_id=None,
                        start=start,
                        end=start + len(quote),
                        quote=quote,
                    ),
                    confidence=0.9,
                )
            ],
        )

    citations = client.get(f"/piles/{pile_id}/citations").json()["citations"]
    assert len(citations) == 1

    body = client.get(f"/documents/{document_id}/text").json()["text"]
    for citation in citations:
        assert body[citation["char_start"] : citation["char_end"]] == citation["source_quote"]


def test_the_source_text_is_the_extracted_string_not_the_file(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """Offsets were recorded against `extracted_text`, so that is what is served.

    Handing back the original bytes would put every highlight in the wrong
    place the moment a PDF's extraction differed from its rendering by one
    character.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    document_id = add_document(engine, pile_id, "01_msa.md")
    body = client.get(f"/documents/{document_id}/text").json()
    assert body["text"] == CONTRACT
    assert body["length"] == len(CONTRACT)


def test_a_resume_that_raises_leaves_the_pile_reviewable(engine: Engine, tmp_path: Path) -> None:
    """Otherwise a failed resume strands the review with no way back.

    `status = 'running'` describes a run that is not running, and for a reviewed
    pile it is a one-way door: the normal claim wants `ingested` documents,
    which it no longer has, and the resume sweep wants `awaiting_review`. The
    checkpoint sits at the gate and nothing can reach it.

    Found live — a resumed run raised because the pile's `rulebook_path` had
    been recorded by a worker inside a container and pointed at a path the host
    process could not read. Nothing was wrong with the pile itself.
    """
    from documents_analyst.worker import runner

    pile_id, _ = make_pile(engine, tmp_path)
    item = raise_item(engine, pile_id, "conflict")
    set_status(engine, pile_id, "awaiting_review")
    with engine.begin() as conn:
        decision_repo.settle(conn, item, status="approved", actor="devansh")

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("a node failed mid-resume")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(runner, "_invoke", explode)
        with pytest.raises(RuntimeError):
            runner.resume_after_approval(
                engine,
                checkpointer=None,
                settings=settings_for(engine, tmp_path),
                models=None,
                llm=None,
                pile_id=pile_id,
                worker_id="test-worker",
            )

    with engine.begin() as conn:
        assert pile_repo.get(conn, pile_id)["status"] == "awaiting_review"  # type: ignore[index]
        assert pile_repo.resumable(conn) == [pile_id], "the next tick must be able to try again"


def test_the_worker_listener_wakes_on_a_notification(engine: Engine, tmp_path: Path) -> None:
    """The other end of the trigger: an approval wakes the worker in ~1s.

    Asserted because the listener is the one part of the loop with no
    reconciling backstop of its own — if the thread dies quietly, every
    approval still lands and still gets picked up, just up to
    `SCAN_INTERVAL_S` later. That is a latency regression nobody would notice
    from the outside, which is exactly the kind worth pinning down.
    """
    import threading

    from documents_analyst.worker import notify

    pile_id, _ = make_pile(engine, tmp_path)
    item = raise_item(engine, pile_id, "conflict")

    wake, stop = threading.Event(), threading.Event()
    dsn = psycopg_dsn(engine.url.render_as_string(hide_password=False))
    thread = notify.start_listener(dsn, wake, stop)
    try:
        assert wake.wait(5), "the listener wakes once on connect, so nothing is missed"
        wake.clear()
        with engine.begin() as conn:
            decision_repo.settle(conn, item, status="approved", actor="devansh")
        assert wake.wait(5), "an approval must wake the worker rather than wait for the timer"
    finally:
        stop.set()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    "path",
    [
        "/piles/not-a-uuid",
        "/piles/not-a-uuid/deliverable",
        "/piles/not-a-uuid/decisions",
        "/decisions/not-a-uuid",
        "/documents/not-a-uuid/text",
        "/piles/not-a-uuid/deliverable/document",
    ],
)
def test_a_malformed_id_is_a_sentence_not_a_500(client: TestClient, path: str) -> None:
    """A bad id reached Postgres and came back as `invalid input syntax for
    type uuid` — a 500 with a database error in the body.

    Found by pointing an MCP client at the API and calling a tool with a
    placeholder id, which is the first thing an agent does. One router-level
    dependency now rejects every `*_id` that is not a UUID, so a route added
    later cannot forget it.
    """
    response = client.get(path)
    assert response.status_code == 404
    assert "is not a valid id" in response.json()["detail"]


# ── a decision changes the thing it was raised about ──────────────────────


def _finding(engine: Engine, pile_id: str) -> tuple[str, str]:
    """A real finding and the review item that carries it."""
    with held(engine, pile_id) as fence, engine.begin() as conn:
        agreement_id = agreement_repo.ensure(
            conn, fence, parties=("acme corporation", "beta logistics inc"), title="MSA"
        )
        recorded = finding_repo.record(
            conn,
            fence,
            agreement_id=agreement_id,
            rule_id="R001",
            severity="high",
            evaluated_by="check",
            message="every agreement must state a liability cap",
            citations=[],
        )
        assert recorded is not None
        item = decision_repo.raise_decision(
            conn, fence, kind="finding", ref_id=recorded[0], recommendation="cap is missing"
        )
    assert item is not None
    return recorded[0], item


def test_answering_a_finding_moves_the_finding(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """The decision row changed and the finding stayed `open`.

    So `GET /piles/{id}/findings` kept reporting a violation somebody had just
    answered, and the count never fell — the reviewer's work was invisible
    everywhere except the audit trail.
    """
    pile_id, _ = make_pile(engine, tmp_path)
    finding_id, item = _finding(engine, pile_id)

    body = client.post(f"/decisions/{item}/approve", json={}, headers={"X-Actor": "devansh"}).json()

    assert body["finding"] == "acknowledged"
    assert client.get(f"/piles/{pile_id}/findings").json()["findings"] == []


def test_rejecting_a_finding_dismisses_it(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """ "I looked at this and it is fine" is a decision the system remembers —
    `findings.record` refreshes a message but never resets a status."""
    pile_id, _ = make_pile(engine, tmp_path)
    finding_id, item = _finding(engine, pile_id)

    body = client.post(f"/decisions/{item}/reject", json={}, headers={"X-Actor": "devansh"}).json()

    assert body["finding"] == "dismissed"
    with engine.begin() as conn:
        assert finding_repo.counts_for_pile(conn, pile_id) == {"dismissed": 1}


# ── serving the dashboard ─────────────────────────────────────────────────


def test_the_api_serves_the_dashboard_when_one_is_built(engine: Engine, tmp_path: Path) -> None:
    """Same origin, which is not a deployment detail here.

    The register pane highlights a source span from our own text when a value is
    clicked. That only works without a cross-origin boundary — an iframe would put
    the citation click on the far side of one. So the bundle is served by this
    app, not beside it.
    """
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>dash</title>")
    (static / "assets" / "app.js").write_text("console.log(1)")

    with TestClient(create_app(settings_for(engine, tmp_path, static_dir=static))) as built:
        assert built.get("/").text == "<!doctype html><title>dash</title>"
        assert built.get("/assets/app.js").status_code == 200
        # And the API is still the API.
        assert built.get("/piles").status_code == 200
        assert built.get("/health").json()["status"] == "ok"


def test_without_a_build_the_root_explains_itself(engine: Engine, tmp_path: Path) -> None:
    """A blank React shell would read as broken; so would a 404.

    Without a bundle the root stays the status page — what is configured, what
    is missing, and how to build the dashboard.

    `static_dir` is pointed at a directory that does not exist, rather than left
    at its default. The default is `/app/static`, which inside the image is a
    real bundle — so this passed on a laptop and failed in the container, which
    is the environment-dependence `make test` exists to catch.
    """
    settings = settings_for(engine, tmp_path, static_dir=tmp_path / "no-build-here")
    with TestClient(create_app(settings)) as bare:
        body = bare.get("/").text
    assert "Contract Pile Analyst" in body
    assert "make ui-build" in body


def test_a_mistyped_api_path_is_not_answered_with_the_app(engine: Engine, tmp_path: Path) -> None:
    """`StaticFiles` mounted at `/` would turn every wrong URL into a 200 and a
    blank screen. Only `/assets` is mounted, so an unknown route still 404s with
    the sentence that says why."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html>")

    with TestClient(create_app(settings_for(engine, tmp_path, static_dir=static))) as built:
        wrong = built.get("/piles/not-a-uuid")
        assert wrong.status_code == 404
        assert "not a valid id" in wrong.json()["detail"]


def test_the_dashboard_subscribes_to_every_event_the_backend_emits() -> None:
    """A named SSE frame reaches only a listener registered for that exact name.

    There is no wildcard in the protocol and no fallback: `onmessage` sees
    unnamed frames only. So `useEvents.KNOWN_TYPES` is the subscription itself,
    and a type the backend emits that is missing from it is dropped by a stream
    that otherwise looks perfectly healthy. It happened to `stage.commit.done`,
    `stage.commit.applied` and `stage.commit.parked` — the timeline stopped at
    the last stage a reviewer could see and never showed the commit finishing.

    Read out of both sources rather than asserted against a third list, because
    a third list is the thing that goes stale.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    hook_path = root / "frontend" / "src" / "hooks" / "useEvents.ts"
    if not hook_path.exists():
        # The runtime image carries the built bundle, not the sources it was
        # built from. This is a source-consistency check, so it runs where the
        # sources are — the host and CI, both of which have the full checkout.
        pytest.skip("frontend sources are not in the runtime image")
    backend = root / "backend" / "src" / "documents_analyst"
    emitted = {
        match.group(1)
        for path in backend.rglob("*.py")
        for match in re.finditer(r'type=(?:f?)"((?:run|stage|export)\.[a-z._]+)"', path.read_text())
    }
    assert emitted, "found no event types in the backend — this test is reading the wrong tree"

    hook = hook_path.read_text()
    listed = set(re.findall(r'"((?:run|stage|export|ping)[a-z._]*)"', hook))

    missing = sorted(emitted - listed)
    assert not missing, (
        f"the dashboard does not listen for {missing} — a named SSE frame with no "
        "listener is discarded, so the timeline would silently lose these"
    )


# ── the final document ────────────────────────────────────────────────────


def _committed(engine: Engine, tmp_path: Path, name: str = "acme") -> tuple[str, Path, list[Any]]:
    """A pile whose final document has been written and recorded — the state `Commit`
    leaves behind, built directly so these tests are about the routes, not the graph."""
    pile_id, folder = make_pile(engine, tmp_path, name)
    sections = [
        deliverable.Section("s1", "Acme ↔ Beta", "Payment Terms Days", "30 days"),
        deliverable.Section("s2", "Acme ↔ Beta", "Recurring Price", "USD 10,000"),
    ]
    path = folder / "final-document.docx"
    deliverable.write(path, title="Final Document — acme", sections=sections)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE piles SET deliverable_version = 1, deliverable_path = :path, "
                "deliverable_sha256 = :sha, deliverable_sections = CAST(:sections AS jsonb) "
                "WHERE id = :p"
            ),
            {
                "p": pile_id,
                "path": str(path),
                "sha": deliverable.document_sha256(sections),
                "sections": json.dumps([s.as_record() for s in sections]),
            },
        )
    return pile_id, path, sections


def test_the_register_says_whether_a_document_has_been_written(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    pile_id, _ = make_pile(engine, tmp_path, "fresh")
    body = client.get(f"/piles/{pile_id}/deliverable").json()
    assert body["document"]["written"] is False
    assert body["document"]["download"] is None
    assert client.get(f"/piles/{pile_id}/deliverable/document").status_code == 409


def test_the_document_downloads_from_the_pile_folder(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    pile_id, path, _ = _committed(engine, tmp_path)
    info = client.get(f"/piles/{pile_id}/deliverable").json()["document"]
    assert info["written"] is True and info["filename"] == "final-document.docx"

    response = client.get(info["download"])
    assert response.status_code == 200
    assert response.content == path.read_bytes()
    assert "wordprocessingml" in response.headers["content-type"]
    assert "final-document.docx" in response.headers["content-disposition"]


def test_a_missing_document_is_410_not_a_stale_success(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    pile_id, path, _ = _committed(engine, tmp_path)
    path.unlink()
    response = client.get(f"/piles/{pile_id}/deliverable/document")
    assert response.status_code == 410
    assert "restores it" in response.json()["detail"]


def test_verify_reads_the_file_back(client: TestClient, engine: Engine, tmp_path: Path) -> None:
    pile_id, path, sections = _committed(engine, tmp_path)
    ok = client.get(f"/piles/{pile_id}/deliverable/verify").json()
    assert ok["verified"] is True and ok["sections_checked"] == 2

    tampered = [deliverable.Section(s.slot_id, s.agreement, s.heading, "edited") for s in sections]
    deliverable.write(path, title="x", sections=tampered)
    bad = client.get(f"/piles/{pile_id}/deliverable/verify").json()
    assert bad["verified"] is False
    assert len(bad["problems"]) == 2, "each differing section is named"


def test_the_stored_path_never_decides_what_is_read(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    """The file is found by pile NAME under this process's own `watch_root`. A stored
    path is data; pointing it at another file reads nothing from there."""
    pile_id, path, _ = _committed(engine, tmp_path)
    elsewhere = tmp_path / "secret.docx"
    elsewhere.write_bytes(b"not yours")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE piles SET deliverable_path = :x WHERE id = :p"),
            {"x": str(elsewhere), "p": pile_id},
        )
    response = client.get(f"/piles/{pile_id}/deliverable/document")
    assert response.content == path.read_bytes()


def test_history_lists_section_changes_with_their_decision(
    client: TestClient, engine: Engine, tmp_path: Path
) -> None:
    pile_id, _, _ = _committed(engine, tmp_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deliverable_versions (pile_id, version, section_label, operation, "
                "before_text, after_text, reason) VALUES (:p, 2, 'Payment Terms Days', 'edit', "
                "'30 days', '45 days', 'source_update')"
            ),
            {"p": pile_id},
        )
    changes = client.get(f"/piles/{pile_id}/deliverable/history").json()["changes"]
    assert [(c["version"], c["section_label"], c["after_text"]) for c in changes] == [
        (2, "Payment Terms Days", "45 days")
    ]
