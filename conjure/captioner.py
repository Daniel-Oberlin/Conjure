"""Captioner — image → a short text description, to backfill labels for assets that lack one.

Bare backfilled images have no prompt/title, so they show as "—" in search results and don't match
keyword/FTS. (Semantic vector search already finds them; captions add the *readable* + *keyword*
layer.) This is image→text vision — distinct from the text→image `ImageGenerator`s in llm.py.

Pluggable behind `Captioner`; default `GeminiCaptioner` (google-genai, the existing image provider).
`build_captioner(settings)` returns None when no provider/key is configured (the pass is then a no-op).
"""

from __future__ import annotations

import hashlib
import re
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

#: What a lead-in says when a model ignores "no preamble". Matched against the text BEFORE a colon,
#: so it only ever removes something that announced itself as an announcement.
_PREAMBLE_CUE = re.compile(
    r"\b(here\s+(is|are)|here's|sure|okay|certainly|phrase|caption|description|answer|response)\b",
    re.IGNORECASE)

#: The local backend's model when `caption_model` is empty or still holds a hosted default. Same
#: weights the local judge uses, so configuring both costs one download.
LOCAL_CAPTION_MODEL = "Qwen/Qwen3-VL-4B-Instruct"


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


class LocalCaptioner:
    """Caption via a local vision-language model — `torch` + `transformers`, no key and no network.

    Unlike the judge beside it this one does have to GENERATE (a caption is prose, not a letter), so it
    runs a short bounded decode. Greedy, never sampled: two runs over the same library must produce the
    same labels or a re-index silently rewrites search results.

    Why this backend exists, in the order the reasons actually matter:

    * **It is the last thing in the ingest path that leaves the machine.** Audio is local (Whisper),
      speech is local (Kokoro), embeddings are local (SigLIP) — every imported image was still being
      posted to Gemini for a label.
    * **Bulk backfill stops being rate-limited and per-image priced**, which is what a NAS-sized photo
      library needs (`backlogs/library.md`).
    * **It cannot decline.** A hosted model refusing an image leaves an asset unlabelled, and an
      unlabelled asset is invisible to keyword and FTS search — a silent hole rather than an error.

    Shares `_IMAGE_PROMPT` / `_SKYBOX_PROMPT` with the hosted backends, so switching provider changes
    who is asked and not what.
    """

    #: A caption is a short phrase for search. This is a generous ceiling on that, not a target — its
    #: real job is bounding a local model that decides to write an essay.
    MAX_NEW_TOKENS = 48

    def __init__(self, model_id: str, *, device: str = ""):
        self.name = model_id
        self._device = device
        self._model = None
        self._proc = None
        self._torch = None

    def _ensure(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        self._torch = torch
        if not self._device:
            self._device = "mps" if torch.backends.mps.is_available() else "cpu"
        dtype = torch.float32 if self._device == "cpu" else torch.bfloat16   # see LocalJudge on why
        model = AutoModelForImageTextToText.from_pretrained(self.name, dtype=dtype)
        self._model = model.to(self._device).eval()
        self._proc = AutoProcessor.from_pretrained(self.name)

    def _call(self, data: bytes, mime: str, skybox: bool) -> str:
        import io

        from PIL import Image as PILImage

        torch = self._torch
        pic = PILImage.open(io.BytesIO(data)).convert("RGB")
        messages = [{"role": "user", "content": [
            {"type": "image"},
            {"type": "text", "text": _SKYBOX_PROMPT if skybox else _IMAGE_PROMPT},
        ]}]
        chat = self._proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self._proc(text=[chat], images=[pic], return_tensors="pt")
        inputs = {k: (v.to(self._device) if hasattr(v, "to") else v) for k, v in inputs.items()}
        with torch.no_grad():
            out = self._model.generate(**inputs, max_new_tokens=self.MAX_NEW_TOKENS, do_sample=False)
        # Decode only what was ADDED — the prompt is echoed back in `out` and a caption made of the
        # instruction would index every asset under the same words.
        grown = out[0][inputs["input_ids"].shape[1]:]
        text = self._proc.tokenizer.decode(grown, skip_special_tokens=True)
        return clean_caption(text)

    async def caption(self, data: bytes, *, mime: str = "image/png", skybox: bool = False) -> str:
        import asyncio

        self._ensure()
        return await asyncio.to_thread(self._call, data, mime, skybox)


def clean_caption(text: str) -> str:
    """Trim a model's reply down to the bare phrase the prompt asked for.

    The hosted backends get away with `.strip().strip('"')` because they follow the "no preamble"
    instruction. A small local model often does not — it opens with "Here is a short phrase:", wraps
    the answer in quotes, or adds a closing full stop that becomes part of the indexed label. None of
    that is wrong enough to reject and all of it pollutes search, so it is stripped here rather than
    left for every caller to notice.
    """
    body = (text or "").strip()
    if not body:
        return ""
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    # "Here is a short phrase for search: a misty pine forest" — keep what follows the colon. Matched
    # on the SHAPE of a preamble, not its length: "a diner sign reading open all night: neon lettering"
    # is a caption with a colon in it, and a length guard cuts it in half.
    if ":" in first:
        head, _, tail = first.partition(":")
        if tail.strip() and _PREAMBLE_CUE.search(head):
            first = tail.strip()
    return first.strip().strip('"').strip("'").rstrip(".").strip()


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
        return None
    if backend == "local":
        # Nothing to be missing — no key, no network. A bad model id fails loudly at first use.
        model = (getattr(settings, "caption_model", "") or "").strip()
        if not model or model.startswith("gemini"):   # the default belongs to the other backend
            model = LOCAL_CAPTION_MODEL
        return LocalCaptioner(model, device=getattr(settings, "caption_device", "") or "")
    # (openai / claude vision could slot in here behind the same interface)
    return None
