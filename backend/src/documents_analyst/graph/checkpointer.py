"""The Postgres checkpointer, and the schema boundary around it.

LangGraph's `PostgresSaver` creates four tables with `CREATE TABLE IF NOT EXISTS`
and takes no schema argument, so left alone they land in `public` and our
fourteen tables quietly become eighteen. So the connection is pinned to a
`langgraph` schema of its own. Nothing in `analyst/` reads those tables — a
checkpoint is the graph runtime's internal state, and querying it behind
LangGraph's back is how a resume starts disagreeing with itself.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver

from documents_analyst.db.dsn import psycopg_dsn, with_options

CHECKPOINT_SCHEMA = "langgraph"


def psycopg_url(database_url: str, *, schema: str = CHECKPOINT_SCHEMA) -> str:
    """SQLAlchemy's URL → a psycopg one with `search_path` pinned. `options` puts
    the checkpointer's DDL in its own schema; `public` stays on the path because
    LangGraph's DDL references `gen_random_uuid()` and other extensions installed
    there."""
    return with_options(psycopg_dsn(database_url), f"-c%20search_path%3D{schema}%2Cpublic")


@contextmanager
def checkpointer(database_url: str) -> Iterator[PostgresSaver]:
    """A ready checkpointer, its tables created if this is the first run.
    `setup()` on every open, not once at deploy: it is `CREATE TABLE IF NOT
    EXISTS` plus LangGraph's version ledger, cheap and idempotent, and the DDL
    belongs to the library so our migrations cannot do it."""
    with PostgresSaver.from_conn_string(psycopg_url(database_url)) as saver:
        saver.setup()
        yield saver
