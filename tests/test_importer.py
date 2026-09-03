"""Unit tests for the extensible asset importer (conjure/importer.py) — pure, no server."""

import pytest
from conftest import FAKE_GLB, TINY_PNG, WIDE_PNG

from conjure.importer import ImportResult, _ext, _stereo_from_name, importable_extensions, plan_import


def test_ext_normalizes_jpeg_and_lowercases():
    assert _ext("Photo.JPEG") == ".jpg"
    assert _ext("a.PNG") == ".png"


def test_plan_image_reads_kind_and_dims():
    plan = plan_import("pic.png", TINY_PNG, {})
    assert isinstance(plan, ImportResult)
    assert plan.kind == "image" and plan.ext == ".png"
    assert (plan.width, plan.height) == (4, 4)
    assert plan.attributes == {}                         # a plain image carries no stereo tag


def test_plan_stereo_via_explicit_hint():
    plan = plan_import("pair.png", WIDE_PNG, {"stereo": "sbs"})
    assert plan.kind == "image" and plan.attributes["stereo"] == "sbs"


def test_plan_stereo_via_kind_hint_defaults_sbs():
    plan = plan_import("pair.png", WIDE_PNG, {"kind": "stereo"})
    assert plan.attributes["stereo"] == "sbs"


def test_plan_stereo_from_filename_convention():
    assert plan_import("beach_SBS.jpg", WIDE_PNG, {}).attributes["stereo"] == "sbs"
    assert plan_import("beach_TB.jpg", WIDE_PNG, {}).attributes["stereo"] == "tb"


def test_plan_unknown_extension_is_none():
    assert plan_import("notes.txt", b"hello", {}) is None


def test_plan_corrupt_image_fails_sniff():
    assert plan_import("broken.png", b"not really a png", {}) is None


def test_plan_glb_model_sniffs_magic_and_sets_kind():
    plan = plan_import("tree.glb", FAKE_GLB, {"licence": "CC0"})
    assert plan.kind == "model" and plan.ext == ".glb"
    assert plan.licence == "CC0" and plan.label == "tree"   # label seed from the stem


def test_plan_glb_rejects_non_gltf_bytes():
    assert plan_import("fake.glb", b"PK\x03\x04 zip not glb", {}) is None


def test_importable_extensions_cover_images_and_models():
    exts = importable_extensions()
    assert {".png", ".jpg", ".jpeg", ".webp", ".glb"} <= exts


# ---- rigged models: the bbox trimesh gets wrong -------------------------------------------------
# Regression for the 2026-09-01 field finding. trimesh reported a 1.757 m figure as 3.369 m — it
# applies node transforms to skinned meshes, whose vertices are ALREADY in skin space — and
# `_normalize` divides by that, so she placed at 53 % scale, a child-sized doll. Invisible on static
# props, which is why it survived: only a rigged model exercises it.

import json
import struct

from conjure.importer import glb_bounds, read_glb_json


def _glb(doc: dict) -> bytes:
    """Minimal valid GLB carrying `doc` as its JSON chunk (no BIN chunk needed — we only read JSON)."""
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)                       # chunks are 4-byte aligned
    return (b"glTF" + struct.pack("<II", 2, 12 + 8 + len(body))
            + struct.pack("<II", len(body), 0x4E4F534A) + body)


def _figure_doc(*, skinned: bool, node_scale=None, joint_scale=None):
    """A one-primitive model 1.75 m tall, optionally skinned, optionally under a scaled node.

    The joints are their OWN nodes, as in a real file — an armature beside the mesh rather than the mesh
    node doubling as its own joint. That distinction is the whole subject of the two tests below: a scale
    on the MESH node must not reach a skinned vertex, and a scale on the JOINTS must.
    """
    node = {"mesh": 0}
    if skinned:
        node["skin"] = 0
    if node_scale:
        node["scale"] = node_scale
    doc = {
        "scenes": [{"nodes": [0]}], "scene": 0, "nodes": [node],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
        "accessors": [{"min": [-0.5, 0.0, -0.2], "max": [0.5, 1.75, 0.2]}],
    }
    if skinned:
        armature = {"name": "armature", "children": [2, 3, 4]}
        if joint_scale:
            armature["scale"] = joint_scale
        doc["nodes"] += [armature] + [{"name": f"joint{i}"} for i in range(3)]
        doc["scenes"][0]["nodes"].append(1)
        doc["skins"] = [{"joints": [2, 3, 4]}]
    return doc


def test_read_glb_json_returns_the_document():
    doc = _figure_doc(skinned=True)
    assert read_glb_json(_glb(doc))["meshes"] == doc["meshes"]


def test_read_glb_json_rejects_non_glb():
    assert read_glb_json(b"not a glb at all") is None
    assert read_glb_json(b"") is None


