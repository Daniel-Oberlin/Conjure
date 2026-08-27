"""The agent server — the long-lived host of the shared agent (docs/specs/agents.md §8).

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
Barge-in reserves a `{type:"interrupt"}` client message; it is shelved (docs/backlogs/agents.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .config import DEFAULT_USER, USERS_DIR, VOID, Settings
from .director import Busy
from .llm import Turn
from .shell import Shell, default_cwd
from .world import MIGRATED_SID, SessionRepository


# --------------------------------------------------------------------------- connections + fan-out

class Conn:
    """One connected client. Holds its per-connection state — `user` (who it acts as) and `in_shell`
    (its own command-mode toggle) — and its socket. The Director/transcript are shared, not here."""

    def __init__(self, ws: WebSocket, user: str, kind: str = "cli", in_shell: bool = False) -> None:
        self.ws = ws
        self.user = user
        self.kind = kind                 # "cli" | "voice" — which command set applies and how output reads
        self.in_shell = in_shell         # a client can ASK to start here (`?shell=1`, i.e. `cli --open-shell`)
        self.cwd = ""                    # shell working directory; "" until the first command resolves it
        self.bumped = False              # auto-forced into shell by a private session (§8.3) — distinct from
                                         # a user who chose shell, so we only auto-restore what we bumped

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


def _context_event(shell: Shell, live: Optional[dict], user: str, in_shell: bool,
                   cwd: str = "") -> dict:
    """A connection's view of "what's live" — **data**, not a formatted prompt (the client formats). The
    shared bits (agent, llm, world/space/owner) plus this connection's own `user` + `in_shell`."""
    d = shell.director                                   # transiently None while a re-bind is in flight
    ev = {"type": "context",
          "agent": d.agent.name if (d and d.agent) else "agent",
          "llm": d.active if d else "",
          "user": user,
          "in_shell": in_shell,
          # The shell's working directory, so the client can show it in the prompt. Data, not a formatted
          # string — the client decides how to render it (voice renders none).
          "cwd": cwd or default_cwd(user, d.agent.name if (d and d.agent) else "")}
    if d is not None:
        try:                                     # turns/cap + last turn's context size, for a status bar
            ev["stats"] = d.context_stats()
        except Exception:                        # noqa: BLE001 — decoration; never let it cost a client its
            pass                                 # prompt/agent/world, which is the rest of this event
    if live:
        for k in ("scope", "world", "space", "owner"):
            if k in live:
                ev[k] = live[k]
    return ev


def _turn_to_event(turn) -> dict:
    """Replay a transcript Turn as an event for a (re)connecting client (backlog). Tagged `backlog: True`
    so the client shows *every* speaker — including the connecting user's OWN past turns, which live
    rendering suppresses (you already typed them) but history review should show."""
    if turn.speaker == "user":
        return {"type": "user_turn", "speaker": turn.by or "", "text": turn.text, "backlog": True}
    return {"type": "assistant_final", "text": turn.text, "backlog": True}


def _backlog_events(shell: Shell, live: Optional[dict], user: str, in_shell: bool,
                    cwd: str = "") -> list[dict]:
    """What a newly-connected client receives before the live feed: its `context`, then the transcript
    replayed (so a late joiner has the history). Pure — unit-testable without a socket."""
    transcript = shell.director.transcript if shell.director else []   # None mid-rebind → no backlog
    return [_context_event(shell, live, user, in_shell, cwd)] + [_turn_to_event(t) for t in list(transcript)]


async def _send_context(app: FastAPI, conn: Conn) -> None:
    await conn.send(_context_event(app.state.shell, app.state.live, conn.user, conn.in_shell, conn.cwd))


def _permitted(app: FastAPI, conn: Conn) -> bool:
    """Whether `conn` may take part in the live session (§8.3): a public session admits everyone; a private
    one only its owner. Unknown state (pre-first-snapshot) admits — the world server is the gate of record."""
    live = app.state.live
    return (not live) or live.get("public", True) or (conn.user == live.get("owner"))


