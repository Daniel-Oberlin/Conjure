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
    assert json.loads(route.calls.last.request.content) == {
        "query": "oak tree", "size_m": 7, "position": [1, 0, -3], "placement": "grounded"}
    assert "Placed" in out


@respx.mock
async def test_world_resource_lists_only_placed_objects(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.get("http://world/world").mock(return_value=httpx.Response(200, json={
        "name": "Holodeck", "rev": 5, "entities": [
            {"id": "floor", "meta": {"scaffold": True}, "components": {"geometry": {"primitive": "plane"}}},
            {"id": "real_wall_1", "meta": {"real": True, "semantic": "wall"}, "transform": {"position": [0, 1, -2]}},
            {"id": "ent_asset_1", "meta": {"title": "Oak Tree"}, "components": {"gltf-model": "/assets/x.glb"},
             "transform": {"position": [0, 0, -3]}},
            {"id": "img_1", "meta": {"prompt": "a dragon", "image_id": "d.png"},
             "components": {"material": {"src": "/assets/d.png"}}, "transform": {"position": [1, 1, -2]}},
        ], "environment": {}}))
    out = await _tool("world_resource")()
    assert "ent_asset_1" in out and "Oak Tree" in out        # placed model listed
    assert "img_1" in out and "a dragon" in out              # placed image listed
    assert "[asset d.png]" in out and "[asset x.glb]" in out  # library asset id linked for scene→library mapping
    assert "floor" not in out and "real_wall_1" not in out   # scaffold + real surfaces excluded


@respx.mock
async def test_query_world_dumps_everything(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.get("http://world/world").mock(return_value=httpx.Response(200, json={
        "name": "Holodeck", "rev": 5, "entities": [
            {"id": "floor", "meta": {"scaffold": True}, "components": {"geometry": {"primitive": "plane"}}},
            {"id": "ent_asset_1", "meta": {"title": "Oak Tree"}, "components": {"gltf-model": "/assets/x.glb"}},
        ], "environment": {"sky": {"color": "#000"}}}))
    out = await _tool("query_world")()
    assert "floor" in out and "ent_asset_1" in out and "environment" in out  # full dump incl. scaffold


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
async def test_search_library_tool_summarizes_candidates(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.post("http://world/library/search").mock(return_value=httpx.Response(200, json={
        "ok": True, "confidence_tier": "strong",
        "candidates": [{"id": "d.png", "kind": "image", "match": "exact", "label": "a red dragon"}]}))
    out = await _tool("search_library")(query="a red dragon")
    assert "strong" in out and "d.png" in out


@respx.mock
async def test_place_cached_asset_tool_forwards_id(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/place_cached_asset").mock(
        return_value=httpx.Response(200, json={"ok": True, "id": "ent_1", "title": "Oak Tree"}))
    await _tool("place_cached_asset")(id="oak.glb", size_m=2.0)
    assert json.loads(route.calls.last.request.content) == {"id": "oak.glb", "size_m": 2.0, "placement": "grounded"}


@respx.mock
async def test_update_asset_tool_forwards_fields_with_scope(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    route = respx.post("http://world/update_asset").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("update_asset")(id="x.glb", label="X-Wing", reject_for="starship enterprise")
    assert json.loads(route.calls.last.request.content) == {
        "id": "x.glb", "scope": "private/builder", "label": "X-Wing", "reject_for": "starship enterprise"}


@respx.mock
async def test_update_asset_tool_forwards_public(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    route = respx.post("http://world/update_asset").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("update_asset")(id="pear.png", public=False)
    assert json.loads(route.calls.last.request.content) == {
        "id": "pear.png", "scope": "private/builder", "public": False}


@respx.mock
async def test_set_world_visibility_tool_forwards_and_reports_published(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    route = respx.post("http://world/worlds/visibility").mock(return_value=httpx.Response(200, json={
        "ok": True, "world": "home", "public": True, "published_assets": ["a pear"]}))
    out = await _tool("set_world_visibility")(public=True)
    assert json.loads(route.calls.last.request.content) == {"public": True, "scope": "private/builder"}
    assert "public" in out and "a pear" in out               # reports the auto-published assets to the user


@respx.mock
async def test_view_relative_tool_forwards_and_formats(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    route = respx.post("http://world/view_relative").mock(return_value=httpx.Response(200, json={
        "ok": True, "direction": "forward", "distance": 1.0, "point": [0.0, 1.6, -1.0],
        "surface": {"id": "real_wall_7", "semantic": "wall", "friendly_id": 7, "distance": 3.0},
        "nearby": [{"id": "ent_lamp", "title": "brass lamp", "distance": 1.0}]}))
    out = await _tool("view_relative")(direction="forward", distance=1.0)
    assert json.loads(route.calls.last.request.content) == {"direction": "forward", "distance": 1.0}
    assert "1.6" in out and "real_wall_7" in out and "brass lamp" in out   # point + surface + nearby relayed


@respx.mock
async def test_delete_asset_tool_forwards_id_with_scope(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    route = respx.post("http://world/delete_asset").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("delete_asset")(id="dup.glb")
    assert json.loads(route.calls.last.request.content) == {"id": "dup.glb", "scope": "private/builder"}


@respx.mock
async def test_query_assets_tool_forwards_sql_with_scope(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    route = respx.post("http://world/query_assets").mock(return_value=httpx.Response(200, json={
        "ok": True, "rows": [{"kind": "image", "n": 3}]}))
    out = await _tool("query_assets")(sql="SELECT kind, COUNT(*) AS n FROM assets GROUP BY kind")
    assert json.loads(route.calls.last.request.content) == {
        "sql": "SELECT kind, COUNT(*) AS n FROM assets GROUP BY kind", "scope": "private/builder"}
    assert "image" in out and "3" in out                     # rows rendered for the LLM


@respx.mock
async def test_set_grounded_skybox_omits_unset_dims_but_forwards_overrides(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/set_grounded_skybox").mock(return_value=httpx.Response(200, json={"ok": True}))
    await _tool("set_grounded_skybox")(image_id="g.png")
    assert json.loads(route.calls.last.request.content) == {"image_id": "g.png"}   # defaults left to server
    await _tool("set_grounded_skybox")(image_id="g.png", height=6.0, radius=60.0)
    assert json.loads(route.calls.last.request.content) == {"image_id": "g.png", "height": 6.0, "radius": 60.0}


@respx.mock
async def test_edit_scene_image_tool_is_entity_keyed(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/edit_image").mock(return_value=httpx.Response(200, json={
        "ok": True, "id": "ent_image_1", "image_id": "n.png", "provider": "Gemini", "w": 1024, "h": 1024}))
    out = await _tool("edit_scene_image")(id="ent_image_1", prompt="make it night")
    assert json.loads(route.calls.last.request.content) == {"id": "ent_image_1", "prompt": "make it night"}
    assert "n.png" in out and "Gemini" in out          # result carries the new id + provenance


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
async def test_style_surface_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/style_surface").mock(
        return_value=httpx.Response(200, json={"ok": True, "count": 2}))
    await _tool("style_surface")(target="wall", color="blue", opacity=0.4)
    assert json.loads(route.calls.last.request.content) == {"target": "wall", "color": "blue", "opacity": 0.4}


@respx.mock
async def test_show_annotations_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 1}))
    await _tool("show_annotations")(on=True)
    assert json.loads(route.calls.last.request.content)["ops"][0] == {
        "op": "env", "set": {"room.annotations": True, "room.annotationDims": False}}


@respx.mock
async def test_show_annotations_with_dimensions(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 1}))
    await _tool("show_annotations")(on=True, dimensions=True)
    assert json.loads(route.calls.last.request.content)["ops"][0] == {
        "op": "env", "set": {"room.annotations": True, "room.annotationDims": True}}


@respx.mock
async def test_show_edges_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 1}))
    await _tool("show_edges")(on=False)
    assert json.loads(route.calls.last.request.content)["ops"][0] == {
        "op": "env", "set": {"room.edgesVisible": False}}


