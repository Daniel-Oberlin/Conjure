"""Integration tests for the world-server endpoints (the seams our MCP tools + client depend on),
with external services faked. No network, no keys, no LLM."""

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
              "/outpaint_image", "/set_skybox", "/skybox_from_image", "/assets/{filename}", "/ws",
              "/images/generators", "/images/generate", "/images/skybox", "/images/edit",
              "/images/outpaint", "/images/skybox_from"):
        assert p in paths, f"missing route {p}"


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
