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

There is no separate space-rendering path. Each captured surface is a normal, patchable, stylable entity
tagged `meta.real`, so it flows through the entity machinery, the patch protocol, broadcast, and the
director's material edits unchanged:

```jsonc
{
  "id": "real_wall_3",
  "transform": { "position": [x,y,z], "rotation": [ … ] },
  "components": {
    "surface":  { "polygon": [[x,z], …], "extent": [w,h], "holes": [ … ] },
    "material": { "shader": "flat", "color": "#888", "side": "double", "opacity": 1.0 }
  },
  "meta": { "real": true, "semantic": "wall", "friendly_id": 3,
            "source": "headset", "room": "…", "host_wall": "…" }
}
```

`meta.real` is the contract: the director may **restyle, hide, texture and mount onto** it, but must not
move or remove it. The renderer draws it only when its material says visible; otherwise it is an
invisible reference used for mounting and bounds.

**Per-semantic base material** (`_default_surface_material`, `server.py:3292`) — flat `#888`,
double-sided, opaque, except that a **door** seeds translucent (`opacity 0.25`, `transparent`) and a
**window** seeds faint glass (`#cfe6ff`, `opacity 0.18`, `transparent`). See-through is a material
property, not an open/closed state, so the director adjusts it like any other.

### 1.1 Friendly ids

Each surface carries a small `meta.friendly_id`, taken at ingest from the number already in its id
(`real_wall_3` → 3). It is what the user reads off a label and speaks back — "make 12 blue". One
numbering system, so the label, `query_space`, the entity id and the user's reference agree by
construction (`server.py:661`).

---

## 2. Where presentation lives

`environment` in a real world document:

```jsonc
"environment": {
  "public": true,
  "space":  "daniel/space-1",     // WHICH space (a ref) — specs/spaces.md §4
  "passthrough": false,           // AR camera on/off — immersion axis 1
  "boundary": { … },              // the active space's floor polygon — LIVE ONLY, on loan
  "rooms":  { … },                // the space's room records — LIVE ONLY, on loan
  "captureAuthority": "hs_a1b2",  // which headset may report geometry — LIVE ONLY
  "sky":    { … },                // how this world presents the sky
  "frame":  { … },                // this world's yaw/offset, and the sky's delta
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
  Written bare by `_IMMERSION`, read at `conjure-client.js:775`.
- **`boundary`** — geometry, copied in from the space by `_compose` and stripped by `_decompose`. The
  space owns it; a world never persists its own. `rooms` rides in and out the same way, because a room's
  name is a fact about the place rather than a presentation choice.
- **`captureAuthority`** — which headset may report geometry. Per-session coordination, nulled whenever
  a world becomes active (`_reset_capture_authority`, `server.py:301`), because a persisted authority
  names a dead headset and would lock the live one out forever.

### 2.1 Base plus override

The space stores each surface at a **default** material; the world stores only the **delta**.

- `_compose` overlays `surfaceStyles[<id>]` onto the space's base material on load.
- `_decompose` writes back only materials that differ from the base.

So the same physical wall is green in one world and marble in another with one geometry record and two
small override maps, and switching worlds restyles the space without recapturing it.

An override lives exactly as long as its surface: `_decompose` rebuilds the map from the live real
entities, so a surface pruned from the space takes its entry with it and the colour is unrecoverable
(§6).

### 2.2 The client mirror

The client keeps `presentation` (`conjure-client.js:319`) as its local mirror of
`environment.spacePresentation` — every member but `surfaceStyles`, with `defaultSurfaceVisible`
shortened to `defaultVisible` — plus the sibling `passthrough` and two members the server does not
store, `skybox` and `grounded`, which say whether a sky is standing in as the environment. It gates the
holodeck scaffold on those two: the grid shows only in a bare void world, never over a captured space or
under a skybox.

`surfaceStyles` is kept apart, as its own id → material map rebuilt from every snapshot
(`conjure-client.js:965`). Each client renders the real surfaces from its **own** live capture rather than
from the broadcast geometry (`specs/spaces-geometry.md`), so a world's styling reaches the screen by id:
the locally-drawn surface is given `surfaceStyles[id]` as its material.

---

## 3. Immersion — two axes, not five modes

The whole real/virtual spectrum is two independent axes:

- **Axis 1 — passthrough** (`environment.passthrough`, top-level): is the real space visible through the
  camera?
- **Axis 2 — surface visibility** (per-surface material, defaulted by
  `spacePresentation.defaultSurfaceVisible`): are the virtual surfaces drawn, and how are they styled?

`set_immersion(mode)` sets both at once (`_IMMERSION`, `mcp_server.py:248`):

| Mode | passthrough | `spacePresentation.active` | `defaultSurfaceVisible` | What you get |
|---|---|---|---|---|
| `virtual_space` | off | true | **true** | a virtual reconstruction of the space, recolourable |
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
id, the two combined (`wall 4`, `wall #4`), a **room** (its name, its id, or `room 2`), or `all`.

