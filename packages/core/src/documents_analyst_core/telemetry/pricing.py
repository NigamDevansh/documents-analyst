"""Token counts → dollars.

Prices are loaded from configuration and only convert tokens to dollars for the
cost report — they never stop a call. A model with no rate is reported as
unpriced, never as free. Dollar figures are estimates rather than provider invoices.

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

    A missing file is an empty reporting table: every call is then reported as
    unpriced, and nothing is blocked.
    """
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text())
    parsed = {
        model: (Decimal(str(rate["input"])), Decimal(str(rate["output"])))
        for model, rate in raw.items()
    }
    if any(not amount.is_finite() or amount < 0 for rates in parsed.values() for amount in rates):
        raise ValueError("Model prices must be finite, non-negative USD rates")
    return parsed
