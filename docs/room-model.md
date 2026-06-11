# Room model — bringing the real room into Conjure (AR / scene understanding)

> Design for the next phase (roadmap **Phase 5**). Realizes the VR+AR / passthrough and
> anchor-relative threads already in [vision.md](./vision.md) and [spec.md](./spec.md) §3, and slots
> into the capability-tier model ([decisions.md](./decisions.md) #11). Confidence markers: 🟢 settled ·
> 🟡 shape clear, details open · 🔴 open question.

## 1. Goal & scope

The Quest 3 already builds a model of your room (Space Setup planes + a scan mesh + semantic labels).
This phase brings that model **out of the headset and into Conjure's world**, so that:

1. **You can see your real room** — `immersive-ar` passthrough, with the synthetic holodeck void/grid
   replaced by your actual walls; virtual content sits *in* the room.
2. **Semantic info is usable** — surfaces carry labels (`wall`, `floor`, `table`, `couch`, `door`,
   `window`, `screen`, …) that can be **displayed as text** and **targeted for interaction** ("mount
   this picture on the wall", "put a vase on the table").
3. **The mesh can be progressively refined** — start from coarse planes (instant), and on request keep
   **refining the room mesh in the background** as the user looks around, upgrading fidelity without
   blocking interaction.

**In scope:** capture → reverse channel → room representation in the world model → passthrough render
→ semantic display + mount-on-surface → on-demand progressive mesh refinement → director tools.
**Out of scope (later layers):** dense-mesh physics, full real-time occlusion polish, cross-session
**persistent anchors** (designed-for, not built here — see §8), co-located multi-user room sharing.

## 2. What the Quest exposes (WebXR) 🟢

Conjure is **WebXR in the Quest Browser**, so room understanding comes through standard WebXR modules
(no native app). All supported on Quest 3 / Meta Quest Browser:

| Feature | Gives us | Used for |
|---|---|---|
| `immersive-ar` session | Passthrough (real room visible behind virtual content) | "see the room" |
| `plane-detection` | Floor/walls/ceiling/tables as **polygons + pose** | coarse layout, mounting targets |
| `mesh-detection` | The room **scan mesh** (triangles), updated over time | fidelity, occlusion, colliders |
| **semantic labels** | `semanticLabel` on each plane/mesh (`wall`, `table`, …) | display + interaction |
| `depth-sensing` | Per-frame depth map | real-time occlusion (client-only) |
| `anchors` | Pin content to a real spot; persists | stable placement, future persistence |
| `hit-test` | Raycast onto real surfaces | "place where I point" |

**A-Frame access:** A-Frame doesn't natively wrap plane/mesh detection, so the client adds a small
**custom component** that taps the raw session — `renderer.xr.getSession()`, requests the features in
`requiredFeatures`/`optionalFeatures`, and reads `frame.detectedPlanes` / `frame.detectedMeshes` each
frame (or on change). This mirrors how the existing `grid` component is registered in
`client/conjure-client.js`.

## 3. Data flow — the reverse channel 🟡

Capture originates in the **client**; today the data flow is one-way (server → client patches), and
the WS handler explicitly ignores client messages (`server.py` `/ws`, "Phase 0"). This phase adds the
**client → server** path:

```
 Quest WebXR ──capture──▶ client (custom component) ──▶ WS msg {type:"room", …} ──▶ world server
   (planes,                  throttle + simplify          (or POST /room)            store in world
    mesh, labels)                                                                    doc; broadcast
                                                                                     to other clients
```

- **Send up the *semantic layout*, not raw frames.** Labeled planes + furniture boxes are compact and
  are what the **director** needs. A dense mesh is 10k–100k triangles — sent **once on request** and
  **incrementally** thereafter (§7), never per-frame.
- **Transport:** extend the existing WebSocket with an upstream message (`{type:"room", surfaces, mesh?}`),
  or a `POST /room` for bulk mesh. Re-broadcast the resulting world patches so every connected client
  (and the director) sees the same room. Keeps the world **server-authoritative** (architecture §6).
- **Coordinate frame:** geometry is in the client's WebXR reference space; poses are stored in that
  space so placed virtual content aligns. Stable within a session; cross-session stability needs
  **anchors** (§8, spec §3 anchor-relative placement).

## 4. The room in the world model (schema) 🟡

Reuse the entity/patch machinery — real surfaces become **entities tagged real** so they flow through
`query_world`, patches, and broadcast unchanged:

```jsonc
// a real surface entity (pushed up from the headset)
{
  "id": "real_wall_3",
  "transform": { "position": [x,y,z], "rotation": [..] },   // pose in the session reference space
  "components": { "surface": { "polygon": [[x,z],…], "extent": [w,h] } },
  "meta": { "real": true, "semantic": "wall", "source": "quest-plane", "anchorId": "…" }
}
```

- `meta.real = true` tells the **renderer** "this is a *reference* to a real surface — don't draw a
  solid wall (passthrough already shows it), but you may attach labels, mount points, anchors, or
  colliders to it." It tells the **director** "this is physical — reason about it, mount onto it, but
  don't move/delete it."
- `meta.semantic` carries the label for display + interaction.
- **Environment flag:** `environment.passthrough: true|false` toggles AR vs the synthetic holodeck.
- **Dense mesh** (when refined, §7) is stored as a **content-addressed asset** (like GLBs at
  `/assets/<hash>`) and referenced from a `room` block: `environment.room = { meshId, revision, … }`.

## 5. Seeing the room (passthrough) 🟡

- The client can start (or switch to) an **`immersive-ar`** session; the Quest composites passthrough
  behind the scene. When `environment.passthrough` is on, the client **suppresses the void + grid
  walls** (the synthetic holodeck) so the **real walls show through** — directly the "replace the
  holodeck walls with the actual walls" idea.
- A **toggle** (voice / director tool / gesture): `enter_ar` / `enter_vr`. VR = full synthetic world;
  AR = real room + virtual objects. The same world doc renders in both; only `passthrough` + whether
  real-surface entities are drawn differ.
- **Neutral fallback** (decision #11): on a device without passthrough/room data (phone, desktop), the
  world renders as the synthetic holodeck — no room entities, no AR. Nothing *gates* on Quest features.

## 6. Semantic info — display & interaction 🟢 intent / 🟡 surface

**Display (text).** Each real surface can render its label as a floating text overlay (a debug/inform
mode, toggleable): "wall", "table", "window". Drawn by the client from `meta.semantic` on `real`
entities; also available to the director to *speak* ("you've got a table to your left and a window
behind you").

**Interaction (mounting).** The director resolves a **semantic target** to a real surface and places
virtual content **on/anchored to it**:
- "hang this on the **wall**" → pick the wall surface the user faces (or nearest) → orient the plane to
  the wall normal, offset slightly → anchor.
- "put a vase on the **table**" → the table surface's top → rest the model on it (reuse Phase-3 ground
  placement, but onto the real plane height).
- "place it **where I'm pointing**" → `hit-test` raycast onto the real mesh/plane → pose.
Mounted content is pinned with an **anchor** so it stays put as tracking refines.

## 7. Progressive mesh refinement (background, on request) 🟡

The headline differentiator. Start coarse, refine on demand without blocking:

1. **Instant (coarse):** on entering AR, capture **planes** + labels → push up → usable immediately for
   layout, labels, and mounting.
2. **On request** ("scan my room in more detail" / a `refine_room_scan` tool): the client enables
   `mesh-detection` and begins a **background refinement loop** — as the user naturally looks around,
   the system mesh updates (`XRMesh.lastChangedTime`); the client streams **changed mesh chunks up**
   incrementally (throttled, simplified), and the server updates `environment.room.mesh` + bumps a
   `revision`. Interaction continues throughout; fidelity climbs in the background.
3. **Completion / progress:** the client reports coverage/progress; the director can say "I've got a
   rough scan — keep looking around and it'll sharpen," and signal when it's settled. Refinement is
   **cancellable** and **opt-in** (it costs battery + bandwidth).

Higher-fidelity mesh then feeds **occlusion** (§9) and physics colliders, and lets the director reason
about finer geometry ("the shelf", "the doorway"). This is a **background task** pattern — fits the
existing async server + the planned behavior/event model.

## 8. MCP / director surface 🟡

New tools (mirroring the existing coarse/intent-level tool style, architecture §8):

- `enter_ar()` / `enter_vr()` — toggle passthrough vs synthetic holodeck.
- `query_room()` — summarize real surfaces + labels + rough dimensions (so the director can reason /
  describe). (Real surfaces also appear in `query_world` as `real`-tagged entities.)
- `mount_on_surface(content_ref, semantic | surface_id, where?)` — place a procured image / asset /
  primitive onto a real surface by label or id, anchored. (Builds on `place_image`/`place_asset`.)
- `refine_room_scan(enable=true)` — start/stop background mesh refinement (§7).
- `show_room_labels(enable)` — toggle the semantic text overlays.

**Persistence (designed-for, later):** anchors + an anchor↔world mapping let a world reload **fixed to
the same physical room** next session (vision's "persistent rooms"; spec §3). The schema's `anchorId`
on real/mounted entities reserves the hook; building cross-session anchor storage is a follow-on
(ties to Phase 6 memory).

## 9. Occlusion & physics (mostly client-side) 🟡

- **Occlusion:** virtual objects hidden behind real ones. Driven by `depth-sensing` (per-frame depth)
  and/or the room mesh as a depth-write-only occluder. This is a **pure rendering concern** — the depth
  map never goes to the server; the client handles it.
- **Physics colliders:** the simplified room mesh (§7) can act as static colliders so virtual objects
  rest on the real floor / don't pass through real furniture. Lands with the behavior/physics work.

## 10. Privacy & permissions 🟢

Room geometry is **sensitive**. Constraints:
- WebXR **permission-gated**: the user must grant the AR features and have **Space Setup** done; the
  app must degrade gracefully if denied (→ synthetic holodeck).
- **Be explicit about what leaves the headset.** Semantic layout + (on request) mesh go to the world
  server; the **depth map does not**. Document it; keep room data in the (local, user-run) server only.
- Capability-tier fallback (#11) means room features **never gate** the baseline experience.

## 11. Build order (slices within the phase)

Each slice is independently demoable in-headset:

- **A — See it + layout.** `immersive-ar` toggle + suppress synthetic walls; capture **planes +
  labels**; reverse channel (WS upstream); store as `real` entities; render passthrough. *Demo: enter
  AR, see your real room; `query_world` lists "wall/floor/table".*
- **B — Display + mount.** Semantic text overlays; `mount_on_surface` (hang an image on the real wall,
  rest an asset on the real table), anchored. *Demo: "hang that dragon on my wall."*
- **C — Progressive refinement.** `refine_room_scan` background loop streaming mesh deltas up;
  `environment.room.mesh` upgrades live. *Demo: "scan my room in detail" → fidelity climbs as you look.*
- **D — Occlusion.** Depth-sensing occluder (client-side). *Demo: walk a cube behind your real couch.*
- **E — Persistence (stretch).** Anchor storage so a world reloads fixed to the room.

## 12. Open questions / risks 🔴

- **A-Frame ↔ raw WebXR**: confirm plane/mesh/depth feature access + the session lifecycle inside the
  current A-Frame setup (custom component reading `frame.detected*`). Verify against the installed
  Quest Browser before committing the client design.
- **Mesh volume / throttling**: simplification + delta strategy so refinement doesn't swamp the WS or
  the device.
- **Coordinate stability**: reference-space drift within a session; anchor-relative storage for
  cross-session (§8). Decide when to anchor (capture time vs mount time).
- **Schema fit**: `real` entities vs a dedicated `environment.room` block for surfaces — lean entities
  for reuse, but validate it doesn't bloat `query_world` for the director.
- **Director UX**: how much room detail to feed the LLM (summary vs full geometry) to keep turns cheap.

## 13. Test plan (per [testing.md](./testing.md))

- **Tier 1 (unit, free):** reverse-channel message → world-doc patch (real-entity shape, semantic
  labels, dedupe on re-capture); `mount_on_surface` math (orient to wall normal / rest on table
  height); room-mesh revisioning on incremental updates.
- **Tier 2 (contract):** the WebXR feature surface we depend on (plane/mesh/depth shapes) — guarded so
  Quest-Browser API drift is caught.
- **Tier 4 (manual, in-headset):** the slice demos in §11 — the parts WebGL/passthrough can't be
  automated (enter AR, mount on a real wall, watch refinement climb, occlusion behind real furniture).
