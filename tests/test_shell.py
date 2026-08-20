"""The shell — the deterministic command plane above the agent (conjure.shell).

Commands are parsed, never sent to an LLM. The key behaviours: in agent mode only `conjure`-prefixed
input is a command (so agent content like "put a shell on the table" passes through); in shell mode
every line is a command and non-commands are rejected (never forwarded to the LLM)."""
import pytest

from conjure.shell import Shell


class FakeDirector:
    """Just what the shell reads/forwards to: a roster, an active LLM, an agent, and async handle()."""

    def __init__(self):
        self.roster = {"Claude": object(), "Gemini": object()}
        self.active = "Claude"
        self.agent = type("A", (), {"name": "builder"})()
        self._tools = [object(), object()]
        self.user = "daniel"
        self.handled = []

    async def handle(self, text, *, speaker=None, on_text=None, on_tool=None):
        self.handled.append((speaker, text))
        if on_text:
            await on_text(f"did «{text}»", final=True, speaker=self.active)


def _shell():
    d = FakeDirector()
    out = []

    async def on_text(text, *, final, speaker):
        out.append((speaker, text))

    return Shell(d), d, out, on_text


# --------------------------------------------------------------------------- routing engine (mode as a param)

def test_as_command_routes_by_the_passed_mode_not_instance_state():
    # The shell (server-side command engine) takes in_shell as a PARAMETER, so one instance serves many
    # connections each with their own mode (agent server, D). No wake word / phrases live in the client.
    sh, d, out, on_text = _shell()
    # agent mode: only a `conjure`-led line is a command
    assert sh.as_command("make a tree", False) is None
    assert sh.as_command("conjure use gemini", False) == "use gemini"
    assert sh.as_command("conjure", False) == "open shell"        # bare wake → open shell
    # shell mode: every line is a command
    assert sh.as_command("use gemini", True) == "use gemini"
    assert sh.as_command("make a tree", True) == "make a tree"
    # the two mode toggles are recognised server-side
    assert sh.is_open_shell("open shell") and sh.is_open_shell("shell")
    assert sh.is_leave_shell("exit") and sh.is_leave_shell("done")
    assert not sh.is_leave_shell("exit the room")


# --------------------------------------------------------------------------- agent mode (default)

async def test_plain_text_in_agent_mode_is_forwarded():
    sh, d, out, on_text = _shell()
    await sh.feed("put an oak tree in front of me", on_text=on_text)
    assert d.handled == [("daniel", "put an oak tree in front of me")]   # went to the agent, not the shell


async def test_agent_content_mentioning_shell_is_not_a_command():
    # The whole reason for the wake word: "shell" as content must reach the agent.
    sh, d, out, on_text = _shell()
    await sh.feed("put a shell on the table", on_text=on_text)
    assert d.handled == [("daniel", "put a shell on the table")] and sh.in_shell is False


async def test_conjure_open_shell_enters_shell_mode():
    sh, d, out, on_text = _shell()
    await sh.feed("conjure open shell", on_text=on_text)
    assert sh.in_shell is True and d.handled == []           # a command, not forwarded
    assert any("Shell" in t for _, t in out)


async def test_bare_conjure_opens_the_shell():
    sh, d, out, on_text = _shell()
    await sh.feed("conjure", on_text=on_text)
    assert sh.in_shell is True


async def test_conjure_use_switches_llm_inline_without_entering_shell():
    sh, d, out, on_text = _shell()
    await sh.feed("conjure use Gemini", on_text=on_text)
    assert d.active == "Gemini" and sh.in_shell is False and d.handled == []


# --------------------------------------------------------------------------- shell mode

async def test_in_shell_mode_every_line_is_a_command_not_forwarded():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("make a tree", on_text=on_text)             # not a command
    assert d.handled == []                                    # NOT sent to the LLM
    assert any("Unknown command" in t for _, t in out)


async def test_exit_leaves_shell_mode():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("exit", on_text=on_text)
    assert sh.in_shell is False and any("Back to" in t for _, t in out)


async def test_switch_llm_in_shell_mode():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("talk to Gemini", on_text=on_text)
    assert d.active == "Gemini" and any("Gemini" in t for _, t in out)
    out.clear()
    await sh.feed("gemini", on_text=on_text)                  # bare known name also switches
    assert d.active == "Gemini"


async def test_help_and_llms_list_without_touching_the_agent():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("help", on_text=on_text)
    await sh.feed("llms", on_text=on_text)
    assert d.handled == []
    text = "\n".join(t for _, t in out)
    assert "Commands:" in text and "Claude" in text and "Gemini" in text


