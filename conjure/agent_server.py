"""The agent server — the long-lived host of the shared agent (shared-session-plan §3, Step C).

The `Shell` (command logic) → `Director` → shared transcript live here; voice/CLI are **dumb clients**
over a single per-connection **WebSocket**:

    ws://…/ws?user=<name>
      client → server:  {type:"turn", text}          # one line of input (utterance OR command)
      server → client:  {type:"context", …}           # this connection's state (agent, llm, user, in_shell,
                                                        # world, space) — DATA; the client formats its prompt
                        {type:"user_turn"|"assistant_delta"|"assistant_final"|"tool_call"|"notice"|
                              "busy"|"turn_done", …}    # the shared conversation (broadcast) + control

All command logic — the wake word, the "open shell"/"exit" phrases, mode, dispatch — is **server-side**
(the shell). Clients never parse; they send raw text and render. Shell **mode is per-connection**
(`Conn.in_shell`), so one client entering command mode never drags the others in. One shared
Director/transcript/floor underneath; only `{user, in_shell}` is per-connection.

Single **turn floor** (D4): one utterance runs at a time; a second while one's in flight gets `busy`.
The follow loop rides the world server's `/ws` and re-binds the Director on an agent change (C2).
Barge-in (C3) will add a `{type:"interrupt"}` client message that cancels the in-flight turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .config import DEFAULT_USER, Settings
from .director import Busy
from .shell import Shell


# --------------------------------------------------------------------------- connections + fan-out

class Conn:
    """One connected client. Holds its per-connection state — `user` (who it acts as) and `in_shell`
    (its own command-mode toggle) — and its socket. The Director/transcript are shared, not here."""

    def __init__(self, ws: WebSocket, user: str) -> None:
        self.ws = ws
        self.user = user
        self.in_shell = False

    async def send(self, event: dict) -> None:
        await self.ws.send_json(event)


class Hub:
    """The set of live connections. `broadcast` fans a conversation event out to all (one shared
    conversation); per-connection sends (context, notice, turn_done) go through `Conn.send`."""

    def __init__(self) -> None:
        self._conns: set[Conn] = set()

    def add(self, conn: Conn) -> None:
        self._conns.add(conn)

    def remove(self, conn: Conn) -> None:
        self._conns.discard(conn)

    @property
    def conns(self) -> list[Conn]:
        return list(self._conns)

    @property
    def n(self) -> int:
        return len(self._conns)

    async def broadcast(self, event: dict) -> None:
        for c in list(self._conns):
            try:
                await c.send(event)
            except Exception:  # noqa: BLE001 — a dead socket must not break the fan-out
                pass


def _context_event(shell: Shell, live: Optional[dict], user: str, in_shell: bool) -> dict:
    """A connection's view of "what's live" — **data**, not a formatted prompt (the client formats). The
    shared bits (agent, llm, world/space/owner) plus this connection's own `user` + `in_shell`."""
    d = shell.director                                   # transiently None while a re-bind is in flight
    ev = {"type": "context",
          "agent": d.agent.name if (d and d.agent) else "agent",
          "llm": d.active if d else "",
          "user": user,
          "in_shell": in_shell}
    if live:
        for k in ("scope", "world", "space", "owner"):
            if k in live:
                ev[k] = live[k]
    return ev


def _turn_to_event(turn) -> dict:
    """Replay a transcript Turn as an event for a late joiner (backlog)."""
    if turn.speaker == "user":
        return {"type": "user_turn", "speaker": turn.by or "", "text": turn.text}
    return {"type": "assistant_final", "text": turn.text}


def _backlog_events(shell: Shell, live: Optional[dict], user: str, in_shell: bool) -> list[dict]:
    """What a newly-connected client receives before the live feed: its `context`, then the transcript
    replayed (so a late joiner has the history). Pure — unit-testable without a socket."""
    transcript = shell.director.transcript if shell.director else []   # None mid-rebind → no backlog
    return [_context_event(shell, live, user, in_shell)] + [_turn_to_event(t) for t in list(transcript)]


async def _send_context(app: FastAPI, conn: Conn) -> None:
    await conn.send(_context_event(app.state.shell, app.state.live, conn.user, conn.in_shell))


