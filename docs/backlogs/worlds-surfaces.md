# Worlds ↔ surfaces — backlog

Unfinished work, future directions, and known problems for how a world presents a space. The current
state is [`docs/specs/worlds-surfaces.md`](../specs/worlds-surfaces.md); the reasoning behind rejected
alternatives is [`docs/decisions.md`](../decisions.md).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems — verified against the code

### The `authored` immersion mode has nothing to author with

`set_immersion("authored")` sets passthrough off and hides the captured surfaces — the correct axes for
showing *replacement* geometry built to the real footprint. But no tool builds that geometry. There is
no `build_room`, so the mode currently just produces a dark empty space.

What it needs: extrude `boundary`'s floor polygon to the ceiling height into wall/floor/ceiling entities
and style them, constrained to the real footprint so the user cannot walk into a real wall. Blocked in
practice by the boundary defects in [`backlogs/spaces.md`](./spaces.md) — a single polygon at a
hard-coded 2.6 m height is not a safe footprint to extrude in a multi-room space.

### Only the `outline` edge layer exists

The design has three independently-toggleable edge layers. One is built:

| Layer | What | State |
|---|---|---|
| **outline** | a semantic surface's border | ✅ built, on by default (`spacePresentation.edgesVisible`) |
| **feature** | door/window opening outlines | not built |
| **tessellation** | wall-subdivision seams / mesh wireframe | not built |

So "hide the internal seams but keep the outline" cannot be expressed today.

### Wall-art normals are inconsistent with every other surface

Captured surfaces point **outward** from the space, so interior-facing content is oriented along
`−normal`. Wall-art arrives **inward** — roughly 180° from its host wall. `_face_room` and `matchRef`
both need a coincident-flip fallback because of it, and it is the mechanism behind the recurring
"picture lands behind its wall" bug. Documented as a case study in
[`docs/investigations/`](../investigations/); the durable fix would be normalising the convention at
ingest rather than compensating at every consumer.

---

## Not built — the mesh tier

The whole progressive-refinement design is unimplemented. `mesh-detection` and `detectedMeshes` have
**zero occurrences** in the codebase; `meshRevision` and `room.occlusion` likewise.

What it was for: start coarse with planes, and on request (`refine_room_scan`) stream simplified,
semantically-segmented mesh deltas up as the user looks around, so fidelity climbs while the director's
edits keep working. The load-bearing promise is **uniform editability** — each mesh segment maps to the
*same* surface entity that existed as a coarse plane (`meta.meshSegment`), so "make the wall blue" is
identical whether the geometry underneath is a plane or a dense mesh.

Consequences of it being absent:

- `refine_room_scan` and `show_room_labels` do not exist as tools.
- The `room.geometry = planes | mesh | both` and `room.occlusion = off | depth | mesh` display modes do
  not exist.
- `meta.meshSegment` is vestigial (2 occurrences, nothing reads it).

Note the two representations were always meant to **coexist**, not supersede: mounting and addressing
resolve against *planes* even when the *mesh* is what is drawn, because planes are the stable
semantic handles. There is no continuum of LODs — Quest gives planes, one global mesh, and depth, and
the design mixes those three layers rather than climbing rungs.

Real-world depth occlusion is tracked separately and is partly built — see
[`backlogs/occlusion.md`](./occlusion.md).

---

## Not built — the shared frame schema

`environment.spacePresentation.worldAnchor` has **zero occurrences**. The design published the world frame into the
model so a secondary headset (or a returning session) could localise to it, with `method:
spatial-anchor | registration | manual`.

It is worth recording *why* this is not simply pending: the local-first geometry model
([`specs/spaces-geometry.md`](../specs/spaces-geometry.md)) removed the shared render frame altogether.
`#world-root` is identity, each client renders its own capture, and registration's only output is id
correspondence. So a published shared anchor no longer has a consumer for *rendering*. If it comes back
it will be for **cross-session persistence**, not co-location — and that is a different requirement with
a different shape.

---

## Future directions

### What is behind a window in full VR

In AR passthrough a cut opening is free — you see the real world through it. With passthrough off, a
window or door opening looks into nothing. It wants a sky or skybox backdrop behind the shell.

### Per-agent presentation defaults

An agent could declare how it wants a space presented on entry — broad rules over the base targeted by
semantic, id, or `all`. Tracked as `room_view` in [`backlogs/agents.md`](./agents.md) since it belongs
to the agent definition; noted here because this is the layer it would write to.

### Undo for surface styling

`apply_patch` already records an inverse for every op, so the material of a restyled surface is
recoverable in principle. The missing pieces are the same as for state generally: **action grouping**
(one director turn = one undoable unit, not N patches) and **origin filtering** (never undo an automatic
re-capture or re-anchor). See [`backlogs/agents.md`](./agents.md).

---

## Harvested from the old flat `docs/backlog.md` (2026-08-26)

*Items filed against this subsystem before the per-area backlogs existed. Status lines
and dates are as originally written; none has been re-verified against today's code.*

## Image upside-down when framed in a window (wall art is fine)

**Status:** open · noticed 2026-06-24 · **needs Quest testing**

**Symptom:** `place_image(on_surface=<window>)` hangs the image **upside down**; on a wall-art surface
it's correct and quick.

**Cause:** `place_image` (`server.py:1069`) **adopts the captured surface's `rotation` verbatim** for
the image plane. Wall-art planes are captured upright/inward-facing; **window** planes come back with
a flipped orientation (the headset's plane detection inverts their up/normal), so the image inherits
the flip. Capture-side quirk, not the placement math per se.

**Proposed fix:** don't trust the captured rotation for image orientation — compute an **upright,
room-inward-facing** mounting rotation (normal toward the room interior, zero roll) from the surface
position + room center, used for *all* on-surface placements. Alt: normalize window/door surface
rotations at ingest so "up" is consistent. Either way, **verify on a Quest** (window orientation is
device/capture-dependent; can't confirm blind).

---
