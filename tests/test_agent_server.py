"""Agent server (shared-session Step C/D) — the WebSocket host of the shared shell/Director.

`_handle_turn` (per-connection routing), the fan-out, and `_reconcile_state` are exercised directly with
a fake shell + fake connections — no network, no MCP subprocess, no LLM. The `/ws` endpoint is covered
with a TestClient over an injected fake shell (build_app's `shell=` bypasses the real Shell.session)."""

import asyncio
import tempfile
import types

from conjure.agent_server import (Hub, _backlog_events, _context_event, _handle_turn, _reconcile_state,
                                   _sync_transcript, build_app)
from conjure.agent_client import render_event
from conjure.config import get_settings
from conjure.llm import Turn
from conjure.world import SessionRepository


# --------------------------------------------------------------------------- fakes

class FakeDir:
    def __init__(self):
        self.agent = types.SimpleNamespace(name="builder")
        self.active = "Claude"
        self.transcript: list[Turn] = []

    async def handle(self, text, *, speaker, on_text=None, on_tool=None):
        if on_text:
            await on_text("on it", final=False, speaker=self.active)
        if on_tool:
            await on_tool("place_asset", {"query": "tree"})
        if on_text:
            await on_text("done — a tree", final=True, speaker=self.active)
        self.transcript.append(Turn("user", text.strip(), by=speaker))
        self.transcript.append(Turn("assistant", "done — a tree"))

    async def greet(self, instruction):
        text = f"welcome «{instruction}»"
        self.transcript.append(Turn("assistant", text))
        return text

    def context_stats(self):
        return {"turns": len(self.transcript), "cap": 40,
                "chars": {"prompt": 100, "room": 0, "tools": 300, "history": 50}}


class FakeShell:
    """Just what the agent server calls: the routing engine (mode as a param), command dispatch, a
    re-bind, and a director with a transcript."""

    def __init__(self):
        self.director = FakeDir()
        self.opened: list[tuple] = []

    def as_command(self, text, in_shell):
        s = text.strip()
        if in_shell:
            return s
        if s.lower().startswith("conjure"):
            return s[len("conjure"):].strip(" ,:") or "open shell"
        return None

    @staticmethod
    def is_open_shell(cmd):
        return cmd.strip().lower() in ("shell", "open shell")

    @staticmethod
    def is_leave_shell(cmd):
        return cmd.strip().lower() in ("exit", "leave", "close", "done")

    cwd = "/daniel/agents/builder"        # the shell reports its working directory back after a dispatch

    async def _dispatch(self, cmd, on_text, *, speaker=None, permitted=True, cwd="", voice=False):
        await on_text("Now talking to Gemini (builder).", final=True, speaker=self.director.active)

    async def _open_agent(self, agent, *, activate_world=True):
        self.opened.append((agent, activate_world))
        self.director.agent = types.SimpleNamespace(name=agent)
        self.director.transcript = []


class FakeConn:
    def __init__(self, user="daniel", kind="cli"):
        self.user = user
        self.kind = kind                 # "cli" | "voice" — selects the shell's command set
        self.in_shell = False
        self.cwd = ""
        self.bumped = False
        self.sent: list[dict] = []

    async def send(self, event):
        self.sent.append(event)


def _app(shell, conns=(), sessions=None):
    hub = Hub()
    for c in conns:
        hub.add(c)
    return types.SimpleNamespace(state=types.SimpleNamespace(
        shell=shell, hub=hub, turn_active=False, floor_lock=asyncio.Lock(), live=None, user="daniel",
        sessions=sessions or SessionRepository(tempfile.mkdtemp()), loaded_session=None))


def _kinds(conn):
    return [e["type"] for e in conn.sent]


# --------------------------------------------------------------------------- _handle_turn

async def test_utterance_broadcasts_conversation_and_ends_with_turn_done():
    shell = FakeShell()
    conn = FakeConn("alice")
    app = _app(shell, [conn])
    await _handle_turn(app, conn, "put a tree here")
    k = _kinds(conn)
    assert "user_turn" in k and "assistant_delta" in k and "tool_call" in k and "assistant_final" in k
    assert k[-1] == "turn_done"                                   # the prompt gate always closes the line
    ut = next(e for e in conn.sent if e["type"] == "user_turn")
    # `mine` on the SUBMITTER's copy only — that client printed the line on submit, so it drops this one.
    assert ut == {"type": "user_turn", "speaker": "alice", "text": "put a tree here", "mine": True}
    assert app.state.turn_active is False                         # floor released


