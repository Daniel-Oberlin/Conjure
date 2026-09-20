#!/usr/bin/env python3
"""What each agent sends the model before anyone says anything — the static half of the turn.

`Director.context_stats()` reports this for a LIVE turn, which needs a running world server and a
session. This is the part that can be known without either: the tool schemas an agent is granted, and
its prompt. Those two are fixed at load and, for the builder agent, are most of the turn.

Why it exists: the tool block is usually the largest slice of context and the least visible one —
nothing in the conversation shows it, so it grows without anyone deciding to grow it. This prints it
per agent, and again per tool, so a scoping change has a number to move.

    python scripts/context_budget.py             # every shipped agent
    python scripts/context_budget.py --tools     # + the per-tool ranking for the widest agent

Chars, not tokens: every provider tokenizes differently, and `_tools_chars` counts chars for the same
reason. The ~tokens column is chars/4 — an estimate, and labelled as one.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conjure.agents import agent_names, load_agent  # noqa: E402


def _size(tool) -> int:
    """Name + description + input_schema — the three fields every adapter in `llm.py` sends."""
    return (len(tool.name or "") + len(tool.description or "")
            + len(json.dumps(tool.inputSchema or {}, separators=(",", ":"))))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", action="store_true", help="also rank tools by size")
    args = ap.parse_args()

    from conjure import mcp_server

    tools = await mcp_server.mcp.list_tools()
    # Resources, not just tools. An agent's `context` list is the OTHER place a renamed capability
    # hides, and it fails more quietly: `Director._fetch_context` skips a resource it cannot read
    # (`except Exception: continue`), so a stale URI costs the agent its live scene with no error
    # anywhere. Found the hard way — `room://current` outlived the rename that produced
    # `space://current`, and an audit that only walked tools did not see it.
    resources = {str(r.uri) for r in await mcp_server.mcp.list_resources()}
    by_name = {t.name: t for t in tools}
    everything = sum(_size(t) for t in tools)

    print(f"\n{len(tools)} tools on the world server — {everything:,} chars (~{everything // 4:,} tokens)\n")
    print(f"{'agent':<12} {'tools':>6} {'tool chars':>12} {'prompt':>8} {'static total':>13} {'~tokens':>9}")
    print("-" * 64)

    widest: tuple[int, list[str]] = (0, [])
    for name in agent_names():
        try:
            agent = load_agent(name)
        except Exception as e:                       # a broken agent def must not hide the others
            print(f"{name:<12} {'—':>6}  could not load: {type(e).__name__}: {e}")
            continue
        granted = [n for ref in agent.servers for n in ref.tools]
        chars = sum(_size(by_name[n]) for n in granted if n in by_name)
        prompt = len(agent.prompt or "")
        total = chars + prompt
        print(f"{name:<12} {len(granted):>6} {chars:>12,} {prompt:>8,} {total:>13,} {total // 4:>9,}")
        if chars > widest[0]:
            widest = (chars, granted)

    for name in agent_names():
        try:
            agent = load_agent(name)
        except Exception:
            continue
        gone = [n for ref in agent.servers for n in ref.tools if n not in by_name]
        stale = [u for u in (agent.context or []) if u not in resources]
        if gone:
            # Dead weight in the agent def rather than context — it costs nothing but grants nothing.
            print(f"\n  {name}: granted but not on the server: {', '.join(sorted(gone))}")
        if stale:
            # Worse than dead weight: the agent silently loses that slice of its live context.
            print(f"\n  {name}: context resource does not exist: {', '.join(stale)}"
                  f"\n{' ' * 4}(available: {', '.join(sorted(resources))})")

    if args.tools and widest[1]:
        print(f"\nthe widest agent's tools, largest first:\n")
        ranked = sorted((n for n in widest[1] if n in by_name), key=lambda n: -_size(by_name[n]))
        for n in ranked:
            size = _size(by_name[n])
            print(f"  {size:>6,}  {size * 100 // widest[0]:>2}%  {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
