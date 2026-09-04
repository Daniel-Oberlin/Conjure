"""The shell — the deterministic command plane above the agent (conjure.shell).

Commands are parsed, never sent to an LLM. The key behaviours: in agent mode only `conjure`-prefixed
input is a command (so agent content like "put a shell on the table" passes through); in shell mode
every line is a command and non-commands are rejected (never forwarded to the LLM)."""
import types

import pytest

from conjure.shell import Shell


class FakeDirector:
    """Just what the shell reads/forwards to: a roster, an active LLM, an agent, and async handle()."""

    def __init__(self):
        self.roster = {"Claude": object(), "Gemini": object()}
        self.active = "Claude"
        self.agent = type("A", (), {"name": "builder"})()
        self._tools = [types.SimpleNamespace(name="place_asset"),
                       types.SimpleNamespace(name="add_entity")]
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


async def test_conjure_llm_switches_inline_without_entering_shell():
    sh, d, out, on_text = _shell()
    await sh.feed("conjure llm Gemini", on_text=on_text)
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


async def test_llm_switches_in_shell_mode():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("llm Gemini", on_text=on_text)
    assert d.active == "Gemini" and any("Gemini" in t for _, t in out)


async def test_typed_llm_switching_is_the_noun_command_only():
    # Clean break: "talk to X"/"use X" and a bare LLM name used to switch in TEXT, which quietly reserved
    # every roster name as a command word. They're spoken aliases now (see the voice test below).
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    for line in ("talk to Gemini", "use Gemini", "gemini"):
        await sh.feed(line, on_text=on_text)
        assert d.active == "Claude", f"{line!r} should not switch the LLM in text"
    assert all("Unknown command" in t for _, t in out)


async def test_spoken_aliases_switch_the_llm_for_a_voice_client():
    sh, d, out, on_text = _shell()
    await sh._dispatch("talk to Gemini", on_text, voice=True)
    assert d.active == "Gemini"


async def test_a_voice_client_is_refused_the_terminal_commands():
    sh, d, out, on_text = _shell()
    await sh._dispatch("dir", on_text, voice=True)
    assert "terminal command" in out[-1][1]
    assert any(h.startswith("dir") for _, _, h, v in sh._table if not v)   # …and it IS marked CLI-only


async def test_help_and_llms_list_without_touching_the_agent():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("help", on_text=on_text)
    await sh.feed("llms", on_text=on_text)
    assert d.handled == []
    text = "\n".join(t for _, t in out)
    assert "Commands" in text and "Claude" in text and "Gemini" in text


# --------------------------------------------------------------------------- prompt

def test_prompt_formatting_belongs_to_the_client():
    # `Shell.prompt()` is gone — the prompt (now including the shell's cwd) is formatted client-side from
    # `context` data by `agent_client.prompt_from_context`, which is where it's tested.
    sh, d, out, on_text = _shell()
    assert not hasattr(sh, "prompt")


async def test_where_locates_you_in_one_line():
    sh, d, out, on_text = _shell()
    await sh._where(on_text)
    line = out[-1][1]
    assert "user: daniel" in line and "agent: builder" in line and "LLM: Claude" in line
    assert "session:" in line and "world:" in line and "agent mode" in line


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


def _listing(path, *children):
    return {"ok": True, "path": path, "children": list(children)}


async def test_dir_starts_in_your_own_scope_not_the_root():
    # A bare `dir` at the root used to dump every user's worlds, spaces and assets. It now lists one
    # level, starting where you actually are.
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: _listing(p, {"label": "sessions", "kind": "category"}))
    await sh.feed("dir", on_text=on_text)
    assert calls == [("tree", "/daniel/agents/builder")]
    assert "sessions/" in out[-1][1]                         # a trailing / marks what you can cd into


async def test_dir_takes_absolute_home_and_relative_paths():
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(p))
    for arg, want in [("/alice/spaces", "/alice/spaces"),
                      ("~/spaces", "/daniel/spaces"),
                      ("sessions", "/daniel/agents/builder/sessions"),
                      ("../outdoor", "/daniel/agents/outdoor"),
                      ("..", "/daniel/agents")]:
        calls.clear()
        await sh.feed(f"dir {arg}", on_text=on_text)
        assert calls == [("tree", want)], arg


async def test_cd_remembers_the_path_the_server_resolved():
    # `…/worlds` is a shortcut for the ACTIVE session's worlds; remembering the shortcut would silently
    # point somewhere else after a session switch, so we adopt what the server resolved it to.
    real = "/daniel/agents/builder/sessions/session-1/worlds"
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(real))
    await sh.feed("cd worlds", on_text=on_text)
    assert sh.cwd == real
    assert out[-1][1] == "~/agents/builder/sessions/session-1/worlds"   # ~ for your own home


