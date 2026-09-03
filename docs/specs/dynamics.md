# Dynamics — the spec for conjurable, live, shared modules

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/dynamics.md`](../backlogs/dynamics.md); rejected alternatives
and the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

A **dynamic module** is a live, animated, interactive effect the director can conjure into a world —
fireflies, a rippling Water Picture, object manipulation. This spec is the contract for implementing
one, and the reference for the runtime surface a module is handed. It is the counterpart to
[`docs/specs/agents.md`](./agents.md) for agents.

---

## 1. What a module is

**A module is just an A-Frame component, delivered as config-in-snapshot.** The world server adds an
entity carrying the component (`entity.components.<component> = <config>`); the client applies it with
`setAttribute`. Because it is an ordinary entity, it is shared across clients, persisted, and replayed
for free on the existing entity/patch/snapshot path. There is no bespoke per-module loader.

What makes a module *first-class* is only the folder + manifest + search-path resolution in §3–4.

### The invariant

> A module's **presence** (loaded/unloaded) and its **simulation state** are shared by everyone.
> Its **presentation** need not be pixel-identical.

The second half is not a loophole — it is how absoluteness is honoured cheaply. State keyed off a shared
clock + seed + config (all in the world snapshot) means everyone sees the same thing without per-frame
sync, while per-client cosmetics (foveation, billboard yaw, interpolation) stay legal.

Consequences that hold throughout:

- **Load/unload is world state.** It flows through the existing snapshot/patch path and is owner-gated
  (`/module`, `/module/dismiss`, `/manipulate` ∈ `_OWNER_ONLY_PATHS`, `server.py:604`). No new authority
  model — conjuring is another kind of scene mutation.
- **Modules are entities.** A procedural module persists by storing `(seed, config)`; reload restores it
  exactly. No new storage model.
- **Environment is world-level, not a module.** Worlds set the backdrop; modules are the live performers
  on top of it, additive in passthrough and in VR alike.
- **Occlusion is global.** Real-world depth occlusion is one pre-pass for the whole scene
  ([`docs/specs/occlusion.md`](./occlusion.md)); modules never sample depth or opt in.

### Sync causes, never effects

Separate simulation (shared, must be consistent) from presentation (local, per-client). Never broadcast
the water's per-frame distortion field — broadcast the **touch** and let every client run the same ripple
sim from it. Never sync a procedural swarm frame-by-frame — it is `f(sharedClock, seed, config)`, so every
headset computes the same state with zero runtime sync.

---

## 2. Sync tiers

Every module declares a tier. Determinism is a **per-module capability**, not a mandate: A and B opt in
and get near-free sync; C opts out and pays for authoritative commits.

| Tier | What syncs | Cost | Shipped example |
|---|---|---|---|
| **A — autonomous-procedural** | `(clock, seed, config)` in the snapshot; **nothing at runtime** | ~free | `fireflies` |
| **B — input-reactive** | the input **event**; each client sims locally from it | cheap | `water` |
| **C — shared-authoritative-state** | the resting state, committed on gesture end | expensive, naturally rate-limited | `grab` |

**Tier B is deliberately not synchronized.** Only touch events are broadcast; each headset evolves its own
sim, and the fields diverge. That is accepted: the effects are short-lived and cosmetic, and convergence
would cost far more than it is worth.

**Tier C commits resting state, not motion.** Nothing is broadcast mid-gesture; peers see the object
arrive at its resting pose on release.

Determinism in JS is imperfect (variable framerate, cross-device float divergence). Mitigate with the
shared clock and explicitly seeded PRNGs; never `Math.random()` or `Date.now()` at runtime in a tier-A
module. Anything genuinely authoritative belongs in tier C.

> `module.json`'s `tier` field is **informational**. The loader accepts any string and neither validates
> nor acts on it (`dynamics.py:148`); it documents intent and appears in no runtime decision.

---

## 3. Folder layout and resolution

Each module is a self-contained directory. **The directory name is the module's identity** — the manifest
need not repeat it (a `name` field, if present, is validated to match).

```
dynamics/                     # bundled, sibling to agents/  (config.BUNDLED_DYNAMICS_DIR)
  fireflies/
    module.json               # the manifest (§4)
    fireflies.js              # registers the A-Frame component
  water/
    module.json
    water.js
  grab/
    module.json
    grab.js
