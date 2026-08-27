# Dynamics — backlog

Unfinished work, future directions, and known problems for dynamic modules. The current state is
[`docs/specs/dynamics.md`](../specs/dynamics.md); the reasoning behind rejected alternatives is
[`docs/decisions.md`](../decisions.md).

Items are grouped by what they block, roughly most-actionable first.

---

## Contract gaps — declared but not enforced

**Make `actions` load-bearing.** `module.json` `actions` is parsed into `DynamicModuleDef.actions`
(`dynamics.py:152`) and read by nothing. `grab` and `water` both declare it accurately, so it is honest
documentation and no more. At minimum it should be validated against the known action names (the keys of
`Settings.bindings`) at load, so a typo fails loudly instead of silently. Beyond that it could feed the
director catalog ("this module uses the trigger"), and — the real prize — let the arbitration layer know
which modules contend for which action *before* a frame, instead of discovering it through
capture/reserve at runtime.

**Validate `tier`.** Accepted as any string, defaulted to `"A"`, acted on nowhere. Either validate it
against `A|B|C` or drop the field. Currently a manifest claiming `"tier": "Z"` loads fine.

**Manifest fields designed but never built.** The original contract sketched several fields that the
loader has no notion of:

- `capabilities` — what a module may touch (`bus`, `clock`, `sceneGeometry`, `assets`, `audio`), explicit
  so a future sandbox can enforce it. Today every module gets every global.
- `claims` — exclusive single-owner resources (`skybox`, `lighting`, `gravity`, `audioMaster`). Conjuring
  a second claimant should be detected and rejected **at conjure time**, not discovered at render time.
- `requires` / `prefers` / `fallback` — geometry needs, and what to do when they are unmet.
- `version`.

## Placement in surfaceless worlds

Conjure has outdoor worlds with no captured surfaces, and the spec treats captured geometry as optional
context — but the *resolution* half is not built.

- **Advertise a geometry profile** per world (`surfaces: none|captured|authored`, `ground: yes/no`,
  `bound: …`) and have `/module` check a module's `requires` against it, then **place, fall back, or
  refuse with a legible reason — at conjure time, before anything mounts**. `place_image` already degrades
  this way (wall placement becomes free-standing outdoors); modules should reuse that resolution rather
  than each inventing one. Today `anchor: "surface"` with no matching surface just returns
  `no room surface matches …`.
- **A frame of reference always exists** even in a surfaceless meadow: user pose, world origin, up-vector,
  ground reference. That quartet is the universal substrate; anything richer is a bonus a module must do
  without.
- **`volume` anchors** should take a bound spec defaulting to room extents when available and explicit
  dimensions otherwise — never assuming "the room" exists.

## `sceneGeometry` — physical plausibility as a capability

Room-geometry awareness is **decoupled from anchoring**. Fireflies (volume) want walls as *colliders* so
they do not clip through; a bouncing ball (free) wants a floor. That is a read-only `sceneGeometry`
capability, requested independently of where a module is anchored.

**One provider, two backends.** Modules must never ask "is this a real room?" They ask `sceneGeometry`,
which answers uniformly: passthrough room → captured planes; outdoor VR world → whatever the world
authored (usually a ground plane and a play-area bound, no walls). Requesting modules **must handle the
sparse/null case**.

## The event bus — only one third of it exists

`ConjureBus` implements the `shared` scope and nothing else. Missing:

- **`local`** — never leaves the client (my own pointer hover). Today modules just call each other's code
  or use raw DOM events.
- **`out`** — a **downsampled/summarized** feed to the LLM. Never raw drag frames: *"user dragged the
  globe for 10 s and let go facing Mars"*. This is what lets the director react to what people did without
  drowning in events.
- **A structured envelope.** Today a payload is whatever the module sends.
  Designed shape: `{type, source: llm|user:<id>|server:<mod>|module:<id>, target: <modId>|broadcast,
  payload, t}`, ordered by `t` with a tiebreak. Eventual consistency is fine for tiers A and B.
