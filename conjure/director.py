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
import json
import os
import sys
from typing import Awaitable, Callable, Optional

from .agents import AgentDef, ServerSpec, load_agent, load_server_registry, scoped_roster
from .config import DEFAULT_USER, Settings, scope_for
from .llm import LLM, ToolSpec, Turn, build_roster

# Shared system prompt for the builder agent. It lives in the agent's prompt_file
# (agents/builder.json → prompts/builder.md), so the agent definition owns it — including the path.
# This constant goes through the loader (single source) for the default `Director()` prompt and tests.
# `Director._system` appends the logged-in-user identity; the prompt is the same for every LLM.
DIRECTOR_PROMPT = load_agent("builder").prompt

OnText = Callable[..., Awaitable[None]]   # (text, *, final: bool, speaker: str) -> None
OnTool = Callable[..., Awaitable[None]]   # (name: str, args: dict) -> None


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

    def _system(self) -> str:
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
            f"owns one from that rather than guessing or saying you can't tell. A PUBLIC world can only "
            f"contain PUBLIC assets (so a visitor sees the whole scene), so placing your private asset into "
            f"a public world — or making a world public — publishes the assets it uses; the tool tells you "
            f"when it does, and you should pass that along."
        )
        return self._prompt + identity_line

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
        """Run the **active** LLM on one user utterance (with tools), record the user/assistant
        transcript, and return the final reply. `on_text(text, final=, speaker=)` receives reply
        text as it's produced; `on_tool(name, args)` fires before each tool call. Switching the active
        LLM is the shell's job (deterministic — conjure.shell), never inferred from `text` here."""
        text = text.strip()
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

        system = self._system() + await self._fetch_context()
        final = await llm.run_turn(
            system=system,
            history=list(self.transcript),
            user_text=text,
            tools=self._tools,
            execute_tool=execute,
            emit=emit,
        )
        self.transcript.append(Turn("user", text))
        self.transcript.append(Turn("assistant", final))
        await self._log(who, final.strip())
        return final
