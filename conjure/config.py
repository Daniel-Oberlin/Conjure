"""Configuration & secrets loading.

Reads a git-ignored `.env` (see `.env.example`) and exposes a `Settings` object. Provider
selection (STT/TTS/LLM) is config-driven so models stay swappable (decision #1, docs/providers.md).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# The in-project cache — now only a MIGRATION INPUT (docs/user-home-plan.md §6): on startup its
# contents are relocated into the resolved user home. Kept as a constant so the migration can find it.
PROJECT_CACHE = ROOT / ".cache"

# The default logged-in user when none is specified (--user / the /tunnel/<user> route).
# No security — users are identity only (docs/specs/spaces.md).
DEFAULT_USER = "daniel"

# ---------------------------------------------------------------------------
# User home resolution (docs/user-home-plan.md §3/§4).
#
# Locations resolve highest-wins:  env var  >  settings.json  >  XDG default.
# By default the three roots follow the XDG Base Directory spec; if $CONJURE_HOME is set they
# consolidate under it ($CONJURE_HOME/{config,data,cache}) — a portable single-dir install and the
# handle tests use to relocate the whole home onto a tmpdir. Agent *definitions* resolve to a search
# PATH (user config dir first, then the bundled repo set), so a user can add or shadow agents.
# ---------------------------------------------------------------------------

BUNDLED_AGENTS_DIR = ROOT / "agents"    # example/bundled agent defs shipped with the repo (never moved)
BUNDLED_DYNAMICS_DIR = ROOT / "dynamics"  # bundled dynamic-module defs, sibling to agents/ (never moved)


def _home(env: Mapping[str, str]) -> Path | None:
    """The `$CONJURE_HOME` consolidation root, if set — else None (→ XDG roots)."""
    h = env.get("CONJURE_HOME", "").strip()
    return Path(h).expanduser() if h else None


def _xdg_root(env: Mapping[str, str], xdg_var: str, home_fallback: str) -> Path:
    """The base dir for one XDG category: `$<xdg_var>` if set, else `~/<home_fallback>`."""
    base = env.get(xdg_var, "").strip()
    return Path(base).expanduser() if base else Path.home() / home_fallback


def resolve_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where `settings.json` + the user's own agent defs live. Config can't itself be set *by*
    settings.json (chicken/egg), so only env + XDG feed it: env `CONJURE_CONFIG_DIR` > `$CONJURE_HOME/
    config` > `$XDG_CONFIG_HOME/conjure` (~/.config/conjure)."""
    env = os.environ if env is None else env
    explicit = env.get("CONJURE_CONFIG_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = _home(env)
    return (home / "config") if home else _xdg_root(env, "XDG_CONFIG_HOME", ".config") / "conjure"


def load_settings(config_dir: Path) -> dict:
    """Read `<config_dir>/settings.json` → dict; `{}` if absent or unreadable (never raises — a
    broken settings file must not stop the app from booting on defaults)."""
    path = config_dir / "settings.json"
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# The template written on first run. `null` for a path key means "use the resolved default" — the
# resolver treats a falsy value as absent (docs/user-home-plan.md §4), so a fresh file changes nothing;
# it exists purely so the keys are discoverable and a user can fill one in.
DEFAULT_SETTINGS: dict = {
    "data_dir": None,        # override the precious data root (sessions/worlds/assets/library.db)
    "cache_dir": None,       # override the disposable cache root
    "agents_path": None,     # list of dirs; user-first search path for agent definitions
    "dynamics_path": None,   # list of dirs; user-first search path for dynamic-module definitions
    "default_user": DEFAULT_USER,
}


def ensure_settings_file(config_dir: Path) -> Path:
    """Create `<config_dir>/settings.json` with the default template on first run; leave an existing
    one untouched. Idempotent. Returns the path. Called at app startup (not import) — it's the one
    place this module writes to the real home."""
    path = config_dir / "settings.json"
    if not path.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(DEFAULT_SETTINGS, indent=2) + "\n")
    return path


