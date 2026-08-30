"""Director orchestration — the shared brain for voice and CLI.

The orchestration test drives a real Director against fake LLMs + a fake MCP session — no network,
no SDKs. LLM switching is deterministic and lives in the shell now (see test_shell.py); the Director
no longer parses handovers out of an utterance."""

import pytest

from conjure.agents import AgentDef, load_agent
from conjure.director import Director, _fill_injection, _pick_active
from conjure.llm import ToolSpec, Turn, _messages


def test_pick_active_uses_the_agents_llms_list_as_priority():
    roster = {"Claude": object(), "Gemini": object(), "Chat": object()}   # all available
    # explicit priority: first entry in the agent's list that's available wins (Gemini before Claude)
    assert _pick_active(AgentDef(name="a", prompt="p", llms=["Gemini", "Claude"]), roster, "Claude") == "Gemini"
    # first choice unavailable → fall through the list to the next available one
    assert _pick_active(AgentDef(name="a", prompt="p", llms=["Nope", "Chat"]), roster, "Claude") == "Chat"
    # wildcard (any) → agent default_llm, else settings default (default_active), else first
    assert _pick_active(AgentDef(name="a", prompt="p", llms=["*"], default_llm="Chat"), roster, "Claude") == "Chat"
    assert _pick_active(AgentDef(name="a", prompt="p", llms=["*"]), roster, "Gemini") == "Gemini"


# --------------------------------------------------------------------------- director-hosted state tools (§5)

async def test_state_tools_offered_and_dispatched_in_process(tmp_path):
    from conjure.world import StateStore
    d = Director(settings=None, session=FakeSession(), roster={"Claude": object()}, active="Claude",
                 tools=[], allowed_tools=set(), state_defs={"map": {"inject": "{map}"}})
    names = {t.name for t in d._tools}
    assert {"state_get", "state_set", "state_list"} <= names          # offered when the agent declares state
    d.bind_state(StateStore(tmp_path))
    assert await d._execute_tool("state_set", {"doc": "map", "path": "start", "value": "home"}, None, "t") == "ok"
    assert await d._execute_tool("state_get", {"doc": "map", "path": "start"}, None, "t") == '"home"'
    assert d._session.calls == []                                     # dispatched locally, NOT over MCP


async def test_state_injection_reads_the_bound_store(tmp_path):
    from conjure.world import StateStore
    d = Director(settings=None, session=FakeSession(), roster={"Claude": object()}, active="Claude",
                 tools=[], prompt="Map: {map}", state_defs={"map": {"inject": "{map}"}})
    d.bind_state(StateStore(tmp_path))
    d._state.set("map", "start", "home")
    sys = await d._system()
    assert "home" in sys and "Map:" in sys                            # {map} filled from the store


async def test_no_state_tools_without_a_declaration():
    d = Director(settings=None, session=FakeSession(), roster={"Claude": object()}, active="Claude", tools=[])
    assert not any(t.name.startswith("state_") for t in d._tools)     # opt-in only


async def test_state_write_is_rejected_when_it_violates_the_schema(tmp_path):
    from conjure.world import StateStore
    schema = {"type": "object", "properties": {"hp": {"type": "integer"}}, "required": ["hp"],
              "additionalProperties": False}
    d = Director(settings=None, session=FakeSession(), roster={"Claude": object()}, active="Claude",
                 tools=[], allowed_tools=set(), state_defs={"player": {"schema_data": schema}})
    d.bind_state(StateStore(tmp_path))
    ok = await d._execute_tool("state_set", {"doc": "player", "path": "hp", "value": 10}, None, "t")
    assert ok == "ok" and d._state.get("player", "hp") == 10          # valid write goes through
    bad = await d._execute_tool("state_set", {"doc": "player", "path": "hp", "value": "lots"}, None, "t")
    assert bad.startswith("error:") and "schema" in bad              # invalid → rejected
    assert d._state.get("player", "hp") == 10                        # and NOT written (unchanged)


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
    # the current turn reaches the LLM speaker-labeled ("daniel: …"); the reply echoes what it saw
    assert out == "Claude: done «daniel: put a tree in front of me»"
    # the transcript is plain user/assistant — it records no LLM identity, and stores the RAW user text
    assert [(t.speaker, t.text) for t in d.transcript] == [
        ("user", "put a tree in front of me"),
        ("assistant", "Claude: done «daniel: put a tree in front of me»"),
    ]


