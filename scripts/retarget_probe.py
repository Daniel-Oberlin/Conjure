#!/usr/bin/env python3
"""How wrong is a clip played on a rig it was not authored for? (plan § phase 5)

    python scripts/retarget_probe.py                      # every target rig, one clip
    python scripts/retarget_probe.py --clip 3_idle --per-bone

Phase 5's premise is that mapping two skeletons through the canonical humanoid is the easy half and
correcting for their difference in BIND POSE is the hard one. This measures the second claim rather than
assuming it: the correction is only worth building if the number is large, and if it is large the same
number is what says whether a correction worked.

**Two different costs, and the first version of this conflated them.**

  1. CHANNEL LOSS. The humanoid vocabulary is 22 bones and Jane's clip drives 222, so 200 channels
     target a bone the map does not name — skirt, breast and secondary chains. Nothing can carry those
     across rigs, because the target has no bone to receive them. Reported separately, once.
  2. REST DIFFERENCE. Of the channels that DO map, a bone's local rotation means whatever its rest
     orientation makes it mean, so copying it onto a rig with a different bind pose is wrong by the
     difference between the two rests. This is the number phase 5 exists to reduce.

The source is driven by EVERY channel the clip carries, because that is what the clip actually does. An
earlier version posed it with the 22 mapped channels only, to make the identity case read exactly 0.0° —
and that number was a fiction. It also hid a real bug for three rounds: a bone the humanoid does not
name can be a mapped bone's ANCESTOR, and Alice's `CC_Base_BoneRoot` rotates 36.3° while the hip under
it rotates 38.6° the other way. They nearly cancel and she SLIDES. Posing the source without the root
reported the full 38.6° as real on both sides, so the probe agreed with a retarget that had the same
blind spot, and the retargeted figure swung bodily about her own axis.

So **`Jane → Jane` is the FLOOR, not zero**: it is what channel loss alone costs, with no rig
difference at all. What a target's number means is its excess over that floor.

**The measurement.** For bone `b` at time `t`:

    Mo_b(t) = Ws_b(t) · Rs_b⁻¹        the motion, in WORLD terms, as a delta from the source's rest
    want    = Mo_b(t) · Rt_b          the same motion applied to the target's rest
    naive   = Wt_b(t) with the source's LOCAL rotation copied onto the target's bone

and the error is the angle between `want` and `naive`, in degrees. Median and worst over every mapped
bone and every sampled frame — a mean hides the one shoulder that is inside out.

Only ROTATION is compared. A difference in limb LENGTH is a difference in proportion rather than in
retargeting, and it is not this number's business.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import struct
import sys
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure.figures import (CORE_BONES, _local_matrix, best_humanoid,       # noqa: E402
                             node_world_matrices, parent_map, split_glb)

CACHE = os.path.expanduser("~/.local/share/conjure/assets")
DB = os.path.expanduser("~/.local/share/conjure/library.db")

IDENT = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


# ---------------------------------------------------------------- quaternion / matrix algebra

def quat_of(m: list[float]) -> tuple[float, float, float, float]:
    """Rotation of a column-major 4x4 as `(x, y, z, w)`, with the scale divided out.

    Divided out rather than ignored: several of these rigs carry a unit conversion on the armature
    (Alice's 0.01), and a matrix→quaternion that trusts the diagonal reads that as a rotation.
    """
    cols = [(m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10])]
    lens = [math.sqrt(sum(v * v for v in c)) or 1.0 for c in cols]
    r = [[cols[c][row] / lens[c] for c in range(3)] for row in range(3)]
    tr = r[0][0] + r[1][1] + r[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return ((r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s, 0.25 * s)
    if r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        return (0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s, (r[2][1] - r[1][2]) / s)
    if r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        return ((r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s, (r[0][2] - r[2][0]) / s)
    s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
    return ((r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s, (r[1][0] - r[0][1]) / s)


def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def qconj(q):
    return (-q[0], -q[1], -q[2], q[3])


def angle_between(a, b) -> float:
    """Degrees from `a` to `b`, the short way round."""
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, abs(qmul(b, qconj(a))[3])))))


def qmat(q, scale=(1.0, 1.0, 1.0), t=(0.0, 0.0, 0.0)) -> list[float]:
    x, y, z, w = q
    sx, sy, sz = scale
    return [(1 - 2 * (y * y + z * z)) * sx, (2 * (x * y + z * w)) * sx, (2 * (x * z - y * w)) * sx, 0,
            (2 * (x * y - z * w)) * sy, (1 - 2 * (x * x + z * z)) * sy, (2 * (y * z + x * w)) * sy, 0,
            (2 * (x * z + y * w)) * sz, (2 * (y * z - x * w)) * sz, (1 - 2 * (x * x + y * y)) * sz, 0,
            t[0], t[1], t[2], 1]


def mmul(a, b):
    out = [0.0] * 16
    for c in range(4):
        for r in range(4):
            out[c * 4 + r] = sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
    return out


# ---------------------------------------------------------------- reading glTF

def accessor(doc, blob, idx) -> list:
    """One accessor, as a list of scalars or tuples. Enough glTF for rotation tracks and their times."""
    acc = doc["accessors"][idx]
    view = doc["bufferViews"][acc["bufferView"]]
    size = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}[acc["type"]]
    fmt = {5126: "f", 5123: "H", 5121: "B", 5122: "h", 5120: "b"}[acc["componentType"]]
    width = struct.calcsize(fmt)
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = acc["count"] * size
    vals = list(struct.unpack_from("<" + fmt * n, blob, start))
    if fmt != "f":                                    # normalised integer quaternions are legal glTF
        top = float((1 << (8 * width - 1)) - 1) if fmt in "hb" else float((1 << (8 * width)) - 1)
        vals = [max(-1.0, v / top) for v in vals]
    return [tuple(vals[i:i + size]) for i in range(0, len(vals), size)] if size > 1 else vals


@dataclass
class Figure:
    """A rigged model, with everything the probe asks of it read once."""

    label: str
    doc: dict
    mapping: dict          # humanoid bone -> node name
    source: str            # how the map was recovered
    by_name: dict          # node name -> index
    parent: dict
    bind: dict             # node index -> bind-pose world matrix

    def rest(self, bone: str):
        return quat_of(self.bind[self.by_name[self.mapping[bone]]])


def read_figure(label: str, aid: str) -> Figure:
    doc, blob = split_glb(open(os.path.join(CACHE, aid), "rb").read())
    mapping, source, _f = best_humanoid(doc, blob)
    by_name = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
    return Figure(label, doc, mapping or {}, source or "-", by_name, parent_map(doc),
                  node_world_matrices(doc))


def rotations_at(doc, blob, name: str, frac: float) -> dict[str, tuple]:
    """`{node name: quaternion}` for every rotation channel, at `frac` through the clip."""
    anims = doc.get("animations") or []
    anim = next((a for a in anims if (a.get("name") or "") == name), anims[0] if anims else None)
    if anim is None:
        return {}
    out = {}
    for ch in anim["channels"]:
        if ch["target"]["path"] != "rotation":
            continue
        node = doc["nodes"][ch["target"]["node"]].get("name")
        if not node:
            continue
        sam = anim["samplers"][ch["sampler"]]
        times, quats = accessor(doc, blob, sam["input"]), accessor(doc, blob, sam["output"])
        if not times or not quats:
            continue
        want = times[0] + (times[-1] - times[0]) * frac
        i = min(range(len(times)), key=lambda k: abs(times[k] - want))
        out[node] = quats[min(i, len(quats) - 1)]
    return out


def posed_matrices(fig: Figure, rots: dict) -> dict[str, list[float]]:
    """World MATRIX of every mapped bone with `rots` substituted as local rotations.

    Walks bone → root composing each ancestor's local matrix, so a bone inherits the clip's motion
    THROUGH its parents. That inheritance is most of what makes this different from comparing locals,
    and it is why an unmapped bone in the middle of a chain costs more than its own channel.
    """
    nodes = fig.doc["nodes"]
    out = {}
    for bone, node_name in fig.mapping.items():
        idx = fig.by_name.get(node_name)
        if idx is None:
            continue
        chain, j = [], idx
        while j is not None:
            chain.append(j)
            j = fig.parent.get(j)
        m = IDENT
        for j in reversed(chain):
            nd = nodes[j]
            nm = nd.get("name")
            if nm in rots:
                m = mmul(m, qmat(rots[nm], nd.get("scale") or (1.0, 1.0, 1.0),
                                 nd.get("translation") or (0.0, 0.0, 0.0)))
            else:
                m = mmul(m, _local_matrix(nd))
        out[bone] = m
    return out


def posed_world(fig: Figure, rots: dict) -> dict[str, tuple]:
    return {b: quat_of(m) for b, m in posed_matrices(fig, rots).items()}


#: The humanoid skeleton as parent → child, for reading a limb's DIRECTION. Only pairs that are
#: actually adjacent; `chest → shoulder` is the one branch with two children.
LIMBS = (("hips", "spine"), ("spine", "chest"), ("chest", "neck"), ("neck", "head"),
         ("chest", "leftShoulder"), ("leftShoulder", "leftUpperArm"),
         ("leftUpperArm", "leftLowerArm"), ("leftLowerArm", "leftHand"),
         ("chest", "rightShoulder"), ("rightShoulder", "rightUpperArm"),
         ("rightUpperArm", "rightLowerArm"), ("rightLowerArm", "rightHand"),
         ("hips", "leftUpperLeg"), ("leftUpperLeg", "leftLowerLeg"),
         ("leftLowerLeg", "leftFoot"), ("leftFoot", "leftToes"),
         ("hips", "rightUpperLeg"), ("rightUpperLeg", "rightLowerLeg"),
         ("rightLowerLeg", "rightFoot"), ("rightFoot", "rightToes"))


def _norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def body_axes(mats: dict):
    """An orthonormal frame from the figure's OWN posed body: up hips→neck, right leg→leg.

    Directions are expressed in this rather than in world space so that a figure facing a different way,
    or a clip that turns the whole body, is not counted as retargeting error. What is left is the POSE.
    """
    need = ("hips", "neck", "leftUpperLeg", "rightUpperLeg")
    if any(b not in mats for b in need):
        return None
    pos = {b: (mats[b][12], mats[b][13], mats[b][14]) for b in need}
    up = _norm(tuple(pos["neck"][i] - pos["hips"][i] for i in range(3)))
    side = _norm(tuple(pos["leftUpperLeg"][i] - pos["rightUpperLeg"][i] for i in range(3)))
    fwd = _norm(_cross(side, up))
    side = _norm(_cross(up, fwd))                       # re-orthogonalise; the hips are not square
    return (side, up, fwd)


def limb_dirs(mats: dict):
    """`{(parent, child): unit direction in the body frame}` — what a limb is POINTING at.

    Scale-free and convention-free, which is the point: it can judge a retarget that a rotation
    comparison can only describe. Two figures of different proportions holding the same pose agree here.
    """
    axes = body_axes(mats)
    if not axes:
        return {}
    out = {}
    for a, b in LIMBS:
        if a not in mats or b not in mats:
            continue
        d = tuple(mats[b][12 + i] - mats[a][12 + i] for i in range(3))
        if sum(c * c for c in d) < 1e-12:
            continue
        d = _norm(d)
        out[(a, b)] = tuple(sum(d[i] * axis[i] for i in range(3)) for axis in axes)
    return out


#: Limbs whose direction is WHERE A JOINT IS ATTACHED rather than how it is posed. Shoulder width and
#: leg splay are build, not pose: no retargeting can change them and none should try. Measured at rest
#: with no clip at all, Trish's `chest → leftShoulder` is already 152° from Jane's and every rig's
#: `hips → upperLeg` is 8–67° out. Counting that as retargeting error made four rigs look broken.
ATTACHMENT = {("chest", "leftShoulder"), ("chest", "rightShoulder"),
              ("hips", "leftUpperLeg"), ("hips", "rightUpperLeg")}


def dir_error(a: dict, b: dict, attachments: bool = False) -> list[float]:
    """Degrees between corresponding limb directions, over the ARTICULATED limbs by default."""
    out = []
    for k in a:
        if k not in b or ((k in ATTACHMENT) != attachments):
            continue
        dot = max(-1.0, min(1.0, sum(a[k][i] * b[k][i] for i in range(3))))
        out.append(math.degrees(math.acos(dot)))
    return out


# ---------------------------------------------------------------- the probe

#: Which mapped bone each bone's length points AT, for building a convention-free rest frame.
CHILD_OF = {}
for _p, _c in LIMBS:
    CHILD_OF.setdefault(_p, _c)
CHILD_OF["leftHand"] = None       # the ends of the chains have no limb to point along; they
CHILD_OF["rightHand"] = None      # inherit their parent's direction (see `canonical_rest`)
CHILD_OF["leftToes"] = None
CHILD_OF["rightToes"] = None
CHILD_OF["head"] = None

PARENT_OF = {c: p for p, c in LIMBS}

#: Which body axis squares up each bone's frame. Body FORWARD for almost everything, because almost
#: every humanoid limb points up, down or sideways; body UP only for the feet, whose limb IS forward.
#: A fixed table rather than a measured one — see `canonical_rest`.
REF_AGAINST_UP = {"leftFoot", "rightFoot", "leftToes", "rightToes"}


def chain_breaks(fig: Figure) -> list[str]:
    """Humanoid links whose child is NOT actually under its parent in the skeleton.

    A map can pass every geometric check `validate()` makes — bones in the right places, sides not
    swapped, limbs ordered — and still name bones from different BRANCHES of a control rig. Eve's
    inferred map puts `hips` on `ORG-spine` (under `MCH-spine`) and `spine` on `chest` (under `torso`),
    so rotating her hips cannot move her spine: her torso stays behind while her pelvis turns.

    It matters for retargeting in a way it does not for posing one bone at a time, which is why it has
    gone unnoticed. 6 of 28 mapped figures have at least one break, and all of them are the outliers:
    Eve 3, Steve and Shaun 2 each (`hips → upperLeg`), Trish and Yuffie 1 (`spine → chest`).
    """
    out = []
    for parent, child in LIMBS:
        pn, cn = fig.mapping.get(parent), fig.mapping.get(child)
        if not pn or not cn:
            continue
        pi, ci = fig.by_name.get(pn), fig.by_name.get(cn)
        if pi is None or ci is None:
            continue
        j, ok = fig.parent.get(ci), False
        while j is not None:
            if j == pi:
                ok = True
                break
            j = fig.parent.get(j)
        if not ok:
            out.append(f"{parent}->{child}")
    return out


def _frame_q(mats: dict):
    """The body frame of a posed skeleton, as a quaternion. `None` if the bones it needs are absent."""
    axes = body_axes(mats)
    if not axes:
        return None
    side, up, fwd = axes
    return quat_of([side[0], side[1], side[2], 0, up[0], up[1], up[2], 0,
                    fwd[0], fwd[1], fwd[2], 0, 0, 0, 0, 1])


def rest_body_frame(fig: Figure) -> tuple:
    """The figure's own orientation at rest, as a quaternion — up the spine, across the hips."""
    q = _frame_q({b: fig.bind[fig.by_name[n]] for b, n in fig.mapping.items() if n in fig.by_name})
    return q or (0.0, 0.0, 0.0, 1.0)


def canonical_rest(fig: Figure, notes: Optional[list] = None) -> dict[str, tuple]:
    """A convention-free rest orientation per bone: where its limb POINTS, not how its frame is spelled.

    This is what separates the two ways rigs differ. A bone's authored rest `R` mixes together where the
    limb physically is and which axis the exporter chose to call "along the bone" — and only the second
    should be divided out when moving a clip between rigs. So build a frame from things that are
    physical: the direction to the next joint, squared up against the body's own up.

    `R = C · K` then defines the CONVENTION `K = C⁻¹ · R`, and a retarget that divides out the source's
    K and multiplies in the target's carries the pose absolutely rather than as a delta from two
    different starting points — which is the failure the delta correction cannot avoid.
    """
    notes = [] if notes is None else notes
    mats = fig.bind
    axes = body_axes({b: mats[fig.by_name[n]] for b, n in fig.mapping.items() if n in fig.by_name})
    if not axes:
        return {}
    side, up, fwd_body = axes
    pos = {}
    for bone, node in fig.mapping.items():
        if node in fig.by_name:
            m = mats[fig.by_name[node]]
            pos[bone] = (m[12], m[13], m[14])
    out = {}
    for bone in pos:
        child = CHILD_OF.get(bone)
        a, b = bone, child
        if not child or child not in pos:                  # a chain end continues its parent's line
            parent = PARENT_OF.get(bone)
            if parent and parent in pos:
                a, b = parent, bone
            else:
                a = b = None
        if a and b and a in pos and b in pos:
            d = tuple(pos[b][i] - pos[a][i] for i in range(3))
        else:
            d = up
        if sum(c * c for c in d) < 1e-12:
            d = up
        along = _norm(d)
        # THE REFERENCE IS CHOSEN BY THE BONE, NEVER BY MEASUREMENT. Picking it with
        # `abs(dot(along, up)) < 0.99` is the obvious thing and it is a trap: Jane's hips→spine reads
        # 0.9684 and office-babe's reads 0.9975, so two rigs in the same rest pose fall on opposite
        # sides of the threshold, take different branches, and end up with frames a half-turn apart.
        # That showed up as 179.8° on both shoulders — a flip, not a drift. A bone's rough direction
        # is known from its NAME, and a table gives every rig the same answer.
        ref = up if bone in REF_AGAINST_UP else fwd_body
        cosine = abs(sum(along[i] * ref[i] for i in range(3)))
        if cosine > 0.94:                                              # ~20° — say so, never guess
            off = math.degrees(math.acos(min(1.0, cosine)))
            notes.append(f"{bone}: limb is only {off:.0f}° from its reference axis, so its roll is "
                         f"poorly conditioned")
        right = _norm(_cross(ref, along))
        upper = _cross(along, right)
        out[bone] = quat_of([right[0], right[1], right[2], 0,
                             upper[0], upper[1], upper[2], 0,
                             along[0], along[1], along[2], 0, 0, 0, 0, 1])
    return out


def _world_rotations(src: Figure, dst: Figure, carried: dict, absolute: bool) -> dict[str, tuple]:
    """Desired WORLD rotation per target node name, under one of the two retargeting laws.

    `absolute=False` is the delta law the plan assumed: preserve each bone's rotation relative to its
    own rest. `absolute=True` divides out each rig's convention instead, so the pose is carried whole
    and a difference in REST POSE is not silently added to it.
    """
    s_world = posed_world(src, {src.mapping[b]: q for b, q in carried.items()})
    cs, ct = (canonical_rest(src), canonical_rest(dst)) if absolute else ({}, {})
    # INTO THE TARGET'S OWN FRAME. Matching world orientations is not the same as matching poses: a
    # figure whose armature rests with a slight lean should perform the clip in ITS frame rather than
    # inherit the source's. The naive copy gets this for free by working in each rig's own local terms;
    # an absolute law has to be told. `Bt · Bs⁻¹` is the whole of it, and it is identity whenever the
    # two rest the same way — so the control and the identity case are untouched.
    swing = qmul(rest_body_frame(dst), qconj(rest_body_frame(src))) if absolute else (0, 0, 0, 1)
    want = {}
    for bone in carried:
        if bone not in s_world:
            continue
        if absolute:
            if bone not in cs or bone not in ct:
                continue
            k_s = qmul(qconj(cs[bone]), src.rest(bone))    # source convention: canonical -> authored
            k_t = qmul(qconj(ct[bone]), dst.rest(bone))
            here = qmul(swing, s_world[bone])          # the source's orientation, in the target's frame
            want[dst.mapping[bone]] = qmul(qmul(here, qconj(k_s)), k_t)
        else:
            motion = qmul(s_world[bone], qconj(src.rest(bone)))
            want[dst.mapping[bone]] = qmul(motion, dst.rest(bone))
    return want


def _locals_for(dst: Figure, want: dict) -> dict:
    """Local rotations that put each named node at its desired WORLD rotation. Top-down, so a bone is
    measured against where its parent ACTUALLY IS once the clip has moved it."""
    nodes = dst.doc["nodes"]
    scenes = dst.doc.get("scenes") or []
    roots = scenes[dst.doc.get("scene", 0)].get("nodes", []) if scenes else range(len(nodes))
    out: dict[str, tuple] = {}
    stack = [(int(r), IDENT) for r in roots]
    seen = set()
    while stack:
        idx, parent_world = stack.pop()
        if idx in seen or idx >= len(nodes):
            continue
        seen.add(idx)
        nd = nodes[idx]
        name = nd.get("name")
        if name in want:
            local_q = qmul(qconj(quat_of(parent_world)), want[name])
            out[name] = local_q
            local = qmat(local_q, nd.get("scale") or (1.0, 1.0, 1.0),
                         nd.get("translation") or (0.0, 0.0, 0.0))
        else:
            local = _local_matrix(nd)
        world = mmul(parent_world, local)
        for child in nd.get("children") or []:
            stack.append((int(child), world))
    return out


def absolute_locals(src: Figure, dst: Figure, carried: dict) -> dict:
    """Retarget by carrying the pose ABSOLUTELY, with each rig's axis convention divided out."""
    return _locals_for(dst, _world_rotations(src, dst, carried, absolute=True))


def corrected_locals(src: Figure, dst: Figure, carried: dict) -> dict:
    """The REST-CORRECTED local rotations for the target, from the source's local rotations.

    The correction phase 5 is about. A local rotation means whatever its bone's rest orientation makes
    it mean, so it cannot be copied between rigs whose rests differ. Take it out to world against the
    source's rest, put it back through the target's:

        want_world(b) = (world_s(b) · rest_s(b)⁻¹) · rest_t(b)
        local_t(b)    = posed_world(parent of b)⁻¹ · want_world(b)

    **The parent is the POSED one, not the rest one**, which is what makes this a top-down walk rather
    than a per-bone formula. Using the rest parent is the obvious first attempt and it fails the
    identity case at 32° — a clip on its own rig has to come back exactly unchanged, and that is the
    only cheap test this correction has.

    The walk also has to cross UNMAPPED bones. The humanoid names 22 and these rigs carry 100–220, so
    between `chest` and `leftShoulder` there may be several intermediate bones; they keep their rest
    local and still contribute to where the child ends up.
    """
    s_world = posed_world(src, {src.mapping[b]: q for b, q in carried.items()})
    want = {}
    for bone in carried:
        if bone not in s_world:
            continue
        motion = qmul(s_world[bone], qconj(src.rest(bone)))
        want[dst.mapping[bone]] = qmul(motion, dst.rest(bone))

    nodes = dst.doc["nodes"]
    scenes = dst.doc.get("scenes") or []
    roots = scenes[dst.doc.get("scene", 0)].get("nodes", []) if scenes else range(len(nodes))
    out: dict[str, tuple] = {}
    stack = [(int(r), IDENT) for r in roots]
    seen = set()
    while stack:
        idx, parent_world = stack.pop()
        if idx in seen or idx >= len(nodes):
            continue
        seen.add(idx)
        nd = nodes[idx]
        name = nd.get("name")
        if name in want:
            local_q = qmul(qconj(quat_of(parent_world)), want[name])
            out[name] = local_q
            local = qmat(local_q, nd.get("scale") or (1.0, 1.0, 1.0),
                         nd.get("translation") or (0.0, 0.0, 0.0))
        else:
            local = _local_matrix(nd)
        world = mmul(parent_world, local)
        for child in nd.get("children") or []:
            stack.append((int(child), world))
    return out


def rolled_copy(fig: Figure, degrees: float, only: Optional[set] = None) -> Figure:
    """The same skeleton with every bone's REST ROLLED, and its geometry untouched. A control.

    The identity case (`Jane → Jane`) only tests the correction where the two rests are equal, which is
    the one case where doing nothing also works. This is the other control and the one that can fail:
    roll each bone's local rest by X and pre-multiply each child's local by X⁻¹, so every world
    transform telescopes back to exactly what it was and only the rest ORIENTATIONS differ.

    A clip's local rotations are then wrong on this rig by a conjugation — `X⁻¹ · L · X` is what they
    should be — so the naive copy must fail and a correct rest correction must recover it EXACTLY.
    Anything above about a degree here is a bug in the correction and not a fact about rigs.

    `only` rolls just those bones' own rest and leaves the subtree below them alone, which is the
    shape of a REST POSE difference at the root: the naive copy then turns the whole body while every
    limb stays internally consistent, exactly as Alice and Blondie do.
    """
    import copy as _copy
    doc = _copy.deepcopy(fig.doc)
    a = math.radians(degrees) / 2.0
    x = (math.sin(a) * 0.5773, math.sin(a) * 0.5773, math.sin(a) * 0.5773, math.cos(a))  # about 1,1,1
    xi = qconj(x)
    nodes = doc["nodes"]
    bones = {fig.by_name[n] for b, n in fig.mapping.items()
             if n in fig.by_name and (only is None or b in only)}
    # Every node under the armature, not only the mapped ones — a partial roll would leave the
    # intermediates inconsistent with their parents and change the geometry.
    touch = set()
    stack = list(bones)
    while stack:
        i = stack.pop()
        if i in touch:
            continue
        touch.add(i)
        if only is None:                       # `only` rolls just those bones' OWN rest, not the
            stack += [int(c) for c in (nodes[i].get("children") or [])]   # subtree below them
    for i in touch:
        nd = nodes[i]
        r = tuple(nd.get("rotation") or (0.0, 0.0, 0.0, 1.0))
        nd["rotation"] = list(qmul(r, x))
        nd.pop("matrix", None)
        for c in (nd.get("children") or []):
            ch = nodes[int(c)]
            cr = tuple(ch.get("rotation") or (0.0, 0.0, 0.0, 1.0))
            ch["rotation"] = list(qmul(xi, cr))
            t = ch.get("translation")
            if t:
                m = qmat(xi)
                ch["translation"] = [m[0] * t[0] + m[4] * t[1] + m[8] * t[2],
                                     m[1] * t[0] + m[5] * t[1] + m[9] * t[2],
                                     m[2] * t[0] + m[6] * t[1] + m[10] * t[2]]
            ch.pop("matrix", None)
    return Figure(fig.label + f" (rest rolled {degrees:g}°)", doc, dict(fig.mapping), "control",
                  dict(fig.by_name), parent_map(doc), node_world_matrices(doc))


def probe(clip_doc, clip_blob, clip_name, src: Figure, dst: Figure, frames: int):
    """Per-bone rotation error, then limb-direction error under each of the three laws."""
    shared = [b for b in CORE_BONES if b in src.mapping and b in dst.mapping
              and src.mapping[b] in src.by_name and dst.mapping[b] in dst.by_name]
    per_bone: dict[str, list[float]] = {b: [] for b in shared}
    naive_dirs: list[float] = []
    fixed_dirs: list[float] = []
    abs_dirs: list[float] = []
    naive_body: list[float] = []
    fixed_body: list[float] = []
    abs_body: list[float] = []
    for k in range(frames):
        rots = rotations_at(clip_doc, clip_blob, clip_name, k / max(1, frames - 1))
        if not rots:
            break
        # BOTH sides are driven by the same mapped channels. Posing the source with all 222 and the
        # target with the 22 that map measures channel loss and rest difference at once, and then the
        # identity case does not read zero — which is how this bug was caught.
        carried = {b: rots[src.mapping[b]] for b in shared if src.mapping[b] in rots}
        # EVERY rotation the clip carries, not only the mapped ones. A bone the humanoid does not name
        # can still be a mapped bone's ANCESTOR, and then it is part of that bone's world orientation.
        # Posing the source from the mapped subset made this probe agree with a retarget that had the
        # same blind spot — it reported Alice and Grace identical while one slid and the other swung.
        s_mats = posed_matrices(src, rots)
        t_naive = posed_matrices(dst, {dst.mapping[b]: q for b, q in carried.items()})
        t_fixed = posed_matrices(dst, corrected_locals(src, dst, carried))
        t_abs = posed_matrices(dst, absolute_locals(src, dst, carried))
        for bone in shared:
            if bone not in s_mats or bone not in t_naive:
                continue
            motion = qmul(quat_of(s_mats[bone]), qconj(src.rest(bone)))
            per_bone[bone].append(angle_between(qmul(motion, dst.rest(bone)),
                                                quat_of(t_naive[bone])))
        want = limb_dirs(s_mats)
        naive_dirs += dir_error(want, limb_dirs(t_naive))
        fixed_dirs += dir_error(want, limb_dirs(t_fixed))
        abs_dirs += dir_error(want, limb_dirs(t_abs))
        # WHERE THE WHOLE BODY ENDED UP, which the limb numbers cannot see. `limb_dirs` works in each
        # figure's own body frame so that proportions and heading do not count as error — and that
        # factors out a figure turned bodily the wrong way, which is the largest error there is. Naive
        # copy leaves Alice's body 79° from Jane's and still scores well on limbs; reporting only the
        # limbs said it was the better law, and it is not.
        for bucket, mats in ((naive_body, t_naive), (fixed_body, t_fixed), (abs_body, t_abs)):
            a, b = _frame_q(s_mats), _frame_q(mats)
            if a and b:
                bucket.append(angle_between(a, b))
    return per_bone, naive_dirs, fixed_dirs, abs_dirs, naive_body, abs_body, shared


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clip", default="", help="clip label (default: the first Jane shipped with)")
    ap.add_argument("--frames", type=int, default=12)
    ap.add_argument("--per-bone", action="store_true")
    ap.add_argument("--control", type=float, nargs="*", default=[],
                    help="also retarget onto Jane with every bone's REST rolled N degrees — a case "
                         "where the right answer is known to be 0")
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    def model(label):
        row = db.execute("SELECT id, attributes FROM assets WHERE kind='model' AND label=? "
                         "AND superseded_by IS NULL", (label,)).fetchone()
        if not row or not os.path.exists(os.path.join(CACHE, row[0])):
            return None, "-"
        sig = (json.loads(row[1] or "{}").get("rig_sig") or "-")
        return read_figure(label, row[0]), sig

    q = """SELECT a.id, a.label FROM relations r JOIN assets a ON a.id=r.to_id
           JOIN assets f ON f.id=r.from_id
           WHERE f.label='Jane' AND r.type='shipped_with' AND a.superseded_by IS NULL"""
    clips = db.execute(q + (" AND a.label=?" if args.clip else "") + " ORDER BY a.label",
                       (args.clip,) if args.clip else ()).fetchall()
    if not clips:
        print(f"no clip {args.clip!r} on Jane")
        return 2
    clip_id, clip_label = clips[0]
    clip_doc, clip_blob = split_glb(open(os.path.join(CACHE, clip_id), "rb").read())
    clip_name = (clip_doc.get("animations") or [{}])[0].get("name") or clip_label

    src, src_sig = model("Jane")
    if src is None:
        print("Jane is not in the catalog")
        return 2
    controls = [(rolled_copy(src, d), "control") for d in (args.control or [])]

    rots0 = rotations_at(clip_doc, clip_blob, clip_name, 0.0)
    carried = sum(1 for b in CORE_BONES if src.mapping.get(b) in rots0)
    print(f"clip {clip_label!r} authored on Jane ({src_sig}), {args.frames} frames sampled\n")
    print(f"CHANNEL LOSS is the same for every target and is not retargeting's to fix:")
    print(f"  the clip drives {len(rots0)} bones; the humanoid names {len(src.mapping)} of them, "
          f"{carried} of which are CORE.")
    print(f"  {len(rots0) - carried} channel(s) — skirt, breast and secondary chains — have no bone on "
          f"any other rig to receive them.\n")
    print("  `Jane -> Jane` is the FLOOR: channel loss with no rig difference. Read every other row as")
    print("  its EXCESS over that, not as an absolute.\n")
    print("  LIMB DIRECTION is what judges a retarget: where each limb POINTS, in the figure's own body")
    print("  frame, so proportions and heading do not count as error. `naive` copies the source's local")
    print("  rotations; `corrected` takes each through both rigs' rest poses.\n")
    print(f"{'':17} {'':12} {'---- naive ----':>18}  | {'delta':>9} {'--- absolute ----':>19}")
    print(f"{'target':17} {'rig':12} {'limbs':>8} {'body':>9}  | {'limbs':>9} "
          f"{'limbs':>9} {'body':>8} {'worst':>9}")
    targets = [(lbl, None) for lbl in
               ("Jane", "Akari", "office-babe", "Blondie", "Alice", "Grace", "Trish", "Saka",
                "Eve Maccaro", "Steve", "Animated Woman", "Tamaki")]
    for label, preloaded in [(f.label, (f, s2)) for f, s2 in controls] + targets:
        dst, sig = preloaded if preloaded else model(label)
        if dst is None:
            print(f"{label[:16]:17} (not in the catalog)")
            continue
        if not dst.mapping:
            print(f"{label[:16]:17} {sig:12} {'—':>5}  no humanoid map — refused, which is correct")
            continue
        (per_bone, naive_dirs, fixed_dirs, abs_dirs,
         naive_body, abs_body, shared) = probe(clip_doc, clip_blob, clip_name, src, dst, args.frames)
        vals = sorted(v for b in per_bone for v in per_bone[b])
        if not vals:
            print(f"{label[:16]:17} {sig:12} no shared bones")
            continue
        by_name_hits = sum(1 for n in rots0 if n in dst.by_name)
        nd, fd, ad = sorted(naive_dirs), sorted(fixed_dirs), sorted(abs_dirs)
        med = lambda xs: xs[len(xs) // 2] if xs else float("nan")     # noqa: E731
        nb, ab_ = sorted(naive_body), sorted(abs_body)
        breaks = chain_breaks(dst)
        print(f"{label[:16]:17} {sig:12} {med(nd):8.1f}° {med(nb):8.1f}°  |"
              f" {med(fd):8.1f}° {med(ad):9.1f}° {med(ab_):8.1f}° {max(ad) if ad else 0:8.1f}°"
              + (f"   MAP BREAKS: {', '.join(breaks[:2])}" if breaks else ""))
        if args.per_bone:
            for b in sorted(per_bone, key=lambda b: -max(per_bone[b] or [0])):
                if per_bone[b]:
                    s = sorted(per_bone[b])
                    print(f"      {b:16} median {s[len(s) // 2]:6.1f}°   worst {s[-1]:6.1f}°")
    return 0


if __name__ == "__main__":
    sys.exit(main())
