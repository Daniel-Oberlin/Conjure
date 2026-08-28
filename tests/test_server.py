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
    assert "billboard" not in img["components"]      # off by default


def test_place_image_billboard_adds_a_yaw_only_component(srv, client):
    # A free-standing billboard carries the client-side `billboard` component in yaw-only mode; each
    # headset aims it at its own camera, so no server-authored facing rotation is involved.
    r = client.post("/place_image", json={"image_id": _procure(client), "billboard": True}).json()
    assert r["ok"] is True
    img = next(e for e in _entities(client) if e["id"] == r["id"])
    assert img["components"]["billboard"] == {"yaw": True}


def test_place_image_billboard_rejects_on_surface(srv, client):
    # A wall-hung image stays flush to its wall — it can't also chase the viewer. Clean error, no silent drop.
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [0.7, 1.72, -1.04],
         "rotation": [0.0, -41.0, 0.0], "extent": [0.5, 0.4]}]})
    r = client.post("/place_image", json={
        "image_id": _procure(client), "on_surface": "wall art 18", "billboard": True}).json()
    assert r["ok"] is False and "free-standing" in r["error"]


def test_place_image_on_surface_aligns_and_fits_the_frame(srv, client):
    # A wall-art frame at a known (upright) orientation and size.
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [0.7, 1.72, -1.04],
         "rotation": [0.0, -41.0, 0.0], "extent": [0.5, 0.4]}]})
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 18"}).json()
    assert r["ok"] is True
    img = next(e for e in _entities(client) if e["id"] == r["id"])
    # faces the room interior = OPPOSITE the surface's outward normal, upright (no roll)
    from conjure.server import _forward
    rot = img["transform"]["rotation"]
    n = _forward([0.0, -41.0, 0.0])                            # surface outward normal
    cf = _forward(rot)                                         # photo's facing (+Z)
    assert all(abs(cf[i] + n[i]) < 0.05 for i in range(3)) and rot[2] == pytest.approx(0, abs=0.5)
    # fitted inside the 0.5 x 0.4 frame (square image ⇒ 0.4 x 0.4), not the default 1 m floating plane
    g = img["components"]["geometry"]
    assert g["width"] <= 0.5 + 1e-9 and g["height"] <= 0.4 + 1e-9 and max(g["width"], g["height"]) > 0.1
    # sits a couple cm in front of the surface (no z-fight), not exactly coplanar
    import math
    assert 0.01 < math.dist(img["transform"]["position"], [0.7, 1.72, -1.04]) < 0.05


def test_on_surface_image_re_anchors_when_the_surface_moves(srv, client):
    import math
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [0.7, 1.72, -1.04],
         "rotation": [0.0, -41.0, 0.0], "extent": [0.5, 0.4]}]})
    r = client.post("/place_image", json={"image_id": _procure(client), "on_surface": "wall art 18"}).json()
    eid = r["id"]
    img = next(e for e in _entities(client) if e["id"] == eid)
    assert img["meta"]["on_surface"] == "real_wall_art_18"            # home surface recorded
    p0 = img["transform"]["position"]
    # re-capture: the surface moved ~0.7 m (a re-registration). The image must FOLLOW, not strand.
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
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
    # content faces the room interior = OPPOSITE the wall's outward normal (+X → faces −X, yaw −90°), upright
    rot = img["transform"]["rotation"]
    assert rot[0] == pytest.approx(0, abs=0.5) and rot[1] == pytest.approx(-90, abs=0.5) and rot[2] == pytest.approx(0, abs=0.5)
    assert 0.01 < math.dist(img["transform"]["position"], [2.0, 1.5, 0.0]) < 0.05   # ~2 cm toward the room


def test_face_room_faces_opposite_the_surface_normal_upright(srv):
    from conjure.server import _face_room, _forward
    # content faces AWAY from the surface's (outward) normal — into the room — for any surface orientation
    for srot in ([0.0, 90.0, 0.0], [0.0, -41.0, 0.0], [90.0, 0.0, 0.0]):
        fr, n = _face_room(srot), _forward(srot)
        assert all(abs(fr["forward"][i] + n[i]) < 0.02 for i in range(3))
    # a floor (normal down) → content faces UP; a vertical wall stays upright (no roll)
    assert _face_room([90.0, 0.0, 0.0])["forward"][1] > 0.9
    assert _face_room([0.0, 90.0, 0.0])["rotation"][2] == pytest.approx(0, abs=0.5)


def test_face_room_aligns_flat_content_to_the_surface_rectangle(srv):
    # On an up-facing surface (a table yawed 30°) there's no gravity-up, so the image must align to the
    # SURFACE's own rectangle (its in-plane axis), not an arbitrary world axis that tilts it ~30°. The
    # image's up matches the surface's -Y (a 180° flip about vertical — +Y read consistently upside-down).
    from conjure.server import _face_room, _local_axis
    tsrot = [90.0, 30.0, 0.0]
    fr = _face_room(tsrot)
    content_up = _local_axis(fr["rotation"], (0.0, 1.0, 0.0))   # the image's up (its +Y) in world
    surf_axis = _local_axis(tsrot, (0.0, -1.0, 0.0))            # the table's -Y in-plane axis (rectangle edge)
    assert all(abs(content_up[i] - surf_axis[i]) < 0.02 for i in range(3))   # edges parallel, no tilt


def test_place_image_on_unknown_surface_errors(srv, client):
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 999"}).json()
    assert r["ok"] is False and "surface" in r["error"]


def test_place_image_unknown_id_errors(srv, client):
    assert client.post("/place_image", json={"image_id": "nope.png"}).json()["ok"] is False


# --- external asset import (POST /library/import) --------------------------------------------------

def _import(client, filename, data, dry_run=False, **hints):
    import base64
    return client.post("/library/import", json={"dry_run": dry_run, "items": [
        {"filename": filename, "data_b64": base64.b64encode(data).decode(), "hints": hints}]}).json()


def test_import_catalogs_an_image_and_is_dedup_by_content(srv, client):
    from conftest import TINY_PNG
    out = _import(client, "sunset.png", TINY_PNG)
    assert out["ok"] and out["imported"] == 1
    aid = out["results"][0]["id"]
    row = srv.library.get(aid)
    assert row and row["kind"] == "image" and row["width"] == 4 and row["height"] == 4
    assert row["source"] == f"cache://{aid}" and (srv.ASSET_CACHE / aid).exists()
    # Re-importing the same bytes returns the SAME id (content-addressed) — no duplicate row.
    again = _import(client, "sunset-copy.png", TINY_PNG)
    assert again["results"][0]["id"] == aid and srv.library.count() == 1


def test_import_dry_run_writes_nothing(srv, client):
    from conftest import TINY_PNG
    out = _import(client, "x.png", TINY_PNG, dry_run=True)
    assert out["ok"] and out["results"][0]["dry_run"] is True and out["results"][0]["kind"] == "image"
    assert srv.library.count() == 0                       # nothing catalogued


def test_import_rejects_unrecognized_file(srv, client):
    out = _import(client, "notes.txt", b"just text")
    assert out["imported"] == 0 and out["failed"] == 1
    assert out["results"][0]["ok"] is False


def test_import_tags_stereo_and_place_image_renders_per_eye(srv, client):
    from conftest import WIDE_PNG                          # 8x4 → each SBS eye is 4x4 (square)
    aid = _import(client, "beach.png", WIDE_PNG, stereo="sbs")["results"][0]["id"]
    assert _stereo_of(srv.library.get(aid)) == "sbs"
    # Placing it auto-detects stereo (no explicit arg): the plane fits the PER-EYE aspect (square,
    # not the packed 2:1) and carries the client-side `stereo` component.
    r = client.post("/place_image", json={"image_id": aid, "size_m": 1.0}).json()
    img = next(e for e in _entities(client) if e["id"] == r["id"])
    assert img["components"]["stereo"] == {"layout": "sbs"}
    g = img["components"]["geometry"]
    assert g["width"] == 1.0 and g["height"] == 1.0       # per-eye 4x4 → square, not 2:1


def test_place_image_billboard_and_stereo_compose(srv, client):
    # A free-standing stereo photo you can walk around: both components ride the same entity.
    aid = _import(client, "trail.png", __import__("conftest").WIDE_PNG, stereo="sbs")["results"][0]["id"]
    r = client.post("/place_image", json={"image_id": aid, "billboard": True}).json()
    img = next(e for e in _entities(client) if e["id"] == r["id"])
    assert img["components"]["billboard"] == {"yaw": True}
    assert img["components"]["stereo"] == {"layout": "sbs"}
    # billboard is still free-standing only, even for a stereo image.
    bad = client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_1", "semantic": "wall art", "position": [0, 1.5, -1],
         "rotation": [0, 0, 0], "extent": [0.5, 0.4]}]})
    assert bad.status_code == 200
    err = client.post("/place_image", json={
        "image_id": aid, "on_surface": "wall art 1", "billboard": True}).json()
    assert err["ok"] is False and "free-standing" in err["error"]


def test_stereo_refused_on_a_generated_image(srv, client):
    # The footgun: forcing stereo onto a generated (mono) image splits it into mismatched halves.
    gen = _procure(client)                                 # a generated image (op="generate")
    r = client.post("/place_image", json={"image_id": gen, "stereo": "sbs"}).json()
    assert r["ok"] is False and "generated" in r["error"] and "stereo" in r["error"]


def test_stereo_forced_ok_on_an_imported_untagged_image(srv, client):
    # An imported image with no stereo tag is plausibly a real pair — forcing stereo IS allowed.
    from conftest import WIDE_PNG
    aid = _import(client, "plain.png", WIDE_PNG)["results"][0]["id"]   # no stereo hint → untagged
    assert _stereo_of(srv.library.get(aid)) is None
    r = client.post("/place_image", json={"image_id": aid, "stereo": "sbs"}).json()
    assert r["ok"] is True
    img = next(e for e in _entities(client) if e["id"] == r["id"])
    assert img["components"]["stereo"] == {"layout": "sbs"}


def _stereo_of(row):
    import json
    return json.loads(row.get("attributes") or "{}").get("stereo")


def test_fit_dims_uses_per_eye_aspect_for_stereo(srv):
    from conjure.server import ImageRecord
    rec = ImageRecord(id="x.png", url="/assets/x.png", w=8, h=4, provider="", model="", prompt="", op="")
    frame = [2.0, 2.0]                                    # a square frame
    # Mono 8x4 (2:1) fills the frame width-limited → 2.0 x 1.0.
    assert srv._fit_dims(rec, frame) == (2.0, 1.0)
    # SBS: per-eye is 4x4 (square) → fills the square frame fully, not squeezed to 2:1.
    assert srv._fit_dims(rec, frame, "sbs") == (2.0, 2.0)


def test_first_eye_crops_the_stereo_pair_to_one_view(srv):
    import io

    from PIL import Image
    from conftest import WIDE_PNG                          # 8x4
    with Image.open(io.BytesIO(srv._first_eye(WIDE_PNG, "sbs"))) as im:
        assert im.size == (4, 4)                           # left half
    with Image.open(io.BytesIO(srv._first_eye(WIDE_PNG, "tb"))) as im:
        assert im.size == (8, 2)                           # top half


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


def _set_session_public(srv, public):
    """Set the LIVE session's visibility (§8.2) — visibility now lives on the session, not the world."""
    scope, sid = srv.active_scope, srv.active_sid
    meta = srv.sessions.load_meta(scope, sid) if srv.sessions.exists(scope, sid) else {}
    meta["public"] = public
    srv.sessions.save_meta(scope, sid, meta)


def test_public_uses_public_on_placement(srv, client):
    # build in a PRIVATE session → a new image inherits private
    _set_session_public(srv, False)
    iid = client.post("/images/generate", json={"prompt": "a pear"}).json()["image_id"]
    assert srv.library.get(iid)["public"] == 0
    # placing it while the session is still private is a no-op (no publish, no notice)
    assert "notice" not in client.post("/place_image", json={"image_id": iid}).json()
    assert srv.library.get(iid)["public"] == 0
    # now the session is public → placing the private asset publishes it + notices
    _set_session_public(srv, True)
    r = client.post("/place_image", json={"image_id": iid}).json()
    assert r["ok"] and "notice" in r and srv.library.get(iid)["public"] == 1


def test_public_uses_public_on_make_world_public(srv, client):
    _set_session_public(srv, False)
    iid = client.post("/images/generate", json={"prompt": "a pear"}).json()["image_id"]   # private (inherited)
    client.post("/place_image", json={"image_id": iid})                                   # placed in the private session
    assert srv.library.get(iid)["public"] == 0
    # flip the session public (via the world-visibility surface) → its private assets get published + reported
    r = client.post("/worlds/visibility", json={"public": True, "scope": "daniel/agents/builder"}).json()
    assert r["ok"] and r["published_assets"] == ["a pear"]
    assert srv.library.get(iid)["public"] == 1


def test_new_asset_inherits_active_session_visibility(srv, client):
    # made while a PRIVATE session is active ⇒ the asset is private
    _set_session_public(srv, False)
    iid = client.post("/images/generate", json={"prompt": "a pear"}).json()["image_id"]
    assert srv.library.get(iid)["public"] == 0
    # regenerated while a PUBLIC session is active ⇒ public
    srv.library.delete(iid)
    _set_session_public(srv, True)
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
    srv.library.upsert("pano.png", kind="image", scope=srv.DEFAULT_SCOPE, width=2100, height=900,
                       prompt="a sunset beach")
    r = client.post("/library/retag-skyboxes", json={}).json()
    assert r["ok"] and r["retagged"] == 1
    assert srv.library.get("pano.png")["kind"] == "skybox"
    res = client.post("/library/search", json={"query": "a sunset beach", "kind": "skybox"}).json()
    assert any(c["id"] == "pano.png" for c in res["candidates"])  # now found specifically as a skybox


def test_asset_in_agent_scope_walls_reuse_by_id_by_agent():
    # The by-id guard (place_cached_asset / _get_image) mirrors the catalog's hard agent wall.
    from conjure import server as S
    tok = S._caller_scope.set("daniel/agents/builder")
    try:
        assert S._asset_in_agent_scope({"scope": "daniel/agents/builder", "public": 0})     # own private
        assert S._asset_in_agent_scope({"scope": "friend/agents/builder", "public": 1})     # same agent, public
        assert not S._asset_in_agent_scope({"scope": "friend/agents/builder", "public": 0}) # same agent, other user, private
        assert not S._asset_in_agent_scope({"scope": "daniel/agents/outdoor", "public": 1}) # other agent, even public
        assert not S._asset_in_agent_scope(None)
    finally:
        S._caller_scope.reset(tok)


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
              "/images/edit", "/images/outpaint", "/images/skybox_from", "/space/capture", "/space/realign", "/reset",
              "/texture_surface", "/style_surface", "/tunnel"):
        assert p in paths, f"missing route {p}"


# --------------------------------------------------------------------------- room model

def test_room_unchanged_capture_is_not_rebroadcast(srv, client):
    # fix A: a settled room stops emitting patches — an identical re-capture makes NO new revision, so the
    # client isn't re-applying (and rebuilding) every surface every ~2 s (the "pops").
    body = {"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4],
         "rotation": [0, 0, 0]}]}
    client.post("/space/capture", json=body)                          # first capture → adds the surface
    rev = client.get("/world").json()["rev"]
    client.post("/space/capture", json=body)                          # identical → within tolerance
    assert client.get("/world").json()["rev"] == rev         # no new patch broadcast
    body["surfaces"][0]["position"] = [0, 1, -2.1]           # 10 cm = sub-threshold drift (< 0.5 m)
    client.post("/space/capture", json=body)
    assert client.get("/world").json()["rev"] == rev         # drift ignored — seed doesn't churn (§7.4)
    body["surfaces"][0]["position"] = [0, 1, -2.7]           # 70 cm = a real relocation (> 0.5 m)
    client.post("/space/capture", json=body)
    assert client.get("/world").json()["rev"] > rev          # a real move DOES update


def test_room_authority_taken_over_only_when_stale(srv, client, monkeypatch):
    import conjure.server as S
    body = lambda cid: {"client_id": cid, "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4]}]}
    assert client.post("/space/capture", json=body("h1")).json()["ok"] is True     # h1 claims authority
    r = client.post("/space/capture", json=body("h2")).json()                      # h2 while h1 is live → refused
    assert r["ok"] is False and "authority" in r["error"]
    assert client.get("/world").json()["environment"]["captureAuthority"] == "h1"
    monkeypatch.setattr(S, "_authority_ts", S._authority_ts - S._AUTH_TTL - 1)   # h1 goes idle
    assert client.post("/space/capture", json=body("h2")).json()["ok"] is True     # h2 takes over the stale authority
    assert client.get("/world").json()["environment"]["captureAuthority"] == "h2"


def test_room_ingest_creates_real_surfaces_and_boundary(srv, client):
    body = {"client_id": "h1",
            "surfaces": [{"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2],
                          "extent": [3, 2.4]}],
            "boundary": {"floorPolygon": [[0, 0], [3, 0], [3, 3], [0, 3]], "height": 2.6}}
    assert client.post("/space/capture", json=body).json()["ok"] is True
    e = next(e for e in _entities(client) if e["id"] == "real_wall_1")
    assert e["meta"]["real"] is True and e["meta"]["semantic"] == "wall"
    assert e["meta"]["friendly_id"] == 1                  # short id for annotations/voice reference
    env = client.get("/world").json()["environment"]
    assert env["spacePresentation"]["active"] is True
    assert env["captureAuthority"] == "h1"       # coordination + geometry sit BESIDE the presentation,
    assert env["boundary"]["height"] == 2.6      # not inside it — neither is a presentation choice


def test_door_surface_defaults_to_translucent(srv, client):
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
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
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [3, 2.4],
         "holes": [{"x": 0.5, "y": -0.2, "w": 0.9, "h": 2.0}]},
        {"id": "real_window_1", "semantic": "window", "position": [-0.8, 1.4, -2], "extent": [0.8, 0.9]}]})
    ents = {e["id"]: e for e in _entities(client)}
    assert ents["real_wall_1"]["components"]["surface"]["holes"] == [{"x": 0.5, "y": -0.2, "w": 0.9, "h": 2.0}]
    glass = ents["real_window_1"]["components"]["material"]
    assert glass["transparent"] is True and glass["opacity"] < 0.5    # faint glass — you can see outside


def test_inset_structural_anchor_round_trips_into_meta(srv, client):
    # §5.3: an inset's corner-relative anchor (along-wall corner distances + floor/ceiling edge distances)
    # is persisted under meta, so any client can reconstruct its spot from shared structure, not the centroid.
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [4, 2.4]},
        {"id": "real_door_1", "semantic": "door", "position": [0.5, 1.0, -2], "extent": [0.9, 2.0],
         "hostWall": "real_wall_1",
         "along": [{"corner": "real_wall_2", "dist": -1.5}],
         "vertical": [{"edge": "floor", "dist": 1.0}, {"edge": "ceiling", "dist": 1.4}]}]})
    door = next(e for e in _entities(client) if e["id"] == "real_door_1")
    assert door["meta"]["host_wall"] == "real_wall_1"
    assert door["meta"]["along"] == [{"corner": "real_wall_2", "dist": -1.5}]
    assert door["meta"]["vertical"] == [{"edge": "floor", "dist": 1.0}, {"edge": "ceiling", "dist": 1.4}]


