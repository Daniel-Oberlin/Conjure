"""A hand model you can WEAR: what a GLB must contain, and what import records about it.

A worn hand is a rigged model whose 25 bones are driven, every frame, by the headset's own hand
tracking. It is not a figure and it is not posable: there is no vocabulary to discover and nothing to
retarget, because **these files are already authored in the WebXR joint frame**. Their nodes are named
exactly the WebXR hand joint set — no exporter prefixes — and each bone lies along its own local −Z, so
the two indirections the whole of `specs/figures.md` rests on, *which node* and *which way*, collapse to
identity. A transform copy is exact rather than approximate.

That is a property of these particular files and not of hand models in general, which is why nothing
here infers. A hand named any other way gets no map and is refused with a reason
(`docs/plans/hands.md`, and `decisions.md` §31 for why conforming and contact were decided together).

**Ten of the 24 inter-joint distances are not bones**, and the difference cost an afternoon before it
was measured on device:

    wrist -> *-metacarpal   the offset between an ARBITRARY FRAME ORIGIN and the hand. Our model's
                            wrist node and the runtime's wrist pivot need not coincide, and the
                            discrepancy points a different way for each finger.
    *-distal -> *-tip       the WebXR tip sits at the fingertip SURFACE and is derived from the
                            runtime's own estimate of the finger; a model's tip bone is authored.

Neither carries information about how long a bone is. Measured on a Quest 3 against our own model, the
ratios spread by 36% over all 24 and by **5%** over the 14 that are bones — so `BONES` is what a scale
estimate is taken over, and `SEGMENTS` is only for reporting.
"""
from __future__ import annotations

import math
from typing import Optional

#: Bumped when anything below changes what a row would say. Rides on `FRAME_REV`, which is what the
#: catalog actually checks; this exists so a hand-only change is legible in a diff.
HAND_REV = 1

#: The 25 WebXR hand joints, in the spec's own order.
JOINTS = (
    "wrist",
    "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip",
    "index-finger-metacarpal", "index-finger-phalanx-proximal", "index-finger-phalanx-intermediate",
    "index-finger-phalanx-distal", "index-finger-tip",
    "middle-finger-metacarpal", "middle-finger-phalanx-proximal", "middle-finger-phalanx-intermediate",
    "middle-finger-phalanx-distal", "middle-finger-tip",
    "ring-finger-metacarpal", "ring-finger-phalanx-proximal", "ring-finger-phalanx-intermediate",
    "ring-finger-phalanx-distal", "ring-finger-tip",
    "pinky-finger-metacarpal", "pinky-finger-phalanx-proximal", "pinky-finger-phalanx-intermediate",
    "pinky-finger-phalanx-distal", "pinky-finger-tip",
)

CHAINS = (
    ("wrist", "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip"),
) + tuple(
    ("wrist", f"{f}-finger-metacarpal", f"{f}-finger-phalanx-proximal",
     f"{f}-finger-phalanx-intermediate", f"{f}-finger-phalanx-distal", f"{f}-finger-tip")
    for f in ("index", "middle", "ring", "pinky")
)

#: The 24 parent→child pairs, in chain order. Shared with `client/hands-fit.js`, which indexes its own
#: bind table the same way — so the two can be compared without either translating.
SEGMENTS = tuple((c[i], c[i + 1]) for c in CHAINS for i in range(len(c) - 1))


def group_of(segment: tuple[str, str]) -> str:
    """`wrist` (a frame offset), `tip` (a surface point) or `bone` — see the module docstring."""
    if segment[0] == "wrist":
        return "wrist"
    return "tip" if segment[1].endswith("-tip") else "bone"


GROUPS = tuple(group_of(s) for s in SEGMENTS)
#: The 14 that are bones. The only segments a scale estimate may be taken over.
BONES = tuple(i for i, g in enumerate(GROUPS) if g == "bone")

#: `dir·(−Z)` a bone must reach to count as authored in the WebXR frame. Measured on the catalog's two
#: skeletons: **≥ 0.986** down every finger. The gate is loose because it is not trying to grade a rig,
#: only to catch one authored along a different axis — which would read near zero or negative.
#:
#: **Applied to the 19 non-wrist segments only.** At the wrist the same figure is 0.706–0.975, because
#: five bones leave one frame and it cannot point along all of them; a flat threshold there would refuse
#: every hand we have. That is also exactly why the WebXR spec leaves the wrist loose ("SHOULD point
#: roughly towards the centre of the palm") and why nothing is asserted about it.
AXIS_MIN = 0.90


def _unit(v):
    n = math.sqrt(sum(c * c for c in v))
    return [c / n for c in v] if n else [0.0, 0.0, 0.0]


