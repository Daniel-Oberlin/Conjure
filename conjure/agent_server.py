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
import contextlib
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


def _context_event(shell: Shell, live: Optional[dict] = None) -> dict:
    """The client's view of "what's live" for its prompt/state: the bound agent + active LLM + user +
    shell-mode, merged with the world server's live tuple (`scope`/`world`/`space`/`owner`) when known
    (Step C2 — folded in by the world-state follower). `live` absent → agent-side keys only."""
    d = shell.director                                   # transiently None while a re-bind is in flight
    ev = {"type": "context",
          "agent": d.agent.name if (d and d.agent) else "agent",
          "llm": d.active if d else "",
          "user": shell._user,
          "in_shell": shell.in_shell}
    if live:
        for k in ("scope", "world", "space", "owner"):
            if k in live:
                ev[k] = live[k]
    return ev


def _turn_to_event(turn) -> dict:
    """Replay a transcript Turn as a stream event for a late joiner (backlog snapshot)."""
    if turn.speaker == "user":
        return {"type": "user_turn", "speaker": turn.by or "", "text": turn.text}
    return {"type": "assistant_final", "text": turn.text}


def _backlog_events(shell: Shell, live: Optional[dict] = None) -> list[dict]:
    """The snapshot a newly-connected `/stream` subscriber receives before the live feed: the current
    `context` (with the live world tuple), then the transcript replayed as `user_turn`/`assistant_final`
    (so a late joiner has the history). Pure — no transport — so it's unit-testable on its own."""
    transcript = shell.director.transcript if shell.director else []   # None mid-rebind → no backlog
    return [_context_event(shell, live)] + [_turn_to_event(t) for t in list(transcript)]


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

        if is_command:
            # Commands don't call MCP tools, so they needn't hold the floor against a follower re-bind — and
            # an agent-switch command MUST NOT hold it: its hook waits for the follower to re-bind, which
            # itself needs the floor (→ would deadlock). Run it lock-free.
            await shell.feed(text, speaker=speaker, on_text=on_text, on_tool=on_tool)
            await hub.publish(_context_event(shell, getattr(app.state, "live", None)))   # refresh prompts
        else:
            # An utterance runs the LLM + tools: hold the floor so a follower re-bind can't tear down the
            # MCP session mid-tool-call (C2).
            async with app.state.floor_lock:
                await shell.feed(text, speaker=speaker, on_text=on_text, on_tool=on_tool)
    except Busy:                                         # defense-in-depth; the server floor normally prevents it
        await hub.publish({"type": "busy", "rejected_speaker": speaker})
    except Exception as exc:                             # a bad turn must not strand the floor or the server
        await hub.publish({"type": "notice", "text": f"error: {exc}"})
    finally:
        app.state.turn_active = False
        # The unambiguous end-of-turn signal (floor now free): clients gate their next prompt on it, so a
        # reply never prints on top of a fresh prompt. Fires for every path — utterance, command, error,
        # even a no-output turn — because it's in `finally`.
        await hub.publish({"type": "turn_done", "speaker": speaker})


async def _reconcile_state(app: FastAPI, state: dict) -> None:
    """Reconcile the agent server to the world server's live `state` (shared-session-plan §2, C2). If the
    **agent** changed — a headset relocalized, or another client switched — re-bind the Director to it
    (a fresh Director = fresh transcript: a different agent is a different conversation). A same-agent
    world/space change keeps the transcript. Either way, emit a `context` so every client refreshes its
    prompt/state. `activate_world=False`: we're *following* the world server, not re-asserting (no loop)."""
    shell: Shell = app.state.shell
    hub: Hub = app.state.hub
    new_agent = state.get("agent")
    current = shell.director.agent.name if shell.director.agent else None
    if new_agent and new_agent != current:
        async with app.state.floor_lock:                 # serialize against in-flight turns
            current = shell.director.agent.name if shell.director.agent else None
            if new_agent != current:                     # re-check under the lock (state may have moved)
                try:
                    await shell._open_agent(new_agent, activate_world=False)
                except Exception as exc:  # noqa: BLE001 — a bad follow must not kill the follower
                    await hub.publish({"type": "notice", "text": f"[couldn't follow to agent {new_agent}: {exc}]"})
                    return
    app.state.live = state
    await hub.publish(_context_event(shell, state))


