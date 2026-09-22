"""LLM roster — transcript rendering, the per-provider tool-call loops (with faked SDKs), and the
registry. No network: anthropic/genai clients are monkeypatched."""

from conjure.config import Settings
from conjure.llm import (
    HOP_LIMIT_NOTICE,
    MAX_TOOL_HOPS,
    ClaudeLLM,
    LocalLLM,
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
    # System now rides as a block list so it can carry the cache breakpoint (see `_system_for`).
    assert calls["last_kwargs"]["system"] == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]


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


# --------------------------------------------------------------------------- tool search (§5a)

def _spec(name):
    return ToolSpec(name, f"does {name}", {"type": "object"})


def _claude(**settings_overrides):
    llm = ClaudeLLM("Claude", "key", "claude-x")
    llm.configure(_settings(**settings_overrides))
    return llm


def test_tool_search_is_off_by_default_and_the_request_is_unchanged():
    """It changes what the model sees on every turn, so nothing happens until it is asked for."""
    sent = _claude()._tools_for([_spec("place_asset"), _spec("query_world")])
    assert [t["name"] for t in sent] == ["place_asset", "query_world"]
    assert not any("defer_loading" in t for t in sent)


def test_tool_search_defers_everything_except_the_always_loaded_core():
    sent = _claude(tool_search=True, tool_search_always="query_world")._tools_for(
        [_spec("place_asset"), _spec("query_world"), _spec("set_skybox")])
    assert sent[0]["type"] == "tool_search_tool_regex_20251119"      # the search tool leads
    deferred = {t["name"]: t.get("defer_loading", False) for t in sent[1:]}
    assert deferred == {"place_asset": True, "query_world": False, "set_skybox": True}


def test_the_tools_themselves_are_not_changed_by_deferral():
    """Deferral is about how many schemas ride in the request, not about what the tools are — an
    agent's granted set must be identical either way or scoping has quietly moved."""
    tools = [_spec("place_asset"), _spec("query_world")]
    off = {t["name"] for t in _claude()._tools_for(tools)}
    on = {t["name"] for t in _claude(tool_search=True)._tools_for(tools)} - {"tool_search_tool_regex"}
    assert off == on


def test_bm25_can_be_selected_and_a_typo_falls_back_rather_than_sending_junk():
    assert _claude(tool_search=True, tool_search_kind="bm25")._tools_for([])[0]["type"] \
        == "tool_search_tool_bm25_20251119"
    assert _claude(tool_search=True, tool_search_kind="wat")._tools_for([])[0]["type"] \
        == "tool_search_tool_regex_20251119"


def test_build_roster_configures_a_director_that_asks_for_it():
    """`configure` is the opt-in hook — the roster must call it, or the setting is inert."""
    roster, _ = build_roster(_settings(anthropic_api_key="k", tool_search=True))
    assert roster["Claude"]._tools_for([_spec("x")])[0]["name"] == "tool_search_tool_regex"


# --------------------------------------------------------------------------- prompt caching

def test_caching_is_on_by_default_and_marks_the_system_block():
    """Unlike tool search, caching changes cost and not what the model sees — so it defaults ON."""
    sysblocks = ClaudeLLM("Claude", "k", "m")._system_for("PROMPT")
    assert sysblocks == [{"type": "text", "text": "PROMPT",
                          "cache_control": {"type": "ephemeral"}}]


def test_caching_can_be_turned_off_and_then_system_is_a_plain_string():
    llm = ClaudeLLM("Claude", "k", "m")
    llm.configure(_settings(prompt_cache=False))
    assert llm._system_for("PROMPT") == "PROMPT"


def test_two_breakpoints_cover_two_different_spans():
    """tools+system for the hops of one turn, and tools alone for the next turn — the system prompt
    carries a per-turn injection, so a single marker on it caches nothing across turns."""
    llm = ClaudeLLM("Claude", "k", "m")
    sent = llm._tools_for([_spec("place_asset"), _spec("query_world")])
    assert "cache_control" not in sent[0]              # only the LAST tool carries it
    assert sent[-1]["cache_control"] == {"type": "ephemeral"}
    assert llm._system_for("PROMPT")[0]["cache_control"] == {"type": "ephemeral"}


def test_no_tool_breakpoint_when_caching_is_off():
    llm = ClaudeLLM("Claude", "k", "m")
    llm.configure(_settings(prompt_cache=False))
    assert not any("cache_control" in t for t in llm._tools_for([_spec("x")]))


def test_the_tool_marker_does_not_disturb_the_tool_itself():
    """`cache_control` is an annotation; the name/description/schema the model sees must be identical."""
    llm = ClaudeLLM("Claude", "k", "m")
    on = llm._tools_for([_spec("place_asset")])[0]
    llm.configure(_settings(prompt_cache=False))
    off = llm._tools_for([_spec("place_asset")])[0]
    assert {k: v for k, v in on.items() if k != "cache_control"} == off


