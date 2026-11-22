"""Scan → database. The reconciliation pass.

`scanner.py` knows only the filesystem, the repos are pure SQL; this is the seam
that decides what a difference means. The order of the checks is the design:

    1. bytes seen before?     → add a path. Not a new document.
    2. can we read it?        → no: quarantine, carry on with the pile.
    3. text seen before?      → alias it to the original.
    4. filename seen before?  → supersede the older version.
    5. otherwise              → a new document.

Swapping 1 and 3 files a copied file as an alias of itself; 4 before 3 calls a
`.docx` export of an existing `.md` a new version, and the register shows a
contract superseding itself.

Reading and writing are separate steps: `read_document` hashes and parses with no
DB connection in scope, `_apply` decides and writes with no filesystem access,
and only `_apply` runs in a transaction — so hashing a 200MB PDF holds no row
lock.

See docs/WORKER_RUNTIME.md § "What survives when a worker freezes mid-run".
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.engine import Connection, Engine

from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.settings import DEFAULT_DELIVERABLE_FILENAME
from documents_analyst.worker import lease as lease_mod
from documents_analyst.worker.lease import Fence
from documents_analyst.worker.scanner import (
    ScanEntry,
    is_settled,
    scan_pile,
    sha256_file,
    text_sha256,
    unchanged,
)
from documents_analyst_core.portable.document_text import ParseError, extract

LOG = logging.getLogger(__name__)


class ShutdownRequested(Exception):
    """The worker received SIGTERM/SIGINT and should release its pile lease.
    Distinct from `LostLease`: shutdown is ordinary and must not mark the pile
    failed. The caller's `finally` releases the live lease."""


def _check_shutdown(stop_requested: Callable[[], bool] | None) -> None:
    if stop_requested is not None and stop_requested():
        raise ShutdownRequested(
            "shutdown requested; stopping after the current filesystem operation"
        )


@dataclass
class ReconcileResult:
    """What a pass did, in the terms a reviewer would ask about."""

    added: int = 0
    aliased: int = 0
    superseded: int = 0
    path_added: int = 0
    path_removed: int = 0
    quarantined: int = 0
    removed: int = 0
    restored: int = 0
    unsettled: int = 0
    unchanged: int = 0
    listing_incomplete: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(
            self.added
            or self.aliased
            or self.superseded
            or self.removed
            or self.restored
            or self.quarantined
            or self.path_added
            or self.path_removed
        )


@dataclass(frozen=True, slots=True)
class DocumentRead:
    """Everything reading one file tells us, and nothing the database knows.
    Produced outside any transaction, which keeps the slow work (hashing, parsing)
    off a row lock and makes `_apply`'s decision logic testable without a disk."""

    sha256: str | None = None
    text: str | None = None
    text_sha256: str | None = None
    parse_error: str | None = None
    io_error: str | None = None


def read_document(entry: ScanEntry) -> DocumentRead:
    """Hash it, then try to parse it. No database connection in scope. Hash and
    parse failures differ and are reported separately: a file that vanished
    mid-copy is not worth a row, an unparseable one is what quarantine is for."""
    try:
        sha = sha256_file(entry.path)
    except OSError as exc:
        # Vanished or unreadable between listing and hashing. Common enough
        # during a copy that it is not worth a quarantine row — the next scan
        # either finds it or does not.
        return DocumentRead(io_error=exc.__class__.__name__)

    try:
        document = extract(entry.path)
        after = entry.path.stat()
    except ParseError as exc:
        return DocumentRead(sha256=sha, parse_error=str(exc))
    except OSError as exc:
        # The successful hash does not promise the second open will. A rename or
        # permission change here is ordinary churn, not a reason to fail the pile.
        return DocumentRead(io_error=exc.__class__.__name__)

    # Separate opens for hash and extraction, so neither holds a transaction.
    # Verify the listed identity after: an atomic replacement otherwise stores
    # SHA(A) with text(B), and the next scan makes a false alias.
    if (after.st_size, after.st_mtime_ns, after.st_ino) != (
        entry.size,
        entry.mtime_ns,
        entry.inode,
    ):
        return DocumentRead(io_error="changed_during_read")

    body = document.text
    return DocumentRead(sha256=sha, text=body, text_sha256=text_sha256(body))


