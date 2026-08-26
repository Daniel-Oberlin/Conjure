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
from typing import Any, Literal, Optional
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from .config import DEFAULT_USER, scope_for

BASE = os.environ.get("CONJURE_URL", "http://localhost:8080")
# The catalog scope <user>/agents/<agent> — a CAPABILITY injected by the director at MCP-server launch
# (env), NOT an LLM tool argument. Every maintenance call carries it so the world server enforces scope.
SCOPE = os.environ.get("CONJURE_SCOPE", scope_for(DEFAULT_USER, "builder"))

# --- Hard tool gate (docs/specs/agents.md §4, Layer 2) -------------------------------------------
# The agent's tool allow-list + access level, injected as env by the director at launch (never an LLM
# arg). This MCP server is a SEPARATE process from the LLM/director, so enforcing here is a real second
# layer: it holds regardless of what the LLM was *offered* (director-side Layer 1), catching any call
# through this server — a future persona/agent-to-agent path, not just the model. `CONJURE_TOOLS` unset
# = no restriction (e.g. a standalone `python -m conjure.mcp_server`); set (even "") = enforce.
_raw_tools = os.environ.get("CONJURE_TOOLS")            # None = unset; "" = none; "a,b" = allow-list
_ALLOWED_TOOLS: Optional[set[str]] = None if _raw_tools is None else set(filter(None, _raw_tools.split(",")))
_ACCESS = os.environ.get("CONJURE_ACCESS", "all")       # "all" | "read"
# Read-only tools: everything else is treated as mutating (safe default — a NEW tool is denied to a
# read-only agent until it's classified here). `access: "read"` allows only these.
_READONLY_TOOLS = {"query_world", "query_room", "view_relative", "list_worlds",
                   "list_image_generators", "search_library", "query_assets"}


def _tool_denied(name: str) -> Optional[str]:
    """Return a deny message if the agent's capability forbids calling `name`, else None. Enforced in
    `_GatedMCP.call_tool` below — a hard, out-of-LLM-process gate."""
    if name == "set_caller":
        return None                                        # control tool (director-only): never gated
    if _ALLOWED_TOOLS is not None and name not in _ALLOWED_TOOLS:
        return f"error: tool {name!r} is not permitted for this agent (out of its tool scope)"
    if _ACCESS == "read" and name not in _READONLY_TOOLS:
        return f"error: tool {name!r} mutates state, but this agent has read-only access"
    return None


class _GatedMCP(FastMCP):
    """FastMCP with the capability gate on tool dispatch. `_setup_handlers` (in __init__) registers this
    overridden `call_tool` as the handler, so every tool call is checked here before it runs — no
    monkeypatching, no per-tool decorator."""

    async def call_tool(self, name, arguments):
        deny = _tool_denied(name)
        if deny is not None:
            return [TextContent(type="text", text=deny)]
        return await super().call_tool(name, arguments)


mcp = _GatedMCP("conjure-world")


async def _post_patch(ops: list[dict[str, Any]], origin: str = "director") -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(f"{BASE}/patch", json={"origin": origin, "ops": ops}, headers=_headers())
        if resp.status_code == 403:                  # owner-only write refused (a non-owner speaker) — RAISE
            # so every patch tool surfaces the reason (they read patch['rev'] on success, and the
            # fire-and-forget ones would otherwise report a false success). The message reaches the LLM as
            # the tool error.
            raise PermissionError(resp.json().get("error", "forbidden"))
        resp.raise_for_status()
        return resp.json()


def _body(**kw) -> dict[str, Any]:
    """Drop None-valued keys so optional params aren't sent."""
    return {k: v for k, v in kw.items() if v is not None}


_USER = SCOPE.split("/", 1)[0]   # launch identity (fallback until the director sets the per-turn speaker)

# The identity subsequent calls act as, sent on every world-server request (owner gate + asset-ownership
# scope). Defaults to the launch (user, scope); the director overrides it PER TURN via set_caller() so a
# shared session attributes each turn to its actual SPEAKER — docs/specs/agents.md §8.5. Turns are
# serialized (single floor), so a process-global caller is safe against interleaving.
_CALLER = {"user": _USER, "scope": SCOPE}


def _headers() -> dict[str, str]:
    return {"X-Conjure-User": _CALLER["user"], "X-Conjure-Scope": _CALLER["scope"]}


def _scope() -> str:
    """The current caller's catalog scope (`<user>/agents/<agent>`) — the per-turn speaker set by
    set_caller, else the launch scope. Tools pass this in request BODIES for scoped reads/writes (which
    worlds are 'yours', asset ownership), the counterpart to the identity HEADERS from `_headers()`."""
    return _CALLER["scope"]


