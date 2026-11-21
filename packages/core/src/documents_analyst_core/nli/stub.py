"""The honest degradation path.

Used when NLI_ENABLED=false or the model will not load. It does not guess: it
returns pure neutral, which routes every pair to LLM adjudication. Slower and
more expensive, still correct. The dishonest alternative — assuming "no
contradiction" — would silently drop real conflicts.
"""

from __future__ import annotations

from .base import NLIResult


class StubNLI:
    def classify(self, premise: str, hypothesis: str) -> NLIResult:
        return NLIResult(entailment=0.0, neutral=1.0, contradiction=0.0)
