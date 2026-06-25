"""Conjure world-editing MCP server (architecture.md §8).

Exposes the director's action vocabulary as MCP tools. Each tool translates an
intent into a patch and POSTs it to the running world server (`/patch`), which
validates, applies, and broadcasts it to every connected headset. The world stays
authoritative in one place; this server is a thin, stateless front.

Run (stdio transport):  python -m conjure.mcp_server
Needs the world server running (default http://localhost:8080, override CONJURE_URL).

Phase-1 scope: primitive entities + basic environment, matching the current client
renderer. `place_asset` / generation arrive with the asset pipeline (Phase 3).
"""

from __future__ import annotations

import os
from typing import Any, Optional
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("CONJURE_URL", "http://localhost:8080")

mcp = FastMCP("conjure-world")


async def _post_patch(ops: list[dict[str, Any]], origin: str = "director") -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{BASE}/patch", json={"origin": origin, "ops": ops})
        resp.raise_for_status()
        return resp.json()


def _body(**kw) -> dict[str, Any]:
    """Drop None-valued keys so optional params aren't sent."""
    return {k: v for k, v in kw.items() if v is not None}


async def _post(path: str, body: dict[str, Any], timeout: float = 150.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{BASE}{path}", json=body)
        resp.raise_for_status()
        return resp.json()


async def _get(path: str, timeout: float = 10.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{BASE}{path}")
        resp.raise_for_status()
        return resp.json()


def _entity_line(e: dict) -> str:
    """'<id>: <what it is> at <pos>' — shared by query_world and the world://current resource."""
    comps = e.get("components", {})
    meta = e.get("meta", {})
    pos = e.get("transform", {}).get("position")
    if meta.get("real"):
        desc = f"REAL {meta.get('semantic', 'surface')} (room surface — restyle/hide/mount, don't move)"
    elif "gltf-model" in comps:
        desc = f"model {meta.get('title', '?')!r}"
    elif comps.get("material", {}).get("src"):
        desc = f"image {(meta.get('prompt') or meta.get('title') or '?')!r}"
    else:
        prim = comps.get("geometry", {}).get("primitive", "?")
        color = comps.get("material", {}).get("color", "?")
        desc = f"{prim} {color}"
    return f"{e['id']}: {desc} at {pos}"


@mcp.tool()
async def query_world() -> str:
    """Full world dump (every entity + environment). RARELY needed — your placed objects are already in
    the Live context each turn; use this only for detail the summary omits, or a very large scene."""
    doc = await _get("/world")
    lines = [f"World {doc.get('name', '')!r} (rev {doc['rev']}), {len(doc['entities'])} entities:"]
    lines += [f"  - {_entity_line(e)}" for e in doc["entities"]]
    lines.append(f"environment: {doc.get('environment', {})}")
    return "\n".join(lines)


# --- Room model (AR / scene understanding) — see docs/room-model.md -----------------------------

_IMMERSION = {
    "virtual_room": {"passthrough": False, "room.active": True, "room.defaultSurfaceVisible": True},
    "ar":           {"passthrough": True,  "room.active": True, "room.defaultSurfaceVisible": False},
    "mixed":        {"passthrough": True,  "room.active": True},
    "authored":     {"passthrough": False, "room.active": True, "room.defaultSurfaceVisible": False},
    "vr_unbounded": {"passthrough": False, "room.active": False, "room.defaultSurfaceVisible": False},
}


async def _room_summary() -> str:
    """Text summary of the real room: surfaces (by semantic + short id) + the boundary. Shared by the
    query_room tool and the `room://current` resource (which agents inject into their prompt each turn,
    so they needn't call query_room just to see surfaces)."""
    doc = await _get("/world")
    env = doc.get("environment", {})
    room = env.get("room", {})
    reals = [e for e in doc["entities"] if e.get("meta", {}).get("real")]
    if not room.get("active") or not reals:
        return "No room model yet — the headset hasn't shared one (capture the room, or work in VR)."
    lines = [f"Room: {len(reals)} surfaces · passthrough={env.get('passthrough', False)} · "
             f"surfaces-visible-by-default={room.get('defaultSurfaceVisible', False)}"]
    b = room.get("boundary")
    if b:
        lines.append(f"boundary: height {b.get('height')}m, floor polygon {b.get('floorPolygon')}")
    for e in reals:
        m = e.get("meta", {})
        mat = e.get("components", {}).get("material", {})
        vis = mat.get("visible", room.get("defaultSurfaceVisible", False))
        lines.append(f"  - {m.get('semantic', 'surface')} #{m.get('friendly_id', '?')} ({e['id']}) at "
                     f"{e.get('transform', {}).get('position')} (visible={vis}, color={mat.get('color')})")
    return "\n".join(lines)


@mcp.tool()
async def query_room() -> str:
    """Summarize the user's real room: surfaces (by semantic label) + the boundary. Read this before
    placing things (so models land INSIDE the room, not through a wall) or to pick a surface to mount
    on / restyle. Real surfaces also appear in query_world as REAL entities — restyle or hide them
    with update_entity's color, or show_surface; don't move or remove them.
    """
    return await _room_summary()


@mcp.resource("room://current")
async def room_resource() -> str:
    """The live real-room summary — injected each turn into agents that list `room://current` in their
    context (so the builder sees the room without a query_room round-trip)."""
    return await _room_summary()


@mcp.resource("world://current")
async def world_resource() -> str:
    """Live summary of PLACED virtual objects (the models/images/skyboxes you've added) — injected each
    turn so the builder references them by id without a query_world round-trip. Excludes scaffold and
    real surfaces (real surfaces are already in room://current)."""
    doc = await _get("/world")
    placed = [e for e in doc["entities"]
              if not (e.get("meta", {}).get("real") or e.get("meta", {}).get("scaffold"))]
    if not placed:
        return "No objects placed in the world yet."
    return ("Placed objects (reference these directly by id — no need to query the world):\n"
            + "\n".join(f"  - {_entity_line(e)}" for e in placed))


@mcp.tool()
async def set_immersion(mode: str) -> str:
    """Set how much real room vs. virtual the user sees:
    - virtual_room: passthrough off, the room's surfaces rendered (a virtual copy of the room).
    - ar: passthrough on, real room visible, surfaces hidden (mount/occlude against them).
    - mixed: passthrough on; then show_surface to reveal specific surfaces (e.g. a virtual ceiling).
    - authored: passthrough off, captured room hidden — use after build_room to show a built room.
    - vr_unbounded: ignore the room entirely; the original full synthetic VR space.
    """
    env = _IMMERSION.get(mode)
    if env is None:
        return f"Unknown mode {mode!r}. Use one of: {', '.join(_IMMERSION)}."
    await _post_patch([{"op": "env", "set": dict(env)}])
    return f"Immersion set to {mode}."


@mcp.tool()
async def realign_room() -> str:
    """Re-align the virtual room to the real room. Use when the user says the room looks misaligned,
    drifted, or shifted — e.g. after recentering with the Meta button, putting the headset down, or
    reloading. Re-captures the room at the current tracking origin. Only affects a headset in AR."""
    out = await _post("/room/realign", {})
    if not out.get("ok"):
        return f"Couldn't realign: {out.get('error', 'unknown error')}."
    return "Re-aligning the room to your real space — look around for a moment."


@mcp.tool()
async def reset_world() -> str:
    """Wipe the world back to the empty holodeck and start over — removes ALL placed objects, images,
    skybox, primitives, and any captured room. Use when the user asks to reset, clear everything, or
    start fresh. (A captured room re-appears on its own once they're back in AR.)"""
    out = await _post("/reset", {})
    if not out.get("ok"):
        return f"Couldn't reset: {out.get('error', 'unknown error')}."
    return "Reset to an empty holodeck — ready to build again."


@mcp.tool()
async def show_surface(target: str, visible: bool = True) -> str:
    """Show or hide real room surface(s) as virtual geometry. target: a surface id ('real_wall_1'),
    a semantic label ('wall', 'ceiling', 'floor', …), or 'all'. Use to build mixed real+virtual
    views (e.g. show only the ceiling)."""
    doc = await _get("/world")
    reals = [e for e in doc["entities"] if e.get("meta", {}).get("real")]
    t = target.lower()
    targets = [e for e in reals
               if t == "all" or e["id"] == target or e.get("meta", {}).get("semantic") == t
               or str(e.get("meta", {}).get("friendly_id")) == target]
    if not targets:
        return f"No room surface matches {target!r} (try query_room)."
    await _post_patch([{"op": "update", "id": e["id"], "set": {"components.material.visible": visible}}
                       for e in targets])
    return f"{'Showed' if visible else 'Hid'} {len(targets)} surface(s) matching {target!r}."


@mcp.tool()
async def texture_surface(target: str, image_id: str, repeat: Optional[float] = None) -> str:
    """Map a procured image onto room surface(s) — e.g. a starfield on the ceiling, grass on the
    floor, a mural on a wall. First call generate_image, then pass its image_id here.

    target: a surface id ('real_floor'), a semantic label ('floor', 'ceiling', 'wall'), or 'all'.
    repeat: tile the image NxN across the surface (e.g. 4) — for this, generate a SEAMLESS/tileable
        image (grass, brick). Omit to stretch a single copy (good for a starfield, sky, or mural).
    """
    out = await _post("/texture_surface", _body(target=target, image_id=image_id, repeat=repeat))
    if not out.get("ok"):
        return f"Couldn't texture {target!r}: {out.get('error', 'unknown error')}."
    return f"Mapped the image onto {out['count']} surface(s) ({target})."


@mcp.tool()
async def style_surface(target: str, color: Optional[str] = None, opacity: Optional[float] = None) -> str:
    """Color and/or set the transparency of room surface(s) — e.g. 'make the walls semi-transparent
    blue', 'make the ceiling glass', 'paint the floor red'.

    target: a surface id ('real_wall_3'), a semantic label ('wall'/'floor'/'ceiling'), or 'all'.
    color: CSS name or #hex. opacity: 0 (invisible) … 1 (solid); < 1 makes it see-through.
    (To map an image onto a surface, use texture_surface instead.)
    """
    out = await _post("/style_surface", _body(target=target, color=color, opacity=opacity))
    if not out.get("ok"):
        return f"Couldn't style {target!r}: {out.get('error', 'unknown error')}."
    return f"Styled {out['count']} surface(s) ({target})."


@mcp.tool()
async def show_annotations(on: bool = True, dimensions: bool = False) -> str:
    """Show or hide text labels floating on each room surface — each shows its name + short id (e.g.
    'window (12)'), which the user can reference (e.g. 'make 12 blue'). Turn on when the user wants to
    inspect/identify surfaces. dimensions: also show each surface's size (default off; turn on only if
    the user asks for sizes)."""
    await _post_patch([{"op": "env", "set": {"room.annotations": on, "room.annotationDims": dimensions}}])
    return f"Surface annotations {'on' if on else 'off'}{' with dimensions' if (on and dimensions) else ''}."


@mcp.tool()
async def style_annotations(color: Optional[str] = None, opacity: Optional[float] = None) -> str:
    """Restyle the surface annotation labels (the floating "<name> (id)" text) — e.g. 'make the labels
    yellow', 'make the labels faint'. color: CSS name or #hex. opacity: 0 (invisible) … 1 (solid).
    Affects all labels at once; use show_annotations to turn them on/off."""
    sets = {}
    if color is not None:
        sets["room.annotationColor"] = color
    if opacity is not None:
        sets["room.annotationOpacity"] = opacity
    if not sets:
        return "Nothing to change — pass a color and/or opacity."
    await _post_patch([{"op": "env", "set": sets}])
    return f"Annotation labels restyled ({', '.join(sets)})."


@mcp.tool()
async def show_edges(on: bool = True) -> str:
    """Show or hide the polygon outline drawn around every room surface (the bright wireframe of the
    real room). Edges are ON by default; turn them off for a cleaner passthrough view."""
    await _post_patch([{"op": "env", "set": {"room.edgesVisible": on}}])
    return f"Surface edges {'on' if on else 'off'}."


@mcp.tool()
async def style_edges(color: Optional[str] = None, opacity: Optional[float] = None) -> str:
    """Restyle the surface outline wireframe — e.g. 'make the edges green', 'make the outlines faint'.
    color: CSS name or #hex. opacity: 0 (invisible) … 1 (solid). Affects all surface edges at once;
    use show_edges to turn the outline on/off."""
    sets = {}
    if color is not None:
        sets["room.edgeColor"] = color
    if opacity is not None:
        sets["room.edgeOpacity"] = opacity
    if not sets:
        return "Nothing to change — pass a color and/or opacity."
    await _post_patch([{"op": "env", "set": sets}])
    return f"Surface edges restyled ({', '.join(sets)})."


@mcp.tool()
async def add_entity(
    shape: str,
    color: str = "white",
    position: Optional[list[float]] = None,
    scale: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
    name: Optional[str] = None,
) -> str:
    """Add a primitive entity to the world.

    shape: box | sphere | cylinder | cone | plane | torus | ...
    color: CSS color name or #hex.
    position/scale/rotation: [x, y, z] (meters; rotation in degrees). Defaults:
        position [0, 1, -3] (in front of the user), scale [1, 1, 1], rotation [0, 0, 0].
    name: explicit entity id; auto-generated if omitted.
    """
    pos = position or [0.0, 1.0, -3.0]
    eid = name or f"ent_{shape}_{uuid4().hex[:6]}"
    transform: dict[str, Any] = {"position": pos}
    if scale is not None:
        transform["scale"] = scale
    if rotation is not None:
        transform["rotation"] = rotation
    entity = {
        "id": eid,
        "transform": transform,
        "components": {"geometry": {"primitive": shape}, "material": {"color": color}},
    }
    patch = await _post_patch([{"op": "add", "entity": entity}])
    return f"Added entity {eid!r} ({shape}, {color}) at {pos}. World rev {patch['rev']}."


@mcp.tool()
async def place_asset(
    query: str,
    size_m: float,
    position: Optional[list[float]] = None,
    name: Optional[str] = None,
) -> str:
    """Place a real 3D model found by search — use this for real-world objects.

    query: what to find, e.g. 'oak tree', 'wooden chair', 'sports car', 'treasure chest'.
        Use add_entity (not this) only for basic primitive shapes like cube/sphere/cone.
    size_m: the object's REAL-WORLD largest dimension in METERS — use your world knowledge so
        scenes are to-scale. Examples: mug 0.1, house cat 0.5, dining chair 0.9, person 1.8,
        sofa 2.0, car 4.5, oak tree 7, giraffe 5, house 8. Be realistic.
    position: [x, y, z] meters; the object auto-sits on the floor (default [0, 0, -3]).
    name: explicit entity id; auto-generated if omitted.

    A placeholder appears immediately; the real model (scaled to size_m) swaps in once downloaded.
    """
    body: dict[str, Any] = {"query": query, "size_m": size_m}
    if position is not None:
        body["position"] = position
    if name is not None:
        body["name"] = name
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(f"{BASE}/place_asset", json=body)
        resp.raise_for_status()
        result = resp.json()
    if not result.get("ok"):
        return f"Couldn't place {query!r}: {result.get('error', 'no model found')}."
    return (
        f"Placed {result['title']!r} ({result.get('tris', '?')} tris) as {result['id']}. "
        f"{result.get('attribution', '')[:140]}"
    )


# --- Asset library: REUSE before creating anew ------------------------------------------------
# Before generating an image / fetching a model, you may search what's already been made. Reuse is
# always explicit (these tools) — never automatic. See the library policy in the system prompt.

@mcp.tool()
async def search_library(
    query: Optional[str] = None,
    image_id: Optional[str] = None,
    kind: Optional[str] = None,
) -> str:
    """Search assets already in the library to REUSE one instead of making a new one.

    Use when the user references something they likely made before ('the dragon from earlier', 'my
    castle'), or to check before creating when reuse would be natural. query: the text intent ('an
    oak tree'); OR image_id: an existing asset to find more like it ('more like that'). kind:
    optionally restrict to image | model | skybox | grounded_skybox.

    Returns candidates + a CONFIDENCE tier: 'strong' (an exact match or a user default — safe to
    reuse), 'weak' (only fuzzy/semantic hits — reuse only if clearly right, else offer or generate),
    'none' (nothing — generate/fetch fresh). Then reuse: a model via place_cached_asset(id), an image
    via place_image(image_id), a skybox via set_skybox(image_id)/set_grounded_skybox(image_id).
    """
    out = await _post("/library/search", _body(query=query, image_id=image_id, kind=kind))
    if not out.get("ok"):
        return f"Library search failed: {out.get('error', 'unknown error')}."
    cands, tier = out.get("candidates", []), out.get("confidence_tier", "none")
    if not cands:
        return "No matching asset in the library (confidence: none) — generate or fetch a new one."
    lines = [f"- {c['id']} ({c['kind']}, match={c['match']}): "
             f"{c.get('label') or c.get('prompt') or c.get('query') or '—'}"
             f"{(' [' + c['licence'] + ']') if c.get('licence') else ''}" for c in cands[:8]]
    return f"Library matches (confidence: {tier}):\n" + "\n".join(lines)


@mcp.tool()
async def place_cached_asset(
    id: str,
    size_m: Optional[float] = None,
    position: Optional[list[float]] = None,
    name: Optional[str] = None,
) -> str:
    """Place a MODEL already in the library by id (from search_library) — reuse, no web fetch.

    For reusing images use place_image(image_id); for skyboxes set_skybox/set_grounded_skybox. size_m:
    real-world largest dimension in metres (as in place_asset); position: [x,y,z] (auto-sits on floor).
    """
    out = await _post("/place_cached_asset", _body(id=id, size_m=size_m, position=position, name=name))
    if not out.get("ok"):
        return f"Couldn't reuse {id!r}: {out.get('error', 'unknown error')}."
    return f"Reused {out.get('title')!r} as {out['id']}."


@mcp.tool()
async def correct_asset(
    id: str,
    label: Optional[str] = None,
    query: Optional[str] = None,
    tags: Optional[str] = None,
    reject_for: Optional[str] = None,
) -> str:
    """Fix a wrong asset match so it doesn't recur (e.g. an X-wing came back for 'starship enterprise').

    label/query/tags: rewrite the asset's description to what it actually is. reject_for: a query this
    asset should NEVER match again (e.g. reject_for='starship enterprise' on that X-wing). Use when the
    user points out a mismatch.
    """
    out = await _post("/correct_asset",
                      _body(id=id, label=label, query=query, tags=tags, reject_for=reject_for))
    if not out.get("ok"):
        return f"Couldn't correct {id!r}: {out.get('error', 'unknown error')}."
    return "Fixed — updated the library so that won't recur."


# --- Image procurement (produce/transform an image, get back an image_id) ----------------------
# Procurement is decoupled from scene use: these make/transform an image and return an `image_id`
# you then pass to place_image / set_skybox. The `generator` arg is OPTIONAL — omit it to use the
# best default for the task (Gemini for most; OpenAI when you ask for transparency). Only name a
# generator if the user asked for a specific one, or you need a capability the default lacks (call
# list_image_generators if unsure what each supports).

@mcp.tool()
async def generate_image(
    prompt: str,
    aspect_ratio: Optional[str] = None,
    transparent: bool = False,
    generator: Optional[str] = None,
) -> str:
    """Generate a NEW image with AI and return its image_id (does NOT put it in the scene).

    Use for art, paintings, posters, photos, signs — anything pictorial. Then call place_image with
    the returned image_id to hang it. (For physical 3D objects use place_asset instead.)
    prompt: a vivid description, e.g. 'an oil painting of a red dragon over a castle'.
    aspect_ratio: e.g. '1:1', '16:9', '4:3' (the default generator supports any; some snap to fixed).
    transparent: set **true** whenever the user wants a transparent/clear background, a cut-out, a
        sticker, a decal, or "no background" — you MUST set this flag, NOT just describe it in the
        prompt. Routes to a generator that supports alpha.
    generator: optional — see the note above; omit to use the default.
    """
    out = await _post("/images/generate", _body(
        prompt=prompt, aspect_ratio=aspect_ratio, transparent=transparent, generator=generator))
    if not out.get("ok"):
        return f"Couldn't generate image: {out.get('error', 'unknown error')}."
    # Full provenance in the result (so the log shows which generator/model ran, dims, and alpha):
    return (f"Generated image_id={out['image_id']} via {out['provider']}/{out.get('model', '?')} "
            f"({out.get('w')}x{out.get('h')}, transparent={transparent}). "
            f"Call place_image with this image_id to hang it.")


@mcp.tool()
async def generate_skybox_image(prompt: str, generator: Optional[str] = None) -> str:
    """Generate a 360° equirectangular panorama image and return its image_id (does NOT apply it).

    Then call set_skybox with the returned image_id to wrap the scene. Use for the surrounding
    environment, e.g. 'a calm sunset beach', 'deep space with colorful nebulae', 'a misty pine
    forest at dawn'. generator: optional (omit to use the default).
    """
    out = await _post("/images/skybox", _body(prompt=prompt, generator=generator), timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't generate skybox image: {out.get('error', 'unknown error')}."
    return (f"Generated skybox image_id={out['image_id']} ({out['provider']}). "
            f"Call set_skybox with this image_id to wrap the scene.")


@mcp.tool()
async def generate_grounded_skybox_image(prompt: str, generator: Optional[str] = None) -> str:
    """Generate a 360° panorama for a GROUNDED skybox and return its image_id (does NOT apply it).

    Then call set_grounded_skybox with the returned image_id. Prefer this over generate_skybox_image
    when the user wants to STAND ON the scene's ground (a landscape they're standing in — 'put me in a
    meadow', 'stand me on the surface of Mars') rather than just be surrounded by a distant backdrop:
    its lower hemisphere is projected onto the floor at your feet. generator: optional.
    """
    out = await _post("/images/grounded_skybox", _body(prompt=prompt, generator=generator), timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't generate grounded skybox image: {out.get('error', 'unknown error')}."
    return (f"Generated grounded skybox image_id={out['image_id']} ({out['provider']}). "
            f"Call set_grounded_skybox with this image_id to wrap the scene.")


@mcp.tool()
async def edit_image(
    image_id: str,
    prompt: str,
    transparent: bool = False,
    generator: Optional[str] = None,
) -> str:
    """Edit a procured image (by image_id) and return a NEW image_id (does NOT change the scene).

    Use to derive a variant of an image you already have an id for. To change a picture already
    hanging in the scene, prefer edit_scene_image (one step). generator/transparent: optional.
    """
    out = await _post("/images/edit", _body(
        image_id=image_id, prompt=prompt, transparent=transparent, generator=generator))
    if not out.get("ok"):
        return f"Couldn't edit image: {out.get('error', 'unknown error')}."
    return f"Edited → image_id={out['image_id']} ({out['provider']})."


@mcp.tool()
async def outpaint_image(
    image_id: str,
    aspect: Optional[str] = None,
    prompt: Optional[str] = None,
    generator: Optional[str] = None,
) -> str:
    """Extend (outpaint) a procured image to a wider frame and return a NEW image_id (no scene effect).

    aspect: target frame like '16:9' (default) or '21:9'. To widen a picture already in the scene,
    prefer widen_scene_image. generator: optional.
    """
    out = await _post("/images/outpaint", _body(
        image_id=image_id, aspect=aspect, prompt=prompt, generator=generator))
    if not out.get("ok"):
        return f"Couldn't outpaint image: {out.get('error', 'unknown error')}."
    return f"Outpainted → image_id={out['image_id']} ({out['provider']})."


@mcp.tool()
async def skybox_from_image(image_id: str, generator: Optional[str] = None) -> str:
    """Turn a procured image (by image_id) into a 360° panorama and return a NEW image_id.

    Then call set_skybox with the returned image_id. To turn a picture already in the scene into the
    sky, prefer skybox_from_scene_image. generator: optional.
    """
    out = await _post("/images/skybox_from", _body(image_id=image_id, generator=generator), timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't build a skybox image: {out.get('error', 'unknown error')}."
    return (f"Built skybox image_id={out['image_id']} ({out['provider']}). "
            f"Call set_skybox with this image_id.")


@mcp.tool()
async def list_image_generators() -> str:
    """List the available image generators and what each can do (operations, edit mode, max
    resolution, aspect support, transparency), plus the default chosen per task. Call this only if
    unsure which generator a request needs — otherwise omit `generator` and trust the default."""
    out = await _get("/images/generators")
    if not out.get("ok") or not out.get("generators"):
        return "No image generators are configured."
    lines = []
    for g in out["generators"]:
        c = g["capabilities"]
        vendor = f" (vendor: {g['vendor']})" if g.get("vendor") else ""
        lines.append(f"- {g['name']}{vendor}: ops={','.join(c['operations'])}; edit={c['edit_mode']}; "
                     f"max={c['max_resolution']}px; aspect={c['aspect']}; "
                     f"transparency={c['transparency']}")
    lines.append(f"defaults: {out.get('defaults', {})}")
    lines.append("(You can pass a generator by its name or its vendor, e.g. 'Chat' or 'OpenAI'.)")
    return "\n".join(lines)


# --- Scene use of images (reference a procured image_id) ----------------------------------------

@mcp.tool()
async def place_image(
    image_id: str,
    position: Optional[list[float]] = None,
    size_m: Optional[float] = None,
    name: Optional[str] = None,
    on_surface: Optional[str] = None,
) -> str:
    """Hang a procured image (by image_id from generate_image/edit_image/...) as a painting.

    on_surface: hang it ON a real room surface — pass the surface's id ('real_wall_art_3'), its
        semantic+number ('wall art 18'), or just its number ('18'). The image is aligned to that
        surface (upright, parallel) and fitted to its frame automatically — USE THIS whenever the user
        says "put it in/on wall art N" or any specific surface (don't hand-compute position/rotation).
    position: [x, y, z] meters for a free-floating painting when NOT on a surface (default [0, 1.5, -3]).
    size_m: longest side in meters for the free-floating case (default 1.0; aspect preserved). Ignored
        when on_surface is given (the frame's size wins).
    name: pass an existing entity id to swap/move that painting; otherwise a new one is created.
    """
    out = await _post("/place_image", _body(
        image_id=image_id, position=position, size_m=size_m, name=name, on_surface=on_surface))
    if not out.get("ok"):
        return f"Couldn't place image: {out.get('error', 'unknown error')}."
    return f"Hung image {out['image_id']} as {out['id']}."


@mcp.tool()
async def set_skybox(image_id: str) -> str:
    """Wrap the whole scene in a procured image (by image_id from generate_skybox_image /
    skybox_from_image) as the surrounding sky/environment."""
    out = await _post("/set_skybox", _body(image_id=image_id))
    if not out.get("ok"):
        return f"Couldn't set the skybox: {out.get('error', 'unknown error')}."
    return "Wrapped the scene in that image as a 360° skybox."


@mcp.tool()
async def annotate_asset(
    id: str,
    note: Optional[str] = None,
    tags: Optional[str] = None,
    favorite: Optional[bool] = None,
    rating: Optional[int] = None,
    default_for: Optional[str] = None,
) -> str:
    """Record the user's OWN thoughts about an asset so it's easy to recall later (no scene change).

    Use when the user expresses a preference or memory about a specific asset (an image_id, or a
    model/skybox id from the library): 'remember this as my favorite city skybox' (note + favorite),
    'this is an important family photo' (note + rating), 'make this my default dog' (default_for='dog'
    — pins an alias so a later 'add a dog' reuses exactly this one). note: freeform text; tags:
    comma/space-separated keywords; favorite: bool; rating: 0–5; default_for: the word/phrase this
    asset should be the default for.
    """
    out = await _post("/annotate_asset", _body(id=id, note=note, tags=tags, favorite=favorite,
                                               rating=rating, default_for=default_for))
    if not out.get("ok"):
        return f"Couldn't annotate that asset: {out.get('error', 'unknown error')}."
    return "Noted — I'll remember that."


@mcp.tool()
async def set_grounded_skybox(
    image_id: str,
    height: Optional[float] = None,
    radius: Optional[float] = None,
) -> str:
    """Wrap the scene in a procured image (by image_id from generate_grounded_skybox_image) as a
    GROUNDED skybox — its lower half is projected onto the floor so the user stands ON the scene
    instead of floating above a distant horizon. Use the grounded image generated for this purpose.

    height (metres, default 1.6): the implied height the panorama was 'shot' from — RAISE it (e.g. 3, 6)
    if the user wants the ground to feel further below / to stand taller above it, LOWER it (e.g. 1) to
    sit closer to the ground. radius (metres, default 30): how far the projected ground extends before
    curving up to the horizon — INCREASE it (e.g. 60) for a wider open vista, decrease for an enclosed
    feel. Only pass these when the user asks about scale/height/distance; otherwise omit for the defaults.
    """
    out = await _post("/set_grounded_skybox", _body(image_id=image_id, height=height, radius=radius))
    if not out.get("ok"):
        return f"Couldn't set the grounded skybox: {out.get('error', 'unknown error')}."
    return "Wrapped the scene in that image as a grounded skybox — you're standing on it."


# --- One-shot scene edits (act on an image already in the scene, by entity id) ------------------
# Convenience over the procure→place flow for the common conversational case: these procure a new
# image from the entity's current one and apply it in a single call.

@mcp.tool()
async def edit_scene_image(id: str, prompt: str) -> str:
    """Edit an image ALREADY in the scene, in place — conversational editing.

    Use for changes to a picture already hanging, e.g. 'make the dragon blue', 'add a full moon',
    'make it nighttime'. id: the image entity id (in the Live context). One step (no image_id
    needed). Only works on images, not 3D models or the skybox.
    """
    out = await _post("/edit_image", {"id": id, "prompt": prompt})
    if not out.get("ok"):
        return f"Couldn't edit {id!r}: {out.get('error', 'unknown error')}."
    return f"Updated the image {id}."


@mcp.tool()
async def widen_scene_image(id: str, aspect: Optional[str] = None, prompt: Optional[str] = None) -> str:
    """Extend (outpaint) an image ALREADY in the scene to a wider frame, in place.

    Use for 'make the painting wider', 'show more of the landscape'. id: the image entity id (in the Live context). aspect: '16:9' (default) or '21:9'. prompt: optional guidance for the new area.
    """
    out = await _post("/outpaint_image", _body(id=id, aspect=aspect, prompt=prompt))
    if not out.get("ok"):
        return f"Couldn't widen {id!r}: {out.get('error', 'unknown error')}."
    return f"Extended the image {id}."


@mcp.tool()
async def skybox_from_scene_image(id: str) -> str:
    """Turn an image ALREADY in the scene into the surrounding 360° sky.

    Use for 'make that painting the sky', 'put me inside that scene'. id: the image entity id (in the Live context).
    """
    out = await _post("/skybox_from_image", {"id": id}, timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't build a skybox from {id!r}: {out.get('error', 'unknown error')}."
    return "Wrapped the scene in that image as a 360° skybox."


@mcp.tool()
async def move_entity(id: str, position: list[float]) -> str:
    """Move an entity to a new [x, y, z] position (meters)."""
    patch = await _post_patch([{"op": "update", "id": id, "set": {"transform.position": position}}])
    return f"Moved {id!r} to {position}. World rev {patch['rev']}."


@mcp.tool()
async def update_entity(
    id: str,
    color: Optional[str] = None,
    position: Optional[list[float]] = None,
    rotation: Optional[list[float]] = None,
    scale: Optional[list[float]] = None,
) -> str:
    """Update an entity's color and/or transform. Only provided fields change."""
    changes: dict[str, Any] = {}
    if color is not None:
        changes["components.material.color"] = color
    if position is not None:
        changes["transform.position"] = position
    if rotation is not None:
        changes["transform.rotation"] = rotation
    if scale is not None:
        changes["transform.scale"] = scale
    if not changes:
        return "No changes specified."
    patch = await _post_patch([{"op": "update", "id": id, "set": changes}])
    return f"Updated {id!r}: {changes}. World rev {patch['rev']}."


@mcp.tool()
async def remove_entity(id: str) -> str:
    """Remove an entity from the world."""
    patch = await _post_patch([{"op": "remove", "id": id}])
    return f"Removed {id!r}. World rev {patch['rev']}."


@mcp.tool()
async def set_environment(
    sky_color: Optional[str] = None,
    fog_color: Optional[str] = None,
    fog_density: Optional[float] = None,
) -> str:
    """Set environment properties: sky_color, fog_color (CSS/#hex), fog_density (0..1)."""
    changes: dict[str, Any] = {}
    if sky_color is not None:
        changes["sky.color"] = sky_color
    if fog_color is not None or fog_density is not None:
        fog: dict[str, Any] = {"type": "exponential"}
        if fog_color is not None:
            fog["color"] = fog_color
        if fog_density is not None:
            fog["density"] = fog_density
        changes["fog"] = fog
    if not changes:
        return "No environment changes specified."
    patch = await _post_patch([{"op": "env", "set": changes}])
    return f"Environment updated: {changes}. World rev {patch['rev']}."


def main() -> None:
    mcp.run()  # stdio transport


if __name__ == "__main__":
    main()
