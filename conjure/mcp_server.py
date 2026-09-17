"""Conjure world-editing MCP server (architecture.md §8).

Exposes the director's action vocabulary as MCP tools. Each tool translates an
intent into a patch and POSTs it to the running world server (`/patch`), which
validates, applies, and broadcasts it to every connected headset. The world stays
authoritative in one place; this server is a thin, stateless front.

Run (stdio transport):  python -m conjure.mcp_server
Needs the world server running (default http://localhost:8080, override CONJURE_URL).

Phase-1 scope: primitive entities + basic environment, matching the current client
renderer. `place_asset` / generation arrive with the asset pipeline (Phase 3).

**Result strings are read by a model, not a person.** They must state a *fact about the world*, never
a headline that could be read as an instruction: "Surface edges are now on", not "Surface edges on."
The verbless form is imperative-shaped English, and a tool result that reads like a command is a
result the model can answer by running the tool again. Suspected contributor to the 2026-08-28
repeat loop (docs/backlogs/agents.md); unproven, but the phrasing costs nothing either way.
`"<thing>' is now <state>."` is the house pattern.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Literal, Optional
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

from .config import DEFAULT_USER, scope_for
from . import poses
from .figures import figure_description

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
_READONLY_TOOLS = {"query_world", "query_space", "view_relative", "list_worlds",
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

    Neither caller passes a REAL surface: `world://current` filters them out and `query_world`
    collapses them (`_real_surfaces_line`). The guard below only keeps a stray third caller honest."""
    comps = e.get("components", {})
    meta = e.get("meta", {})
    pos = e.get("transform", {}).get("position")
    if meta.get("real"):
        desc = f"REAL {meta.get('semantic', 'surface')} (real surface — see the space summary)"
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
    """The single line every REAL surface collapses to in a world dump.

    A dump used to spend one line each on these, which in a captured space is most of it — measured at
    59 of 73 entities and **87% of the characters**, for lines carrying a semantic label, a position,
    and nothing else. The cost wasn't the worst of it: those lines READ as complete. Every entity in
    the dump has the same `id: description at pos` shape, so "this floor has no colour" and "this line
    doesn't show colour" are indistinguishable — and an agent that wants a colour concludes the world
    doesn't store one. It does: `space://current` carries every surface's id, position, colour and
    visibility, and it's already in the prompt of any agent that cares (observed 2026-08-26 — the
    director read this dump, saw no colours, and reported that surface colours aren't stored, with the
    real answer sitting in its own context).

    So: a count, the per-kind tally the space summary can't give without counting 59 lines, and a
    pointer to where the detail lives. One line can't be mistaken for a full description."""
    kinds: dict[str, int] = {}
    for e in reals:
        k = (e.get("meta") or {}).get("semantic") or "surface"
        kinds[k] = kinds.get(k, 0) + 1
    tally = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))
    return (f"{len(reals)} REAL surfaces ({tally}) — NOT listed here. Each one's id, position, "
            f"colour and visibility are in the space summary, already in your context. Restyle/hide/"
            f"mount them; don't move or remove them.")