@mcp.tool()
async def set_caller(user: str, scope: str) -> str:
    """[control — the director calls this, not the LLM] Set the identity subsequent tool calls act as: the
    current turn's SPEAKER. Not in any agent's tool allow-list (never offered to the model) and exempt from
    the capability gate. Lets one shared MCP server attribute each turn to whoever spoke, so the world
    server enforces ownership/permissions per-speaker (docs/specs/agents.md §8.5)."""
    _CALLER["user"] = user or _USER
    _CALLER["scope"] = scope or SCOPE
    return "ok"


async def _post(path: str, body: dict[str, Any], timeout: float = 150.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{BASE}{path}", json=body, headers=_headers())
        if resp.status_code == 403:                  # owner-only write refused → report it, don't crash
            return {"ok": False, "error": resp.json().get("error", "forbidden")}
        resp.raise_for_status()
        return resp.json()


def _notice(out: dict) -> str:
    """Suffix for a public-uses-public notice the server attached (e.g. 'published your private asset…')."""
    n = out.get("notice")
    return f" {n}" if n else ""


def _gen_info(out: dict) -> str:
    """Provenance for a generated/edited image result (logged + shown to the LLM): which generator/
    model produced it and at what size."""
    dims = f" ({out['w']}x{out['h']})" if out.get("w") and out.get("h") else ""
    return f"{out.get('provider', '?')}/{out.get('model', '?')}{dims}"


async def _get(path: str, timeout: float = 10.0) -> dict:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(f"{BASE}{path}")
        resp.raise_for_status()
        return resp.json()


def _entity_line(e: dict) -> str:
    """'<id>: <what it is> at <pos>' — shared by query_world and the world://current resource.

    Neither caller passes a REAL room surface: `world://current` filters them out and `query_world`
    collapses them (`_real_surfaces_line`). The guard below only keeps a stray third caller honest."""
    comps = e.get("components", {})
    meta = e.get("meta", {})
    pos = e.get("transform", {}).get("position")
    if meta.get("real"):
        desc = f"REAL {meta.get('semantic', 'surface')} (room surface — see the room summary)"
    elif "gltf-model" in comps:
        aid = meta.get("asset_id") or comps["gltf-model"].rsplit("/", 1)[-1]
        desc = f"model {meta.get('title', '?')!r} [asset {aid}]"
    elif comps.get("material", {}).get("src"):
        aid = meta.get("image_id") or comps["material"]["src"].rsplit("/", 1)[-1]
        desc = f"image {(meta.get('prompt') or meta.get('title') or '?')!r} [asset {aid}]"
    else:
        prim = comps.get("geometry", {}).get("primitive", "?")
        color = comps.get("material", {}).get("color", "?")
        desc = f"{prim} {color}"
    return f"{e['id']}: {desc} at {pos}"


def _env_line(env: dict) -> str:
    """One-line summary of the scene's ENVIRONMENT state that isn't an entity — the skybox, sky color, and
    fog. Included in world://current so the director actually SEES a skybox is set (it lives in
    environment.sky, not as a placed entity, so it never showed in the object list before)."""
    sky = env.get("sky") or {}
    bits = []
    if sky.get("src"):
        kind = "grounded skybox" if sky.get("grounded") else "skybox"
        bits.append(f"{kind} image {sky['src']} (remove/replace it with set_environment(sky_color=...))")
    elif sky.get("color"):
        bits.append(f"plain sky color {sky['color']}")
    if env.get("fog"):
        bits.append(f"fog {env['fog']}")
    return ("Environment: " + "; ".join(bits)) if bits else ""


def _real_surfaces_line(reals: list[dict]) -> str:
    """The single line every REAL room surface collapses to in a world dump.

    A dump used to spend one line each on these, which in a captured room is most of it — measured at
    59 of 73 entities and **87% of the characters**, for lines carrying a semantic label, a position,
    and nothing else. The cost wasn't the worst of it: those lines READ as complete. Every entity in
    the dump has the same `id: description at pos` shape, so "this floor has no colour" and "this line
    doesn't show colour" are indistinguishable — and an agent that wants a colour concludes the world
    doesn't store one. It does: `room://current` carries every surface's id, position, colour and
    visibility, and it's already in the prompt of any agent that cares (observed 2026-08-26 — the
    director read this dump, saw no colours, and reported that surface colours aren't stored, with the
    real answer sitting in its own context).

    So: a count, the per-kind tally the room summary can't give without counting 59 lines, and a
    pointer to where the detail lives. One line can't be mistaken for a full description."""
    kinds: dict[str, int] = {}
    for e in reals:
        k = (e.get("meta") or {}).get("semantic") or "surface"
        kinds[k] = kinds.get(k, 0) + 1
    tally = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"{len(reals)} REAL room surfaces ({tally}) — NOT listed here. Each one's id, position, "
            f"colour and visibility are in the room summary, already in your context. Restyle/hide/"
            f"mount them; don't move or remove them.")


