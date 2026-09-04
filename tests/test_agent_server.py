"""Agent server (shared-session Step C/D) — the WebSocket host of the shared shell/Director.

`_handle_turn` (per-connection routing), the fan-out, and `_reconcile_state` are exercised directly with
a fake shell + fake connections — no network, no MCP subprocess, no LLM. The `/ws` endpoint is covered
with a TestClient over an injected fake shell (build_app's `shell=` bypasses the real Shell.session)."""

import asyncio
import json
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
    cwd_display = "/daniel/agents/builder"   # …and the same place in names, which is what the prompt shows

    async def _dispatch(self, cmd, on_text, *, speaker=None, permitted=True, cwd="", cwd_display="",
                        voice=False):
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
        self.cwd_display = ""
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


async def test_voice_hears_speakable_text_while_the_cli_sees_what_was_written():
    """The same reply, rendered twice. An LLM writes asterisks and emoji for a screen; through a TTS
    engine they are noise or silence. The rewrite happens at FAN-OUT, per connection — so the CLI keeps
    the written form and the two clients stay in one conversation."""
    from conjure.agent_server import _conv_broadcast

    shell = FakeShell()
    voice, cli = FakeConn("alice", kind="voice"), FakeConn("alice", kind="cli")
    app = _app(shell, [voice, cli])
    await _conv_broadcast(app, {"type": "assistant_final", "text": "**Done!** 🎉"})

    assert voice.sent[-1]["text"] == "Done! party popper emoji"
    assert cli.sent[-1]["text"] == "**Done!** 🎉"


async def test_only_llm_prose_is_rewritten_for_voice():
    """`user_turn` is the speaker's own words echoed back and `notice` is server-authored — neither is
    LLM prose, so neither is touched. Rewriting the echo would make a client hear something different
    from what it just said."""
    from conjure.agent_server import _conv_broadcast

    shell = FakeShell()
    voice = FakeConn("alice", kind="voice")
    app = _app(shell, [voice])
    await _conv_broadcast(app, {"type": "user_turn", "speaker": "alice", "text": "make it *big*"})
    await _conv_broadcast(app, {"type": "notice", "text": "Setting up your new world…"})

    assert voice.sent[0]["text"] == "make it *big*"
    assert voice.sent[1]["text"] == "Setting up your new world…"


async def test_the_rewrite_is_a_rendering_not_an_edit():
    """The transcript stores what the LLM WROTE; only the wire to a voice client carries the spoken
    form. Otherwise switching clients mid-conversation would rewrite history, and a CLI reading back
    would see text its own LLM never produced."""
    shell = FakeShell()

    async def handle(text, *, speaker, on_text=None, on_tool=None):
        if on_text:
            await on_text("**Done!** 🎉", final=True, speaker="Claude")
        shell.director.transcript.append(Turn("user", text.strip(), by=speaker))
        shell.director.transcript.append(Turn("assistant", "**Done!** 🎉"))

    shell.director.handle = handle
    conn = FakeConn("alice", kind="voice")
    app = _app(shell, [conn])
    await _handle_turn(app, conn, "make a dragon")

    heard = next(e for e in conn.sent if e["type"] == "assistant_final")
    assert heard["text"] == "Done! party popper emoji"                     # what the engine says
    assert shell.director.transcript[-1].text == "**Done!** 🎉"            # what was written, unchanged


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


async def test_a_guest_who_chose_the_shell_is_not_yanked_out_of_it():
    """A `--open-shell` client (or anyone who typed `open shell`) is ALREADY where the private-session
    bump would put them. Claiming that as our bump means the session going public drags them back to the
    agent — out of the mode they asked for."""
    from conjure.agent_server import _apply_bumps
    guest = FakeConn("bob")
    guest.in_shell = True                                            # launched with --open-shell
    app = _app(FakeShell(), [guest])
    app.state.live = {"public": False, "owner": "daniel"}
    await _apply_bumps(app)
    assert guest.in_shell and not guest.bumped                       # nothing to bump — not ours to undo
    app.state.live = {"public": True, "owner": "daniel"}
    await _apply_bumps(app)
    assert guest.in_shell                                            # still in the shell they chose


