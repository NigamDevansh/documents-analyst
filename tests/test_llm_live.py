"""Real provider calls — Phase 2A.2. NOT part of the normal suite.

Everything else about the LLM adapters is tested offline against a stub, which
covers what we own: retries, accounting, the schema gate, error mapping. What a
stub cannot tell us is whether each provider's *own* structured-output payload
is spelled correctly — the argument names, the nesting, where the usage numbers
live. That is only knowable by calling them.

So these exist, they spend a few tokens, and they skip themselves by default:

    RUN_LIVE_LLM=1 uv run pytest -m live -q

They skip rather than fail when a provider's key is absent — the point is to
check the providers you have configured, not to demand all three.

Marked `live` so `pytest -m "not live"` excludes them wholesale, and guarded by
the env var so the plain suite stays offline with no configuration at all.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from documents_analyst.settings import Settings
from documents_analyst_core.portable.llm_client import LLMQuotaExhausted, build_client

pytestmark = pytest.mark.live

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "capital": {"type": "string"},
        "confident": {"type": "boolean"},
    },
    "required": ["capital", "confident"],
    "additionalProperties": False,
}


def client_for(provider: str):  # noqa: ANN201 — the concrete class is the point
    if os.environ.get("RUN_LIVE_LLM") != "1":
        pytest.skip("set RUN_LIVE_LLM=1 to spend tokens on the real providers")

    settings = Settings()
    key = {
        "gemini": settings.gemini_api_key,
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
    }[provider]
    if not key:
        pytest.skip(f"no key configured for {provider}")

    model = (
        settings.resolved_model_extract
        if settings.llm_provider == provider
        else settings.llm_default_models[provider][0]
    )
    return build_client(provider=provider, api_key=key, model=model)


@contextmanager
def no_credit_is_not_a_failure(provider: str) -> Iterator[None]:
    """An account with no credit cannot verify our payload spelling either way.

    Skipping says that honestly. Failing forever on a billing state would teach
    whoever runs this to ignore the result — and this file only earns its cost
    if its failures are believed.
    """
    try:
        yield
    except LLMQuotaExhausted as exc:
        pytest.skip(f"{provider}: no credit on this account — {str(exc)[:120]}")


@pytest.mark.parametrize("provider", ["gemini", "anthropic", "openai"])
def test_structured_output_really_is_constrained(provider: str) -> None:
    """The one thing a stub cannot check: that our request actually reaches the
    provider's structured-output path, spelled the way that provider wants."""
    client = client_for(provider)

    with no_credit_is_not_a_failure(provider):
        payload, usage = client.structured(
            system="Answer only from the schema.",
            user="What is the capital of France?",
            schema=SCHEMA,
            max_tokens=200,
        )

    assert set(payload) == {"capital", "confident"}
    assert isinstance(payload["confident"], bool)
    assert "Paris" in payload["capital"]
    assert usage.input_tokens > 0, "token accounting must survive the real response shape"
    assert usage.output_tokens > 0
    assert usage.attempts == 1


@pytest.mark.parametrize("provider", ["gemini", "anthropic", "openai"])
def test_plain_completion_returns_text_and_counts_tokens(provider: str) -> None:
    client = client_for(provider)

    with no_credit_is_not_a_failure(provider):
        text, usage = client.complete(system="Reply in one word.", user="Ping?", max_tokens=100)

    assert text.strip()
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