def test_wall_holes_update_only_on_opening_count_change(srv, client):
    # Structural-only ingest (§7.4): the seed's openings update when one is ADDED/REMOVED (count changes),
    # not on a same-count reposition (sub-structural drift — the client renders the live holes locally).
    def wall(holes):
        return {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [3, 2.4], "holes": holes}
    one, moved = [{"x": 0.5, "y": 0, "w": 0.9, "h": 2.0}], [{"x": -0.7, "y": 0, "w": 0.8, "h": 1.9}]
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [wall(one)]})

    def holes():
        return next(e for e in _entities(client) if e["id"] == "real_wall_1")["components"]["surface"]["holes"]
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [wall(moved)]})   # same count → sub-structural
    assert holes() == one                                            # unchanged (seed doesn't churn on drift)
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [wall(one + moved)]})  # 1 → 2 openings
    assert holes() == one + moved                                    # opening ADDED → seed updated


def test_friendly_id_stable_after_remove_readd(srv, client):
    # The friendly number is derived from the surface id (real_couch_1 → 1), so a surface that's pruned and
    # comes back keeps the SAME number by construction. (Pruning is prune-on-first-absence: the client owns
    # the absence debounce and posts its CONFIRMED set, docs §7.)
    def fid():
        return next(e for e in _entities(client) if e["id"] == "real_couch_1")["meta"]["friendly_id"]
    couch = {"id": "real_couch_1", "semantic": "couch", "position": [0, 0.4, -2], "extent": [2, 0.8]}
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [couch]})
    first = fid()
    client.post("/space/capture", json={"client_id": "h1", "surfaces": []})           # confirmed-absent (replace) → pruned
    assert not any(e["id"] == "real_couch_1" for e in _entities(client))
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [couch]})      # and returns
    assert fid() == first                                                    # same number, not higher


def test_anchored_surface_protected_from_pruning_others_still_prune(srv, client):
    # bug B: a surface with a photo pinned to it (meta.on_surface) keeps its id even when confirmed-absent
    # so the photo never orphans; a surface with NO content pinned prunes normally (so stray duplicate
    # surfaces don't accumulate).
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_1", "semantic": "wall art", "position": [0, 1.5, -2], "extent": [0.5, 0.5]},
        {"id": "real_wall_art_2", "semantic": "wall art", "position": [2, 1.5, -2], "extent": [0.5, 0.5]}]})
    srv.store.apply_patch([{"op": "add", "entity": {                        # hang a photo on art_1
        "id": "ent_photo", "meta": {"on_surface": "real_wall_art_1"},
        "components": {"geometry": {"primitive": "plane"}}}}])
    client.post("/space/capture", json={"client_id": "h1", "surfaces": []})          # both confirmed-absent (replace)
    ids = {e["id"] for e in _entities(client)}
    assert "real_wall_art_1" in ids                                        # anchored → kept (no orphaning)
    assert "real_wall_art_2" not in ids                                    # unanchored → pruned


def test_texture_surface_resolves_by_friendly_id(srv, client):
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_floor_3", "semantic": "floor", "position": [0, 0, 0], "extent": [3, 3]}]})
    fid = next(e for e in _entities(client) if e["id"] == "real_floor_3")["meta"]["friendly_id"]
    image_id = _procure(client)
    r = client.post("/texture_surface", json={"target": str(fid), "image_id": image_id})
    assert r.json()["ok"] is True and r.json()["count"] == 1


def test_plain_sky_color_replaces_a_skybox_image(srv, client):
    # Setting a plain sky color must REMOVE a skybox image (they're mutually exclusive) — a dotted
    # `sky.color` merge would leave `sky.src` behind, so the skybox lingers in the doc and reappears on
    # reload ("remove the skybox" wouldn't stick). set_environment writes the whole `sky` wholesale.
    client.post("/patch", json={"ops": [{"op": "env", "set": {"sky": {"src": "/assets/x.jpg", "grounded": True}}}]})
    assert srv.store.doc["environment"]["sky"].get("src")
    client.post("/patch", json={"ops": [{"op": "env", "set": {"sky": {"color": "#101018"}}}]})
    sky = srv.store.doc["environment"]["sky"]
    assert sky == {"color": "#101018"} and "src" not in sky        # skybox image dropped, not merged under


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
    client.post("/space/capture", json={"client_id": "h1", "surfaces": []})
    r = client.post("/space/capture", json={"client_id": "h2", "surfaces": []})
    assert r.json()["ok"] is False and "authority" in r.json()["error"]


def test_room_recapture_updates_pose_but_keeps_director_style(srv, client):
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2]}]})
    # director colors + shows the wall
    client.post("/patch", json={"ops": [{"op": "update", "id": "real_wall_1", "set": {
        "components.material.color": "#0000ff", "components.material.visible": True}}]})
    # re-capture with a STRUCTURAL relocation (> 0.5 m — a sub-threshold refine would be ignored, §7.4)
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2.7]}]})
    e = next(e for e in _entities(client) if e["id"] == "real_wall_1")
    assert e["transform"]["position"] == [0, 1, -2.7]              # geometry updated
    assert e["components"]["material"]["color"] == "#0000ff"        # director's style preserved
    assert e["components"]["material"]["visible"] is True


def test_texture_surface_maps_image_onto_surfaces(srv, client):
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
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
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2], "extent": [3, 2.4]}]})
    r = client.post("/style_surface", json={"target": "wall", "color": "blue", "opacity": 0.4})
    assert r.json()["ok"] is True and r.json()["count"] == 1
    mat = next(e for e in _entities(client) if e["id"] == "real_wall_1")["components"]["material"]
    assert mat["color"] == "blue" and mat["opacity"] == 0.4
    assert mat["transparent"] is True and mat["visible"] is True


def test_style_surface_needs_color_or_opacity(srv, client):
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1, -2]}]})
    assert client.post("/style_surface", json={"target": "wall"}).json()["ok"] is False


def test_room_replace_prunes_missing_surface_on_first_absence(srv, client):
    # A `replace` post (the default) is the client's CONFIRMED set — it owns the absence debounce (docs §7),
    # so a surface missing from it is genuinely gone and the server prunes it at once (no server-side counter).
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "a", "semantic": "couch", "position": [0, 1, -2]},
        {"id": "b", "semantic": "couch", "position": [1, 1, -2]}]})
    assert {"a", "b"} <= {e["id"] for e in _entities(client)}         # both present
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [       # b omitted → confirmed absent
        {"id": "a", "semantic": "couch", "position": [0, 1, -2]}]})
    ids = {e["id"] for e in _entities(client)}
    assert "a" in ids and "b" not in ids                             # b pruned on first absence, a stays


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
    assert r["ok"] and r["world"] == "Blade Runner 1"    # the NAME is kept verbatim now; only *matching*
                                                         # is slug-insensitive, so "blade-runner-1" still
                                                         # resolves to it
    assert "in_default" not in {e["id"] for e in _entities(client)}   # new world starts clean
    client.post("/patch", json={"ops": [{"op": "add", "entity": {"id": "in_blade", "components": {}}}]})

    lst = client.post("/worlds/list", json={}).json()
    # `worlds` is {id, name} pairs now — the id is what survives a rename, so it's what an agent stores.
    assert {w["name"] for w in lst["worlds"]} == {"default", "Blade Runner 1"}
    assert all(w["id"].startswith("wld_") for w in lst["worlds"])
    assert lst["active"] == next(w["id"] for w in lst["worlds"] if w["name"] == "Blade Runner 1")

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
    assert env.get("spacePresentation", {}).get("edgesVisible") is True


def test_a_world_rename_moves_nothing_and_strands_nothing():
    """Renaming is a metadata edit: the id is unchanged, so the active pointer, the session record and
    anything else holding a reference keep working. This is the whole reason for the id."""
    from conjure.world import WorldDir, WorldStore
    import tempfile, pathlib as _pl
    d = WorldDir(_pl.Path(tempfile.mkdtemp()))
    wid = d.save("meadow", WorldStore({"rev": 0, "environment": {}, "entities": []}))
    d.set_active(wid)

    d.rename("meadow", "The Meadow")
    assert d.resolve("The Meadow") == wid
    assert d.get_active() == wid                       # the live pointer never moved
    assert d.name_of(wid) == "The Meadow"
    assert d.resolve("meadow") is None                 # the old name is genuinely gone (no aliases)
    assert [e["name"] for e in d.entries()] == ["The Meadow"]


def test_world_names_are_unique_within_a_session():
    # Uniqueness is what keeps name→id resolution total, so a person or an agent can keep saying "meadow".
    from conjure.world import WorldDir, WorldStore
    import tempfile, pathlib as _pl, pytest as _pt
    d = WorldDir(_pl.Path(tempfile.mkdtemp()))
    d.save("meadow", WorldStore({"rev": 0, "environment": {}, "entities": []}))
    second = d.save("beach", WorldStore({"rev": 0, "environment": {}, "entities": []}))
    with _pt.raises(ValueError, match="already exists"):
        d.rename(second, "Meadow")                     # slug-insensitive clash
    with _pt.raises(ValueError, match="already exists"):
        d.create("MEADOW", WorldStore({"rev": 0, "environment": {}, "entities": []}))


def test_reset_room_authority_clears_stale_id(srv):
    from conjure.world import WorldStore
    s = WorldStore({"id": "x", "name": "x", "rev": 0, "entities": [],
                    "environment": {"captureAuthority": "hs_dead"}})
    srv._reset_room_authority(s)
    assert s.doc["environment"]["captureAuthority"] is None
    srv._reset_room_authority(WorldStore({"id": "y", "name": "y", "rev": 0, "entities": [],
                                          "environment": {}}))   # no room/env → must not raise


def test_switching_into_a_world_drops_its_stale_authority(srv, client):
    from conjure.world import WorldStore
    # a world saved by a PAST session, pinned to a now-dead headset id
    srv.worlds.save(srv.DEFAULT_SCOPE, "old-room", WorldStore(
        {"id": "o", "name": "o", "rev": 1, "entities": [],
         "environment": {"captureAuthority": "hs_dead", "spacePresentation": {"active": True}}}))
    assert client.post("/worlds/switch", json={"name": "old-room"}).json()["ok"]
    assert client.get("/world").json()["environment"]["captureAuthority"] is None
    # a NEW headset id can now capture (before the fix this was rejected forever)
    r = client.post("/space/capture", json={"client_id": "hs_new", "surfaces": [
        {"id": "real_wall_1", "semantic": "wall", "position": [0, 1.2, -2], "extent": [3, 2.4]}]}).json()
    assert r["ok"] is True
    assert client.get("/world").json()["environment"]["captureAuthority"] == "hs_new"


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
             "environment": {"sky": {"color": "#001"}, "spacePresentation": {"edgesVisible": True,
                             "surfaceStyles": {"real_couch_41": {"color": "green", "visible": True}}}},
             "space": "daniel/spaces/home"}
    doc = srv._compose(world, space)
    ids = {e["id"] for e in doc["entities"]}
    assert ids == {"ent_dragon", "real_wall_0", "real_couch_41"}     # placed + space geometry
    couch = next(e for e in doc["entities"] if e["id"] == "real_couch_41")
    assert couch["components"]["material"]["color"] == "green"        # world override applied
    wall = next(e for e in doc["entities"] if e["id"] == "real_wall_0")
    assert wall["components"]["material"]["color"] == "#888"          # no override → space base
    assert doc["environment"]["boundary"]["height"] == 2.6                        # boundary from space
    assert "surfaceStyles" not in doc["environment"]["spacePresentation"]          # overlay not broadcast
    assert "space" not in doc                                         # ref not broadcast


def test_decompose_extracts_only_real_overrides_and_round_trips(srv):
    space = _space_doc()
    world = {"rev": 5, "entities": [{"id": "ent_dragon", "meta": {"generated": True}, "components": {}}],
             "environment": {"spacePresentation": {"edgesVisible": True,
                             "surfaceStyles": {"real_couch_41": {"color": "green", "visible": True}}}}}
    composed = srv._compose(world, space)
    back = srv._decompose(composed, space)
    assert [e["id"] for e in back["entities"]] == ["ent_dragon"]      # geometry stripped, placed kept
    styles = back["environment"]["spacePresentation"]["surfaceStyles"]
    assert set(styles) == {"real_couch_41"}                          # only the OVERRIDDEN surface recorded
    assert styles["real_couch_41"]["color"] == "green" and styles["real_couch_41"]["visible"] is True
    assert "boundary" not in back["environment"]                                  # boundary belongs to the space
    # round-trip: re-composing reproduces the same rendered surfaces (materials + geometry)
    assert srv._compose(back, space)["entities"] == composed["entities"]


def test_room_geometry_is_shared_across_worlds_styling_is_per_world(srv, client):
    # capture a room and style the couch in the current ('default') world
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
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
    """specs/spaces.md §6.1: the legacy geometry-embedded migration is gone. A pre-space world
    doc is no longer rewritten on load, and its INLINE real surfaces are NOT resurrected — real geometry
    lives only in the space now (fed by capture via _save_active). Objects still compose; a world with no
    space ref renders room-less (step 5 removed the anonymous-'home' Path B fallback) and resolves to
    UNSET rather than VOID — not-decided-yet, which a headset may still claim (specs/spaces.md §4.3)."""
    from conjure.world import WorldStore
    embedded = {
        "id": "l", "name": "legacy", "rev": 3, "environment": {"boundary": {"height": 2.6}},
        "entities": [
            {"id": "ent_box", "meta": {"generated": True}, "components": {}},
            {"id": "real_table_2", "meta": {"real": True, "semantic": "table"},
             "transform": {"position": [0, 0.5, -1]},
             "components": {"surface": {"extent": [1, 1]}, "material": {"color": "blue"}}}]}
    srv.worlds.save(srv.DEFAULT_SCOPE, "legacy", WorldStore(embedded))
    client.post("/worlds/switch", json={"name": "legacy"})
    ids = {e["id"] for e in _entities(client)}
    assert "ent_box" in ids                                     # placed objects compose as before
    assert "real_table_2" not in ids                            # inline geometry is NOT resurrected
    assert srv.active_space == srv.UNSET                        # ABSENT ref → not-yet-decided, not a decision
    assert srv._no_space() is True                              # …and it renders room-less either way
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
    client.post("/space/capture", json={"client_id": "h", "surfaces": [
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
    assert _wname(srv) == "office-world" and srv.active_space == "office"
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


def test_index_injects_occlusion_mode(srv, client, monkeypatch):
    import dataclasses
    # default: off is injected so the client component resolves to today's behaviour.
    assert 'window.CONJURE_OCCLUSION="off";' in client.get("/").text
    # --occlusion hands|hands-solid flows through settings → the injected window global the component reads.
    for mode in ("hands", "hands-solid"):
        monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, occlusion=mode))
        assert f'window.CONJURE_OCCLUSION="{mode}";' in client.get("/").text


def test_index_injects_controller_beam_settings(srv, client, monkeypatch):
    import dataclasses
    html = client.get("/").text
    assert '<script src="/static/controller-beams.js?v=' in html   # the beam module loads
    # the linger duration is config-driven (seconds → ms), never hard-coded in the client
    assert "window.CONJURE_BEAM_MS=10000;" in html
    assert "window.CONJURE_BEAM_TRIGGER=0.05;" in html
    monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, beam_timeout=4.5, beam_trigger=0.2))
    html2 = client.get("/").text
    assert "window.CONJURE_BEAM_MS=4500;" in html2 and "window.CONJURE_BEAM_TRIGGER=0.2;" in html2


def test_time_endpoint_returns_epoch_ms(client):
    import time
    before = time.time() * 1000.0
    t = client.get("/time").json()["t"]
    after = time.time() * 1000.0
    # epoch ms, bracketed by wall-clock either side of the call (generous slack for slow CI).
    assert before - 1000.0 <= t <= after + 1000.0


def test_conjure_and_dismiss_module(client):
    # conjure fireflies → an entity carrying the fireflies component + module meta, config passed through.
    r = client.post("/module", json={"module": "fireflies", "config": {"count": 20, "seed": 3}}).json()
    assert r["ok"] and r["module"] == "fireflies"
    eid = r["id"]
    e = next(x for x in _entities(client) if x["id"] == eid)
    assert e["components"]["fireflies"] == {"count": 20, "seed": 3}
    assert e["meta"]["module"] == "fireflies" and e["meta"]["dynamic"] is True
    assert "billboard" not in e["components"]                     # a volume effect doesn't face the viewer
    # reconfigure in place by id (no duplicate entity).
    r2 = client.post("/module", json={"module": "fireflies", "name": eid, "config": {"count": 99}}).json()
    assert r2["id"] == eid
    assert sum(1 for x in _entities(client) if x["id"] == eid) == 1
    assert next(x for x in _entities(client) if x["id"] == eid)["components"]["fireflies"]["count"] == 99
    # unknown module → clean error, nothing added.
    assert client.post("/module", json={"module": "nope"}).json()["ok"] is False
    # a fireflies entity added OUTSIDE the tool (raw patch, no meta.module) — dismiss-by-kind must still
    # catch it by its component, so "remove the fireflies" clears everything.
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "raw-flies", "components": {"fireflies": {"count": 5}}}}]})
    # dismiss by kind → removes both the tool-placed and the raw one.
    d = client.post("/module/dismiss", json={"module": "fireflies"}).json()
    assert d["ok"] and eid in d["removed"] and "raw-flies" in d["removed"]
    assert not any(x["id"] in (eid, "raw-flies") for x in _entities(client))


def test_conjure_water_module(client):
    # water is registered; config (incl. src + tuning) passes through to the component; module meta set.
    r = client.post("/module", json={"module": "water",
                                     "config": {"src": "http://x/koi.png", "damping": 0.99}}).json()
    assert r["ok"] and r["module"] == "water"
    e = next(x for x in _entities(client) if x["id"] == r["id"])
    assert e["components"]["water"] == {"src": "http://x/koi.png", "damping": 0.99}
    assert e["meta"]["module"] == "water"
    assert "billboard" not in e["components"]                     # faces the viewer at creation, NOT a billboard
    assert e["transform"]["rotation"] == [0.0, 0.0, 0.0]         # no live gaze in tests → default spawn facing


def test_conjure_module_billboard_param_composes(client):
    # billboard is orthogonal + composable: the param attaches the standalone component to ANY module.
    r = client.post("/module", json={"module": "water", "billboard": True,
                                     "config": {"src": "http://x/y.png"}}).json()
    e = next(x for x in _entities(client) if x["id"] == r["id"])
    assert e["components"]["billboard"] == {"yaw": True} and "water" in e["components"]


def test_water_on_surface_needs_a_matching_surface(client):
    r = client.post("/module", json={"module": "water", "on_surface": "no-such-42",
                                     "config": {"src": "http://x/y.png"}}).json()
    assert r["ok"] is False and "no room surface" in r["error"]


def test_module_event_relays_to_peers_only(client):
    # tier-B shared bus: a client's module_event fans out to the OTHER clients, not back to the sender.
    with client.websocket_connect("/ws?user=daniel") as a, client.websocket_connect("/ws?user=bob") as b:
        a.receive_json(); b.receive_json()                       # initial snapshots
        a.send_json({"type": "module_event", "event": "water.touch",
                     "payload": {"id": "w1", "u": 0.25, "v": 0.75}})
        got = None
        for _ in range(6):                                        # drain any presence → find the relayed event
            m = b.receive_json()
            if m["type"] == "module_event":
                got = m; break
        assert got and got["event"] == "water.touch" and got["payload"]["u"] == 0.25


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