def test_open_shell_into_a_private_session_survives_it_going_public():
    from fastapi.testclient import TestClient
    app = build_app(get_settings(), shell=FakeShell())
    with TestClient(app) as client:
        app.state.live = {"public": False, "owner": "daniel"}
        with client.websocket_connect("/ws?user=bob&shell=1"):
            conn = next(c for c in app.state.hub.conns if c.user == "bob")
            assert conn.in_shell and not conn.bumped                 # theirs, not ours


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


async def test_a_session_meta_without_the_flags_is_never_constructed(tmp_path):
    """The other half of the agent-switch bug: both hooks gate on `is not False`, so a meta with the flag
    ABSENT (rather than False) is skipped forever — no seed, no greeting. That guard is deliberate (a
    legacy session must not be retro-greeted), which is why the fix is for every mint path to WRITE the
    flags (`server._ensure_session`) rather than to loosen the check here."""
    from conjure.agent_server import _maybe_greet, _maybe_seed
    sessions = SessionRepository(tmp_path)
    scope = "daniel/agents/dm"
    sessions.save_meta(scope, "session-1", {"id": "session-1", "owner": "daniel", "agent": "dm",
        "title": "S", "public": True, "active_world": "home", "llm": ""})      # NO greeted/seeded
    shell = FakeShell()
    shell.director.agent = types.SimpleNamespace(name="dm", session={"greeting": "Hello there"},
                                                 state={"map": {"seed_data": {"start": "home"}}})
    app = _app(shell, [], sessions=sessions)
    app.state.live = {"scope": scope, "session": "session-1", "agent": "dm"}
    _maybe_seed(app)
    await _maybe_greet(app)
    assert sessions.state(scope, "session-1").list() == []      # no state file written at all
    assert shell.director.transcript == []                      # and no greeting spoken
    # write the flags the way a freshly-minted session now does, and both hooks fire
    meta = sessions.load_meta(scope, "session-1")
    meta.update(greeted=False, seeded=False)
    sessions.save_meta(scope, "session-1", meta)
    _maybe_seed(app)
    await _maybe_greet(app)
    assert sessions.state(scope, "session-1").read("map") == {"start": "home"}
    assert [t.text for t in shell.director.transcript] == ["Hello there"]


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


async def test_an_agent_change_nobody_here_asked_for_is_announced():
    """Co-location can move the agent under you: an AR client matches a space, the world server joins
    that space's last-active world, and the live scope becomes whatever agent owns it. That's intended
    — the space owns the world owns the scope — but it used to happen in silence, so you kept talking
    and a different agent answered. Name the destination; world + space is what makes a room match
    recognisable as one."""
    shell = FakeShell()
    shell.director.agent.name = "outdoor"                        # mid-session in an outdoor world…
    a, b = FakeConn("alice"), FakeConn("bob")
    app = _app(shell, [a, b])
    await _reconcile_state(app, {"agent": "builder", "scope": "daniel/agents/builder",
                                 "world": "animal-house", "space": "daniel/whipple", "owner": "daniel"})
    for c in (a, b):                                             # everyone present, not just the owner
        assert [e["text"] for e in c.sent if e["type"] == "notice"] == [
            "[now in the builder agent — animal-house · daniel/whipple]"]


async def test_a_switch_this_server_asked_for_is_not_announced_twice():
    # `agent <name>` already narrates ("Switching to agent outdoor…") and reaches the world server, whose
    # /ws broadcast comes straight back here. Without the claim the follower would echo it.
    shell = FakeShell()
    conn = FakeConn("alice")
    app = _app(shell, [conn])
    app.state.expect_agent = "outdoor"                           # what _make_agent_switch_hook sets
    await _reconcile_state(app, {"agent": "outdoor", "scope": "daniel/agents/outdoor",
                                 "world": "default", "space": "<void>", "owner": "daniel"})
    assert shell.director.agent.name == "outdoor"                # still followed…
    assert not [e for e in conn.sent if e["type"] == "notice"]   # …just not narrated a second time
    assert app.state.expect_agent is None                        # one switch, one claim


