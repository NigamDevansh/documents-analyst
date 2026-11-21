"""Repository: stage events. What the dashboard replays after a reconnect.

A table rather than a log file, for one deciding reason: a browser coming back asks
"everything after seq 42", which is a query. Against a file it's a grep, and against a
file two workers are appending to it's a grep over interleaved lines.

`seq` is per run and allocated here, not by the caller. A caller counting its own events
gets it right until a node is retried, at which point two events share a number and the
reconnecting browser silently skips one.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

APPEND = text("""
    INSERT INTO stage_events (pile_id, run_id, seq, type, payload)
         SELECT :pile_id, :run_id,
                COALESCE((SELECT max(seq) FROM stage_events WHERE run_id = :run_id), 0) + 1,
                :type, CAST(:payload AS jsonb)
      RETURNING seq
""")
"""The sequence is allocated inside the INSERT, so `max(seq) + 1` isn't a check-then-act — two
workers appending to one run can't compute the same number (the loser hits `UNIQUE (run_id,
seq)`). Not fenced: a worker that lost its lease still did the thing it's reporting."""


def append(
    conn: Connection, *, pile_id: str, run_id: str, type: str, payload: dict[str, Any] | None = None
) -> int:
    """One event. Returns the sequence it was given."""
    row = conn.execute(
        APPEND,
        {
            "pile_id": pile_id,
            "run_id": run_id,
            "type": type,
            "payload": json.dumps(payload or {}),
        },
    ).one()
    return int(row.seq)


def since(conn: Connection, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
    """The replay query. `after_seq=0` is the whole run."""
    rows = conn.execute(
        text(
            "SELECT seq, type, payload, created_at FROM stage_events "
            "WHERE run_id = :run_id AND seq > :after ORDER BY seq"
        ),
        {"run_id": run_id, "after": after_seq},
    ).all()
    return [dict(row._mapping) for row in rows]


def since_id(
    conn: Connection, pile_id: str, after_id: int = 0, limit: int = 500
) -> list[dict[str, Any]]:
    """The SSE cursor is `id`, not `seq`. `seq` restarts at 1 per run, so a browser that saw
    seq 12 and reconnects during the next run would ask for "after 12" and skip its first
    eleven events. `id` is a table-wide BIGSERIAL, monotonic across runs — what `Last-Event-ID`
    needs. `seq` stays in the payload as within-run ordering."""
    rows = conn.execute(
        text(
            "SELECT id, run_id, seq, type, payload, created_at FROM stage_events "
            "WHERE pile_id = :pile_id AND id > :after ORDER BY id LIMIT :limit"
        ),
        {"pile_id": pile_id, "after": after_id, "limit": limit},
    ).all()
    return [dict(row._mapping) for row in rows]


def latest_for_pile(conn: Connection, pile_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """What has happened in this pile lately, newest first. The other question the
    dashboard asks, and the reason `stage_events` has an index on `(pile_id, id DESC)` —
    a pile's timeline spans runs, so it can't be answered from the per-run replay index."""
    rows = conn.execute(
        text(
            "SELECT run_id, seq, type, payload, created_at FROM stage_events "
            "WHERE pile_id = :pile_id ORDER BY id DESC LIMIT :limit"
        ),
        {"pile_id": pile_id, "limit": limit},
    ).all()
    return [dict(row._mapping) for row in rows]
