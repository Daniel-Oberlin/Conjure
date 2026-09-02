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


def _figure_doc(*, skinned: bool, node_scale=None):
    """A one-primitive model 1.75 m tall, optionally skinned, optionally under a scaled node."""
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
        doc["skins"] = [{"joints": [0, 0, 0]}]
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
