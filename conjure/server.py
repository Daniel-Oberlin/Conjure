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
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .assets import AssetResolver
from .config import get_settings
from .embeddings import build_embedder
from .library import AssetLibrary
from .llm import build_image_generators, select_generator, vendor_for
from .schema import Patch
from .world import WorldStore

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "client"
LOG_FILE = ROOT / "temp" / "conjure.log"   # client diagnostics (gated by settings.debug_log)
SAMPLE_WORLD = ROOT / "examples" / "sample_world.json"
ASSET_CACHE = ROOT / ".cache" / "assets"
ASSET_CACHE.mkdir(parents=True, exist_ok=True)
LIBRARY_DB = ROOT / ".cache" / "library.db"   # durable asset catalog (docs/asset-library-plan.md)
# The agent namespace assets are written under (docs/persistence-model.md). A data seam for now —
# single agent, no enforcement yet; the builder is the only writer.
DEFAULT_SCOPE = "private/builder"
# scripts/tunnel.sh writes the current cloudflared URL here; /tunnel redirects to it (a short, fixed
# LAN address you can type on the Quest instead of the long random trycloudflare URL each session).
TUNNEL_FILE = ROOT / ".cache" / "tunnel_url"
MEDIA_TYPES = {".glb": "model/gltf-binary", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}

settings = get_settings()  # loads .env
store = WorldStore.load(SAMPLE_WORLD)
clients: set[WebSocket] = set()
resolver: AssetResolver | None = (
    AssetResolver(settings.poly_pizza_api_key, ASSET_CACHE) if settings.poly_pizza_api_key else None
)
# Durable catalog of every procured asset. On first run, seed it from whatever's already on disk so
# pre-existing cache files become reusable (idempotent; only scans when the catalog is empty). The
# backfill is best-effort — a catalog hiccup must never stop the world server from booting.
library = AssetLibrary(LIBRARY_DB)
try:
    if library.count() == 0:
        library.backfill(ASSET_CACHE, store.doc, scope=DEFAULT_SCOPE)
except Exception as exc:  # noqa: BLE001
    print(f"[conjure] asset-library backfill skipped: {exc}")
# The embedder is None unless the optional torch/transformers are installed — then vector write-through
# is simply skipped and the catalog runs on FTS/exact only. Lazy: no model loads until first embed.
embedder = build_embedder(settings)

# Embedding is an *enrichment*, not part of procurement, so in production it runs OFF the request path:
# the asset is already procured/returned, and its vector lands a beat later (exact/FTS still match it
# immediately; only semantic search for that one asset is briefly eventual). A lock serializes model
# access (one forward pass at a time); a thread keeps the blocking torch call off the event loop.
# Tests flip _EMBED_BACKGROUND off for deterministic, inline write-through.
_EMBED_BACKGROUND = True
_embed_lock = asyncio.Lock()
_embed_tasks: set[asyncio.Task] = set()   # strong refs so fire-and-forget tasks aren't GC'd mid-flight


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
    """Embed a single catalog row (used by reindex): images from their bytes, models from their title
    text. Reads bytes lazily so a batch doesn't hold every image in memory at once."""
    kind, fn = asset.get("kind"), asset.get("filename")
    if kind == "model":
        text = asset.get("label") or asset.get("query")
        if text:
            _embed_now(asset["id"], text=text)
    elif fn and (ASSET_CACHE / fn).exists():
        _embed_now(asset["id"], image=(ASSET_CACHE / fn).read_bytes())


async def _reindex_bg(assets: list[dict]) -> None:
    n = 0
    for a in assets:
        async with _embed_lock:                   # serialize with normal write-through embeds
            await asyncio.to_thread(_embed_one, a)
        n += 1
    print(f"[conjure] reindex: embedded {n} catalog asset(s)")
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

app = FastAPI(title="Conjure", version="0.0.1")


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
                   provider=result.provider, model=result.model, width=w, height=h)
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
                          prompt=cat.get("prompt") or "", op=op, transparent=transparent)
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
        w, h = im.size
        if im.mode in ("RGBA", "LA"):
            transparent = im.getchannel("A").getextrema()[0] < 255  # some pixel non-opaque
        else:
            transparent = "transparency" in im.info
        return w, h, transparent


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
async def tunnel() -> RedirectResponse:
    """Redirect to the current cloudflared tunnel URL (written by scripts/tunnel.sh). Lets you type a
    short, fixed LAN address (http://<this-machine>:<port>/tunnel) on the Quest instead of the long
    random trycloudflare URL that changes every session."""
    url = TUNNEL_FILE.read_text().strip() if TUNNEL_FILE.exists() else ""
    if not url:
        raise HTTPException(status_code=404, detail="No tunnel running — start one with scripts/tunnel.sh")
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
    """Reset to the empty starter holodeck — clears all entities + environment (incl. any captured
    room) and broadcasts a fresh snapshot so every client reloads. The room re-captures on its own
    once a headset is back in AR."""
    global store
    store = WorldStore.load(SAMPLE_WORLD)
    await _broadcast({"type": "snapshot", "world": store.doc})
    return {"ok": True, "rev": store.doc["rev"]}


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


