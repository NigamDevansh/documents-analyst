"""Three or more values on one field: an amendment chain only when every step is proved.

The judge is scripted and the verbatim-evidence check is stubbed, so these pin the
classifier's logic — ordering, every-pair checking, the reappearing-value guard — without
a database or a model.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from documents_analyst.graph.nodes import classify
from documents_analyst.worker.lease import Fence
from documents_analyst_core.portable.llm_client import Usage

T0 = datetime(2026, 1, 1)


def fact(group: int, value: str, filename: str, day: int, doc: str | None = None) -> dict[str, Any]:
    return {
        "id": f"f{group}-{day}",
        "value_group": group,
        "value_norm": value,
        "value": {"text": value},
        "source_quote": f"payment within {value}",
        "source_document_id": doc or filename,
        "filename": filename,
        "document_created_at": T0 + timedelta(days=day),
        "char_start": 0,
        "char_end": 10,
    }


class Judge:
    """Answers each pair in turn; records what it was asked."""

    def __init__(self, verdicts: list[str]) -> None:
        self.verdicts = list(verdicts)
        self.calls = 0
        self.asked: list[str] = []

    def structured(self, **kwargs: Any) -> tuple[dict[str, Any], Usage]:
        self.calls += 1
        # Which document the later side came from, as the system prompt's
        # relationship line names it.
        later = re.search(r"SIDE_B \(([^)]+)\)", kwargs.get("system", ""))
        self.asked.append(later.group(1) if later else "?")
        verdict = self.verdicts.pop(0)
        return (
            {"verdict": verdict, "reason": f"step {self.calls}", "evidence_quote": "is replaced"},
            Usage(model="judge", input_tokens=1, output_tokens=1),
        )


def context(judge: Judge) -> Any:
    return SimpleNamespace(judge=judge, renew_lease=lambda fence: None, record=lambda *a, **k: None)


REAL_VERIFY = classify._verify_in_document
REAL_PARAGRAPH = classify._paragraph


@pytest.fixture(autouse=True)
def evidence_is_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(classify, "_verify_in_document", lambda ctx, doc, quote, within="": True)
    monkeypatch.setattr(classify, "_paragraph", lambda ctx, fact: ("", 0))


SLOT = {"id": "slot", "field_key": "payment_terms_days"}
CHAIN = [
    [fact(0, "30", "msa.md", 0)],
    [fact(1, "45", "amendment-1.md", 1)],
    [fact(2, "60", "amendment-2.md", 2)],
]


def test_a_verified_chain_is_one_amendment_settling_on_the_latest_value() -> None:
    judge = Judge(["amendment", "amendment"])
    # Passed out of order on purpose: arrival order decides, not the input order.
    verdict = classify._adjudicate(
        context(judge), Fence("p", 1), SLOT, [CHAIN[2], CHAIN[0], CHAIN[1]]
    )
    assert verdict.kind == "amendment" and verdict.chain
    assert verdict.winner_group == 2
    assert judge.calls == 2  # every neighbouring pair, not just the first two
    assert "30 (msa.md) → 45 (amendment-1.md) → 60 (amendment-2.md)" in verdict.reason


def test_one_unproved_step_makes_the_whole_slot_a_conflict() -> None:
    judge = Judge(["amendment", "conflict"])
    verdict = classify._adjudicate(context(judge), Fence("p", 1), SLOT, CHAIN)
    assert verdict.kind == "conflict" and not verdict.chain
    assert "amendment-2.md does not clearly replace amendment-1.md" in verdict.reason


def test_an_older_value_reappearing_later_is_a_conflict_without_asking_the_judge() -> None:
    judge = Judge([])
    groups = [
        [fact(0, "30", "msa.md", 0), fact(0, "30", "invoice.md", 3)],  # the old term, restated
        [fact(1, "45", "amendment-1.md", 1)],
        [fact(2, "60", "amendment-2.md", 2)],
    ]
    verdict = classify._adjudicate(context(judge), Fence("p", 1), SLOT, groups)
    assert verdict.kind == "conflict"
    assert "older value appears again" in verdict.reason
    assert judge.calls == 0


def test_three_way_prose_never_settles_from_an_unchecked_pair() -> None:
    """The original regression: two consent clauses agreeing must not settle a slot
    whose third clause prohibits assignment outright."""
    judge = Judge(["agree", "amendment"])
    groups = [
        [fact(i, value, f"doc{i}.md", i)]
        for i, value in enumerate(["consent needed", "requires consent", "assignment prohibited"])
    ]
    verdict = classify._adjudicate(context(judge), Fence("p", 1), SLOT, groups)
    assert verdict.kind == "conflict"


def test_the_amendment_is_found_when_an_earlier_document_states_the_same_value() -> None:
    """The demo pile: USD 12,000 appears in an invoice, then in the amendment that
    replaces the fee. Showing the judge only the invoice hid the amendment's wording."""
    judge = Judge(["amendment"])
    groups = [
        [fact(0, "10000 USD", "sow.md", 0)],
        [fact(1, "12000 USD", "invoice.md", 1), fact(1, "12000 USD", "amendment.md", 2)],
    ]
    verdict = classify._adjudicate(context(judge), Fence("p", 1), SLOT, groups)
    assert verdict.kind == "amendment" and verdict.winner_group == 1
    assert judge.asked == ["amendment.md"]  # newest first: the amendment is asked about first