@mcp.tool()
async def query_world() -> str:
    """Full dump of the PLACED scene (every non-room entity + the environment). RARELY needed — your
    placed objects are already in the Live context each turn; use this only for detail the summary
    omits, or a very large scene.

    Real room surfaces are **summarised in one line, not listed** — their per-surface detail (colour,
    visibility, position) is in the room summary, which is richer than anything this dump ever showed
    for them."""
    doc = await _get("/world")
    ents = doc["entities"]
    reals = [e for e in ents if (e.get("meta") or {}).get("real")]
    lines = [f"World {doc.get('name', '')!r} (rev {doc['rev']}), {len(ents)} entities:"]
    lines += [f"  - {_entity_line(e)}" for e in ents if not (e.get("meta") or {}).get("real")]
    if reals:
        lines.append(f"  - {_real_surfaces_line(reals)}")
    lines.append(f"environment: {doc.get('environment', {})}")
    return "\n".join(lines)


# --- Room model (AR / scene understanding) — see docs/specs/worlds-surfaces.md -----------------------------

_IMMERSION = {
    "virtual_room": {"passthrough": False, "spacePresentation.active": True, "spacePresentation.defaultSurfaceVisible": True},
    "ar":           {"passthrough": True,  "spacePresentation.active": True, "spacePresentation.defaultSurfaceVisible": False},
    "mixed":        {"passthrough": True,  "spacePresentation.active": True},
    "authored":     {"passthrough": False, "spacePresentation.active": True, "spacePresentation.defaultSurfaceVisible": False},
    "vr_unbounded": {"passthrough": False, "spacePresentation.active": False, "spacePresentation.defaultSurfaceVisible": False},
}


async def _room_summary() -> str:
    """Text summary of the real room: surfaces (by semantic + short id) + the boundary. Shared by the
    query_room tool and the `room://current` resource (which agents inject into their prompt each turn,
    so they needn't call query_room just to see surfaces)."""
    doc = await _get("/world")
    env = doc.get("environment", {})
    pres = env.get("spacePresentation", {})
    reals = [e for e in doc["entities"] if e.get("meta", {}).get("real")]
    if not pres.get("active") or not reals:
        return "No room model yet — the headset hasn't shared one (capture the room, or work in VR)."
    lines = [f"Room: {len(reals)} surfaces · passthrough={env.get('passthrough', False)} · "
             f"surfaces-visible-by-default={pres.get('defaultSurfaceVisible', False)}"]
    b = env.get("boundary")
    if b:
        lines.append(f"boundary: height {b.get('height')}m, floor polygon {b.get('floorPolygon')}")
    for e in reals:
        m = e.get("meta", {})
        mat = e.get("components", {}).get("material", {})
        vis = mat.get("visible", pres.get("defaultSurfaceVisible", False))
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
    """Live summary of the virtual scene you've built — PLACED objects (models/images) plus the
    ENVIRONMENT (skybox / sky color / fog). Injected each turn so the builder references them without a
    query_world round-trip. Excludes scaffold and real surfaces (those are in room://current). The skybox
    lives in the environment, not as an object, so it's reported on its own line — check here before
    telling the user there's no skybox to change/remove."""
    doc = await _get("/world")
    placed = [e for e in doc["entities"]
              if not (e.get("meta", {}).get("real") or e.get("meta", {}).get("scaffold"))]
    lines = (["Placed objects (reference these directly by id — no need to query the world):"]
             + [f"  - {_entity_line(e)}" for e in placed]) if placed else ["No objects placed in the world yet."]
    envline = _env_line(doc.get("environment", {}))
    if envline:
        lines.append(envline)
    return "\n".join(lines)


@mcp.resource("dynamics://available")
async def dynamics_resource() -> str:
    """The dynamic modules the active agent may conjure, as a one-line-per-module catalog
    (`name — description; params: k(default)…`). Injected each turn into agents that list
    `dynamics://available` in their context (docs/specs/dynamics.md §9), so the
    director discovers its scoped modules with no ritual and knows the params each accepts for
    conjure_module. The world server scopes the catalog to the active agent + enforces it on /module."""
    out = await _get("/dynamics/available")
    catalog = (out.get("catalog") if isinstance(out, dict) else "") or ""
    if not catalog.strip():
        return "Dynamic modules (conjure_module): none available to you."
    return ("Dynamic modules you can conjure (conjure_module module=<name>, config=<params>); "
            "dismiss with dismiss_module:\n" + catalog)


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
    return f"Mapped the image onto {out['count']} surface(s) ({target})." + _notice(out)


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
    await _post_patch([{"op": "env", "set": {"spacePresentation.annotations": on, "spacePresentation.annotationDims": dimensions}}])
    return f"Surface annotations {'on' if on else 'off'}{' with dimensions' if (on and dimensions) else ''}."


