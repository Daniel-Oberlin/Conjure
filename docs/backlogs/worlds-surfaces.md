# Worlds ↔ surfaces — backlog

Unfinished work, future directions, and known problems for how a world presents a space. The current
state is [`docs/specs/worlds-surfaces.md`](../specs/worlds-surfaces.md); the reasoning behind rejected
alternatives is [`docs/decisions.md`](../decisions.md).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems — verified against the code

### `environment.room` is the wrong name for what it holds

The key holds this world's **presentation of a space** — visibility, passthrough, per-surface style
overrides, label and outline prefs. Calling it `room` is misleading twice over: the thing it presents is
a *space* (which routinely contains several rooms), and it sits directly beside
`environment.space`, which is a completely different thing (a ref string naming *which* space).
`server.py:2633` has the two adjacent:

```python
env.pop("space", None)              # which space this world attaches to
room = env.setdefault("room", {})   # how this world renders it
```

Proposed: rename to **`environment.spacePresentation`** (camelCase, matching `surfaceStyles` /
`defaultSurfaceVisible` / `floorPolygon`). Not `environment.space` — that would collide with the ref and
give one key a string in old documents and a dict in new ones, the worst migration shape.

Cost: ~67 dotted paths across 11 sub-keys, the client's `roomState` mirror, and 4 world files on disk (2
carrying `surfaceStyles`). The dotted paths are **patch-op keys** (`env_set["room.authorityClientId"]`,
`server.py:2933`), so client and server must ship together or read both names for one release.

Worth folding into the same change: move `boundary` and `authorityClientId` out from under it. Boundary
belongs with the space geometry it is copied from; `authorityClientId` is coordination state sitting in
a presentation bag. That is what makes the new name true rather than merely better.

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
| **outline** | a semantic surface's border | ✅ built, on by default (`room.edgesVisible`) |
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

`environment.room.worldAnchor` has **zero occurrences**. The design published the world frame into the
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
