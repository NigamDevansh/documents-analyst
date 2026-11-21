"""Token counts → dollars.

Prices are loaded from config, not hardcoded — a stale hardcoded price is a
confidently wrong cost report, worse than none. The shipped table is empty on
purpose: per-token rates change without notice and cannot be verified from here,
so an unknown model costs `Decimal(0)` and reports as unpriced ("1.2M tokens,
unpriced") rather than a plausible wrong figure. Tokens and ops are measured
exactly; dollars are a conversion the operator supplies.

    prices = load_prices(Path("config/model_prices.json"))
    cost = cost_usd("gemini-3.6-flash", 1200, 340, prices=prices)
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

PER_TOKENS = Decimal(1_000_000)
"""Rates are quoted per million tokens by all three providers, so the table is
too. Storing per-token would mean thirteen decimal places in a config file
someone has to edit by hand."""

EMPTY: dict[str, tuple[Decimal, Decimal]] = {}
"""No prices ship. See the module docstring."""


class Unpriced(Exception):
    """Never raised by `cost_usd` — declared so a caller that *wants* to fail
    loudly on an unpriced model can raise it itself, rather than inventing an
    exception type at the call site."""


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    prices: dict[str, tuple[Decimal, Decimal]],
) -> Decimal:
    """Unknown model → `Decimal(0)`. We do not guess a price.

    Zero is not "free" and must not be reported as such — use `is_priced` to
    tell the two apart. A summary that adds unpriced calls into a total and
    prints `$0.00` is the confidently-wrong report this module exists to avoid.
    """
    rate = prices.get(model)
    if rate is None:
        return Decimal(0)
    per_in, per_out = rate
    return (Decimal(input_tokens) * per_in + Decimal(output_tokens) * per_out) / PER_TOKENS


def is_priced(model: str, prices: dict[str, tuple[Decimal, Decimal]]) -> bool:
    """Whether a zero from `cost_usd` means "free" or "we do not know"."""
    return model in prices


def load_prices(path: Path) -> dict[str, tuple[Decimal, Decimal]]:
    """Read `{"model-id": {"input": "0.30", "output": "2.50"}}`, USD per million.

    Strings, not floats: `0.1 + 0.2` is not `0.3` in binary floating point, and
    a cost report is arithmetic someone may check against an invoice. `Decimal`
    from a string is exact.

    A missing file is an empty table, not an error — running unpriced is a
    supported state, and refusing to start because nobody filled in a price
    would stop a pile for a reporting detail.
    """
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text())
    return {
        model: (Decimal(str(rate["input"])), Decimal(str(rate["output"])))
        for model, rate in raw.items()
    }
