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
from typing import Awaitable, Callable, Optional

from .config import Settings
from .llm import LLM, ToolSpec, Turn, build_roster

# Shared system prompt (the verbose one, formerly only in voice.py), parameterised by the active
# LLM's casual name. Roster awareness is appended per-call by `Director._system_for`.
DIRECTOR_PROMPT = (
    "You are {name}, a director of a voice-controlled VR holodeck. When the user describes or "
    "requests a scene or a change, USE THE TOOLS to build and edit the world — add, move, update, "
    "or remove objects, and set the environment. "
    "For real-world objects (a tree, a chair, a car, an animal), use place_asset with a short "
    "search query; use add_entity only for basic primitive shapes (cube, sphere, cone, ...). "
    "Images are procured first, then used: to add a NEW picture, call generate_image (it returns an "
    "image_id) then place_image with that image_id; for a NEW surrounding sky, call "
    "generate_skybox_image then set_skybox with its image_id. For a transparent cut-out (a sticker/"
    "decal with no background), pass transparent=true to generate_image. To change a picture ALREADY "
    "in the scene, use the one-step scene editors by its entity id (find ids via query_world): "
    "edit_scene_image to change it ('make it nighttime'), widen_scene_image to extend it wider, "
    "skybox_from_scene_image to turn it into the sky. To map an image onto a REAL room surface — e.g. "
    "a starfield on the ceiling, grass on the floor, a mural on a wall — generate_image then "
    "texture_surface(target, image_id) where target is a semantic label ('floor'/'ceiling'/'wall'), a "
    "surface's short friendly id (a number the user can read off its label), or 'all'; pass repeat=N "
    "with a seamless/tileable image to tile it (grass, brick). To color a surface or make it see-"
    "through, use style_surface(target, color, opacity) ('glass walls' = low opacity). "
    "Use show_annotations(on) to label each surface with its name + short id (e.g. 'window (12)') when "
    "the user wants to identify or reference surfaces — they can then say 'make 12 blue'; pass "
    "dimensions=true only if they ask to see sizes. Don't pick an image generator unless the user asked for a specific one — "
    "omit it and the best default is used. "
    "THE MOMENT you understand a request, FIRST give a brief, natural, VARIED acknowledgement — "
    "e.g. 'On it', 'Sure, one sec', 'Got it', 'Working on it', 'You got it' — then immediately call "
    "the tools. Vary the wording each time; never sound scripted or repeat the same phrase. "
    "CRITICAL: after that acknowledgement, do NOT think out loud, explain your reasoning, or recite "
    "coordinates, sizes, or measurements. Do the work via tool calls, then reply with AT MOST one "
    "short confirmation (e.g. 'Done — there's your dragon.'). Never repeat or restate what the user "
    "said. If no action is needed, just give a brief reply. "
    "Call query_world first when an edit depends on what's already there. "
    "Positions are [x, y, z] in meters: the user faces -z, so place things a few meters in front "
    "(negative z) around y=1 unless asked otherwise. For place_asset, always pass size_m as the "
    "object's real-world size in meters (tree ~7, chair ~0.9, mug ~0.1) so the scene is to-scale; "
    "those objects auto-sit on the floor (y=0) — only raise y to set something on a surface."
)

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

class Director:
    def __init__(self, settings: Settings, session, roster: dict[str, LLM], active: str,
                 tools: Optional[list[ToolSpec]] = None, prompt: str = DIRECTOR_PROMPT):
        self._settings = settings
        self._session = session          # MCP ClientSession (or a stand-in in tests)
        self.roster = roster
        self.active = active
        self._tools = tools or []
        self._prompt = prompt
        self.transcript: list[Turn] = []

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(cls, settings: Settings, *, errlog=None):
        """Open the world-editing MCP server over stdio and build the roster. Yields a ready Director.

        Raises RuntimeError if no LLM keys are configured (an empty roster can't direct anything)."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        roster, active = build_roster(settings)
        if not roster:
            raise RuntimeError(
                "No director LLMs available — set ANTHROPIC_API_KEY and/or GOOGLE_API_KEY in .env.")
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "conjure.mcp_server"],
            env={**os.environ, "CONJURE_URL": settings.world_url},
        )
        close_errlog = None
        if errlog is None:
            errlog = close_errlog = open(os.devnull, "w")
        try:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = [ToolSpec(t.name, t.description or "", t.inputSchema)
                             for t in (await session.list_tools()).tools]
                    yield cls(settings, session, roster, active, tools)
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
