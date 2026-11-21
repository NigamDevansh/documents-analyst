"""GLiNER wrapper behaviour — Phase 2A.2.

The model itself is **not** loaded here. Prefetching it into the image is Phase
2C, so a load test now would download ~300MB at test time and break the "the
suite runs offline" claim it is supposed to support. What is tested is
everything around the model: that an unavailable model is loud, that a disabled
tier is visible, and that spans are checked against their own source.

The real-model test lands in 2C with the prefetch stage, marked `models`.
"""

from __future__ import annotations

from typing import Any

import pytest

from documents_analyst_core.portable.ner_gliner import (
    DisabledNER,
    GLiNERClient,
    ModelUnavailable,
    Span,
)

TEXT = "This agreement is between Acme Corporation and Beta Logistics LLC."


class StubGLiNER(GLiNERClient):
    """Replaces only the model. The offset checking is the real code."""

    def __init__(self, entities: list[dict[str, Any]]) -> None:
        super().__init__()
        self._entities = entities
        self.predict_calls = 0

    def load(self) -> Any:
        client = self

        class Model:
            def predict_entities(
                self, text: str, labels: list[str], threshold: float = 0.5
            ) -> list[dict[str, Any]]:
                client.predict_calls += 1
                return client._entities

        return Model()


def entity(text: str, *, label: str = "organization", score: float = 0.9) -> dict[str, Any]:
    start = TEXT.index(text)
    return {"text": text, "label": label, "start": start, "end": start + len(text), "score": score}


# ── the rule that matters most ────────────────────────────────────────────


def test_a_model_that_will_not_load_raises_rather_than_returning_nothing() -> None:
    """The single worst thing this file could do is let a redaction pass report
    "no PII detected" for a document it never scanned.

    In a plain dev venv the cause is that `gliner` lives in the `[ml]` extra and
    is not installed. That is a real deployment state, not an artefact of the
    test — and it and a wrong model id take the same path out.
    """
    client = GLiNERClient(model_name="not-a-real-model/does-not-exist")

    with pytest.raises(ModelUnavailable) as caught:
        client.extract(TEXT, ["person"])

    assert "not-a-real-model" in str(caught.value)


def test_the_error_says_which_setting_to_change_and_what_is_lost() -> None:
    """An error a person can act on without reading our source."""
    client = GLiNERClient(model_name="nope/nope", env_var="GLINER_MODEL", capability="PII tier 2")

    with pytest.raises(ModelUnavailable) as caught:
        client.load()

    message = str(caught.value)
    assert "GLINER_MODEL" in message
    assert "PII tier 2" in message


def test_a_failed_load_is_remembered_not_retried_per_document() -> None:
    """A 300MB download attempt per document would turn one missing model into
    a pile that never finishes."""
    client = GLiNERClient(model_name="nope/nope")

    with pytest.raises(ModelUnavailable):
        client.load()
    first_error = client._load_error

    with pytest.raises(ModelUnavailable):
        client.load()
    assert client._load_error is first_error


def test_availability_can_be_asked_without_raising() -> None:
    """So a caller can choose to degrade before starting work rather than half
    way through it."""
    assert GLiNERClient(model_name="nope/nope").is_available is False


# ── spans ─────────────────────────────────────────────────────────────────


def test_spans_carry_offsets_into_the_text_that_was_passed_in() -> None:
    client = StubGLiNER([entity("Acme Corporation")])

    spans = client.extract(TEXT, ["organization"])

    assert len(spans) == 1
    span = spans[0]
    assert isinstance(span, Span)
    assert TEXT[span.start : span.end] == span.text == "Acme Corporation"


def test_a_span_whose_offsets_disagree_with_the_source_is_dropped() -> None:
    """Same class of bug Gate 1 exists to stop, caught one layer earlier: those
    offsets would redact the wrong text, or cite a party where it is not."""
    liar = entity("Acme Corporation")
    liar["start"] += 3

    spans = StubGLiNER([liar]).extract(TEXT, ["organization"])

    assert spans == []


@pytest.mark.parametrize(
    ("start", "end", "why"),
    [
        (-4, 21, "negative start — Python slices from the END and can still match"),
        (-len(TEXT), len(TEXT), "whole-string negative index"),
        (20, 5, "reversed — slices to '' and passes whenever matched is empty"),
        (26, len(TEXT) + 500, "end past the string — slicing silently clamps"),
        (len(TEXT) + 10, len(TEXT) + 20, "entirely out of range — slices to ''"),
    ],
)
def test_offsets_outside_the_text_are_refused(start: int, end: int, why: str) -> None:
    """Python slicing is forgiving in exactly the ways that hurt here.

    `text[start:end] == matched` alone lets all of these through, and each one
    then becomes a redaction target or a citation anchor pointing somewhere
    other than where it claims. The bounds are checked before the slice.
    """
    matched = TEXT[start:end]
    bad = {"text": matched, "label": "organization", "start": start, "end": end, "score": 0.9}

    assert StubGLiNER([bad]).extract(TEXT, ["organization"]) == [], why


def test_an_empty_span_is_refused_even_when_it_matches() -> None:
    """`start == end` slices to `""`, which equals a `""` match. A zero-width
    span redacts nothing and cites nothing, so accepting it only adds a row
    that claims a finding."""
    empty = {"text": "", "label": "organization", "start": 5, "end": 5, "score": 0.9}

    assert StubGLiNER([empty]).extract(TEXT, ["organization"]) == []


def test_a_valid_span_at_the_very_end_is_still_accepted() -> None:
    """The bound is `end <= len(text)`, not `<`. Getting that wrong would drop
    every entity that closes a document."""
    tail = TEXT[-4:]
    span = {
        "text": tail,
        "label": "organization",
        "start": len(TEXT) - 4,
        "end": len(TEXT),
        "score": 0.9,
    }

    spans = StubGLiNER([span]).extract(TEXT, ["organization"])

    assert [s.text for s in spans] == [tail]


def test_a_malformed_entity_is_dropped_rather_than_crashing_the_pass() -> None:
    """One bad row from the model must not take out a whole document."""
    spans = StubGLiNER([{"text": "Acme"}, entity("Beta Logistics LLC")]).extract(
        TEXT, ["organization"]
    )

    assert [s.text for s in spans] == ["Beta Logistics LLC"]


def test_empty_input_answers_without_touching_the_model() -> None:
    """An empty document genuinely has no entities, and that answer must not
    depend on whether a model happens to be installed."""
    client = StubGLiNER([entity("Acme Corporation")])

    assert client.extract("", ["organization"]) == []
    assert client.extract(TEXT, []) == []
    assert client.predict_calls == 0


# ── the disabled tier ─────────────────────────────────────────────────────


def test_disabled_ner_is_empty_but_says_so() -> None:
    """`GLINER_ENABLED=false` is a valid configuration. The difference between
    "switched off" and "found nothing" has to survive to the report."""
    disabled = DisabledNER()

    assert disabled.extract(TEXT, ["person"]) == []
    assert disabled.enabled is False
    assert disabled.is_available is False