async def _conv_broadcast(app: FastAPI, event: dict, *, origin: Optional[Conn] = None) -> None:
    """Fan a CONVERSATION event out to PERMITTED clients only — a private session's dialog is never sent to
    a non-owner guest (§8.3). (Control events like context/turn_done go per-connection, not through here.)

    `origin` is the connection that CAUSED the event, if any: it receives the event stamped `mine: True`,
    everyone else gets it plain. That lets the submitter suppress the server's copy of a line it already
    echoed locally, WITHOUT the stream going asymmetric — every client still sees every event, so the live
    stream and a replayed backlog stay the same shape.

    The marker has to be per-connection, not per-user. One person is routinely on two clients at once
    (a CLI and the voice client), and a client that filtered on the speaker's NAME threw away the other
    client's turns as if it had typed them itself — speak into the voice client and the CLI showed the
    agent's reply with nothing it was replying to."""
    for c in app.state.hub.conns:
        if _permitted(app, c):
            try:
                await c.send({**event, "mine": True} if c is origin else event)
            except Exception:  # noqa: BLE001 — a dead socket must not break the fan-out
                pass


async def _apply_bumps(app: FastAPI) -> None:
    """Force non-permitted clients into shell mode when the live session is private, and restore the ones WE
    bumped once it's public / they're permitted again (§8.3). `Conn.bumped` distinguishes our bump from a
    user-chosen shell, so we never yank someone out of a shell they opened themselves."""
    for c in app.state.hub.conns:
        if not _permitted(app, c):
            if not c.bumped and not c.in_shell:               # already in a shell of their own → nothing to bump
                c.bumped = c.in_shell = True
                try:
                    await c.send({"type": "notice", "text": "This session is private — you're in shell mode "
                                  "until its owner makes it public (or you switch sessions)."})
                except Exception:  # noqa: BLE001
                    pass
        elif c.bumped:                                       # regained access → undo OUR bump only
            c.bumped = c.in_shell = False
            try:
                await c.send({"type": "notice", "text": "The session is public again — back in."})
            except Exception:  # noqa: BLE001
                pass


async def _broadcast_context(app: FastAPI) -> None:
    """Refresh every client's prompt/state — each with its OWN user + in_shell, the shared agent/llm/world.
    Used after a shared change (an LLM/agent switch, a follower re-bind)."""
    for c in app.state.hub.conns:
        await _send_context(app, c)


# --------------------------------------------------------------------------- transcript persistence (step 2)

def _current_session(app: FastAPI) -> Optional[tuple[str, str]]:
    """The `(scope, session-id)` of the live session, from the world server's state (Step 2). `session`
    is authoritative when present; else fall back to the scope's on-disk active session (one per scope in
    step 1). None until the first world snapshot names a scope."""
    live = app.state.live
    if not live or not live.get("scope"):
        return None
    scope = live["scope"]
    sid = live.get("session") or app.state.sessions.get_active(scope) or MIGRATED_SID
    return (scope, sid)


def _entry(turn: "Turn") -> dict:
    """A transcript `Turn` → its persisted JSON entry (and back via `_turn`). `role` is user/assistant;
    `by` is the human speaker (user turns only)."""
    return {"role": turn.speaker, "by": turn.by, "text": turn.text}


def _turn(entry: dict) -> "Turn":
    return Turn(entry.get("role", "user"), entry.get("text", ""), by=entry.get("by", ""))


def _sync_transcript(app: FastAPI) -> None:
    """Load the live session's saved transcript into the Director once per session change — so a restart or
    an agent switch resumes the conversation (a re-bind gives a fresh, empty Director; this refills it from
    disk). Also restore the session's last-used LLM (docs/specs/agents.md §5.2): a remembered choice beats
    the agent's default priority. Idempotent: tracked by `app.state.loaded_session`."""
    cur = _current_session(app)
    d = app.state.shell.director if app.state.shell else None
    if d is None or cur is None or cur == app.state.loaded_session:
        return
    scope, sid = cur
    d.transcript = [_turn(e) for e in app.state.sessions.read_transcript(scope, sid)]
    bind = getattr(d, "bind_state", None)                    # point state_* tools + {…} injections at this
    if bind:                                                 # session's state store (step 5, decision A)
        bind(app.state.sessions.state(scope, sid))
    try:                                                     # restore the session's remembered LLM, if valid
        llm = (app.state.sessions.load_meta(scope, sid).get("llm") or "")
        if llm and llm in d.roster:
            d.active = llm
    except (OSError, ValueError):
        pass
    app.state.loaded_session = cur


def _persist_llm(app: FastAPI) -> None:
    """Remember the live session's active LLM in its meta, so a switch sticks across restart/switch-back
    (docs/specs/agents.md §5.2). No-op until a session is known."""
    cur = _current_session(app)
    d = app.state.shell.director if app.state.shell else None
    if d is None or cur is None:
        return
    scope, sid = cur
    if not app.state.sessions.exists(scope, sid):
        return
    try:
        meta = app.state.sessions.load_meta(scope, sid)
        if meta.get("llm") != d.active:
            meta["llm"] = d.active
            app.state.sessions.save_meta(scope, sid, meta)
    except (OSError, ValueError):
        pass


