"""Humanoid skeleton inference (conjure.figures) — discovery layer 2 of the figures pipeline.

Pure geometry over the glTF JSON, so it is fully testable with no headset, no Blender, and no model
files. That matters more here than elsewhere: an inferred bone map that is plausible but wrong is
silently inherited by every later capability (posing, retargeting), so the validator is as load-bearing
as the inference. docs/backlogs/figures.md
"""

import pytest

import math

from conjure.figures import (CORE_BONES, FRAME_VECTORS, POSE_AXES, TRUNK_BONES,
                             anatomical_axes, body_frame, infer_humanoid,
                             node_world_matrices, node_world_positions, parent_map, resolve_pose,
                             score, validate)
from conjure.figures import _ancestors, _local_matrix, _mul, _quat_mul, _sub


def _skeleton():
    """A minimal but anatomically-ordered humanoid: hips, spine to head, two legs, two arms.

    +X is the model's left, +Y up — the convention every sample model follows. Positions are chosen so
    each joint is unambiguous by shape alone, which is exactly what inference is allowed to use.
    """
    nodes, index = [], {}

    def add(name, t, children=()):
        index[name] = len(nodes)
        nodes.append({"name": name, "translation": list(t), "children": list(children)})
        return index[name]

    # built leaf-first so children indices exist; translations are LOCAL (parent-relative)
    add("head", (0, 0.25, 0))
    add("neck", (0, 0.10, 0), [index["head"]])
    add("chest", (0, 0.25, 0), [index["neck"]])
    add("spine", (0, 0.15, 0), [index["chest"]])
    for s, sign in (("l", 1), ("r", -1)):
        add(f"{s}_toes", (0, -0.05, 0.10))
        add(f"{s}_foot", (0, -0.40, 0), [index[f"{s}_toes"]])
        add(f"{s}_shin", (0, -0.40, 0), [index[f"{s}_foot"]])
        add(f"{s}_thigh", (sign * 0.10, -0.05, 0), [index[f"{s}_shin"]])
        # a hand with three finger stubs, so the finger branch-point is findable
        for f in range(3):
            add(f"{s}_finger{f}", (sign * 0.08, 0, f * 0.02))
        add(f"{s}_hand", (sign * 0.25, 0, 0), [index[f"{s}_finger{f}"] for f in range(3)])
        add(f"{s}_lowerarm", (sign * 0.25, 0, 0), [index[f"{s}_hand"]])
        add(f"{s}_upperarm", (sign * 0.08, 0, 0), [index[f"{s}_lowerarm"]])
        add(f"{s}_shoulder", (sign * 0.05, 0.20, 0), [index[f"{s}_upperarm"]])
    add("hips", (0, 1.0, 0),
        [index["spine"], index["l_thigh"], index["r_thigh"], index["l_shoulder"], index["r_shoulder"]])
    joints = [index[n] for n in index]
    return {"scenes": [{"nodes": [index["hips"]]}], "scene": 0, "nodes": nodes,
            "skins": [{"joints": joints}]}, index


def test_world_positions_compose_down_the_tree():
    doc, idx = _skeleton()
    pos = node_world_positions(doc)
    assert pos[idx["hips"]][1] == pytest.approx(1.0)
    assert pos[idx["l_foot"]][1] == pytest.approx(1.0 - 0.05 - 0.40 - 0.40)
    assert pos[idx["l_hand"]][0] > 0 and pos[idx["r_hand"]][0] < 0


def test_inference_recovers_the_skeleton_from_shape_alone():
    """No name in the fixture matches any convention — `l_thigh`, `r_lowerarm` — so a name table would
    return nothing. Only the shape is available, which is the whole point of layer 2."""
    doc, idx = _skeleton()
    got = infer_humanoid(doc)
    assert got["hips"] == "hips"
    assert got["head"] == "head"
    assert got["leftUpperLeg"] == "l_thigh" and got["leftLowerLeg"] == "l_shin"
    assert got["leftFoot"] == "l_foot" and got["leftToes"] == "l_toes"
    assert got["rightUpperLeg"] == "r_thigh" and got["rightFoot"] == "r_foot"


