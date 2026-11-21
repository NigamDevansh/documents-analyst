"""What a node needs that is not in the state.

The state is six fields of control flow, so the engine, models, LLM and telemetry
sink reach a node another way — the typed view of the LangGraph config.
Deliberately not in the state: it isn't serialisable, and the state is
checkpointed every step, so a `RunContext` there would try to pickle a 400MB
model into Postgres.

See docs/BACKEND_INTERNALS.md § "Why everything else is read from the database".
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langchain_core.runnables import RunnableConfig
from sqlalchemy.engine import Engine

from documents_analyst.db.repo.telemetry import StageRunSink
from documents_analyst.models import LoadedModels
from documents_analyst.pricing import prices
from documents_analyst.settings import Settings
from documents_analyst.worker import lease
from documents_analyst.worker.lease import Fence, LostLease
from documents_analyst_core.portable.llm_client import LLMClient
from documents_analyst_core.telemetry.sink import llm_call


@dataclass(frozen=True, slots=True)
class RunContext:
    """Everything the nodes share for one run."""

    engine: Engine
    settings: Settings
    models: LoadedModels
    llm: LLMClient
    telemetry: StageRunSink
    watch_root: Path

    def renew_lease(self, fence: Fence) -> None:
        """Give the next bounded unit of graph work a full lease TTL. Called at
        work boundaries, not from a background thread — a background renewer could
        keep authority alive while a model call is stuck, where renewing before a
        bounded operation lets a genuinely stalled worker be taken over."""
        with self.engine.begin() as conn:
            if not lease.heartbeat(
                conn, fence.pile_id, fence.epoch, ttl_s=self.settings.lease_ttl_s
            ):
                raise LostLease(f"lease for pile {fence.pile_id} was taken before graph work")

    def record(self, node: str, usage: object, *, latency_ms: int, ok: bool = True) -> None:
        """Buffer one LLM exchange for `stage_runs`. Here, not in each node, so
        "what did this cost" is built the same way everywhere — a node computing
        its own `LLMCall` is one that can forget the latency."""
        self.telemetry.record_llm(
            llm_call(
                node=node,
                usage=usage,
                prices=prices(self.settings.model_prices_path),
                latency_ms=latency_ms,
                ok=ok,
            )
        )

    @property
    def max_sections(self) -> int | None:
        """The `SMALL_SAMPLE` dev guard, resolved once here and passed down, so
        core never reads the environment (`extract_facts` takes it as an argument
        for exactly that reason)."""
        return self.settings.small_sample_sections if self.settings.small_sample else None


CONTEXT_KEY = "run_context"


def context_of(config: RunnableConfig) -> RunContext:
    """Pull the context out of a LangGraph config, loudly. A `KeyError` three
    frames into a node is a bad way to learn the graph was invoked without its
    dependencies — and `invoke(state)` with no config is valid LangGraph."""
    configurable = config.get("configurable") or {}
    context = configurable.get(CONTEXT_KEY)
    if context is None:
        raise RuntimeError(
            "the graph was invoked without a RunContext — pass "
            f"config={{'configurable': {{'{CONTEXT_KEY}': ctx, 'thread_id': pile_id}}}}"
        )
    assert isinstance(context, RunContext)
    return context
