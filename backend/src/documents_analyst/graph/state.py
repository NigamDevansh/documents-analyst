"""The state — six fields, all of them control flow.

Everything else is read from the database inside the node that needs it. Not minimalism
for its own sake: LangGraph checkpoints the state on every step, so anything here is
serialised repeatedly and becomes a SECOND copy of data that also lives in a table — two
copies drift, and the checkpoint's is the stale, invisible one.

So the rule: state carries what the graph needs to decide WHERE TO GO. Facts, chunks,
findings and decisions live in Postgres, where the API can read them mid-run and where a
human approving something writes to the same row the graph will read.
"""

from __future__ import annotations

from typing import NotRequired, TypedDict


class PileRunState(TypedDict):
    review_pending: NotRequired[bool]
    budget_id: NotRequired[str]
    """Legacy: written by checkpoints from before the spending limit was removed.
    Kept so those paused runs still load; nothing reads it."""
    pile_id: str
    run_id: str
    lease_epoch: int
    """Carried so every fenced write can prove which generation it belongs to."""
    document_ids: list[str]
    """The batch this run claimed."""
    audit_pass: int
    """1 before the human gate, 2 after commit. Pass 2 only REPORTS — it never
    re-opens the gate, which is what makes the loop terminate."""
    attempt: int
