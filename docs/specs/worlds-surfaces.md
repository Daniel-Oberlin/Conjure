# Worlds ↔ surfaces — presenting a space inside a world

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/worlds-surfaces.md`](../backlogs/worlds-surfaces.md);
rejected alternatives and the reasoning behind consequential forks live in
[`docs/decisions.md`](../decisions.md).

A **space** holds one copy of the real geometry (see [`specs/spaces.md`](./spaces.md)). A **world**
decides how that geometry *looks*: which surfaces are drawn, in what colour, with what image mapped
onto them, whether labels and outlines show, and how much passthrough is mixed in. This spec is that
layer — the schema it lives in, the tools that drive it, and the rules that keep a shared space
consistent while two worlds style it differently.

Where a surface *is* — capture, registration, identity — is
[`specs/spaces-geometry.md`](./spaces-geometry.md). Real-world depth occlusion is
[`specs/occlusion.md`](./occlusion.md).

---

## 1. A real surface is an ordinary entity

There is no separate room-rendering path. Each captured surface is a normal, patchable, stylable entity
tagged `meta.real`, so it flows through the entity machinery, the patch protocol, broadcast, and the
director's material edits unchanged:

```jsonc
{
  "id": "real_wall_3",
  "transform": { "position": [x,y,z], "rotation": [ … ] },
  "components": {
    "surface":  { "polygon": [[x,z], …], "extent": [w,h], "holes": [ … ] },
    "material": { "shader": "flat", "color": "#888", "side": "double",
                  "opacity": 1.0, "src": null }
  },
  "meta": { "real": true, "semantic": "wall", "friendly_id": 3,
            "host_wall": "…", "on_surface": "…" }
}
```

`meta.real` is the contract: the director may **restyle, hide, texture and mount onto** it, but must not
move or remove it. The renderer draws it only when its material says visible; otherwise it is an
invisible reference used for mounting and bounds.

**Per-semantic base material** (`_default_surface_material`, `server.py:2619`) — flat `#888`,
double-sided, opaque, except that a **door** seeds translucent (`opacity 0.25`) and a **window** seeds
faint glass (`#cfe6ff`, `opacity 0.18`). See-through is a material property, not an open/closed state,
so the director adjusts it like any other.

### 1.1 Friendly ids

Each surface carries a small sequential `meta.friendly_id` assigned at ingest. It is what the user reads
off a label and speaks back — "make 12 blue". The label, `query_room`, the entity id and the user's
reference all agree on it deliberately (`server.py:547`).

---

## 2. Where presentation lives

`environment` has exactly four members in a real world document:

```jsonc
"environment": {
  "public": true,
  "space":  "daniel/space-1",     // WHICH space (a ref) — specs/spaces.md §4
  "passthrough": false,           // AR camera on/off — immersion axis 1
  "boundary": { … },              // the active space's floor polygon — LIVE ONLY, on loan
  "captureAuthority": "hs_a1b2",  // which headset may report geometry — LIVE ONLY
  "sky":    { … },                // how this world presents the sky
  "spacePresentation": { … }      // how this world presents the space  ← this spec
}
```

`environment.spacePresentation` holds **only** presentation, and every member of it is persisted:

| Member | Meaning |
|---|---|
| `active` | is there a space in effect at all |
| `defaultSurfaceVisible` | global default for captured surfaces — immersion axis 2 |
| `surfaceStyles` | **per-surface material overrides, keyed by id** — the real user data here |
| `annotations`, `annotationDims`, `annotationColor`, `annotationOpacity` | the label overlay |
| `edgesVisible`, `edgeColor`, `edgeOpacity` | the wireframe outline |

That "only, and every" is the point of the name: the key means one thing, so persistence and meaning
line up instead of needing a per-member caveat. Three things that look like they belong are siblings
instead, because none is a presentation choice:

- **`passthrough`** — an immersion axis, but equally meaningful in a void world with no space at all.
  Written bare by `_IMMERSION`, read at `conjure-client.js:653`.
- **`boundary`** — geometry, copied in from the space by `_compose` and stripped by `_decompose`. The
  space owns it; a world never persists its own.
- **`captureAuthority`** — which headset may report geometry. Per-session coordination, nulled whenever
  a world becomes active (`server.py:240`), because a persisted authority names a dead headset and would
  lock the live one out forever.

