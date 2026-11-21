"""The NLI protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class NLIResult:
    entailment: float
    neutral: float
    contradiction: float


class NLIClient(Protocol):
    def classify(self, premise: str, hypothesis: str) -> NLIResult: ...
