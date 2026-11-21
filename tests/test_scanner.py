"""The scanner — Phase 3. Pure, no database.

Everything here is a function of the filesystem and an injected clock, which is
what lets the awkward cases be tested at all: a half-copied file, a Word lock
file, a folder that vanishes mid-walk. None of those are reachable through a
database fixture.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from documents_analyst.worker.scanner import (
    ScanEntry,
    ignored_name,
    is_settled,
    list_pile_dirs,
    scan_pile,
    sha256_file,
    supported,
    text_sha256,
    unchanged,
)


def test_shutdown_does_not_acquire_another_pile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stop request during pile A leaves pile B untouched and unleased."""
    from documents_analyst.worker import main as worker_main
    from documents_analyst.worker.ingest import ReconcileResult

    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    stopped = threading.Event()
    acquired: list[str] = []

    class DummyEngine:
        def begin(self):  # noqa: ANN201
            return nullcontext(object())

    settings = SimpleNamespace(
        watch_root=tmp_path,
        worker_id="test-worker",
        lease_ttl_s=90,
        settle_seconds=3,
        deliverable_filename="final-document.docx",
    )
    monkeypatch.setattr(worker_main, "list_pile_dirs", lambda _root: [first, second])
    monkeypatch.setattr(worker_main.pile_repo, "ensure", lambda _conn, *, name, watch_path: name)
    monkeypatch.setattr(
        worker_main.lease,
        "acquire",
        lambda _conn, pile_id, _worker, _ttl: acquired.append(pile_id) or 1,
    )
    monkeypatch.setattr(worker_main.lease, "release", lambda *_args: None)

    def stop_after_first(*_args, **_kwargs):  # noqa: ANN202
        stopped.set()
        return ReconcileResult()

    monkeypatch.setattr(worker_main, "ingest_pile", stop_after_first)

    assert worker_main.discover(DummyEngine(), settings, deep=True, stop_event=stopped) == 1
    assert acquired == ["first"]


