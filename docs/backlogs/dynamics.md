# Dynamics — backlog

Unfinished work, future directions, and known problems for dynamic modules. The current state is
[`docs/specs/dynamics.md`](../specs/dynamics.md); the reasoning behind rejected alternatives is
[`docs/decisions.md`](../decisions.md).

Items are grouped by what they block, roughly most-actionable first.

---

## `grab` modes — skybox and void world (**shipped** 2026-09-01)

**Now specified in [`specs/dynamics.md` §8b](../specs/dynamics.md).** Kept here because the reasoning and
the rejected alternatives are the expensive part, and the spec records only what the system does.

Two things changed during the build, both recorded in place below: `env.sky.scale` replaced a plain-sky
radius knob plus a grounded rebuild (one mechanism, exact, no rebuild), and every write became a **dotted
path** after a test caught that committing a yaw destroyed the panorama.

**First field test found a third** — the modes were unreachable, because nothing validated the mode *value*.
See the `config_schema` entry under **Contract gaps**; the fix is generic and lives there. What belongs
here is the design lesson: `_mode()` originally fell back to `object` for an unrecognised string, on the
reasoning that inert grip "would look identical to the module having failed to conjure". That reasoning was
wrong in a way worth remembering — **a silent fallback to the default is not a safe default for a mode
nobody can see.** It made a rejected value look like a working one. The client now names the bad value in
the indicator, and the raw value is logged beside the resolved one (printing only the resolved mode made a
switch to invalid `"sky"` log as `→ object`, which reads like a deliberate switch back).

**Second field test found three more**, all corrected in place above and in the spec:

| Reported | Cause | Fix |
|---|---|---|
| the sky vanished on release, returned on reload | the delta shared the `sky` key; a partial patch read as "no panorama" | deltas moved to `environment.frame` — see below |
| the stick didn't yaw the sky | it only worked *while gripping the floor*, by analogy with holding an object | stick works standalone in the mode; commits when it returns to neutral |
| placed models became unmovable in the new modes | `_boxPick` was restricted to object mode | `_boxPick` runs in every mode; the box is drawn, the handles are not |
| a void world's sky stayed put while its content turned | `frame.yaw`/`offset` were applied to `#world-root` only | `_pinSky` applies them too — one frame, which was the point |

Two of those four were **my design calls, not slips**, and both failed the same way — I treated something as
cosmetic when it was functional. The selection box looked like decoration and was actually the focus region.
Requiring a grip before the stick looked like consistency with object mode and was actually a step with
nothing behind it. Worth flagging for the next round of "this is just visual polish".

**Third field test, two more** — both from `grab` making a fixed constant into a live control, which is a
category of bug worth naming: *code that was correct because a value never changed*.

| Reported | Cause | Fix |
|---|---|---|
| a 0.2 m dome felt like a room with the ceiling above head height | `height` is where the HORIZON sits, not the dome's size; the apex is at `height + radius`, so 0.2 m ⇒ radius 3.75 m and a 3.95 m ceiling | the indicator reports **radius** (plus horizon), and the bounds moved onto radius so limits match the readout |
| models didn't disappear behind a shrunken dome | `depthWrite: false` — nothing can be occluded by a surface that never writes depth | dome writes depth; `polygonOffset` avoids z-fighting with content resting on the ground at `y = 0` |

Neither was wrong when written. `depthWrite: false` was fine while radius was pinned at 30 m and everything
was inside the dome; `height` was fine as an internal parameter nobody read back. Scale turned both into
faults. **When a constant becomes a control, its readers' assumptions become the bug list** — worth walking
deliberately next time rather than discovering it in the headset.

Extend `grab` from one behaviour to three **modes**. Today's behaviour becomes `object` mode and is
unchanged. Two new modes adjust things that have no entity to grab: the **skybox** (relative orientation
and scale) and a **void world's** content as a whole (relative orientation and horizontal position).

### Why this isn't just another gesture

Object mode has no competitor: nothing else writes a placed entity's transform, so `grab` can mutate
`object3D` freely and commit on release. Both new targets are **rewritten every capture**:

- `_pinSky` (`conjure-client.js:2040`) sets the sky's quaternion from `_Tmat⁻¹`, unconditionally.
- `_updateWorldFrame` (`conjure-client.js:1509`) parks `#world-root` on the canonical frame in a void world.

