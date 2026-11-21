"""The shape of a grounded claim.

Every fact in the register is one of these, and none of them exist without a
document, offsets and a verbatim quote.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Citation:
    document_id: str
    chunk_id: str | None
    start: int
    end: int
    quote: str
    page_number: int | None = None
    relocated: bool = False
    """True when the model's offsets were wrong and we found the quote ourselves.

    A fact, not a score. Halving confidence was the first attempt and it does
    not survive contact with the data: a genuinely uncertain fact at 0.5 and a
    relocated certain one at 1.0 arrive at the same number, so "which citations
    did the model actually get right" becomes unanswerable — which is the one
    question this flag exists to answer.

    Immutable and carried on the citation rather than the fact, because it
    describes the *offsets*, not the claim.
    """


@dataclass(frozen=True, slots=True)
class GroundedFact:
    field_key: str
    value_text: str
    """As written in the contract."""
    value_normalized: str | None
    """Comparable form — 30 for "thirty (30) days". None when the field is
    free text and normalisation would invent structure."""
    citation: Citation
    confidence: float
