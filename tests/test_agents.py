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
    assert agent.prompt.strip() and agent.prompt.count("{name}") == 1     # the real director prompt
    assert agent.llms == [WILDCARD]                                       # any configured LLM
    assert [(s.server, s.access) for s in agent.servers] == [("world", "all")]


def test_builder_prompt_is_the_single_source_for_DIRECTOR_PROMPT():
    from conjure.director import DIRECTOR_PROMPT
    assert load_agent("builder").prompt == DIRECTOR_PROMPT               # one file, no divergence


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


def test_scoped_roster_wildcard_and_explicit():
    roster = {"Claude": object(), "Gemini": object()}
    assert scoped_roster(AgentDef(name="any", llms=[WILDCARD]), roster) == roster
    only = scoped_roster(AgentDef(name="g", llms=["Gemini"]), roster)
    assert list(only) == ["Gemini"]


def test_allows_llm():
    assert AgentDef(name="a", llms=[WILDCARD]).allows_llm("anything")
    assert AgentDef(name="b", llms=["Claude"]).allows_llm("Claude")
    assert not AgentDef(name="c", llms=["Claude"]).allows_llm("Gemini")