@mcp.tool()
async def style_annotations(color: Optional[str] = None, opacity: Optional[float] = None) -> str:
    """Restyle the surface annotation labels (the floating "<name> (id)" text) — e.g. 'make the labels
    yellow', 'make the labels faint'. color: CSS name or #hex. opacity: 0 (invisible) … 1 (solid).
    Affects all labels at once; use show_annotations to turn them on/off."""
    sets = {}
    if color is not None:
        sets["spacePresentation.annotationColor"] = color
    if opacity is not None:
        sets["spacePresentation.annotationOpacity"] = opacity
    if not sets:
        return "Nothing to change — pass a color and/or opacity."
    await _post_patch([{"op": "env", "set": sets}])
    return f"Annotation labels restyled ({', '.join(sets)})."


@mcp.tool()
async def show_edges(on: bool = True) -> str:
    """Show or hide the polygon outline drawn around every room surface (the bright wireframe of the
    real room). Edges are ON by default; turn them off for a cleaner passthrough view."""
    await _post_patch([{"op": "env", "set": {"spacePresentation.edgesVisible": on}}])
    return f"Surface edges {'on' if on else 'off'}."


@mcp.tool()
async def style_edges(color: Optional[str] = None, opacity: Optional[float] = None) -> str:
    """Restyle the surface outline wireframe — e.g. 'make the edges green', 'make the outlines faint'.
    color: CSS name or #hex. opacity: 0 (invisible) … 1 (solid). Affects all surface edges at once;
    use show_edges to turn the outline on/off."""
    sets = {}
    if color is not None:
        sets["spacePresentation.edgeColor"] = color
    if opacity is not None:
        sets["spacePresentation.edgeOpacity"] = opacity
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
    placement: Literal["grounded", "free"] = "grounded",
) -> str:
    """Place a real 3D model found by search — use this for real-world objects.

    query: what to find, e.g. 'oak tree', 'wooden chair', 'sports car', 'treasure chest'.
        Use add_entity (not this) only for basic primitive shapes like cube/sphere/cone.
    size_m: the object's REAL-WORLD largest dimension in METERS — use your world knowledge so
        scenes are to-scale. Examples: mug 0.1, house cat 0.5, dining chair 0.9, person 1.8,
        sofa 2.0, car 4.5, oak tree 7, giraffe 5, house 8. Be realistic.
    position: [x, y, z] meters (default [0, 0, -3]).
    name: explicit entity id; auto-generated if omitted.
    placement: 'grounded' (default) sits it on the floor and keeps it upright — use for furniture,
        trees, anything resting on the ground. 'free' keeps it exactly at `position`, so use it for
        anything floating or up high (a hanging lamp, a bird, a cloud, an object on a shelf/table).

    A placeholder appears immediately; the real model (scaled to size_m) swaps in once downloaded.
    """
    body: dict[str, Any] = {"query": query, "size_m": size_m, "placement": placement}
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
    out = await _post("/library/search", _body(query=query, image_id=image_id, kind=kind, scope=_scope()))
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
    placement: Literal["grounded", "free"] = "grounded",
) -> str:
    """Place a MODEL already in the library by id (from search_library) — reuse, no web fetch.

    For reusing images use place_image(image_id); for skyboxes set_skybox/set_grounded_skybox. size_m:
    real-world largest dimension in metres (as in place_asset); position: [x,y,z]. placement: 'grounded'
    (default) sits on the floor, upright; 'free' keeps it exactly at `position` (floating / up high).
    """
    out = await _post("/place_cached_asset", _body(id=id, size_m=size_m, position=position, name=name,
                                                   placement=placement))
    if not out.get("ok"):
        return f"Couldn't reuse {id!r}: {out.get('error', 'unknown error')}."
    return f"Reused {out.get('title')!r} as {out['id']}." + _notice(out)


@mcp.tool()
async def view_relative(direction: str = "forward", distance: float = 1.0) -> str:
    """Resolve a point relative to where the USER is and looking RIGHT NOW (their live headset pose),
    and report what's there. Use this whenever the user refers to space from their own viewpoint —
    'in front of me', 'behind me', 'to my left/right', 'above/below me', 'the wall I'm looking at'.

    direction: forward | back | left | right | up | down. `forward` is the actual look direction
    (includes looking up/down); left/right/up/down are relative to the head. distance: metres along it
    (default 1).

    Returns a world `point` you pass straight to a place tool's `position` (e.g. place_cached_asset/
    place_image/place_asset) — DON'T hand-compute it. Also returns `surface` (the nearest room surface
    the ray hits — style/texture it by its id) and `nearby` placed objects. Needs the user connected
    with a live view (an active session)."""
    out = await _post("/view_relative", _body(direction=direction, distance=distance))
    if not out.get("ok"):
        return f"Couldn't resolve that view: {out.get('error', 'unknown error')}."
    p = out["point"]
    lines = [f"Point {out['distance']} m {out['direction']} of the user: "
             f"[{p[0]:.2f}, {p[1]:.2f}, {p[2]:.2f}] — use as `position` to place there."]
    s = out.get("surface")
    lines.append(
        f"Surface that way: {s.get('semantic')} #{s.get('friendly_id')} (id {s['id']}), "
        f"{s['distance']:.2f} m away — target it by id to style/texture it." if s
        else "No room surface that way within reach.")
    nb = out.get("nearby") or []
    if nb:
        lines.append("Nearby objects: " + ", ".join(f"{n.get('title') or n['id']} ({n['distance']:.1f} m)" for n in nb))
    return "\n".join(lines)


