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

**Display (text).** Each surface can render its label as a floating overlay (toggleable): "window
(12)", "wall (3)" — its `meta.semantic` plus a short **friendly id** (`meta.friendly_id`, a small
sequential number assigned on ingest) the user can read off and reference by voice ("make 12 blue").
Dimensions are off by default; `show_annotations(on, dimensions=true)` adds each surface's size. Label
color/alpha are restyleable (`style_annotations(color, opacity)` → env `room.annotationColor` /
`room.annotationOpacity`). From `meta.semantic`/`friendly_id`; also available for the director to
*speak* the layout.

**Display (outline).** Every real surface is outlined by a bright wireframe (a `surface-edges` line
loop, drawn always-on-top so the whole room reads as a wireframe even in AR). It is **on by default**,
independent of the fill, and globally toggleable + restyleable: `show_edges(on)` →
`room.edgesVisible`; `style_edges(color, opacity)` → `room.edgeColor` / `room.edgeOpacity`.

**Styling.** Because surfaces are entities with a `material` component, the director restyles them with
the **existing** edit vocabulary — color, opacity, **texture an image onto a wall** (`material.src` =
a procured image id's URL → "make my ceiling a galaxy"), show/hide. No new rendering path; it's the
plane-material work already in `place_image`, applied to `real` surfaces.

**Interaction — mounting + bounds.** The director resolves a **semantic target** to a surface and
places content **on/anchored to it** ("hang this on the wall", "vase on the table", "where I point" via
`hit-test`). And it uses `boundary` for **in-room placement**: new models spawn **inside** the room
volume (not through a wall), resting on the real floor — the server can also clamp/validate placements
against the boundary as a backstop.

## 6a. Insets, openings & the planes ↔ mesh relationship 🟡 plan

The Quest reports a **door / window / wall-art** as its *own* plane, attached to a parent wall and
near-coplanar with it. Rendered naïvely (each an idealized solid quad, normal depth test) they
**z-fight** the wall and the larger wall quad **occludes** them — you can't see a door as an opening.
The captured polygon is reduced to a bounding-box `extent`, so a wall doesn't carry its own outline —
instead we derive its **openings** from the inset planes that sit in it (see Status below). Two design
decisions follow.

**(i) Two representations, not two LODs.** The semantic **plane model** and the **scan mesh** are
different *kinds* of model of the same room, used by division of labor:

| Tier | Source | Role |
|---|---|---|
| **Planes** | `plane-detection` | logical/authoring layer — labels, mounting, styling, bounds, the director's vocabulary |
| **Depth** | depth API | cheap per-frame occlusion, no geometry |
| **Mesh** | `mesh-detection` (§7) | physical/render layer — literal geometry, exact occlusion, openings already present |

They **coexist**, they don't replace each other: **mounting & addressing always resolve against
planes**, even while the *mesh* is what's displayed (planes stay live but invisible as stable mount/hit
handles). A `room.geometry = planes | mesh | both` display mode and `room.occlusion = off | depth |
mesh` choose what renders / what occludes. There is **no continuum of mesh LODs** — Quest hands us
planes + one global mesh + depth; we mix these three layers rather than climbing rungs. This is the
same "uniform editing over a stable semantic id" promise as §7, viewed from the rendering side.

**(ii) Openings are synthesized in plane mode, inherent in mesh mode.**
- **Plane mode:** the wall is an idealized solid quad, so we *synthesize* the opening — snap insets to
  the wall, then **cut** the wall into a polygon-with-holes (the `holed-wall` geometry). The door/window
  leaf sits *in* the opening as a **material-driven pane** — see-through is just low `opacity`, not a
  discrete open/closed state, and the director adjusts opacity/color/texture as ordinary properties.
  Wall-art is a decal offset off the surface (it does **not** cut a hole — a picture, not an opening).
- **Mesh mode:** the scan **already has** the doorway opening and window recess as real geometry, so the
  door/window *planes* demote to pure **semantic annotations / mount anchors** (invisible fill, label +
  optional outline) layered on the mesh. Same object, different coat — the methodology differs by
  representation but the addressable surface is the same.

**(iii) Edge layers (shared across both worlds).** Edge visibility is layered, identically for planes
and mesh, so "hide the internal seams, keep the outline" means the same thing everywhere:
- **outline** — a semantic surface's border (wall/floor outline): default **on** (current `surface-edges`);
- **feature** — door/window opening outlines: default **on** (they're real features);
- **tessellation** — wall-subdivision seams (plane mode) *or* mesh triangle wireframe (mesh mode):
  default **off**, toggleable.

**Status — openings cut.** At capture the client **snaps** each inset (door/window/wall-art) onto its
parent wall (project center onto the wall plane, adopt its orientation, nudge a couple cm into the
room) **and** — for doors/windows — records the opening on the wall: the inset's rectangle projected
into the wall's local 2-D frame as `holes` (`snapInsets` in `client/room-snap.js`, unit-tested). Those
ride through the model (`surface.holes`) and the wall renders through the **`holed-wall`** geometry —
the rectangle minus the hole rects, triangulated with `THREE.ShapeGeometry` — so you see into the next
room / outside. A door reaching the floor sits flush against the wall's bottom edge (which would break
triangulation), so each opening is clamped a hair inside the outline. Wall-art does **not** cut. The
leaf is a material-driven pane the director edits as plain properties — door seeds translucent
(`opacity 0.25`), window faint glass (`#cfe6ff`, `opacity 0.18`) (`_default_surface_material`).
**Follow-ups:** what's *behind* a window in full VR (a sky/skybox backdrop — free in AR passthrough);
the `feature`/`tessellation` edge layers; depth-API occlusion; the `room.geometry`/`room.occlusion`
display modes; mesh layer (§7).

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

