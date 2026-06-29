# Shared room layer — design

**Status:** DESIGN, now **subsumed by `spaces-and-users-plan.md`** — a *space* IS this shared room
layer, made first-class (user-owned + geolocated). This doc remains the detailed treatment of the
geometry/style **compose-vs-persist** mechanics (durable world doc vs. live geometry); the spaces plan
is the umbrella. Extends `persistence-model.md` §6 and `room-model.md`. Resolves the per-world-room
problem and the class of "session state frozen into the durable world doc" regressions.

## 1. Motivation

When world persistence landed, the **real room** (captured surfaces + boundary) was stored *inside each
world's document* — `.cache/worlds/<scope>/<name>.json` carries its own copy of the `meta.real`
surfaces and `environment.room`. That turned out to be the root of a recurring class of bug, because
the captured room is **live session state**, not durable per-world content. Symptoms we hit live:

- **Stale / sparse rooms.** `default` was captured during a tracking blip and froze at 2 surfaces;
  switching back to it showed those 2 forever, because the room doesn't re-derive from the live
  headset — it's whatever was baked into that world's doc.
- **Re-capture churn.** The headset streams the full room every ~2 s with `replace:true` into the
  *active* world, so the same physical room is redundantly re-captured into every world you visit.
- **The room doesn't follow you.** Each world shows its own frozen snapshot instead of the room you're
  physically standing in.