# --------------------------------------------------------------------------- prompt

def test_prompt_reflects_mode():
    sh, d, out, on_text = _shell()
    assert sh.prompt() == "conjure:daniel.builder.claude> "   # user · agent-primary; the LLM can vary
    sh.in_shell = True
    assert sh.prompt() == "conjure:daniel.shell> "            # same shape, `shell` in the agent slot


async def test_whoami_identifies_user_agent_llm_session():
    sh, d, out, on_text = _shell()
    await sh._status(on_text)
    line = out[-1][1]
    assert "user: daniel" in line and "agent: builder" in line and "LLM: Claude" in line
    assert "session:" in line and "agent mode" in line       # session is fetched from the world server


# --------------------------------------------------------------------------- dir / delete (admin)

def _admin_shell(responder):
    """A shell whose /admin calls are served by `responder(action, path)` — no network."""
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    calls = []

    async def fake_admin(action, path):
        calls.append((action, path))
        return responder(action, path)

    sh._admin = fake_admin
    return sh, out, on_text, calls


async def test_dir_lists_the_namespace_via_admin():
    node = {"label": "/", "kind": "root", "children": [{"label": "alice", "kind": "user"}]}
    sh, out, on_text, calls = _admin_shell(lambda a, p: {"ok": True, "node": node})
    await sh.feed("dir", on_text=on_text)
    assert calls == [("tree", "/")]                          # bare dir → root
    assert any("alice" in t for _, t in out)


async def test_dir_narrows_by_path():
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: {"ok": True, "node": {"label": "worlds", "kind": "category"}})
    await sh.feed("dir /alice/worlds", on_text=on_text)
    assert calls == [("tree", "/alice/worlds")]


async def test_dir_is_not_a_command_in_agent_mode():
    sh, d, out, on_text = _shell()                           # not in shell → needs the wake word
    await sh.feed("dir", on_text=on_text)
    assert d.handled == [("daniel", "dir")]                              # forwarded to the agent, not run as a command


async def test_delete_asks_before_acting_then_confirms():
    def responder(action, path):
        if action == "tree":
            return {"ok": True, "node": {"label": "alice", "kind": "user",
                                         "children": [{"label": "w1", "kind": "world"}]}}
        return {"ok": True, "deleted": "user 'alice'"}

    sh, out, on_text, calls = _admin_shell(responder)
    await sh.feed("delete /alice", on_text=on_text)
    assert calls == [("tree", "/alice")]                     # previewed, NOT yet deleted
    assert sh._pending_delete == "/alice"
    assert any("confirm" in t.lower() for _, t in out)
    await sh.feed("y", on_text=on_text)
    assert ("delete", "/alice") in calls and sh._pending_delete is None
    assert any("Deleted" in t for _, t in out)


async def test_delete_cancels_on_anything_but_yes():
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: {"ok": True, "node": {"label": "alice", "kind": "user"}})
    await sh.feed("delete /alice", on_text=on_text)
    await sh.feed("no", on_text=on_text)
    assert sh._pending_delete is None
    assert all(a != "delete" for a, _ in calls)              # never fired
    assert any("Cancelled" in t for _, t in out)


async def test_delete_preview_error_does_not_arm():
    sh, out, on_text, calls = _admin_shell(lambda a, p: {"ok": False, "error": "no such user 'ghost'"})
    await sh.feed("delete /ghost", on_text=on_text)
    assert sh._pending_delete is None                        # bad target → nothing armed
    assert all(a != "delete" for a, _ in calls)
    assert any("ghost" in t for _, t in out)


# --------------------------------------------------------------------------- session verbs (step 3b)

def _session_shell(responder):
    sh, d, out, on_text = _shell()
    calls = []

    async def fake_api(method, path, **kw):
        calls.append((method, path, kw))
        return responder(method, path, kw)

    sh._settings = type("S", (), {"world_url": "http://x"})()
    sh._session_api = fake_api
    return sh, out, on_text, calls


async def test_sessions_lists_via_the_world_server():
    sh, out, on_text, calls = _session_shell(lambda mth, p, kw: {"ok": True, "active": "session-1", "sessions": [
        {"id": "session-1", "title": "Home Base", "active_world": "home", "llm": "Claude", "active": True},
        {"id": "session-2", "title": "Playground", "active_world": "home", "llm": "", "active": False}]})
    await sh._dispatch("sessions", on_text)
    assert calls[0][0] == "GET" and calls[0][1] == "/sessions"
    assert calls[0][2]["scope"] == "daniel/agents/builder"                # the live (user, agent) scope
    text = out[-1][1]
    assert "* Home Base" in text and "Playground" in text                 # active marked, both listed


