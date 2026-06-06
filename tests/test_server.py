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


def test_place_image_hangs_textured_plane(srv, client, tmp_path):
    r = client.post("/place_image", json={"prompt": "a red dragon", "size_m": 1.0})
    assert r.json()["ok"] is True
    img = next(e for e in _entities(client) if e.get("components", {}).get("material", {}).get("src"))
    src = img["components"]["material"]["src"]
    assert src.startswith("/assets/") and src.endswith(".png")
    # The image was actually cached and is served.
    assert client.get(src).status_code == 200
    assert img["meta"]["generated"] is True


def test_edit_image_updates_in_place(srv, client):
    eid = client.post("/place_image", json={"prompt": "a dragon"}).json()["id"]
    before = next(e for e in _entities(client) if e["id"] == eid)["components"]["material"]["src"]
    r = client.post("/edit_image", json={"id": eid, "prompt": "add a moon"})
    assert r.json()["ok"] is True
    after = next(e for e in _entities(client) if e["id"] == eid)["components"]["material"]["src"]
    assert after != before  # the texture swapped


def test_edit_unknown_entity_errors(srv, client):
    assert client.post("/edit_image", json={"id": "nope", "prompt": "x"}).json()["ok"] is False


def test_set_skybox_sets_env_sky_src(srv, client):
    r = client.post("/set_skybox", json={"prompt": "a sunset beach"})
    assert r.json()["ok"] is True
    sky = client.get("/world").json()["environment"]["sky"]
    assert "src" in sky and sky["src"].startswith("/assets/")


def test_disabled_when_no_image_generator(srv, client):
    srv.image_gen = None
    assert client.post("/set_skybox", json={"prompt": "x"}).json()["ok"] is False


def test_expected_routes_exist(srv):
    # Contract: the endpoints the MCP tools + client depend on must exist.
    paths = {getattr(r, "path", None) for r in srv.app.routes}
    for p in ("/", "/world", "/patch", "/place_asset", "/place_image", "/edit_image",
              "/outpaint_image", "/set_skybox", "/skybox_from_image", "/assets/{filename}", "/ws"):
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
