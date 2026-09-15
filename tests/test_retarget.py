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

from retarget_probe import (ATTACHMENT, Figure, absolute_locals, angle_between,   # noqa: E402
                            canonical_rest, corrected_locals, dir_error, limb_dirs,
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


# ---- carrying the pose ABSOLUTELY, rather than as a delta from two different rests ----------------

def test_the_absolute_law_also_recovers_a_rolled_rest_exactly():
    """Whatever replaces the delta correction still has to pass the control it passes."""
    src = _rig()
    dst = rolled_copy(src, 90.0)
    carried = _pose(50)
    want = limb_dirs(posed_matrices(src, carried))
    got = limb_dirs(posed_matrices(dst, absolute_locals(src, dst, carried)))
    assert _worst(want, got) < 0.01


def test_a_difference_in_REST_POSE_is_not_carried_over_as_a_delta():
    """The case the delta law cannot get right, and the reason for the absolute one.

    Two rigs identical but for the angle the upper arm RESTS at — one nearer an A-pose, one nearer a T.
    A clip authored on the first puts its arm somewhere definite; the second should end up with its arm
    in the SAME place, not in its own rest plus the first's delta, which is a different pose entirely.
    """
    a = math.radians(40) / 2
    src = _rig()
    dst = _rig(arm_rot=(0.0, 0.0, math.sin(a), math.cos(a)))       # rests 40° further round
    carried = _pose(25)
    want = limb_dirs(posed_matrices(src, carried))

    delta = limb_dirs(posed_matrices(dst, corrected_locals(src, dst, carried)))
    absolute = limb_dirs(posed_matrices(dst, absolute_locals(src, dst, carried)))
    assert _worst(want, delta) > 30.0, "the delta law adds the two rests together"
    assert _worst(want, absolute) < 0.01, "the absolute law puts the limb where the clip put it"


def test_the_reference_axis_is_chosen_by_the_BONE_and_never_by_MEASUREMENT():
    """A threshold on `dot(along, up)` is the obvious way to square up a bone's frame, and it is a trap.

    Jane's `hips → spine` reads 0.9684 and office-babe's reads 0.9975, so two rigs in the same rest
    pose land either side of a 0.99 cut, take different branches, and end up with canonical frames a
    half-turn apart — 179.8° on both shoulders, a flip rather than a drift.

    The two rigs here differ by FOUR DEGREES of spine lean and straddle exactly that cut: 0.985 against
    0.995. Their frames must differ by about four degrees too. Under the measured rule they came out
    ninety apart, which is the regression this pins.
    """
    def leaning(z: float) -> Figure:
        fig = _rig()
        for nd in fig.doc["nodes"]:
            if nd["name"] in ("spine", "head"):
                t = nd["translation"]
                nd["translation"] = [t[0], t[1], t[2] + z]
        return Figure(fig.label, fig.doc, fig.mapping, fig.source, fig.by_name,
                      parent_map(fig.doc), node_world_matrices(fig.doc))

    a, b = leaning(0.035), leaning(0.020)          # ~10° and ~6° of lean: dot(up) 0.985 and 0.995
    ca, cb = canonical_rest(a), canonical_rest(b)
    for bone in ("hips", "neck", "leftShoulder", "leftUpperArm"):
        gap = angle_between(ca[bone], cb[bone])
        assert gap < 15.0, f"{bone}: a 4° difference in lean produced a {gap:.0f}° difference in frame"


def test_where_a_joint_is_ATTACHED_is_build_and_not_retargeting_error():
    """Shoulder width and leg splay are proportion. No retarget can change them and none should try —
    measured at rest with no clip at all, Trish's `chest → leftShoulder` is already 152° from Jane's.
    Counting that made four rigs look broken when their articulated limbs were within a few degrees."""
    assert ("chest", "leftShoulder") in ATTACHMENT and ("hips", "rightUpperLeg") in ATTACHMENT
    a = {("chest", "leftShoulder"): (1.0, 0, 0), ("leftUpperArm", "leftLowerArm"): (1.0, 0, 0)}
    b = {("chest", "leftShoulder"): (0.0, 1.0, 0), ("leftUpperArm", "leftLowerArm"): (1.0, 0, 0)}
    assert dir_error(a, b) == [0.0], "the articulated limb only"
    assert round(dir_error(a, b, attachments=True)[0]) == 90, "and the attachment is still reportable"


def _worst(want: dict, got: dict) -> float:
    out = 0.0
    for k in want:
        if k in got and k not in ATTACHMENT:
            d = max(-1.0, min(1.0, sum(want[k][i] * got[k][i] for i in range(3))))
            out = max(out, math.degrees(math.acos(d)))
    return out
