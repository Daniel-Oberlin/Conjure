"""LLM roster — attribution rendering, the per-provider tool-call loops (with faked SDKs), and the
registry. No network: anthropic/genai clients are monkeypatched."""

from conjure.config import Settings
from conjure.llm import ClaudeLLM, GeminiLLM, ToolSpec, Turn, _attributed, build_roster


def _settings(**overrides) -> Settings:
    base = dict(
        stt="whisper", tts="kokoro", llm="claude", llm_model="claude-x", gemini_model="gemini-x",
        image_provider="gemini", image_model="im", skybox_model="sm", skybox_size="4K",
        anthropic_api_key=None, poly_pizza_api_key=None, openai_api_key=None, google_api_key=None,
        host="0.0.0.0", port=8080, world_url="http://localhost:8080",
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- attribution

def test_attributed_labels_other_llms_only():
    history = [
        Turn("user", "hi"),
        Turn("Claude", "I placed a tree"),
        Turn("Gemini", "I'd add a fountain"),
    ]
    # From Claude's perspective: its own turn is plain, Gemini's is prefixed.
    assert _attributed(history, "Claude") == [
        ("user", "hi"),
        ("assistant", "I placed a tree"),
        ("assistant", "[Gemini] I'd add a fountain"),
    ]
    # From Gemini's perspective the prefixes flip.
    assert _attributed(history, "Gemini") == [
        ("user", "hi"),
        ("assistant", "[Claude] I placed a tree"),
        ("assistant", "I'd add a fountain"),
    ]


# --------------------------------------------------------------------------- registry

def test_build_roster_includes_only_keyed_providers():
    roster, active = build_roster(_settings(anthropic_api_key="a", google_api_key="g"))
    assert set(roster) == {"Claude", "Gemini"} and active == "Claude"  # llm="claude" default


def test_build_roster_active_falls_back_when_preferred_absent():
    roster, active = build_roster(_settings(llm="claude", google_api_key="g"))  # no anthropic key
    assert set(roster) == {"Gemini"} and active == "Gemini"


def test_build_roster_empty_without_keys():
    roster, active = build_roster(_settings())
    assert roster == {} and active == ""


# --------------------------------------------------------------------------- Claude loop

def _blk(type_, **kw):
    return type("Blk", (), {"type": type_, **kw})()


async def test_claude_runs_tools_then_returns_final(monkeypatch):
    import anthropic

    # Turn 1: an acknowledgement + a tool call. Turn 2: the final reply, no tools.
    responses = [
        type("R", (), {"content": [
            _blk("text", text="On it"),
            _blk("tool_use", id="t1", name="place_asset", input={"query": "tree", "size_m": 7}),
        ]})(),
        type("R", (), {"content": [_blk("text", text="Done — there's your tree.")]})(),
    ]
    calls = {"n": 0}

    class _Msgs:
        async def create(self, **kw):
            r = responses[calls["n"]]; calls["n"] += 1
            calls["last_kwargs"] = kw
            return r

    monkeypatch.setattr(anthropic, "AsyncAnthropic",
                        lambda **kw: type("C", (), {"messages": _Msgs()})())

    emitted, tool_calls = [], []

    async def emit(text, *, final):
        emitted.append((final, text))

    async def execute_tool(name, args):
        tool_calls.append((name, args))
        return "ok"

    out = await ClaudeLLM("Claude", "key", "claude-x").run_turn(
        system="SYS", history=[Turn("user", "earlier"), Turn("Gemini", "hello")],
        user_text="put a tree in front of me",
        tools=[ToolSpec("place_asset", "place a model", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert out == "Done — there's your tree."
    assert tool_calls == [("place_asset", {"query": "tree", "size_m": 7})]
    assert (False, "On it") in emitted and emitted[-1] == (True, "Done — there's your tree.")
    # history was serialized with attribution (Gemini's turn prefixed) ahead of the new user msg
    msgs = calls["last_kwargs"]["messages"]
    assert msgs[0] == {"role": "user", "content": "earlier"}
    assert msgs[1] == {"role": "assistant", "content": "[Gemini] hello"}
    assert calls["last_kwargs"]["system"] == "SYS"


# --------------------------------------------------------------------------- Gemini loop

def _part(text=None, function_call=None):
    return type("P", (), {"text": text, "function_call": function_call})()


def _resp(parts):
    content = type("Ct", (), {"parts": parts})()
    return type("R", (), {"candidates": [type("Cd", (), {"content": content})()]})()


async def test_gemini_runs_a_tool_then_returns_final(monkeypatch):
    from google import genai

    fc = type("FC", (), {"name": "set_skybox", "args": {"prompt": "forest"}})()
    responses = [
        _resp([_part(text="Sure"), _part(function_call=fc)]),
        _resp([_part(text="Done — you're in a forest.")]),
    ]
    state = {"n": 0}

    class _Models:
        async def generate_content(self, **kw):
            r = responses[state["n"]]; state["n"] += 1
            return r

    class _Client:
        def __init__(self, **kw):
            self.aio = type("Aio", (), {"models": _Models()})()

    monkeypatch.setattr(genai, "Client", _Client)

    emitted, tool_calls = [], []

    async def emit(text, *, final):
        emitted.append((final, text))

    async def execute_tool(name, args):
        tool_calls.append((name, args))
        return "ok"

    out = await GeminiLLM("Gemini", "key", "gemini-x").run_turn(
        system="SYS", history=[], user_text="wrap me in a forest",
        tools=[ToolSpec("set_skybox", "set the sky", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert out == "Done — you're in a forest."
    assert tool_calls == [("set_skybox", {"prompt": "forest"})]
    assert (False, "Sure") in emitted and emitted[-1] == (True, "Done — you're in a forest.")