### 2.1 Base plus override

The space stores each surface at a **default** material; the world stores only the **delta**.

- `_compose` overlays `surfaceStyles[<id>]` onto the space's base material on load.
- `_decompose` writes back only materials that differ from the base.

So the same physical wall is green in one world and marble in another with one geometry record and two
small override maps, and switching worlds restyles the room without recapturing it.

### 2.2 The client mirror

The client keeps `presentation` (`conjure-client.js:255`) as its local mirror of `environment.spacePresentation`, plus
two members the server does not store — `skybox` and `grounded`, which say whether a sky is standing in
as the environment. It gates the holodeck scaffold on them: the grid shows only in a bare void world,
never over a captured space or under a skybox.

---

## 3. Immersion — two axes, not five modes

The whole real/virtual spectrum is two independent axes:

- **Axis 1 — passthrough** (`environment.passthrough`, top-level): is the real room visible through the
  camera?
- **Axis 2 — surface visibility** (per-surface material, defaulted by
  `spacePresentation.defaultSurfaceVisible`): are the virtual surfaces drawn, and how are they styled?

`set_immersion(mode)` sets both at once (`_IMMERSION`, `mcp_server.py:246`):

| Mode | passthrough | `spacePresentation.active` | `defaultSurfaceVisible` | What you get |
|---|---|---|---|---|
| `virtual_room` | off | true | **true** | a virtual reconstruction of the space, recolourable |
| `ar` | on | true | false | the real space; surfaces invisible but used for mounting and bounds |
| `mixed` | on | true | *(untouched)* | passthrough plus whatever `show_surface` reveals |
| `authored` | off | true | false | captured surfaces hidden, ready for replacement geometry |
| `vr_unbounded` | off | **false** | false | the space ignored entirely — the original synthetic holodeck |

`mixed` deliberately leaves axis 2 alone: it turns passthrough on and lets per-surface calls compose the
mixture (real walls, virtual starry ceiling). `authored` sets the axes for director-built replacement
geometry; the tool that would *generate* that geometry does not exist (see the backlog).

Nothing gates on Quest features. A device without passthrough or space data renders the synthetic
holodeck.

---

## 4. The presentation tools

All of these write `environment.spacePresentation` or a surface's material, and all accept the same **target**
vocabulary: a surface id (`real_wall_3`), a semantic label (`wall`, `floor`, `ceiling`, …), a friendly
id, or `all`.

| Tool | Effect |
|---|---|
| `set_immersion(mode)` | both axes at once (§3) |
| `show_surface(target, visible)` | per-surface visibility — builds the mixtures |
| `style_surface(target, color, opacity)` | colour and transparency — "glass walls" is low opacity |
| `texture_surface(target, image_id, repeat)` | map a procured image onto surfaces; `repeat=N` tiles a seamless image (grass, brick) |
| `show_annotations(on, dimensions)` | float `"<semantic> (<friendly id>)"` on each surface; sizes only on request |
| `style_annotations(color, opacity)` | restyle those labels |
| `show_edges(on)` | the polygon outline around every surface — **on by default** |
| `style_edges(color, opacity)` | restyle the outline |
| `query_room()` | surfaces by semantic + friendly id, with colour and visibility, plus the boundary |
| `realign_room()` | re-capture at the current tracking origin when the space looks drifted |

**Styling is the ordinary edit vocabulary.** Because surfaces are entities with a `material`, "make the
ceiling a galaxy" is `material.src` pointing at a generated image — the same plane-material path
`place_image` uses. There is no separate room-rendering code to maintain.

### 4.1 Mounting onto a surface

Hanging content on a real surface is `place_image(on_surface=<id | friendly id>)`: the image is aligned
to the surface and fitted to its frame automatically, so no caller computes a position or rotation.
`stretch=true` fills the entire surface. Use `texture_surface` instead when the image should *cover* a
surface as a mural rather than hang as a framed picture.

Orientation is handled by `_face_room(srot, up_local)` (`server.py:3682`), which turns content to face
the space's interior (along `−normal`) and keeps it upright against gravity rather than trusting the
surface's own roll. Captured normals point **outward** from the space, so interior-facing is `−normal` —
except wall-art, whose normal may arrive inward. That asymmetry is a live source of bugs; see
[`investigations/`](../investigations/) for the wall-art case study.

