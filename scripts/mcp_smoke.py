#!/usr/bin/env python3
"""End-to-end smoke test for the Conjure MCP server.

Spawns the MCP server over stdio (as an MCP client would), lists its tools, and
calls a few — then you can check GET /world to confirm the edits landed.

Requires the world server running:  python -m conjure   (in another terminal)
Run:  python scripts/mcp_smoke.py
"""

import asyncio

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _text(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


async def main() -> None:
    params = StdioServerParameters(command="python", args=["-m", "conjure.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", [t.name for t in tools.tools])

            print("add ->", _text(await session.call_tool(
                "add_entity", {"shape": "sphere", "color": "gold", "position": [-1, 1.2, -3]})))
            print("env ->", _text(await session.call_tool(
                "set_environment", {"sky_color": "#0b1a2a", "fog_color": "#0b1a2a", "fog_density": 0.03})))
            print("move ->", _text(await session.call_tool(
                "move_entity", {"id": "pillar", "position": [1, 1, -4]})))
            print("query ->\n" + _text(await session.call_tool("query_world", {})))


if __name__ == "__main__":
    asyncio.run(main())
