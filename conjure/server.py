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
import contextlib
import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .assets import AssetResolver
from .captioner import build_captioner
from .config import DEFAULT_USER, get_settings, scope_for
from .embeddings import build_embedder
from .library import AssetLibrary
from .llm import build_image_generators, select_generator, vendor_for
from .schema import Patch
from .world import SpaceStore, WorldRepository, WorldStore, world_path

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "client"
LOG_FILE = ROOT / "temp" / "conjure.log"   # client diagnostics (gated by settings.debug_log)
SAMPLE_WORLD = ROOT / "examples" / "sample_world.json"
AGENTS_DIR = ROOT / "agents"
# Scoped, hierarchical world store (docs/persistence-model.md §4/§6): .cache/worlds/<scope>/<name>.json.
WORLDS_DIR = ROOT / ".cache" / "worlds"
# User-owned physical spaces (docs/spaces-and-users-plan.md §5): .cache/spaces/<user>/<name>.json.
SPACES_DIR = ROOT / ".cache" / "spaces"
ASSET_CACHE = ROOT / ".cache" / "assets"
ASSET_CACHE.mkdir(parents=True, exist_ok=True)
LIBRARY_DB = ROOT / ".cache" / "library.db"   # durable asset catalog (docs/asset-library-plan.md)
# The scope new assets/worlds are written under: <user>/agents/<agent> (docs/spaces-and-users-plan.md
# §3). A data seam for now — single user/agent, no enforcement yet; the builder is the only writer.
DEFAULT_SCOPE = scope_for(DEFAULT_USER, "builder")
# scripts/tunnel.sh writes the current cloudflared URL here; /tunnel redirects to it (a short, fixed
# LAN address you can type on the Quest instead of the long random trycloudflare URL each session).
TUNNEL_FILE = ROOT / ".cache" / "tunnel_url"
MEDIA_TYPES = {".glb": "model/gltf-binary", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}

def _has_alpha(im) -> bool:
    """True if an opened PIL image carries real (non-opaque) transparency."""
    if im.mode in ("RGBA", "LA"):
        return im.getchannel("A").getextrema()[0] < 255   # some pixel non-opaque
    return "transparency" in im.info


# World constructor: a per-agent macro of ordinary server operations, run once at world *creation*
# (docs/persistence-model.md §6). Each command maps to the same env/patch effect the director's tools
# produce; the set grows as constructors need more. The builder shows real-room edges by default; the
# future dungeonmaster turns them off — same mechanism, different agent.json.
_WORLD_COMMANDS = {
    "show_edges": lambda a: [{"op": "env", "set": {"room.edgesVisible": bool(a.get("on", True))}}],
    "show_annotations": lambda a: [{"op": "env", "set": {"room.annotations": bool(a.get("on", False))}}],
    "set_sky_color": lambda a: [{"op": "env", "set": {"sky": {"color": a.get("color", "#000000")}}}],
}


def _agent_world_config(scope: str) -> dict:
    """The owning agent's `world` block from agents/<agent>/agent.json (agent = the scope's last
    segment). Missing/unreadable → no hooks."""
    agent = (scope or "").rsplit("/", 1)[-1]
    try:
        return json.loads((AGENTS_DIR / agent / "agent.json").read_text()).get("world") or {}
    except Exception:  # noqa: BLE001
        return {}


def _new_world_store(scope: str) -> WorldStore:
    """A fresh world: the blank starter + the owning agent's on_create constructor (run once)."""
    s = WorldStore.load(SAMPLE_WORLD)
    s.doc.setdefault("environment", {})["public"] = True          # worlds are public by default (§4)
    ops: list[dict] = []
    for cmd in _agent_world_config(scope).get("on_create", []):
        fn = _WORLD_COMMANDS.get(cmd.get("cmd"))
        if fn:
            ops.extend(fn(cmd.get("args") or {}))
    if ops:
        s.apply_patch(ops, origin="constructor")
    return s


def _reset_room_authority(s: WorldStore) -> None:
    """Room authority (the one headset allowed to report geometry) is LIVE-session state, not durable.
    Each client mints a fresh id per page load, so a *persisted* authority from a past session names a
    dead headset — and ingest_room would reject the live headset's captures forever (it can't match the
    stale id). Clear it whenever a world becomes active so the live headset reclaims it on next capture."""
    room = (s.doc.get("environment") or {}).get("room")
    if isinstance(room, dict) and room.get("authorityClientId"):
        room["authorityClientId"] = None