```

User modules live in `~/.config/conjure/dynamics/<name>/` and **shadow** a bundled module of the same
name. Resolution (`dynamics.resolve_module_dir`, mirroring `resolve_agent_dir`):

```
env CONJURE_DYNAMICS_PATH  →  settings["dynamics_path"]  →  [<config_dir>/dynamics, BUNDLED_DYNAMICS_DIR]
```

user-first, first match wins. Add or override a module by dropping a folder in your config dir — no code
change, no restart (`_dynamics_registry()` reloads per request).

---

## 4. `module.json` — the manifest

```jsonc
{
  "component": "water",            // REQUIRED: the A-Frame component the entry registers
  "entry": "water.js",             // REQUIRED: client script(s) to load — string or list, in order
  "tier": "B",                     // A|B|C — informational (§2)
  "anchor": "free",                // free | surface | volume | ambient (§7)
  "singleton": false,              // true = one live instance, reused/reconfigured across conjures
  "face_user": true,               // free-standing flat content faces the viewer AT CREATION (fixed)
  "default_pos": [0.0, 1.4, -1.2], // where it centres when no position is given (metres)
  "actions": ["select"],           // XR actions it consumes (§6) — informational today
  "description": "One line — feeds the director catalog (dynamics://available).",
  "config_schema": {               // the LLM-facing params: {type, default, desc, enum?}
    "damping": { "type": "number", "default": 0.996, "desc": "→1 = long-lived ripples" }
  }
}
```

| Field | Required | Validated at load | Notes |
|---|---|---|---|
| `component` | yes | non-empty | must equal the name the entry registers with `AFRAME.registerComponent` |
| `entry` | yes | each file **exists** on disk | string or list; normalized to a list; loaded in order |
| `anchor` | no (`free`) | ∈ `free\|surface\|volume\|ambient` | drives placement (§7) |
| `tier` | no (`A`) | **no** | informational |
| `singleton` | no (`false`) | — | `/module` reuses the existing instance instead of adding one |
| `face_user` | no (`false`) | — | server orients the entity toward the caller at creation |
| `default_pos` | no (`[0, 1.3, -1.5]`) | — | used when the caller passes no `position` |
| `actions` | no (`[]`) | **no** | see the note below |
| `description` | no (`""`) | — | one line, rendered into the director catalog |
| `config_schema` | no (`{}`) | `enum` only | `{param: {type, default, desc, enum?}}` |

Notes:

- **`config_schema`** is the parameter surface the director may set. It is rendered into the catalog as
  `name — description; params: k(default), …` (`DynamicModuleDef.catalog_line`). The **authoritative**
  defaults and types still live in the component's own A-Frame `schema` client-side; keep the two
  consistent. A param with no `default` (e.g. `image`) is shown bare.

  A param may declare **`enum`**, and then the choices are rendered in its place
  (`mode(object|skybox|void)`) and **`/module` refuses any value outside them**, naming the valid ones in
  the error so a caller can correct itself on the next call. Use it for any param with a fixed vocabulary:
  prose in `desc` is not sufficient on its own, because an unvalidated enum makes a caller's wrong guess
  indistinguishable from success. `type` and numeric range are **not** yet enforced (see the backlog).
- **`actions`** declares which semantic XR actions the module consumes, in the same declarative spirit as
  `config_schema`. It is parsed into `DynamicModuleDef.actions` (`dynamics.py:152`) and **nothing reads it
  yet** — it is not validated against known action names, not in the catalog, and never reaches the
  client. Runtime action resolution comes entirely from `window.CONJURE_BINDINGS` (§6). Declare it
  accurately anyway; making it load-bearing is a backlog item.
- A malformed manifest is **skipped and logged** by the world server, never fatal to the world
  (`_dynamics_registry`). An agent that *requires* a missing module fails to load (§9).

---

## 5. Client component contract

The entry script registers exactly one A-Frame component named `component`. Guard against double
registration and no-op when A-Frame is absent:

```js
(function () {
  "use strict";
  if (!window.AFRAME) return;
  if (AFRAME.components.myeffect) return;              // idempotent
  AFRAME.registerComponent("myeffect", { /* … */ });
})();
```

### Lifecycle

- **`init`** — build GPU/DOM resources once; read `this.data`.
- **`update(oldData)`** — config changed (a reconfigure, or a peer's snapshot). Cheapest correct pattern:
  tear down and rebuild, so state stays a pure function of `this.data`.
- **`tick(time, dt)`** — per-frame step.
- **`remove`** — **fully dispose**: geometry, materials, render targets, textures, RAF handles, and every
  `ConjureBus` subscription. Leaking on a mobile-class Quest over a long session is the classic module
  bug, and disposal is a hard part of the contract rather than a nicety.

### Provided

| Surface | What it is |
|---|---|
| `this.data` | parsed config from the component `schema` (seeded by `config_schema`) |
| `this.el` | the entity; use `setObject3D(key, obj3d)` / `removeObject3D(key)` for THREE content |
| `AFRAME.THREE` | the renderer's THREE; `this.el.sceneEl.renderer` for the WebGL renderer |
| `window.ConjureClock` | the shared clock (§6) |
| `window.ConjureBus` | the cross-client event bus (§6) |
| `window.ConjurePointers` | the XR input reader + action bindings + pointer arbitration (§6) |
| `window.ConjureFrames` | frame conversion for tier-C commits (§8) |
| `window.ConjureWorldFrame` | the derived-frame deltas — skybox pose/scale and a void world's parking (§8b) |
| placement + facing | the server positions the entity before the component runs (§7) |

### Required

- Register the component named in the manifest.
- Be idempotent on double-load; no-op without A-Frame.
- Fully dispose on `remove`.
- Be deterministic where it claims to be (tier A): seed-driven, clock-driven, no runtime randomness.
- **Never break the render loop.** A throw inside `tick` runs every frame. Catch, log once, and either
  continue (`grab`'s `_once("err", …)`) or self-disable (`water`'s `this._dead = true`).

### Diagnostics

XR interaction cannot be unit-tested, so on-device tracing is the only way to see what a module is doing —
and a silent failure is indistinguishable from "never conjured". Each module mirrors its log to the
console **and** to `POST /client_log`, which lands in `temp/conjure.log` beside the server's own output,
gated by `window.CONJURE_DEBUG_LOG`. Tag by module (`[water]`, `[grab]`, `[pointers]`). Latch one-shot
messages so a per-frame condition logs once rather than flooding.

---

## 6. The runtime surface

Four globals, loaded before any module (`client/index.html`).

### `ConjureClock` — shared time

`GET /time` returns epoch ms; `client/conjure-clock.js` syncs Cristian-style (best-of-N round trips,
30 s re-sync).

- `ConjureClock.now()` → shared epoch **ms** (falls back to local time before the first sync)
- `ConjureClock.status()` → `{offset, rttMs, synced}`
- `ConjureClock.sync()` → force a re-sync

This is the **only** time input for deterministic state. Derive per-instance variation from a seeded PRNG
(e.g. mulberry32) so every client agrees; `fireflies` is the canonical example.

### `ConjureBus` — cross-client events

For interactive modules where each headset runs its **own** simulation but must react to everyone's input.

- `ConjureBus.emitShared(event, payload)` — relay to the OTHER clients. Sends a ws `module_event`; the
  server fans it out with `_broadcast_others` (`server.py:4349`), so the sender never receives its own.
- `ConjureBus.on(event, fn)` / `off(event, fn)` — subscribe / unsubscribe. `fn` receives `{event, payload}`.

A module acts on its **own** input immediately and locally, and uses the bus only for cross-client
traffic. Namespace events by module (`"water.touch"`). Carry an instance id in the payload so a client
routes per instance. **Unsubscribe every handler in `remove`.**

> Only the `shared` scope exists today. The planned `local` and `out` (downsampled LLM feed) scopes, and a
> structured event envelope (`{type, source, target, payload, t}`), are backlog.

### `ConjurePointers` — XR input, as ACTIONS

`client/conjure-pointers.js` is the **one reader of XR input** and the seam that keeps controls out of
module code. Before it, every consumer walked `session.inputSources` itself and hard-coded button indices:
four places to fix when a mapping changed, a control scheme you could only discover by reading source, and
control *sharing* that "worked" only because `grab` happened to use GRIP while `water` used TRIGGER.

Two jobs: read the XR frame **once per frame** and publish a normalized snapshot per pointer (cached on
the frame, so N consumers cost one read); and resolve semantic **actions** through a binding table, so a
module asks "is `resize` active?" and never names a button.

**Controls** (xr-standard gamepad mapping) — the vocabulary a binding may refer to:

| Control | Source |
|---|---|
| `trigger` | button 0 |
| `grip` | button 1 |
| `stickPress` | button 3 |
| `a` / `b` | buttons 4 / 5 |
| `stickX` / `stickY` | axes 2 / 3 |

**Bindings** map control → action. They are config (`Settings.bindings`, injected as
`window.CONJURE_BINDINGS`), never hard-coded in a module. Defaults (`config.py` `DEFAULT_BINDINGS`, a
single constant that both the dataclass default and `get_settings()` read — they used to carry the literal
separately, and adding an action to one left the running server serving the old scheme):

```json
{"select": "trigger", "grab": "grip", "resize": "trigger", "reel": "right.stickY",
 "yaw": "right.stickX", "pitch": "left.stickY", "bank": "left.stickX",
 "mark": "b", "surfaces": "a"}
