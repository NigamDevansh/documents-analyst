"""Each provider's own `_call` — Phase 2A.2. Fully offline.

`test_llm_client.py` stubs `_call` itself, so it never exercises the three
places the providers actually differ: how the structured-output request is
spelled, how the reply is dug out of the response object, and where the token
counts live. Those are exactly the details a live test found wrong once
already, and they are also the ones most likely to break on an SDK upgrade.

So this stubs one level lower — at the vendor SDK object — and asserts on the
payload each provider is handed. The stubs mirror the real response shapes:

  * Gemini    `response.text`, `usage_metadata.{prompt,candidates,thoughts}_token_count`
  * Anthropic `content[]` blocks, `usage.{input,output}_tokens`
  * OpenAI    `choices[0].message.content`, `usage.{prompt,completion}_tokens`

Verified against the installed SDKs' own field names, not from memory.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from documents_analyst_core.portable.llm_client import (
    AnthropicClient,
    GeminiClient,
    LLMQuotaExhausted,
    LLMSchemaViolation,
    LLMTransportError,
    OpenAIClient,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"capital": {"type": "string"}},
    "required": ["capital"],
    "additionalProperties": False,
}
ANSWER = '{"capital": "Paris"}'


def no_sleep(_seconds: float) -> None:
    return None


# ── Gemini ────────────────────────────────────────────────────────────────


class FakeGeminiSDK:
    def __init__(self, *, text: str = ANSWER, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.seen: list[dict[str, Any]] = []
        self.models = SimpleNamespace(generate_content=self._generate)

    def _generate(self, *, model: str, contents: Any, config: Any) -> Any:
        self.seen.append({"model": model, "contents": contents, "config": config})
        if self.error:
            raise self.error
        return SimpleNamespace(
            text=self.text,
            usage_metadata=SimpleNamespace(
                prompt_token_count=11,
                candidates_token_count=7,
                thoughts_token_count=5,
            ),
        )


def gemini(**kwargs: Any) -> tuple[GeminiClient, FakeGeminiSDK]:
    client = GeminiClient(api_key="k", model="gemini-3.6-flash", sleep=no_sleep)
    sdk = FakeGeminiSDK(**kwargs)
    client._sdk = sdk
    return client, sdk


def test_gemini_sends_the_schema_as_response_json_schema() -> None:
    client, sdk = gemini()

    payload, _ = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)

    assert payload == {"capital": "Paris"}
    config = sdk.seen[0]["config"]
    assert config.response_json_schema == SCHEMA
    assert config.response_mime_type == "application/json"
    assert config.system_instruction == "S"
    assert config.max_output_tokens == 200
    assert sdk.seen[0]["contents"] == "U"


def test_gemini_omits_schema_fields_on_a_plain_completion() -> None:
    """Sending `response_mime_type: application/json` for free text would make
    the model answer in JSON when the caller asked for prose."""
    client, sdk = gemini(text="hello")

    text, _ = client.complete(system="S", user="U", max_tokens=50)

    assert text == "hello"
    assert sdk.seen[0]["config"].response_json_schema is None


def test_gemini_bills_thinking_tokens_as_output() -> None:
    """Leaving `thoughts_token_count` out would under-report exactly the calls
    that cost the most."""
    client, _ = gemini()

    _, usage = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)

    assert usage.input_tokens == 11
    assert usage.output_tokens == 12, "7 candidates + 5 thoughts"


def test_gemini_rate_limit_becomes_a_transport_error() -> None:
    client, _ = gemini(error=RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '13s'"))

    with pytest.raises(LLMTransportError) as caught:
        client.complete(system="S", user="U", max_tokens=50)

    assert not isinstance(caught.value, LLMQuotaExhausted)


# ── Anthropic ─────────────────────────────────────────────────────────────


class FakeAnthropicSDK:
    def __init__(self, *, blocks: list[Any] | None = None, error: Exception | None = None) -> None:
        self.blocks = blocks
        self.error = error
        self.seen: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> Any:
        self.seen.append(kwargs)
        if self.error:
            raise self.error
        blocks = self.blocks
        if blocks is None:
            blocks = [SimpleNamespace(type="tool_use", input={"capital": "Paris"})]
        return SimpleNamespace(
            content=blocks,
            usage=SimpleNamespace(input_tokens=21, output_tokens=9),
        )


def anthropic(**kwargs: Any) -> tuple[AnthropicClient, FakeAnthropicSDK]:
    client = AnthropicClient(api_key="k", model="claude-haiku-4-5-20251001", sleep=no_sleep)
    sdk = FakeAnthropicSDK(**kwargs)
    client._sdk = sdk
    return client, sdk


def test_anthropic_forces_the_tool_so_the_schema_cannot_be_ignored() -> None:
    """`tools` alone only *offers* the shape. Without `tool_choice` the model
    may answer in prose and the schema constrains nothing."""
    client, sdk = anthropic()

    payload, usage = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)

    assert payload == {"capital": "Paris"}
    sent = sdk.seen[0]
    assert sent["tools"][0]["input_schema"] == SCHEMA
    assert sent["tool_choice"] == {"type": "tool", "name": AnthropicClient.TOOL_NAME}
    assert sent["system"] == "S"
    assert sent["messages"] == [{"role": "user", "content": "U"}]
    assert (usage.input_tokens, usage.output_tokens) == (21, 9)


def test_anthropic_sends_no_tool_on_a_plain_completion() -> None:
    """A scored invariant is that the extractor is given no tools. A client that
    attached one to every call would break it from underneath."""
    client, sdk = anthropic(blocks=[SimpleNamespace(type="text", text="hello")])

    text, _ = client.complete(system="S", user="U", max_tokens=50)

    assert text == "hello"
    assert "tools" not in sdk.seen[0]
    assert "tool_choice" not in sdk.seen[0]


def test_anthropic_skips_thinking_blocks_to_find_the_tool_use() -> None:
    """Extended thinking puts a `thinking` block first. Reading `content[0]`
    blindly would return nothing and look like a schema violation."""
    client, _ = anthropic(
        blocks=[
            SimpleNamespace(type="thinking", thinking="considering"),
            SimpleNamespace(type="tool_use", input={"capital": "Paris"}),
        ]
    )

    payload, _ = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)

    assert payload == {"capital": "Paris"}


def test_anthropic_credit_exhaustion_is_permanent() -> None:
    client, sdk = anthropic(error=RuntimeError("Error code: 429 - insufficient_quota"))

    with pytest.raises(LLMQuotaExhausted):
        client.complete(system="S", user="U", max_tokens=50)

    assert len(sdk.seen) == 1, "not retried"


# ── OpenAI ────────────────────────────────────────────────────────────────


class FakeOpenAISDK:
    def __init__(self, *, content: str | None = ANSWER, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.seen: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.seen.append(kwargs)
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))],
            usage=SimpleNamespace(prompt_tokens=33, completion_tokens=4),
        )


def openai(**kwargs: Any) -> tuple[OpenAIClient, FakeOpenAISDK]:
    client = OpenAIClient(api_key="k", model="gpt-4o", sleep=no_sleep)
    sdk = FakeOpenAISDK(**kwargs)
    client._sdk = sdk
    return client, sdk


def test_openai_sends_a_strict_json_schema() -> None:
    """`strict: false` is the difference between a schema that constrains and
    one that is a suggestion."""
    client, sdk = openai()

    payload, usage = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)

    assert payload == {"capital": "Paris"}
    fmt = sdk.seen[0]["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["schema"] == SCHEMA
    assert fmt["json_schema"]["strict"] is True
    assert (usage.input_tokens, usage.output_tokens) == (33, 4)


def test_openai_puts_the_system_prompt_in_its_own_message() -> None:
    client, sdk = openai()

    client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)

    assert sdk.seen[0]["messages"] == [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
    ]


def test_openai_uses_max_completion_tokens() -> None:
    """The old `max_tokens` argument is rejected by the newer models. A wrong
    name here fails every call, on every model, at the first request."""
    client, sdk = openai()

    client.complete(system="S", user="U", max_tokens=50)

    assert sdk.seen[0]["max_completion_tokens"] == 50
    assert "max_tokens" not in sdk.seen[0]
    assert "response_format" not in sdk.seen[0], "no schema on a plain completion"


def test_a_refusal_does_not_escape_as_an_unmapped_typeerror() -> None:
    """OpenAI sets `message.content` to None when the model refuses.

    Passing that straight to `json.loads` raises `TypeError` — a bare Python
    error escaping the retry loop, which is precisely what the portable zone's
    error types exist to prevent. It is normalised to an empty string, so
    `complete` returns empty and `structured` reports a schema failure with a
    readable reason.
    """
    client, _ = openai(content=None)
    text, _ = client.complete(system="S", user="U", max_tokens=50)
    assert text == ""

    client, _ = openai(content=None)
    with pytest.raises(LLMSchemaViolation, match="not JSON"):
        client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)


# ── shared across all three ───────────────────────────────────────────────


@pytest.mark.parametrize("build", [gemini, anthropic, openai])
def test_every_provider_normalises_its_tokens_into_one_usage(build: Any) -> None:
    """`telemetry` and the cost widget never branch on vendor, which is only
    true if all three fill the same fields."""
    client, _ = build()

    _, usage = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=200)

    assert usage.model == client.model
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    assert usage.attempts == 1


@pytest.mark.parametrize("build", [gemini, anthropic, openai])
def test_no_provider_sends_a_schema_we_wrote_wrong(build: Any) -> None:
    """The stated policy — an invalid schema is our error and is not retried —
    is only true if the request never leaves. Checked per provider, because the
    preflight lives in shared code that each one inherits."""
    client, sdk = build()

    with pytest.raises(Exception, match="schema itself is invalid"):
        client.structured(system="S", user="U", schema={"type": "not-a-type"}, max_tokens=200)

    assert sdk.seen == [], "nothing was sent, so nothing was billed"


# ── OpenAI strict mode ────────────────────────────────────────────────────

OPTIONAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["field_key"],
                "properties": {
                    "field_key": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}


def test_openai_makes_optional_properties_nullable_rather_than_required() -> None:
    """OpenAI's `strict: true` demands every property appear in `required`.

    Our schemas do not, so sending one unchanged is a 400 before a token is
    generated — `Missing 'confidence'` — and the provider looks supported right
    up to the first real call. Widening the type is the documented fix; making
    the field genuinely required would instead force a model to invent a
    confidence it does not have.
    """
    client, sdk = openai(content='{"facts": []}')

    client.structured(system="S", user="U", schema=OPTIONAL_SCHEMA, max_tokens=200)

    sent = sdk.seen[0]["response_format"]["json_schema"]["schema"]
    item = sent["properties"]["facts"]["items"]
    assert set(item["required"]) == {"field_key", "confidence"}
    assert item["properties"]["confidence"]["type"] == ["number", "null"]
    assert item["properties"]["field_key"]["type"] == "string", "required fields are untouched"


def test_the_caller_s_schema_is_not_mutated() -> None:
    """The taxonomy hands the same dict to every provider. Rewriting it in place
    would leak the OpenAI dialect into a Gemini request."""
    client, _ = openai(content='{"facts": []}')

    client.structured(system="S", user="U", schema=OPTIONAL_SCHEMA, max_tokens=200)

    assert OPTIONAL_SCHEMA["properties"]["facts"]["items"]["required"] == ["field_key"]


def test_a_null_for_an_optional_field_comes_back_as_an_absent_key() -> None:
    """The return leg of the same transformation.

    Without it the two halves disagree: we tell OpenAI null is allowed, the
    model sends null, and the shared schema gate — which checks the ORIGINAL
    schema, as it should — rejects a well-formed reply. Seen live as
    `at evidence_quote: None is not of type 'string'`.
    """
    client, _ = openai(content='{"facts": [{"field_key": "expiry_date", "confidence": null}]}')

    payload, _ = client.structured(system="S", user="U", schema=OPTIONAL_SCHEMA, max_tokens=200)

    assert payload == {"facts": [{"field_key": "expiry_date"}]}


def test_a_null_for_a_REQUIRED_field_is_left_alone_to_fail_the_gate() -> None:
    """Pruning it would turn a model that answered badly into one that answered
    nothing, and the schema gate is what is supposed to notice."""
    client, _ = openai(content='{"facts": [{"field_key": null}]}')

    with pytest.raises(Exception, match="field_key"):
        client.structured(system="S", user="U", schema=OPTIONAL_SCHEMA, max_tokens=200)
