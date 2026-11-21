"""Party extraction — Phase 2B. No model loaded.

`pick_parties` returns indices, never strings. That is the whole design: the
model's output space is a closed set of spans that were found in the document,
so it physically cannot invent a party name. Checking a returned *name*
afterwards would be weaker — it can only catch inventions we thought to look
for.

GLiNER itself arrives in 2C. Here the `NERClient` protocol is satisfied by a
stub, which is exactly how the real one will be injected.
"""

from __future__ import annotations

import pytest

from documents_analyst_core.entities.parties import (
    extract_party_candidates,
    normalize,
    party_names,
    pick_parties,
)
from documents_analyst_core.portable.llm_client import FakeLLM, LLMTransportError
from documents_analyst_core.portable.ner_gliner import Span
from documents_analyst_core.prompts import wrapping

PREAMBLE = (
    "This Master Services Agreement is entered into between Acme Corporation "
    '("Customer") and Beta Logistics LLC ("Supplier"). Acme Corporation is a '
    "Delaware corporation. Disputes are heard by the American Arbitration "
    "Association."
)


class StubNER:
    """Returns spans located in the real text, so offsets stay honest."""

    def __init__(self, names: list[str]) -> None:
        self.names = names

    def extract(self, text: str, labels: list[str], *, threshold: float = 0.5) -> list[Span]:
        spans = []
        for name in self.names:
            start = text.find(name)
            if start >= 0:
                spans.append(
                    Span(
                        text=name,
                        label="organization",
                        start=start,
                        end=start + len(name),
                        score=0.9,
                    )
                )
        return spans


ALL_THREE = ["Acme Corporation", "Beta Logistics LLC", "American Arbitration Association"]


# ── candidates ────────────────────────────────────────────────────────────


def test_candidates_carry_offsets_into_the_preamble() -> None:
    candidates = extract_party_candidates(PREAMBLE, StubNER(ALL_THREE))

    assert [c.text for c in candidates] == ALL_THREE
    for candidate in candidates:
        assert PREAMBLE[candidate.start : candidate.end] == candidate.text


def test_a_repeated_company_appears_once() -> None:
    """ "Acme Corporation" is named twice in this preamble. Two candidates for
    one party would let the model pick both and the register would show a
    contract between a company and itself."""
    candidates = extract_party_candidates(PREAMBLE, StubNER(ALL_THREE))

    assert len(candidates) == 3
    assert [c.text for c in candidates].count("Acme Corporation") == 1


def test_deduplication_is_on_the_normalised_name() -> None:
    """ "Acme Corp." and "ACME CORPORATION" are one company."""
    text = "Between Acme Corp. and ACME CORPORATION and Beta Logistics LLC."
    candidates = extract_party_candidates(
        text, StubNER(["Acme Corp.", "ACME CORPORATION", "Beta Logistics LLC"])
    )

    assert [c.text for c in candidates] == ["Acme Corp.", "Beta Logistics LLC"]


def test_the_first_mention_is_the_one_kept() -> None:
    """The preamble names each party in full before defining a short form, so
    the first mention is the legal name a register should carry."""
    text = "Between Acme Corporation ('Acme') and Beta Logistics LLC."
    candidates = extract_party_candidates(text, StubNER(["Acme Corporation", "Acme"]))

    assert [c.text for c in candidates] == ["Acme Corporation"]


class LyingNER:
    """A client whose spans do not describe the text it was given.

    Not a straw man: `ner` is a protocol, injected precisely so something other
    than `GLiNERClient` can be passed — a different model, a cached result, a
    future adapter. The guarantee cannot depend on which one arrived.
    """

    def __init__(self, spans: list[Span]) -> None:
        self.spans = spans

    def extract(self, text: str, labels: list[str], *, threshold: float = 0.5) -> list[Span]:
        return self.spans


def span(text: str, start: int, end: int) -> Span:
    return Span(text=text, label="organization", start=start, end=end, score=0.9)


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        (span("Ghost Holdings Ltd", 0, 18), "a name that is nowhere in the preamble"),
        (span("Acme Corporation", -100, 200), "negative start slices from the END"),
        (span("Acme Corporation", 30, 10), "reversed offsets slice to ''"),
        (span("Acme Corporation", 8, 9999), "end past the string clamps silently"),
        (span("", 0, 0), "an empty span"),
    ],
)
def test_a_span_that_does_not_match_the_preamble_is_refused(bad: Span, why: str) -> None:
    """Without this, a company appearing nowhere in the contract reaches the
    register having passed every later gate — `pick_parties` only chooses among
    candidates, and `party_names` only reads them back."""
    assert extract_party_candidates(PREAMBLE, LyingNER([bad])) == [], why


