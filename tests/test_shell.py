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
    assert sh.prompt() == "conjure:shell> "


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
