"""LLM roster — transcript rendering, the per-provider tool-call loops (with faked SDKs), and the
registry. No network: anthropic/genai clients are monkeypatched."""

from conjure.config import Settings
from conjure.llm import (
    ClaudeLLM,
    GeminiLLM,
    OpenAIImageGenerator,
    OpenAILLM,
    ToolSpec,
    Turn,
    _messages,
    build_image_generators,
    build_roster,
    select_generator,
)


def _settings(**overrides) -> Settings:
    base = dict(
        stt="whisper", tts="kokoro", llm="claude", llm_model="claude-x", gemini_model="gemini-x",
        image_provider="gemini", image_model="im", skybox_model="sm", skybox_size="4K",
        anthropic_api_key=None, poly_pizza_api_key=None, openai_api_key=None, google_api_key=None,
        host="0.0.0.0", port=8080, world_url="http://localhost:8080",
    )
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- transcript

def test_messages_render_plain_user_assistant():
    history = [
        Turn("user", "hi"),
        Turn("assistant", "I placed a tree"),
        Turn("assistant", "I'd add a fountain"),
    ]
    # No LLM identity survives into the history: every assistant turn is plain, no [Name] prefix,
    # so a switch of LLMs is invisible.
    assert _messages(history) == [
        ("user", "hi"),
        ("assistant", "I placed a tree"),
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
        system="SYS", history=[Turn("user", "earlier"), Turn("assistant", "hello")],
        user_text="put a tree in front of me",
        tools=[ToolSpec("place_asset", "place a model", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert out == "Done — there's your tree."
    assert tool_calls == [("place_asset", {"query": "tree", "size_m": 7})]
    assert (False, "On it") in emitted and emitted[-1] == (True, "Done — there's your tree.")
    # history was serialized plainly (no LLM attribution) ahead of the new user msg
    msgs = calls["last_kwargs"]["messages"]
    assert msgs[0] == {"role": "user", "content": "earlier"}
    assert msgs[1] == {"role": "assistant", "content": "hello"}
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


# --------------------------------------------------------------------------- OpenAI director loop

def _oa_tc(id, name, arguments):
    fn = type("F", (), {"name": name, "arguments": arguments})()
    return type("TC", (), {"id": id, "type": "function", "function": fn})()


def _oa_resp(content=None, tool_calls=None):
    msg = type("M", (), {"content": content, "tool_calls": tool_calls})()
    return type("R", (), {"choices": [type("Ch", (), {"message": msg})()]})()


async def test_openai_director_runs_a_tool_then_returns_final(monkeypatch):
    import openai

    responses = [
        _oa_resp("On it", [_oa_tc("c1", "place_asset", '{"query": "tree", "size_m": 7}')]),
        _oa_resp("Done — there's your tree.", None),
    ]
    state = {"n": 0, "last": None}

    class _Chat:
        async def create(self, **kw):
            state["last"] = kw
            r = responses[state["n"]]; state["n"] += 1
            return r

    monkeypatch.setattr(openai, "AsyncOpenAI",
                        lambda **kw: type("Cl", (), {"chat": type("C", (), {"completions": _Chat()})()})())

    emitted, tool_calls = [], []

    async def emit(t, *, final): emitted.append((final, t))

    async def execute_tool(name, args): tool_calls.append((name, args)); return "ok"

    out = await OpenAILLM("Chat", "key", "gpt-x").run_turn(
        system="SYS", history=[Turn("user", "hi"), Turn("assistant", "yo")],
        user_text="put a tree in front of me",
        tools=[ToolSpec("place_asset", "place", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert out == "Done — there's your tree."
    assert tool_calls == [("place_asset", {"query": "tree", "size_m": 7})]   # JSON-string args parsed
    assert emitted[-1] == (True, "Done — there's your tree.")
    msgs = state["last"]["messages"]
    assert msgs[0] == {"role": "system", "content": "SYS"}                   # system first
    assert {"role": "tool", "tool_call_id": "c1", "content": "ok"} in msgs   # tool result fed back
    assert msgs[2] == {"role": "assistant", "content": "yo"}                 # plain history, no attribution


# --------------------------------------------------------------------------- image registry + capabilities

def test_build_image_generators_keyed_by_casual_name():
    gens = build_image_generators(_settings(google_api_key="g", openai_api_key="o"))
    assert set(gens) == {"Gemini", "Chat"}
    assert gens["Gemini"].capabilities.aspect == "free" and gens["Gemini"].capabilities.max_resolution == 4096
    assert gens["Chat"].capabilities.transparency is True and gens["Chat"].capabilities.aspect == "fixed"


def test_only_image_capable_keyed_vendors_appear():
    # Claude has no image facet; an anthropic-only setup has no image generators.
    assert build_image_generators(_settings(anthropic_api_key="a")) == {}


def test_roster_includes_chat_with_openai_key():
    roster, _ = build_roster(_settings(openai_api_key="o"))
    assert "Chat" in roster and roster["Chat"].name == "Chat"


def test_openai_size_snapping():
    g = OpenAIImageGenerator("Chat", "k", "gpt-image-1")
    assert g._size_for("21:9") == "1536x1024"
    assert g._size_for("9:16") == "1024x1536"
    assert g._size_for("1:1") == "1024x1024"
    assert g._size_for(None) == "1024x1024"


# --------------------------------------------------------------------------- mediation (select_generator)

def _registry(**keys):
    return build_image_generators(_settings(**keys))


def test_default_routes_to_gemini_for_every_op():
    reg = _registry(google_api_key="g", openai_api_key="o")
    for op in ("generate", "edit", "outpaint", "skybox", "skybox_from", "grounded_skybox"):
        gen, err = select_generator(reg, op)
        assert err is None and gen.name == "Gemini", op


def test_transparency_steers_to_openai():
    reg = _registry(google_api_key="g", openai_api_key="o")
    gen, err = select_generator(reg, "generate", transparent=True)
    assert err is None and gen.name == "Chat"


def test_requested_generator_lacking_capability_errors():
    reg = _registry(google_api_key="g", openai_api_key="o")
    gen, err = select_generator(reg, "outpaint", requested="Chat")
    assert gen is None and "outpaint" in err
    gen, err = select_generator(reg, "generate", requested="Gemini", transparent=True)
    assert gen is None and "transparency" in err.lower()


def test_requested_unknown_generator_errors():
    reg = _registry(google_api_key="g")
    gen, err = select_generator(reg, "generate", requested="Nope")
    assert gen is None and "not configured" in err


def test_no_capable_generator_errors():
    reg = _registry(openai_api_key="o")  # only OpenAI → can't skybox at all
    gen, err = select_generator(reg, "skybox")
    assert gen is None and "skybox" in err


def test_explicit_request_honored_when_capable():
    reg = _registry(google_api_key="g", openai_api_key="o")
    gen, err = select_generator(reg, "generate", requested="Chat")
    assert err is None and gen.name == "Chat"


def test_request_accepts_vendor_alias():
    # Users/LLMs say "OpenAI"/"Google", not the casual name — resolve those to the right generator.
    reg = _registry(google_api_key="g", openai_api_key="o")
    assert select_generator(reg, "generate", requested="OpenAI")[0].name == "Chat"
    assert select_generator(reg, "edit", requested="google")[0].name == "Gemini"


# --------------------------------------------------------------------------- OpenAI image gen (faked)

async def test_openai_image_generate_decodes_b64(monkeypatch):
    import base64

    import openai

    payload = base64.b64encode(b"PNGBYTES").decode()

    class _Images:
        async def generate(self, **kw):
            _Images.kw = kw
            return type("R", (), {"data": [type("D", (), {"b64_json": payload})()]})()

    monkeypatch.setattr(openai, "AsyncOpenAI",
                        lambda **kw: type("Cl", (), {"images": _Images()})())
    res = await OpenAIImageGenerator("Chat", "k", "gpt-image-1").generate(
        "a red circle", aspect_ratio="21:9", transparent=True)
    assert res.data == b"PNGBYTES" and res.provider == "Chat"
    assert _Images.kw["size"] == "1536x1024" and _Images.kw["background"] == "transparent"
