"""The rest-pose correction that phase 5 rests on (`scripts/retarget_probe.py`).

The plan assumes mapping two skeletons through the canonical humanoid is the easy half and correcting
for their difference in BIND POSE is the hard one. These tests pin the correction's arithmetic, on
skeletons small enough to reason about, because the corpus can only say what the answer IS and not
whether the method is sound.

Two controls, and the second is the one that can fail. A clip played back on its own rig must come
home unchanged — necessary, but it is also the one case where doing nothing works. So the real test
rolls every bone's REST while leaving the geometry alone: the clip's locals are then wrong by a
conjugation, the naive copy must break, and the correction must recover it exactly.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from retarget_probe import (Figure, angle_between, corrected_locals, limb_dirs,   # noqa: E402
                            posed_matrices, qmul, quat_of, rolled_copy)

from conjure.figures import node_world_matrices, parent_map   # noqa: E402


def _rig(*, arm_rot=None) -> Figure:
    """A four-bone humanoid: hips → spine → chest → shoulder → upper arm → lower arm → hand, plus the
    legs and neck `body_axes` needs to build a frame. Rotations are optional so a second rig can
    differ from the first in exactly one way."""
    chain = [("hips", None, (0, 1.0, 0), None),
             ("spine", 0, (0, 0.2, 0), None),
             ("chest", 1, (0, 0.25, 0), None),
             ("neck", 2, (0, 0.25, 0), None),
             ("head", 3, (0, 0.12, 0), None),
             ("leftShoulder", 2, (0.05, 0.2, 0), None),
             ("leftUpperArm", 5, (0.12, 0, 0), arm_rot),
             ("leftLowerArm", 6, (0.28, 0, 0), None),
             ("leftHand", 7, (0.25, 0, 0), None),
             ("leftUpperLeg", 0, (0.1, -0.05, 0), None),
             ("leftLowerLeg", 9, (0, -0.42, 0), None),
             ("leftFoot", 10, (0, -0.4, 0), None),
             ("leftToes", 11, (0, -0.05, 0.1), None),
             ("rightUpperLeg", 0, (-0.1, -0.05, 0), None),
             ("rightLowerLeg", 13, (0, -0.42, 0), None),
             ("rightFoot", 14, (0, -0.4, 0), None),
             ("rightToes", 15, (0, -0.05, 0.1), None)]
    nodes = []
    for name, parent, t, rot in chain:
        nd = {"name": name, "translation": list(t)}
        if rot:
            nd["rotation"] = list(rot)
        nodes.append(nd)
        if parent is not None:
            nodes[parent].setdefault("children", []).append(len(nodes) - 1)
    doc = {"asset": {"version": "2.0"}, "nodes": nodes, "scenes": [{"nodes": [0]}], "scene": 0}
    mapping = {n["name"]: n["name"] for n in nodes}
    by_name = {n["name"]: i for i, n in enumerate(nodes)}
    return Figure("rig", doc, mapping, "test", by_name, parent_map(doc), node_world_matrices(doc))


#: A clip's worth of local rotations: the left arm swung about Z, the spine turned about Y.
def _pose(deg: float) -> dict:
    a = math.radians(deg) / 2
    return {"leftUpperArm": (0.0, 0.0, math.sin(a), math.cos(a)),
            "spine": (0.0, math.sin(a / 2), 0.0, math.cos(a / 2))}


def test_a_clip_played_back_on_its_OWN_rig_comes_home_unchanged():
    """Necessary, and not sufficient — it is also the one case where copying locals works."""
    rig = _rig()
    carried = _pose(40)
    out = corrected_locals(rig, rig, carried)
    for bone, q in carried.items():
        assert _same(out[bone], q), bone


def test_the_correction_recovers_a_rig_whose_REST_IS_ROLLED_and_the_naive_copy_does_not():
    """The control that can fail. Rolling every bone's rest and pre-multiplying each child's local by
    the inverse leaves every world transform exactly as it was, so the two rigs are geometrically
    identical and differ ONLY in what a local rotation means. The naive copy is then wrong by that
    conjugation; the correction has to undo it exactly."""
    src = _rig()
    dst = rolled_copy(src, 90.0)
    carried = _pose(50)

    naive = posed_matrices(dst, carried)
    fixed = posed_matrices(dst, corrected_locals(src, dst, carried))
    want = limb_dirs(posed_matrices(src, carried))

    def worst(mats):
        d = limb_dirs(mats)
        return max((math.degrees(math.acos(max(-1.0, min(1.0, sum(want[k][i] * d[k][i]
                                                                  for i in range(3))))))
                    for k in want if k in d), default=0.0)

    assert worst(fixed) < 0.01, "the correction must recover a rolled rest exactly"
    assert worst(naive) > 10.0, "and the naive copy must visibly fail, or this proves nothing"


def test_the_correction_uses_the_POSED_parent_and_not_the_rest_parent():
    """The obvious first attempt, and it fails silently on the identity case at ~32 degrees.

    A bone's local rotation is measured against where its parent ACTUALLY IS once the clip has moved
    it, not against where the parent rests — so the correction is a top-down walk rather than a
    per-bone formula. Pinned on a child of a bone the clip MOVES, which is the only place the two
    differ.
    """
    src = _rig()
    dst = rolled_copy(src, 60.0)
    carried = _pose(70)                       # `spine` moves, so `chest` below it has a moved parent
    out = corrected_locals(src, dst, carried)

    mats = posed_matrices(dst, out)
    s_mats = posed_matrices(src, carried)
    for bone in ("chest", "leftUpperArm", "leftHand"):
        motion = qmul(quat_of(s_mats[bone]), _conj(src.rest(bone)))
        assert _same(qmul(motion, dst.rest(bone)), quat_of(mats[bone])), bone


def _conj(q):
    return (-q[0], -q[1], -q[2], q[3])


def _same(a, b, tol=1e-5) -> bool:
    """Quaternion equality componentwise, up to the sign both spellings share.

    Componentwise rather than by angle: `angle_between` is `2·acos(|w|)`, and `acos` is ill-conditioned
    at 1 — two quaternions agreeing to seven figures read as a quarter of a degree apart, which is
    noise reported as a result. The angle is the right measure for how WRONG something is and the wrong
    one for whether it is exact.
    """
    if sum(x * y for x, y in zip(a, b)) < 0:
        b = tuple(-y for y in b)
    return all(abs(x - y) <= tol for x, y in zip(a, b))


def test_a_bone_the_clip_does_not_drive_keeps_its_own_rest():
    """The humanoid names 22 bones and these rigs carry 100–220, so most of a target's skeleton is not
    addressed at all. Those bones have to stay where they were rather than be zeroed."""
    src = _rig()
    dst = rolled_copy(src, 45.0)
    out = corrected_locals(src, dst, _pose(30))
    assert set(out) == {"leftUpperArm", "spine"}, "only the driven bones are rewritten"
