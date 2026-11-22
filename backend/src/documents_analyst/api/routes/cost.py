"""LLM spend and time, by stage."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from documents_analyst.api.deps import Db, run
from documents_analyst.db.repo import telemetry as telemetry_repo
from documents_analyst.pricing import prices

router = APIRouter(prefix="/piles", tags=["cost"])


@router.get("/{pile_id}/cost")
async def cost(pile_id: str, conn: Db, request: Request) -> dict[str, Any]:
    """What this pile has cost, by node. Failed calls are included — a retry that
    succeeded on its third attempt still spent the first two. A model with no
    entry in `config/model_prices.json` contributes 0 and is reported as
    unpriced, never free, because a stale guess makes a cost report confidently
    wrong."""
    priced = prices(request.app.state.settings.model_prices_path)
    totals = await run(conn, telemetry_repo.totals_for_pile, pile_id, priced)
    return {"pile_id": pile_id, **totals}
