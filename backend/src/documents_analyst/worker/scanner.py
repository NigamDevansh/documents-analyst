"""The reconciling scanner — scan is truth, notify is only a latency hint.

Every pass asks "what is in this pile now" and diffs against the DB; it consumes
no event queue, so a missed event or a crash mid-scan costs latency and nothing
else, which is what makes ingestion recoverable without a durable event log.
Swapping `os.scandir` for `list_objects_v2` changes the listing and nothing
downstream. Everything here is a pure function of the filesystem — no database,
no clock but the one passed in — so the awkward cases (a half-copied file, a Word
lock file, a vanished folder) test without a container.

See docs/WORKER_RUNTIME.md § "What \"scanning\" actually is".
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from documents_analyst_core.portable.document_text import SUFFIXES


@dataclass(frozen=True, slots=True)
class ScanEntry:
    path: Path
    size: int
    mtime_ns: int
    inode: int = 0
    """Part of the "has this changed" check. An atomic-rename replacement keeps
    the path and can match size and mtime, but the inode changes — and editors
    that write-temp-then-rename are the common case, not an exotic one."""


IGNORED_PREFIXES = ("~$", ".", "._")
"""`~$doc.docx` is Word's lock file, present while a document is OPEN — the single
most likely thing to appear in a folder someone is working in. Ingesting one
yields a document that is a few hundred bytes of ownership metadata."""

IGNORED_SUFFIXES = (".part", ".partial", ".tmp", ".temp", ".crdownload", ".download", ".swp")
"""In-progress downloads and editor scratch files. They are renamed into place
when complete, so the finished file arrives as a new path anyway."""

IGNORED_DIRS = {".git", "__pycache__", ".DS_Store", "node_modules", ".Trash"}

IGNORED_NAMES = {"thumbs.db", "ehthumbs.db", "desktop.ini", "icon\r"}
"""Files the operating system drops into any folder (Windows thumbnail caches and
folder settings, the macOS custom-icon file). Not documents anyone put there, so
not quarantined either — a quarantine would list them in the final document as
"not analysed" sources. Compared lower-cased."""

RULEBOOK_LOOKALIKES = {"rulebook.yml", "rulebook.json", "rulebook.txt"}
"""Near-misses for `rulebook.yaml`. Skipped rather than quarantined as unsupported
documents, and logged once, because the person meant a rulebook and it is not
being applied."""

LOG = logging.getLogger(__name__)
_WARNED: set[str] = set()


def ignored_name(name: str) -> bool:
    """Name-based, not content-based: a file we shouldn't read must be excluded
    before we open it, so this can't depend on anything inside it."""
    lowered = name.lower()
    return (
        name.startswith(IGNORED_PREFIXES)
        or lowered.endswith(IGNORED_SUFFIXES)
        or lowered in IGNORED_NAMES
    )


def supported(name: str) -> bool:
    """Extensions `document_text` can read. Sourced from there, not restated, so
    adding a format doesn't silently leave the scanner refusing it."""
    return Path(name).suffix.lower() in SUFFIXES


def list_pile_dirs(watch_root: Path) -> list[Path]:
    """Every subdirectory is a pile. No creation step, no config, no API call."""
    if not watch_root.is_dir():
        return []
    return sorted(
        entry for entry in watch_root.iterdir() if entry.is_dir() and entry.name not in IGNORED_DIRS
    )


