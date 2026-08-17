"""The agent runtime — today's **builder** agent, the shared brain for voice and CLI.

The director loads as the `builder` agent — a declarative def in `agents/builder/` (via conjure.agents):
its prompt, the LLMs it's allowed to run on, the MCP servers it's scoped to, and the context it injects.
A deterministic **shell** (conjure.shell) wraps it — control commands run there; anything else is
forwarded here. Both front-ends (voice.py, cli.py) drive shell → agent; they differ only in how text
arrives (mic vs typing) and leaves (TTS vs print):

    async with Director.connect(settings, agent="builder") as director:
        await director.handle("put a tree in front of me", on_text=..., on_tool=...)

The agent owns:
  • the **shared transcript** — a single user/assistant conversation log (architecture §7a); it
    carries no record of which LLM authored a reply, so switching LLMs is invisible in the history,
  • the **LLM roster** (conjure.llm) — the named LLMs it allows, one *active* at a time,
  • the world-editing **MCP tools** (it is an MCP client of its scoped servers over stdio),
  • the per-turn **context** it injects (e.g. `room://current` — the live room, agents.md §5).

Every turn runs on the **active** LLM. Switching the active LLM is the shell's job — deterministic,
parsed there (conjure.shell), never inferred from the utterance here.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import sys
from typing import Awaitable, Callable, Optional

from .agents import AgentDef, ServerSpec, load_agent, load_server_registry, scoped_roster
from .config import DEFAULT_USER, Settings, scope_for
from .llm import LLM, ToolSpec, Turn, build_roster

# The director runtime is agent-agnostic — it knows nothing about any particular agent. A real agent
# always supplies its own prompt via its def (`Director.connect` passes `agentdef.prompt`). This
# generic fallback is only for a bare `Director()` in examples/tests.
_DEFAULT_PROMPT = "You are the director of a Conjure session. Use the tools to carry out requests."

OnText = Callable[..., Awaitable[None]]   # (text, *, final: bool, speaker: str) -> None
OnTool = Callable[..., Awaitable[None]]   # (name: str, args: dict) -> None


class Busy(RuntimeError):
    """A turn was submitted while another is already in flight. The Director holds a **single floor**
    (agent-server-plan D4, reject-while-busy): among people co-located in one room, concurrent speech
    is an edge case and they naturally take turns, so we reject rather than queue or interleave. The
    agent server (later slice) turns this into a `busy` event; in-process callers can catch it."""


def _fill_injection(prompt: str, name: str, value: str) -> str:
    """Fill one prompt-injection placeholder, so an agent's prompt.md owns *all* its own text —
    including the framing around an injected value:

      • ``{name}``                       → replaced by ``value`` (bare substitution; e.g. ``{user}``)
      • ``{#name}…{name}…{/name}``       → a **conditional section**: the inner block (with ``{name}``
                                           substituted) is kept only when ``value`` is non-blank, and
                                           dropped **entirely** otherwise — so header/label text framing
                                           the value vanishes with it (no dangling `--- … ---` when the
                                           room is empty).

    Only touches this exact `name` (registered injections), so stray braces elsewhere in the prompt
    (JSON/SQL examples) are never disturbed."""
    token = "{" + name + "}"
    keep = bool(value.strip())

    def _section(m: "re.Match") -> str:
        return m.group(1).replace(token, value) if keep else ""

    prompt = re.sub(r"\{#" + re.escape(name) + r"\}(.*?)\{/" + re.escape(name) + r"\}",
                    _section, prompt, flags=re.S)
    return prompt.replace(token, value)


# --------------------------------------------------------------------------- the director

def _stdio_params(spec: ServerSpec, settings: Settings, agent: str = "builder", user: str = DEFAULT_USER,
                  *, tools: Optional[list[str]] = None, access: str = "all"):
    """Build stdio launch params from a registry ServerSpec: map a bare 'python' to this interpreter,
    substitute ${world_url} in the env, and inject the agent's **capabilities** as env (never LLM args):
    the (user, agent) catalog SCOPE, plus the tool allow-list + access level. The MCP server enforces
    SCOPE today; CONJURE_TOOLS/CONJURE_ACCESS are scaffolded for the later server-side gate
    (docs/agent-separation-plan.md §3c) — the client-side filter is what scopes tools for now."""
    from mcp import StdioServerParameters
    command = sys.executable if spec.command in ("python", "python3") else spec.command
    env = {**os.environ, **{k: v.replace("${world_url}", settings.world_url) for k, v in spec.env.items()}}
    env["CONJURE_SCOPE"] = scope_for(user, agent)
    env["CONJURE_ACCESS"] = access
    env["CONJURE_TOOLS"] = ",".join(tools or [])     # explicit allow-list (empty = no tools)
    return StdioServerParameters(command=command, args=list(spec.args), env=env)


def _scope_tools(live, allow: list[str]):
    """Filter the live MCP tools to an agent's explicit allow-list (`ServerRef.tools`). Tool access is
    **opt-in only** — there is no wildcard: an agent gets exactly the tools it names, and an empty list
    means none (default-deny). Raises if the allow-list names a tool the server doesn't expose — a typo
    should fail loudly, not silently under-grant. Enforcement-by-omission: the LLM is only ever
    *offered* the tools that survive this filter (a hard server-side gate is a later slice)."""
    names = {t.name for t in live}
    unknown = sorted(t for t in allow if t not in names)
    if unknown:
        raise RuntimeError(f"agent tool allow-list references unknown tool(s) {unknown}; "
                           f"server exposes {sorted(names)}")
    return [t for t in live if t.name in allow]


def _pick_active(agentdef: AgentDef, roster: dict, default_active: str) -> str:
    """The LLM a session starts on (docs/sessions-plan.md §2). The agent's `llms` list doubles as a
    PRIORITY list: the first entry that's actually available wins. `scoped_roster` filters by global
    ROSTER order, so we consult `agentdef.llms` directly to honor the *agent's* ordering. A wildcard
    (`["*"]`) or no match falls back to the agent's `default_llm`, then settings.llm (`default_active`),
    then whatever's first available. (A session's *remembered* LLM overrides this on load — restored by
    the agent server.)"""
    return (next((n for n in agentdef.llms if n in roster), None)
            or (agentdef.default_llm if agentdef.default_llm in roster else None)
            or (default_active if default_active in roster else None)
            or next(iter(roster)))


# The generic, agent-agnostic agent-state tools (docs/sessions-plan.md §5.1) — director-HOSTED (decision A):
# offered to the LLM only when the agent declares a `state` block, and dispatched in-process against the
# live session's StateStore (never over the world MCP server). CRUD over named JSON docs by dotted path;
# no domain schema here (that's the agent's own data).
_STATE_TOOL_SPECS = [
    ToolSpec("state_get", "Read an agent-state document, or a dotted path within it (e.g. doc='map', "
             "path='nodes.throne-room'). Returns JSON.",
             {"type": "object", "properties": {"doc": {"type": "string"}, "path": {"type": "string"}},
              "required": ["doc"]}),
    ToolSpec("state_set", "Set a value at a dotted path in an agent-state document (creates it if absent).",
             {"type": "object", "properties": {"doc": {"type": "string"}, "path": {"type": "string"},
                                               "value": {}}, "required": ["doc", "path", "value"]}),
    ToolSpec("state_merge", "Shallow-merge an object into an agent-state document.",
             {"type": "object", "properties": {"doc": {"type": "string"}, "value": {"type": "object"}},
              "required": ["doc", "value"]}),
    ToolSpec("state_delete", "Delete an agent-state document, or a dotted path within it.",
             {"type": "object", "properties": {"doc": {"type": "string"}, "path": {"type": "string"}},
              "required": ["doc"]}),
    ToolSpec("state_list", "List the agent-state document names.",
             {"type": "object", "properties": {}}),
    ToolSpec("state_schema", "Return the declared shape (schema/seed/inject) for an agent-state document.",
             {"type": "object", "properties": {"doc": {"type": "string"}}, "required": ["doc"]}),
]


class Director:
    def __init__(self, settings: Settings, session, roster: dict[str, LLM], active: str,
                 tools: Optional[list[ToolSpec]] = None, prompt: str = _DEFAULT_PROMPT,
                 agent: Optional[AgentDef] = None, user: str = DEFAULT_USER,
                 allowed_tools: Optional[set[str]] = None, state_defs: Optional[dict] = None):
        self._settings = settings
        self._session = session          # MCP ClientSession (or a stand-in in tests)
        self.roster = roster
        self.active = active
        self._tools = list(tools or [])
        self._allowed_tools = allowed_tools  # the agent's explicit tool allow-list (names); None only for
                                             # a bare Director()/tests (no scoping). `connect` always sets it.
        self._prompt = prompt
        self.agent = agent               # the loaded agent def (None in lightweight tests)
        self.user = user                 # the default speaker — the logged-in user a lone client acts as.
                                         # With a shared agent server the speaker varies per turn (D5), so
                                         # `handle(..., speaker=)` overrides it; `self.user` is the fallback.
        self._speaker = user             # WHO owns the turn currently in flight (set per turn in `handle`);
                                         # the `{user}` injection and ownership resolve from this.
        self._busy = False               # the single floor (D4); guarded in `handle`, see `Busy`.
        self._identity_aware = False     # set by `connect`: tell the MCP server WHO speaks each turn (Step 3).
                                         # Off for hand-built/test Directors (no set_caller call → clean tests).
        self.transcript: list[Turn] = []
        # Director-HOSTED tools (docs/sessions-plan.md §5, decision A): tools the agent server offers the
        # LLM in-process, dispatched here instead of over the world MCP server. The first is the generic
        # `state_*` store; the seam (`_local_tools` + the `_execute_tool` short-circuit) is reusable for more.
        self.state_defs = dict(state_defs or {})   # agent-owned state declaration ({doc: {seed, schema, inject}})
        self._state = None                         # a StateStore bound by the agent server per live session
        self._local_tools: set[str] = set()
        if self.state_defs:                        # agent opted into state → offer + allow the state_* tools
            self._tools += _STATE_TOOL_SPECS
            self._local_tools = {t.name for t in _STATE_TOOL_SPECS}
            if self._allowed_tools is not None:
                self._allowed_tools = self._allowed_tools | self._local_tools

    def bind_state(self, store) -> None:
        """Point the `state_*` tools + `{…}` state injections at a session's `StateStore` (set by the agent
        server whenever the live session changes). Until bound, state tools report no session."""
        self._state = store

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(cls, settings: Settings, *, agent: str = "builder", user: str = DEFAULT_USER,
                      errlog=None):
        """Load the `agent` definition, open its MCP server(s) over stdio, and build the (scoped)
        roster. Yields a ready Director driving that agent.

        Raises RuntimeError if no LLM keys are configured / none the agent allows are available.
        v1 launches exactly one MCP server (the builder's `world`); multi-server launch is a later
        slice."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        registry = load_server_registry()
        agentdef = load_agent(agent, registry=registry)

        full_roster, default_active = build_roster(settings)
        roster = scoped_roster(agentdef, full_roster)
        if not roster:
            raise RuntimeError(
                f"No LLMs available for agent {agent!r} — set ANTHROPIC_API_KEY and/or GOOGLE_API_KEY "
                f"in .env (the agent allows: {agentdef.llms}).")
        active = _pick_active(agentdef, roster, default_active)

        refs = [r for r in agentdef.servers if r.server in registry]
        if len(refs) != 1:
            raise RuntimeError(
                f"agent {agent!r}: v1 launches exactly one MCP server (got {len(refs)}: "
                f"{[r.server for r in refs]}).")
        ref = refs[0]
        params = _stdio_params(registry[ref.server], settings, agent, user,
                               tools=ref.tools, access=ref.access)

        close_errlog = None
        if errlog is None:
            errlog = close_errlog = open(os.devnull, "w")
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    # Scope the offered tools to the agent's explicit allow-list (fails loud on a typo).
                    scoped = _scope_tools((await session.list_tools()).tools, ref.tools)
                    tools = [ToolSpec(t.name, t.description or "", t.inputSchema) for t in scoped]
                    director = cls(settings, session, roster, active, tools, prompt=agentdef.prompt,
                                   agent=agentdef, user=user, allowed_tools={t.name for t in scoped},
                                   state_defs=agentdef.state)
                    director._identity_aware = True   # real MCP session → set the per-turn speaker (Step 3)
                    yield director
        finally:
            if close_errlog is not None:
                close_errlog.close()

    def _injections(self):
        """The prompt-injection registry: placeholder name → provider producing its value. A provider
        may be sync or async; it's invoked ONLY when its placeholder appears in the prompt, so an agent
        pays only for what it references (e.g. `{context}` triggers no MCP resource fetch unless the
        prompt uses it — many agents won't care about room surfaces). Add a row to add an injection."""
        rows = [
            ("user", lambda: self._speaker),      # WHO is speaking this turn (human identity; per-turn)
            ("context", self._fetch_context),     # live MCP context resources (async; agents.md §5)
        ]
        # Agent-state docs declaring an `inject` placeholder (docs/sessions-plan.md §5.3): wire each into
        # the prompt as JSON, read from the live session's StateStore — the SAME mechanism as {user}.
        for docname, spec in self.state_defs.items():
            placeholder = ((spec or {}).get("inject") or "").strip().strip("{}").strip()
            if placeholder:
                rows.append((placeholder, lambda dn=docname: json.dumps(self._state.read(dn))
                             if self._state is not None else ""))
        return tuple(rows)

    async def _system(self) -> str:
        # Agent-agnostic: the whole system prompt — including how/where each injection is *framed* —
        # is the agent's own prompt.md. The runtime only fills the placeholders it declares (see
        # `_fill_injection` for the `{name}` / `{#name}…{/name}` forms), and only computes a value when
        # its placeholder actually appears, so nothing agent-specific lives here.
        prompt = self._prompt
        for name, provider in self._injections():
            if "{" + name + "}" not in prompt and "{#" + name + "}" not in prompt:
                continue                          # not referenced → don't even compute it
            value = provider()
            if inspect.isawaitable(value):
                value = await value
            prompt = _fill_injection(prompt, name, value or "")
        return prompt

    async def _log(self, tag: str, msg: str) -> None:
        """Best-effort diagnostic line → the world server's /client_log (same temp/conjure.log + console
        as client logs, single writer, gated by debug_log). Captures the conversation so director
        behaviour — which tools it calls, with what args, and what they return — is reviewable later.
        Never raises into the turn."""
        if not getattr(self._settings, "debug_log", False):   # settings None in lightweight tests
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                await client.post(f"{self._settings.world_url}/client_log",
                                  json={"tag": tag, "msg": msg})
        except Exception:
            pass

    async def _execute_tool(self, name: str, args: dict, on_tool: Optional[OnTool], who: str) -> str:
        if self._allowed_tools is not None and name not in self._allowed_tools:
            # Defense-in-depth beyond offering-only: a call outside the agent's tool scope (e.g. from a
            # future programmatic/persona path, not the LLM's offered set) is refused, not executed.
            await self._log(f"{who}/tool", f"BLOCKED {name} (out of agent tool scope)")
            return f"error: tool {name!r} is not available to this agent"
        if on_tool:
            await on_tool(name, args)
        await self._log(f"{who}/tool", f"{name}({json.dumps(args, default=str)[:600]})")
        if name in self._local_tools:                 # director-hosted tool → dispatch in-process (§5, A)
            text = await self._state_tool(name, args)
            await self._log(f"{who}/tool", f"  -> {text[:2000]}")
            return text
        out = await self._session.call_tool(name, args)
        text = "".join(getattr(c, "text", "") for c in out.content)
        await self._log(f"{who}/tool", f"  -> {text[:2000]}")   # roomy enough to see a full query_world
        return text

    async def _state_tool(self, name: str, args: dict) -> str:
        """Dispatch a generic `state_*` tool against the live session's `StateStore` (docs/sessions-plan.md
        §5.1). Returns a text result (JSON for reads, "ok"/"not found" for writes)."""
        if self._state is None:
            return "error: no session state is available yet"
        doc = args.get("doc")
        if name == "state_list":
            return json.dumps(self._state.list())
        if name == "state_get":
            return json.dumps(self._state.get(doc, args.get("path")))
        if name == "state_set":
            try:
                self._state.set(doc, args["path"], args.get("value"), validate=self._state_validator(doc))
            except ValueError as e:
                return f"error: {e}"
            return "ok"
        if name == "state_merge":
            try:
                self._state.merge(doc, args.get("value") or {}, validate=self._state_validator(doc))
            except ValueError as e:
                return f"error: {e}"
            return "ok"
        if name == "state_delete":
            return "ok" if self._state.delete(doc, args.get("path")) else "not found"
        if name == "state_schema":
            spec = self.state_defs.get(doc) or {}
            return json.dumps(spec.get("schema_data") or spec)   # the JSON Schema if declared, else the spec
        return f"error: unknown state tool {name!r}"

    def _state_validator(self, doc: str):
        """A validator for a state doc's declared JSON Schema (docs/sessions-plan.md §5.3), or None if the
        doc has no schema. The callback raises ValueError on a schema violation → the write is refused
        (§8.7 reject-on-invalid). No-op if `jsonschema` isn't installed (best-effort)."""
        schema = (self.state_defs.get(doc) or {}).get("schema_data")
        if not schema:
            return None

        def _validate(candidate):
            try:
                import jsonschema
            except ImportError:            # validator unavailable → skip (don't block writes)
                return
            try:
                jsonschema.validate(candidate, schema)
            except jsonschema.ValidationError as e:
                raise ValueError(f"state {doc!r} fails its schema: {e.message}")

        return _validate

    async def _fetch_context(self) -> str:
        """Fetch the agent's `context` MCP resources (e.g. `room://current`) as raw text, injected at
        the prompt's `{context}` placeholder (the agent's prompt.md owns the surrounding framing via a
        `{#context}…{/context}` section — see `_fill_injection`). Gives the agent live room state
        without a query_room round-trip (docs/agents.md §5). Only called when the prompt references
        `{context}`; returns "" when there's nothing (no resources, or all failed) so the section drops
        out. A missing/failed resource is skipped, never fatal."""
        if not self.agent or not self.agent.context:
            return ""
        parts = []
        for uri in self.agent.context:
            try:
                res = await self._session.read_resource(uri)
                text = "".join(getattr(c, "text", "") for c in getattr(res, "contents", []))
                if text.strip():
                    parts.append(text.strip())
            except Exception:
                continue
        return "\n\n".join(parts)

    async def handle(self, text: str, *, speaker: Optional[str] = None,
                     on_text: Optional[OnText] = None, on_tool: Optional[OnTool] = None) -> str:
        """Run the **active** LLM on one user utterance (with tools), record the user/assistant
        transcript, and return the final reply. `speaker` is WHO is talking (the human's id) — it tags
        the user turn and resolves the prompt's `{user}`/ownership for this turn; it defaults to
        `self.user` for a lone client that owns the whole conversation. `on_text(text, final=,
        speaker=)` receives reply text as it's produced (its `speaker` is the *display* LLM name, not
        the human); `on_tool(name, args)` fires before each tool call. Switching the active LLM is the
        shell's job (deterministic — conjure.shell), never inferred from `text` here.

        The Director holds a **single floor** (D4): a turn submitted while another is in flight raises
        `Busy` rather than interleaving into the one shared transcript."""
        if self._busy:                                    # single floor — reject, don't queue (D4).
            raise Busy("the agent is already handling a turn")
        self._busy = True                                 # atomic: no await between the check and here.
        try:
            return await self._handle(text, speaker or self.user, on_text, on_tool)
        finally:
            self._busy = False

    async def greet(self, instruction: str) -> str:
        """Produce a session's **generated** opening line (docs/sessions-plan.md §6): run the active LLM
        once on `instruction` with the agent's system prompt, record ONLY the assistant turn (no user turn
        — the greeting is system-initiated, not a human utterance), and return it. No tools (a greeting
        shouldn't act on the world). Holds the single floor like `handle`."""
        if self._busy:
            raise Busy("the agent is already handling a turn")
        self._busy = True
        try:
            async def _noop_exec(name, args):        # tools=[] ⇒ never called; satisfy the signature
                return ""

            async def _noop_emit(t, *, final):
                return None

            llm = self.roster[self.active]
            final = await llm.run_turn(system=await self._system(), history=list(self.transcript),
                                       user_text=instruction, tools=[], execute_tool=_noop_exec,
                                       emit=_noop_emit)
            self.transcript.append(Turn("assistant", final))
            return final
        finally:
            self._busy = False

    async def _set_caller(self, speaker: str) -> None:
        """Tell the MCP server which user THIS turn's tool calls act as — the speaker — so the world server
        resolves ownership/permissions per-speaker in a shared session (agent-server-plan Step 3). Turns are
        serialized (single floor), so a per-turn identity on the one MCP server is safe. Best-effort: an MCP
        server without the control tool just keeps its launch identity."""
        agent = self.agent.name if self.agent else "builder"
        try:
            await self._session.call_tool("set_caller", {"user": speaker, "scope": scope_for(speaker, agent)})
        except Exception:  # noqa: BLE001 — older/other MCP server, or a stand-in session in tests
            pass

    async def _handle(self, text: str, speaker: str, on_text: Optional[OnText],
                      on_tool: Optional[OnTool]) -> str:
        text = text.strip()
        self._speaker = speaker                           # owns this turn: {user} injection + attribution
        if self._identity_aware:                          # thread the speaker to the tools (owner gate) — Step 3
            await self._set_caller(speaker)
        await self._log("you", text)
        # Tag every log line <agent>.<llm> (e.g. builder.claude); tool lines get a /tool suffix.
        who = f"{getattr(self.agent, 'name', 'agent')}.{self.active.lower()}"
        llm = self.roster[self.active]

        async def emit(t, *, final):
            # Log intermediate spoken text (acks like "On it", and any pre-tool narration) under the
            # agent.llm tag; the final reply is logged once below under the same tag. This is what
            # surfaces e.g. a "let me check the world model" preamble that otherwise only reaches TTS.
            if not final and t and t.strip():
                await self._log(who, t.strip())
            if on_text:
                await on_text(t, final=final, speaker=self.active)

        async def execute(n, a):
            return await self._execute_tool(n, a, on_tool, who)

        system = await self._system()
        final = await llm.run_turn(
            system=system,
            history=list(self.transcript),
            user_text=text,
            tools=self._tools,
            execute_tool=execute,
            emit=emit,
        )
        self.transcript.append(Turn("user", text, by=speaker))   # attribute the turn to who spoke
        self.transcript.append(Turn("assistant", final))         # no LLM attribution (switch stays invisible)
        await self._log(who, final.strip())
        return final
