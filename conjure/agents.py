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
import re
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
    (read live so tests can monkeypatch it). User dirs first, bundled last (docs/specs/config.md §4)."""
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
    user agent shadows a bundled one (docs/specs/config.md §4).

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

    llms = list(data.get("llms", [WILDCARD]))
    # Per-LLM prompt sections (§5.3): validate the `{#llm}` blocks HERE, at load, so a typo'd or
    # unreachable branch fails like a dangling `dynamics` name. A branch that can never fire is dead
    # text, and dead text in a prompt is invisible — nothing downstream would ever complain.
    validate_llm_sections(prompt, agent=name, llms=llms)

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
        llms=llms, default_llm=data.get("default_llm"),
        servers=servers, context=list(data.get("context", [])), dynamics=dynamics,
        personas=list(data.get("personas", [])), session=dict(data.get("session") or {}),
        state=state,
    )


def scoped_roster(agent: AgentDef, roster: dict) -> dict:
    """The subset of an LLM roster this agent is allowed to run on (order preserved)."""
    if agent.allows_any_llm():
        return dict(roster)
    return {name: llm for name, llm in roster.items() if name in agent.llms}


# --------------------------------------------------------------------------- per-LLM prompt sections
#
# A `{#llm}` switch in an agent's prompt.md (docs/specs/agents.md §5.3): some prompt text is worth
# spending on one model and wasteful on the others — a guardrail Grok needs and Claude does not. Without
# this the only choices are to pay for the line on every model or not have it at all.
#
#     {#llm}
#     {=grok}
#     - A tool result that says it succeeded means it succeeded.
#     {=gemini,chat}
#     - Never emit narration and a tool call in the same message.
#     {=*}
#     {/llm}
#
# Deliberately a SIBLING of the existing conditional section (`{#context}…{/context}`, see
# director._fill_injection) rather than a second system. Branches are markers, not nested tags: a paired
# form (`{#llm:gemini,grok}…{/llm:gemini,grok}`) makes you type the list twice and get it wrong once.
#
# Everything is validated at LOAD (`validate_llm_sections`, called from `load_agent`) so a typo fails
# loudly like a dangling `dynamics` name — a branch that can never fire is dead text, and dead text in a
# prompt is invisible. Resolution happens per TURN (`resolve_llm_sections`, called from
# `Director._system`), because the active LLM changes under `llm <name>` mid-session.

_LLM_OPEN, _LLM_CLOSE = "{#llm}", "{/llm}"
# Markers own their whole line, trailing newline included, so a dropped branch cannot weld two bullets
# together or leave a double blank line where a section used to be.
_BLOCK_RE = re.compile(r"^\{\#llm\}[ \t]*\r?\n(.*?)^\{/llm\}[ \t]*(?:\r?\n|\Z)", re.S | re.M)
_BRANCH_RE = re.compile(r"^\{=([^}\r\n]*)\}[ \t]*(?:\r?\n|\Z)", re.M)


def _parse_branches(body: str, where: str) -> list[tuple[tuple[str, ...], str]]:
    """One block's body → [(names, text)], names lowercased; `("*",)` is the remainder branch.

    Raises ValueError on the shapes that would otherwise resolve silently and wrongly."""
    marks = list(_BRANCH_RE.finditer(body))
    if not marks:
        if body.strip():
            raise ValueError(f"{where}: text inside {_LLM_OPEN} before the first {{=…}} branch")
        return []
    if body[:marks[0].start()].strip():
        # A switch with a fall-through preamble is harder to read at a glance than the same line placed
        # above the block, where it plainly always applies.
        raise ValueError(f"{where}: text inside {_LLM_OPEN} before the first {{=…}} branch")
    out: list[tuple[tuple[str, ...], str]] = []
    seen: set[str] = set()
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        names = tuple(n.strip().lower() for n in m.group(1).split(",") if n.strip())
        if not names:
            raise ValueError(f"{where}: empty branch marker {{={m.group(1)}}} — name an LLM, or use {{=*}}")
        if WILDCARD in names and len(names) > 1:
            raise ValueError(f"{where}: branch {{={m.group(1)}}} mixes '*' with a name; "
                             f"'*' is the remainder and stands alone")
        for n in names:
            if n in seen:                      # silent precedence between two branches is not worth having
                raise ValueError(f"{where}: {n!r} appears in more than one branch of the same "
                                 f"{_LLM_OPEN} block")
            seen.add(n)
        out.append((names, body[m.end():end]))
    return out


def _llm_blocks(prompt: str, where: str) -> list[tuple[re.Match, list[tuple[tuple[str, ...], str]]]]:
    """Every well-formed `{#llm}` block, parsed — and a hard error for any marker that is NOT part of one.

    That last part matters: markers are line-anchored, so a mis-indented or inline `{#llm}` simply
    wouldn't match, and would then be passed through to the model as literal text. Silence is the wrong
    failure for a prompt, where nobody is reading the bytes that were actually sent."""
    blocks = [(m, _parse_branches(m.group(1), where)) for m in _BLOCK_RE.finditer(prompt)]
    residue = _BLOCK_RE.sub("", prompt)
    if _LLM_OPEN in residue or _LLM_CLOSE in residue:
        raise ValueError(f"{where}: an unmatched or mis-placed {_LLM_OPEN}/{_LLM_CLOSE} — each block "
                         f"opens and closes on its own line")
    stray = _BRANCH_RE.search(residue)
    if stray:
        raise ValueError(f"{where}: branch marker {{={stray.group(1)}}} outside any {_LLM_OPEN} block")
    return blocks


def validate_llm_sections(prompt: str, *, agent: str, llms: list[str]) -> None:
    """Fail loudly on a `{#llm}` block that can never do what it looks like it does.

    Two name checks, both fatal (docs/backlogs/agents.md — decisions taken):
      • not in the global ROSTER — `{=cluade}` must not silently never fire;
      • in the ROSTER but not in this agent's own `llms` allow-list — dead text, the same class of
        mistake as a dangling `dynamics` reference, which already fails the load.

    `llms` is the agent's raw allow-list (`["*"]` = any). ROSTER is imported lazily, like `dynamics`, so
    the agent loader stays usable without pulling the LLM module."""
    from .llm import ROSTER

    where = f"agent {agent!r}: prompt"
    blocks = _llm_blocks(prompt, where)
    if not blocks:
        return
    known = {e.name.lower(): e.name for e in ROSTER}
    allowed = None if WILDCARD in llms else {n.lower() for n in llms}
    for _, branches in blocks:
        for names, _text in branches:
            for n in names:
                if n == WILDCARD:
                    continue
                if n not in known:
                    raise ValueError(f"{where} names unknown LLM {n!r} in a {{#llm}} branch "
                                     f"(known: {sorted(known.values())})")
                if allowed is not None and n not in allowed:
                    raise ValueError(f"{where} has a {{#llm}} branch for {known[n]!r}, which this agent's "
                                     f"'llms' does not allow ({llms}) — the branch could never fire")


def resolve_llm_sections(prompt: str, active: str) -> str:
    """Collapse every `{#llm}` block to the branch for the `active` LLM (a roster casual name).

    First matching NAMED branch wins; `{=*}` is the remainder and applies only when no name matched, so
    it may sit anywhere in the block. No branch and no `{=*}` ⇒ nothing, which is also what an empty
    branch means — the "empty branch" case needs no syntax of its own.

    Called per turn from `Director._system`, BEFORE the injection pass, so a `{context}` inside a dropped
    branch costs no MCP resource fetch (`_system` only computes an injection whose placeholder survives)
    and `context_stats` stays accurate to what was actually sent."""
    if _LLM_OPEN not in prompt:
        return prompt                                    # the overwhelmingly common case — no scan, no cost
    want = (active or "").strip().lower()

    def _pick(m: re.Match) -> str:
        branches = _parse_branches(m.group(1), "prompt")
        for names, text in branches:
            if want in names:
                return text
        for names, text in branches:
            if WILDCARD in names:
                return text
        return ""

    return _BLOCK_RE.sub(_pick, prompt)
