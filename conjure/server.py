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

import hashlib
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .assets import AssetResolver
from .config import get_settings
from .imagegen import get_image_generator
from .schema import Patch
from .world import WorldStore

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "client"
SAMPLE_WORLD = ROOT / "examples" / "sample_world.json"
ASSET_CACHE = ROOT / ".cache" / "assets"
ASSET_CACHE.mkdir(parents=True, exist_ok=True)
MEDIA_TYPES = {".glb": "model/gltf-binary", ".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}

settings = get_settings()  # loads .env
store = WorldStore.load(SAMPLE_WORLD)
clients: set[WebSocket] = set()
resolver: AssetResolver | None = (
    AssetResolver(settings.poly_pizza_api_key, ASSET_CACHE) if settings.poly_pizza_api_key else None
)
image_gen = get_image_generator(settings)  # None if its key/provider isn't configured

TARGET_SIZE_M = 1.8  # fit a placed model's largest dimension to ~this many meters

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


_NO_STORE = {"Cache-Control": "no-store"}  # avoid stale client during active dev


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "index.html", headers=_NO_STORE)


@app.get("/static/conjure-client.js")
async def client_js() -> FileResponse:
    # Explicit route (takes precedence over the /static mount) so we can disable caching.
    return FileResponse(CLIENT_DIR / "conjure-client.js", media_type="application/javascript", headers=_NO_STORE)


@app.get("/world")
async def world() -> dict:
    return store.doc


@app.post("/patch")
async def post_patch(patch: Patch) -> dict:
    ops = [op.model_dump() for op in patch.ops]
    applied = store.apply_patch(ops, origin=patch.origin)
    await _broadcast({"type": "patch", "patch": applied})
    return applied


class PlaceAssetRequest(BaseModel):
    query: str
    position: Optional[list[float]] = None
    size_m: Optional[float] = None  # intended real-world largest dimension, meters
    name: Optional[str] = None


