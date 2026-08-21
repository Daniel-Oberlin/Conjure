# Dynamic content — plan

**Status:** DESIGN (2026-08-19). Not yet built. Introduces *dynamic modules*: browser-side
components that run animations and interactive effects (interactive water, fireflies, solar system,
milkdrop-style music viz, spirographs, photo-library display, model articulations). Modules load and
unload on demand — declared in a world's constructor or conjured/dismissed live by the agent — react
to events, and maintain their own state.

## Core principle

**Sync causes, never effects. Separate simulation (shared, must be consistent) from presentation
(local, per-client).**

We already live this: billboard yaw is computed per-client and nobody cares that each headset renders
a slightly different rotation, because it's *presentation*, not *state*. Push it further:

- Never broadcast the water's per-frame distortion field — broadcast the **touch** (`{point, t}`) and
  let every client run the same ripple sim from it.
- Never sync the solar system frame-by-frame — it's `f(sharedClock, seed, config)`, so every headset
  independently computes the same position with zero runtime sync.

This collapses most of the wish-list into "nearly free to keep consistent across clients."

## The invariant (one shared reality, absolute)

> A module's **presence** (loaded/unloaded) and its **simulation state** are shared by everyone.
> Its **presentation** need not be pixel-identical.

The second half isn't a loophole — it's how we *honor* absoluteness cheaply. Procedural state keyed off
a shared clock + seed + config (all in the world snapshot) means everyone sees the same thing without
per-frame sync, while per-client cosmetics (foveation, billboard yaw, interpolation) stay legal. "One
shared reality, absolute" and "procedural tier-A sync" are the *same* decision.

Consequences:
- **Load/unload is a world-state fact** — it flows through the existing snapshot/patch and lands under
  `_owner_only_writes`. The agent-in-session and the world owner conjure/dismiss modules; visitors
  can't. No new authority model — it's another kind of scene mutation.
- **Modules are entities** with component config in the scene JSON, exactly like placed images/models.
  A procedural module persists by storing `(seed, config)`; reload restores it exactly.

## Sync tiers (drives the build order)

Classify every module by how much it must sync. Determinism is a **per-module capability flag**, not a
mandate — A/B opt in and get free sync; C opts out and pays for patches.

| Tier | Examples | What syncs | Cost |
|---|---|---|---|
| **A — Autonomous-procedural** | solar system, spirograph, ambient fireflies, milkdrop | `(clock, seed, config)` in the snapshot; **nothing at runtime** | ~free |
| **B — Input-reactive-deterministic** | water ripple, touch-triggered articulations | the input **event** (`{point, t}`); each client sims deterministically; effects short-lived so divergence is moot | cheap |
| **C — Shared-authoritative-state** | photo selection, a shared draggable's resting position, "which slide are we on" | genuine shared state via snapshot/patch; **rate-limited** | expensive |

**Why B is a small delta over A:** B's realtime part (the ripple animating) is the same client-local
sim loop as A; the only new thing is *one shared event* that seeds it. So B = A's runtime + the event
bus + XR touch raycast. Build A first and you get ~80% of B's machinery for free.

Determinism caveats (real but bounded): `Math.random`, variable framerate, and cross-device float
divergence make JS determinism imperfect. Mitigate with a fixed-timestep accumulator keyed to the
shared clock and explicitly seeded PRNGs. It's good enough for cosmetic/short-lived effects (exactly
A/B). Anything authoritative goes to tier C.

## Prerequisite — the shared clock (step 0)

**Status:** BUILT (branch `dynamic-content`). Server `GET /time` returns epoch ms; `client/conjure-clock.js`
syncs to it Cristian-style (best-of-N round-trips, 30 s re-sync) and exposes `window.ConjureClock.now()`
(shared epoch ms), `.status()`, `.sync()`, falling back to local time until the first sync. Chose HTTP
`/time` over a `/ws` message for isolation + testability; can move onto `/ws` later if RTT variance
matters. Two-headset `now()`-agreement check still pending on-device (needs a shared visual → arrives
with fireflies).

All of A/B/C need a common monotonic time. Build it first, standalone and independently testable:

- A small NTP-ish handshake over the existing `/ws` estimates server-time offset per client and
  smooths it. Everything procedural keys off this `clock`.