def test_a_bad_span_does_not_discard_the_good_ones_beside_it() -> None:
    """One wrong row from a model must not lose the parties it got right."""
    good = span("Acme Corporation", PREAMBLE.index("Acme Corporation"), 0)
    good = span("Acme Corporation", good.start, good.start + len("Acme Corporation"))

    candidates = extract_party_candidates(
        PREAMBLE, LyingNER([span("Ghost Holdings Ltd", 0, 18), good])
    )

    assert [c.text for c in candidates] == ["Acme Corporation"]


def test_an_empty_preamble_yields_nothing() -> None:
    assert extract_party_candidates("   ", StubNER(ALL_THREE)) == []


# ── the invariant ─────────────────────────────────────────────────────────


def test_party_pick_is_constrained_to_gliner_spans() -> None:
    """A named invariant in TESTING.md.

    The model returns indices. Every name that can reach the register is a
    verbatim span the NER found in the preamble — there is no path by which a
    name the model typed becomes a party.
    """
    candidates = extract_party_candidates(PREAMBLE, StubNER(ALL_THREE))
    llm = FakeLLM(responses=[{"party_indices": [0, 1]}])

    picked = pick_parties(PREAMBLE, candidates, llm)

    assert picked == [0, 1]
    names = party_names(candidates, picked)
    assert names == ["Acme Corporation", "Beta Logistics LLC"]
    for name in names:
        assert name in PREAMBLE, "every party name is a substring of the source"


def test_an_index_outside_the_candidate_list_is_discarded() -> None:
    """The schema bounds these already. Re-checked because a provider whose
    strict mode slipped would otherwise cause an IndexError three frames away
    in whatever consumed the result."""
    candidates = extract_party_candidates(PREAMBLE, StubNER(ALL_THREE))
    llm = FakeLLM(enforce_schema=False, responses=[{"party_indices": [0, 99, -1, "two"]}])

    assert pick_parties(PREAMBLE, candidates, llm) == [0]


def test_the_arbitration_body_can_be_excluded() -> None:
    """The reason a model is asked at all: the NER cannot tell a party from an
    institution merely mentioned."""
    candidates = extract_party_candidates(PREAMBLE, StubNER(ALL_THREE))
    llm = FakeLLM(responses=[{"party_indices": [0, 1]}])

    names = party_names(candidates, pick_parties(PREAMBLE, candidates, llm))

    assert "American Arbitration Association" not in names


def test_no_candidates_means_no_model_call() -> None:
    llm = FakeLLM(responses=[])

    assert pick_parties(PREAMBLE, [], llm) == []
    assert llm.calls == []


def test_an_unreachable_model_degrades_to_all_candidates() -> None:
    """An over-inclusive party list is a matching problem the next stage can
    still work with. An empty one loses the agreement entirely — and the result
    is visibly unfiltered, so the degradation is not hidden."""
    candidates = extract_party_candidates(PREAMBLE, StubNER(ALL_THREE))
    llm = FakeLLM(responses=[LLMTransportError("provider down")])

    assert pick_parties(PREAMBLE, candidates, llm) == [0, 1, 2]


def test_the_preamble_is_data_not_instructions() -> None:
    candidates = extract_party_candidates(PREAMBLE, StubNER(ALL_THREE))
    llm = FakeLLM(responses=[{"party_indices": [0]}])

    pick_parties(PREAMBLE, candidates, llm)

    call = llm.calls[0]
    assert wrapping.system_preamble() in call.system, "the shared injection boundary"
    assert "<untrusted-PREAMBLE>" in call.user
    assert "<untrusted-CANDIDATES>" in call.user
    assert PREAMBLE not in call.system, "no document text in the instruction"


# ── matching across documents ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (["Acme Corp.", "Beta Logistics LLC"], ["ACME CORPORATION", "Beta Logistics, L.L.C."]),
        (["Beta Logistics LLC", "Acme Corp"], ["Acme Corporation", "Beta Logistics"]),
    ],
)
def test_the_same_pair_produces_the_same_key(left: list[str], right: list[str]) -> None:
    """A pair of parties is an unordered set: "Acme and Beta" and "Beta and
    Acme" are one agreement, and an order-sensitive key would file them as
    two."""
    assert normalize(left) == normalize(right)


def test_different_companies_produce_different_keys() -> None:
    assert normalize(["Acme Corp", "Beta LLC"]) != normalize(["Acme Corp", "Gamma LLC"])
