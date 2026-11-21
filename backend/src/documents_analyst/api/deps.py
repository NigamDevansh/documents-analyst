"""Injection: settings, a connection per request, the token guard, the actor.

Repos take a connection rather than opening one, so the caller owns the boundary —
for the API, one request. The API runs the worker's queries, not its own copies:
every route reads through `documents_analyst.db.repo.*`, the same sync functions the graph
nodes call. `AsyncConnection.run_sync` hands a sync-style connection to those on the
same underlying connection, so there is one definition of every query — no async
twin to drift from its fenced original.

    rows = await run(conn, pile_repo.summaries)

No CORS (frontend is same-origin), no session or user table (no login — one shared
token, and `X-Actor` per request).

See docs/BACKEND_INTERNALS.md § "It runs the worker's queries, not copies of them".
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from documents_analyst.settings import Settings


def engine_of(request: Request) -> AsyncEngine:
    """The pool built once in the lifespan, never per request."""
    engine: AsyncEngine = request.app.state.engine
    return engine


def settings_of(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


async def get_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """One transaction per request, committed at the end of a successful one.
    `engine.begin()`, not `connect()`, so reads and writes share a boundary and a
    handler that raises mid-change rolls both statements back.

    NOT used by the event stream: an SSE response lives as long as the tab is open,
    and an idle transaction held for hours would pin a connection and block VACUUM.
    That route takes its own short connection per read."""
    async with engine_of(request).begin() as conn:
        yield conn


Db = Annotated[AsyncConnection, Depends(get_conn)]
Config = Annotated[Settings, Depends(settings_of)]


async def run[T](conn: AsyncConnection, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call a synchronous repo function on this async connection."""
    return await conn.run_sync(lambda sync_conn: fn(sync_conn, *args, **kwargs))


async def require_token(
    request: Request, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Open when `API_TOKEN` is unset, which is the default. A take-home a reviewer
    must authenticate against before it shows anything is one that gets closed.
    Setting the variable turns the guard on for every route it's attached to; `/`
    and `/health` deliberately aren't among them. `compare_digest`, not `==`,
    because `==` leaks the prefix through timing."""
    expected = settings_of(request).api_token
    if not expected:
        return
    supplied = ""
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API_TOKEN is set on this instance; send Authorization: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )


Guarded = Depends(require_token)


async def validate_ids(request: Request) -> None:
    """Every `*_id` in a path is a UUID; reject the others here. Without this a
    malformed id reaches Postgres, which raises `invalid input syntax for type
    uuid` — a 500 for what is really "you passed nonsense" (an MCP client calling
    `approve_item` with a placeholder id hits this first thing). One router-level
    dependency, not a typed param on twenty handlers, so a new route can't forget
    it. 404, not 422: the caller's next action is the same — get a real id from
    `list_piles` — and 404 avoids implying the thing might exist somewhere."""
    for name, value in request.path_params.items():
        if not name.endswith("_id"):
            continue
        try:
            uuid.UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(
                status_code=404,
                detail=f"{name} {value!r} is not a valid id — ids come from GET /piles",
            ) from None


def actor(x_actor: Annotated[str | None, Header()] = None) -> str:
    """Who is doing this. Required, not defaulted. `X-Actor` lands in
    `pending_decisions.decided_by`, the only authority `Commit` has for editing an
    external document. An approval with no name is one nobody can be asked about
    later, so the request is refused rather than filled with `"api"` — a placeholder
    in an audit trail looks like an answer, which is worse than a gap.

    Required only where it's stored: upload doesn't ask for it (no column records
    who dropped a file in). Not authentication — it's a caller-chosen label, like a
    git author; `API_TOKEN` decides whether the caller may act at all."""
    name = (x_actor or "").strip()
    if not name:
        # The literal, not `status.HTTP_422_UNPROCESSABLE_ENTITY`: Starlette
        # deprecated that name and warns on every use.
        raise HTTPException(
            status_code=422, detail="X-Actor is required: a decision records who made it"
        )
    return name[:120]


Actor = Annotated[str, Depends(actor)]