def test_a_skinned_mesh_ignores_its_node_transform():
    """The whole point. A 2x-scaled node over a skinned mesh must NOT double the height — the vertices
    are in skin space, and the joints (not the node) place them. This is the case trimesh gets wrong."""
    lo, hi, rigged = glb_bounds(_figure_doc(skinned=True, node_scale=[2.0, 2.0, 2.0]))
    assert rigged is True
    assert hi[1] - lo[1] == pytest.approx(1.75)           # NOT 3.5


def _skinned_glb(joint_y: float):
    """A GLB whose one vertex is bound to a joint standing `joint_y` above the origin — POSITION says
    the mesh is at the origin, the JOINT says otherwise, and only skinning knows which is true."""
    # The bind was at the ORIGIN (identity inverse-bind) while the joint now stands at `joint_y`, so the
    # skinning carries the vertex up with it. That gap between bind pose and rest pose is exactly what
    # the real files have — and what makes their accessor boxes meaningless.
    ibm = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]
    bin_chunk = (struct.pack("<3f", 0.0, 0.0, 0.0) + struct.pack("<3f", 0.1, 0.2, 0.1)  # positions
                 + struct.pack("<4H", 0, 0, 0, 0) * 2                                   # joints
                 + struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) * 2                           # weights
                 + struct.pack("<16f", *ibm))
    doc = {
        "scenes": [{"nodes": [0, 1]}], "scene": 0,
        "nodes": [{"mesh": 0, "skin": 0}, {"name": "joint", "translation": [0.0, joint_y, 0.0]}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "JOINTS_0": 1, "WEIGHTS_0": 2}}]}],
        "skins": [{"joints": [1], "inverseBindMatrices": 3}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 2, "type": "VEC3",
             "min": [0.0, 0.0, 0.0], "max": [0.1, 0.2, 0.1]},
            {"bufferView": 1, "componentType": 5123, "count": 2, "type": "VEC4"},
            {"bufferView": 2, "componentType": 5126, "count": 2, "type": "VEC4"},
            {"bufferView": 3, "componentType": 5126, "count": 1, "type": "MAT4"}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 24},
                        {"buffer": 0, "byteOffset": 24, "byteLength": 16},
                        {"buffer": 0, "byteOffset": 40, "byteLength": 32},
                        {"buffer": 0, "byteOffset": 72, "byteLength": 64}],
        "buffers": [{"byteLength": len(bin_chunk)}],
    }
    return doc, bin_chunk


def test_bounds_follow_the_joints_not_the_accessor():
    """The measured failure, and the reason `she's huge` reached the headset. Several rigs author every
    body part as a cluster near the ORIGIN and let each joint carry it into place: `Steve` and one
    `Animated Woman` read 1.3 cm and 3.7 mm from their accessors against true heights of 2.7 m and 1.8 m.
    No scale factor recovers that — the vertices have to be skinned."""
    doc, blob = _skinned_glb(joint_y=1.75)
    lo, hi, rigged = glb_bounds(doc, blob)
    assert rigged is True
    assert hi[1] == pytest.approx(1.95) and lo[1] == pytest.approx(1.75), "the joint places the vertices"
    # Without the binary chunk there is nothing to skin with, and the accessor box is all there is.
    assert glb_bounds(doc)[1][1] == pytest.approx(0.2)


def test_a_scale_on_the_ARMATURE_does_reach_a_skinned_mesh():
    """The complement, and a real file: `Steve` carries a `CharacterArmature` scaled x100, so his
    vertices reach the world a hundred times bigger than the accessor says. He was catalogued at 1.3
    CENTIMETRES — and, now that the fetch path marks him as a figure, would have been placed at it."""
    lo, hi, rigged = glb_bounds(_figure_doc(skinned=True, joint_scale=[100.0, 100.0, 100.0]))
    assert rigged is True
    assert hi[1] - lo[1] == pytest.approx(175.0)


def test_an_unskinned_mesh_does_apply_its_node_transform():
    # The complement: for ordinary props the node transform is real and must be honoured.
    lo, hi, rigged = glb_bounds(_figure_doc(skinned=False, node_scale=[2.0, 2.0, 2.0]))
    assert rigged is False
    assert hi[1] - lo[1] == pytest.approx(3.5)


def test_bounds_none_when_the_document_has_no_geometry():
    assert glb_bounds({"nodes": [], "meshes": [], "accessors": []}) is None


def test_a_rigged_model_records_what_a_figure_needs():
    doc = _figure_doc(skinned=True)
    doc["animations"] = [{"name": "idle"}, {"name": "walk"}]
    plan = plan_import("figure.glb", _glb(doc), {})
    a = plan.attributes
    assert a["rigged"] is True
    assert a["height_m"] == pytest.approx(1.75)
    assert a["joints"] == [3] and a["clips"] == ["idle", "walk"]


