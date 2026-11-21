"""`gemini-embedding-001`, asked for 384 dimensions — the other embedding provider.

Claude has no embedding model, so a deployment extracting with Claude still needs one
from somewhere; this lets a Gemini key serve instead of an OpenAI one. Like
`text-embedding-3-small`, the model is trained so a prefix of its output is a usable
embedding (`output_dimensionality`), which keeps the `vector(384)` column valid.

Two differences from the OpenAI client, both handled here rather than leaked:

- **Vectors below full width come back unnormalised.** Cosine in `match` assumes unit
  vectors only in that it compares magnitudes fairly, so each vector is normalised.
- **The response carries no token count.** The cost report must not show a billable
  call as free, so usage is estimated at four characters a token — the provider's own
  rule of thumb — and the model name in `Usage` says which estimate it is.

Vectors from two providers are not comparable. Switching provider is safe because
`match` re-embeds every fact whose stored model name differs from the current one.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

from documents_analyst_core.portable.llm_client import Usage

from .base import DIMENSIONS
from .openai_embeddings import ATTEMPTS, EmbeddingUnavailable, _permanent

DEFAULT_MODEL = "gemini-embedding-001"

BATCH = 100
"""The API's per-request limit for batched contents."""

CHARS_PER_TOKEN = 4


class GeminiEmbeddings:
    """Same contract as `OpenAIEmbeddings`, including a per-thread `last_usage`."""

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
        self._local = threading.local()

    @property
    def last_usage(self) -> Usage | None:
        return getattr(self._local, "usage", None)

    @last_usage.setter
    def last_usage(self, usage: Usage | None) -> None:
        self._local.usage = usage

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    def _sdk(self) -> Any:
        if self._client is None:
            try:
                from google import genai
            except Exception as exc:  # noqa: BLE001 — package missing
                raise EmbeddingUnavailable(model=self.model_name, detail=str(exc)) from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Order is preserved and asserted, as for OpenAI: callers zip by position."""
        if not texts:
            self.last_usage = None
            return []
        # An empty string fails the whole batch; pad rather than drop, so positions hold.
        prepared = [text if text.strip() else " " for text in texts]

        vectors: list[list[float]] = []
        attempts = 0
        for start in range(0, len(prepared), BATCH):
            got, tries = self._embed_batch(prepared[start : start + BATCH])
            vectors.extend(got)
            attempts += tries

        if len(vectors) != len(texts):
            raise EmbeddingUnavailable(
                model=self.model_name,
                detail=f"asked for {len(texts)} vectors and got {len(vectors)}",
            )
        estimated = sum(max(1, len(t) // CHARS_PER_TOKEN) for t in prepared)
        self.last_usage = Usage(
            model=self.model_name, input_tokens=estimated, output_tokens=0, attempts=attempts
        )
        return vectors

    def _embed_batch(self, batch: list[str]) -> tuple[list[list[float]], int]:
        from google.genai import types

        last: Exception | None = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = self._sdk().models.embed_content(
                    model=self.model_name,
                    contents=batch,
                    config=types.EmbedContentConfig(
                        task_type="SEMANTIC_SIMILARITY",
                        output_dimensionality=self.dimensions,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 — SDK errors are vendor types
                last = exc
                if attempt == ATTEMPTS or _permanent(exc) or _permanent_google(exc):
                    break
                time.sleep(0.5 * 2 ** (attempt - 1))
                continue

            vectors = [
                _unit([float(x) for x in (e.values or [])]) for e in response.embeddings or []
            ]
            for vector in vectors:
                if len(vector) != self.dimensions:
                    raise EmbeddingUnavailable(
                        model=self.model_name,
                        detail=f"expected {self.dimensions} dimensions, got {len(vector)}",
                    )
            return vectors, attempt

        raise EmbeddingUnavailable(model=self.model_name, detail=str(last))


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


def _permanent_google(exc: Exception) -> bool:
    """google-genai puts the HTTP status on `.code`."""
    return getattr(exc, "code", None) in (400, 401, 403, 404)