def test_space_select_commits_once_per_client(srv, client):
    # a client (identified by cid) commits its selection ONCE per claim epoch — repeat votes are ignored
    # so GPS jitter can't re-thrash the choice (steps 4/7).
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    j = {"cid": "cA"}
    assert client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12, **j}).json()["created_space"]
    r2 = client.post("/space/select", json={"matched": False, "lat": 1.0, "lon": 2.0, **j}).json()
    assert r2 == {"ok": True, "selected": False}               # the SAME client's second vote is ignored
    # and /geolocation stops offering candidates to that client once it has committed
    assert client.post("/geolocation", json={"lat": 1.0, "lon": 2.0, **j}).json()["selected"] is True


# ---- steps 4/7: AR admission gate + claim/occupancy lifecycle -------------------------------------
def _wait_until(pred, tries=100):
    import time as _t
    for _ in range(tries):
        if pred():
            return True
        _t.sleep(0.01)
    return pred()


def test_provisional_boot_first_ar_user_establishes_from_anywhere(srv, client):
    """D1/D7: boot is provisional — while UNOCCUPIED (no AR holder), the first AR user establishes a space
    from wherever they are, even though the booted-active space is elsewhere. A new place → mint, not
    refuse."""
    _geo_space(srv, "daniel", "home", 37.77, -122.42)              # booted-active space, in SF
    assert not srv._occupied()                                     # nobody holds it yet
    r = client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12,   # London — new place
                                           "user": "bob", "cid": "bob1"}).json()
    assert r.get("created_space") == "bob/space-1" and "refused" not in r   # established, not gated
    assert srv.active_space == "space-1" and srv.active_space_owner == "bob"


def test_force_occupied_engages_the_gate_for_a_single_headset(srv, client, monkeypatch):
    """--force-occupied (test flag) pins the space CLAIMED with no real holder, so one headset can exercise
    the gate: matching the active space ⇒ admitted, anything else ⇒ refused."""
    import dataclasses
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, force_occupied=True))
    assert srv._occupied() and not srv._space_holders            # occupied via the flag, no real socket
    admit = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                               "user": "daniel", "cid": "d1"}).json()
    assert admit.get("admitted") is True                         # matching the active space ⇒ admitted
    refuse = client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12,
                                                "user": "bob", "cid": "b1"}).json()
    assert refuse.get("refused") is True                         # not in it ⇒ refused


def test_occupied_space_admits_a_colocated_ar_user(srv, client):
    """D4/D6: while the active space is CLAIMED, an AR user who matches it is ADMITTED (co-location join) —
    no world change, nothing minted."""
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    srv._space_holders.add(object())                              # simulate an AR headset holding the space
    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                           "user": "bob", "cid": "bob1"}).json()
    assert r.get("admitted") is True and r["joined"] == "daniel/home"
    assert srv.active_space == "home" and srv.active_space_owner == "daniel"   # unchanged
    assert srv.spaces.list("bob") == []                          # nothing minted for the joiner


def test_occupied_space_refuses_an_ar_user_not_in_it(srv, client):
    """D4/D6: while claimed, an AR user who does NOT match the active space (a different space, or no match
    at all) is REFUSED — not switched in, nothing minted. They get an info message and stay in passthrough."""
    from conjure.world import WorldStore
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    _geo_space(srv, "carol", "studio", 37.7701, -122.4201)        # a DIFFERENT nearby space
    srv._space_holders.add(object())                              # the space is claimed + occupied
    # (a) no match → refused
    r = client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12,
                                           "user": "bob", "cid": "bob1"}).json()
    assert r.get("refused") is True and "daniel" in r["msg"]
    assert srv.active_space == "home" and srv.active_space_owner == "daniel"   # unchanged
    assert srv.spaces.list("bob") == []                          # nothing minted
    # (b) matched a DIFFERENT space (not the active one) → also refused
    r2 = client.post("/space/select", json={"matched": True, "owner": "carol", "name": "studio",
                                            "user": "eve", "cid": "eve1"}).json()
    assert r2.get("refused") is True
    assert srv.active_space == "home"                            # still the claimed space


def _void_world(srv, client, name="beach"):
    """Put the live pointer in a DELIBERATELY room-less world (the outdoor case)."""
    assert client.post("/worlds/new", json={"name": name, "outdoor": True}).json()["ok"]
    assert srv.active_space == srv.VOID


def test_a_deleted_world_sends_you_back_as_the_same_agent(srv, client):
    """specs/spaces.md §6.1. Your space remembers the last world you used in it, so putting the headset on returns you
    there. If that world was deleted the system correctly builds a replacement — but it used to build it
    in the general-purpose BUILDER's scope regardless of who you were with, so you came back as a
    different agent. Preserve the agent; own the world as yourself."""
    from conjure import server as S
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    srv.spaces.save("daniel", "home", {**srv.spaces.load("daniel", "home"),
                                       "last_scope": "daniel/agents/scratch",
                                       "last_world": "wld_deadbeef01"})     # …which does not exist
    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                           "user": "daniel", "cid": "hs1"}).json()
    assert r["ok"] and not r.get("refused")
    assert S.active_scope == "daniel/agents/scratch"        # the agent that space was last used from
    assert S.active_space == "home"                         # …and we're in the room


def test_the_fallback_skips_an_agent_that_cannot_live_in_a_space(srv):
    """The chain skips a candidate whose agent declares `world.outdoor` — its worlds are room-less by
    declaration (specs/agents.md §3), so preferring it would contradict its own definition — and one whose definition
    no longer resolves at all. Then, and only then, the default agent."""
    from conjure import server as S
    assert S._entry_scope_for("daniel", prefer="daniel/agents/scratch") == "daniel/agents/scratch"
    # outdoor can't host a world tied to a space → falls through to the live scope (builder here)
    assert S._entry_scope_for("daniel", prefer="daniel/agents/outdoor") == S.DEFAULT_SCOPE
    # a deleted/renamed agent → likewise
    assert S._entry_scope_for("daniel", prefer="daniel/agents/vanished") == S.DEFAULT_SCOPE
    # and the world is owned by the CALLER even when the space (and its agent) belong to someone else
    assert S._entry_scope_for("bob", prefer="daniel/agents/scratch") == "bob/agents/scratch"


def test_an_outdoor_world_is_not_relocated_by_recognising_the_room(srv, client):
    """specs/spaces.md §4.3. The client votes its capture against candidates even in a void world — it must, or an outdoor
    re-entry never resolves a space at all. But resolving WHICH space you're in and MOVING you to that
    space's last world are different things, and only the first is wanted when you deliberately chose to
    be nowhere. Reported: leave and restart inside an outdoor world, put the headset on, and you're pulled
    into whichever agent last used your living room."""
    from conjure.world import WorldStore
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    ah = srv.worlds.save(srv.DEFAULT_SCOPE, "animal-house", WorldStore(
        {"name": "animal-house", "rev": 1, "environment": {"space": "daniel/home"}, "entities": []}))
    _void_world(srv, client)
    before = srv.active_world
    # after the switch, for the same reason as above — so a relocation, if it happened, would be visible
    srv.spaces.save("daniel", "home", {**srv.spaces.load("daniel", "home"),
                                       "last_scope": srv.DEFAULT_SCOPE, "last_world": ah})

    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                           "user": "daniel", "cid": "hs1"}).json()
    assert r["ok"] and r.get("kept_outdoor") is True
    assert r.get("admitted") is True                       # the space IS claimed — occupancy is still real
    assert srv.active_world == before                      # …but we did not move
    assert srv.active_space == srv.VOID                    # still deliberately room-less


def test_a_boot_placeholder_IS_relocated_by_recognising_the_room(srv, client):
    """The contrast that makes the no-relocation rule safe, and a **regression guard rather than a new behaviour**: a boot
    placeholder has always relocated, and must keep doing so. A world minted before anything knew the
    space is UNSET, not VOID — a guess, not a choice. Were both spelled VOID, the branch above would
    decline to relocate and strand a headset user in a blank world, which is why C2b needs C2."""
    from conjure.world import WorldStore
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    ah = srv.worlds.save(srv.DEFAULT_SCOPE, "animal-house", WorldStore(
        {"name": "animal-house", "rev": 1, "environment": {"space": "daniel/home"}, "entities": []}))
    # a placeholder with NO space ref at all — what boot mints before a headset has said anything
    srv.worlds.save(srv.DEFAULT_SCOPE, "placeholder", WorldStore(
        {"id": "ph", "name": "placeholder", "rev": 1, "environment": {}, "entities": []}))
    client.post("/worlds/switch", json={"name": "placeholder"})
    # Stamp the return pointer AFTER the switch: switching autosaves the OUTGOING world, which restamps
    # the space's history to whatever we just left. `recent` and `last_*` are written together by
    # `_save_active` and can only disagree in a hand-built fixture, so set both — the head of the history
    # IS the last world.
    srv.spaces.save("daniel", "home", {**srv.spaces.load("daniel", "home"),
                                       "recent": [[srv.DEFAULT_SCOPE, ah]],
                                       "last_scope": srv.DEFAULT_SCOPE, "last_world": ah})

    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                           "user": "daniel", "cid": "hs1"}).json()
    assert r["ok"] and not r.get("kept_outdoor")
    assert srv.worlds.name_of(srv.DEFAULT_SCOPE, srv.active_world) == "animal-house"   # relocated
    assert srv.active_space == "home"
    assert srv.UNSET != srv.VOID     # the two room-less states are distinct, which is what made this safe


def test_autosave_does_not_turn_a_placeholder_into_a_decision(srv, client):
    """The load-bearing half of specs/spaces.md §4.3. `_save_active` used to stamp VOID on any room-less world, so a boot
    placeholder became *deliberately* outdoor within one autosave (~1s) — after which the no-relocation rule would refuse to
    relocate it. UNSET must persist as the ABSENCE of the key, so it reads back as UNSET."""
    from conjure.world import WorldStore
    srv.worlds.save(srv.DEFAULT_SCOPE, "placeholder", WorldStore(
        {"id": "ph", "name": "placeholder", "rev": 1, "environment": {}, "entities": []}))
    client.post("/worlds/switch", json={"name": "placeholder"})
    srv._save_active()
    wd = srv.worlds.load(srv.DEFAULT_SCOPE, "placeholder").doc
    assert "space" not in wd["environment"]                # absence preserved — still undecided
    assert srv.active_space == srv.UNSET                   # …and still readable as not-yet-decided
    # a DELIBERATE void world, by contrast, is recorded as such
    _void_world(srv, client, name="dunes")
    srv._save_active()
    assert srv.worlds.load(srv.DEFAULT_SCOPE, "dunes").doc["environment"]["space"] == srv.VOID


def test_ar_hold_over_ws_occupies_then_release_unlocks_reselection(srv, client):
    """step 7 lifecycle over the wire: an AR client's `hold` makes the space occupied (a mismatched joiner
    is then refused); `release` (leaving AR) frees it — re-selection re-opens and a new user can establish."""
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    with client.websocket_connect("/ws?user=daniel") as holder:
        holder.receive_json()                                    # snapshot
        holder.send_json({"type": "hold"})
        assert _wait_until(lambda: srv._occupied())              # the hold registered → claimed
        refused = client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12,
                                                     "user": "bob", "cid": "bob1"}).json()
        assert refused.get("refused") is True                    # a non-co-located AR user is gated out
        holder.send_json({"type": "release"})                    # daniel leaves AR
        assert _wait_until(lambda: not srv._occupied())          # → unclaimed
    # now unoccupied: a fresh AR user can establish from a new place (re-selection re-opened by _unclaim)
    established = client.post("/space/select", json={"matched": False, "lat": 51.5, "lon": -0.12,
                                                     "user": "bob", "cid": "bob2"}).json()
    assert established.get("created_space") == "bob/space-1"


# ---- Phase 4 step 1: per-connection user + public-join gate --------------------------------------
def test_ws_resync_resends_current_snapshot(client):
    # a client that dropped patches while blanked (AR space selection) sends `resync` on un-blank and
    # gets the CURRENT world back, recovering anything added while it was blanked.
    with client.websocket_connect("/ws?user=daniel") as ws:
        ws.receive_json()                                        # initial snapshot
        assert client.post("/module", json={"module": "fireflies", "name": "ff-resync"}).json()["ok"]
        ws.send_json({"type": "resync"})
        snap = None
        for _ in range(6):                                       # drain the broadcast patch → find the snapshot
            m = ws.receive_json()
            if m["type"] == "snapshot":
                snap = m; break
        assert snap is not None
        assert any(e["id"] == "ff-resync" for e in snap["world"]["entities"])


def test_ws_owner_and_public_guest_receive_the_world(srv, client):
    with client.websocket_connect("/ws?user=daniel") as ws:        # owner
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["owner"] == "daniel"   # owner in snapshot (desktop-guest spawn)
    with client.websocket_connect("/ws?user=bob") as ws:           # guest, world public by default
        assert ws.receive_json()["type"] == "snapshot"
    with client.websocket_connect("/ws") as ws:                    # no user → default (owner)
        assert ws.receive_json()["type"] == "snapshot"


def test_ws_guest_refused_private_session_is_blocked_and_evicted(srv, client):
    _set_session_public(srv, False)                                # visibility is the SESSION's now (§8.2)
    with client.websocket_connect("/ws?user=bob") as ws:           # guest + private → evicted, no world
        msg = ws.receive_json()
        # entry-block mirrors an eviction: the SAME `evicted` signal (client shows overlay + auto-resumes),
        # not a top-only `info` that never resumed.
        assert msg["type"] == "evicted" and "private" in msg["msg"] and "daniel" in msg["msg"]
        assert "bob" not in srv.clients.values()                   # not joined → excluded from broadcasts
        assert "bob" in srv._blocked.values()                      # tracked so a go-public re-admits it
    with client.websocket_connect("/ws?user=daniel") as ws:        # owner still gets in
        assert ws.receive_json()["type"] == "snapshot"


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, m):
        self.sent.append(m)


async def test_regate_bumps_connected_guests_when_the_live_session_goes_private(srv):
    # §8.3: re-gating an already-joined set drops every non-owner guest (with an info line) once the live
    # session is private — the owner stays. (Unit-level: TestClient can't hold a live ws across a POST.)
    import conjure.server as S
    S.clients.clear()
    owner_ws, guest_ws = _FakeWS(), _FakeWS()
    S.clients[owner_ws], S.clients[guest_ws] = "daniel", "bob"
    _set_session_public(srv, False)                                # live session now private
    await S._regate_clients()
    assert guest_ws not in S.clients and S.clients.get(owner_ws) == "daniel"   # guest bumped, owner kept
    assert S._blocked.get(guest_ws) == "bob"                       # kept in _blocked for later re-admit
    assert guest_ws.sent and guest_ws.sent[-1]["type"] == "evicted" and "private" in guest_ws.sent[-1]["msg"]
    assert owner_ws.sent == []                                     # owner untouched
    # go public → re-admit the blocked guest with a fresh snapshot, no reconnect
    _set_session_public(srv, True)
    await S._readmit_clients()
    assert S.clients.get(guest_ws) == "bob" and guest_ws not in S._blocked
    assert guest_ws.sent[-1]["type"] == "snapshot"                 # re-rendered
    S.clients.clear(); S._blocked.clear()


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


def test_realign_is_owner_gated(srv, client):
    """A guest could force everyone to re-capture. Low blast radius — realign only broadcasts a
    `recapture` nudge, and the ingest that follows is itself owner-gated — but it is a write-shaped
    action on the owner's space, so it belongs behind the same gate as the capture it triggers."""
    assert client.post("/space/realign", json={}, headers={"X-Conjure-User": "bob"}).status_code == 403
    assert client.post("/space/realign", json={}, headers={"X-Conjure-User": "daniel"}).status_code == 200


def test_every_geometry_and_scene_write_is_gated(srv, client):
    """Pins gate MEMBERSHIP, not just route existence. `_OWNER_ONLY_PATHS` is matched with `in` —
    exact strings — so renaming a route without updating the set silently ungates it instead of
    404ing. The route-inventory test above would not notice; this does."""
    for p in ("/patch", "/reset", "/space/capture", "/space/realign", "/style_surface",
              "/texture_surface", "/place_asset", "/place_image", "/set_skybox", "/manipulate"):
        assert p in srv._OWNER_ONLY_PATHS, f"{p} is a write but is not owner-gated"
    # every gated path must actually be a registered route — a typo would gate nothing
    routes = {r.path for r in srv.app.routes}
    assert srv._OWNER_ONLY_PATHS <= routes, f"gated but unrouted: {srv._OWNER_ONLY_PATHS - routes}"


def test_guest_cannot_edit_the_world_but_reads_are_open(srv, client):
    # the reported bug: a guest styling a surface in the owner's world → still 403 (edit-rights follow ownership)
    assert client.post("/style_surface", json={"target": "door", "color": "blue"},
                       headers={"X-Conjure-User": "bob"}).status_code == 403
    # but reads/listing stay open to guests
    assert client.post("/worlds/list", json={}, headers={"X-Conjure-User": "bob"}).status_code == 200


def test_guest_can_visit_owners_public_session(srv, client):
    # bob creates a world (persists daniel's builder session), making it discoverable in the builder agent
    client.post("/worlds/new", json={"name": "guest-world", "scope": "bob/agents/builder"},
                headers={"X-Conjure-User": "bob"})
    daniels = [a for a in sessions_available(client, "bob/agents/builder") if a["owner"] == "daniel"]
    assert daniels, "daniel's public session should be discoverable in this agent"
    tgt = daniels[0]
    # bob VISITS daniel's session by (owner, name) — resolved in bob's active agent → everyone comes along
    r = client.post("/session/switch",
                    json={"scope": "bob/agents/builder", "owner": "daniel", "session": tgt["title"]},
                    headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] and r.get("owner") == "daniel" and srv.active_scope == "daniel/agents/builder"
    # ...and bob is a guest: still can't edit daniel's world (owner-only writes)
    assert client.post("/style_surface", json={"target": "wall", "color": "red"},
                       headers={"X-Conjure-User": "bob"}).status_code == 403


def test_visiting_a_private_session_is_refused(srv, client):
    # bob creates a world (his session becomes live), then makes his SESSION private
    client.post("/worlds/new", json={"name": "den", "scope": "bob/agents/builder"},
                headers={"X-Conjure-User": "bob"})
    r0 = client.post("/session/visibility", json={"public": False, "scope": "bob/agents/builder"},
                     headers={"X-Conjure-User": "bob"}).json()
    assert r0["ok"] and r0["public"] is False
    assert "bob" not in {a["owner"] for a in sessions_available(client, "daniel/agents/builder")}
    # daniel can't VISIT bob's private session (by owner+name, in daniel's agent)
    bob_sid = client.get("/sessions", params={"scope": "bob/agents/builder"}).json()["active"]
    r = client.post("/session/switch",
                    json={"scope": "daniel/agents/builder", "owner": "bob", "session": bob_sid}).json()
    assert r["ok"] is False and "private" in r["error"]


def test_visit_resolves_owner_and_session_case_insensitively(srv, client):
    # Voice-friendly: owner + session name resolve exact-then-loose (case + separators).
    client.post("/worlds/new", json={"name": "den", "scope": "bob/agents/builder"},
                headers={"X-Conjure-User": "bob"})
    bob_sid = client.get("/sessions", params={"scope": "bob/agents/builder"}).json()["active"]
    r = client.post("/session/switch", json={
        "scope": "daniel/agents/builder", "owner": "BOB", "session": bob_sid.upper()}).json()
    assert r["ok"] and r.get("owner") == "bob" and srv.active_scope == "bob/agents/builder"


