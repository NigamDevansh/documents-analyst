"""Pydantic request/response models.

Pydantic lives at boundaries only. Internally we use frozen dataclasses —
validating every internal object costs time and couples internals to the API
layer for no benefit.

Request bodies are modelled; most responses are not. A response model is a second
copy of a shape the repository already defines, and the copies drift: the day a
query gains a column, a strict response model silently drops it and the dashboard
shows a blank field with nothing failing. Requests come from outside, so every one
is validated, and the enums here are the closed lists the API will accept.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

BATCHABLE_KINDS = ("amendment",)
"""What `approve-batch` may settle in one call. One kind, named here rather than
inline so widening it is a visible change to a constant. An amendment is mechanical
("the later document supersedes the earlier"), and a dozen in one pile is a dozen
identical clicks — how a reviewer learns to click without reading. `conflict` and
`finding` are absent on purpose: batching those would be an approve-all button with
a specific-sounding name, and the gate must never let someone agree to something
nobody looked at."""


class DecisionRequest(BaseModel):
    """Approve or reject one item."""

    note: str | None = Field(default=None, max_length=2000)
    chosen_value_group: int | None = None
    """Which value wins, on a slot where sources disagree. Optional: a
    `finding` or a `label_change` has nothing to choose between."""


class BatchApproveRequest(BaseModel):
    # Pydantic deep-copies a default per instance, so a list here is not the
    # shared-mutable-default trap it would be in a plain dataclass.
    kinds: list[Literal["amendment"]] = ["amendment"]  # noqa: RUF012
    note: str | None = Field(default=None, max_length=2000)
