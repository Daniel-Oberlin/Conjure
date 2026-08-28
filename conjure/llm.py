"""Provider abstraction — the director's named LLMs *and* their image generators (decision #1).

One module, one provider family. A vendor (Anthropic, Google, OpenAI, xAI) can offer two capabilities:

  • **Director** — a conversational, tool-calling LLM (`LLM.run_turn`) the user talks to. The roster
    lets the director switch between them mid-conversation and gain new ones by registration alone.
  • **Image generation** — bytes-in/bytes-out with declared `ImageCapabilities`, so callers (and the
    director, via the world server) can reason about what each generator can and cannot do.

Claude is director-only. Gemini, OpenAI, and xAI ("Grok") do both. Everything is wired from ONE place —
the `ROSTER` table below: casual names, which key powers them, which models, and which ops each generator
is the default for. Change a name there and it changes everywhere (addressing, image selection).

Providers call the vendor SDKs directly (`anthropic`, `google-genai`, `openai` — xAI reuses the `openai`
SDK against its own base URL) and import them
*lazily inside methods* — so importing this module pulls no heavy SDK, and the world server can use
the image side without dragging in director deps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Protocol

from .config import Settings


@dataclass
class Turn:
    """One entry in the shared transcript (architecture §7a)."""

    speaker: str  # the role: "user" or "assistant"
    text: str
    by: str = ""  # for a *user* turn, WHO spoke (the human's id); "" for assistant turns. This is the
                  # only attribution the transcript carries — never the LLM (a switch stays invisible).


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

    name: str  # casual name, also the addressing target ("Claude", "Gemini", "Chat")

    async def run_turn(self, *, system: str, history: list[Turn], user_text: str,
                       tools: list[ToolSpec], execute_tool: ExecuteTool, emit: Emit) -> str:
        """Run one full agentic turn: serialize `history` + `user_text`, call the model, run any
        tool calls via `execute_tool`, `emit` text as it's produced (early acknowledgement chunks
        with final=False, the closing reply with final=True), and return the final reply text."""
        ...


def _messages(history: list[Turn]) -> list[tuple[str, str]]:
    """Render the shared transcript as (role, text) pairs. A **user** turn is prefixed with WHO spoke
    (`daniel: …`) so the director can attribute turns in a shared, multi-user conversation — otherwise it
    can't tell two speakers apart and assumes every line is "you". An **assistant** turn stays
    unattributed: the director is one voice, and the transcript records no LLM identity, so a model switch
    stays invisible (architecture §7a). Portable across providers — it's just text, no per-message `name`
    field — and role-alternation stays clean since each user turn is followed by the director's reply."""
    out: list[tuple[str, str]] = []
    for t in history:
        if t.speaker == "user":
            out.append(("user", f"{t.by}: {t.text}" if t.by else t.text))
        else:
            out.append(("assistant", t.text))
    return out


# =================================================================== director LLMs

# A turn is a loop — call tools, feed the results back, repeat until the model stops asking. Nothing in
# the protocol guarantees it ever stops: a model can answer every tool result with the same tool call
# forever, and each hop costs an API call and broadcasts a patch to every client. Observed 2026-08-28
# (Grok, 40+ identical `show_edges({"on": true})` hops, only ended by killing the server).
#
# So every provider loop is bounded. The number is generous — a real build turn (generate an image,
# place it, style three surfaces) is well under it — because the *pathological* case is caught far
# earlier and more precisely by the Director's repeat guard (docs/specs/agents.md §5.6); this is the
# backstop for a model that loops without repeating itself exactly.
MAX_TOOL_HOPS = 24