- **Test in isolation:** two headsets, one shared counter, verify they agree within tolerance. No
  module required.

## Module contract

`#1` decision — **curated modules first, LLM/user-generated later** — makes the contract load-bearing
*now*: design the boundary as the surface a future sandbox will enforce, then don't enforce it yet.
Get it right and generated modules become "run the same API behind a wall," not a rewrite. Curated
modules may use a richer internal API; the **public** surface stays narrow so the seam is real.

### Manifest (declarative — what the agent/LLM reads to conjure one)

- `id`, `version`
- `tier` (A/B/C — declares sync needs)
- `config schema` (typed params: fireflies count/color, milkdrop track) — **this is the constrained
  surface the LLM gets, designed from day one**
- `anchor` + `fallback` (see anchor classes)
- `requires` / `prefers` (geometry needs — see surfaceless worlds)
- `capabilities` requested (`bus`, `clock`, `sceneGeometry`, `assets`, `audio`, …) — explicit,
  enforceable later
- `claims` (exclusive resources it owns — `skybox`, `lighting`, `gravity`, `audioMaster`)
- `singleton?` — one instance (solar system) vs multi-instance (many ripple pictures). Most are
  singletons but it is **not a rule**; the framework supports multi-instance and arbitrates `claims`.
- disposal is **required, not declared** — every module fully tears down.

### Capability object (injected at runtime — the *only* things a module touches)

- lifecycle: `init(config, capabilities)` → `update(dt, clock)` → `onEvent(evt)` → `dispose()`
- injected: `bus` (scoped emit/subscribe), `clock` (shared time), `attach` (a scene node to mount
  into), `assets` (read-only library query), `sceneGeometry` (see below), `emitOut` (downsampled LLM
  feed).

`dispose()` must release geometry, textures, listeners, RAF handles, and bus subscriptions. On
mobile-class Quest hardware, an unload that leaks GPU memory **crashes the headset** over a long
session — disposal is a hard part of the contract, not a nicety.

## Anchor classes

Modules are conjured **at a location using the existing placement vocabulary**, not a bespoke system.
Four classes:

- **surface** — attached to a captured plane, oriented by normal (`place_image` lineage; walls-out /
  objects-in).
- **free/point** — placed at a world point, unattached (billboard lineage). **Universal fallback.**
- **volume** — fills a bounded region; uses geometry as *bounds*, not attachment (fireflies). Takes a
  bound spec that **defaults to room extents when available, explicit dimensions otherwise** — never
  assumes "the room" exists.
- **ambient/scene** — *no location, global*: skybox, lighting, fog, weather, gravity, audio bed.
  Environment/backdrop lives here (see §Environment).

The manifest's `anchor` picks which placement primitive the module rides on, so "conjure fireflies
over there" is the same gesture as placing an image.

### Room-geometry awareness ≠ anchoring

Physical plausibility is a **capability, decoupled from anchoring**. Fireflies (volume) want the walls
as *colliders* so they don't clip through; a bouncing ball (free) wants the floor to bounce off. That's
the `sceneGeometry` capability — read-only colliders — requested independently of where the module is
anchored. A module can be volume-anchored and ignore geometry (ambient sparkles), or free-anchored and
respect it (the ball).

## Surfaceless worlds (outdoor VR)

Conjure has existing **outdoor worlds** (meadow, futuristic-city, …) with **no captured surfaces**. The
plan treats captured geometry as *optional context*, never assumed.

- **A frame of reference always exists**, even in a surfaceless meadow: user pose, world origin, an
  up-vector, and a ground reference (floor height / y=0). That quartet is the universal substrate every
  module can rely on; anything richer is a bonus a module must do without.
- **One geometry provider, two backends.** Modules never ask "is this a real room?" They ask
  `sceneGeometry`, which answers uniformly: passthrough room → captured planes; outdoor VR world →
  whatever the world authored (usually a ground plane + play-area bound, no walls). Modules requesting
  it **must handle the sparse/null case**: fireflies with no walls roam a configured radius; the ball
  with no captured floor bounces off the world's ground plane.
- **The skybox is not a surface.** Nothing anchors *to* it; an ambient module *owns* it. In an outdoor
  world the backdrop is the world itself.
