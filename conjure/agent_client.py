"""Thin client of the agent server (shared-session Step C / D) — the pieces voice/CLI share to talk to
the agent server's WebSocket and render the shared conversation.

The client is **dumb**: it opens one WebSocket, sends each line as `{type:"turn", text}`, renders the
events it receives, and formats its own prompt from the `context` **data** the server sends. All command
logic (wake word, "open shell"/"exit", routing, mode) lives server-side; the client never parses.

Two ways to render an event: `render_parts` gives `(speaker, text)` so a front-end can style the
attribution apart from the line (the CLI does), and `render_event` flattens that to one string for
callers with no styling.

Everything here is pure (no I/O) except `ws_url` (just a string) — the socket itself is opened by the
caller with `websockets`."""

from __future__ import annotations

import json
from typing import Optional


def ws_url(agent_url: str, user: str, *, backlog: bool = True, client: str = "cli") -> str:
    """The agent server's per-connection WebSocket URL. `user` is who this client acts as (the connection
    is the session). `backlog=False` suppresses the transcript replay on connect — a voice client can't
    *speak* the history, so it wants only the current context. `client` tells the shell which command set
    applies: a spoken directory listing helps nobody, so voice gets the modal/navigational subset."""
    base = agent_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    url = f"{base}/ws?user={user}&client={client}"
    return url if backlog else f"{url}&backlog=0"


def prompt_from_context(ctx: dict) -> str:
    """Format the REPL prompt from the latest `context` DATA: in shell mode
    `conjure:<user>.shell <cwd>>`, else `conjure:<user>.<agent>.<llm>>`. Formatting lives in the client
    (a voice client would render none) — only the data comes from the server (shared-session-plan §8).

    The working directory is shown absolute with `~` for your own home, so it never lies about where you
    are — a shortcut like `…/worlds` resolves server-side and the prompt shows what it resolved to."""
    user = ctx.get("user") or "you"
    if ctx.get("in_shell"):
        cwd = ctx.get("cwd") or ""
        home = f"/{user}"
        if cwd == home:
            cwd = "~"
        elif cwd.startswith(home + "/"):
            cwd = "~" + cwd[len(home):]
        return f"conjure:{user}.shell {cwd}> " if cwd else f"conjure:{user}.shell> "
    agent = ctx.get("agent") or "agent"
    llm = (ctx.get("llm") or "?").lower()
    return f"conjure:{user}.{agent}.{llm}> "


_SLICES = (("prompt", "prompt"), ("room", "room"), ("tools", "tools"), ("history", "hist"))


def human_count(n: int) -> str:
    """A compact char count: 812 · 2.3k · 49.7k · 1.2M. Two significant-ish digits, so the number stays
    the same width as it grows and the bar doesn't jitter."""
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        k = n / 1000
        return f"{k:.1f}k" if k < 100 else f"{k:.0f}k"
    return f"{n / 1_000_000:.1f}M"


def status_segments(ctx: dict, *, working: Optional[float] = None) -> list:
    """The status bar's fields, most-important first, as `(key, full, compact)` — a list rather than one
    string so a narrow terminal can shorten and then drop from the end (see `status_from_context`).

    Everything but `working` comes from the server's `context` event: the client displays, the server
    measures (only it knows what was actually sent to the model)."""
    segs = []
    if working is not None:
        segs.append(("working", f"working {int(working)}s", f"{int(working)}s"))
    agent = ctx.get("agent") or "agent"
    llm = (ctx.get("llm") or "?").lower()
    segs.append(("who", f"{agent}·{llm}", f"{agent}·{llm}"))

    stats = ctx.get("stats") or {}
    turns, cap = stats.get("turns"), stats.get("cap")
    if turns is not None:
        # `cap` counts transcript entries (a user line and a reply are two), and 0 means no trimming.
        full = f"{turns}/{cap} turns" if cap else f"{turns} turns"
        segs.append(("turns", full, f"{turns}/{cap}" if cap else str(turns)))

    chars = stats.get("chars") or {}
    total = sum(v for v in chars.values() if isinstance(v, (int, float)))
    if total > 0:
        segs.append(("total", f"{human_count(total)} chars", human_count(total)))
        # Zero slices are dropped rather than shown as 0% — `room` is 0 until a turn actually assembles
        # the `{context}` injection, and a permanent "room 0 (0%)" would just be noise.
        live = [(key, label) for key, label in _SLICES if chars.get(key)]
        if live:
            pct = {key: round(100 * chars[key] / total) for key, _ in live}
            segs.append(("breakdown",
                         " · ".join(f"{lb} {human_count(chars[k])} ({pct[k]}%)" for k, lb in live),
                         " · ".join(f"{lb} {pct[k]}%" for k, lb in live)))
    return segs


