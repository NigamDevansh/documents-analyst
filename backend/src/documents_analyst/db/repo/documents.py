"""Repository: documents. Where identity is decided.

Three questions get three answers, and conflating any two is how a pile ends up with
a contract disagreeing with itself:

| Question | Answer | Effect |
|---|---|---|
| Same bytes, new path? | `sha256` matches | add the path, **no new row** |
| Same text, different format? | `text_sha256` matches | new row, `alias_of_document_id` |
| Same name, new bytes? | path known, `sha256` differs | new row, `supersedes_document_id` |

The first is enforced by the database (`UNIQUE (pile_id, sha256)`); the other two are
ours.

Every mutating function here takes a `Fence`, not a `pile_id`. A slow scan can outlive
a 90-second lease — a pile of large PDFs on a cold cache is exactly that shape — and
the worker that took over is then writing the same rows. No signature here accepts the
pile and omits the authority, so a write can't be issued without stating which epoch it
holds. Reads are unfenced: a stale reader is harmless.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst.worker.lease import HOLDS_LEASE, Fence, LostLease, fenced


def path_map(conn: Connection, pile_id: str) -> dict[str, tuple[int, int, int]]:
    """`{path: (size, mtime_ns, inode)}` for the whole pile, in one query. Loaded once
    per scan so the inner loop is memory comparisons — per-file queries would make a
    500-document pile 500 round trips to decide nothing changed."""
    rows = conn.execute(
        text(
            "SELECT paths FROM documents WHERE pile_id = :pile_id AND status NOT IN ('removed', 'superseded') ORDER BY updated_at"
        ),
        {"pile_id": pile_id},
    ).all()
    out: dict[str, tuple[int, int, int]] = {}
    for row in rows:
        for path, stat in (row.paths or {}).items():
            out[path] = (
                int(stat.get("size", -1)),
                int(stat.get("mtime_ns", -1)),
                int(stat.get("inode", -1)),
            )
    return out


def by_sha(conn: Connection, pile_id: str, sha256: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "SELECT id, status, paths, text_sha256, filename FROM documents "
            "WHERE pile_id = :pile_id AND sha256 = :sha256"
        ),
        {"pile_id": pile_id, "sha256": sha256},
    ).first()
    return dict(row._mapping) if row else None


def by_text_sha(conn: Connection, pile_id: str, text_sha: str) -> dict[str, Any] | None:
    """The oldest document with this extracted text. Oldest, not newest: the first one
    seen is what existing citations already point at, and re-pointing them at a later
    duplicate would move every offset a reviewer has already looked at."""
    row = conn.execute(
        text(
            "SELECT id, filename FROM documents "
            "WHERE pile_id = :pile_id AND text_sha256 = :text_sha "
            "AND alias_of_document_id IS NULL AND status IN ('ingested', 'extracted') "
            "ORDER BY created_at LIMIT 1"
        ),
        {"pile_id": pile_id, "text_sha": text_sha},
    ).first()
    return dict(row._mapping) if row else None


def add_path(
    conn: Connection, fence: Fence, document_id: str, path: str, stat: dict[str, int]
) -> None:
    """The same bytes at a second path. One document, two locations. A copy of a
    contract into a subfolder isn't a second contract, and treating it as one would put
    the same term in the register twice and report the two as agreeing."""
    fenced(
        conn,
        text(
            "UPDATE documents SET paths = paths || CAST(:entry AS jsonb), updated_at = now() "
            f"WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {
            "id": document_id,
            "entry": json.dumps({path: stat}),
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    )


def insert(
    conn: Connection,
    fence: Fence,
    *,
    sha256: str,
    filename: str,
    path: str,
    stat: dict[str, int],
    text_sha256: str | None = None,
    extracted_text: str | None = None,
    supersedes: str | None = None,
    alias_of: str | None = None,
    status: str = "queued",
) -> str:
    """One document. `ON CONFLICT DO NOTHING` because two workers scanning the same new
    file at once is normal, not a race to prevent."""
    # `INSERT ... SELECT ... WHERE EXISTS`, not VALUES, so the fence is in the same statement:
    # a stale worker inserts nothing rather than being corrected afterwards.
    row = conn.execute(
        text(f"""
            INSERT INTO documents (pile_id, sha256, filename, paths, text_sha256,
                                   extracted_text, supersedes_document_id,
                                   alias_of_document_id, status)
                 SELECT :pile_id, :sha256, :filename, CAST(:paths AS jsonb), :text_sha256,
                        :extracted_text, :supersedes, :alias_of, :status
                  WHERE {HOLDS_LEASE}
            ON CONFLICT (pile_id, sha256) DO NOTHING
              RETURNING id
        """),
        {
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
            "sha256": sha256,
            "filename": filename,
            "paths": json.dumps({path: stat}),
            "text_sha256": text_sha256,
            "extracted_text": extracted_text,
            "supersedes": supersedes,
            "alias_of": alias_of,
            "status": status,
        },
    ).first()
    if row:
        return str(row.id)
    existing = by_sha(conn, fence.pile_id, sha256)
    if existing:
        return str(existing["id"])
    # No row inserted and none already there means the WHERE EXISTS was false:
    # the lease is gone. Silence here would look like a successful no-op.
    raise LostLease(
        f"insert affected 0 rows — epoch {fence.epoch} is stale for pile {fence.pile_id}"
    )


def restore(conn: Connection, fence: Fence, document_id: str) -> None:
    """A `removed` document whose bytes turned up again (a bad sync, an undo). Without this
    the row stays `removed` and its terms never re-enter the register while the file sits in
    the folder. Back to `ingested` if the text is already stored (`queued` otherwise, since
    `extracted_text` is write-once) — `ingested` even with nothing to extract, so the pile is
    re-claimed and a run can clear the stale "source removed" warning."""
    # Not `fenced`: zero rows is ambiguous ("stale epoch" vs "wasn't removed after all"), and
    # LostLease on the latter would be a false concurrency failure. The fence is still in the
    # predicate, so a stale worker writes nothing.
    conn.execute(
        text(
            "UPDATE documents SET status = CASE WHEN extracted_text IS NULL "
            "THEN 'queued' ELSE 'ingested' END, updated_at = now() "
            f"WHERE id = :id AND status IN ('removed', 'superseded') AND {HOLDS_LEASE}"
        ),
        {"id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )

    conn.execute(
        text(
            "UPDATE facts_and_claims SET status = 'active' "
            "WHERE source_document_id = :id AND status = 'superseded' "
            f"AND {HOLDS_LEASE}"
        ),
        {"id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )


def mark_superseded(conn: Connection, fence: Fence, document_id: str) -> None:
    """The old version stays — its facts keep their citations, so the register can still show
    where a superseded term came from. But its claims stop being current: left `active` they
    keep failing rules on text nobody agreed to any more (renegotiate Net 60 to Net 30 and the
    finding never clears while the old Net 60 answers for the agreement)."""
    fenced(
        conn,
        text(
            "UPDATE documents SET status = 'superseded', updated_at = now() "
            f"WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {"id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )
    # Same decision as the line above. `superseded`, not removed, so a reviewer can still see
    # what the previous version said.
    conn.execute(
        text(
            "UPDATE facts_and_claims SET status = 'superseded' "
            "WHERE source_document_id = :id AND status = 'active'"
        ),
        {"id": document_id},
    )


def mark_removed(conn: Connection, fence: Fence, present_paths: set[str]) -> int:
    """Documents whose paths have ALL vanished. All, not any: a document at two paths
    with one deleted is still there. And `removed` rather than DELETE, because facts
    extracted from it are still cited in a register a person may be reading now."""
    rows = conn.execute(
        text(
            "SELECT id, paths FROM documents "
            "WHERE pile_id = :pile_id AND status NOT IN ('removed', 'superseded')"
        ),
        {"pile_id": fence.pile_id},
    ).all()
    gone = [str(row.id) for row in rows if row.paths and not (set(row.paths) & present_paths)]
    if gone:
        fenced(
            conn,
            text(
                "UPDATE documents SET status = 'removed', updated_at = now() "
                f"WHERE id = ANY(:ids) AND {HOLDS_LEASE}"
            ),
            {"ids": gone, "pile_id": fence.pile_id, "epoch": fence.epoch},
        )
        promote_orphaned_aliases(conn, fence)
    return len(gone)


def promote_orphaned_aliases(conn: Connection, fence: Fence) -> int:
    """An alias whose original is gone becomes the original. An alias is excluded from the
    work queue (text already read), so without this the surviving file points at a `removed`
    row and is never extracted — the contract vanishes from the register while its file is on
    disk. Exactly ONE alias is promoted per original (promoting all would recreate the
    duplicate `text_sha256` prevents); oldest first, matching `by_text_sha`."""
    orphans = conn.execute(
        text(
            "SELECT a.id, a.alias_of_document_id AS parent FROM documents a "
            "JOIN documents o ON o.id = a.alias_of_document_id "
            "WHERE a.pile_id = :pile_id AND a.status NOT IN ('removed', 'superseded') "
            "AND o.status IN ('removed', 'superseded') ORDER BY a.created_at"
        ),
        {"pile_id": fence.pile_id},
    ).all()
    if not orphans:
        return 0

    heirs: dict[str, str] = {}
    for row in orphans:
        heirs.setdefault(str(row.parent), str(row.id))

    for parent, heir in heirs.items():
        fenced(
            conn,
            text(
                "UPDATE documents SET alias_of_document_id = NULL, status = 'ingested', "
                f"updated_at = now() WHERE id = :id AND {HOLDS_LEASE}"
            ),
            {"id": heir, "pile_id": fence.pile_id, "epoch": fence.epoch},
        )
        conn.execute(
            text(
                "UPDATE facts_and_claims SET status = 'active' WHERE source_document_id = :heir "
                f"AND status = 'superseded' AND {HOLDS_LEASE}"
            ),
            {"heir": heir, "pile_id": fence.pile_id, "epoch": fence.epoch},
        )
        conn.execute(
            text(
                "UPDATE documents SET alias_of_document_id = :heir, updated_at = now() "
                f"WHERE pile_id = :pile_id AND alias_of_document_id = :parent AND id <> :heir "
                f"AND {HOLDS_LEASE}"
            ),
            {"heir": heir, "parent": parent, "pile_id": fence.pile_id, "epoch": fence.epoch},
        )
    return len(heirs)


def quarantine(conn: Connection, fence: Fence, document_id: str, error: str) -> None:
    """A file we can't read. Recorded, not skipped, so the pile doesn't look complete while a
    document was never considered. Truncated because a parser traceback can run to kilobytes
    and a human reads this column."""
    fenced(
        conn,
        text(
            "UPDATE documents SET status = 'quarantined', quarantine_error = :error, "
            f"attempts = attempts + 1, updated_at = now() WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {
            "id": document_id,
            "error": error[:1000],
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    )


def mark_ingested(
    conn: Connection, fence: Fence, document_id: str, *, text_sha: str, body: str
) -> None:
    """`extracted_text` is WRITE-ONCE. Every char offset in `facts_and_claims` indexes into
    this exact string, so the `WHERE extracted_text IS NULL` guard is load-bearing: a re-ingest
    that rewrote it would shift every stored citation in the pile, silently."""
    # Not `fenced`: zero rows is the ordinary outcome of a re-ingest hitting the write-once
    # guard, not a lost lease. The fence is still in the predicate, so a stale worker can't
    # write.
    conn.execute(
        text(
            "UPDATE documents SET extracted_text = :body, text_sha256 = :text_sha, "
            "status = 'ingested', updated_at = now() "
            f"WHERE id = :id AND extracted_text IS NULL AND {HOLDS_LEASE}"
        ),
        {
            "id": document_id,
            "body": body,
            "text_sha": text_sha,
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    )


def set_extraction_ok(conn: Connection, fence: Fence, document_id: str, *, ok: bool) -> None:
    """Whether extraction actually read this document. `False` means every section call
    failed, so its fact set is known-INCOMPLETE, not known-empty — the difference `assert:
    exists` needs, or a retired model produces a confident finding against a contract that
    states the term clearly. Not guarded on `status`: it records what a model call did,
    whatever the scanner later does to the row."""
    conn.execute(
        text(
            "UPDATE documents SET extraction_ok = :ok, updated_at = now() "
            f"WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {"id": document_id, "ok": ok, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )


EXTRACTION_FAILED_PREFIX = "extraction failed"
"""How a `quarantine_error` marks a file that was READ but whose model extraction kept
failing — as opposed to one that could not be parsed. Retry re-queues only these: the
bytes of an unparseable file have not changed, so trying it again cannot help."""


def record_extraction_failure(
    conn: Connection,
    fence: Fence,
    document_id: str,
    *,
    reason: str,
    max_attempts: int,
) -> bool:
    """Count an incomplete extraction as an attempt; quarantine on the last one.

    Without this a section that always fails (truncated JSON on a dense clause, a
    refusal) left the document `ingested` for ever: re-claimed every scan, every
    section paid for again, and the pile never reaching review. After `max_attempts`
    the file is quarantined with the reason, the rest of the pile carries on, and the
    final document lists it as not analysed. Returns whether it was quarantined."""
    row = conn.execute(
        text(
            "UPDATE documents SET extraction_ok = false, attempts = attempts + 1, "
            "  status = CASE WHEN attempts + 1 >= :max THEN 'quarantined' ELSE status END, "
            "  quarantine_error = CASE WHEN attempts + 1 >= :max THEN :error "
            "                     ELSE quarantine_error END, "
            "  updated_at = now() "
            f"WHERE id = :id AND status = 'ingested' AND {HOLDS_LEASE} "
            "RETURNING status"
        ),
        {
            "id": document_id,
            "max": max_attempts,
            "error": f"{EXTRACTION_FAILED_PREFIX} after {max_attempts} attempts: {reason}"[:1000],
            "pile_id": fence.pile_id,
            "epoch": fence.epoch,
        },
    ).first()
    return bool(row is not None and row.status == "quarantined")


def extraction_incomplete(conn: Connection, agreement_id: str) -> list[str]:
    """Filenames in this agreement whose extraction failed outright. Empty is the answer
    that lets absence-based rules run: every document was read, so a term not in the
    facts is one the contract doesn't state."""
    rows = conn.execute(
        text(
            "SELECT filename FROM documents "
            "WHERE agreement_id = :a AND extraction_ok = false "
            "AND status <> 'removed' ORDER BY filename"
        ),
        {"a": agreement_id},
    ).all()
    return [str(row.filename) for row in rows]