async def test_handle_tags_user_turn_with_speaker():
    # Per-turn attribution: the user turn records WHO spoke; the assistant turn stays unattributed
    # (no LLM identity ever lands in the transcript).
    d = _director()
    await d.handle("put a tree here", speaker="alice")
    user, assistant = d.transcript
    assert (user.speaker, user.by) == ("user", "alice")
    assert (assistant.speaker, assistant.by) == ("assistant", "")


async def test_handle_defaults_speaker_to_director_user():
    # A lone client that owns the whole conversation needn't pass a speaker — it falls back to
    # self.user, so existing single-user callers keep attributing to their --user.
    d = _director()
    d.user = "daniel"
    await d.handle("add a bench")
    assert d.transcript[0].by == "daniel"


async def test_user_injection_resolves_to_the_current_speaker():
    # {user} is filled per turn from whoever is speaking — two speakers in one conversation each see
    # their own name in the system prompt.
    llm = FakeLLM("Claude")
    d = Director(settings=None, session=FakeSession(), roster={"Claude": llm}, active="Claude",
                 tools=[], prompt="You act for '{user}'.", user="daniel")
    await d.handle("hi", speaker="alice")
    await d.handle("hey", speaker="bob")
    assert llm.seen[0]["system"] == "You act for 'alice'."
    assert llm.seen[1]["system"] == "You act for 'bob'."


async def test_second_turn_is_rejected_while_one_is_in_flight():
    # Single floor (D4): a turn submitted mid-turn is rejected with Busy, never interleaved into the
    # one shared transcript. The floor clears when the in-flight turn finishes.
    import asyncio

    from conjure.director import Busy

    gate = asyncio.Event()

    class BlockingLLM:
        name = "Claude"

        async def run_turn(self, *, system, history, user_text, tools, execute_tool, emit):
            await gate.wait()
            await emit("done", final=True)
            return "done"

    d = _director(active="Claude", Claude=BlockingLLM())
    first = asyncio.create_task(d.handle("one", speaker="alice"))
    await asyncio.sleep(0.02)                              # let the first turn take the floor
    with pytest.raises(Busy):
        await d.handle("two", speaker="bob")               # rejected — floor is taken
    gate.set()
    assert await first == "done"
    assert [t.by for t in d.transcript if t.speaker == "user"] == ["alice"]  # bob's turn never recorded
    await d.handle("three", speaker="carol")               # floor cleared → a new turn runs
    assert d.transcript[-2].by == "carol"


async def test_handle_always_runs_the_active_llm():
    # No inline routing: even an utterance that names another LLM goes to the active one verbatim.
    d = _director(active="Claude")
    await d.handle("Gemini, make a picture of a cat")
    assert d.active == "Claude"                                   # unchanged — switching is the shell's job
    # full text, unrouted — and speaker-labeled (the current turn carries its label like history does)
    assert d.roster["Claude"].seen[-1]["user_text"] == "daniel: Gemini, make a picture of a cat"
    assert d.roster["Gemini"].seen == []                          # the named LLM was NOT invoked
    assert d.transcript[-1].speaker == "assistant"


async def test_shell_switched_active_is_used_by_the_next_turn():
    # The shell switches by setting director.active; the next handle() must run on it.
    d = _director(active="Claude")
    d.active = "Gemini"                                           # what shell._switch does
    await d.handle("add a fountain")
    assert d.roster["Gemini"].seen[-1]["user_text"] == "daniel: add a fountain"
    assert d.roster["Claude"].seen == []
    assert d.transcript[-1].speaker == "assistant"


async def test_current_turn_is_speaker_labeled_like_history():
    # The model must never see an unlabeled human message: the current turn is prefixed with its
    # speaker (like _messages labels history), and the next turn's history shows it labeled exactly once.
    d = _director(active="Claude")
    await d.handle("what's here?", speaker="alice")
    assert d.roster["Claude"].seen[-1]["user_text"] == "alice: what's here?"     # current turn labeled
    assert d.transcript[-2].speaker == "user" and d.transcript[-2].by == "alice"  # stored RAW + attributed
    assert d.transcript[-2].text == "what's here?"                                # no label baked into storage
    await d.handle("and now?", speaker="bob")
    hist_texts = [t for _, t in _messages(list(d.transcript))]
    assert "alice: what's here?" in hist_texts                                    # labeled once in history
    assert "alice: alice: what's here?" not in hist_texts                         # never double-labeled


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


