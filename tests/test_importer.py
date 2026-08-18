"""Unit tests for the extensible asset importer (conjure/importer.py) — pure, no server."""

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
