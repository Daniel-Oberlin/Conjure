"""World server — serves the WebXR client, holds the world, broadcasts patches.

Phase-0 surface (architecture.md §3, §8):
  GET  /            -> the A-Frame client
  GET  /world       -> current world document (debug)
  POST /patch       -> apply a hand-authored patch, broadcast to all clients
  WS   /ws          -> state channel: sends a snapshot on connect, then patches

The director / MCP server / behavior runtime are not wired up yet — this is the bare
state loop so we can get a scene onto the Quest and drive it with patches.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import NamedTuple, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .assets import AssetResolver
from .captioner import build_captioner
from . import dynamics as dynamics_loader
from .agents import load_agent, resolve_agent_dir
from .config import (CACHE_ROOT, CONFIG_DIR, DATA_DIR, DEFAULT_USER, PROJECT_CACHE, VOID, agent_of,
                     ensure_settings_file, get_settings, scope_for)
from .embeddings import build_embedder
from .figures import (AIM_DIRECTIONS, FRAME_REV, FRAME_VECTORS, POSE_AXES, TRUNK_BONES,
                      resolve_pose)
from .library import AssetLibrary
from .llm import build_image_generators, select_generator, vendor_for
from .plane_anchor import author_anchor, solve_anchor
from .schema import Patch
from . import namespace
from .world import (_MRU_CAP, MIGRATED_SID, SessionRepository, SpaceStore, WorldRepository, WorldStore,
                    NAME_SEGMENT, _set_path, clean_name, fold_accents, migrate_cache_to_users,
                    migrate_env_room_to_space_presentation,
                    migrate_project_cache_to_home, migrate_worlds_to_ids, new_world_id)

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "client"
LOG_FILE = ROOT / "temp" / "conjure.log"   # client diagnostics (gated by settings.debug_log)
GEO_LOG_DIR = ROOT / "temp"                # geometry event log: temp/geometry-YYYY-MM-DD.jsonl (rotated)
SAMPLE_WORLD = ROOT / "examples" / "sample_world.json"
# The precious DATA root — the resolved user home (docs/specs/config.md §1), NOT the in-project
# .cache anymore. On startup the in-project .cache is migrated into here (see _init_state).
CACHE = DATA_DIR
# User-first tree (docs/specs/agents.md §7.1): everything a user owns lives under <data>/users/<user>/ —
# their agents' sessions (worlds/state/transcript) AND their spaces. Worlds:
#   <data>/users/<user>/agents/<agent>/sessions/<id>/worlds/<name>.json
# Spaces:  <data>/users/<user>/spaces/<name>.json
USERS_DIR = CACHE / "users"
# Pre-session locations, kept only as MIGRATION INPUTS (relocated once into USERS_DIR on boot).
WORLDS_DIR = CACHE / "worlds"                  # legacy world tree (pre-user step also ran here)
SPACES_DIR = CACHE / "spaces"                  # legacy space tree
# The single global session pointer — what's live across the WHOLE server (scope<TAB>session-id).
SESSION_PTR = CACHE / "_session.txt"
ASSET_CACHE = CACHE / "assets"                 # created in _init_state (after migration), not at import
LIBRARY_DB = CACHE / "library.db"             # durable asset catalog (docs/specs/library.md) — DATA
# The scope new assets/worlds are written under: <user>/agents/<agent> (docs/specs/spaces.md
# §3). A data seam for now — single user/agent, no enforcement yet; the builder is the only writer.
DEFAULT_SCOPE = scope_for(DEFAULT_USER, "builder")
# scripts/tunnel.sh (an external shell script) writes the current cloudflared URL here; /tunnel redirects
# to it (a short, fixed LAN address you can type on the Quest instead of the long random trycloudflare URL
# each session). Ephemeral dev-tooling scratch → the disposable CACHE_ROOT (the user-home cache), NOT the
# in-project .cache and NOT the precious DATA tree (docs/specs/config.md §7). The script resolves the
# same CACHE_ROOT from config, so both ends agree on one location.
TUNNEL_FILE = CACHE_ROOT / "tunnel_url"
MEDIA_TYPES = {".glb": "model/gltf-binary", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}

def _has_alpha(im) -> bool:
    """True if an opened PIL image carries real (non-opaque) transparency."""
    if im.mode in ("RGBA", "LA"):
        return im.getchannel("A").getextrema()[0] < 255   # some pixel non-opaque
    return "transparency" in im.info


# World constructor: a per-agent macro of ordinary server operations, run once at world *creation*
# (docs/specs/agents.md §7.5). Each command maps to the same env/patch effect the director's tools
# produce; the set grows as constructors need more. The builder shows real-room edges by default; the
# future dungeonmaster turns them off — same mechanism, different agent.json.
_WORLD_COMMANDS = {
    "show_edges": lambda a: [{"op": "env", "set": {"spacePresentation.edgesVisible": bool(a.get("on", True))}}],
    "show_annotations": lambda a: [{"op": "env", "set": {"spacePresentation.annotations": bool(a.get("on", False))}}],
    "set_sky_color": lambda a: [{"op": "env", "set": {"sky": {"color": a.get("color", "#000000")}}}],
}


def _agent_block(scope: str, key: str) -> dict:
    """A named top-level block (`world`/`session`) from agents/<agent>/agent.json (agent = the scope's
    last segment). Missing/unreadable → {}."""
    agent = (scope or "").rsplit("/", 1)[-1]
    try:
        agent_dir = resolve_agent_dir(agent)             # search path: user defs shadow bundled (§5)
        return json.loads((agent_dir / "agent.json").read_text()).get(key) or {}
    except Exception:  # noqa: BLE001
        return {}


def _agent_world_config(scope: str) -> dict:
    return _agent_block(scope, "world")


def _agent_wants_outdoor(scope: str) -> bool:
    """Does this agent's `world.outdoor` say its worlds are room-less (specs/agents.md §3)?

    Whether a world wants a space is a property of the AGENT, not of the request that happened to
    create it. `new_world(outdoor=True)` covers "this one world is a sky"; an agent whose whole point is
    to put you somewhere else needs to say so once, or its constructor-built first world silently
    inherits whatever room you were standing in."""
    return bool(_agent_world_config(scope).get("outdoor", False))


def _agent_session_public(scope: str) -> bool:
    """The visibility a NEW session in this scope is born with — the agent's `session.public`, default
    True (specs/agents.md §3, §7.5).

    Sessions are public by default because the shared experience is the feature. But an agent can be
    private BY NATURE, and the only lever before this was to instruct the model to call
    `set_world_visibility` on its first turn — which makes privacy contingent on an LLM remembering to
    act. A per-agent default belongs in the agent's declaration, not in its prose. Applies to EVERY mint
    path, so a session born of an agent switch is as private as one born of `session new`."""
    return bool(_agent_block(scope, "session").get("public", True))


def _first_world_spec(scope: str) -> tuple[str, list[dict]]:
    """The session constructor's first-world spec (docs/specs/agents.md §7.5): its NAME (default ``home``,
    overridable) + the first-world-only `on_create` steps. `first_world` may be a bare string (name only)
    or an object ``{name, on_create}``."""
    fw = _agent_block(scope, "session").get("first_world")
    if isinstance(fw, str):
        return (fw or "home"), []
    if isinstance(fw, dict):
        return (fw.get("name") or "home"), list(fw.get("on_create") or [])
    return "home", []


def _run_world_commands(cmds: list[dict]) -> list[dict]:
    """Compile constructor steps to world patch ops. A step names a command as ``cmd`` OR ``tool`` (the
    scripted-tool-call form, §6); both resolve against `_WORLD_COMMANDS`. Unknown/generative steps (e.g.
    skybox-from-description) aren't handled here yet — that's the async generative pass (step 4c)."""
    ops: list[dict] = []
    for c in cmds:
        fn = _WORLD_COMMANDS.get(c.get("cmd") or c.get("tool"))
        if fn:
            ops.extend(fn(c.get("args") or {}))
    return ops


async def _build_first_world(scope: str) -> tuple[Optional[str], Optional[WorldStore], Optional[str]]:
    """Build a session's FIRST world from the agent's declared opening — `session.first_world`: its name,
    its sync `on_create` steps, and its **generative** ones (specs/agents.md §7.5).

    Returns `(name, store, None)` on success or `(None, None, error)` on failure, and **writes nothing
    either way**. That is the whole point: it is shared by every path that mints a session, and the
    generative steps are the one fallible part of construction, so building before committing makes an
    abort a no-op with nothing to roll back (specs/agents.md §7.5).

    Before this, only `/session/new` consulted `first_world`. An agent switch minted a bare `default`
    from `world.on_create` alone, so an agent's intended opening — outdoor's moon-gate sky — was
    reachable only by typing `session new`."""
    wname, fw_on_create = _first_world_spec(scope)
    if any((s.get("tool") or s.get("cmd")) not in _WORLD_COMMANDS for s in fw_on_create):
        # Generative work is slow (image models). Say so before going quiet — the callers all allow for
        # a long wait, but the user shouldn't have to infer that from silence.
        await _broadcast({"type": "notice", "text": "Setting up your new world…"})
    gen_ops, gen_err = await _build_generative_ops(fw_on_create)
    if gen_err:
        return None, None, f"constructor failed: {gen_err}"
    raw = _new_world_store(scope, extra_on_create=fw_on_create)
    if gen_ops:
        raw.apply_patch(gen_ops, origin="constructor")   # generative results (e.g. the skybox) bake in
    _reset_room_authority(raw)
    return wname, raw, None


def _new_world_store(scope: str, *, extra_on_create: list[dict] = (),
                     adopt_space: bool = True, outdoor: bool = False) -> WorldStore:
    """A fresh world: the blank starter + the owning agent's `world.on_create` constructor. `extra_on_create`
    appends the first-world-only chain (§6) when minting a session's first world — generic steps first,
    then the specific ones. Only the SYNC (`_WORLD_COMMANDS`) steps are applied here; generative steps
    (skybox-from-description) are compiled separately by `_build_generative_ops` (async).

    The world ADOPTS the live space (`_space_for_new_world`, D5/step 5) — this is the one chokepoint every
    mint path shares, so a world born while you're standing in your room has your room in it no matter
    which path minted it. `adopt_space=False` is for the ONE caller that runs before a space is resolved
    (`_boot_world`); `outdoor` forces VOID for an explicitly room-less world, and so does the owning
    agent's `world.outdoor` — the per-request flag and the per-agent declaration OR together, so an
    outdoor agent's worlds are room-less however they were minted."""
    s = WorldStore.load(SAMPLE_WORLD)
    env = s.doc.setdefault("environment", {})
    env["public"] = True                                          # worlds are public by default (§4)
    if adopt_space:
        env["space"] = _space_for_new_world(scope, outdoor=outdoor or _agent_wants_outdoor(scope))
    ops = _run_world_commands(_agent_world_config(scope).get("on_create", []))
    ops += _run_world_commands(list(extra_on_create))
    if ops:
        s.apply_patch(ops, origin="constructor")
    return s


_REF = re.compile(r"\$\{([^}]+)\}")     # ${name} / ${name.field} — a constructor step-output reference


def _lookup_ref(bindings: dict, path: str):
    """Walk ``name.field.sub`` through the bound step results. Raises KeyError(path) if any segment is
    missing — so an unknown reference fails hard, not silently."""
    cur = bindings
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


def _resolve_refs(value, bindings: dict):
    """Interpolate ``${name.field}`` references (docs/specs/agents.md §7.5) through an arg value, recursing
    into dicts/lists. A whole-value ``${…}`` preserves the referenced value's type; an embedded one
    substitutes as text. Unknown references raise KeyError (→ fail-hard)."""
    if isinstance(value, str):
        m = re.fullmatch(r"\$\{([^}]+)\}", value)
        if m:                                             # the whole value is one ref → keep its real type
            return _lookup_ref(bindings, m.group(1).strip())
        return _REF.sub(lambda mm: str(_lookup_ref(bindings, mm.group(1).strip())), value)
    if isinstance(value, dict):
        return {k: _resolve_refs(v, bindings) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_refs(v, bindings) for v in value]
    return value


async def _build_generative_ops(steps: list[dict]) -> tuple[list[dict], Optional[str]]:
    """Run the **generative** constructor steps (docs/specs/agents.md §7.5) → world patch ops, WITHOUT
    touching the live world. Skybox-from-description generates + registers the image and emits an `env`
    patch pointing the sky at it; the caller applies the ops to the world it's about to build.

    Steps thread data **explicitly**: a step may bind its result under ``as: <name>``, and a later step
    references it via ``${name.field}`` in its args (e.g. ``set_skybox`` takes ``image_id: ${sky.image_id}``).
    No hidden "last image" — an unresolved reference is an error. **Fail-hard** (§8.13): the first failing
    step returns ``([], error)`` so the caller aborts before creating anything. Sync steps
    (`_WORLD_COMMANDS`) are skipped (already applied). Async + slow: image gen can take tens of seconds."""
    ops: list[dict] = []
    bindings: dict = {}
    for s in steps:
        tool = s.get("tool") or s.get("cmd")
        if _WORLD_COMMANDS.get(tool):
            continue                                          # sync step — already applied by _new_world_store
        try:
            args = _resolve_refs(s.get("args") or {}, bindings)
        except KeyError as e:
            return [], f"{tool}: unknown reference ${{{e.args[0]}}}"
        result: Optional[dict] = None
        if tool in ("generate_skybox_image", "generate_grounded_skybox_image"):
            grounded = "grounded" in tool
            desc = args.get("description") or args.get("prompt") or ""
            res = await (images_grounded_skybox if grounded else images_skybox)(SkyboxImageRequest(prompt=desc))
            if not res.get("ok"):
                return [], res.get("error") or f"{tool} failed"
            result = res                                      # {ok, image_id, url?, …} — bindable via `as`
        elif tool == "set_skybox":
            img = args.get("image_id")
            if not img:
                return [], "set_skybox: image_id required (e.g. ${sky.image_id})"
            rec = IMAGES.get(img)
            if not rec:
                return [], f"set_skybox: no image {img!r}"
            ops.append({"op": "env", "set": {"sky": {"src": rec.url}}})
            result = {"ok": True, "image_id": img}
        # unknown tool → ignore (forward-compatible; a future step type won't hard-fail an old server)
        if s.get("as") and result is not None:
            bindings[s["as"]] = result
    return ops, None


def _reset_room_authority(s: WorldStore) -> None:
    """Room authority (the one headset allowed to report geometry) is LIVE-session state, not durable.
    Each client mints a fresh id per page load, so a *persisted* authority from a past session names a
    dead headset — and ingest_room would reject the live headset's captures forever (it can't match the
    stale id). Clear it whenever a world becomes active so the live headset reclaims it on next capture."""
    env = s.doc.get("environment") or {}
    if env.get("captureAuthority"):
        env["captureAuthority"] = None


def _migrate_world_dirs(root: Path) -> None:
    """One-time: move worlds from the pre-user layout `<root>/private/<agent>/` to the user-first
    `<root>/<DEFAULT_USER>/agents/<agent>/` (docs/specs/spaces.md). Idempotent — only acts
    when the old dir exists and the destination doesn't; prunes the emptied `private/` tree."""
    old_root = root / "private"
    if not old_root.is_dir():
        return
    for agent_dir in sorted(old_root.iterdir()):
        if not agent_dir.is_dir():
            continue
        dest = root / DEFAULT_USER / "agents" / agent_dir.name
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        agent_dir.rename(dest)
        print(f"[conjure] migrated worlds {agent_dir} -> {dest}")
    try:
        old_root.rmdir()                          # remove the now-empty private/ tree (no-op if not empty)
    except OSError:
        pass


def _read_session_ptr() -> tuple[str, str] | None:
    """The single global **session pointer** (docs/specs/agents.md §7.1) — `(scope, session-id)`, the one
    fact boot restores. `agent = agent_of(scope)` and the active world are read back from that session.
    None on a fresh cache (the boot migration writes it whenever there was anything to migrate)."""
    if not SESSION_PTR.exists():
        return None
    scope, _, sid = SESSION_PTR.read_text().strip().partition("\t")
    return (scope, sid) if scope and sid else None


def _write_session_ptr(scope: str, sid: str) -> None:
    SESSION_PTR.parent.mkdir(parents=True, exist_ok=True)
    SESSION_PTR.write_text(f"{scope}\t{sid}")


def _ensure_session(scope: str, sid: str | None = None, *, active_world: str | None = None) -> str:
    """Guarantee a session exists and is the scope's active one; return its id. Creates a default
    `session-1` (meta + active pointer) for a scope that has none yet (a fresh agent, or the fallback
    boot path). Step 1: exactly one session per scope — switching/rename/new is step 3.

    A session minted here is marked **fresh** (`greeted`/`seeded` = False) exactly as `/session/new` marks
    one, because the agent server's constructor hooks gate on `is not False` (§7.5): an ABSENT flag means
    "a legacy session, don't retro-greet it", so omitting it here permanently skipped the constructor for
    every session born of an agent switch — no state seeded, no greeting."""
    if sessions is None:                               # unit-test paths without a session store: no-op
        return sid or MIGRATED_SID
    # The same three rungs as the world level (architecture.md §1), and for the same reason: deleting the
    # session you were last in used to leave no pointer, and this read "no pointer" as "never been here".
    # `MIGRATED_SID` is the NAME to give a brand-new session, not a resolution step — as a fallback it
    # only ever worked by accident, when a scope happened to still have a `session-1`.
    sid = sid or sessions.get_active(scope) or sessions.newest(scope) or MIGRATED_SID
    if not sessions.exists(scope, sid):
        user = scope.split("/", 1)[0]
        sessions.save_meta(scope, sid, {
            "id": sid, "owner": user, "agent": agent_of(scope), "title": "Session 1",
            "public": _agent_session_public(scope),
            "active_world": active_world or worlds.get_active(scope) or "",
            "llm": "", "greeted": False, "seeded": False})
    sessions.set_active(scope, sid)
    return sid


def _boot_world() -> tuple[str, str, WorldStore]:
    """Resume exactly where the server was: read the global session pointer `(scope, sid)`, make that the
    live session, and load that session's active world (docs/specs/agents.md §7.1). `agent = agent_of
    (scope)` is derived. The one-time on-disk relocation (`migrate_cache_to_users`, run in `_init_state`)
    is what writes the pointer for a pre-session cache; here we just restore it, falling back to the
    builder default when there's nothing."""
    global active_sid
    ptr = _read_session_ptr()
    scope, sid = ptr if ptr is not None else (DEFAULT_SCOPE, MIGRATED_SID)
    sid = _ensure_session(scope, sid)                 # make it the scope's live session (create if fresh)
    active_sid = sid
    worlds.set_live(scope, sid)                        # one explicit source for the live scope's worlds (§3)
    active = worlds.get_active(scope)                  # active world within that session (WorldDir pointer)
    if active and worlds.exists(scope, active):
        try:
            s = worlds.load(scope, active)
            _reset_room_authority(s)
            _write_session_ptr(scope, sid)
            return scope, active, s
        except Exception as exc:  # noqa: BLE001
            print(f"[conjure] active world {active!r} unreadable ({exc}); creating a fresh default")
    s = _new_world_store(scope, adopt_space=False)   # boot: no space resolved yet (the globals still hold
    _reset_room_authority(s)                         # their module defaults) — nothing honest to adopt
    wid = worlds.save(scope, "default", s)      # upsert by name → mints the permanent id
    worlds.set_active(scope, wid)
    _ensure_session(scope, sid, active_world=wid)
    _write_session_ptr(scope, sid)
    return scope, wid, s


settings = get_settings()  # loads .env
clients: "dict[WebSocket, str]" = {}     # connected render clients → their user (owner or guest)
_blocked: "dict[WebSocket, str]" = {}    # sockets bumped out of a now-PRIVATE live session (§6c/6d): kept
                                         # (ws → user) so they can be re-admitted when it goes public, but
                                         # OUT of `clients` so they get no world broadcasts meanwhile
gaze: "dict[str, dict]" = {}             # user → {"origin","forward","right","up"} in the reference frame,
                                         # from presence — where each headset is looking. May also carry
                                         # "anchor" (plane-relative head anchor) for the §7b seed-solve.
resolver: AssetResolver | None = (
    AssetResolver(settings.poly_pizza_api_key, ASSET_CACHE) if settings.poly_pizza_api_key else None
)
# Filesystem-mutating, stateful singletons — opened by _init_state() on SERVER STARTUP, never at import,
# so `import conjure.server` (tests, dev, tooling) can't run schema migrations / move world dirs / write
# to the real .cache. They're None until startup; the test fixture sets them directly (startup never
# fires under a plain TestClient).
library: "AssetLibrary | None" = None
worlds: "WorldRepository | None" = None
sessions: "SessionRepository | None" = None    # the session container; `worlds` routes per-session through it
spaces: "SpaceStore | None" = None
store: "WorldStore | None" = None
active_scope: str = DEFAULT_SCOPE
active_sid: str = MIGRATED_SID       # the live SESSION within active_scope (one per scope in step 1)
active_world: str = "default"
active_space: str = "home"          # bare NAME of the space the active world composes against
active_space_owner: str = DEFAULT_USER  # who OWNS that space — may differ from the active WORLD's owner
                                    # (D3: your world can live in someone else's shared space). Together
                                    # (active_space_owner, active_space) identify the live space's file.
# The namespace view (`dir`/`show`/`delete`) reads every global above. Bound to this MODULE, not to their
# values, because both kinds move: `_init_state` rebinds the repositories and the live pointers change on
# every switch. Safe at import — `namespace` reads the attribute at call time, never now.
namespace.bind(sys.modules[__name__])
# The embedder is None unless the optional torch/transformers are installed — then vector write-through
# is simply skipped and the catalog runs on FTS/exact only. Lazy: no model loads until first embed.
embedder = build_embedder(settings)


def _init_state() -> None:
    """Open the catalog (runs schema migrations), migrate the world layout, and boot the active world.
    All filesystem-mutating — so it runs on server startup, not at import. Idempotent enough to re-run.
    Back up library.db to protect curation: a lost catalog is NOT rebuilt from the cache files."""
    global library, worlds, sessions, spaces, store, active_scope, active_world, active_space, active_space_owner
    global _selected_cids, _space_holders
    _selected_cids = set()                           # a fresh session: every AR client re-selects (step 4/7)
    _space_holders = set()                           # nobody holds the space yet → provisional boot (D1)
    # User-home migration FIRST (docs/specs/config.md §7): relocate the in-project .cache into the
    # resolved home BEFORE anything opens a path under it (catalog, repositories, asset cache). Also
    # ensures the settings.json template exists. Idempotent (breadcrumb-guarded).
    ensure_settings_file(CONFIG_DIR)
    migrate_project_cache_to_home(PROJECT_CACHE, DATA_DIR, CACHE_ROOT)
    ASSET_CACHE.mkdir(parents=True, exist_ok=True)   # deferred from import → after the home exists
    library = AssetLibrary(LIBRARY_DB)
    # One-time, idempotent relocations (docs/specs/agents.md §7.1): first the pre-user layout under the
    # legacy worlds tree, then the whole worlds/spaces tree → the user-first session tree.
    _migrate_world_dirs(WORLDS_DIR)                  # pre-user layout → <user>/agents/<agent> (one-time)
    migrate_cache_to_users(CACHE)                    # worlds/spaces → <data>/users/…/sessions/session-1 (one-time)
    n = migrate_worlds_to_ids(USERS_DIR)             # worlds re-keyed name → `wld_…` id (one-time, idempotent)
    if n:
        print(f"[conjure] migrated {n} world(s) to permanent ids")
    n = migrate_env_room_to_space_presentation(USERS_DIR)   # environment.room → .spacePresentation (one-time)
    if n:
        print(f"[conjure] renamed environment.room → environment.spacePresentation in {n} world(s)")
    sessions = SessionRepository(USERS_DIR)
    worlds = WorldRepository(USERS_DIR, sessions=sessions)   # per-name ops route to the scope's active session
    spaces = SpaceStore(USERS_DIR)
    active_scope, active_world, raw = _boot_world()
    active_space_owner, active_space, store = _activate(active_scope, active_world, raw)   # resolve + compose
    if settings.force_geo:
        print(f"[conjure] --force-geo active: {settings.force_geo!r} — reported geolocation is overridden (test)")
    if settings.force_occupied:
        print("[conjure] --force-occupied active: the active space is treated as CLAIMED (admission gate on) (test)")

# Embedding is an *enrichment*, not part of procurement, so in production it runs OFF the request path:
# the asset is already procured/returned, and its vector lands a beat later (exact/FTS still match it
# immediately; only semantic search for that one asset is briefly eventual). A lock serializes model
# access (one forward pass at a time); a thread keeps the blocking torch call off the event loop.
# Tests flip _EMBED_BACKGROUND off for deterministic, inline write-through.
_EMBED_BACKGROUND = True
_embed_lock = asyncio.Lock()
_embed_tasks: set[asyncio.Task] = set()   # strong refs so fire-and-forget tasks aren't GC'd mid-flight
# Only VISUAL assets go in the vector index (embedded from pixels). SigLIP text↔text similarity sits
# at a much higher scale than text↔image, so mixing text-derived vectors (e.g. model titles) would
# make them dominate every text query and bury the images. Models are matched by FTS/exact on their
# title instead (plan §1; visual model embedding via thumbnails is a noted follow-up).
_VISUAL_KINDS = {"image", "skybox", "grounded_skybox", "photo"}


def _embed_now(id: str, *, image: bytes | None = None, text: str | None = None) -> None:
    """Blocking embed + store. Never raises (enrichment, not a requirement)."""
    if embedder is None:
        return
    try:
        vec = embedder.embed_image(image) if image is not None else embedder.embed_text(text or "")
        library.add_embedding(id, vec, embedder.name)
    except Exception as exc:  # noqa: BLE001
        print(f"[conjure] embed failed for {id}: {exc}")


async def _embed_bg(id: str, *, image: bytes | None = None, text: str | None = None) -> None:
    async with _embed_lock:                       # one model forward pass at a time
        await asyncio.to_thread(_embed_now, id, image=image, text=text)


def _embed_asset(id: str, *, image: bytes | None = None, text: str | None = None) -> None:
    """Schedule an asset's embedding. Off the request path when a loop is running (prod); inline
    otherwise (tests / no loop)."""
    if embedder is None:
        return
    if _EMBED_BACKGROUND:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass                                  # no running loop → fall through to inline
        else:
            task = asyncio.create_task(_embed_bg(id, image=image, text=text))
            _embed_tasks.add(task)
            task.add_done_callback(_embed_tasks.discard)
            return
    _embed_now(id, image=image, text=text)


def _embed_one(asset: dict) -> None:
    """Embed a single VISUAL catalog row (used by reindex) from its bytes. Reads bytes lazily so a
    batch doesn't hold every image in memory at once. Non-visual assets (models) are skipped — they're
    matched by FTS/exact on their title, not by vector (see _VISUAL_KINDS)."""
    fn = asset.get("filename")
    if asset.get("kind") in _VISUAL_KINDS and fn and (ASSET_CACHE / fn).exists():
        _embed_now(asset["id"], image=(ASSET_CACHE / fn).read_bytes())


async def _reindex_bg(assets: list[dict]) -> None:
    n = 0
    for a in assets:
        async with _embed_lock:                   # serialize with normal write-through embeds
            await asyncio.to_thread(_embed_one, a)
        n += 1
    print(f"[conjure] reindex: embedded {n} catalog asset(s)")


# Caption backfill — fills the `label` of bare assets (no prompt/title) via image→text vision, so they
# read in search results and match keyword/FTS. None unless a provider/key is configured.
captioner = build_captioner(settings)


def _stereo_layout(asset: dict) -> Optional[str]:
    """The stereo packing of an asset ('sbs' | 'tb') from its catalog attributes, else None."""
    raw = asset.get("attributes")
    if not raw:
        return None
    try:
        attrs = json.loads(raw) if isinstance(raw, str) else raw
        return attrs.get("stereo")
    except (ValueError, TypeError, AttributeError):
        return None


def _first_eye(data: bytes, layout: str) -> bytes:
    """Crop a packed stereo image to its first eye (SBS → left half, TB → top half), re-encoded as PNG.
    Used so a caption/embedding sees one clean view of the scene, not the doubled stereo pair."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        w, h = im.size
        box = (0, 0, w, h // 2) if layout == "tb" else (0, 0, w // 2, h)
        buf = io.BytesIO()
        im.crop(box).save(buf, format="PNG")
        return buf.getvalue()


async def _caption_one(asset: dict) -> None:
    """Caption one asset and store it as the label. Best-effort (never raises into the pass)."""
    if captioner is None:
        return
    fn = asset.get("filename")
    if not (fn and (ASSET_CACHE / fn).exists()):
        return
    mime = MEDIA_TYPES.get(Path(fn).suffix.lower(), "image/png")
    data = (ASSET_CACHE / fn).read_bytes()
    stereo = _stereo_layout(asset)
    if stereo:                                     # caption one eye, not the side-by-side pair
        data, mime = _first_eye(data, stereo), "image/png"
    try:
        text = await captioner.caption(data, mime=mime,
                                       skybox=asset.get("kind") in ("skybox", "grounded_skybox"))
        if text:
            library.upsert(asset["id"], label=text, attributes={"captioned": True})
    except Exception as exc:  # noqa: BLE001
        print(f"[conjure] caption failed for {asset['id']}: {exc}")


async def _caption_bg(assets: list[dict]) -> None:
    n = 0
    for a in assets:
        await _caption_one(a)                      # serial — gentle on the provider's rate limits
        n += 1
    print(f"[conjure] caption: described {n} asset(s)")
# Every configured image generator (keyed by casual name "Gemini"/"Chat"); the procurement
# endpoints mediate which one services a request (conjure.llm.select_generator).
image_generators = build_image_generators(settings)

TARGET_SIZE_M = 1.8  # fit a placed model's largest dimension to ~this many meters

# --- image store: procurement is decoupled from scene use. Procuring an image caches its bytes and
# registers an ImageRecord; scene tools (place_image/set_skybox) reference it by id. -------------
PROCURE_OPS = ("generate", "edit", "outpaint", "skybox", "skybox_from", "grounded_skybox")


@dataclass
class ImageRecord:
    id: str        # "<sha16>.<ext>" — also the /assets filename
    url: str       # "/assets/<id>"
    w: int
    h: int
    provider: str
    model: str
    prompt: str
    op: str
    transparent: bool = False  # has a real alpha channel → render the plane with transparency


IMAGES: dict[str, ImageRecord] = {}

# Short, human-friendly per-surface number shown on annotation labels + usable as a director target
# (e.g. "make 12 blue"). It IS the number already in the surface id (real_wall_3 → 3) — ONE numbering
# system, so the label, query_room, the id, and the user's reference all agree. (Previously a separate
# counter started at 1 while the id started at 0, which drifted off-by-one and confused references.)
# Stable by construction: same surface id → same number, no caching needed.
def _friendly_id_for(surface_id: str) -> int:
    tail = surface_id.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0

@contextlib.asynccontextmanager
async def _lifespan(app):
    """Startup/shutdown (replaces the deprecated on_event hooks). On startup: open the catalog + boot
    the active world (skipped if a test fixture already wired the globals) and start the autosave loop.
    On shutdown: stop autosave and flush the active world. Only fires under a real run / `with TestClient`."""
    global _autosave_task
    if library is None:
        _init_state()
    _autosave_task = asyncio.create_task(_autosave_loop())
    try:
        yield
    finally:
        if _autosave_task is not None:
            _autosave_task.cancel()
        try:
            _save_active()
        except Exception:  # noqa: BLE001
            pass


app = FastAPI(title="Conjure", version="0.0.1", lifespan=_lifespan)


def _slog(tag: str, msg: str) -> None:
    """Append a SERVER-side diagnostic line to temp/conjure.log, same format as the client's lines, so
    server routing events (world switches, space selection, /room accept vs 403) interleave with the
    client's registration/patch trace by timestamp. Gated like /client_log (debug_log OR debug_registration)."""
    if not (settings.debug_log or settings.debug_registration):
        return
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{tag}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --- geometry event log -------------------------------------------------------------------------------
# One JSONL line per CHANGE in the room's surface set or its height census (docs/backlogs/spaces-geometry.md
# — "Instrumentation"). Deliberately NOT conjure.log: that file is a single unrotated blob that pytest also
# appends to, and the unit of analysis here is "compare Tuesday to Friday" over a symptom that recurs on a
# timescale of days. Rotated daily, pruned past `geometry_log_days`.
#
# Structured rather than prose because the questions are numeric: did floor_10's height move relative to the
# rest of the space, and by how much. `t` is the SERVER's receive time (one clock for the whole file, so
# lines from the headset and from ingest_room sort together); the client's own stamp rides along as `ct`.
_geo_log_day: str = ""            # the date of the last retention sweep, so it runs once per day, not per line


