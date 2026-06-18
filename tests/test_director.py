"""Director routing + orchestration — the shared brain for voice and CLI.

Routing is pure and deterministic, so it gets thorough unit coverage (this is where the
"switch / address an LLM mid-conversation" behavior lives). The orchestration test drives a real
Director against fake LLMs + a fake MCP session — no network, no SDKs."""

import pytest

from conjure.director import DIRECTOR_PROMPT, Director, route_turn
from conjure.llm import Turn

ROSTER = {"Claude": object(), "Gemini": object()}  # route_turn only needs the names


# --------------------------------------------------------------------------- routing

def test_plain_request_goes_to_active():
    r = route_turn("put an oak tree in front of me", ROSTER, "Claude")
    assert (r.target, r.content, r.persistent) == ("Claude", "put an oak tree in front of me", False)


def test_let_me_talk_to_is_a_persistent_handover():
    r = route_turn("let me talk to Gemini", ROSTER, "Claude")
    assert r.target == "Gemini" and r.persistent is True
    assert r.content == ""  # no task → Director substitutes a greeting nudge (see handle())


def test_handover_carries_a_trailing_task():
    r = route_turn("let me speak with Gemini about the lighting", ROSTER, "Claude")
    assert (r.target, r.persistent) == ("Gemini", True)
    assert r.content == "about the lighting"


def test_switch_to_is_persistent_and_case_insensitive():
    r = route_turn("switch to gemini", ROSTER, "Claude")
    assert (r.target, r.persistent) == ("Gemini", True)


def test_take_over_is_a_persistent_handover_with_task():
    r = route_turn("Gemini, take over and add a tree", ROSTER, "Claude")
    assert (r.target, r.persistent) == ("Gemini", True)
    assert r.content == "and add a tree"


def test_direct_address_is_one_shot_not_persistent():
    r = route_turn("Gemini, make a picture of a cat", ROSTER, "Claude")
    assert (r.target, r.content, r.persistent) == ("Gemini", "make a picture of a cat", False)


def test_direct_address_without_comma_still_routes():
    # STT rarely punctuates, so "Claude make a cat" must still address Claude.
    r = route_turn("Claude make a cat", ROSTER, "Gemini")
    assert (r.target, r.content, r.persistent) == ("Claude", "make a cat", False)


def test_unknown_name_falls_through_to_active():
    r = route_turn("Bob, do something", ROSTER, "Claude")
    assert r.target == "Claude" and r.content == "Bob, do something" and r.persistent is False


def test_ordinary_first_word_is_not_mistaken_for_a_name():
    r = route_turn("tree in front of me please", ROSTER, "Claude")
    assert r.target == "Claude" and r.content == "tree in front of me please"


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
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return type("R", (), {"content": [type("C", (), {"text": "ok"})()]})()


def _director(active="Claude", tools=None, **llms):
    roster = llms or {"Claude": FakeLLM("Claude"), "Gemini": FakeLLM("Gemini")}
    return Director(settings=None, session=FakeSession(), roster=roster, active=active, tools=tools or [])


async def test_handle_records_attributed_transcript():
    d = _director()
    out = await d.handle("put a tree in front of me")
    assert out == "Claude: done «put a tree in front of me»"
    assert [(t.speaker, t.text) for t in d.transcript] == [
        ("user", "put a tree in front of me"),
        ("Claude", "Claude: done «put a tree in front of me»"),
    ]


async def test_persistent_handover_changes_active():
    d = _director(active="Claude")
    await d.handle("let me talk to Gemini")
    assert d.active == "Gemini"
    # bare handover → Gemini is asked to greet (not replay the switch phrase, not build)
    nudge = d.roster["Gemini"].seen[0]["user_text"].lower()
    assert "greet" in nudge and "switched to you" in nudge
    # subsequent plain turns now go to Gemini
    await d.handle("add a fountain")
    assert d.transcript[-1].speaker == "Gemini"


async def test_failed_handover_reverts_active():
    """If switching to an LLM fails on its first turn (e.g. quota error), don't strand the user on
    the broken LLM — revert to whoever they were talking to. The error still propagates."""
    class BoomLLM:
        name = "Chat"

        async def run_turn(self, **kw):
            raise RuntimeError("insufficient_quota")

    d = _director(active="Claude", Claude=FakeLLM("Claude"), Chat=BoomLLM())
    with pytest.raises(RuntimeError, match="quota"):
        await d.handle("let me speak with Chat")
    assert d.active == "Claude"            # reverted
    assert d.transcript == []             # nothing recorded for the failed turn


async def test_one_shot_address_does_not_change_active():
    d = _director(active="Claude")
    await d.handle("Gemini, make a picture of a cat")
    assert d.active == "Claude"                       # stayed put
    assert d.transcript[-1].speaker == "Gemini"       # but Gemini answered this turn
    assert d.roster["Gemini"].seen[0]["user_text"] == "make a picture of a cat"


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


async def test_later_llm_sees_prior_turns_with_attribution():
    d = _director(active="Claude")
    await d.handle("Gemini, suggest a centerpiece")   # one-shot to Gemini
    await d.handle("what do you think?")              # back to active Claude
    history = d.roster["Claude"].seen[-1]["history"]
    # Claude's view includes Gemini's earlier turn; it is attributed (Director hands raw Turns;
    # the [Name] prefixing happens in llm._attributed, covered in test_llm).
    speakers = [t.speaker for t in history]
    assert "Gemini" in speakers and "user" in speakers


def test_system_prompt_is_per_llm_and_roster_aware():
    d = _director(active="Claude")
    sys_claude = d._system_for("Claude")
    assert sys_claude.startswith("You are Claude,")
    assert "Gemini" in sys_claude                     # told who else is present
    assert "[Name]" in sys_claude                     # told how attribution is marked


def test_prompt_template_has_a_single_name_placeholder():
    # Guards against accidental stray braces breaking .format(name=...). DIRECTOR_PROMPT now reads from
    # prompts/builder.md (the builder agent's prompt_file), so this also guards that file.
    assert DIRECTOR_PROMPT.count("{name}") == 1
    DIRECTOR_PROMPT.format(name="X")  # must not raise


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
