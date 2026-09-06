#!/usr/bin/env python3
"""Check every named pose against every rig — the authoring loop for `conjure/poses.py`.

    python scripts/pose_library.py                    # apply and check, no Blender
    python scripts/pose_library.py --render out/      # ...and a clay contact sheet to look at
    python scripts/pose_library.py --poses kneel,sit --render out/

*Propose → check the signature → render for one human glance → freeze.* The design called for a vision
model at the verify step; that model passes a figure with its head on backwards
(docs/backlogs/figures.md), so geometry does the checking and the render is for a person.

**A pose that fails its own signature on one rig is the finding, not a nuisance.** The whole claim of a
rig-independent vocabulary is that one authored pose works everywhere, and this is what tests it.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure.figures import (anatomical_axes, apply_pose, best_humanoid,   # noqa: E402
                             body_frame, bone_directions, clean_pose,
                             node_world_positions, resolve_pose, split_glb)
from conjure.importer import glb_bounds, vrm_humanoid                      # noqa: E402
from conjure.pose_corpus import CAST, check_predicates                     # noqa: E402
from conjure.poses import POSES, resolve                                   # noqa: E402

BLENDER = os.environ.get("BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender")
POSE_TEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pose_test.py")


def load(rig):
    if not os.path.exists(rig.path):
        return None
    doc, blob = split_glb(open(rig.path, "rb").read())
    if not doc:
        return None
    mapping = vrm_humanoid(doc) or (best_humanoid(doc, blob)[0] or {})
    if not mapping:
        return None
    bounds = glb_bounds(doc, blob)
    return {"rig": rig, "doc": doc, "map": mapping, "axes": anatomical_axes(doc, mapping),
            "frame": body_frame(doc, mapping),
            "height": (bounds[1][1] - bounds[0][1]) if bounds else 1.7}


def joints(subject, pose=None):
    doc = copy.deepcopy(subject["doc"])
    if pose:
        apply_pose(doc, subject["map"], pose)
    by = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
    pos = node_world_positions(doc)
    return ({b: pos[by[n]] for b, n in subject["map"].items() if n in by},
            bone_directions(doc, subject["map"]))


def settle(subject, before, after) -> float:
    """The lift the client would apply — the lowest mapped joint back to the height it had at rest.

    Recomputed here rather than trusted, because it is the number that decides whether a kneel looks
    like kneeling or like hovering, and it is measured on a real skeleton in about four lines.
    """
    if not before or not after:
        return 0.0
    return min(p[1] for p in before.values()) - min(p[1] for p in after.values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--poses", default="", help="comma-separated names (default: all)")
    ap.add_argument("--rigs", default="", help="comma-separated rig names (default: the whole cast)")
    ap.add_argument("--render", default="", help="write a clay contact sheet here")
    ap.add_argument("--size", type=int, default=512)
    args = ap.parse_args()

    wanted = [p for p in args.poses.split(",") if p]
    poses = [resolve(n) for n in wanted] if wanted else list(POSES)
    if any(p is None for p in poses):
        print(f"unknown pose(s): {[n for n in wanted if resolve(n) is None]}")
        return 2
    subjects = [s for s in (load(r) for r in CAST
                            if not args.rigs or r.name.lower() in args.rigs.lower().split(",")) if s]
    if not subjects:
        print("no rigs available")
        return 2

    names = [s["rig"].name for s in subjects]
    width = max(len(p.name) for p in poses)
    print(f"\n{len(poses)} pose(s) x {len(subjects)} rig(s)\n")
    print(f"{'pose':<{width}}  " + "  ".join(f"{n[:12]:<14}" for n in names))
    print("-" * (width + 2 + 16 * len(names)))
    bad = 0
    for pose in poses:
        row = []
        for s in subjects:
            clean, problem = clean_pose(pose.bones, s["axes"])
            if problem:
                row.append("REFUSED"); bad += 1
                print(f"  ! {pose.name} on {s['rig'].name}: {problem}")
                continue
            notes: list = []
            resolve_pose(s["axes"], clean, notes)
            before, _ = joints(s)
            after, dirs = joints(s, clean)
            fails = check_predicates(pose.signature, before, after, s["frame"], s["height"], dirs,
                                     f"pose {pose.name!r}")
            lift = settle(s, before, after)
            if fails:
                bad += 1
                row.append(f"FAIL")
                for f in fails:
                    print(f"  ! {pose.name} on {s['rig'].name}: {f}")
            else:
                row.append(f"ok {lift * 100:+.0f}cm" + ("*" if notes else ""))
            if args.render:
                out = os.path.join(args.render, s["rig"].name, pose.name)
                os.makedirs(out, exist_ok=True)
                subprocess.run([BLENDER, "--background", "--python", POSE_TEST, "--",
                                s["rig"].path, out, json.dumps(clean), str(args.size), "--clay"],
                               capture_output=True, text=True, timeout=600)
        print(f"{pose.name:<{width}}  " + "  ".join(f"{c:<14}" for c in row))
    print("-" * (width + 2 + 16 * len(names)))
    print("`ok +N cm` is the re-grounding lift the client applies; * = a joint limit engaged.")
    if args.render:
        print(f"renders under {args.render}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