@app.get("/assets/{filename}")
async def asset(filename: str) -> FileResponse:
    """Serve a cached asset (GLB model or generated image) from this server's content store."""
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

    # 3. Swap the placeholder for the real glTF model (auto-scaled to sit on the floor),
    #    carrying license + attribution.
    model_pos, model_scale = _normalize(record, pos, req.size_m or TARGET_SIZE_M)
    swap = [
        {"op": "remove", "id": eid},
        {
            "op": "add",
            "entity": {
                "id": eid,
                "transform": {"position": model_pos, "scale": model_scale},
                "components": {"gltf-model": f"/assets/{record.hash}.glb"},
                "meta": {
                    "title": record.title,
                    "license": record.licence,
                    "attribution": record.attribution,
                    "creator": record.creator,
                    "source": "poly.pizza",
                    "tris": record.tris,
                    "generated": False,
                },
            },
        },
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


class PlaceImageRequest(BaseModel):
    prompt: str
    position: Optional[list[float]] = None
    size_m: Optional[float] = None
    name: Optional[str] = None


def _image_plane(eid: str, pos: list[float], size: float, material: dict, meta: dict | None = None) -> dict:
    return {
        "op": "add",
        "entity": {
            "id": eid,
            "transform": {"position": pos},
            "components": {
                "geometry": {"primitive": "plane", "width": size, "height": size},
                "material": material,
            },
            **({"meta": meta} if meta else {}),
        },
    }


@app.post("/place_image")
async def place_image(req: PlaceImageRequest) -> dict:
    """Generate an image and hang it as a textured plane (a painting/poster) facing the user."""
    if image_gen is None:
        return {"ok": False, "error": f"no image generator (set GOOGLE_API_KEY; provider={settings.image_provider})"}

    eid = req.name or f"ent_image_{uuid4().hex[:6]}"
    pos = req.position or [0.0, 1.5, -3.0]  # eye height, on the wall in front
    size = req.size_m or 1.0

    # 1. Placeholder frame appears instantly.
    placeholder = _image_plane(eid, pos, size, {"color": "#333", "side": "double"})
    await _broadcast({"type": "patch", "patch": store.apply_patch([placeholder], origin="image")})

    # 2. Generate.
    try:
        result = await image_gen.generate(req.prompt)
    except Exception as exc:  # noqa: BLE001
        await _broadcast({"type": "patch", "patch": store.apply_patch([{"op": "remove", "id": eid}], origin="image")})
        return {"ok": False, "error": str(exc)}

    ext = ".png" if "png" in result.mime_type else (".webp" if "webp" in result.mime_type else ".jpg")
    img_hash = hashlib.sha256(result.data).hexdigest()[:16]
    (ASSET_CACHE / f"{img_hash}{ext}").write_bytes(result.data)
    url = f"/assets/{img_hash}{ext}"

    # 3. Swap the placeholder for the generated image (flat shader so colors are true).
    swap = [
        {"op": "remove", "id": eid},
        _image_plane(
            eid, pos, size,
            {"src": url, "shader": "flat", "side": "double"},
            {"generated": True, "provider": result.provider, "model": result.model, "prompt": req.prompt},
        ),
    ]
    await _broadcast({"type": "patch", "patch": store.apply_patch(swap, origin="image")})
    return {"ok": True, "id": eid, "prompt": req.prompt, "provider": result.provider, "model": result.model}


class SkyboxRequest(BaseModel):
    prompt: str


@app.post("/set_skybox")
async def set_skybox(req: SkyboxRequest) -> dict:
    """Generate a 360° panorama and wrap the whole scene in it (the sky/environment)."""
    if image_gen is None:
        return {"ok": False, "error": f"no image generator (set GOOGLE_API_KEY; provider={settings.image_provider})"}

    full_prompt = (
        f"A seamless equirectangular 360-degree panorama for a VR skybox: {req.prompt}. "
        "Centered horizon, evenly lit, no people, no text, no watermark, no borders."
    )
    try:
        result = await image_gen.generate(full_prompt, aspect_ratio="21:9")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    ext = ".png" if "png" in result.mime_type else (".webp" if "webp" in result.mime_type else ".jpg")
    img_hash = hashlib.sha256(result.data).hexdigest()[:16]
    (ASSET_CACHE / f"{img_hash}{ext}").write_bytes(result.data)
    url = f"/assets/{img_hash}{ext}"

    patch = store.apply_patch([{"op": "env", "set": {"sky": {"src": url}}}], origin="image")
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "sky": url, "provider": result.provider, "model": result.model}


class EditImageRequest(BaseModel):
    id: str
    prompt: str


@app.post("/edit_image")
async def edit_image(req: EditImageRequest) -> dict:
    """Edit an existing in-world image (from place_image) in place — conversational editing."""
    if image_gen is None:
        return {"ok": False, "error": f"no image generator (set GOOGLE_API_KEY; provider={settings.image_provider})"}

    entity = next((e for e in store.doc["entities"] if e["id"] == req.id), None)
    if entity is None:
        return {"ok": False, "error": f"no entity {req.id!r}"}
    src = entity.get("components", {}).get("material", {}).get("src")
    if not src:
        return {"ok": False, "error": f"{req.id!r} is not an editable image"}
    path = ASSET_CACHE / src.rsplit("/", 1)[-1]
    if not path.exists():
        return {"ok": False, "error": "source image not cached"}

    try:
        result = await image_gen.edit(req.prompt, path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}

    ext = ".png" if "png" in result.mime_type else (".webp" if "webp" in result.mime_type else ".jpg")
    img_hash = hashlib.sha256(result.data).hexdigest()[:16]
    (ASSET_CACHE / f"{img_hash}{ext}").write_bytes(result.data)
    new_url = f"/assets/{img_hash}{ext}"

    patch = store.apply_patch(
        [{"op": "update", "id": req.id, "set": {"components.material.src": new_url, "meta.prompt": req.prompt}}],
        origin="image",
    )
    await _broadcast({"type": "patch", "patch": patch})
    return {"ok": True, "id": req.id, "src": new_url, "model": result.model}


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


# Mount static last so it doesn't shadow the routes above.
app.mount("/static", StaticFiles(directory=CLIENT_DIR), name="static")
