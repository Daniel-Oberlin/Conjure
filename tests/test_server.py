"""Integration tests for the world-server endpoints (the seams our MCP tools + client depend on),
with external services faked. No network, no keys, no LLM."""

import pytest

from conftest import ASSET_RECORD, FakeAssetResolver

from conjure.schema import Patch


def _entities(client):
    return client.get("/world").json()["entities"]


def test_patch_endpoint_applies_and_returns_rev(client):
    r = client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "box", "components": {"geometry": {"primitive": "box"}}}}]})
    assert r.status_code == 200 and r.json()["rev"] == 1
    assert any(e["id"] == "box" for e in _entities(client))


def test_place_asset_normalizes_to_size_and_sits_on_floor(srv, client):
    srv.resolver = FakeAssetResolver(record=ASSET_RECORD)
    r = client.post("/place_asset", json={"query": "oak tree", "size_m": 8, "position": [0, 0, -3]})
    assert r.json()["ok"] is True
    asset = next(e for e in _entities(client) if "gltf-model" in e.get("components", {}))
    # bbox is 4 tall (max dim), size_m 8 → scale 2; min_y -2 → base offset +4 so the base sits at y=0.
    assert asset["components"]["gltf-model"] == "/assets/abc123def456.glb"
    assert asset["transform"]["scale"] == [2.0, 2.0, 2.0]
    assert asset["transform"]["position"] == [0.0, 4.0, -3.0]
    assert asset["meta"]["license"] == "CC-BY 3.0"


def test_place_asset_failure_leaves_no_garbage_entity(srv, client):
    srv.resolver = FakeAssetResolver(error=RuntimeError("403 Forbidden"))
    r = client.post("/place_asset", json={"query": "x", "size_m": 1})
    assert r.json()["ok"] is False and "403" in r.json()["error"]
    # The instant placeholder must have been removed — no leftover asset entity.
    assert not any(e["id"].startswith("ent_asset") for e in _entities(client))


def _procure(client, prompt="a red dragon", **extra) -> str:
    """Procure an image and return its id (the new decoupled first step)."""
    r = client.post("/images/generate", json={"prompt": prompt, **extra})
    assert r.json()["ok"] is True, r.json()
    return r.json()["image_id"]


def test_procurement_returns_id_without_touching_the_world(srv, client):
    before = len(_entities(client))
    body = client.post("/images/generate", json={"prompt": "a red dragon"}).json()
    assert body["ok"] and body["image_id"].endswith(".png") and body["url"].startswith("/assets/")
    assert body["provider"] == "Gemini"
    assert len(_entities(client)) == before              # no scene effect
    assert client.get(body["url"]).status_code == 200    # bytes actually cached + served


def test_place_image_takes_an_id_and_hangs_aspect_correct_plane(srv, client):
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "size_m": 1.0})
    assert r.json()["ok"] is True
    img = next(e for e in _entities(client) if e.get("components", {}).get("material", {}).get("src"))
    assert img["components"]["material"]["src"] == f"/assets/{image_id}"
    assert img["meta"]["generated"] is True and img["meta"]["image_id"] == image_id
    # 4x4 image → square plane (longest side = size_m).
    assert img["components"]["geometry"]["width"] == 1.0
    assert img["components"]["geometry"]["height"] == 1.0


def test_place_image_on_surface_aligns_and_fits_the_frame(srv, client):
    # A wall-art frame at a known (upright) orientation and size.
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [0.7, 1.72, -1.04],
         "rotation": [0.0, -41.0, 0.0], "extent": [0.5, 0.4]}]})
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 18"}).json()
    assert r["ok"] is True
    img = next(e for e in _entities(client) if e["id"] == r["id"])
    # adopts the surface's orientation — no longer world-axis-aligned (that was the tilt-on-the-wall bug)
    assert img["transform"]["rotation"] == [0.0, -41.0, 0.0]
    # fitted inside the 0.5 x 0.4 frame (square image ⇒ 0.4 x 0.4), not the default 1 m floating plane
    g = img["components"]["geometry"]
    assert g["width"] <= 0.5 + 1e-9 and g["height"] <= 0.4 + 1e-9 and max(g["width"], g["height"]) > 0.1
    # sits a couple cm in front of the surface (no z-fight), not exactly coplanar
    import math
    assert 0.01 < math.dist(img["transform"]["position"], [0.7, 1.72, -1.04]) < 0.05


def test_on_surface_image_re_anchors_when_the_surface_moves(srv, client):
    import math
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [0.7, 1.72, -1.04],
         "rotation": [0.0, -41.0, 0.0], "extent": [0.5, 0.4]}]})
    r = client.post("/place_image", json={"image_id": _procure(client), "on_surface": "wall art 18"}).json()
    eid = r["id"]
    img = next(e for e in _entities(client) if e["id"] == eid)
    assert img["meta"]["on_surface"] == "real_wall_art_18"            # home surface recorded
    p0 = img["transform"]["position"]
    # re-capture: the surface moved ~0.7 m (a re-registration). The image must FOLLOW, not strand.
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [1.2, 1.6, -0.5],
         "rotation": [0.0, -41.0, 0.0], "extent": [0.5, 0.4]}]})
    img2 = next(e for e in _entities(client) if e["id"] == eid)
    assert 0.01 < math.dist(img2["transform"]["position"], [1.2, 1.6, -0.5]) < 0.05   # ~2 cm off the NEW pose
    assert math.dist(img2["transform"]["position"], p0) > 0.3         # it actually moved with the surface


def test_reanchor_surface_images_repins_stranded_on_compose():
    import math
    from conjure.server import _reanchor_surface_images
    doc = {"entities": [
        {"id": "real_wall_5", "meta": {"real": True, "semantic": "wall"},
         "transform": {"position": [2.0, 1.5, 0.0], "rotation": [0.0, 90.0, 0.0]},
         "components": {"surface": {"extent": [1.0, 2.0]}}},
        {"id": "ent_image_x", "meta": {"on_surface": "real_wall_5", "image_id": "a.png"},
         "transform": {"position": [-9.0, -9.0, -9.0], "rotation": [0, 0, 0]},   # stranded (stale absolute pos)
         "components": {"geometry": {"primitive": "plane", "width": 0.5, "height": 0.5}}}]}
    _reanchor_surface_images(doc)
    img = doc["entities"][1]
    assert img["transform"]["rotation"] == [0.0, 90.0, 0.0]           # adopts the surface's orientation
    assert 0.01 < math.dist(img["transform"]["position"], [2.0, 1.5, 0.0]) < 0.05   # ~2 cm in front of the wall


def test_place_image_on_unknown_surface_errors(srv, client):
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 999"}).json()
    assert r["ok"] is False and "surface" in r["error"]


def test_place_image_unknown_id_errors(srv, client):
    assert client.post("/place_image", json={"image_id": "nope.png"}).json()["ok"] is False


def test_opaque_image_is_not_marked_transparent(srv, client):
    eid = client.post("/place_image", json={"image_id": _procure(client)}).json()["id"]
    mat = next(e for e in _entities(client) if e["id"] == eid)["components"]["material"]
    assert mat["transparent"] is False


def test_transparent_image_renders_with_transparency(srv, client):
    from conftest import FakeOpenAIImageGenerator
    srv.image_generators = {"Gemini": srv.image_generators["Gemini"], "Chat": FakeOpenAIImageGenerator()}
    gen = client.post("/images/generate", json={"prompt": "a star", "transparent": True}).json()
    assert gen["ok"]
    eid = client.post("/place_image", json={"image_id": gen["image_id"]}).json()["id"]
    mat = next(e for e in _entities(client) if e["id"] == eid)["components"]["material"]
    assert mat["transparent"] is True   # alpha image → plane renders with transparency on


def test_edit_image_updates_in_place(srv, client):
    eid = client.post("/place_image", json={"image_id": _procure(client)}).json()["id"]
    before = next(e for e in _entities(client) if e["id"] == eid)["components"]["material"]["src"]
    r = client.post("/edit_image", json={"id": eid, "prompt": "add a moon"})
    assert r.json()["ok"] is True
    after = next(e for e in _entities(client) if e["id"] == eid)
    assert after["components"]["material"]["src"] != before     # the texture swapped
    assert after["meta"]["image_id"] == r.json()["image_id"]     # meta tracks the new image


def test_outpaint_widens_the_plane(srv, client):
    eid = client.post("/place_image", json={"image_id": _procure(client), "size_m": 1.0}).json()["id"]
    r = client.post("/outpaint_image", json={"id": eid})
    assert r.json()["ok"] is True
    geo = next(e for e in _entities(client) if e["id"] == eid)["components"]["geometry"]
    assert geo["width"] == 2.0  # fake edit returns an 8x4 (2:1) image, height 1.0 → width 2.0


def test_edit_unknown_entity_errors(srv, client):
    assert client.post("/edit_image", json={"id": "nope", "prompt": "x"}).json()["ok"] is False


def test_set_skybox_takes_an_id(srv, client):
    r = client.post("/images/skybox", json={"prompt": "a sunset beach"})
    assert r.json()["ok"] is True
    assert client.post("/set_skybox", json={"image_id": r.json()["image_id"]}).json()["ok"] is True
    sky = client.get("/world").json()["environment"]["sky"]
    assert sky["src"] == f"/assets/{r.json()['image_id']}"


def test_generated_image_is_cataloged(srv, client):
    iid = client.post("/images/generate", json={"prompt": "a red dragon"}).json()["image_id"]
    cat = srv.library.get(iid)
    assert cat and cat["kind"] == "image" and cat["prompt"] == "a red dragon" and cat["provider"] == "Gemini"