So neither mode can commit a transform. Both persist a **delta on a derived frame**, and the per-capture
writer composes the delta. A raw transform commit survives about two seconds. Most of the work therefore
lands in `conjure-client.js`, not `grab.js`; the gesture is the cheap half.

### The delta must not share a key with the panorama — twice learned

**First, in the build.** The implementation wrote `{"op": "env", "set": {"sky": {...}}}` — the whole `sky`
object. The delta shares that key with `sky.src`, so **turning the sky one degree threw the image away.**
Caught by a test. Fixed with **dotted paths**, which `_set_path` and the client's `nest` both supported. Same
failure the seed's write-gate exists to prevent ([`raised-floor`](../investigations/raised-floor.md)).

**Then, in the headset.** Dotted paths fixed the *document*, but the **broadcast patch** still carried
`{sky: {yaw, scale}}`. `applyEnv` reads an `env.sky` object as a complete description of the sky, so no
`src` meant no panorama: it tore the grounded dome down on every release. The sky vanished when you let go
and came back on reload — which is exactly the signature of stored-state-fine, live-state-wrong.

`applyEnv` was not wrong. **Sharing the key was**, because it forced every reader of `sky` to know it might
be holding a fragment. So the delta moved to `environment.frame.skyYaw`/`skyScale`, and the invariant became
structural: nothing writes under `sky`, so nothing reading `sky` can be misled.

The generalisation is a notch beyond "write the aspect that changed". Dotted paths make a *partial write*
safe; they do nothing about a **partial read**. If a value is going to arrive as a fragment, it needs a key
whose readers expect fragments — or a key of its own.

### Controls

