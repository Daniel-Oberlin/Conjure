"""Director orchestration — the shared brain for voice and CLI.

The orchestration test drives a real Director against fake LLMs + a fake MCP session — no network,
no SDKs. LLM switching is deterministic and lives in the shell now (see test_shell.py); the Director
no longer parses handovers out of an utterance."""

from conjure.agents import load_agent
from conjure.director import Director, _fill_injection
from conjure.llm import Turn


# --------------------------------------------------------------------------- orchestration

class FakeLLM:
    """Records what it was asked, optionally calls one tool, emits ack + final."""

    def __init__(self, name, tool=None):
        self.name = name
        self.tool = tool          # (tool_name, args) to call, or None
        self.seen = []            # systems/histories it was handed

    async def run_turn(self, *, system, history, user_text, tools, execute_tool, emit):
        self.seen.append({"system": system, "history": list(history), "user_text": user_text})
        await emit(f"{self.name} on it", final=False)
        if self.tool:
            await execute_tool(*self.tool)
        reply = f"{self.name}: done «{user_text}»"
        await emit(reply, final=True)
        return reply


class FakeSession:
    def __init__(self, resource_text="Room: 2 surfaces (test)\n  - wall #1 (real_wall_1)"):
        self.calls = []
        self.resources_read = []
        self._resource_text = resource_text

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return type("R", (), {"content": [type("C", (), {"text": "ok"})()]})()

    async def read_resource(self, uri):
        self.resources_read.append(uri)
        return type("R", (), {"contents": [type("C", (), {"text": self._resource_text})()]})()


def _agent(context):
    """A minimal agent stand-in for the Director (just what handle/_fetch_context read)."""
    return type("A", (), {"name": "builder", "context": list(context)})()


def _director(active="Claude", tools=None, **llms):
    roster = llms or {"Claude": FakeLLM("Claude"), "Gemini": FakeLLM("Gemini")}
    return Director(settings=None, session=FakeSession(), roster=roster, active=active, tools=tools or [])


async def test_handle_records_user_assistant_transcript():
    d = _director()
    out = await d.handle("put a tree in front of me")
    assert out == "Claude: done «put a tree in front of me»"
    # the transcript is plain user/assistant — it records no LLM identity
    assert [(t.speaker, t.text) for t in d.transcript] == [
        ("user", "put a tree in front of me"),
        ("assistant", "Claude: done «put a tree in front of me»"),
    ]


async def test_handle_always_runs_the_active_llm():
    # No inline routing: even an utterance that names another LLM goes to the active one verbatim.
    d = _director(active="Claude")
    await d.handle("Gemini, make a picture of a cat")
    assert d.active == "Claude"                                   # unchanged — switching is the shell's job
    assert d.roster["Claude"].seen[-1]["user_text"] == "Gemini, make a picture of a cat"  # full text, unrouted
    assert d.roster["Gemini"].seen == []                          # the named LLM was NOT invoked
    assert d.transcript[-1].speaker == "assistant"


async def test_shell_switched_active_is_used_by_the_next_turn():
    # The shell switches by setting director.active; the next handle() must run on it.
    d = _director(active="Claude")
    d.active = "Gemini"                                           # what shell._switch does
    await d.handle("add a fountain")
    assert d.roster["Gemini"].seen[-1]["user_text"] == "add a fountain"
    assert d.roster["Claude"].seen == []
    assert d.transcript[-1].speaker == "assistant"


async def test_director_logs_utterance_tool_calls_and_reply():
    d = _director(Claude=FakeLLM("Claude", tool=("place_asset", {"query": "oak tree"})))
    events: list[tuple[str, str]] = []

    async def cap(tag, msg):
        events.append((tag, msg))

    d.agent = type("Agent", (), {"name": "builder", "context": []})()  # so log tags read builder.claude
    d._log = cap                                              # capture instead of POSTing
    await d.handle("add an oak tree")
    assert ("you", "add an oak tree") in events                          # the user's request
    assert ("builder.claude", "Claude on it") in events                 # intermediate speech, attributed
    assert any(t == "builder.claude/tool" and m.startswith("place_asset(") and "oak tree" in m
               for t, m in events)                                       # tool call, attributed to agent.llm
    assert any(t == "builder.claude/tool" and m.strip().startswith("->") for t, m in events)  # tool result
    assert any(t == "builder.claude" and "done" in m for t, m in events)                       # final reply


async def test_emit_and_tools_are_wired_through():
    seen, gemini = [], FakeLLM("Gemini")
    d = _director(active="Gemini", Gemini=gemini)

    async def on_text(text, *, final, speaker):
        seen.append((speaker, final, text))

    async def on_tool(name, args):
        seen.append(("tool", name, args))

    gemini.tool = ("place_asset", {"query": "tree"})
    await d.handle("a tree", on_text=on_text, on_tool=on_tool)
    assert ("Gemini", False, "Gemini on it") in seen          # acknowledgement, non-final
    assert ("tool", "place_asset", {"query": "tree"}) in seen  # on_tool fired
    assert d._session.calls == [("place_asset", {"query": "tree"})]  # reached the MCP session
    assert seen[-1][1] is True                                 # last emit is the final reply


async def test_a_switched_in_llm_sees_prior_turns_plainly():
    d = _director(active="Claude")
    await d.handle("suggest a centerpiece")           # Claude answers
    d.active = "Gemini"                                # shell switch mid-conversation
    await d.handle("what do you think?")              # Gemini now answers, inheriting the history
    history = d.roster["Gemini"].seen[-1]["history"]
    # Gemini's view includes the earlier turn, but with no record of which LLM produced it —
    # every reply is a plain "assistant" turn (the switch is invisible in the history).
    speakers = [t.speaker for t in history]
    assert speakers == ["user", "assistant"]
    assert all(t.speaker in ("user", "assistant") for t in history)