# --- Catalog maintenance: inspect / update / delete library assets ----------------------------
# query_assets reads (read-only SQL, scoped to you); update_asset is the single writer (fields,
# kind, "default for X" alias, reject-for-a-query); delete_asset removes one. All are scoped to your
# own assets. Use these for fixing the library ("relabel that x-wing", "make this my default dog",
# "delete the duplicate", "how many transparent images do I have").

@mcp.tool()
async def query_assets(sql: str) -> str:
    """Run a READ-ONLY SQL query over your asset catalog (SELECT or PRAGMA only) — for inspecting,
    counting, or finding assets to fix. You only see your own assets.

    The main table is `assets` (columns include: id, kind, source, label, prompt, query, params_json,
    provider, model, width, height, licence, attribution, notes, tags, rating, favorite, embed_model,
    created_at, last_used, use_count). Use `PRAGMA table_info(assets)` to list columns. Examples:
    "SELECT kind, COUNT(*) FROM assets GROUP BY kind"; "SELECT id, label FROM assets WHERE label IS NULL".
    """
    out = await _post("/query_assets", _body(sql=sql, scope=_scope()))
    if not out.get("ok"):
        return f"Query failed: {out.get('error', 'unknown error')}."
    rows = out.get("rows", [])
    if not rows:
        return "0 rows."
    head = list(rows[0].keys())
    lines = [" | ".join(head)] + [" | ".join(str(r.get(c, "")) for c in head) for r in rows[:50]]
    return f"{len(rows)} row(s):\n" + "\n".join(lines)


@mcp.tool()
async def update_asset(
    id: str,
    label: Optional[str] = None,
    query: Optional[str] = None,
    tags: Optional[str] = None,
    notes: Optional[str] = None,
    kind: Optional[str] = None,
    rating: Optional[int] = None,
    favorite: Optional[bool] = None,
    public: Optional[bool] = None,
    default_for: Optional[str] = None,
    reject_for: Optional[str] = None,
) -> str:
    """Update a library asset (one tool for all catalog fixes/curation). Pass only what you want to change.
    You can only change your OWN assets (one in your library scope).

    label/query/tags/notes: the asset's description + keywords + freeform note. kind: re-tag it
    (image | model | skybox | grounded_skybox | audio | photo) — e.g. mark a skybox as grounded.
    rating (0–5)/favorite: your rating. public: catalog visibility — assets are PUBLIC by default (others
    on this server can discover and reuse them); set public=False to make one private (only you can find
    it), or public=True to share it again. default_for: make this the default for a phrase ('dog' →
    reused when the user says 'add a dog'). reject_for: a query this asset should NEVER match again
    (e.g. an x-wing wrongly returned for 'starship enterprise'). Covers 'remember this as my favorite',
    'make this my default dog', 'relabel that', 'reject it for X', 'make that image private'.
    """
    out = await _post("/update_asset", _body(
        id=id, scope=_scope(), label=label, query=query, tags=tags, notes=notes, kind=kind,
        rating=rating, favorite=favorite, public=public, default_for=default_for, reject_for=reject_for))
    if not out.get("ok"):
        return f"Couldn't update {id!r}: {out.get('error', 'unknown error')}."
    return "Updated."


@mcp.tool()
async def delete_asset(id: str) -> str:
    """Delete an asset from your library catalog (its entry, aliases, and search index). The cached
    file is left on disk. Use to remove a bad or duplicate asset ('delete that duplicate woman model')."""
    out = await _post("/delete_asset", _body(id=id, scope=_scope()))
    if not out.get("ok"):
        return f"Couldn't delete {id!r}: {out.get('error', 'unknown error')}."
    return f"Deleted {id} from the library."


# --- Worlds (your own scoped, named, nestable worlds) ------------------------------------------
# Each world is a separate holodeck you can build up, save, and return to. Names can be hierarchical
# ('castle-quest/dining-hall') to organize them. Recall is forgiving — case, spaces, underscores and
# hyphens don't matter — but you should list_worlds first and match the user's words to a real name.