# --------------------------------------------------------------------------- the repeat guard
#
# Observed 2026-08-28 (a user agent on Grok): the model answered every `show_edges({"on": true})` result
# identical call again — 40+ times, each one broadcasting a patch to every connected client — and only
# stopped when the server was killed. `llm.MAX_TOOL_HOPS` bounds the turn; this guard cuts the specific
# pathology far earlier, and without executing the repeat.

class RepeatingLLM:
    """Calls one tool with identical arguments `n` times in a single turn."""

    def __init__(self, name, tool, args, n):
        self.name, self.tool, self.args, self.n = name, tool, args, n
        self.results: list[str] = []

    async def run_turn(self, *, system, history, user_text, tools, execute_tool, emit):
        for _ in range(self.n):
            self.results.append(await execute_tool(self.tool, dict(self.args)))
        await emit("done", final=True)
        return "done"


async def test_an_identical_repeated_tool_call_stops_being_executed():
    llm = RepeatingLLM("Claude", "show_edges", {"on": True}, n=6)
    d = _director(Claude=llm)
    await d.handle("annotations and edges")
    # Twice through — a read/edit/read pair is legitimate — then never again, however long it goes on.
    assert d._session.calls == [("show_edges", {"on": True})] * 2
    assert len(llm.results) == 6                       # every call still ANSWERED, so the turn can end


async def test_the_refusal_tells_the_model_what_to_do_instead():
    """Cutting the model off mid-turn would leave the user with silence. Handing back a result that
    names the loop lets it read its way out and reply."""
    llm = RepeatingLLM("Claude", "show_edges", {"on": True}, n=4)
    d = _director(Claude=llm)
    await d.handle("edges")
    refusal = llm.results[-1]
    assert "show_edges" in refusal and "already" in refusal
    assert refusal.startswith("error:")                # shaped like every other tool failure it knows


async def test_the_same_tool_with_different_arguments_is_not_a_repeat():
    """The guard keys on the arguments too — 'make 12 blue, make 13 red' is one tool, real progress."""
    class _Varying:
        name = "Claude"

        async def run_turn(self, *, system, history, user_text, tools, execute_tool, emit):
            for cid in range(5):
                await execute_tool("style_surface", {"id": cid, "color": "blue"})
            await emit("done", final=True)
            return "done"

    d = _director(Claude=_Varying())
    await d.handle("colour them all")
    assert len(d._session.calls) == 5


async def test_the_repeat_count_resets_between_turns():
    """A guard that carried across turns would refuse 'edges on' just because you asked yesterday."""
    d = _director(Claude=RepeatingLLM("Claude", "show_edges", {"on": True}, n=2))
    await d.handle("edges")
    d.roster["Claude"] = RepeatingLLM("Claude", "show_edges", {"on": True}, n=2)
    await d.handle("edges again")
    assert d._session.calls == [("show_edges", {"on": True})] * 4     # all four ran; none refused


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


async def test_llm_sections_resolve_per_turn_and_follow_a_switch():
    """The active LLM changes under `llm <name>` and the switch is SHARED (§5.2), so the prompt has to
    be resolved every turn. Freezing it at construction would work perfectly until someone switched
    models mid-session — the failure only shows after a switch, which is the worst time to find it."""
    prompt = "Build.\n{#llm}\n{=grok}\n- grok guardrail\n{=*}\n- everyone else\n{/llm}\n"
    claude, grok = FakeLLM("Claude"), FakeLLM("Grok")
    d = Director(settings=None, session=FakeSession(), roster={"Claude": claude, "Grok": grok},
                 active="Claude", tools=[], prompt=prompt, agent=_agent([]))
    await d.handle("add a tree")
    assert "everyone else" in claude.seen[0]["system"]
    assert "grok guardrail" not in claude.seen[0]["system"]

    d.active = "Grok"                                          # what the shell's `llm grok` does
    await d.handle("add another")
    assert "grok guardrail" in grok.seen[0]["system"]
    assert "everyone else" not in grok.seen[0]["system"]
    assert "{#llm}" not in grok.seen[0]["system"]              # markers never reach the model


