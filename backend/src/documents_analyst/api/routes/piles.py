"""Navigator, status dot, per-pile scoping.

A pile is a folder. There is still no creation step and there is no `POST
/piles` here — inventing one would give the system two ways for a pile to exist,
and the second one would not have a folder behind it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from documents_analyst.api.deps import Db, run
from documents_analyst.db.repo import commits as commit_repo
from documents_analyst.db.repo import decisions as decision_repo
from documents_analyst.db.repo import documents as doc_repo
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import findings as finding_repo
from documents_analyst.db.repo import piles as pile_repo
from documents_analyst.db.repo import slots as slot_repo

router = APIRouter(prefix="/piles", tags=["piles"])


@router.get("")
async def list_piles(conn: Db) -> dict[str, Any]:
    """Every pile with the counts the navigator shows beside it."""
    return {"piles": await run(conn, pile_repo.summaries)}


@router.get("/{pile_id}")
async def get_pile(pile_id: str, conn: Db) -> dict[str, Any]:
    """One pile, deep enough to draw its header without a second round trip. Counts
    rather than rows: the register, the queue and the timeline each have their own
    endpoint, and returning them all here would make the cheapest request in the app
    the most expensive."""
    pile = await run(conn, pile_repo.get, pile_id)
    if pile is None:
        raise HTTPException(status_code=404, detail=f"no pile {pile_id}")
    return {
        **pile,
        "documents": await run(conn, doc_repo.counts, pile_id),
        "slots": await run(conn, slot_repo.counts_for_pile, pile_id),
        "findings": await run(conn, finding_repo.counts_for_pile, pile_id),
        "facts": await run(conn, fact_repo.count_for_pile, pile_id),
        "pending_decisions": await run(conn, decision_repo.open_count, pile_id),
        "commits": await run(conn, commit_repo.counts_for_pile, pile_id),
    }


@router.get("/{pile_id}/taxonomy-gaps")
async def taxonomy_gaps(pile_id: str, conn: Db) -> dict[str, Any]:
    """Subjects the 28 closed taxonomy keys couldn't express. Not an error list: a
    term with no key is recorded, matched by embedding and rendered — that's what
    `subject_free` is for. This is the product question underneath: a subject that
    recurs across agreements is evidence the taxonomy is missing a field, and the
    count is the order in which to add them."""
    return {"gaps": await run(conn, fact_repo.open_subject_counts, pile_id)}
