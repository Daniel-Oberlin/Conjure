"""Agent definitions + the MCP server registry — the declarative layer behind the director.

An *agent* is an experience (see docs/specs/agents.md §3): a prompt, the LLMs allowed to run it, the
MCP servers (toolset) it may use, the context to inject, the dynamic modules it may conjure. Each
agent is a self-contained directory — `agents/<name>/agent.json` plus its `prompt.md` — validated
against the shared `agents/servers.json` registry. The directory name *is* the agent's identity. This
module just **loads and validates** those defs; the runtime wiring (launching servers, building the
roster) lives in director.py.

Everything here is acted on somewhere — tool scoping in `director` + `mcp_server._GatedMCP` (§4),
context injection in `director` (§5.3), `dynamics` in the world server (specs/dynamics.md §9),
`session`/`world`/`state` in the constructor (§7.5) — with two exceptions carried but unused:
`personas`, and more than one `mcp_servers` entry. Both are in docs/backlogs/agents.md.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config

_ROOT = Path(__file__).resolve().parent.parent          # repo root (parent of the conjure package)
AGENTS_DIR = config.BUNDLED_AGENTS_DIR                  # the bundled/example agent defs (repo `agents/`)
DEFAULT_REGISTRY = AGENTS_DIR / "servers.json"          # shared MCP registry — bundled (user overlay: later)

WILDCARD = "*"      # "any configured LLM" / "any registered server" — the explicit "god" escape hatch


def _search_path(agents_path: Optional[list[Path]] = None) -> list[Path]:
    """The agent-definition search path — the explicit arg, else the resolved `config.AGENTS_PATH`
    (read live so tests can monkeypatch it). User dirs first, bundled last (docs/user-home-plan.md §5)."""
    return agents_path if agents_path is not None else config.AGENTS_PATH


def resolve_agent_dir(name: str, agents_path: Optional[list[Path]] = None) -> Path:
    """First `<dir>/<name>/agent.json` in the search path — so a user agent shadows a bundled one of the
    same name. Raises FileNotFoundError if the name is nowhere on the path."""
    for base in _search_path(agents_path):
        if (base / name / "agent.json").exists():
            return base / name
    tried = [str(p) for p in _search_path(agents_path)]
    raise FileNotFoundError(f"agent {name!r} not found in search path: {tried}")


def list_agents(agents_path: Optional[list[Path]] = None) -> list[tuple[str, str]]:
    """Sorted unique `(name, source)` across the search path, first-match-wins (so a shadowing user
    agent hides the bundled one). `source` is 'bundled' for the bundled dir, else 'user'."""
    seen: dict[str, str] = {}
    for base in _search_path(agents_path):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if p.name not in seen and (p / "agent.json").exists():
                seen[p.name] = "bundled" if base == config.BUNDLED_AGENTS_DIR else "user"
    return sorted(seen.items())


def agent_names(agents_path: Optional[list[Path]] = None) -> list[str]:
    """Just the names from `list_agents` (the common case)."""
    return [n for n, _ in list_agents(agents_path)]


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
    """An agent's reference to a registered server, with its access level and tool allow-list."""
    server: str
    access: str = "all"        # "all" | "read"  (read-only enforced by mcp_server._GatedMCP, §3c)
    # Tool allow-list — **opt-in only, no wildcard**: an agent gets exactly the tools it names, and the
    # default (omitted) is NONE (default-deny), so every tool access is explicit and intentional.
    # Enforced two ways (docs/specs/agents.md §4): client-side by filtering the offered tools
    # (director._scope_tools) + a runtime re-check, AND a hard gate in mcp_server._GatedMCP (a separate
    # process from the LLM) that refuses a disallowed tool before any world-server call.
    tools: list[str] = field(default_factory=list)


@dataclass
class AgentDef:
    name: str
    description: str = ""
    prompt: str = ""           # resolved prompt text (from `prompt` or `prompt_file`)
    llms: list[str] = field(default_factory=lambda: [WILDCARD])   # allow-list, or [WILDCARD] = any
    default_llm: Optional[str] = None
    servers: list[ServerRef] = field(default_factory=list)
    context: list[str] = field(default_factory=list)    # MCP resources injected each turn (§5.3)
    dynamics: list[str] = field(default_factory=list)   # dynamic modules this agent may conjure — a
    #                                                     required allow-list (docs/specs/dynamics.md §9):
    #                                                     scopes conjure_module + drives the director catalog.
    personas: list[str] = field(default_factory=list)   # persona refs — parsed, read by NOTHING yet
    #                                                     (docs/backlogs/agents.md)
    session: dict = field(default_factory=dict)         # session constructor block (greeting, first_world,
                                                        # state seed) — docs/specs/agents.md §7.5
    state: dict = field(default_factory=dict)           # agent-owned state declaration: {doc: {seed, schema,
                                                        # inject}} — drives the generic state_* tools (§5)

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