@mcp.tool()
async def query_world() -> str:
    """Full dump of the PLACED scene (every non-surface entity + the environment). RARELY needed — your
    placed objects are already in the Live context each turn; use this only for detail the summary
    omits, or a very large scene.

    Real surfaces are **summarised in one line, not listed** — their per-surface detail (colour,
    visibility, position) is in the space summary, which is richer than anything this dump ever showed
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


# --- Space model (AR / scene understanding) — see docs/specs/worlds-surfaces.md -----------------------------

_IMMERSION = {
    "virtual_space": {"passthrough": False, "spacePresentation.active": True, "spacePresentation.defaultSurfaceVisible": True},
    "ar":           {"passthrough": True,  "spacePresentation.active": True, "spacePresentation.defaultSurfaceVisible": False},
    "mixed":        {"passthrough": True,  "spacePresentation.active": True},
    "authored":     {"passthrough": False, "spacePresentation.active": True, "spacePresentation.defaultSurfaceVisible": False},
    "vr_unbounded": {"passthrough": False, "spacePresentation.active": False, "spacePresentation.defaultSurfaceVisible": False},
}


async def _space_summary() -> str:
    """Text summary of the real space: surfaces (by semantic + short id) + the boundary. Shared by the
    query_space tool and the `space://current` resource (which agents inject into their prompt each turn,
    so they needn't call query_space just to see surfaces)."""
    doc = await _get("/world")
    env = doc.get("environment", {})
    pres = env.get("spacePresentation", {})
    reals = [e for e in doc["entities"] if e.get("meta", {}).get("real")]
    if not pres.get("active") or not reals:
        return "No space model yet — the headset hasn't shared one (capture the space, or work in VR)."
    lines = [f"Space: {len(reals)} surfaces · passthrough={env.get('passthrough', False)} · "
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
async def query_space() -> str:
    """Summarize the user's real space: surfaces (by semantic label) + the boundary. Read this before
    placing things (so models land INSIDE the space, not through a wall) or to pick a surface to mount
    on / restyle. Real surfaces also appear in query_world as REAL entities — restyle or hide them
    with update_entity's color, or show_surface; don't move or remove them.
    """
    return await _space_summary()


@mcp.resource("space://current")
async def space_resource() -> str:
    """The live real-space summary — injected each turn into agents that list `space://current` in their
    context (so the builder sees the space without a query_space round-trip)."""
    return await _space_summary()


@mcp.resource("world://current")
async def world_resource() -> str:
    """Live summary of the virtual scene you've built — PLACED objects (models/images) plus the
    ENVIRONMENT (skybox / sky color / fog). Injected each turn so the builder references them without a
    query_world round-trip. Excludes scaffold and real surfaces (those are in space://current). The skybox
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
    """Set how much of the real space vs. virtual the user sees:
    - virtual_space: the captured surfaces are RENDERED — a recolourable virtual copy of the space.
    - ar: the captured surfaces are hidden; the real space shows through (mount/occlude against them).
    - mixed: leaves surface visibility alone, so show_surface composes the blend (e.g. a virtual ceiling
      over real walls).
    - authored: captured surfaces hidden, the space still in effect — intended for replacement geometry
      built to the real footprint. No tool builds that geometry yet, so today this matches `ar`.
    - vr_unbounded: ignore the space entirely; the original full synthetic VR environment.
    """
    env = _IMMERSION.get(mode)
    if env is None:
        return f"Unknown mode {mode!r}. Use one of: {', '.join(_IMMERSION)}."
    await _post_patch([{"op": "env", "set": dict(env)}])
    return f"Immersion is now {mode}."


@mcp.tool()
async def realign_space() -> str:
    """Re-align the virtual surfaces to the real world. Use when the user says things look misaligned,
    drifted, or shifted — e.g. after recentering with the Meta button, putting the headset down, or
    reloading. Re-captures the space at the current tracking origin. Only affects a headset in AR."""
    out = await _post("/space/realign", {})
    if not out.get("ok"):
        return f"Couldn't realign: {_reason(out)}."
    return "Re-aligning the virtual surfaces to your real space — look around for a moment."


@mcp.tool()
async def reset_world() -> str:
    """Wipe the world back to the empty holodeck and start over — removes ALL placed objects, images,
    skybox, primitives, and any captured space. Use when the user asks to reset, clear everything, or
    start fresh. (A captured space re-appears on its own once they're back in AR.)"""
    out = await _post("/reset", {})
    if not out.get("ok"):
        return f"Couldn't reset: {_reason(out)}."
    return "The world has been reset to an empty holodeck — ready to build again."


# Markers that identify a provider content-policy refusal inside an SDK error dump.
_MODERATION_MARKERS = ("moderation_blocked", "safety system", "content_policy", "content policy")


def _reason(out: dict) -> str:
    """The failure to hand back to the model — trimmed, and with a provider's raw JSON dump reduced to
    the one fact that matters.

    Two problems with pasting the SDK error verbatim: a 400-character blob costs context and buries the
    actionable word, and a refusal that reads like a transient error invites a retry. Observed
    2026-08-28: an image blocked for content was retried four times against three generators, and each
    failure was reported to the user as a success (docs/backlogs/agents.md). Callers add the final
    period, so the trailing one is stripped here."""
    err = str(out.get("error") or "unknown error").strip().rstrip(".")
    low = err.lower()
    if any(m in low for m in _MODERATION_MARKERS):
        m = re.search(r"safety_violations=\[([^\]]*)\]", err) or re.search(r"'categories': \[([^\]]*)\]", err)
        what = f" ({m.group(1).replace(chr(39), '')})" if m else ""
        return (f"the image provider's content policy refused this{what}. Rewording the same subject "
                f"for this generator will be refused again — say so rather than retrying")
    return err


async def _is_real_surface(id: str) -> bool:
    """Is this entity id a captured real surface? Keyed on `meta.real`, the authoritative marker — the
    `real_…` id prefix is a convention, not a guarantee. Returns False if the world can't be read: a
    lookup failure must not turn an ordinary entity update into a no-op."""
    try:
        doc = await _get("/world")
    except Exception:  # noqa: BLE001 — world unreachable; the patch below will surface the real error
        return False
    return any(e.get("id") == id and (e.get("meta") or {}).get("real") for e in doc.get("entities", []))


@mcp.tool()
async def show_surface(target: str, visible: bool = True) -> str:
    """Show or hide real surface(s) as virtual geometry. target: a surface id ('real_wall_1'),
    a semantic label ('wall', 'ceiling', 'floor', …), or 'all'. Use to build mixed real+virtual
    views (e.g. show only the ceiling)."""
    doc = await _get("/world")
    reals = [e for e in doc["entities"] if e.get("meta", {}).get("real")]
    t = target.lower()
    targets = [e for e in reals
               if t == "all" or e["id"] == target or e.get("meta", {}).get("semantic") == t
               or str(e.get("meta", {}).get("friendly_id")) == target]
    if not targets:
        return f"No real surface matches {target!r} (try query_space)."
    await _post_patch([{"op": "update", "id": e["id"], "set": {"components.material.visible": visible}}
                       for e in targets])
    return f"{'Showed' if visible else 'Hid'} {len(targets)} surface(s) matching {target!r}."


@mcp.tool()
async def texture_surface(target: str, image_id: str, repeat: Optional[float] = None) -> str:
    """Map a procured image onto real surface(s) — e.g. a starfield on the ceiling, grass on the
    floor, a mural on a wall. First call generate_image, then pass its image_id here.

    target: a surface id ('real_floor'), a semantic label ('floor', 'ceiling', 'wall'), or 'all'.
    repeat: tile the image NxN across the surface (e.g. 4) — for this, generate a SEAMLESS/tileable
        image (grass, brick). Omit to stretch a single copy (good for a starfield, sky, or mural).
    """
    out = await _post("/texture_surface", _body(target=target, image_id=image_id, repeat=repeat))
    if not out.get("ok"):
        return f"Couldn't texture {target!r}: {_reason(out)}."
    return f"Mapped the image onto {out['count']} surface(s) ({target})." + _notice(out)


@mcp.tool()
async def style_surface(target: str, color: Optional[str] = None, opacity: Optional[float] = None) -> str:
    """Color and/or set the transparency of real surface(s) — e.g. 'make the walls semi-transparent
    blue', 'make the ceiling glass', 'paint the floor red'.

    target: a surface id ('real_wall_3'), a semantic label ('wall'/'floor'/'ceiling'), or 'all'.
    color: CSS name or #hex. opacity: 0 (invisible) … 1 (solid); < 1 makes it see-through.
    (To map an image onto a surface, use texture_surface instead.)
    """
    out = await _post("/style_surface", _body(target=target, color=color, opacity=opacity))
    if not out.get("ok"):
        return f"Couldn't style {target!r}: {_reason(out)}."
    return f"Styled {out['count']} surface(s) ({target})."


@mcp.tool()
async def show_annotations(on: bool = True, dimensions: bool = False) -> str:
    """Show or hide text labels floating on each real surface — each shows its name + short id (e.g.
    'window (12)'), which the user can reference (e.g. 'make 12 blue'). Turn on when the user wants to
    inspect/identify surfaces. dimensions: also show each surface's size (default off; turn on only if
    the user asks for sizes)."""
    await _post_patch([{"op": "env", "set": {"spacePresentation.annotations": on, "spacePresentation.annotationDims": dimensions}}])
    return f"Surface annotations are now {'on' if on else 'off'}{' with dimensions' if (on and dimensions) else ''}."


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
    """Show or hide the polygon outline drawn around every real surface (the bright wireframe of the
    real space). Edges are ON by default; turn them off for a cleaner passthrough view."""
    await _post_patch([{"op": "env", "set": {"spacePresentation.edgesVisible": on}}])
    return f"Surface edges are now {'on' if on else 'off'}."


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
async def reset_world_frame(what: str = "all") -> str:
    """Undo the user's manual skybox / world adjustments, putting everything back where the system derives
    it. Use when they say the sky is turned the wrong way, the world has drifted or been dragged off, they
    want the skybox back to normal size, or 'put the world back'.

    These adjustments come from the `grab` module's skybox and void modes (the user drags the floor to turn
    or scale the sky, or to slide a whole outdoor world). Nothing else is affected — placed objects keep
    their own positions.

    what: 'sky' (skybox orientation + size), 'frame' (a void/outdoor world's orientation + position),
    or 'all' (both, the default).
    """
    if what not in ("sky", "frame", "all"):
        return f"Unknown target {what!r}. Use sky, frame, or all."
    out = await _post("/world_frame", {"reset": what})
    if not out.get("ok"):
        return f"Couldn't reset: {_reason(out)}."
    label = {"sky": "The skybox is", "frame": "The world's orientation and position are",
             "all": "The skybox and the world frame are"}[what]
    return f"{label} back to normal."


# ---- figures: posing a rigged humanoid ------------------------------------------------------------
# The point of the humanoid map is that the director speaks ONE vocabulary — for the bone AND for the
# direction. It says "bend her left elbow" and never learns that this rig calls that bone `forearm.fk.L`
# and the next `J_Bip_L_LowerArm`, nor which way each one's local X happens to point.

@mcp.tool()
async def inspect_figure(id: str) -> str:
    """Describe a placed human figure — how tall it is, and which body parts you can pose by name.

    Use before posing so you know the exact bone names this figure has. Not every figure has every bone.
    """
    world = await _get("/world")
    ent = next((e for e in (world.get("entities") or []) if e.get("id") == id), None)
    if ent is None:
        return f"There's no object called {id!r} in the world."
    meta = ent.get("meta") or {}
    if not meta.get("rigged"):
        return f"{id!r} is not a posable figure — it has no skeleton."
    bbox = meta.get("bbox")
    posed = ((ent.get("components") or {}).get("figure") or {}).get("pose")
    already: list = []
    if posed and posed != "{}":
        import json as _json
        try:
            already = sorted(_json.loads(posed))
        except ValueError:
            pass
    # The wording lives in `figures` so the eval harness can ask the director the same question this
    # tool does, against a file instead of a live world (docs/backlogs/figures.md, slice 2).
    # What can be taken OFF, beside what can be posed. `inspect_figure` is the tool a director reaches
    # for when asked "what is she wearing" — leaving parts out of it meant the answer was "nothing".
    from .parts import load_vocabulary, removable as removable_parts
    parts = meta.get("parts") or {}
    groups = removable_parts(parts, load_vocabulary()) if parts else ({} if parts else None)
    if parts and not groups:
        groups = {}
    hidden_raw = ((ent.get("components") or {}).get("figure-parts") or {}).get("hidden") or "[]"
    try:
        import json as _j
        hidden = _j.loads(hidden_raw) if isinstance(hidden_raw, str) else list(hidden_raw)
    except ValueError:
        hidden = []
    return figure_description(
        label=meta.get("title") or id,
        height_m=(bbox[1][1] - bbox[0][1]) if bbox else None,
        tris=meta.get("tris"), bones=(meta.get("humanoid_axes") or {}),
        has_map=bool(meta.get("humanoid")), posed=already,
        removable=groups if parts else {}, hidden=hidden,
        morphs=meta.get("morph_names"), expression=meta.get("expression_scheme"),
        hand_side=(meta.get("hand_side") or "") if meta.get("hand_joints") else "",
        worn=((ent.get("components") or {}).get("hand-rig") or {}).get("hand") or "")


@mcp.tool()
async def dress_figure(id: str, hide: Optional[list[str]] = None, show: Optional[list[str]] = None,
                       only_body: bool = False) -> str:
    """Turn parts of a figure off and on — clothing, hair, shoes, accessories.

    **Match the request to the narrowest thing that satisfies it.** The categories are separate because
    people mean them separately:

      "remove her clothes" / "take her dress off"  -> hide=["clothing"]     NOT only_body
      "lose the shoes" / "barefoot"                -> hide=["shoes"]
      "take off her necklace" / "no jewellery"     -> hide=["accessory"]
      "shave her" / "remove the hair"              -> hide=["hair"]
      "strip her" / "show me the model underneath" -> only_body=True

    `only_body` removes EVERYTHING removable INCLUDING HER HAIR, which is almost never what "remove her
    clothes" means. Reach for it only when the user asked for everything off.

    hide/show also take the name of a single mesh, for when a category is too broad — 'take off the
    left shoe'. The tool reports which categories it hid, so say that back rather than guessing.

    The face and the body itself are never removable. Hiding is visibility, not deletion: a pose
    survives it and showing a part again is instant. What counts as clothing was decided when the model
    was imported, so if something is in the wrong group, name that mesh directly.
    """
    out = await _post("/figure/parts", _body(id=id, hide=hide or [], show=show or [],
                                             only_body=only_body))
    if not out.get("ok"):
        return f"Couldn't change that figure's parts: {_reason(out)}."
    hidden = out.get("hidden") or []
    groups = out.get("removable") or {}
    by_cat = out.get("hidden_by_category") or {}
    # Report by CATEGORY as well as by mesh, so the answer given back to the user is what actually
    # happened. Three times running the director said "clothing's all off" after a call that also took
    # the hair, because the reply only listed mesh names and nobody reads those.
    summary = ", ".join(f"{c} ({len(n)})" for c, n in sorted(by_cat.items())) if by_cat else "nothing"
    lines = [f"{id}: hidden — {summary}."]
    if hidden:
        lines.append(f"Meshes: {', '.join(hidden)}")
    # What is STILL ON — `removable` is the TOTAL per category, and reported as a remainder beside a list
    # of what just came off it reads as "there is more clothing left" when there is none. Same class of
    # misreport as answering "clothing's all off" after a call that also took the hair.
    left = {k: n - len(by_cat.get(k) or []) for k, n in groups.items()}
    lines.append("Still on: "
                 + (", ".join(f"{k} ({n})" for k, n in sorted(left.items()) if n > 0)
                    or "nothing removable"))
    if out.get("unknown"):
        lines.append(f"Not a category or a mesh on this figure: {', '.join(out['unknown'])}")
    return "\n".join(lines)


@mcp.tool()
async def play_clip(id: str, clip: str, loop: bool = True, speed: Optional[float] = None,
                    force: bool = False) -> str:
    """Play a captured animation on a figure — a dance, an idle, a gesture.

    Ask `list_clips` first unless the user named a clip you already know exists. A clip is identified by
    its label ("3_idle") or its asset id; if a label belongs to more than one clip this refuses and
    lists them, because the same clip name turns up on different figures and picking one at random plays
    the wrong body.

    A clip plays on any figure, not only the one it shipped with. Same rig, it binds directly; a
    different rig, it is REWRITTEN for her first — both skeletons mapped through a canonical humanoid
    and every channel that can cross rewritten. So the answer to "can she do what the other one was
    doing" is yes, with one honest caveat: a rewrite carries only the bones a humanoid has, so a skirt,
    a ponytail or a breast chain stays still while the body moves. The reply says when it happened.

    A figure with no recoverable bone map is still refused, because there is nothing to map through.
    `force` is only for looking at the wreck deliberately — it binds by NAME across the mismatch, which
    resolves almost nothing.

    **A clip brings its own SOUND.** Most of these were captured with a voice track recorded against
    them — 20 of Barbie's 21 — and it plays automatically, in sync, positioned on the figure. There is
    no separate audio tool and none is needed: "animate her with sound", "make her talk", "with audio"
    all mean *play a clip that has a voice*. Call `list_clips` and pick one marked `voiced`. Saying you
    cannot do audio is wrong, and it was said on device while the voice was already playing.

    **Leave `speed` alone unless the user asks for fast or slow.** Each clip carries the playback rate
    its capture authored — some are meant to run at 0.5 and some at 1.2 — and passing a rate overrides
    that, so a well-meant `speed=1.0` plays the clip wrong.

    While a clip plays it drives the whole skeleton and any pose is overridden; stopping puts the figure
    back and the pose returns. Use `stop_clip` to stop.
    """
    out = await _post("/figure/clip", _body(id=id, clip=clip, loop=loop, speed=speed, force=force))
    if not out.get("ok"):
        line = f"Couldn't play that: {_reason(out)}"
        if out.get("candidates"):
            line += "\n" + "\n".join(f"  {c['id']} ({c.get('tags') or 'untagged'})"
                                      for c in out["candidates"])
        return line
    secs = out.get("duration_s")
    length = f", {secs:.0f}s" if isinstance(secs, (int, float)) else ""
    tail = " (looping)" if out.get("loop") else ""
    if out.get("voiced"):
        voice = " Her voice plays with it."
    elif out.get("voiced_alternatives"):
        voice = (f" This one is silent — {out['voiced_alternatives']} of her clips have a voice; "
                 f"`list_clips(voiced=True)` lists them.")
    else:
        voice = " This one is silent."
    warn = f"\nWarning: {out['warning']}" if out.get("warning") else ""
    return f"Playing {out.get('label') or out['clip']} on {id}{length}{tail}.{voice}{warn}"


@mcp.tool()
async def stop_clip(id: str) -> str:
    """Stop the animation playing on a figure. It returns to its pose, or to standing if it has none."""
    out = await _post("/figure/clip", _body(id=id, stop=True))
    return f"Stopped the animation on {id}." if out.get("ok") else f"Couldn't stop it: {_reason(out)}"


@mcp.tool()
async def list_clips(id: str, all: bool = False, kind: str = "", voiced: bool = False) -> str:
    """What a figure can be animated with.

    Three lists, and the differences matter. **Shipped** is what this figure's own scene gave it — the
    safe default. **Compatible** is every clip her skeleton receives as-is. **Retargetable** is every
    clip from a DIFFERENT rig, rewritten for her on the way; those play, and they lose the channels no
    other rig has a bone for — a skirt or a ponytail stays still while the body moves. Both of the
    latter are listed only when you pass `all`.

    A clip that fits is not automatically a sensible one: many are authored around furniture that is not
    there, so a figure standing in an empty room will lean on a sink that does not exist. Prefer the
    shipped list unless the user is exploring.

    `kind` narrows to "idle" or "action". Idles are quiet and personal to a figure; actions travel.

    `voiced` keeps only the clips that carry a VOICE TRACK, which plays automatically with the
    animation. That is the whole answer to "animate her with sound" — there is no separate audio tool,
    the sound is part of the clip. Most clips are voiced; a few are not.
    """
    q = (f"/figure/clips?id={id}" + ("&all=true" if all else "") + (f"&kind={kind}" if kind else "")
         + ("&voiced=true" if voiced else ""))
    out = await _get(q)
    if not out.get("ok"):
        return f"Couldn't list clips: {_reason(out)}"

    def show(rows: list) -> list[str]:
        return [f"  {r['label'] or r['id']} — {r.get('kind') or 'clip'}"
                + (f", {r['duration_s']:.0f}s" if isinstance(r.get("duration_s"), (int, float)) else "")
                + (", voiced" if r.get("voiced") else "")
                + f"  [{r['id']}]" for r in rows]

    shipped = out.get("shipped") or []
    lines = [f"{id} — rig {out.get('rig_sig') or 'unknown'}",
             f"Shipped with her ({len(shipped)}):"] + (show(shipped) or ["  none"])
    if all:
        other = out.get("compatible") or []
        lines.append(f"Also compatible ({len(other)}) — authored for other figures on this rig:")
        lines += show(other) or ["  none"]
        far = out.get("retargetable") or []
        if far:
            lines.append(f"Retargetable ({len(far)}) — from other rigs, rewritten to fit her:")
            lines += show(far)
    else:
        counts = []
        if out.get("compatible_count"):
            counts.append(f"{out['compatible_count']} more fit this rig")
        if out.get("retargetable_count"):
            counts.append(f"{out['retargetable_count']} more can be retargeted from other rigs")
        if counts:
            lines.append(" and ".join(counts) + " — pass all to see them.")
    return "\n".join(lines)


@mcp.tool()
async def list_poses() -> str:
    """The named poses a figure can be put into, and what each one is.

    Read this before inventing a pose bone by bone — a named pose is one call, works the same on every
    figure, and settles the figure onto whatever it ends up resting on.
    """
    return ("Poses for pose_figure(id, named=…):\n" + poses.catalogue()
            + "\n\nAny of them can be adjusted in the same call by passing `pose` as well.")


@mcp.tool()
async def pose_figure(id: str, pose: dict | None = None, named: str = "", clear: bool = False) -> str:
    """Pose a human figure — either a whole named pose, or by moving body parts one at a time. Use for
    "have her kneel", "raise her left arm", "turn his head", "have her bend a knee".

    named is a WHOLE POSE from the library, and it is the first thing to reach for when the request has
    a name: kneel, sit, crouch, t-pose, cheer, arms-crossed, hands-on-hips, wave, point, bow, stand.
    Call `list_poses` for what each one is. One word does the work of seven bones, it is the same pose
    on every figure, and the figure settles onto whatever it now rests on:

        named="kneel"                                          down on both knees
        named="sit"                                            seated — put a chair under her yourself
        named="stand"                                          back to a plain neutral stance

    pose maps a bone name to what you want that body part to do, and can be combined with `named` to
    adjust it ("kneel, but with her arms out"). Two ways to say it, and for arms and legs the FIRST is
    almost always the right one:

    1. aim — WHERE THE LIMB SHOULD POINT. Absolute, so it does not depend on how this particular figure
       happens to stand:

         {"rightUpperArm": {"aim": "up"}}                     arm straight up
         {"rightUpperArm": {"aim": "forward"}}                arm out in front
         {"leftUpperArm": {"aim": "out"}, "rightUpperArm": {"aim": "out"}}    both arms out sideways
         {"leftUpperLeg": {"aim": "forward"}}                 left leg lifted out in front
         {"rightUpperArm": {"aim": [0, 1, 1]}}                halfway between up and forward

       Directions: up, down, forward, back, out (away from the body, whichever side that bone is on),
       in (across the body). "out" and "in" already know left from right, so give BOTH sides the SAME
       direction for a symmetric pose — never negate one of them.

       Use aim for arms, legs, hands and feet. It is not available for the head, neck, spine or hips.

       AIM THE LIMB, NOT EVERY BONE IN IT. "Raise her arm" is ONE call on leftUpperArm — aiming the
       forearm too folds the elbow, since "point the forearm up" while the upper arm already points up
       means bend it right back. Aim the shoulder or hip; leave the elbow or knee alone unless the user
       asked for it bent, and then bend it with `bend` below.

    2. bend / spread / turn — A ROTATION FROM WHERE THE PART CURRENTLY RESTS, in DEGREES. Use these to
       adjust, and for the head and spine, which have no aim:

         bend    FOLD THE JOINT THE WAY IT FOLDS, positive. An elbow bends the hand forward, a knee
                 bends the heel backward, a hip lifts the thigh forward, the spine and neck bow
                 forward. Negative is the opposite, which most joints barely allow.
         spread  away from the body (+) / across it (-) — part the legs, arm away from the side
         turn    twist about the part's own length — for the head and spine, + turns to the figure's
                 own LEFT

         {"leftLowerArm": {"bend": 90}}                        bend the left elbow
         {"leftLowerLeg": {"bend": 90}}                        bend the left knee (heel comes up behind)
         {"head": {"turn": -40}}                               look to her right
         {"head": {"bend": -20}}                               tilt the head back to look up
         {"leftUpperLeg": {"spread": 20}, "rightUpperLeg": {"spread": 20}}    stand with feet apart

       Note the last one: SAME sign on both sides. spread is already mirrored, so +20 on the left and
       -20 on the right would swing both legs the same way instead of parting them.

    A bone takes either an aim or a bend/spread, not both — they set the same thing. turn combines with
    either. Bone names are semantic and identical on every figure: leftUpperArm, rightLowerLeg, head,
    spine and so on; call inspect_figure if unsure which this one has.

    Joints have limits and a request past one lands AT the limit, with the reply saying so — an elbow
    does not bend sideways and a hip does not swing 90 degrees backwards. If you get that message, the
    figure is already as far as it goes: do not retry with a bigger number, and if you asked a hinge to
    bend the wrong way, the fix is the opposite sign, not a bigger one.

    A bone you mention is replaced outright, so pass everything you want it to keep; bones you do not
    mention keep their current pose, so you can move one arm without disturbing the rest. An empty {}
    returns just that bone to rest. Pass clear=true to return the whole figure to its rest pose.
    """
    body: dict = {"id": id}
    if clear:
        body["clear"] = True
    else:
        if not named and (not isinstance(pose, dict) or not pose):
            return ("Give me a pose like {\"leftUpperArm\": {\"aim\": \"up\"}}, a named one like "
                    "named=\"kneel\", or clear=true to reset.")
        if named:
            body["named"] = named
        if pose:
            body["pose"] = pose
    out = await _post("/figure", body)
    if not out.get("ok"):
        return f"Couldn't pose that: {_reason(out)}."
    if out.get("cleared"):
        return "Back to a neutral stance."
    moved = (f"{out['named']}." if out.get("named") and not pose
             else f"Moved {', '.join(out.get('posed') or [])}.")
    if out.get("skipped"):
        # A figure this pose could only partly reach. Better said than left to look like the pose
        # simply did not work.
        moved += f" This figure has no {', '.join(out['skipped'])}, so that part was skipped."
    if out.get("needs"):
        # A pose can be the right SHAPE and still need something from the world. Saying so is the point:
        # a figure sitting on nothing looks like a bug unless the caller was told it is waiting on a
        # chair (docs/backlogs/figures.md — solving against the world is tier 3, and is not built).
        moved += f" She needs {out['needs']}."
    if out.get("limited"):
        # What a joint refused, said out loud. A body has limits; a request past them lands at the limit
        # rather than doing nothing, and knowing which one was hit is how the next request gets better.
        return moved + " Joint limits applied: " + "; ".join(out["limited"]) + "."
    return moved


@mcp.tool()
async def wear_hand(id: str, hand: str = "auto") -> str:
    """Put a placed HAND MODEL on the wearer's hand, so it follows their real one. Use for "wear
    these hands", "put the left hand on", "take the hands off".

    A worn hand is driven by the headset's hand tracking, all twenty-five joints, every frame — so it
    moves exactly as the person's own hand does. Nothing about it is posable and no animation plays on
    it; it is the wearer's hand, wearing a model.

    hand is "auto" (pair it with the side the model actually is), "left", "right", or "off" to take it
    off. `auto` is almost always what you want: which hand a model is was MEASURED from its geometry
    when it was imported, not read from its name. Asking for the wrong side is refused, because a left
    model worn on a right hand looks like broken tracking rather than like a mistake.

    Taking it off puts it back where it was placed — wearing does not consume the model, it occupies
    it. It needs hand tracking: with controllers in hand there is nothing to follow, and the model
    simply rests where it was put.
    """
    out = await _post("/figure/hand", {"id": id, "hand": hand})
    if not out.get("ok"):
        return f"Couldn't do that: {_reason(out)}."
    if not out.get("worn"):
        return "Taken off — it's back where it was placed."
    return f"Worn on the {out.get('hand')} hand, following all {out.get('joints')} joints."


@mcp.tool()
async def set_expression(id: str, expression: dict | None = None, clear: bool = False) -> str:
    """Change a figure's FACE — smile, blink, look at you, mouth a word. Use for "have her smile",
    "make her look surprised", "close her eyes", "have her look left".

    expression maps what you want to how much of it, 0 to 1. Several combine, so a face can do more
    than one thing at once:

        {"smile": 1}                          a smile
        {"smile": 0.4}                        a slight one
        {"blink": 1}                          eyes closed
        {"joy": 1}                            a full happy face — mouth AND eyes
        {"surprised": 1}                      brows up, jaw open
        {"smile": 0.6, "look_left": 1}        smiling, glancing left
        {"brow_raise": 1, "mouth_open": 0.3}  a questioning look

    What you can ask for: neutral, smile, joy, sad, angry, surprised, blink, blink_left, blink_right,
    squint, brow_raise, brow_lower, mouth_open, pucker, look_left, look_right, look_up, look_down.
    Mouth shapes for speech: aa, ee, ih, oh, ou.

    NOT EVERY FIGURE HAS A FACE, and most do not — a figure may be fully rigged, posable and
    animated and still have no facial shapes at all. `inspect_figure` says which. When a figure
    cannot, the reply says so plainly; do not retry with different wording, because the shapes are
    simply not in the model.

    Expressions REPLACE each other rather than accumulate: each call sets the whole face, so pass
    everything you want at once. Pass clear=true to return to a resting face.
    """
    body: dict = {"id": id}
    if clear:
        body["clear"] = True
    elif isinstance(expression, dict) and expression:
        body["expression"] = expression
    else:
        return ('Give me an expression like {"smile": 1} or {"blink": 1, "smile": 0.5}, '
                "or clear=true to relax her face.")
    out = await _post("/figure/expression", body)
    if not out.get("ok"):
        return f"Couldn't do that: {_reason(out)}."
    if out.get("cleared"):
        return "Face relaxed."
    said = ", ".join(sorted(out.get("applied") or []))
    msg = f"Set {said}."
    if out.get("skipped"):
        # A face this figure has only part of. Said out loud: a tongue-only rig asked to raise its
        # brows has no brows, and silence there reads as the tool not working.
        msg += f" This figure cannot {', '.join(out['skipped'])}, so that was skipped."
    return msg


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

def _asset_note(c: dict) -> str:
    """The short tail on a search hit. For a FIGURE it carries the two facts that decide which of six
    near-identical Graces to place — how tall, and how expensive — because a bare list of labels gives
    the director no way to choose and it will simply take the first (measured, 2026-09-03)."""
    bits = [c["licence"]] if c.get("licence") else []
    try:
        attrs = json.loads(c.get("attributes") or "{}")
    except (TypeError, ValueError):
        attrs = {}
    if attrs.get("rigged"):
        height = attrs.get("height_m")
        tris = attrs.get("tris")
        bits.append("figure" + (f" {height:.2f} m" if isinstance(height, (int, float)) else "")
                    + (f", {round(tris / 1000)}k tris" if isinstance(tris, int) else ""))
    return f" [{'; '.join(bits)}]" if bits else ""


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
        return f"Library search failed: {_reason(out)}."
    cands, tier = out.get("candidates", []), out.get("confidence_tier", "none")
    if not cands:
        return "No matching asset in the library (confidence: none) — generate or fetch a new one."
    lines = [f"- {c['id']} ({c['kind']}, match={c['match']}): "
             f"{c.get('label') or c.get('prompt') or c.get('query') or '—'}{_asset_note(c)}"
             for c in cands[:8]]
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
        return f"Couldn't reuse {id!r}: {_reason(out)}."
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
    place_image/place_asset) — DON'T hand-compute it. Also returns `surface` (the nearest real surface
    the ray hits — style/texture it by its id) and `nearby` placed objects. Needs the user connected
    with a live view (an active session)."""
    out = await _post("/view_relative", _body(direction=direction, distance=distance))
    if not out.get("ok"):
        return f"Couldn't resolve that view: {_reason(out)}."
    p = out["point"]
    lines = [f"Point {out['distance']} m {out['direction']} of the user: "
             f"[{p[0]:.2f}, {p[1]:.2f}, {p[2]:.2f}] — use as `position` to place there."]
    s = out.get("surface")
    lines.append(
        f"Surface that way: {s.get('semantic')} #{s.get('friendly_id')} (id {s['id']}), "
        f"{s['distance']:.2f} m away — target it by id to style/texture it." if s
        else "No real surface that way within reach.")
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
        return f"Query failed: {_reason(out)}."
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
        return f"Couldn't update {id!r}: {_reason(out)}."
    return "Updated."


@mcp.tool()
async def delete_asset(id: str) -> str:
    """Delete an asset from your library catalog (its entry, aliases, and search index). The cached
    file is left on disk. Use to remove a bad or duplicate asset ('delete that duplicate woman model')."""
    out = await _post("/delete_asset", _body(id=id, scope=_scope()))
    if not out.get("ok"):
        return f"Couldn't delete {id!r}: {_reason(out)}."
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
    only you can see and enter. Pass outdoor=True for an OUTDOOR/void world — no space geometry, just a
    skybox + placed objects (use for 'a world set outdoors', 'floating in space', 'on a beach'); it's not
    tied to a captured space and holds its orientation on its own."""
    out = await _post("/worlds/new", _body(name=name, scope=_scope(), public=public, outdoor=outdoor))
    if not out.get("ok"):
        return f"Couldn't create {name!r}: {_reason(out)}."
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
        return f"Couldn't change visibility: {_reason(out)}."
    pub = out.get("published_assets") or []
    extra = (f" Also published {len(pub)} private asset(s) it uses so visitors can see the whole scene: "
             f"{', '.join(pub)}." if pub else "")
    return f"World '{out.get('world', name or 'current')}' is now {'public' if public else 'private'}." + extra


@mcp.tool()
async def set_space_visibility(public: bool, name: Optional[str] = None) -> str:
    """Make a physical SPACE public or private. A space is the real space your worlds are anchored in; it's
    shared, so anyone co-located can join. Spaces are PUBLIC by default: any co-located user may build their
    OWN worlds in it. Set public=False to make it PRIVATE — then only you can create NEW worlds in it
    (existing worlds are unaffected; joining/viewing still follows each WORLD's visibility). Defaults to your
    CURRENT space; pass `name` to target another space you own. You can only change spaces you own."""
    out = await _post("/space/visibility", _body(public=public, scope=_scope(), name=name))
    if not out.get("ok"):
        return f"Couldn't change space visibility: {_reason(out)}."
    return f"Space '{out.get('space', name or 'current')}' is now {'public' if public else 'private'}."


@mcp.tool()
async def switch_world(name: str) -> str:
    """Switch to one of YOUR worlds in this session — by **id** (preferred, it never changes) or by its
    current name. Saves the current world first, bringing everyone
    present along. Match `name` to a real world from list_worlds; formatting/case needn't be exact.
    (Visiting another user's world is a person's action at the shell — not something you do here.)"""
    out = await _post("/worlds/switch", _body(name=name, scope=_scope()))
    if not out.get("ok"):
        return f"Couldn't switch to {name!r}: {_reason(out)}."
    return f"Switched to '{out.get('world', name)}'."


@mcp.tool()
async def delete_world(name: str) -> str:
    """Delete one of your worlds permanently. You can't delete the world you're currently in — switch
    away first."""
    out = await _post("/worlds/delete", _body(name=name, scope=_scope()))
    if not out.get("ok"):
        return f"Couldn't delete {name!r}: {_reason(out)}."
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
        return f"Couldn't generate image: {_reason(out)}."
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
        return f"Couldn't generate skybox image: {_reason(out)}."
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
        return f"Couldn't generate grounded skybox image: {_reason(out)}."
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
        return f"Couldn't edit image: {_reason(out)}."
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
        return f"Couldn't outpaint image: {_reason(out)}."
    return f"Outpainted → image_id={out['image_id']} via {_gen_info(out)} (aspect {aspect or '16:9'})."


@mcp.tool()
async def skybox_from_image(image_id: str, generator: Optional[str] = None) -> str:
    """Turn a procured image (by image_id) into a 360° panorama and return a NEW image_id.

    Then call set_skybox with the returned image_id. To turn a picture already in the scene into the
    sky, prefer skybox_from_scene_image. generator: optional.
    """
    out = await _post("/images/skybox_from", _body(image_id=image_id, generator=generator), timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't build a skybox image: {_reason(out)}."
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

    on_surface: hang it ON a real surface — pass the surface's id ('real_wall_art_3'), its
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
        return f"Couldn't place image: {_reason(out)}."
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
    on_surface: mount it ON a real surface (e.g. a Water Picture on a wall) — pass the surface's
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
        return f"Couldn't conjure module: {_reason(out)}."
    return f"Conjured {out['module']} (id {out['id']})."


@mcp.tool()
async def dismiss_module(name: Optional[str] = None, module: Optional[str] = None) -> str:
    """Remove a dynamic module (unload it). Pass name (its entity id) to remove one instance, or module
    (e.g. 'fireflies') to remove every instance of that kind."""
    out = await _post("/module/dismiss", _body(name=name, module=module))
    if not out.get("ok"):
        return f"Couldn't dismiss module: {_reason(out)}."
    return f"Dismissed {', '.join(out['removed'])}."


@mcp.tool()
async def set_skybox(image_id: str) -> str:
    """Wrap the whole scene in a procured image (by image_id from generate_skybox_image /
    skybox_from_image) as the surrounding sky/environment."""
    out = await _post("/set_skybox", _body(image_id=image_id))
    if not out.get("ok"):
        return f"Couldn't set the skybox: {_reason(out)}."
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
        return f"Couldn't set the grounded skybox: {_reason(out)}."
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
        return f"Couldn't edit {id!r}: {_reason(out)}."
    return f"Updated image {id} → {out['image_id']} via {_gen_info(out)}."


@mcp.tool()
async def widen_scene_image(id: str, aspect: Optional[str] = None, prompt: Optional[str] = None) -> str:
    """Extend (outpaint) an image ALREADY in the scene to a wider frame, in place.

    Use for 'make the painting wider', 'show more of the landscape'. id: the image entity id (in the Live context). aspect: '16:9' (default) or '21:9'. prompt: optional guidance for the new area.
    """
    out = await _post("/outpaint_image", _body(id=id, aspect=aspect, prompt=prompt))
    if not out.get("ok"):
        return f"Couldn't widen {id!r}: {_reason(out)}."
    return f"Extended image {id} → {out['image_id']} via {_gen_info(out)} (aspect {aspect or '16:9'})."


@mcp.tool()
async def skybox_from_scene_image(id: str) -> str:
    """Turn an image ALREADY in the scene into the surrounding 360° sky.

    Use for 'make that painting the sky', 'put me inside that scene'. id: the image entity id (in the Live context).
    """
    out = await _post("/skybox_from_image", {"id": id}, timeout=200.0)
    if not out.get("ok"):
        return f"Couldn't build a skybox from {id!r}: {_reason(out)}."
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
    """Update an entity's color and/or transform. Only provided fields change. (For a real
    surface, style_surface is the direct route — this works too, but it can only recolor.)"""
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
    note = ""
    if color is not None and await _is_real_surface(id):
        # A real surface's fill only draws when material.visible is explicitly true — otherwise it
        # falls back to the global default, which is FALSE in AR (client applyRealVisibility). So a
        # bare colour change lands on a hidden mesh: the patch succeeds, the tool reports success, and
        # nothing whatsoever appears. `style_surface` avoids this by always setting visible first
        # (server.style_surface); do the same here rather than report a change nobody can see.
        changes["components.material.visible"] = True
        changes["components.material.src"] = ""      # clear any texture so the colour shows
        note = (" (a real surface, so it was also made visible and any texture cleared — "
                "style_surface is the direct route for this)")
    patch = await _post_patch([{"op": "update", "id": id, "set": changes}])
    return f"Updated {id!r}: {changes}. World rev {patch['rev']}.{note}"


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