- **`module_event(target, event)` as an agent tool** — emit a coarse event at a running module ("make that
  one calmer"). `module_event` exists only as a ws relay between peers; the director cannot address a
  running module at all.

## Modules in a world constructor

A world should be able to boot with its dynamic layer already present. `agent.json`'s `world.on_create`
runs a small step vocabulary (`_WORLD_COMMANDS` plus the skybox steps) which has no `conjure_module`, so
today every module must be conjured after the fact.

## The rule/trigger layer

Three tiers of reactivity, of which the middle one does not exist:

- **Module loop** (frame rate) — built; the module *is* the realtime agent.
- **Server rules/behaviors** (milliseconds) — *missing*. A lightweight declarative rule engine:
  `when beat.kick → emit flash`. The point is that ~80% of reactivity then needs no custom code **and** no
  LLM round trip.
- **LLM** (seconds, semantic) — built. The choreographer, not the dancer: sets mood/parameters/goals,
  authors configs, reacts to the summarized feed.

## Server modules

Per-module **server** logic — a runtime-agnostic "server module" — is a deliberate future track. `grab`
does **not** motivate it (its server side is entirely generic), so it should be designed *by* the module
that does: an autonomous/emitting one such as the music transport, the rule engine, or a shared-selection
arbiter. Alignments already made, and the reasons they were made this way, are in
[`docs/decisions.md`](../decisions.md) §17.

Related: the world-server → Node question, [`docs/decisions.md`](../decisions.md) §18. The actionable
first step there is independent of Node and worth doing on its own merits — **extract the shared
geometry/placement math into one pure-JS module** that the client uses and the server consumes. That
duplication (`_face_room` / `_plane_basis` / `_fit_extent` / `_surface_offset` / quaternion+YXZ-euler, all
shadowed in `room-snap.js` / `world-model.js` / `plane-anchor.js`) is the source of a whole class of
parity bugs: YXZ order, quat→euler, the boundary frame-flip, normals-outward.

## Modules not yet built

- **Music visualization** (milkdrop-style). Do **not** relay audio analysis from the server — latency
  wrecks beat-locking. The server broadcasts the **transport** ("track X, play at shared-clock t0") and
  each client runs its own Web Audio `AnalyserNode` locally, in sync: cause, not effect. A server music
  module *also* emits semantic cues (tempo, key, major/minor) onto the bus for the LLM and for rule-driven
  modules that should not do their own DSP.
- **Photo library** — a `library.db` query + thumbnail grid + selection events. The natural bridge between
  dynamic content and the existing asset system, and a genuine tier-C case (shared "which photo are we
  looking at").
- **Solar system**, **spirographs** — tier-A, exercise `f(clock, seed, config)` at larger scale.
- **Model articulations** — tier-B, touch-triggered.

## Generated modules

LLM- and user-generated modules are in the plan and deferred until the framework is mature with several
curated examples. The trust boundary is real even under "no security — users are identity only":
arbitrary JS/shaders can hang the render loop or crash the GPU. See
[`docs/decisions.md`](../decisions.md) §7 (sandboxing for LLM-authored code).

The design intent, which shapes the manifest now: when generation lands, **real code stays in a curated
registry and the generator emits only config + wiring** against the constrained surface — never arbitrary
shader source into the render loop. `config_schema` is the first slice of that boundary.

**Optional modules in `agent.dynamics`.** Every listed name is currently required and a missing one fails
the agent's load. Optional entries were deliberately deferred.

## Performance and robustness

- **Quest perf budget** — the limitation that bites hardest. N particle systems and shaders on a
  mobile-class GPU drop frames (cf. the walking-microstutter reprojection finding in
  [`investigations/pops-and-jitters.md`](../investigations/pops-and-jitters.md)). Needs an
  **active-module quota** and LOD; neither exists.
- **Disposal is unpoliced.** Leaked GPU memory on unload crashes the headset over a long session. The
  contract requires full disposal in `remove`, but nothing verifies it. A dev-mode leak check after
  `dismiss_module` would catch the classic bug.
- **JS determinism** is imperfect — variable framerate and cross-device float divergence. A fixed-timestep
  accumulator keyed to the shared clock is the designed mitigation and is not implemented; modules
  currently integrate on raw `dt`.

## Known problems

None of these has been reproduced on device, but they are not all the same strength, so each says which
it is. **Certain** = the code plainly does this, with a line to check. **Unproven trigger** = the
mechanism is certain but nobody has shown the condition that fires it actually arises.

- **`grab` focuses one object across both controllers.** *Certain* — `hover` is one variable
  (`grab.js:545`), reassigned per pointer, and `_setHud(hover)` runs once after the loop (`:597`), so
  two controllers cannot highlight two different objects — the last pointer in the list wins. Harmless
  today, wrong as soon as two-handed manipulation matters.
- **A stale selection box survives a geometry change.** *Unproven trigger.* The mechanism is certain —
  `_setHud` early-returns when the focused element is unchanged, so the HUD is never rebuilt while focus
  is held. The box still tracks *scale*, being parented to the target; only a geometry change (an image
  re-fitted to its surface) would go stale, and nobody has shown that happens while focus is held.
- **The guest hint is console-only.** *Certain* (`grab.js:534`). The design calls for a guest to see the
  highlight plus an "ask the owner" hint; `_hint()` writes `console.warn`, invisible in a headset, so a
  guest sees the highlight and nothing happening.
- **`water` self-disables permanently on first error.** *Certain* — `this._dead = true` (`water.js:264`)
  is checked at `:240` and appears nowhere else, so one transient GPU hiccup silently kills ripples until
  the module is re-conjured.

## Record / replay

Because tiers A and B are procedural and event-sourced, a session is deterministically replayable from
`(seed, clock, event log)` — near-free debugging and "show me what happened". Nothing records the event
log today, so this is unrealized.

## Modules reacting to each other

Once the bus carries structured events, modules can subscribe to each other: fireflies swarm the kick
drum, water tints to the music's key. Emergence for roughly free — but it depends on the event envelope
and the rule layer above.
