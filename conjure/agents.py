"""Agent definitions + the MCP server registry — the declarative layer behind the director.

An *agent* is an experience (see docs/agents.md): a prompt, the LLMs allowed to run it, the MCP
servers (toolset) it may use, the context to inject, and any personas it hosts. Each agent is a
self-contained directory — `agents/<name>/agent.json` plus its `prompt.md` (and later its personas)
— validated against the shared `agents/servers.json` registry. The directory name *is* the agent's
identity. This module just **loads and validates** those defs; the runtime wiring (launching servers,
building the roster) lives in director.py.

v1 scope: the data model + loader, with the `builder` agent reproducing today's director. Scoping
*enforcement* (read-only access, tool filtering), multi-server launch, personas, and context
injection are later slices — their fields are carried here but not yet acted on.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent          # repo root (parent of the conjure package)
AGENTS_DIR = _ROOT / "agents"
DEFAULT_REGISTRY = AGENTS_DIR / "servers.json"

WILDCARD = "*"      # "any configured LLM" / "any registered server" — the explicit "god" escape hatch


@dataclass
class ServerSpec:
    """A registry entry: how to launch/connect one MCP server."""
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    transport: str = "stdio"


@dataclass
class ServerRef:
    """An agent's reference to a registered server, with its access level."""
    server: str
    access: str = "all"        # "all" | "read"  (read-only enforcement is a later slice)


@dataclass
class AgentDef:
    name: str
    description: str = ""
    prompt: str = ""           # resolved prompt text (from `prompt` or `prompt_file`)
    llms: list[str] = field(default_factory=lambda: [WILDCARD])   # allow-list, or [WILDCARD] = any
    default_llm: Optional[str] = None
    servers: list[ServerRef] = field(default_factory=list)
    context: list[str] = field(default_factory=list)    # MCP resources to inject (later slice)
    personas: list[str] = field(default_factory=list)   # persona refs (later slice)

    def allows_any_llm(self) -> bool:
        return WILDCARD in self.llms

    def allows_llm(self, name: str) -> bool:
        return self.allows_any_llm() or name in self.llms


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(f"not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e


def load_server_registry(path: Path = DEFAULT_REGISTRY) -> dict[str, ServerSpec]:
    """Load the MCP server registry (name → how to launch it)."""
    raw = _read_json(path)
    return {
        name: ServerSpec(name=name, command=spec["command"], args=list(spec.get("args", [])),
                         env=dict(spec.get("env", {})), transport=spec.get("transport", "stdio"))
        for name, spec in raw.items()
    }


def load_agent(name: str, *, agents_dir: Path = AGENTS_DIR,
               registry: Optional[dict[str, ServerSpec]] = None) -> AgentDef:
    """Load and validate the agent definition `<agents_dir>/<name>/agent.json`.

    The directory name is the agent's identity, so `agent.json` needn't repeat it (a `name` field, if
    present, is validated to match). `prompt_file` is resolved relative to the agent's directory.
    `registry` (if given) validates that every referenced MCP server exists — pass it to fail loudly
    on a typo'd server name. Raises ValueError on a malformed def, FileNotFoundError on a missing file.
    """
    agent_dir = agents_dir / name
    data = _read_json(agent_dir / "agent.json")
    if data.get("name", name) != name:
        raise ValueError(f"agent {name!r}: 'name' field is {data.get('name')!r}, expected {name!r}")

    prompt = data.get("prompt") or ""
    if not prompt and data.get("prompt_file"):
        prompt = (agent_dir / data["prompt_file"]).read_text()
    if not prompt.strip():
        raise ValueError(f"agent {name!r}: needs a non-empty 'prompt' or 'prompt_file'")

    servers: list[ServerRef] = []
    for s in data.get("mcp_servers", []):
        ref = ServerRef(server=s["server"], access=s.get("access", "all")) if isinstance(s, dict) \
            else ServerRef(server=str(s))
        servers.append(ref)
    if registry is not None:
        for ref in servers:
            if ref.server != WILDCARD and ref.server not in registry:
                raise ValueError(
                    f"agent {name!r}: references unknown MCP server {ref.server!r} "
                    f"(not in the registry: {sorted(registry)})")

    return AgentDef(
        name=name, description=data.get("description", ""), prompt=prompt,
        llms=list(data.get("llms", [WILDCARD])), default_llm=data.get("default_llm"),
        servers=servers, context=list(data.get("context", [])),
        personas=list(data.get("personas", [])),
    )


def scoped_roster(agent: AgentDef, roster: dict) -> dict:
    """The subset of an LLM roster this agent is allowed to run on (order preserved)."""
    if agent.allows_any_llm():
        return dict(roster)
    return {name: llm for name, llm in roster.items() if name in agent.llms}