@dataclass(frozen=True, slots=True)
class Listing:
    """What a scan found, and whether it managed to look everywhere.

    `complete` matters because an empty list is two different facts in the same
    clothes: "no documents here" and "couldn't read here". Reconciliation treats
    absence as deletion, so conflating them turns one `chmod` or unmounted share
    into every document in the pile being marked `removed`. Only a listing that
    looked everywhere may conclude anything about what is gone."""

    entries: list[ScanEntry] = field(default_factory=list)
    complete: bool = True
    unreadable: tuple[str, ...] = ()

    def __iter__(self) -> Iterator[ScanEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


def scan_pile(pile_dir: Path, *, skip_root_names: Collection[str] = ()) -> Listing:
    """Every readable document under `pile_dir`, recursively. Sorted by path so a
    scan is reproducible, which is what makes the diff against the previous scan
    meaningful rather than incidental.

    `skip_root_names` are files at the pile root that are ours, not sources — the
    generated final document. Skipped at the root only: a file of the same name in a
    subfolder was put there by a person and is read like any other."""
    unreadable: list[str] = []
    skip = frozenset(skip_root_names)
    entries = sorted(
        (
            e
            for e in _walk(pile_dir, unreadable)
            if not (e.path.parent == pile_dir and e.path.name in skip)
        ),
        key=lambda e: e.path,
    )
    return Listing(entries=entries, complete=not unreadable, unreadable=tuple(unreadable))


def _walk(directory: Path, unreadable: list[str]) -> Iterator[ScanEntry]:
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        # Not an error — raising would abandon every other pile for one locked
        # subfolder. Recorded, because a folder we couldn't open tells us nothing
        # about whether its documents still exist.
        unreadable.append(f"{directory}: {exc.__class__.__name__}")
        return

    for entry in entries:
        if entry.name in IGNORED_DIRS or ignored_name(entry.name):
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from _walk(Path(entry.path), unreadable)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if entry.name == "rulebook.yaml":
                continue
            if entry.name.lower() in RULEBOOK_LOOKALIKES:
                if entry.path not in _WARNED:
                    _WARNED.add(entry.path)
                    LOG.warning(
                        "%s is not applied: the rulebook must be named rulebook.yaml", entry.path
                    )
                continue
            stat = entry.stat()
        except FileNotFoundError:
            # Genuinely gone between listing and stat: a real deletion, so it must
            # NOT make the listing incomplete — else deleting a file would suppress
            # the reconciliation that notices it.
            continue
        except OSError as exc:
            # Present but unreadable. Different fact, opposite conclusion.
            unreadable.append(f"{entry.path}: {exc.__class__.__name__}")
            continue
        yield ScanEntry(
            path=Path(entry.path),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            inode=stat.st_ino,
        )


def is_settled(
    entry: ScanEntry, *, settle_seconds: int, now: Callable[[], float] = time.time
) -> bool:
    """Unchanged for `settle_seconds`, judged from the age of its mtime. One
    `stat()`, no state between scans: a file still being written has a recent
    mtime, fails this, and is skipped until the next pass.

    Age rather than two probes because age survives a restart — a worker that
    crashes mid-copy has forgotten any earlier probe and would hash a half-written
    file on its first scan, exactly when a crash is most likely. The clock doesn't
    forget. The gap: a writer that stalls longer than the window looks settled; the
    deep re-hash every `SCAN_DEEP_EVERY` ticks bounds how long that hides."""
    return (now() - entry.mtime_ns / 1e9) >= settle_seconds


def unchanged(entry: ScanEntry, known: dict[str, tuple[int, int, int]]) -> bool:
    """True when nothing about this path suggests reading it again. Path, size,
    mtime AND inode must match. Inode is the one that catches an atomic replace:
    a temp-file-then-rename can land on the same size and coarse-grained mtime, but
    the inode never coincides."""
    previous = known.get(str(entry.path))
    return previous == (entry.size, entry.mtime_ns, entry.inode)


def sha256_file(path: Path, *, chunk_size: int = 1 << 20) -> str:
    """Streamed, because a pile may hold a 200MB PDF and the worker also holds
    model weights in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(chunk_size):
            digest.update(block)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    """Content identity over EXTRACTED TEXT, not bytes. This makes `msa.md` and
    `msa.docx` one contract instead of two: they extract to identical text. A
    `.pdf` of the same contract does not, so it stays its own document."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
