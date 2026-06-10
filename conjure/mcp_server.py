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


@mcp.tool()
async def query_world() -> str:
    """Summarize the current world (entities + environment). Read this before editing."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{BASE}/world")
        resp.raise_for_status()
        doc = resp.json()
    lines = [
        f"World {doc.get('name', '')!r} (rev {doc['rev']}), {len(doc['entities'])} entities:"
    ]
    for e in doc["entities"]:
        comps = e.get("components", {})
        meta = e.get("meta", {})
        pos = e.get("transform", {}).get("position")
        if "gltf-model" in comps:
            desc = f"model {meta.get('title', '?')!r}"
        elif comps.get("material", {}).get("src"):
            desc = f"image {(meta.get('prompt') or meta.get('title') or '?')!r}"
        else:
            prim = comps.get("geometry", {}).get("primitive", "?")
            color = comps.get("material", {}).get("color", "?")
            desc = f"{prim} {color}"
        lines.append(f"  - {e['id']}: {desc} at {pos}")
    lines.append(f"environment: {doc.get('environment', {})}")
    return "\n".join(lines)


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
    transparent: true for a cut-out with a transparent background (e.g. a sticker/decal); routes to a
        generator that supports alpha.
    generator: optional — see the note above; omit to use the default.
    """
    out = await _post("/images/generate", _body(
        prompt=prompt, aspect_ratio=aspect_ratio, transparent=transparent, generator=generator))
    if not out.get("ok"):
        return f"Couldn't generate image: {out.get('error', 'unknown error')}."
    return (f"Generated image_id={out['image_id']} ({out['provider']}, {out.get('w')}x{out.get('h')}). "
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
        lines.append(f"- {g['name']}: ops={','.join(c['operations'])}; edit={c['edit_mode']}; "
                     f"max={c['max_resolution']}px; aspect={c['aspect']}; "
                     f"transparency={c['transparency']}")
    lines.append(f"defaults: {out.get('defaults', {})}")
    return "\n".join(lines)


# --- Scene use of images (reference a procured image_id) ----------------------------------------

@mcp.tool()
async def place_image(
    image_id: str,
    position: Optional[list[float]] = None,
    size_m: Optional[float] = None,
    name: Optional[str] = None,
) -> str:
    """Hang a procured image (by image_id from generate_image/edit_image/...) as a painting facing
    the user. position: [x, y, z] meters (default [0, 1.5, -3]). size_m: longest side in meters
    (default 1.0; the plane keeps the image's aspect). name: pass an existing entity id to swap its
    image in place; otherwise a new one is created.
    """
    out = await _post("/place_image", _body(
        image_id=image_id, position=position, size_m=size_m, name=name))
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


# --- One-shot scene edits (act on an image already in the scene, by entity id) ------------------
# Convenience over the procure→place flow for the common conversational case: these procure a new
# image from the entity's current one and apply it in a single call.

@mcp.tool()
async def edit_scene_image(id: str, prompt: str) -> str:
    """Edit an image ALREADY in the scene, in place — conversational editing.

    Use for changes to a picture already hanging, e.g. 'make the dragon blue', 'add a full moon',
    'make it nighttime'. id: the image entity id (find via query_world). One step (no image_id
    needed). Only works on images, not 3D models or the skybox.
    """
    out = await _post("/edit_image", {"id": id, "prompt": prompt})
    if not out.get("ok"):
        return f"Couldn't edit {id!r}: {out.get('error', 'unknown error')}."
    return f"Updated the image {id}."


@mcp.tool()
async def widen_scene_image(id: str, aspect: Optional[str] = None, prompt: Optional[str] = None) -> str:
    """Extend (outpaint) an image ALREADY in the scene to a wider frame, in place.

    Use for 'make the painting wider', 'show more of the landscape'. id: the image entity id (find
    via query_world). aspect: '16:9' (default) or '21:9'. prompt: optional guidance for the new area.
    """
    out = await _post("/outpaint_image", _body(id=id, aspect=aspect, prompt=prompt))
    if not out.get("ok"):
        return f"Couldn't widen {id!r}: {out.get('error', 'unknown error')}."
    return f"Extended the image {id}."


@mcp.tool()
async def skybox_from_scene_image(id: str) -> str:
    """Turn an image ALREADY in the scene into the surrounding 360° sky.

    Use for 'make that painting the sky', 'put me inside that scene'. id: the image entity id (find
    via query_world).
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