def test_generated_image_is_embedded_when_embedder_present(srv, client):
    from conjure.embeddings import FakeEmbedder

    if not srv.library.has_vectors:
        pytest.skip("sqlite-vec not available")
    srv.embedder = FakeEmbedder(dim=8)
    iid = client.post("/images/generate", json={"prompt": "a red dragon"}).json()["image_id"]
    cat = srv.library.get(iid)
    assert cat["embed_model"] == "fake" and cat["embed_dim"] == 8         # vector written through
    hits = srv.library.vector_search(srv.embedder.embed_image(b"anything"))
    assert any(h["id"] == iid for h in hits)                              # and it's searchable


async def test_background_embed_lands_after_return(srv):
    """In prod mode the embed runs off the request path: scheduled, not done on return; lands once
    awaited. (exact/FTS still match the asset immediately — only its vector is briefly eventual.)"""
    import asyncio

    from conjure.embeddings import FakeEmbedder

    if not srv.library.has_vectors:
        pytest.skip("sqlite-vec not available")
    srv.embedder = FakeEmbedder(dim=8)
    srv._EMBED_BACKGROUND = True
    srv.library.upsert("x.png", kind="image", prompt="x")

    srv._embed_asset("x.png", text="x")                       # returns immediately (schedules a task)
    assert srv.library.get("x.png")["embed_model"] is None    # not embedded yet — it's off-path
    await asyncio.gather(*srv._embed_tasks)                    # let the background embed complete
    assert srv.library.get("x.png")["embed_model"] == "fake"  # vector landed after the return


def test_image_metadata_survives_restart(srv, client):
    iid = client.post("/images/skybox", json={"prompt": "a sunset beach"}).json()["image_id"]
    srv.IMAGES.clear()                       # simulate a restart: in-memory store gone, catalog persists
    rec, _, err = srv._get_image(iid)
    assert err is None
    assert rec.provider == "Gemini" and rec.prompt == "a sunset beach"  # recovered, not "?"


def test_placed_model_is_cataloged(srv, client):
    srv.resolver = FakeAssetResolver(record=ASSET_RECORD)
    client.post("/place_asset", json={"query": "oak tree", "size_m": 2})
    cat = srv.library.get(f"{ASSET_RECORD.hash}.glb")
    assert cat and cat["kind"] == "model" and cat["query"] == "oak tree"
    assert cat["licence"] == ASSET_RECORD.licence and cat["use_count"] == 1


def test_update_asset_sets_fields_kind_and_alias(srv, client):
    iid = client.post("/images/generate", json={"prompt": "a shiba inu"}).json()["image_id"]
    r = client.post("/update_asset", json={"id": iid, "notes": "my default dog", "favorite": True,
                                           "kind": "photo", "default_for": "dog"})
    assert r.json()["ok"] is True
    cat = srv.library.get(iid)
    assert cat["notes"] == "my default dog" and cat["favorite"] == 1 and cat["kind"] == "photo"
    assert srv.library.resolve_alias("dog") == iid      # alias pinned → reuse override


def test_update_asset_unknown_errors(srv, client):
    assert client.post("/update_asset", json={"id": "nope.png", "notes": "x"}).json()["ok"] is False


def test_update_asset_out_of_scope_is_refused(srv, client):
    iid = client.post("/images/generate", json={"prompt": "x"}).json()["image_id"]  # scope private/builder
    r = client.post("/update_asset", json={"id": iid, "scope": "private/dungeonmaster", "notes": "z"})
    assert r.json()["ok"] is False and "scope" in r.json()["error"]


def test_delete_asset_removes_from_catalog(srv, client):
    iid = client.post("/images/generate", json={"prompt": "a red dragon"}).json()["image_id"]
    assert client.post("/delete_asset", json={"id": iid}).json()["ok"] is True
    assert srv.library.get(iid) is None
    assert client.post("/delete_asset", json={"id": iid}).json()["ok"] is False   # gone now


def test_public_uses_public_on_placement(srv, client):
    # build privately → a new image inherits private
    srv.store.doc.setdefault("environment", {})["public"] = False
    iid = client.post("/images/generate", json={"prompt": "a pear"}).json()["image_id"]
    assert srv.library.get(iid)["public"] == 0
    # placing it while the world is still private is a no-op (no publish, no notice)
    assert "notice" not in client.post("/place_image", json={"image_id": iid}).json()
    assert srv.library.get(iid)["public"] == 0
    # now the world is public → placing the private asset publishes it + notices
    srv.store.doc["environment"]["public"] = True
    r = client.post("/place_image", json={"image_id": iid}).json()
    assert r["ok"] and "notice" in r and srv.library.get(iid)["public"] == 1


def test_public_uses_public_on_make_world_public(srv, client):
    srv.store.doc.setdefault("environment", {})["public"] = False
    iid = client.post("/images/generate", json={"prompt": "a pear"}).json()["image_id"]   # private (inherited)
    client.post("/place_image", json={"image_id": iid})                                   # placed in the private world
    assert srv.library.get(iid)["public"] == 0
    # flip the world public → every private asset it references gets published, and reported
    r = client.post("/worlds/visibility", json={"public": True, "scope": "daniel/agents/builder"}).json()
    assert r["ok"] and r["published_assets"] == ["a pear"]
    assert srv.library.get(iid)["public"] == 1


def test_new_asset_inherits_active_world_visibility(srv, client):
    # made while a PRIVATE world is active ⇒ the asset is private (the reported bug — was always public)
    srv.store.doc.setdefault("environment", {})["public"] = False
    iid = client.post("/images/generate", json={"prompt": "a pear"}).json()["image_id"]
    assert srv.library.get(iid)["public"] == 0
    # regenerated while a PUBLIC world is active ⇒ public
    srv.library.delete(iid)
    srv.store.doc["environment"]["public"] = True
    jid = client.post("/images/generate", json={"prompt": "a pear"}).json()["image_id"]
    assert jid == iid and srv.library.get(jid)["public"] == 1
    # a re-procure must NOT silently undo an explicit later choice (inheritance is first-insert only)
    srv.library.update(jid, public=False)
    client.post("/images/generate", json={"prompt": "a pear"})
    assert srv.library.get(jid)["public"] == 0


def test_generated_asset_belongs_to_the_caller_not_default(srv, client):
    # guest generates an image → it's owned by GUEST's scope (X-Conjure-Scope), not the default user
    iid = client.post("/images/generate", json={"prompt": "a stone bridge"},
                      headers={"X-Conjure-Scope": "guest/agents/builder"}).json()["image_id"]
    assert srv.library.get(iid)["scope"] == "guest/agents/builder"
    # ...so guest can curate it — making it private now passes the scope check (the reported bug)
    r = client.post("/update_asset", json={"id": iid, "public": False,
                                           "scope": "guest/agents/builder"}).json()
    assert r["ok"] and srv.library.get(iid)["public"] == 0
    # a different user still can't change it
    assert client.post("/update_asset", json={"id": iid, "public": True,
                                              "scope": "daniel/agents/builder"}).json()["ok"] is False


def test_query_assets_is_scoped_and_read_only(srv, client):
    pub = client.post("/images/generate", json={"prompt": "a red dragon"}).json()["image_id"]
    rows = client.post("/query_assets", json={"sql": "SELECT COUNT(*) AS n FROM assets"}).json()
    assert rows["ok"] and rows["rows"][0]["n"] >= 1
    # builder also has a PRIVATE asset
    srv.library.upsert("secret.png", kind="image", scope=srv.active_scope, label="secret", public=0)
    # a different scope sees builder's PUBLIC asset (cross-scope public reads) but NOT the private one
    other = client.post("/query_assets", json={
        "sql": "SELECT id FROM assets", "scope": "friend/agents/builder"}).json()
    ids = {r["id"] for r in other["rows"]}
    assert pub in ids and "secret.png" not in ids
    # writes are rejected
    assert client.post("/query_assets", json={"sql": "DELETE FROM assets"}).json()["ok"] is False


def test_library_search_returns_tiered_candidates(srv, client):
    iid = client.post("/images/generate", json={"prompt": "a red dragon"}).json()["image_id"]
    r = client.post("/library/search", json={"query": "a red dragon"}).json()
    assert r["ok"] and r["confidence_tier"] == "strong"          # exact intent match
    assert any(c["id"] == iid for c in r["candidates"])
    assert client.post("/library/search", json={"query": "nonexistent thing"}).json()["confidence_tier"] == "none"


def test_place_cached_asset_reuses_a_model(srv, client, tmp_path):
    srv.resolver = FakeAssetResolver(record=ASSET_RECORD)
    client.post("/place_asset", json={"query": "oak tree", "size_m": 2})   # catalogs the model
    (tmp_path / f"{ASSET_RECORD.hash}.glb").write_bytes(b"glTF" + bytes(8))  # its bytes in the cache
    r = client.post("/place_cached_asset", json={"id": f"{ASSET_RECORD.hash}.glb", "size_m": 2}).json()
    assert r["ok"] is True
    ent = next(e for e in _entities(client) if e["id"] == r["id"])
    assert ent["components"]["gltf-model"] == f"/assets/{ASSET_RECORD.hash}.glb"
    assert ent["transform"]["scale"] == [0.5, 0.5, 0.5]          # bbox 4 tall, size_m 2 → scale 0.5


def test_place_cached_asset_rejects_non_model(srv, client):
    iid = client.post("/images/generate", json={"prompt": "x"}).json()["image_id"]
    r = client.post("/place_cached_asset", json={"id": iid}).json()
    assert r["ok"] is False and "not a model" in r["error"]


