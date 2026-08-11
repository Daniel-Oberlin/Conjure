"""Agent definitions + the MCP server registry loader (conjure.agents) — the declarative layer behind
the director. Pure file loading + validation; no network, no LLMs. The builder agent must reproduce
today's director exactly (same prompt, any LLM, the world server)."""
import json

import pytest

from conjure.agents import (WILDCARD, AgentDef, load_agent, load_server_registry, scoped_roster)


def test_registry_has_the_world_server():
    reg = load_server_registry()
    assert "world" in reg
    assert reg["world"].args == ["-m", "conjure.mcp_server"]
    assert "CONJURE_URL" in reg["world"].env


def test_builder_agent_reproduces_the_director():
    agent = load_agent("builder", registry=load_server_registry())
    assert agent.name == "builder"
    assert agent.prompt.strip() and "{name}" not in agent.prompt          # LLM-agnostic (no LLM name)
    assert "{user}" in agent.prompt                                       # owns the logged-in-user placeholder
    assert agent.llms == [WILDCARD]                                       # any configured LLM
    assert [(s.server, s.access) for s in agent.servers] == [("world", "all")]


def _write_agent(tmp_path, name, data, files=None):
    """Create a `<tmp_path>/<name>/agent.json` def (+ any extra files: {relpath: text})."""
    d = tmp_path / name
    d.mkdir()
    (d / "agent.json").write_text(json.dumps(data))
    for rel, text in (files or {}).items():
        (d / rel).write_text(text)
    return tmp_path


def test_unknown_server_ref_is_rejected(tmp_path):
    _write_agent(tmp_path, "bad", {"prompt": "hi {name}", "mcp_servers": [{"server": "nope"}]})
    with pytest.raises(ValueError, match="unknown MCP server"):
        load_agent("bad", agents_dir=tmp_path, registry=load_server_registry())


def test_missing_prompt_is_rejected(tmp_path):
    _write_agent(tmp_path, "empty", {"llms": ["Claude"]})
    with pytest.raises(ValueError, match="prompt"):
        load_agent("empty", agents_dir=tmp_path)


def test_name_field_must_match_the_directory(tmp_path):
    _write_agent(tmp_path, "x", {"name": "y", "prompt": "hi"})   # dir is the identity; a stray name errors
    with pytest.raises(ValueError, match="name"):
        load_agent("x", agents_dir=tmp_path)


def test_prompt_file_is_relative_to_the_agent_dir(tmp_path):
    _write_agent(tmp_path, "p", {"prompt_file": "prompt.md"}, files={"prompt.md": "you are {name}"})
    assert load_agent("p", agents_dir=tmp_path).prompt == "you are {name}"


def test_inline_prompt_and_defaults(tmp_path):
    _write_agent(tmp_path, "mini", {"prompt": "you are {name}"})
    a = load_agent("mini", agents_dir=tmp_path)
    assert a.prompt == "you are {name}" and a.llms == [WILDCARD] and a.servers == []


def test_builder_declares_exactly_the_world_tool_surface():
    # Builder is the full-access agent: since tool access is opt-in only (no wildcard), it must list
    # EVERY tool the world server exposes — no more, no less. Guards the enumeration footgun (a new
    # server tool silently unavailable to builder, or a stale/typo'd name) at test time, not just at
    # launch (where director._scope_tools would raise on a typo).
    import pathlib
    import re

    import conjure
    src = (pathlib.Path(conjure.__file__).parent / "mcp_server.py").read_text()
    server_tools = set(re.findall(r"@mcp\.tool\([^)]*\)\s*\nasync def (\w+)", src))
    builder_tools = set(load_agent("builder").servers[0].tools)
    assert builder_tools == server_tools, {
        "missing_from_builder": server_tools - builder_tools,
        "stale_in_builder": builder_tools - server_tools,
    }


def test_server_ref_tools_allow_list_parses(tmp_path):
    _write_agent(tmp_path, "sky", {"prompt": "hi", "mcp_servers": [
        {"server": "world", "tools": ["set_skybox", "generate_skybox_image"]}]})
    ref = load_agent("sky", agents_dir=tmp_path).servers[0]
    assert ref.tools == ["set_skybox", "generate_skybox_image"] and ref.access == "all"


def test_server_ref_tools_default_to_none_opt_in(tmp_path):
    # No wildcard, no implicit grant: omitting `tools` means the agent gets NO tools (default-deny).
    _write_agent(tmp_path, "b", {"prompt": "hi", "mcp_servers": [{"server": "world"}]})
    assert load_agent("b", agents_dir=tmp_path).servers[0].tools == []


def test_scoped_roster_wildcard_and_explicit():
    roster = {"Claude": object(), "Gemini": object()}
    assert scoped_roster(AgentDef(name="any", llms=[WILDCARD]), roster) == roster
    only = scoped_roster(AgentDef(name="g", llms=["Gemini"]), roster)
    assert list(only) == ["Gemini"]


def test_allows_llm():
    assert AgentDef(name="a", llms=[WILDCARD]).allows_llm("anything")
    assert AgentDef(name="b", llms=["Claude"]).allows_llm("Claude")
    assert not AgentDef(name="c", llms=["Claude"]).allows_llm("Gemini")