async def test_a_cached_turn_still_sends_the_same_tools_and_messages(monkeypatch):
    """Caching must only ANNOTATE the request — the tools and the turn's own content are unchanged,
    and the provider's usage lands in `last_usage` so a measurement can read it."""
    import anthropic

    seen = {}

    class _Msgs:
        async def create(self, **kw):
            # Copy: the loop appends the assistant turn to this very list afterwards.
            seen["messages"] = list(kw["messages"])
            seen["system"] = kw["system"]
            seen["tools"] = kw["tools"]
            seen["cache_control"] = kw.get("cache_control")
            return type("R", (), {
                "content": [_blk("text", text="hi")],
                "stop_reason": "end_turn",
                "usage": type("U", (), {"input_tokens": 5, "output_tokens": 1,
                                        "cache_read_input_tokens": 4,
                                        "cache_creation_input_tokens": 0})(),
            })()

    monkeypatch.setattr(anthropic, "AsyncAnthropic",
                        lambda **kw: type("C", (), {"messages": _Msgs()})())

    async def emit(text, *, final): pass
    async def ex(name, args): return "ok"

    llm = ClaudeLLM("Claude", "k", "m")
    await llm.run_turn(system="SYS", history=[], user_text="hello",
                       tools=[_spec("place_asset")], execute_tool=ex, emit=emit)

    assert seen["cache_control"] == {"type": "ephemeral"}          # the growing tail
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}   # tools + prompt
    assert [t["name"] for t in seen["tools"]] == ["place_asset"]
    assert seen["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert seen["messages"] == [{"role": "user", "content": "hello"}]
    assert llm.last_usage == [{"input": 5, "output": 1, "cache_read": 4, "cache_write": 0}]


# --------------------------------------------------------------------------- the other two providers

def test_the_openai_cache_key_is_stable_for_a_tool_set_and_differs_between_them():
    """Same prefix -> same backend. Keyed on the TOOL SET because that is the stable part: the system
    prompt carries a per-turn injection and the messages differ every turn."""
    llm = OpenAILLM("Chat", "k", "m")
    a = llm._cache_key([_spec("place_asset"), _spec("query_world")])
    b = llm._cache_key([_spec("query_world"), _spec("place_asset")])   # order must not matter
    c = llm._cache_key([_spec("place_asset")])
    assert a == b and a != c and a.startswith("conjure-")


def test_no_cache_key_for_a_vendor_that_only_mirrors_the_api():
    """`prompt_cache_key` is OpenAI's. xAI reuses the Chat Completions shape but need not accept the
    field, and an unknown parameter is a 400 rather than a graceful ignore."""
    assert GrokLLM("Grok", "k", "m")._cache_key([_spec("x")]) is None
    assert OpenAILLM("Chat", "k", "m")._cache_key([]) is None


async def test_the_openai_turn_sends_the_cache_key(monkeypatch):
    import openai

    seen = {}

    class _Completions:
        async def create(self, **kw):
            seen.update(kw)
            return type("R", (), {
                "choices": [type("C", (), {"message": type("M", (), {
                    "content": "hi", "tool_calls": None})()})()],
                "usage": type("U", (), {
                    "prompt_tokens": 9000, "completion_tokens": 12,
                    "prompt_tokens_details": type("D", (), {"cached_tokens": 8000})()})(),
            })()

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **kw: type(
        "C", (), {"chat": type("Ch", (), {"completions": _Completions()})()})())

    llm = OpenAILLM("Chat", "k", "m")
    async def emit(t, *, final): pass
    async def ex(n, a): return "ok"
    await llm.run_turn(system="SYS", history=[], user_text="hello",
                       tools=[_spec("place_asset")], execute_tool=ex, emit=emit)
    assert seen["prompt_cache_key"] == llm._cache_key([_spec("place_asset")])
    # `cached_tokens` is a SUBSET of `prompt_tokens` here, unlike Anthropic — it must be subtracted
    # out, or "input" means something different depending on which provider answered.
    assert llm.last_usage == [{"input": 1000, "output": 12,
                               "cache_read": 8000, "cache_write": 0}]


async def test_gemini_usage_subtracts_the_cached_subset_too(monkeypatch):
    from google import genai

    class _Models:
        async def generate_content(self, **kw):
            return type("R", (), {
                "candidates": [type("C", (), {"content": type("Ct", (), {"parts": []})()})()],
                "usage_metadata": type("U", (), {
                    "prompt_token_count": 5000, "candidates_token_count": 7,
                    "cached_content_token_count": 4000})(),
            })()

    monkeypatch.setattr(genai, "Client", lambda **kw: type(
        "C", (), {"aio": type("A", (), {"models": _Models()})()})())

    llm = GeminiLLM("Gemini", "k", "m")
    async def emit(t, *, final): pass
    async def ex(n, a): return "ok"
    await llm.run_turn(system="SYS", history=[], user_text="hi",
                       tools=[], execute_tool=ex, emit=emit)
    assert llm.last_usage == [{"input": 1000, "output": 7,
                               "cache_read": 4000, "cache_write": 0}]


# --------------------------------------------------------------------------- the local director

def test_a_local_director_is_gated_on_a_model_name_not_a_key():
    """There is no key to be missing, so naming a model IS the opt-in — and an empty one must keep
    the row out of the roster rather than registering a director pointed at nothing."""
    assert "Local" not in build_roster(_settings())[0]
    roster, _ = build_roster(_settings(local_model="qwen3:8b"))
    assert isinstance(roster["Local"], LocalLLM) and roster["Local"].model == "qwen3:8b"


def test_the_local_endpoint_is_configurable_for_a_different_runtime():
    """Ollama, LM Studio and mlx_lm.server differ by port, not by API — so switching is a URL."""
    roster, _ = build_roster(_settings(local_model="m", local_llm_url="http://localhost:1234/v1"))
    assert roster["Local"].base_url == "http://localhost:1234/v1"
    default, _ = build_roster(_settings(local_model="m"))
    assert default["Local"].base_url == "http://localhost:11434/v1"


def test_the_local_director_sends_no_cache_key():
    """`prompt_cache_key` is OpenAI's routing hint for ITS fleet. A local server has one backend and
    does longest-common-prefix reuse itself, so the field is meaningless and must not be sent."""
    assert LocalLLM("Local", "", "m")._cache_key([_spec("x")]) is None


def test_the_local_director_is_addressable_like_any_other():
    assert vendor_for("Local") == "local"