def test_update_asset_relabels_and_rejects(srv, client):
    # an X-wing wrongly cataloged under "starship enterprise"
    srv.resolver = FakeAssetResolver(record=ASSET_RECORD)
    client.post("/place_asset", json={"query": "starship enterprise", "size_m": 2})
    mid = f"{ASSET_RECORD.hash}.glb"
    assert client.post("/library/search", json={"query": "starship enterprise"}).json()["confidence_tier"] == "strong"
    r = client.post("/update_asset", json={"id": mid, "label": "X-Wing", "reject_for": "starship enterprise"})
    assert r.json()["ok"] is True
    assert srv.library.get(mid)["label"] == "X-Wing"            # relabeled
    after = client.post("/library/search", json={"query": "starship enterprise"}).json()
    assert not any(c["id"] == mid for c in after["candidates"])  # excluded after reject


def test_library_reindex_embeds_missing_assets(srv, client, tmp_path):
    import io

    from PIL import Image

    from conjure.embeddings import FakeEmbedder
    if not srv.library.has_vectors:
        pytest.skip("sqlite-vec not available")
    srv.embedder = FakeEmbedder(dim=8)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    (tmp_path / "img.png").write_bytes(buf.getvalue())              # bytes the image embed reads
    srv.library.upsert("img.png", kind="image", filename="img.png", prompt="a red square")
    srv.library.upsert("oak.glb", kind="model", filename="oak.glb", label="Oak Tree", query="oak")

    r = client.post("/library/reindex", json={}).json()
    assert r["ok"] and r["queued"] == 1                            # only the image (visual)
    assert srv.library.get("img.png")["embed_model"] == "fake"     # image embedded from pixels
    assert srv.library.get("oak.glb")["embed_model"] is None       # models are NOT vector-embedded


def test_library_reindex_clears_stale_model_vectors(srv, client):
    from conjure.embeddings import FakeEmbedder
    if not srv.library.has_vectors:
        pytest.skip("sqlite-vec not available")
    srv.embedder = FakeEmbedder(dim=8)
    srv.library.upsert("m.glb", kind="model", label="Oak Tree")
    srv.library.add_embedding("m.glb", [1.0] + [0.0] * 7, "fake")  # a stale text-derived model vector
    assert srv.library.get("m.glb")["embed_model"] == "fake"
    r = client.post("/library/reindex", json={}).json()
    assert r["cleared"] >= 1
    assert srv.library.get("m.glb")["embed_model"] is None          # purged from the visual index


def test_library_reindex_without_embedder_errors(srv, client):
    assert client.post("/library/reindex", json={}).json()["ok"] is False  # embedder is None by default


def test_caption_backfills_labels_and_makes_them_keyword_searchable(srv, client, tmp_path):
    import io

    from PIL import Image

    from conjure.captioner import FakeCaptioner
    srv.captioner = FakeCaptioner()
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buf, "PNG")
    (tmp_path / "bare.png").write_bytes(buf.getvalue())
    srv.library.upsert("bare.png", kind="image", filename="bare.png")   # no label
    srv.library.upsert("kept.png", kind="image", filename="kept.png", label="a red dragon")

    r = client.post("/library/caption", json={}).json()
    assert r["ok"] and r["queued"] == 1                                  # only the bare one
    cap = srv.library.get("bare.png")["label"]
    assert cap and cap.startswith("image ")                              # fake caption stored as label
    assert srv.library.get("kept.png")["label"] == "a red dragon"        # existing label untouched
    token = cap.split()[-1]                                              # the unique hex in the caption
    assert any(c["id"] == "bare.png" for c in srv.library.search(token))  # keyword-searchable now


def test_caption_without_captioner_errors(srv, client):
    assert client.post("/library/caption", json={}).json()["ok"] is False  # captioner None by default


def test_retag_skyboxes_makes_them_findable_by_kind(srv, client):
    srv.library.upsert("pano.png", kind="image", width=2100, height=900, prompt="a sunset beach")
    r = client.post("/library/retag-skyboxes", json={}).json()
    assert r["ok"] and r["retagged"] == 1
    assert srv.library.get("pano.png")["kind"] == "skybox"
    res = client.post("/library/search", json={"query": "a sunset beach", "kind": "skybox"}).json()
    assert any(c["id"] == "pano.png" for c in res["candidates"])  # now found specifically as a skybox


def test_set_grounded_skybox_marks_the_env(srv, client):
    r = client.post("/images/grounded_skybox", json={"prompt": "a meadow"})
    assert r.json()["ok"] is True
    assert client.post("/set_grounded_skybox", json={"image_id": r.json()["image_id"]}).json()["ok"] is True
    sky = client.get("/world").json()["environment"]["sky"]
    assert sky["src"] == f"/assets/{r.json()['image_id']}"
    assert sky["grounded"] is True and sky["height"] == 1.6 and sky["radius"] == 30.0


def test_list_generators_reports_capabilities_and_defaults(srv, client):
    body = client.get("/images/generators").json()
    assert body["ok"] and [g["name"] for g in body["generators"]] == ["Gemini"]
    assert body["generators"][0]["vendor"] == "google"   # vendor surfaced for the LLM
    assert body["defaults"]["skybox"] == "Gemini"


def test_request_by_vendor_alias_routes(srv, client):
    from conftest import FakeOpenAIImageGenerator
    srv.image_generators = {"Gemini": srv.image_generators["Gemini"], "Chat": FakeOpenAIImageGenerator()}
    body = client.post("/images/generate", json={"prompt": "x", "generator": "OpenAI"}).json()
    assert body["ok"] and body["provider"] == "Chat"     # "OpenAI" → the Chat generator


def test_mediation_rejects_incapable_requested_generator(srv, client):
    # Register an OpenAI-like generator that can't outpaint.
    from conftest import FakeOpenAIImageGenerator
    srv.image_generators = {"Gemini": srv.image_generators["Gemini"], "Chat": FakeOpenAIImageGenerator()}
    image_id = _procure(client)
    r = client.post("/images/outpaint", json={"image_id": image_id, "generator": "Chat"})
    assert r.json()["ok"] is False and "outpaint" in r.json()["error"]


def test_transparency_steers_to_openai(srv, client):
    from conftest import FakeOpenAIImageGenerator
    srv.image_generators = {"Gemini": srv.image_generators["Gemini"], "Chat": FakeOpenAIImageGenerator()}
    body = client.post("/images/generate", json={"prompt": "a star", "transparent": True}).json()
    assert body["ok"] and body["provider"] == "Chat"


def test_disabled_when_no_image_generator(srv, client):
    srv.image_generators = {}
    assert client.post("/images/generate", json={"prompt": "x"}).json()["ok"] is False


def test_expected_routes_exist(srv):
    # Contract: the endpoints the MCP tools + client depend on must exist.
    paths = {getattr(r, "path", None) for r in srv.app.routes}
    for p in ("/", "/world", "/patch", "/place_asset", "/place_image", "/edit_image",
              "/outpaint_image", "/set_skybox", "/set_grounded_skybox",
              "/library/search", "/place_cached_asset", "/query_assets", "/update_asset",
              "/delete_asset", "/library/reindex", "/library/retag-skyboxes", "/library/caption",
              "/skybox_from_image", "/assets/{filename}", "/ws",
              "/images/generators", "/images/generate", "/images/skybox", "/images/grounded_skybox",
              "/images/edit", "/images/outpaint", "/images/skybox_from", "/room", "/room/realign", "/reset",
              "/texture_surface", "/style_surface", "/tunnel"):
        assert p in paths, f"missing route {p}"


# --------------------------------------------------------------------------- room model

def test_room_unchanged_capture_is_not_rebroadcast(srv, client):
    # fix A: a settled room stops emitting patches — an identical re-capture makes NO new revision, so the
    # client isn't re-applying (and rebuilding) every surface every ~2 s (the "pops").
    body = {"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4],
         "rotation": [0, 0, 0]}]}
    client.post("/room", json=body)                          # first capture → adds the surface
    rev = client.get("/world").json()["rev"]
    client.post("/room", json=body)                          # identical → within tolerance
    assert client.get("/world").json()["rev"] == rev         # no new patch broadcast
    body["surfaces"][0]["position"] = [0, 1, -2.1]           # move 10 cm (> 3 cm)
    client.post("/room", json=body)
    assert client.get("/world").json()["rev"] > rev          # a real move DOES update


def test_room_authority_taken_over_only_when_stale(srv, client, monkeypatch):
    import conjure.server as S
    body = lambda cid: {"client_id": cid, "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4]}]}
    assert client.post("/room", json=body("h1")).json()["ok"] is True     # h1 claims authority
    r = client.post("/room", json=body("h2")).json()                      # h2 while h1 is live → refused
    assert r["ok"] is False and "authority" in r["error"]
    assert client.get("/world").json()["environment"]["room"]["authorityClientId"] == "h1"
    monkeypatch.setattr(S, "_authority_ts", S._authority_ts - S._AUTH_TTL - 1)   # h1 goes idle
    assert client.post("/room", json=body("h2")).json()["ok"] is True     # h2 takes over the stale authority
    assert client.get("/world").json()["environment"]["room"]["authorityClientId"] == "h2"


def test_room_ingest_creates_real_surfaces_and_boundary(srv, client):
    body = {"client_id": "h1",
            "surfaces": [{"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2],
                          "extent": [3, 2.4]}],
            "boundary": {"floorPolygon": [[0, 0], [3, 0], [3, 3], [0, 3]], "height": 2.6}}
    assert client.post("/room", json=body).json()["ok"] is True
    e = next(e for e in _entities(client) if e["id"] == "real_wall_1")
    assert e["meta"]["real"] is True and e["meta"]["semantic"] == "wall"
    assert e["meta"]["friendly_id"] == 1                  # short id for annotations/voice reference
    room = client.get("/world").json()["environment"]["room"]
    assert room["active"] is True and room["authorityClientId"] == "h1"
    assert room["boundary"]["height"] == 2.6


def test_door_surface_defaults_to_translucent(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [3, 2.4]},
        {"id": "real_door_1", "semantic": "door", "position": [0.5, 1.0, -2], "extent": [0.9, 2.0]}]})
    ents = {e["id"]: e for e in _entities(client)}
    door = ents["real_door_1"]["components"]["material"]
    assert door["transparent"] is True and door["opacity"] == 0.25   # reads as a see-through opening
    wall = ents["real_wall_1"]["components"]["material"]
    assert wall["opacity"] == 1.0 and "transparent" not in wall       # walls stay solid