def load_agent(name: str, *, agents_dir: Optional[Path] = None,
               agents_path: Optional[list[Path]] = None,
               registry: Optional[dict[str, ServerSpec]] = None,
               dynamics_path: Optional[list[Path]] = None) -> AgentDef:
    """Load and validate an agent definition.

    Resolution: an explicit `agents_dir` pins a single directory (`<agents_dir>/<name>`; used by tests);
    otherwise the name is looked up on the search path (`agents_path`, else `config.AGENTS_PATH`), so a
    user agent shadows a bundled one (docs/user-home-plan.md §5).

    The directory name is the agent's identity, so `agent.json` needn't repeat it (a `name` field, if
    present, is validated to match). `prompt_file` is resolved relative to the agent's directory.
    `registry` (if given) validates that every referenced MCP server exists — pass it to fail loudly
    on a typo'd server name. `dynamics` are validated against the dynamics search path (`dynamics_path`,
    else `config.DYNAMICS_PATH`): every listed module must resolve or the agent fails to load. Raises
    ValueError on a malformed def, FileNotFoundError on a missing file.
    """
    agent_dir = (agents_dir / name) if agents_dir is not None else resolve_agent_dir(name, agents_path)
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
        ref = ServerRef(server=s["server"], access=s.get("access", "all"),
                        tools=list(s.get("tools", []))) if isinstance(s, dict) \
            else ServerRef(server=str(s))
        servers.append(ref)
    if registry is not None:
        for ref in servers:
            if ref.server != WILDCARD and ref.server not in registry:
                raise ValueError(
                    f"agent {name!r}: references unknown MCP server {ref.server!r} "
                    f"(not in the registry: {sorted(registry)})")

    # Dynamic modules the agent may conjure — a REQUIRED allow-list (docs/specs/dynamics.md §9
    # §agent scoping): every listed module MUST resolve on the dynamics search path, or the agent fails to
    # load (fail loud on a dangling reference, like an unknown MCP server). Imported lazily so the agents
    # loader stays usable without the dynamics package present.
    dynamics = list(data.get("dynamics", []))
    if dynamics:
        from . import dynamics as _dyn
        for mod in dynamics:
            try:
                _dyn.resolve_module_dir(mod, dynamics_path)
            except FileNotFoundError as e:
                raise ValueError(f"agent {name!r}: references unknown dynamic module {mod!r} ({e})") from e

    # State declaration (docs/specs/agents.md §7.4): resolve each doc's `seed` file (relative to the agent
    # dir, like `prompt_file`) to its parsed JSON under `seed_data`, so the constructor can copy it into a
    # new session without any file I/O at runtime. A missing/bad seed is left unresolved (skipped, not fatal).
    state = dict(data.get("state") or {})
    for doc, spec in state.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("seed"):
            try:
                spec["seed_data"] = json.loads((agent_dir / spec["seed"]).read_text())
            except (OSError, ValueError):
                pass
        if spec.get("schema"):                          # resolve the JSON Schema file (validation, §5.3)
            try:
                spec["schema_data"] = json.loads((agent_dir / spec["schema"]).read_text())
            except (OSError, ValueError):
                pass

    return AgentDef(
        name=name, description=data.get("description", ""), prompt=prompt,
        llms=list(data.get("llms", [WILDCARD])), default_llm=data.get("default_llm"),
        servers=servers, context=list(data.get("context", [])), dynamics=dynamics,
        personas=list(data.get("personas", [])), session=dict(data.get("session") or {}),
        state=state,
    )


def scoped_roster(agent: AgentDef, roster: dict) -> dict:
    """The subset of an LLM roster this agent is allowed to run on (order preserved)."""
    if agent.allows_any_llm():
        return dict(roster)
    return {name: llm for name, llm in roster.items() if name in agent.llms}
