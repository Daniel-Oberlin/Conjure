"""Modular image generation — a pluggable generator registry (decision #1 provider abstraction).

Add a generator by implementing the ImageGenerator protocol and registering a factory with
``@register("name")``; it's selected via ``CONJURE_IMAGE_PROVIDER``. New generators (OpenAI, FLUX,
local Stable Diffusion, …) plug in without touching callers.

First plugin: **Google Gemini ("Nano Banana")** — chosen for best-in-class conversational editing
and layout-aware outpainting (relevant to the vision's skybox/panorama work). ``generate_content``
also gives us an editing path later (image-in → image-out).
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .config import Settings


@dataclass
class ImageResult:
    data: bytes
    mime_type: str
    provider: str
    model: str


class ImageGenerator(Protocol):
    name: str
    model: str

    async def generate(self, prompt: str, *, aspect_ratio: Optional[str] = None,
                       image_size: Optional[str] = None, model: Optional[str] = None) -> ImageResult: ...
    async def edit(self, prompt: str, image: bytes, *, aspect_ratio: Optional[str] = None,
                   image_size: Optional[str] = None, model: Optional[str] = None) -> ImageResult: ...


_FACTORIES: dict[str, Callable[[Settings], "Optional[ImageGenerator]"]] = {}


def register(name: str):
    def deco(factory: Callable[[Settings], "Optional[ImageGenerator]"]):
        _FACTORIES[name] = factory
        return factory

    return deco


def available_providers() -> list[str]:
    return sorted(_FACTORIES)


def get_image_generator(settings: Settings) -> "Optional[ImageGenerator]":
    """Return the configured image generator, or None if unavailable (e.g. no key)."""
    factory = _FACTORIES.get(settings.image_provider)
    return factory(settings) if factory else None


# --- Gemini ("Nano Banana") --------------------------------------------------------------------

class GeminiImageGenerator:
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        self.model = model
        self._api_key = api_key

    async def generate(self, prompt: str, *, aspect_ratio: Optional[str] = None,
                       image_size: Optional[str] = None, model: Optional[str] = None) -> ImageResult:
        # google-genai's call is sync; run it off the event loop.
        return await asyncio.to_thread(self._call, [prompt], aspect_ratio, image_size, model)

    async def edit(self, prompt: str, image: bytes, *, aspect_ratio: Optional[str] = None,
                   image_size: Optional[str] = None, model: Optional[str] = None) -> ImageResult:
        return await asyncio.to_thread(self._call, [image, prompt], aspect_ratio, image_size, model)

    def _call(self, parts: list, aspect_ratio: Optional[str], image_size: Optional[str],
              model: Optional[str]) -> ImageResult:
        """parts: list of prompt str and/or raw image bytes (bytes are sent as an image part)."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
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
                            data=data,
                            mime_type=blob.mime_type or "image/png",
                            provider=self.name,
                            model=effective_model,
                        )
            if last_reason not in (None, types.FinishReason.STOP):
                break
        raise RuntimeError(f"Gemini returned no image part (finish_reason={last_reason})")


@register("gemini")
def _make_gemini(settings: Settings) -> "Optional[ImageGenerator]":
    if not settings.google_api_key:
        return None
    return GeminiImageGenerator(settings.google_api_key, settings.image_model)
