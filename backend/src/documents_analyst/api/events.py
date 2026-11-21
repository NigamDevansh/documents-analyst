"""SSE fan-out — one LISTEN, many subscribers.

A single background task holds one `LISTEN analyst_events` and wakes per-client
`asyncio.Queue`s, not one Postgres connection per browser. The notification is a
hint, the table is the truth: it carries ids and a type, never the payload (which
could push `pg_notify` past its 8000-byte limit and abort the writing
transaction), so a woken stream re-reads `stage_events` from its cursor exactly
as a reconnecting one does.

That one decision makes replay and live delivery one code path, makes a
lost/duplicated notification cost a query not an event (and they ARE lost — the
LISTEN connection drops), and keeps a slow client's queue to one "look again"
token. Reconnect replays via `Last-Event-ID`, which is `stage_events.id`
(a table-wide BIGSERIAL), not `seq` which restarts per run.

See docs/BACKEND_INTERNALS.md § "SSE — one listener, many subscribers".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import psycopg

LOG = logging.getLogger(__name__)

EVENT_CHANNEL = "analyst_events"


class EventBroker:
    """One LISTEN connection for the process, fanned out to subscribers. Keyed by
    pile: a browser looking at one pile isn't woken by another pile's run, so a busy
    neighbour costs it nothing."""

    def __init__(self, dsn: str, *, channel: str = EVENT_CHANNEL) -> None:
        self._dsn = dsn
        self._channel = channel
        self._subscribers: dict[str, set[asyncio.Queue[None]]] = {}
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self.connected = False
        """Whether the LISTEN connection is currently up. Reported by `/health`: a
        stream that silently degrades to "nothing ever arrives" looks exactly like a
        system with nothing to say."""

    async def start(self) -> None:
        if self._task is None:
            self._stopping.clear()
            self._task = asyncio.create_task(self._listen_forever(), name="stage-event-listener")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False

    @contextlib.contextmanager
    def subscribe(self, pile_id: str) -> Iterator[asyncio.Queue[None]]:
        """A wake-up queue for one pile, removed on the way out. `maxsize=1` is the
        backpressure policy and needs no bookkeeping: the token means "there may be
        something new", so a second token while the first is unread is not
        information. A stalled client costs one slot while it lingers, not one per
        event for the life of the run."""
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
        self._subscribers.setdefault(pile_id, set()).add(queue)
        try:
            yield queue
        finally:
            listeners = self._subscribers.get(pile_id)
            if listeners is not None:
                listeners.discard(queue)
                if not listeners:
                    del self._subscribers[pile_id]

    def wake(self, pile_id: str) -> None:
        """Tell this pile's subscribers to look again. Never blocks."""
        for queue in self._subscribers.get(pile_id, ()):
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    def wake_all(self) -> None:
        """Used after a reconnect, when we can't know what was missed. The right
        response to "the listener was down for two seconds" isn't to work out which
        events were published then — it's to tell everyone to re-read."""
        for pile_id in list(self._subscribers):
            self.wake(pile_id)

    async def _listen_forever(self) -> None:
        delay = 0.5
        while not self._stopping.is_set():
            try:
                await self._listen_once()
                delay = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a dropped listener must retry, not die
                self.connected = False
                LOG.warning("stage event listener lost (%s: %s); retrying", type(exc).__name__, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

    async def _listen_once(self) -> None:
        conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
        try:
            await conn.execute(f"LISTEN {self._channel}")
            self.connected = True
            # Anything published while we were reconnecting is gone. Waking
            # every subscriber turns that hole into one extra query each.
            self.wake_all()
            async for notification in conn.notifies():
                self.wake(_pile_of(notification.payload))
        finally:
            self.connected = False
            await conn.close()


def _pile_of(payload: str) -> str:
    """The pile id out of a notification, tolerantly. A malformed payload must not
    kill the listener for every other pile — and the only cost of getting one wrong
    is a missed wake-up, which the next notification or keepalive re-read repairs."""
    try:
        parsed: dict[str, Any] = json.loads(payload)
    except ValueError:
        return ""
    return str(parsed.get("pile_id") or "")


async def sse_frames(
    *,
    engine: Any,
    broker: EventBroker,
    pile_id: str,
    after_id: int,
    keepalive_s: float,
    limit: int,
) -> AsyncIterator[dict[str, str]]:
    """The stream: replay from the cursor, then wake, re-read, repeat. Subscribing
    happens BEFORE the first read, and that order is the difference between a correct
    stream and one that drops an event roughly never — the worst frequency for a bug.
    Each read takes its own short connection, so a tab left open overnight doesn't pin
    a connection and an idle transaction with it."""
    from documents_analyst.db.repo import events as event_repo

    cursor = after_id
    with broker.subscribe(pile_id) as wake:
        while True:
            while True:
                async with engine.connect() as conn:
                    rows = await conn.run_sync(
                        lambda sync_conn, after=cursor: event_repo.since_id(
                            sync_conn, pile_id, after, limit=limit
                        )
                    )
                if not rows:
                    break
                for row in rows:
                    cursor = int(row["id"])
                    yield {
                        "id": str(cursor),
                        "event": str(row["type"]),
                        "data": json.dumps(
                            {
                                "run_id": str(row["run_id"]),
                                "seq": int(row["seq"]),
                                "type": str(row["type"]),
                                "payload": row["payload"],
                                "created_at": row["created_at"].isoformat(),
                            }
                        ),
                    }
                if len(rows) < limit:
                    break
                # A full page means more behind it. Loop rather than wait, or a
                # browser rejoining a long run gets one page per keepalive.

            try:
                await asyncio.wait_for(wake.get(), timeout=keepalive_s)
            except TimeoutError:
                # A comment frame: proxies close idle connections, and reconnecting
                # a pile that is merely thinking is churn nobody can act on.
                yield {"event": "ping", "data": "{}"}
