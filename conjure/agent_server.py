"""The agent server — the long-lived host of the shared agent (shared-session-plan §3, agent-server-plan
Step 2 / Step C1).

Today the `Shell.session` (→ Director → shared transcript) lives *inside* each front-end process. This
server moves that host out: it holds **one** `Shell.session` for the whole process and exposes it over
HTTP + SSE, so voice/CLI become thin clients that share one conversation:

    POST /turn   {speaker, text}   → fire-and-forget; the reply arrives over the stream, not the response
    GET  /stream                   → SSE: a backlog snapshot, then the live conversation event feed

`Director`/`Shell` stay plain objects (unit-testable in-process); only their *host* moves here. Both
utterances and deterministic commands go through `Shell.feed`'s wake-word routing (one door); command
output surfaces as a `notice` event, LLM text as `assistant_delta`/`assistant_final`.

A single **turn floor** (agent-server-plan D4): one turn/command runs at a time; a `POST /turn` while one
is in flight is rejected with `busy` — never queued or interleaved into the one shared transcript.

Not yet here (Step C2): following the world server's live state over its `/ws` to re-bind the Director on
an agent change. For now the `context` event carries the agent/LLM the shell was launched with.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .config import DEFAULT_USER, Settings
from .director import Busy
from .shell import Shell


# --------------------------------------------------------------------------- event fan-out

class Hub:
    """A tiny pub/sub: every `/stream` subscriber gets an unbounded queue; `publish` fans an event out to
    all of them. One shared conversation → every client observes every turn (agent-server-plan §3)."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    async def publish(self, event: dict) -> None:
        for q in list(self._subs):
            q.put_nowait(event)

    @property
    def n_subscribers(self) -> int:
        return len(self._subs)


def _context_event(shell: Shell) -> dict:
    """The client's view of "what's live" for its prompt/state. C1: the agent + active LLM + user +
    shell-mode the shell was launched with. (Step C2 folds in the world server's scope/world/space.)"""
    d = shell.director
    return {"type": "context",
            "agent": d.agent.name if d.agent else "agent",
            "llm": d.active,
            "user": shell._user,
            "in_shell": shell.in_shell}


def _turn_to_event(turn) -> dict:
    """Replay a transcript Turn as a stream event for a late joiner (backlog snapshot)."""
    if turn.speaker == "user":
        return {"type": "user_turn", "speaker": turn.by or "", "text": turn.text}
    return {"type": "assistant_final", "text": turn.text}


def _backlog_events(shell: Shell) -> list[dict]:
    """The snapshot a newly-connected `/stream` subscriber receives before the live feed: the current
    `context`, then the transcript replayed as `user_turn`/`assistant_final` (so a late joiner has the
    history). Pure — no transport — so it's unit-testable on its own."""
    return [_context_event(shell)] + [_turn_to_event(t) for t in list(shell.director.transcript)]


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


# --------------------------------------------------------------------------- the turn

async def _run_turn(app: FastAPI, speaker: str, text: str) -> None:
    """Run one submitted line to completion, fanning its output out as stream events. Routing mirrors the
    shell: an utterance → `user_turn` + `assistant_delta*` + `assistant_final` (+ `tool_call`s); a command
    → `notice`(s) + a refreshed `context` (its effect, e.g. an LLM/agent switch). Clears the floor in
    `finally` so the next turn can start."""
    shell: Shell = app.state.shell
    hub: Hub = app.state.hub
    try:
        is_command = shell._as_command(text) is not None
        if not is_command:
            await hub.publish({"type": "user_turn", "speaker": speaker, "text": text.strip()})

        async def on_text(t, *, final, speaker):        # `speaker` here = the LLM display name, not the human
            if not (t and t.strip()):
                return
            if is_command:
                await hub.publish({"type": "notice", "text": t})
            else:
                await hub.publish({"type": "assistant_final" if final else "assistant_delta",
                                   "text": t, "llm": speaker})

        async def on_tool(name, args):
            await hub.publish({"type": "tool_call", "name": name, "args": args})

        await shell.feed(text, speaker=speaker, on_text=on_text, on_tool=on_tool)
        if is_command:                                   # a switch may have changed agent/LLM → refresh prompts
            await hub.publish(_context_event(shell))
    except Busy:                                         # defense-in-depth; the server floor normally prevents it
        await hub.publish({"type": "busy", "rejected_speaker": speaker})
    except Exception as exc:                             # a bad turn must not strand the floor or the server
        await hub.publish({"type": "notice", "text": f"error: {exc}"})
    finally:
        app.state.turn_active = False


class TurnRequest(BaseModel):
    speaker: str = DEFAULT_USER
    text: str


# --------------------------------------------------------------------------- app

def build_app(settings: Settings, *, agent: Optional[str] = None, user: str = DEFAULT_USER,
              shell: Optional[Shell] = None, errlog=None) -> FastAPI:
    """Build the agent-server FastAPI app. In production the lifespan opens a real `Shell.session`
    (spawning the agent's MCP server); tests inject a ready `shell` to skip that (Director/Shell stay
    plain objects, so no subprocess/network is needed to exercise the endpoints)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hub = Hub()
        app.state.turn_active = False
        app.state.turn_task = None
        if shell is not None:                            # tests / embedding: use the injected shell as-is
            app.state.shell = shell
            yield
            return
        async with Shell.session(settings, agent=agent, user=user, errlog=errlog) as live:
            app.state.shell = live
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "turn_active": app.state.turn_active, "subscribers": app.state.hub.n_subscribers}

    @app.post("/turn")
    async def turn(req: TurnRequest):
        """Submit one line (utterance or command). Fire-and-forget: the reply streams over `/stream`, not
        this response. Rejected with `busy` if a turn is already in flight (single floor, D4)."""
        if app.state.turn_active:
            await app.state.hub.publish({"type": "busy", "rejected_speaker": req.speaker})
            return JSONResponse(status_code=409, content={"ok": False, "busy": True})
        app.state.turn_active = True                     # claim the floor synchronously (no await → atomic)
        app.state.turn_task = asyncio.create_task(_run_turn(app, req.speaker, req.text))
        return {"ok": True, "accepted": True}

    @app.get("/stream")
    async def stream(request: Request):
        """Subscribe to the shared conversation (SSE). On connect: a `context` snapshot + the transcript
        backlog (so late joiners have history); then the live event feed. A comment heartbeat every ~15s
        keeps proxies from closing an idle stream."""
        hub: Hub = app.state.hub
        shell: Shell = app.state.shell
        q = hub.subscribe()

        async def gen():
            try:
                for event in _backlog_events(shell):
                    yield _sse(event)
                while True:
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield ": keepalive\n\n"
                        continue
                    yield _sse(event)
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def main() -> int:
    """Run the agent server (host:port derived from settings.agent_url). Binds 0.0.0.0 so headset-adjacent
    clients on the LAN can reach it, like the world server."""
    import argparse
    from urllib.parse import urlparse

    import uvicorn

    from .config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="conjure-agent", description="Conjure agent server")
    parser.add_argument("--agent", default=None, help="agent to open (default: resume last-used)")
    parser.add_argument("--user", default=DEFAULT_USER, help="the host user for the session")
    args = parser.parse_args()

    port = urlparse(settings.agent_url).port or 8770
    app = build_app(settings, agent=args.agent, user=args.user)
    uvicorn.run(app, host="0.0.0.0", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
