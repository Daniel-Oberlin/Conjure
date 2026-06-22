"""AssetLibrary — the durable asset catalog (docs/asset-library-plan.md, Phase 0)."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from conjure.library import AssetLibrary, normalize


def _lib(tmp_path):
    return AssetLibrary(tmp_path / "library.db")


def test_upsert_and_get_roundtrips_fields(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", source="cache://a.png", filename="a.png",
               label="a red dragon", prompt="a red dragon", provider="Gemini", model="m",
               width=4, height=4, params={"op": "generate", "transparent": False})
    rec = lib.get("a.png")
    assert rec["kind"] == "image" and rec["provider"] == "Gemini" and rec["prompt"] == "a red dragon"
    assert json.loads(rec["params_json"]) == {"op": "generate", "transparent": False}
    assert rec["use_count"] == 0 and rec["created_at"] == rec["last_used"]


def test_partial_update_does_not_clobber_existing_fields(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", prompt="a red dragon", provider="Gemini")
    lib.upsert("a.png", embed_model="siglip", embed_dim=1152)  # later partial write
    rec = lib.get("a.png")
    assert rec["prompt"] == "a red dragon" and rec["provider"] == "Gemini"  # preserved
    assert rec["embed_model"] == "siglip" and rec["embed_dim"] == 1152


def test_attributes_merge_does_not_drop_existing_keys(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("m.glb", kind="model", attributes={"tris": 900, "bbox_min": [0, 0, 0]})
    lib.upsert("m.glb", attributes={"tris": 1200})       # later partial write
    attrs = json.loads(lib.get("m.glb")["attributes"])
    assert attrs == {"tris": 1200, "bbox_min": [0, 0, 0]}  # tris updated, bbox preserved


def test_annotate_sets_curation_and_is_searchable(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("s.png", kind="skybox", prompt="a neon cityscape")
    assert lib.annotate("s.png", note="my favorite city skybox", favorite=True, rating=5)
    rec = lib.get("s.png")
    assert rec["notes"] == "my favorite city skybox" and rec["favorite"] == 1 and rec["rating"] == 5
    # the note's words are now findable even though the prompt never said "favorite"
    assert any(h["id"] == "s.png" for h in lib.search("favorite skybox"))


def test_annotate_unknown_asset_returns_false(tmp_path):
    assert _lib(tmp_path).annotate("nope.png", note="x") is False


def test_alias_is_authoritative_override_in_search(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("rex.glb", kind="model", label="Shiba Inu", query="shiba inu")
    lib.upsert("other.glb", kind="model", label="dog", query="dog")  # would win an exact match
    lib.annotate("rex.glb", default_for="dog")
    hits = lib.search("dog")
    assert hits[0]["id"] == "rex.glb" and hits[0]["match"] == "alias"  # alias beats the exact "dog"
    assert lib.resolve_alias("Dog") == "rex.glb"                       # normalized lookup


def test_touch_bumps_recency_and_count(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", prompt="x")
    before = lib.get("a.png")
    lib.touch("a.png")
    after = lib.get("a.png")
    assert after["use_count"] == 1 and after["last_used"] >= before["last_used"]


def test_exact_match_takes_precedence_and_normalizes(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", label="Oak Tree", prompt="Oak Tree")
    hits = lib.search("  oak   tree ")  # case/whitespace-insensitive
    assert hits and hits[0]["id"] == "a.png" and hits[0]["match"] == "exact"
    assert normalize("  Oak   Tree ") == "oak tree"


def test_fts_matches_on_keyword_overlap(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", label="a big oak tree on a hill", prompt="a big oak tree on a hill")
    hits = lib.search("oak tree")           # not an exact label match
    assert any(h["id"] == "a.png" and h["match"] == "fts" for h in hits)


def test_search_filters_by_kind(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", label="dragon", prompt="dragon")
    lib.upsert("d.glb", kind="model", label="dragon", query="dragon")
    assert [h["id"] for h in lib.search("dragon", kind="model")] == ["d.glb"]


def test_relations_are_unique(tmp_path):
    lib = _lib(tmp_path)
    lib.add_relation("b.png", "a.png", "derived_from")
    lib.add_relation("b.png", "a.png", "derived_from")  # idempotent
    rows = lib._db.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
    assert rows == 1


def test_backfill_seeds_images_and_models_idempotently(tmp_path):
    cache = tmp_path / "assets"
    cache.mkdir()
    buf = io.BytesIO()
    Image.new("RGB", (8, 6), "blue").save(buf, "PNG")
    (cache / "img1.png").write_bytes(buf.getvalue())
    (cache / "mdl1.glb").write_bytes(b"glTF" + bytes(8))
    (cache / "mdl1.json").write_text(json.dumps(
        {"title": "Oak Tree", "licence": "CC-BY", "attribution": "by T", "creator": "T", "tris": 900}))

    lib = _lib(tmp_path)
    added = lib.backfill(cache)
    assert added == 2
    img = lib.get("img1.png")
    assert img["kind"] == "image" and img["width"] == 8 and img["height"] == 6
    mdl = lib.get("mdl1.glb")
    assert mdl["kind"] == "model" and mdl["licence"] == "CC-BY" and mdl["query"] == "Oak Tree"
    assert json.loads(mdl["attributes"])["tris"] == 900   # kind-specific field lives in attributes
    assert lib.backfill(cache) == 0  # nothing new the second time


def test_vector_search_returns_nearest_and_records_space(tmp_path):
    lib = _lib(tmp_path)
    if not lib.has_vectors:
        pytest.skip("sqlite-vec not available")
    for aid in ("a.png", "b.png", "c.png"):
        lib.upsert(aid, kind="image", prompt=aid)
    lib.add_embedding("a.png", [1.0, 0.0, 0.0], "fake")
    lib.add_embedding("b.png", [0.9, 0.1, 0.0], "fake")
    lib.add_embedding("c.png", [0.0, 0.0, 1.0], "fake")
    hits = lib.vector_search([1.0, 0.0, 0.0], limit=2)
    assert [h["id"] for h in hits] == ["a.png", "b.png"] and hits[0]["match"] == "vector"
    assert hits[0]["distance"] <= hits[1]["distance"]
    rec = lib.get("a.png")
    assert rec["embed_model"] == "fake" and rec["embed_dim"] == 3   # space recorded for comparability


def test_vector_search_filters_by_kind(tmp_path):
    lib = _lib(tmp_path)
    if not lib.has_vectors:
        pytest.skip("sqlite-vec not available")
    lib.upsert("img.png", kind="image", prompt="x")
    lib.upsert("mdl.glb", kind="model", query="x")
    lib.add_embedding("img.png", [1.0, 0.0], "fake")
    lib.add_embedding("mdl.glb", [1.0, 0.0], "fake")
    assert [h["id"] for h in lib.vector_search([1.0, 0.0], kind="model")] == ["mdl.glb"]


def test_backfill_recovers_prompt_from_world_doc(tmp_path):
    cache = tmp_path / "assets"
    cache.mkdir()
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "PNG")
    (cache / "img1.png").write_bytes(buf.getvalue())
    world = {"entities": [{"id": "e1", "meta": {"image_id": "img1.png", "prompt": "a sunset"}}]}

    lib = _lib(tmp_path)
    lib.backfill(cache, world)
    assert lib.get("img1.png")["prompt"] == "a sunset"
