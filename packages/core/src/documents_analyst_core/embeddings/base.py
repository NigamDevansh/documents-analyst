"""The embedding protocol.

Core defines the interface; the application picks the implementation. Core
never picks for it.
"""

from __future__ import annotations

from typing import Protocol

from documents_analyst_core.portable.llm_client import Usage

DIMENSIONS = 384
"""Pinned here because the pgvector column type in the schema hardcodes it.

It was bge-small's native width. `text-embedding-3-small` replaced bge and is
natively 1536, but it is trained so a prefix of its output is itself a usable
embedding — so we ask the API for 384 and the column stays valid. Changing this
number is still a migration and a re-embedding of every row, not a config
change, and this constant is where that stays obvious.
"""


class EmbeddingProvider(Protocol):
    """One method, because there is one caller.

    `embed_query` lived here too, for the asymmetric query/passage split bge
    was trained with. Nothing ever called it — there is no search — and a
    protocol method with no caller is the same unread mechanism the schema
    deleted a column for. Add it back with the query that needs it.
    """

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    last_usage: Usage | None
    """What the last `embed_documents` cost, or None if it embedded nothing.

    Part of the protocol rather than an implementation detail because the whole
    of this system's cost reporting is built on nothing being spent silently.
    A provider that runs locally would set it to None; one that bills per token
    must fill it, and the calling node records it beside its LLM calls.
    """
