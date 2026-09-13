#!/usr/bin/env python3
"""Strip skinning or drop a mesh from a GLB — to isolate what a figure's frame cost is made of.

    python scripts/glb_strip.py in.glb out.glb --unskin Hair.001
    python scripts/glb_strip.py in.glb out.glb --unskin Hair.001 --drop Hair.001

`docs/investigations/figures-frame-rate.md` narrowed a chronic frame-rate problem to two candidates that
scale together across the cast and so cannot be told apart by observation: **triangles** and **bones**.
Trish is the extreme case and also the way out of the confound — her hair is 84 k of her 125 k triangles
*and* 679 of her 1041 joints, so removing the hair rig moves both at once and settles nothing.

This makes the two variants that separate them:

    original                       84 k hair triangles, hair skinned
    --unskin Hair.001              84 k hair triangles, hair NOT skinned
    --unskin ... --drop Hair.001   no hair triangles,   hair NOT skinned

    original vs unskinned  → the cost of SKINNING those vertices (bones)
    unskinned vs dropped   → the cost of DRAWING those vertices (triangles)

**What `--unskin` actually removes**, stated precisely so the result is not over-read: the mesh stops
being a `SkinnedMesh`, so `Skeleton.update()` no longer recomputes and uploads a bone matrix per joint
each frame. The joint *nodes* remain in the scene graph and are still walked by `updateMatrixWorld`, so
this isolates the skinning cost rather than all per-bone cost. A skinned mesh's vertices are stored in
bind space and this hair node has an identity world matrix, so the geometry renders exactly where it did
— statically. It will not follow the head when posed, which is irrelevant to a frame-rate measurement
and makes these variants unfit for anything else.
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure.figures import write_glb                                          # noqa: E402


def read(path: str):
    data = open(path, "rb").read()
    if data[:4] != b"glTF":
        sys.exit(f"{path} is not a binary glTF")
    off, doc, blob = 12, None, b""
    while off < len(data) - 8:
        ln, kind = struct.unpack("<II", data[off:off + 8])
        chunk = data[off + 8: off + 8 + ln]
        if kind == 0x4E4F534A:
            doc = json.loads(chunk)
        elif kind == 0x004E4942:
            blob = chunk
        off += 8 + ln
    return doc, blob


def write(path: str, doc: dict, blob: bytes) -> None:
    open(path, "wb").write(write_glb(doc, blob))


def tri_count(doc: dict, mesh_index: int) -> int:
    accs, total = doc["accessors"], 0
    for p in doc["meshes"][mesh_index].get("primitives", []):
        total += (accs[p["indices"]]["count"] // 3 if "indices" in p
                  else accs[p["attributes"]["POSITION"]]["count"] // 3)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--unskin", action="append", default=[],
                    help="node name: stop it being a skinned mesh (keeps its triangles)")
    ap.add_argument("--drop", action="append", default=[],
                    help="node name: remove the mesh node from the scene entirely")
    args = ap.parse_args()

    doc, blob = read(args.src)
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}

    def resolve(name: str) -> int:
        if name not in by_name:
            sys.exit(f"no node named {name!r}. Mesh nodes: "
                     + ", ".join(sorted(str(n.get('name')) for n in nodes if 'mesh' in n)))
        return by_name[name]

    def skin_load(d, reachable=None):
        """(distinct skins in use, joints across them, skinned-mesh count).

        Counted per SKIN, not per mesh node — several meshes share one skeleton (Trish's body is five
        nodes on one 362-joint skin), and summing per node triple-counts the skeleton. The mesh count is
        reported separately because three calls `Skeleton.update()` per rendered skinned mesh, so five
        meshes on one skeleton is not the same cost as one.
        """
        ns = d.get("nodes") or []
        idx = range(len(ns)) if reachable is None else reachable
        used, meshes = {}, 0
        for i in idx:
            if "mesh" in ns[i] and ns[i].get("skin") is not None:
                used[ns[i]["skin"]] = len((d["skins"][ns[i]["skin"]].get("joints") or []))
                meshes += 1
        return len(used), sum(used.values()), meshes

    before = skin_load(doc)
    before_tris = sum(tri_count(doc, n["mesh"]) for n in nodes if "mesh" in n)

    for name in args.unskin:
        i = resolve(name)
        node = nodes[i]
        skin = node.pop("skin", None)
        # The skin stays in `skins` unreferenced rather than being spliced out — removing it would
        # shift every later skin index and silently re-point the other meshes' skeletons.
        for p in doc["meshes"][node["mesh"]].get("primitives", []):
            for attr in ("JOINTS_0", "WEIGHTS_0", "JOINTS_1", "WEIGHTS_1"):
                p["attributes"].pop(attr, None)
        print(f"  unskinned {name!r} (was skin {skin}, "
              f"{len(doc['skins'][skin].get('joints') or []) if skin is not None else 0} joints); "
              f"{tri_count(doc, node['mesh']):,} triangles kept")

    for name in args.drop:
        i = resolve(name)
        removed = False
        for n in nodes:
            kids = n.get("children")
            if kids and i in kids:
                n["children"] = [c for c in kids if c != i]
                removed = True
        for sc in doc.get("scenes") or []:
            if i in (sc.get("nodes") or []):
                sc["nodes"] = [c for c in sc["nodes"] if c != i]
                removed = True
        print(f"  dropped {name!r} ({tri_count(doc, nodes[i]['mesh']):,} triangles) "
              f"{'' if removed else '— NOT REFERENCED, nothing changed'}")

    write(args.dst, doc, blob)

    # Report what a renderer will now see: reachable mesh nodes only.
    seen, stack = set(), [i for sc in (doc.get("scenes") or []) for i in (sc.get("nodes") or [])]
    while stack:
        i = stack.pop()
        if i in seen:
            continue
        seen.add(i)
        stack.extend(nodes[i].get("children") or [])
    tris = sum(tri_count(doc, nodes[i]["mesh"]) for i in seen if "mesh" in nodes[i])
    after = skin_load(doc, seen)
    print(f"\n{args.dst}")
    print(f"  triangles drawn   : {before_tris:>8,} → {tris:>8,}")
    print(f"  skeletons in use  : {before[0]:>8} → {after[0]:>8}")
    print(f"  joints across them: {before[1]:>8,} → {after[1]:>8,}")
    print(f"  skinned meshes    : {before[2]:>8} → {after[2]:>8}"
          "   (three updates a skeleton per rendered skinned mesh)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
