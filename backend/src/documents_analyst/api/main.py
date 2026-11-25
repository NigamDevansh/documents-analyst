"""App factory, health, and the degraded-mode banner.

Compose runs `uvicorn documents_analyst.api.main:create_app --factory`, so there is no
module-level `app` built at import time — tests construct an app with their own
settings instead of inheriting a global one.

The lifespan opens two long-lived things shared by every request: an async engine,
and ONE `LISTEN` connection behind the event broker. Per-request versions of either
would mean a Postgres connection per open browser tab.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine

from documents_analyst.api import brand
from documents_analyst.api.deps import require_token, validate_ids
from documents_analyst.api.events import EventBroker
from documents_analyst.api.routes import (
    citations,
    cost,
    decisions,
    deliverable,
    documents,
    events,
    piles,
)
from documents_analyst.db.dsn import psycopg_dsn
from documents_analyst.models import cache_status
from documents_analyst.pricing import price_warnings
from documents_analyst.settings import Settings, get_settings

LOG = logging.getLogger(__name__)

PHASE = "7 — dashboard"


def check_database(settings: Settings) -> dict[str, Any]:
    """Report, never raise. A health endpoint that 500s when the database is down
    tells you less than one that answers and names the problem — and this is the
    page someone hits *because* something is wrong."""
    try:
        engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
        with engine.connect() as conn:
            applied = conn.execute(text("SELECT count(*) FROM schema_migrations")).scalar_one()
            tables = conn.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
            ).scalar_one()
        engine.dispose()
        return {"reachable": True, "migrations_applied": applied, "tables": tables}
    except Exception as exc:  # noqa: BLE001 — any failure is "not ready"
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    resolved = settings

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open the pool and the listener; close both, in that order, at the end.
        The broker starts even with nothing connected: `LISTEN` costs one idle
        connection, and starting it lazily on the first subscriber would make that
        client's stream miss whatever was published while it connected."""
        app.state.engine = create_async_engine(
            resolved.database_url, pool_pre_ping=True, pool_size=5, max_overflow=5
        )
        app.state.broker = EventBroker(psycopg_dsn(resolved.database_url))
        await app.state.broker.start()
        try:
            yield
        finally:
            await app.state.broker.stop()
            await app.state.engine.dispose()

    app = FastAPI(
        title=brand.NAME,
        version="0.7.0",
        description="Drop a folder of contracts in. Get back one contract register.",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # The token guard is attached per router, not app-wide, so `/` and `/health`
    # stay reachable on a locked-down instance: a page whose job is to name what's
    # misconfigured can't sit behind the credential, and a health check can't need
    # one to be usable by whatever restarts the container.
    for module in (piles, documents, decisions, deliverable, citations, events, cost):
        app.include_router(
            module.router, dependencies=[Depends(require_token), Depends(validate_ids)]
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness plus a full readiness picture in one call. Always 200: the body
        says what's wrong, the status code says the process is up. Conflating the two
        is how a degraded-but-working system gets restarted by a health check."""
        db = check_database(settings)
        missing = settings.missing_requirements()
        # Reads directory names only — never loads a model. Pulling 2GB of weights
        # into memory to answer "are you up" would be worse than no answer.
        models = cache_status(settings)
        # A stream that silently stopped delivering looks exactly like one with
        # nothing to say, so whether the LISTEN connection is up is reported.
        broker = getattr(app.state, "broker", None)
        listening = bool(broker is not None and broker.connected)
        return {
            "status": "ok",
            "phase": PHASE,
            "database": db,
            "models": models,
            "events": {"listening": listening, "channel": "analyst_events"},
            "auth": {"token_required": bool(settings.api_token)},
            "degraded": bool(missing) or not db["reachable"] or not models["complete"],
            "missing_configuration": missing,
            # What the setup guide shows: where pile folders go, as the person sees it
            # (`WATCH_DIR` on the host, not `/data/watched` inside the container).
            # The full key list with a set/missing flag each — never a value.
            "setup": settings.key_checklist(),
            # Prices never block a call; these only say where the cost report is
            # unpriced or using a price past its `valid_until`.
            "pricing": {"warnings": price_warnings(settings)},
            "watch": {
                "path": settings.watch_dir_host or str(settings.watch_root),
                "deliverable_filename": settings.deliverable_filename,
                "pile_concurrency": settings.pile_concurrency,
                "scan_interval_s": settings.scan_interval_s,
            },
        }

    def index_available() -> bool:
        """Whether a built dashboard is sitting in `STATIC_DIR`. Checked per request,
        not at startup: `make api` and `make ui` run side by side while the bundle
        builds, and a boot-time decision would serve the fallback until a restart."""
        return (Path(settings.static_dir) / "index.html").is_file()

    @app.get("/", response_class=HTMLResponse, response_model=None)
    def index() -> Response:
        """The dashboard if it's built, else an honest status page. A container that
        answers `/` with a 404 reads as broken, and a blank React shell reads worse.
        Without a bundle this stays the status page: what's configured, what's
        missing, where the API is."""
        if index_available():
            return FileResponse(Path(settings.static_dir) / "index.html")
        return HTMLResponse(_status_page(settings))

    # Assets last, and only the assets. `StaticFiles` mounted at `/` would answer a
    # mistyped API path with `index.html`, turning "no such route" into a 200 and an
    # empty screen.
    static = Path(settings.static_dir)
    if static.is_dir():
        app.mount("/assets", StaticFiles(directory=static / "assets"), name="assets")

    return app


def _status_page(settings: Settings) -> str:
    """The page served when no dashboard bundle is present. Someone who runs `docker
    compose up` before reading the README should get something that tells them what
    to do — a 404, an exited container, or an empty React shell all read as
    "broken", which is why this isn't deleted now that a bundle exists."""
    db = check_database(settings)
    missing = settings.missing_requirements()

    if missing:
        items = "".join(f"<li><code>{m}</code></li>" for m in missing)
        banner = (
            '<div class="warn"><b>Missing configuration.</b> The system started '
            "and its infrastructure and test suite are available. Extraction and "
            "adjudication are disabled until you set them in "
            f"<code>.env</code> and restart:<ul>{items}</ul></div>"
        )
    else:
        banner = '<div class="ok"><b>Fully configured.</b> No missing keys.</div>'

    if db["reachable"]:
        dbline = (
            f'<div class="ok">Database reachable — <b>{db["migrations_applied"]}</b> '
            f"migrations applied, <b>{db['tables']}</b> tables.</div>"
        )
    else:
        dbline = (
            f'<div class="warn"><b>Database unreachable.</b> <code>{db["error"]}</code><br>'
            "If this is a fresh start, the <code>migrate</code> service may still "
            "be running — give it a moment and reload.</div>"
        )

    return f"""<!doctype html><meta charset="utf-8">
<title>{brand.NAME}</title>
<link rel="icon" href="{brand.ICON_DATA_URI}">
<style>
 body{{font:15px/1.6 system-ui,-apple-system,sans-serif;max-width:44rem;
  margin:3rem auto;padding:0 1.25rem;color:#16181d}}
 h1{{font-size:1.6rem;margin:0 0 .3rem}}
 .sub{{color:#5b6069;margin:0 0 1.5rem}}
 .ok,.warn{{padding:.8rem 1rem;border-radius:4px;margin:.6rem 0;
       border-left:3px solid}}
 .ok{{background:#e8f3ee;border-color:#1d6b4f}}
 .warn{{background:#fff4e0;border-color:#8a5a00}}
 code{{background:#f2f3f5;padding:.05em .3em;border-radius:3px;font-size:.9em}}
 ul{{margin:.5rem 0 0 1.1rem}} li{{margin:.2rem 0}}
 footer{{margin-top:2rem;color:#5b6069;font-size:.9rem;
    border-top:1px solid #e2e5ea;padding-top:.8rem}}
</style>
<h1>{brand.NAME}</h1>
<p class="sub">Phase {PHASE}. The dashboard is not built — run <code>make ui-build</code>, or <code>make ui</code> for the dev server.</p>
{dbline}
{banner}
<p>The whole workflow is reachable over HTTP now — <a href="/docs">/docs</a> is
generated from the routes and is the fastest way to drive it without a UI:
<code>/piles</code>, then that pile's <code>/deliverable</code>,
<code>/decisions</code>, <code>/citations</code> and <code>/events</code>.</p>
<footer>
 <a href="/health">/health</a> &middot; <a href="/docs">/docs</a> &middot;
 <a href="/piles">/piles</a><br>
 Drop a folder into <code>watched/</code> and it becomes a pile — no setup step.
</footer>"""
