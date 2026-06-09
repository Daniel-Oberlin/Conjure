"""LLM roster — the director's swappable, named LLMs (decision #1; architecture §7a).

Each LLM is a provider behind one uniform interface (`LLM.run_turn`), so the director can use any of
them, switch between them mid-conversation, and gain new ones by *registration alone* — no caller
changes. Voice and CLI share this through `conjure.director`; neither imports a vendor SDK.

Providers call the lightweight vendor SDKs directly (`anthropic`, `google-genai`) — NOT PipeCat — so
the roster works in the base (no-voice) install and one code path serves both interfaces.

To add a provider: implement the `LLM` protocol and register it in `build_roster`. Nothing else in
the codebase changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from .config import Settings


@dataclass
class Turn:
    """One entry in the attributed transcript (architecture §7a)."""

    speaker: str  # "user" or an LLM's casual name ("Claude", "Gemini", …)
    text: str


@dataclass
class ToolSpec:
    """A world-editing tool, provider-neutral (sourced from the MCP server)."""

    name: str
    description: str
    input_schema: dict


# Callbacks the director hands to each turn.
ExecuteTool = Callable[[str, dict], Awaitable[str]]  # (tool_name, args) -> result text
Emit = Callable[..., Awaitable[None]]                # (text, *, final: bool) -> None


class LLM(Protocol):
    """A named director LLM. Implementations own their vendor's message format and tool-call loop."""

    name: str  # casual name, also the addressing target ("Claude", "Gemini")

    async def run_turn(self, *, system: str, history: list[Turn], user_text: str,
                       tools: list[ToolSpec], execute_tool: ExecuteTool, emit: Emit) -> str:
        """Run one full agentic turn: serialize `history` + `user_text`, call the model, run any
        tool calls via `execute_tool`, `emit` text as it's produced (early acknowledgement chunks
        with final=False, the closing reply with final=True), and return the final reply text."""
        ...


def _attributed(history: list[Turn], me: str) -> list[tuple[str, str]]:
    """Render the shared transcript for `me` as (role, text) pairs. Assistant turns from *other*
    LLMs get a ``[Name]`` prefix so `me` can see who said what (architecture §7a); the user's turns
    and `me`'s own turns pass through plain."""
    out: list[tuple[str, str]] = []
    for t in history:
        if t.speaker == "user":
            out.append(("user", t.text))
        elif t.speaker == me:
            out.append(("assistant", t.text))
        else:
            out.append(("assistant", f"[{t.speaker}] {t.text}"))
    return out


# --------------------------------------------------------------------------- Claude (Anthropic)

class ClaudeLLM:
    name: str

    def __init__(self, name: str, api_key: str, model: str):
        self.name = name
        self._api_key = api_key
        self.model = model

    async def run_turn(self, *, system, history, user_text, tools, execute_tool, emit) -> str:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        ant_tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema}
                     for t in tools]
        messages: list = [{"role": role, "content": text} for role, text in _attributed(history, self.name)]
        messages.append({"role": "user", "content": user_text})

        final_text = ""
        while True:
            resp = await client.messages.create(
                model=self.model, max_tokens=1024, system=system, tools=ant_tools, messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})
            tool_uses = [b for b in resp.content if b.type == "tool_use"]
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if text:
                await emit(text, final=not tool_uses)
                if not tool_uses:
                    final_text = text
            if not tool_uses:
                break
            results = []
            for tu in tool_uses:
                out = await execute_tool(tu.name, tu.input)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            messages.append({"role": "user", "content": results})
        return final_text


# --------------------------------------------------------------------------- Gemini (Google)

class GeminiLLM:
    name: str

    def __init__(self, name: str, api_key: str, model: str):
        self.name = name
        self._api_key = api_key
        self.model = model

    async def run_turn(self, *, system, history, user_text, tools, execute_tool, emit) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        gem_tools = [types.Tool(function_declarations=[
            types.FunctionDeclaration(name=t.name, description=t.description,
                                      parameters_json_schema=t.input_schema)
            for t in tools
        ])] if tools else None
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=gem_tools,
            # Run the loop ourselves so we execute tools via our own MCP client.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        contents: list = [
            types.Content(role=("user" if role == "user" else "model"), parts=[types.Part(text=text)])
            for role, text in _attributed(history, self.name)
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

        final_text = ""
        while True:
            resp = await client.aio.models.generate_content(
                model=self.model, contents=contents, config=config)
            cand = (resp.candidates or [None])[0]
            parts = (cand.content.parts if cand and cand.content else []) or []
            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            text = "".join(p.text for p in parts if getattr(p, "text", None)).strip()
            if cand and cand.content:
                contents.append(cand.content)  # echo the model turn back verbatim
            if text:
                await emit(text, final=not calls)
                if not calls:
                    final_text = text
            if not calls:
                break
            results = [
                types.Part.from_function_response(
                    name=fc.name, response={"result": await execute_tool(fc.name, dict(fc.args or {}))})
                for fc in calls
            ]
            contents.append(types.Content(role="user", parts=results))  # Gemini has no "tool" role
        return final_text


# --------------------------------------------------------------------------- registry

def build_roster(settings: Settings) -> tuple[dict[str, LLM], str]:
    """Return ``({casual name: LLM}, default-active name)`` for every provider whose key is present.

    This is the *only* place providers are registered — add one here and it is instantly usable by
    both voice and CLI, with mid-conversation switching, and no changes anywhere else."""
    roster: dict[str, LLM] = {}
    if settings.anthropic_api_key:
        roster["Claude"] = ClaudeLLM("Claude", settings.anthropic_api_key, settings.llm_model)
    if settings.google_api_key:
        roster["Gemini"] = GeminiLLM("Gemini", settings.google_api_key, settings.gemini_model)

    pref = {"claude": "Claude", "gemini": "Gemini"}.get(settings.llm, "")
    active = pref if pref in roster else next(iter(roster), "")
    return roster, active