```

The last two are diagnostics rather than interaction: `mark` writes the geometry ground-truth probe
([`spaces-geometry.md` §10.3](./spaces-geometry.md)) and `surfaces` cycles the surface debug overlay's
layers (§11 there).

A control may be **hand-qualified** (`"left.stickY"`), so one hand can hold an object while the other
shapes it. Re-binding is a config change, not an edit in every module.

**Reading pointers:**

- `ConjurePointers.list(sceneEl)` — every pointer this frame (controllers *and* tracked hands), `[]`
  outside an XR session.
- `ConjurePointers.controllers(sceneEl)` — controllers only, the common case for ray interaction.

Both are cached per XRFrame with a 4 ms recency window. The recency check matters: the browser is not
guaranteed to hand out a fresh `XRFrame` object each frame, and an identity-only cache would never
invalidate — every consumer would see the first frame's buttons forever.

**Each pointer** carries pose and resolved controls:

| Member | Meaning |
|---|---|
| `key` | stable per input source — `"right:ctrl"`, `"left:hand"` |
| `handedness`, `isHand`, `source` | the raw XR input source and its kind |
| `origin`, `dir`, `quat` | target-ray pose in the world frame (the rig sits at the origin) |
| `fingertip` | index-finger-tip position for tracked hands, else `null` |
| `value(action)` | 0..1 for buttons, −1..1 for axes, resolved through the bindings |
| `active(action)` | `value(action) >= 0.5` (`ACTIVE_AT`) |
| `started(action)` / `ended(action)` | rising / falling edge this frame (own-hand controls) |
| `armed()` | is this pointer **in use** — see below |
| `anyActive()` | is any bound action engaged |
| `availableTo(owner)` | free, or already this owner's (see arbitration) |

**`armed()` — one definition of "in use".** A pointer arms when `select` is pulled past
`beam_trigger` (default 0.05) **or** any bound action is engaged, and lingers for `beam_timeout`
(default 10 s) after the most recent pull. Continuous use keeps re-arming it, so a momentary release
mid-gesture does not flicker it off.

Arming lives in the input layer rather than in the beam so that **presentation and focus agree by
construction**: `controller-beams.js` shows a beam exactly when `armed()`, and `grab` refuses to highlight
anything when it is false. A selection box appearing with no visible beam aimed at it reads as the scene
reacting to nothing.

### Sharing a pointer between modules

Module tick order is not guaranteed, and two modules can want the same control — `resize` and `select` are
both the trigger by default. Arbitration is explicit and lives here:

| Mechanism | Lifetime | Use |
|---|---|---|
| **capture** — `claim(key, owner)` / `release(key, owner)` | until released | held for a whole gesture. While `grab` is dragging, that pointer is exclusively grab's and nothing else reacts to its buttons. |
| **reservation** — `reserve(key, owner)` | the next press; **renewed every frame** | "I'd take the next press here." `grab` reserves while the beam is on one of its corner handles, so the same trigger resizes *there* and ripples on the picture's body. |

`ownerOf(key)` returns the capture if there is one, else a reservation **made this frame or last**. That
one frame of slack is what makes reservations order-independent: a module ticking before the reserver
still defers.

The contract for a consuming module is one line, before acting on a pointer:

```js
if (!p.availableTo("mymodule")) continue;      // someone else holds or has reserved it
```

Edge state (`_was`, captures, reservations, arm windows) is dropped when a pointer vanishes, so a
reconnecting controller never inherits a stale "held".

---

## 7. Placement and anchors

The server places the carrying entity from the manifest before the client renders it.

- **`free`** — free-standing at `default_pos` (or the caller's `position`). If `face_user` is true, it is
  rotated to face the viewer **at creation** (fixed, not tracking).
- **`surface`** — conjured with `on_surface`, the entity is aligned to a real room surface, fitted to its
  frame, and stood off by `on_surface_standoff`; `meta.surface_offset` is recorded so it rides recaptures.
  An image-bearing module fits its picture's aspect *inside* the frame by default; `stretch: true` fills it.
- **`volume`** — a volumetric effect centred on the entity origin (a firefly swarm). Leave `on_surface` off.
- **`ambient`** — world-wide; position is incidental. `grab` is the shipped example.

**Billboard is orthogonal and composable.** It is its own A-Frame component (`billboard`, yaw-only), which
the server attaches to *any* module when the caller passes `billboard: true`, overriding the fixed spawn
facing. A module never implements its own billboarding.

**Image convenience.** A module config may carry `image: <image_id>` from `generate_image`/import; `/module`
resolves it to `config.src` and sizes the plane to the picture's aspect — so "a water picture of a koi
pond" is `generate_image` → `conjure_module`.

---

## 8. Tier-C: manipulating other entities

`grab` is the shipped tier-C example and the first module to read and write scene entities beyond its own
node — a curated capability, not something the contract grants generally.

### Discovering what is manipulable

Direct children of `#world-root` that have an id and are neither `data-real` (room surfaces) nor
`data-scaffold`, excluding the grab entity itself. Other modules' entities are included: a Water Picture
is grabbable.