async def test_cd_with_no_argument_returns_to_your_agent():
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(p))
    await sh.feed("cd /alice", on_text=on_text)
    assert sh.cwd == "/alice"
    await sh.feed("cd", on_text=on_text)
    assert sh.cwd == "/daniel/agents/builder"


async def test_a_failed_cd_leaves_you_where_you_were():
    sh, out, on_text, calls = _admin_shell(lambda a, p: {"ok": False, "error": "no such user 'nope'"})
    await sh.feed("cd /nope", on_text=on_text)
    assert sh.cwd == "/daniel/agents/builder" and "no such user" in out[-1][1]


async def test_show_renders_the_fields_the_server_returns():
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: {"ok": True, "path": p, "kind": "world",
                      "fields": [["world", "meadow"], ["entities", "24"]]})
    await sh.feed("show worlds/meadow", on_text=on_text)
    assert calls == [("show", "/daniel/agents/builder/worlds/meadow")]
    assert "world" in out[-1][1] and "meadow" in out[-1][1] and "24" in out[-1][1]


async def test_dir_is_not_a_command_in_agent_mode():
    sh, d, out, on_text = _shell()                           # not in shell → needs the wake word
    await sh.feed("dir", on_text=on_text)
    assert d.handled == [("daniel", "dir")]                              # forwarded to the agent, not run as a command


async def test_delete_acts_on_the_one_line_no_confirmation():
    def responder(action, path):
        if action == "tree":
            return {"ok": True, "path": path, "children": [{"label": "w1", "kind": "world"}]}
        return {"ok": True, "deleted": "user 'alice'"}

    sh, out, on_text, calls = _admin_shell(responder)
    await sh.feed("delete /alice", on_text=on_text)
    assert calls == [("tree", "/alice"), ("delete", "/alice")]   # resolved, then gone — one round trip each
    assert not any("confirm" in t.lower() for _, t in out)
    assert "Deleted" in out[-1][1] and "1 world" in out[-1][1]   # says what it took, after the fact


async def test_a_second_line_is_not_swallowed_as_an_answer():
    """The old y/n prompt made the NEXT line an answer, which is what broke the one-shot path. Nothing
    should be armed after a delete — the following command must run as itself."""
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: {"ok": True, "path": p} if a == "tree" else {"ok": True, "deleted": p})
    await sh.feed("delete /alice", on_text=on_text)
    await sh.feed("dir /", on_text=on_text)
    assert [a for a, _ in calls] == ["tree", "delete", "tree"]   # `dir` listed, not read as a y/n


async def test_delete_reports_the_path_the_server_resolved():
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: {"ok": True, "path": "/daniel/agents/builder/sessions/s1/worlds",
                      "children": [{"label": "meadow", "kind": "world"}]})
    await sh.feed("delete worlds", on_text=on_text)
    assert ("delete", "/daniel/agents/builder/sessions/s1/worlds") in calls   # the shortcut's real target
    assert "sessions/s1/worlds" in out[-1][1]                 # and you're told, so a wrong target is visible


async def test_a_delete_that_cannot_be_resolved_never_fires():
    sh, out, on_text, calls = _admin_shell(lambda a, p: {"ok": False, "error": "no such user 'ghost'"})
    await sh.feed("delete /ghost", on_text=on_text)
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


async def test_path_verbs_carry_the_TARGET_session_not_just_its_scope():
    """`rename session-1 <title>` retitled whatever session was ACTIVE.

    The path was resolved, `show` handed back the right sid in `fields`, and then the POST went out with
    `scope` only — and /session/rename, /session/visibility and /worlds/rename all default a missing
    `session` to the live one. So every path verb silently retargeted itself, and the more precise you
    were about which session you meant, the more wrong the result.
    """
    sh, out, on_text, calls = _session_shell(lambda mth, p, kw: {"ok": True})

    async def fake_admin(action, path):
        # what /admin/show returns for a non-active session and a world inside it
        if path.endswith("/worlds/alien"):
            return {"ok": True, "kind": "world",
                    "fields": [["world", "alien"], ["id", "wld_123"], ["session", "session-1"],
                               ["scope", "daniel/agents/outdoor"]]}
        return {"ok": True, "kind": "session",
                "fields": [["session", "session-1"], ["title", "Session 1"],
                           ["scope", "daniel/agents/outdoor"]]}
    sh._admin = fake_admin
    sh._cwd = "/daniel/agents/outdoor/sessions"

    await sh._dispatch("rename session-1 session-alpha", on_text)
    assert calls[-1][:2] == ("POST", "/session/rename")
    assert calls[-1][2]["session"] == "session-1"                  # …not None, which means "the active one"
    assert calls[-1][2]["title"] == "session-alpha"

    await sh._dispatch("private session-1", on_text)
    assert calls[-1][:2] == ("POST", "/session/visibility")
    assert calls[-1][2]["session"] == "session-1"

    # Worlds are stored per session, so a world path has to carry one too.
    await sh._dispatch("rename session-1/worlds/alien tundra", on_text)
    assert calls[-1][:2] == ("POST", "/worlds/rename")
    assert calls[-1][2]["session"] == "session-1" and calls[-1][2]["name"] == "wld_123"


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
    # `session delete` and `session clear` are gone — one `delete <path>` and one `clear` serve every
    # noun. Visibility moved to bare `public`/`private`, which also works spoken.
    await sh._dispatch("private", on_text)
    assert calls[-1][:2] == ("POST", "/session/visibility") and calls[-1][2]["public"] is False
    await sh._dispatch("public", on_text)
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
    await sh._dispatch("private", on_text, speaker="guest")
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
    await sh._dispatch("private", on_text, speaker="guest", permitted=False)
    assert not calls and "private" in out[-1][1].lower()            # visibility is shared-effect too