def _maybe_seed(app: FastAPI) -> None:
    """Seed a new session's agent-state ONCE (docs/specs/agents.md §7.4/§7.5). The world server marks a
    fresh session ``seeded: False``; here we copy the agent's declared seed docs (`AgentDef.state[doc].
    seed_data`, resolved at load) into the session's `StateStore` — a fresh mutable copy per instance —
    then flip ``seeded: True``. Never clobbers a doc that already exists. Runs before the greeting so a
    generated greeting can reference seeded state."""
    cur = _current_session(app)
    d = app.state.shell.director if app.state.shell else None
    if d is None or cur is None:
        return
    scope, sid = cur
    try:
        meta = app.state.sessions.load_meta(scope, sid)
    except (OSError, ValueError):
        return
    if meta.get("seeded") is not False:
        return
    defs = getattr(d.agent, "state", {}) or {}
    store = app.state.sessions.state(scope, sid)
    existing = set(store.list())
    for doc, spec in defs.items():
        seed = (spec or {}).get("seed_data")
        if seed is not None and doc not in existing:
            store.write(doc, seed)
    meta["seeded"] = True
    app.state.sessions.save_meta(scope, sid, meta)


async def _maybe_greet(app: FastAPI) -> None:
    """Speak a new session's opening line ONCE (docs/specs/agents.md §7.5). The world server marks a fresh
    session ``greeted: False``; here — if the transcript is empty and the agent declares a greeting — we
    append it (literal verbatim, or generated via one LLM turn), persist it, broadcast it to live clients,
    and flip ``greeted: True`` so reconnects/re-syncs never repeat it."""
    cur = _current_session(app)
    d = app.state.shell.director if app.state.shell else None
    if d is None or cur is None:
        return
    scope, sid = cur
    try:
        meta = app.state.sessions.load_meta(scope, sid)
    except (OSError, ValueError):
        return
    if meta.get("greeted") is not False or d.transcript:      # only a fresh, un-greeted, empty session
        return
    greeting = (getattr(d.agent, "session", {}) or {}).get("greeting")
    text = ""
    try:
        if isinstance(greeting, str):
            text = greeting.strip()
        elif isinstance(greeting, dict) and greeting.get("generate"):
            async with app.state.floor_lock:                  # serialize the LLM turn against user turns
                text = (await d.greet(greeting["generate"])).strip()
    except Exception as exc:  # noqa: BLE001 — a greeting must never strand the session
        await _conv_broadcast(app, {"type": "notice", "text": f"[greeting failed: {exc}]"})
        text = ""
    if text:
        if not (d.transcript and d.transcript[-1].text == text):   # `greet` already appended; literal didn't
            d.transcript.append(Turn("assistant", text))
        app.state.sessions.append_transcript(scope, sid, {"role": "assistant", "by": "", "text": text})
        await _conv_broadcast(app, {"type": "assistant_final", "text": text, "llm": d.active})
    meta["greeted"] = True                                     # mark greeted even if empty → never retry
    app.state.sessions.save_meta(scope, sid, meta)


