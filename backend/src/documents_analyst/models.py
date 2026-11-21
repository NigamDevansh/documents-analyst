"""Settings → loaded models. The composition core deliberately doesn't do.

`documents_analyst_core.models` knows the degradation ladder but reads no configuration; `Settings`
holds the configuration but loads nothing. This module is the join, and it exists because
without it `MODEL_CACHE_DIR` was a documented knob that did nothing: pydantic reads `.env`
into a `Settings` field, which is NOT the same as exporting a process environment variable.
Both the env var and `model_cache_dir` in config now resolve to the same directory.
"""

from __future__ import annotations

import functools
import threading
from dataclasses import dataclass, field, replace
from typing import Any, cast

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
        api_key=settings.embedding_api_key,
        model_name=settings.resolved_embedding_model,
        provider=settings.embedding_provider,
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


class _Serialized:
    """A model that answers one caller at a time. Attribute reads pass straight through;
    every method call takes the lock.

    The worker runs several piles in parallel threads that share one set of weights —
    loading a copy per thread would multiply gigabytes. PyTorch inference is safe to share,
    but a Hugging Face fast tokenizer is not: two threads in it at once raise "Already
    borrowed". These calls take milliseconds next to an LLM call's seconds, so queueing
    them costs nothing visible.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._lock = threading.Lock()

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def call(*args: Any, **kwargs: Any) -> Any:
            with self._lock:
                return attr(*args, **kwargs)

        return call


def shared_across_threads(models: LoadedModels) -> LoadedModels:
    """The same models, with the two local ones serialised. Embeddings are an HTTP client
    whose only state (`last_usage`) is already per thread, so they stay as they are."""
    return replace(
        models,
        nli=cast(NLIClient, _Serialized(models.nli)),
        ner=cast(NERClient, _Serialized(models.ner)),
    )
