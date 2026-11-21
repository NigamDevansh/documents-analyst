"""Upload — and the row it deliberately does not write — plus extracted text.

Upload writes a file, not a document row. Inserting the row here would mean the API
also decides the sha256, whether the hash or the text hash is already in the pile
(alias vs second contract), whether the parse failed, whether it supersedes something
— all of which exists, tested, in `worker/ingest.py`. A second implementation in a
request handler is a second set of answers that only has to disagree once to
duplicate a contract.

So the API does the part only it can — put the bytes in the pile's folder — and says
"look now"; discovery does the rest, one ingestion path. The response is `202
Accepted` with no document id (it doesn't exist yet); it appears on the stream a
moment later. `SETTLE_SECONDS` is the floor on how soon it's picked up.

See docs/BACKEND_INTERNALS.md § "Routes".
"""

from __future__ import annotations

import logging
import re
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, status

from documents_analyst.api.deps import Config, Db, run
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import piles as pile_repo

LOG = logging.getLogger(__name__)

router = APIRouter(tags=["documents"])

MAX_UPLOAD_BYTES = 64 * 1024 * 1024


@router.get("/piles/{pile_id}/documents")
async def list_documents(pile_id: str, conn: Db) -> dict[str, Any]:
    """The document list, without the extracted text of every one of them."""
    return {"documents": await run(conn, doc_repo.for_pile, pile_id)}


@router.post("/piles/{pile_id}/documents", status_code=status.HTTP_202_ACCEPTED)
async def upload(pile_id: str, file: UploadFile, conn: Db, settings: Config) -> dict[str, Any]:
    """Write the bytes into the pile's watched folder and wake the worker."""
    pile = await run(conn, pile_repo.get, pile_id)
    if pile is None:
        raise HTTPException(status_code=404, detail=f"no pile {pile_id}")

    name = safe_filename(file.filename or "")
    if not name:
        raise HTTPException(status_code=422, detail="a filename is required")
    if name == settings.deliverable_filename:
        # That name, at the pile root, is the generated document: the scanner skips it
        # and the next commit overwrites it. Accepting it would answer 202 for bytes
        # that are never read and then destroyed.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{name} is the name of the generated final document; rename the file",
        )

    body = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB",
        )
    if not body:
        raise HTTPException(status_code=422, detail="the file is empty")

    folder = Path(str(pile["watch_path"]))
    if not folder.is_dir():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"the pile's folder is not reachable from this process: {folder}",
        )

    target = folder / name
    # Written to a temporary neighbour and renamed, because a partly written file in
    # a watched folder is one the scanner may hash. `SETTLE_SECONDS` would cover it,
    # but rename-into-place is atomic on the same filesystem and doesn't depend on a
    # timer being generous enough. A shared staging name would let concurrent uploads
    # of the same final name cross-contaminate — one caller getting 202 for another's
    # bytes — so the staging name is unique.
    staging = folder / f".{name}.{uuid.uuid4().hex}.upload"
    try:
        staging.write_bytes(body)
        staging.replace(target)
    except OSError as exc:
        staging.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"could not write into {folder}: {exc}"
        ) from exc

    await run(conn, pile_repo.notify_work, pile_id)
    LOG.info("uploaded %s into pile %s (%d bytes)", name, pile_id, len(body))
    return {
        "accepted": True,
        "pile_id": pile_id,
        "filename": name,
        "bytes": len(body),
        "detail": (
            "the scanner ingests it on its next pass; watch the pile's event "
            "stream for the run that picks it up"
        ),
    }


@router.get("/documents/{document_id}/text")
async def document_text(document_id: str, conn: Db) -> dict[str, Any]:
    """`extracted_text` — the exact string every citation offset points into. Not the
    file on disk: offsets were recorded against this string during extraction and
    verified by Gate 1, so serving the original bytes would misplace every highlight
    the moment a PDF's extraction differed from its rendering by one character."""
    document = await run(conn, doc_repo.text_of, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail=f"no document {document_id}")
    body = document.get("extracted_text") or ""
    return {
        **{k: v for k, v in document.items() if k != "extracted_text"},
        "text": body,
        "length": len(body),
        "facts": await run(conn, fact_repo.for_document, document_id),
    }


_UNSAFE = re.compile(r"[^A-Za-z0-9._ -]")


def safe_filename(raw: str) -> str:
    """The basename, stripped to characters that can't mean anything else. An uploaded
    filename is attacker-controlled text about to be used as a path. `Path(raw).name`
    removes traversal, the character class removes separators and shell
    metacharacters, and a leading-dot name is renamed not dropped — the scanner
    ignores dotfiles, so keeping it would silently accept a file that's never read.
    Backslashes are normalised to `/` first, because some browsers send the whole
    Windows path and on POSIX `Path` doesn't treat `\\` as a separator — so
    `..\\..\\windows\\system32` would survive as one long mangled name."""
    base = Path(unicodedata.normalize("NFKC", raw).replace("\\", "/")).name
    cleaned = _UNSAFE.sub("_", base).strip()
    while cleaned.startswith("."):
        cleaned = cleaned[1:]
    return cleaned[:180]