### Determining focus

Focus follows the beam, in three stages, first hit wins:

1. **`_pick`** — an exact raycast against the object's meshes *and* the HUD's corner handles (they are
   children of the target). This decides what you *grab*, so it must be exact.
2. **`_boxPick`** — does the ray pass through the object's **selection box**, tested in the object's own
   space so the box is oriented rather than axis-aligned? A model's corner handles stand off in empty
   space; between the silhouette and a corner the ray hits nothing, and without this stage focus dropped,
   the HUD was destroyed, and the handles vanished before you could reach them.
3. **`_softHandle`** — a near-miss within 6 cm of a corner handle of the **already-focused** object. A
   3.5 cm sphere is about 1° at arm's length. Because a real hit always wins, this cannot steal a body grab.

Two rules make this stable, both learned the hard way:

- **Handles are identified by identity, not proximity** (`userData.grabHud` *and* the HUD belongs to this
  element). An earlier "nearest handle within 6 cm of the ray" test stole ordinary body grabs, because a
  small model's box corners sit well inside that slop when you aim at its middle.
- **The selection box is decoration and is not raycastable.** three raycasts `LineSegments` with a fat
  slop (`Raycaster.params.Line.threshold`, default 1 unit), which turns the wireframe into a hit volume
  6–10× the object's size. On a flat object it degenerates further: a zero-depth box gives the helper a
  singular world matrix, and every hit comes back `distance: NaN`. Hit distances are therefore also
  filtered for finiteness (`_nearest`) — a `NaN` loses every `<` comparison, so one reaching an
  accumulator pins it permanently.