async def test_session_verbs_route_to_the_right_endpoints():
    sh, out, on_text, calls = _session_shell(
        lambda mth, p, kw: {"ok": True, "session": "session-2", "title": kw.get("title")})
    await sh._dispatch("session new Playground", on_text)
    assert calls[-1][:2] == ("POST", "/session/new") and calls[-1][2]["title"] == "Playground"
    await sh._dispatch("session switch Playground", on_text)
    assert calls[-1][:2] == ("POST", "/session/switch") and calls[-1][2]["session"] == "Playground"
    await sh._dispatch('session "Home Base"', on_text)                    # 1 quoted arg → your own session
    assert calls[-1][:2] == ("POST", "/session/switch") and calls[-1][2]["session"] == "Home Base"
    assert "owner" not in calls[-1][2]
    await sh._dispatch('session daniel "Home Base"', on_text)             # 2 args → VISIT that user's session
    assert calls[-1][2]["owner"] == "daniel" and calls[-1][2]["session"] == "Home Base"
    await sh._dispatch("session rename Cozy Corner", on_text)
    assert calls[-1][:2] == ("POST", "/session/rename") and calls[-1][2]["title"] == "Cozy Corner"
    await sh._dispatch("session delete session-2", on_text)
    assert calls[-1][:2] == ("POST", "/session/delete") and calls[-1][2]["session"] == "session-2"
    await sh._dispatch("session private", on_text)
    assert calls[-1][:2] == ("POST", "/session/visibility") and calls[-1][2]["public"] is False
    await sh._dispatch("session public", on_text)
    assert calls[-1][:2] == ("POST", "/session/visibility") and calls[-1][2]["public"] is True


async def test_sessions_listing_marks_live_and_last_used():
    def api(mth, p, kw):
        return {"ok": True, "active": "session-1",
                "sessions": [{"id": "session-1", "title": "Mine", "active_world": "w", "public": True,
                              "active": True}],
                "available": [{"scope": "alice/agents/builder", "owner": "alice", "agent": "builder",
                               "session": "s1", "title": "Alice One", "active_world": "wl"}],
                "live": {"scope": "alice/agents/builder", "session": "s1"}}   # live is alice's — you're visiting
    sh, out, on_text, calls = _session_shell(api)
    await sh._dispatch("sessions", on_text)
    text = out[-1][1]
    assert "* Mine" in text                     # your last-used (per-agent resume) → *
    assert "@ alice" in text                    # the one live session (you're here), in the others' list → @
    assert "@ = live session" in text           # legend explains the markers


async def test_session_command_acts_as_the_speaker_not_the_host():
    # A guest's session verb must target the GUEST's own scope — not the shared shell's host user — so a
    # guest can't manage the host's sessions (reported bug: guest made the owner's session private).
    sh, out, on_text, calls = _session_shell(lambda mth, p, kw: {"ok": True, "public": kw.get("public")})
    await sh._dispatch("session private", on_text, speaker="guest")
    assert calls[-1][:2] == ("POST", "/session/visibility")
    assert calls[-1][2]["scope"] == "guest/agents/builder"          # guest's own scope, not daniel's


async def test_shared_effect_verbs_refused_for_a_non_permitted_speaker():
    # §6d: a bumped guest (permitted=False) can't drive the shared session — switch/new/agent are refused,
    # but own-scope management (private/public/rename/delete) still works.
    sh, out, on_text, calls = _session_shell(lambda mth, p, kw: {"ok": True})
    await sh._dispatch("session switch beach", on_text, speaker="guest", permitted=False)
    assert not calls and "private" in out[-1][1].lower()            # refused before any POST
    await sh._dispatch("session new", on_text, speaker="guest", permitted=False)
    assert not calls
    await sh._dispatch("session private", on_text, speaker="guest", permitted=False)   # own-scope: allowed
    assert calls[-1][:2] == ("POST", "/session/visibility")


async def test_llm_switch_and_delete_refused_for_a_non_permitted_speaker():
    # The active LLM is SHARED, and delete is destructive — both refused for a bumped guest (§6d).
    sh, d, out, on_text = _shell()
    sh._permitted = False
    handled = await sh._switch("use gemini", on_text)
    assert handled and d.active == "Claude" and "private" in out[-1][1].lower()   # LLM unchanged, refused
    import types as _t
    await sh._delete(on_text, _t.SimpleNamespace(group=lambda k: "/daniel/worlds/x"))
    assert sh._pending_delete is None and "private" in out[-1][1].lower()          # delete refused, not armed