### 4.2 Where the description lives

`room://current` — injected into an agent's prompt each turn — is the **only** place surfaces are
described. It carries every surface's semantic, friendly id, position, **colour** and visibility.

`query_world` deliberately does **not** list them. It collapses every real surface to one counted line
with a per-kind tally, because listing 59 label-and-position rows was 79% of that dump and showed less
than the summary already had. So `query_world`'s silence about a colour means nothing, and no agent
should conclude from it that colours are not stored.

---

## 5. Insets, openings, and edges

The Quest reports a **door, window or wall-art** as its own plane, attached to a parent wall and
near-coplanar with it. Drawn naïvely they z-fight the wall, and the larger wall quad occludes them — you
cannot see a door as an opening.

**Openings are cut.** At capture the client snaps each inset onto its host wall — project the centre
onto the wall plane, adopt its orientation, nudge a fixed ~1 cm into the room — and, for doors and
windows, records the opening on the wall as `surface.holes`: the inset's rectangle projected into the
wall's local 2-D frame (`snapInsets`, `client/room-snap.js`). The wall then renders through the
**`holed-wall`** geometry — the rectangle minus the hole rects, triangulated with `THREE.ShapeGeometry`
— so you see through into the next room or outside.

- **Wall-art does not cut.** It is a decal offset off the surface: a picture, not an opening.
- A door reaching the floor would sit flush with the wall's bottom edge and break triangulation, so each
  opening is clamped a hair inside the outline.
- The leaf sits *in* the opening as a material-driven pane; see-through is opacity (§1).
- Openings are cut against the **sealed** wall, because sealing runs before snapping
  ([`spaces-geometry.md`](./spaces-geometry.md)).

**Edges.** A bright wireframe outlines every real surface, drawn always-on-top so the whole space reads
as a wireframe even in AR. It is on by default, independent of the fill, and globally toggleable and
restyleable. The fill is inflated by `--surface-weld` (2 mm, split per side) so abutting fills overlap
and passthrough cannot flicker through float-rounding cracks — **the outline stays true size**, so the
wireframe is not thickened by the weld.

---

## 6. What the director may and may not do

| | Allowed | Not allowed |
|---|---|---|
| Real surfaces | restyle, texture, hide/show, mount onto, annotate, outline | move, remove, re-pose |
| Placed entities | everything | — |
| Geometry | request a realign | author surface poses |

Enforcement is layered: `meta.real` is the prompt-level contract; scene mutation is owner-gated
server-side (`specs/spaces.md §7`); and pruning protection keeps a surface with content pinned to it
(`anchored`) from being removed underneath that content.

---

## 7. Surface reference

**MCP tools:** `set_immersion`, `show_surface`, `style_surface`, `texture_surface`, `show_annotations`,
`style_annotations`, `show_edges`, `style_edges`, `query_room`, `realign_room`,
`place_image(on_surface=…)`.

**Resource:** `room://current` — the per-surface summary injected each turn.

| Concern | Where |
|---|---|
| immersion mode table | `conjure/mcp_server.py:246` `_IMMERSION` |
| the room summary | `conjure/mcp_server.py:255` `_room_summary` |
| `query_world` collapse | `conjure/mcp_server.py` `_real_surfaces_line` |
| per-semantic base material | `conjure/server.py:2619` `_default_surface_material` |
| compose / decompose | `conjure/server.py:2636` / `:2659` |
| interior-facing orientation | `conjure/server.py:3682` `_face_room` |
| client presentation mirror | `client/conjure-client.js:255` `presentation` |
| inset snapping + hole cutting | `client/room-snap.js` `snapInsets` |
| holed-wall geometry | `client/conjure-client.js` `applySurfaceGeometry` |

---

## 8. Related specs

- [`specs/spaces.md`](./spaces.md) — the space record, ownership, selection, admission.
- [`specs/spaces-geometry.md`](./spaces-geometry.md) — capture, registration, identity, stability.
- [`specs/occlusion.md`](./occlusion.md) — real-world depth occlusion.
- [`specs/agents.md`](./agents.md) — how an agent's tool set and context resources are declared.
