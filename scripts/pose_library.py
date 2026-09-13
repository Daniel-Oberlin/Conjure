#!/usr/bin/env python3
"""Check every named pose against every rig — the authoring loop for `conjure/poses.py`.

    python scripts/pose_library.py                    # apply and check, no Blender
    python scripts/pose_library.py --render out/      # ...and a clay contact sheet to look at
    python scripts/pose_library.py --poses kneel,sit --render out/
    python scripts/pose_library.py --identify           # + the visual check, calibrated first

*Propose → check the signature → have a model say which pose it sees → glance → freeze.*

Two verifiers, and they fail differently. The **signature** is geometry: deterministic, free, and what
actually fails a pose. **`--identify`** renders the pose and asks a vision model which of the library it
is looking at — recognition, never "is this pose any good", which is a judgement and does not work (that
model passes a figure with its head on backwards). Identification is advisory, because it confuses poses
that genuinely resemble one another; it earns its place by catching what a signature structurally
cannot, since a signature only asserts the bones a pose SETS and is blind to the bones a pose forgets.

**A pose that fails its own signature on one rig is the finding, not a nuisance.** The whole claim of a
rig-independent vocabulary is that one authored pose works everywhere, and this is what tests it.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure.figures import (anatomical_axes, apply_pose, best_humanoid,   # noqa: E402
                             body_frame, body_profile, bone_directions, clean_pose, limb_radius,
                             node_world_positions, resolve_pose, split_glb)
from conjure.importer import glb_bounds, vrm_humanoid                      # noqa: E402
from conjure.judge import build_judge                                      # noqa: E402
from conjure.config import get_settings                                    # noqa: E402
from conjure.pose_corpus import CAST, check_predicates                     # noqa: E402
from conjure.poses import NONE_OF_THESE, POSES, WRONG_KNEEL, resolve       # noqa: E402

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
    # The mesh measurement, taken once per rig: how wide the torso is at each height, and how thick each
    # limb that has to clear it. This is what the `clears` predicate consults, and the only thing in the
    # pipeline that looks at a vertex.
    mesh = {"profile": body_profile(doc, blob, mapping),
            "radii": {b: limb_radius(doc, blob, mapping, b)
                      for b in ("leftLowerArm", "rightLowerArm", "leftHand", "rightHand")}}
    return {"rig": rig, "doc": doc, "map": mapping, "axes": anatomical_axes(doc, mapping),
            "frame": body_frame(doc, mapping), "mesh": mesh,
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


IDENTIFY = ("These three images are the same 3-D character in one pose, seen from the front, from her "
            "front-left, and from her left side. Which of these poses is she in?")


def views(outdir: str):
    names = ("posed.png", "posed_q.png", "posed_side.png")
    labels = ("from the front", "from her front-left", "from her left side")
    paths = [os.path.join(outdir, n) for n in names]
    if not all(os.path.exists(p) for p in paths):
        return None
    return [(lab, open(p, "rb").read()) for lab, p in zip(labels, paths)]


def render(subject, pose: dict, outdir: str, size: int) -> bool:
    os.makedirs(outdir, exist_ok=True)
    if not os.path.exists(BLENDER):
        return False
    subprocess.run([BLENDER, "--background", "--python", POSE_TEST, "--",
                    subject["rig"].path, outdir, json.dumps(pose), str(size), "--clay"],
                   capture_output=True, text=True, timeout=600)
    return views(outdir) is not None


async def identify(judge, shots, options) -> str:
    """Which pose the judge thinks it is looking at, by name — or `NONE_OF_THESE`.

    **Recognition, not judgement.** Asking a vision model whether a pose is anatomically possible does
    not work: it passes a figure with its head on backwards, and its verdicts move between identical
    calls (docs/backlogs/figures.md). Asking it WHICH of the named poses this is turns the same
    model into a reliable instrument, because recognition against a fixed list is what it is good at —
    which is the discipline the design stated all along and the plausibility question quietly broke.
    """
    verdict = await judge.choose(shots, IDENTIFY, options)
    return options[verdict.choice].split(" —")[0] if verdict.answered else "no answer"


async def calibrate(judge, subject, workdir: str, size: int, options) -> bool:
    """Show it the WRONG kneel and require it to decline. Same discipline as the eval harness, and this
    time the instrument passes: an identifier that rounds a bad pose to the nearest good one would
    certify exactly the mistakes this loop exists to catch."""
    out = os.path.join(workdir, "_calibration")
    if not render(subject, WRONG_KNEEL, out, size):
        return False
    return await identify(judge, views(out), options) != "kneel"


async def run(args) -> int:
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

    judge = build_judge(get_settings()) if args.identify else None
    options = [f"{p.name} — {p.about.split(' (')[0]}" for p in POSES] + [NONE_OF_THESE]
    workdir = args.render or tempfile.mkdtemp(prefix="pose-library-")
    trusted = False
    if judge:
        trusted = await calibrate(judge, subjects[0], workdir, args.size, options)
        print(f"\n  judge calibration: {'PASSED' if trusted else 'FAILED'} — shown the wrong kneel, "
              f"it {'declined it' if trusted else 'CALLED IT A KNEEL'}")
        if not trusted:
            print("  identification is reported but does not fail a pose")

    names = [s["rig"].name for s in subjects]
    width = max(len(p.name) for p in poses)
    print(f"\n{len(poses)} pose(s) x {len(subjects)} rig(s)\n")
    print(f"{'pose':<{width}}  " + "  ".join(f"{n[:12]:<14}" for n in names))
    print("-" * (width + 2 + 16 * len(names)))
    bad = seen = 0
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
                                     f"pose {pose.name!r}", s["mesh"])
            lift = settle(s, before, after)
            if fails:
                bad += 1
                row.append(f"FAIL")
                for f in fails:
                    print(f"  ! {pose.name} on {s['rig'].name}: {f}")
            else:
                row.append(f"ok {lift * 100:+.0f}cm" + ("*" if notes else ""))
            if args.render or judge:
                out = os.path.join(workdir, s["rig"].name, pose.name)
                shot = render(s, clean, out, args.size)
                if judge and shot:
                    # The visual half, automated. It is asked WHICH pose this is, never whether it is a
                    # good one — and it catches what a signature cannot: both the wrong kneel and the
                    # wrong arms-crossed passed their first signatures and are declined here.
                    saw = await identify(judge, views(out), options)
                    if saw != pose.name:
                        seen += 1
                        print(f"  ~ {pose.name} on {s['rig'].name}: the judge sees {saw!r}")
        print(f"{pose.name:<{width}}  " + "  ".join(f"{c:<14}" for c in row))
    print("-" * (width + 2 + 16 * len(names)))
    print("`ok +N cm` is the re-grounding lift the client applies; * = a joint limit engaged.")
    if judge:
        # ADVISORY, and the reason is measured. Identification found two real defects no signature
        # caught — leg poses and one-armed poses both left the idle limbs at the rig's bind pose, which
        # on a T-posed VRoid rig meant kneeling like a scarecrow. It also confuses poses that genuinely
        # resemble each other (a deep crouch reads as a sit) and is least steady on the stylised rig. A
        # good finder, then, and a poor gate: the `~` lines are for a person to look at, and what fails
        # a pose is its signature.
        print(f"{seen} cell(s) the judge read as a different pose — advisory, and worth a look; "
              f"what fails a pose is its signature.")
    if args.render:
        print(f"renders under {args.render}")
    elif judge:
        shutil.rmtree(workdir, ignore_errors=True)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--poses", default="", help="comma-separated names (default: all)")
    ap.add_argument("--rigs", default="", help="comma-separated rig names (default: the whole cast)")
    ap.add_argument("--render", default="", help="write a clay contact sheet here")
    ap.add_argument("--identify", action="store_true",
                    help="also RENDER each pose and ask a vision model which one it is — the visual "
                         "half of the loop, automated. Needs Blender and a judge key")
    ap.add_argument("--size", type=int, default=512)
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
