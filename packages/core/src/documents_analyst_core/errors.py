"""The exception hierarchy.

Typed, because the graph branches on specific failures — catching broadly would
swallow a real bug into a "provider is down" state that parks quietly instead of
failing loudly. Modules in `portable/` declare their OWN error types inline so
they carry zero internal imports; the worker maps between the two at the boundary.

See docs/CORE_INTERNALS.md § "Error hierarchy".
"""

from __future__ import annotations


class DocumentsAnalystCoreError(Exception):
    """Root of everything this library raises."""


# ── configuration ─────────────────────────────────────────────────────────
class ConfigError(DocumentsAnalystCoreError):
    """Injected config is missing or invalid. Core never reads env itself."""


# ── parsing ───────────────────────────────────────────────────────────────
class ParseError(DocumentsAnalystCoreError):
    """A document could not be read."""


class UnsupportedFormat(ParseError):
    """The extension is not one we handle."""


# ── grounding: Gate 1 territory ───────────────────────────────────────────
class GroundingError(DocumentsAnalystCoreError):
    """A claim could not be tied back to its source."""


class QuoteNotVerbatim(GroundingError):
    """`source_quote` is not present at the offsets given in extracted_text.

    This is the failure that makes "never bluffs" testable rather than
    aspirational. It is never recovered from by loosening the check.
    """


# ── local models ──────────────────────────────────────────────────────────
class ModelUnavailable(DocumentsAnalystCoreError):
    """A local model failed to load.

    Carries the three things a person needs to act: which model, which env var
    controls it, and what stopped working. Without those the message degrades
    to "model failed", which sends someone reading source code.
    """

    def __init__(self, *, model: str, env_var: str, capability: str) -> None:
        self.model = model
        self.env_var = env_var
        self.capability = capability
        super().__init__(
            f"Could not load {model!r} (set by {env_var}). Unavailable capability: {capability}."
        )


# ── LLM ───────────────────────────────────────────────────────────────────
class LLMError(DocumentsAnalystCoreError):
    """Base for provider-call failures."""


class LLMSchemaViolation(LLMError):
    """Output did not match the requested schema after the bounded retries."""


class LLMTransportError(LLMError):
    """The provider was unreachable."""
