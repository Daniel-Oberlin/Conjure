# Dynamic module spec — the contract for a conjurable, live, shared effect

**Status:** living contract (2026-08-21). This is the authoritative reference for implementing a
*dynamic module*: a live, animated, shared effect the director can conjure into a world (fireflies, a
rippling Water Picture, …). It is the counterpart to `docs/agents.md` for agents, and reflects the
first-class/extensible structure landed in `docs/dynamic-modules-refactor-plan.md`.

A module is **just an A-Frame component** delivered as config-in-snapshot: the world server adds an
entity carrying the component (`entity.components.<component> = <config>`), the client applies it via
`setAttribute`, and it is therefore shared across clients, persisted, and replayed — for free, on the
existing entity/patch/snapshot path. There is no bespoke per-module loader. What makes a module
*first-class* is only the folder + manifest + search-path resolution described here.

---

## 1. Folder layout — mirror `agents/`

Each module is a self-contained directory. The **directory name is the module's identity** (the
manifest needn't repeat it). Bundled modules live in `dynamics/` at the repo root; user modules live in
`~/.config/conjure/dynamics/<name>/` and **shadow** a bundled module of the same name.

```
dynamics/
  fireflies/
    module.json          # the manifest (see §2)
    fireflies.js         # the client script that registers the A-Frame component
  water/
    module.json
    water.js
    assets/…             # optional per-module assets travel with the module
```

**Resolution** (`conjure.dynamics.resolve_module_dir`, mirroring `resolve_agent_dir`):
`env CONJURE_DYNAMICS_PATH` (os-sep list) → `settings["dynamics_path"]` →
`[<config_dir>/dynamics, BUNDLED_DYNAMICS_DIR]`, **user-first** (first match wins). Add or override a
module by dropping a folder in your config dir — no code change, no restart.

---

## 2. `module.json` — the manifest (mirrors `agent.json`)

```jsonc
{
  "component": "water",            // REQUIRED: the A-Frame component the entry registers
  "entry": "water.js",             // REQUIRED: client script(s) to load — string or list, in order
  "tier": "B",                     // A|B|C (informational; docs/dynamic-content-plan.md tiers)
  "anchor": "free",                // free | surface | volume | ambient (§4)
  "singleton": false,              // true = one live instance, reused/reconfigured across conjures
  "face_user": true,               // free-standing flat content faces the viewer AT CREATION (fixed)
  "default_pos": [0.0, 1.4, -1.2], // where it centres when no position is given (metres)
  "description": "One line — feeds the director catalog (dynamics://available).",
  "config_schema": {               // the LLM-facing params: {type, default, desc}
    "damping": { "type": "number", "default": 0.996, "desc": "→1 = long-lived ripples" }
  }
}
```