`show_surface`, `style_surface` and `texture_surface` also take **`room=`**, which narrows `target` to one
room. The two compose because neither says the other's half: a room used as a bare target takes the whole
room, a semantic takes it across the whole space, and `style_surface("wall", room="kitchen")` is the
kitchen's walls. An unresolvable `room` matches **nothing** rather than everything — silently widening a
scoped edit to the whole space is the one failure here that repaints surfaces nobody mentioned — and the
tool says the *room* was the half that failed (`UnknownRoom`), not the target.

A room is tried **last**, so naming a room "wall" cannot stop `wall` meaning every wall; `name_room`
refuses the reserved words as well. Rooms themselves are [`specs/spaces.md` §1.1](./spaces.md).

| Tool | Effect |
|---|---|
| `set_immersion(mode)` | both axes at once (§3) |
| `show_surface(target, visible)` | per-surface visibility — builds the mixtures |
| `style_surface(target, color, opacity)` | colour and transparency — "glass walls" is low opacity |
| `texture_surface(target, image_id, repeat)` | map a procured image onto surfaces; `repeat=N` tiles a seamless image (grass, brick) |
| `show_annotations(on, dimensions)` | float `"[<semantic> <friendly id>]"` on each surface; sizes only on request |
| `style_annotations(color, opacity)` | restyle those labels |
| `show_edges(on)` | the rectangular outline around every surface — **on by default** |
| `style_edges(color, opacity)` | restyle the outline |
| `query_space()` | surfaces by semantic + friendly id, grouped by room, with colour and visibility, plus the boundary |
| `name_room(room, name)` | name a room — `room #2` becomes `kitchen`; space-owner-gated |
| `realign_space()` | re-capture at the current tracking origin when the space looks drifted |

**Styling is the ordinary edit vocabulary.** Because surfaces are entities with a `material`, "make the
ceiling a galaxy" is `material.src` pointing at a generated image — the same plane-material path
`place_image` uses. There is no separate space-rendering code to maintain.

**But colour alone is not enough on a captured surface.** Its fill draws only when
`material.visible` is *explicitly* true; absent that it falls back to `defaultSurfaceVisible`, which is
**false in AR** (§3) — the real table is already there to look at, so the virtual panel starts hidden.
So `style_surface` always sets `material.visible = True` and clears `material.src` alongside the colour;
without that the patch applies, the tool reports success, and nothing changes on screen. An opacity below
1 also sets `material.transparent`.

The generic `update_entity` is the trap: it sets only the fields it is given. The same three-field
treatment is therefore applied to any colour change landing on an entity that carries `meta.real`, and
the result says so — because a tool that reports success for an invisible change is worse than one that
refuses. Recolouring a captured surface through either tool is safe; `style_surface` remains the direct
route and the only one that takes an opacity.

**The treatment is applied server-side in `/patch`, after `_resolve_op_ids`** (`_fill_real_surface_ops`),
which is what makes it hold for *every* way of naming a target. Deciding it in the caller meant deciding
it from the string the user typed: an exact id was recognised, and `wall 4` — which fans out to one op
per matching surface — was not, so the same instruction worked or silently did nothing depending on how
it was phrased. An explicit `visible` or `src` in the same op is left alone, since hiding a surface while
recolouring it is a coherent request; the rule completes an instruction rather than overruling one.

### 4.1 Mounting onto a surface

Hanging content on a real surface is `place_image(on_surface=<id | friendly id>)`: the image is aligned
to the surface and fitted to its frame automatically, so no caller computes a position or rotation.
`stretch=true` fills the entire surface. Use `texture_surface` instead when the image should *cover* a
surface as a mural rather than hang as a framed picture.

