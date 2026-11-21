"""Cost and stage accounting — Phase 2E.

Core produces telemetry values and never persists them. These tests check the
values are right and, more importantly, that the places we cannot compute a
number say so instead of reporting zero.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from documents_analyst_core.telemetry.pricing import cost_usd, is_priced, load_prices
from documents_analyst_core.telemetry.records import LLMCall
from documents_analyst_core.telemetry.sink import (
    CollectingSink,
    NullSink,
    TelemetrySink,
    llm_call,
    timed,
)

PRICES = {"gemini-3.6-flash": (Decimal("0.30"), Decimal("2.50"))}


class Usage:
    """Stands in for `portable.llm_client.Usage` — duck-typed on purpose."""

    def __init__(self, model: str = "gemini-3.6-flash", **kw: int) -> None:
        self.model = model
        self.input_tokens = kw.get("input_tokens", 1000)
        self.output_tokens = kw.get("output_tokens", 200)
        self.attempts = kw.get("attempts", 1)


# ── pricing ───────────────────────────────────────────────────────────────


def test_a_priced_model_converts_exactly() -> None:
    """Decimal, not float. A cost report is arithmetic someone may check
    against an invoice, and `0.1 + 0.2` is not `0.3` in binary."""
    cost = cost_usd("gemini-3.6-flash", 1_000_000, 1_000_000, prices=PRICES)

    assert cost == Decimal("2.80")
    assert isinstance(cost, Decimal)


def test_an_unknown_model_costs_zero_and_says_it_is_unknown() -> None:
    """Zero is not "free". Without `is_priced`, a call we could not price is
    indistinguishable from one that was free, and a total reads as complete
    when it is partial."""
    assert cost_usd("some-new-model", 5000, 500, prices=PRICES) == Decimal(0)
    assert is_priced("some-new-model", PRICES) is False
    assert is_priced("gemini-3.6-flash", PRICES) is True


def test_the_shipped_table_is_empty() -> None:
    """Deliberate. Per-token rates change without notice and cannot be verified
    from inside this process, so a cost summary reads "unpriced" rather than a
    plausible wrong dollar figure."""
    from documents_analyst_core.telemetry.pricing import EMPTY

    assert EMPTY == {}


def test_prices_load_from_a_file_as_decimals(tmp_path: Path) -> None:
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({"m": {"input": "0.15", "output": "0.60"}}))

    prices = load_prices(path)

    assert prices["m"] == (Decimal("0.15"), Decimal("0.60"))
    assert cost_usd("m", 1_000_000, 0, prices=prices) == Decimal("0.15")


def test_a_missing_price_file_is_an_empty_table_not_an_error(tmp_path: Path) -> None:
    """Running unpriced is a supported state. Refusing to start because nobody
    filled in a price would stop a pile for a reporting detail."""
    assert load_prices(tmp_path / "absent.json") == {}


# ── records ───────────────────────────────────────────────────────────────


def test_a_record_is_built_from_whatever_usage_the_client_returned() -> None:
    call = llm_call(node="extract", usage=Usage(), prices=PRICES, latency_ms=1234)

    assert call.model == "gemini-3.6-flash"
    assert (call.input_tokens, call.output_tokens) == (1000, 200)
    assert call.latency_ms == 1234
    assert call.cost_usd == Decimal("0.0008")


def test_retried_attempts_are_carried_into_the_record() -> None:
    """`Usage.attempts` counts failures too, and `stage_runs.attempts` is where
    that has to land — a retry that succeeded on the third try still cost
    tokens twice over."""
    call = llm_call(node="extract", usage=Usage(attempts=3), prices=PRICES)

    assert call.attempts == 3


def test_a_failed_call_is_still_a_record() -> None:
    """`ok=False` rows exist so a cost report cannot hide the spend of calls
    that produced nothing."""
    call = llm_call(node="extract", usage=Usage(), prices=PRICES, ok=False)

    assert call.ok is False
    assert call.cost_usd > 0, "it failed, it was not free"


def test_the_prompt_version_travels_with_the_call() -> None:
    """Not persisted — no column, by decision — but carried, so "did the prompt
    change between these two runs" is answerable from a log line."""
    from documents_analyst_core.prompts import extraction

    call = llm_call(node="extract", usage=Usage(), prices=PRICES, prompt_version=extraction.VERSION)

    assert call.prompt_version == "extraction/1"


