"""The agent runtime — today's **builder** agent, the shared brain for voice and CLI.

The director loads as the `builder` agent — a declarative def in `agents/builder/` (via conjure.agents):
its prompt, the LLMs it's allowed to run on, the MCP servers it's scoped to, and the context it injects.
A deterministic **shell** (conjure.shell) wraps it — control commands run there; anything else is
forwarded here. Both front-ends (voice.py, cli.py) drive shell → agent; they differ only in how text
arrives (mic vs typing) and leaves (TTS vs print):

    async with Director.connect(settings, agent="builder") as director:
        await director.handle("put a tree in front of me", on_text=..., on_tool=...)

The agent owns:
  • the **attributed transcript** — every turn tagged with its speaker (architecture §7a),
  • the **LLM roster** (conjure.llm) — the named LLMs it allows, one *active* at a time,
  • the world-editing **MCP tools** (it is an MCP client of its scoped servers over stdio),
  • the per-turn **context** it injects (e.g. `room://current` — the live room, agents.md §5),
  • the inline **routing** that switches/addresses LLMs (the shell also does this deterministically;
    migrating the inline path fully to the shell is deferred — agents.md §10).

Routing (deterministic — no tokens, fully testable):
  • "let me talk to Gemini" / "switch to Gemini" / "Gemini, take over" → **persistent handover**:
    that LLM becomes active going forward.
  • "Gemini, make a picture of a cat" → **one-shot**: that LLM handles just this turn; the
    previously-active LLM stays active afterward.
  • anything else → the active LLM.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from .agents import AgentDef, ServerSpec, load_agent, load_server_registry, scoped_roster
from .config import DEFAULT_USER, Settings, scope_for
from .llm import LLM, ToolSpec, Turn, build_roster

# Shared system prompt for the builder agent. It lives in the agent's prompt_file
# (agents/builder.json → prompts/builder.md), so the agent definition owns it — including the path.
# This constant goes through the loader (single source) for the default `Director()` prompt and tests.
# `{name}` is filled per-call with the active LLM's casual name; roster awareness is appended by
# `Director._system_for`.
DIRECTOR_PROMPT = load_agent("builder").prompt

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

def _stdio_params(spec: ServerSpec, settings: Settings, agent: str = "builder", user: str = DEFAULT_USER):
    """Build stdio launch params from a registry ServerSpec: map a bare 'python' to this interpreter,
    substitute ${world_url} in the env, and inject the (user, agent) catalog SCOPE as a capability (so
    the MCP server's maintenance tools are scoped to this user+agent — never an LLM arg)."""
    from mcp import StdioServerParameters
    command = sys.executable if spec.command in ("python", "python3") else spec.command
    env = {**os.environ, **{k: v.replace("${world_url}", settings.world_url) for k, v in spec.env.items()}}
    env["CONJURE_SCOPE"] = scope_for(user, agent)
    return StdioServerParameters(command=command, args=list(spec.args), env=env)


class Director:
    def __init__(self, settings: Settings, session, roster: dict[str, LLM], active: str,
                 tools: Optional[list[ToolSpec]] = None, prompt: str = DIRECTOR_PROMPT,
                 agent: Optional[AgentDef] = None, user: str = DEFAULT_USER):
        self._settings = settings
        self._session = session          # MCP ClientSession (or a stand-in in tests)
        self.roster = roster
        self.active = active
        self._tools = tools or []
        self._prompt = prompt
        self.agent = agent               # the loaded agent def (None in lightweight tests)
        self.user = user                 # the logged-in user this director acts as (owns its spaces/worlds)
        self.transcript: list[Turn] = []

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
        active = (agentdef.default_llm if agentdef.default_llm in roster      # agent's preference
                  else default_active if default_active in roster            # then settings.llm
                  else next(iter(roster)))                                   # then first available

        specs = [registry[r.server] for r in agentdef.servers if r.server in registry]
        if len(specs) != 1:
            raise RuntimeError(
                f"agent {agent!r}: v1 launches exactly one MCP server (got {len(specs)}: "
                f"{[s.name for s in specs]}).")
        params = _stdio_params(specs[0], settings, agent, user)

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
                              prompt=agentdef.prompt, agent=agentdef, user=user)
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
        identity_line = (
            f" The logged-in user you act for is '{self.user}' — if asked who is logged in / who they "
            f"are, that's the answer. Worlds and spaces belong to whoever created them. You can freely "
            f"create and switch worlds (everyone present comes along) and edit any world you own. You "
            f"can ALSO see and enter other users' PUBLIC worlds — list_worlds shows them under 'other "
            f"users' public worlds', and switch_world(name, owner='<their-username>') takes you there — "
            f"but you can't edit a world you don't own. Worlds are PUBLIC by default; make one private "
            f"(or public again) with set_world_visibility(public=…), or create a private one via "
            f"new_world(name, public=False). If a tool refuses an edit to another user's world, relay it "
            f"plainly; never invent a name collision or claim a capability (like private worlds) is absent. "
            f"Library ASSETS work the same way: public by default (others on this server can reuse them), "
            f"and you can flip one with update_asset(id, public=…) — but only for assets YOU own; another "
            f"user's asset that merely shows up in your searches stays theirs. An asset's owner is the user "
            f"in its `scope` column (the part before '/agents/'), readable via query_assets — so state who "
            f"owns one from that rather than guessing or saying you can't tell."
        )
        return self._prompt.format(name=name) + roster_line + identity_line

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
        if on_tool:
            await on_tool(name, args)
        await self._log(f"{who}/tool", f"{name}({json.dumps(args, default=str)[:600]})")
        out = await self._session.call_tool(name, args)
        text = "".join(getattr(c, "text", "") for c in out.content)
        await self._log(f"{who}/tool", f"  -> {text[:2000]}")   # roomy enough to see a full query_world
        return text

    async def _fetch_context(self) -> str:
        """Prefetch the agent's `context` MCP resources (e.g. `room://current`) and return them as a
        block to append to the system prompt — so the agent has live room state without a query_room
        round-trip (docs/agents.md §5). A missing/failed resource is skipped, never fatal."""
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
        if not parts:
            return ""
        return ("\n\n--- Live context (current; already fetched for you — use it, don't re-query) ---\n"
                + "\n\n".join(parts))

    async def handle(self, text: str, *, on_text: Optional[OnText] = None,
                     on_tool: Optional[OnTool] = None) -> str:
        """Route one user utterance to an LLM, run its turn (with tools), record the attributed
        transcript, and return the final reply. `on_text(text, final=, speaker=)` receives reply
        text as it's produced; `on_tool(name, args)` fires before each tool call."""
        await self._log("you", text.strip())
        route = route_turn(text, self.roster, self.active)
        # Attribute every LLM line to <agent>.<llm> (e.g. builder.claude); tool lines get a /tool suffix.
        who = f"{getattr(self.agent, 'name', 'agent')}.{route.target.lower()}"
        prev_active = self.active
        if route.persistent:
            self.active = route.target
        llm = self.roster[route.target]
        # A bare handover has no task: ask the newly-active LLM to greet rather than guess.
        user_text = route.content or (
            f"You are now the active director (the user switched to you). Greet them in one short "
            f"line as {route.target}; don't build anything yet.")

        async def emit(t, *, final):
            # Log intermediate spoken text (acks like "On it", and any pre-tool narration) under the
            # agent.llm tag; the final reply is logged once below under the same tag. This is what
            # surfaces e.g. a "let me check the world model" preamble that otherwise only reaches TTS.
            if not final and t and t.strip():
                await self._log(who, t.strip())
            if on_text:
                await on_text(t, final=final, speaker=route.target)

        async def execute(n, a):
            return await self._execute_tool(n, a, on_tool, who)

        system = self._system_for(route.target) + await self._fetch_context()
        try:
            final = await llm.run_turn(
                system=system,
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
        await self._log(who, final.strip())
        return final