def test_inference_finds_the_hand_not_a_fingertip():
    # The widest joint is a FINGER; the hand is where the finger chains diverge.
    doc, _ = _skeleton()
    got = infer_humanoid(doc)
    assert got["leftHand"] == "l_hand" and got["rightHand"] == "r_hand"


def test_left_and_right_are_not_swapped():
    doc, _ = _skeleton()
    got = infer_humanoid(doc)
    assert got["leftFoot"].startswith("l_") and got["rightFoot"].startswith("r_")
    assert validate(doc, got) == []


def test_a_skeleton_that_is_not_humanoid_infers_nothing():
    doc = {"scenes": [{"nodes": [0]}], "scene": 0,
           "nodes": [{"name": "a", "translation": [0, 0, 0]}], "skins": [{"joints": [0]}]}
    assert infer_humanoid(doc) is None


# ---- the validator is the load-bearing half ------------------------------------------------------
# Every case here is one that ACTUALLY slipped through on a real model before the check existed.

def test_validate_catches_a_left_right_swap():
    doc, _ = _skeleton()
    good = infer_humanoid(doc)
    swapped = dict(good)
    swapped["leftHand"], swapped["rightHand"] = good["rightHand"], good["leftHand"]
    assert any("swapped" in p for p in validate(doc, swapped))


def test_validate_catches_one_node_used_for_several_bones():
    """Grace's inference mapped upper leg, lower leg AND foot to the same IK control. Every ordering
    comparison was then equal-not-less and every segment length zero, so the map passed as clean."""
    doc, _ = _skeleton()
    m = dict(infer_humanoid(doc))
    m["leftLowerLeg"] = m["leftUpperLeg"]
    assert any("both mapped to" in p for p in validate(doc, m))


def test_validate_catches_an_unmapped_core_bone():
    doc, _ = _skeleton()
    m = dict(infer_humanoid(doc))
    del m["head"]
    assert any("unmapped" in p for p in validate(doc, m))


def test_validate_catches_inverted_vertical_order():
    doc, _ = _skeleton()
    m = dict(infer_humanoid(doc))
    m["head"], m["hips"] = m["hips"], m["head"]
    assert validate(doc, m), "a head below the hips must be reported"


def test_validate_tolerates_sub_millimetre_noise():
    # Yuffie's spine sits a fraction of a millimetre below her hips. Real, but not a problem — a
    # validator that cries over float noise gets ignored.
    doc, idx = _skeleton()
    doc["nodes"][idx["spine"]]["translation"] = [0, -0.001, 0]
    m = infer_humanoid(doc)
    assert not [p for p in validate(doc, m) if "sits below" in p]


def test_score_separates_misses_from_disagreements():
    stated = {"hips": "hips", "head": "head", "leftHand": "l_hand"}
    inferred = {"hips": "hips", "head": "WRONG"}
    s = score(inferred, stated)
    assert s["checked"] == 2 and s["correct"] == 1
    assert s["wrong"]["head"]["stated"] == "head"
    assert "leftHand" in s["missing"]


def test_core_bones_are_the_documented_set():
    assert "hips" in CORE_BONES and "leftUpperArm" in CORE_BONES
    assert not any(b.endswith("Distal") for b in CORE_BONES), "fingers are not inferable from topology"


# ---------------------------------------------------------------- the anatomical frame
#
# Semantic bone names say WHICH joint; these say WHICH WAY. The tests below all take the same shape,
# and it is the only shape that can catch a wrong axis: pose a bone, then look at where the joint BELOW
# it actually ended up. An axis that is plausible, unit-length and wrong passes every structural check
# there is — which is exactly how the previous round of this feature shipped legs that raised backwards.


def _quaternion_matrix(q):
    x, y, z, w = q
    return [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0,
            2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0,
            2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0,
            0, 0, 0, 1]


