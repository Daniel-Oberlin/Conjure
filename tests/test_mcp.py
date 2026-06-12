"""Contract: each MCP tool POSTs the payload its world-server endpoint expects. Guards against
drift between the director's tools and the server (a silent break otherwise)."""

import json

import httpx
import pytest
import respx

import conjure.mcp_server as m


def _tool(name):
    """The callable behind an @mcp.tool() (handles either a plain function or a FunctionTool)."""
    obj = getattr(m, name)
    return obj if callable(obj) and not hasattr(obj, "fn") else obj.fn


@respx.mock
async def test_place_asset_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/place_asset").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": "e", "title": "Tree", "tris": 100, "attribution": "a"}))
    out = await _tool("place_asset")(query="oak tree", size_m=7, position=[1, 0, -3])
    assert route.called
    assert json.loads(route.calls.last.request.content) == {"query": "oak tree", "size_m": 7, "position": [1, 0, -3]}
    assert "Placed" in out


@respx.mock
async def test_generate_image_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/images/generate").mock(
        return_value=httpx.Response(200, json={"ok": True, "image_id": "abc.png", "url": "/assets/abc.png",
                                               "w": 1024, "h": 1024, "provider": "Gemini"}))
    out = await _tool("generate_image")(prompt="a dragon", transparent=True)
    assert json.loads(route.calls.last.request.content) == {
        "prompt": "a dragon", "transparent": True}              # None aspect_ratio/generator dropped
    assert "image_id=abc.png" in out                            # id surfaced for the next call


@respx.mock
async def test_generate_skybox_image_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/images/skybox").mock(
        return_value=httpx.Response(200, json={"ok": True, "image_id": "s.png", "provider": "Gemini"}))
    await _tool("generate_skybox_image")(prompt="a forest")
    assert json.loads(route.calls.last.request.content) == {"prompt": "a forest"}


@respx.mock
async def test_procurement_edit_passes_image_id(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/images/edit").mock(
        return_value=httpx.Response(200, json={"ok": True, "image_id": "n.png", "provider": "Gemini"}))
    await _tool("edit_image")(image_id="abc.png", prompt="add a moon", generator="Chat")
    assert json.loads(route.calls.last.request.content) == {
        "image_id": "abc.png", "prompt": "add a moon", "transparent": False, "generator": "Chat"}


@respx.mock
async def test_place_image_tool_takes_image_id(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/place_image").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": "i", "image_id": "abc.png"}))
    await _tool("place_image")(image_id="abc.png", size_m=2.0)
    assert json.loads(route.calls.last.request.content) == {"image_id": "abc.png", "size_m": 2.0}


@respx.mock
async def test_set_skybox_tool_takes_image_id(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/set_skybox").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("set_skybox")(image_id="s.png")
    assert json.loads(route.calls.last.request.content) == {"image_id": "s.png"}


@respx.mock
async def test_edit_scene_image_tool_is_entity_keyed(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/edit_image").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("edit_scene_image")(id="ent_image_1", prompt="make it night")
    assert json.loads(route.calls.last.request.content) == {"id": "ent_image_1", "prompt": "make it night"}


@respx.mock
async def test_set_immersion_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 1}))
    await _tool("set_immersion")(mode="ar")
    body = json.loads(route.calls.last.request.content)
    assert body["ops"][0] == {"op": "env", "set": {
        "passthrough": True, "room.active": True, "room.defaultSurfaceVisible": False}}


async def test_set_immersion_rejects_unknown_mode(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    out = await _tool("set_immersion")(mode="bogus")
    assert "Unknown mode" in out


@respx.mock
async def test_show_surface_updates_matching_surfaces(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.get("http://world/world").mock(return_value=httpx.Response(200, json={"entities": [
        {"id": "real_wall_1", "meta": {"real": True, "semantic": "wall"}},
        {"id": "real_wall_2", "meta": {"real": True, "semantic": "wall"}},
        {"id": "cube", "meta": {}}]}))
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 2}))
    await _tool("show_surface")(target="wall", visible=True)
    ops = json.loads(route.calls.last.request.content)["ops"]
    assert {o["id"] for o in ops} == {"real_wall_1", "real_wall_2"}      # both walls, not the cube
    assert all(o["set"] == {"components.material.visible": True} for o in ops)


@respx.mock
async def test_reset_world_tool(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/reset").mock(return_value=httpx.Response(200, json={"ok": True, "rev": 1}))
    out = await _tool("reset_world")()
    assert route.called and "reset" in out.lower()


@respx.mock
async def test_realign_room_tool(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/room/realign").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("realign_room")()
    assert route.called and json.loads(route.calls.last.request.content) == {}


@respx.mock
async def test_texture_surface_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/texture_surface").mock(
        return_value=httpx.Response(200, json={"ok": True, "count": 1, "image_id": "g.png"}))
    await _tool("texture_surface")(target="floor", image_id="g.png", repeat=4)
    assert json.loads(route.calls.last.request.content) == {
        "target": "floor", "image_id": "g.png", "repeat": 4}


@respx.mock
async def test_query_room_summarizes(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.get("http://world/world").mock(return_value=httpx.Response(200, json={
        "entities": [{"id": "real_floor", "meta": {"real": True, "semantic": "floor"},
                      "components": {"material": {}}, "transform": {"position": [0, 0, 0]}}],
        "environment": {"passthrough": True, "room": {"active": True, "boundary": {"height": 2.6}}}}))
    out = await _tool("query_room")()
    assert "floor" in out and "2.6" in out


@respx.mock
async def test_list_image_generators_tool(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.get("http://world/images/generators").mock(return_value=httpx.Response(200, json={
        "ok": True, "generators": [{"name": "Gemini", "capabilities": {
            "operations": ["generate", "edit", "outpaint", "skybox"], "edit_mode": "prompt",
            "max_resolution": 4096, "aspect": "free", "transparency": False}}],
        "defaults": {"generate": "Gemini"}}))
    out = await _tool("list_image_generators")()
    assert "Gemini" in out and "transparency=False" in out


@respx.mock
async def test_tool_reports_failure(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.post("http://world/place_asset").mock(return_value=httpx.Response(200, json={"ok": False, "error": "boom"}))
    out = await _tool("place_asset")(query="x", size_m=1)
    assert "boom" in out or "Couldn't" in out
