"""LLM roster — transcript rendering, the per-provider tool-call loops (with faked SDKs), and the
registry. No network: anthropic/genai clients are monkeypatched."""

from conjure.config import Settings
from conjure.llm import (
    HOP_LIMIT_NOTICE,
    MAX_TOOL_HOPS,
    ClaudeLLM,
    GeminiLLM,
    GrokImageGenerator,
    GrokLLM,
    OpenAIImageGenerator,
    OpenAILLM,
    ToolSpec,
    Turn,
    _messages,
    build_image_generators,
    build_roster,
    select_generator,
    vendor_for,
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


def test_messages_label_user_turns_with_the_speaker():
    # A shared, multi-user conversation: each user turn is prefixed with WHO spoke, so the director can
    # attribute turns ("who said the last line?"). The assistant turn stays unattributed (one director,
    # no LLM identity). A turn with no speaker (a lone client) is left bare.
    history = [Turn("user", "hi", by="alice"), Turn("assistant", "hello"),
               Turn("user", "yo", by="bob"), Turn("user", "no speaker")]
    assert _messages(history) == [
        ("user", "alice: hi"), ("assistant", "hello"), ("user", "bob: yo"), ("user", "no speaker")]


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


# --------------------------------------------------------------------------- Grok (OpenAI-compatible) director

async def _run_director(llm, monkeypatch):
    """Drive one no-tool turn through an OpenAI-compatible director, capturing the AsyncOpenAI kwargs
    (so a test can assert whether/where `base_url` is set)."""
    import openai

    captured = {}

    class _Chat:
        async def create(self, **kw):
            return _oa_resp("hi there", None)

    def _fake(**kw):
        captured.update(kw)
        return type("Cl", (), {"chat": type("C", (), {"completions": _Chat()})()})()

    monkeypatch.setattr(openai, "AsyncOpenAI", _fake)

    async def emit(t, *, final): ...

    async def execute_tool(name, args): return "ok"

    out = await llm.run_turn(system="SYS", history=[], user_text="hi",
                             tools=[], execute_tool=execute_tool, emit=emit)
    return out, captured


async def test_grok_director_points_at_xai_base_url(monkeypatch):
    # Grok reuses the OpenAI loop verbatim; the only difference is the base URL.
    out, kw = await _run_director(GrokLLM("Grok", "key", "grok-4"), monkeypatch)
    assert out == "hi there"
    assert kw["base_url"] == "https://api.x.ai/v1" and kw["api_key"] == "key"


async def test_openai_director_uses_no_base_url(monkeypatch):
    # The reference OpenAI director talks to the SDK default endpoint — no base_url override.
    _, kw = await _run_director(OpenAILLM("Chat", "key", "gpt-x"), monkeypatch)
    assert "base_url" not in kw


def test_grok_is_in_the_roster_and_vendor_alias():
    roster, _ = build_roster(_settings(xai_api_key="x"))
    assert "Grok" in roster and roster["Grok"].name == "Grok"
    assert vendor_for("Grok") == "xai"


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


def test_a_refusal_names_the_generator_that_can_do_it():
    """Observed 2026-08-28: asked for a transparent image "using Gemini", the director got back
    "omit the generator or pick one that can", never worked out *which* one could, and gave up on the
    request entirely. A refusal has to carry its own next step — both ways out, by name."""
    reg = _registry(google_api_key="g", openai_api_key="o")
    _, err = select_generator(reg, "generate", requested="Gemini", transparent=True)
    assert "Chat" in err            # the one that CAN — otherwise the caller is guessing
    assert "Gemini" in err          # and the one it keeps by dropping transparency instead
    _, err = select_generator(reg, "outpaint", requested="Chat")
    assert "Gemini" in err


def test_a_refusal_says_so_plainly_when_nothing_can_do_it():
    """Naming an alternative that doesn't exist would send the caller round a second useless loop."""
    reg = _registry(google_api_key="g")           # Gemini only — nothing can do transparency
    _, err = select_generator(reg, "generate", requested="Gemini", transparent=True)
    assert "No configured generator" in err


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


# --------------------------------------------------------------------------- the hop cap
#
# Every director loop feeds tool results back and asks again. Nothing in any vendor's protocol says the
# model ever stops: observed 2026-08-28, Grok answered every `show_edges` result with the same call
# again, 40+ times, each hop an API call and a patch broadcast to every client, until the server was
# killed by hand. These tests pin the backstop in all three loops — a stuck model must end the turn,
# not the process.

async def _never_stops_openai(monkeypatch):
    """An SDK stand-in that answers every request with the same tool call, forever."""
    import openai

    hops = {"n": 0}

    class _Chat:
        async def create(self, **kw):
            hops["n"] += 1
            return _oa_resp(None, [_oa_tc(f"c{hops['n']}", "show_edges", '{"on": true}')])

    monkeypatch.setattr(openai, "AsyncOpenAI",
                        lambda **kw: type("Cl", (), {"chat": type("C", (), {"completions": _Chat()})()})())
    return hops


async def test_a_model_that_never_stops_calling_tools_ends_the_turn(monkeypatch):
    hops = await _never_stops_openai(monkeypatch)
    emitted, executed = [], []

    async def emit(t, *, final): emitted.append((final, t))

    async def execute_tool(name, args): executed.append(name); return "Surface edges on."

    out = await GrokLLM("Grok", "key", "grok-4").run_turn(
        system="SYS", history=[], user_text="annotations and edges",
        tools=[ToolSpec("show_edges", "edges", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert hops["n"] == MAX_TOOL_HOPS          # bounded — without the cap this test never returns
    assert len(executed) == MAX_TOOL_HOPS
    assert out == HOP_LIMIT_NOTICE             # and the turn RESOLVES rather than dying silently


async def test_hitting_the_cap_is_audible_and_recorded(monkeypatch):
    """The notice is emitted final=True, so it reaches the user's ears AND lands in the transcript —
    which is what shows the model its own failure on the next turn instead of an unexplained gap."""
    await _never_stops_openai(monkeypatch)
    emitted = []

    async def emit(t, *, final): emitted.append((final, t))

    async def execute_tool(name, args): return "ok"

    out = await GrokLLM("Grok", "key", "grok-4").run_turn(
        system="SYS", history=[], user_text="go",
        tools=[ToolSpec("show_edges", "edges", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert emitted[-1] == (True, HOP_LIMIT_NOTICE) and out == HOP_LIMIT_NOTICE


async def test_the_claude_loop_is_bounded_too(monkeypatch):
    """Three loops, one constant — a cap on only the provider that misbehaved would be a patch."""
    import anthropic

    hops = {"n": 0}

    class _Msgs:
        async def create(self, **kw):
            hops["n"] += 1
            return type("R", (), {"content": [
                _blk("tool_use", id=f"t{hops['n']}", name="show_edges", input={"on": True})]})()

    monkeypatch.setattr(anthropic, "AsyncAnthropic",
                        lambda **kw: type("C", (), {"messages": _Msgs()})())

    async def emit(t, *, final): ...

    async def execute_tool(name, args): return "ok"

    out = await ClaudeLLM("Claude", "key", "claude-x").run_turn(
        system="SYS", history=[], user_text="go",
        tools=[ToolSpec("show_edges", "edges", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert hops["n"] == MAX_TOOL_HOPS and out == HOP_LIMIT_NOTICE


async def test_the_gemini_loop_is_bounded_too(monkeypatch):
    from google import genai

    hops = {"n": 0}
    fc = type("FC", (), {"name": "show_edges", "args": {"on": True}})()

    class _Models:
        async def generate_content(self, **kw):
            hops["n"] += 1
            return _resp([_part(function_call=fc)])

    class _Client:
        def __init__(self, **kw):
            self.aio = type("Aio", (), {"models": _Models()})()

    monkeypatch.setattr(genai, "Client", _Client)

    async def emit(t, *, final): ...

    async def execute_tool(name, args): return "ok"

    out = await GeminiLLM("Gemini", "key", "gemini-x").run_turn(
        system="SYS", history=[], user_text="go",
        tools=[ToolSpec("show_edges", "edges", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert hops["n"] == MAX_TOOL_HOPS and out == HOP_LIMIT_NOTICE


async def test_a_turn_that_finishes_normally_is_untouched_by_the_cap(monkeypatch):
    """The guard must not shorten real work: a two-hop turn still returns the model's own final text."""
    import openai

    responses = [
        _oa_resp("On it", [_oa_tc("c1", "show_edges", '{"on": true}')]),
        _oa_resp("Edges are on.", None),
    ]
    state = {"n": 0}

    class _Chat:
        async def create(self, **kw):
            r = responses[state["n"]]; state["n"] += 1
            return r

    monkeypatch.setattr(openai, "AsyncOpenAI",
                        lambda **kw: type("Cl", (), {"chat": type("C", (), {"completions": _Chat()})()})())

    async def emit(t, *, final): ...

    async def execute_tool(name, args): return "ok"

    out = await GrokLLM("Grok", "key", "grok-4").run_turn(
        system="SYS", history=[], user_text="edges please",
        tools=[ToolSpec("show_edges", "edges", {"type": "object"})],
        execute_tool=execute_tool, emit=emit,
    )
    assert out == "Edges are on."


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


# --------------------------------------------------------------------------- Grok image gen (faked)

def test_grok_image_generator_is_generate_only():
    gens = build_image_generators(_settings(xai_api_key="x"))
    assert set(gens) == {"Grok"}
    caps = gens["Grok"].capabilities
    assert caps.operations == frozenset({"generate"})
    assert caps.transparency is False and caps.aspect == "free" and caps.max_resolution == 2048


def test_grok_is_never_auto_selected_but_honored_on_request():
    # Gemini/OpenAI own every default op; Grok only appears when explicitly asked for (by name or alias).
    reg = build_image_generators(_settings(google_api_key="g", xai_api_key="x"))
    assert select_generator(reg, "generate")[0].name == "Gemini"          # default, not Grok
    assert select_generator(reg, "generate", requested="Grok")[0].name == "Grok"
    assert select_generator(reg, "generate", requested="xai")[0].name == "Grok"   # vendor alias
    # it can't do the ops it doesn't advertise
    gen, err = select_generator(reg, "edit", requested="Grok")
    assert gen is None and "edit" in err


async def test_grok_image_generate_decodes_b64_and_rides_extra_body(monkeypatch):
    import base64

    import openai

    payload = base64.b64encode(b"GROKPNG").decode()

    class _Images:
        async def generate(self, **kw):
            _Images.kw = kw
            return type("R", (), {"data": [type("D", (), {"b64_json": payload})()]})()

    monkeypatch.setattr(openai, "AsyncOpenAI",
                        lambda **kw: type("Cl", (), {"images": _Images(), "_kw": kw})())
    res = await GrokImageGenerator("Grok", "k", "grok-imagine-image-2.0").generate(
        "a neon skyline", aspect_ratio="16:9")
    assert res.data == b"GROKPNG" and res.provider == "Grok"
    # aspect_ratio + resolution are xAI extensions → extra_body, and bytes come back as b64_json
    assert _Images.kw["response_format"] == "b64_json"
    assert _Images.kw["extra_body"] == {"resolution": "2k", "aspect_ratio": "16:9"}