async def test_llm_switch_and_delete_refused_for_a_non_permitted_speaker():
    # The active LLM is SHARED, and delete is destructive — both refused for a bumped guest (§6d).
    sh, d, out, on_text = _shell()
    sh._permitted = False
    import re as _re
    m = _re.match(r"^llm\s+(?P<name>[\w.-]+)$", "llm gemini")
    handled = await sh._switch_llm(on_text, m)
    assert handled and d.active == "Claude" and "private" in out[-1][1].lower()   # LLM unchanged, refused
    import types as _t
    await sh._delete(on_text, _t.SimpleNamespace(group=lambda k: "/daniel/worlds/x",
                                                 groupdict=lambda: {"path": "/daniel/worlds/x"}))
    assert "private" in out[-1][1].lower()                    # delete refused before it reaches the server


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


async def test_a_resumed_agent_is_never_asserted_at_the_world_server(monkeypatch):
    """At boot the world server is the source of truth — it has already restored the live scope. A
    RESUMED agent is at best what we just read back from it, at worst `_last_agent`'s fallback after the
    world server didn't answer; asserting either can only no-op or overwrite the truth with a guess.
    An EXPLICIT --agent is a real instruction, so it still asserts."""
    from conjure import shell as shell_mod
    asserted = []

    async def fake_open(self, agent, *, activate_world=True):
        if activate_world:
            asserted.append(agent)

    monkeypatch.setattr(shell_mod.Shell, "_open_agent", fake_open)
    monkeypatch.setattr(shell_mod.Shell, "_last_agent", lambda self: _immediately("outdoor"))

    async with shell_mod.Shell.session(settings=None, agent=None, user="daniel"):
        pass
    assert asserted == []                                     # resumed → stays quiet
    async with shell_mod.Shell.session(settings=None, agent="scratch", user="daniel"):
        pass
    assert asserted == ["scratch"]                            # explicit → asserted


async def _immediately(value):
    return value


async def test_last_agent_waits_for_a_world_server_that_is_still_booting(monkeypatch):
    """The two servers race at startup — the world server runs migrations before it binds — and this
    answer picks which agent's MCP server gets spawned. Guessing past it boots the wrong agent, spawns
    and tears down an MCP subprocess, and fires a spurious 'now in the … agent' when the follower
    corrects us. So poll until it answers."""
    from conjure import shell as shell_mod
    monkeypatch.setattr(shell_mod, "_LAST_AGENT_POLL", 0.001)
    monkeypatch.setattr(shell_mod, "LAST_AGENT_WAIT", 5.0)
    tries = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, params=None):
            tries.append(url)
            if len(tries) < 4:
                raise OSError("connection refused")           # still running migrations, port not bound
            return type("R", (), {"json": staticmethod(lambda: {"ok": True, "agent": "outdoor"})})()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    sh = Shell(None, settings=types.SimpleNamespace(world_url="http://x"))
    sh._user = "daniel"
    assert await sh._last_agent() == "outdoor"                # waited it out instead of guessing 'builder'
    assert len(tries) == 4


async def test_last_agent_gives_up_rather_than_hanging_forever(monkeypatch):
    # The world server may never come. Bounded wait, then the safe default — which session() won't assert.
    from conjure import shell as shell_mod
    monkeypatch.setattr(shell_mod, "_LAST_AGENT_POLL", 0.001)
    monkeypatch.setattr(shell_mod, "LAST_AGENT_WAIT", 0.02)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url, params=None):
            raise OSError("connection refused")

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    sh = Shell(None, settings=types.SimpleNamespace(world_url="http://x"))
    sh._user = "daniel"
    assert await sh._last_agent() == "builder"


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


