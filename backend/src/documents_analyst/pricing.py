"""Where token prices come from, resolved once.

`config/model_prices.json` ships as `{}` on purpose: we don't publish numbers we can't
verify against an invoice, and a price table with a stale guess produces a cost report
that's confidently wrong — worse than one that says it doesn't know.

So an unpriced model costs `Decimal(0)` and `is_priced` is how a reader tells that apart
from free. The distinction has to survive to the widget; a summary that adds unpriced calls
into a total and prints `$0.00` is exactly the failure this prevents.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from documents_analyst_core.telemetry.pricing import load_prices


@lru_cache(maxsize=4)
def prices(path: Path) -> dict[str, tuple[Decimal, Decimal]]:
    """Cached per path. Read once per process, not once per LLM call. Keyed by path rather
    than cached globally so a test can point at its own table without the first caller's
    answer sticking for the whole session."""
    return load_prices(path)
