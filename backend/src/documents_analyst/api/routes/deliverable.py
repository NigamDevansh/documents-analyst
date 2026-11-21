"""The final document: the register as we hold it, the file itself, its verification and
its history.

Two views of the same document, and why both exist:

- `GET .../deliverable` is the database view — slots, states, value groups, citation
  counts. It renders before anything has been committed, which is what lets a reviewer
  see the register taking shape while decisions are still open.
- `GET .../deliverable/document` is the file — `<pile>/final-document.docx`, exactly
  as the last commit wrote it. `GET .../deliverable/verify` reads that file back and
  compares it, section by section, to what was recorded as written.

No route here writes the document. Writing belongs to the worker, under the pile's lease,
in the same transaction that records it (`graph/nodes/commit.py`); the API only reads.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

from documents_analyst import deliverable as document
from documents_analyst.api.deps import Config, Db, run
from documents_analyst.db.repo import commits as commit_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.register import slots as slot_repo

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/piles", tags=["deliverable"])

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@router.get("/{pile_id}/deliverable")
async def deliverable(pile_id: str, conn: Db, settings: Config) -> dict[str, Any]:
    """The register as we hold it: every slot, its state, and every value. A disputed
    slot returns ALL its value groups and no current value — not a missing field:
    rendering the first group would state a number nobody agreed and give it a settled
    slot's weight. `orphaned` marks a row every one of whose source files was deleted;
    it still has a value and a citation that resolves, because the extracted text is
    ours and outlives the file, which is exactly why the flag is carried through."""
    pile = await run(conn, pile_repo.get, pile_id)
    if pile is None:
        raise HTTPException(status_code=404, detail=f"no pile {pile_id}")

    slots = await run(conn, slot_repo.for_pile, pile_id)
    version = int(str(pile["deliverable_version"] or 0))
    return {
        "pile_id": pile_id,
        "version": version,
        "document": {
            "filename": settings.deliverable_filename,
            "written": version > 0,
            "sha256": pile["deliverable_sha256"],
            "download": f"/piles/{pile_id}/deliverable/document" if version > 0 else None,
        },
        "slots": [
            {
                "slot_id": slot.slot_id,
                "agreement_id": slot.agreement_id,
                "agreement_label": slot.agreement_label,
                "field_key": slot.field_key or None,
                "subject_free": slot.subject_free,
                "label": slot.label,
                "state": slot.state,
                "resolved_by": slot.resolved_by,
                "orphaned": slot.orphaned,
                "current": _value(slot.current),
                "values": [_value(v) for v in slot.values],
            }
            for slot in slots
        ],
    }


def _value(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "value_group": value.value_group,
        "text": value.value_text,
        "normalized": value.value_normalized,
        "document_ids": list(value.document_ids),
        "citation_count": value.citation_count,
    }


async def _committed(conn: Any, pile_id: str) -> dict[str, Any]:
    pile = await run(conn, pile_repo.get, pile_id)
    if pile is None:
        raise HTTPException(status_code=404, detail=f"no pile {pile_id}")
    record = await run(conn, commit_repo.recorded, pile_id)
    if int(record["deliverable_version"] or 0) == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this pile has no final document yet — nothing has been committed",
        )
    return record


@router.get("/{pile_id}/deliverable/document")
async def download(pile_id: str, conn: Db, settings: Config) -> FileResponse:
    """The final document itself. Resolved by pile NAME inside this process's own
    `watch_root`, never by the path stored in the row: the worker may have written it
    from a container (`/data/watched/acme/…`) that the API sees as `./watched/acme/…`,
    and a stored path is data that should not decide a filesystem read."""
    record = await _committed(conn, pile_id)
    try:
        path = document.path_for(settings, str(record["name"]))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"{path.name} is not on disk; the next run restores it from the record",
        )
    return FileResponse(path, filename=path.name, media_type=DOCX)


@router.get("/{pile_id}/deliverable/verify")
async def verify(pile_id: str, conn: Db, settings: Config) -> dict[str, Any]:
    """Read the file back and compare it to the record — the check `Reconcile` runs after
    every commit, on demand. File parsing is synchronous, so it runs off the event loop."""
    record = await _committed(conn, pile_id)
    sections = document.recorded_sections(record["deliverable_sections"])
    path = document.path_for(settings, str(record["name"]))
    result = await asyncio.to_thread(document.verify, path, sections)
    return {
        "pile_id": pile_id,
        "version": int(record["deliverable_version"]),
        "document_sha256": record["deliverable_sha256"],
        "verified": result.ok,
        "sections_checked": result.sections_checked,
        "problems": result.problems,
        "sections": [
            {"agreement": s.agreement, "heading": s.heading, "sha256": s.sha256} for s in sections
        ],
    }


@router.get("/{pile_id}/deliverable/history")
async def history(pile_id: str, conn: Db) -> dict[str, Any]:
    """What changed, when, and on whose decision — one row per changed section per
    version. Sections that don't appear in a version were not touched by it."""
    if await run(conn, pile_repo.get, pile_id) is None:
        raise HTTPException(status_code=404, detail=f"no pile {pile_id}")
    return {"changes": await run(conn, commit_repo.history, pile_id)}