def test_wall_holes_stored_and_window_defaults_to_glass(srv, client):
    # Doors/windows are cut OUT of their wall: snapInsets records the openings on the wall (wall-local
    # 2D), and they ride through the model so the renderer can punch them out and you see through.
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [3, 2.4],
         "holes": [{"x": 0.5, "y": -0.2, "w": 0.9, "h": 2.0}]},
        {"id": "real_window_1", "semantic": "window", "position": [-0.8, 1.4, -2], "extent": [0.8, 0.9]}]})
    ents = {e["id"]: e for e in _entities(client)}
    assert ents["real_wall_1"]["components"]["surface"]["holes"] == [{"x": 0.5, "y": -0.2, "w": 0.9, "h": 2.0}]
    glass = ents["real_window_1"]["components"]["material"]
    assert glass["transparent"] is True and glass["opacity"] < 0.5    # faint glass — you can see outside


def test_wall_holes_update_on_recapture(srv, client):
    def wall(holes):
        return {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [3, 2.4], "holes": holes}
    client.post("/room", json={"client_id": "h1", "surfaces": [wall([{"x": 0.5, "y": 0, "w": 0.9, "h": 2.0}])]})
    client.post("/room", json={"client_id": "h1", "surfaces": [wall([{"x": -0.7, "y": 0, "w": 0.8, "h": 1.9}])]})
    holes = next(e for e in _entities(client) if e["id"] == "real_wall_1")["components"]["surface"]["holes"]
    assert holes == [{"x": -0.7, "y": 0, "w": 0.8, "h": 1.9}]         # re-capture moved the opening in place


def test_friendly_id_stable_after_remove_readd(srv, client):
    # The friendly number is derived from the surface id (real_couch_1 → 1), so a DYNAMIC surface that's
    # pruned and comes back keeps the SAME number by construction. (Static surfaces are never pruned — see
    # test_static_surfaces_are_never_pruned.)
    def fid():
        return next(e for e in _entities(client) if e["id"] == "real_couch_1")["meta"]["friendly_id"]
    couch = {"id": "real_couch_1", "semantic": "couch", "position": [0, 0.4, -2], "extent": [2, 0.8]}
    client.post("/room", json={"client_id": "h1", "surfaces": [couch]})
    first = fid()
    client.post("/room", json={"client_id": "h1", "surfaces": []})           # one sparse capture
    assert any(e["id"] == "real_couch_1" for e in _entities(client))         # transient drop is kept
    for _ in range(srv._REMOVE_AFTER_ABSENT):                                # sustained absence → pruned
        client.post("/room", json={"client_id": "h1", "surfaces": []})
    assert not any(e["id"] == "real_couch_1" for e in _entities(client))
    client.post("/room", json={"client_id": "h1", "surfaces": [couch]})      # and returns
    assert fid() == first                                                    # same number, not higher


def test_anchored_surface_protected_from_pruning_others_still_prune(srv, client):
    # bug B: a surface with a photo pinned to it (meta.on_surface) keeps its id across detection dropouts
    # so the photo never orphans; an identical surface with NO content pinned prunes normally (so stray
    # duplicate surfaces don't accumulate).
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_1", "semantic": "wall art", "position": [0, 1.5, -2], "extent": [0.5, 0.5]},
        {"id": "real_wall_art_2", "semantic": "wall art", "position": [2, 1.5, -2], "extent": [0.5, 0.5]}]})
    srv.store.apply_patch([{"op": "add", "entity": {                        # hang a photo on art_1
        "id": "ent_photo", "meta": {"on_surface": "real_wall_art_1"},
        "components": {"geometry": {"primitive": "plane"}}}}])
    for _ in range(srv._REMOVE_AFTER_ABSENT + 2):                           # both surfaces absent
        client.post("/room", json={"client_id": "h1", "surfaces": []})
    ids = {e["id"] for e in _entities(client)}
    assert "real_wall_art_1" in ids                                        # anchored → kept (no orphaning)
    assert "real_wall_art_2" not in ids                                    # unanchored → pruned


def test_static_set_freezes_after_establishing_dynamic_stays_live(srv, client, monkeypatch):
    import conjure.server as S
    body = lambda wz, cx: {"client_id": "h1", "replace": True, "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, wz], "extent": [3, 2.4]},
        {"id": "real_couch_1", "semantic": "couch", "position": [cx, 0.4, 0], "extent": [2, 0.8]}]}
    client.post("/room", json=body(-2.0, 1.0))                              # establishing → both captured
    monkeypatch.setattr(S, "_room_capture_start", S._room_capture_start - S._ESTABLISH_SECS - 1)  # established
    client.post("/room", json=body(-2.3, 1.3))                              # move both 30 cm
    wall = next(e for e in _entities(client) if e["id"] == "real_wall_1")
    couch = next(e for e in _entities(client) if e["id"] == "real_couch_1")
    assert wall["transform"]["position"][2] == -2.0                         # STATIC frozen (move ignored)
    assert couch["transform"]["position"][0] == 1.3                         # DYNAMIC still updates


def test_texture_surface_resolves_by_friendly_id(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_floor_3", "semantic": "floor", "position": [0, 0, 0], "extent": [3, 3]}]})
    fid = next(e for e in _entities(client) if e["id"] == "real_floor_3")["meta"]["friendly_id"]
    image_id = _procure(client)
    r = client.post("/texture_surface", json={"target": str(fid), "image_id": image_id})
    assert r.json()["ok"] is True and r.json()["count"] == 1


def test_reset_clears_world_to_starter(srv, client):
    client.post("/patch", json={"ops": [{"op": "add", "entity": {"id": "box", "components": {}}}]})
    assert any(e["id"] == "box" for e in _entities(client))
    assert client.post("/reset").json()["ok"] is True
    ids = {e["id"] for e in _entities(client)}
    assert "box" not in ids and "floor" in ids   # back to the starter holodeck (has the floor)


async def test_realign_broadcasts_recapture(srv):
    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, m):
            self.sent.append(m)

    ws = FakeWS()
    srv.clients[ws] = "daniel"
    try:
        await srv.realign_room()
    finally:
        srv.clients.pop(ws, None)
    assert ws.sent and ws.sent[-1]["type"] == "recapture"


def test_tunnel_redirects_to_published_url(srv, client, tmp_path, monkeypatch):
    f = tmp_path / "tunnel_url"
    f.write_text("https://abc-def.trycloudflare.com\n")
    monkeypatch.setattr(srv, "TUNNEL_FILE", f)
    r = client.get("/tunnel", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "https://abc-def.trycloudflare.com"
    assert r.headers.get("cache-control") == "no-store"


def test_tunnel_404_when_none_running(srv, client, tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "TUNNEL_FILE", tmp_path / "absent")
    assert client.get("/tunnel", follow_redirects=False).status_code == 404


def test_room_authority_rejects_other_headset(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": []})
    r = client.post("/room", json={"client_id": "h2", "surfaces": []})
    assert r.json()["ok"] is False and "authority" in r.json()["error"]


def test_room_recapture_updates_pose_but_keeps_director_style(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2]}]})
    # director colors + shows the wall
    client.post("/patch", json={"ops": [{"op": "update", "id": "real_wall_1", "set": {
        "components.material.color": "#0000ff", "components.material.visible": True}}]})
    # re-capture with a refined pose
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.1, -2]}]})
    e = next(e for e in _entities(client) if e["id"] == "real_wall_1")
    assert e["transform"]["position"] == [0, 1.1, -2]              # geometry updated
    assert e["components"]["material"]["color"] == "#0000ff"        # director's style preserved
    assert e["components"]["material"]["visible"] is True


def test_texture_surface_maps_image_onto_surfaces(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_ceiling", "semantic": "ceiling", "position": [0, 2.6, 0], "extent": [4, 4]},
        {"id": "real_floor", "semantic": "floor", "position": [0, 0, 0], "extent": [4, 4]}]})
    image_id = _procure(client, "a starfield")
    r = client.post("/texture_surface", json={"target": "ceiling", "image_id": image_id, "repeat": 2})
    assert r.json()["ok"] is True and r.json()["count"] == 1
    ceil = next(e for e in _entities(client) if e["id"] == "real_ceiling")
    mat = ceil["components"]["material"]
    assert mat["src"] == f"/assets/{image_id}" and mat["color"] == "#FFFFFF"
    assert mat["visible"] is True and mat["repeat"] == "2.0 2.0"
    assert ceil["meta"]["image_id"] == image_id
    # the floor (a different semantic) was untouched
    floor = next(e for e in _entities(client) if e["id"] == "real_floor")
    assert "src" not in floor["components"]["material"]