async def test_an_unrelated_snapshot_does_not_burn_the_claim():
    """Snapshots arrive for all sorts of reasons (a room capture, a world switch). One landing between
    `agent <name>` and the switch it asked for must not consume the claim, or the switch that follows
    gets announced on top of the narration the hook already gave."""
    shell = FakeShell()
    conn = FakeConn("alice")
    app = _app(shell, [conn])
    app.state.expect_agent = "outdoor"
    await _reconcile_state(app, {"agent": "builder", "scope": "daniel/agents/builder",   # no agent change
                                 "world": "animal-house", "space": "daniel/whipple", "owner": "daniel"})
    assert app.state.expect_agent == "outdoor"                   # still claimed
    await _reconcile_state(app, {"agent": "outdoor", "scope": "daniel/agents/outdoor",   # the asked-for one
                                 "world": "alien", "space": "<void>", "owner": "daniel"})
    assert not [e for e in conn.sent if e["type"] == "notice"]


async def test_the_claim_does_not_muffle_the_next_unasked_switch():
    shell = FakeShell()
    conn = FakeConn("alice")
    app = _app(shell, [conn])
    app.state.expect_agent = "outdoor"
    await _reconcile_state(app, {"agent": "outdoor", "scope": "daniel/agents/outdoor",
                                 "world": "default", "space": "<void>", "owner": "daniel"})
    await _reconcile_state(app, {"agent": "builder", "scope": "daniel/agents/builder",
                                 "world": "animal-house", "space": "daniel/whipple", "owner": "daniel"})
    assert [e["text"] for e in conn.sent if e["type"] == "notice"] == [
        "[now in the builder agent — animal-house · daniel/whipple]"]


async def test_a_void_world_is_not_named_as_a_space():
    # VOID is a sentinel, not a place — printing '<void>' at someone is noise.
    shell = FakeShell()
    conn = FakeConn("alice")
    app = _app(shell, [conn])
    await _reconcile_state(app, {"agent": "outdoor", "scope": "daniel/agents/outdoor",
                                 "world": "alien", "space": "<void>", "owner": "daniel"})
    assert [e["text"] for e in conn.sent if e["type"] == "notice"] == ["[now in the outdoor agent — alien]"]


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


def test_shell_1_opens_the_connection_already_in_command_mode():
    """`cli --open-shell`. The FIRST context event must already say so — the mode is connection state, not
    a synthetic 'conjure open shell' turn, so nothing lands in the shared transcript and the client's
    prompt is right before it can render a wrong one."""
    from fastapi.testclient import TestClient
    with TestClient(build_app(get_settings(), shell=FakeShell())) as client:
        with client.websocket_connect("/ws?user=alice&shell=1") as ws:
            first = ws.receive_json()
            assert first["type"] == "context" and first["in_shell"] is True
            ws.send_json({"type": "turn", "text": "help"})    # a BARE command — no wake word needed
            events = []
            for _ in range(20):
                ev = ws.receive_json()
                events.append(ev)
                if ev["type"] == "turn_done":
                    break
    assert all(e["type"] != "user_turn" for e in events)      # ran as a command, not said to the agent
    assert any(e["type"] == "notice" for e in events)


def test_the_open_shell_flag_is_in_the_url_so_a_reconnect_restores_it():
    from conjure.agent_client import ws_url
    assert "shell=1" in ws_url("http://h:1", "alice", shell=True)
    assert "shell=1" not in ws_url("http://h:1", "alice")


# --------------------------------------------------------------------------- read-ahead on the socket
#
# Observed 2026-08-28: a `session new` whose generative constructor ran for its full 180s made the CLI
# look DEAD. Every command typed during the wait produced nothing, then the whole backlog flushed at
# once when the slow one finally failed. Nothing was lost — the receive loop simply awaited the handler
# inline, so the following lines were never read off the socket, and the `busy` reply the clients
# already render could not fire for a follow-up on the same connection.

class SlowShell(FakeShell):
    """A shell whose command takes as long as we let it — the generative-constructor shape."""

    def __init__(self):
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()
        self.dispatched: list[str] = []

    async def _dispatch(self, cmd, on_text, *, speaker=None, permitted=True, cwd="", cwd_display="",
                        voice=False):
        self.dispatched.append(cmd)
        self.started.set()
        await self.release.wait()
        await on_text(f"done «{cmd}»", final=True, speaker=self.director.active)


def _drain(ws, until="turn_done", limit=20):
    out = []
    for _ in range(limit):
        ev = ws.receive_json()
        out.append(ev)
        if ev["type"] == until:
            return out
    return out


