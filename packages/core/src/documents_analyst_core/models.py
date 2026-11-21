"""Model selection and the degradation ladder, in one readable place.

`CODE_ARCHITECTURE.md` §2.5 states what happens when each model will not load.
That table is a promise, and a promise spread across four call sites is one
nobody can check. So it is here, once:

| Model | Unavailable → |
|---|---|
| embeddings | **raises.** Open-subject matching is load-bearing and there is no honest fallback |
| NLI | `StubNLI` + a warning. Every pair escalates to the LLM: slower, pricier, correct |
| GLiNER | `DisabledNER` + a warning. Party extraction yields **no candidates** — not "LLM-only": `pick_parties` chooses among candidates, so with none it returns nothing. Asking the model to name parties freely would break the 2B guarantee that a party name is always a verbatim span from the document |

Each function returns `(client, warning)`. The warning is **returned, not
logged**, for the same reason a `Rejection` is: the caller has to put it on the
degraded banner and in `stage_runs`, and something it never receives cannot
appear in either.

Core still picks nothing on its own — these take explicit arguments. The one
concession is `cache_dir`: when a caller passes None these fall back to
`model_cache`, which honours `MODEL_CACHE_DIR`, rather than to each library's
own `~/.cache`. Without that the documented host workflow was broken — prefetch
filled `.cache/models` and the loaders then downloaded a second copy somewhere
else, which is the same bug the image had.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import model_cache
from .embeddings.base import EmbeddingProvider
from .embeddings.openai_embeddings import DEFAULT_MODEL as DEFAULT_EMBEDDING_MODEL
from .embeddings.openai_embeddings import EmbeddingUnavailable, OpenAIEmbeddings
from .nli.base import NLIClient
from .nli.deberta import DebertaNLI
from .nli.deberta import ModelUnavailable as NLIUnavailable
from .nli.stub import StubNLI
from .portable.ner_gliner import DisabledNER, GLiNERClient, ModelUnavailable, NERClient


@dataclass(frozen=True, slots=True)
class Loaded:
    """What a caller got, and what it cost them.

    `degraded` is not derivable from the client alone: `StubNLI` is also what
    an operator gets when they deliberately set `NLI_ENABLED=false`, and
    "switched off" and "failed to load" must not read the same in a report.
    """

    warning: str = ""
    degraded: bool = False
    disabled: bool = False


def load_embeddings(
    *, api_key: str, model_name: str = DEFAULT_EMBEDDING_MODEL, provider: str = "openai"
) -> tuple[EmbeddingProvider, Loaded]:
    """The one hard failure. Raises rather than returning a degraded client.

    A fallback embedder would return vectors, cosines would return numbers, and
    the results would be quietly wrong — a pile that finds no conflicts looks
    exactly like a pile that has none. There is no version of that which is
    honest, so there is no fallback.

    **No `cache_dir` any more, and that is the point of the change.** This used
    to load bge-small from disk, which is why it took a cache directory and why
    a missing download was a startup failure. The failure it can have now is a
    missing key, which is checked without a network call — so the worker still
    stops at startup rather than three documents into a pile.
    """
    if provider == "gemini":
        from .embeddings.gemini_embeddings import GeminiEmbeddings

        return GeminiEmbeddings(api_key=api_key, model_name=model_name), Loaded()
    client = OpenAIEmbeddings(api_key=api_key, model_name=model_name)
    return client, Loaded()


def load_nli(
    *, enabled: bool, model_name: str, cache_dir: str | None = None
) -> tuple[NLIClient, Loaded]:
    """Degrades to `StubNLI`, which returns pure neutral.

    Neutral routes every candidate pair to LLM adjudication. The dishonest
    alternative — assuming "no contradiction" — would silently drop real
    conflicts, which is the failure this whole system exists to prevent.
    """
    if not enabled:
        return StubNLI(), Loaded(
            warning="NLI is switched off (NLI_ENABLED=false); every candidate pair "
            "goes to the LLM. Slower and more expensive, not less correct.",
            disabled=True,
        )
    client = DebertaNLI(model_name=model_name, cache_dir=cache_dir or str(model_cache.hf_hub()))
    try:
        client.load()
    except NLIUnavailable as exc:
        return StubNLI(), Loaded(warning=str(exc), degraded=True)
    return client, Loaded()


def load_ner(
    *, enabled: bool, model_name: str, cache_dir: str | None = None
) -> tuple[NERClient, Loaded]:
    """Degrades to `DisabledNER`, which returns no spans and says so.

    Party extraction then has no candidates, and `pick_parties` returns an
    empty list rather than inviting the model to name parties freely — the
    indices-not-strings guarantee holds in the degraded path too, which is the
    point of expressing "off" as an object instead of an `if`.
    """
    if not enabled:
        return DisabledNER(), Loaded(
            warning="GLiNER is switched off (GLINER_ENABLED=false); party candidates "
            "and PII tier 2 are unavailable.",
            disabled=True,
        )
    client = GLiNERClient(model_name=model_name, cache_dir=cache_dir or str(model_cache.hf_hub()))
    try:
        client.load()
    except ModelUnavailable as exc:
        return DisabledNER(), Loaded(warning=str(exc), degraded=True)
    return client, Loaded()


__all__ = [
    "EmbeddingUnavailable",
    "Loaded",
    "load_embeddings",
    "load_ner",
    "load_nli",
]