No pointer, no highlight: focus requires `p.armed()` (§6).

### The HUD

An oriented `Box3Helper` plus eight corner spheres, parented to the target so they inherit its transform.
Handle radius is specified in **world** metres and divided by the target's world scale — a glTF normalized
by a ~0.005 scale would otherwise render 0.1 mm handles — then capped at a quarter of the box's shortest
**non-degenerate** side, so handles never swamp a small object and a flat image (zero depth) still gets a
usable radius.

### Gestures

Chosen by what the beam is on when the action starts, and held for as long as **the action that started
it** is held, so a gesture can never be ended by a different control:

- **`grab` on the body** → move. *Free* objects: rigid 6DOF, plus `reel` to push/pull along the beam.
  *Grounded* models: slide on the floor plane, yaw only — matching how they are re-solved on every capture.
  *Surface-attached*: slide on the host plane, clamped to its extent, keeping the original stand-off.
- **`resize` on a corner handle** → uniform scale, proportions preserved. Progress is measured **signed
  along the grabbed corner's outward axis** rather than radially: dragging a corner out is mostly lateral
  hand movement that barely changes controller→centre distance, and an unsigned measure bounced back
  through zero at the centre.
- **sticks, while holding anything with a body** → `yaw` about gravity-up; a *free* one also takes `pitch`
  and `bank`, measured against the **viewer**. Viewer-relative because nothing in a glTF records which way
  a model faces, so its own axes cannot define pitch or bank — and the viewer's frame is well-defined from
  wherever you stand, which makes it the one convention that fits every kind of content.
  A **body** means a loaded model or a `geometry` (an image plane, a primitive). Two exclusions:
  a **billboard**, which re-aims at each viewer every frame and would overwrite the spin on the next tick;
  and a **dynamic module's own entity**, which carries only its component and has nothing of its own to
  turn. Surface-attached content never reaches the stick path at all — it returns from its own branch, so
  wall art stays flush. A free-standing image can therefore be turned edge-on and effectively vanish;
  that is the user's call to make, and one nudge back undoes it.
- **release** → commit.

Scale is clamped twice: within one gesture (0.25×–4×) and in total against the size the object was first
seen at (0.02×–50×), so repeated gestures cannot compound their way to absurdity. Bounds are **relative**
because a glTF's `transform.scale` is whatever its normalization took.

### Committing

Dragging mutates the local `object3D` only; nothing is broadcast mid-drag. On release the client POSTs the
resting transform to `POST /manipulate`, and the **world server is the authority**: it authorizes
(owner-only, and refuses `meta.real` surfaces), applies, persists via autosave, and broadcasts. The mover's
echo is idempotent — `applyPatch` re-sets values it already holds — so there is no pop.

What the client sends alongside the transform, and why, is a consequence of local-first geometry
([`docs/specs/spaces-geometry.md`](../specs/spaces-geometry.md)): the dragged pose is in the **local** render
frame, while the server persists the reference frame and re-solves content from it every capture.

- `anchor` — the plane-relative anchor authored against **our own** walls (`ConjureFrames.anchorFor`),
  stored verbatim. Anchors are plane-relative, so one authored against any client's walls solves correctly
  on every other client. Letting the server re-author from a committed position instead costs extra
  author/solve hops between plane sets that are not rigidly related; the residual is content settling
  slightly off where it was dropped.
- `surface_offset` — for surface-attached content, the host-local offset (`ConjureFrames.surfaceOffset`).
  Host-relative and therefore frame-independent.
- `position`/`rotation` — converted with `ConjureFrames.toRef`. With no room basis (a void/outdoor world)
  the frames coincide and the local pose is committed as-is.

If the client sends no anchor, the server re-authors one; without that, a move is reverted at the next
capture, because anchored content re-derives its pose from `meta.anchor`.

**Permissions** are enforced twice: client-side (a guest gets a hint and grip will not grab, so there is no
local divergence) and server-side (`/manipulate` is owner-only).

`grab`'s server side is entirely generic — authorize, apply, recompute `surface_offset`, broadcast — so it
lives as a plain world-server endpoint rather than module code. Snapping and clamping stay client-side,
because you must see them as you drag; a server-commit snap would hop after release.

---

## 8b. Tier-C modes: adjusting a derived frame

`grab` has three **modes**. `object` is §8 above and the default. `skybox` and `void` adjust things with no
entity to grab: the skybox's relative orientation and scale, and a void world's content orientation and
horizontal position.

### Why these cannot commit a transform

Object mode has no competitor — nothing else writes a placed entity's transform. Both new targets are
**rewritten from the derived frame on every capture**:

| Target | Written by | Derived from |
|---|---|---|
| skybox pose | `_pinSky` | `_Tmat⁻¹` — registration in a room, `canonicalFrame` in a void world |
| `#world-root` parking (void) | `_updateWorldFrame` | the same, inverted |

