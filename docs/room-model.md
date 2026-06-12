# Room model — bringing the real room into Conjure (AR / scene understanding)

> Design for the next phase (roadmap **Phase 5**). Realizes the VR+AR / passthrough and
> anchor-relative threads already in [vision.md](./vision.md) and [spec.md](./spec.md) §3, and slots
> into the capability-tier model ([decisions.md](./decisions.md) #11). Confidence markers: 🟢 settled ·
> 🟡 shape clear, details open · 🔴 open question.

## 1. Goal & scope

The Quest 3 already builds a model of your room (Space Setup planes + a scan mesh + semantic labels).
This phase brings that model **into Conjure's world as first-class, editable geometry**, so that:

1. **You can see your real room** — `immersive-ar` passthrough, real walls visible.
2. **The room is renderable *virtual* geometry too** — walls/ceiling/floor/furniture can be **shown or
   hidden** individually, and the director can set their **color, texture (e.g. a generated image),
   opacity, and material**, exactly like any other entity.
3. **A full immersion spectrum** — be fully in a **virtual room model** (passthrough off, surfaces
   rendered), fully in **AR** (passthrough on, surfaces invisible), any **mixture** (e.g. real walls +
   a virtual ceiling), or **hide the room entirely** for the original **unbounded VR** holodeck.
4. **The director is room-aware** — it knows the room boundary, so new models appear **inside the room**
   (not through walls), and it can **mount** content on real surfaces by semantic label.
5. **The director can author its own room** — hide the captured surfaces and **build a replacement room
   that fits the real boundary** ("make this a marble cathedral" at your room's actual dimensions, so
   you never walk into a real wall).
6. **Progressive refinement is uniform** — as the coarse planes refine into a dense **mesh** in the
   background (on request), the director **edits the mesh the same way** (by semantic surface: color,
   texture, visibility) as it did the simplified boundaries.
7. **Multiple headsets, one authority** — several headsets can be in the room, but exactly **one is the
   source of truth** for the room model; the others share it.

**Out of scope (later layers):** dense-mesh physics, full real-time occlusion polish, cross-session
**persistent anchors** (designed-for — §8/§11), generated-mesh authored rooms (leans on Phase 7
mesh-gen; boundary-extruded authored rooms are in scope here).

## 2. What the Quest exposes (WebXR) 🟢

Conjure is **WebXR in the Quest Browser**, so room understanding comes through standard WebXR modules
(no native app). All supported on Quest 3 / Meta Quest Browser:

| Feature | Gives us | Used for |
|---|---|---|
| `immersive-ar` session | Passthrough (real room visible behind virtual content) | the AR end of the spectrum |
| `plane-detection` | Floor/walls/ceiling/tables as **polygons + pose** | coarse surfaces, boundary, mounting |
| `mesh-detection` | The room **scan mesh** (triangles), updated over time, **semantically segmented** | refined surfaces, occlusion, colliders |
| **semantic labels** | `semanticLabel` on each plane/mesh (`wall`, `table`, …) | addressing surfaces for display/style/interaction |
| `depth-sensing` | Per-frame depth map | real-time occlusion (client-only) |
| `anchors` | Pin content/origin to a real spot; persists | stable placement, multi-headset alignment, persistence |
| `hit-test` | Raycast onto real surfaces | "place where I point" |

**A-Frame access:** A-Frame doesn't natively wrap plane/mesh detection, so the client adds a small
**custom component** that taps the raw session — `renderer.xr.getSession()`, requests the features, and
reads `frame.detectedPlanes` / `frame.detectedMeshes` (+ `semanticLabel`) each frame or on change.
Mirrors how the `grid` component is registered in `client/conjure-client.js`.

## 3. Data flow — the reverse channel 🟡

Capture originates in the **client**; today the flow is one-way (server → client patches), and the WS
handler ignores client messages (`server.py` `/ws`, "Phase 0"). This phase adds **client → server**:

```
 Quest WebXR ──capture──▶ authority client ──▶ WS {type:"room", …} ──▶ world server ──▶ broadcast
   (planes, mesh,            throttle+simplify     (or POST /room)        store; one        to all
    labels, boundary)        (authority only)                            authority         clients
```

- **Send up the *semantic layout + boundary* first** (compact); a dense **mesh** is sent **on request**
  and **incrementally** (§7), never per-frame.
- **Only the room authority pushes room data** (§8); the server rejects room updates from others.
- **Transport:** an upstream WS message (`{type:"room", surfaces, boundary, mesh?}`) plus a `POST /room`
  for bulk mesh. Re-broadcast the resulting patches so every client + the director sees one room. World
  stays **server-authoritative** (architecture §6).
- **Coordinate frame:** poses live in a **shared physical frame** (an anchor / Shared-Spaces origin) so
  placed content aligns and multiple headsets agree (§8). Within one session a local reference space is
  fine; cross-session/headset needs the anchor (spec §3 anchor-relative placement).

## 4. The room in the world model (schema) 🟡

Reuse the entity/patch machinery — **each real surface is a normal, stylable entity** tagged `real`, so
it flows through `query_world`, patches, broadcast, *and* the director's existing material edits:

```jsonc
// a captured real surface — visible/hideable + stylable like any entity
{
  "id": "real_wall_3",
  "transform": { "position": [x,y,z], "rotation": [..] },     // pose in the shared physical frame
  "components": {
    "surface":  { "polygon": [[x,z],…], "extent": [w,h] },    // geometry: plane polygon …
    "material": { "visible": false, "shader": "flat", "color": "#888",
                  "src": null, "opacity": 1.0, "side": "double" }   // … director edits this
  },
  "meta": { "real": true, "semantic": "wall", "source": "quest-plane",
            "anchorId": "…", "meshSegment": "wall_3" }          // … or backed by a mesh segment
}
```

```jsonc
// environment block — session/room-wide state
"environment": {
  "passthrough": true,                       // AR camera on/off  (axis 1, §5)
  "room": {
    "active": true,                          // is the room model in effect at all?
    "authorityClientId": "u1",               // the single headset that owns the room (§8)
    "origin": { "anchorId": "…" },           // the shared physical frame
    "boundary": { "floorPolygon": [[x,z],…], "height": 2.6 },   // for placement bounds + authored rooms
    "defaultSurfaceVisible": false,          // global default for captured surfaces (axis 2, §5)
    "meshId": "abc123", "meshRevision": 3    // refined dense mesh asset (optional, §7)
  }
}
```

- `meta.real = true` ⇒ the **director** treats it as physical: it can **restyle/hide** it and **mount**
  onto it, but shouldn't move/delete the real geometry; the **renderer** draws it only when
  `material.visible` (else it's an invisible reference for placement/occlusion/mounting).
- A surface's geometry is **either** a plane `polygon` **or** a `meshSegment` of the refined mesh — the
  **same entity id + material** persist across the coarse→refined upgrade (§7), so director edits stick.
- `environment.room.boundary` is the reusable **spatial constraint**: placement bounds (§6) and the
  footprint for **director-authored rooms** (§5).

## 5. Immersion modes — passthrough × surface-visibility 🟡

The "spectrum" is just **two independent axes**, which makes every mode (and any mixture) fall out:

- **Axis 1 — passthrough** (`environment.passthrough`): is the real room visible via the camera?
- **Axis 2 — surface visibility** (`material.visible` per surface, with `room.defaultSurfaceVisible`):
  are the virtual room surfaces drawn (and how are they styled)?

| Mode | passthrough | surfaces | What you experience |
|---|---|---|---|
| **Virtual room** | off | visible (styled) | A virtual reconstruction of your room — walls/ceiling you can recolor/texture. |
| **AR** | on | hidden (active) | Your real room; surfaces invisible but used for mounting/occlusion/bounds. |
| **Mixed** | on | *some* visible | Real walls + a virtual starry ceiling; or a real floor + virtual walls. Per-surface. |
| **Authored room** | off | captured hidden + **authored geometry shown** | Director-built room (cathedral, holodeck-with-real-dimensions) fit to your boundary so you stay inside real walls. |
| **Unbounded VR** | off | room **inactive** (`room.active=false`) | The original big synthetic holodeck — room ignored, full VR space as before. |

A convenience tool `set_immersion(mode)` sets the axes; the director (or user) can also flip individual
surfaces. **Neutral fallback** (decision #11): a device without passthrough/room data renders the
synthetic holodeck — nothing *gates* on Quest features.

**Director-authored room (§1.5).** Hide the captured surfaces and build a replacement constrained to
`boundary`: extrude the floor polygon to the ceiling height into wall/floor/ceiling entities, style
them (generated stone texture, etc.), keep within the real footprint for safety. A `build_room(style)`
tool does this from the boundary. A fully *generated mesh* room is the richer version and leans on the
Phase-7 mesh generator; boundary-extruded authored rooms are in scope now.

## 6. Semantic info — display, styling & interaction 🟢 intent / 🟡 surface

**Display (text).** Each surface can render its label as a floating overlay (toggleable): "wall",
"table", "window". From `meta.semantic`; also available for the director to *speak* the layout.

**Styling.** Because surfaces are entities with a `material` component, the director restyles them with
the **existing** edit vocabulary — color, opacity, **texture an image onto a wall** (`material.src` =
a procured image id's URL → "make my ceiling a galaxy"), show/hide. No new rendering path; it's the
plane-material work already in `place_image`, applied to `real` surfaces.

**Interaction — mounting + bounds.** The director resolves a **semantic target** to a surface and
places content **on/anchored to it** ("hang this on the wall", "vase on the table", "where I point" via
`hit-test`). And it uses `boundary` for **in-room placement**: new models spawn **inside** the room
volume (not through a wall), resting on the real floor — the server can also clamp/validate placements
against the boundary as a backstop.

## 7. Progressive mesh refinement (background, on request) — uniform editing 🟡

Start coarse, refine on demand, **without changing how the director edits**:

1. **Instant (coarse):** capture **planes + boundary + labels** → push up → usable immediately for
   layout, labels, styling, mounting, bounds.
2. **On request** (`refine_room_scan` / "scan my room in detail"): the authority client enables
   `mesh-detection` and runs a **background loop** — as the user looks around, the system mesh updates
   (`XRMesh.lastChangedTime`); the client streams **changed, simplified segments up**, the server
   updates `environment.room.mesh` + bumps `meshRevision`. Interaction continues; fidelity climbs.
3. **Uniform editability (the key requirement):** the refined mesh is **semantically segmented**, and
   each segment maps to the **same surface entity** (by `meta.meshSegment`) that existed as a coarse
   plane. So "make the wall blue" / "texture the ceiling" / "hide the couch" work **identically**
   whether the room is coarse planes or a dense mesh — the director addresses **semantic surfaces**, not
   raw triangles; only the backing geometry sharpens underneath a stable id + material.

Higher fidelity then also feeds **occlusion** (§10) and physics colliders. Refinement is **opt-in,
cancellable, and authority-only** (battery + bandwidth).

## 8. Multiple headsets — one room authority 🟡 / 🔴 co-location dependency

Several headsets may share the physical room, but **exactly one is the room authority** (owns capture +
refinement of the canonical room model). The world stays server-authoritative; the room model is one
shared object every client reads.

- **Authority selection:** the first headset to capture (or an explicit assignment / the session host)
  becomes authority; the server records `authorityClientId`. **Only its** room updates are accepted;
  others' room captures are ignored. **Handoff** if the authority leaves (promote another headset →
  re-capture or keep the last-known model).
- **Everyone else consumes** the broadcast room model and can still place/edit world content normally
  (server-authoritative); they just don't author room *geometry*.
- **Coordinate alignment (the hard part 🔴):** for other headsets to see the room model correctly, they
  must share the authority's **physical origin** — i.e. **co-location** (Quest "Shared Spaces" or a
  shared anchor). The room's poses live in that shared frame. This makes full multi-headset room sharing
  **depend on co-location** (spec §12, the multi-user co-location milestone, decision #11 extension).
  Single-headset is the baseline; the authority concept is designed in now so multi-user drops in later.

## 8a. Anchored world frame — consistency + persistence 🟢 within-session / 🟡 cross-session

**The problem.** Everything is stored relative to the WebXR **reference space**, whose origin a
recenter (Meta button) / long put-down moves. Re-capturing the room at the new origin keeps the room
aligned but leaves *placed* content (models/images) at old coordinates → the world shears apart. All
content must stay consistent **relative to each other and the real world**.

**The fix (implemented, within-session).** One **WebXR anchor** defines the persistent world origin.
`#world-root` (the container every entity lives under) is positioned at the anchor's pose **every
frame**; all content is stored in **anchor-relative** coordinates. On a recenter only the container
moves (the anchor tracks reality), so the whole world — room, models, images — stays put and mutually
consistent. The room capture multiplies plane poses by the anchor's inverse, so captured coords are in
the same frame and are **stable across recenters** (no more re-capture churn or lost edits). Falls back
to identity (today's behavior) when anchors aren't available (desktop / no support). Client-only —
the world model is frame-agnostic. (`room-capture._updateWorldFrame` in `client/conjure-client.js`.)

**Cross-session persistence (next).** Persist the anchor handle
(`anchor.requestPersistentHandle()` → store the UUID) and restore it next session
(`session.restorePersistentAnchors`/`restorePersistentAnchor`), re-localizing the saved world onto the
same physical anchor. With content already anchor-relative, the world reloads fixed to the real room
(vision's "persistent rooms"; spec §3). Pairs with Phase 6 memory (where the world doc is persisted).

## 9. MCP / director surface 🟡

Coarse/intent-level tools (architecture §8). Real surfaces also appear in `query_world` as
`real`-tagged entities, so the director's existing `update`/material edits already apply to them.

- `set_immersion(mode)` — `virtual_room | ar | mixed | authored | vr_unbounded` (sets the two axes, §5).
- `enter_ar()` / `enter_vr()` — passthrough toggle (a thin alias over `set_immersion`).
- `query_room()` — surfaces + labels + **boundary** (floor polygon, height) so the director reasons /
  describes / places in-bounds.
- `show_surface(id|semantic, visible)` — per-surface visibility (drives the spectrum / mixtures).
- *(style via existing edits)* — recolor/opacity/texture a surface with the same vocabulary as other
  entities (e.g. `material.src` = a generated image → "make the ceiling a galaxy").
- `mount_on_surface(content_ref, semantic | surface_id, where?)` — place a procured image/asset/primitive
  onto a real surface, anchored (builds on `place_image`/`place_asset`).
- `build_room(style, scope?)` — author a replacement room from `boundary` (extrude walls/ceiling/floor,
  style them); hides captured surfaces.
- `refine_room_scan(enable)` — start/stop background mesh refinement (§7); authority-only.
- `show_room_labels(enable)` — toggle the semantic text overlays.

**Persistence (designed-for, later):** anchors + an anchor↔world mapping let a world reload **fixed to
the same physical room** next session (vision's "persistent rooms"; spec §3). `meta.anchorId` reserves
the hook; cross-session anchor storage is a follow-on (Phase 6 memory).

## 10. Occlusion & physics (mostly client-side) 🟡

- **Occlusion:** virtual objects hidden behind real ones — `depth-sensing` (per-frame depth) and/or the
  room mesh as a depth-only occluder. Pure rendering; the depth map **never** goes to the server.
- **Physics colliders:** the simplified room mesh (§7) as static colliders so objects rest on the real
  floor / don't pass through real furniture. Lands with the behavior/physics work (Phase 7).

## 11. Privacy & permissions 🟢

Room geometry is **sensitive**:
- WebXR **permission-gated**: the user must grant the AR features and have **Space Setup**; degrade
  gracefully if denied (→ synthetic holodeck).
- **Be explicit about what leaves the headset:** semantic layout + boundary + (on request) mesh go to
  the (local, user-run) world server; the **depth map does not**. In multi-user, the room model is
  shared with co-located headsets — surface that.
- Capability-tier fallback (#11): room features **never gate** the baseline.

## 12. Build order (slices within the phase)

Each slice is independently demoable in-headset:

- **A — See it + layout + bounds.** `immersive-ar` toggle; suppress synthetic walls; capture **planes +
  boundary + labels**; reverse channel; store as `real` entities. *Demo: enter AR, `query_world` lists
  "wall/floor/table"; new models land inside the room.*
- **B — Visibility + styling + the spectrum.** Per-surface `visible`; `set_immersion` modes (virtual
  room ↔ AR ↔ mixed ↔ unbounded VR); restyle/texture surfaces. *Demo: "show my walls and make them
  blue," "make the ceiling a galaxy," "drop into full VR."*
- **C — Mount + authored room.** `mount_on_surface`; `build_room(style)` from the boundary. *Demo: "hang
  that dragon on my real wall," "turn my room into a cathedral."*
- **D — Progressive refinement (uniform edit).** `refine_room_scan` streaming mesh deltas; surfaces keep
  their ids/materials as geometry sharpens. *Demo: "scan in detail" → edits still target "the wall."*
- **E — Occlusion.** Depth-sensing occluder (client-side).
- **F — Multi-headset + persistence (stretch).** Room authority + co-location alignment; anchor storage
  so a world reloads fixed to the room.

## 13. Open questions / risks 🔴

- **A-Frame ↔ raw WebXR**: confirm plane/mesh/depth access + session lifecycle inside the current
  A-Frame setup on the installed Quest Browser (custom component reading `frame.detected*`). Spike first.
- **Coordinate stability & co-location**: reference-space drift within a session; **shared origin** for
  multi-headset (§8) and cross-session (anchors). Decide when to anchor (capture vs mount time).
- **Mesh volume / throttling & segmentation**: simplification + delta strategy; mapping mesh segments to
  stable semantic surface ids so uniform editing (§7) holds.
- **Schema fit**: `real` stylable entities vs a dedicated `room` block — lean entities for reuse; ensure
  `query_world` doesn't bloat the director's turns (summary vs full geometry).
- **Authority handoff** semantics when the owning headset drops mid-session.
- **Authored-room safety**: always constrain to the real boundary so users don't walk into real walls.

## 14. Test plan (per [testing.md](./testing.md))

- **Tier 1 (unit, free):** reverse-channel message → world-doc patch (real-entity + boundary shape,
  semantic labels, dedupe on re-capture); **authority enforcement** (reject non-authority room updates);
  `set_immersion` axis mapping; surface visibility/material edits; `build_room` boundary-extrusion math;
  in-bounds placement clamp; mesh-segment ↔ surface-id stability across a refine revision.
- **Tier 2 (contract):** the WebXR feature surface we depend on (plane/mesh/depth shapes) — guard
  Quest-Browser API drift.
- **Tier 4 (manual, in-headset):** the slice demos in §12 — enter AR; toggle the spectrum; recolor/
  texture walls; mount on a real wall; author a cathedral at room dimensions; watch refinement climb
  while edits stick; occlusion behind real furniture; two headsets sharing one room model.
