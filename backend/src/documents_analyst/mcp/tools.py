"""Print the tool surface, without needing an MCP client.

    python -m documents_analyst.mcp.tools

Two reasons it exists rather than "connect a client and see": a reviewer should be able to
read what an agent is offered — including which tools are read-only — in one command,
before pointing a model at their contracts; and on stdio the protocol IS stdout, so the
running server can't print anything itself. It touches no database and calls no API — the
tool surface is a property of the server, and this is the server describing itself.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from documents_analyst.mcp.api import machine_actor
from documents_analyst.mcp.server import build_server
from documents_analyst.settings import get_settings


async def describe() -> list[dict[str, Any]]:
    settings = get_settings()
    server = build_server(settings)
    return [
        {
            "name": tool.name,
            "read_only": bool(tool.annotations and tool.annotations.read_only_hint),
            "arguments": sorted((tool.input_schema.get("properties") or {}).keys()),
            "required": sorted(tool.input_schema.get("required") or []),
            "description": (tool.description or "").strip().splitlines()[0],
        }
        for tool in await server.list_tools()
    ]


def main() -> int:
    settings = get_settings()
    tools = asyncio.run(describe())

    print(f"analyst MCP server — {len(tools)} tools")
    print(f"  API      {settings.mcp_api_base_url}")
    print(f"  actor    {machine_actor(settings.mcp_actor)}   (written into decided_by)")
    print(f"  token    {'set' if settings.api_token else 'not set — the API is open'}")
    print()
    for tool in tools:
        mark = "read " if tool["read_only"] else "WRITE"
        required = ", ".join(tool["required"]) or "—"
        print(f"  {mark}  {tool['name']:<20} requires: {required}")
        print(f"         {tool['description']}")
    print()
    print(json.dumps(tools, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