async def test_a_second_client_of_the_same_user_still_sees_the_turn():
    # The CLI and the voice client are both "alice". Marking the echo per-USER made each discard the
    # other's turns as its own: speak into the voice client and the CLI showed the agent's reply with
    # nothing it was replying to. The marker is per-CONNECTION, so only the submitter drops it.
    shell = FakeShell()
    voice, cli = FakeConn("alice"), FakeConn("alice")
    app = _app(shell, [voice, cli])
    await _handle_turn(app, voice, "put a tree here")

    spoken = next(e for e in voice.sent if e["type"] == "user_turn")
    heard = next(e for e in cli.sent if e["type"] == "user_turn")
    assert spoken["mine"] is True                                  # the voice client submitted it
    assert "mine" not in heard                                     # …the CLI did not, so the CLI prints it
    assert heard == {"type": "user_turn", "speaker": "alice", "text": "put a tree here"}
    assert render_event(heard, verbose=False) == "alice: put a tree here"
    assert render_event(spoken, verbose=False) is None


# --------------------------------------------------------------------------- transcript persistence (step 2)

async def test_utterance_is_persisted_to_the_session_transcript(tmp_path):
    shell = FakeShell()
    conn = FakeConn("alice")
    app = _app(shell, [conn], sessions=SessionRepository(tmp_path))
    app.state.live = {"scope": "daniel/agents/builder", "session": "session-1"}
    await _handle_turn(app, conn, "put a tree here")
    saved = app.state.sessions.read_transcript("daniel/agents/builder", "session-1")
    assert [(e["role"], e["by"], e["text"]) for e in saved] == [
        ("user", "alice", "put a tree here"), ("assistant", "", "done — a tree")]


async def test_llm_switch_is_remembered_and_restored_per_session(tmp_path):
    from conjure.agent_server import _persist_llm, _sync_transcript
    sessions = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    sessions.save_meta(scope, "session-1", {"id": "session-1", "owner": "daniel", "agent": "builder",
                                            "title": "S1", "public": True, "active_world": "home", "llm": ""})
    shell = FakeShell()
    shell.director.roster = {"Claude": object(), "Gemini": object()}
    app = _app(shell, [], sessions=sessions)
    app.state.live = {"scope": scope, "session": "session-1"}
    # a switch to Gemini is remembered in the session meta
    shell.director.active = "Gemini"
    _persist_llm(app)
    assert sessions.load_meta(scope, "session-1")["llm"] == "Gemini"
    # a later bind starts on Claude but is restored to the session's remembered Gemini
    shell.director.active = "Claude"
    app.state.loaded_session = None
    _sync_transcript(app)
    assert shell.director.active == "Gemini"


async def test_private_session_conversation_is_not_sent_to_guests():
    from conjure.agent_server import _conv_broadcast
    owner, guest = FakeConn("daniel"), FakeConn("bob")
    app = _app(FakeShell(), [owner, guest])
    app.state.live = {"scope": "daniel/agents/builder", "session": "s1", "public": False, "owner": "daniel"}
    await _conv_broadcast(app, {"type": "assistant_final", "text": "secret"})
    assert any(e.get("text") == "secret" for e in owner.sent)        # owner hears the private dialog
    assert not any(e.get("text") == "secret" for e in guest.sent)    # a non-owner guest does not (§8.3)


async def test_apply_bumps_shells_non_owner_guests_and_restores():
    from conjure.agent_server import _apply_bumps
    owner, guest = FakeConn("daniel"), FakeConn("bob")
    app = _app(FakeShell(), [owner, guest])
    app.state.live = {"public": False, "owner": "daniel"}            # private session
    await _apply_bumps(app)
    assert guest.in_shell and guest.bumped                           # guest forced to shell
    assert not owner.in_shell and not owner.bumped                   # owner untouched
    app.state.live = {"public": True, "owner": "daniel"}             # made public again
    await _apply_bumps(app)
    assert not guest.in_shell and not guest.bumped                   # OUR bump is undone


