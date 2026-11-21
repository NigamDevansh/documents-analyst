"""What gets recorded.

One record per interaction, shaped to fill `stage_runs` without the sink having
to compute anything. The sink adds `pile_id` and `run_id`, which are the two
things core does not know.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class LLMCall:
    node: str
    model: str
    input_tokens: int
    output_tokens: int
    attempts: int
    cost_usd: Decimal
    ok: bool
    """False attempts are recorded too. A retry that succeeded on attempt three
    still cost tokens, and a cost report that hides that is lying."""
    latency_ms: int = 0
    """Wall time for the WHOLE exchange, retries included.

    `stage_runs.latency_ms` existed before anything filled it. Measuring only
    the successful attempt would report a call that took nine seconds across
    three tries as though it took three — and "why is this pile slow" is
    exactly the question this column is for.
    """
    prompt_version: str = ""
    """Which prompt produced this call — `extraction/1`, `adjudication/1`.

    Not persisted (no column, by decision), but carried so a log line or a
    debug view can answer "did the prompt change between these two runs"
    without going to git.
    """