def test_session_visibility_controls_discovery(srv, client):
    sc = "bob/agents/builder"
    # bob creates a world in his session (public by default) → his session is discoverable by others
    assert client.post("/worlds/new", json={"name": "secret", "scope": sc},
                       headers={"X-Conjure-User": "bob"}).json()["ok"]
    owners = lambda: {a["owner"] for a in sessions_available(client, "daniel/agents/builder")}
    assert "bob" in owners()
    # make bob's SESSION private → it drops out of discovery (§8.2)
    r = client.post("/session/visibility", json={"public": False, "scope": sc},
                    headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] and r["public"] is False
    assert "bob" not in owners()
    # flip it back public
    client.post("/session/visibility", json={"public": True, "scope": sc}, headers={"X-Conjure-User": "bob"})
    assert "bob" in owners()


def test_new_world_defaults_public(srv, client):
    # created with no `public` arg → its session is public by default ⇒ discoverable by other users
    assert client.post("/worlds/new", json={"name": "shared", "scope": "bob/agents/builder"},
                       headers={"X-Conjure-User": "bob"}).json()["ok"]
    assert "bob" in {a["owner"] for a in sessions_available(client, "daniel/agents/builder")}


def test_worlds_list_is_session_local_no_cross_user(srv, client):
    # bob has a public world; the AGENT-facing world list must NOT surface it (docs/specs/agents.md §7.2) —
    # cross-user discovery is gone from /worlds/list and lives only in the shell's /sessions listing.
    client.post("/worlds/new", json={"name": "shared", "scope": "bob/agents/builder"},
                headers={"X-Conjure-User": "bob"})
    listing = client.post("/worlds/list", json={"scope": "daniel/agents/builder"}).json()
    assert "available" not in listing
    assert "shared" not in listing["worlds"]


# ---- step 5: world-creation adopts the active space (D5), else VOID; Path B fallback removed -----------
def test_new_world_adopts_the_active_space(srv, client):
    """D5/step 5: a fresh world is tied to the currently-active shared space up front (build your own world
    in it — D3), so it composes that space's geometry and no anonymous 'home' is ever minted."""
    from conjure import server as S
    # the live (daniel/home) world holds a captured wall; creating a new world saves it into the space,
    # then the new world adopts daniel/home and composes that wall back in.
    srv.store.doc["entities"].append({"id": "real_wall_9", "meta": {"real": True, "semantic": "wall"},
        "transform": {"position": [0, 1, -2]}, "components": {"surface": {"extent": [3, 2.4]}}})
    assert client.post("/worlds/new", json={"name": "loft"}).json()["ok"]
    assert S.active_space == "home" and S.active_space_owner == "daniel"   # resolved the stamped active space
    assert any(e["id"] == "real_wall_9" for e in _entities(client))     # composes the active space's geometry
    # the composed live doc drops the ref (compose pops it), but it's persisted on the world doc
    assert srv.worlds.load("daniel/agents/builder", "loft").doc["environment"]["space"] == "daniel/home"


def test_new_world_is_void_when_no_active_space(srv, client):
    """D5/step 5: with no active space (unclaimed server → active_space == VOID), a new NON-outdoor world is
    born VOID — the honest 'no room yet', not the old anonymous-'home' Path B fallback."""
    from conjure import server as S
    S.active_space = S.VOID                                             # unclaimed: no AR user established a space
    assert client.post("/worlds/new", json={"name": "sketch"}).json()["ok"]
    assert srv.store.doc["environment"]["space"] == "<void>"
    assert S.active_space == "<void>"
    assert "<void>" not in srv.spaces.list("daniel")                   # nothing anonymous minted


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


def test_agent_switch_keeps_the_live_space(srv, client):
    """Switching agents mints a `default` world in the NEW scope — and it must adopt the live space, or
    you walk out of your own room. Reported from the headset: builder was in a fully-composed room, an
    agent switch landed in a void world, and the new agent correctly reported 'no room surfaces yet'."""
    from conjure import server as S
    srv.store.doc["entities"].append({"id": "real_wall_9", "meta": {"real": True, "semantic": "wall"},
        "transform": {"position": [0, 1, -2]}, "components": {"surface": {"extent": [3, 2.4]}}})
    # `scratch`, not `outdoor`: an outdoor agent's worlds are room-less BY DECLARATION (specs/agents.md §3), so it would
    # pass this test for the wrong reason — or rather fail it for the right one.
    r = client.post("/scope/activate", json={"scope": "daniel/agents/scratch"}).json()
    assert r["ok"] and not r.get("unchanged")
    assert S.active_scope == "daniel/agents/scratch"
    assert S.active_space == "home" and S.active_space_owner == "daniel"     # still in the same room
    assert any(e["id"] == "real_wall_9" for e in _entities(client))          # composes the room's geometry
    assert srv.worlds.load("daniel/agents/scratch", r["id"]).doc["environment"]["space"] == "daniel/home"


def test_a_new_session_first_world_keeps_the_live_space(srv, client):
    """The same hole in the OTHER implicit mint path: `session new` builds the session's first world."""
    from conjure import server as S
    srv.store.doc["entities"].append({"id": "real_floor_3", "meta": {"real": True, "semantic": "floor"},
        "transform": {"position": [0, 0, 0]}, "components": {"surface": {"extent": [4, 4]}}})
    assert client.post("/session/new", json={"scope": S.DEFAULT_SCOPE, "title": "Second"}).json()["ok"]
    assert S.active_space == "home" and S.active_space_owner == "daniel"
    assert any(e["id"] == "real_floor_3" for e in _entities(client))
    assert srv.worlds.load(S.DEFAULT_SCOPE, S.active_world).doc["environment"]["space"] == "daniel/home"


def test_implicit_mint_degrades_to_void_in_someone_elses_private_space(srv, client):
    """`/worlds/new` REFUSES to build in another user's private space (an explicit ask deserves an error).
    An agent switch is navigation and must not hard-fail, so it degrades to VOID instead — never silently
    adopting a space the creator has no right to build in."""
    from conjure import server as S
    _geo_space(srv, "carol", "loft", 1.0, 2.0, public=False)
    monkey = (S.active_space_owner, S.active_space)
    S.active_space_owner, S.active_space = "carol", "loft"
    try:
        assert client.post("/worlds/new", json={"name": "trespass"}).json()["ok"] is False   # explicit → error
        r = client.post("/scope/activate", json={"scope": "daniel/agents/scratch"}).json()   # implicit → VOID
        assert r["ok"]
        assert srv.worlds.load("daniel/agents/scratch", r["id"]).doc["environment"]["space"] == "<void>"
    finally:
        S.active_space_owner, S.active_space = monkey


def test_an_outdoor_agents_worlds_are_room_less_however_they_are_minted(srv, client):
    """An agent whose point is to put you SOMEWHERE ELSE declares `world.outdoor`, and every mint path
    honours it. Without this, the space stamp (which every path now applies) gave the outdoor agent's
    constructor-built first world the whole room you were standing in — measured at 59 surfaces on the
    real capture. `new_world(outdoor=True)` only ever covered "this one world is a sky"."""
    srv.store.doc["entities"].append({"id": "real_wall_9", "meta": {"real": True, "semantic": "wall"},
        "transform": {"position": [0, 1, -2]}, "components": {"surface": {"extent": [3, 2.4]}}})
    # the implicit path: an agent switch mints this scope's first world
    r = client.post("/scope/activate", json={"scope": "daniel/agents/outdoor"}).json()
    assert r["ok"]
    assert srv.worlds.load("daniel/agents/outdoor", r["id"]).doc["environment"]["space"] == "<void>"
    assert not any(e.get("meta", {}).get("real") for e in _entities(client))   # no room composed in
    # the explicit path, with the flag omitted — the agent's declaration still wins
    assert client.post("/worlds/new", json={"name": "dunes",
                                            "scope": "daniel/agents/outdoor"}).json()["ok"]
    assert srv.worlds.load("daniel/agents/outdoor", "dunes").doc["environment"]["space"] == "<void>"


def _probe_agent(tmp_path, monkeypatch, name, block):
    """A throwaway agent definition on a temp search path, so a test can exercise the REAL declaration
    read (`resolve_agent_dir` → `_agent_block`) rather than stubbing the helper it's testing."""
    import json as _json

    from conjure import config as _config
    d = tmp_path / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.json").write_text(_json.dumps({"description": "probe", "prompt": "p", **block}))
    monkeypatch.setattr(_config, "AGENTS_PATH", [tmp_path / "agents", _config.BUNDLED_AGENTS_DIR])
    return f"daniel/agents/{name}"


def test_reset_agent_wipes_it_back_to_never_used(srv, client, tmp_path, monkeypatch):
    """specs/agents.md §6.3. Testing a first run means removing every trace of an agent — and every `delete` refuses to
    touch what is currently live, which is exactly the agent you want to reset. So it's its own verb: the
    sequence (purge → clear the pointers that named what was purged → land somewhere coherent) is the
    part that's easy to get wrong, and doing it by hand means editing the live pointer with the server
    down. We did that twice."""
    from conjure import server as S
    scope = _probe_agent(tmp_path, monkeypatch, "throwaway", {
        "session": {"first_world": {"name": "parlour"}}})
    assert client.post("/scope/activate", json={"scope": scope}).json()["ok"]
    client.post("/session/new", json={"scope": scope})           # a second session, with worlds
    assert len(srv.sessions.list(scope)) == 2
    assert S.active_scope == scope                               # …and we are standing in it
    # something distinctive to look for afterwards — session ids are sequential and get REUSED after a
    # purge, so their names prove nothing about whether the contents really went
    client.post("/worlds/new", json={"name": "keepsake", "scope": scope})
    assert srv.worlds.exists(scope, "keepsake")

    r = client.post("/agent/reset", json={"user": "daniel", "agent": "throwaway"}).json()
    assert r["ok"] and r["sessions"] == 2 and r["was_live"] is True
    assert len(srv.sessions.list(scope)) == 1                    # re-entry minted ONE fresh session…
    assert not srv.worlds.exists(scope, "keepsake")              # …and nothing came back with it
    assert r["world"] == "parlour"                               # rebuilt from the agent's opening
    assert S.active_scope == scope
    # the fresh session is genuinely fresh — un-greeted, un-seeded, so the constructor will run again
    sid = srv.sessions.get_active(scope)
    meta = srv.sessions.load_meta(scope, sid)
    assert meta["greeted"] is False and meta["seeded"] is False


def test_reset_keeps_assets_unless_asked(srv, client, tmp_path, monkeypatch):
    """Generated content is expensive and is not part of "the conversation", so it survives by default.
    Pass `assets` when the opening you're testing GENERATES one — otherwise the constructor quietly
    reuses nothing and you never exercise what you meant to."""
    scope = _probe_agent(tmp_path, monkeypatch, "hoarder", {})
    srv.library.upsert("keep.png", kind="image", scope=scope, label="a sky")
    client.post("/scope/activate", json={"scope": scope})

    r = client.post("/agent/reset", json={"user": "daniel", "agent": "hoarder"}).json()
    assert r["ok"] and r["assets"] == 0
    assert srv.library.get("keep.png") is not None               # kept

    r2 = client.post("/agent/reset", json={"user": "daniel", "agent": "hoarder",
                                           "assets": True}).json()
    assert r2["ok"] and r2["assets"] == 1
    assert srv.library.get("keep.png") is None                   # purged on request


def test_reset_refuses_an_agent_that_does_not_exist(srv, client):
    r = client.post("/agent/reset", json={"user": "daniel", "agent": "nosuchagent"}).json()
    assert r["ok"] is False and "nothing to reset" in r["error"]


def test_autosave_never_resurrects_a_deleted_session(srv, client, tmp_path, monkeypatch):
    """What makes the reset simple, and correct on its own: `_save_active` writes the live world back to
    disk, so purging the live session and then letting autosave run would re-create what was just
    removed. Keyed on the session DIRECTORY, not on `session.json` — a session can hold worlds before any
    meta is written, and treating that as deleted would silently disable autosave for it."""
    from conjure import server as S
    scope = _probe_agent(tmp_path, monkeypatch, "ghost", {})
    client.post("/scope/activate", json={"scope": scope})
    sid = S.active_sid
    assert srv.sessions.dir(scope, sid).exists()

    srv.sessions.delete(scope, sid)                              # purged out from under the live pointer
    S._save_active()                                             # must be a no-op, not a resurrection
    assert not srv.sessions.dir(scope, sid).exists()


def test_every_session_mint_path_runs_the_agents_opening(srv, client, tmp_path, monkeypatch):
    """specs/agents.md §7.5. `first_world` used to have exactly one caller — `/session/new`. An agent switch minted a bare
    `default` from `world.on_create` alone, and a session switch hard-coded the name `home`. All three now
    share one builder, so an agent's declared opening happens however you arrive."""
    scope = _probe_agent(tmp_path, monkeypatch, "lounge", {
        "session": {"first_world": {"name": "parlour",
                                    "on_create": [{"cmd": "show_edges", "args": {"on": False}}]}}})

    # (a) agent switch — the path that skipped the opening entirely
    assert client.post("/scope/activate", json={"scope": scope}).json()["world"] == "parlour"
    assert srv.store.doc["environment"]["spacePresentation"]["edgesVisible"] is False

    # (b) session new — the path that always had it
    assert client.post("/session/new", json={"scope": scope}).json()["ok"]
    assert srv.worlds.name_of(scope, srv.active_world) == "parlour"

    # (c) session switch into a session with no world yet — the path that hard-coded `home`
    sid = srv._next_sid(scope)
    srv.sessions.save_meta(scope, sid, {"id": sid, "owner": "daniel", "agent": "lounge",
                                        "title": "Third", "public": True, "active_world": "",
                                        "llm": "", "greeted": False, "seeded": False})
    assert client.post("/session/switch", json={"scope": scope, "session": sid}).json()["ok"]
    assert srv.worlds.name_of(scope, srv.active_world) == "parlour"
    assert srv.store.doc["environment"]["spacePresentation"]["edgesVisible"] is False


def test_a_failing_opening_aborts_and_leaves_you_where_you_were(srv, client, tmp_path, monkeypatch):
    """§5a. The opening is the fallible part of construction (it calls image models), so it is built
    BEFORE anything is written — an abort is therefore a no-op with nothing to roll back, and the answer
    to "where do you go" is *nowhere*. Prototype-phase choice: a half-constructed session is worse than a
    refused switch."""
    from conjure import server as S
    scope = _probe_agent(tmp_path, monkeypatch, "doomed", {
        "session": {"first_world": {"name": "nope", "on_create": [
            {"tool": "generate_skybox_image", "args": {"description": "a sky"}, "as": "sky"},
            {"tool": "set_skybox", "args": {"image_id": "${sky.image_id}"}}]}}})
    monkeypatch.setattr(S, "image_generators", {})        # no generator ⇒ the constructor fails
    before_scope, before_world = S.active_scope, S.active_world

    r = client.post("/scope/activate", json={"scope": scope}).json()
    assert r["ok"] is False and "constructor failed" in r["error"]
    assert S.active_scope == before_scope and S.active_world == before_world   # you did not move
    assert srv.worlds.list(scope) == []                                        # and nothing was written
    assert srv.sessions.list(scope) == []

    # the explicit route refuses identically
    r2 = client.post("/session/new", json={"scope": scope}).json()
    assert r2["ok"] is False and "constructor failed" in r2["error"]
    assert srv.sessions.list(scope) == []


def test_an_agent_can_declare_its_sessions_private(srv, client, tmp_path, monkeypatch):
    """Sessions are public by default because the shared experience is the feature — but an agent can be
    private BY NATURE, and the only lever before this was to instruct the model to flip visibility on its
    first turn. That makes privacy contingent on an LLM remembering to act. Now it's a declaration, and
    it holds on EVERY mint path, not just the explicit one."""
    from conjure import server as S
    scope = _probe_agent(tmp_path, monkeypatch, "hush", {"session": {"public": False}})
    # implicit path — an agent switch mints this scope's first session
    assert client.post("/scope/activate", json={"scope": scope}).json()["ok"]
    assert srv.sessions.load_meta(scope, "session-1")["public"] is False
    # and it is live, so every gate that keys on the live session's visibility sees private
    assert S._active_public() is False

    # explicit path — `session new` in the same scope
    assert client.post("/session/new", json={"scope": scope}).json()["ok"]
    assert srv.sessions.load_meta(scope, "session-2")["public"] is False


def test_a_session_is_public_unless_the_agent_says_otherwise(srv, client, tmp_path, monkeypatch):
    """The other half — an agent that declares nothing, and one that declares public explicitly."""
    from conjure import server as S
    scope = _probe_agent(tmp_path, monkeypatch, "chatty", {"session": {"greeting": "hi"}})
    assert client.post("/scope/activate", json={"scope": scope}).json()["ok"]
    assert srv.sessions.load_meta(scope, "session-1")["public"] is True
    assert S._active_public() is True


def test_session_visibility_default_is_read_off_the_declaration(srv, tmp_path, monkeypatch):
    """The unit behind the two above: opt-in, and an unreadable def is public (the safe-for-sharing
    default, matching every other agent)."""
    from conjure import server as S
    assert S._agent_session_public(
        _probe_agent(tmp_path, monkeypatch, "hush2", {"session": {"public": False}})) is False
    assert S._agent_session_public(S.DEFAULT_SCOPE) is True
    assert S._agent_session_public("daniel/agents/nonexistent") is True


def test_only_the_declaring_agent_is_outdoor(srv):
    """`world.outdoor` is opt-in and read off the agent definition, so it can't leak between agents."""
    from conjure import server as S
    assert S._agent_wants_outdoor("daniel/agents/outdoor") is True
    assert S._agent_wants_outdoor("daniel/agents/builder") is False
    assert S._agent_wants_outdoor("daniel/agents/nonexistent") is False    # unreadable def ⇒ not outdoor


def test_a_normal_agent_still_adopts_the_space(srv, client):
    """The other half: `world.outdoor` is opt-in, so an agent that doesn't declare it is unaffected."""
    srv.store.doc["entities"].append({"id": "real_wall_9", "meta": {"real": True, "semantic": "wall"},
        "transform": {"position": [0, 1, -2]}, "components": {"surface": {"extent": [3, 2.4]}}})
    assert client.post("/worlds/new", json={"name": "loft"}).json()["ok"]
    assert srv.worlds.load("daniel/agents/builder", "loft").doc["environment"]["space"] == "daniel/home"


def test_an_outdoor_agent_may_be_used_inside_someone_elses_private_space(srv, client):
    """An outdoor world wants no space, so the build-permission question doesn't arise — refusing here
    would mean you couldn't switch to a sky agent while standing in a friend's room."""
    from conjure import server as S
    _geo_space(srv, "carol", "loft", 1.0, 2.0, public=False)
    keep = (S.active_space_owner, S.active_space)
    S.active_space_owner, S.active_space = "carol", "loft"
    try:
        assert client.post("/worlds/new", json={"name": "trespass"}).json()["ok"] is False   # builder: refused
        r = client.post("/worlds/new", json={"name": "sky", "scope": "daniel/agents/outdoor"}).json()
        assert r["ok"] is True                                                               # outdoor: fine
        assert srv.worlds.load("daniel/agents/outdoor", "sky").doc["environment"]["space"] == "<void>"
    finally:
        S.active_space_owner, S.active_space = keep


