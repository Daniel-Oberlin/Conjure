"""Humanoid skeleton inference (conjure.figures) — discovery layer 2 of the figures pipeline.

Pure geometry over the glTF JSON, so it is fully testable with no headset, no Blender, and no model
files. That matters more here than elsewhere: an inferred bone map that is plausible but wrong is
silently inherited by every later capability (posing, retargeting), so the validator is as load-bearing
as the inference. docs/backlogs/figures.md
"""

import pytest

from conjure.figures import CORE_BONES, infer_humanoid, node_world_positions, score, validate


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
