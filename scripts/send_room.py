#!/usr/bin/env python3
"""Post a SYNTHETIC room to a running Conjure server — exercise the room model without a headset.

Simulates what the Quest's WebXR capture would send (docs/specs/worlds-surfaces.md): four walls, a floor, a
ceiling, and a table, plus the room boundary — **centered on the user** so it surrounds you (a real
capture arrives relative to where you stand). Also flips to `virtual_room` immersion so the surfaces
are visible on desktop. Then drive the director (`set_immersion` / `show_surface`).

Usage:  python scripts/send_room.py
        CONJURE_URL=http://localhost:8080 python scripts/send_room.py
Then, e.g.:  conjure-cli say "make the walls glass and the ceiling a galaxy"
             conjure-cli say "drop into full VR"   (set_immersion vr_unbounded)
"""

import json
import os
import urllib.request

BASE = os.environ.get("CONJURE_URL", "http://localhost:8080")

# Room centered on the user. The client rig is at the world origin, so center the room there
# (CX, CZ) so you stand inside it. W = width (x), D = depth (z), H = height. The user faces -z.
CX, CZ = 0.0, 0.0
W, D, H = 4.0, 5.0, 2.6
x0, x1 = CX - W / 2, CX + W / 2
z0, z1 = CZ - D / 2, CZ + D / 2

ROOM = {
    "client_id": "synthetic",
    "boundary": {"floorPolygon": [[x0, z0], [x1, z0], [x1, z1], [x0, z1]], "height": H},
    "surfaces": [
        {"id": "real_floor",   "semantic": "floor",   "position": [CX, 0, CZ], "rotation": [-90, 0, 0], "extent": [W, D]},
        {"id": "real_ceiling", "semantic": "ceiling", "position": [CX, H, CZ], "rotation": [-90, 0, 0], "extent": [W, D]},
        {"id": "real_wall_front", "semantic": "wall", "position": [CX, H / 2, z0], "rotation": [0, 0, 0],   "extent": [W, H]},
        {"id": "real_wall_back",  "semantic": "wall", "position": [CX, H / 2, z1], "rotation": [0, 0, 0],   "extent": [W, H]},
        {"id": "real_wall_left",  "semantic": "wall", "position": [x0, H / 2, CZ], "rotation": [0, 90, 0],  "extent": [D, H]},
        {"id": "real_wall_right", "semantic": "wall", "position": [x1, H / 2, CZ], "rotation": [0, 90, 0],  "extent": [D, H]},
        {"id": "real_table", "semantic": "table", "position": [CX, 0.75, CZ - 1.5], "rotation": [-90, 0, 0], "extent": [1.2, 0.8]},
    ],
    "replace": True,
}

# Make the surfaces visible on desktop (no passthrough): virtual_room immersion.
VISIBLE = {"ops": [{"op": "env", "set": {
    "passthrough": False, "room.active": True, "room.defaultSurfaceVisible": True}}]}


def _post(path: str, body: dict) -> str:
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode()


def main() -> int:
    print(_post("/room", ROOM))
    _post("/patch", VISIBLE)   # show the surfaces (virtual_room mode)
    print("Posted a synthetic room around you (virtual_room mode). Try:")
    print('  conjure-cli say "make the walls glass and the ceiling a galaxy"')
    print('  conjure-cli say "switch to AR" / "drop into full VR"')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
