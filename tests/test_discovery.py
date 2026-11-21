"""Discovery against a real database — Phase 3.

Marked `db`. These need Postgres because the behaviours they check *are*
database behaviours: `UNIQUE (pile_id, sha256)` collapsing a duplicate insert,
`FOR UPDATE SKIP LOCKED` handing two workers different rows, an epoch-fenced
UPDATE affecting zero rows. Faking any of those would test the fake.

Several of these are the named invariants from `docs/TESTING.md`.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from documents_analyst.db import migrate
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.worker import ingest as ingest_module
from documents_analyst.worker import lease, queue
from documents_analyst.worker.ingest import ShutdownRequested, ingest_pile
from documents_analyst.worker.lease import Fence, LostLease, fenced_update
from documents_analyst.worker.scanner import Listing, scan_pile

pytestmark = pytest.mark.db

SCRATCH = "analyst_discovery_test"
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A throwaway database with every migration applied."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — needs a live Postgres")

    base, admin_url = url.rsplit("/", 1)[0], url.rsplit("/", 1)[0] + "/postgres"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
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


def aged(path: Path, body: str | bytes = "contract text") -> Path:
    """Write a file and backdate it, so it is settled the moment we scan."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)
    when = time.time() - 60
    os.utime(path, (when, when))
    return path


def ingest(engine: Engine, pile_dir: Path, name: str, *, deep: bool = True):  # noqa: ANN201
    """Ensure the pile, take a real lease, ingest under it, release.

    The lease is not optional scaffolding in this helper: every mutating write
    is fenced against `piles.lease_epoch`, so ingesting without one writes
    nothing at all.
    """
    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name=name, watch_path=pile_dir)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "test-worker", ttl_s=90)
    assert epoch is not None, "the test helper could not take the lease"
    try:
        result = ingest_pile(
            engine,
            fence=Fence(pile_id=pile_id, epoch=epoch),
            watch_path=pile_dir,
            settle_seconds=3,
            deep=deep,
        )
    finally:
        with engine.begin() as conn:
            lease.release(conn, pile_id, epoch)
    return pile_id, result


# ── a folder is a pile ────────────────────────────────────────────────────


def test_a_folder_becomes_a_pile_with_no_registration(engine: Engine, tmp_path: Path) -> None:
    pile_dir = tmp_path / "acme"
    aged(pile_dir / "01_msa.md", "Payment is due Net 30.")

    pile_id, result = ingest(engine, pile_dir, "acme")

    assert result.added == 1
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 1}


def test_a_rulebook_is_noticed_when_it_appears(engine: Engine, tmp_path: Path) -> None:
    """Looked for on every scan, so adding one to an existing pile takes effect
    without a config reload nobody would remember to trigger."""
    pile_dir = tmp_path / "rules"
    aged(pile_dir / "01_msa.md")
    pile_id, _ = ingest(engine, pile_dir, "rules")

    with engine.begin() as conn:
        assert pile_repo.get(conn, pile_id)["rulebook_path"] is None

    aged(pile_dir / "rulebook.yaml", "rules: []")
    ingest(engine, pile_dir, "rules")

    with engine.begin() as conn:
        assert pile_repo.get(conn, pile_id)["rulebook_path"] is not None


# ── the named invariants ──────────────────────────────────────────────────


def test_same_content_new_path_is_not_reprocessed(engine: Engine, tmp_path: Path) -> None:
    """A copy of a contract into a subfolder is not a second contract.

    Treating it as one would put the same term in the register twice and then
    report the two as agreeing — a conflict count inflated by filing.
    """
    pile_dir = tmp_path / "copies"
    body = "Payment is due Net 30."
    aged(pile_dir / "01_msa.md", body)
    pile_id, first = ingest(engine, pile_dir, "copies")

    aged(pile_dir / "archive" / "01_msa.md", body)
    _, second = ingest(engine, pile_dir, "copies")

    assert first.added == 1
    assert second.added == 0 and second.path_added == 1
    with engine.begin() as conn:
        assert sum(doc_repo.counts(conn, pile_id).values()) == 1, "one document, two paths"


def test_same_content_different_format_is_not_reprocessed(engine: Engine, tmp_path: Path) -> None:
    """`.md` and `.docx` of one contract are one document.

    Different bytes, identical extracted text — measured in 2A.1. Without the
    text hash the register would carry every term twice and report the pair as
    a conflict with itself.
    """
    pile_dir = tmp_path / "formats"
    pile_dir.mkdir()
    for name in ("01_msa.md", "01_msa.docx"):
        source = FIXTURES / "conflict_pile" / name
        target = pile_dir / name
        target.write_bytes(source.read_bytes())
        when = time.time() - 60
        os.utime(target, (when, when))

    pile_id, result = ingest(engine, pile_dir, "formats")

    assert result.added == 1 and result.aliased == 1
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT filename, alias_of_document_id FROM documents "
                "WHERE pile_id = :p ORDER BY created_at"
            ),
            {"p": pile_id},
        ).all()
    assert [r.alias_of_document_id is None for r in rows] == [True, False]