def _greet_app(tmp_path, greeting, conns=()):
    sessions = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    sessions.save_meta(scope, "session-2", {"id": "session-2", "owner": "daniel", "agent": "builder",
        "title": "S2", "public": True, "active_world": "home", "llm": "", "greeted": False})
    shell = FakeShell()
    shell.director.agent = types.SimpleNamespace(name="builder", session={"greeting": greeting})
    app = _app(shell, conns, sessions=sessions)
    app.state.live = {"scope": scope, "session": "session-2", "agent": "builder"}
    return app, shell, sessions, scope


async def test_new_session_state_is_seeded_once(tmp_path):
    from conjure.agent_server import _maybe_seed
    sessions = SessionRepository(tmp_path)
    scope = "daniel/agents/dm"
    sessions.save_meta(scope, "session-1", {"id": "session-1", "owner": "daniel", "agent": "dm",
        "title": "S", "public": True, "active_world": "home", "llm": "", "greeted": False, "seeded": False})
    shell = FakeShell()
    shell.director.agent = types.SimpleNamespace(name="dm", state={"map": {"seed_data": {"start": "home"}}})
    app = _app(shell, [], sessions=sessions)
    app.state.live = {"scope": scope, "session": "session-1", "agent": "dm"}
    _maybe_seed(app)
    assert sessions.state(scope, "session-1").read("map") == {"start": "home"}
    assert sessions.load_meta(scope, "session-1")["seeded"] is True
    sessions.state(scope, "session-1").set("map", "start", "cave")    # a later edit
    _maybe_seed(app)                                                  # idempotent — doesn't re-seed/clobber
    assert sessions.state(scope, "session-1").get("map", "start") == "cave"


async def test_new_session_speaks_a_literal_greeting_once(tmp_path):
    from conjure.agent_server import _maybe_greet
    conn = FakeConn("alice")
    app, shell, sessions, scope = _greet_app(tmp_path, "Hello there", [conn])
    await _maybe_greet(app)
    assert [t.text for t in shell.director.transcript] == ["Hello there"]        # appended once
    assert sessions.read_transcript(scope, "session-2")[-1]["text"] == "Hello there"   # persisted
    assert sessions.load_meta(scope, "session-2")["greeted"] is True             # flag flipped
    assert any(e.get("type") == "assistant_final" and e.get("text") == "Hello there" for e in conn.sent)
    n = len(shell.director.transcript)
    await _maybe_greet(app)                                                       # idempotent
    assert len(shell.director.transcript) == n


async def test_new_session_generated_greeting_runs_one_turn(tmp_path):
    from conjure.agent_server import _maybe_greet
    app, shell, sessions, scope = _greet_app(tmp_path, {"generate": "be warm"})
    await _maybe_greet(app)
    assert [t.text for t in shell.director.transcript] == ["welcome «be warm»"]   # generated, appended once
    assert sessions.load_meta(scope, "session-2")["greeted"] is True


async def test_reconcile_loads_the_saved_transcript_for_the_live_session(tmp_path):
    # A saved session's dialog is replayed into the Director when it becomes live (restart / switch-back).
    sessions = SessionRepository(tmp_path)
    sessions.append_transcript("daniel/agents/builder", "session-1", {"role": "user", "by": "bob", "text": "hi"})
    sessions.append_transcript("daniel/agents/builder", "session-1", {"role": "assistant", "by": "", "text": "hello"})
    shell = FakeShell()
    app = _app(shell, [], sessions=sessions)
    await _reconcile_state(app, {"agent": "builder", "scope": "daniel/agents/builder",
                                 "session": "session-1", "world": "default", "space": "<void>", "owner": "daniel"})
    assert [(t.speaker, t.by, t.text) for t in shell.director.transcript] == [
        ("user", "bob", "hi"), ("assistant", "", "hello")]
    assert app.state.loaded_session == ("daniel/agents/builder", "session-1")


async def test_second_utterance_is_rejected_busy_while_one_is_in_flight():
    shell = FakeShell()
    conn = FakeConn("bob")
    app = _app(shell, [conn])
    app.state.turn_active = True                                  # simulate an in-flight turn (single floor)
    await _handle_turn(app, conn, "make a tree")
    k = _kinds(conn)
    assert "busy" in k and k[-1] == "turn_done"
    assert "user_turn" not in k                                   # nothing ran / broadcast


