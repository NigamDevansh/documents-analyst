"""`text-embedding-3-small`, asked for 384 dimensions.

Replaces bge-small, not for quality (they score within a point on retrieval) but
because a 130MB model, an ONNX runtime and a prefetch step were carried for one
call site in a pipeline that already calls OpenAI. 384 is asked for, not
truncated by us: the model is trained so a prefix of its 1536-dim output is
itself usable, and the API's `dimensions` parameter returns that prefix
renormalised — which keeps the `vector(384)` column valid, so switching provider
is not also a re-embedding of every row.
"""

from __future__ import annotations

import time
from typing import Any

from documents_analyst_core.portable.llm_client import Usage

from .base import DIMENSIONS

DEFAULT_MODEL = "text-embedding-3-small"

BATCH = 128
"""Inputs per request. The API accepts more; this is about failure size.

Everything embedded here is short — a fact key or an open subject, tens of
tokens — so the token ceiling is never the binding constraint. A batch is
retried whole, so a smaller one means a rate limit costs less work.
"""

ATTEMPTS = 3
"""Retried, unlike `chat_async`. Embedding is a pure read: the same input
returns the same vector and nothing is created, so a retry after an ambiguous
timeout cannot duplicate anything — unlike a non-idempotent write, where an
ambiguous timeout must never be retried blind."""


class EmbeddingUnavailable(Exception):
    """No key, no package, or the provider refused.

    Named to match `bge_small.ModelUnavailable`, which it replaces, because the
    contract is the same one: **there is no degraded mode.** Every conflict
    between two open subjects depends on their vectors being comparable. A zero
    vector would still be 384 floats, the cosine would still return a number,
    and the pile would quietly find no conflicts — indistinguishable from a pile
    that has none. So this raises and `load_embeddings` lets it.
    """

    def __init__(self, *, model: str, detail: str = "") -> None:
        self.model = model
        super().__init__(
            f"Could not embed with {model!r}"
            + (f": {detail}" if detail else "")
            + ". Set OPENAI_API_KEY (embeddings use OpenAI regardless of LLM_PROVIDER). "
            "Unavailable capability: fact-key embeddings — open subjects cannot be "
            "matched across documents, and there is no honest fallback."
        )


class OpenAIEmbeddings:
    """Stateless per call, apart from the usage of the last one.

    `last_usage` is how an embedding batch reaches `stage_runs` next to the LLM
    calls in the same node. Embeddings are cheap, not free, and this repo's own
    rule is that a cost report which shows a billable call as free is lying.
    The node reads it immediately after the call it made; nothing else does.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model_name: str = DEFAULT_MODEL,
        dimensions: int = DIMENSIONS,
    ) -> None:
        if not api_key:
            raise EmbeddingUnavailable(model=model_name, detail="no API key")
        self.model_name = model_name
        self.dimensions = dimensions
        self._api_key = api_key
        self._client: Any = None
        self.last_usage: Usage | None = None

    def _sdk(self) -> Any:
        if self._client is None:
            try:
                import openai
            except Exception as exc:  # noqa: BLE001 — package missing
                raise EmbeddingUnavailable(model=self.model_name, detail=str(exc)) from exc
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    @property
    def is_available(self) -> bool:
        """Whether a call *could* be made. Deliberately does not call.

        A network probe at worker start would spend money to answer a question
        a missing key already answers, and would fail for a blip that the first
        real call's retry would have ridden out.
        """
        return bool(self._api_key)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Order is preserved and asserted.

        Callers zip these back against their facts by position — a reordering
        would attach every vector to the wrong text and the mistake would be
        invisible, because wrong vectors still cluster.
        """
        if not texts:
            self.last_usage = None
            return []

        # The API rejects an empty string, and rejects the **whole batch** for
        # one. An open subject that arrives as `""` would therefore lose the
        # vectors of the 127 real subjects beside it, and the run with them —
        # so it is padded here rather than dropped, because dropping shifts
        # every later vector onto the wrong fact.
        prepared = [text if text.strip() else " " for text in texts]

        vectors: list[list[float]] = []
        tokens = attempts = 0
        for start in range(0, len(prepared), BATCH):
            batch = prepared[start : start + BATCH]
            got, used, tries = self._embed_batch(batch)
            vectors.extend(got)
            tokens += used
            attempts += tries

        if len(vectors) != len(texts):
            raise EmbeddingUnavailable(
                model=self.model_name,
                detail=f"asked for {len(texts)} vectors and got {len(vectors)}",
            )
        # Input tokens only. An embedding response has no completion, and
        # reporting the same number twice would double the cost of every run.
        self.last_usage = Usage(
            model=self.model_name, input_tokens=tokens, output_tokens=0, attempts=attempts
        )
        return vectors

    def _embed_batch(self, batch: list[str]) -> tuple[list[list[float]], int, int]:
        """One request, retried. Returns vectors, tokens and attempts made."""
        last: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self._sdk().embeddings.create(
                    model=self.model_name, input=batch, dimensions=self.dimensions
                )
            except Exception as exc:  # noqa: BLE001 — SDK errors are vendor types
                last = exc
                if attempt == ATTEMPTS or _permanent(exc):
                    break
                time.sleep(0.5 * 2 ** (attempt - 1))
                continue

            # `index` is what the API guarantees, not list position. Sorting by
            # it costs nothing and removes an assumption we would never notice
            # being wrong.
            items = sorted(response.data, key=lambda row: getattr(row, "index", 0))
            vectors = [[float(x) for x in row.embedding] for row in items]
            for vector in vectors:
                if len(vector) != self.dimensions:
                    # `vector(384)` is fixed in the schema. A different width
                    # fails at INSERT with a message about the column rather
                    # than about the model, so it is caught here instead.
                    raise EmbeddingUnavailable(
                        model=self.model_name,
                        detail=f"expected {self.dimensions} dimensions, got {len(vector)}",
                    )
            usage = getattr(response, "usage", None)
            return vectors, int(getattr(usage, "prompt_tokens", 0) or 0), attempt

        raise EmbeddingUnavailable(model=self.model_name, detail=str(last))


def _permanent(exc: Exception) -> bool:
    """Whether trying again cannot help.

    A bad key, a model name that does not exist and a malformed request all
    return the same way three times. Retrying them costs the caller 1.5 seconds
    of sleeps before the honest answer — and the answer is the one thing they
    need, because every one of these is fixed by editing `.env`, not by waiting.
    `EMBEDDING_MODEL=BAAI/bge-small-en-v1.5` left over from the local model is
    exactly this case.

    Status is read off the exception rather than by importing `openai` errors:
    this module is the only thing in core that touches the embeddings API, and
    dragging the vendor's exception hierarchy into it to classify four integers
    would be the more fragile of the two.
    """
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    return status in (400, 401, 403, 404, 422)
