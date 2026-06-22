"""Configuration & secrets loading.

Reads a git-ignored `.env` (see `.env.example`) and exposes a `Settings` object. Provider
selection (STT/TTS/LLM) is config-driven so models stay swappable (decision #1, docs/providers.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    """Load `.env` from the repo root into the process environment, if present."""
    env_path = ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)


@dataclass(frozen=True)
class Settings:
    # provider selection (see docs/providers.md)
    stt: str
    tts: str
    llm: str
    llm_model: str
    image_provider: str
    image_model: str
    skybox_model: str
    skybox_size: str
    # secrets
    anthropic_api_key: str | None
    poly_pizza_api_key: str | None
    openai_api_key: str | None
    google_api_key: str | None
    # server / connectivity
    host: str
    port: int
    world_url: str
    # Per-vendor director/image models for the roster members beyond Claude. Trailing + defaulted so
    # existing Settings(...) constructions stay valid. (Wired in conjure.llm's ROSTER table.)
    gemini_model: str = "gemini-2.5-flash"           # Gemini director model
    openai_director_model: str = "gpt-4.1"           # OpenAI ("Chat") director model
    openai_image_model: str = "gpt-image-1"          # OpenAI image generator model
    debug_log: bool = True                           # append client diagnostics to temp/conjure.log
    # Asset-library embeddings (docs/asset-library-plan.md §4). "auto" uses local SigLIP when the
    # optional torch/transformers are installed, else stays off; "fake"/"none" for tests/disable.
    embed_backend: str = "auto"
    embed_model: str = "google/siglip2-so400m-patch14-384"


def get_settings() -> Settings:
    load_env()
    return Settings(
        stt=os.environ.get("CONJURE_STT", "whisper"),
        tts=os.environ.get("CONJURE_TTS", "kokoro"),
        llm=os.environ.get("CONJURE_LLM", "claude"),
        llm_model=os.environ.get("CONJURE_LLM_MODEL", "claude-sonnet-4-6"),
        image_provider=os.environ.get("CONJURE_IMAGE_PROVIDER", "gemini"),
        image_model=os.environ.get("CONJURE_IMAGE_MODEL", "gemini-2.5-flash-image"),
        # Skyboxes wrap the whole view, so they use a higher-res model (Nano Banana Pro @ 4K).
        skybox_model=os.environ.get("CONJURE_SKYBOX_MODEL", "gemini-3-pro-image"),
        skybox_size=os.environ.get("CONJURE_SKYBOX_SIZE", "4K"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        poly_pizza_api_key=os.environ.get("POLY_PIZZA_API_KEY") or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        google_api_key=os.environ.get("GOOGLE_API_KEY") or None,
        host=os.environ.get("CONJURE_HOST", "0.0.0.0"),
        port=int(os.environ.get("CONJURE_PORT", "8080")),
        world_url=os.environ.get("CONJURE_URL", "http://localhost:8080"),
        gemini_model=os.environ.get("CONJURE_GEMINI_MODEL", "gemini-2.5-flash"),
        openai_director_model=os.environ.get("CONJURE_OPENAI_DIRECTOR_MODEL", "gpt-4.1"),
        openai_image_model=os.environ.get("CONJURE_OPENAI_IMAGE_MODEL", "gpt-image-1"),
        debug_log=os.environ.get("CONJURE_DEBUG_LOG", "1").strip().lower() not in ("0", "false", "no", "off"),
        embed_backend=os.environ.get("CONJURE_EMBED_BACKEND", "auto"),
        embed_model=os.environ.get("CONJURE_EMBED_MODEL", "google/siglip2-so400m-patch14-384"),
    )