def _moved(doc, bone_node, delta, watch_node):
    """Where `watch_node` lands after `delta` (a parent-space quaternion) is applied to `bone_node`.

    Forward kinematics in eight lines, and it has to be here rather than in the module: this is the
    runtime's job, and a test that reused the runtime's own arithmetic could only prove it consistent
    with itself. `delta * rest` is what the client does with the axes it is sent.
    """
    mats, parent = node_world_matrices(doc), parent_map(doc)
    node = doc["nodes"][bone_node]
    q = _quat_mul(delta, node.get("rotation", [0.0, 0.0, 0.0, 1.0]))
    t, sc, m = node.get("translation", [0, 0, 0]), node.get("scale", [1, 1, 1]), _quaternion_matrix(q)
    local = [m[0] * sc[0], m[1] * sc[0], m[2] * sc[0], 0, m[4] * sc[1], m[5] * sc[1], m[6] * sc[1], 0,
             m[8] * sc[2], m[9] * sc[2], m[10] * sc[2], 0, t[0], t[1], t[2], 1]
    p = parent.get(bone_node)
    world = _mul(mats[p], local) if p is not None else local
    chain = _ancestors(watch_node, parent)
    for c in reversed(chain[:chain.index(bone_node)]):
        world = _mul(world, _local_matrix(doc["nodes"][c]))
    return (world[12], world[13], world[14])


def _travel(doc, mapping, axes, bone, watch, request):
    """The world-space displacement of `watch` when `bone` is posed by `request`."""
    by_name = {n["name"]: i for i, n in enumerate(doc["nodes"])}
    before = node_world_positions(doc)[by_name[mapping[watch]]]
    delta = resolve_pose(axes, {bone: request})[bone]
    after = _moved(doc, by_name[mapping[bone]], delta, by_name[mapping[watch]])
    return _sub(after, before)


def _posed(doc=None):
    doc = doc or _skeleton()[0]
    mapping = infer_humanoid(doc)
    return doc, mapping, anatomical_axes(doc, mapping)


def test_the_body_frame_is_measured_not_assumed():
    doc, mapping, _ = _posed()
    frame = body_frame(doc, mapping)
    assert frame["up"] == pytest.approx((0, 1, 0), abs=1e-6)
    assert frame["left"] == pytest.approx((1, 0, 0), abs=1e-6)
    # +X left and +Y up make forward +Z — the facing convention every sample model follows, arrived at
    # by cross product rather than by assertion, so a rig standing off-axis still gets a square frame.
    assert frame["forward"] == pytest.approx((0, 0, 1), abs=1e-6)


def test_bend_swings_a_limb_forward_on_both_sides():
    """The bug this whole layer exists for: "raise her legs" put them BEHIND her."""
    doc, mapping, axes = _posed()
    for side in ("left", "right"):
        d = _travel(doc, mapping, axes, f"{side}UpperLeg", f"{side}Foot", {"bend": 45})
        assert d[2] > 0.3, f"{side} foot should swing forward, went {d}"
        assert abs(d[0]) < 1e-6, "and straight forward, not out to the side"
    # Both sides by the SAME sign, because flexing both hips moves both knees the same way. Only the
    # lateral rotations mirror.
    left = _travel(doc, mapping, axes, "leftUpperLeg", "leftFoot", {"bend": 45})
    right = _travel(doc, mapping, axes, "rightUpperLeg", "rightFoot", {"bend": 45})
    assert left == pytest.approx(right, abs=1e-6)


def test_spread_swings_a_limb_outward_on_both_sides():
    doc, mapping, axes = _posed()
    left = _travel(doc, mapping, axes, "leftUpperLeg", "leftFoot", {"spread": 45})
    right = _travel(doc, mapping, axes, "rightUpperLeg", "rightFoot", {"spread": 45})
    assert left[0] > 0.3 and right[0] < -0.3, "feet should part, not both go the same way"
    assert left[0] == pytest.approx(-right[0], abs=1e-6), "and mirror each other exactly"


