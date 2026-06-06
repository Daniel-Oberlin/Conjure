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
async def test_place_image_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/place_image").mock(return_value=httpx.Response(200, json={"ok": True, "id": "i", "model": "x"}))
    await _tool("place_image")(prompt="a dragon")
    assert json.loads(route.calls.last.request.content) == {"prompt": "a dragon"}


@respx.mock
async def test_edit_image_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/edit_image").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("edit_image")(id="ent_image_1", prompt="add a moon")
    assert json.loads(route.calls.last.request.content) == {"id": "ent_image_1", "prompt": "add a moon"}


@respx.mock
async def test_set_skybox_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/set_skybox").mock(return_value=httpx.Response(200, json={"ok": True, "model": "x"}))
    await _tool("set_skybox")(prompt="a forest")
    assert json.loads(route.calls.last.request.content) == {"prompt": "a forest"}


@respx.mock
async def test_tool_reports_failure(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.post("http://world/place_asset").mock(return_value=httpx.Response(200, json={"ok": False, "error": "boom"}))
    out = await _tool("place_asset")(query="x", size_m=1)
    assert "boom" in out or "Couldn't" in out