async def _follow_world_state(app: FastAPI) -> None:
    """Ride the world server's `/ws` as a **passive listener** (never sends `hold`, so it's not a
    space-holder): every snapshot carries the live `state` (Step B), which we reconcile to. Reconnects
    with backoff — the world server may start after us or restart under us (order-independent, §4)."""
    import websockets

    settings: Settings = app.state.settings
    ws_url = settings.world_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    ws_url = f"{ws_url}/ws?user={app.state.user}"
    while not app.state.stop_follow.is_set():
        try:
            async with websockets.connect(ws_url) as ws:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    state = msg.get("state")             # only snapshots carry it (patches/presence don't)
                    if state:
                        await _reconcile_state(app, state)
        except Exception:  # noqa: BLE001 — world server down/restarting → back off and reconnect
            if app.state.stop_follow.is_set():
                return
            await asyncio.sleep(1.0)


async def _shell_and_follow(app: FastAPI, settings: Settings, agent: Optional[str], user: str,
                            errlog) -> None:
    """Own the `Shell.session` **and** run the world-state follow loop in ONE task. This is required by
    anyio's structured concurrency: the Director's MCP `ClientSession` must be entered, exited, and
    **re-bound** (on an agent change) all in the same task — a cross-task `aclose()` raises "exit a cancel
    scope that isn't the current task's". Turns run in their own tasks but only *call* the session
    (`call_tool`), which is safe; only re-binds (here) enter/exit it. Serialization against turns is via
    `floor_lock`."""
    async with Shell.session(settings, agent=agent, user=user, errlog=errlog) as shell:
        shell._agent_switch_hook = _make_agent_switch_hook(app, settings, shell)
        app.state.shell = shell
        app.state.shell_ready.set()
        await _follow_world_state(app)                   # loops until stop_follow; re-binds happen in-task


def _make_agent_switch_hook(app: FastAPI, settings: Settings, shell: Shell):
    """A client's `agent <name>` must NOT re-bind the Director from its (spawned) turn task — that's a
    cross-task MCP teardown. Instead, assert the target scope on the world server; its `/ws` broadcast
    makes THIS server's follower re-bind (in the owning task) and every other client (headsets) follow
    too. So a client agent-switch is just another pointer move through the single source of truth."""
    from .config import scope_for

    async def _switch(agent_name: str, on_text) -> None:
        scope = scope_for(app.state.user, agent_name)
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{settings.world_url}/scope/activate", json={"scope": scope})
        except Exception as exc:  # noqa: BLE001
            if on_text:
                await on_text(f"Couldn't switch to {agent_name}: {exc}", final=True, speaker=shell.director.active)
            return
        if on_text:
            await on_text(f"Switching to agent {agent_name}…", final=True, speaker=shell.director.active)
        # Wait for the /ws follower (owning task) to re-bind the Director before we return, so the command's
        # context — and the client's next prompt — reflect the NEW agent instead of lagging a step. We hold
        # no floor here, so the follower can take it. `shell.director` is transiently None mid-rebind (the
        # LIFO close-then-open), so guard it. Best-effort with a ~10s cap.
        for _ in range(200):
            d = shell.director
            if d is not None and d.agent is not None and d.agent.name == agent_name:
                break
            await asyncio.sleep(0.05)

    return _switch


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
        app.state.live = None                            # last world-server state tuple (set by the follower)
        app.state.floor_lock = asyncio.Lock()            # serializes turns against a follower re-bind (C2)
        app.state.stop_follow = asyncio.Event()
        app.state.shell_ready = asyncio.Event()
        app.state.worker_task = None
        if shell is not None:                            # tests / embedding: use the injected shell as-is
            app.state.shell = shell
            yield
            return
        # One task owns the shell + the follow loop (see _shell_and_follow for why they can't be split).
        task = asyncio.create_task(_shell_and_follow(app, settings, agent, user, errlog))
        app.state.worker_task = task
        while not app.state.shell_ready.is_set():        # wait until the shell is open (or the worker died)
            if task.done():
                task.result()                            # re-raise a startup failure (no keys, bad agent…)
                break
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            app.state.stop_follow.set()
            task.cancel()                                # unwinds Shell.session in ITS own task (clean anyio)
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.state.user = user

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
                for event in _backlog_events(shell, app.state.live):
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
