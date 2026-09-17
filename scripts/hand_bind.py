#!/usr/bin/env python3
"""Measure a hand model's 24 bind bone lengths — the table `client/hands-fit.js` compares against.

    python3 scripts/hand_bind.py                 both catalog hands, 24 lengths each
    python3 scripts/hand_bind.py --check         diff the models against the table in the client
    python3 scripts/hand_bind.py --axes          the -Z / -Y convention, per bone
    python3 scripts/hand_bind.py <id> <id> ...   specific asset ids

Exists because `hands-fit.js` carries those 24 numbers as a literal, and a literal measured from two
specific files goes stale the moment the files are re-imported — silently, while still producing a
confident ratio. `--check` is the tripwire; it is not in the pytest suite because the suite deliberately
never reads the real asset cache (tests/conftest.py points ASSET_CACHE at a tmp dir).

Two things it found on first run, both of which contradict what was assumed about these files:

  * The 25 joint nodes are SIBLINGS under `hand_L`/`hand_R`, not a parent→child chain. Every joint also
    carries a Blender `<name>_end` tail node. A gate that requires "each finger a real parent→child
    chain" would refuse the very models it was written for.
  * The clean pair is NOT a mirror: the right index metacarpal is 6.3 mm shorter than the left. Middle,
    ring and pinky agree to 0.3 mm, so this is specific rather than general sloppiness.
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conjure.figures import (node_world_matrices, node_world_positions,  # noqa: E402
                             parent_map, split_glb)
from conjure.library import AssetLibrary                                 # noqa: E402
from conjure.server import ASSET_CACHE, LIBRARY_DB                       # noqa: E402

argv = sys.argv[1:]
FLAGS = {a for a in argv if a.startswith("--")}
IDS = [a for a in argv if not a.startswith("--")]

#: The 5 chains, wrist-first. 4 bones down the thumb and 5 down each finger = 24 — the same order as
#: `SEGMENTS` in client/hands-fit.js, which is what makes the two tables comparable at all.
CHAINS = [["wrist", "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip"]]
for _f in ("index", "middle", "ring", "pinky"):
    CHAINS.append(["wrist", f"{_f}-finger-metacarpal", f"{_f}-finger-phalanx-proximal",
                   f"{_f}-finger-phalanx-intermediate", f"{_f}-finger-phalanx-distal", f"{_f}-finger-tip"])
SEGMENTS = [(c[i], c[i + 1]) for c in CHAINS for i in range(len(c) - 1)]

CLIENT = Path(__file__).resolve().parent.parent / "client" / "hands-fit.js"


def hands() -> list[dict]:
    lib = AssetLibrary(LIBRARY_DB)
    out = []
    for r in lib.search(kind="model", limit=5000):
        if IDS:
            if not any(r["id"].startswith(i) for i in IDS):
                continue
        elif "vr_hand" not in (r["label"] or "").lower():
            continue
        p = Path(r["filename"])
        p = p if p.is_absolute() else Path(ASSET_CACHE) / r["filename"]
        if p.exists():
            out.append({"id": r["id"], "label": r["label"] or "?", "path": p})
    return out


def measure(path: Path) -> dict:
    doc, _blob = split_glb(path.read_bytes())
    if isinstance(doc, (bytes, bytearray, str)):
        doc = json.loads(doc)
    nodes = doc.get("nodes") or []
    by = {}
    for i, n in enumerate(nodes):
        if n.get("name"):
            by.setdefault(n["name"], i)
    pos, mats, par = node_world_positions(doc), node_world_matrices(doc), parent_map(doc)

    lengths, axes = [], []
    for a, b in SEGMENTS:
        if a not in by or b not in by:
            lengths.append(None)
            axes.append(None)
            continue
        pa, pb = pos[by[a]], pos[by[b]]
        d = [pb[k] - pa[k] for k in range(3)]
        n = math.dist(pa, pb)
        lengths.append(n)
        # Does the bone run along the PARENT joint's own local -Z, as the WebXR spec says it should?
        m, unit = mats[by[a]], (n or 1.0)
        neg_z = [-m[8], -m[9], -m[10]]
        zl = math.sqrt(sum(c * c for c in neg_z)) or 1.0
        axes.append(sum((d[k] / unit) * (neg_z[k] / zl) for k in range(3)))

    missing = sorted({j for c in CHAINS for j in c if j not in by})
    parents = {nodes[par[by[j]]].get("name") for c in CHAINS for j in c[1:]
               if j in by and by[j] in par}
    return {"lengths": lengths, "axes": axes, "missing": missing,
            "joint_parents": sorted(x for x in parents if x), "nodes": len(nodes)}


def client_table() -> dict[str, list[float]]:
    """Parse BIND out of the client, so --check compares the shipped literal and not a copy of it."""
    src = CLIENT.read_text()
    out: dict[str, list[float]] = {}
    for side in ("left", "right"):
        m = re.search(side + r":\s*\[(.*?)\]", src, re.S)
        if not m:
            continue
        out[side] = [float(x) for x in re.findall(r"0\.\d+", m.group(1))]
    return out


def side_of(label: str) -> str:
    return "left" if label.strip().lower().endswith("_l") else "right"


def show(h: dict, m: dict) -> None:
    print(f"\n{h['id']}  {h['label']}  ({m['nodes']} nodes)")
    if m["missing"]:
        print("  MISSING joints:", ", ".join(m["missing"]))
    print("  joints parented under:", ", ".join(m["joint_parents"]) or "(roots)")
    at = 0
    for c in CHAINS:
        row = [m["lengths"][at + k] for k in range(len(c) - 1)]
        at += len(c) - 1
        print(f"  {c[1].split('-')[0]:7s} " + " ".join("—" if v is None else f"{v * 100:6.3f}" for v in row))
    if "--axes" in FLAGS:
        at = 0
        for c in CHAINS:
            row = [m["axes"][at + k] for k in range(len(c) - 1)]
            at += len(c) - 1
            print(f"  {c[1].split('-')[0]:7s} dir·(-Z) "
                  + " ".join("—" if v is None else f"{v:+.3f}" for v in row))


def main() -> None:
    found = hands()
    if not found:
        print("no hand models in the catalog (looked for labels containing 'VR_hand')")
        return
    measured = {}
    for h in found:
        m = measure(h["path"])
        measured[h["id"]] = (h, m)
        if "--check" not in FLAGS:
            show(h, m)

    if "--check" not in FLAGS:
        return

    table = client_table()
    print(f"{CLIENT.relative_to(CLIENT.parents[1])}: "
          + ", ".join(f"{k}={len(v)}" for k, v in table.items()) + " bone(s)\n")
    bad = 0
    for h, m in measured.values():
        side = side_of(h["label"])
        want = table.get(side) or []
        if len(want) != len(SEGMENTS):
            print(f"  ?    {h['label']:10s} the client has no {side} table of 24")
            bad += 1
            continue
        worst, at = 0.0, None
        for i, (a, b) in enumerate(SEGMENTS):
            got = m["lengths"][i]
            if got is None:
                continue
            d = abs(got - want[i])
            if d > worst:
                worst, at = d, b
        ok = worst < 5e-5                                   # 0.05 mm — the table is printed to 5 places
        bad += 0 if ok else 1
        print(f"  {'OK ' if ok else 'STALE'} {h['label']:10s} {h['id'][:12]}  "
              f"max |Δ| {worst * 1000:.3f} mm" + (f" at {at}" if at and not ok else ""))
    if bad:
        print(f"\n{bad} model(s) disagree with the client's BIND table — re-measure it, and re-read the\n"
              f"caveat in hands-fit.js: a ratio against a stale table is confidently wrong.")
    else:
        print("\nThe client's BIND table is what these files say.")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
