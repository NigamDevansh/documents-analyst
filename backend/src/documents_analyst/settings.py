"""The single boundary for runtime configuration.

It reads the tracked non-secret defaults in `config/runtime.yaml`, then applies real
environment and `.env` overrides. Everything else takes injected values — which is what
lets `packages/core` be tested with no `.env` present, and keeps `portable/` copyable into
an app whose config system we don't control.

Startup validates and reports EVERYTHING missing at once, not the first problem and then
the next after a restart. See `missing_requirements()`.

See docs/BACKEND_INTERNALS.md §6 and docs/BUILD_AND_RUNTIME.md §5.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, PrivateAttr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

ROOT = Path(__file__).resolve().parents[3]
RUNTIME_CONFIG = ROOT / "config" / "runtime.yaml"
"""Tracked home for non-secret runtime defaults."""

COMPOSE_ONLY_FIELDS = frozenset({"WATCH_DIR"})
"""Read by Docker Compose from `.env`, never by `Settings` (whose `extra="ignore"` skips
them). `WATCH_DIR` is the host folder mounted at `/data/watched` — the one place a person
chooses where their pile folders live."""

DEFAULT_DELIVERABLE_FILENAME = "final-document.docx"
"""Also the scanner's fallback skip-name, so a caller that forgets to pass the configured
name still never ingests the default output."""

# `.env` is intentionally small. Credentials and connection tokens must not be
# committed to `config/runtime.yaml`; model identifiers stay here because they
# are a deployment choice rather than an application-behaviour choice.
ENV_FILE_FIELDS = frozenset(
    {
        "gemini_api_key",
        "anthropic_api_key",
        "openai_api_key",
        "database_url",
        "api_token",
        "mcp_token",
        "llm_provider",
        "embedding_provider",
        "llm_model_extract",
        "llm_model_judge",
        "embedding_model",
        "nli_model",
        "gliner_model",
    }
)

ENV_EXAMPLE_FIELDS = ENV_FILE_FIELDS - {"database_url"}
"""The intentionally short assignment list in `.env.example`. `DATABASE_URL` is allowed in
`.env` and must stay an environment override for production credentials, but it's commented
out in the example so copying it doesn't replace the safe tracked local-dev default."""