async def test_injection_inside_a_dropped_llm_branch_is_never_fetched():
    """Sections resolve BEFORE injections, so a `{context}` in a branch this LLM doesn't get costs no
    MCP resource fetch at all — and `context_stats` stays an honest account of what was sent."""
    llm, session = FakeLLM("Claude"), FakeSession()
    prompt = "Build.\n{#llm}\n{=grok}\n{#context}scene:\n{context}{/context}\n{/llm}\n"
    d = Director(settings=None, session=session, roster={"Claude": llm}, active="Claude", tools=[],
                 prompt=prompt, agent=_agent(["room://current"]))
    await d.handle("add a tree")
    assert session.resources_read == []                        # the placeholder never survived to be seen
    assert "scene:" not in llm.seen[0]["system"]
    # …and the same prompt on Grok does fetch it, so the skip is the branch and not a broken placeholder
    grok, session2 = FakeLLM("Grok"), FakeSession()
    d2 = Director(settings=None, session=session2, roster={"Grok": grok}, active="Grok", tools=[],
                  prompt=prompt, agent=_agent(["room://current"]))
    await d2.handle("add a tree")
    assert session2.resources_read == ["room://current"]
    assert "Room: 2 surfaces (test)" in grok.seen[0]["system"]


async def test_context_fetch_failure_is_not_fatal():
    class BoomSession(FakeSession):
        async def read_resource(self, uri):
            raise RuntimeError("no such resource")

    llm = FakeLLM("Claude")
    d = Director(settings=None, session=BoomSession(), roster={"Claude": llm}, active="Claude",
                 tools=[], prompt="Build.\n{#context}scene:\n{context}{/context}",
                 agent=_agent(["room://current"]))
    out = await d.handle("add a tree")                       # must not raise
    assert out == "Claude: done «daniel: add a tree»"
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
    d._prompt, d._speaker = "You act for '{user}'. Build stuff.", "alice"   # {user} = the current speaker
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
    # capabilities injected as env (never LLM args): scope + tool allow-list + access level
    assert p.env["CONJURE_SCOPE"].endswith("/agents/builder")
    assert p.env["CONJURE_TOOLS"] == "" and p.env["CONJURE_ACCESS"] == "all"   # no tools by default (opt-in)


def test_stdio_params_scaffolds_tool_scope_capabilities():
    import sys  # noqa: F401

    from conjure.agents import ServerSpec
    from conjure.director import _stdio_params

    spec = ServerSpec(name="world", command="python", args=[], env={})
    p = _stdio_params(spec, type("S", (), {"world_url": "http://h"})(), agent="outdoor",
                      tools=["set_skybox", "generate_skybox_image"], access="read")
    assert p.env["CONJURE_TOOLS"] == "set_skybox,generate_skybox_image"
    assert p.env["CONJURE_ACCESS"] == "read"
    assert p.env["CONJURE_SCOPE"].endswith("/agents/outdoor")


def test_scope_tools_is_opt_in_only_and_fails_loud_on_typo():
    from conjure.director import _scope_tools

    def T(n):
        return type("T", (), {"name": n, "description": "", "inputSchema": {}})()

    live = [T("set_skybox"), T("style_surface"), T("generate_skybox_image")]
    assert _scope_tools(live, []) == []                                                    # none by default (deny)
    assert {t.name for t in _scope_tools(live, ["set_skybox"])} == {"set_skybox"}           # explicit opt-in
    assert {t.name for t in _scope_tools(live, ["set_skybox", "style_surface"])} \
        == {"set_skybox", "style_surface"}
    with pytest.raises(RuntimeError, match="unknown tool"):
        _scope_tools(live, ["set_skybox", "nope"])                                          # typo → loud


async def test_identity_aware_director_tells_mcp_the_per_turn_speaker():
    # Step 3: a connect-built (identity-aware) director sends set_caller(speaker, scope) before the turn's
    # tools, so they act as WHO spoke — not the MCP server's fixed launch identity.
    d = _director(active="Claude")
    d._identity_aware = True
    d.agent = type("A", (), {"name": "builder", "context": []})()
    await d.handle("put a tree", speaker="guest")
    assert ("set_caller", {"user": "guest", "scope": "guest/agents/builder"}) in d._session.calls


