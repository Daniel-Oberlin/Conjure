#!/usr/bin/env python3
"""A/B the Anthropic tool-search flag on real director turns — docs/plans/local-llms.md §5a.

    python scripts/tool_search_ab.py                 # the builder agent, both ways
    python scripts/tool_search_ab.py --agent outdoor --turns 3

Answers one question: **what does shipping 56 tool schemas on every turn actually cost us, and does
deferring them change which tools the director reaches for?**

**No tool is executed.** `execute_tool` is a recording stub that returns a plausible success string.
That is deliberate on two counts. What is being measured is which tools Claude CHOOSES and what the
request costs — the world's response is not part of either. And the world server runs on live data
that the repo shares across branches, so a measurement run must not be able to edit a real world.

The consequence to keep in mind when reading the output: tool RESULTS are synthetic, so a turn's later
hops are steered by a stub rather than by what the world would really have said. Hop 1's tool choice is
the trustworthy part; a long chain is indicative, not authoritative.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conjure.agents import load_agent                                      # noqa: E402
from conjure.config import get_settings                                    # noqa: E402
from conjure.director import _scope_tools                                  # noqa: E402
from conjure.llm import ClaudeLLM, ToolSpec                                # noqa: E402

#: Requests that between them reach into different corners of the tool list — a surface, an asset, the
#: sky, a query, a figure. A scoping change that saves tokens by hiding the tool one of these needed is
#: the failure this A/B exists to catch, so a narrow script would defeat the point.
TURNS = [
    "Put a picture of a misty pine forest on the north wall.",
    "Make the sky a clear blue evening and dim the lights a little.",
    "What's in the world right now?",
    "Add a wooden chair near the window and turn it to face the middle of the room.",
    "Show me the edges of the real surfaces so I can see what you detected.",
]


async def one_turn(llm: ClaudeLLM, system: str, tools: list[ToolSpec], text: str) -> dict:
    called: list[str] = []

    async def execute_tool(name: str, args: dict) -> str:
        called.append(name)
        return json.dumps({"ok": True, "note": "recorded by tool_search_ab.py; nothing was executed"})

    async def emit(_text: str, *, final: bool = False) -> None:
        return None

    t0 = time.time()
    try:
        await llm.run_turn(system=system, history=[], user_text=text,
                           tools=tools, execute_tool=execute_tool, emit=emit)
        error = ""
    except Exception as e:                                   # a failed turn is a result, not a crash
        error = f"{type(e).__name__}: {e}"
    elapsed = time.time() - t0
    usage = llm.last_usage
    return {
        "tools": called,
        "hops": len(usage),
        "input": sum(u["input"] for u in usage),
        "output": sum(u["output"] for u in usage),
        "cache_read": sum(u["cache_read"] for u in usage),
        "first_input": usage[0]["input"] if usage else 0,
        "seconds": elapsed,
        "error": error,
    }


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", default="builder")
    ap.add_argument("--turns", type=int, default=len(TURNS))
    ap.add_argument("--always", default="query_world,query_space")
    args = ap.parse_args()

    settings = get_settings()
    if not settings.anthropic_api_key:
        print("no ANTHROPIC_API_KEY — nothing to measure")
        return 2

    from conjure import mcp_server

    agent = load_agent(args.agent)
    live = await mcp_server.mcp.list_tools()
    granted = [n for ref in agent.servers for n in ref.tools]
    scoped = _scope_tools(live, granted)
    tools = [ToolSpec(t.name, t.description or "", t.inputSchema or {}) for t in scoped]
    # The live `{context}` injection needs a running world server; this measures the static prompt, so
    # the placeholder is left as-is. Both arms see the identical system prompt, which is what matters.
    system = agent.prompt or ""

    schema_chars = sum(len(t.name) + len(t.description) + len(json.dumps(t.input_schema)) for t in tools)
    print(f"\nagent {args.agent}: {len(tools)} tools, {schema_chars:,} chars of schema "
          f"(~{schema_chars // 4:,} tokens), prompt {len(system):,} chars")
    print(f"model {settings.llm_model}\n")

    rows = []
    for label, on in (("off", False), ("on", True)):
        llm = ClaudeLLM("Claude", settings.anthropic_api_key, settings.llm_model)
        if on:
            class _S:
                tool_search = True
                tool_search_kind = "regex"
                tool_search_always = args.always
            llm.configure(_S())
        for text in TURNS[:args.turns]:
            r = await one_turn(llm, system, tools, text)
            r["arm"], r["text"] = label, text
            rows.append(r)
            print(f"  [{label:>3}] {r['input']:>7,} in  {r['seconds']:>5.1f}s  "
                  f"hops={r['hops']}  {','.join(r['tools']) or '(none)'}"
                  + (f"  ERROR {r['error']}" if r["error"] else ""))

    def tot(arm, key):
        return sum(r[key] for r in rows if r["arm"] == arm)

    print(f"\n{'':<6}{'input tok':>12}{'first hop':>12}{'output':>9}{'seconds':>10}")
    for arm in ("off", "on"):
        print(f"{arm:<6}{tot(arm,'input'):>12,}{tot(arm,'first_input'):>12,}"
              f"{tot(arm,'output'):>9,}{tot(arm,'seconds'):>10.1f}")
    saved = tot("off", "input") - tot("on", "input")
    pct = (saved * 100 // tot("off", "input")) if tot("off", "input") else 0
    print(f"\ninput tokens saved: {saved:,} ({pct}%)   "
          f"wall clock: {tot('on','seconds') - tot('off','seconds'):+.1f}s")

    # The half that decides whether the saving was worth anything.
    print("\nsame tools chosen per turn?")
    half = len(rows) // 2
    for a, b in zip(rows[:half], rows[half:]):
        mark = "same" if a["tools"] == b["tools"] else "DIFFERENT"
        print(f"  {mark:>9}  {a['text'][:44]:<46} off={a['tools']} on={b['tools']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