def test_agent_switch_mints_a_session_marked_fresh(srv, client):
    """A session born of an agent switch must carry `greeted`/`seeded` = False, like one from
    `/session/new`. The agent server's constructor hooks gate on `is not False` (an ABSENT flag means a
    legacy session it must not retro-greet), so omitting them here meant a switched-to agent NEVER seeded
    its state or spoke its greeting. Reported as 'why no state file at all?'."""
    assert client.post("/scope/activate", json={"scope": "daniel/agents/scratch"}).json()["ok"]
    meta = srv.sessions.load_meta("daniel/agents/scratch", "session-1")
    assert meta["greeted"] is False and meta["seeded"] is False
    # and the explicit path still agrees — one shape of fresh-session meta, not two
    assert client.post("/session/new", json={"scope": "daniel/agents/scratch"}).json()["ok"]
    fresh = srv.sessions.load_meta("daniel/agents/scratch", "session-2")
    assert fresh["greeted"] is False and fresh["seeded"] is False


def test_ensure_session_does_not_reset_an_existing_sessions_flags(srv):
    """Only a NEWLY created session is marked fresh — re-ensuring a session that has already greeted must
    not make it greet again."""
    from conjure import server as S
    scope = "daniel/agents/outdoor"
    S._ensure_session(scope, "session-1")
    meta = srv.sessions.load_meta(scope, "session-1")
    meta.update(greeted=True, seeded=True)
    srv.sessions.save_meta(scope, "session-1", meta)
    S._ensure_session(scope, "session-1")                       # again — must be a no-op on the flags
    again = srv.sessions.load_meta(scope, "session-1")
    assert again["greeted"] is True and again["seeded"] is True


def test_space_for_new_world_is_the_one_stamp_every_mint_path_shares(srv):
    """The unit behind the three tests above — plus the boot opt-out, the one caller that legitimately
    runs before any space is resolved."""
    from conjure import server as S
    assert S._space_for_new_world(S.DEFAULT_SCOPE) == "daniel/home"          # live space, not yet on disk
    assert S._space_for_new_world(S.DEFAULT_SCOPE, outdoor=True) == "<void>"
    S.active_space = S.VOID
    assert S._space_for_new_world(S.DEFAULT_SCOPE) == "<void>"               # nothing live to adopt
    # `adopt_space=False` leaves the ref off entirely, so `_activate` reads it as "no space chosen yet"
    assert "space" not in S._new_world_store(S.DEFAULT_SCOPE, adopt_space=False).doc["environment"]


# ---- step 6: space visibility (D8) — public space = anyone builds; private = owner-only creation --------
def test_set_space_visibility_defaults_to_active_and_is_scope_bound(srv, client):
    _geo_space(srv, "daniel", "home", 37.77, -122.42)            # active space daniel/home (public)
    _geo_space(srv, "carol", "loft", 1.0, 2.0)                   # someone else's space
    # owner toggles their CURRENT space private (name omitted ⇒ the active space they own)
    r = client.post("/space/visibility", json={"public": False}).json()
    assert r["ok"] and r["space"] == "daniel/home" and r["public"] is False
    assert srv.spaces.load("daniel", "home")["public"] is False
    # scope-bound: bob can't change carol's space (it's not in bob's scope)
    r2 = client.post("/space/visibility", json={"public": False, "scope": "bob/agents/builder",
                                                "name": "loft"}).json()
    assert r2["ok"] is False and "no space" in r2["error"]


def test_private_space_blocks_others_world_creation(srv, client):
    """D8: only the owner may create worlds in a PRIVATE space; making it public re-opens creation. Not
    retroactive — the owner's existing worlds are untouched."""
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    client.post("/space/visibility", json={"public": False})    # daniel makes his active space private
    # a guest can't build their own world in daniel's private space
    r = client.post("/worlds/new", json={"name": "bobworld", "scope": "bob/agents/builder"},
                    headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] is False and "private" in r["error"]
    assert not srv.worlds.exists("bob/agents/builder", "bobworld")
    # the owner can still build in their own space
    assert client.post("/worlds/new", json={"name": "danworld"}).json()["ok"]
    # re-open it → the guest can now build their own world in it (D3, "your world in someone else's space")
    client.post("/space/visibility", json={"public": True})
    assert client.post("/worlds/new", json={"name": "bobworld", "scope": "bob/agents/builder"},
                       headers={"X-Conjure-User": "bob"}).json()["ok"]
    assert srv.worlds.load("bob/agents/builder", "bobworld").doc["environment"]["space"] == "daniel/home"


def test_space_select_refuses_building_in_a_private_space_with_no_world(srv, client):
    # a co-located non-owner who matches a PRIVATE space with no joinable world can't mint one there (D8)
    srv.spaces.save("daniel", "home", {"owner": "daniel", "name": "home", "public": False,
        "geolocation": {"lat": 37.77, "lon": -122.42}, "surfaces": [], "boundary": None})
    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                           "user": "bob", "cid": "b1"}).json()
    assert r.get("refused") is True and "private" in r["msg"]
    assert srv.worlds.list("bob/agents/builder") == []          # nothing built for bob


def sessions_available(client, scope):
    """Other users' PUBLIC sessions the shell offers to visit (docs/specs/agents.md §7.2) — the discovery
    that replaced the agent-facing 'available worlds' list."""
    return client.get("/sessions", params={"scope": scope}).json()["available"]


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
def _wname(srv, scope=None, wid=None):
    """The display name of a world id — `active_world` is an ID now, so tests compare names through this."""
    return srv.worlds.name_of(scope or srv.active_scope, wid or srv.active_world)


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


from conjure.world import MIGRATED_SID


def _ls(client, path):
    r = client.post("/admin/tree", json={"path": path}).json()
    assert r["ok"] is True, r
    return {c["label"] for c in r["children"]}


def test_admin_tree_lists_one_level_at_a_time(srv, client):
    # `dir` walks the namespace a level at a time — the old recursive dump was unusable at any real size.
    _seed_worlds(srv, "alice/agents/builder", "w1")
    _seed_space(srv, "alice", "living-room")
    _seed_asset(srv, "bob-asset", "bob/agents/builder", label="thing")
    assert {"alice", "bob"} <= _ls(client, "/")
    assert _ls(client, "/alice") == {"agents", "spaces"}
    assert _ls(client, "/alice/agents") == {"builder"}
    assert _ls(client, "/alice/spaces") == {"living-room"}
    assert _ls(client, "/bob/agents/builder/assets") == {"bob-asset"}


def test_admin_tree_exposes_the_agent_and_session_levels(srv, client):
    # Worlds are stored PER SESSION (WorldRepository routes to the scope's active session), so the path
    # says so: hiding it merges two sessions' worlds into one indistinguishable list.
    _seed_worlds(srv, "alice/agents/builder", "w1", "w2")
    sid = srv.sessions.get_active("alice/agents/builder") or MIGRATED_SID
    assert _ls(client, "/alice/agents/builder") == {"sessions", "assets", "worlds"}
    assert _ls(client, "/alice/agents/builder/sessions") == {sid}
    assert _ls(client, f"/alice/agents/builder/sessions/{sid}") == {"worlds", "state"}
    assert _ls(client, f"/alice/agents/builder/sessions/{sid}/worlds") == {"w1", "w2"}


def test_worlds_shortcut_resolves_to_the_active_session(srv, client):
    _seed_worlds(srv, "alice/agents/builder", "w1", "w2")
    sid = srv.sessions.get_active("alice/agents/builder") or MIGRATED_SID
    r = client.post("/admin/tree", json={"path": "/alice/agents/builder/worlds"}).json()
    assert {c["label"] for c in r["children"]} == {"w1", "w2"}
    # it reports the REAL path it resolved to, so a remembered cwd can never go stale
    assert r["path"] == f"/alice/agents/builder/sessions/{sid}/worlds"


def _seed_session(srv, scope, sid, **meta):
    """A second session under `scope`, with worlds of its own — the setup the path verbs got wrong."""
    srv.sessions.save_meta(scope, sid, {"id": sid, "owner": scope.split("/")[0],
                                        "agent": scope.rsplit("/", 1)[-1], "public": True, **meta})


def test_renaming_a_session_by_path_renames_THAT_session_not_the_active_one(srv, client):
    # `rename session-1 <title>` retitled the ACTIVE session instead: the shell resolved the path, got the
    # right sid back in `fields`, then posted /session/rename with only `scope` — and the endpoint
    # defaults a missing `session` to the active one. The path was decoration.
    scope = "alice/agents/builder"
    _seed_worlds(srv, scope, "w1")
    live = srv.sessions.get_active(scope) or MIGRATED_SID
    _seed_session(srv, scope, "session-9", title="Nine")
    _seed_session(srv, scope, live, title="Live")   # worlds can exist before any session.json does

    r = client.post("/session/rename", json={"scope": scope, "session": "session-9",
                                             "title": "renamed"}).json()
    assert r["ok"] is True and r["session"] == "session-9"
    assert srv.sessions.load_meta(scope, "session-9")["title"] == "renamed"
    assert srv.sessions.load_meta(scope, live)["title"] == "Live"      # the active one is untouched

    # visibility took the same shortcut
    v = client.post("/session/visibility", json={"scope": scope, "session": "session-9",
                                                 "public": False}).json()
    assert v["ok"] is True
    assert srv.sessions.load_meta(scope, "session-9")["public"] is False
    assert srv.sessions.load_meta(scope, live).get("public", True) is True


def test_renaming_a_world_targets_the_named_session_not_the_live_one(srv, client):
    # Worlds are stored PER SESSION, so a bare scope means "the live session" — and with a same-named
    # world in both, renaming one in a non-live session silently renamed the live session's copy.
    scope = "alice/agents/builder"
    _seed_worlds(srv, scope, "shared")                                 # in the LIVE session
    _seed_session(srv, scope, "session-9", title="Nine")
    other = srv.sessions.worlds(scope, "session-9")
    from conjure.world import WorldStore
    other.save("shared", WorldStore({"id": "shared", "name": "shared", "rev": 0,
                                     "environment": {}, "entities": []}))
    live = srv.sessions.get_active(scope) or MIGRATED_SID

    r = client.post("/worlds/rename", json={"scope": scope, "session": "session-9",
                                            "name": "shared", "new_name": "moved"}).json()
    assert r["ok"] is True
    assert other.list() == ["moved"]                                   # the one we named
    assert srv.sessions.worlds(scope, live).list() == ["shared"]       # the live one is untouched

    bad = client.post("/worlds/rename", json={"scope": scope, "session": "session-404",
                                              "name": "shared", "new_name": "x"}).json()
    assert bad["ok"] is False and "session-404" in bad["error"]


def test_a_session_lists_under_its_TITLE_and_that_label_is_addressable(srv, client):
    # A row is led by what you address it as — a world row leads with its name, so a session row leads
    # with its title. The invariant that matters is the round trip: whatever `dir` prints must resolve.
    scope = "alice/agents/builder"
    _seed_worlds(srv, scope, "w1")
    sid = srv.sessions.get_active(scope) or MIGRATED_SID
    _seed_session(srv, scope, sid, title="Kitchen Table")

    rows = client.post("/admin/tree", json={"path": f"/{scope}/sessions"}).json()["children"]
    row = next(r for r in rows if r["label"] == "Kitchen Table")
    assert sid in row["detail"]                                 # the id stays visible, as the stable handle
    assert _ls(client, f"/{scope}/sessions/Kitchen Table") == {"worlds", "state"}

    # an unnamed session still leads with its id, and doesn't print it twice
    _seed_session(srv, scope, "session-9")
    bare = next(r for r in client.post("/admin/tree", json={"path": f"/{scope}/sessions"}).json()["children"]
                if r["label"] == "session-9")
    assert "session-9" not in bare.get("detail", "")


def test_session_titles_must_be_unique_like_world_and_space_names(srv, client):
    # Worlds and spaces have always refused a duplicate name. Sessions didn't, so two could both be
    # 'Home' — and `_resolve_sid` returns None on an ambiguous match, so the error read "no session
    # 'Home'": doesn't-exist when it meant matches-two.
    scope = "alice/agents/builder"
    _seed_worlds(srv, scope, "w1")
    _seed_session(srv, scope, "session-8", title="Home")
    _seed_session(srv, scope, "session-9", title="Away")

    dup = client.post("/session/rename", json={"scope": scope, "session": "session-9",
                                               "title": "home"}).json()      # collides loosely
    assert dup["ok"] is False and "session-8" in dup["error"]
    assert srv.sessions.load_meta(scope, "session-9")["title"] == "Away"     # unchanged

    # renaming a session to what it is already called is not a collision with itself
    same = client.post("/session/rename", json={"scope": scope, "session": "session-9",
                                                "title": "Away"}).json()
    assert same["ok"] is True

    # a title that would shadow ANOTHER session's id is refused too
    shadow = client.post("/session/rename", json={"scope": scope, "session": "session-9",
                                                  "title": "Session 8"}).json()
    assert shadow["ok"] is False and "session-8" in shadow["error"]


def test_session_titles_are_cleaned_on_write_like_world_and_space_names(srv, client):
    # A title carrying its own quotes can never be typed back — shlex eats them on the way in. Worlds and
    # spaces always survived this because they match through `slug`, which drops punctuation; sessions
    # matched on a key that kept it, so a quoted title was unreachable by any form of itself.
    scope = "alice/agents/builder"
    _seed_worlds(srv, scope, "w1")
    _seed_session(srv, scope, "session-9", title="Nine")

    r = client.post("/session/rename", json={"scope": scope, "session": "session-9",
                                             "title": '"Session 1" "alien"'}).json()
    assert r["ok"] is True and r["title"] == "Session 1 alien"
    assert srv.sessions.load_meta(scope, "session-9")["title"] == "Session 1 alien"
    # …and it round-trips: the cleaned title addresses its own session
    assert client.post("/session/rename", json={"scope": scope, "session": "Session 1 alien",
                                                "title": "Nine"}).json()["session"] == "session-9"
    # a title with nothing usable in it is refused rather than stored
    bad = client.post("/session/rename", json={"scope": scope, "session": "session-9",
                                               "title": '""'}).json()
    assert bad["ok"] is False
    assert srv.sessions.load_meta(scope, "session-9")["title"] == "Nine"   # unchanged


def test_a_session_titled_with_quotes_ALREADY_on_disk_is_still_reachable(srv, client):
    # Cleaning stops new ones; matching punctuation-insensitively (like slug, which worlds and spaces have
    # always used) is what reaches the ones already written — so they can be renamed without a migration.
    scope = "alice/agents/builder"
    _seed_worlds(srv, scope, "w1")
    _seed_session(srv, scope, "session-9", title='"Session 1" "alien"')   # written by the old parser
    r = client.post("/session/rename", json={"scope": scope, "session": "Session 1 alien",
                                             "title": "recovered"}).json()
    assert r["ok"] is True and r["session"] == "session-9"
    assert srv.sessions.load_meta(scope, "session-9")["title"] == "recovered"


def test_admin_path_addresses_a_session_by_its_title(srv, client):
    # `dir` prints the title in every session row, so a path has to accept it — otherwise the listing is
    # a liar and `rename "Session 1" Home` comes back "no session 'Session 1'". Same resolver the
    # /session/* endpoints use, so the shell and the API agree on what a reference means.
    scope = "alice/agents/builder"
    _seed_worlds(srv, scope, "w1")
    sid = srv.sessions.get_active(scope) or MIGRATED_SID
    meta = srv.sessions.load_meta(scope, sid) if srv.sessions.exists(scope, sid) else {"id": sid}
    meta["title"] = "Session 1"
    srv.sessions.save_meta(scope, sid, meta)

    assert _ls(client, f"/{scope}/sessions/{sid}") == {"worlds", "state"}          # by id, as before
    assert _ls(client, f"/{scope}/sessions/Session 1") == {"worlds", "state"}      # by exact title
    assert _ls(client, f"/{scope}/sessions/session 1") == {"worlds", "state"}      # loose: case
    assert _ls(client, f"/{scope}/sessions/Session-1") == {"worlds", "state"}      # loose: separators
    # …and it still resolves to the canonical ID path, so a remembered cwd can't go stale.
    r = client.post("/admin/tree", json={"path": f"/{scope}/sessions/Session 1/worlds"}).json()
    assert r["ok"] is True and r["path"] == f"/{scope}/sessions/{sid}/worlds"

    bad = client.post("/admin/tree", json={"path": f"/{scope}/sessions/Session 9"}).json()
    assert bad["ok"] is False and "Session 9" in bad["error"]


def test_admin_tree_unknown_user_errors(srv, client):
    r = client.post("/admin/tree", json={"path": "/nobody"}).json()
    assert r["ok"] is False and "nobody" in r["error"]


def test_admin_delete_single_world(srv, client):
    _seed_worlds(srv, "alice/agents/builder", "w1", "w2")
    r = client.post("/admin/delete", json={"path": "/alice/agents/builder/worlds/w1"}).json()
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
    r = client.post("/admin/delete", json={"path": "/alice/agents/builder/assets/drop"}).json()
    assert r["ok"] is True
    assert srv.library.get("drop") is None and srv.library.get("keep") is not None


def test_admin_delete_refuses_active_world(srv, client):
    _seed_worlds(srv, srv.DEFAULT_SCOPE, "default")     # the active world, now on disk
    r = client.post("/admin/delete", json={"path": "/daniel/agents/builder/worlds/default"}).json()
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


# --- a world inheriting a non-empty space's geometry is spacePresentation.active (director can see it) --------------
# Regression: creating/switching to a world that inherits an existing space's surfaces left spacePresentation.active
# unset (only ingest_room set it), so the CLI/voice director's query_room reported "no room" though the
# geometry was merged. _compose now defaults spacePresentation.active True when reals are merged (respecting an
# explicit False from an immersion mode like vr_unbounded).
def _space_with_walls():
    return {"surfaces": [
        {"id": "real_wall_art_1", "meta": {"real": True, "semantic": "wall art"},
         "transform": {"position": [0, 1.6, -1.3], "rotation": [0, 90, 0]},
         "components": {"surface": {"extent": [0.6, 0.9]}}},
        {"id": "real_wall_2", "meta": {"real": True, "semantic": "wall"},
         "transform": {"position": [0, 1.2, -1.3], "rotation": [0, 90, 0]}}], "boundary": None}


def test_compose_marks_room_active_when_inheriting_space_geometry():
    from conjure import server
    doc = server._compose({"environment": {"spacePresentation": {"edgesVisible": True}}, "entities": []}, _space_with_walls())
    assert doc["environment"]["spacePresentation"].get("active") is True
    assert sum(1 for e in doc["entities"] if (e.get("meta") or {}).get("real")) == 2


def test_compose_respects_explicit_room_active_false():
    from conjure import server  # a director immersion mode (vr_unbounded) hides the room — must not be flipped
    doc = server._compose({"environment": {"spacePresentation": {"active": False}}, "entities": []}, _space_with_walls())
    assert doc["environment"]["spacePresentation"].get("active") is False


def test_compose_leaves_room_inactive_without_reals():
    from conjure import server
    doc = server._compose({"environment": {"spacePresentation": {}}, "entities": []}, {"surfaces": [], "boundary": None})
    assert not doc["environment"]["spacePresentation"].get("active")