async def test_session_error_is_surfaced_to_the_client():
    sh, out, on_text, calls = _session_shell(lambda *a: {"ok": False, "error": "no session 'zzz'"})
    await sh._dispatch("session switch zzz", on_text)
    assert "no session" in out[-1][1]


# --------------------------------------------------------------------------- agent switching

async def test_agent_switch_unavailable_in_handbuilt_shell():
    # A Shell(director) with no session-owned lifecycle can't relaunch an agent's MCP server.
    sh, d, out, on_text = _shell()
    await sh.feed("conjure agent outdoor", on_text=on_text)
    assert any("isn't available" in t for _, t in out)
    assert d.handled == []                                   # not forwarded to the agent as content


async def test_agent_switch_to_unknown_reports_available(monkeypatch):
    from contextlib import AsyncExitStack
    sh, d, out, on_text = _shell()
    sh._stack = AsyncExitStack()                             # pretend session-managed
    tried = []
    monkeypatch.setattr(sh, "_open_agent", lambda name: tried.append(name))
    await sh.feed("conjure agent nope", on_text=on_text)
    assert tried == []                                       # never attempted to open a bad agent
    assert any("No agent 'nope'" in t for _, t in out)       # and it lists what IS available


async def test_agent_switch_opens_the_named_agent(monkeypatch):
    from contextlib import AsyncExitStack
    sh, d, out, on_text = _shell()                           # current agent = builder
    sh._stack = AsyncExitStack()

    async def fake_open(name):                               # stand in for the real connect/relaunch
        sh._director = type("D", (), {"agent": type("A", (), {"name": name})(), "active": "Claude"})()

    monkeypatch.setattr(sh, "_open_agent", fake_open)
    await sh.feed("conjure agent outdoor", on_text=on_text)
    assert sh._agent_name() == "outdoor"                     # the shell now drives the new agent
    assert any("Switched to agent outdoor" in t for _, t in out)


async def test_agent_switch_uses_the_host_hook_when_set(monkeypatch):
    # In the agent server the switch is DELEGATED (routed via the world server, then its follower re-binds
    # in the owning task) — never the in-process _open_agent teardown, which from a spawned turn task is a
    # cross-task MCP aclose ("exit a cancel scope in a different task").
    from contextlib import AsyncExitStack
    sh, d, out, on_text = _shell()                           # current agent = builder
    sh._stack = AsyncExitStack()
    opened = []
    monkeypatch.setattr(sh, "_open_agent", lambda *a, **k: opened.append(a))
    hooked = []

    async def hook(name, cb):
        hooked.append(name)
        await cb(f"Switching to agent {name}…", final=True, speaker="Claude")

    sh._agent_switch_hook = hook
    await sh.feed("conjure agent outdoor", on_text=on_text)
    assert hooked == ["outdoor"]                             # delegated to the host
    assert opened == []                                      # in-process teardown NOT used (no cross-task aclose)
    assert any("Switching to agent outdoor" in t for _, t in out)


async def test_agent_switch_already_on_it_is_a_noop(monkeypatch):
    from contextlib import AsyncExitStack
    sh, d, out, on_text = _shell()                           # already builder
    sh._stack = AsyncExitStack()
    tried = []
    monkeypatch.setattr(sh, "_open_agent", lambda name: tried.append(name))
    await sh.feed("conjure agent builder", on_text=on_text)
    assert tried == [] and any("Already on builder" in t for _, t in out)


async def test_open_agent_closes_current_before_opening_next(monkeypatch):
    # Exercises the REAL _open_agent (not a monkeypatched stand-in): the current agent's connection
    # must be torn down BEFORE the next is opened — anyio cancel scopes unwind LIFO in one task, so
    # opening the new on top and closing the old underneath is the bug the live run hit.
    from conjure import shell as shell_mod

    log = []

    class _FakeConnect:
        def __init__(self, agent):
            self.agent = agent

        async def __aenter__(self):
            log.append(("open", self.agent))
            return type("D", (), {"agent": type("A", (), {"name": self.agent})(),
                                  "active": "Claude", "roster": {"Claude": object()}})()

        async def __aexit__(self, *exc):
            log.append(("close", self.agent))
            return False

    monkeypatch.setattr(shell_mod.Director, "connect",
                        lambda settings, *, agent, user, errlog: _FakeConnect(agent))

    sh = Shell(None, settings=None)
    sh._user = "daniel"
    await sh._open_agent("builder")          # initial open (session start)
    await sh._open_agent("outdoor")          # switch
    assert log == [("open", "builder"), ("close", "builder"), ("open", "outdoor")]
    assert sh._agent_name() == "outdoor"


