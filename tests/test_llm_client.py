"""LLM adapter behaviour — Phase 2A.2. Fully offline.

The vendor SDKs are stubbed rather than called. That is deliberate: what we own
is the retry policy, the token accounting, the schema gate and the error
mapping, and all four are provider-independent. A recorded LLM response would
pin the SDK's own response shape, which we are not the ones getting wrong.

Real-provider checks live in `test_llm_live.py` behind the `live` marker.
"""

from __future__ import annotations

from typing import Any

import pytest

from documents_analyst_core.portable.llm_client import (
    MAX_BACKOFF_S,
    PROVIDERS,
    REPAIR_PREFIX,
    AnthropicClient,
    FakeLLM,
    GeminiClient,
    LLMError,
    LLMQuotaExhausted,
    LLMSchemaViolation,
    LLMTransportError,
    OpenAIClient,
    Usage,
    build_client,
    retry_delay,
    schema_error,
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "field_key": {"type": "string", "enum": ["payment_terms", "governing_law"]},
        "value": {"type": "string"},
    },
    "required": ["field_key", "value"],
    "additionalProperties": False,
}

GOOD = {"field_key": "payment_terms", "value": "Net 30"}
OUTSIDE_TAXONOMY = {"field_key": "late_fee", "value": "2%"}


class StubClient(GeminiClient):
    """A provider client whose only fake part is the network call.

    Everything above `_call` — retries, accounting, the schema gate — is the
    real code under test.
    """

    def __init__(self, replies: list[Any], *, max_retries: int = 3) -> None:
        super().__init__(
            api_key="k",
            model="stub-1",
            max_retries=max_retries,
            # Injected, so backoff is asserted rather than waited for.
            sleep=self._record_sleep,
        )
        self.replies = list(replies)
        self.seen: list[dict[str, Any]] = []
        self.slept: list[float] = []

    def _record_sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def _client(self) -> Any:
        return object()

    def _call(
        self, *, system: str, user: str, max_tokens: int, schema: dict[str, Any] | None
    ) -> tuple[str, int, int]:
        self.seen.append({"system": system, "user": user, "schema": schema})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, 10, 5


# ── the schema gate ───────────────────────────────────────────────────────


def test_a_field_outside_the_taxonomy_is_caught() -> None:
    """The reason we validate ourselves instead of trusting three different
    provider 'strict' modes."""
    assert schema_error(GOOD, SCHEMA) is None

    problem = schema_error(OUTSIDE_TAXONOMY, SCHEMA)
    assert problem is not None
    assert "late_fee" in problem


def test_the_error_names_the_location_so_a_repair_can_act_on_it() -> None:
    problem = schema_error({"field_key": "payment_terms"}, SCHEMA)
    assert problem and "value" in problem


def test_a_schema_we_wrote_wrong_is_our_bug_not_the_models() -> None:
    """Retrying cannot fix an invalid schema, so it raises instead of burning
    three attempts and reporting a model failure."""
    with pytest.raises(LLMError, match="schema itself is invalid"):
        schema_error({}, {"type": "not-a-type"})


# ── retries ───────────────────────────────────────────────────────────────


def test_a_bad_reply_is_retried_with_the_reason_attached() -> None:
    client = StubClient(
        [
            '{"field_key": "late_fee", "value": "2%"}',
            '{"field_key": "payment_terms", "value": "Net 30"}',
        ]
    )

    payload, usage = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=100)

    assert payload == GOOD
    assert usage.attempts == 2
    assert REPAIR_PREFIX in client.seen[1]["system"]
    assert "late_fee" in client.seen[1]["system"], "the repair must say what was wrong"


def test_the_repair_goes_in_the_system_prompt_never_the_user_turn() -> None:
    """A scored invariant: instructions are composed by us, not concatenated
    from document text. A retry must not quietly change what 'the document
    said' for the next attempt."""
    client = StubClient(["not json at all", '{"field_key": "governing_law", "value": "NY"}'])

    client.structured(system="S", user="THE DOCUMENT", schema=SCHEMA, max_tokens=100)

    assert client.seen[0]["user"] == client.seen[1]["user"] == "THE DOCUMENT"
    assert REPAIR_PREFIX not in client.seen[1]["user"]