def test_move_reauthors_anchor_so_edit_persists(srv, client):
    # Regression: after §7c a model's pose is driven by meta.anchor (re-solved each capture). A move/rotate
    # that updates only transform gets reverted ("flash then snap back") — /patch must re-author the anchor.
    from conjure.plane_anchor import solve_anchor
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_floor_0", "semantic": "floor", "position": [0, 0, 0], "rotation": [-90, 0, 0], "extent": [4, 6]},
        {"id": "real_wall_1", "semantic": "wall", "position": [2, 1.2, 0], "rotation": [0, 90, 0], "extent": [3, 2.4]},
        {"id": "real_wall_2", "semantic": "wall", "position": [-2, 1.2, 0], "rotation": [0, -90, 0], "extent": [3, 2.4]},
        {"id": "real_wall_3", "semantic": "wall", "position": [0, 1.2, 3], "rotation": [0, 0, 0], "extent": [3, 2.4]},
        {"id": "real_wall_4", "semantic": "wall", "position": [0, 1.2, -3], "rotation": [0, 180, 0], "extent": [3, 2.4]}]})
    anchor = srv._content_anchor({"position": [0.3, 0, -0.8]}, "grounded")
    assert anchor
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "ent_m", "transform": {"position": [0.3, 0, -0.8]}, "components": {"gltf-model": "/a"},
        "meta": {"placement": "grounded", "anchor": anchor}}}]})
    client.post("/patch", json={"ops": [{"op": "update", "id": "ent_m", "set": {"transform.position": [1.2, 0, 0.5]}}]})
    e = next(x for x in _entities(client) if x["id"] == "ent_m")
    sol = solve_anchor(e["meta"]["anchor"], srv._seed_planes())      # anchor re-authored to the NEW pose
    assert sol["ok"] and abs(sol["position"][0] - 1.2) < 1e-6 and abs(sol["position"][2] - 0.5) < 1e-6


def test_move_leaves_unanchored_content_alone(srv, client):
    # Content WITHOUT an anchor (client places it from its F_ref pose) must NOT get a spurious anchor on move.
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "ent_free", "transform": {"position": [0, 1, -2]}, "components": {"gltf-model": "/a"}, "meta": {}}}]})
    client.post("/patch", json={"ops": [{"op": "update", "id": "ent_free", "set": {"transform.position": [1, 1, -2]}}]})
    e = next(x for x in _entities(client) if x["id"] == "ent_free")
    assert "anchor" not in (e.get("meta") or {})


def _anchored_room(client):
    """A seed room with a floor + 4 walls — enough for _content_anchor to author a plane-relative anchor."""
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_floor_0", "semantic": "floor", "position": [0, 0, 0], "rotation": [-90, 0, 0], "extent": [4, 6]},
        {"id": "real_wall_1", "semantic": "wall", "position": [2, 1.2, 0], "rotation": [0, 90, 0], "extent": [3, 2.4]},
        {"id": "real_wall_2", "semantic": "wall", "position": [-2, 1.2, 0], "rotation": [0, -90, 0], "extent": [3, 2.4]},
        {"id": "real_wall_3", "semantic": "wall", "position": [0, 1.2, 3], "rotation": [0, 0, 0], "extent": [3, 2.4]},
        {"id": "real_wall_4", "semantic": "wall", "position": [0, 1.2, -3], "rotation": [0, 180, 0], "extent": [3, 2.4]}]})


def test_manipulate_reauthors_anchor_so_a_grab_survives_recapture(srv, client):
    # Regression (found on-device): grabbing a grounded model moved it, but the new position didn't survive a
    # restart. An anchored model's pose is RE-SOLVED from meta.anchor every capture, so /manipulate must
    # re-author the anchor from the new pose — exactly as /patch does — or the next solve reverts the grab.
    from conjure.plane_anchor import solve_anchor
    _anchored_room(client)
    anchor = srv._content_anchor({"position": [0.3, 0, -0.8]}, "grounded")
    assert anchor
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "ent_dog", "transform": {"position": [0.3, 0, -0.8]}, "components": {"gltf-model": "/a"},
        "meta": {"placement": "grounded", "anchor": anchor}}}]})
    r = client.post("/manipulate", json={"id": "ent_dog", "position": [1.2, 0, 0.5]}).json()
    assert r["ok"] is True
    e = next(x for x in _entities(client) if x["id"] == "ent_dog")
    sol = solve_anchor(e["meta"]["anchor"], srv._seed_planes())      # anchor follows the grab, not the old pose
    assert sol["ok"] and abs(sol["position"][0] - 1.2) < 1e-6 and abs(sol["position"][2] - 0.5) < 1e-6


def test_manipulate_leaves_unanchored_content_without_an_anchor(srv, client):
    # A free (un-anchored) object grabbed in 6DOF must not acquire a spurious anchor — mirrors /patch.
    _anchored_room(client)
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "ent_float", "transform": {"position": [0, 1, -2]}, "components": {"gltf-model": "/a"}, "meta": {}}}]})
    client.post("/manipulate", json={"id": "ent_float", "position": [1, 1.4, -2]})
    e = next(x for x in _entities(client) if x["id"] == "ent_float")
    assert "anchor" not in (e.get("meta") or {})
    assert e["transform"]["position"] == [1, 1.4, -2]


# ---- /scope/activate: switch the live world to a scope's world (agent switch) --------------------

def test_scope_activate_is_noop_when_already_active(srv, client):
    r = client.post("/scope/activate", json={"scope": srv.DEFAULT_SCOPE}).json()
    assert r["ok"] and r.get("unchanged") and srv.active_scope == srv.DEFAULT_SCOPE


def test_scope_activate_opens_a_new_agent_with_its_declared_first_world(srv, client):
    """A first-ever switch to an agent runs that agent's DECLARED opening — `session.first_world`: its
    name and its (here generative) `on_create` chain — not a bare `default`. Before, the opening was
    reachable only by typing `session new`, so outdoor's moon-gate sky never appeared on the way people
    actually arrive."""
    outdoor = "daniel/agents/outdoor"
    r = client.post("/scope/activate", json={"scope": outdoor}).json()
    assert r["ok"] and r["world"] == "home"                   # outdoor declares first_world.name = "home"
    assert srv.active_scope == outdoor                        # the live world now belongs to outdoor
    assert srv.worlds.exists(outdoor, "home")                 # …created + persisted under its scope
    # its generative step ran: the constructor's skybox is baked into the world it built
    assert srv.store.doc["environment"].get("sky", {}).get("src")


def test_scope_activate_resumes_last_active_with_its_content(srv, client):
    outdoor = "daniel/agents/outdoor"
    client.post("/scope/activate", json={"scope": outdoor})       # outdoor's first world goes live
    client.post("/patch", json={"ops": [{"op": "add", "entity": {"id": "sky_marker", "components": {}}}]})
    client.post("/scope/activate", json={"scope": srv.DEFAULT_SCOPE})   # leave (saves outdoor/default)
    assert srv.active_scope == srv.DEFAULT_SCOPE
    client.post("/scope/activate", json={"scope": outdoor})       # return → resume, not recreate
    assert srv.active_scope == outdoor
    assert "sky_marker" in {e["id"] for e in _entities(client)}   # its content came back


def test_state_reports_the_live_tuple(srv, client):
    # The canonical "what's live" seam: identifiers for the single shared session (docs/specs/agents.md §9.1).
    s = client.get("/state").json()
    assert s["ok"]
    assert s["scope"] == srv.DEFAULT_SCOPE and s["agent"] == "builder" and s["owner"] == "daniel"
    assert s["world"] == "default"
    assert s["space"] == "daniel/home"                       # fixture's active space, fully-qualified


def test_state_reflects_a_scope_activation(srv, client):
    # Switching agent/scope moves the live tuple; /state derives agent from the scope and reports the
    # space the new world composes against (VOID for outdoor — it declares `world.outdoor`).
    client.post("/scope/activate", json={"scope": "daniel/agents/outdoor"})
    s = client.get("/state").json()
    assert s["scope"] == "daniel/agents/outdoor" and s["agent"] == "outdoor" and s["world"] == "home"
    expected_space = srv.VOID if srv._no_space() else f"{srv.active_space_owner}/{srv.active_space}"
    assert s["space"] == expected_space


def test_snapshot_msg_carries_live_state_beside_the_world_doc(srv):
    # The /ws snapshot stays backward-compatible (world doc + top-level owner for the renderer) and adds
    # the live-state identifiers under `state`, so headset + agent server reconcile from one broadcast.
    msg = srv._snapshot_msg()
    assert msg["type"] == "snapshot"
    assert msg["world"] is srv.store.doc                     # full doc, top-level (renderer)
    assert msg["owner"] == "daniel"                          # top-level owner (desktop-guest spawn hint)
    assert msg["state"] == srv._live_state()                 # additive identifiers
    assert msg["state"]["world"] == "default"                # here `world` is the NAME, not the doc


def test_agent_last_defaults_to_builder(srv, client):
    assert client.get("/agent/last", params={"user": "someone_new"}).json()["agent"] == "builder"


def test_scope_activate_moves_the_global_session_pointer_and_live_agent(srv, client):
    # One shared session (docs/specs/agents.md §9): the live agent is derived from the global session
    # pointer (scope → agent), not a per-user _last_agent record.
    client.post("/scope/activate", json={"scope": "daniel/agents/outdoor"})
    assert srv._read_session_ptr()[0] == "daniel/agents/outdoor"         # global pointer moved
    assert srv.agent_of(srv.active_scope) == "outdoor"
    assert client.get("/agent/last").json()["agent"] == "outdoor"        # derived; user param vestigial
    client.post("/scope/activate", json={"scope": srv.DEFAULT_SCOPE})    # switching back
    assert srv._read_session_ptr()[0] == srv.DEFAULT_SCOPE
    assert client.get("/agent/last").json()["agent"] == "builder"


def test_session_new_list_switch_rename_delete(srv, client):
    scope = srv.DEFAULT_SCOPE
    srv._ensure_session(scope)                                           # materialize session-1 (boot would)
    # new session → created, switched to, global pointer moved
    r = client.post("/session/new", json={"scope": scope, "title": "Playground"}).json()
    assert r["ok"] and r["session"] == "session-2" and r["title"] == "Playground"
    assert srv.active_sid == "session-2" and srv._read_session_ptr() == (scope, "session-2")
    # list shows both, session-2 live
    lst = client.get("/sessions", params={"scope": scope}).json()
    by_id = {s["id"]: s for s in lst["sessions"]}
    assert lst["active"] == "session-2" and {"session-1", "session-2"} <= set(by_id)
    assert by_id["session-2"]["active"] is True and by_id["session-2"]["title"] == "Playground"
    # switch back by TITLE (session-1's default title is "Session 1")
    r = client.post("/session/switch", json={"scope": scope, "session": "Session 1"}).json()
    assert r["ok"] and r["session"] == "session-1" and srv.active_sid == "session-1"
    # rename the active session (retitle only — id stable)
    r = client.post("/session/rename", json={"scope": scope, "title": "Home Base"}).json()
    assert r["ok"] and r["session"] == "session-1"
    titles = {s["id"]: s["title"] for s in client.get("/sessions", params={"scope": scope}).json()["sessions"]}
    assert titles["session-1"] == "Home Base"
    # can't delete the active session; can delete a non-active one
    assert client.post("/session/delete", json={"scope": scope, "session": "session-1"}).json()["ok"] is False
    assert client.post("/session/delete", json={"scope": scope, "session": "Playground"}).json()["ok"] is True
    assert "session-2" not in {s["id"] for s in
                               client.get("/sessions", params={"scope": scope}).json()["sessions"]}


def test_switching_sessions_loads_the_target_sessions_world(srv, client):
    # Regression (the set_live-ordering bug): a session-switch must load the incoming world from the TARGET
    # session, not the outgoing one — else a session whose active world has a different name 500s with
    # FileNotFoundError. (Earlier tests missed it: every test session had a 'home' world.)
    scope = srv.DEFAULT_SCOPE
    srv._ensure_session(scope)                                   # session-1
    wd = srv.sessions.worlds(scope, "session-1")
    wd.save("alpha", srv.WorldStore({"id": "a", "name": "alpha", "rev": 0,
                                     "environment": {"space": "<void>"}, "entities": []}))
    wd.set_active("alpha")
    client.post("/session/new", json={"scope": scope})           # session-2 ('home'), now active
    r = client.post("/session/switch", json={"scope": scope, "session": "session-1"}).json()
    assert r["ok"] and r["world"] == "alpha"                     # loaded from session-1, not session-2
    assert srv.active_sid == "session-1" and _wname(srv) == "alpha"


def test_admin_delete_is_ownership_gated(srv, client):
    # §6e: a caller may only delete their OWN namespace — not another user's worlds/spaces/assets.
    import conjure.server as S
    S.worlds.save("bob/agents/builder", "keep", S.WorldStore(
        {"id": "k", "name": "keep", "rev": 0, "environment": {"space": "<void>"}, "entities": []}))
    # guest (a different user) tries to delete bob's namespace → refused, nothing deleted
    r = client.post("/admin/delete", json={"path": "/bob/agents/builder/worlds/keep"},
                    headers={"X-Conjure-User": "guest"}).json()
    assert r["ok"] is False and "your own" in r["error"]
    assert S.worlds.exists("bob/agents/builder", "keep")
    # bob deleting his own is allowed
    r = client.post("/admin/delete", json={"path": "/bob/agents/builder/worlds/keep"},
                    headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] is True and not S.worlds.exists("bob/agents/builder", "keep")


def test_session_visibility_toggles_and_shows_in_the_listing(srv, client):
    scope = srv.DEFAULT_SCOPE
    srv._ensure_session(scope)
    # new sessions are public by default; listing reports it
    by_id = {s["id"]: s for s in client.get("/sessions", params={"scope": scope}).json()["sessions"]}
    assert by_id["session-1"]["public"] is True
    # make the active session private
    r = client.post("/session/visibility", json={"scope": scope, "public": False}).json()
    assert r["ok"] and r["session"] == "session-1" and r["public"] is False
    assert srv.sessions.load_meta(scope, "session-1")["public"] is False   # recorded on the session meta
    by_id = {s["id"]: s for s in client.get("/sessions", params={"scope": scope}).json()["sessions"]}
    assert by_id["session-1"]["public"] is False
    # and back to public
    assert client.post("/session/visibility", json={"scope": scope, "public": True}).json()["public"] is True


async def test_visibility_change_broadcasts_state_so_the_agent_server_can_regate(srv, client):
    # The bump-out runs on the agent server when it receives fresh state; the visibility toggle must
    # BROADCAST it (the bug: it didn't, so a guest kept conversing in a "private" session).
    import conjure.server as S
    srv._ensure_session(srv.DEFAULT_SCOPE)
    S.clients.clear()
    ws = _FakeWS()
    S.clients[ws] = "daniel"                                      # a connected /ws client (the agent follower)
    client.post("/session/visibility", json={"scope": srv.DEFAULT_SCOPE, "public": False})
    assert any(m.get("type") == "snapshot" and m.get("state", {}).get("public") is False for m in ws.sent)
    S.clients.clear()


def test_first_world_spec_and_constructor_command_forms(srv):
    import conjure.server as S
    assert S._first_world_spec("daniel/agents/builder") == ("home", [])   # no session block → default
    # a constructor step names its command as `cmd` OR `tool` — both resolve
    ops = S._run_world_commands([{"cmd": "show_edges", "args": {"on": True}},
                                 {"tool": "set_sky_color", "args": {"color": "#123456"}}])
    assert {"op": "env", "set": {"spacePresentation.edgesVisible": True}} in ops
    assert {"op": "env", "set": {"sky": {"color": "#123456"}}} in ops


def test_new_world_store_runs_world_then_first_world_chain(srv):
    # world.on_create (builder: edges ON) runs first, then the first-world-only chain (edges OFF) — the
    # later step wins, proving the chain order (docs/specs/agents.md §7.5).
    import conjure.server as S
    s = S._new_world_store("daniel/agents/builder",
                           extra_on_create=[{"tool": "show_edges", "args": {"on": False}}])
    assert s.doc["environment"]["spacePresentation"]["edgesVisible"] is False


def test_session_new_builds_the_first_world_from_the_constructor(srv, client):
    scope = srv.DEFAULT_SCOPE
    srv._ensure_session(scope)
    client.post("/session/new", json={"scope": scope})
    assert _wname(srv) == "home"                                          # default first-world name
    assert srv.worlds.list(scope) == ["home"]                            # built in the new session
    assert srv.store.doc["environment"]["spacePresentation"]["edgesVisible"] is True   # builder's world.on_create ran


async def test_build_generative_ops_binds_and_references_step_output(srv):
    import conjure.server as S
    ops, err = await S._build_generative_ops([
        {"tool": "generate_skybox_image", "args": {"description": "a calm dawn meadow"}, "as": "sky"},
        {"tool": "set_skybox", "args": {"image_id": "${sky.image_id}"}}])   # explicit reference
    assert err is None and len(ops) == 1
    assert ops[0]["op"] == "env" and "src" in ops[0]["set"]["sky"]          # a skybox env patch


async def test_build_generative_ops_fail_hard_on_missing_ref_and_missing_image(srv):
    import conjure.server as S
    ops, err = await S._build_generative_ops([{"tool": "set_skybox", "args": {"image_id": "${sky.image_id}"}}])
    assert ops == [] and "unknown reference" in err                        # no such binding → fail-hard
    ops2, err2 = await S._build_generative_ops([{"tool": "set_skybox", "args": {}}])
    assert ops2 == [] and "image_id required" in err2                      # no implicit "last" → explicit error


async def test_session_new_bakes_a_generative_first_world(srv, client, monkeypatch):
    import conjure.server as S
    S._ensure_session(S.DEFAULT_SCOPE)
    monkeypatch.setattr(S, "_first_world_spec", lambda scope: ("home", [
        {"tool": "generate_skybox_image", "args": {"description": "a meadow"}, "as": "sky"},
        {"tool": "set_skybox", "args": {"image_id": "${sky.image_id}"}}]))
    assert client.post("/session/new", json={"scope": S.DEFAULT_SCOPE}).json()["ok"]
    assert "src" in S.store.doc["environment"]["sky"]                     # skybox baked into the first world


async def test_session_new_aborts_on_constructor_failure(srv, client, monkeypatch):
    import conjure.server as S
    S._ensure_session(S.DEFAULT_SCOPE)
    before = set(S.sessions.list(S.DEFAULT_SCOPE))
    monkeypatch.setattr(S, "_first_world_spec", lambda scope: ("home", [
        {"tool": "set_skybox", "args": {"image_id": "${sky.image_id}"}}]))   # unresolved ref → fail-hard
    r = client.post("/session/new", json={"scope": S.DEFAULT_SCOPE}).json()
    assert r["ok"] is False and "constructor failed" in r["error"]
    assert set(S.sessions.list(S.DEFAULT_SCOPE)) == before               # nothing created (clean abort)


def test_boot_world_restores_the_global_session_pointer(srv):
    # After a restart, boot resumes exactly the session the global pointer records — the scope's active
    # session, and that session's active world — so the viewer comes back where everyone was, agent
    # derived from the scope (docs/specs/agents.md §7.1).
    import conjure.server as S
    from conjure.world import WorldStore
    outdoor = S.scope_for(S.DEFAULT_USER, "outdoor")
    S.worlds.save(outdoor, "beach", WorldStore(                       # routes to outdoor's session-1/worlds
        {"id": "b", "name": "beach", "rev": 0, "environment": {"space": "<void>"}, "entities": []}))
    S.worlds.set_active(outdoor, "beach")                             # active world within that session
    S._write_session_ptr(outdoor, S.MIGRATED_SID)                    # the live SESSION
    scope, wid, _ = S._boot_world()
    assert scope == outdoor and S.worlds.name_of(scope, wid) == "beach"