def _geo_prune(today: str) -> None:
    """Delete rotated geometry logs older than `geometry_log_days`. Runs at most once per calendar day."""
    global _geo_log_day
    if _geo_log_day == today:
        return
    _geo_log_day = today
    days = settings.geometry_log_days
    if days <= 0:                                        # 0 = keep everything
        return
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    for p in GEO_LOG_DIR.glob("geometry-*.jsonl"):
        stamp = p.stem.removeprefix("geometry-")
        if len(stamp) == 10 and stamp < cutoff:          # lexical compare is chronological for ISO dates
            try:
                p.unlink()
            except OSError:
                pass


def _glog(ev: str, fields: dict, *, sid: str = "server", ct: float | None = None) -> None:
    """Append one geometry event. `ev` is a dotted name (`churn.mint`, `level.census`, `seed.prune`);
    `fields` is whatever that event needs. Never raises — a diagnostic must not break a capture."""
    if not settings.geometry_log:
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    rec = {"t": now.isoformat(timespec="milliseconds"), "sid": sid, "ev": ev}
    if ct is not None:
        rec["ct"] = ct
    rec.update(fields)
    try:
        GEO_LOG_DIR.mkdir(exist_ok=True)
        _geo_prune(today)
        with (GEO_LOG_DIR / f"geometry-{today}.jsonl").open("a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except (OSError, TypeError, ValueError):
        pass

# Edit-rights follow ownership (specs/spaces.md §7): only the ACTIVE world's owner may change the
# scene content of the shared world. Enforced server-side, never via the prompt — the MCP client and the
# headset attach an `X-Conjure-User` header; a non-owner hitting these routes gets 403. A *missing*
# header (the direct dev CLI) is treated as the owner (interim convenience). Reads, scoped catalog ops,
# procurement, and **world navigation** (`/worlds/new`, `/worlds/switch`) are NOT gated: anyone may
# create or switch worlds and everyone comes along — but a created/switched-into world is in the caller's
# OWN scope, so the caller becomes its owner and only *then* can edit it. This lets a guest spin up and
# build their own worlds with everyone present, while another user's curated world stays protected. (A
# consent/permission model to relax further — co-edit someone else's world — is a later tightening.)
_OWNER_ONLY_PATHS = {
    "/reset", "/patch", "/space/capture", "/space/realign", "/texture_surface", "/style_surface",
    "/place_asset", "/place_cached_asset", "/place_image", "/set_skybox", "/set_grounded_skybox",
    "/edit_image", "/outpaint_image", "/skybox_from_image",
    "/module", "/module/dismiss", "/manipulate", "/world_frame", "/figure",
}


@app.middleware("http")
async def _owner_only_writes(request, call_next):
    if request.url.path in _OWNER_ONLY_PATHS:
        who = request.headers.get("X-Conjure-User")
        owner = active_scope.split("/", 1)[0]
        if who and who != owner:
            _slog("guard", f"403 {request.url.path} by {who!r} — active world {owner}/{active_world} "
                           f"(space {active_space_owner}/{active_space})")
            return JSONResponse(status_code=403, content={
                "ok": False, "error": f"This world belongs to {owner}; only the owner can change it."})
    return await call_next(request)


# Asset ownership: a created asset belongs to WHOEVER made it (the caller), not the default user. The MCP
# client sends its full scope as `X-Conjure-Scope`; a pure-ASGI middleware stashes it in a ContextVar
# that the ingest paths (_store_image, model catalog) read — so guest's bridge image is owned by guest,
# and guest can curate it (visibility, relabel). A missing header (direct dev CLI) ⇒ DEFAULT_SCOPE.
_caller_scope: ContextVar[str] = ContextVar("caller_scope", default=DEFAULT_SCOPE)


class _ScopeFromHeader:
    """Pure-ASGI middleware (runs in the endpoint's own task, so the ContextVar reliably propagates —
    unlike a BaseHTTPMiddleware, which would run the endpoint in a separate task)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            val = dict(scope.get("headers") or {}).get(b"x-conjure-scope")
            token = _caller_scope.set(val.decode() if val else DEFAULT_SCOPE)
            try:
                await self.app(scope, receive, send)
            finally:
                _caller_scope.reset(token)
        else:
            await self.app(scope, receive, send)


app.add_middleware(_ScopeFromHeader)

# Durability: a background task writes the active world to its file whenever its rev advances. Polling
# debounces naturally — a multi-patch turn or a room-capture flurry coalesces into one write — and it
# touches no apply_patch call site. ~1 s of in-flight changes is the only crash-loss window.
_AUTOSAVE_INTERVAL = 1.0
_autosave_task: asyncio.Task | None = None
# Space claim & occupancy (specs/spaces.md §6.2/§6.3 — admission gate + lifecycle). The active space is a
# shared PHYSICAL resource: it's CLAIMED while an AR headset holds it (occupied), and UNCLAIMED (free for
# the next AR user to re-establish from anywhere) when the last one leaves. Boot starts unclaimed (D1 —
# provisional boot), so the first AR user always establishes. Two pieces of live-session state:
#   _space_holders  — the /ws sockets of AR clients that PASSED the co-location gate (declared "hold" after
#                     a successful select). occupied ⇔ non-empty. Cleared per-socket on release/disconnect.
#   _selected_cids  — client ids that have committed a /space/select this claim epoch (idempotency, so GPS
#                     jitter can't re-vote); reset by _unclaim() when the space frees up so re-selection can
#                     run. Per-CLIENT (not one global flag) so a SECOND, co-located AR user can still vote.
_space_holders: "set[WebSocket]" = set()
_selected_cids: set[str] = set()

# A world's space reference has THREE states, not two. `VOID` ("<void>") means *deliberately* room-less —
# an outdoor world, or one whose agent declares `world.outdoor`. `UNSET` means *not decided yet*: a world
# minted before anything knew which space we're in (the boot placeholder). Both render identically (no
# real geometry) and both report VOID to the client, so the client contract is unchanged — the difference
# is what a headset is allowed to do about it later:
#
#   UNSET  →  a space selection may relocate you (that's the point of a placeholder)
#   VOID   →  a space selection claims the space but LEAVES YOU WHERE YOU ARE (specs/spaces.md §4.3)
#
# The distinction is why C2b is safe. Without it, `_save_active` rewrites the boot placeholder to VOID
# within a second, C2b then declines to relocate, and a headset user is stranded in a blank world for
# reasons nobody could see. UNSET is purely in-memory: on disk it is the ABSENCE of the key, which
# `_activate` reads back as UNSET, so it round-trips without a new persisted sentinel.
UNSET = "<unset>"


def _no_space() -> bool:
    """Is there no active space to work with — either deliberately (VOID) or not-yet (UNSET)?
    Every caller that used to ask `active_space == VOID` means this; only the two places that must tell
    a decision from a placeholder compare against `UNSET` directly."""
    return active_space in (VOID, UNSET)


# A space's own history of what was open in it, newest first — `[[scope, world_id], …]`, capped like the
# world/session MRUs (`_MRU_CAP`). Same idea, third place: a single `last_world` meant that deleting
# it left a space with no memory at all, so walking back into your room after a cleanup minted a fresh
# world rather than opening the one you had there before it (architecture.md §1, rung 1).
#
# The SCOPE rides along with each world, not just the id: the agent that world belongs to is half of
# "put me back where I was", and re-deriving it afterwards is what `_entry_scope_for` has to guess at.
#
# It is NOT the same as the session-level ladder's rung 2, and deliberately so — see `_space_last_live`.
def _touch_recent(recent, scope: str, wid: str) -> list[list[str]]:
    """Move `(scope, wid)` to the front of a space's history."""
    rest = [p for p in (recent or []) if list(p)[:2] != [scope, wid]]
    return [[scope, wid]] + [list(p)[:2] for p in rest][:_MRU_CAP - 1]


def _may_join_world_in(user: str, scope: str) -> bool:
    """Would `user` survive the visibility gate on `scope`'s live session (§8.3)? Owners always may; a
    guest only if that session is public. The counterpart to `_may_create_world_in`, which governs
    building rather than entering."""
    if scope.split("/", 1)[0] == user:
        return True
    return _session_public(scope, _active_sid_for(scope))


def _space_last_live(sp: dict, user: str) -> tuple[Optional[str], Optional[str], str]:
    """The newest world in this space that `user` can actually open → `(scope, world_id, why_not_head)`.

    Two reasons to walk past an entry, and the caller is told which:

      - **gone** — deleted. Self-healing, exactly like the world and session pointers: a world removed
        anywhere else never comes back to prune this list, so reading has to skip the dead.
      - **private** — it exists, but it belongs to someone else's private session. A space's history is
        genuinely cross-user (`_save_active` writes it under the SPACE's owner using whichever scope is
        live), so your own room can remember a guest's private world. Joining it and *then* being thrown
        out by `_regate_clients` is the behaviour this filter replaces: it evicted you from your own room
        to passthrough, with the evicting switch already committed.

    `why_not_head` is `""` when the newest entry was usable — the ordinary case, and the silent one.

    **Only worlds tied to THIS space are candidates**, which is why there is no rung 2 here. A world
    carries `environment.space` and `_activate` composes it against that space, so falling back to a
    sibling from the same *session* — the session ladder's rung 2 — could hand a headset standing in one
    room a world built for another, and render its walls on top of the real ones. When this history is
    exhausted the right answer is to build a world BOUND to this space, not to reach sideways.
    """
    hist = [tuple(p)[:2] for p in (sp.get("recent") or []) if len(p) >= 2]
    legacy = (sp.get("last_scope"), sp.get("last_world"))
    if legacy[0] and legacy[1] and legacy not in hist:
        hist.append(legacy)                     # a space saved before `recent` existed carries only this
    why = ""
    for scope, wid in hist:
        if not (scope and wid):
            continue
        if not worlds.exists(scope, wid):
            why = why or "gone"
            continue
        if not _may_join_world_in(user, scope):
            why = why or "private"              # the FIRST reason we walked, which is what to report
            continue
        return scope, wid, why
    return None, None, why


def _occupied() -> bool:
    """Is the active space CLAIMED — is any AR headset currently holding it? While occupied, an AR joiner
    must match the active space (the admission gate); while unoccupied the space is free to (re)establish.
    `--force-occupied` (test) pins this True via a phantom holder so a single headset can exercise the gate."""
    return settings.force_occupied or bool(_space_holders)


def _unclaim() -> None:
    """The last AR holder left → the space is no longer occupied. Free it: the next AR user may establish a
    (possibly different) space from wherever they are (D6 — unlock when empty). The active world keeps
    rendering as a provisional placeholder; only re-selection is re-opened (clear the per-client commit
    guard). No-op while anyone still holds the space."""
    global _selected_cids
    if _occupied():
        return
    if _selected_cids:
        _slog("select", "space unclaimed (last AR holder left) — re-selection re-opened")
    _selected_cids = set()


def _save_active() -> None:
    """Persist the live composed doc by SPLITTING it: real-surface geometry + boundary → the active
    space; placed objects + display prefs + per-surface style overrides → the active world doc."""
    if store is None or worlds is None or spaces is None:
        return
    if sessions is not None and active_sid and not sessions.dir(active_scope, active_sid).exists():
        # The live session's DIRECTORY was removed out from under us (`reset agent`, a session purge).
        # Saving now would re-create what was just deleted — autosave must never resurrect a deleted
        # session. Nothing is lost: whoever deleted it meant to.
        #
        # Keyed on the directory, not `sessions.exists`, which tests for `session.json`: a session can
        # legitimately hold worlds before any meta is written, and treating that as deleted would
        # silently disable autosave for it.
        return
    if _no_space():                                 # room-less world: no geometry to split out
        world_doc = copy.deepcopy(store.doc)
        env = world_doc.setdefault("environment", {})
        if active_space == UNSET:
            env.pop("space", None)                  # no decision yet — persist the ABSENCE, not a VOID
        else:                                       # a deliberate outdoor world — record it as such
            env["space"] = VOID
        worlds.save(active_scope, active_world, WorldStore(world_doc))
        return
    # Geometry is persisted into the SPACE's OWNER's scope — not the world owner's — so a world built in
    # someone else's space writes its captured walls back to that owner's space (D3).
    owner = active_space_owner
    space = spaces.load(owner, active_space) if spaces.exists(owner, active_space) else \
        {"owner": owner, "name": active_space, "public": True, "geolocation": None,
         "surfaces": [], "boundary": None}
    fresh = _space_from_world_doc(owner, active_space, store.doc)      # geometry + boundary, default-materialed
    space["surfaces"], space["boundary"] = fresh["surfaces"], fresh["boundary"]
    # What was open HERE, newest first — the space's own MRU (specs/spaces.md §6.1). `last_scope`/
    # `last_world` stay as the head of it: several readers still want just "the last one" (the admin
    # listing, public-space discovery), and a space saved before this field existed has only those.
    space["recent"] = _touch_recent(space.get("recent"), active_scope, active_world)
    space["last_scope"], space["last_world"] = active_scope, active_world   # for nearest-space selection
    spaces.save(owner, active_space, space)
    spaces.set_active(owner, active_space)                            # keep the owner's current physical space current
    world_doc = _decompose(store.doc, space)                          # overrides diffed vs the (default) base
    world_doc.setdefault("environment", {})["space"] = _space_ref(owner, active_space)   # fully-qualified (D3)
    worlds.save(active_scope, active_world, WorldStore(world_doc))


async def _autosave_loop() -> None:
    saved_rev = store.doc.get("rev")
    while True:
        await asyncio.sleep(_AUTOSAVE_INTERVAL)
        rev = store.doc.get("rev")               # re-reads the globals (reset/switch rebind them)
        if rev != saved_rev:
            try:
                _save_active()
                saved_rev = rev
            except Exception as exc:  # noqa: BLE001 — autosave must never crash the server
                print(f"[conjure] world autosave failed: {exc}")


#: The authored heights a figure may keep. Outside it the file is not in metres and "life size" would be
#: a doll or a five-metre giant. Measured across the library: 1.55–1.70 for the three hand-converted
#: figures, 5.21 and 1.80 for two asset-pack characters, 2.53 for a deliberately blocky one. The upper
#: bound sits below that last: a game character authored at two and a half metres is a style, not a
#: measurement, and `size_m` remains available when a giant is the point.
HUMAN_HEIGHT_M = (0.5, 2.5)


def _normalize(record, pos: list[float], target_m: Optional[float],
               *, rigged: bool = False) -> tuple[list[float], list[float]]:
    """Scale a model so its largest dimension is `target_m` meters and its base sits at pos.y,
    centered at pos.x/z. Returns (position, scale); native scale if the bounding box is unknown.

    **A rigged model (a figure) is different in two ways** (docs/backlogs/figures.md):

    - It is authored at *life size*, and that size is meaningful in a way a prop's is not — normalizing
      every human to the same height erases the difference between a child and a giant. With no explicit
      `target_m` a figure therefore keeps its native scale.
    - Its meaningful dimension is **height**, not the largest extent: a T-posed figure's arm span rivals
      its height, and a seated one's exceeds it, so `max(size)` sizes by the wrong axis. When a caller
      *does* ask for a specific size, that means height.

    Life size is honoured only when the authored height is one a person could have. Not every rigged
    model is authored metric: measured in the library, `Animated Woman` comes out at 4.8 m and another
    at 0.37 m — units artifacts, not authored choices, and "keep native size" would place a doll or a
    giant. Outside the plausible range a figure is normalized like anything else, by height. Open
    question 6 called for this clamp before a source needing it turned up; one has.
    """
    if not record.bbox_min or not record.bbox_max:
        return pos, [1.0, 1.0, 1.0]
    mn, mx = record.bbox_min, record.bbox_max
    size = [mx[i] - mn[i] for i in range(3)]
    if rigged and (target_m is not None or HUMAN_HEIGHT_M[0] <= size[1] <= HUMAN_HEIGHT_M[1]):
        s = 1.0 if target_m is None else target_m / (size[1] or 1.0)     # by HEIGHT, and native by default
    elif rigged:
        s = TARGET_SIZE_M / (size[1] or 1.0)             # not authored metric: normalize, still by height
    else:
        s = (target_m or TARGET_SIZE_M) / (max(size) or 1.0)
    cx, cz = (mn[0] + mx[0]) / 2, (mn[2] + mx[2]) / 2
    position = [pos[0] - s * cx, pos[1] - s * mn[1], pos[2] - s * cz]
    return position, [s, s, s]


def _kind_for_op(op: str) -> str:
    """The catalog `kind` for a procured image, by how it was made."""
    if op in ("skybox", "skybox_from"):
        return "skybox"
    if op == "grounded_skybox":
        return "grounded_skybox"
    return "image"


def _euler_yxz_quat(rot_deg: list[float]) -> list[float]:
    """A-Frame euler (degrees, YXZ order) → quaternion [x,y,z,w]. Mirrors THREE.Quaternion.setFromEuler so a
    server-authored anchor's orientation vote matches what the client would compute from the same rotation."""
    x, y, z = math.radians(rot_deg[0]), math.radians(rot_deg[1]), math.radians(rot_deg[2])
    c1, c2, c3 = math.cos(x / 2), math.cos(y / 2), math.cos(z / 2)
    s1, s2, s3 = math.sin(x / 2), math.sin(y / 2), math.sin(z / 2)
    return [s1 * c2 * c3 + c1 * s2 * s3, c1 * s2 * c3 - s1 * c2 * s3,
            c1 * c2 * s3 - s1 * s2 * c3, c1 * c2 * c3 + s1 * s2 * s3]


def _quat_mul(a: list[float], b: list[float]) -> list[float]:
    """Hamilton product a·b of quaternions [x,y,z,w] (THREE.Quaternion.multiply convention)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [ax * bw + aw * bx + ay * bz - az * by, ay * bw + aw * by + az * bx - ax * bz,
            az * bw + aw * bz + ax * by - ay * bx, aw * bw - ax * bx - ay * by - az * bz]


def _quat_conj(q: list[float]) -> list[float]:
    """Conjugate = inverse for a unit quaternion."""
    return [-q[0], -q[1], -q[2], q[3]]


def _quat_rot(q: list[float], v: list[float]) -> list[float]:
    """Rotate vector v by unit quaternion q [x,y,z,w]."""
    x, y, z, w = q
    tx, ty, tz = 2 * (y * v[2] - z * v[1]), 2 * (z * v[0] - x * v[2]), 2 * (x * v[1] - y * v[0])
    return [v[0] + w * tx + (y * tz - z * ty), v[1] + w * ty + (z * tx - x * tz),
            v[2] + w * tz + (x * ty - y * tx)]


def _seed_planes() -> list[dict]:
    """The current seed's floor + walls as plane-anchor Planes (F_ref), in the SAME convention as the
    client's localToPlanes: floor normal = +Y; wall normal = the surface's outward normal (a-plane +Z,
    via _plane_basis). Feeds author_anchor/solve_anchor (docs §7a/§13.1)."""
    planes: list[dict] = []
    if store is None:
        return planes
    for e in store.doc.get("entities", []):
        m = e.get("meta") or {}
        if not m.get("real"):
            continue
        pos = (e.get("transform") or {}).get("position")
        if not pos:
            continue
        sem = m.get("semantic")
        if sem == "floor":
            planes.append({"id": e["id"], "kind": "floor", "normal": [0.0, 1.0, 0.0], "point": pos})
        elif sem == "wall":
            nrm = _plane_basis((e.get("transform") or {}).get("rotation") or [0.0, 0.0, 0.0])[0]
            planes.append({"id": e["id"], "kind": "wall", "normal": nrm, "point": pos})
    return planes


def _content_anchor(transform: dict, placement: str) -> Optional[dict]:
    """Author a persisted plane-relative anchor (docs §7c) for a content entity at `transform` (F_ref)
    against the current seed. Returns the anchor dict, or None if the seed has too few walls to anchor
    (caller then leaves the entity on its raw F_ref pose, as before). Server-side twin of the client's
    per-capture authoring — authored ONCE here so the client can just SOLVE it."""
    planes = _seed_planes()
    if sum(1 for p in planes if p["kind"] == "wall") < 2:
        return None
    pos = transform.get("position")
    if not pos:
        return None
    mode = placement if placement in ("grounded", "free") else "grounded"
    entity = {"position": pos, "quaternion": _euler_yxz_quat(transform.get("rotation") or [0.0, 0.0, 0.0]),
              "mode": mode}
    return author_anchor(entity, planes)


#: Everything about a model that is DERIVED from its bytes. Extraction owns these outright — a stale one
#: is not worth keeping, since it was computed from the same file by older code.
_DERIVED_MODEL_ATTRS = ("bbox_min", "bbox_max", "rigged", "height_m", "joints", "clips", "morph_targets",
                        "humanoid", "humanoid_source", "humanoid_axes", "humanoid_follows",
                        "spring_bones", "tris")


def _extracted_model_attrs(asset_id: str) -> dict:
    """Everything `/library/import` learns from a model's bytes, for a path that fetched them instead."""
    path = ASSET_CACHE / asset_id
    if not path.exists():
        return {}
    try:
        from .importer import plan_import
        return dict(plan_import(asset_id, path.read_bytes(), {}).attributes)
    except Exception as exc:  # noqa: BLE001 — cataloguing a model must never fail over its metadata
        _slog("figure", f"extraction failed for {asset_id}: {exc}")
        return {}


def _refresh_model_attrs(asset_id: str, attrs: dict, force: bool = False) -> dict:
    """A model's catalog attributes, re-derived from its bytes when the stored ones predate this build.

    **A catalog row is a snapshot of what we UNDERSTOOD about a model, not of the model.** Understanding
    keeps changing and the rows do not, so this is where the two are reconciled: on the first placement
    after a version bump, extraction runs again and the row is written back. Measured occasions for it,
    all real: figures catalogued before inference existed; bone maps inferred before conversion rebuilt
    the deform hierarchy, which today's `validate()` rejects; frames measured before aiming or joint
    limits; and three rigged characters catalogued as PROPS because the Poly Pizza fetch path recorded a
    triangle count and never looked at the skeleton at all.

    Extraction is authoritative for everything derived — including CLEARING a bone map it can no longer
    justify, since no map is an honest error message and a plausible wrong one is an elbow that bends
    backwards three weeks later. Curation (label, tags, licence, rating) is untouched.
    """
    if not force and attrs.get("frame_rev") == FRAME_REV:
        return attrs
    path = ASSET_CACHE / asset_id
    if not path.exists():
        return attrs
    fresh = _extracted_model_attrs(asset_id)
    if not fresh:
        return attrs
    # `{}` rather than absent: the catalog MERGES attributes and skips None, so an empty map is how a
    # rejected one is actually cleared.
    write = {k: fresh.get(k) for k in _DERIVED_MODEL_ATTRS if fresh.get(k) is not None}
    for k in ("humanoid", "humanoid_axes", "humanoid_follows"):
        write[k] = fresh.get(k) or {}
    write["frame_rev"] = FRAME_REV
    library.upsert(asset_id, attributes=write)
    if bool(attrs.get("rigged")) != bool(fresh.get("rigged")) or attrs.get("humanoid") != fresh.get("humanoid"):
        _slog("figure", f"{asset_id}: re-extracted — rigged={bool(fresh.get('rigged'))} "
                        f"map={len(fresh.get('humanoid') or {})} bones")
    return {**attrs, **write}


def _model_entity_op(eid: str, model_id: str, *, title, licence, attribution, creator, tris, source,
                     bbox_min, bbox_max, pos, size_m, placement="grounded", rigged=False,
                     humanoid=None, humanoid_axes=None, humanoid_follows=None) -> dict:
    """Build the `add` op for a glTF model entity, auto-scaled and carrying its license/attribution. Shared
    by /place_asset (web) and /place_cached_asset (library reuse). `placement` (docs §5b/c) drives how each
    client re-solves it: "grounded" (default — sits on the LOCAL floor, upright) or "free" (keeps the full
    authored 3-D pose, so it can float / rest at any height).

    `rigged` marks a FIGURE: placed at life size rather than normalized, and flagged in `meta` so the
    client can treat it as one (docs/backlogs/figures.md)."""
    rec = SimpleNamespace(bbox_min=bbox_min, bbox_max=bbox_max)
    model_pos, model_scale = _normalize(rec, pos, size_m, rigged=rigged)
    mode = placement if placement in ("grounded", "free") else "grounded"
    transform = {"position": model_pos, "scale": model_scale}
    meta = {"title": title, "license": licence, "attribution": attribution, "creator": creator,
            "source": source, "tris": tris, "generated": False, "placement": mode}
    if rigged:
        meta["rigged"] = True
        # Ship the authored bounds to the client. A skinned mesh's NODE often sits under the bone chain
        # (Grace's hair hangs off `head`, ten bones up the spine), while its vertices are already in skin
        # space — so anything deriving a box from `mesh.matrixWorld` double-counts the whole skeleton.
        # grab's selection box came out twice her height that way. We computed the right answer at import;
        # sending it removes the guesswork rather than re-deriving it against three's bind matrices.
        if bbox_min and bbox_max:
            meta["bbox"] = [list(bbox_min), list(bbox_max)]
        if humanoid:
            # The semantic bone vocabulary travels WITH the entity, so /figure can resolve
            # "leftUpperArm" without a catalog lookup and the client needs no per-rig knowledge.
            meta["humanoid"] = dict(humanoid)
        if humanoid_follows:
            # Bones that deform the mesh but hang outside their own limb (an IK foot parented to the
            # armature root). They cannot be posed, but they have to RIDE the limb or the mesh stretches
            # from a planted foot to a raised ankle — reported from the headset on two of three
            # asset-pack characters.
            meta["humanoid_follows"] = dict(humanoid_follows)
        if humanoid_axes:
            # And the anatomical frame beside it: which way to rotate each bone so "bend 45" is the
            # same motion on a VRM and on a re-parented Daz rig. Measured from the bind pose at import
            # (figures.anatomical_axes) — a property of the file, so it travels with the entity too.
            meta["humanoid_axes"] = dict(humanoid_axes)
    # Step 7c: author + persist the plane-relative anchor now (server-side, once) so the client can SOLVE it
    # rather than re-author from the F_ref pose against its docSurfaces copy every capture. Client ignores it
    # until step 7b/c flips it to consume it; None (too few seed walls) leaves the entity on its F_ref pose.
    anchor = _content_anchor(transform, mode)
    if anchor:
        meta["anchor"] = anchor
        _slog("anchor", f"authored {eid}: mode={anchor['mode']} "
                        f"floor={'y' if anchor['floor'] else 'n'} walls={len(anchor['walls'])} "
                        f"[{', '.join(w['id'].split('_')[-1] for w in anchor['walls'])}]")
    return {"op": "add", "entity": {
        "id": eid, "transform": transform,
        "components": {"gltf-model": f"/assets/{model_id}"}, "meta": meta,
    }}


def _asset_in_agent_scope(rec: Optional[dict]) -> bool:
    """By-id counterpart to the catalog's hard agent wall (see library.search/query): an asset is
    reachable only if its scope has the SAME agent segment as the caller AND (it's the caller's own
    scope OR public). Guards the reuse-by-id paths so a known/guessed id can't sidestep the wall."""
    if not rec:
        return False
    sc = _caller_scope.get()
    if agent_of(rec.get("scope") or "") != agent_of(sc):
        return False                                  # different agent — hard wall (even if public)
    return rec.get("scope") == sc or bool(rec.get("public", 0))


def _inherit_visibility(asset_id: str) -> dict:
    """`{"public": 0|1}` for a NEW asset, inherited from the active world's visibility (specs/spaces.md
    §5: created in a private world ⇒ private). Empty dict if the asset already exists — never overwrite a
    visibility the owner set after the fact."""
    if library.get(asset_id) is not None:
        return {}
    return {"public": 1 if _active_public() else 0}       # inherits the SESSION's visibility (§8.2)


def _ensure_referenced_public(asset_id: str) -> Optional[str]:
    """Public-uses-public invariant (specs/spaces.md §5): a public world may reference only public
    assets, so a visitor can load the whole scene. Placing one of YOUR OWN private assets into a public
    world publishes it (you're sharing it by placing it) and returns a notice for the director to relay;
    no-op in a private world, or for an already-public asset / another user's asset."""
    if store is None or not _active_public():
        return None                                   # private session ⇒ anything goes
    rec = library.get(asset_id)
    owner = active_scope.split("/", 1)[0]
    if rec and not rec.get("public", 1) and (rec.get("scope") or "").split("/", 1)[0] == owner:
        library.update(asset_id, public=True)
        return f"Note: published your private asset '{rec.get('label') or asset_id}' so it stays visible " \
               f"to anyone in this public world."
    return None


def _with_notice(out: dict, notice: Optional[str]) -> dict:
    """Attach a public-uses-public notice to a response dict (so the MCP tool can relay it), if any."""
    if notice:
        out["notice"] = notice
    return out


def _referenced_asset_ids(doc: dict) -> set[str]:
    """Every catalog asset id a world doc references — any '/assets/<id>' string anywhere in it (entity
    materials, gltf-model, skybox src). A schema-agnostic walk, so new reference sites are covered free."""
    ids: set[str] = set()

    def walk(x):
        if isinstance(x, str):
            if "/assets/" in x:
                ids.add(x.split("/assets/", 1)[1].split("/")[0].split("?")[0])
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(doc)
    return ids


def _publish_world_assets(doc: dict, owner: str) -> list[str]:
    """Publish any of `owner`'s PRIVATE assets the world references (public-uses-public). Used when a
    world is made public. Returns the labels published (for the director to report)."""
    published = []
    for aid in _referenced_asset_ids(doc):
        rec = library.get(aid)
        if rec and not rec.get("public", 1) and (rec.get("scope") or "").split("/", 1)[0] == owner:
            library.update(aid, public=True)
            published.append(rec.get("label") or aid)
    return published


def _catalog_asset(asset_id: str, *, kind: str, label: str | None = None, prompt: str | None = None,
                   query: str | None = None, params: dict | None = None, attributes: dict | None = None,
                   provider: str | None = None, model: str | None = None, width: int | None = None,
                   height: int | None = None, transparent: bool | None = None, licence: str | None = None,
                   attribution: str | None = None, creator: str | None = None, scope: str | None = None,
                   embed_image: bytes | None = None) -> None:
    """Register/merge a catalog row for an asset whose bytes already live in the store. The single
    write-through every ingest path shares (generation, Poly-Pizza models, external import): one place
    that applies the invariants — caller scope, `cache://` source, inherited world visibility (spaces-
    and-users-plan §4: made private ⇒ private), and, for a visual kind with bytes, a vector embedding.
    Only-non-None merge in `library.upsert` means a later partial write never clobbers creation data."""
    if kind == "model" and not (attributes or {}).get("frame_rev"):
        # The Poly Pizza fetch path recorded a triangle count and a bounding box and stopped there, so
        # three rigged characters were catalogued as props: normalized to 1.8 m instead of life size, and
        # unposable, because nobody had looked at their skeletons. Ingest paths that disagree about how
        # hard they look at a file is the same bug twice now, so the look happens HERE — the one write
        # every path already shares — rather than in each of them.
        attributes = {**(attributes or {}), **_extracted_model_attrs(asset_id)}
    library.upsert(asset_id, kind=kind, scope=scope if scope is not None else _caller_scope.get(),
                   source=f"cache://{asset_id}", filename=asset_id, label=label, prompt=prompt,
                   query=query, params=params, attributes=attributes, provider=provider, model=model,
                   width=width, height=height,
                   transparent=None if transparent is None else (1 if transparent else 0),
                   licence=licence, attribution=attribution, creator=creator,
                   **_inherit_visibility(asset_id))
    if embed_image is not None and kind in _VISUAL_KINDS:
        _embed_asset(asset_id, image=embed_image)   # embed the pixels into the shared space (best-effort)


def register_asset(data: bytes, *, kind: str, ext: str, embed_image: bytes | None = None, **fields) -> str:
    """Content-address `data` into the asset store and catalog it; return the id ('<sha16><ext>', which
    is also the /assets filename). For callers holding raw bytes (image generation, external import).
    Content-addressing gives free dedup — the same bytes re-import to the same id. `embed_image` lets a
    caller embed a cleaned view (e.g. one eye of a stereo pair) instead of the stored bytes. (Poly-Pizza
    models, whose bytes the resolver already cached, call `_catalog_asset` directly.)"""
    asset_id = f"{hashlib.sha256(data).hexdigest()[:16]}{ext}"
    (ASSET_CACHE / asset_id).write_bytes(data)
    _catalog_asset(asset_id, kind=kind, embed_image=data if embed_image is None else embed_image, **fields)
    return asset_id


def _store_image(result, *, prompt: str, op: str) -> ImageRecord:
    """Write a procured image to the content store and register an ImageRecord; return it."""
    ext = ".png" if "png" in result.mime_type else (".webp" if "webp" in result.mime_type else ".jpg")
    w, h, transparent = _img_meta(result.data)
    image_id = register_asset(result.data, kind=_kind_for_op(op), ext=ext, label=prompt, prompt=prompt,
                              params={"op": op, "transparent": transparent},
                              provider=result.provider, model=result.model, width=w, height=h,
                              transparent=transparent)
    rec = ImageRecord(id=image_id, url=f"/assets/{image_id}", w=w, h=h, provider=result.provider,
                      model=result.model, prompt=prompt, op=op, transparent=transparent)
    IMAGES[image_id] = rec
    return rec


def _get_image(image_id: str):
    """Return (record, bytes, error) for a procured image id. Rebuilds a minimal record from disk if
    the in-memory entry was lost to a restart, so post-restart edits still work."""
    if not image_id or "/" in image_id or ".." in image_id:
        return None, None, f"bad image id {image_id!r}"
    path = ASSET_CACHE / image_id
    if not path.exists():
        return None, None, f"no image {image_id!r}"
    cat = library.get(image_id)
    if cat is not None and not _asset_in_agent_scope(cat):   # hard agent wall on reuse-by-id
        return None, None, f"no image {image_id!r}"          # don't leak another agent's asset
    data = path.read_bytes()
    rec = IMAGES.get(image_id)
    if rec is None:
        w, h, transparent = _img_meta(data)
        # Recover provenance from the catalog (survives restart) rather than the old "?" placeholders.
        cat = library.get(image_id) or {}
        op = "?"
        if cat.get("params_json"):
            try:
                op = json.loads(cat["params_json"]).get("op", "?")
            except (ValueError, TypeError):
                pass
        rec = ImageRecord(id=image_id, url=f"/assets/{image_id}", w=w, h=h,
                          provider=cat.get("provider") or "?", model=cat.get("model") or "?",
                          prompt=cat.get("prompt") or cat.get("label") or "", op=op, transparent=transparent)
        IMAGES[image_id] = rec
    return rec, data, None


def _entity_image(entity_id: str):
    """Return (entity, image_id, error) for an in-scene image entity (resolves meta.image_id, else
    derives the id from the material src filename for entities placed before this field existed)."""
    entity = next((e for e in store.doc["entities"] if e["id"] == entity_id), None)
    if entity is None:
        return None, None, f"no entity {entity_id!r}"
    meta = entity.get("meta", {})
    image_id = meta.get("image_id")
    if not image_id:
        src = entity.get("components", {}).get("material", {}).get("src") or ""
        if "/assets/" in src:
            image_id = src.rsplit("/", 1)[-1]
    if not image_id:
        return None, None, f"{entity_id!r} is not an editable image"
    return entity, image_id, None


async def _procure(op: str, *, prompt: str, requested: Optional[str], transparent: bool, run) -> dict:
    """Mediate + run one procurement op. `run(generator) -> ImageResult`. Returns the uniform image
    result dict (no scene effect)."""
    if not image_generators:
        return {"ok": False, "error": "no image generator configured (set GOOGLE_API_KEY, OPENAI_API_KEY, and/or XAI_API_KEY)"}
    gen, err = select_generator(image_generators, op, requested=requested, transparent=transparent)
    if err:
        return {"ok": False, "error": err}
    try:
        result = await run(gen)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    rec = _store_image(result, prompt=prompt, op=op)
    return {"ok": True, "image_id": rec.id, "url": rec.url, "w": rec.w, "h": rec.h,
            "provider": rec.provider, "model": rec.model, "op": op}


def _img_meta(data: bytes) -> tuple[int, int, bool]:
    """(width, height, has-real-alpha) for a stored image."""
    import io

    from PIL import Image

    with Image.open(io.BytesIO(data)) as im:
        return im.size[0], im.size[1], _has_alpha(im)


_NO_STORE = {"Cache-Control": "no-store"}  # avoid stale client during active dev


@app.get("/")
async def index() -> HTMLResponse:
    # Stamp the client script URL with its mtime so a code change always busts the cache. The Quest
    # Browser caches /static across reloads even with no-store, which left headsets running stale JS.
    html = (CLIENT_DIR / "index.html").read_text()
    rwm = int((CLIENT_DIR / "room-worker.js").stat().st_mtime)   # geometry worker (fix/pops-and-jitters)
    tmm = int((CLIENT_DIR / "three.module.min.js").stat().st_mtime)  # worker's standalone three (ESM)
    # Dynamic modules are discovered + scoped to the ACTIVE agent (docs/specs/dynamics.md §9): inject a
    # <script> per module from its folder, mtime-stamped so a code change busts the cache.
    dyn_tags, dyn_mtimes = _dynamic_module_tags()

    # Stamp EVERY /static/*.js reference with its file mtime. This was nine hand-written replace() lines,
    # one per script, and adding a tenth is how the next one gets forgotten — `figure.js` shipped without
    # a stamp and the Quest served a stale copy through several reloads, so a fixed component looked like
    # a broken one. The cache is stubborn enough that an unstamped script is effectively frozen at
    # whatever the headset saw first.
    mtimes = []

    def _stamp(m):
        f = CLIENT_DIR / m.group(1)
        if not f.exists():
            return m.group(0)
        t = int(f.stat().st_mtime)
        mtimes.append(t)
        return f'/static/{m.group(1)}?v={t}'

    html = re.sub(r"/static/([A-Za-z0-9._-]+\.js)(?!\?)", _stamp, html)
    v = max(mtimes + [rwm, tmm, *dyn_mtimes])    # badge reflects the newest of the scripts
    build = datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    html = html.replace("    <!-- __DYNAMIC_MODULES__", dyn_tags + "    <!-- __DYNAMIC_MODULES__")
    html = html.replace("__CLIENT_VERSION__", f"{build} (v{v})")
    # Tell the client which diagnostics are on so it doesn't POST/HUD when off. debug_log gates general
    # client logging; debug_registration gates the co-location registration HUD + per-capture log.
    flag = "true" if settings.debug_log else "false"
    rflag = "true" if settings.debug_registration else "false"
    jflag = "true" if settings.debug_jitter else "false"   # jitter probes only (clean, no registration diag)
    gflag = "true" if settings.geometry_log else "false"   # always-on, change-gated geometry event log
    soflag = "true" if settings.debug_surface_overlay else "false"   # seed/device wireframe overlay (opt-in)
    # Co-location robustness knobs (two-headset guest tuning) — read by RoomSnap.register/selectSpace and the
    # capture throttle in conjure-client.js. Omitting a field falls back to the client's built-in default.
    reg = (f"{{minCov:{settings.reg_min_cov},minCovFrac:{settings.reg_min_cov_frac},"
           f"sizeTol:{settings.reg_size_tol},inlierM:{settings.reg_inlier_m},yawPeaks:{settings.reg_yaw_peaks}}}")
    cap_ms = int(settings.capture_interval * 1000)
    # --force-geo also tells the CLIENT to synthesize a location fix instead of calling navigator.geolocation
    # (which is unreliable on the Quest). The server still overrides the actual coords (_apply_forced_geo),
    # so any value works; this just lets space selection PROCEED without a real GPS fix during testing.
    fg = json.dumps(settings.force_geo) if settings.force_geo else "null"
    ds = json.dumps(settings.drop_surface) if settings.drop_surface else "null"   # --drop-surface (test §5.2)
    # Render apply-gate tolerances (--apply-tol-*) → the client's surfaceMoved (world-model.js).
    tol = (f"{{pos:{settings.apply_tol_pos},rotDeg:{settings.apply_tol_rot_deg},ext:{settings.apply_tol_ext}}}")
    gwr = "true" if settings.group_surface_relay else "false"   # --group-surface-relay (junction-seam fix)
    # Wall-identity-by-plane knobs (--wall-*) → RoomSnap.matchWall. yawTol is passed in RADIANS (matchWall's
    # unit); the CLI/config take degrees for readability.
    wall = (f"{{perpTol:{settings.wall_perp_tol},yawTol:{math.radians(settings.wall_yaw_tol_deg)},"
            f"overlapSlop:{settings.wall_overlap_slop}}}")
    html = html.replace("</head>", f"  <script>window.CONJURE_DEBUG_LOG={flag};"
                        f"window.CONJURE_DEBUG_REGISTRATION={rflag};window.CONJURE_DEBUG_JITTER={jflag};"
                        f"window.CONJURE_GEOMETRY_LOG={gflag};"
                        f"window.CONJURE_DEBUG_SURFACE_OVERLAY={soflag};"
                        f'window.CONJURE_VOID_ORIGIN="{settings.void_origin}";' 
                        f"window.CONJURE_FORCE_GEO={fg};"
                        f"window.CONJURE_DROP_SURFACE={ds};"
                        f"window.CONJURE_REG={reg};window.CONJURE_CAPTURE_MS={cap_ms};"
                        f"window.CONJURE_APPLY_TOL={tol};window.CONJURE_GROUP_SURFACE_RELAY={gwr};"
                        f"window.CONJURE_WALL={wall};"
                        f"window.CONJURE_INSET_STANDOFF={settings.inset_standoff};"
                        f"window.CONJURE_WALL_SEAL_TOL={settings.wall_seal_tol};"
                        f"window.CONJURE_SURFACE_WELD={settings.surface_weld};"
                        f"window.CONJURE_GEO_SLICE_MS={settings.geo_slice_ms};"
                        f"window.CONJURE_POSE_TAU={settings.pose_tau};"
                        f"window.CONJURE_FOVEATION={settings.foveation};"
                        f"window.CONJURE_BEAM_MS={int(settings.beam_timeout * 1000)};"
                        f"window.CONJURE_BEAM_TRIGGER={settings.beam_trigger};"
                        f"window.CONJURE_BINDINGS={settings.bindings};"
                        f'window.CONJURE_OCCLUSION="{settings.occlusion}";'
                        f'window.CONJURE_WORKER_URL="/static/room-worker.js?v={v}";</script>\n  </head>')
    return HTMLResponse(html, headers=_NO_STORE)


@app.get("/time")
async def server_time() -> dict:
    """Server wall-clock in epoch milliseconds — the shared time base for dynamic modules
    (docs/specs/dynamics.md §6). Clients estimate their offset to this with a
    Cristian-style round-trip (client/conjure-clock.js) so every headset computes tier-A procedural
    state — f(sharedClock, seed, config) — identically, with zero per-frame sync."""
    return {"t": time.time() * 1000.0}


@app.get("/tunnel")
@app.get("/tunnel/{user}")
async def tunnel(user: str = DEFAULT_USER) -> RedirectResponse:
    """Redirect to the current cloudflared tunnel URL (written by scripts/tunnel.sh). Lets you type a
    short, fixed LAN address (http://<this-machine>:<port>/tunnel) on the Quest instead of the long
    random trycloudflare URL that changes every session. `/tunnel/<user>` logs that user in for the web
    session by carrying it through as `?user=<user>` (the headset client reads it)."""
    url = TUNNEL_FILE.read_text().strip() if TUNNEL_FILE.exists() else ""
    if not url:
        raise HTTPException(status_code=404, detail="No tunnel running — start one with scripts/tunnel.sh")
    if user and user != DEFAULT_USER:
        url = url + ("&" if "?" in url else "?") + "user=" + user
    # Temporary (the URL changes each run) + no-store so the browser never caches a stale tunnel.
    return RedirectResponse(url, status_code=307, headers=_NO_STORE)


@app.get("/static/conjure-client.js")
async def client_js() -> FileResponse:
    # Explicit route (takes precedence over the /static mount) so we can disable caching.
    return FileResponse(CLIENT_DIR / "conjure-client.js", media_type="application/javascript", headers=_NO_STORE)


@app.get("/static/room-snap.js")
async def room_snap_js() -> FileResponse:
    # Explicit no-store route for the snapping module (loaded before conjure-client.js).
    return FileResponse(CLIENT_DIR / "room-snap.js", media_type="application/javascript", headers=_NO_STORE)


@app.get("/static/world-model.js")
async def world_model_js() -> FileResponse:
    # Explicit no-store route for the pure world-model/presence module (loaded before conjure-client.js).
    return FileResponse(CLIENT_DIR / "world-model.js", media_type="application/javascript", headers=_NO_STORE)


@app.get("/static/grounded-skybox.js")
async def grounded_skybox_js() -> FileResponse:
    # Explicit no-store route for the grounded-skybox module (loaded before conjure-client.js).
    return FileResponse(CLIENT_DIR / "grounded-skybox.js", media_type="application/javascript", headers=_NO_STORE)


@app.get("/static/conjure-pointers.js")
async def conjure_pointers_js() -> FileResponse:
    # Explicit no-store route for the unified XR input layer (loaded before every module that reads input).
    return FileResponse(CLIENT_DIR / "conjure-pointers.js", media_type="application/javascript", headers=_NO_STORE)


@app.get("/static/controller-beams.js")
async def controller_beams_js() -> FileResponse:
    # Explicit no-store route for the controller pointer-beams module (loaded before conjure-client.js).
    return FileResponse(CLIENT_DIR / "controller-beams.js", media_type="application/javascript", headers=_NO_STORE)


@app.get("/dynamics/available")
async def dynamics_available() -> dict:
    """The ACTIVE agent's scoped dynamic-module catalog (docs/specs/dynamics.md §9).
    Rendered as `name — description; params: k(default)…` per module and injected into the director's
    prompt via the `dynamics://available` MCP resource — so the director discovers its modules without a
    ritual. Text under `catalog`; the raw name list under `modules`."""
    registry = _dynamics_registry()
    names = [n for n in _active_agent_dynamics() if n in registry]
    lines = [registry[n].catalog_line() for n in names]
    return {"ok": True, "modules": names, "catalog": "\n".join(lines)}


@app.get("/dynamics/{module}/{filename}")
async def dynamics_file(module: str, filename: str) -> FileResponse:
    """Serve a discovered module's client script/asset from its folder (`dynamics/<name>/<file>`). Path
    components are basename-only (no traversal), and the file must sit inside the resolved module dir."""
    module, filename = Path(module).name, Path(filename).name    # strip any path parts (traversal guard)
    try:
        module_dir = dynamics_loader.resolve_module_dir(module)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"unknown dynamic module {module!r}")
    target = (module_dir / filename).resolve()
    if not str(target).startswith(str(module_dir.resolve())) or not target.is_file():
        raise HTTPException(status_code=404, detail=f"no file {filename!r} in module {module!r}")
    media = "application/javascript" if filename.endswith(".js") else None
    return FileResponse(target, media_type=media, headers=_NO_STORE)