def test_replaced_file_supersedes_not_duplicates(engine: Engine, tmp_path: Path) -> None:
    """Editing a contract in place produces a new version, not a rival.

    Two live documents with the same filename and different terms is exactly
    the shape of a false conflict.
    """
    pile_dir = tmp_path / "versions"
    aged(pile_dir / "01_msa.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "versions")

    aged(pile_dir / "01_msa.md", "Payment is due Net 45.")
    _, second = ingest(engine, pile_dir, "versions", deep=True)

    assert second.superseded == 1
    with engine.begin() as conn:
        counts = doc_repo.counts(conn, pile_id)
    assert counts == {"ingested": 1, "superseded": 1}


def test_the_superseded_version_is_kept_not_deleted(engine: Engine, tmp_path: Path) -> None:
    """Its facts are still cited in a register someone may be reading. Deleting
    the row would make the audit trail end at a document that no longer
    exists."""
    pile_dir = tmp_path / "kept"
    aged(pile_dir / "a.md", "Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "kept")
    aged(pile_dir / "a.md", "Net 45.")
    ingest(engine, pile_dir, "kept")

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT extracted_text FROM documents WHERE pile_id = :p AND status = 'superseded'"
            ),
            {"p": pile_id},
        ).one()
    assert "Net 30." in row.extracted_text


def test_poison_file_quarantines_and_pile_continues(engine: Engine, tmp_path: Path) -> None:
    """One unreadable file must not stall everything behind it.

    Quarantined rather than skipped: skipping leaves the pile looking complete
    while a document was never considered, and "we could not read this one" is
    exactly the sentence a reviewer needs.
    """
    pile_dir = tmp_path / "poison"
    pile_dir.mkdir()
    (pile_dir / "08_corrupt.pdf").write_bytes(
        (FIXTURES / "conflict_pile" / "08_corrupt.pdf").read_bytes()
    )
    aged(pile_dir / "01_msa.md", "Payment is due Net 30.")
    when = time.time() - 60
    os.utime(pile_dir / "08_corrupt.pdf", (when, when))

    pile_id, result = ingest(engine, pile_dir, "poison")

    assert result.quarantined == 1
    assert result.added == 1, "the good document still landed"
    with engine.begin() as conn:
        counts = doc_repo.counts(conn, pile_id)
        row = conn.execute(
            text("SELECT quarantine_error FROM documents WHERE status = 'quarantined'")
        ).one()
    assert counts == {"ingested": 1, "quarantined": 1}
    assert "could not be read" in row.quarantine_error


def test_partial_write_is_not_ingested(engine: Engine, tmp_path: Path) -> None:
    """A file written just now is skipped, not failed. It is a file we will see
    again in thirty seconds."""
    pile_dir = tmp_path / "partial"
    pile_dir.mkdir()
    (pile_dir / "copying.md").write_text("half a contr")  # fresh mtime, on purpose

    pile_id, result = ingest(engine, pile_dir, "partial")

    assert result.unsettled == 1 and result.added == 0
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {}


def test_files_dropped_while_down_are_found(engine: Engine, tmp_path: Path) -> None:
    """The reconciling design, stated as a test.

    Nothing published an event while the worker was stopped. The next scan asks
    "what is here now" and finds all three, because it diffs rather than
    consuming a queue.
    """
    pile_dir = tmp_path / "offline"
    pile_dir.mkdir()
    pile_id, _ = ingest(engine, pile_dir, "offline")

    for i in range(3):
        aged(pile_dir / f"{i}.md", f"Contract number {i}.")

    _, result = ingest(engine, pile_dir, "offline")

    assert result.added == 3
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 3}


def test_a_deleted_file_is_marked_removed_not_dropped(engine: Engine, tmp_path: Path) -> None:
    pile_dir = tmp_path / "deletes"
    aged(pile_dir / "gone.md", "Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "deletes")

    (pile_dir / "gone.md").unlink()
    _, result = ingest(engine, pile_dir, "deletes")

    assert result.removed == 1
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"removed": 1}


def test_a_document_at_two_paths_survives_losing_one(engine: Engine, tmp_path: Path) -> None:
    """All its paths must vanish, not any. Otherwise deleting one copy removes
    a document that is still sitting in the folder."""
    pile_dir = tmp_path / "twopaths"
    body = "Payment is due Net 30."
    aged(pile_dir / "a.md", body)
    aged(pile_dir / "sub" / "a.md", body)
    pile_id, _ = ingest(engine, pile_dir, "twopaths")

    (pile_dir / "a.md").unlink()
    _, result = ingest(engine, pile_dir, "twopaths")

    assert result.removed == 0
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 1}


# ── the lease ─────────────────────────────────────────────────────────────


def test_concurrent_runs_serialize(engine: Engine, tmp_path: Path) -> None:
    """One pile, one worker. The second gets None and goes elsewhere."""
    pile_dir = tmp_path / "leased"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "leased")

    with engine.begin() as conn:
        first = lease.acquire(conn, pile_id, "worker-A", ttl_s=90)
    with engine.begin() as conn:
        second = lease.acquire(conn, pile_id, "worker-B", ttl_s=90)

    assert first is not None
    assert second is None, "B must not get a lease A still holds"