def test_boot_world_writes_the_pointer_for_future_boots(srv):
    # A fresh cache (no pointer) boots to the builder default AND writes the pointer + session so the next
    # boot resumes it. (The pre-session on-disk cache is handled earlier by migrate_cache_to_users.)
    import conjure.server as S
    assert S._read_session_ptr() is None                             # fresh
    scope, wid, _ = S._boot_world()
    assert (scope, S.worlds.name_of(scope, wid)) == (S.DEFAULT_SCOPE, "default")
    assert S._read_session_ptr() == (S.DEFAULT_SCOPE, S.MIGRATED_SID)  # pointer written for next boot
    assert S.sessions.exists(S.DEFAULT_SCOPE, S.MIGRATED_SID)          # and the session materialized


def test_boot_world_defaults_to_builder_without_a_pointer(srv):
    import conjure.server as S
    scope, wid, _ = S._boot_world()
    assert scope == S.DEFAULT_SCOPE and S.worlds.name_of(scope, wid) == "default"   # builder's default


# ── dynamic modules: discovered registry, scoping, serving, injection ────────────────────────────
# (docs/specs/dynamics.md §§3-4, 9 — first-class, extensible; scoped to the active agent.)
def test_conjure_module_places_a_scoped_module(srv, client):
    # builder is the active agent (DEFAULT_SCOPE) and lists 'fireflies' → allowed; the entity carries the
    # module's component, so it's config-in-snapshot/shared/persisted on the existing path.
    r = client.post("/module", json={"module": "fireflies", "position": [0, 1, -2]})
    assert r.json()["ok"] is True, r.json()
    ent = next(e for e in _entities(client) if (e.get("meta") or {}).get("module") == "fireflies")
    assert "fireflies" in ent["components"] and ent["meta"]["dynamic"] is True


def test_conjure_unknown_module_is_rejected(srv, client):
    r = client.post("/module", json={"module": "nope"})
    body = r.json()
    assert body["ok"] is False and "unknown module" in body["error"]


def test_conjure_out_of_scope_module_is_rejected(srv, client, monkeypatch):
    # 'fireflies' IS discovered on the server, but the OUTDOOR agent doesn't list it → hard-refused
    # (the plan's server-side scoping: soft catalog + hard endpoint). Fail closed.
    import conjure.server as S
    monkeypatch.setattr(S, "active_scope", S.scope_for(S.DEFAULT_USER, "outdoor"))
    r = client.post("/module", json={"module": "fireflies"})
    body = r.json()
    assert body["ok"] is False and "not available to the active agent" in body["error"]


def test_dynamics_available_catalog_reflects_the_active_agent(srv, client):
    body = client.get("/dynamics/available").json()
    assert body["ok"] is True
    assert set(body["modules"]) == {"fireflies", "water", "grab"}
    assert "fireflies —" in body["catalog"] and "water —" in body["catalog"] and "grab —" in body["catalog"]


def test_dynamics_file_is_served_with_version_bust(srv, client):
    r = client.get("/dynamics/fireflies/fireflies.js")
    assert r.status_code == 200
    assert "registerComponent" in r.text
    assert r.headers["content-type"].startswith("application/javascript")


def test_dynamics_file_traversal_is_blocked(srv, client):
    # basename-only path components: a traversal attempt can't escape the module folder.
    r = client.get("/dynamics/fireflies/module.json")   # a real sibling file is fine (basename kept)
    assert r.status_code == 200
    r2 = client.get("/dynamics/ghostmod/x.js")
    assert r2.status_code == 404


def test_index_injects_every_discovered_module_script(srv, client):
    html = client.get("/").text
    assert 'src="/dynamics/fireflies/fireflies.js?v=' in html
    assert 'src="/dynamics/water/water.js?v=' in html
    assert 'src="/dynamics/grab/grab.js?v=' in html          # the tier-C manipulation module
    # the deleted flat client files are no longer referenced
    assert "/static/dynamic-modules.js" not in html and "/static/water.js" not in html


def test_a_page_carries_every_module_script_whatever_agent_is_live(srv, client, monkeypatch):
    """A page's scripts are frozen at load; the live agent is not. `outdoor` declares no `dynamics`, so
    scoping the <script> tags to it served a headset ZERO module code — and then `/space/select` could
    match the room and join a builder world full of `grab`/`water` entities. An unregistered A-Frame
    component is silent (a plain DOM attribute), so the modules rendered nothing, logged nothing, and
    only a manual page reload fixed it. Loading is now agent-independent; only CONJURING is scoped."""
    import conjure.server as S
    monkeypatch.setattr(S, "active_scope", S.scope_for(S.DEFAULT_USER, "outdoor"))
    assert S._active_agent_dynamics() == []                  # outdoor may conjure nothing…
    html = client.get("/").text
    for src in ('src="/dynamics/fireflies/fireflies.js?v=',  # …but the components are still REGISTERED,
                'src="/dynamics/water/water.js?v=',          # so a world it's handed mid-session renders
                'src="/dynamics/grab/grab.js?v='):
        assert src in html
    # …and the hard gate is untouched: registered client-side ≠ conjurable by this agent.
    assert client.post("/module", json={"module": "grab"}).json()["ok"] is False


# ── water picture (image module) aspect-ratio handling (mirror place_image) ──────────────────────
def _wall_art(client, extent=(0.5, 0.4)):
    """A wall-art surface at a known upright orientation + size (a 0.5×0.4 frame by default)."""
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [0.7, 1.72, -1.04],
         "rotation": [0.0, -41.0, 0.0], "extent": list(extent)}]})


def _water(client):
    return next(e for e in _entities(client) if "water" in e.get("components", {}))["components"]["water"]


def test_water_free_standing_respects_image_aspect(srv, client):
    # A free-standing Water Picture sizes its plane to the picture's aspect (like place_image), NOT the
    # component's fixed 1.2×0.9 default. A 4×4 square image ⇒ a 1.2×1.2 square plane (longest side 1.2).
    image_id = _procure(client)
    r = client.post("/module", json={"module": "water", "config": {"image": image_id}}).json()
    assert r["ok"] is True
    w = _water(client)
    assert w["src"] == f"/assets/{image_id}"
    assert w["width"] == 1.2 and w["height"] == 1.2       # square, not the 0.9 default height


def test_water_on_surface_fits_inside_keeping_aspect(srv, client):
    # On a 0.5×0.4 frame a square image fits INSIDE at 0.4×0.4 — not stretched to fill (0.5×0.4).
    _wall_art(client)
    image_id = _procure(client)
    r = client.post("/module", json={"module": "water", "on_surface": "wall art 18",
                                     "config": {"image": image_id}}).json()
    assert r["ok"] is True
    w = _water(client)
    assert w["width"] == pytest.approx(0.4) and w["height"] == pytest.approx(0.4)


def test_water_on_surface_stretch_fills_the_surface(srv, client):
    # stretch=True is the opt-in fill: the plane matches the whole surface frame (aspect not preserved).
    _wall_art(client)
    image_id = _procure(client)
    r = client.post("/module", json={"module": "water", "on_surface": "wall art 18",
                                     "stretch": True, "config": {"image": image_id}}).json()
    assert r["ok"] is True
    w = _water(client)
    assert w["width"] == pytest.approx(0.5) and w["height"] == pytest.approx(0.4)


def test_water_explicit_size_is_honored(srv, client):
    # An explicit width+height in config wins as-is (an intentional exact size / stretch).
    image_id = _procure(client)
    r = client.post("/module", json={"module": "water",
                                     "config": {"image": image_id, "width": 2.0, "height": 1.0}}).json()
    assert r["ok"] is True
    w = _water(client)
    assert w["width"] == 2.0 and w["height"] == 1.0


def test_place_image_on_surface_stretch_fills_the_surface(srv, client):
    # The same opt-in fill for regular images: stretch=True covers the entire surface frame.
    _wall_art(client)
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 18",
                                          "stretch": True}).json()
    assert r["ok"] is True
    g = next(e for e in _entities(client) if e["id"] == r["id"])["components"]["geometry"]
    assert g["width"] == pytest.approx(0.5) and g["height"] == pytest.approx(0.4)


def test_water_on_surface_re_fits_size_when_the_surface_resizes(srv, client):
    # Consistency with regular images: a Water Picture must re-fit its plane to the surface's CURRENT
    # frame on re-capture — not just ride its pose. The frame grows past the resize threshold (0.5 m),
    # so the square image's fitted plane grows 0.4×0.4 → 1.0×1.0 (fit inside 1.2×1.0, aspect kept).
    _wall_art(client, extent=(0.5, 0.4))
    image_id = _procure(client)
    client.post("/module", json={"module": "water", "on_surface": "wall art 18",
                                 "config": {"image": image_id}})
    assert _water(client)["width"] == pytest.approx(0.4) and _water(client)["height"] == pytest.approx(0.4)
    # re-capture: same wall art, a genuinely-resized frame → the water plane re-fits inside it.
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_art_18", "semantic": "wall art", "position": [0.7, 1.72, -1.04],
         "rotation": [0.0, -41.0, 0.0], "extent": [1.2, 1.0]}]})
    assert _water(client)["width"] == pytest.approx(1.0) and _water(client)["height"] == pytest.approx(1.0)


def test_on_surface_image_snaps_square_facing_the_viewer_on_a_horizontal_surface(srv, client):
    # On a table (horizontal) gravity gives no in-plane up. The image must face the placing viewer (TOP
    # edge away, BOTTOM nearest) AND snap SQUARE to the surface rectangle — even from an OBLIQUE angle it
    # picks the nearest rectangle axis, not the exact askew viewer direction.
    srv.gaze["daniel"] = {"origin": [0.3, 1.6, 0.0]}          # viewer offset in +x (oblique to the table)
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_table_2", "semantic": "table", "position": [0.0, 0.7, -1.0],
         "rotation": [90.0, 0.0, 0.0], "extent": [1.0, 0.6]}]})
    r = client.post("/place_image", json={"image_id": _procure(client), "on_surface": "table 2"}).json()
    assert r["ok"] is True
    from conjure.server import _plane_basis
    img = next(e for e in _entities(client) if e["id"] == r["id"])
    _, _, up = _plane_basis(img["transform"]["rotation"])     # the image's own +Y (its top) in world
    # away-from-viewer is dominated by -z, so it snaps to the pure -z rectangle axis (NOT the askew x-tilt)
    assert up[2] < -0.999 and abs(up[0]) < 1e-6 and abs(up[1]) < 1e-6
    assert img["meta"]["content_up"] in ([0.0, -1.0], [0, -1])  # snapped surface-local axis, recorded


def test_on_surface_horizontal_facing_survives_recapture(srv, client):
    # The viewer-derived facing is stored surface-local, so re-capturing the table keeps the same facing
    # (doesn't revert to an arbitrary rectangle axis) — consistent with how pose/size ride a recapture.
    srv.gaze["daniel"] = {"origin": [0.0, 1.6, 0.0]}
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_table_2", "semantic": "table", "position": [0.0, 0.7, -1.0],
         "rotation": [90.0, 0.0, 0.0], "extent": [1.0, 0.6]}]})
    r = client.post("/place_image", json={"image_id": _procure(client), "on_surface": "table 2"}).json()
    from conjure.server import _plane_basis
    up0 = _plane_basis(next(e for e in _entities(client) if e["id"] == r["id"])["transform"]["rotation"])[2]
    # re-capture the same table, shifted past the move threshold (no gaze needed — facing comes from meta).
    srv.gaze.clear()
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_table_2", "semantic": "table", "position": [0.9, 0.7, -1.0],
         "rotation": [90.0, 0.0, 0.0], "extent": [1.0, 0.6]}]})
    up1 = _plane_basis(next(e for e in _entities(client) if e["id"] == r["id"])["transform"]["rotation"])[2]
    assert all(abs(up1[i] - up0[i]) < 0.02 for i in range(3))   # same facing, not the rectangle fallback


# ── tier-C manipulation: /manipulate commit (grab) ───────────────────────────────────────────────
# (docs/specs/dynamics.md §8 — client drags locally, commits the resting transform.)
def _place_box(client, eid="box1"):
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": eid, "transform": {"position": [0, 1, -2]},
        "components": {"geometry": {"primitive": "box"}}, "meta": {"generated": True}}}]})
    return eid


def test_manipulate_applies_transform_and_broadcasts(srv, client):
    eid = _place_box(client)
    r = client.post("/manipulate", json={"id": eid, "position": [1, 1.5, -3],
                                         "rotation": [0, 45, 0], "scale": [2, 2, 2]}).json()
    assert r["ok"] is True
    e = next(x for x in _entities(client) if x["id"] == eid)
    assert e["transform"]["position"] == [1, 1.5, -3]
    assert e["transform"]["rotation"] == [0, 45, 0]
    assert e["transform"]["scale"] == [2, 2, 2]


def test_manipulate_partial_update_leaves_other_fields(srv, client):
    eid = _place_box(client)
    client.post("/manipulate", json={"id": eid, "scale": [3, 3, 3]})
    e = next(x for x in _entities(client) if x["id"] == eid)
    assert e["transform"]["scale"] == [3, 3, 3] and e["transform"]["position"] == [0, 1, -2]  # unchanged


def test_manipulate_recomputes_surface_offset_for_on_surface_content(srv, client):
    # A picture hung on a wall carries meta.on_surface; moving it must refresh surface_offset so it still
    # rides a room recapture (same contract as place_image).
    _wall_art(client)
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 18"}).json()
    before = next(x for x in _entities(client) if x["id"] == r["id"])["meta"]["surface_offset"]
    # move it along the wall
    client.post("/manipulate", json={"id": r["id"], "position": [0.8, 1.8, -1.1]})
    e = next(x for x in _entities(client) if x["id"] == r["id"])
    assert e["transform"]["position"] == [0.8, 1.8, -1.1]
    assert e["meta"]["surface_offset"] != before        # offset refreshed from the new resting pose


def test_manipulate_refuses_real_surfaces(srv, client):
    client.post("/space/capture", json={"client_id": "h1", "surfaces": [
        {"id": "real_wall_9", "semantic": "wall", "position": [0, 1.5, -2],
         "rotation": [0, 0, 0], "extent": [2, 2.5]}]})
    r = client.post("/manipulate", json={"id": "real_wall_9", "position": [1, 1, -1]}).json()
    assert r["ok"] is False and "real room surfaces" in r["error"]


def test_manipulate_unknown_entity_is_rejected(srv, client):
    r = client.post("/manipulate", json={"id": "ghost", "position": [0, 0, 0]}).json()
    assert r["ok"] is False and "no entity" in r["error"]


def test_manipulate_is_owner_gated(srv, client):
    eid = _place_box(client)
    # a guest (not the active world's owner 'daniel') is refused by the owner-only middleware
    r = client.post("/manipulate", json={"id": eid, "position": [9, 9, 9]},
                    headers={"X-Conjure-User": "someone-else"})
    assert r.status_code == 403
    e = next(x for x in _entities(client) if x["id"] == eid)
    assert e["transform"]["position"] == [0, 1, -2]     # unchanged


def test_manipulate_stores_a_client_authored_anchor_verbatim(srv, client):
    # The client authors the anchor against ITS OWN walls and sends it; the server stores it as-is. Letting
    # the server re-author from the committed position instead adds author/solve hops between plane sets
    # that aren't rigidly related, and the residual shows up as content settling off the drop point.
    _anchored_room(client)
    anchor = srv._content_anchor({"position": [0.3, 0, -0.8]}, "grounded")
    assert anchor
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "ent_dog2", "transform": {"position": [0.3, 0, -0.8]}, "components": {"gltf-model": "/a"},
        "meta": {"placement": "grounded", "anchor": anchor}}}]})
    mine = {"mode": "grounded", "floor": {"id": "real_floor_0", "offset": 0.25},
            "walls": [{"id": "real_wall_1", "offset": -1.0, "rel": [0, 0, 0, 1]},
                      {"id": "real_wall_3", "offset": -2.0, "rel": [0, 0, 0, 1]}]}
    r = client.post("/manipulate", json={"id": "ent_dog2", "position": [1.2, 0, 0.5], "anchor": mine}).json()
    assert r["ok"] is True
    e = next(x for x in _entities(client) if x["id"] == "ent_dog2")
    assert e["meta"]["anchor"] == mine          # stored verbatim, NOT re-authored from the position


def test_manipulate_still_reauthors_when_no_anchor_is_sent(srv, client):
    # No client anchor (no room basis on that client) ⇒ the server re-authors, as before.
    from conjure.plane_anchor import solve_anchor
    _anchored_room(client)
    anchor = srv._content_anchor({"position": [0.3, 0, -0.8]}, "grounded")
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "ent_dog3", "transform": {"position": [0.3, 0, -0.8]}, "components": {"gltf-model": "/a"},
        "meta": {"placement": "grounded", "anchor": anchor}}}]})
    client.post("/manipulate", json={"id": "ent_dog3", "position": [1.2, 0, 0.5]})
    e = next(x for x in _entities(client) if x["id"] == "ent_dog3")
    sol = solve_anchor(e["meta"]["anchor"], srv._seed_planes())
    assert sol["ok"] and abs(sol["position"][0] - 1.2) < 1e-6


def test_manipulate_stores_a_client_surface_offset_verbatim(srv, client):
    # Surface-attached content is positioned host-relative; the client computes the offset against its own
    # rendered host and the server keeps it as-is (same reasoning as the client-authored anchor).
    _wall_art(client)
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 18"}).json()
    mine = {"p": [0.11, -0.02, -0.02], "q": [0.0, 1.0, 0.0, 0.0]}
    client.post("/manipulate", json={"id": r["id"], "position": [0.8, 1.8, -1.1], "surface_offset": mine})
    e = next(x for x in _entities(client) if x["id"] == r["id"])
    assert e["meta"]["surface_offset"] == mine


def test_manipulate_derives_surface_offset_when_client_sends_none(srv, client):
    # No client offset (host not rendered there) ⇒ the server still derives one from the committed pose.
    _wall_art(client)
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 18"}).json()
    before = next(x for x in _entities(client) if x["id"] == r["id"])["meta"]["surface_offset"]
    client.post("/manipulate", json={"id": r["id"], "position": [0.85, 1.85, -1.12]})
    e = next(x for x in _entities(client) if x["id"] == r["id"])
    assert e["meta"]["surface_offset"] != before


def test_moved_surface_image_keeps_its_spot_across_a_recapture(srv, client):
    # Regression (found on-device): a wall image moved WITHIN its frame snapped back to the middle on the
    # next re-anchor, while its size survived. _on_surface_set re-derived the pose from scratch (surface
    # centre + standoff) and overwrote the offset; it must RIDE the stored offset instead.
    import math
    _wall_art(client, extent=(1.2, 1.0))
    image_id = _procure(client)
    r = client.post("/place_image", json={"image_id": image_id, "on_surface": "wall art 18"}).json()
    placed = next(x for x in _entities(client) if x["id"] == r["id"])
    surf = next(x for x in _entities(client) if x["id"] == "real_wall_art_18")
    spos, srot = surf["transform"]["position"], surf["transform"]["rotation"]
    # Move it off-centre within the frame, exactly as a grab does: a pose plus the matching host-local
    # offset (the client computes the pair from the same drop, so they agree).
    rot = placed["transform"]["rotation"]
    moved = [placed["transform"]["position"][0] + 0.25, placed["transform"]["position"][1] - 0.15,
             placed["transform"]["position"][2]]
    off = srv._surface_offset(spos, srot, moved, rot)
    client.post("/manipulate", json={"id": r["id"], "position": moved, "rotation": rot,
                                     "surface_offset": off})
    # world LOAD re-pins every on-surface image (what happens on a restart): it must stay where it was put
    srv._reanchor_surface_images(srv.store.doc)
    after = next(x for x in srv.store.doc["entities"] if x["id"] == r["id"])
    assert after["meta"]["surface_offset"] == off          # offset preserved, not rewritten
    assert math.dist(after["transform"]["position"], moved) < 1e-3