@app.get("/world")
async def world() -> dict:
    return store.doc


@app.post("/reset")
async def reset_world() -> dict:
    """Reset the ACTIVE world to the empty starter (+ the agent's constructor) — clears all entities +
    environment (incl. any captured room) and broadcasts a fresh snapshot. The room re-captures on its
    own once a headset is back in AR. The world keeps its name; only its contents are wiped."""
    global store
    raw = _new_world_store(active_scope)      # empty starter world (placed content + prefs only)
    space = spaces.load(active_space_owner, active_space) \
        if (spaces and not _no_space() and spaces.exists(active_space_owner, active_space)) \
        else {"surfaces": [], "boundary": None}   # keep the active space's geometry (from its owner's scope)
    store = WorldStore(_compose(raw.doc, space))   # keep the physical room; clear only the world's content
    _reset_room_authority(store)
    _save_active()                            # persist the reset so it survives a restart
    await _broadcast(_snapshot_msg())
    return {"ok": True, "rev": store.doc["rev"]}


# ---- world management (scoped; scope is injected server-side, never an LLM argument) ----------------
async def _switch_to(scope: str, ref: str, store_override: WorldStore | None = None,
                     *, sid: Optional[str] = None, wid: Optional[str] = None) -> dict:
    """Make (scope, name) the live world: persist the outgoing one, set the incoming as `store`, record
    it as active, and broadcast a snapshot so the headset reloads. `store_override` installs a freshly
    built world (new_world) instead of loading from disk. `sid` names the target SESSION (default: the
    scope's active session) — resolved AFTER `_save_active` so the outgoing world is written to the
    session it actually belongs to, not the incoming one."""
    global store, active_scope, active_world, active_space, active_space_owner, active_sid
    _save_active()                            # split + persist the outgoing world (+ its space) FIRST,
                                              # while the OLD live session is still set on the facade
    sid = _ensure_session(scope, sid)        # flip to the target session (create if fresh) — and point the
    worlds.set_live(scope, sid)              # facade at it BEFORE the load/save, so the incoming world is
                                             # read from the NEW session, not the old one (session-switch bug)
    # Resolve AFTER `set_live`: a name only means something inside the target session, and `active_world`
    # is an ID — the whole point being that renaming the world doesn't move this pointer.
    if wid is None:
        wid = worlds.resolve(scope, ref)
    if store_override is not None:
        # A freshly-built world isn't on disk yet; the upsert persists it and mints an id when `ref` is a
        # new name (activate is read-only now — creating owns persistence, step 0).
        raw = store_override
        wid = worlds.save(scope, wid or ref, raw)
    else:
        if wid is None:
            raise ValueError(f"no world {ref!r}")
        raw = worlds.load(scope, wid)
    name = worlds.name_of(scope, wid)
    active_scope, active_world, active_sid = scope, wid, sid
    active_space_owner, active_space, store = _activate(scope, wid, raw)   # resolve space (owner+name) + compose
    worlds.set_active(scope, wid)                 # per-session memory: which world to resume in this session
    _write_session_ptr(scope, sid)               # global pointer: which SESSION is live across the server
    _slog("world", f"switch → {scope.split('/', 1)[0]}/{name} [{wid}] "
                   f"(space {active_space_owner}/{active_space})")
    await _propagate_visibility()                 # snapshot + bump/re-admit per the new session's visibility
    return {"ok": True, "world": name, "id": wid, "rev": store.doc["rev"]}


async def _activate_scope(scope: str, *, force: bool = False) -> dict:
    """Make a world in `scope` live: resume that scope's last-active world, or create its `default` if
    the scope has none — the `_boot_world` logic generalized to any scope. A no-op when `scope` is
    already active. Used on agent switch so the live world belongs to the NEW agent's scope, not the
    previous agent's. A world minted here ADOPTS the live space like any other (`_space_for_new_world`),
    so switching agents while standing in your room keeps the room — and it runs the agent's **declared
    opening** (`_build_first_world`), so a first-ever switch gives you what that agent is for rather than
    a bare `default`. The opening is built before anything is written, so a failure leaves you where you
    were (§5a).

    `force` re-enters a scope that is already live — used by `reset agent`, whose whole purpose is to
    rebuild the scope it is standing in."""
    # The live agent is derived from the global session pointer (set by _switch_to below) — no separate
    # last-agent to record here (docs/specs/agents.md §9.1).
    if scope == active_scope and not force:
        return {"ok": True, "world": worlds.name_of(scope, active_world), "id": active_world,
                "scope": scope, "unchanged": True}
    # The three-rung ladder (architecture.md §1). Rung 1 is the MRU pointer, which walks past however
    # many of its entries have been deleted. Rung 2 is the floor: a world that exists but was never in
    # the history — created and never switched to, or the history exhausted. Only then do we mint.
    # Captured before minting, which creates one: a scope with no session at all has never been used, so
    # building its opening is a FIRST RUN and unremarkable. The same mint under someone who had worlds
    # here is a degradation, and an empty new world is otherwise indistinguishable from losing the lot.
    been_here = bool(sessions and sessions.list(scope))
    # The session rung, announced here rather than inside `_ensure_session` (which is sync, and whose
    # other callers are boot paths with nobody listening yet). An agent switch is the arrival where a
    # purged session is actually noticed.
    if sessions is not None and not sessions.get_active(scope):
        resumed = sessions.newest(scope)
        if resumed:
            await _broadcast({"type": "notice",
                              "text": f"The session you were in is gone — resumed "
                                      f"'{_session_title(scope, resumed)}'."})
    active = worlds.get_active(scope)                          # rung 1 — self-healing, silent
    if not active:
        active = worlds.newest(scope)                          # rung 2 — a surviving sibling
        if active:
            await _broadcast({"type": "notice",
                              "text": f"The world you were in is gone — opened "
                                      f"'{worlds.name_of(scope, active) or active}'."})
    if active:
        return await _switch_to(scope, active)
    wname, raw, err = await _build_first_world(scope)          # rung 3 — the agent's opening
    if err:
        return {"ok": False, "error": err}
    if been_here:
        await _broadcast({"type": "notice", "text": f"No worlds left here — starting '{wname}'."})
    return await _switch_to(scope, wname, store_override=raw)


class ResetAgentRequest(BaseModel):
    user: str = DEFAULT_USER
    agent: str
    assets: bool = False      # also purge the catalog rows in this scope (default: keep them)


@app.post("/agent/reset")
async def agent_reset(req: ResetAgentRequest) -> dict:
    """Wipe an agent back to never-used, so the next arrival is a genuine **first run**.

    Removes every session in the scope — transcripts, agent state, and the worlds inside them. Assets are
    **kept** by default: a generated skybox is expensive and is not part of "the conversation". Pass
    `assets` to purge those too, which is what you want when testing an opening that generates one.

    Spelled as its own verb rather than as several `delete` calls because a reset is a *sequence* —
    purge, then clear the pointers that named what was purged, then land somewhere coherent — and the
    part that is easy to get wrong is the sequence, not any one deletion. Doing it by hand means editing
    the live-session pointer with the server down.

    Resetting the agent you are STANDING IN is the normal case (that is how you test a first run), so it
    is supported directly: purge, then force re-entry, which rebuilds the session and runs the agent's
    declared opening (§7.5). `_save_active` refuses to resurrect a session whose directory is gone, so
    the purge is not undone on the way out."""
    if sessions is None or worlds is None:
        return {"ok": False, "error": "no session store"}
    try:
        agent = clean_name(req.agent, what="agent name")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    try:
        resolve_agent_dir(agent)
    except Exception:                                          # noqa: BLE001
        return {"ok": False, "error": f"no agent {agent!r} — nothing to reset"}

    scope = scope_for(req.user, agent)
    was_live = scope == active_scope
    sids = sessions.list(scope)
    for sid in sids:
        sessions.delete(scope, sid)
    n_assets = 0
    if req.assets and library is not None:
        # Query the catalog directly rather than through `namespace.asset_rows`, which returns DISPLAY nodes
        # the shell's tree (the id rides in `label`). A deletion loop shouldn't depend on a formatter.
        for row in library.by_user(req.user, limit=10_000):
            if (row.get("scope") or "") != scope:
                continue
            ok, _ = library.delete(row["id"])
            n_assets += 1 if ok else 0
    _slog("reset", f"agent {scope}: {len(sids)} session(s)"
                   + (f", {n_assets} asset(s)" if req.assets else "") + (" (was live)" if was_live else ""))

    out = {"ok": True, "agent": agent, "scope": scope,
           "sessions": len(sids), "assets": n_assets, "was_live": was_live}
    if was_live:
        # Re-enter what we just emptied: a fresh session, and the agent's opening built from scratch.
        entered = await _activate_scope(scope, force=True)
        if not entered.get("ok"):
            return {**out, "ok": False, "error": entered.get("error"), "reentered": False}
        out["world"] = entered.get("world")
    return out


class ActivateScopeRequest(BaseModel):
    scope: str = DEFAULT_SCOPE


@app.post("/scope/activate")
async def scope_activate(req: ActivateScopeRequest) -> dict:
    """Activate a world in `scope` (resume last-active, else create default). Called on agent switch so
    the live world matches the new agent. Un-gated like /worlds/switch — everyone present comes along."""
    return await _activate_scope(req.scope)


# ---- session management (docs/specs/agents.md §7.1) --------------------------------------------------
# Multiple sessions per scope, switchable. A session is an instance of the agent (its own transcript +
# worlds + state); the agent server keys its transcript on the live (scope, session). Switching a session
# writes the global pointer, so every peripheral follows — the same source-of-truth pattern as agents.

def _next_sid(scope: str) -> str:
    """A fresh, stable session id for `scope`: ``session-<N>``, N one past the highest existing."""
    n = 0
    for sid in sessions.list(scope):
        m = re.fullmatch(r"session-(\d+)", sid)
        if m:
            n = max(n, int(m.group(1)))
    return f"session-{n + 1}"


def _loose(s: Optional[str]) -> str:
    """Voice-friendly match key: case-insensitive with spaces/underscores/hyphens treated as equal
    ('Test 7' == 'test-7' == 'test_7'), and other punctuation dropped. Lookup ONLY — never changes a
    stored name.

    Dropping punctuation mirrors `world.slug`, which is how worlds and spaces have always matched. That
    difference was invisible until a name arrived carrying quotes: a WORLD called '"alien"' still answered
    to `alien` because slug threw the quotes away, while a SESSION titled the same did not, because this
    key kept them — so the session became unreachable by any form of its own name. `clean_name` now stops
    such a name being stored at all; matching the two keys up is what lets the ones already on disk be
    reached (and renamed) without a migration."""
    s = re.sub(r"[\s_-]+", " ", fold_accents(s).strip().lower())
    return re.sub(r"[^a-z0-9 ]", "", s).strip()


def _session_title_taken(scope: str, title: str, *, other_than: str = "") -> Optional[str]:
    """The id of another session in `scope` that `title` would collide with, or None.

    Collision is measured with `_loose` — the key `_resolve_sid` matches on — because that is what makes
    a title ambiguous in practice, not string equality. Worlds and spaces have refused a duplicate name
    all along (`WorldDir.name_taken`); sessions didn't, so two could both be 'Home' and `_resolve_sid`
    would return None for the ambiguous match, reporting "no session 'Home'" — doesn't-exist when it
    meant matches-two.

    Ids count as well as titles: titling one session 'Session 1' while a *different* session-1 exists
    doesn't strictly collide (an exact id wins first), but it makes the same words mean two things
    depending on spelling, which is the confusion the guard is for."""
    want = _loose(title)
    if not want:
        return None
    for sid in sessions.list(scope):
        if sid == other_than:
            continue
        try:
            other = sessions.load_meta(scope, sid).get("title") or ""
        except (OSError, ValueError):
            other = ""
        if want in (_loose(sid), _loose(other)):
            return sid
    return None


def _resolve_sid(scope: str, ref: Optional[str]) -> Optional[str]:
    """Resolve a session reference to a session id: exact id, else exact title, else a UNIQUE loose
    (case + separators) match on id or title. Ambiguous loose matches return None (caller reports
    not-found). Voice-friendly — lets clients say `session <title>` in any case/spacing."""
    if not ref:
        return None
    ids = sessions.list(scope)
    if ref in ids:
        return ref                                             # exact id
    titles: dict[str, str] = {}
    for sid in ids:
        try:
            titles[sid] = sessions.load_meta(scope, sid).get("title") or ""
        except (OSError, ValueError):
            titles[sid] = ""
    exact = [sid for sid, t in titles.items() if t == ref]     # exact title (case-sensitive)
    if len(exact) == 1:
        return exact[0]
    key = _loose(ref)                                          # unique loose match on id or title
    loose = [sid for sid in ids if _loose(sid) == key or _loose(titles[sid]) == key]
    return loose[0] if len(loose) == 1 else None


def _resolve_user(spoken: str, agent: str) -> Optional[str]:
    """Resolve a spoken owner to a real user that has sessions in `agent`: exact, else a UNIQUE loose
    match. The bounded candidate set (users present in the caller's agent) keeps voice matching safe."""
    users = worlds.users_in_agent(agent)
    if spoken in users:
        return spoken
    key = _loose(spoken)
    matches = [u for u in users if _loose(u) == key]
    return matches[0] if len(matches) == 1 else None


async def _switch_session(scope: str, sid: str) -> dict:
    """Make session `sid` live: route worlds to it, resume its active world (or build the agent's declared
    opening if it has none yet), and switch — which writes the global pointer ``(scope, sid)`` and
    broadcasts. The agent server follows the pointer and swaps the transcript (step 2). We DON'T flip the
    scope's active session here — `_switch_to(sid=…)` does it after saving the outgoing world, so it
    lands in the right session.

    The opening comes from `_build_first_world`, the same routine `/session/new` and an agent switch use;
    this path used to hard-code the name ``home`` and skip the constructor entirely."""
    wdir = sessions.worlds(scope, sid)              # explicit target — no active-pointer flip yet
    active = wdir.get_active()
    if not (active and wdir.exists(active)):
        wname, raw, err = await _build_first_world(scope)
        if err:
            return {"ok": False, "error": err}      # nothing written — the caller stays where it was
        wdir.save(wname, raw)
        wdir.set_active(wname)
        active = wname
    return await _switch_to(scope, active, sid=sid)


class SessionRef(BaseModel):
    scope: str = DEFAULT_SCOPE
    session: Optional[str] = None     # id or title; default (rename) = the active session
    title: Optional[str] = None
    owner: Optional[str] = None       # cross-user VISIT: switch into this user's session, in the CALLER's agent


@app.get("/sessions")
async def sessions_list(scope: str = DEFAULT_SCOPE) -> dict:
    """Every session in `scope` with its meta + which is live, plus `available` — other USERS' public
    sessions a human can visit (docs/specs/agents.md §7.2). The shell's `sessions` verb renders both; the
    agent never sees this (cross-user movement is a person's act, not the LLM's). `available` excludes the
    caller's whole user, so your own other agents/sessions don't masquerade as strangers'."""
    active = sessions.get_active(scope)
    out = []
    for sid in sessions.list(scope):
        try:
            meta = sessions.load_meta(scope, sid)
        except (OSError, ValueError):
            meta = {}
        out.append({"id": sid, "title": meta.get("title", sid), "active_world": meta.get("active_world"),
                    "llm": meta.get("llm", ""), "public": meta.get("public", True), "active": sid == active})
    # Discovery is scoped to the caller's AGENT (same lens as their own list); switch agents to cross.
    available = worlds.list_public_sessions(agent=agent_of(scope), exclude_user=scope.split("/", 1)[0])
    # The single global live session — lets the shell mark it '@' wherever it appears (yours or others').
    return {"ok": True, "sessions": out, "active": active, "available": available,
            "live": {"scope": active_scope, "session": active_sid}}


@app.post("/session/new")
async def session_new(req: SessionRef) -> dict:
    """Create a new session in `scope` and switch to it. Its first world is built by the constructor
    (docs/specs/agents.md §7.5): named by the agent's `session.first_world` (default ``home``), set up by
    `world.on_create` ⊕ the first-world-only `on_create` chain. The greeting is appended by the agent
    server; generative first-world steps (skybox) are a later pass."""
    # Clean + uniqueness-check the title FIRST: the constructor below can generate a skybox (tens of
    # seconds), and a title we were always going to refuse shouldn't cost that before it's refused.
    try:
        title = clean_name(req.title, what="session title") if req.title else None
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if title:
        clash = _session_title_taken(req.scope, title)
        if clash:
            return {"ok": False, "error": f"a session called {title!r} already exists here ({clash})"}
    # The opening is built BEFORE anything is created or switched, so a failure aborts with nothing on
    # disk to roll back (§5a). Shared with every other path that mints a session.
    wname, raw, err = await _build_first_world(req.scope)
    if err:
        return {"ok": False, "error": err}
    sid = _next_sid(req.scope)
    user = req.scope.split("/", 1)[0]
    sessions.save_meta(req.scope, sid, {
        "id": sid, "owner": user, "agent": agent_of(req.scope),
        "title": title or f"Session {sid.split('-')[-1]}",
        "public": _agent_session_public(req.scope),
        "active_world": wname, "llm": "", "greeted": False, "seeded": False})
    wdir = sessions.worlds(req.scope, sid)     # explicit target — the active-pointer flip is _switch_to's
    wdir.save(wname, raw)
    wdir.set_active(wname)
    await _switch_session(req.scope, sid)                # resumes the first world we just built
    return {"ok": True, "session": sid, "title": sessions.load_meta(req.scope, sid)["title"]}