def test_fill_injection_bare_section_and_drop():
    # bare {name} → value
    assert _fill_injection("hi {user}!", "user", "alice") == "hi alice!"
    # {#name}…{name}…{/name} section kept (with {name} filled) when the value is non-blank
    assert _fill_injection("a{#ctx}[{ctx}]{/ctx}b", "ctx", "X") == "a[X]b"
    # …dropped ENTIRELY when the value is blank, so framing text vanishes with it (no dangling header)
    assert _fill_injection("a{#ctx}[{ctx}]{/ctx}b", "ctx", "") == "ab"
    assert _fill_injection("a{#ctx}[{ctx}]{/ctx}b", "ctx", "   ") == "ab"
    # unrelated braces (JSON/SQL examples in a prompt) are never touched
    assert _fill_injection('x {"k": 1} {user}', "user", "bob") == 'x {"k": 1} bob'


async def test_context_section_rendered_when_present():
    llm, session = FakeLLM("Claude"), FakeSession()
    d = Director(settings=None, session=session, roster={"Claude": llm}, active="Claude", tools=[],
                 prompt="Build.\n{#context}scene:\n{context}{/context}", agent=_agent(["room://current"]))
    await d.handle("add a tree")
    system = llm.seen[0]["system"]
    assert "scene:" in system and "Room: 2 surfaces (test)" in system   # agent's framing + the data
    assert "{context}" not in system and "{#context}" not in system     # placeholders consumed
    assert session.resources_read == ["room://current"]


async def test_context_section_dropped_when_empty():
    # No context data → the whole {#context} block (framing included) is removed: no dangling header.
    llm, session = FakeLLM("Claude"), FakeSession()
    d = Director(settings=None, session=session, roster={"Claude": llm}, active="Claude", tools=[],
                 prompt="Build.\n{#context}scene:\n{context}{/context}", agent=_agent([]))
    await d.handle("add a tree")
    system = llm.seen[0]["system"]
    assert "scene:" not in system                                       # framing gone with the value
    assert "{context}" not in system and "{#context}" not in system


async def test_context_not_fetched_when_prompt_omits_placeholder():
    # An agent whose prompt references neither {context} nor {#context} pays nothing — the fetch is
    # skipped entirely, even though it declares context resources. (Many agents ignore room surfaces.)
    llm, session = FakeLLM("Claude"), FakeSession()
    d = Director(settings=None, session=session, roster={"Claude": llm}, active="Claude", tools=[],
                 prompt="Just build. No scene needed.", agent=_agent(["room://current"]))
    await d.handle("add a tree")
    assert session.resources_read == []                        # never fetched


async def test_context_fetch_failure_is_not_fatal():
    class BoomSession(FakeSession):
        async def read_resource(self, uri):
            raise RuntimeError("no such resource")

    llm = FakeLLM("Claude")
    d = Director(settings=None, session=BoomSession(), roster={"Claude": llm}, active="Claude",
                 tools=[], prompt="Build.\n{#context}scene:\n{context}{/context}",
                 agent=_agent(["room://current"]))
    out = await d.handle("add a tree")                       # must not raise
    assert out == "Claude: done «add a tree»"
    assert "scene:" not in llm.seen[0]["system"]             # failed fetch → value "" → section dropped
    assert "{context}" not in llm.seen[0]["system"]


async def test_system_prompt_is_llm_agnostic():
    # The prompt names no LLM and mentions no roster/attribution — it is identical whichever LLM is
    # active, so switching LLMs is invisible to the model.
    d = _director(active="Claude")
    system = await d._system()
    assert system.startswith("You are the director")
    assert "Claude" not in system and "Gemini" not in system   # no LLM identity
    assert "[Name]" not in system                              # no attribution machinery


async def test_system_injects_only_referenced_placeholders():
    # _system() carries NO agent-specific text of its own; it fills the injection placeholders the
    # agent's prompt references ({user} here) and leaves the rest of the prompt untouched.
    d = _director(active="Claude")
    d._prompt, d.user = "You act for '{user}'. Build stuff.", "alice"
    assert await d._system() == "You act for 'alice'. Build stuff."


def test_builder_prompt_owns_its_identity_and_has_no_llm_placeholder():
    # The old {name} (LLM) placeholder is gone; the builder prompt now owns the {user} placeholder and
    # its ownership framing (moved out of the director runtime).
    prompt = load_agent("builder").prompt
    assert "{name}" not in prompt
    assert "{user}" in prompt
    assert "belong to whoever created them" in prompt   # the ownership text now lives in the prompt


def test_stdio_params_maps_python_and_substitutes_world_url():
    # The registry stays interpreter-/host-agnostic; _stdio_params resolves it for launch.
    import sys

    from conjure.agents import ServerSpec
    from conjure.director import _stdio_params

    spec = ServerSpec(name="world", command="python", args=["-m", "conjure.mcp_server"],
                      env={"CONJURE_URL": "${world_url}"})
    p = _stdio_params(spec, type("S", (), {"world_url": "http://host:9999"})())
    assert p.command == sys.executable
    assert p.args == ["-m", "conjure.mcp_server"]
    assert p.env["CONJURE_URL"] == "http://host:9999"
