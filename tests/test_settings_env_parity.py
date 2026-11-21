"""Keep the small secret/model `.env` aligned with the global runtime config.

Non-secret defaults belong in `config/runtime.yaml`; credentials, connection
tokens and model selection belong in `.env`. These tests make both sides
explicit, so a new setting cannot silently end up hardcoded in a module or in
the wrong configuration surface.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from documents_analyst.settings import (
    COMPOSE_ONLY_FIELDS,
    ENV_EXAMPLE_FIELDS,
    ENV_FILE_FIELDS,
    ENV_ONLY_FIELDS,
    RUNTIME_CONFIG,
    Settings,
)

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
DOTENV = ROOT / ".env"
DOCKERFILE = ROOT / "Dockerfile"

ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=")


def documented() -> set[str]:
    return {
        m.group(1) for line in ENV_EXAMPLE.read_text().splitlines() if (m := ASSIGNMENT.match(line))
    }


def declared() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def runtime_configured() -> set[str]:
    loaded = yaml.safe_load(RUNTIME_CONFIG.read_text())
    assert isinstance(loaded, dict)
    return {str(name).upper() for name in loaded}


def test_the_example_is_limited_to_approved_environment_fields() -> None:
    assert documented() == {name.upper() for name in ENV_EXAMPLE_FIELDS} | COMPOSE_ONLY_FIELDS


def test_every_non_secret_default_lives_in_runtime_config() -> None:
    expected = declared() - {name.upper() for name in ENV_ONLY_FIELDS}
    assert runtime_configured() == expected


def test_a_local_dotenv_contains_only_allowed_overrides() -> None:
    """The checked-out `.env` must not quietly revive old tuning knobs."""
    if not DOTENV.exists():
        return
    found = {
        m.group(1)
        for line in DOTENV.read_text().splitlines()
        if (m := ASSIGNMENT.match(line))
    } - COMPOSE_ONLY_FIELDS
    assert {name.lower() for name in found} <= ENV_FILE_FIELDS


def test_compose_only_fields_are_not_settings() -> None:
    """`WATCH_DIR` belongs to the mount, not the app: a Settings field of the same name
    would make `.env` configure two different things with one line."""
    assert not {name.lower() for name in COMPOSE_ONLY_FIELDS} & set(Settings.model_fields)


def test_provider_keys_are_present_and_blank() -> None:
    """Provider values ship empty, so copying the file cannot look configured."""
    text = ENV_EXAMPLE.read_text()
    for key in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert f"\n{key}=\n" in text, f"{key} should be present and empty"


def test_example_selects_gemini_as_the_primary_provider() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "\nLLM_PROVIDER=gemini\n" in text
    assert "\nGEMINI_API_KEY=\n" in text


def test_global_config_is_the_baseline_and_env_wins_for_a_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal local defaults come from YAML, Docker/host env still wins.

    **The environment has to be cleared to see the baseline**, and this test did
    not do it — so it asserted the YAML value while running inside a container
    that sets `WATCH_ROOT=/data/watched`, which is the override half of its own
    name working correctly. It passed on a host and failed under `make test`: a
    test that only holds in one of the two documented ways to run the suite.
    """
    for name in ("WATCH_ROOT", "STATIC_DIR", "MODEL_CACHE_DIR", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)

    base = Settings(_env_file=None)
    assert base.watch_root == Path("watched")
    assert base.model_cache_dir == ".cache/models"

    # Both routes a deployment has: the process environment, as compose uses…
    monkeypatch.setenv("WATCH_ROOT", "/data/watched")
    assert Settings(_env_file=None).watch_root == Path("/data/watched")

    # …and an explicit value, as a test or an embedding application uses.
    assert Settings(_env_file=None, watch_root="/tmp/watched").watch_root == Path("/tmp/watched")


def test_the_runtime_image_copies_the_global_config() -> None:
    """A host-only config source would make the first container boot fail."""
    assert "COPY config/ config/" in DOCKERFILE.read_text()


def test_llm_provider_rejects_values_outside_the_supported_three() -> None:
    """A bare `str` would let a typo through and fail only at first LLM call."""
    import pytest
    from pydantic import ValidationError

    from documents_analyst.settings import Settings

    for good in ("gemini", "anthropic", "openai"):
        assert Settings(_env_file=None, llm_provider=good).llm_provider == good

    with pytest.raises(ValidationError, match="gemini"):
        Settings(_env_file=None, llm_provider="geminix")


def test_gemini_is_the_default_with_model_defaults() -> None:
    """The primary provider must be usable with its own key and no model names.

    **It is no longer usable with that key alone**, and the assertion says so
    rather than being relaxed: embeddings are OpenAI now whichever provider
    extracts, so a Gemini-only configuration starts, extracts, adjudicates —
    and cannot match one document's open subjects against another's. That is a
    real hole, so it belongs on the degraded banner.
    """
    from documents_analyst.settings import Settings

    gemini = Settings(_env_file=None, gemini_api_key="y")
    assert gemini.llm_provider == "gemini"
    assert gemini.resolved_model_extract == "gemini-3.6-flash"
    assert gemini.resolved_model_judge == "gemini-3.6-flash"
    assert gemini.missing_requirements() == [
        "OPENAI_API_KEY — embeddings (open-subject matching across documents) disabled"
    ]

    assert not gemini.model_copy(update={"openai_api_key": "z"}).is_degraded


def test_openai_and_anthropic_remain_explicit_supported_fallbacks() -> None:
    from documents_analyst.settings import Settings

    openai = Settings(
        _env_file=None,
        llm_provider="openai",
        openai_api_key="y",
    )
    assert openai.resolved_model_extract == "gpt-4o"
    assert openai.resolved_model_judge == "gpt-4o"
    assert not openai.is_degraded, "one key covers extraction and embeddings"

    anthropic = Settings(
        _env_file=None,
        llm_provider="anthropic",
        anthropic_api_key="y",
        openai_api_key="z",  # embeddings, as for gemini above
    )
    assert anthropic.resolved_model_extract and anthropic.resolved_model_judge
    assert not anthropic.is_degraded


def test_no_real_key_leaked_into_the_example() -> None:
    text = ENV_EXAMPLE.read_text()
    assert "sk_" not in text.replace("sk_ key", "").replace("sk_YOUR", ""), (
        ".env.example appears to contain a real key"
    )


# ── the offline guarantee ─────────────────────────────────────────────────


def test_the_suite_holds_no_provider_key() -> None:
    """`docs/TESTING.md` §1: the whole suite runs with no keys and no network.

    `Settings` reads `.env`, so a developer with a working key would get a live
    LLM client inside a unit test and quietly spend real money. `conftest.py`
    blanks the credentials; this asserts that it worked, because a fixture nobody
    checks is a fixture that can be deleted by a merge.
    """
    from documents_analyst.settings import get_settings

    settings = get_settings()

    assert settings.llm_api_key == "", "a test run must not hold an LLM key"
    assert settings.openai_api_key == "", "nor an embeddings key"


def test_the_final_document_needs_no_key_and_no_network() -> None:
    """Writing, repairing and verifying the final document are local file work. No
    module on that path may import an HTTP client: the document is the one output
    that must exist even when every provider is down."""
    import inspect

    from documents_analyst import deliverable
    from documents_analyst.graph.nodes import commit, reconcile, resolve

    for module in (deliverable, commit, resolve, reconcile):
        source = inspect.getsource(module)
        for client in ("httpx", "requests", "urllib.request"):
            assert f"import {client}" not in source, f"{module.__name__} imports {client}"
