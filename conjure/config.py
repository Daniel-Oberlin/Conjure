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
    # secrets
    anthropic_api_key: str | None
    poly_pizza_api_key: str | None
    openai_api_key: str | None
    google_api_key: str | None
    # server / connectivity
    host: str
    port: int
    world_url: str


def get_settings() -> Settings:
    load_env()
    return Settings(
        stt=os.environ.get("CONJURE_STT", "whisper"),
        tts=os.environ.get("CONJURE_TTS", "kokoro"),
        llm=os.environ.get("CONJURE_LLM", "claude"),
        llm_model=os.environ.get("CONJURE_LLM_MODEL", "claude-sonnet-4-6"),
        image_provider=os.environ.get("CONJURE_IMAGE_PROVIDER", "gemini"),
        image_model=os.environ.get("CONJURE_IMAGE_MODEL", "gemini-2.5-flash-image"),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        poly_pizza_api_key=os.environ.get("POLY_PIZZA_API_KEY") or None,
        openai_api_key=os.environ.get("OPENAI_API_KEY") or None,
        google_api_key=os.environ.get("GOOGLE_API_KEY") or None,
        host=os.environ.get("CONJURE_HOST", "0.0.0.0"),
        port=int(os.environ.get("CONJURE_PORT", "8080")),
        world_url=os.environ.get("CONJURE_URL", "http://localhost:8080"),
    )
