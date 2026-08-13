"""Thin client of the agent server (shared-session Step C1) — the pieces voice/CLI share to submit turns
and render the shared conversation stream. The heavy state (Director, transcript, LLM roster) lives in the
agent server; a client only POSTs a turn and renders SSE events.

The parsing/formatting is **pure** (no I/O) so it's unit-testable; the two coroutines (`post_turn`,
`stream_events`) are the only network."""

from __future__ import annotations

import json
from typing import AsyncIterator, Optional


# --------------------------------------------------------------------------- pure helpers

def parse_sse_line(line: str) -> Optional[dict]:
    """Parse one SSE line into an event dict, or None for comments/blanks/non-`data:` lines and malformed
    JSON. (Keeps the network loop dumb: feed it raw lines.)"""
    if not line or not line.startswith("data:"):
        return None
    payload = line[len("data:"):].strip()
    if not payload:
        return None
    try:
        return json.loads(payload)
    except ValueError:
        return None


def prompt_from_context(ctx: dict) -> str:
    """The REPL prompt, from the latest `context` event: `conjure:shell>` in shell mode, else
    `conjure:<user>.<agent>.<llm>>` (who you are · the live agent · the LLM running it). Mirrors the old
    in-process `Shell.prompt`, now driven by the stream (shared-session-plan §8)."""
    if ctx.get("in_shell"):
        return "conjure:shell> "
    user = ctx.get("user") or "you"
    agent = ctx.get("agent") or "agent"
    llm = (ctx.get("llm") or "?").lower()
    return f"conjure:{user}.{agent}.{llm}> "


def render_event(ev: dict, *, me: str, verbose: bool) -> Optional[str]:
    """The line to print for a stream event (or None to print nothing). `me` is the local user, so we
    don't echo our own submitted turns (but we DO show other speakers — one shared conversation).
    `context` returns None (the caller folds it into its ctx for the prompt)."""
    t = ev.get("type")
    if t == "user_turn":
        spk = ev.get("speaker")
        return f"{spk}: {ev.get('text', '')}" if spk and spk != me else None
    if t in ("assistant_delta", "assistant_final", "notice"):
        return ev.get("text") or None
    if t == "busy":
        return "[busy — another turn is already in progress]"
    if t == "tool_call" and verbose:
        return f"  · {ev.get('name')}({json.dumps(ev.get('args', {}))})"
    return None                                   # context / non-verbose tool_call / unknown → silent


def apply_context(ctx: dict, ev: dict) -> None:
    """Fold a `context` event into the local ctx (in place), so the next prompt reflects the live
    agent/LLM/user/shell-mode."""
    for k in ("agent", "llm", "user", "in_shell"):
        if k in ev:
            ctx[k] = ev[k]


# --------------------------------------------------------------------------- network

async def post_turn(base_url: str, speaker: str, text: str) -> dict:
    """Submit one line. Returns the server's `{accepted}` / `{busy}` verdict (the reply itself arrives on
    the stream). Returns an `{error}` dict if the agent server is unreachable — the caller surfaces it."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{base_url}/turn", json={"speaker": speaker, "text": text})
            return r.json()
    except Exception as exc:  # noqa: BLE001 — connectivity is the caller's to report
        return {"ok": False, "error": str(exc)}


async def stream_events(base_url: str) -> AsyncIterator[dict]:
    """Yield parsed events from the agent server's SSE `/stream` (backlog snapshot, then the live feed).
    Raises on connection failure — the caller decides whether to retry/backoff."""
    import httpx

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("GET", f"{base_url}/stream") as r:
            async for line in r.aiter_lines():
                ev = parse_sse_line(line)
                if ev is not None:
                    yield ev