async def test_hand_built_director_does_not_set_caller():
    # Hand-built/test directors (no real MCP) skip set_caller, so existing call-sequence tests stay clean.
    d = _director(active="Claude")                          # _identity_aware defaults False
    await d.handle("put a tree", speaker="guest")
    assert all(name != "set_caller" for name, _ in d._session.calls)


async def test_execute_tool_blocks_out_of_agent_scope():
    d = _director(active="Claude")
    d._allowed_tools = {"set_skybox"}                       # e.g. an outdoor-style scoped director
    out = await d._execute_tool("style_surface", {}, None, "outdoor.claude")
    assert "not available" in out                          # refused
    assert d._session.calls == []                          # never reached the MCP session
    await d._execute_tool("set_skybox", {"image_id": "x"}, None, "outdoor.claude")
    assert d._session.calls == [("set_skybox", {"image_id": "x"})]   # allowed tool runs


def test_recent_history_caps_the_model_view_but_keeps_full_transcript():
    import types
    from conjure.director import Director, Turn
    d = Director(settings=None, session=FakeSession(), roster={"Claude": object()}, active="Claude", tools=[])
    d.transcript = [Turn("user" if i % 2 == 0 else "assistant", f"t{i}") for i in range(10)]
    d._settings = types.SimpleNamespace(history_cap=4)
    recent = d._recent_history()
    assert [t.text for t in recent] == ["t6", "t7", "t8", "t9"]     # last 4 sent to the LLM
    assert len(d.transcript) == 10                                   # full transcript untouched (persist/backlog)
    d._settings = types.SimpleNamespace(history_cap=0)               # 0 = unlimited
    assert len(d._recent_history()) == 10


# --------------------------------------------------------------------------- context accounting (status bar)

def _tool(name, description, schema):
    return ToolSpec(name=name, description=description, input_schema=schema)


async def test_context_stats_before_any_turn_reports_what_is_free_to_compute():
    # On connect no turn has been assembled, so there is nothing measured. Report the parts that cost
    # nothing (the prompt template, the tool schemas, the transcript tail) rather than a bar of zeros
    # that would read as "tools 100%".
    d = _director(tools=[_tool("place_asset", "Place a 3D model", {"type": "object"})])
    stats = d.context_stats()
    assert stats["turns"] == 0
    assert stats["chars"]["tools"] > 0
    assert stats["chars"]["prompt"] == len(d._prompt or "")
    assert stats["chars"]["room"] == 0            # the {context} injection is an MCP fetch — not for a status bar


async def test_context_stats_after_a_turn_measures_what_was_actually_sent():
    d = _director(tools=[_tool("place_asset", "Place a 3D model", {"type": "object"})])
    d._prompt = "You are a builder. Room: {context}"
    d.agent = _agent(["world://room"])
    d._session._resource_text = "SURFACE-DATA" * 20          # the live room injection
    await d.handle("put a tree in front of me", speaker="daniel")

    chars = d.context_stats()["chars"]
    assert chars["room"] == len("SURFACE-DATA" * 20)         # attributed to the injection, not the prompt
    assert chars["prompt"] > 0 and chars["room"] not in (0, chars["prompt"])
    assert chars["history"] >= len("daniel: put a tree in front of me")
    assert chars["tools"] > 0
    # turns counts transcript ENTRIES — a user line and its reply are two, which is what `cap` trims.
    assert d.context_stats()["turns"] == 2


async def test_context_stats_turn_cap_comes_from_settings():
    import dataclasses
    from conjure.config import get_settings
    d = _director()
    d._settings = dataclasses.replace(get_settings(), history_cap=8)
    assert d.context_stats()["cap"] == 8
    d._settings = dataclasses.replace(get_settings(), history_cap=0)
    assert d.context_stats()["cap"] == 0                     # 0 = no trimming


def test_tools_chars_counts_name_description_and_schema():
    from conjure.director import _tools_chars
    assert _tools_chars([]) == 0
    small = _tools_chars([_tool("a", "b", {})])
    big = _tools_chars([_tool("a", "b" * 100, {"type": "object"})])
    assert big > small > 0
    # an unserializable schema must not break a turn — it's just skipped
    assert _tools_chars([_tool("a", "b", {"bad": object()})]) >= 0