@respx.mock
async def test_style_edges_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 1}))
    await _tool("style_edges")(color="lime", opacity=0.5)
    assert json.loads(route.calls.last.request.content)["ops"][0] == {
        "op": "env", "set": {"room.edgeColor": "lime", "room.edgeOpacity": 0.5}}


@respx.mock
async def test_style_annotations_tool_payload(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 1}))
    await _tool("style_annotations")(color="yellow")
    assert json.loads(route.calls.last.request.content)["ops"][0] == {
        "op": "env", "set": {"room.annotationColor": "yellow"}}


async def test_style_edges_noop_without_args(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    out = await _tool("style_edges")()
    assert "Nothing to change" in out


@respx.mock
async def test_show_surface_matches_friendly_id(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.get("http://world/world").mock(return_value=httpx.Response(200, json={"entities": [
        {"id": "real_wall_a", "meta": {"real": True, "semantic": "wall", "friendly_id": 12}},
        {"id": "real_wall_b", "meta": {"real": True, "semantic": "wall", "friendly_id": 13}}]}))
    route = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"rev": 2}))
    await _tool("show_surface")(target="12", visible=False)
    ops = json.loads(route.calls.last.request.content)["ops"]
    assert {o["id"] for o in ops} == {"real_wall_a"}            # the friendly id picks one wall