@app.post("/session/switch")
async def session_switch(req: SessionRef) -> dict:
    """Switch the live session — one of your own (`session <name>`), or with `owner` a cross-user VISIT
    into that user's session **in the caller's active agent** (`session <user> <name>`). Owner and
    session names resolve exact-then-unique-loose (case + separators) for voice. A visit is allowed only
    if the target session is PUBLIC; you land there as a guest (you stay yourself — owner-only writes
    still refuse edits)."""
    caller_agent = agent_of(req.scope)
    caller_user = req.scope.split("/", 1)[0]
    if req.owner and _loose(req.owner) != _loose(caller_user):    # VISIT another user, in the caller's agent
        owner = _resolve_user(req.owner, caller_agent)
        if not owner:
            return {"ok": False, "error": f"no user {req.owner!r} with sessions in the {caller_agent} agent"}
        target_scope = scope_for(owner, caller_agent)
        sid = _resolve_sid(target_scope, req.session)
        if not sid:
            return {"ok": False, "error": f"no session {req.session!r} owned by {owner}"}
        if not _session_public(target_scope, sid):
            return {"ok": False, "error": f"that session is private — ask {owner} to make it public"}
        r = await _switch_session(target_scope, sid)
        # Carry the TITLE as well as the id: the id is a stable handle, the title is what a person (or a
        # TTS engine) should be told they switched to.
        return {"ok": True, "session": sid, "title": _session_title(target_scope, sid),
                "owner": owner, "world": r.get("world")}
    sid = _resolve_sid(req.scope, req.session)                   # own session
    if not sid:
        return {"ok": False, "error": f"no session {req.session!r} in {req.scope}"}
    r = await _switch_session(req.scope, sid)
    return {"ok": True, "session": sid, "title": _session_title(req.scope, sid),
            "world": r.get("world")}


@app.post("/session/rename")
async def session_rename(req: SessionRef) -> dict:
    sid = _resolve_sid(req.scope, req.session) if req.session else sessions.get_active(req.scope)
    if not sid or not sessions.exists(req.scope, sid):
        return {"ok": False, "error": f"no session {req.session!r} in {req.scope}"}
    if not req.title:
        return {"ok": False, "error": "a new title is required"}
    try:                                            # same cleaning a world or space name gets, so a title
        title = clean_name(req.title, what="session title")   # can always be typed back (world.clean_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    clash = _session_title_taken(req.scope, title, other_than=sid)
    if clash:                                       # …and the same uniqueness worlds and spaces enforce
        return {"ok": False, "error": f"a session called {title!r} already exists here ({clash})"}
    meta = sessions.load_meta(req.scope, sid)
    meta["title"] = title                           # retitle only — the id is stable, nothing moves
    sessions.save_meta(req.scope, sid, meta)
    return {"ok": True, "session": sid, "title": title}


@app.post("/session/delete")
async def session_delete(req: SessionRef) -> dict:
    sid = _resolve_sid(req.scope, req.session)
    if not sid:
        return {"ok": False, "error": f"no session {req.session!r} in {req.scope}"}
    if req.scope == active_scope and sid == active_sid:
        return {"ok": False, "error": "can't delete the active session — switch away first"}
    return {"ok": sessions.delete(req.scope, sid), "session": sid}


class SessionVisibilityRequest(BaseModel):
    public: bool
    scope: str = DEFAULT_SCOPE
    session: Optional[str] = None     # id or title; default = the active session


@app.post("/session/visibility")
async def session_visibility(req: SessionVisibilityRequest) -> dict:
    """Make a session public (discoverable + joinable by others) or private (owner only). Visibility now
    lives on the SESSION and a world inherits it (docs/specs/agents.md §9.4); step 6a records it, step 6b
    re-keys discovery + the join/privacy gates onto it. Default target = the active session."""
    sid = _resolve_sid(req.scope, req.session) if req.session else sessions.get_active(req.scope)
    if not sid or not sessions.exists(req.scope, sid):
        return {"ok": False, "error": f"no session {req.session!r} in {req.scope}"}
    meta = sessions.load_meta(req.scope, sid)
    meta["public"] = req.public
    sessions.save_meta(req.scope, sid, meta)
    if req.scope == active_scope and sid == active_sid:   # the LIVE session's visibility changed (§8.3)
        await _propagate_visibility()                     # bump/re-admit headset + broadcast → agent re-gates
    return {"ok": True, "session": sid, "public": req.public}


def _session_public(scope: str, sid: str) -> bool:
    """A session's visibility (docs/specs/agents.md §9.4) — the unit of visibility a world now inherits.
    Defaults public (missing meta / pre-session). Source of truth for the join gate + asset inheritance."""
    if sessions is None:
        return True
    try:
        return bool(sessions.load_meta(scope, sid).get("public", True))
    except (OSError, ValueError):
        return True


def _session_title(scope: str, sid: str) -> str:
    """A session's display title, falling back to its id. What a person should be TOLD they switched to;
    the id stays the stable handle underneath."""
    try:
        return sessions.load_meta(scope, sid).get("title") or sid
    except (OSError, ValueError):
        return sid


def _active_public() -> bool:
    """Whether the LIVE session is public — what the `/ws` join gate + new-asset visibility key on (§8.2)."""
    return _session_public(active_scope, active_sid)


@app.get("/agent/last")
async def agent_last(user: str = DEFAULT_USER) -> dict:
    """The **live** agent, so a front-end launched without an explicit --agent resumes it. There's one
    shared session (docs/specs/agents.md §9), so the answer is global: `agent = agent_of(active_scope)`,
    derived from the session pointer — not a per-user record. The `user` param is vestigial (kept for
    back-compat)."""
    return {"ok": True, "agent": agent_of(active_scope)}


@app.get("/state")
async def live_state() -> dict:
    """The canonical **"what's live"** snapshot for the single shared session (docs/specs/agents.md §9.1) —
    `{scope, agent, world, owner, space}`. The reconciliation seam a client / the agent server reads on
    connect (and mirrored into every `/ws` snapshot's `state`). Identifiers only; `GET /world` returns the
    full doc. Subsumes `GET /agent/last` (its `agent` is `state.agent`)."""
    return {"ok": True, **_live_state()}


class WorldRef(BaseModel):
    name: str
    scope: str = DEFAULT_SCOPE
    public: bool = True               # new_world: create public (default) or private
    outdoor: bool = False             # new_world: an OUTDOOR/void world (skybox, no room; space = <void>)


class ScopeRef(BaseModel):
    scope: str = DEFAULT_SCOPE


class GeoReport(BaseModel):
    lat: float
    lon: float
    accuracy: Optional[float] = None
    user: Optional[str] = None       # who's reporting (the connecting AR user)
    cid: Optional[str] = None        # the reporting client's per-page-load id (select-commit idempotency)


class SpaceSelect(BaseModel):
    """The client's verdict after voting its live capture against the /geolocation candidates."""
    matched: bool = False            # did a candidate's geometry register?
    owner: Optional[str] = None      # the matched space's owner + name (when matched)
    name: Optional[str] = None
    lat: Optional[float] = None      # the reporter's location — stamps/mints the space when NOT matched
    lon: Optional[float] = None
    user: Optional[str] = None       # the connecting user (mints spaces/worlds in THEIR scope)
    cid: Optional[str] = None        # the committing client's per-page-load id (commit-once idempotency)


_GEO_RANGE_M = 300.0                # coarse prefilter radius: spaces within this are surface-match candidates


def _unique_space_name(user: str) -> str:
    existing = set(spaces.list(user))
    i = len(existing) + 1
    while f"space-{i}" in existing:
        i += 1
    return f"space-{i}"


def _candidate_surface(e: dict) -> dict:
    """Trim a stored surface entity to just the geometry the client's registration vote needs
    (RoomSnap.surfaceToRef) — id, semantic, pose, extent. Drops materials/debug/overlays from the wire."""
    t, comps = e.get("transform") or {}, e.get("components") or {}
    return {"id": e.get("id"),
            "meta": {"semantic": (e.get("meta") or {}).get("semantic", "surface")},
            "transform": {"position": t.get("position", [0, 0, 0]), "rotation": t.get("rotation", [0, 0, 0])},
            "components": {"surface": {"extent": ((comps.get("surface") or {}).get("extent", [1, 1]))}}}


def _geo_candidates(lat: float, lon: float) -> list[dict]:
    """Stage 1 of space selection (specs/spaces.md §6, D2/D7): every space ACROSS ALL USERS whose stored
    geolocation is within `_GEO_RANGE_M` of (lat, lon), each with its surface constellation for the
    client's registration vote. Geolocation only NARROWS the field (two rooms at one address both qualify);
    the client's `RoomSnap.selectSpace` picks the exact one. Nearest-first is just a tiebreak — the
    geometric vote, not distance, decides. A filesystem walk over every user's spaces; index later."""
    out = []
    for owner in spaces.list_users():
        for name in spaces.list(owner):
            sp = spaces.load(owner, name)
            g = sp.get("geolocation")
            if not g:
                continue                                   # un-located spaces can't be matched by GPS
            d = _haversine_m((lat, lon), (g["lat"], g["lon"]))
            if d <= _GEO_RANGE_M:
                out.append({"owner": owner, "name": name, "distance_m": round(d, 1),
                            "last_scope": sp.get("last_scope"), "last_world": sp.get("last_world"),
                            "surfaces": [_candidate_surface(s) for s in sp.get("surfaces", [])]})
    out.sort(key=lambda c: c["distance_m"])
    return out


_forced_geo_warned: set[str] = set()


def _forced_geo() -> tuple[float, float] | None:
    """TEST override for the client's reported location (`--force-geo` / `CONJURE_FORCE_GEO`) — exercise the
    space-selection flow from a stationary laptop:
        "zero"                    → (0, 0): a convenient "somewhere else" (far from any real space ⇒ drives
                                    the new-place mint path).
        "/<user>/spaces/<name>"   → that space's stored geolocation (pretend you're back at a known place ⇒
                                    drives the candidate / return-visit path).
    Returns (lat, lon), or None when unset or unresolvable (a one-time warning is logged, then the real
    reported location is used)."""
    spec = (settings.force_geo or "").strip()
    if not spec:
        return None
    if spec.lower() == "zero":
        return (0.0, 0.0)
    if spec.startswith("/"):                                    # /<user>/spaces/<name> (dir/delete path form)
        segs = [s for s in spec.strip("/").split("/") if s]
        if len(segs) == 3 and segs[1] == "spaces" and spaces and spaces.exists(segs[0], segs[2]):
            g = spaces.load(segs[0], segs[2]).get("geolocation")
            if g:
                return (g["lat"], g["lon"])
    if spec not in _forced_geo_warned:
        _forced_geo_warned.add(spec)
        print(f"[conjure] --force-geo {spec!r} could not be resolved (use 'zero' or /<user>/spaces/<name> "
              f"of a geo-stamped space); using the real reported location")
    return None


def _apply_forced_geo(req) -> None:
    """If --force-geo is set, override the request's reported lat/lon (test-only; see `_forced_geo`)."""
    forced = _forced_geo()
    if forced:
        req.lat, req.lon = forced


def _cid(req) -> str:
    """The committing client's identity for select idempotency: its per-page-load id, else the reporting
    user, else "" (a shared anonymous id — enough for a single un-identified caller in tests)."""
    return req.cid or req.user or ""


@app.post("/geolocation")
async def report_geolocation(req: GeoReport) -> dict:
    """Stage 1 (discovery) of space selection. The AR client reports its coarse location; we return every
    geo-near candidate space across all users (each with its surface constellation) for the client to
    disambiguate by registration (`RoomSnap.selectSpace`) and then commit via `/space/select`. **Read-only**
    — it never changes the active space. Each client commits ONCE per claim epoch (see `/space/select`);
    once it has, later reports from that client return no candidates so GPS jitter can't re-open its choice
    (a DIFFERENT, co-located AR client still gets candidates — it must vote to pass the admission gate)."""
    if spaces is None:
        return {"ok": False, "error": "no space store"}
    if _cid(req) in _selected_cids:
        return {"ok": True, "selected": True, "candidates": []}
    _apply_forced_geo(req)                                     # test-only geolocation override (--force-geo)
    cands = _geo_candidates(req.lat, req.lon)
    _slog("geo", f"report user={req.user!r} ({req.lat:.5f},{req.lon:.5f}) → {len(cands)} candidate(s): "
                 + ", ".join(f"{c['owner']}/{c['name']}@{c['distance_m']}m" for c in cands))
    return {"ok": True, "candidates": cands}


@app.post("/space/select")
async def select_space(req: SpaceSelect) -> dict:
    """Stage 2 (commit) of space selection + the AR **admission gate** (specs/spaces.md §6.1/§6.3). The
    client has voted its live capture against the /geolocation candidates; what happens depends on whether
    the active space is already CLAIMED (an AR headset is holding it — see `_occupied`):

      • **Unclaimed (provisional)** — this AR user ESTABLISHES the space (first-in claims it, D1/D7):
          - **matched** → JOIN that space's last-active world (return visit); if it has no world yet, mint
            the connecting user a default world tied to it (D3).
          - **not matched** → "somewhere new": mint a fresh geo-stamped space (`space-N`) + default world
            owned by the connecting user (D2/D7). Born WITH its location — no separate "stamp" path.
      • **Claimed (occupied)** — the ADMISSION GATE (D4/D6): the AR joiner must be in the SAME space:
          - **matched the active space** → ADMITTED (co-location join — no world change; they're already
            looking at it).
          - **anything else** (matched a different space, or no match) → REFUSED: not switched in, nothing
            minted; the client shows an info message and stays in passthrough. Voice/CLI/desktop never reach
            here (no AR session → no /space/select), so the gate governs AR headsets only.

    A client commits ONCE per claim epoch (idempotent by `cid`) so GPS jitter / repeated votes can't thrash
    the choice. On admit/establish the client declares `hold` over /ws → it becomes a `_space_holder`."""
    global _selected_cids
    if spaces is None or worlds is None:
        return {"ok": False, "error": "no space store"}
    cid = _cid(req)
    if cid in _selected_cids:
        return {"ok": True, "selected": False}             # this client already committed this claim epoch
    _apply_forced_geo(req)                                  # test-only geolocation override (--force-geo)
    who = req.user or active_scope.split("/", 1)[0]        # the connecting user (owns anything minted)
    geo = {"lat": req.lat, "lon": req.lon} if req.lat is not None and req.lon is not None else None
    matched_ref = (req.owner, req.name) if (req.matched and req.owner and req.name) else None

    # --- Claimed + occupied: admission gate. The joiner must match the ACTIVE space, else they're refused.
    if _occupied():
        _selected_cids.add(cid)
        if matched_ref == (active_space_owner, active_space):
            _slog("select", f"user={who!r} ADMITTED to {active_space_owner}/{active_space} (co-located)")
            return {"ok": True, "admitted": True, "joined": _space_ref(active_space_owner, active_space)}
        _slog("select", f"user={who!r} REFUSED — not in the active space {active_space_owner}/{active_space} "
                        f"(voted {matched_ref or 'no-match'})")
        return {"ok": True, "refused": True,
                "msg": f"You're not in {active_space_owner}'s space — content stays hidden here."}

    # --- Unclaimed (provisional boot / everyone left): this AR user ESTABLISHES the space.
    _selected_cids.add(cid)

    # specs/spaces.md §4.3 — a DELIBERATELY room-less world is not relocated by recognising the room you're standing in.
    # The client votes its capture against the candidates even here, and must: without it, an outdoor
    # re-entry never resolves a space at all. But resolving WHICH space you are in and MOVING you to that
    # space's last world are two different things, and only the first is wanted when you chose to be
    # nowhere. So: claim the space (occupancy + boundary are still real) and stay put.
    #
    # This is only safe because UNSET exists (§4.3). A boot placeholder is room-less too, and relocating it
    # is exactly right — it is a guess, not a choice. Were both spelled VOID, this branch would strand a
    # headset user in a blank world.
    if active_space == VOID:
        _slog("select", f"user={who!r} matched {req.owner}/{req.name} but the live world is outdoor "
                        f"→ space claimed, NOT relocating")
        await _broadcast({"type": "notice", "text": "You're in a world with no room — staying put."})
        return {"ok": True, "admitted": True, "kept_outdoor": True,
                "msg": "You're in an outdoor world, so I've left you in it."}

    if matched_ref and spaces.exists(*matched_ref):        # matched → join a world of ITS history
        sp = spaces.load(*matched_ref)
        was = (active_scope, active_world)                 # where the restart/last session left us
        scope, w, why = _space_last_live(sp, who)
        _slog("select", f"user={who!r} MATCHED {req.owner}/{req.name} → "
                        + (f"join {scope.split('/', 1)[0]}/{w}{f' (skipped {why})' if why else ''}"
                           if w else f"no world to join here{f' ({why})' if why else ''}, mint one"))
        if w and scope:
            if why:
                # Walked past the most recent entry. Rung 1 of the ladder, but a visible one — you asked
                # for nothing and got a different world than you last had in this room, so say which and
                # say why: "gone" and "private" call for different reactions from the person hearing it.
                await _broadcast({"type": "notice", "text":
                                  (f"The world you were last in here is gone — opened "
                                   if why == "gone" else
                                   f"The most recent world here is private — opened ")
                                  + f"'{worlds.name_of(scope, w) or w}' instead."})
            out = await _switch_to(scope, w)
        elif _may_create_world_in(who, *matched_ref):      # nothing of this space's survives → build (D3)
            # Keep the AGENT that space was last used from rather than dropping to the default one (§2),
            # and SAY that we had to build — a silent fallback is indistinguishable from a bug. NOT a
            # sibling from that session: see `_space_last_live` on why reaching sideways is wrong here.
            prior = sp.get("last_scope")
            landing = _entry_scope_for(who, prefer=prior)
            if prior:
                _slog("select", f"user={who!r} no joinable world in this space ({why or 'none'}) → "
                                f"new world in {landing} (was {prior})")
                await _broadcast({"type": "notice", "text":
                                  ("The worlds here are private — " if why == "private" else
                                   "The worlds you had here are gone — ")
                                  + f"starting a fresh one in {agent_of(landing)}."})
            out = await _establish_world_in(who, _space_ref(*matched_ref), prefer_scope=prior)
        else:                                              # private space, nothing to join, not the owner (D8)
            _slog("select", f"user={who!r} matched {req.owner}/{req.name} but it's PRIVATE with no world → refused")
            return {"ok": True, "refused": True,
                    "msg": f"{matched_ref[0]}'s space is private — there's no world here you can join."}
        # You were somewhere else a moment ago and the room moved you. That is the design working — the
        # space owns the world owns the scope, and your room is the better evidence of where you are
        # (decision #20) — but it has to SAY so, and until now it only did when the AGENT changed
        # (`_agent_change_notice`). Restart in a different room under the same agent and you were
        # relocated in silence. Announce the same fact the agent notice would have carried, minus the
        # agent, and only when that notice will NOT fire, so nobody hears it twice.
        if (active_scope, active_world) != was and agent_of(active_scope) == agent_of(was[0]):
            await _broadcast({"type": "notice",
                              "text": f"You're in {_space_ref(*matched_ref)} now — opened "
                                      f"'{worlds.name_of(active_scope, active_world)}'."})
        out["joined"] = _space_ref(*matched_ref)
        return out

    # not matched → somewhere new: mint a geo-stamped space + world owned by the connecting user
    new_space = _unique_space_name(who)
    _slog("select", f"user={who!r} NO-MATCH → mint {who}/{new_space}")
    spaces.save(who, new_space, {"owner": who, "name": new_space, "public": True,
                                 "geolocation": geo, "surfaces": [], "boundary": None})
    spaces.set_active(who, new_space)
    out = await _establish_world_in(who, _space_ref(who, new_space), world_name=new_space)
    out["created_space"] = _space_ref(who, new_space)
    return out


def _entry_scope_for(user: str, *, prefer: Optional[str] = None) -> str:
    """Which scope to mint a replacement world in when a pointer went stale (architecture.md §1;
    specs/spaces.md §6.1).

    **Degrade to the next-broadest thing that is still true, never to a global default.** The user's
    intent — *put me back with the agent I was using* — survives a missing world, so preserve the AGENT
    and let the caller own the world as themselves:

        the space's remembered scope  →  the live scope  →  the default agent

    A candidate is skipped when its agent no longer resolves on the search path (deleted or renamed), or
    when it declares `world.outdoor` — an outdoor agent's worlds are room-less by declaration (specs/agents.md §3), so
    it cannot host a world tied to a space and preferring it would contradict its own definition.

    This is what fixed coming back as the *builder*: the scope was hard-coded, so a space whose remembered
    world had been deleted handed you to a general-purpose agent regardless of who you were with."""
    for cand in (prefer, active_scope):
        agent = agent_of(cand) if cand else ""
        if not agent:
            continue
        scope = scope_for(user, agent)
        if _agent_wants_outdoor(scope):          # can't tie an outdoor agent's world to a space
            continue
        try:
            resolve_agent_dir(agent)             # still on the search path?
        except Exception:                        # noqa: BLE001 — deleted/renamed agent: try the next
            continue
        return scope
    return scope_for(user, "builder")


async def _establish_world_in(user: str, space_ref: str, world_name: str = "default", *,
                              prefer_scope: Optional[str] = None) -> dict:
    """Create `world_name` in `user`'s scope tied to `space_ref` and switch into it — the connecting user
    building their own world in a (possibly someone else's) space (D3). `prefer_scope` names the agent
    this should land in if it still can (`_entry_scope_for`)."""
    scope = _entry_scope_for(user, prefer=prefer_scope)
    fresh = _new_world_store(scope)
    fresh.doc.setdefault("environment", {})["space"] = space_ref
    return await _switch_to(scope, world_name, store_override=fresh)


@app.post("/worlds/list")
async def worlds_list(req: ScopeRef) -> dict:
    """The caller's own worlds in their current session (docs/specs/agents.md §7.2) — NOT other users'
    worlds: cross-user discovery/visiting is a human act at the shell, never handed to the agent. `active`
    is the caller's OWN world that is live, or null when the live (shared) world is someone else's;
    `current` is always the true live world `{owner, name}` so the agent knows when it's inhabiting
    another user's shared world (it can be there, but can't edit it)."""
    in_own = req.scope == active_scope
    current = {"owner": active_scope.split("/", 1)[0], "id": active_world,
               "name": worlds.name_of(active_scope, active_world)}
    # `{id, name}` pairs, not bare names: the id is what survives a rename, so it's what an agent should
    # store in its state and hand back to `switch_world`.
    return {"ok": True, "worlds": worlds.entries(req.scope),
            "active": active_world if in_own else None, "current": current}


@app.post("/worlds/new")
async def worlds_new(req: WorldRef) -> dict:
    """Create a new world from the agent's constructor and switch to it. The world gets a permanent
    `wld_…` id; `name` is just its (unique-within-the-session) display name."""
    try:
        if worlds.exists(req.scope, req.name):
            return {"ok": False, "error": f"world {req.name!r} already exists — switch to it instead"}
        creator = req.scope.split("/", 1)[0]
        outdoor = req.outdoor or _agent_wants_outdoor(req.scope)   # per-request OR per-agent (§3)
        # D8/step 6: the active space must let the creator build here — their own space, or a PUBLIC one.
        # A PRIVATE space owned by someone else restricts world-creation to its owner (VOID isn't a space).
        # An outdoor world wants no space at all, so the permission question doesn't arise.
        if not outdoor and not _no_space() and not _may_create_world_in(
                creator, active_space_owner, active_space):
            return {"ok": False, "error": f"{active_space_owner}'s space is private — "
                                          f"only {active_space_owner} can build worlds here."}
        # Visibility is the SESSION's now (§8.2): a new world inherits it — no per-world public flag. `req.public`
        # is vestigial for worlds; use `session public|private` to change the session's visibility.
        # D5/step 5: the world ADOPTS the active, geo+surface-selected space — "build your own world in it",
        # even someone else's (D3) — or VOID when outdoor / nothing is live. That stamp is `_new_world_store`'s
        # job now, shared with every other mint path (agent switch, session mint) so none can forget it.
        fresh = _new_world_store(req.scope, outdoor=outdoor)
        # Mint the id here so the switch addresses the world by identity from the very first moment.
        wid = new_world_id()
        fresh.doc["id"], fresh.doc["name"] = wid, req.name
        return await _switch_to(req.scope, req.name, store_override=fresh, wid=wid)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/worlds/switch")
async def worlds_switch(req: WorldRef) -> dict:
    """Switch to a world in the caller's OWN scope — there is no world-level cross-user switch
    (docs/specs/agents.md §7.2). Entering another user's world is a human act — visit their public session at the
    shell (`session switch <owner>/<agent>/<sid>`)."""
    try:
        if not worlds.exists(req.scope, req.name):
            return {"ok": False, "error": f"no world {req.name!r} (create it with new_world)"}
        return await _switch_to(req.scope, req.name)   # accepts an id or a name
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/worlds/delete")
async def worlds_delete(req: WorldRef) -> dict:
    try:
        if req.scope == active_scope and worlds.resolve(req.scope, req.name) == active_world:
            return {"ok": False, "error": "can't delete the active world — switch away first"}
        return {"ok": worlds.delete(req.scope, req.name), "world": req.name}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


class WorldRenameRequest(BaseModel):
    name: str                         # the world to rename — its id, or its current display name
    new_name: str
    scope: str = DEFAULT_SCOPE
    session: Optional[str] = None     # id or title; default = the scope's live/active session. Worlds are
    #                                   stored PER SESSION, so a bare scope silently means "the live one" —
    #                                   which renamed the wrong world when the path named another session.


class SpaceRenameRequest(BaseModel):
    name: Optional[str] = None        # the space to rename — its id (`space-1`) or name; default: current
    new_name: str = ""
    owner: Optional[str] = None       # default: the active space's owner (you)