def _resolve_dir(env: Mapping[str, str], settings: Mapping, *, env_var: str, settings_key: str,
                 home: Path | None, home_sub: str, xdg_var: str, home_fallback: str) -> Path:
    """One data/cache root, highest-wins: env `<env_var>` > settings[`<settings_key>`] >
    `$CONJURE_HOME/<home_sub>` > `$<xdg_var>/conjure`."""
    explicit = env.get(env_var, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    from_settings = settings.get(settings_key)
    if from_settings:
        return Path(str(from_settings)).expanduser()
    return (home / home_sub) if home else _xdg_root(env, xdg_var, home_fallback) / "conjure"


def resolve_agents_path(env: Mapping[str, str], settings: Mapping, config_dir: Path) -> list[Path]:
    """The ordered agent-definition search path (docs/user-home-plan.md §5): env
    `CONJURE_AGENTS_PATH` (os-sep-separated) > settings["agents_path"] > [<config>/agents, bundled].
    User entries come first so a user agent shadows a bundled one of the same name."""
    explicit = env.get("CONJURE_AGENTS_PATH", "").strip()
    if explicit:
        return [Path(p).expanduser() for p in explicit.split(os.pathsep) if p]
    from_settings = settings.get("agents_path")
    if from_settings:
        return [Path(str(p)).expanduser() for p in from_settings]
    return [config_dir / "agents", BUNDLED_AGENTS_DIR]


def resolve_dynamics_path(env: Mapping[str, str], settings: Mapping, config_dir: Path) -> list[Path]:
    """The ordered dynamic-module search path — mirrors `resolve_agents_path` (docs/user-home-plan.md §5,
    docs/specs/dynamics.md §3): env `CONJURE_DYNAMICS_PATH` (os-sep-separated) >
    settings["dynamics_path"] > [<config>/dynamics, bundled]. User entries come first so a user module
    shadows a bundled one of the same name."""
    explicit = env.get("CONJURE_DYNAMICS_PATH", "").strip()
    if explicit:
        return [Path(p).expanduser() for p in explicit.split(os.pathsep) if p]
    from_settings = settings.get("dynamics_path")
    if from_settings:
        return [Path(str(p)).expanduser() for p in from_settings]
    return [config_dir / "dynamics", BUNDLED_DYNAMICS_DIR]


def resolve_paths(env: Mapping[str, str] | None = None, settings: Mapping | None = None) -> dict:
    """Resolve the whole user home in one shot → {config_dir, data_dir, cache_dir, agents_path,
    dynamics_path}. Pure over its `env`/`settings` inputs (defaults: process env + the on-disk settings
    file), so tests can drive it without touching the real home."""
    env = os.environ if env is None else env
    config_dir = resolve_config_dir(env)
    settings = load_settings(config_dir) if settings is None else settings
    home = _home(env)
    data_dir = _resolve_dir(env, settings, env_var="CONJURE_DATA_DIR", settings_key="data_dir",
                            home=home, home_sub="data", xdg_var="XDG_DATA_HOME", home_fallback=".local/share")
    cache_dir = _resolve_dir(env, settings, env_var="CONJURE_CACHE_DIR", settings_key="cache_dir",
                             home=home, home_sub="cache", xdg_var="XDG_CACHE_HOME", home_fallback=".cache")
    return {
        "config_dir": config_dir,
        "data_dir": data_dir,
        "cache_dir": cache_dir,
        "agents_path": resolve_agents_path(env, settings, config_dir),
        "dynamics_path": resolve_dynamics_path(env, settings, config_dir),
    }


# Import-time snapshot of the resolved home. Module-level constants (not just a resolver behind a
# function) so tests can monkeypatch them exactly like the legacy paths (docs/user-home-plan.md §4.1).
# NOTE: not yet consumed by the app — step 3 repoints the live paths onto these after migration.
_RESOLVED = resolve_paths()
CONFIG_DIR = _RESOLVED["config_dir"]
DATA_DIR = _RESOLVED["data_dir"]
CACHE_ROOT = _RESOLVED["cache_dir"]      # genuinely-disposable cache (NOT the precious data tree)
AGENTS_PATH: list[Path] = _RESOLVED["agents_path"]
DYNAMICS_PATH: list[Path] = _RESOLVED["dynamics_path"]

# The precious data tree now lives in the resolved home (post-migration). These names are kept because
# other modules import them (e.g. agent_server → USERS_DIR); they alias into DATA_DIR. `CACHE_DIR` is a
# historical alias for the DATA root (it is NOT the disposable cache — that's CACHE_ROOT).
CACHE_DIR = DATA_DIR
USERS_DIR = DATA_DIR / "users"
SESSION_PTR = DATA_DIR / "_session.txt"

VOID = "<void>"      # sentinel space for an OUTDOOR/void world — not tied to a captured room; it shows a
                     # skybox + placed objects, and the client derives its frame on the fly from live walls
                     # (RoomSnap.canonicalFrame) instead of a space. Lives HERE, not in server.py, because
                     # it travels in `/state` (`_live_state`) and so is read by peripherals that must not
                     # import the world server.


def scope_for(user: str, agent: str) -> str:
    """The capability scope a (user, agent) pair operates under: `<user>/agents/<agent>`
    (docs/specs/spaces.md §3). Injected by the runtime, never an LLM argument."""
    return f"{user}/agents/{agent}"


def agent_of(scope: str) -> str:
    """The agent segment of a capability scope `<user>/agents/<agent>` — the HARD asset boundary: an
    agent only ever accesses assets whose scope has the SAME agent segment, regardless of
    public/private (a `builder` never sees `outdoor` assets, and vice-versa). Falls back to the whole
    scope when there's no `/agents/` segment (legacy), so it can only match itself."""
    return scope.split("/agents/", 1)[1] if scope and "/agents/" in scope else (scope or "")


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
    agent_url: str = "http://localhost:8770"         # the agent server thin clients (cli/voice) talk to
                                                     # (docs/specs/agents.md §11); mirrors world_url one hop up
    gemini_model: str = "gemini-2.5-flash"           # Gemini director model
    openai_director_model: str = "gpt-4.1"           # OpenAI ("Chat") director model
    openai_image_model: str = "gpt-image-1"          # OpenAI image generator model
    # xAI ("Grok") — OpenAI-compatible, so it reuses the same SDK against api.x.ai (conjure.llm).
    xai_api_key: str | None = None                   # kept defaulted (unlike the older secrets above) so
    #                                                  positional Settings(...) in tests stay valid
    xai_director_model: str = "grok-4"               # Grok director model (grok-4.x / grok-3 also valid)
    xai_image_model: str = "grok-imagine-image-2.0"  # Grok image generator (generate-only)
    debug_log: bool = True                           # append client diagnostics to temp/conjure.log
    debug_registration: bool = False                 # co-location registration HUD + per-capture log (opt-in)
    debug_jitter: bool = False                        # frame-pacing/jitter probes only (clean cost measurement)
    # Co-location robustness (two-headset GUEST tuning). Injected into the client as window.CONJURE_REG /
    # CONJURE_CAPTURE_MS; they govern how tolerantly a guest registers its own capture against the
    # authority's shared room. See conjure/__main__.py for the terminology + per-knob meaning.
    reg_min_cov: int = 4                             # min DISTINCT reference surfaces covered to accept a lock
    reg_min_cov_frac: float = 0.3                    # min fraction of the reference covered (0..1)
    reg_size_tol: float = 0.5                        # how much LARGER (m) a detected plane may be than a reference
    reg_inlier_m: float = 0.4                        # max distance (m) a plane may sit from a same-kind reference
    reg_yaw_peaks: int = 5                           # candidate room rotations tried when solving orientation
    capture_interval: float = 2.0                    # seconds between recaptures/re-registrations
    # Render apply-gate (docs/specs/spaces-geometry.md §9.1): a locally-rendered surface is only re-laid when
    # it moves past ONE of these tolerances — otherwise sub-tolerance re-derivation is skipped so the mesh
    # doesn't rebuild (the "pop"). Bigger = calmer (fewer updates, more lag to real change); smaller = snappier.
    apply_tol_pos: float = 0.02                      # metres a surface must move to re-lay it
    apply_tol_rot_deg: float = 1.0                   # degrees it must rotate to re-lay it
    apply_tol_ext: float = 0.02                      # metres its size/opening must change to re-lay it
    inset_standoff: float = 0.02                     # m a door/window/wall-art surface sits in front of its wall
    on_surface_standoff: float = 0.02                # m an on-surface image sits in front of its host surface
    surface_weld: float = 0.002                      # m added to a surface's FILL w/h (split per side) so
    #                                                  abutting fills overlap instead of leaving a float-rounding
    #                                                  crack ("noisy static" see-through). Edges/outline stay
    #                                                  true. Injected as window.CONJURE_SURFACE_WELD; 0 disables.
    wall_seal_tol: float = 0.15                      # m: seal a wall's top→ceiling / bottom→floor when the edge
    #                                                  is already within this of the plane (docs §9.1). The Quest
    #                                                  fits walls a few mm-cm short of the ceiling → an open slit
    #                                                  once fills are solid; this snaps the shell closed. Vertical
    #                                                  only (plane/width untouched). 0 disables. Injected as
    #                                                  window.CONJURE_WALL_SEAL_TOL.
    foveation: float = 0.0                           # 0..1 fixed-foveated-rendering level, applied at runtime over
    #                                                  index.html's foveationLevel. Higher = periphery drawn at
    #                                                  lower res = less GPU (fewer dropped frames while walking)
    #                                                  at the cost of peripheral sharpness; 0 = full-res (today's
    #                                                  default). Injected as window.CONJURE_FOVEATION.
    history_cap: int = 40                            # max transcript TURNS (user/assistant entries) sent to the
    #                                                  LLM each turn; older turns are dropped from the MODEL's
    #                                                  view (still persisted + replayed to clients) to keep
    #                                                  tool-calling reliable as a session grows — context bloat
    #                                                  degrades tool use well before the window fills. 0 =
    #                                                  unlimited. Agent-server side; --history-cap / CONJURE_HISTORY_CAP.
    occlusion: str = "off"                           # real-world depth occlusion: "off" | "hands" | "hands-solid"
    #                                                  (docs/specs/occlusion.md). A depth pre-pass
    #                                                  writes real-world depth (color-write off) so virtual content is
    #                                                  hidden where a nearer real surface is — e.g. your hand covers a
    #                                                  virtual wall. off = today (virtual always over passthrough);
    #                                                  hands = tracked-hand occluders only (sharp, cheap); full =
    #                                                  environment depth (walls/furniture/people, coarse). Injected as
    #                                                  window.CONJURE_OCCLUSION.
    pose_tau: float = 0.0                            # s: pose-smoothing time constant (docs/specs/spaces-geometry.md §9.2).
    #                                                  Per-surface SLEW eases each surface toward its newly-captured
    #                                                  pose over ~3·tau instead of snapping, turning the ~2 s drift
    #                                                  STEP into a short settle. 0 (default) disables → snap as today.
    #                                                  Injected as window.CONJURE_POSE_TAU; A/B like --geo-slice-ms.
    geo_slice_ms: float = 3.0                        # per-frame budget (ms) for the time-sliced mesh-rebuild
    #                                                  pump (docs/specs/spaces-geometry.md §9): a whole-room
    #                                                  re-triangulation is spread across frames so it never
    #                                                  drops one. Injected as window.CONJURE_GEO_SLICE_MS;
    #                                                  <=0 disables slicing (rebuild all inline each frame).
    # Wall identity by plane (docs/specs/spaces-geometry.md §4.2/§4.3) — how tolerantly matchWall calls two
    # captures the SAME wall. Injected as window.CONJURE_WALL. Loosen for two headsets that scan a wall
    # differently; tighten to demand a stronger match (a wrong wall merge puts content on the wrong wall).
    wall_perp_tol: float = 0.15                      # max plane-offset gap (m) to call two walls one plane
    wall_yaw_tol_deg: float = 30.0                   # max normal-yaw difference (°) for a wall match
    wall_overlap_slop: float = 0.3                   # max along-line gap (m) between spans and still one wall
    group_surface_relay: bool = True                 # re-lay ALL real surfaces together when any crosses
    #                                                  tolerance, so wall↔floor/ceiling junctions and
    #                                                  inset↔cutout share one render epoch and don't drift
    #                                                  apart (the seam bug); off = per-surface (independent)
    # TEST override for the client's reported geolocation (--force-geo). "zero" pins you at (0,0) — a
    # convenient "somewhere else"; "/<user>/spaces/<name>" pins you at that space's stored location.
    # Empty (default) ⇒ use the real browser/headset location. See server._forced_geo.
    force_geo: str | None = None
    # TEST override (--drop-surface): the client pretends it DIDN'T capture surfaces matching this
    # semantic ("wall art") or id substring — kept in the posted seed, omitted from the local render — so
    # the missing-surface recovery (docs/specs/spaces-geometry.md §6) can be exercised with one headset.
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
    # Controller pointer beams (see docs/specs/dynamics.md §6): a laser from each controller,
    # shown while you're pointing/interacting (e.g. rippling a Water Picture) and hidden otherwise. The beam
    # arms when the trigger is pulled past `beam_trigger` (analog 0..1) and LINGERS for `beam_timeout`
    # seconds after the most recent pull, so a momentary release mid-interaction doesn't flicker it off.
    # Injected as window.CONJURE_BEAM_MS (ms) / CONJURE_BEAM_TRIGGER. beam_timeout=0 disables the linger
    # (beam shows only while the trigger is held past threshold); the config is the single source of truth
    # for the duration — it is never hard-coded in the client.
    beam_timeout: float = 10.0
    beam_trigger: float = 0.05
    # Control → ACTION bindings for XR interaction (client/conjure-pointers.js). Modules declare which
    # ACTIONS they use (module.json `actions`) and never name a button, so the control scheme lives here in
    # one place instead of being hard-coded across modules. Controls: trigger | grip | a | b | stickPress |
    # stickX | stickY. Injected as window.CONJURE_BINDINGS; override with CONJURE_BINDINGS as JSON.
    #   select — primary "interact with content" (e.g. rippling a Water Picture)
    #   grab   — pick up / move an object          resize — scale it by a corner handle
    #   reel   — axis: push/pull a held object along the beam
    #   yaw/pitch/bank — axes: turn a held MODEL. A control may name a hand ("left.stickY"),
    #            so one hand can hold an object while the other shapes it. Pitch and bank are
    #            VIEWER-relative (tip away from you / roll as you see it): nothing in a glTF
    #            records which way a model faces, so its own axes can't define them.
    bindings: str = ('{"select":"trigger","grab":"grip","resize":"trigger","reel":"right.stickY",'
                     '"yaw":"right.stickX","pitch":"left.stickY","bank":"left.stickX"}')


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
        agent_url=os.environ.get("CONJURE_AGENT_URL", "http://localhost:8770"),
        gemini_model=os.environ.get("CONJURE_GEMINI_MODEL", "gemini-2.5-flash"),
        openai_director_model=os.environ.get("CONJURE_OPENAI_DIRECTOR_MODEL", "gpt-4.1"),
        openai_image_model=os.environ.get("CONJURE_OPENAI_IMAGE_MODEL", "gpt-image-1"),
        xai_api_key=os.environ.get("XAI_API_KEY") or None,
        xai_director_model=os.environ.get("CONJURE_XAI_DIRECTOR_MODEL", "grok-4"),
        xai_image_model=os.environ.get("CONJURE_XAI_IMAGE_MODEL", "grok-imagine-image-2.0"),
        debug_log=os.environ.get("CONJURE_DEBUG_LOG", "1").strip().lower() not in ("0", "false", "no", "off"),
        debug_registration=os.environ.get("CONJURE_DEBUG_REGISTRATION", "").strip().lower() in ("1", "true", "yes", "on"),
        debug_jitter=os.environ.get("CONJURE_DEBUG_JITTER", "").strip().lower() in ("1", "true", "yes", "on"),
        reg_min_cov=int(os.environ.get("CONJURE_REG_MIN_COV", "4")),
        reg_min_cov_frac=float(os.environ.get("CONJURE_REG_MIN_COV_FRAC", "0.3")),
        reg_size_tol=float(os.environ.get("CONJURE_REG_SIZE_TOL", "0.5")),
        reg_inlier_m=float(os.environ.get("CONJURE_REG_INLIER_M", "0.4")),
        reg_yaw_peaks=int(os.environ.get("CONJURE_REG_YAW_PEAKS", "5")),
        capture_interval=float(os.environ.get("CONJURE_CAPTURE_INTERVAL", "2.0")),
        apply_tol_pos=float(os.environ.get("CONJURE_APPLY_TOL_POS", "0.02")),
        apply_tol_rot_deg=float(os.environ.get("CONJURE_APPLY_TOL_ROT_DEG", "1.0")),
        apply_tol_ext=float(os.environ.get("CONJURE_APPLY_TOL_EXT", "0.02")),
        inset_standoff=float(os.environ.get("CONJURE_INSET_STANDOFF", "0.02")),
        foveation=float(os.environ.get("CONJURE_FOVEATION", "0.0")),
        occlusion=(os.environ.get("CONJURE_OCCLUSION", "off").strip().lower() or "off"),
        history_cap=int(os.environ.get("CONJURE_HISTORY_CAP", "40")),
        pose_tau=float(os.environ.get("CONJURE_POSE_TAU", "0.0")),
        geo_slice_ms=float(os.environ.get("CONJURE_GEO_SLICE_MS", "3.0")),
        on_surface_standoff=float(os.environ.get("CONJURE_ON_SURFACE_STANDOFF", "0.02")),
        surface_weld=float(os.environ.get("CONJURE_SURFACE_WELD", "0.002")),
        wall_seal_tol=float(os.environ.get("CONJURE_WALL_SEAL_TOL", "0.15")),
        wall_perp_tol=float(os.environ.get("CONJURE_WALL_PERP_TOL", "0.15")),
        wall_yaw_tol_deg=float(os.environ.get("CONJURE_WALL_YAW_TOL_DEG", "30.0")),
        wall_overlap_slop=float(os.environ.get("CONJURE_WALL_OVERLAP_SLOP", "0.3")),
        group_surface_relay=(os.environ.get("CONJURE_GROUP_SURFACE_RELAY", "1") != "0"),
        force_geo=(os.environ.get("CONJURE_FORCE_GEO", "").strip() or None),
        drop_surface=(os.environ.get("CONJURE_DROP_SURFACE", "").strip() or None),
        force_occupied=os.environ.get("CONJURE_FORCE_OCCUPIED", "").strip().lower() in ("1", "true", "yes", "on"),
        embed_backend=os.environ.get("CONJURE_EMBED_BACKEND", "auto"),
        embed_model=os.environ.get("CONJURE_EMBED_MODEL", "google/siglip2-so400m-patch14-384"),
        caption_provider=os.environ.get("CONJURE_CAPTION_PROVIDER", "gemini"),
        caption_model=os.environ.get("CONJURE_CAPTION_MODEL", "gemini-2.5-flash"),
        beam_timeout=float(os.environ.get("CONJURE_BEAM_TIMEOUT", "10.0")),
        beam_trigger=float(os.environ.get("CONJURE_BEAM_TRIGGER", "0.05")),
        bindings=os.environ.get(
            "CONJURE_BINDINGS", '{"select":"trigger","grab":"grip","resize":"trigger","reel":"right.stickY",'
            '"yaw":"right.stickX","pitch":"left.stickY","bank":"left.stickX"}'),
    )
