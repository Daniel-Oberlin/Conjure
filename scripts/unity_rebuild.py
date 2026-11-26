#!/usr/bin/env python3
"""Re-assemble a Unity player build into self-contained, textured GLBs.

    python scripts/unity_rebuild.py <Data dir> --out temp/unity --under Ships
    python scripts/unity_rebuild.py <Data dir> --out temp/unity --under Ships --only 1701D
    python scripts/unity_rebuild.py <Data dir> --list                 # the scene tree's renderable roots
    python scripts/unity_rebuild.py <Data dir> --out out --lossless   # full-size PNG textures

`<Data dir>` is a player build's data folder — an APK's `assets/bin/Data`. One GLB is written per
child of `--under` (a GameObject name or path), or per renderable scene root without it.

**`--max-texture` defaults to 1024 and that is a budget, not an optimisation** — the same one
`playcanvas_rebuild.py` uses (docs/investigations/figures-frame-rate.md). `--lossless` overrides it:
full-size PNG, exactly the pixels the build decodes. See `conjure/unity.py` for what else is exact.

Needs the optional dependency: pip install -e ".[unity]"
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure.unity import Build, rebuild_build                                                   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", help="the build's data folder (e.g. extracted APK assets/bin/Data)")
    ap.add_argument("--out", default="temp/unity", help="output directory for .glb files")
    ap.add_argument("--under", default="", help="export each child of this GameObject (name or path)")
    ap.add_argument("--only", default="", help="only things whose name contains this")
    ap.add_argument("--max-texture", type=int, default=1024, help="longest texture side (0 = no limit)")
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality for opaque textures")
    ap.add_argument("--lossless", action="store_true", help="full-size PNG textures, no budget")
    ap.add_argument("--list", action="store_true", help="print the scene's things and exit")
    a = ap.parse_args()
    if a.list:
        build = Build(a.root)
        for t in build.things(a.under):
            print(t.path)
        return 0
    written = rebuild_build(a.root, a.out, under=a.under, only=a.only, max_texture=a.max_texture,
                            lossless=a.lossless, quality=a.quality, report=print)
    print(f"\n{len(written)} file(s) written to {a.out}")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