@app.post("/room")
async def ingest_room(req: RoomUpdate) -> dict:
    """Ingest captured room geometry from the room **authority** headset. Existing surfaces are
    *updated* in place (preserving director style); new ones added; with replace=True, stale ones
    removed. Sets environment.room (boundary, active, authority)."""
    room = store.doc["environment"].get("room", {})
    authority = room.get("authorityClientId")
    if authority and authority != req.client_id:
        return {"ok": False, "error": f"another headset ({authority}) is the room authority"}

    existing = {e["id"]: e for e in store.doc["entities"] if e.get("meta", {}).get("real")}
    new_ids = {s.id for s in req.surfaces}
    ops: list[dict] = []

    if req.replace:
        ops += [{"op": "remove", "id": eid} for eid in existing if eid not in new_ids]

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
    _embed_asset(model_id, text=record.title)   # models embed their title text (shared space)

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
    return {"ok": True, **library.find(text=req.query, query_vec=qvec, kind=req.kind)}


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


class CorrectAssetRequest(BaseModel):
    id: str
    label: Optional[str] = None       # rewrite the machine description …
    query: Optional[str] = None
    tags: Optional[str] = None
    reject_for: Optional[str] = None  # … and/or exclude this asset from future matches on a query


@app.post("/correct_asset")
async def correct_asset(req: CorrectAssetRequest) -> dict:
    """Fix a mismatch (e.g. an X-wing returned for 'starship enterprise'): relabel its description
    and/or reject it for a query so it won't match that again. No scene effect."""
    if library.get(req.id) is None:
        return {"ok": False, "error": f"no asset {req.id!r} in the library"}
    fields = {k: v for k, v in (("label", req.label), ("query", req.query), ("tags", req.tags))
              if v is not None}
    if not fields and not req.reject_for:
        return {"ok": False, "error": "nothing to correct (pass label/query/tags or reject_for)"}
    if fields:
        library.upsert(req.id, **fields)
    if req.reject_for:
        library.reject(req.id, req.reject_for)
    return {"ok": True, "id": req.id}


class ReindexRequest(BaseModel):
    kind: Optional[str] = None       # optionally restrict to image | model | …


@app.post("/library/reindex")
async def library_reindex(req: ReindexRequest) -> dict:
    """Embed cataloged assets that have no vector yet (e.g. everything backfilled before embeddings) —
    a one-time pass so the existing library becomes searchable by similarity. Runs off the request
    path; returns how many were queued."""
    if embedder is None:
        return {"ok": False, "error": "no embedder — install the optional 'embed' dependency group"}
    targets = library.assets_missing_embedding(kind=req.kind)
    if _EMBED_BACKGROUND:
        task = asyncio.create_task(_reindex_bg(targets))
        _embed_tasks.add(task)
        task.add_done_callback(_embed_tasks.discard)
    else:
        for a in targets:
            _embed_one(a)
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


class AnnotateAssetRequest(BaseModel):
    id: str                                  # the catalog asset id (an image_id, or "<hash>.glb")
    note: Optional[str] = None
    tags: Optional[str] = None
    favorite: Optional[bool] = None
    rating: Optional[int] = None
    default_for: Optional[str] = None        # pin an alias: "dog" → this asset (a reuse override)


@app.post("/annotate_asset")
async def annotate_asset(req: AnnotateAssetRequest) -> dict:
    """Record the user's own curation of a library asset (notes/tags/favorite/rating + a 'default for
    X' alias). No scene effect — it just makes the asset more findable and reusable later."""
    if not library.annotate(req.id, note=req.note, tags=req.tags, favorite=req.favorite,
                             rating=req.rating, default_for=req.default_for):
        return {"ok": False, "error": f"no asset {req.id!r} in the library"}
    return {"ok": True, "id": req.id}


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
    return {"ok": True, "id": req.id, "image_id": new.id}


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
    return {"ok": True, "id": req.id, "image_id": new.id}


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
    return {"ok": True, "sky": new.url, "image_id": new.id}


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    await websocket.accept()
    clients.add(websocket)
    await websocket.send_json({"type": "snapshot", "world": store.doc})
    try:
        while True:
            # Phase 0: client->server intents (behaviors/input) are ignored for now.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(websocket)


async def _broadcast(message: dict) -> None:
    dead = []
    for ws_ in clients:
        try:
            await ws_.send_json(message)
        except Exception:
            dead.append(ws_)
    for d in dead:
        clients.discard(d)


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