def test_an_expired_lease_is_taken_over_and_the_epoch_increments(
    engine: Engine, tmp_path: Path
) -> None:
    """The frozen-worker scenario. A TTL alone would let A wake and keep
    writing; the epoch is what makes its writes identifiable as stale."""
    pile_dir = tmp_path / "expiry"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "expiry")

    with engine.begin() as conn:
        a_epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    time.sleep(0.05)
    with engine.begin() as conn:
        b_epoch = lease.acquire(conn, pile_id, "worker-B", ttl_s=90)

    assert a_epoch is not None and b_epoch is not None
    assert b_epoch > a_epoch


def test_stale_epoch_write_is_rejected(engine: Engine, tmp_path: Path) -> None:
    """The fencing mechanism, and the scenario it exists for.

    A frozen worker cannot be polite — it does not know it froze. So the write
    is rejected by the ROW, not by its own good behaviour.
    """
    pile_dir = tmp_path / "fencing"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "fencing")

    with engine.begin() as conn:
        stale = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    time.sleep(0.05)
    with engine.begin() as conn:
        lease.acquire(conn, pile_id, "worker-B", ttl_s=90)

    fenced = text(
        "UPDATE piles SET status = 'running' WHERE id = :pile_id AND lease_epoch = :epoch"
    )
    with engine.begin() as conn, pytest.raises(LostLease, match="stale"):
        fenced_update(conn, fenced, {"pile_id": pile_id, "epoch": stale})


def test_a_stale_worker_cannot_extend_a_lease_it_lost(engine: Engine, tmp_path: Path) -> None:
    """Otherwise it keeps the pile away from the worker that legitimately took
    over — a frozen process holding a live pile hostage."""
    pile_dir = tmp_path / "heartbeat"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "heartbeat")

    with engine.begin() as conn:
        stale = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    time.sleep(0.05)
    with engine.begin() as conn:
        fresh = lease.acquire(conn, pile_id, "worker-B", ttl_s=90)

    with engine.begin() as conn:
        assert lease.heartbeat(conn, pile_id, stale, ttl_s=90) is False
        assert lease.heartbeat(conn, pile_id, fresh, ttl_s=90) is True


def test_an_expired_worker_cannot_resurrect_its_own_lease(engine: Engine, tmp_path: Path) -> None:
    """Expiry is final for an epoch, even before another worker takes over.

    Without the expiry predicate a paused worker that wakes first can renew its
    old epoch indefinitely. That makes the TTL a suggestion rather than the
    hand-off boundary that lets another worker take the pile.
    """
    pile_dir = tmp_path / "expired-heartbeat"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "expired-heartbeat")

    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    assert epoch is not None
    time.sleep(0.05)

    with engine.begin() as conn:
        assert lease.heartbeat(conn, pile_id, epoch, ttl_s=90) is False
        row = conn.execute(
            text("SELECT lease_expires_at < now() AS expired FROM piles WHERE id = :p"),
            {"p": pile_id},
        ).one()
    assert row.expired


def test_an_expired_worker_cannot_write_before_a_takeover(engine: Engine, tmp_path: Path) -> None:
    """Fencing expires with the lease, rather than waiting for worker B.

    The old worker must be unable to ingest even in the interval after its TTL
    elapsed but before another worker happens to acquire the pile.
    """
    pile_dir = tmp_path / "expired-write"
    aged(pile_dir / "a.md", "Payment is due Net 30.")

    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name="expired-write", watch_path=pile_dir)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    assert epoch is not None
    time.sleep(0.05)

    with pytest.raises(LostLease):
        ingest_pile(
            engine,
            fence=Fence(pile_id=pile_id, epoch=epoch),
            watch_path=pile_dir,
            settle_seconds=3,
            deep=True,
        )

    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {}