@mcp.tool()
async def list_worlds() -> str:
    """List the worlds in your CURRENT SESSION (and which is active). Call this before switching so you
    match the user's description ('the dining hall') to a real world. You only know the worlds in your own
    session — other sessions, agents, and other users' worlds aren't yours to list or switch; a person
    reaches those from the shell, not you.

    Each world has a permanent **id** and a **name** the user can change. If you record a world anywhere
    that outlives this turn — notably `state_set` — store the **id**: the name may be different next
    time, the id never is."""
    out = await _post("/worlds/list", _body(scope=_scope()))
    entries = out.get("worlds", [])
    active = out.get("active")        # the caller's OWN live world (an id), or None when the live world is theirs
    current = out.get("current")      # the true live (shared) world {owner, id, name}
    caller = _scope().split("/", 1)[0]
    lines = []
    if entries:
        lines.append("Your worlds (id — name):")
        lines += [f"  {'* ' if e['id'] == active else '  '}{e['id']} — {e['name']}" for e in entries]
        if any(e["id"] == active for e in entries):
            lines.append("(* = currently active. Store the id, not the name, if you need to remember one.)")
    else:
        lines.append("You have no saved worlds yet.")
    if current and current.get("owner") != caller:    # a person visited another user's session → you're a guest
        lines.append(f"\nYou're currently in {current['owner']}'s world '{current['name']}' "
                     f"(shared — you can be here but can't change it; it's theirs).")
    return "\n".join(lines)


@mcp.tool()
async def new_world(name: str, public: bool = True, outdoor: bool = False) -> str:
    """Create a new, empty world and switch to it. `name` may be hierarchical to organize worlds
    ('castle-quest/dining-hall'). The new world starts from your agent's default setup. Worlds are
    PUBLIC by default (others can discover and visit them); pass public=False to create a PRIVATE world
    only you can see and enter. Pass outdoor=True for an OUTDOOR/void world — no room geometry, just a
    skybox + placed objects (use for 'a world set outdoors', 'floating in space', 'on a beach'); it's not
    tied to a captured room and holds its orientation on its own."""
    out = await _post("/worlds/new", _body(name=name, scope=_scope(), public=public, outdoor=outdoor))
    if not out.get("ok"):
        return f"Couldn't create {name!r}: {out.get('error', 'unknown error')}."
    kind = "outdoor " if outdoor else ""
    return f"Created and switched to {kind}'{out.get('world', name)}' ({'public' if public else 'private'})."


@mcp.tool()
async def set_world_visibility(public: bool, name: Optional[str] = None) -> str:
    """Make a world public or private. Worlds are PUBLIC by default — others can discover them
    (list_worlds) and visit them. Set public=False to make one PRIVATE: only you can see or enter it.
    Defaults to your CURRENT world ('make this private'); pass `name` to target another of your worlds.
    You can only change the visibility of worlds you own."""
    out = await _post("/worlds/visibility", _body(public=public, scope=_scope(), name=name))
    if not out.get("ok"):
        return f"Couldn't change visibility: {out.get('error', 'unknown error')}."
    pub = out.get("published_assets") or []
    extra = (f" Also published {len(pub)} private asset(s) it uses so visitors can see the whole scene: "
             f"{', '.join(pub)}." if pub else "")
    return f"World '{out.get('world', name or 'current')}' is now {'public' if public else 'private'}." + extra


@mcp.tool()
async def set_space_visibility(public: bool, name: Optional[str] = None) -> str:
    """Make a physical SPACE public or private. A space is the real room your worlds are anchored in; it's
    shared, so anyone co-located can join. Spaces are PUBLIC by default: any co-located user may build their
    OWN worlds in it. Set public=False to make it PRIVATE — then only you can create NEW worlds in it
    (existing worlds are unaffected; joining/viewing still follows each WORLD's visibility). Defaults to your
    CURRENT space; pass `name` to target another space you own. You can only change spaces you own."""
    out = await _post("/space/visibility", _body(public=public, scope=_scope(), name=name))
    if not out.get("ok"):
        return f"Couldn't change space visibility: {out.get('error', 'unknown error')}."
    return f"Space '{out.get('space', name or 'current')}' is now {'public' if public else 'private'}."


@mcp.tool()
async def switch_world(name: str) -> str:
    """Switch to one of YOUR worlds in this session — by **id** (preferred, it never changes) or by its
    current name. Saves the current world first, bringing everyone
    present along. Match `name` to a real world from list_worlds; formatting/case needn't be exact.
    (Visiting another user's world is a person's action at the shell — not something you do here.)"""
    out = await _post("/worlds/switch", _body(name=name, scope=_scope()))
    if not out.get("ok"):
        return f"Couldn't switch to {name!r}: {out.get('error', 'unknown error')}."
    return f"Switched to '{out.get('world', name)}'."


@mcp.tool()
async def delete_world(name: str) -> str:
    """Delete one of your worlds permanently. You can't delete the world you're currently in — switch
    away first."""
    out = await _post("/worlds/delete", _body(name=name, scope=_scope()))
    if not out.get("ok"):
        return f"Couldn't delete {name!r}: {out.get('error', 'unknown error')}."
    return f"Deleted world '{name}'."