Field notes:
- **`component`** must equal the component name the entry script registers with `AFRAME.registerComponent`.
- **`entry`** filenames are served from the module folder at `GET /dynamics/<name>/<file>` (mtime-versioned
  so a code change busts the Quest's cache). List multiple files if the component spans scripts; they load
  in order.
- **`config_schema`** is the parameter surface the director may set. It is rendered into the catalog as
  `k(default)…` and documents each param for the LLM. The **authoritative** defaults/types still live in
  the component's own A-Frame `schema` (client-side); keep the two consistent. A param with no `default`
  (e.g. `image`) is shown bare.
- Loading validates: non-empty `component`, at least one `entry` that **exists** on disk, and a known
  `anchor`. A malformed manifest is skipped by the world server (logged), never fatal.

---

## 3. Client component contract

The entry script registers exactly one A-Frame component named `component`. Guard against double
registration (the script may load once per agent) and no-op when A-Frame is absent:

```js
(function () {
  "use strict";
  if (!window.AFRAME) return;
  if (AFRAME.components.myeffect) return;              // idempotent
  AFRAME.registerComponent("myeffect", { /* … */ });
})();
```

### Lifecycle (A-Frame) — `init / update / tick / remove`
- **`init`** — build GPU/DOM resources once; read `this.data`.
- **`update(oldData)`** — config changed (a reconfigure or a peer's snapshot). Cheapest correct pattern:
  tear down and rebuild so state stays a pure function of `this.data`.
- **`tick(time, dt)`** — per-frame step. Tier-A modules compute state as `f(sharedClock, seed, config)`
  so every headset shows the same thing with **zero per-frame sync** (see §5).
- **`remove`** — **fully dispose**: geometry, materials, render targets, textures, `RAF` handles, and every
  `ConjureBus` subscription. Leaking on a mobile-class Quest over a long session is the classic module bug.

### Provided to a module
- **`this.data`** — parsed config from the component `schema` (seeded by the manifest's `config_schema`).
- **`this.el`** — the entity; use `this.el.setObject3D(key, obj3d)` / `removeObject3D(key)` for THREE content.
- **`AFRAME.THREE`** (a.k.a. `THREE`) — the renderer's THREE; `this.el.sceneEl.renderer` for the WebGL renderer.
- **`window.ConjureClock`** — the shared clock. `ConjureClock.now()` → shared epoch **ms** (falls back to
  local time before it syncs). Divide by 1000 for seconds. This is the ONLY time input for deterministic
  (tier-A) state — never `Date.now()`/`Math.random()` at runtime; derive per-instance variation from a
  seeded PRNG so every client agrees.
- **`window.ConjureBus`** — the cross-client event bus (§6).
- **Placement + facing** — the server positions the entity (anchor/`default_pos`/`face_user`) before the
  component runs; `billboard` is a separate, composable component the server attaches on request (§4).

### Required of a module
- Register the component named in the manifest.
- Be idempotent on double-load; no-op without A-Frame.
- Fully dispose on `remove` (see above).
- Be deterministic where it claims to be (tier A): seed-driven, clock-driven, no runtime randomness.

---

## 4. Placement & anchors

The server places the carrying entity from the manifest before the client renders it:
- **`free`** — free-standing in space at `default_pos` (or the caller's `position`). If `face_user` is
  true, it's rotated to face the viewer **at creation** (fixed — not tracking).
- **`surface`** / on-surface — when conjured with `on_surface`, the entity is aligned to a real room
  surface and fitted to its frame (like `place_image`); it rides the surface across recaptures.
- **`volume`** — a volumetric effect centred on the entity origin (e.g. a firefly swarm); leave
  `on_surface` off.
- **`ambient`** — world-wide/environmental; position is incidental.

**Billboard is orthogonal and composable.** It is its own A-Frame component (`billboard`, yaw-only by
default). The server attaches it to *any* flat module when the caller passes `billboard: true` ("always
face me"), overriding the fixed spawn facing. A module doesn't implement its own billboarding.

---

## 5. The shared clock & determinism (tier A)

Tier-A modules render `state = f(ConjureClock.now(), seed, config)`. Because the clock is shared (each
headset estimates its offset to server time) and the seed/config travel in the snapshot, every client
computes the **same** state each frame with no per-frame messaging — the cheapest way to honour "one
shared reality." Use a small seeded PRNG (e.g. mulberry32) for per-element params; `fireflies` is the
canonical example.

---

## 6. The shared-event bus (tier B, interactive)

For interactive modules where each headset runs its **own** simulation but must react to everyone's
input (e.g. water ripples), use `window.ConjureBus`:
- `ConjureBus.emitShared(event, payload)` — relay an event to the OTHER clients (server fans it out).
- `ConjureBus.on(event, fn)` / `ConjureBus.off(event, fn)` — subscribe / unsubscribe. `fn` receives the
  inbound `{event, payload}` message.

A module acts on its **own** input immediately (local), and uses the bus only for the shared,
cross-client traffic. Namespace events by module (`"water.touch"`). Unsubscribe every handler in
`remove`. Payloads should carry an instance id so a client ignores/routes events per instance.

---

## 7. How the director discovers + conjures a module

Modules are **scoped to an agent**. An agent declares the modules it may conjure in `agent.json`:

```jsonc
"dynamics": ["water", "fireflies"],          // REQUIRED allow-list — the agent fails to load if a name
                                             // isn't found on the dynamics search path
"context": ["room://current", "dynamics://available"]   // inject the catalog each turn
```

- **Soft scoping (discovery):** the world server builds `dynamics://available` from the *active agent's*
  scoped modules — one line each, `name — description; params: k(default)…` — and injects it into the
  director's prompt each turn (the same context-injection mechanism as `room://current`). No discovery
  ritual, no dynamic tool schema; `conjure_module` stays one generic tool.
- **Hard scoping (enforcement):** `/module` (and thus `conjure_module`) validates the requested module
  against the active agent's `dynamics` and refuses an out-of-scope name — even if the module exists on
  the server. Soft + hard together.
- **Client loading:** the world server injects `<script src="/dynamics/<name>/<entry>?v=<mtime>">` for
  the active agent's modules into `index.html`, re-injected on agent switch. "Not scoped → not available"
  holds client-side too.

The director conjures via the generic tool:
`conjure_module(module=<name>, config={…}, position?, on_surface?, billboard?, name?)`, and removes with
`dismiss_module(name=<entity id>)` or `dismiss_module(module=<kind>)`.

---

## 8. Worked examples

### `fireflies` — tier A (autonomous, minimal)
A swarm of glow points wandering around the entity origin. State is `f(clock, seed, config)`: per-firefly
base points and orbit freqs/phases/amps derive from a seeded PRNG, and `tick` advances them from
`ConjureClock.now()` — so every headset shows the identical swarm with zero sync. `remove` disposes the
`THREE.Points` geometry + material. `anchor: "volume"`, no `face_user`. See `dynamics/fireflies/`.

### `water` — tier B (interactive)
An image seen through a rippling water surface. Each headset runs its **own** height-field sim (a
render-target ping-pong); a touch/drag disturbs the local sim immediately AND `emitShared("water.touch",
{id, u, v, strength})` so peers stamp the same disturbance into their sims. `face_user: true` makes a
free-standing picture face the viewer at creation; `on_surface` hangs it on a wall fitted to the frame;
`billboard` (composed by the server) makes it follow the viewer. `remove` disposes the render targets,
geometry, material, and unsubscribes from the bus. See `dynamics/water/`.

---

## 9. Checklist for a new module

1. `dynamics/<name>/module.json` — `component`, `entry`, `anchor`, `description`, `config_schema`.
2. `dynamics/<name>/<entry>.js` — register the component; idempotent; deterministic where claimed; full
   `remove` disposal.
3. Add `<name>` to an agent's `agent.json` `dynamics` list (and `dynamics://available` to its `context`).
4. Conjure it: `conjure_module(module="<name>", …)`. Confirm it renders, is shared, and disposes cleanly.