def test_texture_surface_unknown_target_errors(srv, client):
    image_id = _procure(client)
    assert client.post("/texture_surface", json={"target": "nope", "image_id": image_id}).json()["ok"] is False


def test_style_surface_sets_color_and_opacity(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4]}]})
    r = client.post("/style_surface", json={"target": "wall", "color": "blue", "opacity": 0.4})
    assert r.json()["ok"] is True and r.json()["count"] == 1
    mat = next(e for e in _entities(client) if e["id"] == "real_wall_1")["components"]["material"]
    assert mat["color"] == "blue" and mat["opacity"] == 0.4
    assert mat["transparent"] is True and mat["visible"] is True


def test_style_surface_needs_color_or_opacity(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2]}]})
    assert client.post("/style_surface", json={"target": "wall"}).json()["ok"] is False


def test_room_replace_prunes_only_after_repeated_absence(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "a", "semantic": "couch", "position": [0, 1, -2]},
        {"id": "b", "semantic": "couch", "position": [1, 1, -2]}]})
    just_a = {"client_id": "h1", "surfaces": [{"id": "a", "semantic": "couch", "position": [0, 1, -2]}]}
    client.post("/room", json=just_a)                                 # b missing from ONE capture
    assert {"a", "b"} <= {e["id"] for e in _entities(client)}         # survives the transient drop
    for _ in range(srv._REMOVE_AFTER_ABSENT):                         # sustained absence → genuinely gone
        client.post("/room", json=just_a)
    ids = {e["id"] for e in _entities(client)}
    assert "a" in ids and "b" not in ids


async def test_patch_is_broadcast_to_clients(srv):
    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

    ws = FakeWS()
    srv.clients[ws] = "daniel"
    try:
        await srv.post_patch(Patch(ops=[{"op": "add", "entity": {"id": "c", "components": {}}}]))
    finally:
        srv.clients.pop(ws, None)
    assert ws.sent and ws.sent[-1]["type"] == "patch"
    assert ws.sent[-1]["patch"]["ops"][0]["op"] == "add"


# ---- scoped multi-world store -------------------------------------------------------------------
def test_world_new_switch_list_isolate_state(srv, client):
    client.post("/patch", json={"ops": [{"op": "add", "entity": {"id": "in_default", "components": {}}}]})
    r = client.post("/worlds/new", json={"name": "Blade Runner 1"}).json()
    assert r["ok"] and r["world"] == "blade-runner-1"            # name normalized to a slug
    assert "in_default" not in {e["id"] for e in _entities(client)}   # new world starts clean
    client.post("/patch", json={"ops": [{"op": "add", "entity": {"id": "in_blade", "components": {}}}]})

    lst = client.post("/worlds/list", json={}).json()
    assert set(lst["worlds"]) == {"default", "blade-runner-1"} and lst["active"] == "blade-runner-1"

    client.post("/worlds/switch", json={"name": "default"})      # outgoing world saved, default restored
    ids = {e["id"] for e in _entities(client)}
    assert "in_default" in ids and "in_blade" not in ids
    client.post("/worlds/switch", json={"name": "blade runner 1"})  # loose formatting still resolves
    assert "in_blade" in {e["id"] for e in _entities(client)}


def test_world_new_refuses_duplicate_canonical_name(srv, client):
    client.post("/worlds/new", json={"name": "metropolis"})
    r = client.post("/worlds/new", json={"name": "Metropolis"}).json()   # same slug
    assert r["ok"] is False and "exists" in r["error"]


def test_world_cannot_delete_active_then_can_after_switch(srv, client):
    client.post("/worlds/new", json={"name": "temp"})             # active = temp
    assert client.post("/worlds/delete", json={"name": "temp"}).json()["ok"] is False
    client.post("/worlds/switch", json={"name": "default"})
    assert client.post("/worlds/delete", json={"name": "temp"}).json()["ok"] is True
    assert "temp" not in client.post("/worlds/list", json={}).json()["worlds"]


def test_world_constructor_runs_on_create(srv, client):
    # builder's agent.json on_create turns real-room edges on; a fresh world bakes that into its doc
    client.post("/worlds/new", json={"name": "fresh"})
    env = client.get("/world").json()["environment"]
    assert env.get("room", {}).get("edgesVisible") is True


def test_world_supports_nested_names(srv, client):
    r = client.post("/worlds/new", json={"name": "Castle Quest/Dining Hall"}).json()
    assert r["world"] == "castle-quest/dining-hall"
    assert "castle-quest/dining-hall" in client.post("/worlds/list", json={}).json()["worlds"]


def test_reset_room_authority_clears_stale_id(srv):
    from conjure.world import WorldStore
    s = WorldStore({"id": "x", "name": "x", "rev": 0, "entities": [],
                    "environment": {"room": {"authorityClientId": "hs_dead"}}})
    srv._reset_room_authority(s)
    assert s.doc["environment"]["room"]["authorityClientId"] is None
    srv._reset_room_authority(WorldStore({"id": "y", "name": "y", "rev": 0, "entities": [],
                                          "environment": {}}))   # no room/env → must not raise


def test_switching_into_a_world_drops_its_stale_authority(srv, client):
    from conjure.world import WorldStore
    # a world saved by a PAST session, pinned to a now-dead headset id
    srv.worlds.save(srv.DEFAULT_SCOPE, "old-room", WorldStore(
        {"id": "o", "name": "o", "rev": 1, "entities": [],
         "environment": {"room": {"active": True, "authorityClientId": "hs_dead"}}}))
    assert client.post("/worlds/switch", json={"name": "old-room"}).json()["ok"]
    assert client.get("/world").json()["environment"]["room"]["authorityClientId"] is None
    # a NEW headset id can now capture (before the fix this was rejected forever)
    r = client.post("/room", json={"client_id": "hs_new", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [3, 2.4]}]}).json()
    assert r["ok"] is True
    assert client.get("/world").json()["environment"]["room"]["authorityClientId"] == "hs_new"


def test_migrate_world_dirs_moves_pre_user_layout(srv, tmp_path):
    root = tmp_path / "worlds"
    (root / "private" / "builder").mkdir(parents=True)
    (root / "private" / "builder" / "default.json").write_text("{}")
    (root / "private" / "builder" / "_active.txt").write_text("new-room")
    srv._migrate_world_dirs(root)
    moved = root / "daniel" / "agents" / "builder"
    assert (moved / "default.json").exists()
    assert (moved / "_active.txt").read_text() == "new-room"
    assert not (root / "private").exists()                 # emptied old tree pruned
    srv._migrate_world_dirs(root)                          # idempotent — no error, no change
    assert (moved / "default.json").exists()


def test_tunnel_user_route_carries_user(srv, client, tmp_path, monkeypatch):
    tf = tmp_path / "tunnel_url"
    tf.write_text("https://x.trycloudflare.com")
    monkeypatch.setattr(srv, "TUNNEL_FILE", tf)
    r = client.get("/tunnel/bob", follow_redirects=False)
    assert r.status_code == 307 and "user=bob" in r.headers["location"]
    r2 = client.get("/tunnel", follow_redirects=False)     # default user → no user param appended
    assert "user=" not in r2.headers["location"]


# ---- space <-> world composition (Phase 2) ------------------------------------------------------
def _space_doc():
    base = {"shader": "flat", "color": "#888", "side": "double", "opacity": 1.0}
    return {"owner": "daniel", "name": "home", "boundary": {"floorPolygon": [[0, 0]], "height": 2.6},
            "surfaces": [
                {"id": "real_wall_0", "meta": {"real": True, "semantic": "wall"},
                 "transform": {"position": [0, 1, -2]}, "components": {"surface": {"extent": [3, 2.4]},
                                                                       "material": dict(base)}},
                {"id": "real_couch_41", "meta": {"real": True, "semantic": "couch"},
                 "transform": {"position": [1, 0.5, 0]}, "components": {"surface": {"extent": [2, 0.8]},
                                                                        "material": dict(base)}}]}


def test_compose_merges_space_geometry_with_world_overrides(srv):
    space = _space_doc()
    world = {"rev": 5, "entities": [{"id": "ent_dragon", "meta": {"generated": True}, "components": {}}],
             "environment": {"sky": {"color": "#001"}, "room": {"edgesVisible": True,
                             "surfaceStyles": {"real_couch_41": {"color": "green", "visible": True}}}},
             "space": "daniel/spaces/home"}
    doc = srv._compose(world, space)
    ids = {e["id"] for e in doc["entities"]}
    assert ids == {"ent_dragon", "real_wall_0", "real_couch_41"}     # placed + space geometry
    couch = next(e for e in doc["entities"] if e["id"] == "real_couch_41")
    assert couch["components"]["material"]["color"] == "green"        # world override applied
    wall = next(e for e in doc["entities"] if e["id"] == "real_wall_0")
    assert wall["components"]["material"]["color"] == "#888"          # no override → space base
    assert doc["environment"]["room"]["boundary"]["height"] == 2.6    # boundary from space
    assert "surfaceStyles" not in doc["environment"]["room"]          # overlay not broadcast
    assert "space" not in doc                                         # ref not broadcast


