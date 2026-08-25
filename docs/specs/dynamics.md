# Dynamics — the spec for conjurable, live, shared modules

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/dynamics.md`](../backlogs/dynamics.md); rejected alternatives
and the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

A **dynamic module** is a live, animated, interactive effect the director can conjure into a world —
fireflies, a rippling Water Picture, object manipulation. This spec is the contract for implementing
one, and the reference for the runtime surface a module is handed. It is the counterpart to
[`docs/agents.md`](../agents.md) for agents.

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
  (`/module`, `/module/dismiss`, `/manipulate` ∈ `_OWNER_ONLY_PATHS`, `server.py:595`). No new authority
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
  "config_schema": {               // the LLM-facing params: {type, default, desc}
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
| `config_schema` | no (`{}`) | — | `{param: {type, default, desc}}` |

Notes:

- **`config_schema`** is the parameter surface the director may set. It is rendered into the catalog as
  `name — description; params: k(default), …` (`DynamicModuleDef.catalog_line`). The **authoritative**
  defaults and types still live in the component's own A-Frame `schema` client-side; keep the two
  consistent. A param with no `default` (e.g. `image`) is shown bare.
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
  server fans it out with `_broadcast_others` (`server.py:4317`), so the sender never receives its own.
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
`window.CONJURE_BINDINGS`), never hard-coded in a module. Defaults (`config.py:339`):

```json
{"select": "trigger", "grab": "grip", "resize": "trigger", "reel": "right.stickY",
 "yaw": "right.stickX", "pitch": "left.stickY", "bank": "left.stickX"}
```

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
- **sticks, while holding a model** → `yaw` about gravity-up; a *free* model also takes `pitch` and `bank`,
  measured against the **viewer**. Viewer-relative because nothing in a glTF records which way a model
  faces, so its own axes cannot define pitch or bank. Images are excluded — turning a picture edge-on is
  only a way to lose it.
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
([`docs/local-first-geometry.md`](../local-first-geometry.md)): the dragged pose is in the **local** render
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
Fully described in §8.

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