def test_shutdown_releases_the_current_lease_without_marking_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`discover()` owns the release, so cancellation is not an error path."""
    from documents_analyst.worker import main as worker_main
    from documents_analyst.worker.ingest import ShutdownRequested

    pile = tmp_path / "only"
    pile.mkdir()
    stopped = threading.Event()
    released: list[tuple[str, int]] = []
    statuses: list[str] = []

    class DummyEngine:
        def begin(self):  # noqa: ANN201
            return nullcontext(object())

    settings = SimpleNamespace(
        watch_root=tmp_path,
        worker_id="test-worker",
        lease_ttl_s=90,
        settle_seconds=3,
        deliverable_filename="final-document.docx",
    )
    monkeypatch.setattr(worker_main, "list_pile_dirs", lambda _root: [pile])
    monkeypatch.setattr(worker_main.pile_repo, "ensure", lambda _conn, *, name, watch_path: name)
    monkeypatch.setattr(worker_main.lease, "acquire", lambda *_args: 7)
    monkeypatch.setattr(
        worker_main.lease,
        "release",
        lambda _conn, pile_id, epoch: released.append((pile_id, epoch)),
    )
    monkeypatch.setattr(
        worker_main.pile_repo,
        "set_status",
        lambda _conn, _fence, status: statuses.append(status),
    )

    def cancel(*_args, **_kwargs):  # noqa: ANN202
        stopped.set()
        raise ShutdownRequested("test cancellation")

    monkeypatch.setattr(worker_main, "ingest_pile", cancel)

    assert worker_main.discover(DummyEngine(), settings, deep=True, stop_event=stopped) == 0
    assert released == [("only", 7)]
    assert statuses == []


def write(path: Path, body: str = "contract text", *, age_s: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    if age_s:
        when = time.time() - age_s
        os.utime(path, (when, when))
    return path


def entry_for(path: Path) -> ScanEntry:
    stat = path.stat()
    return ScanEntry(path=path, size=stat.st_size, mtime_ns=stat.st_mtime_ns, inode=stat.st_ino)


# ── what is a pile ────────────────────────────────────────────────────────


def test_every_subdirectory_is_a_pile(tmp_path: Path) -> None:
    """No creation step, no config file, no API call."""
    (tmp_path / "acme").mkdir()
    (tmp_path / "cobalt").mkdir()
    write(tmp_path / "loose.md")

    assert [p.name for p in list_pile_dirs(tmp_path)] == ["acme", "cobalt"]


def test_a_missing_watch_root_is_empty_not_an_error(tmp_path: Path) -> None:
    """A first run before anyone has made the folder must not crash the worker."""
    assert list_pile_dirs(tmp_path / "nope") == []


# ── what gets ignored, and why ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "why"),
    [
        ("~$msa.docx", "Word's lock file — present whenever a document is OPEN"),
        (".DS_Store", "a dotfile"),
        ("._msa.docx", "an AppleDouble resource fork"),
        ("msa.docx.part", "an in-progress download"),
        ("msa.docx.tmp", "an editor scratch file"),
        (".msa.docx.swp", "vim's swap file"),
    ],
)
def test_transient_files_are_ignored_by_name(name: str, why: str) -> None:
    """By name, before opening. A file we should not read must be excluded
    without depending on anything inside it."""
    assert ignored_name(name), why


def test_a_real_contract_is_not_ignored() -> None:
    for name in ("01_msa.docx", "02_sow.pdf", "notes.md", "terms.txt"):
        assert not ignored_name(name)
        assert supported(name)


def test_unsupported_formats_are_left_alone() -> None:
    """A pile is a folder someone uses. Spreadsheets and images live there too,
    and refusing them quietly is right — the README says we do not read them."""
    for name in ("budget.xlsx", "logo.png", "rulebook.yaml"):
        assert not supported(name)


def test_the_word_lock_file_is_excluded_from_a_real_walk(tmp_path: Path) -> None:
    """The single most likely stray file in a folder someone is working in.
    Ingesting one produces a "document" of ownership metadata."""
    write(tmp_path / "01_msa.docx")
    write(tmp_path / "~$01_msa.docx")

    assert [e.path.name for e in scan_pile(tmp_path)] == ["01_msa.docx"]


def test_the_walk_is_recursive_and_ordered(tmp_path: Path) -> None:
    write(tmp_path / "b.md")
    write(tmp_path / "sub" / "a.md")

    found = [e.path.name for e in scan_pile(tmp_path)]

    assert found == ["b.md", "a.md"] or found == ["a.md", "b.md"]
    assert len(found) == 2


def test_a_folder_that_vanishes_mid_walk_does_not_abort_the_scan(tmp_path: Path) -> None:
    """Raising here would abandon every other pile for one deleted subfolder."""
    write(tmp_path / "keep.md")
    ghost = tmp_path / "ghost"
    ghost.mkdir()

    entries = scan_pile(tmp_path)
    ghost.rmdir()

    assert [e.path.name for e in scan_pile(tmp_path)] == ["keep.md"]
    assert entries


# ── "empty" and "could not look" are different facts ──────────────────────


def test_an_empty_folder_is_a_complete_listing(tmp_path: Path) -> None:
    """Nothing here, and we are sure. This is what licenses reconciliation to
    conclude that anything it knew about has been deleted."""
    listing = scan_pile(tmp_path)

    assert listing.entries == []
    assert listing.complete


def test_a_folder_we_cannot_read_is_not_an_empty_folder(tmp_path: Path) -> None:
    """The distinction the whole removal path hangs on.

    `chmod 000` on a pile — or an unmounted share, or a folder renamed for two
    seconds — produces exactly the same empty list as a folder someone really
    did empty. Treating them alike marks every document in the pile `removed`
    on a transient permission error.
    """
    write(tmp_path / "01_msa.md")
    os.chmod(tmp_path, 0o000)
    try:
        listing = scan_pile(tmp_path)
    finally:
        os.chmod(tmp_path, 0o755)

    assert listing.entries == []
    assert not listing.complete, "an unreadable folder must not look like an empty one"
    assert listing.unreadable, "and it must say which path it could not read"


def test_one_unreadable_subfolder_taints_the_whole_listing(tmp_path: Path) -> None:
    """Because removal is decided per pile, not per folder. A document inside
    the locked subfolder is still there; we simply cannot see it, and the pile
    must not conclude it is gone."""
    write(tmp_path / "keep.md")
    locked = tmp_path / "locked"
    write(locked / "hidden.md")
    os.chmod(locked, 0o000)
    try:
        listing = scan_pile(tmp_path)
    finally:
        os.chmod(locked, 0o755)

    assert [e.path.name for e in listing] == ["keep.md"], "the rest of the walk still works"
    assert not listing.complete


def test_a_file_deleted_mid_walk_still_leaves_a_complete_listing(tmp_path: Path) -> None:
    """The opposite case, and it must not be tarred with the same brush: a file
    that is genuinely gone has to stay reportable as gone, or deleting a file
    would suppress the reconciliation that notices it."""
    keep = write(tmp_path / "keep.md")
    doomed = write(tmp_path / "doomed.md")

    real_scandir = os.scandir

    def vanishing(path: object) -> object:
        entries = list(real_scandir(path))
        doomed.unlink(missing_ok=True)
        return entries

    import documents_analyst.worker.scanner as scanner_mod

    original = scanner_mod.os.scandir
    scanner_mod.os.scandir = vanishing  # type: ignore[assignment]
    try:
        listing = scan_pile(tmp_path)
    finally:
        scanner_mod.os.scandir = original  # type: ignore[assignment]

    assert [e.path.name for e in listing] == [keep.name]
    assert listing.complete, "a real deletion is not a failure to look"


# ── the settle check ──────────────────────────────────────────────────────


def test_a_file_still_being_written_is_not_settled(tmp_path: Path) -> None:
    """Copying a 200MB PDF is not atomic. Hashing it mid-write yields a hash for
    a document that never existed, which then never matches anything again."""
    fresh = entry_for(write(tmp_path / "big.pdf"))

    assert not is_settled(fresh, settle_seconds=3)


def test_a_file_untouched_for_the_window_is_settled(tmp_path: Path) -> None:
    old = entry_for(write(tmp_path / "done.pdf", age_s=10))

    assert is_settled(old, settle_seconds=3)


def test_settling_is_judged_from_the_clock_not_from_a_previous_scan(tmp_path: Path) -> None:
    """Age, not two probes.

    A worker that crashes mid-copy and restarts has forgotten any previous
    probe, and would hash a half-written file on its first scan — precisely when
    a crash is most likely to have just happened. The clock does not forget, so
    a fresh process reaches the same verdict as the one that died.
    """
    fresh = entry_for(write(tmp_path / "copying.pdf"))

    assert not is_settled(fresh, settle_seconds=3), "a brand-new process still says no"
    assert is_settled(fresh, settle_seconds=3, now=lambda: time.time() + 60)


# ── the cheap-path check ──────────────────────────────────────────────────


def test_an_untouched_file_is_recognised_without_reading_it(tmp_path: Path) -> None:
    """The reason a scan of a settled pile costs one query and some integer
    comparisons rather than re-hashing every document."""
    path = write(tmp_path / "msa.md", age_s=10)
    entry = entry_for(path)
    known = {str(path): (entry.size, entry.mtime_ns, entry.inode)}

    assert unchanged(entry, known)


def test_an_atomic_replace_is_detected_by_the_inode(tmp_path: Path) -> None:
    """An editor writing a temp file and renaming it over the original is the
    common case. Size can coincide and mtime granularity is coarse enough that
    "same second" happens; the inode never coincides."""
    path = write(tmp_path / "msa.md", "aaaa", age_s=10)
    before = entry_for(path)
    known = {str(path): (before.size, before.mtime_ns, before.inode)}

    replacement = write(tmp_path / "msa.md.new", "bbbb", age_s=10)
    os.utime(replacement, (path.stat().st_atime, path.stat().st_mtime))
    replacement.replace(path)

    after = entry_for(path)
    assert after.size == before.size, "same length, on purpose"
    assert not unchanged(after, known), "the inode is what catches it"


def test_an_unknown_path_is_never_unchanged(tmp_path: Path) -> None:
    assert not unchanged(entry_for(write(tmp_path / "new.md")), {})


# ── hashing ───────────────────────────────────────────────────────────────


def test_the_byte_hash_is_streamed_and_stable(tmp_path: Path) -> None:
    big = write(tmp_path / "big.md", "x" * (3 << 20))

    assert sha256_file(big) == sha256_file(big)
    assert len(sha256_file(big)) == 64


def test_the_two_hashes_answer_different_questions(tmp_path: Path) -> None:
    """`sha256` detects change; `text_sha256` establishes identity. A file
    whose bytes differ but whose text matches is the same contract — that is
    what makes `.md` and `.docx` one document rather than two that appear to
    disagree about everything."""
    left = write(tmp_path / "a.md", "Net 30.\n")
    right = write(tmp_path / "b.md", "Net 30.\n\n")

    assert sha256_file(left) != sha256_file(right)
    assert text_sha256("Net 30.") == text_sha256("Net 30.")


# ── the fence is applied by construction, not by memory ───────────────────


def test_every_mutating_document_write_carries_the_fence() -> None:
    """Walks the repo source and fails on any write without `HOLDS_LEASE`.

    Not paranoia. Fencing was added to six of the seven writes by hand and the
    seventh — `mark_removed` — silently missed, because a mechanical edit did
    not match after the formatter had rewrapped a string. It shipped, and only
    a behavioural test caught it. This makes the next omission fail on sight
    rather than depend on someone thinking to test that particular row.
    """
    import re

    source = (
        Path(__file__).resolve().parent.parent
        / "backend/src/documents_analyst/db/repo/documents.py"
    ).read_text()

    unfenced = []
    for match in re.finditer(r"\ndef (\w+)\(.*?(?=\ndef |\Z)", source, re.S):
        body, name = match.group(0), match.group(1)
        mutates = "UPDATE documents" in body or "INSERT INTO documents" in body
        if mutates and "HOLDS_LEASE" not in body:
            unfenced.append(name)

    assert not unfenced, f"mutating writes with no lease fence: {unfenced}"


def test_no_mutating_write_takes_a_bare_pile_id() -> None:
    """A signature that accepts the pile and omits the authority is one a
    caller can use without holding a lease. The type is the reminder."""
    import inspect

    from documents_analyst.db.repo import documents as doc_repo

    for name in (
        "add_path",
        "insert",
        "restore",
        "mark_superseded",
        "mark_removed",
        "quarantine",
        "mark_ingested",
    ):
        params = inspect.signature(getattr(doc_repo, name)).parameters
        assert "fence" in params, f"{name} does not take a Fence"
        assert "pile_id" not in params, f"{name} still takes a bare pile_id"


# ── starting without a key ────────────────────────────────────────────────


def test_no_embedding_key_degrades_to_discovery_rather_than_crashing() -> None:
    """The README promises a checkout with an untouched `.env` starts and names
    what is missing. Embeddings used to be a baked local model, so this could
    only fail on a broken image; it is a missing key now, which is the ordinary
    state of a first run — and `docker compose up` answered it with a traceback
    and a restart loop, which reads as "this project is broken".

    Discovery needs no models at all. So the worker becomes exactly what
    `--scan-only` already is: it finds piles and ingests documents, and claims
    none of them for the graph. The refusal to run the graph is the point —
    embeddings have no honest fallback — and it is not the same thing as
    refusing to start.
    """
    from documents_analyst.settings import Settings
    from documents_analyst.worker import main as worker_main

    settings = Settings(_env_file=None, openai_api_key="", gemini_api_key="g-test")

    with worker_main.graph_environment(settings) as env:
        assert env is None, "no embedding key must not yield a graph environment"

    # The same for the adjudicator, which crashed the worker the same way and
    # for longer — `build_client` has always refused an empty key. Checked with
    # the local models switched off, because the point of the ordering in
    # `graph_environment` is that neither of them is loaded to reach this.
    no_llm = Settings(_env_file=None, openai_api_key="k", nli_enabled=False, gliner_enabled=False)
    with worker_main.graph_environment(no_llm) as env:
        assert env is None, "no LLM key must not yield a graph environment either"


def test_a_usable_key_still_opens_the_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half of the branch, so a blanket `except` cannot pass this
    file by turning every startup into scan-only."""
    from documents_analyst.settings import Settings
    from documents_analyst.worker import main as worker_main

    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        gemini_api_key="g-test",
        nli_enabled=False,
        gliner_enabled=False,
    )
    monkeypatch.setattr(worker_main, "checkpointer", lambda _url: nullcontext(object()))

    with worker_main.graph_environment(settings) as env:
        assert env is not None
        assert env.models.embeddings.is_available is True