def test_decompose_extracts_only_real_overrides_and_round_trips(srv):
    space = _space_doc()
    world = {"rev": 5, "entities": [{"id": "ent_dragon", "meta": {"generated": True}, "components": {}}],
             "environment": {"room": {"edgesVisible": True,
                             "surfaceStyles": {"real_couch_41": {"color": "green", "visible": True}}}}}
    composed = srv._compose(world, space)
    back = srv._decompose(composed, space)
    assert [e["id"] for e in back["entities"]] == ["ent_dragon"]      # geometry stripped, placed kept
    styles = back["environment"]["room"]["surfaceStyles"]
    assert set(styles) == {"real_couch_41"}                          # only the OVERRIDDEN surface recorded
    assert styles["real_couch_41"]["color"] == "green" and styles["real_couch_41"]["visible"] is True
    assert "boundary" not in back["environment"]["room"]             # boundary belongs to the space
    # round-trip: re-composing reproduces the same rendered surfaces (materials + geometry)
    assert srv._compose(back, space)["entities"] == composed["entities"]


def test_room_geometry_is_shared_across_worlds_styling_is_per_world(srv, client):
    # capture a room and style the couch in the current ('default') world
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "real_couch_1", "semantic": "couch", "position": [1, 0.5, 0], "extent": [2, 0.8]},
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4]}]})
    client.post("/style_surface", json={"target": "couch", "color": "green"})
    couch = next(e for e in _entities(client) if e["id"] == "real_couch_1")
    assert couch["components"]["material"]["color"] == "green"
    # a NEW world shares the same physical room geometry, but not 'default's styling
    assert client.post("/worlds/new", json={"name": "blade"}).json()["ok"]
    ids = {e["id"] for e in _entities(client)}
    assert {"real_couch_1", "real_wall_1"} <= ids                       # the room followed us
    here = next(e for e in _entities(client) if e["id"] == "real_couch_1")
    assert here["components"]["material"]["color"] != "green"           # styling is per-world
    # switch back to 'default' → the couch is green again (per-world override restored)
    client.post("/worlds/switch", json={"name": "default"})
    back = next(e for e in _entities(client) if e["id"] == "real_couch_1")
    assert back["components"]["material"]["color"] == "green"


def test_activate_no_longer_migrates_embedded_geometry(srv, client):
    """new-space-flow step 0: the legacy geometry-embedded migration is gone. A pre-space world doc is
    no longer rewritten on load, and its INLINE real surfaces are NOT resurrected — real geometry lives
    only in the space now (fed by capture via _save_active). Objects still compose, and a world with no
    space ref lands in 'home' (the Path B bridge, removed in step 5)."""
    from conjure.world import WorldStore
    embedded = {
        "id": "l", "name": "legacy", "rev": 3, "environment": {"room": {"boundary": {"height": 2.6}}},
        "entities": [
            {"id": "ent_box", "meta": {"generated": True}, "components": {}},
            {"id": "real_table_2", "meta": {"real": True, "semantic": "table"},
             "transform": {"position": [0, 0.5, -1]},
             "components": {"surface": {"extent": [1, 1]}, "material": {"color": "blue"}}}]}
    srv.worlds.save(srv.DEFAULT_SCOPE, "legacy", WorldStore(embedded))
    client.post("/worlds/switch", json={"name": "legacy"})
    ids = {e["id"] for e in _entities(client)}
    assert "ent_box" in ids                                     # placed objects compose as before
    assert "real_table_2" not in ids                            # inline geometry is NOT migrated back in
    assert srv.active_space == "home"                           # Path B bridge until step 5
    wd = srv.worlds.load(srv.DEFAULT_SCOPE, "legacy").doc
    assert any(e["id"] == "real_table_2" for e in wd["entities"])   # inline geometry NOT stripped from disk
    assert "space" not in wd.get("environment", {})                # activate no longer stamps a space ref


# ---- step 2: fully-qualified space references (D3 — a world can live in another user's space) ------
def test_resolve_space_ref_qualified_and_bare(srv):
    assert srv._resolve_space_ref("daniel/home", "bob") == ("daniel", "home")   # fully-qualified
    assert srv._resolve_space_ref("home", "bob") == ("bob", "home")             # bare → the world's owner
    assert srv._space_ref("daniel", "home") == "daniel/home"


def test_save_active_writes_fully_qualified_space_ref(srv):
    # the active world (daniel/default) composes against daniel's 'home'; a save records the OWNER/NAME ref
    srv._save_active()
    wd = srv.worlds.load(srv.DEFAULT_SCOPE, "default").doc
    assert wd["environment"]["space"] == "daniel/home"


def test_world_can_live_in_another_users_space(srv, client):
    """D3 / step 2: environment.space is <owner>/<name>, so bob's world can be tied to daniel's space.
    Activating composes daniel's geometry; bob's capture persists back to DANIEL's space (not bob's),
    and the qualified reference is preserved."""
    from conjure.world import WorldStore
    srv.spaces.save("daniel", "home", {"owner": "daniel", "name": "home", "public": True,
        "geolocation": None, "boundary": {"height": 2.6},
        "surfaces": [{"id": "real_wall_9", "meta": {"real": True, "semantic": "wall"},
                      "components": {"material": {"color": "#888"}}}]})
    srv.worlds.save("bob/agents/builder", "in-daniels-room", WorldStore(
        {"id": "b", "name": "in-daniels-room", "rev": 1,
         "environment": {"space": "daniel/home"}, "entities": []}))
    srv.active_space = srv.VOID          # neutralize the outgoing save so it can't clobber the seeded space
    r = client.post("/worlds/switch", json={"name": "in-daniels-room", "scope": "bob/agents/builder"},
                    headers={"X-Conjure-User": "bob"})
    assert r.json()["ok"]
    assert srv.active_space_owner == "daniel" and srv.active_space == "home"     # resolved owner+name
    assert any(e["id"] == "real_wall_9" for e in _entities(client))             # daniel's geometry composed in
    # bob (now the active world's owner) captures another wall → it lands in DANIEL's space
    client.post("/room", json={"client_id": "h", "surfaces": [
        {"id": "real_wall_9", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4]},
        {"id": "real_wall_10", "semantic": "wall", "position": [2, 1, 0], "extent": [3, 2.4]}]},
        headers={"X-Conjure-User": "bob"})
    srv._save_active()
    sp = srv.spaces.load("daniel", "home")
    assert {s["id"] for s in sp["surfaces"]} >= {"real_wall_9", "real_wall_10"}  # persisted to daniel's space
    assert srv.spaces.list("bob") == []                                         # NOT minted in bob's scope
    wd = srv.worlds.load("bob/agents/builder", "in-daniels-room").doc
    assert wd["environment"]["space"] == "daniel/home"                          # qualified ref preserved


# ---- two-stage space selection: /geolocation (discovery) + /space/select (commit) — step 3 ----------
def _geo_space(srv, owner, name, lat, lon, **extra):
    srv.spaces.save(owner, name, {"owner": owner, "name": name, "public": True,
                                  "geolocation": {"lat": lat, "lon": lon}, "surfaces": [], **extra})


def test_geolocation_returns_geo_near_candidates_across_users(srv, client):
    _geo_space(srv, "daniel", "home", 37.7749, -122.4194)      # SF
    _geo_space(srv, "bob", "loft", 37.7750, -122.4195)         # SF, ~15 m away — a DIFFERENT user, still a candidate
    _geo_space(srv, "daniel", "office", 40.7128, -74.0060)     # NY — far, filtered out
    srv.spaces.save("daniel", "unlocated", {"owner": "daniel", "name": "unlocated", "public": True,
                                            "geolocation": None, "surfaces": []})   # no GPS → never a candidate
    r = client.post("/geolocation", json={"lat": 37.7749, "lon": -122.4194}).json()
    assert r["ok"]
    got = {(c["owner"], c["name"]) for c in r["candidates"]}
    assert got == {("daniel", "home"), ("bob", "loft")}        # geo-near across users; NY + un-located excluded
    assert r["candidates"][0]["distance_m"] <= r["candidates"][1]["distance_m"]   # nearest-first tiebreak
    assert "surfaces" in r["candidates"][0]                    # constellation shipped for the client vote


def test_geolocation_no_candidates_when_far_from_every_space(srv, client):
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    r = client.post("/geolocation", json={"lat": 51.5, "lon": -0.12}).json()      # London
    assert r["ok"] and r["candidates"] == []


def test_space_select_matched_joins_last_world(srv, client):
    from conjure.world import WorldStore
    _geo_space(srv, "daniel", "office", 40.71, -74.0,
               last_scope=srv.DEFAULT_SCOPE, last_world="office-world")
    srv.worlds.save(srv.DEFAULT_SCOPE, "office-world", WorldStore(
        {"id": "o", "name": "office-world", "rev": 1, "environment": {"space": "daniel/office"}, "entities": []}))
    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "office"}).json()
    assert r["ok"] and r["joined"] == "daniel/office"
    assert srv.active_world == "office-world" and srv.active_space == "office"
    assert srv.active_space_owner == "daniel"


def test_space_select_unmatched_always_mints_a_geo_stamped_space(srv, client):
    # No match ⇒ somewhere new ⇒ mint. A space is born WITH its location (no separate "stamp the
    # pre-existing active space" path — that bridge was retired once all spaces were geo-stamped).
    _geo_space(srv, "daniel", "home", 37.77, -122.42)          # active space, already located elsewhere
    r = client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12}).json()
    assert r["ok"] and r["created_space"] == "daniel/space-2"
    assert srv.active_space == "space-2" and srv.active_space_owner == "daniel"
    sp = srv.spaces.load("daniel", "space-2")
    assert sp["geolocation"]["lat"] == 51.5 and sp["surfaces"] == []