async def test_clear_wipes_the_director_transcript():
    sh, d, out, on_text = _shell()
    d.transcript = ["u1", "a1", "u2", "a2"]            # accumulated history
    sh.in_shell = True
    await sh.feed("clear", on_text=on_text)            # hand-built shell → in-memory clear path
    assert d.transcript == []
    assert any("cleared" in t.lower() for _, t in out)


# --------------------------------------------------------------------------- path resolution (pure)

def test_resolve_path_handles_absolute_home_relative_and_dotdot():
    from conjure.shell import default_cwd, display_path, resolve_path
    cwd = "/daniel/agents/builder"
    assert resolve_path(cwd, "", "daniel") == cwd                      # bare → where you are
    assert resolve_path(cwd, "sessions", "daniel") == f"{cwd}/sessions"
    assert resolve_path(cwd, "/alice/spaces", "daniel") == "/alice/spaces"
    assert resolve_path(cwd, "~", "daniel") == "/daniel"
    assert resolve_path(cwd, "~/spaces", "daniel") == "/daniel/spaces"
    assert resolve_path(cwd, "..", "daniel") == "/daniel/agents"
    assert resolve_path(cwd, "../outdoor/sessions", "daniel") == "/daniel/agents/outdoor/sessions"
    assert resolve_path(cwd, "./sessions/", "daniel") == f"{cwd}/sessions"
    # `..` can't climb past the root, so a path can never escape the namespace
    assert resolve_path("/", "../../etc", "daniel") == "/etc"
    assert resolve_path(cwd, "../../../../..", "daniel") == "/"
    # `~` is YOUR home — a guest resolves it to their own, never the host's
    assert resolve_path(cwd, "~/spaces", "guest") == "/guest/spaces"
    assert default_cwd("daniel", "builder") == "/daniel/agents/builder"
    assert display_path("/daniel/agents/builder", "daniel") == "~/agents/builder"
    assert display_path("/alice/spaces", "daniel") == "/alice/spaces"   # someone else's stays absolute


# --------------------------------------------------------------------------- nouns

async def test_world_lists_switches_and_creates():
    sh, out, on_text, calls = _session_shell(lambda mth, p, kw: {"ok": True})
    await sh._dispatch("world meadow", on_text)
    assert calls[-1][:2] == ("POST", "/worlds/switch") and calls[-1][2]["name"] == "meadow"
    await sh._dispatch("world new castle keep", on_text)
    assert calls[-1][:2] == ("POST", "/worlds/new") and calls[-1][2]["name"] == "castle keep"


async def test_world_changes_are_refused_for_a_non_permitted_speaker():
    sh, out, on_text, calls = _session_shell(lambda mth, p, kw: {"ok": True})
    await sh._dispatch("world meadow", on_text, speaker="guest", permitted=False)
    assert not calls and "private" in out[-1][1].lower()


async def test_tools_lists_what_the_agent_can_call():
    sh, d, out, on_text = _shell()
    await sh._dispatch("tools", on_text)
    assert "place_asset" in out[-1][1]


# --------------------------------------------------------------------------- the voice / CLI split

def test_every_row_declares_whether_it_is_voice_safe():
    sh, d, out, on_text = _shell()
    voice = {h.split()[0] for _, _, h, v in sh._table if v}
    cli = {h.split()[0] for _, _, h, v in sh._table if not v}
    # Voice gets the modal/navigational verbs — the ones that make sense with no screen — plus the
    # whole-name listings, which answer a question rather than dump a tree.
    assert {"agent", "llm", "session", "world", "where", "clear", "spaces", "users", "reset"} <= voice
    # What stays CLI-only is what takes or prints a PATH: unspeakable to say, unusable to hear.
    assert {"dir", "show", "cd", "delete", "rename"} <= cli
    assert not (voice & cli), "a command is either speakable or it isn't"


# --------------------------------------------------------------------------- voice-tailored output
#
# The shell's listings are columns with a `*` or `@` marking the current row. Spoken, the marker is at
# best mispronounced and at worst — since the voice speech stage strips asterisks before the engine sees
# them — silently gone, leaving a listener with a list and no idea which one they are in.

def _set_shell(reply):
    """A shell whose /admin/settings is served by `reply(kw)`."""
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    seen = []

    async def api(**kw):
        seen.append(kw)
        return reply(kw)

    sh._settings_api = api
    return sh, out, on_text, seen


async def test_set_reports_the_source_and_how_to_keep_it():
    # "why is it that?" and "how do I keep it?" both belong in the reply, not in someone's memory.
    sh, out, on_text, seen = _set_shell(lambda kw: {
        "ok": True, "key": "foveation", "value": "0.3", "env_value": "0.3", "tier": "client",
        "env": "CONJURE_FOVEATION", "source": "env", "was": "0.0", "was_source": "default"})
    await sh._dispatch("set foveation 0.3", on_text)
    line = out[-1][1]
    assert "was 0.0 · default" in line
    assert "CONJURE_FOVEATION=0.3" in line                    # the env form, to paste into .env
    assert "--save" in line                                   # …and the way to persist it
    assert "next headset load" in line                        # a client knob says when it takes effect


