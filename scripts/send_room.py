#!/usr/bin/env python3
"""Post a SYNTHETIC room to a running Conjure server — exercise the room model without a headset.

Simulates what the Quest's WebXR capture would send (docs/room-model.md): four walls, a floor, a
ceiling, and a table, plus the room boundary. Lets you see real surfaces render and drive the
director's `set_immersion` / `show_surface` in a desktop browser.

Usage:  python scripts/send_room.py
        CONJURE_URL=http://localhost:8080 python scripts/send_room.py
Then, e.g.:  conjure-cli say "show my walls and make them blue"
             conjure-cli say "drop into full VR"   (set_immersion vr_unbounded)
"""

import json
import os
import urllib.request

URL = os.environ.get("CONJURE_URL", "http://localhost:8080") + "/room"

# A 4m × 3m room, 2.6m tall, centered a couple meters in front of the user (faces -z).
W, D, H = 4.0, 3.0, 2.6
ROOM = {
    "client_id": "synthetic",
    "boundary": {"floorPolygon": [[-W / 2, -1], [W / 2, -1], [W / 2, -1 - D], [-W / 2, -1 - D]], "height": H},
    "surfaces": [
        {"id": "real_floor", "semantic": "floor", "position": [0, 0, -1 - D / 2], "rotation": [-90, 0, 0], "extent": [W, D]},
        {"id": "real_ceiling", "semantic": "ceiling", "position": [0, H, -1 - D / 2], "rotation": [90, 0, 0], "extent": [W, D]},
        {"id": "real_wall_back", "semantic": "wall", "position": [0, H / 2, -1 - D], "extent": [W, H]},
        {"id": "real_wall_left", "semantic": "wall", "position": [-W / 2, H / 2, -1 - D / 2], "rotation": [0, 90, 0], "extent": [D, H]},
        {"id": "real_wall_right", "semantic": "wall", "position": [W / 2, H / 2, -1 - D / 2], "rotation": [0, -90, 0], "extent": [D, H]},
        {"id": "real_table", "semantic": "table", "position": [0, 0.75, -2.2], "rotation": [-90, 0, 0], "extent": [1.2, 0.8]},
    ],
    "replace": True,
}


def main() -> int:
    req = urllib.request.Request(
        URL, data=json.dumps(ROOM).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        print(resp.read().decode())
    print("Posted a synthetic room. Try: conjure-cli say \"show me my room\" / \"make the walls glass\".")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
