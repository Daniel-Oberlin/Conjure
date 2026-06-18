"""The director — the shared brain for voice and CLI.

Formerly each interface (voice.py, cli.py) carried its own director: its own system prompt, its own
LLM call, its own tool loop. This module is the single director both now drive. They differ only in
how text arrives (mic vs typing) and leaves (TTS vs print):

    async with Director.connect(settings) as director:
        await director.handle("put a tree in front of me", on_text=..., on_tool=...)

The director owns:
  • the **attributed transcript** — every turn tagged with its speaker (architecture §7a),
  • the **LLM roster** (conjure.llm) — many named LLMs, one *active* at a time,
  • the world-editing **MCP tools** (it is an MCP client of conjure.mcp_server over stdio),
  • the **routing** that lets the user switch or address LLMs mid-conversation by voice/text.

Routing (deterministic — no tokens, fully testable):
  • "let me talk to Gemini" / "switch to Gemini" / "Gemini, take over" → **persistent handover**:
    that LLM becomes active going forward.
  • "Gemini, make a picture of a cat" → **one-shot**: that LLM handles just this turn; the
    previously-active LLM stays active afterward.
  • anything else → the active LLM.
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .agents import AgentDef, ServerSpec, load_agent, load_server_registry, scoped_roster
from .config import Settings
from .llm import LLM, ToolSpec, Turn, build_roster

# Shared system prompt for the builder agent. It now lives in the agent's prompt_file
# (agents/builder.json → prompts/builder.md) so the agent definition owns it; this constant reads that
# file (single source) for the default `Director()` prompt and for tests. `{name}` is filled per-call
# with the active LLM's casual name; roster awareness is appended by `Director._system_for`.
DIRECTOR_PROMPT = (Path(__file__).resolve().parent.parent / "prompts" / "builder.md").read_text()

OnText = Callable[..., Awaitable[None]]   # (text, *, final: bool, speaker: str) -> None
OnTool = Callable[..., Awaitable[None]]   # (name: str, args: dict) -> None


# --------------------------------------------------------------------------- routing

@dataclass
class Route:
    target: str       # casual name that handles this turn
    content: str      # text to send the target (address/handover phrasing stripped)
    persistent: bool  # True → target becomes the new active LLM


# Persistent-handover phrasings. A trailing name is captured; any task after it is preserved.
_HANDOVER = re.compile(
    r"^(?:please\s+)?(?:can\s+i\s+|i'?d\s+like\s+to\s+|i\s+want\s+to\s+|i\s+wanna\s+|"
    r"let'?s\s+|let\s+me\s+)?(?:speak|talk|chat)\s+(?:with|to)\s+(?P<name>[a-z0-9]+)\b",
    re.I,
)
_SWITCH = re.compile(r"^(?:switch|change|hand(?:\s+it)?\s+over)\s+(?:to\s+)?(?P<name>[a-z0-9]+)\b", re.I)
_TAKEOVER = re.compile(
    r"^(?P<name>[a-z0-9]+)[,:]?\s+(?:take\s+over|take\s+it\s+from\s+here|you'?re\s+up|"
    r"you\s+have\s+the\s+floor)\b",
    re.I,
)
# Direct address: "<Name> ..." (comma optional — STT rarely punctuates). The name-match gate below
# is what prevents false positives on ordinary first words.
_ADDRESS = re.compile(r"^(?P<name>[a-z][a-z0-9]*)\b[,:]?\s+(?P<rest>.+)$", re.I | re.S)


def _match_name(token: str, roster) -> Optional[str]:
    for name in roster:
        if name.lower() == token.lower():
            return name
    return None


def route_turn(text: str, roster, active: str) -> Route:
    """Decide who handles this utterance and whether it changes the active LLM. Pure + deterministic."""
    s = text.strip()
    # 1) explicit handover/switch → persistent
    for rx in (_HANDOVER, _SWITCH, _TAKEOVER):
        m = rx.match(s)
        if m:
            name = _match_name(m.group("name"), roster)
            if name:
                rest = s[m.end():].lstrip(" ,.:;-")
                # A bare handover ("let me talk to Gemini") carries no task — content stays empty so
                # the director hands the new LLM a greeting nudge instead of replaying the switch
                # phrase (which it may misread as a build request). A trailing task is preserved.
                return Route(name, rest, persistent=True)
    # 2) direct address "<Name> …" → one-shot (active unchanged)
    m = _ADDRESS.match(s)
    if m:
        name = _match_name(m.group("name"), roster)
        if name:
            return Route(name, m.group("rest").strip(), persistent=False)
    # 3) default → the active LLM
    return Route(active, s, persistent=False)


# --------------------------------------------------------------------------- the director

def _stdio_params(spec: ServerSpec, settings: Settings):
    """Build stdio launch params from a registry ServerSpec: map a bare 'python' to this interpreter
    and substitute ${world_url} in the env (so the registry stays interpreter-/host-agnostic)."""
    from mcp import StdioServerParameters
    command = sys.executable if spec.command in ("python", "python3") else spec.command
    env = {**os.environ, **{k: v.replace("${world_url}", settings.world_url) for k, v in spec.env.items()}}
    return StdioServerParameters(command=command, args=list(spec.args), env=env)


class Director:
    def __init__(self, settings: Settings, session, roster: dict[str, LLM], active: str,
                 tools: Optional[list[ToolSpec]] = None, prompt: str = DIRECTOR_PROMPT,
                 agent: Optional[AgentDef] = None):
        self._settings = settings
        self._session = session          # MCP ClientSession (or a stand-in in tests)
        self.roster = roster
        self.active = active
        self._tools = tools or []
        self._prompt = prompt
        self.agent = agent               # the loaded agent def (None in lightweight tests)
        self.transcript: list[Turn] = []

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(cls, settings: Settings, *, agent: str = "builder", errlog=None):
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
        active = (agentdef.default_llm if agentdef.default_llm in roster      # agent's preference
                  else default_active if default_active in roster            # then settings.llm
                  else next(iter(roster)))                                   # then first available

        specs = [registry[r.server] for r in agentdef.servers if r.server in registry]
        if len(specs) != 1:
            raise RuntimeError(
                f"agent {agent!r}: v1 launches exactly one MCP server (got {len(specs)}: "
                f"{[s.name for s in specs]}).")
        params = _stdio_params(specs[0], settings)

        close_errlog = None
        if errlog is None:
            errlog = close_errlog = open(os.devnull, "w")
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = [ToolSpec(t.name, t.description or "", t.inputSchema)
                             for t in (await session.list_tools()).tools]
                    yield cls(settings, session, roster, active, tools,
                              prompt=agentdef.prompt, agent=agentdef)
        finally:
            if close_errlog is not None:
                close_errlog.close()

    def _system_for(self, name: str) -> str:
        others = [n for n in self.roster if n != name]
        roster_line = (
            f" Other AIs are present in this session: {', '.join(others)}. The user may switch to "
            f"one ('let me talk to {others[0]}') or address one directly; you only receive turns "
            f"meant for you. In the transcript, assistant lines prefixed like [Name] were said by "
            f"another AI — unprefixed assistant lines are yours; you may reference what they said."
        ) if others else ""
        return self._prompt.format(name=name) + roster_line

    async def _execute_tool(self, name: str, args: dict, on_tool: Optional[OnTool]) -> str:
        if on_tool:
            await on_tool(name, args)
        out = await self._session.call_tool(name, args)
        return "".join(getattr(c, "text", "") for c in out.content)

    async def handle(self, text: str, *, on_text: Optional[OnText] = None,
                     on_tool: Optional[OnTool] = None) -> str:
        """Route one user utterance to an LLM, run its turn (with tools), record the attributed
        transcript, and return the final reply. `on_text(text, final=, speaker=)` receives reply
        text as it's produced; `on_tool(name, args)` fires before each tool call."""
        route = route_turn(text, self.roster, self.active)
        prev_active = self.active
        if route.persistent:
            self.active = route.target
        llm = self.roster[route.target]
        # A bare handover has no task: ask the newly-active LLM to greet rather than guess.
        user_text = route.content or (
            f"You are now the active director (the user switched to you). Greet them in one short "
            f"line as {route.target}; don't build anything yet.")

        async def emit(t, *, final):
            if on_text:
                await on_text(t, final=final, speaker=route.target)

        async def execute(n, a):
            return await self._execute_tool(n, a, on_tool)

        try:
            final = await llm.run_turn(
                system=self._system_for(route.target),
                history=list(self.transcript),
                user_text=user_text,
                tools=self._tools,
                execute_tool=execute,
                emit=emit,
            )
        except Exception:
            # A switch that failed on its first turn shouldn't strand the user on a broken LLM —
            # revert to whoever they were talking to. (The caller surfaces the error.)
            if route.persistent:
                self.active = prev_active
            raise
        self.transcript.append(Turn("user", text.strip()))
        self.transcript.append(Turn(route.target, final))
        return final