async def test_a_saved_setting_says_where_it_went_instead_of_how_to_save_it():
    sh, out, on_text, seen = _set_shell(lambda kw: {
        "ok": True, "key": "pose_tau", "value": "0.25", "env_value": "0.25", "tier": "client",
        "env": "CONJURE_POSE_TAU", "source": "env", "was": "0.0", "saved": "/home/d/settings.json"})
    await sh._dispatch("set pose_tau 0.25 --save", on_text)
    assert seen[-1]["save"] is True
    assert "settings.json" in out[-1][1] and "persist:" not in out[-1][1]


async def test_a_refused_setting_says_why():
    sh, out, on_text, seen = _set_shell(
        lambda kw: {"ok": False, "error": "port is read once at startup (CONJURE_PORT) — "
                                          "set it in .env and restart."})
    await sh._dispatch("set port 9000", on_text)
    assert "read once at startup" in out[-1][1]


async def test_set_is_a_terminal_command():
    # Deferred by design: a mis-heard number succeeds silently where a mis-heard key would fail loudly.
    sh, out, on_text, seen = _set_shell(lambda kw: {"ok": True})
    await sh._dispatch("set foveation 0.3", on_text, voice=True)
    assert "terminal command" in out[-1][1] and seen == []


def _glob_shell(matches, kind="world"):
    """A shell whose /admin/match answers with `matches` (labels), and records every delete."""
    deleted = []
    def responder(action, path):
        if action == "match":
            return {"ok": True, "glob": True, "columns": ["name", "entities", "space"],
                    "matches": [{"path": f"/w/{n}", "display": f"/daniel/agents/builder/worlds/{n}",
                                 "kind": kind, "row": {"label": n, "kind": kind, "cells": ["0 entities", "?"]}}
                                for n in matches]}
        if action == "delete":
            deleted.append(path)
            return {"ok": True}
        return {"ok": True, "path": path, "display": path, "children": []}
    sh, out, on_text, calls = _admin_shell(responder)
    return sh, out, on_text, deleted


async def test_a_pattern_delete_previews_and_takes_a_bang_to_act():
    # §6.6 says `delete` needs no confirmation because it takes an explicit path. A pattern voids that
    # argument — `delete "s*"` looks precise and may match nine things — so the guard lands there, and
    # only there: an explicit path keeps its one-shot form.
    sh, out, on_text, deleted = _glob_shell(["Kitchen sketch", "Porch sketch", "Sketch 4"])
    await sh._dispatch('delete "*sketch*"', on_text)
    assert deleted == []                                       # nothing went
    assert "nothing deleted" in out[-1][1] and "Kitchen sketch" in out[-1][1]

    await sh._dispatch('delete "*sketch*" !', on_text)
    assert len(deleted) == 3 and "Deleted 3" in out[-1][1]


async def test_an_explicit_path_still_deletes_in_one_shot():
    # The one-shot form is why the y/n prompt was dropped; a pattern guard must not take it back.
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: {"ok": True, "path": p, "display": p, "children": [], "self":
                      {"label": "Meadow", "kind": "world", "cells": ["0 entities"]}})
    await sh._dispatch("delete Meadow", on_text)
    assert ("delete", "/daniel/agents/builder/Meadow") in calls
    assert "Deleted" in out[-1][1]


async def test_cd_refuses_a_pattern_that_matches_more_than_one():
    sh, out, on_text, deleted = _glob_shell(["Kitchen sketch", "Porch sketch"])
    await sh._dispatch('cd "*sketch*"', on_text)
    assert "2 match" in out[-1][1] and sh.cwd == "/daniel/agents/builder"      # unmoved

    sh, out, on_text, deleted = _glob_shell(["Kitchen sketch"])
    await sh._dispatch('cd "*sketch*"', on_text)
    assert sh.cwd == "/w/Kitchen sketch"                       # exactly one → you can stand in it


async def test_rename_refuses_a_pattern():
    sh, out, on_text, deleted = _glob_shell(["a", "b"])
    await sh._dispatch('rename "*sketch*" Something', on_text)
    assert "one path" in out[-1][1]