# --- Image procurement (produce/transform an image, get back an image_id) ----------------------
# Procurement is decoupled from scene use: these make/transform an image and return an `image_id`
# you then pass to place_image / set_skybox. The `generator` arg selects WHICH image generator runs.
# Omit it to use the best default for the task (Gemini for most; OpenAI when you ask for transparency).
# BUT if the user names one for the image — "use Grok", "make it with OpenAI", "have Gemini do it" —
# you MUST pass that name as `generator` (casual name OR vendor, e.g. 'Grok'/'xai', 'Chat'/'OpenAI').
# This is separate from which director LLM is talking: "use Grok for the picture" means generator='Grok',
# even if Grok is already the active director. Call list_image_generators if unsure what each supports.

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
    generator: which generator to use — see the note above. Omit for the default; pass the user's
        named choice (e.g. 'Grok', 'OpenAI') whenever they ask for a specific one.
    """
    out = await _post("/images/generate", _body(
        prompt=prompt, aspect_ratio=aspect_ratio, transparent=transparent, generator=generator))
    if not out.get("ok"):
        return f"Couldn't generate image: {out.get('error', 'unknown error')}."
    # Full provenance in the result (so the log shows which generator/model ran, dims, and alpha):
    return (f"Generated image_id={out['image_id']} via {_gen_info(out)}, transparent={transparent}. "
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
    return (f"Generated skybox image_id={out['image_id']} via {_gen_info(out)}. "
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
    return (f"Generated grounded skybox image_id={out['image_id']} via {_gen_info(out)}. "
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
    return f"Edited → image_id={out['image_id']} via {_gen_info(out)}, transparent={transparent}."


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
    return f"Outpainted → image_id={out['image_id']} via {_gen_info(out)} (aspect {aspect or '16:9'})."


@mcp.tool()
async def skybox_from_image(image_id: str, generator: Optional[str] = None) -> str:
    """Turn a procured image (by image_id) into a 360° panorama and return a NEW image_id.

    Then call set_skybox with the returned image_id. To turn a picture already in the scene into the
    sky, prefer skybox_from_scene_image. generator: optional.
    """
    out = await _post("/images/skybox_from", _body(image_id=image_id, generator=generator), timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't build a skybox image: {out.get('error', 'unknown error')}."
    return (f"Built skybox image_id={out['image_id']} via {_gen_info(out)}. "
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
    billboard: bool = False,
    stereo: Optional[str] = None,
    stretch: bool = False,
) -> str:
    """Hang a procured image (by image_id from generate_image/edit_image/...) as a painting.

    on_surface: hang it ON a real room surface — pass the surface's id ('real_wall_art_3'), its
        semantic+number ('wall art 18'), or just its number ('18'). The image is aligned to that
        surface (upright, parallel) and fitted INSIDE its frame automatically, keeping the picture's
        aspect ratio — USE THIS whenever the user says "put it in/on wall art N" or any specific surface
        (don't hand-compute position/rotation).
    stretch: only with on_surface — fill the ENTIRE surface, stretching the image to the frame's shape
        (its aspect ratio is NOT preserved). Off by default (fit-inside, aspect-correct). Use only when
        the user explicitly asks to fill/cover/stretch to the whole surface.
    position: [x, y, z] meters for a free-floating painting when NOT on a surface (default [0, 1.5, -3]).
    size_m: longest side in meters for the free-floating case (default 1.0; aspect preserved). Ignored
        when on_surface is given (the frame's size wins).
    name: pass an existing entity id to swap/move that painting; otherwise a new one is created.
    billboard: True makes a FREE-STANDING image always turn to face each viewer (yaw-only, stays
        upright) — good for a floating picture you walk around. Cannot combine with on_surface (a
        surface-hung image stays flush to its wall). Use it when the user says "always face me" or similar.
    stereo: 'sbs' (side-by-side) or 'tb' (top-bottom) to render a packed 3D stereo pair with real
        per-eye depth in the headset. Almost always leave this OFF: imported stereo photos already carry
        it and it's applied automatically. ONLY pass it for an imported stereo image that wasn't tagged.
        NEVER pass it for a GENERATED image (generate_image output is a flat mono picture, not a stereo
        pair — forcing stereo just splits it into two mismatched halves; the server will reject it).
        Composes with billboard (a floating stereo photo you can walk around).
    """
    out = await _post("/place_image", _body(
        image_id=image_id, position=position, size_m=size_m, name=name, on_surface=on_surface,
        billboard=billboard or None,   # omit when off, so the wire stays minimal (server default = False)
        stretch=stretch or None,
        stereo=stereo))
    if not out.get("ok"):
        return f"Couldn't place image: {out.get('error', 'unknown error')}."
    return f"Hung image {out['image_id']} as {out['id']}." + _notice(out)


@mcp.tool()
async def conjure_module(
    module: str,
    config: Optional[dict] = None,
    position: Optional[list[float]] = None,
    on_surface: Optional[str] = None,
    billboard: bool = False,
    stretch: bool = False,
    name: Optional[str] = None,
) -> str:
    """Conjure a DYNAMIC MODULE — a live, animated effect that runs in the headset and is shared by
    everyone in the session (deterministic from a shared clock, so all viewers see the same thing).

    module: which effect (by name). The modules available to you — with a one-line description and the
        config params each accepts — are listed in the 'Dynamic modules you can conjure' context injected
        each turn (dynamics://available). Use a name from that list; an out-of-scope name is refused.
    config: module-specific settings (see each module's params in that catalog); omit to use defaults. A
        module that takes an `image` param accepts an image_id from generate_image (resolved to its src).
    position: [x, y, z] meters for where the effect centres (default just in front of the viewer). A
        free-standing flat module faces the viewer at creation automatically (fixed, not tracking). An
        image module (e.g. a Water Picture) sizes its plane to the picture's aspect ratio by default —
        pass width/height in config only to force an exact size.
    on_surface: mount it ON a real room surface (e.g. a Water Picture on a wall) — pass the surface's
        number or id, like place_image; it's aligned to the surface and, for an image module, fitted
        INSIDE the frame keeping the picture's aspect ratio. (Leave off for volume/ambient modules like a
        firefly swarm.)
    stretch: only with on_surface — fill the ENTIRE surface, stretching an image module's picture to the
        frame's shape (aspect NOT preserved). Off by default (fit-inside, aspect-correct); use only when
        the user explicitly asks to fill/cover/stretch to the whole surface.
    billboard: True makes it ALWAYS turn to face each viewer (yaw-only) as they move — use only when the
        user says 'always face me' / 'follow me'. Off by default (a fixed spawn-facing is the norm).
    name: reuse an id to move/reconfigure an existing instance; otherwise a new one is created.

    Use this when the user asks for an ambient/animated effect ('add some fireflies', 'make it magical')
    or an interactive one ('a koi pond I can ripple', 'water on wall art 12'). Remove it with
    dismiss_module.
    """
    out = await _post("/module", _body(module=module, config=config, position=position,
                                       on_surface=on_surface, billboard=billboard or None,
                                       stretch=stretch or None, name=name))
    if not out.get("ok"):
        return f"Couldn't conjure module: {out.get('error', 'unknown error')}."
    return f"Conjured {out['module']} (id {out['id']})."


@mcp.tool()
async def dismiss_module(name: Optional[str] = None, module: Optional[str] = None) -> str:
    """Remove a dynamic module (unload it). Pass name (its entity id) to remove one instance, or module
    (e.g. 'fireflies') to remove every instance of that kind."""
    out = await _post("/module/dismiss", _body(name=name, module=module))
    if not out.get("ok"):
        return f"Couldn't dismiss module: {out.get('error', 'unknown error')}."
    return f"Dismissed {', '.join(out['removed'])}."


@mcp.tool()
async def set_skybox(image_id: str) -> str:
    """Wrap the whole scene in a procured image (by image_id from generate_skybox_image /
    skybox_from_image) as the surrounding sky/environment."""
    out = await _post("/set_skybox", _body(image_id=image_id))
    if not out.get("ok"):
        return f"Couldn't set the skybox: {out.get('error', 'unknown error')}."
    return "Wrapped the scene in that image as a 360° skybox." + _notice(out)


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
    return "Wrapped the scene in that image as a grounded skybox — you're standing on it." + _notice(out)


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
    return f"Updated image {id} → {out['image_id']} via {_gen_info(out)}."


@mcp.tool()
async def widen_scene_image(id: str, aspect: Optional[str] = None, prompt: Optional[str] = None) -> str:
    """Extend (outpaint) an image ALREADY in the scene to a wider frame, in place.

    Use for 'make the painting wider', 'show more of the landscape'. id: the image entity id (in the Live context). aspect: '16:9' (default) or '21:9'. prompt: optional guidance for the new area.
    """
    out = await _post("/outpaint_image", _body(id=id, aspect=aspect, prompt=prompt))
    if not out.get("ok"):
        return f"Couldn't widen {id!r}: {out.get('error', 'unknown error')}."
    return f"Extended image {id} → {out['image_id']} via {_gen_info(out)} (aspect {aspect or '16:9'})."


@mcp.tool()
async def skybox_from_scene_image(id: str) -> str:
    """Turn an image ALREADY in the scene into the surrounding 360° sky.

    Use for 'make that painting the sky', 'put me inside that scene'. id: the image entity id (in the Live context).
    """
    out = await _post("/skybox_from_image", {"id": id}, timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't build a skybox from {id!r}: {out.get('error', 'unknown error')}."
    return f"Wrapped the scene as a 360° skybox (image {out['image_id']} via {_gen_info(out)})."


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
    """Set environment properties: sky_color, fog_color (CSS/#hex), fog_density (0..1). Setting sky_color
    makes the sky a PLAIN COLOR and REMOVES any skybox image (they're mutually exclusive) — this is how you
    remove/clear a skybox. See the current skybox/sky in world://current."""
    changes: dict[str, Any] = {}
    if sky_color is not None:
        changes["sky"] = {"color": sky_color}   # replace the sky wholesale → drops any skybox src/grounded
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