def _migrate_world_dirs(root: Path) -> None:
    """One-time: move worlds from the pre-user layout `<root>/private/<agent>/` to the user-first
    `<root>/<DEFAULT_USER>/agents/<agent>/` (docs/spaces-and-users-plan.md §9). Idempotent — only acts
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


def _boot_world() -> tuple[str, str, WorldStore]:
    """Resume the last-active world for the default scope, else create its `default` (running the
    constructor). One-time courtesy: adopt a step-1 single-world file (.cache/world.json) as `default`."""
    scope = DEFAULT_SCOPE
    active = worlds.get_active(scope)
    if active and worlds.exists(scope, active):
        try:
            s = worlds.load(scope, active)
            _reset_room_authority(s)
            return scope, active, s
        except Exception as exc:  # noqa: BLE001
            print(f"[conjure] active world {active!r} unreadable ({exc}); creating a fresh default")
    legacy = ROOT / ".cache" / "world.json"
    if not worlds.list(scope) and legacy.exists():
        try:
            s = WorldStore.load(legacy)
            print("[conjure] adopted .cache/world.json as the 'default' world")
        except Exception:  # noqa: BLE001
            s = _new_world_store(scope)
    else:
        s = _new_world_store(scope)
    _reset_room_authority(s)
    worlds.save(scope, "default", s)
    worlds.set_active(scope, "default")
    return scope, "default", s


settings = get_settings()  # loads .env
clients: "dict[WebSocket, str]" = {}     # connected render clients → their user (owner or guest)
resolver: AssetResolver | None = (
    AssetResolver(settings.poly_pizza_api_key, ASSET_CACHE) if settings.poly_pizza_api_key else None
)
# Filesystem-mutating, stateful singletons — opened by _init_state() on SERVER STARTUP, never at import,
# so `import conjure.server` (tests, dev, tooling) can't run schema migrations / move world dirs / write
# to the real .cache. They're None until startup; the test fixture sets them directly (startup never
# fires under a plain TestClient). See docs/backlog.md (the import-time-startup hazard).
library: "AssetLibrary | None" = None
worlds: "WorldRepository | None" = None
spaces: "SpaceStore | None" = None
store: "WorldStore | None" = None
active_scope: str = DEFAULT_SCOPE
active_world: str = "default"
active_space: str = "home"          # the space the active world is composed against
# The embedder is None unless the optional torch/transformers are installed — then vector write-through
# is simply skipped and the catalog runs on FTS/exact only. Lazy: no model loads until first embed.
embedder = build_embedder(settings)


def _init_state() -> None:
    """Open the catalog (runs schema migrations), migrate the world layout, and boot the active world.
    All filesystem-mutating — so it runs on server startup, not at import. Idempotent enough to re-run.
    Back up library.db to protect curation: a lost catalog is NOT rebuilt from the cache files."""
    global library, worlds, spaces, store, active_scope, active_world, active_space, _geo_selected
    _geo_selected = False                            # a fresh session re-selects on first geolocation report
    library = AssetLibrary(LIBRARY_DB)
    worlds = WorldRepository(WORLDS_DIR)
    spaces = SpaceStore(SPACES_DIR)
    _migrate_world_dirs(WORLDS_DIR)                  # pre-user layout → <user>/agents/<agent> (one-time)
    active_scope, active_world, raw = _boot_world()
    active_space, store = _activate(active_scope, active_world, raw)   # compose (+ migrate geometry → space)

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


async def _caption_one(asset: dict) -> None:
    """Caption one asset and store it as the label. Best-effort (never raises into the pass)."""
    if captioner is None:
        return
    fn = asset.get("filename")
    if not (fn and (ASSET_CACHE / fn).exists()):
        return
    mime = MEDIA_TYPES.get(Path(fn).suffix.lower(), "image/png")
    try:
        text = await captioner.caption((ASSET_CACHE / fn).read_bytes(), mime=mime,
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

# Owner-only writes (co-location-plan.md §4): only the ACTIVE world's owner may change the shared world
# or flip which world is active. Enforced server-side, never via the prompt — the MCP client and the
# headset attach an `X-Conjure-User` header; a non-owner hitting these routes gets 403. A *missing*
# header (the direct dev CLI) is treated as the owner (interim convenience). Reads, scoped catalog ops,
# procurement, and a guest's own scoped worlds are NOT gated.
_OWNER_ONLY_PATHS = {
    "/reset", "/patch", "/room", "/texture_surface", "/style_surface", "/place_asset",
    "/place_cached_asset", "/place_image", "/set_skybox", "/set_grounded_skybox",
    "/edit_image", "/outpaint_image", "/skybox_from_image", "/worlds/new", "/worlds/switch",
}


@app.middleware("http")
async def _owner_only_writes(request, call_next):
    if request.url.path in _OWNER_ONLY_PATHS:
        who = request.headers.get("X-Conjure-User")
        owner = active_scope.split("/", 1)[0]
        if who and who != owner:
            return JSONResponse(status_code=403, content={
                "ok": False, "error": f"This world belongs to {owner}; only the owner can change it."})
    return await call_next(request)

# Durability: a background task writes the active world to its file whenever its rev advances. Polling
# debounces naturally — a multi-patch turn or a room-capture flurry coalesces into one write — and it
# touches no apply_patch call site. ~1 s of in-flight changes is the only crash-loss window.
_AUTOSAVE_INTERVAL = 1.0
_autosave_task: asyncio.Task | None = None
_geo_selected = False               # nearest-space selection runs ONCE per session (first geolocation report)


def _save_active() -> None:
    """Persist the live composed doc by SPLITTING it: real-surface geometry + boundary → the active
    space; placed objects + display prefs + per-surface style overrides → the active world doc."""
    if store is None or worlds is None or spaces is None:
        return
    user = active_scope.split("/", 1)[0]
    space = spaces.load(user, active_space) if spaces.exists(user, active_space) else \
        {"owner": user, "name": active_space, "public": True, "geolocation": None,
         "surfaces": [], "boundary": None}
    fresh = _space_from_world_doc(user, active_space, store.doc)       # geometry + boundary, default-materialed
    space["surfaces"], space["boundary"] = fresh["surfaces"], fresh["boundary"]
    space["last_scope"], space["last_world"] = active_scope, active_world   # for nearest-space selection
    spaces.save(user, active_space, space)
    spaces.set_active(user, active_space)                             # keep the user's current physical space current
    world_doc = _decompose(store.doc, space)                          # overrides diffed vs the (default) base
    world_doc.setdefault("environment", {})["space"] = active_space
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




def _normalize(record, pos: list[float], target_m: float) -> tuple[list[float], list[float]]:
    """Scale a model so its largest dimension is `target_m` meters and its base sits at pos.y,
    centered at pos.x/z. Returns (position, scale); native scale if the bounding box is unknown.
    """
    if not record.bbox_min or not record.bbox_max:
        return pos, [1.0, 1.0, 1.0]
    mn, mx = record.bbox_min, record.bbox_max
    size = [mx[i] - mn[i] for i in range(3)]
    max_dim = max(size) or 1.0
    s = target_m / max_dim
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


def _model_entity_op(eid: str, model_id: str, *, title, licence, attribution, creator, tris, source,
                     bbox_min, bbox_max, pos, size_m) -> dict:
    """Build the `add` op for a glTF model entity, auto-scaled to sit on the floor and carrying its
    license/attribution. Shared by /place_asset (web) and /place_cached_asset (library reuse)."""
    rec = SimpleNamespace(bbox_min=bbox_min, bbox_max=bbox_max)
    model_pos, model_scale = _normalize(rec, pos, size_m or TARGET_SIZE_M)
    return {"op": "add", "entity": {
        "id": eid,
        "transform": {"position": model_pos, "scale": model_scale},
        "components": {"gltf-model": f"/assets/{model_id}"},
        "meta": {"title": title, "license": licence, "attribution": attribution, "creator": creator,
                 "source": source, "tris": tris, "generated": False},
    }}


def _store_image(result, *, prompt: str, op: str) -> ImageRecord:
    """Write a procured image to the content store and register an ImageRecord; return it."""
    ext = ".png" if "png" in result.mime_type else (".webp" if "webp" in result.mime_type else ".jpg")
    image_id = f"{hashlib.sha256(result.data).hexdigest()[:16]}{ext}"
    (ASSET_CACHE / image_id).write_bytes(result.data)
    w, h, transparent = _img_meta(result.data)
    rec = ImageRecord(id=image_id, url=f"/assets/{image_id}", w=w, h=h, provider=result.provider,
                      model=result.model, prompt=prompt, op=op, transparent=transparent)
    IMAGES[image_id] = rec
    # Write through to the durable catalog (keyed on the prompt = intent), so reuse can find it later
    # and its provenance survives a restart.
    library.upsert(image_id, kind=_kind_for_op(op), scope=DEFAULT_SCOPE, source=f"cache://{image_id}",
                   filename=image_id, label=prompt, prompt=prompt,
                   params={"op": op, "transparent": transparent},
                   provider=result.provider, model=result.model, width=w, height=h,
                   transparent=1 if transparent else 0)
    _embed_asset(image_id, image=result.data)   # embed the pixels into the shared space (best-effort)
    return rec


def _get_image(image_id: str):
    """Return (record, bytes, error) for a procured image id. Rebuilds a minimal record from disk if
    the in-memory entry was lost to a restart, so post-restart edits still work."""
    if not image_id or "/" in image_id or ".." in image_id:
        return None, None, f"bad image id {image_id!r}"
    path = ASSET_CACHE / image_id
    if not path.exists():
        return None, None, f"no image {image_id!r}"
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
        return {"ok": False, "error": "no image generator configured (set GOOGLE_API_KEY and/or OPENAI_API_KEY)"}
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
    cm = int((CLIENT_DIR / "conjure-client.js").stat().st_mtime)
    sm = int((CLIENT_DIR / "room-snap.js").stat().st_mtime)
    gm = int((CLIENT_DIR / "grounded-skybox.js").stat().st_mtime)
    v = max(cm, sm, gm)                       # badge reflects the newest of the scripts
    build = datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    html = html.replace("/static/conjure-client.js", f"/static/conjure-client.js?v={cm}")
    html = html.replace("/static/room-snap.js", f"/static/room-snap.js?v={sm}")
    html = html.replace("/static/grounded-skybox.js", f"/static/grounded-skybox.js?v={gm}")
    html = html.replace("__CLIENT_VERSION__", f"{build} (v{v})")
    # Tell the client whether debug logging is on, so it doesn't POST diagnostics when it's off.
    flag = "true" if settings.debug_log else "false"
    html = html.replace("</head>", f"  <script>window.CONJURE_DEBUG_LOG={flag};</script>\n  </head>")
    return HTMLResponse(html, headers=_NO_STORE)


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


@app.get("/static/grounded-skybox.js")
async def grounded_skybox_js() -> FileResponse:
    # Explicit no-store route for the grounded-skybox module (loaded before conjure-client.js).
    return FileResponse(CLIENT_DIR / "grounded-skybox.js", media_type="application/javascript", headers=_NO_STORE)


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
    user = active_scope.split("/", 1)[0]
    space = spaces.load(user, active_space) if (spaces and spaces.exists(user, active_space)) \
        else {"surfaces": [], "boundary": None}
    store = WorldStore(_compose(raw.doc, space))   # keep the physical room; clear only the world's content
    _reset_room_authority(store)
    _surface_absence.clear()                 # fresh room session
    _save_active()                            # persist the reset so it survives a restart
    await _broadcast(_snapshot_msg())
    return {"ok": True, "rev": store.doc["rev"]}


# ---- world management (scoped; scope is injected server-side, never an LLM argument) ----------------
async def _switch_to(scope: str, name: str, store_override: WorldStore | None = None) -> dict:
    """Make (scope, name) the live world: persist the outgoing one, set the incoming as `store`, record
    it as active, and broadcast a snapshot so the headset reloads. `store_override` installs a freshly
    built world (new_world) instead of loading from disk."""
    global store, active_scope, active_world, active_space
    name = world_path(name)                   # canonical path, so `active` matches list() + the pointer
    _save_active()                            # split + persist the outgoing world (+ its space)
    raw = store_override if store_override is not None else worlds.load(scope, name)
    active_scope, active_world = scope, name
    active_space, store = _activate(scope, name, raw)   # compose the incoming world against its space
    worlds.set_active(scope, name)
    _surface_absence.clear()                  # the room re-syncs into the newly active world
    await _broadcast(_snapshot_msg())
    return {"ok": True, "world": name, "rev": store.doc["rev"]}


class WorldRef(BaseModel):
    name: str
    scope: str = DEFAULT_SCOPE


class ScopeRef(BaseModel):
    scope: str = DEFAULT_SCOPE


class GeoReport(BaseModel):
    lat: float
    lon: float
    accuracy: Optional[float] = None
    user: Optional[str] = None       # who's reporting; only the space OWNER's location drives selection


_NEAR_M = 150.0                     # within this distance of a space's geolocation ⇒ "the same space"


def _unique_space_name(user: str) -> str:
    existing = set(spaces.list(user))
    i = len(existing) + 1
    while f"space-{i}" in existing:
        i += 1
    return f"space-{i}"


@app.post("/geolocation")
async def report_geolocation(req: GeoReport) -> dict:
    """A connected client reports its coarse location (the headset's `navigator.geolocation`). Selection
    runs ONCE per session (first report) — never mid-session, so GPS jitter can't yank you between worlds
    (docs/spaces-and-users-plan.md §7). Order: (1) active space un-located → you're in it, stamp it;
    (2) within ~150 m of a *different* space → auto-switch to its last-active world; (3) somewhere new →
    create a fresh space + default world here. Geolocation is the coarse prefilter; the client's
    registration vote is the fine 'are you actually in this space?' confirm."""
    global _geo_selected
    if spaces is None or worlds is None:
        return {"ok": False, "error": "no space store"}
    user = active_scope.split("/", 1)[0]
    if req.user and req.user != user:                 # a guest's location must not re-select the space
        return {"ok": True, "selected": False}
    geo = {"lat": req.lat, "lon": req.lon, "accuracy": req.accuracy}
    if _geo_selected:
        return {"ok": True, "selected": False}        # already chose this session
    _geo_selected = True

    # (1) the active space has no location yet → you're physically in it
    if spaces.exists(user, active_space):
        sp = spaces.load(user, active_space)
        if not sp.get("geolocation"):
            sp["geolocation"] = geo
            spaces.save(user, active_space, sp)
            return {"ok": True, "stamped": active_space}

    near = _nearest_space(user, req.lat, req.lon)
    # (2) near an existing space
    if near and near[1] <= _NEAR_M:
        if near[0] == active_space:
            return {"ok": True, "space": active_space}     # already here
        sp = spaces.load(user, near[0])
        scope, w = sp.get("last_scope", active_scope), sp.get("last_world")
        if w and worlds.exists(scope, w):
            out = await _switch_to(scope, w)               # auto-switch into that space's last world
            out["space"] = near[0]
            return out
    # (3) somewhere new → create a fresh space + default world and switch into it
    new_space = _unique_space_name(user)
    spaces.save(user, new_space, {"owner": user, "name": new_space, "public": True,
                                  "geolocation": geo, "surfaces": [], "boundary": None})
    spaces.set_active(user, new_space)
    fresh = _new_world_store(active_scope)
    fresh.doc.setdefault("environment", {})["space"] = new_space
    out = await _switch_to(active_scope, new_space, store_override=fresh)   # world named after the space
    out["created_space"] = new_space
    return out


@app.post("/worlds/list")
async def worlds_list(req: ScopeRef) -> dict:
    return {"ok": True, "worlds": worlds.list(req.scope), "active": active_world if req.scope == active_scope else worlds.get_active(req.scope)}


@app.post("/worlds/new")
async def worlds_new(req: WorldRef) -> dict:
    """Create a new (optionally nested) world from the agent's constructor and switch to it. Refuses to
    clobber an existing world of the same canonical name."""
    try:
        if worlds.exists(req.scope, req.name):
            return {"ok": False, "error": f"world {req.name!r} already exists — switch to it instead"}
        return await _switch_to(req.scope, req.name, store_override=_new_world_store(req.scope))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/worlds/switch")
async def worlds_switch(req: WorldRef) -> dict:
    try:
        if not worlds.exists(req.scope, req.name):
            return {"ok": False, "error": f"no world {req.name!r} (create it with new_world)"}
        return await _switch_to(req.scope, req.name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/worlds/delete")
async def worlds_delete(req: WorldRef) -> dict:
    try:
        if req.scope == active_scope and world_path(req.name) == active_world:
            return {"ok": False, "error": "can't delete the active world — switch away first"}
        return {"ok": worlds.delete(req.scope, req.name), "world": req.name}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


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
    await _broadcast({"type": "patch", "patch": applied})
    return applied


# --- Room model: the client→server reverse channel (a headset reports its real room) ------------
# Captured surfaces become `real`-tagged stylable entities; `environment.room` holds the boundary,
# active flag, and the single room **authority** (only that headset may report room geometry).
# See docs/room-model.md.

class RoomSurface(BaseModel):
    id: str                                   # stable id from the headset, e.g. "real_wall_3"
    semantic: str = "surface"                 # wall | floor | ceiling | table | …
    position: list[float]
    rotation: Optional[list[float]] = None
    polygon: Optional[list[list[float]]] = None   # 2D outline in the surface plane
    extent: Optional[list[float]] = None          # [w, h]
    holes: Optional[list[dict]] = None            # wall openings (door/window) {x,y,w,h} in wall-local 2D
    mesh_segment: Optional[str] = None            # segment id when backed by the refined mesh
    debug: Optional[dict] = None                  # raw pose/label for diagnosis (stored in meta)


class RoomUpdate(BaseModel):
    client_id: str                            # which headset is reporting
    surfaces: list[RoomSurface] = []
    boundary: Optional[dict] = None           # {floorPolygon: [[x,z]…], height: float}
    replace: bool = True                      # replace the whole real-surface set vs merge


def _surface_entity(s: RoomSurface) -> dict:
    """A fresh `real` surface entity. Visibility/style are left to the renderer default
    (environment.room.defaultSurfaceVisible) + later director edits, so re-capture never clobbers
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


# ---- space ↔ world composition (docs/spaces-and-users-plan.md §5/§6) -------------------------------
# A SPACE owns the real-surface geometry (+ a base material) and the boundary, shared across a user's
# worlds. A WORLD owns placed objects, display prefs, and per-surface style OVERRIDES (material that
# differs from the space's base), keyed by surface id in environment.room.surfaceStyles. The live
# store.doc stays the COMPOSED shape below (so client/patch/director are unchanged); only persistence
# splits — _compose on load, _decompose on save.

def _compose(world_doc: dict, space: dict) -> dict:
    """Live doc: the world's placed entities + prefs, merged with the space's real-surface geometry —
    each surface's material = the space's base, overridden by world.environment.room.surfaceStyles[id].
    Boundary comes from the space. The surfaceStyles map and `space` ref are persistence-only (dropped)."""
    doc = copy.deepcopy(world_doc)
    env = doc.setdefault("environment", {})
    env.pop("space", None)
    doc.pop("space", None)
    room = env.setdefault("room", {})
    styles = room.pop("surfaceStyles", {}) or {}
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
        room["boundary"] = space["boundary"]
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
    room = doc.setdefault("environment", {}).setdefault("room", {})
    room.pop("boundary", None)
    if overrides:
        room["surfaceStyles"] = overrides
    else:
        room.pop("surfaceStyles", None)
    return doc


def _space_from_world_doc(user: str, name: str, doc: dict) -> dict:
    """Build a space from a (legacy, geometry-embedded) world doc: real surfaces become the space's
    geometry with per-semantic DEFAULT materials (styling stays per-world as surfaceStyles), plus the
    boundary. The space is user-owned and public by default (spaces-and-users-plan.md §5)."""
    surfaces = []
    for e in doc.get("entities", []):
        if e.get("meta", {}).get("real"):
            s = copy.deepcopy(e)
            s.setdefault("components", {})["material"] = _default_surface_material(
                s.get("meta", {}).get("semantic", "surface"))
            surfaces.append(s)
    boundary = (doc.get("environment", {}).get("room", {}) or {}).get("boundary")
    return {"owner": user, "name": name, "public": True, "geolocation": None,
            "surfaces": surfaces, "boundary": boundary}


def _activate(scope: str, name: str, world: WorldStore) -> tuple[str, WorldStore]:
    """Resolve the world's space and compose the live doc. On first load of a LEGACY geometry-embedded
    world, migrate it: extract its surfaces into a shared user space (reusing the user's existing space —
    same physical room), record its styling as surfaceStyles, strip the geometry, and persist the
    migrated world. Returns (space_name, composed store) with room authority reset (session state)."""
    user = scope.split("/", 1)[0]
    doc = world.doc
    space_name = (doc.get("environment", {}) or {}).get("space")
    if space_name and spaces.exists(user, space_name):
        space, world_doc = spaces.load(user, space_name), doc          # already migrated
    else:
        space_name = space_name or spaces.get_active(user) or "home"
        space = spaces.load(user, space_name) if spaces.exists(user, space_name) else None
        if space and space.get("surfaces"):
            pass                                                       # reuse the shared physical room
        else:                                                          # no shared geometry yet → seed it
            space = _space_from_world_doc(user, space_name, doc)
            spaces.save(user, space_name, space)
            spaces.set_active(user, space_name)
        world_doc = _decompose(doc, space)                             # legacy styling → overrides; strip geometry
        world_doc.setdefault("environment", {})["space"] = space_name
        worlds.save(scope, name, WorldStore(world_doc))                # persist the geometry-free world
    composed = WorldStore(_compose(world_doc, space))
    _reset_room_authority(composed)
    return space_name, composed


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    r = 6371000.0
    lat1, lat2 = math.radians(a[0]), math.radians(b[0])
    dlat, dlon = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _nearest_space(user: str, lat: float, lon: float) -> tuple[str, float] | None:
    """The user's space nearest (lat, lon) and its distance in metres, or None. Skips spaces that have
    no stored geolocation yet. A coarse prefilter (geolocation is ~hundreds of feet) — the registration
    vote is the fine 'are you actually in this space?' check (docs/spaces-and-users-plan.md §7)."""
    best = None
    for name in spaces.list(user):
        g = spaces.load(user, name).get("geolocation")
        if not g:
            continue
        d = _haversine_m((lat, lon), (g["lat"], g["lon"]))
        if best is None or d < best[1]:
            best = (name, d)
    return best


# A capture on resume-from-idle (or a momentary tracking blip) is often SPARSE — plane/mesh detection
# re-populates gradually. With replace=True, removing a surface a single such capture missed deletes a
# valid wall AND resets its director styling (the re-add path uses default material → invisible). So
# debounce removals: only prune a surface after it's been absent from this many CONSECUTIVE captures.
_REMOVE_AFTER_ABSENT = 3
_surface_absence: dict[str, int] = {}


@app.post("/room")
async def ingest_room(req: RoomUpdate) -> dict:
    """Ingest captured room geometry from the room **authority** headset. Existing surfaces are
    *updated* in place (preserving director style); new ones added; with replace=True, stale ones are
    removed only after being absent from several consecutive captures (so a sparse resume capture
    doesn't wipe valid surfaces). Sets environment.room (boundary, active, authority)."""
    room = store.doc["environment"].get("room", {})
    authority = room.get("authorityClientId")
    if authority and authority != req.client_id:
        return {"ok": False, "error": f"another headset ({authority}) is the room authority"}

    existing = {e["id"]: e for e in store.doc["entities"] if e.get("meta", {}).get("real")}
    new_ids = {s.id for s in req.surfaces}
    ops: list[dict] = []

    if req.replace:
        for eid in existing:
            if eid in new_ids:
                _surface_absence.pop(eid, None)               # seen → reset its absence streak
            else:
                n = _surface_absence.get(eid, 0) + 1
                if n >= _REMOVE_AFTER_ABSENT:                 # gone for real → prune
                    ops.append({"op": "remove", "id": eid})
                    _surface_absence.pop(eid, None)
                else:
                    _surface_absence[eid] = n                 # transient drop → keep it this round

    for s in req.surfaces:
        if s.id in existing:  # update geometry/pose in place — keep the entity's material (style)
            up: dict = {"transform.position": s.position, "meta.semantic": s.semantic}
            if s.rotation is not None:
                up["transform.rotation"] = s.rotation
            if s.polygon is not None:
                up["components.surface.polygon"] = s.polygon
            if s.extent is not None:
                up["components.surface.extent"] = s.extent
            if s.holes is not None:
                up["components.surface.holes"] = s.holes
            if s.mesh_segment is not None:
                up["meta.meshSegment"] = s.mesh_segment
            ops.append({"op": "update", "id": s.id, "set": up})
        else:
            ops.append({"op": "add", "entity": _surface_entity(s)})

    env_set: dict = {"room.active": True, "room.authorityClientId": req.client_id}
    if req.boundary is not None:
        env_set["room.boundary"] = req.boundary
    if "defaultSurfaceVisible" not in room:
        env_set["room.defaultSurfaceVisible"] = False  # default: invisible references (AR-style)
    ops.append({"op": "env", "set": env_set})

    patch = store.apply_patch(ops, origin="room")
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "surfaces": len(req.surfaces), "authority": req.client_id}