@respx.mock
async def test_query_room_summarizes(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.get("http://world/world").mock(return_value=httpx.Response(200, json={
        "entities": [{"id": "real_floor", "meta": {"real": True, "semantic": "floor", "friendly_id": 7},
                      "components": {"material": {}}, "transform": {"position": [0, 0, 0]}}],
        "environment": {"passthrough": True, "room": {"active": True, "boundary": {"height": 2.6}}}}))
    out = await _tool("query_room")()
    assert "floor" in out and "2.6" in out and "#7" in out      # friendly id surfaced


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


@respx.mock
async def test_new_world_tool_forwards_name_with_scope(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    route = respx.post("http://world/worlds/new").mock(
        return_value=httpx.Response(200, json={"ok": True, "world": "castle-quest/dining-hall"}))
    out = await _tool("new_world")(name="Castle Quest/Dining Hall")
    assert json.loads(route.calls.last.request.content) == {
        "name": "Castle Quest/Dining Hall", "scope": "private/builder", "public": True, "outdoor": False}
    assert "castle-quest/dining-hall" in out


@respx.mock
async def test_list_worlds_tool_marks_active(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    respx.post("http://world/worlds/list").mock(return_value=httpx.Response(200, json={
        "ok": True, "active": "wld_1111111111",
        "worlds": [{"id": "wld_1111111111", "name": "Blade Runner 1"},
                   {"id": "wld_2222222222", "name": "default"}]}))
    out = await _tool("list_worlds")()
    assert "Blade Runner 1" in out and "default" in out and "*" in out    # active marked
    # The agent is shown the permanent id alongside the name, and told to store THAT — a name it stashes
    # in `state_set` would be stale the moment a person renames the world.
    assert "wld_1111111111" in out and "Store the id" in out


@respx.mock
async def test_switch_world_tool_reports_error(monkeypatch):
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "private", "scope": "private/builder"})
    respx.post("http://world/worlds/switch").mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "no world 'nope'"}))
    out = await _tool("switch_world")(name="nope")
    assert "Couldn't switch" in out and "nope" in out


# --- Hard tool gate (agent-separation-plan §3c, Layer 2) -------------------------------------------

