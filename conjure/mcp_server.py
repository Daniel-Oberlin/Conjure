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


@mcp.tool()
async def place_image(
    prompt: str,
    position: Optional[list[float]] = None,
    size_m: Optional[float] = None,
    name: Optional[str] = None,
) -> str:
    """Generate an image with AI and hang it as a painting/poster facing the user.

    Use for art, paintings, posters, photos, signs — anything pictorial. (For physical 3D
    objects use place_asset instead.)
    prompt: a vivid description of the image, e.g. 'an oil painting of a red dragon over a castle'.
    position: [x, y, z] meters (default [0, 1.5, -3], eye height on the wall in front).
    size_m: width/height of the framed image in meters (default 1.0).
    name: explicit entity id; auto-generated if omitted.

    A placeholder frame appears immediately; the generated image swaps in once ready.
    """
    body: dict[str, Any] = {"prompt": prompt}
    if position is not None:
        body["position"] = position
    if size_m is not None:
        body["size_m"] = size_m
    if name is not None:
        body["name"] = name
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{BASE}/place_image", json=body)
        resp.raise_for_status()
        result = resp.json()
    if not result.get("ok"):
        return f"Couldn't generate image: {result.get('error', 'unknown error')}."
    return f"Generated and hung an image ({result.get('model', '?')}) as {result['id']}."


@mcp.tool()
async def edit_image(id: str, prompt: str) -> str:
    """Edit an existing in-world image (one placed by place_image) — conversational editing.

    Use for changes to a picture already in the scene, e.g. 'make the dragon blue', 'add a full
    moon', 'make it nighttime', 'turn it into winter'. The edit happens in place. If you don't
    know the image's id, call query_world first (images are listed as `image '<description>'`).
    Only works on images, not 3D models or the skybox.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{BASE}/edit_image", json={"id": id, "prompt": prompt})
        resp.raise_for_status()
        result = resp.json()
    if not result.get("ok"):
        return f"Couldn't edit {id!r}: {result.get('error', 'unknown error')}."
    return f"Updated the image {id}."


@mcp.tool()
async def set_skybox(prompt: str) -> str:
    """Wrap the whole scene in a generated 360° environment (the sky all around the user).

    prompt: the surroundings, e.g. 'a calm sunset beach', 'deep space with colorful nebulae',
        'a misty pine forest at dawn', 'inside a grand marble cathedral'. Use this to set the
        *environment*; use place_image for a flat framed picture.
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{BASE}/set_skybox", json={"prompt": prompt})
        resp.raise_for_status()
        result = resp.json()
    if not result.get("ok"):
        return f"Couldn't set the skybox: {result.get('error', 'unknown error')}."
    return f"Wrapped the scene in a generated skybox ({result.get('model', '?')})."


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