- **Per-anchor behavior when surfaces are missing:**
  - *free/point* — works everywhere. Universal fallback.
  - *surface* — needs a plane. Outdoors: the world may offer a synthetic anchor, or the conjure **falls
    back to free placement in front of the user**, or is refused with a clear reason — per the module's
    declared `fallback`. `requires: surface` with no fallback → refused: *"needs a wall; you're
    outdoors."*
  - *volume* — uses its configured box/radius bound; never assumes "the room."
  - *ambient/scene* — unaffected.
- **Resolve or refuse at conjure-time, not render-time.** The world advertises a **geometry profile**
  (`surfaces: none|captured|authored`, `ground: yes/no`, `bound: …`). `conjure_module` checks the
  module's `requires` against it and either places, applies the fallback, or refuses with a legible
  message — before anything mounts. This is exactly how `place_image` already degrades (wall placement
  becomes free-standing/billboard outdoors); modules reuse that resolution.

## Real-world occlusion (depth pre-pass)

**Status:** SHIPPED — `off` / `hands` / `hands-solid` (branch `occlusion-depth`). Plumbing (`--occlusion`
→ config → `window.CONJURE_OCCLUSION` → `?occlusion=` URL override) plus a filled, depth-only **hand
mesh** per tracked hand (finger ribbons + palm fan from `frame.getJointPose`, one BufferGeometry, added
to the scene graph so it survives A-Frame's depth clear). `hands-solid` draws the same mesh as opaque
white (a white-glove avatar that also occludes). Verified on Quest 3. **`full` (environment depth) is
shelved — see below.**

### Shelved: `full` (environment-depth occlusion) — learnings

`full` would occlude *everything* real and dynamic (furniture, people, objects you hold), not just
hands, via the Quest depth sensor. Shelved after on-device investigation:

- **The Quest does provide it:** requesting WebXR depth-sensing yields `depthUsage=gpu-optimized`,
  `depthDataFormat=unsigned-short`. So it's genuinely possible on the hardware.
- **three r169's built-in occlusion doesn't consume it:** `renderer.xr.hasDepthSensing()` stayed
  `false` — three's depth mesh expects a `luminance-alpha` `sampler2DArray`, and the Quest's
  `unsigned-short` per-view delivery isn't accepted, so three never builds the texture / renders the
  mesh. Confirms we can't ride three's built-in path here.
- **Also, ordering:** three renders its depth mesh *before* A-Frame's scene render, which clears depth
  (`autoClearDepth=true`) — so even if three had a texture, it'd be wiped without further intervention.
- **To implement it ourselves:** create our own `XRWebGLBinding(session, gl)`; each frame, per eye view,
  `getDepthInformation(view)` → `unsigned-short` texture + `rawValueToMeters` + `normDepthBufferFromNormView`;
  a custom fullscreen occluder shader converts raw→meters→clip-space depth and writes `gl_FragDepth`,
  added to the scene graph so it survives the clear (same trick as hands); handle both eye viewports +
  foveation.
- **Why shelved:** (1) it's device-specific WebGL that can't be unit-tested — every pass needs a headset
  round-trip; (2) environment depth is low-res + ~1 frame laggy, so edges are inherently blocky/
  shimmering (unlike the sharp hand mesh); (3) hands are already covered sharply by `hands`, so full's
  marginal value is only moving real things (people/pets/held objects).
- **Cheaper alternative recorded:** render the **`mesh-detection` captured room mesh** (walls + furniture)
  as depth-only occluders — sharp, stable, no depth sensor — but static-only (no hands/people/moving).
  Pairs well with `hands`. A better next step than full if static-furniture occlusion is the goal.

In AR passthrough the compositor draws passthrough, then our opaque virtual layer on top — with **no
knowledge of real-world depth**, so a virtual wall covers your real hands. The fix is a single **depth
pre-pass**, *not* per-material shader occlusion:

- Once per frame, per eye, a full-screen pass samples real-world depth, converts it to the pipeline's
  clip-space depth, and writes the **Z-buffer with color-write off**. The scene then renders normally
  with depth testing on.
- Virtual fragments behind a real surface fail the z-test → write no color → stay alpha 0 → the
  compositor fills them with passthrough. We seed **depth only**; the OS supplies real-world *color*
  for free (no need to paint passthrough ourselves).
- **One integration site, not per material.** Every material — including all future dynamic-content
  modules and transparent content — occludes for free via ordinary depth testing. **Modules never opt
  in; occlusion is global** and not a per-module capability.

**Tradeoff:** a hard z-write gives hard-edged occlusion, so the coarse, ~1-frame-stale Quest depth map
shows as blocky/shimmering silhouettes. Feathered (soft) edges would require the per-material path
we're deliberately avoiding — accept hard edges globally, add soft edges later only where they matter
(hands). The one real cost is a careful render-loop injection per eye matching three.js's projection +
depth encoding (reverse-Z / log-depth) and foveation/multiview.

### `occlusion` mode — `off | hands | full`

A launch flag / URL param (consistent with the existing `?stereodebug=` client toggles) selects what
the pre-pass writes — same mechanism throughout ("things that write depth but not color"); the mode
just picks the source:

- **off** — no pre-pass; virtual always over passthrough (today's behavior).
- **hands** — the pre-pass renders only the tracked **hand mesh** (depth-only). Sharp, cheap; hands
  occlude virtual content, nothing else does. Doesn't depend on the environment-depth map.
- **full** — the pre-pass writes the **environment-depth** texture (walls, furniture, people, and
  hands, coarsely). Optionally composite the hand mesh on top for sharp hands *within* full.

## Event bus

The bus is the real spine — more than the modules. Every event carries a **scope** so routing is
explicit:

- `local` — never leaves the client (my own pointer hover).
- `shared` — relayed to everyone in the session (the touch that spawns a ripple).
- `out` — a **downsampled/summarized** feed to the LLM (never raw drag frames — "user dragged the globe
  for 10s and let go facing Mars").

Event shape ≈ `{type, source: llm|user:<id>|server:<mod>|module:<id>, target: <modId>|broadcast,
payload, t}`. Ordering by `t` with a tiebreak; for tier A/B eventual consistency is fine. Events come
from the LLM, from users (pointing, selecting, dragging), and from future **server modules** (e.g. a
music player emitting volume/tempo/key/beat cues).

## Three tiers of reactivity (the answer to "the LLM can't do realtime")

- **Module loop** (frame-rate) — the module *is* the realtime agent.
- **Server rules/behaviors** (milliseconds) — a lightweight declarative rule engine: `when beat.kick →
  emit flash`. Simple reactivity needs no custom code *and* no LLM roundtrip.
- **LLM** (seconds, semantic) — the **choreographer**, not the dancer. Sets mood/parameters/goals,
  authors module configs, reacts to the summarized `out` feed, emits coarse events back.

### Music-viz specifically
Don't relay audio analysis from the server — latency wrecks beat-locking. The server broadcasts the
**transport** ("track X, play at shared-clock t0"); each client runs its own Web Audio `AnalyserNode`
locally, in sync (cause, not effect). The server music module *also* emits semantic cues (tempo, key,
major/minor) onto the bus for the LLM and for rule-driven modules that shouldn't do their own DSP.

## Exclusive resources

Skybox, lighting, gravity, and the audio master are **single-owner** — stronger than "singleton by
convention." The framework must arbitrate ownership: a module `claims` them in its manifest, and
conjuring a second claimant is detected **at conjure-time** (rejected, or ownership handed off), not at
render-time.

## Environment vs. worlds (resolved)

An immersive/outdoor **environment is world-level, not a module.** Conjure's existing worlds already
are backdrop replacements; dynamic *modules* are the conjured layer that runs **on top of** whichever
backdrop is active — additive in passthrough, additive in a VR world alike. This keeps modules from
reimplementing what worlds already do and keeps "one shared reality" clean: the world sets the stage,
modules are the live performers on it. Environment modules and the passthrough/room-capture view are
therefore **mutually-exclusive backdrops** — a property of *worlds*, not something a module toggles.

## Agent surface (tools)

Keep the LLM's surface tiny and identical to how it already places content:
- `conjure_module(id, config, anchor)` — mirrors `place_image`; load/unload is an owner-gated world
  mutation.
- `module_event(target, event)` — emit a coarse event at a running module ("make that one calmer").

Modules may also be declared in a world's **constructor** so a world boots with its dynamic layer.

## Curated-now / generated-later

`#1`: LLM- and user-generated modules are **in the plan, deferred** until the framework is mature with
several curated examples. The trust boundary (executing arbitrary JS/shaders that can hang the render
loop or crash the GPU) is real even under "no security — users are identity only." Design the manifest
+ capability object *now* as the eventual public/sandbox API; the LLM's `config schema` surface is the
first slice of that boundary. When generation lands, real code stays in a curated registry and the
generator only emits config + wiring against the constrained surface — never arbitrary shader source
into the render loop.

## Build order

1. **Shared clock** (step 0) — ✅ built; two-headset agreement confirmed via the fireflies swarm.
2. **Fireflies** (tier A) — ✅ built. `client/dynamic-modules.js` (procedural from `ConjureClock`+seed,
   full disposal), launched as an entity carrying the component (config-in-snapshot, shared, persisted).
   Agent-launch: `conjure_module` / `dismiss_module` MCP tools → server `/module` + `/module/dismiss`
   with a `DYNAMIC_MODULES` registry (the manifest seed: component/tier/anchor/singleton). Confirmed
   in sync across two users.
3. **Event bus + water ripple** (tier B) — ✅ first cut (branch `water-ripple`, needs on-device iteration).
   `window.ConjureBus.emitShared/on` (client) + server `module_event` ws relay to peers = the shared-event
   spine. `client/water.js`: GPU ping-pong wave-equation sim (height+velocity, reflecting/clamped walls,
   Courant-clamped), refraction display (∇h → UV + specular), touch/drag via BOTH fingertip-proximity
   (hand tracking) and controller ray+trigger. Per the user's call, tier-B is **NOT synchronized** — only
   touch events broadcast; each headset evolves its own sim. `conjure_module("water", {image, …})`.
   Original tier-B goal below —
   proves shared events, XR touch raycast, multi-user input
   attribution, deterministic short-lived sim. If ripple-from-anyone's-touch looks right on two
   headsets, the architecture holds.
4. **Everything else is a module** — milkdrop (+ server music module + rule layer), photo library
   (bridges to `library.db` / the asset catalog), solar system, spirographs, model articulations.
5. **Later:** LLM/user-generated modules behind the now-designed boundary.

## Practical limitations (flagged)

- **Quest perf budget & disposal** — the one that bites hardest. N particle systems + shaders on
  mobile-class GPU drop frames (cf. the existing walking-microstutter reprojection finding); leaked
  GPU memory on unload crashes the headset. Needs an active-module quota, LOD, and hard disposal.
- **JS determinism** — imperfect; fixed timestep + seeded PRNGs; cosmetic/short-lived only.
- **Clock sync** — must exist and be smoothed; it's the enabler for all of A/B.
- **Trust boundary** — arbitrary module code; curated-only until the boundary is proven, generated
  behind a constrained surface after.
- **XR interaction plumbing** — raycast → module hit-test → event, via hands/controllers, with
  multi-user attribution (events tagged by emitter).

## Non-goals / preserve

- **No per-viewer modules** — a loaded module runs for everyone (§invariant). Per-client *presentation*
  differences are fine; per-client *presence/state* is not.
- **No new authority model** — load/unload reuses `_owner_only_writes`; visitors inhabit, don't conjure.
- **No new storage model** — modules are scene entities in the world snapshot.
- **Environment stays world-level** — not reimplemented as a module.
- **Occlusion is global** — the depth pre-pass (`occlusion` mode) handles real-world occlusion for all
  content at once; modules never sample depth or opt in.

## Things this buys beyond the wish-list

- **Modules reacting to each other** via the bus (fireflies swarm the kick drum; water tints to the
  music's key) — emergence for free once the bus exists.
- **A rule/trigger layer** so ~80% of reactivity needs no code and no LLM.
- **Record/replay** — because A/B are procedural + event-sourced, a session is deterministically
  replayable from `(seed, clock, event log)`; near-free debugging + "show me what happened."
- **Asset-catalog bridge** — the photo-library module is a `library.db` query + thumbnail grid +
  selection events; dynamic content meeting the existing asset system.