def test_tokens_from_failed_attempts_are_still_billed() -> None:
    """A retry that succeeded on attempt three still cost tokens twice over.
    A cost report that hides that is lying."""
    client = StubClient(["{}", "{}", '{"field_key": "payment_terms", "value": "Net 30"}'])

    _, usage = client.structured(system="S", user="U", schema=SCHEMA, max_tokens=100)

    assert usage.attempts == 3
    assert usage.input_tokens == 30, "10 per attempt, all three counted"
    assert usage.output_tokens == 15


def test_retries_are_bounded_and_the_last_reason_survives() -> None:
    client = StubClient(["{}"] * 5, max_retries=3)

    with pytest.raises(LLMSchemaViolation) as caught:
        client.structured(system="S", user="U", schema=SCHEMA, max_tokens=100)

    assert "3 attempt(s)" in str(caught.value)
    assert "field_key" in str(caught.value), "says what was still wrong"
    assert len(client.seen) == 3


def test_a_transport_failure_is_retried_then_reported_as_transport() -> None:
    """Mapped to our own error type so callers never catch a vendor exception —
    the whole point of the portable zone."""
    client = StubClient([RuntimeError("connection reset")] * 3, max_retries=3)

    with pytest.raises(LLMTransportError, match="connection reset"):
        client.complete(system="S", user="U", max_tokens=100)


def test_a_transport_blip_recovers_without_failing_the_call() -> None:
    client = StubClient([RuntimeError("blip"), "the answer"])

    text, usage = client.complete(system="S", user="U", max_tokens=100)

    assert text == "the answer"
    assert usage.attempts == 2


def test_a_schema_we_wrote_wrong_is_never_sent() -> None:
    """Checked before the loop, not after a response.

    Providers disagree about malformed schemas — some reject the request, some
    ignore the bad keyword and answer anyway. Learning about it from a response
    means the broken schema was already sent, mapped to a transport failure,
    retried, and billed. `check_schema` makes the stated policy true.
    """
    client = StubClient(["{}"] * 3)

    with pytest.raises(LLMError, match="schema itself is invalid"):
        client.structured(system="S", user="U", schema={"type": "not-a-type"}, max_tokens=100)

    assert client.seen == [], "nothing left the process"
    assert client.slept == []


# ── the two bugs the live run found ───────────────────────────────────────


def test_a_provider_that_never_answered_is_not_a_schema_violation() -> None:
    """Found by the live test against Gemini's 5-requests-per-minute free tier.

    All three attempts were rate-limited, and `structured()` reported
    `LLMSchemaViolation` — sending whoever reads the log to debug a taxonomy
    when nothing had ever been judged. The two failures are now distinct.
    """
    client = StubClient([RuntimeError("429 RESOURCE_EXHAUSTED")] * 3, max_retries=3)

    with pytest.raises(LLMTransportError) as caught:
        client.structured(system="S", user="U", schema=SCHEMA, max_tokens=100)

    assert not isinstance(caught.value, LLMSchemaViolation)
    assert "no reply" in str(caught.value)


def test_a_stated_retry_delay_is_honoured() -> None:
    """Gemini answers a rate limit with `retryDelay: '13s'`. Retrying instantly
    three times guarantees three failures inside four seconds — which is exactly
    what happened on the first live run."""
    assert retry_delay("429 RESOURCE_EXHAUSTED ... 'retryDelay': '13s'", 0, 1.0) == 13.0
    assert retry_delay("Please retry in 7.5s", 0, 1.0) == 7.5


def test_backoff_is_exponential_when_nothing_was_stated() -> None:
    assert [retry_delay("boom", n, 1.0) for n in range(3)] == [1.0, 2.0, 4.0]


def test_a_delay_is_capped_so_one_bad_value_cannot_park_a_run() -> None:
    assert retry_delay("retryDelay: '99999s'", 0, 1.0) == MAX_BACKOFF_S
    assert retry_delay("boom", 40, 1.0) == MAX_BACKOFF_S


def test_a_rate_limit_waits_between_attempts() -> None:
    client = StubClient([RuntimeError("Please retry in 2s"), "the answer"])

    client.complete(system="S", user="U", max_tokens=100)

    assert client.slept == [2.0]


def test_no_sleep_after_the_final_attempt() -> None:
    """Waiting after the last failure buys nothing — and because a stated delay
    is honoured, a final `retryDelay: 60s` would hold the caller for a full
    minute purely to hand back an error it already had."""
    client = StubClient([RuntimeError("Please retry in 60s")] * 3, max_retries=3)

    with pytest.raises(LLMTransportError):
        client.complete(system="S", user="U", max_tokens=100)

    assert client.slept == [60.0, 60.0], "two gaps between three attempts, none after"


