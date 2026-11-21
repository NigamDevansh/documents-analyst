"""One `DATABASE_URL`, two libraries that disagree about how to read it.

`postgresql+psycopg://` is SQLAlchemy dialect syntax. psycopg — which both the
LangGraph checkpointer and the API's `LISTEN` connection use directly — doesn't
understand the `+psycopg` part and fails on it. Kept in one place because three
callers doing the same string edit is the duplication that looks harmless until one
learns about a query parameter and the others don't.
"""

from __future__ import annotations

SQLALCHEMY_PREFIX = "postgresql+psycopg://"


def psycopg_dsn(database_url: str) -> str:
    """A URL psycopg can connect with, unchanged in every other respect."""
    return database_url.replace(SQLALCHEMY_PREFIX, "postgresql://", 1)


def with_options(dsn: str, options: str) -> str:
    """Append libpq `options`, respecting an existing query string."""
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options={options}"
