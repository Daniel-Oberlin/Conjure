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

# The default logged-in user when none is specified (--user / the /tunnel/<user> route).
# No security — users are identity only (docs/spaces-and-users-plan.md).
DEFAULT_USER = "daniel"


def scope_for(user: str, agent: str) -> str:
    """The capability scope a (user, agent) pair operates under: `<user>/agents/<agent>`
    (docs/spaces-and-users-plan.md §3). Injected by the runtime, never an LLM argument."""
    return f"{user}/agents/{agent}"


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
    debug_registration: bool = False                 # co-location registration HUD + per-capture log (opt-in)
    # Co-location robustness (two-headset GUEST tuning). Injected into the client as window.CONJURE_REG /
    # CONJURE_CAPTURE_MS; they govern how tolerantly a guest registers its own capture against the
    # authority's shared room. See conjure/__main__.py for the terminology + per-knob meaning.
    reg_min_cov: int = 4                             # min DISTINCT reference surfaces covered to accept a lock
    reg_min_cov_frac: float = 0.3                    # min fraction of the reference covered (0..1)
    reg_size_tol: float = 0.5                        # how much LARGER (m) a detected plane may be than a reference
    reg_inlier_m: float = 0.4                        # max distance (m) a plane may sit from a same-kind reference
    reg_yaw_peaks: int = 5                           # candidate room rotations tried when solving orientation
    capture_interval: float = 2.0                    # seconds between recaptures/re-registrations
    # Render apply-gate (docs/local-first-geometry.md §4-6): a locally-rendered surface is only re-laid when
    # it moves past ONE of these tolerances — otherwise sub-tolerance re-derivation is skipped so the mesh
    # doesn't rebuild (the "pop"). Bigger = calmer (fewer updates, more lag to real change); smaller = snappier.
    apply_tol_pos: float = 0.02                      # metres a surface must move to re-lay it
    apply_tol_rot_deg: float = 1.0                   # degrees it must rotate to re-lay it
    apply_tol_ext: float = 0.02                      # metres its size/opening must change to re-lay it
    group_wall_relay: bool = True                    # re-lay ALL walls together when any crosses tolerance,
    #                                                  so corner-joined walls share one epoch and don't drift
    #                                                  apart (the corner-seam bug); off = per-wall (independent)
    # TEST override for the client's reported geolocation (--force-geo). "zero" pins you at (0,0) — a
    # convenient "somewhere else"; "/<user>/spaces/<name>" pins you at that space's stored location.
    # Empty (default) ⇒ use the real browser/headset location. See server._forced_geo.
    force_geo: str | None = None
    # TEST override (--drop-surface): the client pretends it DIDN'T capture surfaces matching this
    # semantic ("wall art") or id substring — kept in the posted seed, omitted from the local render — so
    # the missing-surface recovery (docs/local-first-geometry.md §5.2) can be exercised with one headset.
    drop_surface: str | None = None
    # TEST override for space occupancy (--force-occupied): treat the active space as already CLAIMED by a
    # phantom AR holder, so the admission gate engages for a SINGLE headset (match the active space ⇒
    # admitted; anything else ⇒ refused). See server._occupied.
    force_occupied: bool = False
    # Asset-library embeddings (docs/asset-library-plan.md §4). "auto" uses local SigLIP when the
    # optional torch/transformers are installed, else stays off; "fake"/"none" for tests/disable.
    embed_backend: str = "auto"
    embed_model: str = "google/siglip2-so400m-patch14-384"
    # Caption backfill for assets with no label (docs/asset-library-plan.md §12). Gemini multimodal by
    # default; "none"/"fake" to disable/test.
    caption_provider: str = "gemini"
    caption_model: str = "gemini-2.5-flash"


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
        debug_registration=os.environ.get("CONJURE_DEBUG_REGISTRATION", "").strip().lower() in ("1", "true", "yes", "on"),
        reg_min_cov=int(os.environ.get("CONJURE_REG_MIN_COV", "4")),
        reg_min_cov_frac=float(os.environ.get("CONJURE_REG_MIN_COV_FRAC", "0.3")),
        reg_size_tol=float(os.environ.get("CONJURE_REG_SIZE_TOL", "0.5")),
        reg_inlier_m=float(os.environ.get("CONJURE_REG_INLIER_M", "0.4")),
        reg_yaw_peaks=int(os.environ.get("CONJURE_REG_YAW_PEAKS", "5")),
        capture_interval=float(os.environ.get("CONJURE_CAPTURE_INTERVAL", "2.0")),
        apply_tol_pos=float(os.environ.get("CONJURE_APPLY_TOL_POS", "0.02")),
        apply_tol_rot_deg=float(os.environ.get("CONJURE_APPLY_TOL_ROT_DEG", "1.0")),
        apply_tol_ext=float(os.environ.get("CONJURE_APPLY_TOL_EXT", "0.02")),
        group_wall_relay=(os.environ.get("CONJURE_GROUP_WALL_RELAY", "1") != "0"),
        force_geo=(os.environ.get("CONJURE_FORCE_GEO", "").strip() or None),
        drop_surface=(os.environ.get("CONJURE_DROP_SURFACE", "").strip() or None),
        force_occupied=os.environ.get("CONJURE_FORCE_OCCUPIED", "").strip().lower() in ("1", "true", "yes", "on"),
        embed_backend=os.environ.get("CONJURE_EMBED_BACKEND", "auto"),
        embed_model=os.environ.get("CONJURE_EMBED_MODEL", "google/siglip2-so400m-patch14-384"),
        caption_provider=os.environ.get("CONJURE_CAPTION_PROVIDER", "gemini"),
        caption_model=os.environ.get("CONJURE_CAPTION_MODEL", "gemini-2.5-flash"),
    )