ENV_ONLY_FIELDS = frozenset(
    {
        "gemini_api_key",
        "anthropic_api_key",
        "openai_api_key",
        "api_token",
        "mcp_token",
        "llm_model_extract",
        "llm_model_judge",
    }
)
"""Secrets and intentionally blank per-deployment model-role overrides."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Use YAML for defaults; init, real env and `.env` override it. Keeping the standard
        env source ahead of YAML preserves the non-secret vars Docker sets, without turning
        `.env.example` into a catalogue of operational knobs."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls, yaml_file=RUNTIME_CONFIG),
            file_secret_settings,
        )

    # ── required for full operation (absence degrades, never crashes) ─────
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    llm_provider: Literal["", "gemini", "anthropic", "openai"]
    """Blank picks the provider from the keys present (see `_pick_providers`): a Claude
    key means Claude, else a Gemini key means Gemini, else OpenAI. Set it to choose."""
    embedding_provider: Literal["", "openai", "gemini"] = ""
    """Where fact-key embeddings come from. Claude has no embedding model, so this is
    always OpenAI or Gemini. Blank picks OpenAI if its key is set, else Gemini."""
    _provider_auto: bool = PrivateAttr(default=False)
    _embedding_auto: bool = PrivateAttr(default=False)
    """Closed on purpose. A bare `str` would let a typo through and fail later with a
    confusing missing-key message; this is rejected at startup, naming the bad value."""

    # ── the final document ────────────────────────────────────────────────
    deliverable_filename: str = Field(
        default=DEFAULT_DELIVERABLE_FILENAME, pattern=r"^[^/\\.][^/\\]*\.docx$"
    )
    """What the generated document is called. It is written into the pile's own folder
    (`watch_root/<pile>/<deliverable_filename>`), so the scanner skips exactly this name at
    the pile root — otherwise the system would ingest its own output as a new source. A
    plain file name, `.docx`, no directories and no leading dot (a dotfile is ignored by
    the scanner, which is what the temp file relies on)."""

    # ── LLM ───────────────────────────────────────────────────────────────
    llm_model_extract: str = ""
    """Blank means the provider default (see `resolved_model_extract`) — so a newer model can
    be set without a code change, and it still boots with none."""
    llm_model_judge: str = ""
    llm_default_models: dict[str, tuple[str, str]]
    llm_max_retries: int

    # ── database ──────────────────────────────────────────────────────────
    # `localhost`, not `db`: compose passes DATABASE_URL explicitly, so this default is only
    # reached by a host process, where `db` doesn't resolve.
    database_url: str

    # ── scanner and lease (see WORKER_RUNTIME.md) ─────────────────────────
    watch_root: Path
    scan_interval_s: int
    scan_deep_every: int
    """Every Nth tick re-hashes everything instead of trusting size+mtime — catches an
    overwrite that preserved its mtime."""
    settle_seconds: int
    """A file must be unchanged this long before we read it: copying a big PDF isn't atomic,
    and hashing it mid-write records a document that never existed."""
    lease_ttl_s: int = Field(gt=0, le=270)
    """How long a pile lease is valid — the budget for reading one document. Renewed before
    each hash+parse; raise it (up to 270s) if a pile holds documents slower than this."""
    job_max_attempts: int
    """Attempts before a document is quarantined, so one poison file can't stall the pile."""
    worker_id: str
    """Blank derives from hostname+pid. Set it when running several workers on one host so
    lease ownership is readable."""
    pile_concurrency: int = Field(ge=1, le=16)
    """How many piles one worker runs at the same time, one thread per pile folder. Files
    inside a pile are still read one at a time, in order; this only lets folder 2 start
    while folder 1 is still being extracted. 1 restores strictly one pile after another."""
    watch_dir_host: str
    """Where the watched folder is on the machine running Docker — display only. Inside a
    container `watch_root` is `/data/watched`, which tells a person nothing about where to
    put their folders, so compose passes the host side of the mount (`WATCH_DIR`) here and
    the dashboard's setup guide shows it. Blank means "show `watch_root`"."""

    # ── models ────────────────────────────────────────────────────────────
    embedding_model: str
    """Blank means the embedding provider's default (`embedding_default_models`). Asked for
    at 384 dims so the `vector(384)` column fits; a different native width would need a
    migration."""
    embedding_default_models: dict[str, str]
    nli_model: str
    gliner_model: str
    gliner_threshold: float
    nli_enabled: bool
    gliner_enabled: bool
    """`false` skips GLiNER: party candidates come back empty and PII tier 2 is off."""
    subject_similarity: float = Field(gt=0, le=1)
    """Cosine score needed to group two free-text subjects into one slot."""
    nli_contradiction_threshold: float = Field(gt=0, le=1)
    """NLI score below which two value groups are treated as compatible."""
    model_cache_dir: str
    """Blank uses the library defaults (the baked ones in the image). Set to `.cache/models`
    for a host run so a worker reads the weights `prefetch_models.py --dir` wrote instead of
    downloading a second copy."""

    # ── budgets ───────────────────────────────────────────────────────────
    max_llm_cost_usd_per_run: float
    max_documents_per_run: int
    small_sample: bool
    """Dev guard: extract only the first N sections of each document."""
    small_sample_sections: int
    model_prices_path: Path
    """USD per million tokens, per model. Ships empty: a model with no entry is reported
    unpriced, not free — a stale guess makes a cost report confidently wrong."""

    # ── serving ───────────────────────────────────────────────────────────
    static_dir: Path
    log_level: str
    """Passed to `logging.basicConfig`. No `log_format` beside it: logging here is plain lines
    to stdout, and a knob that configures nothing misleads more than it helps."""

    api_host: str  # noqa: S104 — inside a container, by design
    api_port: int

    api_token: str = ""
    """Blank leaves the API open, the default so a reviewer needs no credential. Set it and
    every route but `/` and `/health` requires `Authorization: Bearer …`, checked with
    `compare_digest` so timing can't leak the token."""

    sse_keepalive_s: float
    """How long a quiet event stream waits before a comment frame. Without it a proxy closes
    the idle connection, forcing a reconnect+replay on a pile that's only thinking."""

    sse_replay_limit: int
    """Rows one replay page hands back. A browser rejoining a whole run pages through rather
    than building one enormous first frame."""

    # ── MCP ───────────────────────────────────────────────────────────────
    mcp_api_base_url: str
    """Where the MCP server reaches our API. `localhost` for `claude mcp add` on the host;
    compose passes `http://api:8000`."""

    mcp_actor: str
    """What a machine writes into `pending_decisions.decided_by`. Prefixed `mcp:` if missing,
    so the audit trail always tells a machine approval from a human one. Set it per deployment
    (`mcp:claude-desktop`, `mcp:ci`)."""

    mcp_transport: Literal["stdio", "streamable-http"]
    """`stdio` for `claude mcp add`, the demo; `streamable-http` for the compose service."""

    mcp_host: str
    """Loopback by default. The MCP server spends `API_TOKEN` on every downstream call, so an
    open port here lets anyone approve changes using our credential — bind publicly only behind
    `MCP_TOKEN` (in a container, publish to `127.0.0.1`)."""

    mcp_port: int

    mcp_token: str = ""
    """Inbound credential for the HTTP transport; blank means none. Separate from `API_TOKEN`
    — one is presented downstream, the other demanded of a caller, and sharing one string would
    hand every client the API's credential. N/A to stdio."""

    public_api_base_url: str
    """The API's address as an outside caller sees it. Blank falls back to `MCP_API_BASE_URL` —
    wrong in compose, where the MCP server dials `http://api:8000`, a name only Docker resolves,
    so a host-side agent would get an unreachable download URL."""
    mcp_timeout_s: float

    mcp_confirm_min_chars: int
    """How much of an item's evidence an agent must quote back to approve it — enough that it
    can't be guessed from the id. Shorter evidence must be quoted in full."""

    # No CORS setting, deliberately: FastAPI serves the built frontend from the
    # same origin, so there is no cross-origin request to allow.

    # ── derived ───────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def _pick_providers(self) -> Settings:
        """Resolve blank providers from the keys present, once, at construction — so every
        reader of `llm_provider` sees a concrete provider and nobody re-derives it.

        Claude before Gemini before OpenAI: a Claude user is told to add an OpenAI or Gemini
        key for embeddings, and that extra key must not take extraction away from Claude.
        With no key at all it resolves to OpenAI — the one key that covers everything —
        and the setup screen lists every option."""
        if not self.llm_provider:
            picked = (
                "anthropic"
                if self.anthropic_api_key
                else "gemini"
                if self.gemini_api_key
                else "openai"
            )
            object.__setattr__(self, "llm_provider", picked)
            self._provider_auto = True
        if not self.embedding_provider:
            embed = "openai" if self.openai_api_key or not self.gemini_api_key else "gemini"
            object.__setattr__(self, "embedding_provider", embed)
            self._embedding_auto = True
        return self

    @property
    def embedding_api_key(self) -> str:
        return self.openai_api_key if self.embedding_provider == "openai" else self.gemini_api_key

    @property
    def resolved_embedding_model(self) -> str:
        if self.embedding_model:
            return self.embedding_model
        return self.embedding_default_models.get(self.embedding_provider, "")

    @property
    def llm_api_key(self) -> str:
        return {
            "gemini": self.gemini_api_key,
            "anthropic": self.anthropic_api_key,
            "openai": self.openai_api_key,
        }[self.llm_provider]

    @property
    def public_api_url(self) -> str:
        """What to put in a URL we hand to somebody else."""
        return self.public_api_base_url or self.mcp_api_base_url

    @property
    def mcp_binds_publicly(self) -> bool:
        """Whether the HTTP transport could be reached off this machine. A container binding
        `0.0.0.0` isn't by itself exposed — Docker's port publishing decides that — so this
        only drives the startup `MCP_TOKEN` check, not a reachability claim."""
        return self.mcp_host not in ("127.0.0.1", "localhost", "::1")

    @property
    def resolved_model_extract(self) -> str:
        if self.llm_model_extract:
            return self.llm_model_extract
        return self.llm_default_models.get(self.llm_provider, ("", ""))[0]

    @property
    def resolved_model_judge(self) -> str:
        if self.llm_model_judge:
            return self.llm_model_judge
        return self.llm_default_models.get(self.llm_provider, ("", ""))[1]

    def missing_requirements(self) -> list[str]:
        """Every missing key at once, with what each one costs. A list, not a raise: `docker
        compose up` with an untouched `.env` must start and show a banner, not exit or fail at
        the first LLM call."""
        missing: list[str] = []
        if not self.llm_api_key:
            if self._provider_auto:
                missing.append(
                    "An LLM key — ANTHROPIC_API_KEY (Claude), OPENAI_API_KEY or GEMINI_API_KEY; "
                    "fact extraction and adjudication disabled"
                )
            else:
                var = {
                    "gemini": "GEMINI_API_KEY",
                    "anthropic": "ANTHROPIC_API_KEY",
                    "openai": "OPENAI_API_KEY",
                }[self.llm_provider]
                missing.append(f"{var} — fact extraction and adjudication disabled")
        if not self.resolved_model_extract:
            missing.append(
                f"LLM_MODEL_EXTRACT — no default for provider {self.llm_provider!r}; "
                "fact extraction disabled"
            )
        if not self.resolved_model_judge:
            missing.append(
                f"LLM_MODEL_JUDGE — no default for provider {self.llm_provider!r}; "
                "adjudication and judge rules disabled"
            )
        if not self.embedding_api_key:
            if self._embedding_auto:
                missing.append(
                    "OPENAI_API_KEY or GEMINI_API_KEY — embeddings (matching the same term "
                    "across documents) disabled; Claude has no embedding model"
                )
            else:
                var = "OPENAI_API_KEY" if self.embedding_provider == "openai" else "GEMINI_API_KEY"
                missing.append(f"{var} — embeddings (EMBEDDING_PROVIDER) disabled")
        return missing

    def key_checklist(self) -> dict[str, object]:
        """What this deployment needs, as requirements a person can satisfy — the setup
        screen's content. Names, purposes and set/missing; never a value, not even a
        prefix: `/health` stays open on a locked-down instance.

        Two requirements, each met by any one of its keys: an LLM key (Claude, OpenAI or
        Gemini — or exactly the one `LLM_PROVIDER` names) and an embedding key (OpenAI or
        Gemini; Claude has none). The list is the same on every machine; only the ticks
        and the provider that was picked differ."""
        llm_keys = {
            "anthropic": ("ANTHROPIC_API_KEY", "Claude", self.anthropic_api_key),
            "openai": ("OPENAI_API_KEY", "OpenAI", self.openai_api_key),
            "gemini": ("GEMINI_API_KEY", "Gemini", self.gemini_api_key),
        }
        llm_options = list(llm_keys) if self._provider_auto else [self.llm_provider]
        embed_keys = {
            "openai": ("OPENAI_API_KEY", "OpenAI", self.openai_api_key),
            "gemini": ("GEMINI_API_KEY", "Gemini", self.gemini_api_key),
        }
        embed_options = list(embed_keys) if self._embedding_auto else [self.embedding_provider]

        def option(table: dict[str, tuple[str, str, str]], provider: str) -> dict[str, object]:
            name, label, value = table[provider]
            return {"provider": provider, "label": label, "name": name, "set": bool(value)}

        requirements = [
            {
                "id": "llm",
                "title": "An LLM key",
                "purpose": "reads facts out of each document and judges conflicts",
                "any_of": [option(llm_keys, p) for p in llm_options],
                "met": bool(self.llm_api_key),
                "using": self.llm_provider,
                "chosen_by": "auto" if self._provider_auto else "LLM_PROVIDER",
                "models": [self.resolved_model_extract, self.resolved_model_judge],
            },
            {
                "id": "embeddings",
                "title": "An embedding key",
                "purpose": "matches the same term across documents. Claude has no embedding "
                "model, so add an OpenAI or Gemini key even when extracting with Claude",
                "any_of": [option(embed_keys, p) for p in embed_options],
                "met": bool(self.embedding_api_key),
                "using": self.embedding_provider,
                "chosen_by": "auto" if self._embedding_auto else "EMBEDDING_PROVIDER",
                "models": [self.resolved_embedding_model],
            },
        ]
        return {
            "requirements": requirements,
            "ready": not self.missing_requirements(),
        }

    @property
    def is_degraded(self) -> bool:
        return bool(self.missing_requirements())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # The tracked YAML source supplies the required non-secret fields at
    # runtime; mypy cannot model a Pydantic settings source.
    return Settings()  # type: ignore[call-arg]