def test_structured_also_stops_sleeping_after_the_final_attempt() -> None:
    """Same loop, written twice — so it is asserted twice."""
    client = StubClient([RuntimeError("Please retry in 5s")] * 3, max_retries=3)

    with pytest.raises(LLMTransportError):
        client.structured(system="S", user="U", schema=SCHEMA, max_tokens=100)

    assert client.slept == [5.0, 5.0]


def test_a_single_attempt_configuration_never_sleeps() -> None:
    client = StubClient([RuntimeError("Please retry in 30s")], max_retries=1)

    with pytest.raises(LLMTransportError):
        client.complete(system="S", user="U", max_tokens=100)

    assert client.slept == []


def test_running_out_of_credit_is_not_retried() -> None:
    """OpenAI's `credit_balance_exhausted` shares a 429 with a rate limit, but
    waiting cannot fix it. Three attempts only delay the honest answer."""
    client = StubClient([RuntimeError("Error code: 429 - insufficient_quota")] * 3)

    with pytest.raises(LLMQuotaExhausted):
        client.complete(system="S", user="U", max_tokens=100)

    assert len(client.seen) == 1, "one attempt, not three"
    assert client.slept == []


def test_quota_exhaustion_is_still_catchable_as_a_transport_failure() -> None:
    """A caller that only cares 'the provider did not answer' keeps working."""
    assert issubclass(LLMQuotaExhausted, LLMTransportError)


# ── construction ──────────────────────────────────────────────────────────


def test_every_configured_provider_has_a_client() -> None:
    """Pins the mapping to `settings.llm_provider`. Adding a provider to the
    Literal without a client here would fail at the first call, not at start."""
    assert set(PROVIDERS) == {"gemini", "anthropic", "openai"}


@pytest.mark.parametrize(
    ("provider", "expected"),
    [("gemini", GeminiClient), ("anthropic", AnthropicClient), ("openai", OpenAIClient)],
)
def test_build_client_selects_by_name(provider: str, expected: type) -> None:
    client = build_client(provider=provider, api_key="k", model="m")
    assert isinstance(client, expected)


def test_an_unknown_provider_says_which_names_exist() -> None:
    with pytest.raises(LLMError, match="anthropic"):
        build_client(provider="gemeni", api_key="k", model="m")


def test_a_missing_key_fails_at_construction_not_at_the_first_call() -> None:
    """Minutes into a run is the wrong time to learn the key was blank."""
    with pytest.raises(LLMError, match="API key is required"):
        build_client(provider="gemini", api_key="", model="m")


def test_importing_costs_no_vendor_sdk() -> None:
    """Constructing a client must not import the SDK either — an extension that
    copies this file and uses one provider cannot be made to install all three.
    """
    client = build_client(provider="anthropic", api_key="k", model="m")
    assert client._sdk is None


# ── the test double ───────────────────────────────────────────────────────


def test_fake_records_what_it_was_asked() -> None:
    fake = FakeLLM(responses=[GOOD])

    payload, usage = fake.structured(system="S", user="U", schema=SCHEMA, max_tokens=64)

    assert payload == GOOD
    assert isinstance(usage, Usage)
    assert fake.calls[0].schema == SCHEMA
    assert fake.calls[0].user == "U"
    assert fake.calls[0].max_tokens == 64


def test_fake_refuses_a_canned_response_its_own_schema_rejects() -> None:
    """Otherwise a test passes against a shape production would refuse — the
    fake becomes a way to prove things that are not true."""
    fake = FakeLLM(responses=[OUTSIDE_TAXONOMY])

    with pytest.raises(LLMSchemaViolation, match="does not fit"):
        fake.structured(system="S", user="U", schema=SCHEMA, max_tokens=64)


def test_fake_can_raise_to_exercise_a_caller_s_degraded_path() -> None:
    fake = FakeLLM(responses=[LLMTransportError("provider down")])

    with pytest.raises(LLMTransportError):
        fake.structured(system="S", user="U", schema=SCHEMA, max_tokens=64)


def test_fake_running_out_of_responses_is_loud() -> None:
    """Silence would look like the caller made fewer calls than it did."""
    fake = FakeLLM(responses=[])

    with pytest.raises(LLMError, match="no responses left"):
        fake.structured(system="S", user="U", schema=SCHEMA, max_tokens=64)