async def _broadcast_context(app: FastAPI) -> None:
    """Refresh every client's prompt/state — each with its OWN user + in_shell, the shared agent/llm/world.
    Used after a shared change (an LLM/agent switch, a follower re-bind)."""
    for c in app.state.hub.conns:
        await _send_context(app, c)


# --------------------------------------------------------------------------- handling one line

async def _handle_turn(app: FastAPI, conn: Conn, text: str) -> None:
    """Route one submitted line, using THIS connection's shell mode. All parsing is server-side (the
    shell): an utterance → broadcast `user_turn` + `assistant_*`/`tool_call` (shared conversation); the
    two mode toggles → flip `conn.in_shell` + refresh this client's context; any other command → run it
    (shared effect) with output as a `notice` to this client, then refresh everyone's context. A
    `turn_done` to this client always closes the line (its prompt gate)."""
    shell: Shell = app.state.shell
    hub: Hub = app.state.hub
    try:
        cmd = shell.as_command(text, conn.in_shell)
        if cmd is None:                                  # ---- an utterance to the agent
            if app.state.turn_active:                    # single floor (D4): reject a concurrent turn
                await conn.send({"type": "busy"})
                return
            app.state.turn_active = True
            try:
                await hub.broadcast({"type": "user_turn", "speaker": conn.user, "text": text.strip()})

                async def on_text(t, *, final, speaker):     # `speaker` = the LLM display name
                    if t and t.strip():
                        await hub.broadcast({"type": "assistant_final" if final else "assistant_delta",
                                             "text": t, "llm": speaker})

                async def on_tool(name, args):
                    await hub.broadcast({"type": "tool_call", "name": name, "args": args})

                # Hold the floor so a follower re-bind can't tear down the MCP session mid-tool-call (C2).
                async with app.state.floor_lock:
                    await shell.director.handle(text, speaker=conn.user, on_text=on_text, on_tool=on_tool)
            finally:
                app.state.turn_active = False
        elif shell.is_open_shell(cmd):                   # ---- enter shell mode (this connection only)
            conn.in_shell = True
            await conn.send({"type": "notice", "text": "Shell — deterministic commands (help, llms, agents, "
                                                       "use <llm>, agent <name>, whoami, dir, delete). "
                                                       "'exit' returns to the agent."})
            await _send_context(app, conn)
        elif shell.is_leave_shell(cmd):                  # ---- leave shell mode (this connection only)
            if conn.in_shell:
                conn.in_shell = False
                await conn.send({"type": "notice", "text": "Back to the agent."})
            await _send_context(app, conn)
        else:                                            # ---- a deterministic command (shared effect)
            async def on_text(t, *, final, speaker):
                if t and t.strip():
                    await conn.send({"type": "notice", "text": t})

            await shell._dispatch(cmd, on_text)
            await _broadcast_context(app)                # an LLM/agent switch changes everyone's prompt
    except Busy:                                         # defense-in-depth; the floor normally prevents it
        await conn.send({"type": "busy"})
    except Exception as exc:                             # a bad line must not strand the floor or the socket
        app.state.turn_active = False
        await conn.send({"type": "notice", "text": f"error: {exc}"})
    finally:
        await conn.send({"type": "turn_done"})           # the client's prompt gate — always fires


# --------------------------------------------------------------------------- world-state follow (C2)

async def _reconcile_state(app: FastAPI, state: dict) -> None:
    """Reconcile to the world server's live `state`: on an agent change, re-bind the Director in the
    owning task (a fresh Director = fresh transcript); either way refresh every client's context. Not
    re-asserting the world (activate_world=False) — we're following, not driving (no loop)."""
    shell: Shell = app.state.shell
    hub: Hub = app.state.hub
    new_agent = state.get("agent")
    current = shell.director.agent.name if (shell.director and shell.director.agent) else None
    if new_agent and new_agent != current:
        async with app.state.floor_lock:                 # serialize against in-flight turns
            current = shell.director.agent.name if (shell.director and shell.director.agent) else None
            if new_agent != current:
                try:
                    await shell._open_agent(new_agent, activate_world=False)
                except Exception as exc:  # noqa: BLE001 — a bad follow must not kill the follower
                    await hub.broadcast({"type": "notice", "text": f"[couldn't follow to agent {new_agent}: {exc}]"})
                    return
    app.state.live = state
    await _broadcast_context(app)


