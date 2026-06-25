"""Captioner — image → a short text description, to backfill labels for assets that lack one.

Bare backfilled images have no prompt/title, so they show as "—" in search results and don't match
keyword/FTS. (Semantic vector search already finds them; captions add the *readable* + *keyword*
layer.) This is image→text vision — distinct from the text→image `ImageGenerator`s in llm.py.

Pluggable behind `Captioner`; default `GeminiCaptioner` (google-genai, the existing image provider).
`build_captioner(settings)` returns None when no provider/key is configured (the pass is then a no-op).
"""

from __future__ import annotations

import hashlib
from typing import Optional, Protocol, runtime_checkable

# Concise, search-oriented prompts. Objects/scenes name subject+setting+style; skyboxes describe the
# 360° scene (setting, time of day, mood) since they read differently from a framed image.
_IMAGE_PROMPT = (
    "Describe this image in one short phrase (a few words) for search — name the main subject, "
    "setting, and style. Reply with only the phrase, no preamble or trailing punctuation."
)
_SKYBOX_PROMPT = (
    "Describe this 360° panorama in one short phrase for search — its setting, time of day, and mood "
    "(e.g. 'a misty pine forest at dawn'). Reply with only the phrase, no preamble."
)


@runtime_checkable
class Captioner(Protocol):
    name: str
    async def caption(self, data: bytes, *, mime: str = "image/png", skybox: bool = False) -> str: ...


class FakeCaptioner:
    """Deterministic, dependency-free captioner for tests."""

    def __init__(self):
        self.name = "fake"

    async def caption(self, data: bytes, *, mime: str = "image/png", skybox: bool = False) -> str:
        return f"{'skybox' if skybox else 'image'} {hashlib.sha256(data or b'').hexdigest()[:8]}"


class GeminiCaptioner:
    """Caption via Gemini multimodal (google-genai). One async call per image."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        self.name = model
        self._api_key = api_key

    async def caption(self, data: bytes, *, mime: str = "image/png", skybox: bool = False) -> str:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        resp = await client.aio.models.generate_content(
            model=self.name,
            contents=[types.Part.from_bytes(data=data, mime_type=mime),
                      types.Part(text=_SKYBOX_PROMPT if skybox else _IMAGE_PROMPT)],
        )
        return (resp.text or "").strip().strip('"').strip()


def build_captioner(settings) -> Optional[Captioner]:
    """Pick the captioner backend from config, or None if it can't run (the backfill is then a no-op)."""
    backend = (getattr(settings, "caption_provider", "gemini") or "gemini").strip().lower()
    if backend in ("none", "off", ""):
        return None
    if backend == "fake":
        return FakeCaptioner()
    if backend == "gemini":
        key = getattr(settings, "google_api_key", None)
        if key:
            return GeminiCaptioner(key, getattr(settings, "caption_model", "gemini-2.5-flash"))
        print("[conjure] caption_provider=gemini but GOOGLE_API_KEY not set — captioning off")
    # (openai / claude vision could slot in here behind the same interface)
    return None