def test_telemetry_does_not_import_the_portable_zone() -> None:
    """`llm_call` takes `usage` as a plain object on purpose.

    `portable/` is copied into repositories that do not have this package, so
    the dependency has to point one way. An import here would make the portable
    zone un-copyable the moment someone used telemetry from it.
    """
    import ast

    telemetry = (
        Path(__file__).resolve().parent.parent
        / "packages/core/src/documents_analyst_core/telemetry"
    )
    for module in sorted(telemetry.glob("*.py")):
        tree = ast.parse(module.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "portable" not in node.module, f"{module.name} imports {node.module}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "portable" not in alias.name, f"{module.name} imports {alias.name}"


# ── timing ────────────────────────────────────────────────────────────────


def test_timed_reports_elapsed_milliseconds() -> None:
    with timed() as elapsed:
        sum(range(200_000))

    assert elapsed() >= 0
    assert isinstance(elapsed(), int)


def test_timed_is_readable_after_the_block_and_stops_counting() -> None:
    """Read once after the block and it must not keep growing — otherwise the
    number recorded depends on how soon the caller got round to reading it."""
    with timed() as elapsed:
        pass
    first = elapsed()
    sum(range(200_000))

    assert elapsed() == first


def test_timed_measures_the_whole_exchange_including_a_retry() -> None:
    """`stage_runs.latency_ms` should say how long the caller waited, retries
    included. Timing only the successful attempt would report a call that took
    three tries as though it took one."""
    with timed() as elapsed:
        for _ in range(3):
            sum(range(100_000))
    total = elapsed()

    with timed() as one:
        sum(range(100_000))

    assert total >= one()


# ── sinks ─────────────────────────────────────────────────────────────────


def test_the_null_sink_satisfies_the_protocol() -> None:
    """For callers with no `stage_runs` table — telemetry must not become a reason
    they need one."""
    sink: TelemetrySink = NullSink()

    sink.record_llm(llm_call(node="n", usage=Usage(), prices=PRICES))


def test_the_collecting_sink_totals_tokens_and_cost() -> None:
    sink = CollectingSink()
    sink.record_llm(llm_call(node="extract", usage=Usage(), prices=PRICES))
    sink.record_llm(llm_call(node="judge", usage=Usage(input_tokens=500), prices=PRICES))

    assert sink.total_tokens == (1500, 400)
    assert sink.total_cost_usd == Decimal("0.00145")


def test_unpriced_models_are_named_not_folded_into_the_total() -> None:
    """The one number a summary must never present silently.

    Without this, calls we could not price look identical to calls that were
    free, and `$0.00` reads as a complete figure when it is a partial one.
    """
    sink = CollectingSink()
    sink.record_llm(llm_call(node="extract", usage=Usage(model="mystery-1"), prices=PRICES))
    sink.record_llm(llm_call(node="judge", usage=Usage(), prices=PRICES))

    assert sink.unpriced_models == {"mystery-1"}
    assert sink.total_cost_usd > 0, "the priced call still contributes"


def test_failures_are_queryable() -> None:
    sink = CollectingSink()
    sink.record_llm(llm_call(node="a", usage=Usage(), prices=PRICES, ok=False))
    sink.record_llm(llm_call(node="b", usage=Usage(), prices=PRICES))

    assert [c.node for c in sink.failures] == ["a"]


# ── the table this has to fill ────────────────────────────────────────────


def test_every_stage_runs_column_has_a_source() -> None:
    """`stage_runs.latency_ms` existed for four phases with nothing filling it.

    This walks the migration and checks each column is either on a record or is
    one of the two the application supplies, so the next column added to the
    table cannot sit empty unnoticed.
    """
    sql = (Path(__file__).resolve().parent.parent / "db/migrations/002_schema.sql").read_text()
    block = sql.split("CREATE TABLE stage_runs (", 1)[1].split(");", 1)[0]
    columns = {
        m.group(1) for line in block.splitlines() if (m := re.match(r"\s{4}(\w+)\s+\w", line))
    }

    from_application = {"id", "pile_id", "run_id", "created_at", "node"}
    from_llm = {"model", "tokens_in", "tokens_out", "attempts", "ok", "latency_ms", "llm_cost_usd"}
    unaccounted = columns - from_application - from_llm
    assert not unaccounted, f"stage_runs columns nothing fills: {sorted(unaccounted)}"

    fields = set(LLMCall.__dataclass_fields__)
    assert {"latency_ms", "attempts", "ok", "cost_usd"} <= fields


@pytest.mark.parametrize("record", [LLMCall])
def test_records_are_frozen(record: type) -> None:
    """A telemetry record that can be edited after the fact is not a record."""
    assert record.__dataclass_params__.frozen