async def test_open_shell_flips_only_this_connection():
    shell = FakeShell()
    a, b = FakeConn("alice"), FakeConn("bob")
    app = _app(shell, [a, b])
    await _handle_turn(app, a, "conjure open shell")
    assert a.in_shell is True and b.in_shell is False            # per-connection — no leak to b
    ctx = [e for e in a.sent if e["type"] == "context"][-1]
    assert ctx["in_shell"] is True and ctx["user"] == "alice"
    assert a.sent[-1]["type"] == "turn_done"
    assert b.sent == []                                          # b's socket untouched


async def test_exit_leaves_shell_mode_for_this_connection():
    shell = FakeShell()
    a = FakeConn("alice")
    a.in_shell = True
    app = _app(shell, [a])
    await _handle_turn(app, a, "exit")                           # in shell mode → the whole line is a command
    assert a.in_shell is False
    assert any(e["type"] == "notice" and "Back to the agent" in e["text"] for e in a.sent)
    assert a.sent[-1]["type"] == "turn_done"


async def test_command_notices_the_caller_and_refreshes_everyones_context():
    shell = FakeShell()
    a, b = FakeConn("alice"), FakeConn("bob")
    app = _app(shell, [a, b])
    await _handle_turn(app, a, "conjure llm gemini")   # typed form is the noun command now
    assert any(e["type"] == "notice" and "Gemini" in e["text"] for e in a.sent)   # output → the caller
    assert not any(e["type"] == "notice" for e in b.sent)                          # …not the others
    assert any(e["type"] == "context" for e in a.sent)                             # shared LLM change →
    assert any(e["type"] == "context" for e in b.sent)                             # everyone's prompt refreshes
    assert a.sent[-1]["type"] == "turn_done"


# --------------------------------------------------------------------------- context (data, per-connection)

def test_context_event_is_per_connection_data_no_prompt_string():
    shell = FakeShell()
    live = {"world": "garden", "space": "daniel/home", "scope": "daniel/agents/builder", "owner": "daniel"}
    ev = _context_event(shell, live, "guest", True)
    assert ev["type"] == "context" and ev["agent"] == "builder" and ev["llm"] == "Claude"
    assert ev["user"] == "guest" and ev["in_shell"] is True                        # this connection's own state
    assert ev["world"] == "garden" and ev["space"] == "daniel/home"
    assert "prompt" not in ev                                                       # DATA only — the client formats


def test_backlog_is_context_then_transcript():
    shell = FakeShell()
    shell.director.transcript = [Turn("user", "hi", by="alice"), Turn("assistant", "hello")]
    events = _backlog_events(shell, None, "bob", False)
    assert events[0]["type"] == "context" and events[0]["user"] == "bob"
    assert events[1] == {"type": "user_turn", "speaker": "alice", "text": "hi", "backlog": True}
    assert events[2] == {"type": "assistant_final", "text": "hello", "backlog": True}


# --------------------------------------------------------------------------- follow (C2)

async def test_reconcile_rebinds_on_agent_change_and_refreshes_all_connections():
    shell = FakeShell()
    a, b = FakeConn("alice"), FakeConn("bob")
    app = _app(shell, [a, b])
    await _reconcile_state(app, {"agent": "outdoor", "scope": "daniel/agents/outdoor",
                                 "world": "default", "space": "<void>", "owner": "daniel"})
    assert shell.opened == [("outdoor", False)]                  # re-bound, not re-asserting the world
    assert shell.director.agent.name == "outdoor"
    assert app.state.live["agent"] == "outdoor"
    for c in (a, b):
        ctx = [e for e in c.sent if e["type"] == "context"][-1]
        assert ctx["agent"] == "outdoor" and ctx["world"] == "default"


# --------------------------------------------------------------------------- /ws endpoint

def test_ws_sends_context_on_connect_and_roundtrips_a_turn():
    from fastapi.testclient import TestClient
    with TestClient(build_app(get_settings(), shell=FakeShell())) as client:
        with client.websocket_connect("/ws?user=alice") as ws:
            first = ws.receive_json()
            assert first["type"] == "context" and first["user"] == "alice" and first["in_shell"] is False
            ws.send_json({"type": "turn", "text": "put a tree"})
            kinds = []
            for _ in range(20):
                ev = ws.receive_json()
                kinds.append(ev["type"])
                if ev["type"] == "turn_done":
                    break
    assert "user_turn" in kinds and "assistant_final" in kinds and kinds[-1] == "turn_done"