def mark_extracted(conn: Connection, fence: Fence, document_id: str) -> None:
    """This document's facts are written; it's no longer work. Without this it stays
    `ingested`, so `CLAIMABLE` re-claims the pile every tick and re-runs the whole graph.
    Guarded on `status = 'ingested'` so it can't drag a document back out of `superseded`,
    `removed` or `quarantined`, which the scanner can set mid-run."""
    conn.execute(
        text(
            "UPDATE documents SET status = 'extracted', updated_at = now() "
            f"WHERE id = :id AND status = 'ingested' AND {HOLDS_LEASE}"
        ),
        {"id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )


def for_pile(conn: Connection, pile_id: str) -> list[dict[str, Any]]:
    """The document list — without `extracted_text`. Not `SELECT *`: a pile holds a megabyte
    or two of text, and shipping it to draw a sidebar makes the list slow in proportion to
    work done. The text has its own endpoint."""
    rows = conn.execute(
        text(
            "SELECT id, filename, status, sha256, text_sha256, agreement_id, "
            "alias_of_document_id, supersedes_document_id, quarantine_error, paths, "
            "length(extracted_text) AS text_length, created_at, updated_at "
            "FROM documents WHERE pile_id = :pile_id "
            "ORDER BY filename, created_at, id"
        ),
        {"pile_id": pile_id},
    ).all()
    return [dict(row._mapping) for row in rows]


def text_of(conn: Connection, document_id: str) -> dict[str, Any] | None:
    """The extracted text behind every citation offset. `extracted_text`, not the file on
    disk: offsets were recorded against this string, so serving the original bytes would
    misplace every highlight where a PDF's extraction differs from its rendering."""
    row = conn.execute(
        text(
            "SELECT id, pile_id, filename, status, extracted_text, "
            "quarantine_error FROM documents WHERE id = :id"
        ),
        {"id": document_id},
    ).first()
    return dict(row._mapping) if row else None


def counts(conn: Connection, pile_id: str) -> dict[str, int]:
    rows = conn.execute(
        text(
            "SELECT status, count(*) AS n FROM documents WHERE pile_id = :pile_id GROUP BY status"
        ),
        {"pile_id": pile_id},
    ).all()
    return {row.status: int(row.n) for row in rows}


def reconcile_path(
    conn: Connection, fence: Fence, path: str, keep: set[str], present: set[str], complete: bool
) -> int:
    """A path can hold one byte identity. Retain copies at other real paths."""
    rows = conn.execute(
        text(
            "SELECT id, paths FROM documents WHERE pile_id = :pile_id AND paths ? :path "
            "AND status NOT IN ('removed', 'superseded')"
        ),
        {"pile_id": fence.pile_id, "path": path},
    ).all()
    superseded = 0
    for row in rows:
        if str(row.id) in keep:
            continue
        others = set(row.paths or {}) - {path}
        if not others or (complete and not (others & present)):
            mark_superseded(conn, fence, str(row.id))
            superseded += 1
        else:
            conn.execute(
                text(
                    f"UPDATE documents SET paths = paths - :path WHERE id = :id AND {HOLDS_LEASE}"
                ),
                {"id": row.id, "path": path, "pile_id": fence.pile_id, "epoch": fence.epoch},
            )
    return superseded


def restore_as_alias(conn: Connection, fence: Fence, document_id: str, canonical: str) -> None:
    conn.execute(
        text(
            "UPDATE documents SET status = 'ingested', alias_of_document_id = :canonical, updated_at = now() "
            f"WHERE id = :id AND {HOLDS_LEASE}"
        ),
        {"canonical": canonical, "id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )
    conn.execute(
        text(
            "UPDATE facts_and_claims SET status = 'superseded' WHERE source_document_id = :id "
            f"AND status = 'active' AND {HOLDS_LEASE}"
        ),
        {"id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )


def requeue(conn: Connection, fence: Fence, document_id: str) -> None:
    conn.execute(
        text(
            "UPDATE documents SET status = 'ingested', updated_at = now() "
            f"WHERE id = :id AND status = 'extracted' AND alias_of_document_id IS NULL AND {HOLDS_LEASE}"
        ),
        {"id": document_id, "pile_id": fence.pile_id, "epoch": fence.epoch},
    )


def prune_missing_paths(conn: Connection, fence: Fence, present: set[str]) -> int:
    rows = conn.execute(
        text(
            "SELECT id, paths FROM documents WHERE pile_id = :pile_id AND status NOT IN ('removed', 'superseded')"
        ),
        {"pile_id": fence.pile_id},
    ).all()
    changed = 0
    for row in rows:
        old = row.paths or {}
        remaining = {path: stat for path, stat in old.items() if path in present}
        if remaining and len(remaining) != len(old):
            conn.execute(
                text(
                    f"UPDATE documents SET paths = CAST(:paths AS jsonb), updated_at = now() WHERE id = :id AND {HOLDS_LEASE}"
                ),
                {
                    "id": row.id,
                    "paths": json.dumps(remaining),
                    "pile_id": fence.pile_id,
                    "epoch": fence.epoch,
                },
            )
            changed += 1
    return changed