def test_shutdown_stops_before_the_next_document(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGTERM during one document releases the lease before another begins."""
    pile_dir = tmp_path / "shutdown-document"
    aged(pile_dir / "a.md", "first contract")
    aged(pile_dir / "b.md", "second contract")

    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name="shutdown-document", watch_path=pile_dir)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=90)
    assert epoch is not None

    stopped = False
    original = ingest_module._apply

    def stop_after_first(*args, **kwargs):  # noqa: ANN202
        nonlocal stopped
        original(*args, **kwargs)
        stopped = True

    monkeypatch.setattr(ingest_module, "_apply", stop_after_first)
    with pytest.raises(ShutdownRequested):
        ingest_pile(
            engine,
            fence=Fence(pile_id=pile_id, epoch=epoch),
            watch_path=pile_dir,
            settle_seconds=3,
            deep=True,
            stop_requested=lambda: stopped,
        )

    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 1}
        lease.release(conn, pile_id, epoch)


def test_an_expired_worker_cannot_release_its_own_lease(engine: Engine, tmp_path: Path) -> None:
    """An expiry is a lost lease even before another worker acquires it."""
    pile_dir = tmp_path / "expired-release"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "expired-release")

    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    assert epoch is not None
    time.sleep(0.05)

    with engine.begin() as conn:
        lease.release(conn, pile_id, epoch)
        row = conn.execute(
            text(
                "SELECT lease_owner_id, lease_expires_at < now() AS expired FROM piles WHERE id = :p"
            ),
            {"p": pile_id},
        ).one()
    assert row.lease_owner_id == "worker-A"
    assert row.expired


def test_a_late_release_does_not_free_the_new_holders_pile(engine: Engine, tmp_path: Path) -> None:
    """A worker on its way out must not hand the pile to a third worker while
    the second is mid-run."""
    pile_dir = tmp_path / "late"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "late")

    with engine.begin() as conn:
        stale = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    time.sleep(0.05)
    with engine.begin() as conn:
        lease.acquire(conn, pile_id, "worker-B", ttl_s=90)

    with engine.begin() as conn:
        lease.release(conn, pile_id, stale)
    with engine.begin() as conn:
        assert lease.acquire(conn, pile_id, "worker-C", ttl_s=90) is None


# ── the queue ─────────────────────────────────────────────────────────────


def test_a_pile_with_no_graph_ready_work_is_not_claimed(engine: Engine, tmp_path: Path) -> None:
    """Everything already extracted means nothing to do. Claiming it would spin
    a worker on a pile with no work while another pile waits."""
    pile_dir = tmp_path / "idle"
    aged(pile_dir / "a.md")
    ingest(engine, pile_dir, "idle")

    with engine.begin() as conn:
        conn.execute(text("UPDATE documents SET status = 'extracted'"))
        conn.execute(text("UPDATE piles SET lease_expires_at = NULL"))
        assert queue.claim_pile(conn, "worker-A") is None


def test_a_pile_with_ingested_work_is_claimable(engine: Engine, tmp_path: Path) -> None:
    pile_dir = tmp_path / "haswork"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "haswork")

    with engine.begin() as conn:
        conn.execute(text("UPDATE piles SET lease_expires_at = NULL"))
        assert queue.claim_pile(conn, "worker-A") is not None


def test_a_parked_pile_is_not_reclaimed(engine: Engine, tmp_path: Path) -> None:
    """It is waiting on a person or a fix. Re-claiming produces the same
    failure on a loop.

    Asserted as "not *this* pile" rather than "nothing at all": the scratch
    database holds every earlier test's piles, and some of those are legitimately
    claimable. A bare `is None` here passed for the wrong reason once already.
    """
    pile_dir = tmp_path / "parked"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "parked")

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE piles SET status = 'parked', lease_expires_at = NULL WHERE id = :p"),
            {"p": pile_id},
        )
        # It is the only pile whose lease is free AND has graph-ready work, apart
        # from any left claimable by an earlier test — so if `parked` were not
        # excluded it would be a candidate.
        for _ in range(5):
            claimed = queue.claim_pile(conn, "worker-A")
            assert claimed != pile_id, "a parked pile must never be handed out"
            if claimed is None:
                break
            conn.execute(text("UPDATE piles SET status = 'error' WHERE id = :p"), {"p": claimed})


def test_a_run_batches_every_ingested_document(engine: Engine, tmp_path: Path) -> None:
    """One run, one review queue. Per-document runs would mean four separate
    trips to the human gate for no benefit."""
    pile_dir = tmp_path / "batch"
    for i in range(4):
        aged(pile_dir / f"{i}.md", f"Contract {i}.")
    pile_id, _ = ingest(engine, pile_dir, "batch")

    with engine.begin() as conn:
        assert len(queue.ingested_documents(conn, pile_id)) == 4


def test_a_document_past_its_attempt_limit_is_not_retried(engine: Engine, tmp_path: Path) -> None:
    """What stops a poison file being retried for ever."""
    pile_dir = tmp_path / "attempts"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "attempts")

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE documents SET status = 'ingested', attempts = 3 WHERE pile_id = :p"),
            {"p": pile_id},
        )
        assert queue.ingested_documents(conn, pile_id, max_attempts=3) == []


# ── write-once extracted text ─────────────────────────────────────────────


def test_extracted_text_is_write_once(engine: Engine, tmp_path: Path) -> None:
    """Every char offset in `facts_and_claims` indexes into this exact string.
    A re-ingest that rewrote it would shift every stored citation in the pile —
    silently, with no error anywhere."""
    pile_dir = tmp_path / "writeonce"
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "writeonce")

    with engine.begin() as conn:
        doc_id = conn.execute(
            text("SELECT id FROM documents WHERE pile_id = :p"), {"p": pile_id}
        ).scalar_one()
        with engine.begin() as inner:
            epoch = lease.acquire(inner, pile_id, "test-worker", ttl_s=90)
        assert epoch is not None
        doc_repo.mark_ingested(
            conn,
            Fence(pile_id=pile_id, epoch=epoch),
            str(doc_id),
            text_sha="deadbeef",
            body="SOMETHING ELSE",
        )
        stored = conn.execute(
            text("SELECT extracted_text FROM documents WHERE id = :d"), {"d": doc_id}
        ).scalar_one()

    assert "Net 30" in stored, "the second write was refused"


def test_a_restored_file_comes_back(engine: Engine, tmp_path: Path) -> None:
    """Deleting a file and putting it back is an ordinary thing to do — a bad
    sync, an undo, a folder restored from backup.

    Left `removed`, the row stays that way for ever and its terms never
    re-enter the register, while the document sits in the folder looking
    present. Nothing errors; the pile is quietly one contract short.
    """
    pile_dir = tmp_path / "revive"
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "revive")

    (pile_dir / "a.md").unlink()
    _, gone = ingest(engine, pile_dir, "revive")
    assert gone.removed == 1

    aged(pile_dir / "a.md", "Payment is due Net 30.")
    _, back = ingest(engine, pile_dir, "revive")

    assert back.restored == 1
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 1}


def test_a_restored_file_does_not_have_its_text_rewritten(engine: Engine, tmp_path: Path) -> None:
    """It goes back to `ingested`, not `queued`, when the text is already
    stored. Re-extracting would rewrite `extracted_text`, which is write-once
    precisely because every citation indexes into it."""
    pile_dir = tmp_path / "revive2"
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "revive2")

    (pile_dir / "a.md").unlink()
    ingest(engine, pile_dir, "revive2")
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    ingest(engine, pile_dir, "revive2")

    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT status, extracted_text FROM documents WHERE pile_id = :p"),
            {"p": pile_id},
        ).one()
    assert row.status == "ingested"
    assert "Net 30" in row.extracted_text


# ── the fence, on the path that actually writes ───────────────────────────


def test_a_takeover_mid_ingest_stops_the_old_worker_writing(engine: Engine, tmp_path: Path) -> None:
    """The gap this fencing closes, exercised on the real ingestion path.

    A slow scan — large PDFs, a cold page cache — can outlive a 90-second
    lease. Worker B then takes the pile and worker A carries on hashing and
    writing document rows into it. The lease alone cannot stop that: A does not
    know it lost it. Only the row can refuse.
    """
    pile_dir = tmp_path / "takeover"
    aged(pile_dir / "a.md", "Payment is due Net 30.")

    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name="takeover", watch_path=pile_dir)
    with engine.begin() as conn:
        a_epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    assert a_epoch is not None

    # B takes over while A is still "working".
    time.sleep(0.05)
    with engine.begin() as conn:
        assert lease.acquire(conn, pile_id, "worker-B", ttl_s=90) is not None

    with pytest.raises(LostLease):
        ingest_pile(
            engine,
            fence=Fence(pile_id=pile_id, epoch=a_epoch),
            watch_path=pile_dir,
            settle_seconds=3,
            deep=True,
        )

    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {}, "A wrote nothing after losing the lease"


def test_every_mutating_write_is_fenced_not_just_the_insert(engine: Engine, tmp_path: Path) -> None:
    """One unfenced write is enough to corrupt a live run's output.

    Asserted as "the row did not change", not "it raised". Two of these —
    `restore` and `mark_ingested` — deliberately do not raise on zero rows,
    because there zero is ambiguous between "stale epoch" and "nothing to do".
    What matters uniformly is that a stale worker writes nothing.
    """
    pile_dir = tmp_path / "allfenced"
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "allfenced")

    with engine.begin() as conn:
        doc_id = str(
            conn.execute(
                text("SELECT id FROM documents WHERE pile_id = :p"), {"p": pile_id}
            ).scalar_one()
        )
        before = dict(
            conn.execute(
                text("SELECT status, paths, quarantine_error FROM documents WHERE id = :d"),
                {"d": doc_id},
            )
            .one()
            ._mapping
        )

    stale = Fence(pile_id=pile_id, epoch=-1)
    mutations = {
        "add_path": lambda c: doc_repo.add_path(c, stale, doc_id, "/x", {"size": 1}),
        "restore": lambda c: doc_repo.restore(c, stale, doc_id),
        "mark_superseded": lambda c: doc_repo.mark_superseded(c, stale, doc_id),
        "quarantine": lambda c: doc_repo.quarantine(c, stale, doc_id, "boom"),
        "mark_removed": lambda c: doc_repo.mark_removed(c, stale, set()),
        "insert": lambda c: doc_repo.insert(
            c, stale, sha256="deadbeef", filename="x.md", path="/x.md", stat={"size": 1}
        ),
    }

    for name, mutate in mutations.items():
        with engine.begin() as conn, contextlib.suppress(LostLease):
            mutate(conn)  # raising is the loud half of the same guarantee
        with engine.begin() as conn:
            after = dict(
                conn.execute(
                    text("SELECT status, paths, quarantine_error FROM documents WHERE id = :d"),
                    {"d": doc_id},
                )
                .one()
                ._mapping
            )
            rows = conn.execute(
                text("SELECT count(*) FROM documents WHERE pile_id = :p"), {"p": pile_id}
            ).scalar_one()
        assert after == before, f"{name} changed the row with a stale epoch"
        assert rows == 1, f"{name} added a row with a stale epoch"


def test_a_stale_worker_cannot_rewrite_extracted_text(engine: Engine, tmp_path: Path) -> None:
    """`mark_ingested` deliberately does not raise — a re-ingest hitting the
    write-once guard is ordinary. But the fence is still in the predicate, so a
    stale worker changes nothing."""
    pile_dir = tmp_path / "fencedtext"
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "fencedtext")

    with engine.begin() as conn:
        doc_id = str(
            conn.execute(
                text("SELECT id FROM documents WHERE pile_id = :p"), {"p": pile_id}
            ).scalar_one()
        )
        conn.execute(
            text("UPDATE documents SET extracted_text = NULL WHERE id = :d"), {"d": doc_id}
        )
        doc_repo.mark_ingested(
            conn, Fence(pile_id=pile_id, epoch=-1), doc_id, text_sha="x", body="INJECTED"
        )
        stored = conn.execute(
            text("SELECT extracted_text FROM documents WHERE id = :d"), {"d": doc_id}
        ).scalar_one()

    assert stored is None, "the stale write did not land"


def test_a_stale_worker_cannot_mark_a_successors_pile_error(engine: Engine, tmp_path: Path) -> None:
    """The generic worker error path must be fenced as well as document writes."""
    pile_dir = tmp_path / "fenced-status"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "fenced-status")

    stale = Fence(pile_id=pile_id, epoch=-1)
    with engine.begin() as conn:
        assert pile_repo.set_status(conn, stale, "error") is False
        status = conn.execute(
            text("SELECT status FROM piles WHERE id = :p"), {"p": pile_id}
        ).scalar_one()
    assert status != "error"


def test_an_expired_worker_cannot_mark_its_pile_error(engine: Engine, tmp_path: Path) -> None:
    """Status writes require an unexpired lease as well as the matching epoch."""
    pile_dir = tmp_path / "expired-status"
    aged(pile_dir / "a.md")
    pile_id, _ = ingest(engine, pile_dir, "expired-status")

    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=0)
    assert epoch is not None
    time.sleep(0.05)

    with engine.begin() as conn:
        assert pile_repo.set_status(conn, Fence(pile_id=pile_id, epoch=epoch), "error") is False
        status = conn.execute(
            text("SELECT status FROM piles WHERE id = :p"), {"p": pile_id}
        ).scalar_one()
    assert status != "error"


def test_the_lease_is_renewed_during_a_long_ingest(engine: Engine, tmp_path: Path) -> None:
    """A pile that takes longer than its TTL must not lose its own lease.

    The TTL here (2s) is shorter than the pile takes to walk, so without a
    renewal in front of each document the third or fourth write would be
    refused by its own fence.
    """
    pile_dir = tmp_path / "renewed"
    for i in range(4):
        aged(pile_dir / f"{i}.md", f"Contract {i}.")

    with engine.begin() as conn:
        pile_id = pile_repo.ensure(conn, name="renewed", watch_path=pile_dir)
    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "worker-A", ttl_s=2)
    assert epoch is not None

    result = ingest_pile(
        engine,
        fence=Fence(pile_id=pile_id, epoch=epoch),
        watch_path=pile_dir,
        settle_seconds=3,
        deep=True,
        lease_ttl_s=2,
    )

    assert result.added == 4
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT lease_expires_at > now() AS live FROM piles WHERE id = :p"),
            {"p": pile_id},
        ).one()
    assert row.live, "the lease was extended rather than allowed to lapse"


# ── "could not look" must never mean "gone" ───────────────────────────────


def test_an_unreadable_pile_folder_removes_nothing(engine: Engine, tmp_path: Path) -> None:
    """The failure this guards is silent, total, and one `chmod` wide.

    A pile whose folder cannot be listed scans as an empty folder, and
    reconciliation reads an empty folder as "everything was deleted". One
    permission change, one unmounted share, or one folder renamed for two
    seconds would mark every contract in the pile `removed` — the register
    empties, and nothing errors anywhere.
    """
    pile_dir = tmp_path / "locked"
    for i in range(3):
        aged(pile_dir / f"{i}.md", f"Contract {i}.")
    pile_id, first = ingest(engine, pile_dir, "locked")
    assert first.added == 3

    os.chmod(pile_dir, 0o000)
    try:
        _, second = ingest(engine, pile_dir, "locked")
    finally:
        os.chmod(pile_dir, 0o755)

    assert second.removed == 0, "an unreadable folder is not an emptied folder"
    assert second.listing_incomplete
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 3}


def test_a_genuinely_emptied_pile_still_marks_its_documents_removed(
    engine: Engine, tmp_path: Path
) -> None:
    """The other half, and the reason the fix has to be a distinction rather
    than simply never removing anything."""
    pile_dir = tmp_path / "emptied"
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "emptied")

    (pile_dir / "a.md").unlink()
    _, second = ingest(engine, pile_dir, "emptied")

    assert second.removed == 1
    assert not second.listing_incomplete
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"removed": 1}


# ── a version replaces; a namesake does not ───────────────────────────────


def test_same_name_in_two_folders_is_two_contracts_not_a_version(
    engine: Engine, tmp_path: Path
) -> None:
    """`2023/msa.md` and `2024/msa.md` are two live agreements.

    Matching a previous version on the filename alone supersedes one with the
    other, and a superseded document leaves the register with nothing to show
    for it. Per-year and per-vendor subfolders holding same-named files are the
    normal shape of a contract pile, not an edge case.
    """
    pile_dir = tmp_path / "byyear"
    aged(pile_dir / "2023" / "msa.md", "Payment is due Net 30.")
    aged(pile_dir / "2024" / "msa.md", "Payment is due Net 45.")

    pile_id, result = ingest(engine, pile_dir, "byyear")

    assert result.added == 2 and result.superseded == 0
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 2}


def test_a_file_edited_in_place_still_supersedes(engine: Engine, tmp_path: Path) -> None:
    """The case the filename match exists for, kept working."""
    pile_dir = tmp_path / "inplace"
    aged(pile_dir / "msa.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "inplace")

    aged(pile_dir / "msa.md", "Payment is due Net 45.")
    _, second = ingest(engine, pile_dir, "inplace")

    assert second.superseded == 1
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 1, "superseded": 1}


def test_a_file_moved_and_edited_still_supersedes(engine: Engine, tmp_path: Path) -> None:
    """The other case it exists for: the old path is gone, so this really is
    the same contract in a new place rather than a second one."""
    pile_dir = tmp_path / "moved"
    aged(pile_dir / "msa.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "moved")

    (pile_dir / "msa.md").unlink()
    aged(pile_dir / "archive" / "msa.md", "Payment is due Net 45.")
    _, second = ingest(engine, pile_dir, "moved")

    assert second.superseded == 1, "the old path is gone; this replaces it"
    with engine.begin() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 1, "superseded": 1}


def test_an_incomplete_listing_never_guesses_a_missing_namesake_was_moved(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hidden `locked/msa.md` must not be superseded by `other/msa.md`.

    Incomplete listings only know what was visible, not what was absent. The
    existing contract therefore stays live and the visible same-named contract
    is added independently. On a complete later pass the normal version rules
    can make an informed decision again.
    """
    pile_dir = tmp_path / "incomplete-version"
    aged(pile_dir / "locked" / "msa.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "incomplete-version")
    visible = aged(pile_dir / "other" / "msa.md", "Payment is due Net 45.")
    actual = scan_pile(pile_dir)
    entry = next(item for item in actual.entries if item.path == visible)

    monkeypatch.setattr(
        ingest_module,
        "scan_pile",
        lambda _, **_kw: Listing(
            entries=[entry], complete=False, unreadable=("locked: PermissionError",)
        ),
    )
    _, result = ingest(engine, pile_dir, "incomplete-version")

    assert result.added == 1 and result.superseded == 0
    with engine.connect() as conn:
        assert doc_repo.counts(conn, pile_id) == {"ingested": 2}


def test_a_file_changed_between_hash_and_parse_is_retried_not_mixed(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never persist SHA(A) with extracted text(B) after an atomic-style edit."""
    pile_dir = tmp_path / "changed-during-read"
    target = aged(pile_dir / "msa.md", "Payment is due Net 30.")
    real_extract = ingest_module.extract

    def replace_after_parse(path: Path):  # noqa: ANN202
        document = real_extract(path)
        path.write_text("Payment is due Net 450.")
        return document

    monkeypatch.setattr(ingest_module, "extract", replace_after_parse)
    pile_id, first = ingest(engine, pile_dir, "changed-during-read")

    assert first.added == 0
    assert any("changed_during_read" in note for note in first.notes)
    with engine.connect() as conn:
        assert doc_repo.counts(conn, pile_id) == {}

    monkeypatch.setattr(ingest_module, "extract", real_extract)
    aged(target, target.read_text())
    _, second = ingest(engine, pile_dir, "changed-during-read")
    assert second.added == 1


def test_an_os_error_during_parse_is_benign_scanner_churn(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second file open can fail after hashing; it must not fail the pile."""
    pile_dir = tmp_path / "vanished-during-parse"
    aged(pile_dir / "msa.md", "Payment is due Net 30.")

    def vanished(_: Path):  # noqa: ANN202
        raise FileNotFoundError("gone after hash")

    monkeypatch.setattr(ingest_module, "extract", vanished)
    pile_id, result = ingest(engine, pile_dir, "vanished-during-parse")

    assert result.added == 0
    assert any("FileNotFoundError" in note for note in result.notes)
    with engine.connect() as conn:
        assert doc_repo.counts(conn, pile_id) == {}


# ── an alias is not a second document ─────────────────────────────────────


def test_an_alias_is_never_handed_to_the_graph(engine: Engine, tmp_path: Path) -> None:
    """`text_sha256` recognising `.md` and `.docx` as one contract is worth
    nothing if the queue then hands both to the graph.

    It would extract the same contract twice and report the two copies as
    agreeing: every term duplicated in the register, and a conflict count
    inflated by filing — the exact outcome aliasing exists to prevent.
    """
    pile_dir = tmp_path / "aliasqueue"
    pile_dir.mkdir()
    for name in ("01_msa.md", "01_msa.docx"):
        target = pile_dir / name
        target.write_bytes((FIXTURES / "conflict_pile" / name).read_bytes())
        when = time.time() - 60
        os.utime(target, (when, when))

    pile_id, result = ingest(engine, pile_dir, "aliasqueue")
    assert result.added == 1 and result.aliased == 1

    with engine.begin() as conn:
        ingested = conn.execute(
            text("SELECT count(*) FROM documents WHERE pile_id = :p AND status = 'ingested'"),
            {"p": pile_id},
        ).scalar_one()
        claimable = queue.ingested_documents(conn, pile_id)
    assert ingested == 2, "the alias is a real row — the register names the file it came from"
    assert len(claimable) == 1, "but only the original is work"


# ── transactions wrap writes, not slow work (WORKER_RUNTIME §7a rule 2) ───


def test_hashing_and_parsing_happen_outside_the_transaction(
    engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Holding a transaction open across a parse keeps row locks for its whole
    duration — blocking the very takeover the lease exists to allow.

    A 200MB PDF is the case that makes this real, and it is not reproducible in
    a test. So the invariant is asserted directly instead: while the file is
    being read, this worker has no transaction open.
    """
    depths: list[int] = []
    open_transactions = 0
    real_begin = engine.begin

    def counting_begin():  # noqa: ANN202
        @contextlib.contextmanager
        def wrapper():  # noqa: ANN202
            nonlocal open_transactions
            with real_begin() as conn:
                open_transactions += 1
                try:
                    yield conn
                finally:
                    open_transactions -= 1

        return wrapper()

    real_read = ingest_module.read_document

    def watching_read(entry):  # noqa: ANN001, ANN202
        depths.append(open_transactions)
        return real_read(entry)

    monkeypatch.setattr(engine, "begin", counting_begin)
    monkeypatch.setattr(ingest_module, "read_document", watching_read)

    pile_dir = tmp_path / "txnscope"
    for i in range(3):
        aged(pile_dir / f"{i}.md", f"Contract {i}.")
    _, result = ingest(engine, pile_dir, "txnscope")

    assert result.added == 3
    assert depths == [0, 0, 0], f"a document was read inside a transaction: {depths}"


def test_deleting_the_original_promotes_its_alias(engine: Engine, tmp_path: Path) -> None:
    """Tidying up a duplicate must not delete the contract.

    `01_msa.md` and `01_msa.docx` are one document; the `.md` is stored as an
    alias because its text was already read. Delete the `.docx` and the alias
    is the only copy left — but an alias is deliberately excluded from the work
    queue, so it would sit there `ingested`, pointing at a `removed` row, and
    never be extracted by anything. The agreement disappears from the register
    while its file is still in the folder, and nothing errors.
    """
    pile_dir = tmp_path / "orphaned"
    pile_dir.mkdir()
    for name in ("01_msa.md", "01_msa.docx"):
        target = pile_dir / name
        target.write_bytes((FIXTURES / "conflict_pile" / name).read_bytes())
        when = time.time() - 60
        os.utime(target, (when, when))

    pile_id, first = ingest(engine, pile_dir, "orphaned")
    assert first.added == 1 and first.aliased == 1

    with engine.begin() as conn:
        original = conn.execute(
            text(
                "SELECT filename FROM documents WHERE pile_id = :p AND alias_of_document_id IS NULL"
            ),
            {"p": pile_id},
        ).scalar_one()
    (pile_dir / str(original)).unlink()

    _, second = ingest(engine, pile_dir, "orphaned")

    assert second.removed == 1
    with engine.begin() as conn:
        live = conn.execute(
            text(
                "SELECT filename, alias_of_document_id FROM documents "
                "WHERE pile_id = :p AND status = 'ingested'"
            ),
            {"p": pile_id},
        ).all()
        claimable = queue.ingested_documents(conn, pile_id)

    assert len(live) == 1 and live[0].alias_of_document_id is None, (
        "the survivor is now an original"
    )
    assert len(claimable) == 1, "and the graph can finally see it"


def test_only_one_alias_is_promoted_when_several_survive(engine: Engine, tmp_path: Path) -> None:
    """Promoting every orphan would recreate the duplicate the alias prevented.

    Driven at the repository rather than through a folder, because producing
    two aliases of one original from real files depends on which byte
    differences survive text extraction — and a test that quietly skips when
    the fixture does not cooperate is a test that proves nothing.
    """
    pile_dir = tmp_path / "heirs"
    aged(pile_dir / "a.md", "Payment is due Net 30.")
    pile_id, _ = ingest(engine, pile_dir, "heirs")

    with engine.begin() as conn:
        epoch = lease.acquire(conn, pile_id, "promoter", ttl_s=90)
    assert epoch is not None
    fence = Fence(pile_id=pile_id, epoch=epoch)

    with engine.begin() as conn:
        original = str(
            conn.execute(
                text("SELECT id FROM documents WHERE pile_id = :p"), {"p": pile_id}
            ).scalar_one()
        )
        for suffix in ("b", "c"):
            conn.execute(
                text(
                    "INSERT INTO documents (pile_id, sha256, filename, text_sha256, "
                    "extracted_text, alias_of_document_id, status) "
                    "VALUES (:p, :sha, :name, 'same-text', 'Payment is due Net 30.', "
                    ":parent, 'ingested')"
                ),
                {"p": pile_id, "sha": f"sha-{suffix}", "name": f"{suffix}.md", "parent": original},
            )
        conn.execute(
            text("UPDATE documents SET status = 'removed' WHERE id = :id"), {"id": original}
        )

    with engine.begin() as conn:
        promoted = doc_repo.promote_orphaned_aliases(conn, fence)
        originals = conn.execute(
            text(
                "SELECT count(*) FROM documents WHERE pile_id = :p "
                "AND alias_of_document_id IS NULL AND status <> 'removed'"
            ),
            {"p": pile_id},
        ).scalar_one()
        still_aliased = conn.execute(
            text(
                "SELECT count(*) FROM documents WHERE pile_id = :p "
                "AND alias_of_document_id IS NOT NULL AND status <> 'removed'"
            ),
            {"p": pile_id},
        ).scalar_one()
    with engine.begin() as conn:
        lease.release(conn, pile_id, epoch)

    assert promoted == 1, "one heir per orphaned original"
    assert originals == 1, "two originals with identical text is the duplicate we prevented"
    assert still_aliased == 1, "the other one re-points at the heir rather than being dropped"
