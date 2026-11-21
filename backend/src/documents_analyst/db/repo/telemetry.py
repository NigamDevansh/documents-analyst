"""Repository: telemetry — `stage_runs`, and the sink that fills it.

Core produces `LLMCall` records and knows nothing about Postgres; this is the other half.
The sink buffers and flushes, not per call — a node makes one LLM call per section, and a
transaction around each would put a round trip in the hottest loop.
"""

from __future__ import annotations

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


def totals_for_pile(conn: Connection, pile_id: str) -> dict[str, object]:
    """What this pile has cost AND where the time went, by node. `ok = false` rows count — a
    retry still spent its earlier attempts, and dropping them under-reports when a run was
    struggling. `latency_ms` is wall time including retries."""
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
        "by_node": by_node,
        "cost_usd": sum((r["cost_usd"] or Decimal(0)) for r in by_node),
        "calls": sum(int(r["calls"] or 0) for r in by_node),
        "failures": sum(int(r["failures"] or 0) for r in by_node),
        "latency_ms": sum(int(r["latency_ms"] or 0) for r in by_node),
    }
