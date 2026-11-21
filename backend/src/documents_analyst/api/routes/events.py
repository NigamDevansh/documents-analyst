"""GET /piles/{id}/events — SSE.

The route is thin on purpose; the interesting parts are in `api/events.py`: one
`LISTEN` for the process, a notification that means "look again" rather than carrying
data, and a cursor that is `stage_events.id` rather than `seq`.

No request-scoped transaction here: this response lives as long as a tab is open, and
the per-request connection dependency would pin one connection and one idle
transaction for all of it. The stream takes its own short connection per read.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Request
from sse_starlette.sse import EventSourceResponse

from documents_analyst.api.events import sse_frames

router = APIRouter(prefix="/piles", tags=["events"])


@router.get("/{pile_id}/events")
async def events(
    pile_id: str,
    request: Request,
    last_event_id: Annotated[str | None, Header()] = None,
) -> EventSourceResponse:
    """Live pipeline events, replaying anything missed since `Last-Event-ID`.
    Reconnect replays, it doesn't skip: the browser sends the last id it saw, we read
    the rows after it, send those, and only then wait for new ones — which is why
    `stage_events` is a table not a log file ("everything after 42" is a query). A
    client that never connected sends nothing and gets the pile's whole history,
    paged, so a dashboard opened mid-run draws the same timeline as one watching from
    the start."""
    settings = request.app.state.settings
    return EventSourceResponse(
        sse_frames(
            engine=request.app.state.engine,
            broker=request.app.state.broker,
            pile_id=pile_id,
            after_id=_cursor(last_event_id),
            keepalive_s=settings.sse_keepalive_s,
            limit=settings.sse_replay_limit,
        )
    )


def _cursor(last_event_id: str | None) -> int:
    """A bad header replays everything rather than 400ing. `Last-Event-ID` is set by
    the browser's own EventSource, not our code, so a proxy that mangles it should
    cost one extra page of history — not a stream that refuses to open, the one case
    where losing the timeline hurts most."""
    try:
        return max(0, int(last_event_id or 0))
    except ValueError:
        return 0