Orientation is handled by `_face_interior(srot, up_local)` (`server.py:4635`), which turns content to face
the space's interior (along `−normal`) and keeps it upright against gravity rather than trusting the
surface's own roll. Captured normals point **outward** from the space, so interior-facing is `−normal` —
except wall-art, whose normal may arrive inward. That asymmetry is a live source of bugs; see
[`investigations/`](../investigations/) for the wall-art case study.

### 4.2 Where the description lives

`space://current` — injected into an agent's prompt each turn — is the **only** place surfaces are
described. It carries every surface's semantic, friendly id, position, **colour** and visibility.

`query_world` deliberately does **not** list them. It collapses every real surface to one counted line
with a per-kind tally, because listing 59 label-and-position rows was 87% of that dump's characters and
showed less than the summary already had. So `query_world`'s silence about a colour means nothing, and
no agent should conclude from it that colours are not stored.

---

## 5. Insets, openings, and edges

The Quest reports a **door, window or wall-art** as its own plane, attached to a parent wall and
near-coplanar with it. Drawn naïvely they z-fight the wall, and the larger wall quad occludes them — you
cannot see a door as an opening.

**Openings are cut.** At capture the client snaps each inset onto its host wall — pin the perpendicular
clearance to a fixed 2 cm (`--inset-standoff`), adopt the wall's orientation — and, for doors and
windows, records the opening on the wall as `surface.holes`: the inset's rectangle projected into the
wall's local 2-D frame (`snapInsets`, `client/space-snap.js`). The wall then renders through the
**`holed-wall`** geometry — the rectangle minus the hole rects, triangulated with `THREE.ShapeGeometry`
— so you see through into the next room or outside.

- **Wall-art does not cut.** It is a decal offset off the surface: a picture, not an opening.
- A door reaching the floor would sit flush with the wall's bottom edge and break triangulation, so each
  opening is clamped 2 cm inside the outline.
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
(`anchored`) from being removed underneath that content. Two further guards refuse a whole destructive
capture rather than one surface (`specs/spaces-geometry.md`), and a prune that destroys a director's
styling — a material differing from the per-semantic default — is called out in the server log as
`*** LOST STYLING`, because every other prune is routine and would bury it.

---

## 7. Surface reference

**MCP tools:** `set_immersion`, `show_surface`, `style_surface`, `texture_surface`, `show_annotations`,
`style_annotations`, `show_edges`, `style_edges`, `query_space`, `realign_space`,
`place_image(on_surface=…)`.

**Resource:** `space://current` — the per-surface summary injected each turn.

| Concern | Where |
|---|---|
| immersion mode table | `conjure/mcp_server.py:248` `_IMMERSION` |
| the space summary | `conjure/mcp_server.py:257` `_space_summary` |
| `query_world` collapse | `conjure/mcp_server.py` `_real_surfaces_line` |
| per-semantic base material | `conjure/server.py:3292` `_default_surface_material` |
| compose / decompose | `conjure/server.py:3309` / `:3343` |
| interior-facing orientation | `conjure/server.py:4635` `_face_interior` |
| client presentation mirror | `client/conjure-client.js:319` `presentation` |
| inset snapping + hole cutting | `client/space-snap.js` `snapInsets` |
| holed-wall geometry | `client/conjure-client.js` `holed-wall` / `applySurfaceGeometry` |
| surface target matching | `conjure/server.py:3903` `_surface_targets` / `_real_surface_match` |

---

## 8. Not built — a room model as the environment

`presentation.skybox` and `.grounded` already suppress the scaffold, and `authored` immersion was
written for content that does not exist yet: a **room model** set as the environment the way a sky is,
singleton and replacing rather than placed. Four captures ship one, each paired with a cube-mapped sky —
a projection the library has never held.

Designed in [`backlogs/library.md`](../backlogs/library.md) § *A room model as the ENVIRONMENT*. The
part that belongs here: the room IS the walls, so it takes the same scaffold switch, and aligning it
with the real room needs offset and scale from inside the headset — which `grab` currently allows only
in a void world, and a room inside a real room is exactly the case that needs them.

## 9. Related specs

- [`specs/spaces.md`](./spaces.md) — the space record, ownership, selection, admission.
- [`specs/spaces-geometry.md`](./spaces-geometry.md) — capture, registration, identity, stability.
- [`specs/occlusion.md`](./occlusion.md) — real-world depth occlusion.
- [`specs/agents.md`](./agents.md) — how an agent's tool set and context resources are declared.