@app.post("/worlds/rename")
async def worlds_rename(req: WorldRenameRequest) -> dict:
    """Retitle a world. A metadata edit that moves nothing: every reference — the active pointers, the
    session's `active_world`, a space's `last_world`, another user's `environment.space`, and whatever a
    schema-free agent state doc stashed — holds the permanent id, not the name.

    `session` names WHICH session's worlds to look in. Without it we fall through to `worlds.rename`,
    which routes to the scope's live/active session — right for the live case, wrong (silently, on a
    same-named world) the moment a caller means a world in some other session."""
    sid = _resolve_sid(req.scope, req.session) if req.session else None
    if req.session and not sid:
        return {"ok": False, "error": f"no session {req.session!r} in {req.scope}"}
    try:
        if sid:
            wid = _session_worlds(req.scope, sid).rename(req.name, req.new_name)
        else:
            wid = worlds.rename(req.scope, req.name, req.new_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if wid == active_world and req.scope == active_scope and sid in (None, active_sid):
        # The LIVE world is held in memory and autosaved back to disk, so the rename has to land there
        # too — otherwise the next `_save_active()` writes the old name straight over it.
        store.doc["name"] = req.new_name
        await _broadcast({"type": "world_renamed", "id": wid, "name": req.new_name})
    _slog("world", f"rename {wid} → {req.new_name!r}")
    return {"ok": True, "id": wid, "name": req.new_name}


@app.post("/space/rename")
async def space_rename(req: SpaceRenameRequest) -> dict:
    """Give one of YOUR spaces a human name. The file key (`space-1`) is its permanent id and never
    changes, so worlds pointing at it — including other users' worlds, which we may not rewrite — are
    untouched."""
    if spaces is None:
        return {"ok": False, "error": "no space store"}
    owner = req.owner or active_space_owner
    try:
        sid = spaces.rename(owner, req.name or active_space, req.new_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    _slog("space", f"rename {owner}/{sid} → {req.new_name!r}")
    return {"ok": True, "id": sid, "name": req.new_name}


class WorldVisibilityRequest(BaseModel):
    public: bool
    scope: str = DEFAULT_SCOPE
    name: Optional[str] = None        # default: the caller's currently-active world ("make THIS private")


@app.post("/worlds/visibility")
async def worlds_visibility(req: WorldVisibilityRequest) -> dict:
    """**Superseded by session visibility (§8.2)** — visibility is the SESSION's now, not per-world, and a
    world inherits it. Kept as the surface the `set_world_visibility` tool still calls: it sets the target
    scope's ACTIVE session public/private (so "make this world private" makes the session private), and —
    when the LIVE session goes public — publishes its world's private assets so visitors can load them.
    Use `/session/visibility` (or `session public|private`) directly for the same effect."""
    try:
        sid = active_sid if req.scope == active_scope else (sessions.get_active(req.scope) or MIGRATED_SID)
        if not sessions.exists(req.scope, sid):
            return {"ok": False, "error": f"no session to change visibility for in {req.scope}"}
        owner = req.scope.split("/", 1)[0]
        meta = sessions.load_meta(req.scope, sid)
        meta["public"] = req.public
        sessions.save_meta(req.scope, sid, meta)
        published = []
        live = req.scope == active_scope and sid == active_sid
        if req.public and req.scope == active_scope:   # the live session went public → publish its assets
            published = _publish_world_assets(store.doc, owner)
            _save_active()
        if live:
            await _propagate_visibility()              # bump/re-admit headset + broadcast → agent re-gates
        return {"ok": True, "world": req.name or active_world, "session": sid,
                "public": req.public, "published_assets": published}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


class SpaceVisibilityRequest(BaseModel):
    public: bool
    scope: str = DEFAULT_SCOPE
    name: Optional[str] = None        # default: your CURRENT space (only if you own the active one)


@app.post("/space/visibility")
async def space_visibility(req: SpaceVisibilityRequest) -> dict:
    """Make one of YOUR spaces public or private (D8/step 6). A PUBLIC space lets any admitted (co-located)
    user build their OWN worlds in it; a PRIVATE space restricts world-creation to you. **Not retroactive**
    — existing worlds tied to the space are untouched; it only blocks NEW ones by others (and joining/
    viewing still follows each world's own visibility + co-location). Scope-bound like `/worlds/visibility`:
    you can only change a space you own, so it isn't middleware-gated on the active world's owner. `name`
    omitted ⇒ your CURRENT space (only when you own the active one)."""
    if spaces is None:
        return {"ok": False, "error": "no space store"}
    owner = req.scope.split("/", 1)[0]
    name = req.name
    if not name:                                       # "make THIS space private"
        if _no_space() or active_space_owner != owner:
            return {"ok": False, "error": "you're not in one of your own spaces — name the space to change"}
        name = active_space
    if not spaces.exists(owner, name):
        return {"ok": False, "error": f"no space {name!r} of yours"}
    sp = spaces.load(owner, name)
    sp["public"] = req.public
    spaces.save(owner, name, sp)
    _slog("space", f"{owner}/{name} visibility → {'public' if req.public else 'private'}")
    return {"ok": True, "space": _space_ref(owner, name), "public": req.public}


# ---- admin: the namespace as a filesystem (shell `dir` / `show` / `delete`) ----------------------
#
# The view itself — path resolution, listings, per-entry detail and the delete dispatch — lives in
# `conjure.namespace`, bound to this module at startup so it reads the live repositories and pointers.
# What stays here is the HTTP surface and the two addressing helpers the rest of the server also uses
# (`_active_sid_for`, `_session_worlds`).


class AdminPath(BaseModel):
    path: str = "/"


def _active_sid_for(scope: str) -> str:
    """The session `worlds.<op>` would address for this scope — mirrors `WorldRepository._dir` exactly,
    MIGRATED_SID fallback included, so the `worlds` shortcut and world addressing can never disagree."""
    if scope == active_scope:
        return active_sid
    return sessions.get_active(scope) or MIGRATED_SID


def _session_worlds(scope: str, sid: str):
    """The WorldDir for one specific session — `worlds.<op>` only ever reaches the ACTIVE one."""
    return sessions.worlds(scope, sid)


@app.post("/admin/tree")
async def admin_tree(req: AdminPath) -> dict:
    """One level of the namespace at `path` (shell `dir`)."""
    loc = namespace.resolve(req.path)
    if isinstance(loc, str):
        return {"ok": False, "error": loc}
    if loc.kind in ("world", "space", "asset"):                # a leaf lists as itself
        row = namespace.leaf_row(loc)
        if row is None:
            return {"ok": False, "error": f"no {loc.kind} {loc.name!r}"}
        return {"ok": True, "path": namespace.loc_path(loc), "kind": loc.kind, "self": row, "children": [row]}
    # `self` is the row for the node ITSELF when it has one (a session's own summary, say). A session's
    # children are just `worlds/` and `state/`, so without this a delete confirmation for one could only
    # say "nothing" — see Shell._summarize.
    return {"ok": True, "path": namespace.loc_path(loc), "kind": loc.kind,
            "self": namespace.leaf_row(loc), "children": namespace.children(loc)}


@app.post("/admin/show")
async def admin_show(req: AdminPath) -> dict:
    """One entry in depth (shell `show`) — the detail `dir`'s one-line rows leave out."""
    loc = namespace.resolve(req.path)
    if isinstance(loc, str):
        return {"ok": False, "error": loc}
    return {"ok": True, "path": namespace.loc_path(loc), "kind": loc.kind, "fields": namespace.fields(loc)}


@app.post("/admin/delete")
async def admin_delete(req: AdminPath, request: Request) -> dict:
    """Purge whatever `path` points at (shell `delete`, post-confirm). Ownership-gated (§6e): the caller
    (X-Conjure-User) may only delete their OWN namespace. A missing caller header is treated as
    trusted-local (back-compat, mirroring `_owner_only_writes`)."""
    loc = namespace.resolve(req.path)
    if isinstance(loc, str):
        return {"ok": False, "error": loc}
    if loc.kind == "root":
        return {"ok": False, "error": "refusing to delete everything — name a user (e.g. /alice)"}
    caller = request.headers.get("X-Conjure-User")
    if caller and caller != loc.user:
        return {"ok": False, "error": f"you can only delete your own namespace — {loc.user!r} isn't yours."}
    try:
        return namespace.delete(loc)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def _reanchor_moved_content_ops(applied_ops: list[dict]) -> list[dict]:
    """After a /patch moved or rotated ANCHORED content, re-author its persisted plane-relative anchor (§7c)
    from the NEW pose. Otherwise the client's per-capture anchor solve keeps re-deriving the OLD pose and
    reverts the edit — the "flash then snap back" on move/rotate. Only touches content that already carries
    meta.anchor; real surfaces and un-anchored content (placed from a raw F_ref pose) are left alone."""
    moved, seen = [], set()
    for op in applied_ops:
        if op.get("op") != "update" or op.get("id") is None or op["id"] in seen:
            continue
        if any(str(k).startswith(("transform.position", "transform.rotation")) for k in (op.get("set") or {})):
            moved.append(op["id"]); seen.add(op["id"])
    ents = {e["id"]: e for e in store.doc["entities"]}
    out = []
    for eid in moved:
        e = ents.get(eid)
        if not e or not (e.get("meta") or {}).get("anchor"):
            continue
        anchor = _content_anchor(e.get("transform") or {}, (e.get("meta") or {}).get("placement") or "grounded")
        if anchor:
            out.append({"op": "update", "id": eid, "set": {"meta.anchor": anchor}})
    return out


@app.post("/patch")
async def post_patch(patch: Patch) -> dict:
    ops: list[dict] = []
    for op in (o.model_dump() for o in patch.ops):
        # Resolve update/remove targets so a friendly number / semantic / 'wall 4' works in the generic
        # tools too (not just the surface tools). Exact ids pass through unchanged; a target that maps to
        # several surfaces (e.g. 'wall') fans out to one op each.
        if op.get("op") in ("update", "remove") and op.get("id") is not None:
            ids = _resolve_op_ids(op["id"])
            if ids and ids != [op["id"]]:
                ops.extend({**op, "id": i} for i in ids)
                continue
        ops.append(op)
    applied = store.apply_patch(ops, origin=patch.origin)
    # A move/rotate of anchored content must re-author its anchor (§7c) or the client reverts it next capture.
    reanchor = _reanchor_moved_content_ops(applied["ops"])
    if reanchor:
        extra = store.apply_patch(reanchor, origin=patch.origin)
        applied = {"rev": extra["rev"], "origin": patch.origin,
                   "ops": applied["ops"] + extra["ops"], "inverse": extra["inverse"] + applied["inverse"]}
    await _broadcast({"type": "patch", "patch": applied})
    return applied


# --- Room model: the client→server reverse channel (a headset reports its real room) ------------
# Captured surfaces become `real`-tagged stylable entities; `environment.room` holds the boundary,
# active flag, and the single room **authority** (only that headset may report room geometry).
# See docs/specs/worlds-surfaces.md.

class RoomSurface(BaseModel):
    id: str                                   # stable id from the headset, e.g. "real_wall_3"
    semantic: str = "surface"                 # wall | floor | ceiling | table | …
    position: list[float]
    rotation: Optional[list[float]] = None
    polygon: Optional[list[list[float]]] = None   # 2D outline in the surface plane
    extent: Optional[list[float]] = None          # [w, h]
    holes: Optional[list[dict]] = None            # wall openings (door/window) {x,y,w,h} in wall-local 2D
    mesh_segment: Optional[str] = None            # segment id when backed by the refined mesh
    hostWall: Optional[str] = None                # for an inset (door/window/wall-art): the wall id it belongs to,
                                                  # derived once by the authority's snapInsets → stored + reused on recovery (§5.2)
    # Corner-relative structural anchor for an inset (docs/specs/spaces-geometry.md §6.1): its place on the
    # host wall as distances to SHARED features — signed along-wall distances from the host wall's corner
    # points, and perpendicular distances from the wall∩floor / wall∩ceiling edges — never the wall's
    # scan-artifact centroid. Any client reconstructs the same physical spot from its OWN captured
    # corners/edges (client reconstructInset), which is what lands a guest's insets right.
    along: Optional[list[dict]] = None            # [{"corner": <partner wall id>, "dist": float}]
    vertical: Optional[list[dict]] = None         # [{"edge": "floor"|"ceiling", "dist": float}]
    structuralFallback: Optional[str] = None      # set when a structural ref was missing (e.g. "freestanding")
    debug: Optional[dict] = None                  # raw pose/label for diagnosis (stored in meta)


class RoomUpdate(BaseModel):
    client_id: str                            # which headset is reporting
    surfaces: list[RoomSurface] = []
    boundary: Optional[dict] = None           # {floorPolygon: [[x,z]…], height: float}
    replace: bool = True                      # replace the whole real-surface set vs merge


def _surface_entity(s: RoomSurface) -> dict:
    """A fresh `real` surface entity. Visibility/style are left to the renderer default
    (environment.spacePresentation.defaultSurfaceVisible) + later director edits, so re-capture never clobbers
    a director's color/visibility (those go through update, below)."""
    transform: dict = {"position": s.position}
    if s.rotation is not None:
        transform["rotation"] = s.rotation
    surface: dict = {}
    if s.polygon is not None:
        surface["polygon"] = s.polygon
    if s.extent is not None:
        surface["extent"] = s.extent
    if s.holes is not None:
        surface["holes"] = s.holes
    meta = {"real": True, "semantic": s.semantic, "source": "headset",
            "friendly_id": _friendly_id_for(s.id)}
    if s.mesh_segment is not None:
        meta["meshSegment"] = s.mesh_segment
    if s.hostWall is not None:
        meta["host_wall"] = s.hostWall            # inset → its wall, so recovery snaps to it (not by distance)
    if s.along is not None:
        meta["along"] = s.along                   # §5.3 corner-relative anchor — along-wall corner distances
    if s.vertical is not None:
        meta["vertical"] = s.vertical             # …and floor/ceiling edge distances
    if s.structuralFallback is not None:
        meta["structural_fallback"] = s.structuralFallback
    if s.debug is not None:
        meta["debug"] = s.debug
    return {
        "id": s.id,
        "transform": transform,
        "components": {"surface": surface, "material": _default_surface_material(s.semantic)},
        "meta": meta,
    }


# Per-semantic fill defaults. Doors and windows are cut OUT of their wall (an actual opening), so their
# leaf sits in the hole: a door is a faint translucent pane (see into the next room), a window is faint
# glass with a cool tint (see outside). Everything else is an opaque panel. The director adjusts these as
# plain properties (opacity/color/texture); re-capture preserves those edits (update-in-place keeps the
# material), so these only seed a surface's first appearance.
def _default_surface_material(semantic: str) -> dict:
    mat = {"shader": "flat", "color": "#888", "side": "double", "opacity": 1.0}
    sem = (semantic or "").lower()
    if sem == "door":
        mat.update(opacity=0.25, transparent=True)
    elif sem == "window":
        mat.update(color="#cfe6ff", opacity=0.18, transparent=True)
    return mat


# ---- space ↔ world composition (docs/specs/spaces.md §2/§4) -------------------------------
# A SPACE owns the real-surface geometry (+ a base material) and the boundary, shared across a user's
# worlds. A WORLD owns placed objects, display prefs, and per-surface style OVERRIDES (material that
# differs from the space's base), keyed by surface id in environment.spacePresentation.surfaceStyles. The live
# store.doc stays the COMPOSED shape below (so client/patch/director are unchanged); only persistence
# splits — _compose on load, _decompose on save.

def _compose(world_doc: dict, space: dict) -> dict:
    """Live doc: the world's placed entities + prefs, merged with the space's real-surface geometry —
    each surface's material = the space's base, overridden by world.environment.spacePresentation.surfaceStyles[id].
    Boundary comes from the space. The surfaceStyles map and `space` ref are persistence-only (dropped)."""
    doc = copy.deepcopy(world_doc)
    env = doc.setdefault("environment", {})
    env.pop("space", None)
    doc.pop("space", None)
    pres = env.setdefault("spacePresentation", {})
    styles = pres.pop("surfaceStyles", {}) or {}
    placed = [e for e in doc.get("entities", []) if not e.get("meta", {}).get("real")]
    reals = []
    for s in space.get("surfaces", []):
        e = copy.deepcopy(s)
        ov = styles.get(e.get("id"))
        if ov:
            comps = e.setdefault("components", {})
            comps["material"] = {**comps.get("material", {}), **ov}
        reals.append(e)
    doc["entities"] = placed + reals
    if space.get("boundary") is not None:
        env["boundary"] = space["boundary"]        # geometry on loan from the space, live-only
    # A world INHERITING a non-empty space's geometry (created new / switched-to / reset) genuinely has a
    # room, even with no live headset ingest this session — so mark it active for the director's query_room
    # (which gates on spacePresentation.active). Only default it: an explicit False (a director immersion mode like
    # vr_unbounded, mcp_server.py) is respected. spacePresentation.active only ever meant "a room exists to work with".
    if reals and "active" not in pres:
        pres["active"] = True
    _reanchor_surface_images(doc)              # re-pin on-surface images to the (possibly moved) surfaces
    return doc


def _decompose(composed: dict, space: dict) -> dict:
    """Inverse of _compose for persistence: the world doc to save — placed entities + prefs + the
    `surfaceStyles` overrides (each real surface's material where it differs from the space's base).
    Real-surface GEOMETRY + boundary are the space's job (persisted separately)."""
    doc = copy.deepcopy(composed)
    base = {s["id"]: s.get("components", {}).get("material", {}) for s in space.get("surfaces", [])}
    overrides, placed = {}, []
    for e in doc.get("entities", []):
        if e.get("meta", {}).get("real"):
            mat = e.get("components", {}).get("material", {})
            if mat and mat != base.get(e.get("id")):
                overrides[e["id"]] = mat
        else:
            placed.append(e)
    doc["entities"] = placed
    env = doc.setdefault("environment", {})
    env.pop("boundary", None)                      # the space owns the boundary; never persisted per-world
    pres = env.setdefault("spacePresentation", {})
    if overrides:
        pres["surfaceStyles"] = overrides
    else:
        pres.pop("surfaceStyles", None)
    return doc


def _space_from_world_doc(user: str, name: str, doc: dict) -> dict:
    """Extract a space's geometry from a COMPOSED (live) world doc — the save-time counterpart of
    `_compose`. Real surfaces become the space's geometry carried at per-semantic DEFAULT materials
    (per-world styling is split off separately as surfaceStyles by `_decompose`), plus the boundary.
    The space is user-owned and public by default (specs/spaces.md §2). Used by `_save_active`
    to persist newly-captured walls back into the shared space."""
    surfaces = []
    for e in doc.get("entities", []):
        if e.get("meta", {}).get("real"):
            s = copy.deepcopy(e)
            s.setdefault("components", {})["material"] = _default_surface_material(
                s.get("meta", {}).get("semantic", "surface"))
            surfaces.append(s)
    boundary = doc.get("environment", {}).get("boundary")
    return {"owner": user, "name": name, "public": True, "geolocation": None,
            "surfaces": surfaces, "boundary": boundary}


def _resolve_space_ref(ref: str, world_owner: str) -> tuple[str, str]:
    """A world's `environment.space` references a SHARED space that may belong to ANOTHER user (D3 —
    "build your own world in someone else's space"). Parse it into `(space_owner, space_name)`:

        "<owner>/<name>"  → that owner's space (fully-qualified — the target form)
        "<name>"          → a bare/legacy reference → the world's OWN owner (back-compat)

    VOID is not a space and is handled by callers before this is reached."""
    if "/" in ref:
        owner, sname = ref.split("/", 1)
        return owner, sname
    return world_owner, ref


def _space_ref(owner: str, name: str) -> str:
    """The fully-qualified `environment.space` value for a space — `<owner>/<name>` (D3). Persisted so a
    world remembers WHOSE space it's tied to, even when the space's owner isn't the world's owner."""
    return f"{owner}/{name}"


def _space_is_public(owner: str, name: str) -> bool:
    """A space's world-creation visibility (D8): a PUBLIC space lets any admitted (co-located) user build
    their OWN worlds in it; a PRIVATE space restricts creation to the owner. Spaces are public by default,
    and an unknown / not-yet-created space is treated as public (it's about to become the caller's own)."""
    return bool(spaces.load(owner, name).get("public", True)) if spaces.exists(owner, name) else True


def _may_create_world_in(user: str, owner: str, name: str) -> bool:
    """D8: `user` may create a world tied to space `owner/name` iff they OWN the space or it is PUBLIC.
    (Joining/viewing an existing world is governed separately by co-location + the WORLD's visibility.)"""
    return user == owner or _space_is_public(owner, name)


def _space_for_new_world(scope: str, *, outdoor: bool = False) -> str:
    """The `environment.space` a freshly-minted world adopts (D5/step 5): the LIVE, geo+surface-selected
    space, so a world created while a headset is standing in a room composes THAT room. VOID — the honest
    "no room here" — in three cases:

      - `outdoor`: an explicitly room-less world (skybox only);
      - no space is live (`active_space == VOID`) — an unclaimed server, or a void/outdoor world;
      - the creator may not build in the live space (`_may_create_world_in` — someone else's PRIVATE
        space). `/worlds/new` REFUSES that outright because the user asked for it explicitly; the
        implicit mint paths (agent switch, session mint) must not fail a navigation, so they degrade to
        VOID rather than silently adopting a space the creator has no right to build in.

    Deliberately NOT gated on the space existing on disk: a live space is persisted lazily (`_save_active`
    /autosave), so it is routinely real-but-unflushed at mint time. The one caller that runs before any
    space is resolved passes `adopt_space=False` instead.
    """
    if outdoor or _no_space():
        return VOID
    if not _may_create_world_in(scope.split("/", 1)[0], active_space_owner, active_space):
        return VOID
    return _space_ref(active_space_owner, active_space)


def _activate(scope: str, name: str, world: WorldStore) -> tuple[str, str, WorldStore]:
    """Make `world` live: resolve the SPACE it references and COMPOSE the render doc against it.

    A world is stored geometry-free — it carries only placed objects, display prefs, and per-surface
    style overrides. The real-surface geometry + boundary live in a shared, user-owned *space* (docs/
    specs/spaces.md §2). `environment.space` points a world at its space:

        VOID ("<void>")     → an outdoor/void world: no room to merge — objects + skybox only.
        "<owner>/<name>"    → a shared space, possibly ANOTHER user's (D3, the target form).
        "<name>"            → a bare/legacy ref → the world-owner's own space (back-compat).
        absent              → no space chosen YET → UNSET (D5 step 5 + specs/spaces.md §4.3): renders like VOID
                              (the honest "no room yet", never the old anonymous-'home' fallback), but a
                              headset selecting a space MAY relocate it, where a deliberate VOID may not.

    `_compose` merges the world's objects/prefs with the space's surfaces to build the doc the client
    renders. On the way back out, `_save_active` SPLITS the live doc again (geometry → the space's owner's
    scope, objects + overrides → the world), so geometry only ever flows world→space on real capture.

    Returns `(space_owner, space_name, composed_store)` with room-capture authority reset (fresh session
    state). A room-less world returns `(world_owner, VOID | UNSET, …)` — the owner is irrelevant for it.

    specs/spaces.md §6.1 — the old LEGACY-MIGRATION path is gone (activate is read-only; it never
    rewrites a world doc). **step 2** — space references are now fully-qualified `<owner>/<name>`, so a
    world can be tied to a space owned by someone else. **step 5** — Path B (the `absent → home` fallback)
    is gone: a world with no space ref now composes as VOID, and `/worlds/new` stamps the active space up
    front (see `worlds_new`), so nothing anonymous is ever minted.
    """
    world_owner = scope.split("/", 1)[0]
    doc = world.doc
    space_ref = (doc.get("environment", {}) or {}).get("space")
    if space_ref == VOID or not space_ref:                         # explicit outdoor/void OR no space chosen
        composed_doc = copy.deepcopy(doc)                          # yet: no room to merge — objects +
        composed_doc["entities"] = [e for e in composed_doc.get("entities", [])   # skybox only. Neither
                                    if not (e.get("meta") or {}).get("real")]     # owns real geometry —
        # The LIVE doc says VOID for both, so the client's two-state contract (`isVoidWorld` → canonical
        # frame) is untouched; only the server keeps the third state, in `active_space`.
        composed_doc.setdefault("environment", {})["space"] = VOID
        composed = WorldStore(composed_doc)
        _reset_room_authority(composed)
        return world_owner, (VOID if space_ref == VOID else UNSET), composed
    owner, space_name = _resolve_space_ref(space_ref, world_owner)
    if spaces.exists(owner, space_name):
        space = spaces.load(owner, space_name)                     # the shared physical room
    else:                                                          # first time here → an empty shared space
        space = {"owner": owner, "name": space_name, "public": True, "geolocation": None,
                 "surfaces": [], "boundary": None}
        spaces.save(owner, space_name, space)                      # so geolocation/discovery can see it
        if owner == world_owner:
            spaces.set_active(owner, space_name)                   # only track YOUR OWN space as current
    composed = WorldStore(_compose(doc, space))
    _reset_room_authority(composed)
    return owner, space_name, composed


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    r = 6371000.0
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


# Removal debounce now lives on the CLIENT (docs §7): it posts its confirmed set only when it structurally
# changes, so a surface missing from a post is genuinely gone and the server prunes it at once — no
# server-side absence counter is needed.
# Room authority (the one headset allowed to report geometry) is claimed by the first capturer's
# per-page-load client id and cleared only on world-activate/boot — so a RECONNECTING owner (fresh id)
# used to be locked out until a restart. Fix B: an authority goes STALE after _AUTH_TTL with no post; a
# new capturer then TAKES IT OVER. Safe because /room is already owner-only (middleware), so only the
# active world's owner ever reaches here — the guard is just against two of their live headsets at once.
_AUTH_TTL = 6.0                       # seconds (~3 capture cycles) an idle authority holds before takeover
_authority_ts: float = 0.0            # server time of the last accepted capture from the current authority
# NOTE (local-first, docs/specs/spaces-geometry.md §2): the old ESTABLISH-then-FREEZE machinery (a timed
# window that committed the static shell as a coherent set then froze it) is gone. It existed to stabilize
# the SHARED, server-rendered geometry — but clients now render their OWN capture locally, so the server
# just keeps the stored SEED current (add new / update meaningfully-changed / prune absent) and never
# broadcasts geometry for rendering. What remains: absence-pruning + `anchored` (photo-pinned) protection.


def _dist3(a: list, b: list) -> float:
    return sum((a[i] - b[i]) ** 2 for i in range(3)) ** 0.5


def _ang_delta_deg(a: list, b: list) -> float:
    """Max per-axis angular difference (degrees, wrapped to ±180) between two euler triples."""
    return max(abs(((a[i] - b[i] + 180) % 360) - 180) for i in range(3))


# The seed updates ONLY on a STRUCTURAL change (docs/specs/spaces-geometry.md §8), never on per-capture
# cm-drift — so the stored reference constellation stays stable (clients render their own live geometry
# regardless). Per-cm jitter of the owner's re-registered posts (Tmat re-solves each capture, amplified by
# distance) used to sail through the old cm-level gate and rewrite ~a quarter of the seed every 2 s.
_LARGE_MOVE_M = 0.5        # a surface must move/resize this far (m) to count as a real relocation, not drift
_LARGE_ROT_DEG = 20.0      # ...or rotate this far (deg) — a wall genuinely re-oriented, not registration wobble


def _surface_changes(e: dict, s) -> list[str]:
    """Which STRUCTURAL aspects of `s` differ from the stored seed entity `e` (§7.4)? Sub-threshold
    per-capture drift returns [] so the seed doesn't churn.

    Returns EVERY aspect that changed, not the first — because the caller writes exactly these fields and
    nothing else. That is the whole point: the gate and the payload used to be decoupled, so an
    opening-count change (a legitimate edit to `holes`) rewrote the surface's POSITION too, importing
    whatever frame that capture happened to be in. Observed 2026-08-31: a relocalization put the space
    ~93 mm low, a door appeared on two walls, and the seed absorbed the offset into those walls and a
    ceiling while its other 55 surfaces kept the old frame — leaving the reference internally inconsistent.
    The seed is the baseline the floating-room detector, guest registration, recovery and the server's own
    plane queries all measure against, so a pose written from an untrusted capture reaches all of them.

    `extent` carries `position` with it deliberately: a rectangle's centre and its size are one measurement
    (§9.1's matched-pair rule), and storing a new size against an old centre would be worse than either.
    """
    t = e.get("transform") or {}
    comps = (e.get("components") or {}).get("surface") or {}
    out: list[str] = []
    if s.semantic != (e.get("meta") or {}).get("semantic"):
        out.append("semantic")
    if s.holes is not None and len(s.holes) != len(comps.get("holes") or []):
        out.append("holes")
    if s.position is not None and _dist3(s.position, t.get("position") or [0, 0, 0]) > _LARGE_MOVE_M:
        out.append("position")
    if s.rotation is not None and _ang_delta_deg(s.rotation, t.get("rotation") or [0, 0, 0]) > _LARGE_ROT_DEG:
        out.append("rotation")
    if s.extent is not None:
        ee = comps.get("extent")
        if ee is None or abs(s.extent[0] - ee[0]) > _LARGE_MOVE_M or abs(s.extent[1] - ee[1]) > _LARGE_MOVE_M:
            out.append("extent")
    return out


def _surface_update_set(s, aspects) -> dict:
    """The `update`-op `set` for a re-captured surface — **only the aspects that actually changed**.

    A pose is written when the surface genuinely moved (past `_LARGE_MOVE_M`) or was resized, never as a
    side effect of an unrelated edit. The corner-relative inset anchors (`along`/`vertical`) ride the pose
    for the same reason: they are derived from the capture's geometry, so refreshing them from an untrusted
    one is how inset identity starts churning (§6.1)."""
    up: dict = {}
    if "semantic" in aspects:
        up["meta.semantic"] = s.semantic
        if s.mesh_segment is not None:
            up["meta.meshSegment"] = s.mesh_segment
    if "holes" in aspects and s.holes is not None:
        up["components.surface.holes"] = s.holes
    if "rotation" in aspects and s.rotation is not None:
        up["transform.rotation"] = s.rotation
    if "extent" in aspects:                            # size and centre are one measurement — write both
        if s.extent is not None:
            up["components.surface.extent"] = s.extent
        if s.polygon is not None:
            up["components.surface.polygon"] = s.polygon
    if ("position" in aspects or "extent" in aspects) and s.position is not None:
        up["transform.position"] = s.position
        if s.hostWall is not None:
            up["meta.host_wall"] = s.hostWall
        if s.along is not None:
            up["meta.along"] = s.along
        if s.vertical is not None:
            up["meta.vertical"] = s.vertical
        if s.structuralFallback is not None:
            up["meta.structural_fallback"] = s.structuralFallback
    return up


@app.post("/space/capture")
async def ingest_room(req: RoomUpdate) -> dict:
    """Ingest captured room geometry from the room **authority** headset into the shared MODEL / SEED.

    LOCAL-FIRST (docs/specs/spaces-geometry.md §2): every client renders its OWN live capture, so this no
    longer broadcasts geometry for rendering. It just keeps the stored SEED current — the reference
    constellation guests register against, the director's geometry queries, and what's persisted. A surface
    is added when new, updated only on a STRUCTURAL change, and then only in the fields that changed
    (`_surface_changes` §7.4 — no time-based
    establish/freeze anymore), and pruned after sustained absence; surfaces with a photo pinned to them
    (`anchored`) are never pruned. Those geometry ops are applied to the store but NOT broadcast. Only what
    clients actually consume is broadcast: room-activation env + on-surface image re-anchors. An idle
    authority is taken over after `_AUTH_TTL` (a reconnecting owner isn't locked out)."""
    global _authority_ts
    env = store.doc["environment"]
    pres = env.get("spacePresentation", {})
    authority = env.get("captureAuthority")
    now = time.time()
    if authority and authority != req.client_id:
        if (now - _authority_ts) < _AUTH_TTL:                 # another headset is live → refuse
            _slog("room", f"reject client={req.client_id} — {authority!r} holds authority "
                          f"({now - _authority_ts:.1f}s ago)")
            return {"ok": False, "error": f"another headset ({authority}) is the room authority"}
        _slog("room", f"authority takeover: {authority!r} idle {now - _authority_ts:.0f}s → {req.client_id}")
    _authority_ts = now                                       # keep/refresh authority for this client

    existing = {e["id"]: e for e in store.doc["entities"] if e.get("meta", {}).get("real")}
    new_ids = {s.id for s in req.surfaces}

    # Geometry ops update the stored SEED only — never broadcast (clients render locally). A surface with a
    # photo pinned to it (`anchored`) is protected from pruning so the photo's id never orphans.
    anchored = {(e.get("meta") or {}).get("on_surface") for e in store.doc["entities"]} - {None}
    geo_ops: list[dict] = []
    changed_ids: set[str] = set()
    if req.replace:
        # The client posts its CONFIRMED set (it owns the absence debounce, docs §7), so a surface missing
        # from the post is genuinely gone → prune immediately. `anchored` (photo-pinned) ids are kept.
        for eid in existing:
            if eid not in new_ids and eid not in anchored:
                geo_ops.append({"op": "remove", "id": eid})
                # A prune destroys the surface's MATERIAL along with its geometry, and the client rebuilds
                # `surfaceStyles` from the snapshot — so this line is the moment a surface's colour is lost
                # for good. It is the server half of the "drops out and returns uncoloured" symptom; the
                # client half is `churn.prune`. Named by id because `seed_ops=N` alone can't be chased.
                meta = (existing[eid].get("meta") or {})
                mat = (existing[eid].get("components") or {}).get("material") or {}
                # `styled` means "a DIRECTOR edit is being destroyed", not "a material exists" — every real
                # surface is created with a per-semantic default (_default_surface_material), so a plain
                # truthiness check would report True for all of them and say nothing. Compare against that
                # default instead, and record the colour so the loss is legible in the log.
                _glog("seed.prune", {"id": eid, "sem": meta.get("semantic"),
                                     "styled": mat != _default_surface_material(meta.get("semantic") or ""),
                                     "color": mat.get("color"), "tex": bool(mat.get("src"))})
                _slog("seed", f"surface {eid} absent from post → PRUNED from seed")
    for s in req.surfaces:                                    # add new / update STRUCTURALLY-changed (§7.4)
        if s.id in existing:
            aspects = _surface_changes(existing[s.id], s)
            if aspects:
                up = _surface_update_set(s, aspects)
                geo_ops.append({"op": "update", "id": s.id, "set": up})
                changed_ids.add(s.id)
                why = "+".join(aspects)
                _glog("seed.update", {"id": s.id, "sem": s.semantic, "why": why,
                                      "wrote": sorted(up)})
                _slog("seed", f"surface {s.id} {why} → seed updated ({', '.join(sorted(up))})")
        else:
            geo_ops.append({"op": "add", "entity": _surface_entity(s)})
            changed_ids.add(s.id)
            _glog("seed.add", {"id": s.id, "sem": s.semantic})
            _slog("seed", f"surface {s.id} new → added to seed")
    if geo_ops:
        store.apply_patch(geo_ops, origin="room")             # seed updated in place; NOT broadcast

    # Only these reach clients: room-activation/boundary env + on-surface image re-anchors (content, which
    # clients DO render). Geometry is theirs to render locally.
    env_set: dict = {}
    if not pres.get("active"):
        env_set["spacePresentation.active"] = True
    if env.get("captureAuthority") != req.client_id:
        env_set["captureAuthority"] = req.client_id
    if req.boundary is not None and req.boundary != env.get("boundary"):
        env_set["boundary"] = req.boundary
    if "defaultSurfaceVisible" not in pres:
        env_set["spacePresentation.defaultSurfaceVisible"] = False         # default: invisible references (AR-style)
    wire_ops: list[dict] = []
    if env_set:
        wire_ops.append({"op": "env", "set": env_set})
    moved = {s.id: {"position": s.position, "rotation": s.rotation, "extent": s.extent}
             for s in req.surfaces if s.id in changed_ids}
    wire_ops += _reanchor_ops(store.doc, moved)               # re-pin photos on surfaces that moved
    if wire_ops:
        patch = store.apply_patch(wire_ops, origin="room")
        await _broadcast({"type": "patch", "patch": patch})

    if geo_ops or wire_ops:
        _slog("room", f"accept client={req.client_id} → {active_scope.split('/', 1)[0]}/{active_world} "
                      f"surfaces={len(req.surfaces)} changed={len(changed_ids)} seed_ops={len(geo_ops)} wire={len(wire_ops)}")
    return {"ok": True, "surfaces": len(req.surfaces), "authority": req.client_id}


@app.post("/space/realign")
async def realign_room() -> dict:
    """Ask connected headsets to re-capture the room at the current tracking origin (restores alignment
    after a recenter/reload). No-op for clients not in an AR session. (Clients render their own capture
    locally now, so this is just a nudge to recapture — there's no server-side freeze to reopen.)"""
    await _broadcast({"type": "recapture"})
    return {"ok": True}


class TextureSurfaceRequest(BaseModel):
    target: str                        # surface id, semantic label ('floor'/'ceiling'/'wall'), or 'all'
    image_id: str
    repeat: Optional[float] = None     # tile NxN across the surface (e.g. grass); None = stretch one copy


@app.post("/texture_surface")
async def texture_surface(req: TextureSurfaceRequest) -> dict:
    """Map a procured image onto room surface(s) — stars on the ceiling, grass on the floor, a mural
    on a wall. Sets the real surface's material to the image (white-tinted, visible)."""
    rec, _, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    targets = _room_targets(req.target)
    if not targets:
        return {"ok": False, "error": f"no room surface matches {req.target!r} (try query_room)"}
    mat = {"components.material.src": rec.url, "components.material.shader": "flat",
           "components.material.color": "#FFFFFF", "components.material.side": "double",
           "components.material.visible": True}
    if req.repeat:
        mat["components.material.repeat"] = f"{req.repeat} {req.repeat}"  # tile (needs a seamless image)
    ops = [{"op": "update", "id": e["id"], "set": {**mat, "meta.image_id": rec.id}} for e in targets]
    patch = store.apply_patch(ops, origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return _with_notice({"ok": True, "count": len(targets), "image_id": rec.id}, _ensure_referenced_public(rec.id))


_SEM_NUM = re.compile(r"^([a-z][a-z ]*?)\s*#?\s*(\d+)$")


def _real_surface_match(e: dict, target: str) -> bool:
    """Does real surface `e` match a director target — a surface id, semantic label ('wall'), friendly
    number ('4'), the combined 'wall 4' form, or 'all'? The friendly number equals the id's number."""
    m = e.get("meta", {})
    if not m.get("real"):
        return False
    t = str(target).strip().lower()
    sem, fid = m.get("semantic", ""), str(m.get("friendly_id"))
    if t in ("all", e["id"].lower(), sem) or t == fid:
        return True
    mm = _SEM_NUM.match(t)                       # 'wall 4' / 'wall #4'
    return bool(mm and mm.group(1).strip() in (sem, "surface") and mm.group(2) == fid)


def _room_targets(target: str) -> list[dict]:
    """Real surfaces matching `target` (see _real_surface_match)."""
    return [e for e in store.doc["entities"] if _real_surface_match(e, target)]


def _resolve_op_ids(target: str) -> list[str]:
    """Entity ids a patch op should hit: an exact entity id (any entity) wins; otherwise real surfaces
    matching a friendly number / semantic / 'all'. Lets the generic update/move/remove tools accept the
    same surface references the surface tools do, so 'wall 4' works no matter which tool the director picks."""
    exact = [e["id"] for e in store.doc["entities"] if e["id"] == target]
    return exact or [e["id"] for e in store.doc["entities"] if _real_surface_match(e, target)]


class StyleSurfaceRequest(BaseModel):
    target: str                        # surface id, semantic label, or 'all'
    color: Optional[str] = None        # CSS name or #hex
    opacity: Optional[float] = None    # 0..1; < 1 makes it see-through


@app.post("/style_surface")
async def style_surface(req: StyleSurfaceRequest) -> dict:
    """Color and/or set the transparency of room surface(s) — e.g. semi-transparent blue walls, a
    glass ceiling. (For an image, use /texture_surface.)"""
    targets = _room_targets(req.target)
    if not targets:
        return {"ok": False, "error": f"no room surface matches {req.target!r} (try query_room)"}
    setm: dict = {"components.material.visible": True}
    if req.color is not None:
        setm["components.material.color"] = req.color
        setm["components.material.src"] = ""             # clear any texture so the color shows
    if req.opacity is not None:
        setm["components.material.opacity"] = req.opacity
        setm["components.material.transparent"] = req.opacity < 1.0
    if len(setm) == 1:
        return {"ok": False, "error": "specify a color and/or opacity"}
    patch = store.apply_patch([{"op": "update", "id": e["id"], "set": setm} for e in targets], origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "count": len(targets)}


class PlaceAssetRequest(BaseModel):
    query: str
    position: Optional[list[float]] = None
    size_m: Optional[float] = None  # intended real-world largest dimension, meters
    name: Optional[str] = None
    placement: str = "grounded"     # "grounded" (sits on the floor) | "free" (floats at the given position)


@app.api_route("/assets/{filename}", methods=["GET", "HEAD"])
async def asset(filename: str) -> FileResponse:
    """Serve a cached asset (GLB model or generated image). Supports HEAD (some loaders probe with
    it before GET — without this they get a noisy 405 then retry)."""
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="bad filename")
    path = ASSET_CACHE / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path, media_type=MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"))


@app.post("/place_asset")
async def place_asset(req: PlaceAssetRequest) -> dict:
    """Resolve a search query to a real model: drop a placeholder, then swap in the GLB."""
    if resolver is None:
        return {"ok": False, "error": "POLY_PIZZA_API_KEY not set"}

    eid = req.name or f"ent_asset_{uuid4().hex[:6]}"
    pos = req.position or [0.0, 0.0, -3.0]  # base on the floor, a few meters in front

    # 1. Placeholder appears immediately (progressive construction, architecture.md §5).
    placeholder = {
        "op": "add",
        "entity": {
            "id": eid,
            "transform": {"position": pos},
            "components": {"geometry": {"primitive": "box"}, "material": {"color": "#888", "opacity": 0.5}},
        },
    }
    await _broadcast({"type": "patch", "patch": store.apply_patch([placeholder], origin="asset")})

    # 2. Search + download + cache.
    try:
        record = await resolver.resolve(req.query)
    except Exception as exc:  # noqa: BLE001
        record = None
        detail = str(exc)
    else:
        detail = "no model found"

    if record is None:
        await _broadcast({"type": "patch", "patch": store.apply_patch([{"op": "remove", "id": eid}], origin="asset")})
        return {"ok": False, "error": detail}

    # Catalog the model keyed on the search query (its intent) so it's reusable later; touch it so
    # recency reflects this use. licence/attribution are captured for the legal record.
    model_id = f"{record.hash}.glb"
    _catalog_asset(model_id, kind="model", label=record.title, query=req.query, licence=record.licence,
                   attribution=record.attribution, creator=record.creator,
                   attributes={"tris": record.tris, "bbox_min": record.bbox_min,
                               "bbox_max": record.bbox_max})   # models: not vector-embedded (see _VISUAL_KINDS)
    library.touch(model_id)
    # Models are NOT vector-embedded — found by FTS/exact on their title (see _VISUAL_KINDS).

    # 3. Swap the placeholder for the real glTF model (auto-scaled to sit on the floor),
    #    carrying license + attribution.
    swap = [
        {"op": "remove", "id": eid},
        _model_entity_op(eid, model_id, title=record.title, licence=record.licence,
                         attribution=record.attribution, creator=record.creator, tris=record.tris,
                         source="poly.pizza", bbox_min=record.bbox_min, bbox_max=record.bbox_max,
                         pos=pos, size_m=req.size_m, placement=req.placement),
    ]
    await _broadcast({"type": "patch", "patch": store.apply_patch(swap, origin="asset")})
    return {
        "ok": True,
        "id": eid,
        "title": record.title,
        "tris": record.tris,
        "licence": record.licence,
        "attribution": record.attribution,
    }


# --- asset library: explicit, director-driven reuse over the catalog (docs/specs/library.md §7).
#     search_library (read-only) → tiered candidates; place_cached_asset → reuse a model by id;
#     correct_asset → relabel / reject a mismatch. The director searches BEFORE going to the web. -----

class LibrarySearchRequest(BaseModel):
    query: Optional[str] = None      # text intent ("an oak tree"), OR
    image_id: Optional[str] = None   # an asset already in the catalog → "more like this"
    kind: Optional[str] = None       # image | model | skybox | … (optional filter)
    scope: str = DEFAULT_SCOPE       # caller's scope (capability, set by the MCP server) → own ∪ public


@app.post("/library/search")
async def library_search(req: LibrarySearchRequest) -> dict:
    """Find reusable assets by intent. Returns tiered candidates (strong/weak/none) so the director
    decides reuse vs. generate. Read-only — no scene effect."""
    qvec = None
    if embedder is not None:                         # embed the query off the loop (semantic stage)
        if req.query:
            qvec = await asyncio.to_thread(embedder.embed_text, req.query)
        elif req.image_id:
            a = library.get(req.image_id)
            fn = a and a.get("filename")
            if fn and (ASSET_CACHE / fn).exists():
                data = (ASSET_CACHE / fn).read_bytes()
                qvec = await asyncio.to_thread(embedder.embed_image, data)
    return {"ok": True, **library.find(text=req.query, query_vec=qvec, kind=req.kind, scope=req.scope)}


class PlaceCachedAssetRequest(BaseModel):
    id: str                                  # a model asset id from search_library ("<hash>.glb")
    position: Optional[list[float]] = None
    size_m: Optional[float] = None
    name: Optional[str] = None
    placement: str = "grounded"              # "grounded" (sits on the floor) | "free" (floats at position)


@app.post("/place_cached_asset")
async def place_cached_asset(req: PlaceCachedAssetRequest) -> dict:
    """Place a MODEL already in the library by id — the reuse counterpart to place_asset (no web
    fetch). Images reuse place_image; skyboxes reuse set_skybox/set_grounded_skybox."""
    rec = library.get(req.id)
    if rec is None or not _asset_in_agent_scope(rec):        # hard agent wall (cross-agent id → "not found")
        return {"ok": False, "error": f"no asset {req.id!r} in the library"}
    if rec["kind"] != "model":
        return {"ok": False, "error": f"{req.id!r} is a {rec['kind']}, not a model — "
                "use place_image (images) or set_skybox (skyboxes)"}
    if not (ASSET_CACHE / req.id).exists():
        return {"ok": False, "error": f"bytes for {req.id!r} are missing from the cache"}
    attrs = _refresh_model_attrs(req.id, json.loads(rec["attributes"] or "{}"))
    eid = req.name or f"ent_asset_{uuid4().hex[:6]}"
    pos = req.position or [0.0, 0.0, -3.0]
    op = _model_entity_op(eid, req.id, title=rec["label"], licence=rec["licence"],
                          attribution=rec["attribution"], creator=rec["creator"],
                          tris=attrs.get("tris"), source="library", bbox_min=attrs.get("bbox_min"),
                          bbox_max=attrs.get("bbox_max"), pos=pos, size_m=req.size_m,
                          placement=req.placement, rigged=bool(attrs.get("rigged")),
                          humanoid=attrs.get("humanoid"), humanoid_axes=attrs.get("humanoid_axes"),
                          humanoid_follows=attrs.get("humanoid_follows"))
    await _broadcast({"type": "patch", "patch": store.apply_patch([op], origin="asset")})
    library.touch(req.id)
    return _with_notice({"ok": True, "id": eid, "image_id": req.id, "title": rec["label"]},
                        _ensure_referenced_public(req.id))


# --- catalog maintenance: scoped CRUD over the asset library (docs/specs/library.md). The
#     `scope` on each request is set by the agent's MCP server (a capability, not an LLM arg) and
#     enforced here. query_assets is read-only + scope-filtered; update/delete are per-id scope-checked.

class QueryAssetsRequest(BaseModel):
    sql: str
    scope: str = DEFAULT_SCOPE


@app.post("/query_assets")
async def query_assets(req: QueryAssetsRequest) -> dict:
    """Read-only SQL over the catalog, scoped to the caller (SELECT/PRAGMA only, single statement)."""
    try:
        return {"ok": True, "rows": library.query(req.sql, scope=req.scope)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface a bad query, don't 500
        return {"ok": False, "error": f"query failed: {exc}"}


class UpdateAssetRequest(BaseModel):
    id: str
    scope: str = DEFAULT_SCOPE
    label: Optional[str] = None
    query: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None
    kind: Optional[str] = None
    rating: Optional[int] = None
    favorite: Optional[bool] = None
    public: Optional[bool] = None       # catalog visibility (others' reads see your public assets)
    default_for: Optional[str] = None   # pin an alias ("dog" → this asset)
    reject_for: Optional[str] = None    # exclude this asset from a query's matches


@app.post("/update_asset")
async def update_asset(req: UpdateAssetRequest) -> dict:
    """The single catalog mutator: set fields / kind / alias / reject on an asset (scope-checked).
    Keeps FTS + vector-kind + aliases consistent (subsumes the old correct_asset/annotate_asset)."""
    ok, err = library.update(req.id, scope=req.scope, label=req.label, query=req.query, tags=req.tags,
                             notes=req.notes, kind=req.kind, rating=req.rating, favorite=req.favorite,
                             public=req.public, default_for=req.default_for, reject_for=req.reject_for)
    return {"ok": True, "id": req.id} if ok else {"ok": False, "error": err}


class DeleteAssetRequest(BaseModel):
    id: str
    scope: str = DEFAULT_SCOPE


@app.post("/delete_asset")
async def delete_asset(req: DeleteAssetRequest) -> dict:
    """Remove an asset from the catalog (row + aliases/relations/vector; bytes kept). Scope-checked."""
    ok, err = library.delete(req.id, scope=req.scope)
    return {"ok": True, "id": req.id} if ok else {"ok": False, "error": err}


class RefreshModelsRequest(BaseModel):
    force: bool = False              # re-extract even rows already stamped at the current revision


@app.post("/library/refresh-models")
async def library_refresh_models(req: RefreshModelsRequest) -> dict:
    """Re-derive every model row's attributes from its bytes — the batch form of what placement does one
    model at a time. Use after a build that changes what extraction knows (a new inference rule, joint
    limits) so the whole library catches up at once instead of a figure at a time."""
    rows = library.search(kind="model", limit=10000, scope=_caller_scope.get())
    changed = []
    for row in rows:
        before = json.loads(row["attributes"] or "{}")
        after = _refresh_model_attrs(row["id"], before, force=req.force)
        if after is not before and after != before:
            changed.append({"id": row["id"], "label": row["label"],
                            "rigged": bool(after.get("rigged")),
                            "bones": len(after.get("humanoid") or {})})
    return {"ok": True, "checked": len(rows), "updated": changed}


class RetagSkyboxesRequest(BaseModel):
    min_aspect: float = 1.9          # images at least this wide (≈ equirectangular) → skyboxes


@app.post("/library/retag-skyboxes")
async def library_retag_skyboxes(req: RetagSkyboxesRequest) -> dict:
    """One-time backfill cleanup: re-tag wide image-kind assets (panoramas) as skyboxes so they're
    findable by kind='skybox'. The early backfill couldn't distinguish them from regular images."""
    return {"ok": True, "retagged": library.retag_skyboxes(min_aspect=req.min_aspect)}


class ReindexRequest(BaseModel):
    kind: Optional[str] = None       # optionally restrict to image | model | …


@app.post("/library/reindex")
async def library_reindex(req: ReindexRequest) -> dict:
    """Embed cataloged assets that have no vector yet (e.g. everything backfilled before embeddings) —
    a one-time pass so the existing library becomes searchable by similarity. Runs off the request
    path; returns how many were queued."""
    if embedder is None:
        return {"ok": False, "error": "no embedder — install the optional 'embed' dependency group"}
    # Self-healing: the vector index should hold exactly the VISUAL assets. Clear any text-derived
    # vectors that crept in (e.g. earlier model-title embeddings), then embed visual assets missing one.
    cleared = 0
    for a in library.embedded_nonvisual(_VISUAL_KINDS):
        library.clear_embedding(a["id"])
        cleared += 1
    targets = [a for a in library.assets_missing_embedding(kind=req.kind) if a["kind"] in _VISUAL_KINDS]
    if _EMBED_BACKGROUND:
        task = asyncio.create_task(_reindex_bg(targets))
        _embed_tasks.add(task)
        task.add_done_callback(_embed_tasks.discard)
    else:
        for a in targets:
            _embed_one(a)
    return {"ok": True, "queued": len(targets), "cleared": cleared}


@app.post("/library/caption")
async def library_caption() -> dict:
    """Backfill labels for assets that have none (the bare backfilled images) via image→text vision,
    so they read in search results and match keyword search. One-time; runs off the request path."""
    if captioner is None:
        return {"ok": False, "error": "no captioner — set CONJURE_CAPTION_PROVIDER and the provider key"}
    targets = library.assets_missing_caption(_VISUAL_KINDS)
    if _EMBED_BACKGROUND:
        task = asyncio.create_task(_caption_bg(targets))
        _embed_tasks.add(task)
        task.add_done_callback(_embed_tasks.discard)
    else:
        for a in targets:
            await _caption_one(a)
    return {"ok": True, "queued": len(targets)}


class ImportItem(BaseModel):
    filename: str            # original name (drives extension → handler, and stereo-name heuristics)
    data_b64: str            # the file bytes, base64-encoded
    hints: dict = {}         # optional: {'kind','stereo','label','licence','attribution','creator'}


class ImportRequest(BaseModel):
    items: list[ImportItem]
    dry_run: bool = False    # report what WOULD import (sniffed kind + metadata) without writing


@app.post("/library/import")
async def library_import(req: ImportRequest) -> dict:
    """Import external asset files into the library. The importer registry (conjure/importer.py) sniffs
    each file by extension + magic bytes, extracts kind-specific metadata, then content-addresses the
    bytes and catalogs the row via register_asset — dedup is automatic (same bytes → same id). Stereo
    side-by-side/top-bottom images are tagged so place_image renders them per-eye. Freshly imported
    visual assets are captioned in the background. `dry_run` reports the plan without writing."""
    from .importer import plan_import

    results: list[dict] = []
    imported_ids: list[str] = []
    for item in req.items:
        try:
            data = base64.b64decode(item.data_b64)
        except (ValueError, TypeError):
            results.append({"filename": item.filename, "ok": False, "error": "bad base64"})
            continue
        plan = plan_import(item.filename, data, dict(item.hints or {}))
        if plan is None:
            results.append({"filename": item.filename, "ok": False, "error": "unrecognized or invalid file"})
            continue
        if req.dry_run:
            results.append({"filename": item.filename, "ok": True, "dry_run": True, "kind": plan.kind,
                            "width": plan.width, "height": plan.height, "attributes": plan.attributes})
            continue
        stereo = plan.attributes.get("stereo")
        embed = _first_eye(data, stereo) if stereo else None   # embed one clean eye, not the pair
        asset_id = register_asset(data, kind=plan.kind, ext=plan.ext, label=plan.label,
                                  width=plan.width, height=plan.height, transparent=plan.transparent,
                                  attributes=plan.attributes or None, licence=plan.licence,
                                  attribution=plan.attribution, creator=plan.creator,
                                  params={"source": "import"}, embed_image=embed)
        imported_ids.append(asset_id)
        results.append({"filename": item.filename, "ok": True, "id": asset_id, "kind": plan.kind,
                        "attributes": plan.attributes})
    # Caption the freshly-imported visual assets that still lack a label (background, best-effort).
    if imported_ids and captioner is not None:
        rows = [r for r in (library.get(i) for i in imported_ids)
                if r and r["kind"] in _VISUAL_KINDS and not r.get("label")]
        if rows:
            if _EMBED_BACKGROUND:
                task = asyncio.create_task(_caption_bg(rows))
                _embed_tasks.add(task)
                task.add_done_callback(_embed_tasks.discard)
            else:
                for a in rows:
                    await _caption_one(a)
    return {"ok": True, "results": results, "imported": len(imported_ids),
            "failed": sum(1 for r in results if not r.get("ok"))}


# --- procurement: produce/transform an image, return an id; NO scene effect ---------------------

_SKYBOX_PROMPT = (
    "A seamless equirectangular 360-degree panorama for a VR skybox: {p}. "
    "Centered horizon, evenly lit, no people, no text, no watermark, no borders."
)
_OUTPAINT_PROMPT = (
    "Outpaint this image: extend it outward to fill a wider {aspect} frame. Keep the existing scene "
    "and subject exactly, and continue it seamlessly outward in the same style, colors, and lighting. "
    "Do not crop, zoom, or change the original content."
)
_SKYBOX_FROM_PROMPT = (
    "Turn this image into a seamless equirectangular 360-degree panorama for a VR skybox, extending "
    "the scene all the way around. Keep its style, palette, and mood. Centered horizon, no people, "
    "no text, no borders."
)
# A grounded skybox projects the panorama's LOWER hemisphere onto a flat ground at your feet, so the
# bottom of the image must read as believable, near-flat, evenly-textured ground (grass, sand, stone,
# floor) — not a distorted smear or a distant horizon line. Tweak this freely; it's independent of the
# plain skybox prompt above.
_GROUNDED_SKYBOX_PROMPT = (
    "A seamless equirectangular 360-degree panorama for a VR skybox you can stand inside of: {p}. "
    "The lower portion of the image is the GROUND directly beneath the viewer and must be a continuous, "
    "flat, evenly-lit ground surface (e.g. grass, sand, stone, dirt, or floor) that reads naturally when "
    "looked straight down at — no distortion, no seams, no objects directly underfoot. Centered horizon, "
    "evenly lit, no people, no text, no watermark, no borders."
)


@app.get("/images/generators")
async def images_generators() -> dict:
    """List the available image generators, their capabilities, and the default chosen per op."""
    defaults = {op: (lambda g: g.name if g else None)(select_generator(image_generators, op)[0])
                for op in PROCURE_OPS}
    defaults["transparent"] = (lambda g: g.name if g else None)(
        select_generator(image_generators, "generate", transparent=True)[0])
    return {
        "ok": True,
        "generators": [{"name": n, "vendor": vendor_for(n), "capabilities": g.capabilities.to_dict()}
                       for n, g in image_generators.items()],
        "defaults": defaults,
    }


class GenerateImageRequest(BaseModel):
    prompt: str
    aspect_ratio: Optional[str] = None
    transparent: bool = False
    generator: Optional[str] = None


@app.post("/images/generate")
async def images_generate(req: GenerateImageRequest) -> dict:
    return await _procure(
        "generate", prompt=req.prompt, requested=req.generator, transparent=req.transparent,
        run=lambda g: g.generate(req.prompt, aspect_ratio=req.aspect_ratio, transparent=req.transparent))


class SkyboxImageRequest(BaseModel):
    prompt: str
    generator: Optional[str] = None


@app.post("/images/skybox")
async def images_skybox(req: SkyboxImageRequest) -> dict:
    full = _SKYBOX_PROMPT.format(p=req.prompt)
    return await _procure(
        "skybox", prompt=req.prompt, requested=req.generator, transparent=False,
        run=lambda g: g.generate(full, aspect_ratio="21:9", image_size=settings.skybox_size,
                                 model=settings.skybox_model))


@app.post("/images/grounded_skybox")
async def images_grounded_skybox(req: SkyboxImageRequest) -> dict:
    # Same 4K equirectangular pipeline as /images/skybox, but with the grounded prompt (well-defined,
    # flat ground) so the projected floor looks right. set_grounded_skybox then applies it.
    full = _GROUNDED_SKYBOX_PROMPT.format(p=req.prompt)
    return await _procure(
        "grounded_skybox", prompt=req.prompt, requested=req.generator, transparent=False,
        run=lambda g: g.generate(full, aspect_ratio="21:9", image_size=settings.skybox_size,
                                 model=settings.skybox_model))


class EditImageAssetRequest(BaseModel):
    image_id: str
    prompt: str
    transparent: bool = False
    generator: Optional[str] = None


@app.post("/images/edit")
async def images_edit(req: EditImageAssetRequest) -> dict:
    _, img, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    return await _procure(
        "edit", prompt=req.prompt, requested=req.generator, transparent=req.transparent,
        run=lambda g: g.edit(req.prompt, img, transparent=req.transparent))


class OutpaintImageAssetRequest(BaseModel):
    image_id: str
    aspect: Optional[str] = None
    prompt: Optional[str] = None
    generator: Optional[str] = None


@app.post("/images/outpaint")
async def images_outpaint(req: OutpaintImageAssetRequest) -> dict:
    _, img, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    aspect = req.aspect or "16:9"
    instruction = _OUTPAINT_PROMPT.format(aspect=aspect) + (f" {req.prompt}" if req.prompt else "")
    return await _procure(
        "outpaint", prompt=instruction, requested=req.generator, transparent=False,
        run=lambda g: g.edit(instruction, img, aspect_ratio=aspect))


class SkyboxFromImageAssetRequest(BaseModel):
    image_id: str
    generator: Optional[str] = None


@app.post("/images/skybox_from")
async def images_skybox_from(req: SkyboxFromImageAssetRequest) -> dict:
    _, img, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    return await _procure(
        "skybox_from", prompt=_SKYBOX_FROM_PROMPT, requested=req.generator, transparent=False,
        run=lambda g: g.edit(_SKYBOX_FROM_PROMPT, img, aspect_ratio="21:9",
                             image_size=settings.skybox_size, model=settings.skybox_model))


# --- scene use: incorporate a procured image (by id) into the world ------------------------------

def _image_plane(eid: str, pos: list[float], width: float, height: float, material: dict,
                 meta: dict | None = None, rotation: list[float] | None = None,
                 billboard: bool = False, stereo: str | None = None) -> dict:
    transform: dict = {"position": pos}
    if rotation is not None:
        transform["rotation"] = rotation
    components: dict = {
        "geometry": {"primitive": "plane", "width": width, "height": height},
        "material": material,
    }
    if billboard:   # free-standing only: yaw-only face-the-viewer (each client aims at its own camera)
        components["billboard"] = {"yaw": True}
    if stereo:   # per-eye split rendered client-side (layers); layout 'sbs' (left|right) or 'tb'
        components["stereo"] = {"layout": stereo}
    return {
        "op": "add",
        "entity": {
            "id": eid,
            "transform": transform,
            "components": components,
            **({"meta": meta} if meta else {}),
        },
    }


def _fit_longest(w: float, h: float, size: float) -> tuple[float, float]:
    """Fit a w×h rectangle's longest side to `size` meters, preserving aspect → (width, height)."""
    w, h = w or 1, h or 1
    if w >= h:
        return size, round(size * h / w, 3)
    return round(size * w / h, 3), size


def _plane_dims(rec: ImageRecord, size: float, stereo: str | None = None) -> tuple[float, float]:
    """Fit the image's longest side to `size` meters, preserving aspect. For a packed stereo image
    (`stereo`='sbs'|'tb') the plane shows ONE eye, so fit the per-eye aspect (half width, or half
    height for top-bottom) — otherwise a side-by-side pair would render twice as wide as it should."""
    w, h = rec.w or 1, rec.h or 1
    if stereo == "tb":
        h = h / 2
    elif stereo:
        w = w / 2
    return _fit_longest(w, h, size)


def _fit_extent(aspect: float, extent: list[float]) -> tuple[float, float]:
    """Fit an image of the given aspect (w/h), preserving it, *inside* a surface's [w, h] frame."""
    ew, eh = float(extent[0]), float(extent[1])
    if ew / eh > aspect:                       # frame is wider than the image ⇒ height-limited
        return round(aspect * eh, 3), round(eh, 3)
    return round(ew, 3), round(ew / aspect, 3)  # width-limited


def _fit_dims(rec: ImageRecord, extent: list[float], stereo: str | None = None) -> tuple[float, float]:
    """Fit the image (preserving aspect) *inside* a surface's [w, h] frame — so a picture hung on a
    wall-art surface fills its frame without stretching or overflowing. For a packed stereo image, fit
    the PER-EYE aspect (one eye is shown) so a side-by-side pair isn't squeezed to half its true shape."""
    w, h = rec.w or 1, rec.h or 1
    if stereo == "tb":
        h = h / 2
    elif stereo:
        w = w / 2
    return _fit_extent((w / h) if (w and h) else 1.0, extent)


def _module_plane_dims(rec: ImageRecord, config: dict, extent, *, stretch: bool,
                       default_size: float = 1.2) -> tuple[float, float]:
    """(width, height) for a flat image-bearing dynamic module (e.g. a Water Picture), mirroring
    place_image's aspect handling so a module respects its image's shape by default:
    - explicit `width`+`height` in the caller's config win as-is (an intentional exact size);
    - on a surface (extent given): fit the image's aspect INSIDE the frame (default), or `stretch` to
      fill the whole surface;
    - free-standing: fit the image's longest side to the requested size (a lone `width`/`height`, else
      `default_size`), preserving aspect.
    Water carries no stereo packing, so per-eye halving doesn't apply here."""
    cw, ch = config.get("width"), config.get("height")
    if cw and ch:
        return float(cw), float(ch)
    if extent:
        return (float(extent[0]), float(extent[1])) if stretch else _fit_dims(rec, extent)
    return _plane_dims(rec, float(cw or ch or default_size))


def _forward(rotation: list[float]) -> list[float]:
    """World-space front (+Z) of an <a-plane> at A-Frame euler `rotation` (degrees, YXZ order) — the
    surface's own facing/normal (roll about +Z doesn't change it, so `rz` is ignored)."""
    x, y = math.radians(rotation[0]), math.radians(rotation[1])
    return [math.cos(x) * math.sin(y), -math.sin(x), math.cos(x) * math.cos(y)]


# --- Content orientation: a hung photo must face the VIEWER, upright, on ANY surface. We orient the
# CONTENT ourselves (never adopt the surface's own rotation — walls carry an arbitrary 180° roll → upside
# down, and a surface's +Z is its OUTWARD normal → facing away). Every surface now stores its true outward
# normal (client snapInsets no longer negates wall art), so the room interior is simply -normal; up is
# gravity — one rule for walls, tables, ceilings, pictures. The ONE viewer-dependent case is a HORIZONTAL
# surface (floor/table/ceiling), where gravity gives no in-plane up: there the content's up is snapped to
# the surface-rectangle axis whose bottom edge sits nearest the placing viewer (square to the surface,
# readable from where they stood), stored surface-local so a re-capture reproduces it (see
# `_content_up_local` / `_face_room(up_local=…)`). A wall never needs this.
# Measured on-device (`[normals]` probe): surface normals are reliably outward-from-room, so -normal is the
# interior in every room including a multi-room space (each wall's own normal marks its own room).
def _norm3(v: list[float]) -> list[float]:
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return [c / n for c in v]


def _local_axis(rot_deg: list[float], axis: tuple[float, float, float]) -> list[float]:
    """World direction of a local `axis` for an a-plane at A-Frame euler `rot_deg` (degrees, YXZ order):
    v = Ry·Rx·Rz·axis. (`_forward` is this for +Z; +Y also needs the roll rz, so use the full form.)"""
    rx, ry, rz = (math.radians(a) for a in rot_deg)
    x, y, z = axis
    x, y = x * math.cos(rz) - y * math.sin(rz), x * math.sin(rz) + y * math.cos(rz)   # Rz
    y, z = y * math.cos(rx) - z * math.sin(rx), y * math.sin(rx) + z * math.cos(rx)   # Rx
    x, z = x * math.cos(ry) + z * math.sin(ry), -x * math.sin(ry) + z * math.cos(ry)  # Ry
    return [x, y, z]


def _basis_yxz(right: list[float], up: list[float], fwd: list[float]) -> list[float]:
    """YXZ euler (degrees) of the rotation whose local +X,+Y,+Z map to right,up,fwd (world). Matches
    three.js Euler.setFromRotationMatrix(order='YXZ') so it round-trips with the client's eulerYXZ."""
    m13, m23, m33 = fwd[0], fwd[1], fwd[2]                # forward = 3rd basis column
    x = math.asin(max(-1.0, min(1.0, -m23)))
    if abs(m23) < 0.9999999:
        y = math.atan2(m13, m33)
        z = math.atan2(right[1], up[1])                  # m21, m22
    else:                                                # gimbal: forward ≈ ±up
        y = math.atan2(-right[2], right[0])              # -m31, m11
        z = 0.0
    return [math.degrees(x), math.degrees(y), math.degrees(z)]


def _face_user(user: str, position: list[float] | None, distance: float = 1.2) -> dict | None:
    """Spawn pose for FREE-STANDING flat content so it faces the viewer at creation (like a placed image),
    from the caller's live gaze: place it `distance` m ahead of the head (unless a position is given) and
    yaw it to face back at the head — upright, NOT a billboard (fixed at creation; it won't chase you).
    Returns {position, rotation:[deg×3] YXZ} in the #world-root frame (same frame gaze/entities use), or
    None with no live view (desktop / not looked around yet) → caller falls back to the default pose."""
    g = gaze.get(user)
    if not g:
        return None
    gv = _head_from_anchor(g.get("anchor")) or g
    o, fwd = gv["origin"], gv["forward"]
    pos = position or [round(o[i] + fwd[i] * distance, 3) for i in range(3)]
    yaw = math.degrees(math.atan2(o[0] - pos[0], o[2] - pos[2]))     # +z of the plane → toward the head
    return {"position": pos, "rotation": [0.0, round(yaw, 2), 0.0]}


def _face_room(srot: list[float], up_local: Optional[list[float]] = None) -> dict:
    """Orientation for content hung on a surface: face the room INTERIOR (upright). Surfaces store their
    OUTWARD normal, so the interior is `-normal`; `up` = gravity projected onto the plane. On a HORIZONTAL
    surface (floor/table/ceiling) gravity gives no in-plane up, so the content's up is ambiguous — pass
    `up_local` = [a, b] (coefficients on the surface's own +X/+Y in-plane axes, from `_content_up_local`)
    to orient it toward the placing viewer; without it we fall back to the surface's own rectangle.
    Returns {rotation:[deg×3] (YXZ), forward:[x,y,z] unit}. Uniform for walls, tables, ceilings, pictures."""
    n = _forward(srot)                                            # surface's OUTWARD normal
    f = _norm3([-n[0], -n[1], -n[2]])                             # content faces the interior = -normal
    d = f[1]                                                      # gravity (0,1,0) · forward
    up = [-d * f[0], 1.0 - d * f[1], -d * f[2]]                   # gravity ⟂ forward (upright on a wall)
    if sum(c * c for c in up) < 1e-6:                             # forward ≈ vertical (floor/ceiling/table):
        if up_local:                                             # viewer-derived up (bottom toward viewer),
            _, sr, su0 = _plane_basis(srot)                      # rebuilt in the surface's CURRENT in-plane
            su = [up_local[0] * sr[i] + up_local[1] * su0[i] for i in range(3)]   # basis so it rides recapture
        else:
            su = _local_axis(srot, (0.0, -1.0, 0.0))            # no viewer → align to the SURFACE's own
        d2 = sum(su[i] * f[i] for i in range(3))                 # rectangle so edges stay parallel. Use -Y
        up = [su[i] - d2 * f[i] for i in range(3)]               # (a 180° flip about vertical) — +Y read upside-down
        if sum(c * c for c in up) < 1e-6:                        # (su ∥ f, shouldn't happen) → any ⟂ axis
            up = [1.0 - f[0] * f[0], -f[0] * f[1], -f[0] * f[2]]
    up = _norm3(up)
    right = [up[1] * f[2] - up[2] * f[1], up[2] * f[0] - up[0] * f[2], up[0] * f[1] - up[1] * f[0]]  # up × fwd
    return {"rotation": _basis_yxz(right, up, f), "forward": f}


def _content_up_local(srot: list[float], spos: list[float], user: str) -> Optional[list[float]]:
    """For a HORIZONTAL surface, the content's in-plane 'up' that puts its BOTTOM edge nearest the placing
    viewer — so a photo/water picture laid on a table reads upright from where they stood. 'Up' points
    AWAY from the viewer, SNAPPED to the nearest surface-rectangle axis (±X/±Y) so the content sits square
    to the surface (edges parallel) rather than slightly askew at the viewer's exact angle. Returned as
    [a, b] coefficients on the surface's own +X/+Y in-plane axes — one of [±1,0]/[0,±1] — stored in meta so
    re-anchoring rebuilds the SAME facing after the surface is re-captured/moves. None for a vertical
    surface (gravity already gives up) or with no live gaze (voice/desktop) → caller keeps the fallback."""
    n = _forward(srot)
    f = _norm3([-n[0], -n[1], -n[2]])
    d = f[1]
    if sum(c * c for c in (-d * f[0], 1.0 - d * f[1], -d * f[2])) >= 1e-6:
        return None                                              # vertical-ish → gravity up, viewer-independent
    g = gaze.get(user)
    if not g:
        return None
    gv = _head_from_anchor(g.get("anchor")) or g
    o = gv["origin"]
    w = [spos[0] - o[0], 0.0, spos[2] - o[2]]                    # horizontal viewer→surface (top points away)
    if w[0] * w[0] + w[2] * w[2] < 1e-6:                         # viewer directly above/below → no horizontal dir
        return None
    w = _norm3(w)
    _, sr, su0 = _plane_basis(srot)                              # project onto the surface's in-plane basis
    a = sum(w[i] * sr[i] for i in range(3))
    b = sum(w[i] * su0[i] for i in range(3))
    if abs(a) >= abs(b):                                         # snap to the dominant rectangle axis → square
        return [1.0 if a >= 0 else -1.0, 0.0]
    return [0.0, 1.0 if b >= 0 else -1.0]


# --- on-surface re-anchoring: keep place_image(on_surface=…) planes glued to their surface across a room
#     re-registration/re-capture. The image records meta.on_surface = the surface id; we re-derive its pose
#     (2 cm in front, re-oriented toward the room via _face_room, re-fit to the current frame) from the
#     surface's CURRENT geometry — so when the surface moves, the image follows instead of being stranded.
def _surface_offset(spos: list[float], srot: list[float],
                    ipos: list[float], irot: list[float]) -> dict:
    """The rigid pose of on-surface content EXPRESSED IN ITS HOST'S LOCAL FRAME (host⁻¹·image): {p, q}
    (§7c-B2). A client rides the content by re-applying this to its OWN local host pose
    (`image = host_local · offset`) — so it never needs its `docSurfaces` copy of the host's seed pose, and
    the T⁻¹ fallback retires. Invariant as the surface moves (image tracks host), so it's the same every
    recompute; carried on meta.surface_offset."""
    qh, qi = _euler_yxz_quat(srot), _euler_yxz_quat(irot)
    qhc = _quat_conj(qh)
    q_off = _quat_mul(qhc, qi)
    p_off = _quat_rot(qhc, [ipos[i] - spos[i] for i in range(3)])
    return {"p": [round(c, 5) for c in p_off], "q": [round(c, 6) for c in q_off]}


def _dims_component(e: dict) -> Optional[str]:
    """The component key on `e` that carries its on-surface plane's size (width+height): `geometry` for a
    placed image, or a flat dynamic module's own component (e.g. `water`). None if the entity has no sized
    plane. Lets re-anchoring re-fit a Water Picture the SAME way it re-fits a regular image, so both ride
    a re-captured surface's frame consistently (not just its pose)."""
    for key, comp in (e.get("components") or {}).items():
        if isinstance(comp, dict) and comp.get("width") and comp.get("height"):
            return key
    return None


def _on_surface_set(spos: list[float], srot: list[float], extent, e: dict) -> dict:
    """The `update`-op `set` for on-surface content (a placed image OR an image-bearing dynamic module):
    face the room interior (upright, via `_face_room`), sit 2 cm in front, re-fit to the surface frame
    keeping the content's current aspect, and carry the host-local offset (§7c-B2) so the client can ride
    it without its own copy of the host seed pose. A horizontal surface reuses the placing viewer's facing
    from `meta.content_up` (surface-local), so a re-capture keeps the bottom edge toward where it was placed."""
    off = (e.get("meta") or {}).get("surface_offset")
    if off and off.get("p") and off.get("q"):
        # RIDE the stored host-local offset: content = host · offset — the same composition the client
        # renders with. Re-deriving from scratch instead (below) re-centres the content on its surface and
        # rewrites the offset to match, which silently threw away any repositioning the user did within the
        # frame: a moved image snapped back to the middle on the next re-anchor while its SIZE survived
        # (scale isn't touched here), which is exactly how that bug presented.
        qh = _euler_yxz_quat(srot)
        qi = _quat_mul(qh, list(off["q"]))
        pos = [spos[i] + _quat_rot(qh, list(off["p"]))[i] for i in range(3)]
        rot = _basis_yxz(_quat_rot(qi, [1.0, 0.0, 0.0]), _quat_rot(qi, [0.0, 1.0, 0.0]),
                         _quat_rot(qi, [0.0, 0.0, 1.0]))
        out: dict = {"transform.position": [round(c, 5) for c in pos],
                     "transform.rotation": [round(c, 4) for c in rot]}
    else:
        # No offset yet (first placement / legacy content): centre it on the surface, facing the room.
        fr = _face_room(srot, (e.get("meta") or {}).get("content_up"))
        f = fr["forward"]
        pos = [spos[i] + get_settings().on_surface_standoff * f[i] for i in range(3)]
        out = {"transform.position": pos, "transform.rotation": fr["rotation"],
               "meta.surface_offset": _surface_offset(spos, srot, pos, fr["rotation"])}
    dc = _dims_component(e)                       # geometry (image) or the module's own component (water)
    if extent and dc:
        comp = e["components"][dc]
        w, h = _fit_extent(comp["width"] / comp["height"], extent)
        out[f"components.{dc}.width"], out[f"components.{dc}.height"] = w, h
    return out


def _reanchor_surface_images(doc: dict) -> None:
    """Re-pin every on-surface image (meta.on_surface) to its surface's CURRENT pose, mutating `doc` in
    place. Run at compose time (world load) so a re-registration that moved the space's surfaces never
    leaves an image stranded off its frame."""
    surfaces = {e["id"]: e for e in doc.get("entities", []) if e.get("meta", {}).get("real")}
    for e in doc.get("entities", []):
        surf = surfaces.get((e.get("meta") or {}).get("on_surface"))
        spos = surf and surf.get("transform", {}).get("position")
        if not spos:
            continue
        sets = _on_surface_set(spos, surf.get("transform", {}).get("rotation") or [0.0, 0.0, 0.0],
                               surf.get("components", {}).get("surface", {}).get("extent"), e)
        for path, val in sets.items():
            _set_path(e, path, val)


def _reanchor_ops(doc: dict, moved: dict) -> list[dict]:
    """`update` ops re-pinning on-surface images whose surface id is in `moved` (id → {position, rotation,
    extent}, e.g. just re-captured). Used live in ingest_room so the image rides the re-captured surface."""
    ops = []
    for e in doc.get("entities", []):
        s = moved.get((e.get("meta") or {}).get("on_surface"))
        if not s or not s.get("position"):
            continue
        sets = _on_surface_set(s["position"], s.get("rotation") or [0.0, 0.0, 0.0], s.get("extent"), e)
        ops.append({"op": "update", "id": e["id"], "set": sets})
    return ops


# --- view-relative geometry (gaze): resolve a point/probe relative to where a user is looking ------
def _dot(a: list[float], b: list[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _plane_basis(rotation: list[float]) -> tuple[list[float], list[float], list[float]]:
    """(normal, in-plane right, in-plane up) of an <a-plane> at A-Frame euler `rotation` (degrees;
    roll ignored, like _forward). normal is the plane's +Z; right/up are its +X/+Y."""
    rx, ry = math.radians(rotation[0]), math.radians(rotation[1])
    cx, sx, cy, sy = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry)
    return [cx * sy, -sx, cx * cy], [cy, 0.0, -sy], [sx * sy, cx, sx * cy]


def _ray_surface(origin: list[float], direction: list[float]) -> Optional[dict]:
    """Nearest REAL room surface a ray (from `origin` along unit `direction`) hits within its extent —
    'the wall I'm looking at'. Returns {id, semantic, friendly_id, distance, point} or None."""
    best = None
    for e in store.doc["entities"]:
        m = e.get("meta", {})
        if not m.get("real"):
            continue
        surf = e.get("components", {}).get("surface", {})
        extent, pos = surf.get("extent"), e.get("transform", {}).get("position")
        if not (extent and len(extent) >= 2 and pos):
            continue
        normal, right, up = _plane_basis(e.get("transform", {}).get("rotation") or [0, 0, 0])
        denom = _dot(direction, normal)
        if abs(denom) < 1e-6:
            continue                                       # ray parallel to the plane
        rel = [pos[i] - origin[i] for i in range(3)]
        t = _dot(rel, normal) / denom
        if t <= 1e-3:
            continue                                       # behind the viewer
        hit = [origin[i] + direction[i] * t for i in range(3)]
        d = [hit[i] - pos[i] for i in range(3)]
        if abs(_dot(d, right)) <= extent[0] / 2 + 1e-3 and abs(_dot(d, up)) <= extent[1] / 2 + 1e-3:
            if best is None or t < best["distance"]:
                best = {"id": e["id"], "semantic": m.get("semantic"), "friendly_id": m.get("friendly_id"),
                        "distance": round(t, 3), "point": [round(c, 3) for c in hit]}
    return best


def _nearby_entities(point: list[float], radius: float) -> list[dict]:
    """Placed (non-real) objects within `radius` of `point`, nearest first — 'what's over there'."""
    out = []
    for e in store.doc["entities"]:
        m = e.get("meta", {})
        if m.get("real"):
            continue
        pos = e.get("transform", {}).get("position")
        if not pos or len(pos) < 3:
            continue
        dist = math.dist(point, pos)
        if dist <= radius:
            out.append({"id": e["id"], "title": m.get("title") or m.get("prompt"), "distance": round(dist, 3)})
    out.sort(key=lambda x: x["distance"])
    return out


_VIEW_DIRS = {"forward", "back", "left", "right", "up", "down"}


class ViewRelativeRequest(BaseModel):
    direction: str = "forward"            # forward|back|left|right|up|down (forward = where you're looking)
    distance: float = 1.0                 # metres along that direction


@app.post("/view_relative")
async def view_relative(req: ViewRelativeRequest, request: Request) -> dict:
    """Resolve a point relative to the requesting user's live head pose, and probe what's there:
    '1 m in front of me', 'the wall I'm looking at', 'what's behind me'. The frame is the user's own
    head (forward = look dir incl. pitch; left/right/up/down head-relative). Gaze comes from presence,
    keyed to the X-Conjure-User header."""
    user = request.headers.get("X-Conjure-User") or active_scope.split("/", 1)[0]
    g = gaze.get(user)
    if not g:
        return {"ok": False, "error": "no live view for you yet — connect a headset/session and look around first"}
    d = (req.direction or "forward").strip().lower()
    if d not in _VIEW_DIRS:
        return {"ok": False, "error": f"direction must be one of {sorted(_VIEW_DIRS)}"}
    # §7b: prefer the head pose SOLVED from the plane-relative head anchor against the seed (non-rigid-
    # consistent) over the presence pose (rigid T, approximate off-authority). Falls back to the presence
    # pose for a desktop viewer / void world / degenerate solve.
    gv = _head_from_anchor(g.get("anchor")) or g
    resolved = "anchor" if gv is not g else "pose"
    vec = {"forward": gv["forward"], "back": [-c for c in gv["forward"]],
           "right": gv["right"], "left": [-c for c in gv["right"]],
           "up": gv["up"], "down": [-c for c in gv["up"]]}[d]
    origin = gv["origin"]
    point = [round(origin[i] + vec[i] * req.distance, 3) for i in range(3)]
    return {"ok": True, "direction": d, "distance": req.distance, "origin": [round(c, 3) for c in origin],
            "resolved_via": resolved, "direction_vec": [round(c, 4) for c in vec], "point": point,
            "surface": _ray_surface(origin, vec), "nearby": _nearby_entities(point, 1.5)}


class PlaceImageRequest(BaseModel):
    image_id: str
    position: Optional[list[float]] = None
    size_m: Optional[float] = None
    name: Optional[str] = None
    on_surface: Optional[str] = None   # hang on a real surface (id/label/number) — align + fit to it
    billboard: bool = False            # free-standing only: always turn to face the viewer (yaw-only)
    stereo: Optional[str] = None       # 'sbs' | 'tb' — render as a stereo pair; else auto from catalog
    stretch: bool = False              # on_surface: fill the whole surface (default fits inside, aspect-correct)


@app.post("/place_image")
async def place_image(req: PlaceImageRequest, request: Request) -> dict:
    """Hang a previously-procured image (by id) as a textured plane facing the user. If `name` is an
    existing entity, swap its image in place (keeping position)."""
    rec, _, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    if req.billboard and req.on_surface:   # a wall-hung image stays flush to its wall; can't also chase the viewer
        return {"ok": False, "error": "billboard images are free-standing only — omit on_surface"}
    # Stereo: a tag recorded by the importer is authoritative; an explicit request only ADDS to that.
    # Guard the footgun: forcing stereo onto a GENERATED image splits a mono picture into halves (looks
    # like one eye is mirrored). Our generators never produce stereo, so refuse — stereo is for imported
    # side-by-side/top-bottom photos only. (An imported-but-untagged image has no generation op → allowed.)
    tagged = _stereo_layout(library.get(req.image_id) or {})
    if req.stereo and not tagged and rec.op in PROCURE_OPS:
        return {"ok": False, "error": (
            f"stereo only applies to imported side-by-side/top-bottom photos — {req.image_id!r} was "
            "generated, so it isn't a stereo pair (import a real stereo image instead)")}
    stereo = req.stereo or tagged
    pos = req.position or [0.0, 1.5, -3.0]  # eye height, on the wall in front
    width, height = _plane_dims(rec, req.size_m or 1.0, stereo)
    rotation = None
    if req.on_surface:  # hang on a real surface: face the room (upright), fit its frame, sit just in front
        surfaces = _room_targets(req.on_surface)
        if not surfaces:
            return {"ok": False, "error": f"no room surface matches {req.on_surface!r}"}
        surf = surfaces[0]
        srot = surf.get("transform", {}).get("rotation") or [0.0, 0.0, 0.0]
        spos = surf.get("transform", {}).get("position") or pos
        extent = surf.get("components", {}).get("surface", {}).get("extent")
        if extent:                                         # fit inside the frame (aspect-correct) unless stretch=fill
            width, height = (float(extent[0]), float(extent[1])) if req.stretch else _fit_dims(rec, extent, stereo)
        caller = request.headers.get("X-Conjure-User") or active_scope.split("/", 1)[0]
        up_local = _content_up_local(srot, spos, caller)  # horizontal surface → bottom edge toward the viewer
        fr = _face_room(srot, up_local)                   # face the room interior (-normal), upright
        rotation = fr["rotation"]
        pos = [spos[i] + get_settings().on_surface_standoff * fr["forward"][i] for i in range(3)]   # toward the viewer
    eid = req.name or f"ent_image_{uuid4().hex[:6]}"
    meta = {"generated": True, "provider": rec.provider, "model": rec.model,
            "prompt": rec.prompt, "image_id": rec.id}
    if req.on_surface:                                 # remember the home surface so it re-anchors on re-capture
        meta["on_surface"] = surf["id"]
        if up_local:                                   # remember the viewer facing so re-capture reproduces it
            meta["content_up"] = up_local
        meta["surface_offset"] = _surface_offset(spos, srot, pos, rotation)   # §7c-B2: client rides this offset
    # A transparent (alpha) image must render with transparency on, or three.js shows it opaque.
    material = {"src": rec.url, "shader": "flat", "side": "double", "transparent": rec.transparent}

    existing = any(e["id"] == eid for e in store.doc["entities"])
    if existing:  # swap in place
        sets = {
            "components.material.src": rec.url, "components.material.transparent": rec.transparent,
            "components.geometry.width": width, "components.geometry.height": height,
            "meta.image_id": rec.id, "meta.prompt": rec.prompt,
            "meta.provider": rec.provider, "meta.model": rec.model}
        if req.on_surface:  # re-hanging on a surface ⇒ also re-align/reposition + record the home surface
            sets["transform.position"] = pos
            sets["transform.rotation"] = rotation
            sets["meta.on_surface"] = surf["id"]
            if up_local:
                sets["meta.content_up"] = up_local
            sets["meta.surface_offset"] = _surface_offset(spos, srot, pos, rotation)   # §7c-B2
        if req.billboard:   # turn an existing free-standing image into a viewer-facing billboard
            sets["components.billboard"] = {"yaw": True}
        if stereo:  # swapping in a stereo image ⇒ turn on the per-eye split
            sets["components.stereo"] = {"layout": stereo}
        ops = [{"op": "update", "id": eid, "set": sets}]
    else:
        ops = [_image_plane(eid, pos, width, height, material, meta, rotation,
                            billboard=req.billboard, stereo=stereo)]
    await _broadcast({"type": "patch", "patch": store.apply_patch(ops, origin="image")})
    return _with_notice({"ok": True, "id": eid, "image_id": rec.id}, _ensure_referenced_public(rec.id))


# --- dynamic content modules (docs/specs/dynamics.md) ------------------------------------------
# A module is an A-Frame component the client renders from an entity's `components` (see
# docs/specs/dynamics.md §5); placing one is just adding that entity, so it's config-in-snapshot,
# shared, and persisted on the existing path. Modules are now FIRST-CLASS + extensible — discovered from
# `dynamics/<name>/module.json` on a user-first search path (mirror of agents), not a hardcoded dict.
# The world server serves + places ALL discovered modules; per-request it scopes to the ACTIVE agent's
# `dynamics` allow-list (soft catalog + this hard endpoint check — the plan's decision 1).


def _dynamics_registry() -> dict[str, "dynamics_loader.DynamicModuleDef"]:
    """All discovered dynamic modules (loader-backed). Loaded fresh so a newly-added user module appears
    without a restart; the set is tiny. A single malformed module is skipped, never fatal to the world."""
    reg: dict = {}
    for name in dynamics_loader.module_names():
        try:
            reg[name] = dynamics_loader.load_module(name)
        except (ValueError, FileNotFoundError, OSError) as e:
            _slog("dynamics", f"skipping module {name!r}: {e}")
    return reg


def _active_agent_dynamics() -> list[str]:
    """The dynamic modules the ACTIVE agent may conjure (its agent.json `dynamics`) — scopes /module, the
    served <script>s, and the director catalog. Empty if the agent can't be loaded (fail closed)."""
    try:
        return load_agent(agent_of(active_scope)).dynamics
    except Exception as e:
        _slog("dynamics", f"active agent dynamics unavailable: {e}")
        return []


def _dynamic_module_tags() -> tuple[str, list[int]]:
    """The `<script>` tags for EVERY discovered module, injected into index.html, each
    `src="/dynamics/<name>/<entry>?v=<mtime>"` so a code change busts the Quest's stubborn cache.
    Returns (html, [mtimes]) — the mtimes feed the client-version badge. Unknown/broken modules are
    skipped.

    Deliberately NOT scoped to the active agent (docs/specs/dynamics.md §9). A page's scripts are fixed
    the moment it loads, but the live agent is not: space selection joins the matched room's world in
    whatever scope owns it (`/space/select`), `agent <name>` moves the pointer, a session switch moves
    it again. Any of those can hand a headset a world full of components its page never registered —
    and an unregistered component is SILENT (`el.setAttribute("grab", …)` on an unknown name is just a
    DOM attribute), so the module renders nothing and logs nothing until someone reloads the page.
    Registering a component is inert until an entity carries it, so serving all of them costs a few KB
    and removes the whole class of ordering bugs. Scoping still bites where it decides anything:
    `/module` refuses an out-of-scope conjure, and the director's catalog only lists the agent's own."""
    registry = _dynamics_registry()
    tags: list[str] = []
    mtimes: list[int] = []
    for name in sorted(registry):
        spec = registry[name]
        for fname in spec.entry:
            try:
                mt = int((spec.dir / fname).stat().st_mtime)
            except OSError:
                continue
            mtimes.append(mt)
            tags.append(f'    <script src="/dynamics/{name}/{fname}?v={mt}"></script>\n')
    return "".join(tags), mtimes


class PlaceModuleRequest(BaseModel):
    module: str
    config: Optional[dict] = None
    position: Optional[list[float]] = None
    on_surface: Optional[str] = None   # mount on a real surface (id/label/number) — align + fit like place_image
    billboard: bool = False            # compose the billboard component (always face viewer) onto this instance
    stretch: bool = False              # on_surface: fill the whole surface (default fits inside, aspect-correct)
    name: Optional[str] = None


@app.post("/module")
async def place_module(req: PlaceModuleRequest, request: Request) -> dict:
    """Conjure a dynamic module (docs/specs/dynamics.md §1): add an entity carrying the module's
    component, so every client renders the same effect (shared, deterministic from the shared clock).
    `name` reuses/reconfigures an existing instance; a singleton module reuses its one instance.
    Scoped to the ACTIVE agent's `dynamics` allow-list (the hard half of the plan's soft+hard scoping)."""
    registry = _dynamics_registry()
    spec = registry.get(req.module)
    if not spec:
        return {"ok": False, "error": f"unknown module {req.module!r}; available: "
                f"{', '.join(sorted(registry))}"}
    allowed = _active_agent_dynamics()
    if req.module not in allowed:
        return {"ok": False, "error": f"module {req.module!r} is not available to the active agent "
                f"(allowed: {', '.join(sorted(allowed)) or 'none'})"}
    config = dict(req.config or {})
    # Convenience: a module can take an `image` id (from generate_image/import) — resolve it to a src URL
    # here (like place_image), so "a water picture of a koi pond" is generate_image → conjure_module.
    rec = None
    if config.get("image"):
        rec, _, err = _get_image(str(config.pop("image")))
        if err:
            return {"ok": False, "error": err}
        config["src"] = rec.url
    # Reject a config value outside its schema's `enum` — with the valid choices in the message, so a caller
    # that guessed can correct itself on the next call.
    #
    # This exists because of a silent failure worth not repeating (2026-09-01): the director conjured `grab`
    # with mode="sky" and then mode="frame" — plausible guesses, taken from internal field names — got
    # {"ok": true} both times, and told the user each mode was active while the client, seeing a value it did
    # not recognise, quietly stayed in `object` mode. Nothing anywhere said no. An unvalidated enum makes a
    # caller's wrong guess indistinguishable from success, and the user pays for it in confusion.
    for key, val in list(config.items()):
        cs = (spec.config_schema or {}).get(key)
        choices = cs.get("enum") if isinstance(cs, dict) else None
        if isinstance(choices, list) and choices and val not in choices:
            return {"ok": False, "error": f"{req.module}: {key}={val!r} is not valid; "
                    f"use one of: {', '.join(str(c) for c in choices)}"}
    comp = spec.component
    pos = req.position or list(spec.default_pos or [0.0, 1.3, -1.5])
    rotation = None
    meta = {"module": req.module, "dynamic": True}
    extra_components: dict = {}
    if req.on_surface:   # mount on a real surface: align to it, fit its frame, ride it (like place_image)
        surfaces = _room_targets(req.on_surface)
        if not surfaces:
            return {"ok": False, "error": f"no room surface matches {req.on_surface!r}"}
        surf = surfaces[0]
        srot = surf.get("transform", {}).get("rotation") or [0.0, 0.0, 0.0]
        spos = surf.get("transform", {}).get("position") or pos
        extent = surf.get("components", {}).get("surface", {}).get("extent")
        # An image module (water) fits its picture's aspect INSIDE the frame by default (like place_image);
        # `stretch` fills the whole surface. A non-image module leaves its own sizing alone.
        if rec is not None and extent:
            config["width"], config["height"] = _module_plane_dims(rec, config, extent, stretch=req.stretch)
        caller = request.headers.get("X-Conjure-User") or active_scope.split("/", 1)[0]
        up_local = _content_up_local(srot, spos, caller)       # horizontal surface → bottom edge toward viewer
        fr = _face_room(srot, up_local)                        # face the room interior (-normal), upright
        rotation = fr["rotation"]
        pos = [spos[i] + get_settings().on_surface_standoff * fr["forward"][i] for i in range(3)]
        meta["on_surface"] = surf["id"]
        if up_local:
            meta["content_up"] = up_local                      # reproduce the facing on recapture
        meta["surface_offset"] = _surface_offset(spos, srot, pos, rotation)   # ride the surface on recapture
    else:
        # Free-standing image module → size the plane to the picture's aspect (like place_image), instead
        # of the component's fixed default; stretch is a surface-fill option, so it's irrelevant here.
        if rec is not None:
            config["width"], config["height"] = _module_plane_dims(rec, config, None, stretch=False)
        if spec.face_user:   # free-standing flat content → face the viewer AT CREATION (fixed, not billboard)
            caller = request.headers.get("X-Conjure-User") or active_scope.split("/", 1)[0]
            fu = _face_user(caller, req.position)
            if fu:
                pos, rotation = fu["position"], fu["rotation"]
    # Billboard is an ORTHOGONAL, composable behavior (its own A-Frame component) — attach it to ANY flat
    # module on request; it then always faces the viewer, over-riding the fixed spawn facing.
    if req.billboard:
        extra_components["billboard"] = {"yaw": True}

    eid = req.name
    if not eid and spec.singleton:   # one instance: reuse the existing entity if present
        eid = next((e["id"] for e in store.doc["entities"]
                    if (e.get("meta") or {}).get("module") == req.module), None)
    eid = eid or f"mod_{req.module}_{uuid4().hex[:6]}"
    existing = any(e["id"] == eid for e in store.doc["entities"])
    if existing:   # reconfigure/move in place
        sets = {f"components.{comp}": config, "transform.position": pos}
        if rotation is not None:
            sets["transform.rotation"] = rotation
        ops = [{"op": "update", "id": eid, "set": sets}]
    else:
        transform: dict = {"position": pos}
        if rotation is not None:
            transform["rotation"] = rotation
        components = dict(extra_components); components[comp] = config
        ops = [{"op": "add", "entity": {"id": eid, "transform": transform,
                "components": components, "meta": meta}}]
    await _broadcast({"type": "patch", "patch": store.apply_patch(ops, origin="module")})
    return {"ok": True, "id": eid, "module": req.module}


# --- figures: pose a rigged model through its humanoid bone map (docs/backlogs/figures.md) ---------
def _aim_problem(bone: str, aim, frame: dict, rot: dict) -> Optional[str]:
    """What is wrong with an `aim` request, or None. Every branch refuses LOUDLY rather than no-op.

    That is not politeness. A pose that silently does nothing is indistinguishable from a pose the user
    simply cannot see from where they are standing, and this feature has now shipped that failure twice
    (grab's unreachable modes; three fixes the headset never ran)."""
    if bone in TRUNK_BONES:
        return (f"{bone}: aim points a bone along its own LENGTH, so on the trunk it would mean aiming "
                f"the top of the skull — use bend, spread or turn there")
    for other in ("bend", "spread"):
        if other in rot:
            return f"{bone}: aim and {other} both set the swing — use one or the other"
    if isinstance(aim, str):
        if aim not in AIM_DIRECTIONS:
            return (f"{bone}: unknown direction {aim!r} — use {', '.join(AIM_DIRECTIONS)}, "
                    "or a vector [out, up, forward]")
    else:
        if not isinstance(aim, (list, tuple)) or len(aim) != 3:
            return (f"{bone}: aim takes a direction ({', '.join(AIM_DIRECTIONS)}) or a vector "
                    "[out, up, forward]")
        try:
            vals = [float(c) for c in aim]
        except (TypeError, ValueError):
            return f"{bone}: aim vector must be three numbers"
        if not all(math.isfinite(v) for v in vals) or not any(vals):
            return f"{bone}: aim vector must be finite and not all zero"
    if not all(k in frame for k in FRAME_VECTORS):
        # The bone map is fine; the FRAME was measured by an older build. Placing again re-measures it.
        return f"{bone}: this figure's frame predates aiming — place it again to measure one"
    return None


class FigureRequest(BaseModel):
    id: str                                       # entity id of a placed rigged model
    pose: Optional[dict] = None                   # {semanticBone: {bend|spread|turn: DEGREES}}
    clear: bool = False                           # drop the pose and return to the bind pose


@app.post("/figure")
async def figure(req: FigureRequest) -> dict:
    """Pose a figure in ANATOMICAL terms — semantic bone names, and semantic axes to rotate them about.

    Two indirections, and the feature needs both. The bone map answers *which* node to rotate, so a
    caller says "leftUpperArm" without knowing that this rig calls it `upper_arm.fk.L`, the next
    `J_Bip_L_UpperArm` and a third `lShldrBend`. The anatomical frame answers *which way*: a bone's own
    local axes are whatever its rigger chose — measured, `leftUpperLeg` rests 177 degrees from identity
    on Grace and 6 on Saka — so raw euler angles meant a different motion on every figure. `bend`,
    `spread` and `turn` are measured from the bind pose at import and mean the same thing on all of them.

    Those three are RELATIVE — right for an adjustment, wrong for a destination, because a relative
    number asks the caller to know where the bone rests and the three rigs disagree by 48 degrees about
    where an arm does. `aim` is the absolute form: a named body direction, resolved as the swing from
    wherever that bone rests onto it. Measured on device: asked to raise an arm, the director emitted the
    same numbers for "up" and for "down", because with only relative words available there was nothing
    else it could do.

    The pose is stored on the entity's `figure` component in exactly the terms it was asked for, because
    that state is durable: it is what a reload replays and what a persona will later reason about. The
    client resolves it against the axes shipped alongside (specs/dynamics.md §1)."""
    ent = next((e for e in store.doc["entities"] if e["id"] == req.id), None)
    if ent is None:
        return {"ok": False, "error": f"no entity {req.id!r}"}
    meta = ent.get("meta") or {}
    if not meta.get("rigged"):
        return {"ok": False, "error": f"{req.id!r} is not a rigged figure"}

    humanoid = meta.get("humanoid") or {}
    if not humanoid:
        return {"ok": False, "error": f"{req.id!r} has no humanoid bone map, so it cannot be posed"}
    axes = meta.get("humanoid_axes") or {}
    if not axes:
        return {"ok": False, "error": f"{req.id!r} has no anatomical frame — it was placed before "
                "figures could be posed anatomically; place it again to measure one"}

    component = {"humanoid": json.dumps(humanoid), "axes": json.dumps(axes, separators=(",", ":"))}
    follows = meta.get("humanoid_follows")
    if follows:
        component["follows"] = json.dumps(follows, separators=(",", ":"))

    if req.clear:
        await _broadcast({"type": "patch",
                          "patch": store.apply_patch([{"op": "update", "id": req.id,
                                                       "set": {"components.figure": {**component,
                                                                                     "pose": ""}}}],
                                                     origin="figure")})
        return {"ok": True, "id": req.id, "cleared": True}

    pose = req.pose or {}
    if not isinstance(pose, dict) or not pose:
        return {"ok": False, "error": "pass a pose like {\"leftUpperArm\": {\"bend\": 45}}, or clear=true"}

    # A bone with a name but no frame is not posable: two of Saka's 54 have no measurable direction.
    # Saying so is the point — a silent no-op on something nobody can see is the failure mode this
    # feature keeps rediscovering (docs/backlogs/figures.md, grab's mode fiasco).
    unknown = [b for b in pose if b not in axes]
    if unknown:
        return {"ok": False, "error": f"unknown bone(s) {', '.join(sorted(unknown))}; "
                f"this figure has: {', '.join(sorted(axes))}"}
    clean: dict = {}
    for bone, rot in pose.items():
        if not isinstance(rot, dict):
            return {"ok": False, "error": f"{bone}: expected {{{', '.join(sorted(POSE_AXES))}}} in degrees "
                    "or {\"aim\": \"up\"}"}
        bad = [k for k in rot if k not in POSE_AXES and k != "aim"]
        if bad:
            return {"ok": False, "error": f"{bone}: unknown rotation(s) {', '.join(sorted(bad))} — "
                    f"use {', '.join(sorted(POSE_AXES))} or aim"}
        vals: dict = {}
        if rot.get("aim") is not None:
            problem = _aim_problem(bone, rot["aim"], axes.get(bone) or {}, rot)
            if problem:
                return {"ok": False, "error": problem}
            vals["aim"] = rot["aim"] if isinstance(rot["aim"], str) else [float(c) for c in rot["aim"]]
        for k, v in rot.items():
            if k == "aim":
                continue
            try:
                angle = float(v)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"{bone}.{k}: expected degrees, got {v!r}"}
            if not math.isfinite(angle):
                # Same hazard as /world_frame: a non-finite angle blanks that branch of the scene graph
                # and stays blanked, and a persisted one comes back on every reload.
                return {"ok": False, "error": f"{bone}.{k}: angles must be finite"}
            vals[k] = angle
        clean[bone] = vals                      # an empty {} is legal: it returns that bone to rest

    # Merge onto any existing pose so a caller can move one arm without resetting the rest. Per BONE,
    # not per axis: "bend her elbow" after "turn her elbow" replaces the elbow, which is what a reader
    # of the second instruction expects.
    prior = ((ent.get("components") or {}).get("figure") or {}).get("pose") or ""
    merged = {}
    if prior:
        try:
            merged = json.loads(prior)
        except ValueError:
            merged = {}
    merged.update(clean)
    merged = {b: r for b, r in merged.items() if r}          # a cleared bone leaves no trace
    sets = {"components.figure": {**component, "pose": json.dumps(merged)}}
    # Resolve it once here purely to REPORT what the joints refused. The client resolves it again for
    # real; this costs a few hundred multiplications and is what turns a silent clamp into feedback the
    # caller can act on — the director asked for 90 degrees of hip extension twice in one session, and
    # nothing told it otherwise.
    limited: list = []
    resolve_pose(axes, clean, limited)
    await _broadcast({"type": "patch",
                      "patch": store.apply_patch([{"op": "update", "id": req.id, "set": sets}],
                                                 origin="figure")})
    out = {"ok": True, "id": req.id, "posed": sorted(clean), "bones": len(merged)}
    if limited:
        out["limited"] = limited
    return out


class DismissModuleRequest(BaseModel):
    name: Optional[str] = None     # a specific entity id
    module: Optional[str] = None   # …or every instance of this module kind


@app.post("/module/dismiss")
async def dismiss_module(req: DismissModuleRequest) -> dict:
    """Unload a dynamic module — remove its entity/entities (the client disposes GPU resources in the
    component's remove())."""
    if req.name:
        ids = [req.name] if any(e["id"] == req.name for e in store.doc["entities"]) else []
    elif req.module:
        # Match by module meta OR by carrying the module's component — so "remove the fireflies" also
        # catches entities placed outside the tool (a raw /patch add, a pre-meta/legacy instance) that
        # have no meta.module but do have the fireflies component.
        spec = _dynamics_registry().get(req.module)
        comp = spec.component if spec else req.module
        ids = [e["id"] for e in store.doc["entities"]
               if (e.get("meta") or {}).get("module") == req.module or comp in (e.get("components") or {})]
    else:
        return {"ok": False, "error": "pass a module name (entity id) or a module kind to dismiss"}
    if not ids:
        return {"ok": False, "error": "no matching module to dismiss"}
    patch = store.apply_patch([{"op": "remove", "id": i} for i in ids], origin="module")
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "removed": ids}


# --- tier-C manipulation: commit a client-side drag/rotate/resize (docs/specs/dynamics.md §8).
#     The `grab` dynamic module manipulates a placed object's transform LOCALLY while dragging, then
#     posts the resting transform here on release; the world server is the authority — it applies, persists
#     (autosave), and broadcasts to all. The mover's echo is idempotent (it already holds these values). ----
class ManipulateRequest(BaseModel):
    id: str
    position: Optional[list[float]] = None
    rotation: Optional[list[float]] = None   # A-Frame euler degrees (YXZ), like every other transform
    scale: Optional[list[float]] = None
    # The plane-relative anchor the CLIENT authored against its own walls, stored verbatim. Anchors are
    # plane-relative (shared surface ids + offsets), so one authored against any client's walls solves
    # correctly on every other client. Preferring it avoids re-authoring here from the committed position:
    # that adds author/solve hops between plane sets that aren't rigidly related, and the residual shows up
    # as content settling slightly off where the user dropped it. Omitted (no room basis) ⇒ we re-author.
    anchor: Optional[dict] = None
    # Likewise for SURFACE-ATTACHED content: the host-local offset (host⁻¹·content) the client computed
    # against its own rendered host. Host-relative ⇒ frame-independent ⇒ stored verbatim.
    surface_offset: Optional[dict] = None


@app.post("/manipulate")
async def manipulate_entity(req: ManipulateRequest) -> dict:
    """Commit a placed object's new resting transform after a `grab` manipulation (tier C). Owner-gated
    like every world write. Real room surfaces are never movable. For on-surface content, recompute
    `meta.surface_offset` from the new pose so it still rides a room recapture (mirrors place_image)."""
    ent = next((e for e in store.doc["entities"] if e["id"] == req.id), None)
    if ent is None:
        return {"ok": False, "error": f"no entity {req.id!r}"}
    if (ent.get("meta") or {}).get("real"):
        return {"ok": False, "error": "real room surfaces can't be moved"}
    sets: dict = {}
    if req.position is not None:
        sets["transform.position"] = req.position
    if req.rotation is not None:
        sets["transform.rotation"] = req.rotation
    if req.scale is not None:
        sets["transform.scale"] = req.scale
    if not sets:
        return {"ok": False, "error": "nothing to change"}
    surf_id = (ent.get("meta") or {}).get("on_surface")
    if surf_id and req.surface_offset:
        sets["meta.surface_offset"] = req.surface_offset   # client-computed against its own rendered host
    elif surf_id and (req.position is not None or req.rotation is not None):
        surf = next((e for e in store.doc["entities"] if e["id"] == surf_id), None)
        spos = surf and surf.get("transform", {}).get("position")
        if spos:
            srot = surf.get("transform", {}).get("rotation") or [0.0, 0.0, 0.0]
            npos = req.position or ent.get("transform", {}).get("position")
            nrot = req.rotation if req.rotation is not None else (ent.get("transform", {}).get("rotation") or [0.0, 0.0, 0.0])
            if npos:
                sets["meta.surface_offset"] = _surface_offset(spos, srot, npos, nrot)
    # Store the client's anchor for ANY non-surface content, whether or not it already had one. Content
    # without a stored anchor isn't un-anchored in practice: the client's _placeContent already authors one
    # on the fly from the F_ref pose every capture, so it's wall-solved either way. Keeping the exact anchor
    # the user's drop produced just replaces a re-derived approximation with the real thing — the same
    # accuracy models get. (Surface-attached content is host-relative; surface_offset covers it below.)
    # …but NOT in a room-less world. An anchor is plane-relative — surface ids plus offsets — so in a VOID
    # world it names walls that do not exist here, and any client holding a stale basis will solve it and
    # teleport the object. The client should not send one (it needs a basis to author it), but a stale
    # basis is exactly the bug this guards: refusing it here contains a client-side fault to that client,
    # instead of persisting it into the world for everyone and every reload.
    if req.anchor and not (ent.get("meta") or {}).get("on_surface") and not _no_space():
        sets["meta.anchor"] = req.anchor          # client-authored, stored as-is (see ManipulateRequest)
    applied = store.apply_patch([{"op": "update", "id": req.id, "set": sets}], origin="manipulate")
    # ANCHORED content (a grounded model) re-derives its pose from `meta.anchor` on every client capture, so a
    # move that doesn't RE-AUTHOR the anchor is reverted at the next capture/reload — the "I moved it but it
    # didn't survive" bug. Exactly what /patch does for the same reason (§7c). Skipped when the client sent
    # its own anchor above — re-authoring would throw away the exact one and reintroduce the drift.
    reanchor = [] if "meta.anchor" in sets else _reanchor_moved_content_ops(applied["ops"])
    if reanchor:
        extra = store.apply_patch(reanchor, origin="manipulate")
        applied = {"rev": extra["rev"], "origin": "manipulate",
                   "ops": applied["ops"] + extra["ops"], "inverse": extra["inverse"] + applied["inverse"]}
    await _broadcast({"type": "patch", "patch": applied})
    return {"ok": True, "id": req.id}


class WorldFrameRequest(BaseModel):
    """A `grab` skybox/void-mode commit, or a reset of either (docs/specs/dynamics.md §8b)."""

    sky: dict | None = None      # {"yaw": deg, "scale": factor} — relative to the sky's derived pose
    frame: dict | None = None    # {"yaw": deg, "offset": [x, z]} — rigid horizontal, void worlds only
    reset: str | None = None     # "sky" | "frame" | "all" — back to the derived frame


# Defaults a reset returns to: no rotation, no offset, unit scale — i.e. the derived frame standing alone.
# Keyed by DOTTED PATH for the same reason every write below is: see the docstring.
_FRAME_DEFAULTS = {
    "sky": {"frame.skyYaw": 0.0, "frame.skyScale": 1.0},
    "frame": {"frame.yaw": 0.0, "frame.offset": [0.0, 0.0]},
}
# Caller-facing group/key → the stored dotted path. The caller says `sky: {yaw}`, which reads naturally and
# matches the mode names; storage keeps EVERYTHING under `environment.frame` — see the docstring for why the
# sky's delta must not live under `environment.sky`.
_FRAME_PATHS = {
    ("sky", "yaw"): "frame.skyYaw", ("sky", "scale"): "frame.skyScale",
    ("frame", "yaw"): "frame.yaw", ("frame", "offset"): "frame.offset",
}


@app.post("/world_frame")
async def set_world_frame(req: WorldFrameRequest) -> dict:
    """Persist a user adjustment to a DERIVED frame — the skybox's relative orientation/scale, or a void
    world's content orientation/position. Owner-gated like every world write.

    These are not entity transforms, which is why they live in `environment` rather than going through
    /manipulate: the client rewrites both the skybox pose and a void world's `#world-root` parking from the
    derived frame on every capture, so what persists is a DELTA the client composes on top, never a pose.

    Everything is stored under `environment.frame`, INCLUDING the sky's delta, and every write uses a dotted
    path. Both of those are scar tissue from the same afternoon (2026-09-01):

    - Writing a whole `sky` dict erased `sky.src` — turning the sky one degree threw the image away. Same
      failure the seed's write-gate exists to prevent (docs/investigations/raised-floor.md): write the aspect
      that changed, never the record it lives in. Caught by a test.
    - Dotted paths fixed the *document*, but the broadcast patch still carried `{sky: {yaw, scale}}`, and the
      client's `applyEnv` reasonably reads any `sky` object as a full description of the sky — no `src` means
      no panorama, so it tore the dome down on every release. Caught in the headset.

    The second one is the real lesson: as long as the delta lived under `sky`, every reader of `sky` had to
    know it might be a fragment. Moving it to `frame` means the panorama and the user's adjustment are
    simply different keys, and no image tool and no reader can confuse them.

    Validated here for structural safety only (finite, and a positive scale — a zero would collapse the sky
    sphere). The ergonomic bounds live client-side with the gesture that produces them and the metres
    readout that reports them, so there is one home for those numbers rather than two that can disagree.
    """
    sets: dict = {}
    if req.reset in ("sky", "all"):
        sets.update(_FRAME_DEFAULTS["sky"])
    if req.reset in ("frame", "all"):
        sets.update(_FRAME_DEFAULTS["frame"])
    if req.reset and not sets:
        return {"ok": False, "error": f"unknown reset {req.reset!r}; use sky, frame, or all"}

    def _num(container: dict, key: str, label: str) -> float | dict:
        try:
            val = float(container[key])
        except (TypeError, ValueError):
            return {"error": f"{label} must be a number"}
        if val != val or val in (float("inf"), float("-inf")):
            return {"error": f"{label} must be finite"}
        return val

    if req.sky is not None:
        for key in ("yaw", "scale"):
            if key not in req.sky:
                continue
            val = _num(req.sky, key, f"sky.{key}")
            if isinstance(val, dict):
                return {"ok": False, **val}
            if key == "scale" and val <= 0:
                return {"ok": False, "error": "sky.scale must be > 0"}
            sets[_FRAME_PATHS[("sky", key)]] = val

    if req.frame is not None:
        if "yaw" in req.frame:
            val = _num(req.frame, "yaw", "frame.yaw")
            if isinstance(val, dict):
                return {"ok": False, **val}
            sets[_FRAME_PATHS[("frame", "yaw")]] = val
        if "offset" in req.frame:
            off = req.frame["offset"]
            if not isinstance(off, (list, tuple)) or len(off) != 2:
                return {"ok": False, "error": "frame.offset must be [x, z]"}
            pair = []
            for i in (0, 1):
                val = _num({"v": off[i]}, "v", "frame.offset")
                if isinstance(val, dict):
                    return {"ok": False, **val}
                pair.append(val)
            sets[_FRAME_PATHS[("frame", "offset")]] = pair

    if not sets:
        return {"ok": False, "error": "nothing to change"}
    applied = store.apply_patch([{"op": "env", "set": sets}], origin="world_frame")
    await _broadcast({"type": "patch", "patch": applied})
    return {"ok": True, "set": sets}


class SetSkyboxRequest(BaseModel):
    image_id: str


@app.post("/set_skybox")
async def set_skybox(req: SetSkyboxRequest) -> dict:
    """Wrap the whole scene in a previously-procured image (by id) as the sky/environment."""
    rec, _, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    patch = store.apply_patch([{"op": "env", "set": {"sky": {"src": rec.url}}}], origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return _with_notice({"ok": True, "sky": rec.url, "image_id": rec.id}, _ensure_referenced_public(rec.id))


# Ground-projected skybox: the panorama's lower hemisphere is warped flat onto the floor (client-side,
# see grounded-skybox.js) so you stand ON the scene instead of floating above a distant floor. `height`
# (the implied capture height, ≈ standing eye height) and `radius` (the dome size in metres) are the
# two tunables; defaults match the rig's eye height and a comfortably-larger-than-a-room dome.
class SetGroundedSkyboxRequest(BaseModel):
    image_id: str
    height: float = 1.6
    radius: float = 30.0


@app.post("/set_grounded_skybox")
async def set_grounded_skybox(req: SetGroundedSkyboxRequest) -> dict:
    """Wrap the scene in a procured image as a GROUNDED skybox (projected onto the floor at your feet)."""
    rec, _, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    sky = {"src": rec.url, "grounded": True, "height": req.height, "radius": req.radius}
    patch = store.apply_patch([{"op": "env", "set": {"sky": sky}}], origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return _with_notice({"ok": True, "sky": rec.url, "image_id": rec.id}, _ensure_referenced_public(rec.id))


# --- scene editors (hybrid): one-call edits of an image already in the scene (entity id). They
#     procure a new image from the entity's current one, then apply it — convenience over the
#     procure→place flow for the common conversational case. ---------------------------------------

class EditSceneImageRequest(BaseModel):
    id: str
    prompt: str


@app.post("/edit_image")
async def edit_image(req: EditSceneImageRequest) -> dict:
    """Edit an image already in the scene in place — conversational editing ('make it nighttime')."""
    entity, image_id, err = _entity_image(req.id)
    if err:
        return {"ok": False, "error": err}
    _, img, err = _get_image(image_id)
    if err:
        return {"ok": False, "error": err}
    out = await _procure("edit", prompt=req.prompt, requested=None, transparent=False,
                         run=lambda g: g.edit(req.prompt, img))
    if not out["ok"]:
        return out
    new = IMAGES[out["image_id"]]
    patch = store.apply_patch(
        [{"op": "update", "id": req.id,
          "set": {"components.material.src": new.url, "meta.image_id": new.id, "meta.prompt": req.prompt}}],
        origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "id": req.id, "image_id": new.id,
            "provider": new.provider, "model": new.model, "w": new.w, "h": new.h}


class OutpaintSceneRequest(BaseModel):
    id: str
    aspect: Optional[str] = None
    prompt: Optional[str] = None


@app.post("/outpaint_image")
async def outpaint_image(req: OutpaintSceneRequest) -> dict:
    """Extend an in-scene image to a wider frame (outpaint), in place; widens the plane to match."""
    entity, image_id, err = _entity_image(req.id)
    if err:
        return {"ok": False, "error": err}
    _, img, err = _get_image(image_id)
    if err:
        return {"ok": False, "error": err}
    aspect = req.aspect or "16:9"
    instruction = _OUTPAINT_PROMPT.format(aspect=aspect) + (f" {req.prompt}" if req.prompt else "")
    out = await _procure("outpaint", prompt=instruction, requested=None, transparent=False,
                         run=lambda g: g.edit(instruction, img, aspect_ratio=aspect))
    if not out["ok"]:
        return out
    new = IMAGES[out["image_id"]]
    height = entity.get("components", {}).get("geometry", {}).get("height", 1.0)
    new_width = round(height * new.w / new.h, 3) if new.h else height
    patch = store.apply_patch(
        [{"op": "update", "id": req.id,
          "set": {"components.material.src": new.url, "components.geometry.width": new_width,
                  "meta.image_id": new.id}}],
        origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "id": req.id, "image_id": new.id,
            "provider": new.provider, "model": new.model, "w": new.w, "h": new.h}


class SkyboxFromSceneRequest(BaseModel):
    id: str


@app.post("/skybox_from_image")
async def skybox_from_image(req: SkyboxFromSceneRequest) -> dict:
    """Turn an in-scene image into the surrounding 360° sky."""
    entity, image_id, err = _entity_image(req.id)
    if err:
        return {"ok": False, "error": err}
    _, img, err = _get_image(image_id)
    if err:
        return {"ok": False, "error": err}
    out = await _procure("skybox_from", prompt=_SKYBOX_FROM_PROMPT, requested=None, transparent=False,
                         run=lambda g: g.edit(_SKYBOX_FROM_PROMPT, img, aspect_ratio="21:9",
                                              image_size=settings.skybox_size, model=settings.skybox_model))
    if not out["ok"]:
        return out
    new = IMAGES[out["image_id"]]
    patch = store.apply_patch([{"op": "env", "set": {"sky": {"src": new.url}}}], origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "sky": new.url, "image_id": new.id,
            "provider": new.provider, "model": new.model, "w": new.w, "h": new.h}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    user = websocket.query_params.get("user") or DEFAULT_USER     # from the /tunnel/<user> route's ?user=
    owner = active_scope.split("/", 1)[0]                          # = the live session's owner (§8.2)
    public = _active_public()                                     # visibility lives on the SESSION now
    joined = (user == owner) or public
    if joined:
        clients[websocket] = user                                # joined → gets the world + broadcasts
        await websocket.send_json(_snapshot_msg())
    else:
        # Guest + private session: no world. Track in `_blocked` (like a mid-session bump) so a later
        # go-public re-admits it (§6c `_readmit_clients`), and send the SAME `evicted` signal an eviction
        # does — so the render client shows the center overlay + top banner and auto-resumes on snapshot,
        # instead of a bare top-only `info` that never resumed (entry vs eviction were inconsistent).
        _blocked[websocket] = user
        await websocket.send_json({"type": "evicted",
            "msg": f"this session is private — ask {owner} to make it public."})
    try:
        while True:
            raw = await websocket.receive_text()
            if websocket not in clients:
                continue                                         # a refused/bumped guest's input is ignored
                                                                 # (until a go-public re-admits it to `clients`)
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            mtype = msg.get("type")
            if mtype == "hold":                                  # AR client passed the admission gate → it
                _space_holders.add(websocket)                    # now HOLDS the active space (occupied). Sent
                                                                 # after a successful select + re-sent on ws
                                                                 # reconnect while it's still in AR.
            elif mtype == "release":                             # left AR (exit-vr) → no longer holding
                _space_holders.discard(websocket)
                _unclaim()                                       # frees the space if it was the last holder
            elif mtype == "resync":                              # client cleared a blanked window (space
                await websocket.send_json(_snapshot_msg())       # selection) → re-send the current world so
                                                                 # any patches dropped while blanked are recovered
            elif mtype == "module_event":                        # tier-B shared bus: relay a module's event
                await _broadcast_others(websocket, {"type": "module_event",   # (e.g. a water touch) to the
                    "event": msg.get("event"), "payload": msg.get("payload")})  # OTHER clients (not the sender)
            elif mtype == "presence":                            # relay this client's pose to the others
                pose = msg.get("pose")
                g = _gaze_from_pose(pose)
                if g:
                    if msg.get("anchor"):                        # §7b: keep the plane-relative head anchor so
                        g["anchor"] = msg["anchor"]              # view_relative can solve it against the seed
                    gaze[user] = g                               # remember where this user is looking
                await _broadcast_others(websocket, {"type": "presence", "user": user, "pose": pose})
    except WebSocketDisconnect:
        pass
    finally:
        clients.pop(websocket, None)
        _blocked.pop(websocket, None)                            # drop any bump record for this socket
        _space_holders.discard(websocket)                        # a holder's socket closing = they left
        _unclaim()                                               # → unclaim the space if that was the last one
        if user not in clients.values():                         # last socket for this user gone
            gaze.pop(user, None)
        if joined:
            await _broadcast({"type": "presence_leave", "user": user})   # drop their avatar everywhere


def _gaze_from_pose(pose: dict | None) -> dict | None:
    """Derive {origin, forward} (reference frame) from a presence pose {p, q}. `forward` is the head's
    -Z (look direction) rotated by the quaternion q=[x,y,z,w]. Used to resolve 'the wall I'm looking at'
    and 'in front of me'."""
    if not pose:
        return None
    p, q = pose.get("p"), pose.get("q")
    if not (p and q and len(p) == 3 and len(q) == 4):
        return None
    x, y, z, w = q
    forward = [-2 * (w * y + x * z), 2 * (w * x - y * z), -1 + 2 * (x * x + y * y)]   # head -Z
    right = [1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)]       # head +X
    up = [2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x)]          # head +Y
    return {"origin": [float(p[0]), float(p[1]), float(p[2])],
            "forward": forward, "right": right, "up": up}


def _axes_from_quat(q: list[float]) -> dict:
    """Head axes (reference frame) from a quaternion [x,y,z,w]: forward = -Z, right = +X, up = +Y."""
    x, y, z, w = q
    return {"forward": [-2 * (w * y + x * z), 2 * (w * x - y * z), -1 + 2 * (x * x + y * y)],
            "right": [1 - 2 * (y * y + z * z), 2 * (x * y + w * z), 2 * (x * z - w * y)],
            "up": [2 * (x * y - w * z), 1 - 2 * (x * x + z * z), 2 * (y * z + w * x)]}


def _head_from_anchor(anchor: dict | None) -> dict | None:
    """Resolve a head pose in the SEED frame by solving the streamed plane-relative head anchor (§5.1,
    presenceTick, free mode) against the current seed walls with the shared solver (§7a/b). This is
    non-rigid-consistent — unlike the presence pose, which reaches F_ref through the client's RIGID
    registration T and so drifts from the seed where a client's local map differs (a guest, or far from
    where registration was tightest). Returns the same {origin, forward, right, up} shape as
    `_gaze_from_pose`, or None (no anchor / too few seed walls / degenerate) → caller falls back to the
    presence pose."""
    if not anchor:
        return None
    planes = _seed_planes()
    if sum(1 for p in planes if p["kind"] == "wall") < 2:
        return None
    sol = solve_anchor(anchor, planes)
    if not sol.get("ok"):
        return None
    p = sol["position"]
    out = {"origin": [float(p[0]), float(p[1]), float(p[2])]}
    out.update(_axes_from_quat(sol["quaternion"]))
    return out


def _live_state() -> dict:
    """The canonical **"what's live"** identifiers for the single shared session (docs/specs/agents.md §9.1):
    the active world + its `scope`/`agent`/`owner`, and the `space` it composes against (the fully-qualified
    `<owner>/<name>` ref, or VOID for an outdoor/space-less world). Identifiers only — no world doc — so it's
    the cheap reconciliation seam every peripheral reads: the headset renders the world, the agent server
    binds its brain to `agent` (Step C), any client refreshes its context/prompt. `GET /world` still returns
    the full doc."""
    return {
        "scope": active_scope,
        "agent": agent_of(active_scope),
        "session": active_sid,          # the live SESSION within the scope (docs/specs/agents.md §7.1); the
                                        # agent server keys its transcript on (scope, session)
        # `world` is the display NAME (what a person reads); `world_id` is the permanent identity a
        # client should key state on, so a rename doesn't read as a world switch.
        "world": worlds.name_of(active_scope, active_world) if worlds else active_world,
        "world_id": active_world,
        "owner": active_scope.split("/", 1)[0],
        "public": _active_public(),     # the live session's visibility (§8.2) — the agent server gates on it
        "space": VOID if _no_space() else _space_ref(active_space_owner, active_space),
    }


def _snapshot_msg() -> dict:
    """The snapshot a client receives — the full world doc for rendering, the active world's OWNER (so a
    desktop guest knows whom to spawn next to, Phase 4 §6), and the canonical live-state identifiers under
    `state` so every subscriber reconciles from one broadcast (docs/specs/agents.md §9.1). `world`/`owner` stay
    top-level for the existing renderer; `state.world` is the world *name*, `state` is additive."""
    return {"type": "snapshot", "world": store.doc, "owner": active_scope.split("/", 1)[0],
            "state": _live_state()}


async def _regate_clients() -> None:
    """Bump already-connected `/ws` clients when the live session is private (docs/specs/agents.md §9.4):
    every non-owner guest is moved from `clients` to `_blocked` (kept, so a later go-public can re-admit
    them), taken off the broadcast set, and sent an `evicted` message so the render client blanks to
    passthrough. A public session is a no-op; the owner always stays."""
    if _active_public():
        return
    owner = active_scope.split("/", 1)[0]
    for ws_, u in list(clients.items()):
        if u == owner:
            continue
        clients.pop(ws_, None)
        _blocked[ws_] = u
        _space_holders.discard(ws_)
        try:
            await ws_.send_json({"type": "evicted",
                "msg": f"this session is now private — ask {owner} to make it public."})
        except Exception:  # noqa: BLE001 — a dead socket must not break the sweep
            pass
    # Dropping holders without this left the space CLAIMED by nobody and still marked as committed: the
    # evicted headset's cid is in `_selected_cids` for the epoch, so its re-vote returned
    # `{"selected": False}` and it sat in passthrough until the wearer physically left AR. Freeing it is
    # what makes eviction recoverable — and it is a no-op while the owner is still holding.
    _unclaim()


async def _readmit_clients() -> None:
    """The inverse of `_regate_clients` (§6c): when the live session is public, re-admit every socket we
    blocked — back into `clients` and sent a fresh snapshot so its render client un-blanks and re-renders,
    no page reload needed. A no-op while private."""
    if not _active_public():
        return
    for ws_, u in list(_blocked.items()):
        del _blocked[ws_]
        clients[ws_] = u
        try:
            await ws_.send_json(_snapshot_msg())
        except Exception:  # noqa: BLE001
            clients.pop(ws_, None)


async def _propagate_visibility() -> None:
    """Fan the live session's current visibility out to everyone after a switch or a visibility toggle
    (§6c). Private: bump non-owner guests FIRST, then snapshot the rest (so the bumped don't receive it).
    Public: snapshot current clients, THEN re-admit + snapshot anyone previously blocked. Either way the
    snapshot carries fresh `state`, so the agent server re-gates its CLI/voice clients too."""
    if _active_public():
        await _broadcast(_snapshot_msg())
        await _readmit_clients()
    else:
        await _regate_clients()
        await _broadcast(_snapshot_msg())


async def _broadcast(message: dict, *, skip: "WebSocket | None" = None) -> None:
    # Server-side trace of every world update sent out, into the SAME temp/conjure.log the client writes
    # (interleaved by timestamp). Pair with the client's [ws] recv / [patch] lines to localize a
    # "director said done but nothing changed" bug: no [bcast] ⇒ the mutation never produced a patch
    # (tool no-op/error/guard); [bcast] but no client [ws] recv ⇒ delivery; recv but [patch] DROPPED ⇒
    # the client was blanked (space selection). Presence is excluded (high-frequency).
    _t = message.get("type")
    if _t == "patch":
        _p = message.get("patch") or {}
        _slog("bcast", f"patch rev {_p.get('rev')} ops={len(_p.get('ops') or [])} "
                       f"origin={_p.get('origin')} → {len(clients)} client(s)")
    elif _t == "snapshot":
        _w = message.get("world") or {}
        _slog("bcast", f"snapshot rev {_w.get('rev')} ents={len(_w.get('entities') or [])} → {len(clients)} client(s)")
    dead = []
    for ws_ in list(clients):
        if ws_ is skip:
            continue
        try:
            await ws_.send_json(message)
        except Exception:
            dead.append(ws_)
    for d in dead:
        clients.pop(d, None)


async def _broadcast_others(sender: "WebSocket", message: dict) -> None:
    await _broadcast(message, skip=sender)


class ClientLog(BaseModel):
    tag: Optional[str] = None
    msg: str


class GeometryLog(BaseModel):
    sid: str                           # one AR client/page-load, so a session's lines group
    events: list[dict]                 # [{ev, ct, ...}] — batched, see the client's geoFlush


@app.post("/client_log")
async def client_log(req: ClientLog) -> dict:
    """Append a diagnostic line from the WebXR client to temp/conjure.log (and echo to the console), so
    headset-side logs are captured without remote browser debugging. Gated by settings.debug_log OR
    settings.debug_registration (so registration diagnostics still write when only that flag is on)."""
    _slog(req.tag or "log", req.msg)
    return {"ok": True}


@app.post("/geometry_log")
async def geometry_log(req: GeometryLog) -> dict:
    """Append a BATCH of geometry events to temp/geometry-<date>.jsonl (docs/backlogs/spaces-geometry.md).

    Batched on purpose. The client's `debugLog` does one fetch per line, which is why --debug-jitter had to
    be decoupled from --debug-registration — the measurement was contaminating the measurement. An
    always-on probe cannot afford that, so the client buffers and flushes on a timer; this route is the
    only thing per flush, not per event."""
    for e in req.events[:200]:                            # a runaway client can't flood the day's file
        ev = str(e.pop("ev", "?"))
        ct = e.pop("ct", None)
        _glog(ev, e, sid=req.sid, ct=ct if isinstance(ct, (int, float)) else None)
    return {"ok": True}


# Mount static last so it doesn't shadow the routes above.
app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