def _persist_new_turns(app: FastAPI, before: int) -> None:
    """Append the turns the Director added this turn (its transcript grew past `before`) to the live
    session's `transcript.jsonl`. No-op until a session is known."""
    cur = _current_session(app)
    d = app.state.shell.director if app.state.shell else None
    if d is None or cur is None:
        return
    scope, sid = cur
    for turn in d.transcript[before:]:
        app.state.sessions.append_transcript(scope, sid, _entry(turn))


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
                await _conv_broadcast(app, {"type": "user_turn", "speaker": conn.user, "text": text.strip()},
                                      origin=conn)   # `mine: True` back to the submitter only

                async def on_text(t, *, final, speaker):     # `speaker` = the LLM display name
                    if t and t.strip():
                        await _conv_broadcast(app, {"type": "assistant_final" if final else "assistant_delta",
                                                    "text": t, "llm": speaker})

                async def on_tool(name, args):
                    await _conv_broadcast(app, {"type": "tool_call", "name": name, "args": args})

                # Hold the floor so a follower re-bind can't tear down the MCP session mid-tool-call (C2).
                async with app.state.floor_lock:
                    before = len(shell.director.transcript)
                    await shell.director.handle(text, speaker=conn.user, on_text=on_text, on_tool=on_tool)
                    _persist_new_turns(app, before)      # append this turn to the session's transcript (step 2)
            finally:
                app.state.turn_active = False
                await _broadcast_context(app)            # the turn moved the counts — refresh every status bar
        elif shell.is_open_shell(cmd):                   # ---- enter shell mode (this connection only)
            conn.in_shell = True
            await conn.send({"type": "notice", "text": "Shell — deterministic commands. Nouns act on "
                                                       "what's live (agent, llm, session, world); dir / "
                                                       "show / cd / delete walk the namespace. 'help' "
                                                       "lists them, 'exit' returns to the agent."})
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

            before_llm = shell.director.active if shell.director else None
            await shell._dispatch(cmd, on_text, speaker=conn.user,   # act as the SPEAKER (own scope), not host;
                                  permitted=_permitted(app, conn),   # gate shared-effect verbs on §6d
                                  cwd=conn.cwd, voice=(conn.kind == "voice"))
            conn.cwd = shell.cwd                         # `cd` is per-connection, like shell mode
            if shell.director and shell.director.active != before_llm:
                _persist_llm(app)                        # a `use <llm>` sticks to the session (step 3c)
            await _broadcast_context(app)                # an LLM/agent switch changes everyone's prompt
    except Busy:                                         # defense-in-depth; the floor normally prevents it
        await conn.send({"type": "busy"})
    except Exception as exc:                             # a bad line must not strand the floor or the socket
        app.state.turn_active = False
        await conn.send({"type": "notice", "text": f"error: {exc}"})
    finally:
        await conn.send({"type": "turn_done"})           # the client's prompt gate — always fires


# --------------------------------------------------------------------------- world-state follow (C2)

def _agent_change_notice(state: dict) -> str:
    """What to say when the live agent moved WITHOUT this server being asked to move it.

    The most common cause is co-location: an AR client votes its capture against the geo candidates, the
    world server matches a space, and joins that space's last-active world — in whatever scope owns it
    (`/space/select`). Your room, in other words, can hand you a different agent. That's the intended
    design (the space owns the world owns the scope), but it used to happen in total silence: you kept
    talking, and something else answered. `state` carries no reason, so name the destination — world and
    space are exactly the evidence that makes a room match recognisable as one."""
    where = " · ".join(x for x in (state.get("world"), state.get("space")) if x and x != VOID)
    return f"[now in the {state.get('agent')} agent{' — ' + where if where else ''}]"


async def _reconcile_state(app: FastAPI, state: dict) -> None:
    """Reconcile to the world server's live `state`: on an agent change, re-bind the Director in the
    owning task (a fresh Director = fresh transcript); either way refresh every client's context. Not
    re-asserting the world (activate_world=False) — we're following, not driving (no loop)."""
    shell: Shell = app.state.shell
    hub: Hub = app.state.hub
    new_agent = state.get("agent")
    # A client's own `agent <name>` already narrates, and its /scope/activate comes straight back here as
    # a change — `expect_agent` is the hook's claim on that echo. Consume it only when the switch it named
    # actually LANDS: snapshots arrive for all sorts of reasons, and clearing on an unrelated one would
    # drop the claim before the real change showed up, announcing it on top of the hook's narration.
    expected = getattr(app.state, "expect_agent", None)
    if new_agent and expected == new_agent:
        app.state.expect_agent = None
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
                if expected != new_agent:                # nobody here asked for this — say so
                    await hub.broadcast({"type": "notice", "text": _agent_change_notice(state)})
    app.state.live = state
    _sync_transcript(app)                                # (re)load the live session's saved dialog (step 2)
    _maybe_seed(app)                                     # seed a new session's agent-state once (step 5b)
    await _maybe_greet(app)                              # speak a new session's opening line once (step 4b)
    await _apply_bumps(app)                              # private session ⇒ shell non-owner clients (step 6c)
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
        shell._clear_transcript_hook = _make_clear_transcript_hook(app, shell)
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
        # Claim this one before asking, so the follower recognises the change it's about to see as OURS
        # and stays quiet — the narration below is the announcement. Set it FIRST: the /ws broadcast can
        # land while the POST is still returning.
        app.state.expect_agent = agent_name
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{settings.world_url}/scope/activate", json={"scope": scope})
        except Exception as exc:  # noqa: BLE001
            app.state.expect_agent = None                 # nothing moved — don't muffle a later real change
            if on_text:
                await on_text(f"Couldn't switch to {agent_name}: {exc}", final=True, speaker=shell.director.active)
            return
        if on_text:
            await on_text(f"Switching to agent {agent_name}…", final=True, speaker=shell.director.active)
        # Wait for the follower (owning task) to re-bind before returning, so the client's next context
        # reflects the new agent instead of lagging. We hold no floor here, so the follower can take it.
        # `shell.director` is transiently None mid-rebind, so guard it. ~10s cap.
        try:
            for _ in range(200):
                d = shell.director
                if d is not None and d.agent is not None and d.agent.name == agent_name:
                    break
                await asyncio.sleep(0.05)
        finally:
            # Drop the claim once this switch is done either way. The follower normally consumes it, but
            # an already-active scope answers `unchanged` and broadcasts nothing — an uncleared claim
            # would then muffle a genuine, unasked switch to the same agent later on.
            app.state.expect_agent = None

    return _switch