- **Session-state lockouts.** `room.authorityClientId` (the one headset allowed to report geometry) was
  persisted; a new client id after a restart no longer matched, so **every capture was silently
  rejected** and the room appeared lost (it wasn't — just un-renderable / un-updatable). Fixed
  separately by clearing authority on activation, but it's the same underlying mistake: durable doc
  holding live state.

The fixes so far have been one-off (clear authority, etc.). This doc proposes the **structural** cure:
stop storing the live room in the durable per-world doc at all.

## 2. The contract — durable vs. session/live

The audit of what a world doc persists, split cleanly:

| Goes in the **durable world doc** (per world) | Lives in the **shared live room layer** (per scope) |
|---|---|
| `id` / `name` / `description` / `tags` / `rev` / `budget` / `anchors` / `connections` | real surface **geometry** (`transform`, `surface.extent`/`holes`, `semantic`, `friendly_id`) |
| **placed objects** (non-`real` entities — the built content) | room **boundary** (floor polygon, height) |
| `environment.sky` (skybox + grounded/height/radius) | `room.active` (derived: is a live room present?) |
| per-world **display prefs**: `room.edgesVisible`, `defaultSurfaceVisible`, `annotations`, `annotationDims` | `room.authorityClientId` (which live headset owns capture) |
| per-world **surface style overrides** (see §3) | — |

Two things worth calling out:

- **Display prefs stay per-world and durable** — `edgesVisible`, `defaultSurfaceVisible`, and
  `annotations`/`annotationDims` are deliberate per-world choices (the constructor's whole job is to set
  edges on for the builder, off for the dungeonmaster). **Annotations state is kept** (persisted
  per-world) by design — turning labels on in a world should survive a reload.
- **Surface *styling* is per-world; surface *geometry* is shared.** "Make the couch green in the
  bladerunner world but not in default" must work, even though both worlds show the *same physical
  couch*. So geometry is shared, and each world keeps a small **style overlay** keyed by surface id.

## 3. Design

### 3.1 Data model

**Shared room layer** — one per scope, persisted at `.cache/worlds/<scope>/_room.json`:

```jsonc
{
  "surfaces": [ { "id": "real_couch_41", "semantic": "couch", "transform": {...},
                  "components": { "surface": { "extent": [...], "holes": [...] } } } ],
  "boundary": { "floorPolygon": [...], "height": 2.6 }
}
```

Geometry + default material only. The id is the canonical surface id (the room layer is now the single
source of truth for ids, which also stabilizes them — today each world can diverge).

**Durable world doc** — `.cache/worlds/<scope>/<name>.json`, NO real-surface geometry:

```jsonc
{
  "entities": [ /* placed objects only */ ],
  "environment": {
    "sky": {...},
    "room": { "edgesVisible": true, "defaultSurfaceVisible": false, "annotations": true,
              "surfaceStyles": { "real_couch_41": { "color": "green", "visible": true } } }
  }
}
```

`surfaceStyles` is the per-world overlay: surface id → material overrides (`color`/`visible`/`opacity`/
`src`). Absent id ⇒ that surface renders with the layer's default material in this world.

**Session/runtime** (not persisted, or persisted-but-reset): `room.active`, `room.authorityClientId`.

### 3.2 The key simplification — keep the *live* doc composed

To avoid a deep rewrite of the client and the patch protocol, **the in-memory `store.doc` stays a fully
composed world** — exactly the shape the client and `apply_patch` already use. The split is **only in
persistence and load**:

- **On load / switch:** compose `store.doc` =
  `world_doc.entities (placed)` + `[ apply_overlay(geom, world.surfaceStyles[geom.id]) for geom in room_layer.surfaces ]`,
  with `environment` = world's prefs + the layer's `boundary`/`active`. The client sees one normal
  snapshot; it never knows about the split.
- **On capture (`ingest_room`):** update geometry in the **room layer**, then re-compose the affected
  real entities into `store.doc` (preserving each world's style overlay) and broadcast the patch as
  today. Authority/active set on the layer (session).
- **On styling (`style_surface` / `show_surface` / `texture_surface`):** apply to `store.doc` live as
  today **and** record the override in the active world's `surfaceStyles` for persistence.
- **On autosave:** route — real-surface geometry + boundary → the room layer file; placed objects +
  `surfaceStyles` + prefs → the world file. (Compose for live; decompose for save.)

So the live representation and every existing surface/patch/director path are **unchanged**; we add a
compose-on-load and a decompose-on-save, plus the standalone room-layer store.

### 3.3 What this buys

- **The room follows you.** Switch worlds → same shared geometry, different placed objects + style
  overlay. No re-capture; the surfaces appear instantly with this world's colors.
- **No more stale/sparse worlds.** A world can't freeze at 2 surfaces — geometry comes from the live
  layer, which the current headset keeps current. `default` and `new-room` show the same real room.
- **No re-capture churn into N worlds.** Capture writes one shared layer.
- **Session state can't be frozen per-world** — `active`/`authority` live on the layer as runtime state,
  killing the whole regression class (authority lockouts, etc.) at the root.
- **Stable surface ids** — one canonical id set in the layer, instead of per-world divergent captures.

### 3.4 The room layer reflects the *current physical room*

The live layer is "the physical room the headset is in right now," shared by all of a scope's worlds.
A virtual world (its placed neon signs, furniture) is therefore **portable across physical spaces** —
you can load the bladerunner world in any real room and its content overlays your real surfaces. This
is the right model for an AR holodeck (virtual content is per-world; the real room is wherever you
are). See §6 for the wrinkle this leaves open (placed-object anchoring).

## 4. Capture, styling, switching — end to end

- **Capture:** headset → `POST /room` → room-layer geometry updated (`replace` + absence-debounce as
  today, but on the layer) → affected entities re-composed into `store.doc` → patch broadcast. Authority
  checked against the layer's live authority.
- **Style:** director → `style_surface(couch, green)` → `store.doc` couch material set (live) +
  `world.surfaceStyles["real_couch_41"] = {color: green, visible: true}` (persisted per-world).
- **Switch A→B:** persist A (placed + styles + prefs) and the layer; load B's placed + styles + prefs;
  re-compose `store.doc` from the **same** layer + B's overlay; broadcast snapshot. The real room stays;
  the couch reverts to whatever B says (or default).

## 5. What changes / what doesn't

**Changes (server-side, mostly persistence/compose):**
- New `RoomLayer` store (`.cache/worlds/<scope>/_room.json`): load/save geometry + boundary.
- `ingest_room`: write geometry to the layer; re-compose; authority/active on the layer.
- `style_surface` / `show_surface` / `texture_surface`: also write `surfaceStyles` on the active world.
- World load/switch (`_boot_world` / `_switch_to`): compose `store.doc` from layer + world.
- Autosave / `WorldStore.save`: decompose (geometry → layer; rest → world).

**Does NOT change:**
- The client (`conjure-client.js`, room-snap, registration) — it still gets composed snapshots/patches.
- The patch protocol / `apply_patch` / the live `store.doc` shape.
- The director tools' interfaces (`style_surface`, `query_world`, the room context resource).
- The world-management tools (`list/new/switch/delete`).

## 6. Edge cases & open questions

- **Dangling style overrides.** A `surfaceStyles` id whose surface no longer exists in the live layer
  is a harmless no-op; prune opportunistically.
- **Different physical room than a world was made in.** Geometry is current-room; a world's *placed
  objects* were positioned against a previous room's surfaces and may not line up. Out of scope here —
  proper fix is anchor-relative placement (`anchors`/`parent` already in the schema). Note, don't solve.
- **Multiple physical rooms over time.** One live layer per scope = "the current room." If we later want
  per-physical-room layers (recognize you're back in the kitchen), the layer key generalizes from
  `<scope>` to `<scope>/<room-id>`; deferred.
- **Multi-headset.** Authority on the layer is the concurrency guard (one reporter); unchanged in spirit.
- **`room.active` semantics.** Derived from "layer has surfaces (and a live authority this session)."
  Persisted-but-reset is acceptable interim.

## 7. Migration from today's per-world surfaces

Existing world docs embed real surfaces. One-time, on first load under the new model:
1. If the scope's room layer is empty, **seed it** from the most complete existing capture (e.g.
   `new-room`'s 45 surfaces, not `default`'s 2) — geometry + boundary.
2. For every world, extract any **non-default surface materials** into that world's `surfaceStyles`, then
   **drop the real-surface geometry** from the per-world doc.
3. Clear `authorityClientId` (already done on activation).

Idempotent; after migration the per-world docs hold only placed objects + styles + prefs.

## 8. Incremental build plan

1. **RoomLayer store + compose/decompose**, seeded by migration; capture writes the layer; load composes.
   (No behavior change for the user yet — same rendered room, sourced differently.) + tests.
2. **Per-world `surfaceStyles` overlay**: style tools write it; compose applies it; switching shows
   per-world colors over shared geometry. + tests.
3. **Session-ize `active`/`authority`** on the layer; stop persisting them per-world.
4. (Optional) prune dangling overrides; tidy.

Each step keeps the live `store.doc` composed, so the client and director paths stay green throughout.