**Geometry-registered frame — stability across the boundary flip (implemented).** The WebXR anchor
alone is *not* enough. Measured on-device: **leaving the room boundary and returning relocalizes the
whole tracking frame** — a single rigid jump of **~167° yaw + ~3 m translation** (gravity preserved),
and the anchor flips with it. Everything stored anchor-relative then moves: every surface re-mints its
id (server `replace` resets friendly numbers + director edits) and placed content rotates off the real
walls. A re-detected plane is a brand-new `XRPlane`, so object-identity caching can't save it either.

The fix uses the **room's own geometry as the source of truth**, not the anchor. We keep a persistent
**reference constellation** of surfaces; each capture, `room-capture._register` solves the single
yaw+translation transform aligning the newly detected planes onto it — recovering yaw from the *shift in
surface-normal directions* (no prior pairing needed, so the ~180° flip is fine), then translation, then
scoring candidates by position inliers. That transform **is** the world frame (`_Tmat`): surfaces
re-inherit their ids by nearest-reference match, and `#world-root` is parked at `_Tmat`⁻¹ so **placed
content stays locked** too. Validated against real before/after captures: **43/44 surfaces keep their id
across the flip** (vs 1/47 before). The WebXR anchor is now just the **bootstrap** frame for the first
capture; a `reset` event forces immediate re-registration. This is exactly the §8b registration path, so
the same machinery serves multi-user co-location.

**How `_register` recovers the frame — a Hough/RANSAC-style vote (`client/room-snap.js`).** The hard
part is a chicken-and-egg: you need the transform to know which detected plane is which reference
surface, but you need correspondences to find the transform. It's solved by **consensus, not
proximity** — so it works no matter how far the frame jumped (the ~167° flip included), and it never
uses the nearest-surface 0.5 m match (that's the *post*-registration id step in `conjure-client.js`
Pass B). The trust gate upstream guarantees a level floor, so gravity/pitch/roll are pinned and the only
unknowns are **yaw about up + an x/z translation**:

1. **Yaw — by normal-direction histogram.** For every same-semantic, similar-size *vertical* pair
   (current ↔ reference), record the delta of their normal yaws. A global rotation θ shifts *every* true
   pair's normal by the same θ, so real pairs pile into one 6° histogram bin while mismatches scatter —
   the modal peak(s) are the candidate yaw(s). Position-free, so any offset is fine.
2. **Translation — by grid vote.** For each candidate yaw, rotate the current positions and bin the
   implied `ref.pos − R·cur.pos` over same-size pairs into a 0.25 m grid; the densest cell is the
   consensus translation.
3. **Score — by inliers.** Build the (yaw, translation) transform, project all planes, count how many
   land within 0.4 m of a same-semantic reference; keep the best candidate, and **accept only if ≥ 4
   inliers and ≥ 40 % of detected planes** — else return no-lock (hold the last good frame / passthrough).

Because acceptance requires a genuine consensus, **a failed registration doubles as a "you're not in
this space" signal**: a different physical space — or too sparse a capture (fewer than 3 reference
surfaces / voting pairs) — produces no dominant peak and few inliers. That's the seam a future
load-time space-consistency check (`spaces-and-users-plan.md` §7) would build on, with the caveat that
"different space" and "bad tracking" both surface as the same no-lock.