def test_forced_geo_resolves_zero_space_and_bad_specs(srv, monkeypatch):
    import dataclasses
    force = lambda v: monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, force_geo=v))
    force("zero")
    assert srv._forced_geo() == (0.0, 0.0)                     # a convenient "somewhere else"
    _geo_space(srv, "daniel", "space-0", 51.5, -0.12)
    force("/daniel/spaces/space-0")
    assert srv._forced_geo() == (51.5, -0.12)                  # pinned at an existing space's location
    force("/daniel/spaces/nope")
    assert srv._forced_geo() is None                           # unresolvable → no override (real location used)
    force(None)
    assert srv._forced_geo() is None                           # unset


def test_forced_geo_overrides_the_reported_location(srv, client, monkeypatch):
    import dataclasses
    _geo_space(srv, "daniel", "space-0", 51.5, -0.12)          # London
    monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, force_geo="/daniel/spaces/space-0"))
    # the client reports SF, but --force-geo pins us at space-0 (London) → space-0 is a candidate
    r = client.post("/geolocation", json={"lat": 37.77, "lon": -122.42}).json()
    assert ("daniel", "space-0") in {(c["owner"], c["name"]) for c in r["candidates"]}


def test_space_select_commits_once_per_session(srv, client):
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    assert client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12}).json()["created_space"]
    r2 = client.post("/space/select", json={"matched": False, "lat": 1.0, "lon": 2.0}).json()
    assert r2 == {"ok": True, "selected": False}               # a second vote is ignored this session
    # and /geolocation stops offering candidates once selection is committed
    assert client.post("/geolocation", json={"lat": 1.0, "lon": 2.0}).json()["selected"] is True


# ---- Phase 4 step 1: per-connection user + public-join gate --------------------------------------
def test_ws_owner_and_public_guest_receive_the_world(srv, client):
    with client.websocket_connect("/ws?user=daniel") as ws:        # owner
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["owner"] == "daniel"   # owner in snapshot (desktop-guest spawn)
    with client.websocket_connect("/ws?user=bob") as ws:           # guest, world public by default
        assert ws.receive_json()["type"] == "snapshot"
    with client.websocket_connect("/ws") as ws:                    # no user → default (owner)
        assert ws.receive_json()["type"] == "snapshot"


def test_ws_guest_refused_private_world_gets_info_and_no_broadcast(srv, client):
    srv.store.doc.setdefault("environment", {})["public"] = False
    with client.websocket_connect("/ws?user=bob") as ws:           # guest + private → info, no world
        msg = ws.receive_json()
        assert msg["type"] == "info" and "private" in msg["msg"] and "daniel" in msg["msg"]
        assert "bob" not in srv.clients.values()                   # not joined → excluded from broadcasts
    with client.websocket_connect("/ws?user=daniel") as ws:        # owner still gets in
        assert ws.receive_json()["type"] == "snapshot"


def test_presence_relayed_to_others_and_leave_on_disconnect(srv, client):
    with client.websocket_connect("/ws?user=daniel") as ws1:
        ws1.receive_json()                                          # snapshot
        with client.websocket_connect("/ws?user=bob") as ws2:
            ws2.receive_json()                                      # snapshot
            ws1.send_json({"type": "presence", "pose": {"p": [1, 1.6, 2], "q": [0, 0, 0, 1]}})
            m = ws2.receive_json()                                  # relayed to the OTHER client, tagged
            assert m["type"] == "presence" and m["user"] == "daniel" and m["pose"]["p"] == [1, 1.6, 2]
        leave = ws1.receive_json()                                  # bob disconnected → his avatar drops
        assert leave["type"] == "presence_leave" and leave["user"] == "bob"


def test_gaze_from_pose_derives_forward():
    from conjure.server import _gaze_from_pose
    # identity orientation → looking down -Z from the origin
    g = _gaze_from_pose({"p": [0, 1.6, 0], "q": [0, 0, 0, 1]})
    assert g["origin"] == [0.0, 1.6, 0.0]
    assert g["forward"][0] == 0 and g["forward"][1] == 0 and round(g["forward"][2], 6) == -1.0
    # 90° yaw about +Y → looking down -X
    import math
    s = math.sin(math.pi / 4)
    g2 = _gaze_from_pose({"p": [0, 1.6, 0], "q": [0, s, 0, s]})
    assert round(g2["forward"][0], 6) == -1.0 and round(g2["forward"][2], 6) == 0.0
    assert _gaze_from_pose({"p": [0, 0, 0]}) is None        # no quaternion → no gaze


def test_presence_stores_and_clears_gaze(srv, client):
    with client.websocket_connect("/ws?user=daniel") as ws:
        ws.receive_json()                                   # snapshot
        ws.send_json({"type": "presence", "pose": {"p": [1, 1.6, 2], "q": [0, 0, 0, 1]}})
        # the server records where daniel is looking
        import time as _t
        for _ in range(50):
            if "daniel" in srv.gaze:
                break
            _t.sleep(0.005)
        assert srv.gaze["daniel"]["origin"] == [1.0, 1.6, 2.0]
        assert round(srv.gaze["daniel"]["forward"][2], 6) == -1.0
    assert "daniel" not in srv.gaze                         # cleared when the last socket closes


def _set_gaze(srv, user="daniel", origin=(0, 1.6, 0)):
    srv.gaze[user] = {"origin": list(origin), "forward": [0, 0, -1],
                      "right": [1, 0, 0], "up": [0, 1, 0]}


def test_view_relative_resolves_directions(srv, client):
    _set_gaze(srv)
    def pt(d):
        return client.post("/view_relative", json={"direction": d, "distance": 1.0},
                           headers={"X-Conjure-User": "daniel"}).json()
    assert pt("forward")["point"] == [0.0, 1.6, -1.0]
    assert pt("back")["point"] == [0.0, 1.6, 1.0]
    assert pt("right")["point"] == [1.0, 1.6, 0.0]
    assert pt("left")["point"] == [-1.0, 1.6, 0.0]
    assert pt("up")["point"] == [0.0, 2.6, 0.0]
    assert pt("down")["point"] == [0.0, 0.6, 0.0]
    assert pt("sideways")["ok"] is False               # bad direction


def test_view_relative_raycasts_the_surface_youre_looking_at(srv, client):
    _set_gaze(srv)
    # a wall 3 m ahead (-Z), facing the viewer (normal +Z, rotation 0), 2 m wide x 2.5 m tall
    srv.store.doc["entities"].append({
        "id": "real_wall_7", "transform": {"position": [0, 1.6, -3], "rotation": [0, 0, 0]},
        "components": {"surface": {"extent": [2.0, 2.5]}}, "meta": {"real": True, "semantic": "wall", "friendly_id": 7}})
    r = client.post("/view_relative", json={"direction": "forward"},
                    headers={"X-Conjure-User": "daniel"}).json()
    assert r["ok"] and r["surface"]["id"] == "real_wall_7" and r["surface"]["distance"] == 3.0
    # looking the other way → no surface that way
    assert client.post("/view_relative", json={"direction": "back"},
                       headers={"X-Conjure-User": "daniel"}).json()["surface"] is None


def test_view_relative_lists_nearby_objects_and_needs_gaze(srv, client):
    _set_gaze(srv)
    srv.store.doc["entities"].append({                  # an object 1 m ahead
        "id": "ent_lamp", "transform": {"position": [0, 1.6, -1]}, "meta": {"title": "brass lamp"}})
    r = client.post("/view_relative", json={"direction": "forward"},
                    headers={"X-Conjure-User": "daniel"}).json()
    assert any(n["id"] == "ent_lamp" and n["title"] == "brass lamp" for n in r["nearby"])
    # a user with no live view → graceful error
    assert client.post("/view_relative", json={"direction": "forward"},
                       headers={"X-Conjure-User": "ghost"}).json()["ok"] is False


def test_geolocation_is_read_only_and_never_moves_the_active_space(srv, client):
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    # /geolocation only DISCOVERS candidates — even a guest's report can't stamp or switch the active
    # space (mutation is deferred to /space/select; who's admitted to commit is step 4).
    r = client.post("/geolocation", json={"lat": 51.5, "lon": -0.12, "user": "bob"}).json()
    assert r["ok"] and "candidates" in r                                 # read-only discovery
    assert srv.spaces.load("daniel", "home")["geolocation"]["lat"] == 37.77   # untouched
    assert srv.active_space == "home" and srv.active_space_owner == "daniel"


# ---- Phase 4 step 4: owner-only writes ----------------------------------------------------------
def test_owner_only_writes_enforced(srv, client):
    box = {"ops": [{"op": "add", "entity": {"id": "b", "components": {}}}]}
    # a guest (user != owner=daniel) is refused with 403
    r = client.post("/patch", json=box, headers={"X-Conjure-User": "bob"})
    assert r.status_code == 403 and r.json()["ok"] is False and "daniel" in r.json()["error"]
    # the owner is allowed
    assert client.post("/patch", json=box, headers={"X-Conjure-User": "daniel"}).status_code == 200
    # no header (direct dev CLI) is treated as the owner — allowed
    assert client.post("/patch", json=box).status_code == 200


def test_guest_cannot_edit_the_world_but_reads_are_open(srv, client):
    # the reported bug: a guest styling a surface in the owner's world → still 403 (edit-rights follow ownership)
    assert client.post("/style_surface", json={"target": "door", "color": "blue"},
                       headers={"X-Conjure-User": "bob"}).status_code == 403
    # but reads/listing stay open to guests
    assert client.post("/worlds/list", json={}, headers={"X-Conjure-User": "bob"}).status_code == 200