@app.post("/room/realign")
async def realign_room() -> dict:
    """Ask connected headsets to re-capture the room at the current tracking origin (restores
    alignment after a recenter/reload). No-op for clients not in an AR session."""
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
    return {"ok": True, "count": len(targets), "image_id": rec.id}


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
    library.upsert(model_id, kind="model", scope=DEFAULT_SCOPE, source=f"cache://{model_id}",
                   filename=model_id, label=record.title, query=req.query, licence=record.licence,
                   attribution=record.attribution, creator=record.creator,
                   attributes={"tris": record.tris, "bbox_min": record.bbox_min,
                               "bbox_max": record.bbox_max})
    library.touch(model_id)
    # Models are NOT vector-embedded — found by FTS/exact on their title (see _VISUAL_KINDS).

    # 3. Swap the placeholder for the real glTF model (auto-scaled to sit on the floor),
    #    carrying license + attribution.
    swap = [
        {"op": "remove", "id": eid},
        _model_entity_op(eid, model_id, title=record.title, licence=record.licence,
                         attribution=record.attribution, creator=record.creator, tris=record.tris,
                         source="poly.pizza", bbox_min=record.bbox_min, bbox_max=record.bbox_max,
                         pos=pos, size_m=req.size_m),
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


# --- asset library: explicit, director-driven reuse over the catalog (docs/asset-library-plan.md §7).
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


@app.post("/place_cached_asset")
async def place_cached_asset(req: PlaceCachedAssetRequest) -> dict:
    """Place a MODEL already in the library by id — the reuse counterpart to place_asset (no web
    fetch). Images reuse place_image; skyboxes reuse set_skybox/set_grounded_skybox."""
    rec = library.get(req.id)
    if rec is None:
        return {"ok": False, "error": f"no asset {req.id!r} in the library"}
    if rec["kind"] != "model":
        return {"ok": False, "error": f"{req.id!r} is a {rec['kind']}, not a model — "
                "use place_image (images) or set_skybox (skyboxes)"}
    if not (ASSET_CACHE / req.id).exists():
        return {"ok": False, "error": f"bytes for {req.id!r} are missing from the cache"}
    attrs = json.loads(rec["attributes"] or "{}")
    eid = req.name or f"ent_asset_{uuid4().hex[:6]}"
    pos = req.position or [0.0, 0.0, -3.0]
    op = _model_entity_op(eid, req.id, title=rec["label"], licence=rec["licence"],
                          attribution=rec["attribution"], creator=rec["creator"],
                          tris=attrs.get("tris"), source="library", bbox_min=attrs.get("bbox_min"),
                          bbox_max=attrs.get("bbox_max"), pos=pos, size_m=req.size_m)
    await _broadcast({"type": "patch", "patch": store.apply_patch([op], origin="asset")})
    library.touch(req.id)
    return {"ok": True, "id": eid, "image_id": req.id, "title": rec["label"]}


# --- catalog maintenance: scoped CRUD over the asset library (docs/asset-library-plan.md). The
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
    default_for: Optional[str] = None   # pin an alias ("dog" → this asset)
    reject_for: Optional[str] = None    # exclude this asset from a query's matches


@app.post("/update_asset")
async def update_asset(req: UpdateAssetRequest) -> dict:
    """The single catalog mutator: set fields / kind / alias / reject on an asset (scope-checked).
    Keeps FTS + vector-kind + aliases consistent (subsumes the old correct_asset/annotate_asset)."""
    ok, err = library.update(req.id, scope=req.scope, label=req.label, query=req.query, tags=req.tags,
                             notes=req.notes, kind=req.kind, rating=req.rating, favorite=req.favorite,
                             default_for=req.default_for, reject_for=req.reject_for)
    return {"ok": True, "id": req.id} if ok else {"ok": False, "error": err}


class DeleteAssetRequest(BaseModel):
    id: str
    scope: str = DEFAULT_SCOPE


@app.post("/delete_asset")
async def delete_asset(req: DeleteAssetRequest) -> dict:
    """Remove an asset from the catalog (row + aliases/relations/vector; bytes kept). Scope-checked."""
    ok, err = library.delete(req.id, scope=req.scope)
    return {"ok": True, "id": req.id} if ok else {"ok": False, "error": err}


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
                 meta: dict | None = None, rotation: list[float] | None = None) -> dict:
    transform: dict = {"position": pos}
    if rotation is not None:
        transform["rotation"] = rotation
    return {
        "op": "add",
        "entity": {
            "id": eid,
            "transform": transform,
            "components": {
                "geometry": {"primitive": "plane", "width": width, "height": height},
                "material": material,
            },
            **({"meta": meta} if meta else {}),
        },
    }


