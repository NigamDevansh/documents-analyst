"""Settings → loaded models. The composition core deliberately doesn't do.

`documents_analyst_core.models` knows the degradation ladder but reads no configuration; `Settings`
holds the configuration but loads nothing. This module is the join, and it exists because
without it `MODEL_CACHE_DIR` was a documented knob that did nothing: pydantic reads `.env`
into a `Settings` field, which is NOT the same as exporting a process environment variable.
Both the env var and `model_cache_dir` in config now resolve to the same directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from documents_analyst_core import model_cache
from documents_analyst_core.embeddings.base import EmbeddingProvider
from documents_analyst_core.embeddings.openai_embeddings import EmbeddingUnavailable
from documents_analyst_core.models import load_embeddings, load_ner, load_nli
from documents_analyst_core.nli.base import NLIClient
from documents_analyst_core.portable.ner_gliner import NERClient

from .settings import Settings


def resolve_cache(settings: Settings) -> str | None:
    """Apply the configured model cache directory to the process, and return it. Exported,
    not merely returned: transformers and gliner each read their own environment variable,
    and any code path reaching one without an explicit `cache_dir` would otherwise use
    `~/.cache` while everything else used the configured directory — which is how the image
    ended up baking 2.2GB and downloading it again. Blank means "leave the environment
    alone", which inside the image is already `/opt/models` from the Dockerfile."""
    if not settings.model_cache_dir:
        return None
    model_cache.apply_environment(settings.model_cache_dir)
    return settings.model_cache_dir


@dataclass(frozen=True, slots=True)
class LoadedModels:
    """Every model a worker needs, plus what it cost to get them. Two of the three are
    local; embeddings are an API client now, so its "loading" is checking a key."""

    embeddings: EmbeddingProvider
    nli: NLIClient
    ner: NERClient
    warnings: list[str] = field(default_factory=list)
    """Degradations, in load order. Returned rather than logged so the caller can put them
    on the degraded banner and in `stage_runs` — a warning the caller never receives can
    appear in neither."""

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)


def load_models(settings: Settings) -> LoadedModels:
    """Load all three. Raises `EmbeddingUnavailable` and nothing else. The asymmetry is the
    ladder from `CODE_ARCHITECTURE.md` §2.5: matching open subjects has no honest fallback,
    so an unusable embedder stops the process here rather than producing a run that finds no
    conflicts and looks clean. NLI and GLiNER degrade and say so. Embeddings are the one
    model no longer local, so "load" is a key check, but it's still done here at startup —
    three documents into a pile is the wrong place to find out."""
    cache = resolve_cache(settings)
    warnings: list[str] = []

    embeddings, _ = load_embeddings(
        api_key=settings.openai_api_key, model_name=settings.embedding_model
    )
    nli, nli_state = load_nli(
        enabled=settings.nli_enabled, model_name=settings.nli_model, cache_dir=_hub(cache)
    )
    ner, ner_state = load_ner(
        enabled=settings.gliner_enabled, model_name=settings.gliner_model, cache_dir=_hub(cache)
    )

    warnings += [s.warning for s in (nli_state, ner_state) if s.warning]
    return LoadedModels(embeddings=embeddings, nli=nli, ner=ner, warnings=warnings)


def cache_status(settings: Settings) -> dict[str, object]:
    """What's on disk, without loading anything. Cheap enough for `/health`, which must not
    pull 2GB of weights into memory to answer whether the process is up. `missing` names the
    absent models rather than a bare boolean, because "incomplete" and "which one" are
    different questions and only the second is actionable."""
    cache = resolve_cache(settings)
    return {
        "root": str(model_cache.root(cache)),
        "complete": model_cache.is_populated(cache),
        "missing": model_cache.missing(cache),
    }


def _hub(cache: str | None) -> str | None:
    return str(model_cache.hf_hub(cache)) if cache else None


__all__ = [
    "EmbeddingUnavailable",
    "LoadedModels",
    "cache_status",
    "load_models",
    "resolve_cache",
]