def _apply(
    conn: Connection,
    fence: Fence,
    entry: ScanEntry,
    read: DocumentRead,
    present: set[str],
    listing_complete: bool,
    result: ReconcileResult,
) -> None:
    """Decide what the difference means, and write it. No filesystem access.
    Everything it needs arrived in `read`, so it is safe in a transaction — a
    handful of indexed statements, not a parse."""
    path = str(entry.path)
    stat = {"size": entry.size, "mtime_ns": entry.mtime_ns, "inode": entry.inode}

    if read.io_error is not None:
        result.notes.append(f"{entry.path.name}: unreadable ({read.io_error})")
        return
    assert read.sha256 is not None, "a read without an io_error always has a hash"

    # 1 ─ these exact bytes are already a document in this pile.
    seen = doc_repo.by_sha(conn, fence.pile_id, read.sha256)
    twin = (
        doc_repo.by_text_sha(conn, fence.pile_id, read.text_sha256 or "")
        if read.text_sha256
        else None
    )
    previous = _previous_version(
        conn, fence.pile_id, entry.path.name, path, present, listing_complete=listing_complete
    )
    keep = {str(row["id"]) for row in (seen, twin) if row}
    displaced = doc_repo.reconcile_path(conn, fence, path, keep, present, listing_complete)
    result.superseded += displaced
    if seen:
        # Refresh stat metadata even for an existing path (e.g. restoring old bytes).
        doc_repo.add_path(conn, fence, str(seen["id"]), path, stat)
        if path not in (seen["paths"] or {}):
            result.path_added += 1
        if displaced and seen["status"] == "extracted":
            doc_repo.requeue(conn, fence, str(seen["id"]))
        if seen["status"] in ("removed", "superseded"):
            if twin and str(twin["id"]) != str(seen["id"]):
                doc_repo.restore_as_alias(conn, fence, str(seen["id"]), str(twin["id"]))
            else:
                doc_repo.restore(conn, fence, str(seen["id"]))
            result.restored += 1
            result.notes.append(f"{entry.path.name}: restored a previously seen version")
        return

    # 2 ─ could we read it? A parse failure is recorded as a document, so the
    #     pile reports "one file could not be read" rather than quietly holding
    #     one fewer contract.
    if read.parse_error is not None:
        document_id = doc_repo.insert(
            conn,
            fence,
            sha256=read.sha256,
            filename=entry.path.name,
            path=path,
            stat=stat,
            status="queued",
        )
        doc_repo.quarantine(conn, fence, document_id, read.parse_error)
        result.quarantined += 1
        # `ParseError` already leads with the filename, so prefixing it again
        # printed the name twice.
        result.notes.append(read.parse_error)
        LOG.warning("quarantined %s", read.parse_error)
        return

    # 3 ─ same extracted text, different bytes: a format duplicate.
    if twin:
        doc_repo.insert(
            conn,
            fence,
            sha256=read.sha256,
            filename=entry.path.name,
            path=path,
            stat=stat,
            text_sha256=read.text_sha256,
            extracted_text=read.text,
            alias_of=str(twin["id"]),
            status="ingested",
        )
        result.aliased += 1
        result.notes.append(f"{entry.path.name}: same text as {twin['filename']}")
        return

    # 4 ─ this filename existed with different bytes: a new version.
    document_id = doc_repo.insert(
        conn,
        fence,
        sha256=read.sha256,
        filename=entry.path.name,
        path=path,
        stat=stat,
        supersedes=previous,
        status="queued",
    )
    doc_repo.mark_ingested(
        conn, fence, document_id, text_sha=read.text_sha256 or "", body=read.text or ""
    )

    if previous:
        # reconcile_path already retired an overwritten path. A moved-and-edited
        # source may instead have no overlapping path and still needs retirement.
        if displaced == 0:
            doc_repo.mark_superseded(conn, fence, previous)
            result.superseded += 1
        result.notes.append(f"{entry.path.name}: replaces an earlier version")
    else:
        result.added += 1