def test_each_document_stating_the_later_value_is_tried_until_one_is_an_amendment() -> None:
    judge = Judge(["conflict", "amendment"])
    groups = [
        [fact(0, "10000 USD", "sow.md", 0)],
        [fact(1, "12000 USD", "amendment.md", 1), fact(1, "12000 USD", "invoice.md", 2)],
    ]
    verdict = classify._adjudicate(context(judge), Fence("p", 1), SLOT, groups)
    assert verdict.kind == "amendment" and verdict.winner_group == 1
    assert judge.asked == ["invoice.md", "amendment.md"]


def test_at_most_three_documents_are_tried_and_the_newest_verdict_stands() -> None:
    judge = Judge(["conflict"] * 5)
    later = [fact(1, "12000 USD", f"invoice-{day}.md", day) for day in range(1, 6)]
    verdict = classify._adjudicate(
        context(judge), Fence("p", 1), SLOT, [[fact(0, "10000 USD", "sow.md", 0)], later]
    )
    assert verdict.kind == "conflict"
    assert judge.asked == ["invoice-5.md", "invoice-4.md", "invoice-3.md"]


def test_evidence_is_checked_inside_the_paragraph_shown_before_the_whole_document() -> None:
    """Boilerplate that opens two sections of an amendment is ambiguous across the
    document but exact within the paragraph the judge was shown."""
    body = (
        "3. Fee\n\nSection 4.2 is hereby deleted in its entirety and replaced with the "
        "following: the monthly fee shall be USD 12,000.\n\n4. Terms\n\nSection 5.1 is "
        "hereby deleted in its entirety and replaced with the following: Net 45."
    )
    quote = "is hereby deleted in its entirety and replaced with the following"
    paragraph = body.split("\n\n")[1]

    class Engine:
        def begin(self) -> Any:
            return self

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: Any) -> None:
            return None

        def execute(self, *args: Any, **kwargs: Any) -> Any:
            return self

        def first(self) -> Any:
            return SimpleNamespace(extracted_text=body)

    ctx: Any = SimpleNamespace(engine=Engine())
    assert not REAL_VERIFY(ctx, "doc", quote)  # twice in the document
    assert REAL_VERIFY(ctx, "doc", quote, within=paragraph)
    assert not REAL_VERIFY(ctx, "doc", "hereby amended to USD 12,000", within=paragraph)

    fact = {
        "source_document_id": "doc",
        "char_start": body.index("the monthly"),
        "char_end": body.index(".\n\n4."),
    }
    shown, start = REAL_PARAGRAPH(ctx, fact)
    assert shown == paragraph and body[start:].startswith(paragraph)


def test_an_amendment_verdict_on_the_effective_date_is_never_taken() -> None:
    """An amendment's own start date is not a new start date for the agreement, even
    when the judge points at a general "supersedes conflicting terms" clause."""
    judge = Judge(["amendment"])
    groups = [
        [fact(0, "2025-01-01", "msa.md", 0)],
        [fact(1, "2025-04-01", "amendment.md", 1)],
    ]
    slot = {"id": "slot", "field_key": "effective_date"}
    verdict = classify._adjudicate(context(judge), Fence("p", 1), slot, groups)
    assert verdict.kind == "conflict"
    assert "states its own effective date" in verdict.reason