def test_spread_raises_an_arm_that_already_points_outward():
    """The degenerate case that a fixed body axis cannot handle.

    A T-posed arm already points along the direction "outward" means, so the cross product that defines
    spread everywhere else collapses. Falling back to the body's up axis keeps the motion continuous:
    an arm at 90 degrees keeps rising rather than stopping dead or spinning about its own length.
    """
    doc, mapping, axes = _posed()
    for side in ("left", "right"):
        d = _travel(doc, mapping, axes, f"{side}UpperArm", f"{side}Hand", {"spread": 45})
        assert d[1] > 0.3, f"{side} hand should rise, went {d}"


def test_bend_lifts_the_toes_of_a_forward_pointing_foot():
    """The other degenerate case: a foot points the way `bend` would swing it, so bend means the ankle."""
    doc, mapping, axes = _posed()
    d = _travel(doc, mapping, axes, "leftFoot", "leftToes", {"bend": 45})
    assert d[1] > 0.05 and abs(d[2]) < 0.02, f"toes should lift, went {d}"


def test_turn_rotates_a_limb_about_its_own_length_inward_on_both_sides():
    doc, mapping, axes = _posed()
    left = _travel(doc, mapping, axes, "leftLowerLeg", "leftToes", {"turn": 90})
    right = _travel(doc, mapping, axes, "rightLowerLeg", "rightToes", {"turn": 90})
    assert left[0] < -0.05 and right[0] > 0.05, "both toes should turn toward the midline"
    assert left[1] == pytest.approx(0, abs=1e-6), "a twist does not raise the joint below it"


def test_the_head_turns_about_the_body_up_axis():
    doc, mapping, axes = _posed()
    assert axes["head"]["turn"] == pytest.approx([0, 1, 0], abs=1e-6)


def test_axes_ride_the_bones_own_parent_frame():
    """Why the axes are stored in parent space rather than model space.

    Turning the whole figure 90 degrees changes every axis in the world, and changes NONE of them in the
    frame each bone's local rotation actually lives in. Storing them that way is what makes applying one
    a single multiplication onto the rest quaternion — and what makes a pose survive the figure being
    rotated, which is otherwise a whole class of bug nobody would see until the headset.
    """
    doc, mapping, axes = _posed()
    turned, _ = _skeleton()
    root = len(turned["nodes"])
    half = math.sqrt(0.5)
    turned["nodes"].append({"name": "root", "rotation": [0, half, 0, half],   # +90 degrees about Y
                            "children": [turned["scenes"][0]["nodes"][0]]})
    turned["scenes"][0]["nodes"] = [root]
    spun = anatomical_axes(turned, mapping)
    assert spun["leftUpperLeg"]["bend"] == pytest.approx(axes["leftUpperLeg"]["bend"], abs=1e-5)
    # ...and in model space they differ, which is the thing being avoided.
    world = anatomical_axes(turned, mapping, space="world")["leftUpperLeg"]["bend"]
    assert world != pytest.approx(axes["leftUpperLeg"]["bend"], abs=1e-3)
    # The motion is what matters, and it is unchanged in the FIGURE's own terms: her forward is now +X.
    d = _travel(turned, mapping, spun, "leftUpperLeg", "leftFoot", {"bend": 45})
    assert d[0] > 0.3 and abs(d[2]) < 1e-5


def test_a_bone_with_nothing_to_point_at_gets_no_axes():
    """No frame is better than a made-up one — /figure refuses to pose what it cannot aim."""
    doc, _ = _skeleton()
    assert anatomical_axes(doc, {"hips": "hips"}) == {}


def test_resolve_pose_composes_twist_innermost():
    doc, mapping, axes = _posed()
    both = resolve_pose(axes, {"leftUpperArm": {"bend": 30, "turn": 20}})["leftUpperArm"]
    bend = resolve_pose(axes, {"leftUpperArm": {"bend": 30}})["leftUpperArm"]
    turn = resolve_pose(axes, {"leftUpperArm": {"turn": 20}})["leftUpperArm"]
    assert both == pytest.approx(_quat_mul(bend, turn), abs=1e-9)
    assert POSE_AXES.index("turn") < POSE_AXES.index("bend") < POSE_AXES.index("spread")


