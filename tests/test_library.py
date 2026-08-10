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


def test_find_tiers_exact_and_alias_strong_fts_weak_none(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("oak.glb", kind="model", label="Oak Tree", query="oak tree")
    lib.upsert("hill.glb", kind="model", label="a big oak on a hill", query="a big oak on a hill")
    assert lib.find("oak tree")["confidence_tier"] == "strong"        # exact intent match
    assert lib.find("zebra unicorn")["confidence_tier"] == "none"     # nothing
    # "oak" matches the hill model on FTS but nothing exactly → weak
    weak = lib.find("oak")
    assert weak["confidence_tier"] == "weak" and any(c["id"] == "hill.glb" for c in weak["candidates"])
    lib.set_alias("tree", "oak.glb")
    assert lib.find("tree")["confidence_tier"] == "strong"            # alias is authoritative


def test_find_excludes_rejected_assets(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("xwing.glb", kind="model", label="X-Wing", query="starship enterprise")  # wrong fetch
    assert any(c["id"] == "xwing.glb" for c in lib.find("starship enterprise")["candidates"])
    lib.reject("xwing.glb", "starship enterprise")
    res = lib.find("starship enterprise")
    assert not any(c["id"] == "xwing.glb" for c in res["candidates"]) and res["confidence_tier"] == "none"


def test_find_more_like_this_via_query_vec(tmp_path):
    lib = _lib(tmp_path)
    if not lib.has_vectors:
        pytest.skip("sqlite-vec not available")
    for aid, vec in [("a.png", [1.0, 0.0]), ("b.png", [0.0, 1.0])]:
        lib.upsert(aid, kind="image", prompt=aid)
        lib.add_embedding(aid, vec, "fake")
    res = lib.find(query_vec=[0.96, 0.05])           # image-only query, no text
    assert res["candidates"][0]["id"] == "a.png" and res["candidates"][0]["match"] == "vector"
    assert res["confidence_tier"] == "weak"          # semantic-only → weak (no exact/alias)


def test_assets_missing_caption_targets_bare_visual_assets(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("bare.png", kind="image")                           # no label → needs a caption
    lib.upsert("labeled.png", kind="image", label="a red dragon")  # has one → skip
    lib.upsert("m.glb", kind="model", label="Oak Tree")            # model (has a title) → skip
    ids = {a["id"] for a in lib.assets_missing_caption(("image", "skybox", "grounded_skybox", "photo"))}
    assert ids == {"bare.png"}


def test_retag_wide_images_as_skyboxes(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("pano.png", kind="image", width=2100, height=900, prompt="a sunset beach")  # 2.33:1
    lib.upsert("square.png", kind="image", width=512, height=512, prompt="a cat")          # 1:1
    if lib.has_vectors:
        lib.add_embedding("pano.png", [1.0, 0.0], "fake")
    assert lib.retag_skyboxes() == 1
    assert lib.get("pano.png")["kind"] == "skybox"      # wide → skybox
    assert lib.get("square.png")["kind"] == "image"     # square stays an image
    if lib.has_vectors:                                  # vector's kind metadata fixed in place
        assert any(h["id"] == "pano.png" for h in lib.vector_search([1.0, 0.0], kind="skybox"))


def test_update_enforces_scope_and_syncs_kind(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("m.png", kind="skybox", scope="private/builder", label="beach")
    if lib.has_vectors:
        lib.add_embedding("m.png", [1.0, 0.0], "fake")
    ok, err = lib.update("m.png", scope="private/dm", kind="grounded_skybox")
    assert not ok and "scope" in err                              # wrong scope refused
    ok, _ = lib.update("m.png", scope="private/builder", kind="grounded_skybox", favorite=True)
    assert ok
    rec = lib.get("m.png")
    assert rec["kind"] == "grounded_skybox" and rec["favorite"] == 1
    if lib.has_vectors:                                           # vector's kind metadata synced too
        assert any(h["id"] == "m.png" for h in lib.vector_search([1.0, 0.0], kind="grounded_skybox"))


def test_delete_cleans_up_row_alias_relation_and_vector(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", prompt="x")
    lib.set_alias("dog", "a.png")
    lib.add_relation("b.png", "a.png", "derived_from")
    if lib.has_vectors:
        lib.add_embedding("a.png", [1.0, 0.0], "fake")
    ok, _ = lib.delete("a.png")
    assert ok and lib.get("a.png") is None
    assert lib.resolve_alias("dog") is None                       # alias removed
    rel = lib._db.execute("SELECT COUNT(*) FROM relations WHERE from_id='a.png' OR to_id='a.png'").fetchone()[0]
    assert rel == 0                                               # relations removed
    if lib.has_vectors:
        assert not lib.vector_search([1.0, 0.0])                  # vector removed


def test_query_is_scoped_and_select_only(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("a.png", kind="image", scope="daniel/agents/builder", label="mine")
    lib.upsert("b.png", kind="image", scope="friend/agents/builder", label="theirs", public=0)
    rows = lib.query("SELECT id FROM assets", scope="daniel/agents/builder")
    assert [r["id"] for r in rows] == ["a.png"]                   # my scope; friend's PRIVATE row hidden
    import pytest as _pytest
    with _pytest.raises(ValueError):
        lib.query("DELETE FROM assets", scope="daniel/agents/builder")  # writes rejected
    with _pytest.raises(ValueError):
        lib.query("SELECT * FROM main.assets", scope="daniel/agents/builder")  # bypass rejected


def test_update_sets_public_and_is_scope_checked(tmp_path):
    lib = _lib(tmp_path)
    me, friend = "daniel/agents/builder", "friend/agents/builder"
    lib.upsert("mine.png", kind="image", scope=me, label="bridge", query="bridge")
    lib.upsert("theirs.png", kind="image", scope=friend, label="theirs")
    # make my asset private → it drops out of another scope's reads
    ok, err = lib.update("mine.png", scope=me, public=False)
    assert ok and err is None and lib.get("mine.png")["public"] == 0
    assert "mine.png" not in {r["id"] for r in lib.query("SELECT id FROM assets", scope=friend)}
    # share it again
    lib.update("mine.png", scope=me, public=True)
    assert "mine.png" in {r["id"] for r in lib.query("SELECT id FROM assets", scope=friend)}
    # I can't change someone else's asset (only shows up via public reads)
    ok, err = lib.update("theirs.png", scope=me, public=False)
    assert not ok and "scope" in err


def test_reads_return_own_scope_plus_public(tmp_path):
    """Cross-scope public reads (co-location §8a): a caller sees its own rows + any user's public rows,
    but never another scope's private rows — across query/search/find."""
    lib = _lib(tmp_path)
    me, friend = "daniel/agents/builder", "friend/agents/builder"
    lib.upsert("mine_pub.glb", kind="model", scope=me, label="my oak", query="oak tree")
    lib.upsert("mine_priv.glb", kind="model", scope=me, label="my secret oak", query="oak tree", public=0)
    lib.upsert("friend_pub.glb", kind="model", scope=friend, label="friend oak", query="oak tree")
    lib.upsert("friend_priv.glb", kind="model", scope=friend, label="friend secret oak", query="oak tree", public=0)

    visible = {r["id"] for r in lib.query("SELECT id FROM assets", scope=me)}
    assert visible == {"mine_pub.glb", "mine_priv.glb", "friend_pub.glb"}   # friend's private excluded

    found = {c["id"] for c in lib.find("oak tree", scope=me)["candidates"]}
    assert "friend_pub.glb" in found and "friend_priv.glb" not in found and "mine_priv.glb" in found


def test_assets_are_hard_scoped_to_their_agent(tmp_path):
    """The agent segment is an absolute wall: a `builder` caller never sees an `outdoor` asset — even
    a PUBLIC one — across query/search/find; but same-agent, cross-user public still shows."""
    lib = _lib(tmp_path)
    builder, outdoor = "daniel/agents/builder", "daniel/agents/outdoor"
    friend_builder = "friend/agents/builder"
    lib.upsert("b.glb", kind="model", scope=builder, label="oak", query="oak tree")            # own
    lib.upsert("o.glb", kind="model", scope=outdoor, label="oak", query="oak tree")            # other agent, public
    lib.upsert("fb.glb", kind="model", scope=friend_builder, label="oak", query="oak tree")    # other user, same agent, public

    q = {r["id"] for r in lib.query("SELECT id FROM assets", scope=builder)}
    assert q == {"b.glb", "fb.glb"}                       # own + same-agent public; NOT o.glb (other agent)

    s = {h["id"] for h in lib.search("oak tree", scope=builder)}
    assert "o.glb" not in s and {"b.glb", "fb.glb"} <= s

    f = {c["id"] for c in lib.find("oak tree", scope=builder)["candidates"]}
    assert "o.glb" not in f and {"b.glb", "fb.glb"} <= f

    # …and the wall is symmetric: outdoor sees only its own agent's assets.
    assert {r["id"] for r in lib.query("SELECT id FROM assets", scope=outdoor)} == {"o.glb"}


def test_vector_search_is_walled_by_agent(tmp_path):
    lib = _lib(tmp_path)
    if not lib.has_vectors:
        pytest.skip("sqlite-vec not available")
    lib.upsert("b.png", kind="image", scope="daniel/agents/builder", prompt="x")
    lib.upsert("o.png", kind="image", scope="daniel/agents/outdoor", prompt="x")   # other agent, public
    lib.add_embedding("b.png", [1.0, 0.0, 0.0], "fake")
    lib.add_embedding("o.png", [1.0, 0.0, 0.0], "fake")                            # identical vector
    hits = {h["id"] for h in lib.vector_search([1.0, 0.0, 0.0], scope="daniel/agents/builder", limit=5)}
    assert hits == {"b.png"}                              # outdoor's public asset is walled off


def test_agent_of_extracts_the_agent_segment():
    from conjure.config import agent_of
    assert agent_of("daniel/agents/builder") == "builder"
    assert agent_of("friend/agents/outdoor") == "outdoor"
    assert agent_of("legacy-no-agents") == "legacy-no-agents"   # no /agents/ → matches only itself
    assert agent_of("") == ""


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


def test_transparent_column_roundtrips(tmp_path):
    lib = _lib(tmp_path)
    lib.upsert("op.png", kind="image", filename="op.png", label="opaque", transparent=0)
    lib.upsert("tr.png", kind="image", filename="tr.png", label="cutout", transparent=1)
    lib.upsert("un.png", kind="image", filename="un.png", label="unchecked")  # transparent stays NULL
    assert lib.get("tr.png")["transparent"] == 1
    assert lib.get("op.png")["transparent"] == 0
    assert lib.get("un.png")["transparent"] is None


def test_migration_v4_to_v6_preserves_data_reworks_scope_and_adds_public(tmp_path):
    """A schema bump must ALTER, never DROP — and v4→v6 cumulatively adds transparent (v5), the public
    flag, and rewrites scope to user-first <user>/agents/<agent> (v6)."""
    import sqlite3
    path = tmp_path / "library.db"
    raw = sqlite3.connect(str(path))
    raw.execute("CREATE TABLE assets (id TEXT PRIMARY KEY, kind TEXT, label TEXT, "
                "filename TEXT, scope TEXT, notes TEXT)")
    raw.execute("INSERT INTO assets (id, kind, label, scope, notes) VALUES "
                "('keep.png','image','my dragon','private/builder','my favorite')")
    raw.execute("PRAGMA user_version = 4")
    raw.commit()
    raw.close()

    lib = AssetLibrary(path)                      # opens at v4 → cumulative migrate to v6
    rec = lib.get("keep.png")
    assert rec is not None and rec["label"] == "my dragon" and rec["notes"] == "my favorite"
    assert "transparent" in rec and rec["transparent"] is None     # v4→v5 column
    assert rec["public"] == 1                                      # v5→v6 flag, default public
    assert rec["scope"] == "daniel/agents/builder"                 # v5→v6 user-first scope rewrite
    assert lib._db.execute("PRAGMA user_version").fetchone()[0] == 6
