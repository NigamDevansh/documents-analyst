"""The claim → source map: field, value, quote, `document_id`, `char_start/end`.

Powers click-a-claim-to-highlight directly: the register is rendered same-origin, so a
click handler reaches the source pane with no cross-origin iframe to work around.

Every row here has already been proved. These are the offsets Gate 1 verified the
quote at, by exact string comparison against `documents.extracted_text` — so a
citation the UI can draw is, by construction, one that was checked. A claim whose
quote wasn't in its document was dropped at extraction and never reached this table.

`relocated` is not a column and doesn't need to be: a repaired offset was repaired by
finding the quote itself, exactly once, in the same text this endpoint hands out.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from documents_analyst.api.deps import Db, run
from documents_analyst.db.repo import facts as fact_repo
from documents_analyst.db.repo import findings as finding_repo
from documents_analyst.db.repo import piles as pile_repo

router = APIRouter(prefix="/piles", tags=["citations"])


@router.get("/{pile_id}/citations")
async def citations(pile_id: str, conn: Db) -> dict[str, Any]:
    if await run(conn, pile_repo.get, pile_id) is None:
        raise HTTPException(status_code=404, detail=f"no pile {pile_id}")
    rows = await run(conn, fact_repo.citations_for_pile, pile_id)
    return {"pile_id": pile_id, "count": len(rows), "citations": rows}


@router.get("/{pile_id}/findings")
async def findings(pile_id: str, conn: Db) -> dict[str, Any]:
    """Open rulebook verdicts, with the citations behind each one. Separate from the
    review queue on purpose: a finding becomes a queue item once, when first raised;
    after that it's a standing statement about the pile, and a reviewer who dismissed
    it shouldn't be asked again. The queue needs an answer; this is what's true."""
    return {"findings": await run(conn, finding_repo.open_for_pile, pile_id)}