def _sub(a, b):
    return [a[i] - b[i] for i in range(3)]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def hand_joints(doc: dict) -> dict[str, str]:
    """`{webxrJointName: nodeName}`, or `{}` when this file is not a WebXR-named hand.

    An identity map today, and written out anyway. The moment a second naming scheme appears this is
    the one place that changes, and every consumer already reads a map rather than a convention — which
    is the lesson `specs/figures.md` §3 paid for.
    """
    names = {}
    for i, node in enumerate(doc.get("nodes") or []):
        n = node.get("name")
        if n in JOINTS and n not in names:
            names[n] = n
    return names if len(names) == len(JOINTS) else {}


def bind_lengths(doc: dict, joints: dict[str, str]) -> list[Optional[float]]:
    """The 24 inter-joint distances in the bind pose, in metres, in `SEGMENTS` order."""
    from .figures import node_world_positions
    pos = node_world_positions(doc)
    index = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
    out: list[Optional[float]] = []
    for a, b in SEGMENTS:
        ia, ib = index.get(joints.get(a, "")), index.get(joints.get(b, ""))
        out.append(math.dist(pos[ia], pos[ib]) if ia is not None and ib is not None else None)
    return out


def hand_side(doc: dict, joints: dict[str, str]) -> str:
    """`"left"` or `"right"`, MEASURED — never read from the `_L` in a filename.

    From the chirality of the hand itself, using positions only so that no exporter's idea of which way
    a node faces can change the answer: the sign of

        (wrist → middle-metacarpal) × (index-metacarpal → pinky-metacarpal) · (wrist → thumb-metacarpal)

    Negative is a left hand. Measured on all five catalog hands it comes out −0.298 or +0.283, so the
    two populations are separated by a margin of about 0.28 and a near-zero result means something is
    wrong with the file rather than that it is ambiguous.
    """
    from .figures import node_world_positions
    pos = node_world_positions(doc)
    index = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}

    def at(name):
        i = index.get(joints.get(name, ""))
        return pos[i] if i is not None else None

    w, mid = at("wrist"), at("middle-finger-metacarpal")
    idx, pky, thb = at("index-finger-metacarpal"), at("pinky-finger-metacarpal"), at("thumb-metacarpal")
    if not all((w, mid, idx, pky, thb)):
        return ""
    s = _dot(_cross(_unit(_sub(mid, w)), _unit(_sub(pky, idx))), _unit(_sub(thb, w)))
    if abs(s) < 0.05:
        return ""                     # degenerate: a flat or malformed hand, not an ambiguous one
    return "left" if s < 0 else "right"


def check(doc: dict, joints: dict[str, str]) -> list[str]:
    """What is wrong with this hand, as sentences. Empty means it can be worn.

    Deliberately NOT a parent→child chain test, which is the shape the plan originally called for.
    Measured: all 25 joints in these files are SIBLINGS under a single `hand_L`/`hand_R` node, each with
    a Blender `<name>_end` tail beside it. The chain exists in the skin's joint list and in the
    geometry, not in the node tree, so that check would have refused the very files it was written for.
    """
    from .figures import node_world_matrices, node_world_positions
    problems: list[str] = []
    missing = [j for j in JOINTS if j not in joints]
    if missing:
        problems.append(f"{len(missing)} of {len(JOINTS)} WebXR joints absent: "
                        f"{', '.join(missing[:6])}{' …' if len(missing) > 6 else ''}")
        return problems                       # nothing below is meaningful without the joints

    index = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
    mats, pos = node_world_matrices(doc), node_world_positions(doc)
    off = []
    for k, (a, b) in enumerate(SEGMENTS):
        if GROUPS[k] == "wrist":
            continue                          # five bones leave one frame; see AXIS_MIN
        ia, ib = index[joints[a]], index[joints[b]]
        d = _unit(_sub(pos[ib], pos[ia]))
        m = mats[ia]
        neg_z = _unit([-m[8], -m[9], -m[10]])
        if _dot(d, neg_z) < AXIS_MIN:
            off.append(f"{b} {_dot(d, neg_z):+.2f}")
    if off:
        problems.append(f"{len(off)} bone(s) do not lie along their own local −Z, so a transform copy "
                        f"would not be exact: {', '.join(off[:5])}{' …' if len(off) > 5 else ''}")
    if not hand_side(doc, joints):
        problems.append("which hand this is cannot be measured — the metacarpals are collinear or "
                        "coincident, so the file is malformed rather than ambiguous")
    return problems


def describe(doc: dict) -> dict:
    """The catalog attributes for a hand model, or `{}` when this is not one.

    `hand_problems` is recorded rather than swallowed, for the same reason `parts_unclassified` is: a
    file that ALMOST qualifies is the interesting one, and a silent `{}` makes "not a hand" and "a hand
    we rejected" indistinguishable — which is the shape of error this repo keeps paying for.
    """
    joints = hand_joints(doc)
    if not joints:
        return {}
    problems = check(doc, joints)
    out = {
        "hand_joints": joints,
        "hand_side": hand_side(doc, joints),
        "hand_rev": HAND_REV,
        "hand_wearable": not problems,
    }
    if problems:
        out["hand_problems"] = problems
    return out
