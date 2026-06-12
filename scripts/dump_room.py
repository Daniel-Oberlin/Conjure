#!/usr/bin/env python3
"""Print the captured room surfaces (semantic, position, rotation, extent) + boundary — for
diagnosing how the headset's planes mapped into the world model.

Usage:  python scripts/dump_room.py
        CONJURE_URL=http://localhost:8080 python scripts/dump_room.py
"""

import json
import os
import urllib.request

URL = os.environ.get("CONJURE_URL", "http://localhost:8080") + "/world"


def main() -> int:
    with urllib.request.urlopen(URL) as resp:
        doc = json.load(resp)
    env = doc.get("environment", {})
    room = env.get("room", {})
    reals = [e for e in doc["entities"] if e.get("meta", {}).get("real")]
    print(f"room.active={room.get('active')} passthrough={env.get('passthrough')} "
          f"authority={room.get('authorityClientId')}  ({len(reals)} surfaces)")
    if room.get("boundary"):
        print(f"boundary: {room['boundary']}")
    for e in reals:
        t = e.get("transform", {})
        pos = [round(x, 2) for x in t.get("position", [])]
        rot = [round(x, 1) for x in t.get("rotation", [0, 0, 0])]
        ext = e.get("components", {}).get("surface", {}).get("extent")
        ext = [round(x, 2) for x in ext] if ext else ext
        print(f"  {e['meta'].get('semantic', '?'):9} pos={pos}  rot={rot}  ext={ext}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