def _previous_version(
    conn: Connection,
    pile_id: str,
    filename: str,
    path: str,
    present: set[str],
    *,
    listing_complete: bool,
) -> str | None:
    """The live document this file *replaces*, if any. Matching on filename alone
    is a silent failure: `2023/msa.md` and `2024/msa.md` are two contracts sharing
    a name, and superseding one with the other drops a live agreement. So a
    candidate qualifies only when every path it holds is either the one we are
    ingesting or no longer on disk — admitting an edit-in-place and a
    move-and-edit, refusing two files still sitting in different folders."""
    from sqlalchemy import text as sql

    rows = conn.execute(
        sql(
            "SELECT id, paths FROM documents WHERE pile_id = :pile_id AND filename = :filename "
            "AND status IN ('queued', 'ingested', 'extracted') "
            "AND alias_of_document_id IS NULL ORDER BY created_at DESC"
        ),
        {"pile_id": pile_id, "filename": filename},
    ).all()
    for row in rows:
        paths = set(row.paths or {})
        if not listing_complete:
            # An unreadable directory makes "not present" unknowable. The one
            # safe case is an edit in place: the candidate holds only the path
            # being processed, so no absent path is inferred deleted.
            if paths == {path}:
                return str(row.id)
            continue
        others = paths - {path}
        if not (others & present):
            return str(row.id)
    return None


def ingest_pile(
    engine: Engine,
    *,
    fence: Fence,
    watch_path: Path,
    settle_seconds: int = 3,
    deep: bool = False,
    lease_ttl_s: int = 90,
    stop_requested: Callable[[], bool] | None = None,
    skip_root_names: Collection[str] = (DEFAULT_DELIVERABLE_FILENAME,),
) -> ReconcileResult:
    """One pile: list it, then reconcile it a document at a time.

    Takes an `Engine`, not a `Connection`, because the transaction boundary is the
    safety argument. Each document is hashed and parsed with no transaction open,
    and only the short `_apply` write runs inside one, so a slow pile or a huge PDF
    never holds a row lock. The lease is renewed before each read, not on a timer,
    so a two-minute parse still gets the full TTL. A lost lease or shutdown stops
    the pass — every write is fenced anyway, so continuing would only do unsafe or
    pointless work."""
    _check_shutdown(stop_requested)
    listing = scan_pile(watch_path, skip_root_names=skip_root_names)
    result = ReconcileResult(listing_incomplete=not listing.complete)
    present = {str(entry.path) for entry in listing.entries}

    with engine.begin() as conn:
        known = doc_repo.path_map(conn, fence.pile_id)

    for entry in listing.entries:
        _check_shutdown(stop_requested)

        if not is_settled(entry, settle_seconds=settle_seconds):
            # Skipped, not failed: a file still being copied is one we see again
            # in 30 seconds, and hashing it now records a document that never was.
            result.unsettled += 1
            continue
        if not deep and unchanged(entry, known):
            result.unchanged += 1
            continue

        with engine.begin() as conn:
            if not lease_mod.heartbeat(conn, fence.pile_id, fence.epoch, lease_ttl_s):
                raise lease_mod.LostLease(f"lease for pile {fence.pile_id} was taken mid-ingest")

        read = read_document(entry)
        _check_shutdown(stop_requested)

        with engine.begin() as conn:
            _apply(conn, fence, entry, read, present, listing.complete, result)

    _check_shutdown(stop_requested)
    if listing.complete:
        with engine.begin() as conn:
            result.path_removed = doc_repo.prune_missing_paths(conn, fence, present)
            result.removed = doc_repo.mark_removed(conn, fence, present)
            doc_repo.promote_orphaned_aliases(conn, fence)
    else:
        # A folder we couldn't open tells us nothing about whether its documents
        # still exist. Concluding "gone" from "couldn't look" would mark the whole
        # pile `removed` on one chmod or one unmounted share.
        for note in listing.unreadable:
            result.notes.append(f"could not read {note} — nothing marked removed this pass")
        LOG.warning(
            "pile %s listed incompletely (%s); skipping removal detection",
            watch_path.name,
            ", ".join(listing.unreadable),
        )
    if result.changed:
        from documents_analyst.db.repo import events

        with engine.begin() as conn:
            events.append(
                conn,
                pile_id=fence.pile_id,
                run_id=str(uuid.uuid4()),
                type="run.sources_changed",
                payload={},
            )
    return result