A gesture that wrote either transform would be erased within about two seconds. So what persists is a
**delta the writer composes**, never a pose. This is the whole shape of the feature; the gesture is the
cheap half.

All four deltas live under **`environment.frame`** and are reached through `POST /world_frame` (owner-gated).
The request groups them as `sky` and `frame` because that reads naturally and matches the mode names;
storage keeps them together:

| Stored path | Request | Meaning | Range |
|---|---|---|---|
| `frame.skyYaw` | `sky.yaw` | degrees about gravity, turning the panorama relative to its world | unbounded (mod 360) |
| `frame.skyScale` | `sky.scale` | uniform factor on the live sky | clamped client-side by effective metres |
| `frame.yaw` | `frame.yaw` | degrees about gravity — moves the world **and its sky**, void only | unbounded |
| `frame.offset` | `frame.offset` | `[x, z]` metres, void only — **never y** | unbounded, by decision |

**Nothing is stored under `environment.sky`, and every write uses a dotted path.** Both rules are scar
tissue from the same afternoon. Writing a whole `sky` object erased `sky.src` — turning the sky one degree
threw the image away. Dotted paths fixed the document, but the *broadcast* patch still carried
`{sky: {yaw, scale}}`, and `applyEnv` reads an `env.sky` object as a complete description of the sky, so no
`src` meant no panorama and it tore the dome down on release.

`applyEnv` was right. Sharing the key was the mistake: every reader of `sky` would have had to know it might
be a fragment. The panorama and the user's adjustment are different kinds of thing, so they are different
keys — which makes the invariant structural rather than a rule anyone has to remember.

`window.ConjureWorldFrame` is the client surface. `setSky`/`setFrame` are local-only and mutate the very
fields that get persisted, so a preview cannot disagree with its commit; `commit` POSTs on release, and
`applyEnv` folds the echo back into the same fields — idempotent, so no pop.

### The gestures

Both modes grab the **floor**, which is what lets them reuse the grounded-object drag rather than needing a
new pick target. For a grounded skybox the floor genuinely *is* the dome's lower projection, so dragging a
ground point outward stretches the dome. Engaging requires pointing downward (`dir.y < 0`), so aiming at the
sky does nothing.

- **`skybox`** — the floor drag decomposed in **polar** coordinates about the sky's centre: radial → scale,
  tangential → yaw, a diagonal does both. Measured absolutely from the grab, so the grabbed point tracks
  the hand exactly and a long gesture cannot drift — safe because neither the floor plane nor the sky's
  centre moves when yaw or scale changes.
- **`void`** — a plain horizontal slide of `#world-root`, so all content *and* avatars move together, which
  is what keeps co-presence intact. Accumulated rather than absolute, because stick yaw and drag mix.
- **`yaw` on the stick**, same control and sign as object mode — but **with no grip required**. There is no
  object to be holding, and demanding a grip on the floor first is a step with nothing behind it. Applied
  once per tick rather than per pointer, because a hand-qualified binding like `right.stickX` resolves
  globally (§6), so every pointer reports the same value and a per-pointer loop would double it. A stick has
  no release event, so the commit fires when it returns to neutral.

**A void world's sky moves with its content.** `frame.yaw`/`frame.offset` are applied by `_pinSky` as well as
`_updateWorldFrame`, so turning or sliding a world carries its backdrop along — they are one frame, which is
the whole point of pinning the sky to it. `skyYaw` is then an *additional* turn of the panorama relative to
that world, which is what skybox mode adjusts.

Scale is inherently multiplicative (`r_now / r_grab`), which is what makes a 500 m plain-sky radius
reachable at all — an additive `reel` at 1.5 m/s would take 5½ minutes to walk it down. `reel` is unused in
both modes.

**Sensitivity is deliberately non-uniform.** Tangential yaw is ill-conditioned near the centre: a 0.2 m hand
movement at 0.2 m radius is ~45° of yaw and ~2° at 5 m. A minimum engage radius was considered and rejected
— you learn a turntable's feel faster than a rule about where you may touch it, and grab-far-for-fine is
self-teaching. What *is* required is an epsilon floor on the grab radius **for the arithmetic only**:
`r_now/r_grab` at zero is `Infinity`, and a non-finite value reaching a transform blanks that branch of the
scene graph and stays blanked.

`WM.yawAboutPivot` and `WM.polarDrag` hold the planar maths, extracted to `world-model.js` so they can be
unit-tested — XR interaction cannot be, and these two identities are exactly where a sign error hides.

### Modes are hybrid

A hit on an object still grabs the object in **any** mode, so object nudging stays available while
positioning a world; anything else engages the mode's target. Focus uses the same `_pick` → `_boxPick` stack
everywhere — only `_softHandle` is object-only, since it exists to reach corner handles and those are not
drawn elsewhere.

The **box is drawn in every mode**; the corner handles only in object mode, where resize is reachable. The
box was briefly suppressed outside object mode as noise, which missed that it is not decoration but the
focus *region*: without it you must strike the mesh triangles exactly, and a model a few metres away is a
small target — objects went from easy to effectively unmovable.