**Cross-session persistence (next).** Persist the anchor handle
(`anchor.requestPersistentHandle()` → store the UUID) and restore it next session
(`session.restorePersistentAnchors`/`restorePersistentAnchor`), re-localizing the saved world onto the
same physical anchor. With content already anchor-relative, the world reloads fixed to the real room
(vision's "persistent rooms"; spec §3). Pairs with Phase 6 memory (where the world doc is persisted).

## 8b. Shared world frame — multi-user co-location 🟡 design

> **Now the technical core of `spaces-and-users-plan.md` §8.** Key correction to the marker below: this
> does **not** require a platform shared-anchor. A guest registers its own planes onto the **same
> persistent space geometry** (§8a, the register vote), solving its own `_Tmat` into the shared
> reference frame — so content co-locates with no Quest "Shared Spaces" dependency.
>
> **Implemented (register-only guests).** `room-capture` now branches on authority: the active world's
> owner authors as before; everyone else is **register-only** — it re-seeds its reference wholesale from
> the authoritative broadcast each capture, solves `_Tmat`, pins `#world-root`, and **never** establishes,
> lerp-mutates, mints, or posts geometry. This removes the feedback-drift a guest used to cause by
> evolving its local copy of the shared reference (the "world drifts more over time" symptom). Presence
> avatars are also done. **Remaining work: matcher robustness** for the guest's partial/extra plane set
> (the register vote must lock on partial overlap from a different vantage).

**One model, N perceptions.** There is exactly **one** world model (the server doc) in exactly **one**
coordinate frame (the authority's anchor frame, §8a). A secondary headset does **not** build its own
model — it *consumes* the shared one (§8: only the authority authors room geometry). It has a
*perception* (its own WebXR planes + tracking origin), not a model. So "does the secondary's model map
onto the world model?" collapses to a single rigid transform:

> **T** = (secondary's tracking space) → (authority's anchor frame)

With **T**, the secondary renders identically to the authority: it parks its `#world-root` at the shared
frame and draws everything anchor-relative — the **exact mechanism §8a already built**. Note `T` *is*
`room-capture._anchorInv` (refSpace → anchor frame); the only thing that differs per headset is **how
that transform is obtained**, not how it's used. Room **ids never differ**: they're authored once by the
authority and broadcast; secondaries never generate them (the position-derived id scheme in §4 assumes a
single writer — and there is one).

**The gap.** Today the frame lives *only* inside the authority client's session (`room-capture._anchor`;
the server stores `authorityClientId` but no anchor/origin — the model is frame-agnostic, §8a). Nothing
a secondary (or a re-joining authority) can localize to. Multi-user requires **promoting the frame to
shared, resolvable state** — the *same* promotion cross-session persistence needs, so one investment
serves both.

**Schema (design).** Publish the frame into the model:
```jsonc
environment.room.worldAnchor = {
  "id": "wa_<uuid>",                 // logical id of the shared frame
  "method": "spatial-anchor" | "registration" | "manual",
  "authorityClientId": "<creator>",
  "handle": "<platform shared/persistent anchor uuid>",  // spatial-anchor method
  "createdRev": <int>
  // registration/manual carry no handle: each client derives T itself (see below)
}
```

**Client flow (design).** Factor `room-capture` into *obtain the world anchor* → *use it* (the "use"
half — pin `#world-root` every frame — already exists and is shared verbatim):
- **Authority:** create the anchor, `requestPersistentHandle()`, publish `worldAnchor{handle,…}`. (Its
  own render path is unchanged.)
- **Secondary:** read `worldAnchor` from the snapshot → `restorePersistentAnchors([handle])` → an
  `XRAnchor` in *its* refSpace → assign it as `_anchor`; the existing per-frame `_updateWorldFrame` does
  the rest. Same call as cross-session restore, so the two features share a code path.

**Obtaining T — layered, decreasing reliance on platform features:**
- **(a) Native shared spatial anchors** — authority shares a Meta colocation/shared anchor; secondary
  *resolves* it → `T` directly. Cleanest. **Biggest risk:** WebXR anchors are session/device-local and
  cross-device sharing is Meta-specific — it **may not be exposed to the browser/WebXR**, which could
  force a native shell or block (a). *Spike this before committing.*
- **(b) Geometry registration (strong fallback, no special API)** — the secondary runs its own plane
  detection and registers its local walls/corners against the **broadcast room model** (match normals +
  corner correspondences → solve the rigid `T`, write it straight into `_anchorInv`). Reuses the room as
  its own fiducial; costs compute and is ambiguous in symmetric/empty rooms.
- **(c) Manual / marker calibration (last resort)** — a printed marker or a two-point "touch here, then
  here" gesture → `T`. Always works; least magical.

**Invariants that make this safe:** ids stay **authority-owned** (do *not* let secondaries contribute
geometry, or the single-writer id assumption breaks); **drift handling is free** — every headset
re-reads its shared-anchor pose each frame and re-pins `#world-root`, so independent tracking drift and
recenters self-correct per device, exactly as for the authority. **Verdict:** the *data* architecture is
already multi-user-ready (single model, single frame, authority-owned ids, anchor-relative render that
generalizes per-device); the only missing piece is frame-promotion + a way for secondaries to localize
(`T`) — well-contained, and the §8 authority concept is the right shape to receive it.

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
- **F — Multi-headset + persistence (stretch).** Room authority + co-location alignment (the shared
  world frame + `T` strategies, §8b); anchor storage so a world reloads fixed to the room.

## 13. Open questions / risks 🔴

- **A-Frame ↔ raw WebXR**: confirm plane/mesh/depth access + session lifecycle inside the current
  A-Frame setup on the installed Quest Browser (custom component reading `frame.detected*`). Spike first.
- **Coordinate stability & co-location**: reference-space drift within a session; **shared origin** for
  multi-headset (§8b) and cross-session (anchors). Decide when to anchor (capture vs mount time). **Open
  unknown:** is cross-device shared/persistent anchor *resolution* exposed to WebXR in the Quest Browser,
  or only to native SDKs? Gates strategy (a) vs the (b) registration fallback — spike before committing.
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
