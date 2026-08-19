"""Thin client of the agent server (shared-session Step C / D) — the pieces voice/CLI share to talk to
the agent server's WebSocket and render the shared conversation.

The client is **dumb**: it opens one WebSocket, sends each line as `{type:"turn", text}`, renders the
events it receives, and formats its own prompt from the `context` **data** the server sends. All command
logic (wake word, "open shell"/"exit", routing, mode) lives server-side; the client never parses.

Everything here is pure (no I/O) except `ws_url` (just a string) — the socket itself is opened by the
caller with `websockets`."""

from __future__ import annotations

import json
from typing import Optional


def ws_url(agent_url: str, user: str, *, backlog: bool = True) -> str:
    """The agent server's per-connection WebSocket URL. `user` is who this client acts as (the connection
    is the session). `backlog=False` suppresses the transcript replay on connect — a voice client can't
    *speak* the history, so it wants only the current context."""
    base = agent_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    url = f"{base}/ws?user={user}"
    return url if backlog else f"{url}&backlog=0"


def prompt_from_context(ctx: dict) -> str:
    """Format the REPL prompt from the latest `context` DATA: `conjure:<user>.shell>` in shell mode, else
    `conjure:<user>.<agent>.<llm>>`. Formatting lives in the client (a voice client would render none) —
    only the data comes from the server (shared-session-plan §8)."""
    user = ctx.get("user") or "you"
    if ctx.get("in_shell"):
        return f"conjure:{user}.shell> "                      # same shape, `shell` in the agent slot
    agent = ctx.get("agent") or "agent"
    llm = (ctx.get("llm") or "?").lower()
    return f"conjure:{user}.{agent}.{llm}> "


def render_event(ev: dict, *, me: str, verbose: bool) -> Optional[str]:
    """The line to print for a conversation event (or None to print nothing). `me` is the local user, so
    we don't echo our own submitted turns (but we DO show other speakers — one shared conversation).
    `context`/`turn_done` are control events → None (the caller folds context into ctx / releases the
    prompt gate)."""
    t = ev.get("type")
    if t == "user_turn":
        spk = ev.get("speaker")
        # Live: suppress our own turn (we already typed it). Backlog: show it — reviewing history, we
        # weren't here to type it, so a transcript missing our own prompts reads as gaps.
        return f"{spk}: {ev.get('text', '')}" if spk and (spk != me or ev.get("backlog")) else None
    if t in ("assistant_delta", "assistant_final", "notice"):
        return ev.get("text") or None
    if t == "busy":
        return "[busy — another turn is already in progress]"
    if t == "tool_call" and verbose:
        return f"  · {ev.get('name')}({json.dumps(ev.get('args', {}))})"
    return None                                   # context / turn_done / non-verbose tool_call / unknown


def apply_context(ctx: dict, ev: dict) -> None:
    """Fold a `context` event into the local ctx (in place), so the next prompt reflects the live
    agent/LLM/user/shell-mode the server reports for this connection."""
    for k in ("agent", "llm", "user", "in_shell", "world", "space", "owner"):
        if k in ev:
            ctx[k] = ev[k]
