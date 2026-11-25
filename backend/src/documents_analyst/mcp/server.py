"""Seven composed tools, over stdio and HTTP.

A separate process and an HTTP client of our own API: importing the handlers would make
"machine callers go through the same gates as the browser" a convention, where as an HTTP
client it's structural — there is no other path, and a test walks this package's source to
keep it that way.

The tools are composed, not a mirror of the routes: `get_review_queue` returns each item
with its evidence (an agent that must make a second call decides without it), and
`get_final_document` hands back the verified .docx the pipeline wrote.

`approve_item` takes a `confirm` string that must appear verbatim in the item's own
evidence — an agent looping over ids can't pass it, one that read the conflict can. A
mismatch returns the real evidence, which is the mechanism working, not a hole. `reject_item`
needs no confirmation — rejecting commits nothing.

See docs/BACKEND_INTERNALS.md § "Approving requires quoting the evidence".
"""

from __future__ import annotations

import argparse
import logging
import re
import secrets
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from documents_analyst.mcp.api import AnalystApi, ApiError
from documents_analyst.settings import Settings, get_settings

LOG = logging.getLogger("documents_analyst.mcp")

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
DECIDES = ToolAnnotations(read_only_hint=False, idempotent_hint=True, open_world_hint=False)
"""`idempotent_hint` because a repeated decision doesn't decide twice — the API answers 409
with who decided it first. `destructive_hint` is left unset: an approval changes the final
document, but through a queue a human filled."""


