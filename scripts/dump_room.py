#!/usr/bin/env python3
"""Print the captured room surfaces (semantic, position, rotation, extent) + boundary — for
diagnosing how the headset's planes mapped into the world model.

Usage:  python scripts/dump_room.py
        CONJURE_URL=http://localhost:8080 python scripts/dump_room.py
"""

import json
import math
import os
import urllib.request

URL = os.environ.get("CONJURE_URL", "http://localhost:8080") + "/world"


def normal_from_euler(rot):
    """World-space normal of the rendered <a-plane> (its local +Z), given A-Frame euler degrees.
    A-Frame applies rotations in YXZ order, so the normal is Ry·Rx·(0,0,1) (Z/roll drops out):
    (cosx·siny, -sinx, cosx·cosy). Using the wrong order here is what made dumps read 'square' while
    the headset rendered skewed."""
    x, y, z = (math.radians(a) for a in (rot + [0, 0, 0])[:3])
    c1, s1, c2, s2 = math.cos(x), math.sin(x), math.cos(y), math.sin(y)
    return (c1 * s2, -s1, c1 * c2)


def _yaw(n):
    """Compass yaw of the normal about vertical, degrees — for spotting walls swung off-axis."""
    return round(math.degrees(math.atan2(n[0], -n[2])), 1)


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
        fid = e["meta"].get("friendly_id", "?")
        n = normal_from_euler(t.get("rotation", [0, 0, 0]))
        nstr = [round(c, 2) for c in n]
        print(f"  #{str(fid):3} {e['meta'].get('semantic', '?'):9} pos={pos}  rot={rot}  ext={ext}")
        print(f"            normal={nstr}  yaw={_yaw(n)}°  vertical={'yes' if abs(n[1]) < 0.3 else 'no'}")
        dbg = e.get("meta", {}).get("debug")
        if dbg:
            q = [round(x, 3) for x in dbg.get("quat", [])]
            rp = [round(x, 2) for x in dbg.get("pos", [])]
            print(f"            raw: label={dbg.get('label')} orient={dbg.get('orient')} "
                  f"pos={rp} quat={q} polyY={[round(y,3) for y in dbg.get('polyY',[])]} n={dbg.get('n')} "
                  f"reg={dbg.get('registered')} [{dbg.get('regStat')}]")
            if dbg.get("snap"):
                print(f"            snap: {dbg.get('snap')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
