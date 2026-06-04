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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .schema import Patch
from .world import WorldStore

ROOT = Path(__file__).resolve().parent.parent
CLIENT_DIR = ROOT / "client"
SAMPLE_WORLD = ROOT / "examples" / "sample_world.json"

store = WorldStore.load(SAMPLE_WORLD)
clients: set[WebSocket] = set()

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
