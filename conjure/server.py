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

from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .assets import AssetResolver
from .config import get_settings
from .schema import Patch
from .world import WorldStore

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "client"
SAMPLE_WORLD = ROOT / "examples" / "sample_world.json"
ASSET_CACHE = ROOT / ".cache" / "assets"

settings = get_settings()  # loads .env
store = WorldStore.load(SAMPLE_WORLD)
clients: set[WebSocket] = set()
resolver: AssetResolver | None = (
    AssetResolver(settings.poly_pizza_api_key, ASSET_CACHE) if settings.poly_pizza_api_key else None
)

app = FastAPI(title="Conjure", version="0.0.1")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(CLIENT_DIR / "index.html")


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
    name: Optional[str] = None


@app.get("/assets/{filename}")
async def asset(filename: str) -> FileResponse:
    """Serve a cached GLB so the headset loads it from this server (same origin/connection)."""
    if resolver is None:
        raise HTTPException(status_code=404, detail="assets disabled (no POLY_PIZZA_API_KEY)")
    asset_hash = filename[:-4] if filename.endswith(".glb") else filename
    path = resolver.path_for(asset_hash)
    if not path.exists():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(path, media_type="model/gltf-binary")


@app.post("/place_asset")
async def place_asset(req: PlaceAssetRequest) -> dict:
    """Resolve a search query to a real model: drop a placeholder, then swap in the GLB."""
    if resolver is None:
        return {"ok": False, "error": "POLY_PIZZA_API_KEY not set"}

    eid = req.name or f"ent_asset_{uuid4().hex[:6]}"
    pos = req.position or [0.0, 1.0, -3.0]

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

    # 3. Swap the placeholder for the real glTF model (carrying license + attribution).
    swap = [
        {"op": "remove", "id": eid},
        {
            "op": "add",
            "entity": {
                "id": eid,
                "transform": {"position": pos},
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