def test_resolve_pose_treats_a_missing_axis_as_zero():
    doc, mapping, axes = _posed()
    assert resolve_pose(axes, {"head": {}})["head"] == pytest.approx([0, 0, 0, 1])
    assert resolve_pose(axes, {"head": {"bend": 0}})["head"] == pytest.approx([0, 0, 0, 1])
    assert resolve_pose(axes, {"nosuchbone": {"bend": 10}}) == {}


def test_every_mapped_bone_of_a_humanoid_gets_a_full_frame():
    doc, mapping, axes = _posed()
    assert set(axes) == set(mapping)
    for bone, frame in axes.items():
        # three rotations to swing about, and the four bind-pose vectors an absolute aim needs
        assert set(frame) == set(POSE_AXES) | set(FRAME_VECTORS), bone
        for name, axis in frame.items():
            assert math.isclose(math.dist(axis, (0, 0, 0)), 1.0, abs_tol=1e-4), f"{bone}.{name} not unit"


# ---------------------------------------------------------------- aiming (absolute directions)
#
# The relative rotations ask the caller to know where a bone rests, and the three real rigs disagree by
# 48 degrees about where an arm does — measured, and the reason "raise her arm up" pointed it backwards
# on all three. An aim says the destination instead.


def _apose(doc, idx, drop=0.5):
    """The fixture with its arms lowered — an A-pose, so aiming can be tested against two rest poses."""
    for side, sign in (("l", 1), ("r", -1)):
        doc["nodes"][idx[f"{side}_upperarm"]]["translation"] = [sign * 0.08, 0, 0]
        for bone in (f"{side}_lowerarm", f"{side}_hand"):
            doc["nodes"][idx[bone]]["translation"] = [sign * 0.25 * (1 - drop), -0.25 * drop, 0]
    return doc


#: Axes are rounded to five places on the wire, so a direction lands within ~1e-5 rather than exactly.
DIRECTION_TOL = 1e-4


def _direction(doc, mapping, axes, bone, request):
    """Where `bone` points after the request — the whole claim of an aim, in one number."""
    by_name = {n["name"]: i for i, n in enumerate(doc["nodes"])}
    rest = anatomical_axes(doc, mapping, space="world")[bone]["rest"]
    delta = resolve_pose(anatomical_axes(doc, mapping, space="world"), {bone: request})[bone]
    x, y, z, w = delta                                        # rotate the rest direction by the delta
    t = (2 * (y * rest[2] - z * rest[1]), 2 * (z * rest[0] - x * rest[2]), 2 * (x * rest[1] - y * rest[0]))
    return (rest[0] + w * t[0] + y * t[2] - z * t[1],
            rest[1] + w * t[1] + z * t[0] - x * t[2],
            rest[2] + w * t[2] + x * t[1] - y * t[0])


def test_aim_points_the_bone_where_it_was_asked_to():
    doc, mapping, axes = _posed()
    for want, expected in (("up", (0, 1, 0)), ("down", (0, -1, 0)),
                           ("forward", (0, 0, 1)), ("back", (0, 0, -1))):
        assert _direction(doc, mapping, axes, "leftUpperArm", {"aim": want}) == pytest.approx(
            expected, abs=DIRECTION_TOL), want


def test_out_and_in_are_side_aware():
    doc, mapping, axes = _posed()
    assert _direction(doc, mapping, axes, "leftUpperArm", {"aim": "out"})[0] > 0.99
    assert _direction(doc, mapping, axes, "rightUpperArm", {"aim": "out"})[0] < -0.99
    # The same word, mirrored by the measured frame rather than by a sign the caller has to supply —
    # which is the failure the device run recorded: asked to spread both legs, the director negated one.
    assert _direction(doc, mapping, axes, "leftUpperArm", {"aim": "in"})[0] < -0.99


