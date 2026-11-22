"""Repository: telemetry — `stage_runs`, and the sink that fills it.

Core produces `LLMCall` records and knows nothing about Postgres; this is the other half.
The sink buffers and flushes, not per call — a node makes one LLM call per section, and a
transaction around each would put a round trip in the hottest loop.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from documents_analyst_core.telemetry.records import LLMCall

INSERT = text("""
    INSERT INTO stage_runs (pile_id, run_id, node, model, tokens_in, tokens_out,
                            attempts, ok, latency_ms, llm_cost_usd)
         VALUES (:pile_id, :run_id, :node, :model, :tokens_in, :tokens_out,
                 :attempts, :ok, :latency_ms, :cost_usd)
""")
"""Not fenced, deliberately: a stale worker's tokens were still spent, and refusing the row
would make the cost report cheaper than the bill. Unlike a document write, a telemetry row
can't corrupt a successor's output."""


@dataclass
class StageRunSink:
    """A `TelemetrySink` that lands in `stage_runs`. Structurally satisfies the protocol in
    `documents_analyst_core.telemetry.sink` (no inheritance — core mustn't be importable here). Given a
    connection at flush time, not construction, so it can outlive one transaction."""

    pile_id: str
    run_id: str
    node: str = ""
    pending: list[LLMCall] = field(default_factory=list)

    def record_llm(self, call: LLMCall) -> None:
        self.pending.append(call)

    def flush(self, conn: Connection) -> int:
        """Write everything buffered and forget it. Clears on success only, so a flush that
        raised mid-way doesn't drop the records it hadn't written — the calls just before a
        failure are the expensive ones."""
        rows = [self._row(call) for call in self.pending]
        for row in rows:
            conn.execute(INSERT, row)
        written = len(rows)
        self.pending.clear()
        return written

    def _row(self, call: LLMCall) -> dict[str, object]:
        return {
            "pile_id": self.pile_id,
            "run_id": self.run_id,
            "node": call.node or self.node,
            "model": call.model,
            "tokens_in": call.input_tokens,
            "tokens_out": call.output_tokens,
            "attempts": call.attempts,
            "ok": call.ok,
            "latency_ms": call.latency_ms,
            "cost_usd": call.cost_usd,
        }


def _unpriced(
    conn: Connection, pile_id: str, run_id: str | None, priced: Collection[str]
) -> list[dict[str, object]]:
    """Models that used tokens but have no entry in the price table, with their call
    counts. Decided from the table, not from a stored zero: a cheap embedding call costs
    less than the `numeric(12,6)` column's smallest step and is stored as 0.000000, and
    reading that as "no price" called a priced model unpriced. Local models (NLI, NER)
    record no tokens, so they are free rather than unpriced."""
    rows = conn.execute(
        text(
            "SELECT model, count(*) AS calls FROM stage_runs WHERE pile_id = :pile_id "
            "AND (CAST(:run_id AS uuid) IS NULL OR run_id = CAST(:run_id AS uuid)) "
            "AND tokens_in + tokens_out > 0 GROUP BY model ORDER BY model"
        ),
        {"pile_id": pile_id, "run_id": run_id},
    ).all()
    return [
        {"model": row.model, "calls": int(row.calls)} for row in rows if row.model not in priced
    ]


def totals_for_pile(
    conn: Connection, pile_id: str, priced: Collection[str] = ()
) -> dict[str, object]:
    """What this pile has cost AND where the time went, by node. `ok = false` rows count — a
    retry still spent its earlier attempts, and dropping them under-reports when a run was
    struggling. `latency_ms` is wall time including retries. `priced` is the set of models
    with a price entry; calls to any other model are listed under `unpriced`."""
    rows = conn.execute(
        text(
            "SELECT node, sum(tokens_in) AS tokens_in, sum(tokens_out) AS tokens_out, "
            "sum(llm_cost_usd) AS cost_usd, count(*) AS calls, "
            "count(*) FILTER (WHERE NOT ok) AS failures, "
            "coalesce(sum(latency_ms), 0) AS latency_ms "
            "FROM stage_runs WHERE pile_id = :pile_id GROUP BY node ORDER BY node"
        ),
        {"pile_id": pile_id},
    ).all()
    by_node = [dict(row._mapping) for row in rows]
    return {
        "unpriced": _unpriced(conn, pile_id, None, priced),
        "by_node": by_node,
        "cost_usd": sum((r["cost_usd"] or Decimal(0)) for r in by_node),
        "calls": sum(int(r["calls"] or 0) for r in by_node),
        "failures": sum(int(r["failures"] or 0) for r in by_node),
        "latency_ms": sum(int(r["latency_ms"] or 0) for r in by_node),
    }


def totals_for_run(
    conn: Connection, pile_id: str, run_id: str, priced: Collection[str] = ()
) -> dict[str, object]:
    """What one run spent, for its closing timeline event. `unpriced_calls` counts calls
    to models with no entry in `config/model_prices.json`: their tokens are real but
    their dollars are unknown, so a total that silently treated them as free would be
    wrong. Stored as strings and ints — this lands in a JSON event payload."""
    row = conn.execute(
        text(
            "SELECT coalesce(sum(llm_cost_usd), 0) AS cost_usd, count(*) AS calls "
            "FROM stage_runs WHERE pile_id = :pile_id AND run_id = :run_id"
        ),
        {"pile_id": pile_id, "run_id": run_id},
    ).one()
    totals: dict[str, object] = {
        "cost_usd": f"{Decimal(row.cost_usd):.4f}",
        "calls": int(row.calls),
    }
    unpriced = _unpriced(conn, pile_id, run_id, priced)
    if unpriced:
        totals["unpriced_calls"] = sum(int(str(u["calls"])) for u in unpriced)
        totals["unpriced_models"] = [u["model"] for u in unpriced]
    return totals