def test_a_line_typed_during_a_slow_command_is_answered_not_swallowed():
    from fastapi.testclient import TestClient
    shell = SlowShell()
    with TestClient(build_app(get_settings(), shell=shell)) as client:
        with client.websocket_connect("/ws?user=alice&shell=1") as ws:
            ws.receive_json()                                  # context
            ws.send_json({"type": "turn", "text": "session new trash"})
            ws.send_json({"type": "turn", "text": "dir"})      # arrives while the first is still running
            events = _drain(ws)                                # …and must be answered NOW, not in 180s
            assert [e["type"] for e in events] == ["busy", "turn_done"]
            shell.release.set()
            assert any(e["type"] == "notice" for e in _drain(ws))
    # The rejected line never ran — serialization is unchanged, only the silence is gone.
    assert shell.dispatched == ["session new trash"]


def test_the_busy_reply_re_arms_the_client_prompt():
    """`turn_done` is the client's prompt gate and its in-flight counter. A `busy` without one leaves the
    terminal wedged for a different reason than the bug it replaced."""
    from fastapi.testclient import TestClient
    shell = SlowShell()
    with TestClient(build_app(get_settings(), shell=shell)) as client:
        with client.websocket_connect("/ws?user=alice&shell=1") as ws:
            ws.receive_json()
            ws.send_json({"type": "turn", "text": "slow"})
            ws.send_json({"type": "turn", "text": "quick"})
            assert [e["type"] for e in _drain(ws)] == ["busy", "turn_done"]
            shell.release.set()
            _drain(ws)


def test_commands_still_run_one_at_a_time():
    """Read-ahead must not become concurrency: the second command runs only after the first completes."""
    from fastapi.testclient import TestClient
    shell = SlowShell()
    with TestClient(build_app(get_settings(), shell=shell)) as client:
        with client.websocket_connect("/ws?user=alice&shell=1") as ws:
            ws.receive_json()
            ws.send_json({"type": "turn", "text": "first"})
            ws.send_json({"type": "turn", "text": "second"})
            _drain(ws)                                         # the busy pair for "second"
            shell.release.set()
            _drain(ws)                                         # "first" completes
            ws.send_json({"type": "turn", "text": "third"})    # now the floor is free
            _drain(ws)
    assert shell.dispatched == ["first", "third"]              # "second" was refused, never queued


async def test_a_world_side_notice_is_relayed_into_the_conversation(monkeypatch):
    """The world server narrates its slow moments ("Setting up your new world…") on ITS socket, which
    only the headset is on. The follow loop consumed only `state` snapshots, so the person who typed the
    command — on the agent server — waited out a generative constructor in silence."""
    import types

    import conjure.agent_server as a

    sent = []

    class _Conn:
        kind = "cli"
        async def send(self, ev):
            sent.append(ev)

    app = types.SimpleNamespace(state=types.SimpleNamespace(
        settings=get_settings(), user="alice", hub=a.Hub(), live=None,
        stop_follow=asyncio.Event()))
    app.state.hub.add(_Conn())

    class _WS:
        """One world-server message, then the loop is told to stop."""
        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "notice", "text": "Setting up your new world…"})
                app.state.stop_follow.set()
            return gen()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    import websockets                        # imported inside `_follow_world`, so patch the module
    monkeypatch.setattr(websockets, "connect", lambda *_a, **_k: _WS())
    await a._follow_world_state(app)
    assert {"type": "notice", "text": "Setting up your new world…"} in sent


async def test_the_follow_loop_still_ignores_everything_that_is_not_a_notice(monkeypatch):
    """Relaying indiscriminately would push world patch traffic into the conversation pane."""
    import types

    import conjure.agent_server as a

    sent = []

    class _Conn:
        kind = "cli"
        async def send(self, ev):
            sent.append(ev)

    app = types.SimpleNamespace(state=types.SimpleNamespace(
        settings=get_settings(), user="alice", hub=a.Hub(), live=None,
        stop_follow=asyncio.Event()))
    app.state.hub.add(_Conn())

    class _WS:
        def __aiter__(self):
            async def gen():
                yield json.dumps({"type": "patch", "patch": {"rev": 7}})
                yield json.dumps({"type": "notice"})            # no text — nothing to say
                app.state.stop_follow.set()
            return gen()
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    import websockets                        # imported inside `_follow_world`, so patch the module
    monkeypatch.setattr(websockets, "connect", lambda *_a, **_k: _WS())
    await a._follow_world_state(app)
    assert sent == []