def test_guest_can_discover_and_enter_owners_public_world(srv, client):
    # the guest creates a world (flips active to bob), so daniel's "default" gets persisted on the way out
    client.post("/worlds/new", json={"name": "guest-world", "scope": "bob/agents/builder"},
                headers={"X-Conjure-User": "bob"})
    # bob lists worlds → daniel's default shows up under `available`, tagged by owner
    listing = client.post("/worlds/list", json={"scope": "bob/agents/builder"}).json()
    avail = {(w["owner"], w["name"]) for w in listing["available"]}
    assert ("daniel", "default") in avail
    # bob is currently in his own guest-world (he just created it), so it's reported active + current=bob
    assert listing["active"] == "guest-world" and listing["current"]["owner"] == "bob"
    # bob switches INTO daniel's public world by owner+name → everyone comes along, active = daniel's
    r = client.post("/worlds/switch", json={"name": "default", "scope": "bob/agents/builder",
                                            "owner": "daniel"}, headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] and srv.active_scope == "daniel/agents/builder"
    # ...and bob still can't edit daniel's world
    assert client.post("/style_surface", json={"target": "wall", "color": "red"},
                       headers={"X-Conjure-User": "bob"}).status_code == 403
    # now bob's list reports the TRUTH: none of HIS worlds is active; the live world is daniel's (the bug)
    relist = client.post("/worlds/list", json={"scope": "bob/agents/builder"}).json()
    assert relist["active"] is None                                   # not his stale guest-world pointer
    assert relist["current"] == {"owner": "daniel", "name": "default"}


def test_world_visibility_create_private_and_toggle(srv, client):
    sc = "bob/agents/builder"
    # create a PRIVATE world → not discoverable by others
    assert client.post("/worlds/new", json={"name": "secret", "scope": sc, "public": False},
                       headers={"X-Conjure-User": "bob"}).json()["ok"]
    seen = {(w["owner"], w["name"]) for w in worlds_seen(client, "daniel/agents/builder")}
    assert ("bob", "secret") not in seen
    # flip the CURRENT (active) world public → now discoverable
    r = client.post("/worlds/visibility", json={"public": True, "scope": sc},
                    headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] and r["public"] is True
    seen = {(w["owner"], w["name"]) for w in worlds_seen(client, "daniel/agents/builder")}
    assert ("bob", "secret") in seen
    # flip it back private
    client.post("/worlds/visibility", json={"public": False, "scope": sc}, headers={"X-Conjure-User": "bob"})
    seen = {(w["owner"], w["name"]) for w in worlds_seen(client, "daniel/agents/builder")}
    assert ("bob", "secret") not in seen


def test_new_world_defaults_public(srv, client):
    # created with no `public` arg → public by default ⇒ discoverable by other users
    assert client.post("/worlds/new", json={"name": "shared", "scope": "bob/agents/builder"},
                       headers={"X-Conjure-User": "bob"}).json()["ok"]
    seen = {(w["owner"], w["name"]) for w in worlds_seen(client, "daniel/agents/builder")}
    assert ("bob", "shared") in seen


def test_outdoor_void_world_has_no_space(srv, client):
    from conjure import server as S
    assert client.post("/worlds/new", json={"name": "beach", "outdoor": True}).json()["ok"]
    assert S.active_space == "<void>"                                  # not tied to a physical space
    assert srv.store.doc["environment"]["space"] == "<void>"
    assert not any(e.get("meta", {}).get("real") for e in srv.store.doc["entities"])  # no room geometry
    # saving a void world creates NO space file and round-trips as void
    S._save_active()
    assert "<void>" not in srv.spaces.list("daniel")
    assert srv.worlds.load("daniel/agents/builder", "beach").doc["environment"]["space"] == "<void>"
    # geolocation is read-only discovery — it never yanks a void world into a physical space (and the
    # client doesn't run space-selection in a void world anyway; it uses canonicalFrame)
    assert client.post("/geolocation", json={"lat": 40.0, "lon": -73.0}).json()["ok"]
    assert S.active_space == "<void>"


def worlds_seen(client, scope):
    return client.post("/worlds/list", json={"scope": scope}).json()["available"]


def test_guest_may_create_and_switch_worlds_everyone_comes_along(srv, client):
    # navigation is NOT owner-gated: a guest creates a world in their OWN scope and everyone comes along.
    r = client.post("/worlds/new", json={"name": "guest-world", "scope": "bob/agents/builder"},
                    headers={"X-Conjure-User": "bob"})
    assert r.status_code == 200 and r.json()["ok"] is True
    # having created it, the guest now owns the active world and CAN edit it (active_scope flipped to bob)
    assert srv.active_scope == "bob/agents/builder"
    assert client.post("/style_surface", json={"target": "wall", "color": "red"},
                       headers={"X-Conjure-User": "bob"}).status_code == 200
    # and switching back to daniel's world re-protects it from the guest
    client.post("/worlds/switch", json={"name": "default", "scope": "daniel/agents/builder"},
                headers={"X-Conjure-User": "bob"})
    assert client.post("/style_surface", json={"target": "wall", "color": "blue"},
                       headers={"X-Conjure-User": "bob"}).status_code == 403


# ---- admin: dir / delete over the user→{worlds,spaces,assets} namespace (shell `dir`/`delete`) ------
def _seed_worlds(srv, scope, *names):
    from conjure.world import WorldStore
    for n in names:
        srv.worlds.save(scope, n, WorldStore(
            {"id": n, "name": n, "rev": 0, "environment": {}, "entities": []}))


def _seed_space(srv, user, name, *, geo=None, surfaces=1):
    srv.spaces.save(user, name, {
        "owner": user, "name": name, "public": True, "geolocation": geo,
        "surfaces": [{"id": f"s{i}"} for i in range(surfaces)], "boundary": {}})


def _seed_asset(srv, id, scope, **fields):
    srv.library.upsert(id, kind="model", scope=scope, public=1, **fields)


def test_admin_tree_root_lists_all_users_with_nested_categories(srv, client):
    _seed_worlds(srv, "alice/agents/builder", "w1")
    _seed_space(srv, "alice", "living-room")
    _seed_asset(srv, "bob-asset", "bob/agents/builder", label="thing")
    node = client.post("/admin/tree", json={"path": "/"}).json()["node"]
    users = {c["label"] for c in node["children"]}
    assert {"alice", "bob"} <= users
    alice = next(c for c in node["children"] if c["label"] == "alice")
    cats = {c["label"] for c in alice["children"]}
    assert cats == {"worlds", "spaces", "assets"}
    worlds = next(c for c in alice["children"] if c["label"] == "worlds")
    assert {w["label"] for w in worlds["children"]} == {"w1"}


def test_admin_tree_narrows_by_path(srv, client):
    _seed_worlds(srv, "alice/agents/builder", "w1", "w2")
    node = client.post("/admin/tree", json={"path": "/alice/worlds"}).json()["node"]
    assert node["label"] == "worlds"
    assert {w["label"] for w in node["children"]} == {"w1", "w2"}


def test_admin_tree_unknown_user_errors(srv, client):
    r = client.post("/admin/tree", json={"path": "/nobody"}).json()
    assert r["ok"] is False and "nobody" in r["error"]


def test_admin_delete_single_world(srv, client):
    _seed_worlds(srv, "alice/agents/builder", "w1", "w2")
    r = client.post("/admin/delete", json={"path": "/alice/worlds/w1"}).json()
    assert r["ok"] is True
    assert srv.worlds.list("alice/agents/builder") == ["w2"]


def test_admin_delete_all_spaces(srv, client):
    _seed_space(srv, "alice", "room-a")
    _seed_space(srv, "alice", "room-b")
    r = client.post("/admin/delete", json={"path": "/alice/spaces"}).json()
    assert r["ok"] is True and "2 spaces" in r["deleted"]
    assert srv.spaces.list("alice") == []


def test_admin_delete_whole_user(srv, client):
    _seed_worlds(srv, "alice/agents/builder", "w1")
    _seed_space(srv, "alice", "room-a")
    _seed_asset(srv, "alice-asset", "alice/agents/builder")
    r = client.post("/admin/delete", json={"path": "/alice"}).json()
    assert r["ok"] is True
    assert srv.worlds.list("alice/agents/builder") == []
    assert srv.spaces.list("alice") == []
    assert srv.library.count_by_user("alice") == 0


def test_admin_delete_single_asset_scoped(srv, client):
    _seed_asset(srv, "keep", "alice/agents/builder")
    _seed_asset(srv, "drop", "alice/agents/builder")
    r = client.post("/admin/delete", json={"path": "/alice/assets/drop"}).json()
    assert r["ok"] is True
    assert srv.library.get("drop") is None and srv.library.get("keep") is not None


def test_admin_delete_refuses_active_world(srv, client):
    _seed_worlds(srv, srv.DEFAULT_SCOPE, "default")     # the active world, now on disk
    r = client.post("/admin/delete", json={"path": "/daniel/worlds/default"}).json()
    assert r["ok"] is False and "active world" in r["error"]
    assert srv.worlds.exists(srv.DEFAULT_SCOPE, "default")


def test_admin_delete_refuses_active_space(srv, client):
    _seed_space(srv, "daniel", "home")                  # active_space == "home" for daniel
    r = client.post("/admin/delete", json={"path": "/daniel/spaces/home"}).json()
    assert r["ok"] is False and "active space" in r["error"]
    assert srv.spaces.exists("daniel", "home")


def test_admin_delete_refuses_active_user(srv, client):
    _seed_worlds(srv, srv.DEFAULT_SCOPE, "w1")
    r = client.post("/admin/delete", json={"path": "/daniel"}).json()
    assert r["ok"] is False and "active user" in r["error"]


def test_admin_delete_empty_path_refused(srv, client):
    r = client.post("/admin/delete", json={"path": "/"}).json()
    assert r["ok"] is False and "everything" in r["error"]