# What the user hears when the backstop fires. It is emitted as the turn's final text and therefore
# lands in the transcript — deliberately, so the next turn's history shows the model its own failure
# instead of leaving an unexplained gap. Every degradation is audible (architecture §1).
HOP_LIMIT_NOTICE = "I got stuck repeating myself, so I stopped there. Ask me again?"


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
        messages: list = [{"role": role, "content": text} for role, text in _messages(history)]
        messages.append({"role": "user", "content": user_text})

        final_text = ""
        for _ in range(MAX_TOOL_HOPS):
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
        else:
            final_text = HOP_LIMIT_NOTICE                    # ran the cap out without ever stopping
            await emit(final_text, final=True)
        return final_text


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
            for role, text in _messages(history)
        ]
        contents.append(types.Content(role="user", parts=[types.Part(text=user_text)]))

        final_text = ""
        for _ in range(MAX_TOOL_HOPS):
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
        else:
            final_text = HOP_LIMIT_NOTICE                    # ran the cap out without ever stopping
            await emit(final_text, final=True)
        return final_text


class _OpenAICompatibleLLM:
    """Director loop for the OpenAI Chat Completions API and any vendor that mirrors it. The whole
    tool-call loop is vendor-neutral; a subclass only sets `base_url` (None = OpenAI's own endpoint,
    otherwise e.g. xAI's ``https://api.x.ai/v1``)."""

    name: str
    base_url: Optional[str] = None

    def __init__(self, name: str, api_key: str, model: str):
        self.name = name
        self._api_key = api_key
        self.model = model

    def _client(self):
        from openai import AsyncOpenAI

        kwargs: dict = {"api_key": self._api_key}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return AsyncOpenAI(**kwargs)

    async def run_turn(self, *, system, history, user_text, tools, execute_tool, emit) -> str:
        import json

        client = self._client()
        oa_tools = [{"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.input_schema}} for t in tools]
        messages: list = [{"role": "system", "content": system}]
        messages += [{"role": role, "content": text} for role, text in _messages(history)]
        messages.append({"role": "user", "content": user_text})

        final_text = ""
        for _ in range(MAX_TOOL_HOPS):
            kwargs: dict = {"model": self.model, "messages": messages}
            if oa_tools:
                kwargs["tools"] = oa_tools
            resp = await client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            tool_calls = [tc for tc in (msg.tool_calls or []) if tc.type == "function"]
            text = (msg.content or "").strip()

            # Re-append the assistant message verbatim (must carry tool_calls so the API accepts the
            # following tool results). Build it explicitly to avoid extra SDK fields.
            assistant: dict = {"role": "assistant"}
            if msg.content:
                assistant["content"] = msg.content
            if tool_calls:
                assistant["tool_calls"] = [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ]
            messages.append(assistant)

            if text:
                await emit(text, final=not tool_calls)
                if not tool_calls:
                    final_text = text
            if not tool_calls:
                break
            for tc in tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                out = await execute_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": out})
        else:
            final_text = HOP_LIMIT_NOTICE                    # ran the cap out without ever stopping
            await emit(final_text, final=True)
        return final_text


class OpenAILLM(_OpenAICompatibleLLM):
    """OpenAI ("Chat") — the reference Chat Completions director."""


class GrokLLM(_OpenAICompatibleLLM):
    """xAI ("Grok") — the same Chat Completions loop pointed at api.x.ai. Tool calling is
    OpenAI-compatible, so only the base URL differs."""

    base_url = "https://api.x.ai/v1"


# =================================================================== image generation

# Bound a single image request so a stalled API call fails cleanly instead of hanging the server
# forever (a 4K skybox is the slow case — generous, but finite). Kept below the MCP/CLI HTTP
# timeouts so the SDK surfaces the error first.
_IMAGE_TIMEOUT_S = 180
_IMAGE_TIMEOUT_MS = _IMAGE_TIMEOUT_S * 1000


@dataclass
class ImageResult:
    data: bytes
    mime_type: str
    provider: str
    model: str


@dataclass(frozen=True)
class ImageCapabilities:
    """What an image generator can do — exposed so callers/the LLM can reason about fit."""

    operations: frozenset[str]    # subset of {"generate", "edit", "outpaint", "skybox"}
    edit_mode: Optional[str]      # "prompt" (conversational) | "mask" (inpaint) | None
    max_resolution: int           # longest-side pixels (e.g. 4096, 1536)
    aspect: str                   # "free" | "fixed"
    fixed_sizes: tuple[str, ...]  # () for free aspect, else allowed "WxH" sizes
    transparency: bool            # can produce an alpha (transparent) background

    def to_dict(self) -> dict:
        return {
            "operations": sorted(self.operations),
            "edit_mode": self.edit_mode,
            "max_resolution": self.max_resolution,
            "aspect": self.aspect,
            "fixed_sizes": list(self.fixed_sizes),
            "transparency": self.transparency,
        }


class ImageGenerator(Protocol):
    name: str
    model: str
    capabilities: ImageCapabilities

    async def generate(self, prompt: str, *, aspect_ratio: Optional[str] = None,
                       image_size: Optional[str] = None, model: Optional[str] = None,
                       transparent: bool = False) -> ImageResult: ...

    async def edit(self, prompt: str, image: bytes, *, aspect_ratio: Optional[str] = None,
                   image_size: Optional[str] = None, model: Optional[str] = None,
                   transparent: bool = False, mask: Optional[bytes] = None) -> ImageResult: ...


class GeminiImageGenerator:
    """Google Gemini ("Nano Banana"). Conversational (prompt) editing, free aspect, up to 4K — the
    only generator that can outpaint and make a high-res equirectangular skybox. No transparency."""

    capabilities = ImageCapabilities(
        operations=frozenset({"generate", "edit", "outpaint", "skybox"}),
        edit_mode="prompt", max_resolution=4096, aspect="free", fixed_sizes=(), transparency=False,
    )

    def __init__(self, name: str, api_key: str, model: str):
        self.name = name
        self._api_key = api_key
        self.model = model

    async def generate(self, prompt, *, aspect_ratio=None, image_size=None, model=None,
                       transparent=False) -> ImageResult:
        import asyncio
        return await asyncio.to_thread(self._call, [prompt], aspect_ratio, image_size, model)

    async def edit(self, prompt, image, *, aspect_ratio=None, image_size=None, model=None,
                   transparent=False, mask=None) -> ImageResult:
        import asyncio
        return await asyncio.to_thread(self._call, [image, prompt], aspect_ratio, image_size, model)

    def _call(self, parts: list, aspect_ratio, image_size, model) -> ImageResult:
        """parts: list of prompt str and/or raw image bytes (bytes are sent as an image part)."""
        import base64

        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key,
                              http_options=types.HttpOptions(timeout=_IMAGE_TIMEOUT_MS))
        contents = [
            types.Part(inline_data=types.Blob(data=p, mime_type="image/png")) if isinstance(p, bytes) else p
            for p in parts
        ]
        img_cfg: dict = {}
        if aspect_ratio:
            img_cfg["aspect_ratio"] = aspect_ratio
        if image_size:
            img_cfg["image_size"] = image_size
        config_kwargs: dict = {"response_modalities": ["IMAGE"]}
        if img_cfg:
            config_kwargs["image_config"] = types.ImageConfig(**img_cfg)
        effective_model = model or self.model
        config = types.GenerateContentConfig(**config_kwargs)

        # Gemini occasionally returns an empty candidate (finish_reason STOP, no image) — a
        # transient blip. Retry that once. But a non-STOP reason (SAFETY, PROHIBITED_CONTENT,
        # MAX_TOKENS…) is a real refusal: surface it immediately rather than pay for a retry.
        last_reason = None
        for _attempt in range(2):
            resp = client.models.generate_content(
                model=effective_model, contents=contents, config=config)
            for candidate in resp.candidates or []:
                last_reason = getattr(candidate, "finish_reason", None)
                for part in (candidate.content.parts if candidate.content else []) or []:
                    blob = getattr(part, "inline_data", None)
                    if blob and blob.data:
                        data = blob.data
                        if isinstance(data, str):  # some SDK paths hand back base64
                            data = base64.b64decode(data)
                        return ImageResult(
                            data=data, mime_type=blob.mime_type or "image/png",
                            provider=self.name, model=effective_model)
            if last_reason not in (None, types.FinishReason.STOP):
                break
        raise RuntimeError(f"Gemini returned no image part (finish_reason={last_reason})")


class _OpenAICompatibleImageGenerator:
    """Shared plumbing for OpenAI-style image APIs (OpenAI, xAI): the client factory (subclasses set
    `base_url`) and the base64 → bytes decode. Subclasses own `capabilities`, `generate`, and `edit`,
    since the request bodies differ (gpt-image-1 takes `size`/`background`; grok takes `aspect_ratio`/
    `resolution` via `extra_body` and returns `response_format="b64_json"`)."""

    base_url: Optional[str] = None

    def __init__(self, name: str, api_key: str, model: str):
        self.name = name
        self._api_key = api_key
        self.model = model

    def _client(self):
        from openai import AsyncOpenAI

        kwargs: dict = {"api_key": self._api_key, "timeout": _IMAGE_TIMEOUT_S}
        if self.base_url:
            kwargs["base_url"] = self.base_url
        return AsyncOpenAI(**kwargs)

    def _result(self, resp, model: str) -> ImageResult:
        import base64

        # OpenAI-compatible image endpoints return base64 (gpt-image-1 always; grok when asked).
        data = base64.b64decode(resp.data[0].b64_json)
        return ImageResult(data=data, mime_type="image/png", provider=self.name, model=model)


class OpenAIImageGenerator(_OpenAICompatibleImageGenerator):
    """OpenAI gpt-image-1. Strong prompt adherence + legible text, mask/whole-image edit, and the
    only generator that can produce transparent (alpha) backgrounds — but fixed sizes ≤1536, no
    arbitrary aspect, no outpaint, no high-res skybox."""

    capabilities = ImageCapabilities(
        operations=frozenset({"generate", "edit"}),
        edit_mode="mask", max_resolution=1536, aspect="fixed",
        fixed_sizes=("1024x1024", "1536x1024", "1024x1536"), transparency=True,
    )

    def _size_for(self, aspect_ratio: Optional[str]) -> str:
        """Map a requested free aspect onto the nearest gpt-image-1 fixed size."""
        if not aspect_ratio:
            return "1024x1024"
        try:
            w, h = (float(x) for x in aspect_ratio.replace(" ", "").split(":"))
            r = w / h if h else 1.0
        except Exception:
            return "1024x1024"
        if r > 1.2:
            return "1536x1024"
        if r < 0.83:
            return "1024x1536"
        return "1024x1024"

    async def generate(self, prompt, *, aspect_ratio=None, image_size=None, model=None,
                       transparent=False) -> ImageResult:
        client = self._client()
        eff = model or self.model
        kwargs: dict = {"model": eff, "prompt": prompt, "size": self._size_for(aspect_ratio)}
        if transparent:
            kwargs["background"] = "transparent"
        resp = await client.images.generate(**kwargs)
        return self._result(resp, eff)

    async def edit(self, prompt, image, *, aspect_ratio=None, image_size=None, model=None,
                   transparent=False, mask=None) -> ImageResult:
        client = self._client()
        eff = model or self.model
        kwargs: dict = {"model": eff, "prompt": prompt, "image": ("image.png", image, "image/png")}
        if aspect_ratio:
            kwargs["size"] = self._size_for(aspect_ratio)
        if transparent:
            kwargs["background"] = "transparent"
        if mask is not None:
            kwargs["mask"] = ("mask.png", mask, "image/png")
        resp = await client.images.edit(**kwargs)
        return self._result(resp, eff)


class GrokImageGenerator(_OpenAICompatibleImageGenerator):
    """xAI grok-imagine-image-2.0. Generate-only, OpenAI-compatible: `aspect_ratio` + `resolution`
    ("1k"/"2k") ride in `extra_body`, bytes come back via `response_format="b64_json"`. No editing,
    no mask, no transparency, no high-res skybox — so it's never auto-selected (Gemini owns
    skybox/edit/outpaint, OpenAI owns transparency); it's reachable only when explicitly requested."""

    base_url = "https://api.x.ai/v1"
    capabilities = ImageCapabilities(
        operations=frozenset({"generate"}),
        edit_mode=None, max_resolution=2048, aspect="free", fixed_sizes=(), transparency=False,
    )

    async def generate(self, prompt, *, aspect_ratio=None, image_size=None, model=None,
                       transparent=False) -> ImageResult:
        client = self._client()
        eff = model or self.model
        extra: dict = {"resolution": image_size or "2k"}
        if aspect_ratio:
            extra["aspect_ratio"] = aspect_ratio
        resp = await client.images.generate(
            model=eff, prompt=prompt, n=1, response_format="b64_json", extra_body=extra)
        return self._result(resp, eff)

    async def edit(self, prompt, image, *, aspect_ratio=None, image_size=None, model=None,
                   transparent=False, mask=None) -> ImageResult:
        # Not advertised in `capabilities`, so mediation never routes an edit here; defensive only.
        raise RuntimeError("Grok image generation can't edit — use Gemini or OpenAI")


# =================================================================== the single-source roster

@dataclass(frozen=True)
class RosterEntry:
    """One vendor's wiring — THE place its casual name, vendor, key, models, and roles are defined."""

    name: str                                    # casual name, used everywhere
    vendor: str                                  # vendor alias ("openai") — also accepted in requests
    key_field: str                               # Settings attr holding the api key
    director: Optional[type]                     # director LLM class, or None
    director_model_field: Optional[str]
    image: Optional[type]                         # image-generator class, or None
    image_model_field: Optional[str]
    default_ops: tuple[str, ...]                  # ops this generator is the hard-coded default for


# Change a name/model/role here and it changes everywhere. Order matters: it sets default priority
# and the tie-break for transparency selection.
ROSTER: list[RosterEntry] = [
    RosterEntry("Claude", "anthropic", "anthropic_api_key", ClaudeLLM, "llm_model", None, None, ()),
    RosterEntry("Gemini", "google", "google_api_key", GeminiLLM, "gemini_model",
                GeminiImageGenerator, "image_model", ("generate", "edit", "outpaint", "skybox")),
    RosterEntry("Chat", "openai", "openai_api_key", OpenAILLM, "openai_director_model",
                OpenAIImageGenerator, "openai_image_model", ()),  # opt-in; default for transparency
    RosterEntry("Grok", "xai", "xai_api_key", GrokLLM, "xai_director_model",
                GrokImageGenerator, "xai_image_model", ()),       # opt-in; default for nothing
]


def vendor_for(name: str) -> Optional[str]:
    """The vendor alias for a casual name (e.g. 'Chat' → 'openai'), for display + request matching."""
    return next((e.vendor for e in ROSTER if e.name == name), None)


def build_roster(settings: Settings) -> tuple[dict[str, LLM], str]:
    """Return ``({casual name: director LLM}, default-active name)`` for every vendor whose key is set.

    Derived entirely from `ROSTER` — add a vendor there and it's instantly a director (and/or image
    generator) usable by voice and CLI, with mid-conversation switching, and no other code change."""
    roster: dict[str, LLM] = {}
    for e in ROSTER:
        if e.director and getattr(settings, e.key_field):
            roster[e.name] = e.director(e.name, getattr(settings, e.key_field),
                                        getattr(settings, e.director_model_field))
    pref = (settings.llm or "").strip().lower()
    active = next((e.name for e in ROSTER if e.name in roster and e.name.lower() == pref), "")
    return roster, active or next(iter(roster), "")


def build_image_generators(settings: Settings) -> dict[str, ImageGenerator]:
    """Return ``{casual name: image generator}`` for every image-capable vendor whose key is set —
    keyed by the same casual name as the director roster ("Gemini", "Chat")."""
    gens: dict[str, ImageGenerator] = {}
    for e in ROSTER:
        if e.image and getattr(settings, e.key_field):
            gens[e.name] = e.image(e.name, getattr(settings, e.key_field),
                                   getattr(settings, e.image_model_field))
    return gens


def _supports(gen: ImageGenerator, op: str) -> bool:
    caps = gen.capabilities
    if op == "skybox_from":  # turning an existing image into a skybox needs both
        return {"edit", "skybox"} <= caps.operations
    if op == "grounded_skybox":  # same high-res equirectangular generation as a plain skybox
        return "skybox" in caps.operations
    return op in caps.operations


def _resolve_name(registry: dict[str, ImageGenerator], requested: str) -> Optional[str]:
    """Match a request to a registry key by casual name OR vendor alias ('OpenAI' → 'Chat')."""
    req = requested.strip().lower()
    for e in ROSTER:
        if e.name in registry and req in (e.name.lower(), e.vendor.lower()):
            return e.name
    return next((n for n in registry if n.lower() == req), None)  # custom registries (e.g. tests)


def _who_can(registry: dict[str, ImageGenerator], op: str, *, transparent: bool = False) -> str:
    """A sentence naming the configured generators that *can* do what the requested one can't — so a
    refusal carries its own next step instead of leaving the caller to guess."""
    able = [n for n, g in registry.items()
            if _supports(g, op) and (g.capabilities.transparency if transparent else True)]
    what = "transparency" if transparent else op
    if not able:
        return f"No configured generator can do {what}."
    return f"Use {' or '.join(able)} for {what}."


def select_generator(registry: dict[str, ImageGenerator], op: str, *,
                     requested: Optional[str] = None,
                     transparent: bool = False) -> tuple[Optional[ImageGenerator], Optional[str]]:
    """Pick the image generator for `op` (mediation).

    - `requested` (a casual name or vendor alias, e.g. "OpenAI") is honored if present and capable,
      else a clear error explains why.
    - otherwise: transparency steers to the transparency-capable generator; else the generator whose
      `default_ops` includes `op` (per `ROSTER` order); else any capable one; else an error.
    Returns ``(generator, None)`` or ``(None, error_message)``."""
    def usable(g: ImageGenerator) -> bool:
        return _supports(g, op) and (g.capabilities.transparency if transparent else True)

    if requested is not None:
        match = _resolve_name(registry, requested)
        if match is None:
            have = ", ".join(registry) or "none"
            return None, f"image generator {requested!r} is not configured (available: {have})"
        gen = registry[match]
        if not _supports(gen, op):
            return None, (f"{match} can't {op} (it supports: "
                          f"{', '.join(sorted(gen.capabilities.operations))}). "
                          f"{_who_can(registry, op)}")
        if transparent and not gen.capabilities.transparency:
            # Name the way out. Both halves of the request are the user's — the generator AND the
            # transparency — and only one can be honored, so the caller has to choose. Saying only
            # "pick one that can" leaves the model to guess which, and the observed failure (2026-08-28)
            # was it not retrying at all.
            return None, (f"{match} can't produce transparency. "
                          f"{_who_can(registry, op, transparent=True)} "
                          f"Or drop transparent to keep {match}.")
        return gen, None

    candidates = [(e, registry[e.name]) for e in ROSTER if e.name in registry and usable(registry[e.name])]
    if not candidates:
        return None, f"no configured image generator supports {'transparency' if transparent else op}"
    if transparent:
        return candidates[0][1], None
    for entry, gen in candidates:
        if op in entry.default_ops:
            return gen, None
    return candidates[0][1], None
