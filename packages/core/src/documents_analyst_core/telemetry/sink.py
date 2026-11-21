"""The Sink protocol — how core hands telemetry to the application.

Core defines the shape and calls it. `backend/db` implements it and writes
`stage_runs`. This is what keeps "core imports no SQLAlchemy" true while cost
accounting still ends up in a queryable table.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from .pricing import cost_usd
from .records import LLMCall


class TelemetrySink(Protocol):
    def record_llm(self, call: LLMCall) -> None: ...


class NullSink:
    """Used in tests and by callers that have no `stage_runs` table."""

    def record_llm(self, call: LLMCall) -> None:
        return None


@dataclass
class CollectingSink:
    """Keeps everything in memory. For tests, and for any caller that wants to
    show what a pass cost without owning a database to put it in."""

    llm_calls: list[LLMCall] = field(default_factory=list)

    def record_llm(self, call: LLMCall) -> None:
        self.llm_calls.append(call)

    # ── summaries ─────────────────────────────────────────────────────────
    @property
    def total_tokens(self) -> tuple[int, int]:
        return (
            sum(c.input_tokens for c in self.llm_calls),
            sum(c.output_tokens for c in self.llm_calls),
        )

    @property
    def total_cost_usd(self) -> Decimal:
        return sum((c.cost_usd for c in self.llm_calls), Decimal(0))

    @property
    def unpriced_models(self) -> set[str]:
        """Models that contributed tokens but no dollars.

        The one number a cost summary must never present silently: without
        this, calls we could not price are indistinguishable from calls that
        were free, and the total reads as complete when it is partial.
        """
        return {c.model for c in self.llm_calls if c.cost_usd == 0 and c.input_tokens}

    @property
    def failures(self) -> list[LLMCall]:
        return [c for c in self.llm_calls if not c.ok]


@contextmanager
def timed() -> Iterator[Callable[[], int]]:
    """Elapsed milliseconds for the block, readable after it exits.

    A context manager rather than each caller subtracting two `time.monotonic()`
    readings, because half of them would time only the successful attempt and
    `stage_runs.latency_ms` would then under-report exactly the slow calls it
    exists to surface.

        with timed() as ms:
            payload, usage = llm.structured(...)
        sink.record_llm(LLMCall(..., latency_ms=ms()))

    `monotonic`, not `time()`: a clock adjustment mid-call must not produce a
    negative duration in a report.
    """
    start = time.monotonic()
    elapsed: list[int] = []

    def read() -> int:
        return elapsed[0] if elapsed else int((time.monotonic() - start) * 1000)

    try:
        yield read
    finally:
        elapsed.append(int((time.monotonic() - start) * 1000))


def llm_call(
    *,
    node: str,
    usage: object,
    prices: dict[str, tuple[Decimal, Decimal]],
    latency_ms: int = 0,
    prompt_version: str = "",
    ok: bool = True,
) -> LLMCall:
    """Build a record from the `Usage` an LLM client returned.

    Takes `usage` as `object` deliberately: `telemetry` must not import from
    `portable/`, because the portable zone is copied into repositories that do
    not have this package. Reading the four attributes it needs keeps the
    dependency pointing one way.
    """
    model = str(getattr(usage, "model", ""))
    input_tokens = int(getattr(usage, "input_tokens", 0))
    output_tokens = int(getattr(usage, "output_tokens", 0))
    return LLMCall(
        node=node,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        attempts=int(getattr(usage, "attempts", 1)),
        cost_usd=cost_usd(model, input_tokens, output_tokens, prices=prices),
        ok=ok,
        latency_ms=latency_ms,
        prompt_version=prompt_version,
    )