def test_an_unrigged_model_carries_no_figure_fields():
    a = plan_import("prop.glb", _glb(_figure_doc(skinned=False)), {}).attributes
    assert "rigged" not in a and "height_m" not in a and "clips" not in a


# ---- VRM: a humanoid map stated outright ---------------------------------------------------------
# A .vrm IS a GLB with extra extension blocks, and its humanoid map is the artifact the whole figures
# pipeline exists to manufacture — posing, retargeting and "raise her left arm" all need a per-model
# translation from a semantic bone name to that model's own node. VRM states it; everything else must
# be inferred (docs/backlogs/figures.md, discovery layers).

from conjure.importer import vrm_humanoid


def _vrm_doc(version="1.0"):
    doc = _figure_doc(skinned=True)
    doc["nodes"] = [{"mesh": 0, "skin": 0}, {"name": "J_Bip_C_Hips"}, {"name": "J_Bip_L_UpperArm"}]
    if version == "1.0":
        doc["extensions"] = {"VRMC_vrm": {"humanoid": {"humanBones": {
            "hips": {"node": 1}, "leftUpperArm": {"node": 2}}}}}
        doc["extensionsUsed"] = ["VRMC_vrm", "VRMC_springBone"]
    else:
        doc["extensions"] = {"VRM": {"humanoid": {"humanBones": [
            {"bone": "hips", "node": 1}, {"bone": "leftUpperArm", "node": 2}]}}}
    return doc


def test_vrm_1_0_humanoid_map_is_read():
    assert vrm_humanoid(_vrm_doc("1.0")) == {"hips": "J_Bip_C_Hips", "leftUpperArm": "J_Bip_L_UpperArm"}


def test_vrm_0_x_humanoid_map_is_read():
    # 0.x stores humanBones as a LIST of {bone, node} rather than a dict — both are in the wild.
    assert vrm_humanoid(_vrm_doc("0.x")) == {"hips": "J_Bip_C_Hips", "leftUpperArm": "J_Bip_L_UpperArm"}


def test_humanoid_map_stores_node_NAMES_not_indices():
    # Names survive a re-export that reorders nodes; indices silently point at the wrong bone.
    assert all(isinstance(v, str) for v in vrm_humanoid(_vrm_doc()).values())


def test_a_plain_glb_has_no_humanoid_map():
    assert vrm_humanoid(_figure_doc(skinned=True)) is None


def test_a_stated_map_also_gets_its_anatomical_frame_measured():
    """The bone map says which node; the frame says which way to rotate it. Both are properties of the
    FILE, measured once here so no consumer has to re-derive them (docs/backlogs/figures.md)."""
    doc = _vrm_doc()
    doc["nodes"] = [{"mesh": 0, "skin": 0},
                    {"name": "J_Bip_C_Hips", "translation": [0, 1.0, 0], "children": [3]},
                    {"name": "J_Bip_L_UpperArm", "translation": [0.2, 0.4, 0], "children": [4]},
                    {"name": "J_Bip_C_Head", "translation": [0, 0.6, 0]},
                    {"name": "J_Bip_L_LowerArm", "translation": [0.3, 0, 0]}]
    doc["extensions"]["VRMC_vrm"]["humanoid"]["humanBones"].update(
        {"head": {"node": 3}, "leftLowerArm": {"node": 4}})
    doc["scenes"] = [{"nodes": [0, 1, 2]}]
    a = plan_import("figure.vrm", _glb(doc), {}).attributes
    assert a["humanoid_source"] == "vrm"
    frame = a["humanoid_axes"]["leftUpperArm"]
    # three rotations to swing about, plus the bind-pose vectors an absolute aim swings FROM
    assert sorted(frame) == ["bend", "forward", "limits", "out", "rest", "spread", "turn", "up"]
    # The arm points along +X, so its rest direction and its twist axis are its own length — the one
    # axis that is unambiguous whatever the rig, and a cheap check that the frame belongs to THIS bone
    # rather than to the body.
    assert frame["turn"] == pytest.approx([1, 0, 0], abs=1e-4)
    assert frame["rest"] == pytest.approx([1, 0, 0], abs=1e-4)


def test_an_unrigged_model_gets_no_anatomical_frame():
    assert "humanoid_axes" not in plan_import("prop.glb", _glb(_figure_doc(skinned=False)), {}).attributes


def test_a_vrm_imports_as_a_model_and_records_its_humanoid_map():
    plan = plan_import("saka.vrm", _glb(_vrm_doc()), {})
    assert plan.kind == "model"
    assert plan.ext == ".glb", "a .vrm is stored as .glb so the client needs no special case"
    a = plan.attributes
    assert a["humanoid_source"] == "vrm" and a["humanoid"]["hips"] == "J_Bip_C_Hips"
    assert a["spring_bones"] is True


def test_vrm_extension_is_importable():
    assert ".vrm" in importable_extensions()