Gripping empty space is safe here in a way it would not be as default behaviour: pointing at nothing is a
controller's resting state, so this only ever fires inside a mode the user deliberately entered and that is
named on screen.

### What is blocked in a captured room

Only skybox **yaw** works there. Scale would shrink the sky's opaque sphere — which `applyImmersion` keeps
visible precisely to occlude passthrough — until it intersects the real walls; because it writes depth, that
reads as a hard edge slicing across the room. Void mode is meaningless there at all: local-first forces
`#world-root` to identity, so a move is reverted at the next capture and desynchronises content from the
real walls in between. The radial term simply goes inert, so the same gesture still yaws.

### The sky is pinned in position, not only rotation

`_pinSky` sets position as well as quaternion (horizontal only — `y` is forced to 0, because a grounded
dome's projected ground lands at the entity's `y`). Before this the sky was the one thing in the scene not
anchored to the space: content was parked on the frame while the sky sat at the raw refSpace origin. Two
faults followed — across sessions a dome's ground centre returned to a different physical spot than content
did, and a Meta-button recenter (the refSpace `reset` listener) slid the sky's centre out from under content
that stayed put. Pinning position fixes both and gives the radial drag one unambiguous centre.

**Grounded scale needs no geometry rebuild.** Scaling every vertex by *k* yields sphere radius `k·radius` and
threshold `y1' = k·y1` while the warp factor `f = −height/tmp.y` is unchanged, so a uniform `object3D` scale
is *precisely* a rebuild at `(k·height, k·radius)`. A rebuild would be ~33k vertices per drag frame.

**`height` is where the horizon sits, not how tall the dome is.** Every vertex below `y1 = −1.5h` lands at
local `y = −h`, which `mesh.position.y = h` puts at world 0 — while the equator stays at local 0, i.e. world
`h`. So `height` is the panorama's implied capture height, and the dome's **apex is at `height + radius`**.
The indicator therefore reports **radius** as the size (plus the horizon height when grounded): calling a
dome with a 3.95 m ceiling "0.2 m" read as a broken gesture when only the label was wrong. The scale bounds
are on radius too, so a value at the limit reads as being at the limit.

**The grounded dome writes depth**, so content outside it is hidden behind it. It was `depthWrite: false` —
defensible while radius was fixed at 30 m and everything was inside — but once radius became a live control
you can shrink the dome around you, and objects left outside drew straight through it. A backdrop at
infinity is a special case of correct depth, not a substitute for it. `polygonOffset` biases the dome back in
depth rather than moving its ground, because the projected ground sits at world `y = 0` exactly where
floor-standing content rests, and coplanar surfaces z-fight.

**A plain sky's scale is a clipping control, not a size control.** From the centre of the sphere the view is
identical at any radius — the texture subtends the same angles. What changes is occlusion (content beyond
the radius hides behind the sky) and parallax (at 500 m, walking 2 m shifts the image 0.4%; at 5 m, ~40%,
and the panorama swims). The metres readout is the only feedback it has.

### Switching modes, and the indicator

Modes are set by **voice or CLI, never a button**. `/module` on a singleton reuses and reconfigures its one
live instance, so `conjure_module(module="grab", config={"mode": "skybox"})` reconfigures the running
component — no new endpoint, no binding spent. Modes **persist until changed**; these are rare, deliberate,
setup-time acts, so voice latency is right where a constantly-toggled control would not be.

Outside object mode a head-locked indicator is **always on** (`#grab-hud`, same `overlay` pattern as
`#coloc-hud`), reporting the mode, the yaw, and the effective size in metres. In object mode it is absent,
so normal use gains no clutter.

This is a safety mechanism rather than decoration. The director sometimes reports success without calling
the tool, and here that failure is silent and expensive — you would grip expecting to turn the sky and
instead fling a chair across the room. **The indicator appearing is the confirmation the tool fired**,
available before you touch anything.

### Reset

`reset_world_frame` (voice) and `conjure-ctl world-frame --reset` clear the deltas so the derived frame
stands alone — `sky` (`skyYaw`/`skyScale`), `frame` (`yaw`/`offset`), or `all`. This is the **only** recovery path, by design: with no minimum engage radius one twitch near
the centre can apply a large yaw, a symmetric panorama gives no way to tell yaw 0 from yaw 180 by eye, and
an unbounded void offset can put the world — including the floor point you would need to grab to drag it
back — out of reach.

---

## 9. Discovery, scoping, and conjuring

Modules are **scoped to an agent**. An agent declares what it may conjure in `agent.json`:

```jsonc
"dynamics": ["fireflies", "water", "grab"],              // REQUIRED allow-list
"context": ["room://current", "world://current", "dynamics://available"]
```

Every listed name is **required**: the agent fails to load if one is not found on the search path.

Scoping governs **conjuring**, in two places at once:

- **Soft (discovery).** `GET /dynamics/available` builds the catalog from the *active agent's* scoped
  modules — one `name — description; params: …` line each — surfaced as the `dynamics://available` MCP
  resource and injected into the director's prompt each turn, the same mechanism as `room://current`. No
  discovery ritual and no dynamic tool schema; `conjure_module` stays one generic tool.
- **Hard (enforcement).** `/module` validates the requested module against the active agent's `dynamics`
  and refuses an out-of-scope name, even if the module exists on the server.

**Client loading is deliberately NOT scoped.** The server injects
`<script src="/dynamics/<name>/<entry>?v=<mtime>">` for **every discovered module** into `index.html` at
the `__DYNAMIC_MODULES__` marker (`_dynamic_module_tags`). The `?v=<mtime>` stamp busts the Quest's
stubborn cache when a module's code changes.

Scoping the tags to the active agent was a bug, because a page's scripts are fixed the moment it loads
and the live agent is not. Space selection joins the matched room's world in whatever scope owns it
(`/space/select`), `agent <name>` moves the pointer, a session switch moves it again — any of which can
hand a headset a world full of components its page never registered. The failure is silent:
`el.setAttribute("grab", {…})` on an unregistered A-Frame component is just a DOM attribute, so the
module renders nothing, logs nothing, and only a manual page reload recovers. Registering a component
is inert until an entity carries it, so serving all of them costs a few KB and makes the whole class of
ordering bugs impossible. Registered client-side ≠ conjurable by this agent.

The director conjures with one generic tool:

```
conjure_module(module, config?, position?, on_surface?, billboard?, stretch?, name?)
dismiss_module(name=<entity id>)  |  dismiss_module(module=<kind>)
```

`name` reuses and reconfigures an existing instance; a `singleton` module reuses its one instance
automatically. `dismiss_module(module=…)` matches by `meta.module` **or** by the entity carrying the
module's component, so it also catches instances placed outside the tool.

### HTTP surface

| Endpoint | Purpose | Owner-gated |
|---|---|---|
| `GET /dynamics/available` | the active agent's catalog (`{modules, catalog}`) | no |
| `GET /dynamics/<module>/<file>` | serve a module's script/asset; basename-only, traversal-guarded, `no-store` | no |
| `POST /module` | conjure/reconfigure an instance | **yes** |
| `POST /module/dismiss` | remove instance(s) | **yes** |
| `POST /manipulate` | commit a tier-C resting transform | **yes** |
| `GET /time` | shared-clock reference | no |
| `POST /client_log` | module diagnostics → `temp/conjure.log` | no |
| ws `module_event` | tier-B bus relay to peers | no |

---

## 10. The shipped modules

### `fireflies` — tier A

A swarm of glow points wandering around the entity origin. State is `f(clock, seed, config)`: per-firefly
base points and orbit frequencies/phases/amplitudes derive from a seeded PRNG, and `tick` advances them
from `ConjureClock.now()` — so every headset shows the identical swarm with zero sync. `anchor: "volume"`,
no `face_user`. `remove` disposes the `THREE.Points` geometry and material.

### `water` — tier B

An image seen through a rippling clear-water surface: a GPU wave-equation sim (ping-pong half-float
height/velocity field, reflecting boundaries) refracts the picture and adds specular glints.

Touch or drag it — fingertip proximity for tracked hands, or the controller ray while `select` is held —
to make waves. A touch disturbs the local sim immediately **and** `emitShared("water.touch", …)` so peers
stamp the same disturbance into their own sims. Drags are rasterized along the segment so fast movement
leaves no gaps.

Idle when still: after the last touch it keeps simulating only until ripples damp out, then does no GPU
work at all. `face_user: true`; `on_surface` hangs it on a wall fitted to the frame; `billboard` makes it
follow the viewer. `remove` disposes render targets, geometry, materials, and unsubscribes from the bus.

### `grab` — tier C

A singleton, `anchor: "ambient"` module that repositions, rotates, and resizes **other** placed objects.
Fully described in §8. Its `mode` config switches it to adjusting the skybox or a whole void world instead
(§8b) — the only module that writes `environment` rather than an entity.

### Not a module: `controller-beams`

`client/controller-beams.js` draws the laser from each controller. It is ordinary client infrastructure,
not a conjurable module — but it reads the same `ConjurePointers` layer and keys purely off `armed()`,
which is what keeps the visible beam and grab's highlight in agreement.

---

## 11. Checklist for a new module

1. `dynamics/<name>/module.json` — `component`, `entry`, `anchor`, `description`, `config_schema`, and
   `actions` if it reads input.
2. `dynamics/<name>/<entry>.js` — register the component; idempotent; deterministic where claimed; never
   throw out of `tick`; full disposal in `remove`.
3. Read input only through `ConjurePointers`, by **action**; check `availableTo` before acting; `claim`
   for the duration of a gesture and `release` on end.
4. Add `<name>` to an agent's `agent.json` `dynamics` list (and `dynamics://available` to its `context`).
5. Conjure it: `conjure_module(module="<name>", …)`. Confirm it renders, is shared across two clients,
   and disposes cleanly on `dismiss_module`.