def _plane_dims(rec: ImageRecord, size: float) -> tuple[float, float]:
    """Fit the image's longest side to `size` meters, preserving its aspect."""
    w, h = rec.w or 1, rec.h or 1
    if w >= h:
        return size, round(size * h / w, 3)
    return round(size * w / h, 3), size


def _fit_dims(rec: ImageRecord, extent: list[float]) -> tuple[float, float]:
    """Fit the image (preserving aspect) *inside* a surface's [w, h] frame — so a picture hung on a
    wall-art surface fills its frame without stretching or overflowing."""
    ew, eh = float(extent[0]), float(extent[1])
    aspect = (rec.w / rec.h) if (rec.w and rec.h) else 1.0
    if ew / eh > aspect:                       # frame is wider than the image ⇒ height-limited
        return round(aspect * eh, 3), round(eh, 3)
    return round(ew, 3), round(ew / aspect, 3)  # width-limited


def _forward(rotation: list[float]) -> list[float]:
    """World-space front (+Z) of an <a-plane> at A-Frame euler `rotation` (degrees, YXZ order) — the
    direction the texture faces. Used to offset a hung picture a hair off its surface (no z-fight)."""
    x, y = math.radians(rotation[0]), math.radians(rotation[1])
    return [math.cos(x) * math.sin(y), -math.sin(x), math.cos(x) * math.cos(y)]