async def test_tool_gate_blocks_out_of_scope_tool(monkeypatch):
    # Enforced in the real wired dispatch (mcp.call_tool), a separate process from the LLM — so it
    # holds regardless of what the model was offered. Denied BEFORE any HTTP to the world server.
    monkeypatch.setattr(m, "_ALLOWED_TOOLS", {"set_skybox"})
    monkeypatch.setattr(m, "_ACCESS", "all")
    out = await m.mcp.call_tool("style_surface", {})
    assert "not permitted" in "".join(getattr(c, "text", "") for c in out)
    assert m._tool_denied("set_skybox") is None                 # an allowed tool passes the gate


async def test_tool_gate_read_only_blocks_mutating(monkeypatch):
    monkeypatch.setattr(m, "_ALLOWED_TOOLS", None)              # no tool-list restriction…
    monkeypatch.setattr(m, "_ACCESS", "read")                  # …but read-only access
    out = await m.mcp.call_tool("add_entity", {})
    assert "read-only" in "".join(getattr(c, "text", "") for c in out)
    assert m._tool_denied("query_world") is None                # read tools still allowed


def test_tool_gate_unset_env_means_no_restriction(monkeypatch):
    # CONJURE_TOOLS unset (e.g. a standalone `python -m conjure.mcp_server`) ⇒ no restriction.
    monkeypatch.setattr(m, "_ALLOWED_TOOLS", None)
    monkeypatch.setattr(m, "_ACCESS", "all")
    assert m._tool_denied("style_surface") is None


def test_readonly_tools_are_all_real_tool_names():
    import pathlib
    import re

    import conjure
    src = (pathlib.Path(conjure.__file__).parent / "mcp_server.py").read_text()
    server_tools = set(re.findall(r"@mcp\.tool\([^)]*\)\s*\nasync def (\w+)", src))
    assert m._READONLY_TOOLS <= server_tools, m._READONLY_TOOLS - server_tools


@respx.mock
async def test_set_caller_threads_the_speaker_into_requests(monkeypatch):
    # Step 3: the director sets the per-turn speaker; subsequent world-server calls carry that identity
    # (owner gate + asset-ownership scope), not the fixed launch identity.
    monkeypatch.setattr(m, "BASE", "http://world")
    monkeypatch.setattr(m, "_CALLER", {"user": "daniel", "scope": "daniel/agents/builder"})
    await _tool("set_caller")(user="guest", scope="guest/agents/builder")
    assert m._headers() == {"X-Conjure-User": "guest", "X-Conjure-Scope": "guest/agents/builder"}

    r_post = respx.post("http://world/query_world").mock(return_value=httpx.Response(200, json={"ok": True}))
    await m._post("/query_world", {})
    assert r_post.calls.last.request.headers["X-Conjure-User"] == "guest"

    r_patch = respx.post("http://world/patch").mock(return_value=httpx.Response(200, json={"ok": True, "rev": 1}))
    await m._post_patch([{"op": "add", "entity": {"id": "x"}}])         # patch now carries identity too
    assert r_patch.calls.last.request.headers["X-Conjure-User"] == "guest"


def test_set_caller_is_exempt_from_the_capability_gate(monkeypatch):
    # A restricted/read-only agent must still let the director set the caller (it's a control tool).
    monkeypatch.setattr(m, "_ALLOWED_TOOLS", {"query_world"})
    monkeypatch.setattr(m, "_ACCESS", "read")
    assert m._tool_denied("set_caller") is None
    assert m._tool_denied("place_asset") is not None                    # a normal tool stays gated


@respx.mock
async def test_post_patch_raises_owner_only_on_403(monkeypatch):
    # A non-owner speaker's write is 403'd by the world server; _post_patch raises so the patch tools
    # surface the reason instead of a KeyError (they read patch['rev']) or a false success.
    monkeypatch.setattr(m, "BASE", "http://world")
    respx.post("http://world/patch").mock(return_value=httpx.Response(
        403, json={"ok": False, "error": "This world belongs to daniel; only the owner can change it."}))
    with pytest.raises(PermissionError, match="belongs to daniel"):
        await m._post_patch([{"op": "add", "entity": {"id": "x"}}])