def build_server(settings: Settings, api: AnalystApi | None = None) -> MCPServer:
    """The seven tools, bound to one API client. `api` is injectable so the test suite can
    hand it a client speaking to the FastAPI app in-process — still HTTP, with real status
    codes and headers, without needing a socket."""
    client = api or AnalystApi(settings)
    server: MCPServer = MCPServer(
        name="documents-analyst",
        version="0.7.0",
        instructions=(
            "Read-only tools describe a pile of documents: its register, the "
            "review queue, and the exact source span behind every value. "
            "approve_item and reject_item settle one review item as a machine "
            "actor; approving requires quoting the item's evidence back. "
            "get_final_document returns the generated .docx, read back and verified."
        ),
    )

    @server.tool(annotations=READ_ONLY)
    async def list_piles() -> dict[str, Any]:
        """Every pile of contracts, with what is waiting in each.

        A pile is a watched folder; `pending_decisions` is what still needs answering,
        `open_conflicts` is how many terms two documents disagree about.
        """
        piles = await client.piles()
        return {
            "piles": [
                {
                    "pile_id": p["id"],
                    "name": p["name"],
                    "status": p["status"],
                    "documents": p["documents"],
                    "pending_decisions": p["pending_decisions"],
                    "open_conflicts": p["open_conflicts"],
                    "open_findings": p["open_findings"],
                    "register_version": p["deliverable_version"],
                }
                for p in piles
            ],
            "count": len(piles),
        }

    @server.tool(annotations=READ_ONLY)
    async def get_register(
        pile_id: Annotated[str, Field(description="From list_piles")],
    ) -> dict[str, Any]:
        """The contract register: every material term and what it says.

        A disputed term comes back with `state: disputed`, no current value, and every
        competing value — the register never picks a side; that's what the review queue is
        for.
        """
        register = await client.register(pile_id)
        slots = register.get("slots") or []
        return {
            "pile_id": pile_id,
            "register_version": register.get("version"),
            "terms": [
                {
                    "label": s["label"],
                    "state": s["state"],
                    "value": (s["current"] or {}).get("text"),
                    "sources": (s["current"] or {}).get("citation_count", 0),
                    "competing_values": (
                        [{"value_group": v["value_group"], "text": v["text"]} for v in s["values"]]
                        if s["current"] is None
                        else []
                    ),
                }
                for s in slots
            ],
            "disputed": sum(1 for s in slots if s["state"] in ("disputed", "contested")),
        }

    @server.tool(annotations=READ_ONLY)
    async def get_review_queue(
        pile_id: Annotated[str, Field(description="From list_piles")],
    ) -> dict[str, Any]:
        """What is waiting for a decision, with the evidence behind each item (returned here
        because `approve_item` requires quoting it back).

        Approving a `conflict` requires naming which value wins by its `value_group`, so each
        item's `options` carries the group, the value as written, and the documents that say
        it. An `amendment` carries `recommended_value_group` (the later document supersedes);
        approving without a group accepts it.

        Kinds: `conflict`, `amendment`, `finding` (a rulebook rule failed), `label_change`,
        `orphaned_claims`.
        """
        queue = await client.queue(pile_id)
        return {
            "pile_id": pile_id,
            "pending": queue.get("pending", 0),
            "items": [
                {
                    "decision_id": str(item["id"]),
                    "kind": item["kind"],
                    "relationship": item["relationship"],
                    "detected_by": item["detected_by"],
                    "recommendation": item["recommendation"],
                    "evidence": _evidence(item),
                    "group_key": item["group_key"],
                    "field": (item.get("payload") or {}).get("field_key"),
                    "recommended_value_group": (item.get("payload") or {}).get(
                        "recommended_value_group"
                    ),
                    "options": _options(item),
                }
                for item in queue.get("items") or []
            ],
        }

    @server.tool(annotations=READ_ONLY)
    async def get_citations(
        pile_id: Annotated[str, Field(description="From list_piles")],
        field: Annotated[str | None, Field(description="Filter, e.g. 'payment_terms_days'")] = None,
    ) -> dict[str, Any]:
        """Every claim in the register with the exact source span proving it. `char_start`/
        `char_end` index into that document's extracted text, and the quote was verified
        verbatim there — a claim whose quote wasn't in its document was dropped, so
        everything here is checkable.
        """
        body = await client.citations(pile_id)
        rows = body.get("citations") or []
        if field:
            rows = [r for r in rows if field in (r.get("field_key") or r.get("subject_free") or "")]
        return {
            "pile_id": pile_id,
            "count": len(rows),
            "citations": [
                {
                    "field": r.get("field_key") or r.get("subject_free"),
                    "value": (r.get("value") or {}).get("text"),
                    "quote": r["source_quote"],
                    "document": r["filename"],
                    "document_id": str(r["source_document_id"]),
                    "char_start": r["char_start"],
                    "char_end": r["char_end"],
                }
                for r in rows
            ],
        }

    @server.tool(annotations=DECIDES)
    async def approve_item(
        decision_id: Annotated[str, Field(description="From get_review_queue")],
        confirm: Annotated[
            str,
            Field(
                description=(
                    "A verbatim quote from this item's evidence, proving it was read. "
                    "Copy it from get_review_queue."
                )
            ),
        ],
        note: Annotated[str | None, Field(description="Why, for the audit trail")] = None,
        chosen_value_group: Annotated[
            int | None,
            Field(
                description=(
                    "Which value wins, by `value_group` from get_review_queue's `options`. "
                    "REQUIRED for a conflict. Optional for an amendment, which defaults to "
                    "`recommended_value_group` — the later document."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Approve one review item, quoting its evidence back. The only door to changing the
        register — `Commit` acts on approved items and nothing else — and recorded against
        this machine's actor, never a person's.
        """
        item = await client.decision(decision_id)
        if item["status"] != "pending":
            return {
                "approved": False,
                "reason": f"already {item['status']}"
                + (f" by {item['decided_by']}" if item.get("decided_by") else ""),
            }

        evidence = _evidence(item)
        if not _confirms(evidence, confirm, minimum=settings.mcp_confirm_min_chars):
            raise ApiError(
                "the `confirm` text is not a verbatim quote from this item's evidence, so "
                "the approval was refused. Read the evidence and quote at least "
                f"{settings.mcp_confirm_min_chars} characters of it back:\n\n{evidence}"
            )

        settled = await client.settle(
            decision_id, approve=True, note=note, chosen_value_group=chosen_value_group
        )
        decision = settled.get("decision") or {}
        return {
            "approved": True,
            "decision_id": decision_id,
            "decided_by": decision.get("decided_by"),
            "kind": decision.get("kind"),
            "note": "committed to the register by the worker on its next pass",
        }

    @server.tool(annotations=DECIDES)
    async def reject_item(
        decision_id: Annotated[str, Field(description="From get_review_queue")],
        note: Annotated[str | None, Field(description="Why, for the audit trail")] = None,
    ) -> dict[str, Any]:
        """Reject one review item. No confirmation needed — rejecting commits nothing, and
        requiring proof-of-reading would only make the cautious answer the expensive one.
        """
        item = await client.decision(decision_id)
        if item["status"] != "pending":
            return {
                "rejected": False,
                "reason": f"already {item['status']}"
                + (f" by {item['decided_by']}" if item.get("decided_by") else ""),
            }
        settled = await client.settle(decision_id, approve=False, note=note)
        decision = settled.get("decision") or {}
        return {
            "rejected": True,
            "decision_id": decision_id,
            "decided_by": decision.get("decided_by"),
        }

    @server.tool(annotations=READ_ONLY)
    async def get_final_document(
        pile_id: Annotated[str, Field(description="From list_piles")],
    ) -> dict[str, Any]:
        """The pile's final document (.docx), as the last commit wrote it, verified.

        The document is written by the pipeline after review, not on request, so this
        reads it rather than producing it: it returns the version, its SHA-256, and the
        result of reading the file back and comparing every section to what was recorded.
        `ready: false` means nothing has been committed for this pile yet.
        """
        try:
            check = await client.verify_document(pile_id)
        except ApiError as exc:
            if exc.status == 409:
                return {"ready": False, "reason": "nothing has been committed for this pile yet"}
            raise
        return {
            "ready": True,
            "filename": settings.deliverable_filename,
            "version": check.get("version"),
            "document_sha256": check.get("document_sha256"),
            "verified": check.get("verified"),
            "problems": check.get("problems") or [],
            "sections": len(check.get("sections") or []),
            # `resource_uri` always works — read back over the MCP session, and the ONLY path
            # on an `API_TOKEN` instance, where following the URL gets a 401.
            "resource_uri": f"document://{pile_id}",
            # `public_api_url`, not the address this process dials: in compose the MCP server
            # reaches the API at `http://api:8000`, unreachable from the host.
            "download_url": (
                f"{settings.public_api_url.rstrip('/')}/piles/{pile_id}/deliverable/document"
            ),
            "download_url_needs_api_token": bool(settings.api_token),
        }

    @server.resource(
        "document://{pile_id}",
        name="Final document",
        description="The pile's final .docx, fetched with the server's own API credential.",
        mime_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    )
    async def final_document(pile_id: str) -> bytes:
        """The file, over the channel the caller is already authenticated on. On an
        `API_TOKEN` instance the download URL is unusable to a client (it holds `MCP_TOKEN`,
        not the API credential); reading the resource routes back through this process, which
        holds it."""
        payload, _ = await client.document_bytes(pile_id)
        return payload

    return server


def _options(item: dict[str, Any]) -> list[dict[str, Any]]:
    """The competing values, with the group numbers an approval names. Straight from the
    decision's payload, which the classifier wrote when it raised the item — so the numbers
    an agent is shown are the numbers stored on the facts, and a choice can't land on a
    different value than the one it read. Sources are trimmed to what identifies them; the
    full spans are in `get_citations`."""
    return [
        {
            "value_group": option.get("value_group"),
            "value": option.get("value_text"),
            "normalized": option.get("value_norm"),
            # Distinct filenames. One entry per citation arrives here, so a contract stating
            # the same figure twice listed the file twice — an agent reading `said_by` would
            # count two sources where there's one document. `quotes` below keeps every one.
            "said_by": list(
                dict.fromkeys(source.get("filename") for source in option.get("sources") or [])
            ),
            "quotes": [source.get("quote") for source in option.get("sources") or []],
        }
        for option in (item.get("payload") or {}).get("options") or []
    ]


def _evidence(item: dict[str, Any]) -> str:
    """What an agent has to have read: the quote if there is one, else the recommendation.
    `evidence_quote` is a span from a real contract, the strongest thing to demand. A rulebook
    `finding` has no quote — the violation is the absence of a clause — so its recommendation
    stands in, still text that only reading the item produces."""
    for key in ("evidence_quote", "recommendation"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return f"{item.get('kind', 'item')} {item.get('id', '')}".strip()


def _confirms(evidence: str, confirm: str, *, minimum: int) -> bool:
    """Exact substring, on collapsed whitespace, and nothing cleverer. Whitespace is
    normalised because a quote copied out of a tool result may have been re-wrapped; every
    other character must match. Fuzzy matching would make this a similarity score, and a
    threshold is exactly the guess this check exists to remove."""
    haystack = _collapse(evidence)
    needle = _collapse(confirm)
    if not needle:
        return False
    if len(needle) < min(minimum, len(haystack)):
        return False
    return needle in haystack


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class RequireToken:
    """Inbound bearer auth for the HTTP transport. The MCP server is a confused deputy
    without this: it holds `API_TOKEN` and presents it on every downstream call, so an
    unauthenticated port here lets anyone who reaches it approve register changes using our
    credential — the API's own guard never sees them. Locking the API and leaving this open is
    strictly worse than leaving both open, because it looks protected. A single shared secret,
    matching the API's model; `compare_digest` because a plain `==` leaks the prefix through
    timing. stdio needs none of this — the transport IS the process."""

    def __init__(self, app: Any, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        supplied = headers.get("authorization", "")
        offered = supplied[7:].strip() if supplied.lower().startswith("bearer ") else ""
        if not offered or not secrets.compare_digest(offered, self._token):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b'{"error":"MCP_TOKEN is required on this instance"}',
                }
            )
            return
        await self._app(scope, receive, send)


def http_app(settings: Settings, server: MCPServer) -> Any:
    """The streamable-HTTP ASGI app, wrapped in inbound auth when configured."""
    app = server.streamable_http_app(host=settings.mcp_host)
    if settings.mcp_token:
        return RequireToken(app, settings.mcp_token)
    return app


def refuse_unauthenticated_public_bind(settings: Settings) -> str | None:
    """Why this instance must not start, or `None`. The one indefensible combination: a bind
    that isn't loopback, no `MCP_TOKEN`, and an `API_TOKEN` this process would spend on a
    stranger's behalf. Refusing at startup rather than warning, because the failure is silent
    — everything works, and the protection somebody thought they had is simply absent. An open
    API with an open MCP port is allowed: the port grants nothing the API doesn't already."""
    if not settings.mcp_binds_publicly or settings.mcp_token or not settings.api_token:
        return None
    return (
        f"refusing to serve MCP on {settings.mcp_host}:{settings.mcp_port} with no MCP_TOKEN "
        "while API_TOKEN is set — anyone who reached this port could approve register "
        "changes using the API credential this process holds. Set MCP_TOKEN, or bind to "
        "127.0.0.1 (in a container, keep MCP_HOST=0.0.0.0 and publish to 127.0.0.1)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="The analyst's MCP server.")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=None,
        help="overrides MCP_TRANSPORT; stdio for `claude mcp add`",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    # stderr, never stdout: on the stdio transport, stdout IS the protocol, and one stray
    # log line makes the client fail to parse a frame.
    logging.basicConfig(level=settings.log_level, format="%(levelname)s %(message)s")

    transport = args.transport or settings.mcp_transport
    server = build_server(settings)

    if transport == "stdio":
        LOG.info("MCP server on stdio, talking to %s", settings.mcp_api_base_url)
        server.run(transport="stdio")
        return 0

    refusal = refuse_unauthenticated_public_bind(settings)
    if refusal:
        LOG.error("%s", refusal)
        return 2

    import uvicorn

    LOG.info(
        "MCP server on http://%s:%s/mcp (%s), talking to %s",
        settings.mcp_host,
        settings.mcp_port,
        "token required" if settings.mcp_token else "no inbound token",
        settings.mcp_api_base_url,
    )
    uvicorn.run(
        http_app(settings, server),
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
