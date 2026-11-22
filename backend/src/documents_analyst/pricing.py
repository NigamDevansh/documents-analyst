"""Where token prices come from, resolved once — and which configured models lack one.

`config/model_prices.json` holds USD rates per million tokens. Prices only turn token
counts into dollars for the cost report: **they never stop a call**. There is no
spending limit. A model with no entry still runs and is reported as unpriced; an entry
past its `valid_until` keeps being used and is reported as expired, so an out-of-date
price table costs accuracy, never a stalled pile. See config/MODEL_PRICING.md.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from documents_analyst_core.telemetry.pricing import load_prices

if TYPE_CHECKING:
    from documents_analyst.settings import Settings

EXPIRY_WARNING_DAYS = 14
"""How far ahead an expiring price is reported — long enough to update the table
before the report starts using a price nobody has checked."""


@lru_cache(maxsize=4)
def prices(path: Path) -> dict[str, tuple[Decimal, Decimal]]:
    """Cached per path. Read once per process, not once per LLM call. Keyed by path rather
    than cached globally so a test can point at its own table without the first caller's
    answer sticking for the whole session."""
    return load_prices(path)


def price_warnings(settings: Settings, today: date | None = None) -> list[dict[str, str]]:
    """One entry per configured model whose price is missing, expired or about to expire.

    Configured means the models this deployment will actually call: extraction, judging,
    and embeddings when an embedding key is set. Read fresh from the file rather than the
    cache, so `/health` shows an edit at once; the cost report picks it up on restart.
    """
    today = today or date.today()
    path = Path(settings.model_prices_path)
    try:
        raw = json.loads(path.read_text()) if path.is_file() else {}
    except (OSError, ValueError):
        return [
            {
                "model": "",
                "state": "invalid",
                "valid_until": "",
                "message": f"{path} could not be read — costs are reported as unpriced.",
            }
        ]

    models = [settings.resolved_model_extract, settings.resolved_model_judge]
    if settings.embedding_api_key:
        models.append(settings.resolved_embedding_model)

    warnings: list[dict[str, str]] = []
    for model in dict.fromkeys(m for m in models if m):
        entry = raw.get(model)
        if not isinstance(entry, dict):
            warnings.append(
                {
                    "model": model,
                    "state": "missing",
                    "valid_until": "",
                    "message": f"No price for {model} in config/model_prices.json — it runs, "
                    "but its cost shows as unpriced.",
                }
            )
            continue
        until = str(entry.get("valid_until") or "")
        if not until:
            continue
        try:
            expires = date.fromisoformat(until)
        except ValueError:
            continue
        if today > expires:
            state, message = (
                "expired",
                f"The price for {model} expired on {until}. It is still used for the cost "
                "report; check the provider's current rate and update config/model_prices.json.",
            )
        elif today >= expires - timedelta(days=EXPIRY_WARNING_DAYS):
            state, message = (
                "expiring",
                f"The price for {model} expires on {until}. Check the provider's current "
                "rate and update config/model_prices.json.",
            )
        else:
            continue
        warnings.append({"model": model, "state": state, "valid_until": until, "message": message})
    return warnings