async def _follow_world_state(app: FastAPI) -> None:
    """Ride the world server's `/ws` as a passive listener (never sends `hold`): every snapshot carries
    the live `state` (Step B), which we reconcile to. Reconnects with backoff (order-independent §4)."""
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
                    state = msg.get("state")             # only snapshots carry it
                    if state:
                        await _reconcile_state(app, state)
        except Exception:  # noqa: BLE001 — world server down/restarting → back off and reconnect
            if app.state.stop_follow.is_set():
                return
            await asyncio.sleep(1.0)


async def _shell_and_follow(app: FastAPI, settings: Settings, agent: Optional[str], user: str,
                            errlog) -> None:
    """Own the `Shell.session` AND run the follow loop in ONE task — the Director's MCP session must be
    entered/exited/re-bound all in the same task (anyio structured concurrency; a cross-task aclose raises
    a cancel-scope error). Turns run in the connection tasks but only *call* the session (safe); only
    re-binds (here) enter/exit it, serialized against turns by `floor_lock`."""
    async with Shell.session(settings, agent=agent, user=user, errlog=errlog) as shell:
        shell._agent_switch_hook = _make_agent_switch_hook(app, settings, shell)
        app.state.shell = shell
        app.state.shell_ready.set()
        await _follow_world_state(app)


def _make_agent_switch_hook(app: FastAPI, settings: Settings, shell: Shell):
    """A client's `agent <name>` must NOT re-bind the Director from a connection task — that's a cross-task
    MCP teardown. Instead assert the target scope on the world server; its `/ws` broadcast makes THIS
    server's follower re-bind (in the owning task) and every other client follow. A client agent-switch is
    just another pointer move through the single source of truth."""
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
        # Wait for the follower (owning task) to re-bind before returning, so the client's next context
        # reflects the new agent instead of lagging. We hold no floor here, so the follower can take it.
        # `shell.director` is transiently None mid-rebind, so guard it. ~10s cap.
        for _ in range(200):
            d = shell.director
            if d is not None and d.agent is not None and d.agent.name == agent_name:
                break
            await asyncio.sleep(0.05)

    return _switch


# --------------------------------------------------------------------------- app

def build_app(settings: Settings, *, agent: Optional[str] = None, user: str = DEFAULT_USER,
              shell: Optional[Shell] = None, errlog=None) -> FastAPI:
    """Build the agent-server FastAPI app. Production: the lifespan opens a real `Shell.session` (spawning
    the agent's MCP server) + the follow loop. Tests inject a ready `shell` to skip that."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.hub = Hub()
        app.state.turn_active = False
        app.state.live = None                            # last world-server state tuple (set by the follower)
        app.state.floor_lock = asyncio.Lock()            # serializes turns against a follower re-bind (C2)
        app.state.stop_follow = asyncio.Event()
        app.state.shell_ready = asyncio.Event()
        app.state.worker_task = None
        if shell is not None:                            # tests / embedding: use the injected shell as-is
            app.state.shell = shell
            yield
            return
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
        return {"ok": True, "turn_active": app.state.turn_active, "connections": app.state.hub.n}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        """One client connection. `?user=<name>` is who it acts as; the connection IS the session (its
        shell mode lives here). On connect: this client's context + the transcript backlog. Then a receive
        loop: `{type:"turn", text}` runs a line."""
        await websocket.accept()
        conn = Conn(websocket, websocket.query_params.get("user") or DEFAULT_USER)
        want_backlog = websocket.query_params.get("backlog", "1").lower() not in ("0", "false", "no")
        app.state.hub.add(conn)
        try:
            if want_backlog:                             # a text client replays history; a voice client can't
                for event in _backlog_events(app.state.shell, app.state.live, conn.user, conn.in_shell):
                    await conn.send(event)
            else:
                await conn.send(_context_event(app.state.shell, app.state.live, conn.user, conn.in_shell))
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "turn":
                    await _handle_turn(app, conn, msg.get("text", ""))
                # (C3) elif msg.get("type") == "interrupt": cancel the in-flight turn
        except WebSocketDisconnect:
            pass
        finally:
            app.state.hub.remove(conn)

    return app


def main() -> int:
    """Run the agent server (host:port from settings.agent_url). Binds 0.0.0.0 for LAN clients."""
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