async def test_open_agent_restores_previous_on_failure(monkeypatch):
    from conjure import shell as shell_mod
    opened = []

    class _FlakyConnect:
        def __init__(self, agent):
            self.agent = agent

        async def __aenter__(self):
            if self.agent == "outdoor":
                raise RuntimeError("no LLM key for outdoor")
            opened.append(self.agent)
            return type("D", (), {"agent": type("A", (), {"name": self.agent})(),
                                  "active": "Claude", "roster": {"Claude": object()}})()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(shell_mod.Director, "connect",
                        lambda settings, *, agent, user, errlog: _FlakyConnect(agent))

    sh = Shell(None, settings=None)
    sh._user = "daniel"
    await sh._open_agent("builder")
    with pytest.raises(RuntimeError, match="no LLM key"):
        await sh._open_agent("outdoor")      # fails…
    assert sh._agent_name() == "builder"     # …and the shell is restored to builder, not stranded


async def test_open_agent_activates_the_new_agents_world(monkeypatch):
    # On switch, the shell asks the world server to make a world in the NEW agent's scope live.
    from conjure import shell as shell_mod

    class _C:
        def __init__(self, agent):
            self.agent = agent

        async def __aenter__(self):
            return type("D", (), {"agent": type("A", (), {"name": self.agent})(),
                                  "active": "Claude", "roster": {"Claude": object()}})()

        async def __aexit__(self, *e):
            return False

    monkeypatch.setattr(shell_mod.Director, "connect",
                        lambda settings, *, agent, user, errlog: _C(agent))
    sh = Shell(None, settings=None)
    sh._user = "daniel"
    activated = []

    async def fake_activate(agent):
        activated.append(agent)

    monkeypatch.setattr(sh, "_activate_world", fake_activate)
    await sh._open_agent("builder")
    await sh._open_agent("outdoor")
    assert activated == ["builder", "outdoor"]


async def test_session_resolves_none_agent_to_last_used(monkeypatch):
    from conjure import shell as shell_mod

    class _C:
        def __init__(self, agent):
            self.agent = agent

        async def __aenter__(self):
            return type("D", (), {"agent": type("A", (), {"name": self.agent})(),
                                  "active": "Claude", "roster": {"Claude": object()}})()

        async def __aexit__(self, *e):
            return False

    monkeypatch.setattr(shell_mod.Director, "connect",
                        lambda settings, *, agent, user, errlog: _C(agent))

    async def fake_last(self):
        return "outdoor"

    monkeypatch.setattr(shell_mod.Shell, "_last_agent", fake_last)

    async with shell_mod.Shell.session(settings=None, agent=None, user="daniel") as sh:
        assert sh._agent_name() == "outdoor"                  # None resumes the last-used agent
    async with shell_mod.Shell.session(settings=None, agent="builder", user="daniel") as sh:
        assert sh._agent_name() == "builder"                  # explicit --agent overrides


async def test_last_agent_falls_back_to_builder_without_server():
    sh = Shell(None, settings=None)
    sh._user = "daniel"
    assert await sh._last_agent() == "builder"                # no world_url → safe default


async def test_session_switch_own_vs_visit_by_arg_count():
    """`session <name>` switches your own; `session <user> <name>` visits — the arg count decides, and
    quoting keeps a spaced name as one token (no paths)."""
    sh, out, on_text, calls = _session_shell(lambda mth, p, kw: {"ok": True, "session": "s", "owner": kw.get("owner")})
    await sh._dispatch("session beach", on_text)                          # 1 arg → own
    assert calls[-1][2]["session"] == "beach" and "owner" not in calls[-1][2]
    await sh._dispatch("session daniel beach", on_text)                   # 2 args → visit
    assert calls[-1][2]["owner"] == "daniel" and calls[-1][2]["session"] == "beach"
    await sh._dispatch('session daniel "blade runner"', on_text)          # quoted spaced name stays one token
    assert calls[-1][2]["owner"] == "daniel" and calls[-1][2]["session"] == "blade runner"


async def test_session_clear_wipes_the_director_transcript():
    sh, d, out, on_text = _shell()
    d.transcript = ["u1", "a1", "u2", "a2"]            # accumulated history
    sh.in_shell = True
    await sh.feed("session clear", on_text=on_text)    # hand-built shell → in-memory clear path
    assert d.transcript == []
    assert any("cleared" in t.lower() for _, t in out)