def test_surface_image_without_an_offset_still_centres(srv, client):
    # Legacy/first placement (no stored offset) keeps the old behaviour: centred on the surface.
    _wall_art(client)
    srv.store.doc["entities"].append({
        "id": "legacy_img", "transform": {"position": [0, 0, 0], "rotation": [0, 0, 0]},
        "components": {"geometry": {"width": 0.2, "height": 0.2}},
        "meta": {"on_surface": "real_wall_art_18"}})
    srv._reanchor_surface_images(srv.store.doc)
    e = next(x for x in srv.store.doc["entities"] if x["id"] == "legacy_img")
    assert e["meta"]["surface_offset"]                      # derived for the first time
    assert e["transform"]["position"] != [0, 0, 0]          # placed onto the surface


def test_manipulate_stores_a_client_anchor_on_previously_unanchored_content(srv, client):
    # Free-standing content isn't really un-anchored: the client authors an anchor on the fly from its
    # F_ref pose every capture. Keeping the exact one the drop produced replaces that re-derived
    # approximation, so free content is as accurate as models. (The server still never INVENTS one — see
    # test_manipulate_leaves_unanchored_content_without_an_anchor.)
    _anchored_room(client)
    client.post("/patch", json={"ops": [{"op": "add", "entity": {
        "id": "ent_free_img", "transform": {"position": [0, 1.4, -2]},
        "components": {"geometry": {"primitive": "plane", "width": 0.5, "height": 0.4}}, "meta": {}}}]})
    mine = {"mode": "free", "floor": {"id": "real_floor_0", "offset": 1.4},
            "walls": [{"id": "real_wall_1", "offset": -1.0, "rel": [0, 0, 0, 1]},
                      {"id": "real_wall_3", "offset": -2.0, "rel": [0, 0, 0, 1]}]}
    client.post("/manipulate", json={"id": "ent_free_img", "position": [0.4, 1.5, -1.6], "anchor": mine})
    e = next(x for x in _entities(client) if x["id"] == "ent_free_img")
    assert e["meta"]["anchor"] == mine


def test_index_injects_pointer_bindings_and_the_shared_reader(srv, client):
    import dataclasses
    html = client.get("/").text
    assert '<script src="/static/conjure-pointers.js?v=' in html      # the single XR input reader
    # Control→action bindings are config, so re-binding (e.g. resize onto the trigger) needs no module edit.
    # resize shares the trigger with select; arbitration (grab reserves the pointer while the beam is on
    # one of its corner handles) is what keeps them apart — see client/conjure-pointers.js.
    assert '"resize":"trigger"' in html and '"select":"trigger"' in html and '"grab":"grip"' in html
    rebound = '{"select":"trigger","grab":"grip","resize":"grip","reel":"stickY"}'
    monkey = dataclasses.replace(srv.settings, bindings=rebound)
    srv.settings, old = monkey, srv.settings
    try:
        assert '"resize":"grip"' in client.get("/").text
    finally:
        srv.settings = old


# ---- rename: identity is the id, so a name change moves nothing --------------------------------------

def test_renaming_a_world_keeps_its_id_and_every_pointer(srv, client):
    scope = srv.DEFAULT_SCOPE
    wid = client.get("/state").json()["world_id"]
    r = client.post("/worlds/rename", json={"scope": scope, "name": "default", "new_name": "The Meadow"}).json()
    assert r["ok"] and r["id"] == wid                       # same world, new label
    st = client.get("/state").json()
    assert st["world"] == "The Meadow" and st["world_id"] == wid
    assert srv.active_world == wid                          # the live pointer never moved
    assert srv.worlds.get_active(scope) == wid              # nor the per-session one
    assert client.post("/worlds/switch", json={"scope": scope, "name": wid}).json()["ok"]   # id still works
    # no aliases: the old name is genuinely gone, and says so rather than silently resolving elsewhere
    assert client.post("/worlds/switch", json={"scope": scope, "name": "default"}).json()["ok"] is False


def test_renaming_the_live_world_survives_the_autosave_round_trip(srv, client):
    """Regression: the live world is held in memory and written back by `_save_active`, so a rename that
    only touched disk was silently reverted by the next switch."""
    scope = srv.DEFAULT_SCOPE
    client.post("/worlds/new", json={"name": "elsewhere"})
    client.post("/worlds/switch", json={"scope": scope, "name": "default"})
    wid = client.get("/state").json()["world_id"]

    client.post("/worlds/rename", json={"scope": scope, "name": wid, "new_name": "Renamed Live"})
    client.post("/worlds/switch", json={"scope": scope, "name": "elsewhere"})   # forces a save of the outgoing
    client.post("/worlds/switch", json={"scope": scope, "name": wid})
    assert client.get("/state").json()["world"] == "Renamed Live"


def test_renaming_a_world_rejects_a_name_another_world_already_has(srv, client):
    scope = srv.DEFAULT_SCOPE
    client.post("/worlds/new", json={"name": "beach"})
    r = client.post("/worlds/rename", json={"scope": scope, "name": "beach", "new_name": "DEFAULT"}).json()
    assert r["ok"] is False and "already exists" in r["error"]      # slug-insensitive clash


def test_renaming_a_space_keeps_its_file_key_so_worlds_still_point_at_it(srv, client):
    # `environment.space` is `<owner>/<id>` and may live in ANOTHER user's world, which we may not
    # rewrite — so the id has to be the thing that never changes.
    _seed_space(srv, "daniel", "space-1")
    before = client.get("/world").json()["environment"].get("space")
    r = client.post("/space/rename", json={"owner": "daniel", "name": "space-1",
                                           "new_name": "Living Room"}).json()
    assert r["ok"] and r["id"] == "space-1"                         # the key is untouched
    assert srv.spaces.load("daniel", "space-1")["name"] == "Living Room"
    assert srv.spaces.resolve("daniel", "Living Room") == "space-1"
    assert client.get("/world").json()["environment"].get("space") == before


def test_cd_and_show_reject_a_world_that_does_not_exist(srv, client):
    # Regression: any trailing path segments used to resolve to a `world` location unchecked, so `cd`
    # onto a nonexistent world quietly succeeded and left you somewhere that isn't there.
    ok = client.post("/admin/tree", json={"path": "/daniel/agents/builder/worlds/default"}).json()
    assert ok["ok"] is True
    bad = client.post("/admin/tree", json={"path": "/daniel/agents/builder/worlds/nope"}).json()
    assert bad["ok"] is False and "no world" in bad["error"]


# --------------------------------------------------------------------------- the arrival ladder
#
# Reported 2026-08-28: switch out of an agent, delete the world you were in from the OTHER agent, switch
# back — and it tried to build a new world, walking past two surviving ones. `_activate_scope` had two
# rungs (resume the remembered world, else mint) where architecture.md §1 specifies three, and
# `WorldDir.delete` unlinked the pointer instead of repointing it, which is what made rung 1 unreachable.

def _worlds_in(srv, scope):
    return sorted(srv.worlds.name_of(scope, w) for w in srv.worlds.list(scope))


def test_deleting_the_world_you_were_in_resumes_a_sibling_not_a_new_one(srv, client):
    outdoor = "daniel/agents/outdoor"
    client.post("/scope/activate", json={"scope": outdoor})            # mints outdoor's opening, 'home'
    client.post("/worlds/new", json={"scope": outdoor, "name": "keeper"})
    client.post("/worlds/new", json={"scope": outdoor, "name": "doomed"})
    before = _worlds_in(srv, outdoor)
    client.post("/scope/activate", json={"scope": srv.DEFAULT_SCOPE})  # step away, as the report did
    srv.worlds.delete(outdoor, "doomed")                               # …and delete the one it was in
    client.post("/scope/activate", json={"scope": outdoor})            # come back
    assert srv.worlds.name_of(outdoor, srv.active_world) in ("keeper", "home")
    assert _worlds_in(srv, outdoor) == [w for w in before if w != "doomed"]   # nothing was minted


def test_the_fallback_is_announced(srv, client, monkeypatch):
    """Every degradation is audible (architecture.md §1) — an arrival somewhere other than where you left
    off is indistinguishable from a bug if it happens in silence."""
    outdoor, said = "daniel/agents/outdoor", []

    async def spy(msg):
        if msg.get("type") == "notice":
            said.append(msg["text"])

    client.post("/scope/activate", json={"scope": outdoor})
    client.post("/worlds/new", json={"scope": outdoor, "name": "keeper"})
    client.post("/scope/activate", json={"scope": srv.DEFAULT_SCOPE})
    # Rung 2 only. Deleting ONE of two worlds now lands on rung 1 — the previous entry in the history —
    # and that is a resumption, not a degradation, so it is deliberately silent.
    srv.worlds._dir(outdoor).dir.joinpath("_active.txt").unlink()      # history lost, worlds intact
    monkeypatch.setattr(srv, "_broadcast", spy)
    client.post("/scope/activate", json={"scope": outdoor})
    assert any("gone" in t for t in said), said


def test_resuming_the_previous_world_is_silent(srv, client, monkeypatch):
    """Rung 1 is not a degradation — it is the pointer doing its job. Narrating it would make every
    ordinary delete-and-return feel like something went wrong."""
    outdoor, said = "daniel/agents/outdoor", []

    async def spy(msg):
        if msg.get("type") == "notice":
            said.append(msg["text"])

    client.post("/scope/activate", json={"scope": outdoor})
    client.post("/worlds/new", json={"scope": outdoor, "name": "keeper"})
    client.post("/scope/activate", json={"scope": srv.DEFAULT_SCOPE})
    srv.worlds.delete(outdoor, "keeper")
    monkeypatch.setattr(srv, "_broadcast", spy)
    client.post("/scope/activate", json={"scope": outdoor})
    assert not said, said


def test_a_first_ever_arrival_is_not_announced_as_a_loss(srv, client, monkeypatch):
    """Minting IS the right answer for an agent you have never opened. Narrating that as a degradation
    would cry wolf on the one path where nothing went wrong."""
    said = []

    async def spy(msg):
        if msg.get("type") == "notice":
            said.append(msg["text"])

    monkeypatch.setattr(srv, "_broadcast", spy)
    client.post("/scope/activate", json={"scope": "daniel/agents/outdoor"})   # never opened in this fixture
    assert not any("gone" in t or "No worlds left" in t for t in said), said


def test_a_world_that_was_never_entered_is_still_preferred_over_minting(srv, client):
    """Rung 2. `/worlds/new` switches to what it creates, so to get an un-entered world we make one and
    leave — the MRU then has no live entry for it, which used to read as 'this scope is empty'."""
    outdoor = "daniel/agents/outdoor"
    client.post("/scope/activate", json={"scope": outdoor})
    client.post("/worlds/new", json={"scope": outdoor, "name": "orphan"})
    srv.worlds._dir(outdoor).dir.joinpath("_active.txt").unlink()      # history lost, worlds intact
    client.post("/scope/activate", json={"scope": srv.DEFAULT_SCOPE})
    n_before = len(srv.worlds.list(outdoor))
    client.post("/scope/activate", json={"scope": outdoor})
    assert len(srv.worlds.list(outdoor)) == n_before                   # opened one, did not add one


def test_deleting_the_live_session_resumes_a_sibling_session(srv, client):
    """The same rung one level up. It used to 'work' only when a scope happened to still have the literal
    id `session-1` — `MIGRATED_SID`, a migration-era constant standing in for a search."""
    scope = srv.DEFAULT_SCOPE
    first = srv._ensure_session(scope)                                 # whatever the fixture is live in
    made = client.post("/session/new", json={"scope": scope, "title": "second"}).json()
    assert made["ok"] and srv.sessions.get_active(scope) != first
    srv.sessions.delete(scope, srv.sessions.get_active(scope))
    assert srv._ensure_session(scope) == first                         # …resumed, not recreated


# --------------------------------------------------------------------------- a space remembers more than one world
#
# `last_world` was a SINGLE field, so deleting it left the space with no memory at all: walk back into
# that room after a cleanup and it minted a fresh world rather than opening the one you had there before.
# The third place the same single-pointer shape caused the same bug (worlds and sessions were the first
# two). A space's history is `recent` — `[[scope, world_id], …]`, newest first.

def _world_in(srv, name, space):
    from conjure.world import WorldStore
    return srv.worlds.save(srv.DEFAULT_SCOPE, name, WorldStore(
        {"name": name, "rev": 1, "environment": {"space": space}, "entities": []}))


def test_a_space_falls_back_to_the_world_you_had_there_before(srv, client):
    older = _world_in(srv, "workshop", "daniel/office")
    newer = _world_in(srv, "gallery", "daniel/office")
    _geo_space(srv, "daniel", "office", 40.71, -74.0,
               recent=[[srv.DEFAULT_SCOPE, newer], [srv.DEFAULT_SCOPE, older]],
               last_scope=srv.DEFAULT_SCOPE, last_world=newer)
    srv.worlds.delete(srv.DEFAULT_SCOPE, "gallery")                # the one you were last in, gone
    n_before = len(srv.worlds.list(srv.DEFAULT_SCOPE))
    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "office",
                                           "user": "daniel", "cid": "hs-fallback"}).json()
    assert r["ok"] and _wname(srv) == "workshop"                   # …not a fresh mint
    assert len(srv.worlds.list(srv.DEFAULT_SCOPE)) == n_before


def test_falling_back_inside_a_space_says_so(srv, client, monkeypatch):
    said = []

    async def spy(msg):
        if msg.get("type") == "notice":
            said.append(msg["text"])

    older = _world_in(srv, "workshop", "daniel/office")
    newer = _world_in(srv, "gallery", "daniel/office")
    _geo_space(srv, "daniel", "office", 40.71, -74.0,
               recent=[[srv.DEFAULT_SCOPE, newer], [srv.DEFAULT_SCOPE, older]],
               last_scope=srv.DEFAULT_SCOPE, last_world=newer)
    srv.worlds.delete(srv.DEFAULT_SCOPE, "gallery")
    monkeypatch.setattr(srv, "_broadcast", spy)
    client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "office",
                                       "user": "daniel", "cid": "hs-say"})
    assert any("gone" in t and "workshop" in t for t in said), said


def test_resuming_the_head_of_a_space_history_is_not_announced_as_a_loss(srv, client, monkeypatch):
    said = []

    async def spy(msg):
        if msg.get("type") == "notice":
            said.append(msg["text"])

    wid = _world_in(srv, "gallery", "daniel/office")
    _geo_space(srv, "daniel", "office", 40.71, -74.0,
               recent=[[srv.DEFAULT_SCOPE, wid]], last_scope=srv.DEFAULT_SCOPE, last_world=wid)
    monkeypatch.setattr(srv, "_broadcast", spy)
    client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "office",
                                       "user": "daniel", "cid": "hs-head"})
    assert not any("gone" in t for t in said), said


def test_a_space_saved_before_the_history_existed_still_resolves(srv, client):
    """Every space doc on disk predates `recent`; the legacy pair has to keep working on its own."""
    wid = _world_in(srv, "office-world", "daniel/office")
    _geo_space(srv, "daniel", "office", 40.71, -74.0,
               last_scope=srv.DEFAULT_SCOPE, last_world=wid)      # no `recent` at all
    r = client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "office",
                                           "user": "daniel", "cid": "hs-legacy"}).json()
    assert r["ok"] and _wname(srv) == "office-world"


def test_the_history_never_reaches_into_another_space(srv, client):
    """The session ladder's rung 2 — 'any sibling in that session' — would be WRONG here: a world carries
    its own `environment.space`, so a sibling built for another room would compose that room's walls on
    top of the real ones. When a space's own history is spent, the answer is to build one BOUND to it."""
    _world_in(srv, "elsewhere", "daniel/home")                    # a sibling in the same scope, other space
    gone = _world_in(srv, "gallery", "daniel/office")
    _geo_space(srv, "daniel", "office", 40.71, -74.0,
               recent=[[srv.DEFAULT_SCOPE, gone]], last_scope=srv.DEFAULT_SCOPE, last_world=gone)
    srv.worlds.delete(srv.DEFAULT_SCOPE, "gallery")
    client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "office",
                                       "user": "daniel", "cid": "hs-bound"})
    assert _wname(srv) != "elsewhere"                             # did NOT reach sideways
    # …and what it DID open is bound to the room you are standing in — which is the whole reason a
    # sibling from the same session is the wrong candidate, however consistent it would look.
    assert (srv.active_space_owner, srv.active_space) == ("daniel", "office")


def test_the_history_records_what_was_open_and_caps(srv, client):
    from conjure.world import _MRU_CAP
    _geo_space(srv, "daniel", "home", 37.77, -122.42)
    client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                       "user": "daniel", "cid": "hs-rec"})
    for i in range(_MRU_CAP + 3):
        client.post("/worlds/new", json={"scope": srv.DEFAULT_SCOPE, "name": f"w{i}"})
    srv._save_active()
    recent = srv.spaces.load("daniel", "home").get("recent") or []
    assert 0 < len(recent) <= _MRU_CAP
    assert recent[0] == [srv.active_scope, srv.active_world]      # newest first, and it is where we are


# -- being moved by the room is announced ---------------------------------------------------------

def test_a_room_match_that_relocates_you_says_so(srv, client, monkeypatch):
    """Decision #20 made a room-driven AGENT change audible. A relocation within the SAME agent — restart
    the server in a different room — stayed silent, which is the same surprise minus the attribution."""
    said = []

    async def spy(msg):
        if msg.get("type") == "notice":
            said.append(msg["text"])

    wid = _world_in(srv, "office-world", "daniel/office")
    _geo_space(srv, "daniel", "office", 40.71, -74.0,
               recent=[[srv.DEFAULT_SCOPE, wid]], last_scope=srv.DEFAULT_SCOPE, last_world=wid)
    monkeypatch.setattr(srv, "_broadcast", spy)
    client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "office",
                                       "user": "daniel", "cid": "hs-move"})
    assert any("daniel/office" in t and "office-world" in t for t in said), said


def test_being_admitted_to_the_room_you_are_already_in_says_nothing(srv, client, monkeypatch):
    """No relocation, no announcement — otherwise every headset reconnect narrates itself."""
    said = []

    async def spy(msg):
        if msg.get("type") == "notice":
            said.append(msg["text"])

    _geo_space(srv, "daniel", "home", 37.77, -122.42,
               recent=[[srv.active_scope, srv.active_world]],
               last_scope=srv.active_scope, last_world=srv.active_world)
    monkeypatch.setattr(srv, "_broadcast", spy)
    client.post("/space/select", json={"matched": True, "owner": "daniel", "name": "home",
                                       "user": "daniel", "cid": "hs-same"})
    assert not any("now — opened" in t for t in said), said