class PlaceImageRequest(BaseModel):
    image_id: str
    position: Optional[list[float]] = None
    size_m: Optional[float] = None
    name: Optional[str] = None
    on_surface: Optional[str] = None   # hang on a real surface (id/label/number) — align + fit to it


@app.post("/place_image")
async def place_image(req: PlaceImageRequest) -> dict:
    """Hang a previously-procured image (by id) as a textured plane facing the user. If `name` is an
    existing entity, swap its image in place (keeping position)."""
    rec, _, err = _get_image(req.image_id)
    if err:
        return {"ok": False, "error": err}
    pos = req.position or [0.0, 1.5, -3.0]  # eye height, on the wall in front
    width, height = _plane_dims(rec, req.size_m or 1.0)
    rotation = None
    if req.on_surface:  # hang on a real surface: adopt its orientation, fit its frame, sit just in front
        surfaces = _room_targets(req.on_surface)
        if not surfaces:
            return {"ok": False, "error": f"no room surface matches {req.on_surface!r}"}
        surf = surfaces[0]
        rotation = surf.get("transform", {}).get("rotation") or [0.0, 0.0, 0.0]
        spos = surf.get("transform", {}).get("position") or pos
        extent = surf.get("components", {}).get("surface", {}).get("extent")
        if extent:
            width, height = _fit_dims(rec, extent)
        f = _forward(rotation)
        pos = [spos[i] + 0.02 * f[i] for i in range(3)]   # 2 cm toward the viewer ⇒ no z-fight
    eid = req.name or f"ent_image_{uuid4().hex[:6]}"
    meta = {"generated": True, "provider": rec.provider, "model": rec.model,
            "prompt": rec.prompt, "image_id": rec.id}
    # A transparent (alpha) image must render with transparency on, or three.js shows it opaque.
    material = {"src": rec.url, "shader": "flat", "side": "double", "transparent": rec.transparent}

    existing = any(e["id"] == eid for e in store.doc["entities"])
    if existing:  # swap in place
        sets = {
            "components.material.src": rec.url, "components.material.transparent": rec.transparent,
            "components.geometry.width": width, "components.geometry.height": height,
            "meta.image_id": rec.id, "meta.prompt": rec.prompt,
            "meta.provider": rec.provider, "meta.model": rec.model}
        if req.on_surface:  # re-hanging on a surface ⇒ also re-align/reposition
            sets["transform.position"] = pos
            sets["transform.rotation"] = rotation
        ops = [{"op": "update", "id": eid, "set": sets}]
    else:
        ops = [_image_plane(eid, pos, width, height, material, meta, rotation)]
    await _broadcast({"type": "patch", "patch": store.apply_patch(ops, origin="image")})
    return {"ok": True, "id": eid, "image_id": rec.id}


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
    return {"ok": True, "sky": rec.url, "image_id": rec.id}


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
    return {"ok": True, "sky": rec.url, "image_id": rec.id}


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
    owner = active_scope.split("/", 1)[0]
    public = bool((store.doc.get("environment", {}) or {}).get("public", True))   # worlds default public
    joined = (user == owner) or public
    if joined:
        clients[websocket] = user                                # joined → gets the world + broadcasts
        await websocket.send_json(_snapshot_msg())
    else:                                                        # guest + private world → no world, info msg
        await websocket.send_json({"type": "info", "level": "info",
            "msg": f"'{active_world}' is private — ask {owner} to make it public."})
    try:
        while True:
            raw = await websocket.receive_text()
            if not joined:
                continue                                         # a refused guest's input is ignored
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if msg.get("type") == "presence":                    # relay this client's pose to the others
                await _broadcast_others(websocket, {"type": "presence", "user": user, "pose": msg.get("pose")})
    except WebSocketDisconnect:
        pass
    finally:
        clients.pop(websocket, None)
        if joined:
            await _broadcast({"type": "presence_leave", "user": user})   # drop their avatar everywhere


def _snapshot_msg() -> dict:
    """The snapshot a client receives — the world plus the active world's OWNER, so a desktop guest
    knows whom to spawn next to (Phase 4 §6)."""
    return {"type": "snapshot", "world": store.doc, "owner": active_scope.split("/", 1)[0]}


async def _broadcast(message: dict, *, skip: "WebSocket | None" = None) -> None:
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


@app.post("/client_log")
async def client_log(req: ClientLog) -> dict:
    """Append a diagnostic line from the WebXR client to temp/conjure.log (and echo to the console), so
    headset-side logs are captured without remote browser debugging. Gated by settings.debug_log
    (CONJURE_DEBUG_LOG=0 disables it — then nothing is written and the file isn't created)."""
    if not settings.debug_log:
        return {"ok": True}
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} [{req.tag or 'log'}] {req.msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    return {"ok": True}


# Mount static last so it doesn't shadow the routes above.
app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