async def test_cd_remembers_ids_and_shows_names():
    # The cwd is canonical (ids) so a rename can't strand it; the prompt renders the display form the
    # server sent alongside. Two fields, one round trip.
    sh, out, on_text, calls = _admin_shell(
        lambda a, p: {"ok": True, "path": "/daniel/agents/builder/sessions/session-3",
                      "display": "/daniel/agents/builder/sessions/Kitchen sketch", "children": []})
    await sh._dispatch('cd "Kitchen sketch"', on_text)
    assert sh.cwd == "/daniel/agents/builder/sessions/session-3"          # remembered by id
    assert sh.cwd_display == "/daniel/agents/builder/sessions/Kitchen sketch"
    assert "Kitchen sketch" in out[-1][1] and "session-3" not in out[-1][1]   # shown by name


async def test_a_cwd_whose_session_vanished_walks_up_instead_of_erroring():
    # Someone else renamed or deleted the session you were standing in. The old behaviour was an error
    # per command until you worked out you had to `cd` somewhere; now it moves you and says so.
    gone = "/daniel/agents/builder/sessions/session-3"
    def responder(action, path):
        if path.startswith(gone):
            return {"ok": False, "error": f"no session 'session-3' for daniel/agents/builder"}
        return {"ok": True, "path": path, "display": path, "children": [{"label": "Porch",
                                                                         "kind": "session"}]}
    sh, out, on_text, calls = _admin_shell(responder)
    await sh._dispatch("dir", on_text, cwd=gone)          # cwd is per-connection, passed in per dispatch
    assert "is gone" in out[-2][1] and "session-3" in out[-2][1]
    assert sh.cwd == "/daniel/agents/builder/sessions"                    # the nearest survivor
    assert "Porch" in out[-1][1]                                          # …and it listed there


async def test_a_bad_argument_is_still_reported_as_a_bad_argument():
    # The walk-up must not swallow a genuine typo: if the CWD is fine, the error stands.
    def responder(action, path):
        if path.endswith("/nope"):
            return {"ok": False, "error": "no world 'nope' in session-1"}
        return {"ok": True, "path": path, "display": path, "children": []}
    sh, out, on_text, calls = _admin_shell(responder)
    await sh._dispatch("dir nope", on_text)
    assert "no world 'nope'" in out[-1][1] and "moved you" not in out[-1][1]


def test_columns_align_every_row_under_a_header():
    from conjure.shell import columns
    rows = [{"label": "Meadow", "kind": "world", "cells": ["12 entities", "Living Room"], "active": True},
            {"label": "Kitchen sketch", "kind": "world", "cells": ["1 entity", "outdoor"]}]
    out = columns(rows, ["name", "entities", "space"])
    assert out[0].split() == ["name", "entities", "space"]
    # every column starts at the same offset in every line — the whole point of the change
    starts = [line.index("Living Room") for line in out if "Living Room" in line]
    assert [line.index("outdoor") for line in out if "outdoor" in line] == starts
    assert out[1].lstrip().startswith("─")              # a rule, so the header doesn't read as data
    assert out[2].startswith(" *")                      # the live row is marked, and stays aligned
    assert out[3].startswith("  ")


def test_a_single_column_listing_gets_no_header():
    from conjure.shell import columns
    rows = [{"label": "agents", "kind": "category"}, {"label": "spaces", "kind": "category"}]
    assert columns(rows, []) == ["  agents/", "  spaces/"]     # a lone column needs no explaining


def test_the_id_column_shows_only_for_assets_or_in_long_mode():
    from conjure.shell import columns
    sess = [{"label": "Kitchen sketch", "kind": "session", "cells": ["2 worlds", "public"],
             "ref": "session-1"}]
    assert not any("session-1" in l for l in columns(sess, ["name", "worlds", "vis"]))
    assert any("session-1" in l for l in columns(sess, ["name", "worlds", "vis"], long=True))
    # assets are the exception: the id IS the address there, so it shows unasked
    asset = [{"label": "Kitchen counter", "kind": "asset", "cells": ["image", "public"], "ref": "a1.png"}]
    assert any("a1.png" in l for l in columns(asset, ["name", "type", "vis"]))


def test_a_paragraph_length_label_is_clipped_rather_than_wrapped():
    from conjure.shell import columns, NAME_WIDTH
    long_label = "a sensual woman with long dark hair, wearing elegant black lingerie, standing gracefully"
    rows = [{"label": long_label, "kind": "asset", "cells": ["image", "public"], "ref": "a1.png"},
            {"label": "Cat", "kind": "asset", "cells": ["image", "public"], "ref": "b2.png"}]
    out = columns(rows, ["name", "type", "vis"])
    body = out[2:]                                      # skip the header and its rule
    assert "…" in body[0] and len(body[0]) < len(long_label)
    assert all(l.index("image") == body[0].index("image") for l in body)     # still aligned
    assert NAME_WIDTH < len(long_label)                    # the sample actually exercises the clip


def test_join_spoken_reads_like_a_person():
    from conjure.shell import _join_spoken
    assert _join_spoken([]) == ""
    assert _join_spoken(["a"]) == "a"
    assert _join_spoken(["a", "b"]) == "a and b"
    assert _join_spoken(["a", "b", "c"]) == "a, b and c"


