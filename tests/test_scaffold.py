"""Load-bearing smoke tests.

Small on purpose, and named for what they cover rather than for the phase they
were written in: Gate 1's verbatim comparison, and the fact that missing keys
degrade instead of crashing. Both are claims the write-up makes, so both are
asserted here rather than left to the suites that exercise them in passing.

The rest of core is covered by the suites that use it. This file is deliberately
not a place to add tests as the tree grows.
"""

from __future__ import annotations

from documents_analyst_core.errors import (
    DocumentsAnalystCoreError,
    ModelUnavailable,
    QuoteNotVerbatim,
)
from documents_analyst_core.nli.stub import StubNLI
from documents_analyst_core.portable.verify import find_quote, verify_quote


class TestGateOne:
    """The verbatim check. Three lines of implementation, and the whole
    'never bluffs' claim rests on them."""

    TEXT = "Payment shall be made within thirty (30) days of invoice."

    def test_exact_match_passes(self) -> None:
        assert verify_quote(self.TEXT, 29, 44, "thirty (30) day")

    def test_wrong_text_at_right_offsets_fails(self) -> None:
        assert not verify_quote(self.TEXT, 29, 44, "forty-five (45)")

    def test_off_by_one_fails(self) -> None:
        """No tolerance, deliberately. An offset that is nearly right points at
        nearly the right text, and 'nearly' is how a citation ends up on the
        wrong clause."""
        assert not verify_quote(self.TEXT, 28, 44, "thirty (30) day")
        assert not verify_quote(self.TEXT, 29, 45, "thirty (30) day")

    def test_right_text_at_wrong_offsets_fails(self) -> None:
        """A model that produces the correct quote but bad offsets is still
        wrong — the offsets are what the citation links to."""
        assert not verify_quote(self.TEXT, 0, 15, "thirty (30) day")

    def test_out_of_range_offsets_do_not_raise(self) -> None:
        """Python slicing clamps rather than raising, which here is the right
        behaviour: an absurd offset returns False instead of crashing a run."""
        assert not verify_quote(self.TEXT, 9000, 9100, "anything")

    def test_empty_quote_never_verifies(self) -> None:
        assert not verify_quote(self.TEXT, 5, 10, "")


class TestFindQuote:
    def test_unique_quote_is_located(self) -> None:
        assert find_quote("alpha beta gamma", "beta") == (6, 10)

    def test_absent_quote_is_none(self) -> None:
        assert find_quote("alpha beta gamma", "delta") is None

    def test_ambiguous_quote_is_none(self) -> None:
        """Two occurrences means we cannot say which was meant. Guessing would
        reintroduce exactly the uncertainty the module removes."""
        assert find_quote("term term", "term") is None


class TestErrors:
    def test_everything_descends_from_one_root(self) -> None:
        assert issubclass(QuoteNotVerbatim, DocumentsAnalystCoreError)

    def test_model_unavailable_names_the_fix(self) -> None:
        """The message has to carry model, env var and lost capability —
        otherwise it degrades to 'model failed' and sends someone into the
        source."""
        err = ModelUnavailable(
            model="knowledgator/gliner-pii-base-v1.0",
            env_var="GLINER_MODEL",
            capability="PII tier 2",
        )
        text = str(err)
        assert "gliner-pii-base" in text
        assert "GLINER_MODEL" in text
        assert "PII tier 2" in text


def test_nli_stub_is_neutral_not_confident() -> None:
    """The honest degradation path. Returning 'no contradiction' would look
    like a clean result and silently drop real conflicts; neutral routes every
    pair to LLM adjudication instead."""
    result = StubNLI().classify("Payment is 30 days.", "Payment is 45 days.")
    assert result.neutral == 1.0
    assert result.contradiction == 0.0
    assert result.entailment == 0.0


class TestSettings:
    def test_no_keys_degrades_and_names_all_of_them(self) -> None:
        """`docker compose up` with an untouched .env must start. Reporting
        every missing key at once is what stops the fix-one-restart-repeat
        loop."""
        from documents_analyst.settings import Settings

        settings = Settings(_env_file=None)
        assert settings.is_degraded
        missing = settings.missing_requirements()
        assert len(missing) == 2
        assert any("GEMINI_API_KEY" in m for m in missing)
        # Embeddings are OpenAI whatever LLM_PROVIDER says, so the key is its own
        # requirement and its own line.
        assert any("OPENAI_API_KEY" in m for m in missing)
        assert all("disabled" in m for m in missing), "each line must name what is lost"

    def test_keys_present_is_not_degraded(self) -> None:
        from documents_analyst.settings import Settings

        settings = Settings(
            _env_file=None,
            gemini_api_key="gemini-test",
            openai_api_key="openai-test",  # embeddings, whatever the provider is
            llm_provider="gemini",
        )
        assert not settings.is_degraded
        assert settings.missing_requirements() == []

    def test_provider_selects_the_matching_key(self) -> None:
        from documents_analyst.settings import Settings

        settings = Settings(
            _env_file=None,
            llm_provider="gemini",
            openai_api_key="sk-openai-test",
        )
        assert settings.llm_api_key == ""
        assert any("GEMINI_API_KEY" in m for m in settings.missing_requirements())