def test_the_same_aim_lands_the_same_way_from_two_different_rest_poses():
    """The reason aiming exists. Saka rests her arms horizontal and Grace hers 48 degrees below; the
    same relative number cannot mean "up" for both, and the same aim must."""
    t_pose, idx = _skeleton()
    a_pose = _apose(*_skeleton())
    mapping = infer_humanoid(t_pose)
    t_rest = anatomical_axes(t_pose, mapping, space="world")["leftUpperArm"]["rest"]
    a_rest = anatomical_axes(a_pose, mapping, space="world")["leftUpperArm"]["rest"]
    assert t_rest[1] == pytest.approx(0, abs=1e-6) and a_rest[1] < -0.3, "the fixtures must differ"
    for doc in (t_pose, a_pose):
        got = _direction(doc, mapping, anatomical_axes(doc, mapping), "leftUpperArm", {"aim": "up"})
        assert got == pytest.approx((0, 1, 0), abs=DIRECTION_TOL)


def test_a_half_turn_swings_through_the_side_not_through_the_torso():
    """An arm aimed at its own opposite is antiparallel, and a half-turn has no unique axis. Left to a
    generic shortest-arc routine it picks an arbitrary perpendicular; here it must be the body's forward,
    so the arm travels through the frontal plane the way a person raises one."""
    doc, mapping, axes = _posed()
    q = resolve_pose(axes, {"leftUpperArm": {"aim": "in"}})["leftUpperArm"]
    assert q[3] == pytest.approx(0, abs=1e-9), "not a half-turn"
    assert (abs(q[0]), abs(q[1]), abs(q[2])) == pytest.approx((0, 0, 1), abs=DIRECTION_TOL)


def test_an_aim_replaces_the_relative_swing_but_not_the_twist():
    doc, mapping, axes = _posed()
    aim = resolve_pose(axes, {"leftUpperArm": {"aim": "up"}})["leftUpperArm"]
    assert resolve_pose(axes, {"leftUpperArm": {"aim": "up", "bend": 40}})["leftUpperArm"] \
        == pytest.approx(aim), "bend must not add to an aim — the server refuses the pair outright"
    turned = resolve_pose(axes, {"leftUpperArm": {"aim": "up", "turn": 30}})["leftUpperArm"]
    assert turned != pytest.approx(aim), "a twist is orthogonal to where the bone points, so it composes"


def test_an_aim_that_is_already_satisfied_is_a_no_op():
    doc, mapping, axes = _posed()
    assert resolve_pose(axes, {"leftUpperLeg": {"aim": "down"}})["leftUpperLeg"] \
        == pytest.approx([0, 0, 0, 1], abs=1e-9)


def test_an_unmeasurable_aim_resolves_to_nothing_rather_than_a_guess():
    doc, mapping, axes = _posed()
    stale = {"leftUpperArm": {k: v for k, v in axes["leftUpperArm"].items() if k not in FRAME_VECTORS}}
    assert resolve_pose(stale, {"leftUpperArm": {"aim": "up"}})["leftUpperArm"] == [0, 0, 0, 1]
    assert resolve_pose(axes, {"leftUpperArm": {"aim": "sideways"}})["leftUpperArm"] == [0, 0, 0, 1]


def test_the_trunk_is_not_aimable_and_says_which_bones_are():
    # Aim points a bone along its LENGTH; the head already points up, so "look up" would be a no-op.
    assert set(TRUNK_BONES) == {"hips", "spine", "chest", "upperChest", "neck", "head"}
    assert not any(b.startswith(("left", "right")) for b in TRUNK_BONES)


def test_pose_resolution_matches_the_shared_golden_vectors():
    """The same fixture tests/js/figure.test.js reads. Two implementations of this arithmetic exist —
    Python renders the verification images, the client drives the headset — and they are only worth
    having if they agree to the digit. Same discipline as plane-anchor's golden vectors."""
    import json
    from pathlib import Path
    golden = json.loads((Path(__file__).resolve().parent / "js" / "fixtures"
                         / "figure-pose-golden.json").read_text())
    for case in golden["cases"]:
        frame = golden["frames"][case["frame"]]
        got = resolve_pose({"bone": frame}, {"bone": case["request"]})["bone"]
        assert got == pytest.approx(case["quat"], abs=1e-9), case["name"]