def test_spoken_list_turns_the_marker_into_words():
    from conjure.shell import spoken_list
    assert spoken_list("agent", ["builder", "outdoor", "scratch"], current="builder") == (
        "3 agents: builder, outdoor and scratch. You're in builder.")
    assert spoken_list("LLM", ["Claude", "Grok"], current="Grok", here="You're talking to") == (
        "2 LLMs: Claude and Grok. You're talking to Grok.")


def test_spoken_list_handles_the_awkward_edges():
    from conjure.shell import spoken_list
    assert spoken_list("world", []) == "No worlds here."               # not "(none)"
    assert spoken_list("session", ["Home"], current="Home") == (
        "One session: Home, and you're in it.")                        # not "You're in Home."
    assert spoken_list("world", ["a", "b"]) == "2 worlds: a and b."    # nothing current → no claim


async def test_agents_and_llms_are_spoken_as_sentences_not_columns():
    sh, d, out, on_text = _shell()
    await sh._dispatch("llm", on_text, voice=True)
    spoken = out[-1][1]
    assert spoken == "2 LLMs: Claude and Gemini. You're talking to Claude."
    assert "*" not in spoken and "\n" not in spoken

    out.clear()
    await sh._dispatch("llm", on_text, voice=False)                    # the terminal keeps its column
    typed = out[-1][1]
    assert typed.startswith("LLMs:") and "* Claude" in typed


async def test_where_is_a_sentence_for_voice_and_a_status_line_for_a_terminal():
    """`where` is the single most useful thing to ask by voice — there's no status bar in a headset."""
    sh, d, out, on_text = _shell()
    await sh._dispatch("where", on_text, voice=True)
    spoken = out[-1][1]
    assert "·" not in spoken                                            # reads as nothing, or "middle dot"
    assert "LLMs," not in spoken                                        # roster/tool counts are a terminal thing
    assert spoken.startswith("You're daniel, in the builder agent with Claude.")

    out.clear()
    await sh._dispatch("where", on_text, voice=False)
    assert "·" in out[-1][1] and "tools)" in out[-1][1]


# --------------------------------------------------------------------------- wake-word aliases

def test_a_mis_heard_wake_word_still_opens_the_shell():
    """The failure this fixes is silent: STT hears "coinjure", the line doesn't match, and a COMMAND is
    forwarded to the agent as content — which then tries to build you a coinjure."""
    sh, d, out, on_text = _shell()
    for heard in ("conjure where", "coinjure where", "Conjur where", "KONJURE where", "conjour where"):
        assert sh.as_command(heard, False) == "where", heard
    for heard in ("conjure", "coinjure"):
        assert sh.as_command(heard, False) == "open shell"


def test_an_alias_does_not_swallow_ordinary_speech():
    """Every alias must be a non-word. A real word here would eat content that merely starts with it —
    a worse failure than the mis-hearing, because it would be silent in the other direction."""
    sh, d, out, on_text = _shell()
    for content in ("conjuring a spell", "concert hall", "conjunction", "a conjure trick"):
        assert sh.as_command(content, False) is None, content


def test_the_wake_pattern_is_built_from_the_configured_list():
    """So a new mis-hearing is a config line, not a code change."""
    from conjure.shell import wake_pattern
    rx = wake_pattern(["abra", "cadabra"])
    assert rx.match("abra open shell").group("rest") == "open shell"
    assert rx.match("cadabra, where").group("rest") == "where"        # comma optional — STT rarely punctuates
    assert rx.match("conjure where") is None                          # not in this list


# --------------------------------------------------------------------------- spaces / users / reset by voice
#
# These three were CLI-only. `spaces` and `users` answer a question rather than dump a tree, so they read
# fine aloud — but only once the noun and the "you are here" clause fit what is being listed. `reset` is
# destructive, so by voice it is armed and then confirmed.

async def test_spaces_are_places_you_stand_in_not_things_you_are_in():
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(
        p, {"label": "living room", "kind": "space", "active": True},
           {"label": "studio", "kind": "space"}))
    await sh._dispatch("spaces", on_text, voice=True)
    assert out[-1][1] == "2 spaces: living room and studio. You're standing in living room."
    assert calls == [("tree", "/daniel/spaces")]


async def test_one_space_and_you_are_in_it_reads_as_a_sentence():
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(
        p, {"label": "home", "kind": "space", "active": True}))
    await sh._dispatch("spaces", on_text, voice=True)
    assert out[-1][1] == "One space: home, and you're standing in it."