def _make_clear_transcript_hook(app: FastAPI, shell: Shell):
    """`clear` resets the live session's chat history — the Director's in-memory transcript (what the LLM
    sees each turn) AND the persisted JSONL — so a bloated conversation that's degrading tool-calling can
    be wiped without touching the world, assets, or session. No cross-task teardown, so it runs inline."""
    async def _clear(on_text) -> None:
        d = shell.director
        cur = _current_session(app)
        if d is not None:
            d.transcript = []
        if cur is not None:
            app.state.sessions.clear_transcript(*cur)
        if on_text:
            await on_text("Chat history cleared — starting fresh (world and assets kept).", final=True,
                          speaker=(d.active if d is not None else None))

    return _clear


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
        app.state.sessions = SessionRepository(USERS_DIR)  # transcript store; tests may repoint at a tmp
        app.state.loaded_session = None                  # (scope, sid) whose transcript is loaded (step 2)
        app.state.floor_lock = asyncio.Lock()            # serializes turns against a follower re-bind (C2)
        app.state.expect_agent = None                    # an agent switch THIS server asked for → don't
        #                                                  announce it twice (see _reconcile_state)
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
        shell mode lives here). `?shell=1` opens it already in shell mode — the state, not a synthetic
        "conjure open shell" turn, so the first context event is already right and a reconnect restores
        the mode the client was launched in. On connect: this client's context + the transcript backlog.
        Then a receive loop: `{type:"turn", text}` runs a line."""
        await websocket.accept()
        conn = Conn(websocket, websocket.query_params.get("user") or DEFAULT_USER,
                    kind=(websocket.query_params.get("client") or "cli").lower(),
                    in_shell=websocket.query_params.get("shell", "0").lower() in ("1", "true", "yes"))
        want_backlog = websocket.query_params.get("backlog", "1").lower() not in ("0", "false", "no")
        app.state.hub.add(conn)
        try:
            if not _permitted(app, conn):                # joining a PRIVATE session → shell only, no dialog (§8.3)
                conn.bumped = not conn.in_shell          # `--open-shell` asked for this — don't claim it as OUR
                conn.in_shell = True                     # bump, or going public would yank them back out
                await conn.send(_context_event(app.state.shell, app.state.live, conn.user, conn.in_shell, conn.cwd))
                await conn.send({"type": "notice", "text": "This session is private — you're in shell mode "
                                 "until its owner makes it public (or you switch sessions)."})
            elif want_backlog:                           # a text client replays history; a voice client can't
                for event in _backlog_events(app.state.shell, app.state.live, conn.user,
                                             conn.in_shell, conn.cwd):
                    await conn.send(event)
            else:
                await conn.send(_context_event(app.state.shell, app.state.live, conn.user, conn.in_shell, conn.cwd))
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
    parser.add_argument("--history-cap", type=int, default=settings.history_cap, metavar="TURNS",
                        help="max conversation turns sent to the LLM each turn (older ones dropped from the "
                             "model's view; still saved + replayed to clients). Keeps tool-calling reliable as "
                             "a session grows; 0 = unlimited (default: %(default)s). Also 'session clear'.")
    args = parser.parse_args()
    import dataclasses
    settings = dataclasses.replace(settings, history_cap=args.history_cap)

    port = urlparse(settings.agent_url).port or 8770
    app = build_app(settings, agent=args.agent, user=args.user)
    uvicorn.run(app, host="0.0.0.0", port=port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
