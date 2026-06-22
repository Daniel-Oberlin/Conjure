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


def test_annotate_asset_records_curation_and_alias(srv, client):
    iid = client.post("/images/generate", json={"prompt": "a shiba inu"}).json()["image_id"]
    r = client.post("/annotate_asset", json={"id": iid, "note": "my default dog", "favorite": True,
                                             "default_for": "dog"})
    assert r.json()["ok"] is True
    cat = srv.library.get(iid)
    assert cat["notes"] == "my default dog" and cat["favorite"] == 1
    assert srv.library.resolve_alias("dog") == iid      # alias pinned → reuse override


def test_annotate_unknown_asset_errors(srv, client):
    assert client.post("/annotate_asset", json={"id": "nope.png", "note": "x"}).json()["ok"] is False


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


def test_correct_asset_relabels_and_rejects(srv, client):
    # an X-wing wrongly cataloged under "starship enterprise"
    srv.resolver = FakeAssetResolver(record=ASSET_RECORD)
    client.post("/place_asset", json={"query": "starship enterprise", "size_m": 2})
    mid = f"{ASSET_RECORD.hash}.glb"
    assert client.post("/library/search", json={"query": "starship enterprise"}).json()["confidence_tier"] == "strong"
    r = client.post("/correct_asset", json={"id": mid, "label": "X-Wing", "reject_for": "starship enterprise"})
    assert r.json()["ok"] is True
    assert srv.library.get(mid)["label"] == "X-Wing"            # relabeled
    after = client.post("/library/search", json={"query": "starship enterprise"}).json()
    assert not any(c["id"] == mid for c in after["candidates"])  # excluded after reject


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
              "/outpaint_image", "/set_skybox", "/set_grounded_skybox", "/annotate_asset",
              "/library/search", "/place_cached_asset", "/correct_asset",
              "/skybox_from_image", "/assets/{filename}", "/ws",
              "/images/generators", "/images/generate", "/images/skybox", "/images/grounded_skybox",
              "/images/edit", "/images/outpaint", "/images/skybox_from", "/room", "/room/realign", "/reset",
              "/texture_surface", "/style_surface", "/tunnel"):
        assert p in paths, f"missing route {p}"


# --------------------------------------------------------------------------- room model

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
    # The friendly number is derived from the surface id (real_wall_1 → 1), so a surface that vanishes
    # (transient tracking loss → replace removes it) and comes back keeps the SAME number by
    # construction — no climbing, no put-down/pick-up renumbering.
    def fid():
        return next(e for e in _entities(client) if e["id"] == "real_wall_1")["meta"]["friendly_id"]
    wall = {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4]}
    client.post("/room", json={"client_id": "h1", "surfaces": [wall]})
    first = fid()
    client.post("/room", json={"client_id": "h1", "surfaces": []})          # wall drops out
    assert not any(e["id"] == "real_wall_1" for e in _entities(client))
    client.post("/room", json={"client_id": "h1", "surfaces": [wall]})       # and returns
    assert fid() == first                                                    # same number, not higher


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
    srv.clients.add(ws)
    try:
        await srv.realign_room()
    finally:
        srv.clients.discard(ws)
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


def test_room_replace_removes_stale_surfaces(srv, client):
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "a", "semantic": "wall", "position": [0, 1, -2]},
        {"id": "b", "semantic": "wall", "position": [1, 1, -2]}]})
    client.post("/room", json={"client_id": "h1", "surfaces": [
        {"id": "a", "semantic": "wall", "position": [0, 1, -2]}]})
    ids = {e["id"] for e in _entities(client)}
    assert "a" in ids and "b" not in ids


async def test_patch_is_broadcast_to_clients(srv):
    class FakeWS:
        def __init__(self):
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

    ws = FakeWS()
    srv.clients.add(ws)
    try:
        await srv.post_patch(Patch(ops=[{"op": "add", "entity": {"id": "c", "components": {}}}]))
    finally:
        srv.clients.discard(ws)
    assert ws.sent and ws.sent[-1]["type"] == "patch"
    assert ws.sent[-1]["patch"]["ops"][0]["op"] == "add"