async def test_a_space_you_are_not_in_makes_no_claim():
    """Outdoors or in a void world there is no active row — saying nothing beats guessing."""
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(p, {"label": "home", "kind": "space"}))
    await sh._dispatch("spaces", on_text, voice=True)
    assert out[-1][1] == "1 space: home."


async def test_users_are_named_and_you_are_told_which_one_you_are():
    """The path is "/", whose last segment is empty — the generic noun would have said "2 entrys"."""
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(
        p, {"label": "daniel", "kind": "user", "active": True}, {"label": "guest", "kind": "user"}))
    await sh._dispatch("users", on_text, voice=True)
    assert out[-1][1] == "2 users: daniel and guest. You're daniel."


async def test_the_spoken_user_is_the_SPEAKER_not_the_marked_row():
    """The active marker tracks whoever the world server last acted for. Read back as "You're …" to a
    guest that is simply false, and a guest is exactly who needs it right."""
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(
        p, {"label": "daniel", "kind": "user", "active": True}, {"label": "guest", "kind": "user"}))
    await sh._dispatch("users", on_text, voice=True, speaker="guest")
    assert out[-1][1].endswith("You're guest.")


async def test_the_terminal_listing_is_untouched_by_the_voice_phrasing():
    sh, out, on_text, calls = _admin_shell(lambda a, p: _listing(
        p, {"label": "living room", "kind": "space", "active": True}))
    await sh._dispatch("spaces", on_text)                    # no voice= → terminal
    assert "living room" in out[-1][1] and "You're standing in" not in out[-1][1]


# -- reset: armed, then confirmed -----------------------------------------------------------------

def _reset_shell():
    sh, out, on_text, calls = _session_shell(
        lambda mth, p, kw: {"ok": True, "sessions": 3, "world": "default"})
    sh.in_shell = True
    return sh, out, on_text, calls


async def test_a_spoken_reset_asks_before_it_wipes_anything():
    sh, out, on_text, calls = _reset_shell()
    await sh._dispatch("reset agent builder", on_text, voice=True)
    assert calls == []                                       # nothing was destroyed yet
    said = out[-1][1]
    assert "can't be undone" in said and "yes" in said.lower() and "builder" in said


async def test_yes_fires_the_armed_reset():
    sh, out, on_text, calls = _reset_shell()
    await sh._dispatch("reset agent builder", on_text, voice=True)
    await sh._dispatch("yes", on_text, voice=True)
    assert [c[1] for c in calls] == ["/agent/reset"]
    assert calls[0][2]["agent"] == "builder" and calls[0][2]["assets"] is False


async def test_a_stray_yes_destroys_nothing():
    sh, out, on_text, calls = _reset_shell()
    await sh._dispatch("yes", on_text, voice=True)
    assert calls == [] and "Nothing to confirm" in out[-1][1]


async def test_confirming_twice_does_not_reset_twice():
    """The armed reset is consumed by the first `yes`, so a repeated or echoed one is inert."""
    sh, out, on_text, calls = _reset_shell()
    await sh._dispatch("reset agent builder", on_text, voice=True)
    await sh._dispatch("yes", on_text, voice=True)
    await sh._dispatch("yes", on_text, voice=True)
    assert len(calls) == 1


async def test_an_expired_confirmation_is_refused_and_says_how_to_retry():
    import conjure.shell as sm
    sh, out, on_text, calls = _reset_shell()
    await sh._dispatch("reset agent builder", on_text, voice=True)
    who, name, assets, at = sh._pending_reset
    sh._pending_reset = (who, name, assets, at - sm._CONFIRM_TTL - 1)
    await sh._dispatch("yes", on_text, voice=True)
    assert calls == [] and "expired" in out[-1][1] and "reset agent builder" in out[-1][1]


async def test_someone_else_cannot_confirm_your_reset():
    sh, out, on_text, calls = _reset_shell()
    await sh._dispatch("reset agent builder", on_text, voice=True)
    await sh._dispatch("yes", on_text, voice=True, speaker="guest")
    assert calls == [] and "daniel's to confirm" in out[-1][1]


async def test_a_misheard_agent_name_is_caught_before_arming():
    """STT mangling a name is the likeliest way this goes wrong, and the server would only say 'no such
    agent' AFTER a confirmation. Check first, and name the real ones."""
    sh, out, on_text, calls = _reset_shell()
    await sh._dispatch("reset agent bilder", on_text, voice=True)
    assert sh._pending_reset is None and calls == []
    assert "no agent called bilder" in out[-1][1] and "builder" in out[-1][1]


async def test_typing_the_reset_still_runs_it_immediately():
    """Typing is already deliberate; a confirmation prompt in a terminal is friction for its own sake."""
    sh, out, on_text, calls = _reset_shell()
    await sh.feed("reset agent builder", on_text=on_text)
    assert [c[1] for c in calls] == ["/agent/reset"] and sh._pending_reset is None