Each mode grabs the thing that mode is about, and both new modes reuse the **grounded-object floor drag**
(`_update`'s `(groundY − origin.y)/dir.y` branch) rather than introducing a new pick target.

| | engage | drag radially | drag sideways | stick | commit |
|---|---|---|---|---|---|
| **object** | the object under the beam | — | — | `yaw`/`pitch`/`bank`, `reel` | `/manipulate` |
| **skybox** | floor point under the beam | scale about centre | yaw about centre | `yaw` | `env.sky` |
| **void** | floor point under the beam | translates | translates | `yaw` (viewer pivot) | `env.frame` |

Skybox mode decomposes the floor drag in **polar** coordinates about the sky's centre: radial → scale,
tangential → yaw, and a diagonal drag does both. Two consequences worth knowing:

- **You grab the floor, not the sky.** For a grounded skybox the floor *is* the dome's lower projection,
  so dragging a ground point outward literally stretches the dome — the gesture is physically honest.
- **Scale is inherently multiplicative** (`r_now / r_grab`), which is what makes a 500 m plain-sky radius
  reachable at all. A `reel`-based additive scale was rejected: at `reelSpeed` 1.5 m/s, walking 500 m
  down would take 5½ minutes. `reel` ends up unused in both new modes; leave it unbound.

Void mode has no polar split — it is a plain horizontal slide of `#world-root`, so **everything** under it
rides along: placed content, and avatars too (`:1259`), which is what keeps co-presence intact.

**Sensitivity is deliberately non-uniform.** Tangential yaw is ill-conditioned near the centre: a 0.2 m
hand movement at 0.2 m radius is ~45° of yaw, and ~2° at 5 m. A minimum engage radius was considered and
**rejected** — you learn the feel faster than you learn the rule, and grab-far-for-fine mirrors a real
turntable. What *is* required is an **epsilon floor on `r_grab` for the arithmetic only**: `r_now/r_grab`
at `r_grab ≈ 0` yields `Infinity`, and `grab.js` already carries two hard-won comments about a non-finite
value entering an accumulator and pinning it permanently (`_nearest`, `_boxPick`). Same hazard, same guard.

### One scale knob, not two (revised during the build)

The plan had a plain-sky `radius` and a grounded `height`, the latter rebuilding geometry on commit. Built
instead as a single **`env.sky.scale`** applied as a uniform `object3D` scale to whichever sky is live
(`pinSkyScale`). It is exact for the grounded dome by the warp argument below, needs no rebuild at all, and —
the reason that matters — the live preview and the committed value use *the same mechanism*, so they cannot
drift apart. Effective metres are `base × scale`, and the ergonomic bounds clamp that product.

### Grounded skybox scale is a uniform mesh scale, not a rebuild

Changing `height` alone would force a geometry rebuild (~33k verts at the default resolution), unusable
per-frame. But work through the warp in `grounded-skybox.js`: scaling every vertex by *k* gives sphere
radius `k·radius`, threshold `y1' = k·y1`, and the warp factor `f = −height/tmp.y` comes out **unchanged**.
So a uniform scale by *k* is exactly equivalent to rebuilding at `(k·height, k·radius)`, and
`mesh.position.y = height` scales correctly because it lives inside the entity's `object3D`.

**Therefore:** `#grounded-sky`'s `object3D.scale.setScalar(k)` — exact, zero rebuild, per-frame free.
Persist as `height` and `radius` both multiplied. Bounds 0.2–20 m on height.

### Plain-sky scale is a clipping control, not a size control

From the centre of the sky sphere the rendered view is **identical at any radius** — the texture subtends
the same angles. Scaling a plain `<a-sky>` does not change how big the panorama looks. It changes two
other things: **occlusion** (content beyond the radius is hidden behind the sky) and **parallax** (at
500 m, walking 2 m shifts the image 0.4% — a backdrop at infinity; at 5 m the same walk shifts it ~40% and
the panorama swims). Worth building, and honest labelling matters more than the feature.

`env.sky.radius` already exists for the grounded path; the plain path never reads it.

### Pin the sky's position to the frame origin

**A behaviour change to existing code, agreed 2026-09-01.** `<a-sky>` and `#grounded-sky` are *siblings*
of `#world-root` (`index.html:29–35`), so they receive none of its parking; `_pinSky` writes rotation only.
The sky is consequently the one thing in the scene not anchored to the space:

| | orientation | position |
|---|---|---|
| content (`#world-root`) | canonical / registration frame | canonical / registration frame |
| sky | same frame (via `_pinSky`) | **refSpace origin** — a per-session accident |

Two observable faults follow. Across sessions the dome's ground centre returns to wherever `local-floor`
landed while content returns to the same spot relative to the room. And mid-session, a Meta-button
recenter fires the refSpace `reset` listener (`:2243`): content re-derives and stays put, while the sky's
centre **slides out from under it**.

Fix: `_pinSky` decomposes position as well as quaternion from `_Tmat⁻¹`. This also collapses two different
"centres" into one, which is what makes "toward or away from centre" unambiguous for the scale gesture.

Inherits the partial-capture instability `canonicalFrame` already flags (see below) — strictly better than
a per-session-arbitrary centre, not perfect.

### Reference frames, for the record

`canonicalFrame` (`room-snap.js:390`) builds a void world's `_Tmat` in two parts. **Orientation:** an
area-weighted histogram of wall normals mod 90° gives the wall grid; θ snaps to whichever grid direction
is nearest the *largest* wall's outward normal, which is what makes it invariant to the session's arbitrary
tracking yaw. **Origin:** the arithmetic mean of the vertical walls' centre points, flattened with `c.y = 0`
(wall centres sit at mid-height; translating by their y would float the world ~1.2 m up).

So a void world's centre is *"the middle of your room at floor level, as weighted by how the Quest chose to
split your walls"* — a capture-weighted centroid, not a geometric one.

### Blocked in a captured room

No scale and no translation in a captured room — only skybox yaw. Scale there would shrink the sky's opaque
sphere, which `applyImmersion` deliberately keeps visible to occlude passthrough, until it intersects the
real walls; since it writes depth, that reads as a hard edge slicing across the room. Void translation is
meaningless there anyway: local-first forces `#world-root` to identity, so any move is reverted within a
capture and desynchronises content from the real walls in between. The radial component simply goes inert,
so the same gesture still yaws — graceful, not a special case. `isVoidWorld` is closure-private today and
needs exposing.

### Mode switching: voice and CLI, no controller binding

`/module` on a **singleton** reuses and reconfigures its one live instance (`server.py:4603`), so
`conjure_module(module="grab", config={"mode": "skybox"})` reconfigures the running entity in place —
A-Frame's `update()` fires and the module reacts. No new endpoint, no new binding, no controller fallback.
Modes **persist until changed**. These are rare, deliberate, setup-time acts, so voice latency is right;
a constantly-toggled control would not be.

Mode is world-shared config rather than per-user. Mostly moot, since manipulation is owner-only.

### The indicator is a safety mechanism, not decoration

A head-locked text entity with `overlay` set, same pattern as `_diagHud` / `#coloc-hud` (`:2049`), on its
own id and offset so both can show at once. **Always on** in skybox and void mode — not debug-gated —
and absent entirely in object mode, so normal use gains no clutter.

```
SKYBOX        yaw  30°   radius  500 m
SKYBOX ⏚      yaw  30°   height  1.6 m        (grounded)
SKYBOX        yaw  30°   — scale locked (room)
VOID          yaw  15°   offset  0.4, −1.2 m
```

Why it is load-bearing: the director sometimes reports success without calling the tool
([`director-confabulates-toolcalls`](../investigations/)). Here that failure is silent and expensive — you
grip expecting to turn the sky and instead fling a chair across the room. **The indicator appearing is the
confirmation the tool fired**, before you touch anything.

### Reset — "put the world back"

Voice and CLI, clearing the stored deltas so the derived frame stands alone: `env.sky.yaw` → 0 and
radius/height → defaults (500 m plain, 1.6 m grounded); `env.frame.yaw` → 0 and `env.frame.offset` → [0,0].

This is the **only** recovery path, by design, because two decisions above remove the alternatives. With no
minimum engage radius one twitch near the centre can apply a large yaw, and a symmetric panorama gives no
way to tell yaw 0 from yaw 180 by eye. And the void offset is **deliberately unbounded** — drag the world
40 m and all content, including the floor point you would need to grab to drag it back, is out of reach.

### Scope decisions

- **Hybrid, not exclusive.** An exact hit on an object still grabs the object in any mode; anything else
  engages the mode's target. Keeps object nudging available while positioning a world. Earlier reasoning
  against "grip on empty space" applied to the *default* state; inside a deliberately-chosen, visibly
  indicated mode it does not hold.
- **Suppress the selection box and corner handles** in the new modes — visual noise for a capability you
  are not there to use.
- **Void rotation is yaw-only.** Pitch or roll tilts the floor, and grounded content is re-solved upright
  every capture, so the two would fight visibly.
- **Void translation is horizontal-only.** Registration deliberately solves yaw plus x/z and **never y** —
  the reason height differences are frame-invariant in the geometry work. A void offset touching y would
  be the one place in the system breaking that rule, and it would fight the grounded floor solve.
- **Void yaw pivots about the viewer**, not `#world-root`'s origin, so stick-yaw and drag-translate stay
  independent instead of every nudge swinging you through an arc to be dragged back out.
- **The void offset is shared** (`env`), not per-client. Two people in the same physical room derive the
  same canonical frame, so content co-locates; a per-client yaw would break that — you would point at a
  chair the other person sees elsewhere.

### Prerequisite noted, not owned

**Void worlds bypass the load gate.** `loadGate` derives `expect` from the world doc's surfaces and a void
world has none, so `expect = 0`, the `expect >= minSeed` test fails, and it returns `"go"` immediately.
The canonical frame — hence the world centre this feature measures from and parks content on — can be
derived from however many walls happened to be loaded. That is the
[`surface-churn`](../investigations/surface-churn.md) fault landing on the world frame instead of on
identity, and `canonicalFrame` already names it: *"Partial-capture stability (a fuller view winning) is a
follow-up."* Not this feature's job to fix; this feature is the first thing that would visibly suffer.
Cross-referenced in [`spaces-geometry`](./spaces-geometry.md).

### Related

`claims` (under **Contract gaps**) lists `skybox` as an exclusive single-owner resource. Skybox mode makes
`grab` a skybox claimant, so the two should be designed together if `claims` is ever built.

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

**`config_schema` values: `enum` is now enforced, `type` and range are not.** A param may declare
`"enum": [...]`; `catalog_line` renders the choices (`mode(object|skybox|void)`) and `/module` refuses
anything outside them, naming the valid values so a caller can correct itself.

That much exists because of a field failure worth not repeating (2026-09-01): the director conjured `grab`
with `mode="sky"`, then `mode="frame"` — plausible guesses lifted from internal field names — was told
`ok: true` both times, and announced each mode as active while the headset silently stayed in `object`.
Three layers each declined to object: `config_schema` was documentation only, `/module` passed any value
through, and the client degraded quietly to its default. **A wrong guess was indistinguishable from
success**, which is the worst shape a validation gap can take with an LLM caller.

Still unenforced: `type` (a string where a number is declared passes), and any numeric range. Same fix
shape, same argument.

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