def status_from_context(ctx: dict, *, working: Optional[float] = None, width: Optional[int] = None) -> str:
    """The status bar line, fitted to `width`. Degrades in three stages rather than wrapping: the full
    line, then the compact form of each field (the breakdown loses its char counts and keeps its
    percentages), then dropping fields from the least-important end — breakdown, total, turn count."""
    segs = status_segments(ctx, working=working)

    def joined(compact: bool) -> str:
        return "   ".join(seg[2] if compact else seg[1] for seg in segs)

    if width is None or len(joined(False)) <= width:
        return joined(False)
    while len(segs) > 1:
        if len(joined(True)) <= width:
            return joined(True)
        segs.pop()                                    # drop the least-important field and re-measure
    line = joined(True)                               # one field left: truncate rather than show nothing
    return line if len(line) <= width else line[:max(0, width)]


def render_parts(ev: dict, *, me: str, verbose: bool, agent: str = "agent") -> Optional[tuple]:
    """`(speaker, text)` for a conversation event, or None to print nothing — the structured form, so a
    front-end can style the attribution apart from the line (the CLI bolds it). `speaker` is None for
    output that isn't somebody *talking*: shell notices, tool traces, the busy marker.

    `me` is the local user, so we don't echo our own submitted turns (but we DO show other speakers —
    one shared conversation). `agent` attributes the agent's replies by name ('builder: …'), matching how
    user turns read; it comes from the caller's live `context`, so a REPLAYED backlog turn is labelled
    with the agent that's active *now* (the event carries no agent of its own — agent_server.py `_replay`).

    `context`/`turn_done` are control events → None (the caller folds context into ctx / releases the
    prompt gate)."""
    t = ev.get("type")
    if t == "user_turn":
        spk = ev.get("speaker")
        # Live: suppress our own turn (we already typed it). Backlog: show it — reviewing history, we
        # weren't here to type it, so a transcript missing our own prompts reads as gaps.
        if spk and (spk != me or ev.get("backlog")):
            return spk, ev.get("text", "")
        return None
    if t in ("assistant_delta", "assistant_final"):
        # NOT a token stream despite the name: `emit` fires once per LLM round-trip (llm.py), so a
        # `delta` is a whole intermediate message — an ack, or narration before a tool call.
        text = ev.get("text")
        return ((agent or "agent"), text) if text else None
    if t == "notice":
        text = ev.get("text")                     # the SHELL speaking (deterministic plane), not the agent
        return (None, text) if text else None
    if t == "busy":
        return None, "[busy — another turn is already in progress]"
    if t == "tool_call" and verbose:
        return None, f"  · {ev.get('name')}({json.dumps(ev.get('args', {}))})"
    return None                                   # context / turn_done / non-verbose tool_call / unknown


def render_event(ev: dict, *, me: str, verbose: bool, agent: str = "agent") -> Optional[str]:
    """`render_parts` flattened to one printable line — for a front-end with no styling (voice, tests)."""
    parts = render_parts(ev, me=me, verbose=verbose, agent=agent)
    if parts is None:
        return None
    speaker, text = parts
    return f"{speaker}: {text}" if speaker else text


def apply_context(ctx: dict, ev: dict) -> None:
    """Fold a `context` event into the local ctx (in place), so the next prompt reflects the live
    agent/LLM/user/shell-mode the server reports for this connection."""
    for k in ("agent", "llm", "user", "in_shell", "world", "space", "owner", "stats", "cwd"):
        if k in ev:
            ctx[k] = ev[k]
