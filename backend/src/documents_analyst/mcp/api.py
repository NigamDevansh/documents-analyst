"""The MCP server's client for our own API.

The whole architectural claim: the MCP server reaches the system only over HTTP, so
"machine callers go through the same gates as the browser" is structural, not a convention
— importing `documents_analyst.db` here would quietly make it one, which is why a test walks this
package's source and fails on any import of the database or SQLAlchemy. So every API
guarantee applies to an agent unchanged (a decision needs a name, a decided item answers
409, only an approval reaches `Commit`).

Errors are messages, not tracebacks: a tool result enters a model's context, so `ApiError`
carries the API's own `detail` and `_explain` turns the statuses that mean something
specific into a sentence a caller can act on.

See docs/BACKEND_INTERNALS.md § "MCP server".
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from documents_analyst.settings import Settings

LOG = logging.getLogger(__name__)


class ApiError(RuntimeError):
    """A refusal from our own API, phrased for whoever called the tool."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AnalystApi:
    """Thin, typed-enough calls to the routes the seven tools compose. One
    `httpx.AsyncClient` for the process: a client per call would re-open a connection for
    every tool invocation, and an agent working through a review queue makes a lot."""

    def __init__(
        self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._settings = settings
        self._actor = machine_actor(settings.mcp_actor)
        # A transport rather than a whole client, so base URL, timeout and the token header
        # are always built from `Settings`, including under test. Injecting a finished
        # client would let the suite exercise a differently configured one than deployment.
        self._client = httpx.AsyncClient(
            base_url=settings.mcp_api_base_url.rstrip("/"),
            timeout=settings.mcp_timeout_s,
            headers=self._auth_headers(settings),
            transport=transport,
        )

    @staticmethod
    def _auth_headers(settings: Settings) -> dict[str, str]:
        """The token is sent only when there is one, so an open instance stays
        open and a locked one gets a real credential rather than the word
        `None`."""
        if not settings.api_token:
            return {}
        return {"Authorization": f"Bearer {settings.api_token}"}

    @property
    def actor(self) -> str:
        """What this process writes into `decided_by`. Always machine-tagged."""
        return self._actor

    async def aclose(self) -> None:
        await self._client.aclose()

    # ── reads ─────────────────────────────────────────────────────────────

    async def piles(self) -> list[dict[str, Any]]:
        body = await self._get("/piles")
        return list(body.get("piles") or [])

    async def pile(self, pile_id: str) -> dict[str, Any]:
        return await self._get(f"/piles/{pile_id}")

    async def register(self, pile_id: str) -> dict[str, Any]:
        return await self._get(f"/piles/{pile_id}/deliverable")

    async def queue(self, pile_id: str) -> dict[str, Any]:
        return await self._get(f"/piles/{pile_id}/decisions")

    async def decision(self, decision_id: str) -> dict[str, Any]:
        return await self._get(f"/decisions/{decision_id}")

    async def citations(self, pile_id: str) -> dict[str, Any]:
        return await self._get(f"/piles/{pile_id}/citations")

    async def findings(self, pile_id: str) -> dict[str, Any]:
        return await self._get(f"/piles/{pile_id}/findings")

    async def verify_document(self, pile_id: str) -> dict[str, Any]:
        return await self._get(f"/piles/{pile_id}/deliverable/verify")

    async def document_bytes(self, pile_id: str) -> tuple[bytes, str]:
        """The file itself, fetched with OUR credential. This is what makes an
        authenticated instance usable from an agent: a client holding `MCP_TOKEN` doesn't
        hold `API_TOKEN`, so following the download link gets a 401. Reading it back
        through the MCP session means the caller uses the channel it's already
        authenticated on, and the API credential never leaves this process."""
        response = await self._send("GET", f"/piles/{pile_id}/deliverable/document")
        return response.content, response.headers.get("content-type", "application/octet-stream")

    # ── writes ────────────────────────────────────────────────────────────

    async def settle(
        self,
        decision_id: str,
        *,
        approve: bool,
        note: str | None = None,
        chosen_value_group: int | None = None,
    ) -> dict[str, Any]:
        """Approve or reject one item, as this machine. `X-Actor` is set here and nowhere
        else, so no tool argument can dress a machine decision up as a person's."""
        return await self._post(
            f"/decisions/{decision_id}/{'approve' if approve else 'reject'}",
            json={"note": note, "chosen_value_group": chosen_value_group},
            headers={"X-Actor": self._actor},
        )

    # ── transport ─────────────────────────────────────────────────────────

    async def _get(self, path: str) -> dict[str, Any]:
        return self._body(await self._send("GET", path))

    async def _post(
        self, path: str, *, json: dict[str, Any], headers: dict[str, str]
    ) -> dict[str, Any]:
        return self._body(await self._send("POST", path, json=json, headers=headers))

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise ApiError(
                f"cannot reach the analyst API at {self._settings.mcp_api_base_url} "
                f"({type(exc).__name__}). Is it running? `make api`, or "
                "`docker compose --profile full up`."
            ) from exc
        if response.is_success:
            return response
        raise ApiError(_explain(response), status=response.status_code)

    @staticmethod
    def _body(response: httpx.Response) -> dict[str, Any]:
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {"result": parsed}


def machine_actor(configured: str) -> str:
    """`decided_by`, always identifiable as a machine. The prefix is forced, not trusted:
    an operator who sets `MCP_ACTOR=devansh` — reasonably, meaning "the agent I'm running" —
    would otherwise put approvals in the audit trail indistinguishable from ones a person
    made in the UI, and that distinction is the entire reason the column isn't just
    "someone"."""
    name = (configured or "").strip() or "agent"
    return name if name.startswith("mcp:") else f"mcp:{name}"


def _explain(response: httpx.Response) -> str:
    """The API's own `detail`, plus what to do about it where that is knowable."""
    try:
        detail = str(response.json().get("detail") or "").strip()
    except ValueError:
        detail = response.text.strip()[:200]

    hint = {
        401: " — this instance has API_TOKEN set; give the MCP process the same value",
        404: "",
        409: "",
        410: " — the file is missing from the pile folder; the next run restores it",
        422: "",
        503: " — the analyst is running but is missing configuration for this",
    }.get(response.status_code, "")
    return f"{response.status_code}: {detail or response.reason_phrase}{hint}"
