# Spaces ↔ geometry — backlog

Unfinished work, future directions, and known problems for capture, registration, placement and
stability. The current state is [`docs/specs/spaces-geometry.md`](../specs/spaces-geometry.md); the
reasoning behind rejected alternatives is [`docs/decisions.md`](../decisions.md). The debugging
campaigns that produced most of these — including what has been **ruled out** — are in
[`docs/investigations/`](../investigations/).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems

### A surface drops out and returns uncoloured

**Status:** open · noticed 2026-08-30, intermittently, since the smoothing work landed

A real surface occasionally vanishes and comes back **without its styling**. The mechanism is fully traced;
what is *not* known is which of three causes fires.

Three consecutive captures without the surface trip two independent debounces: `_localAbsent` removes the
DOM element (`conjure-client.js:1528`) and `_absent` drops the id from `_known` (`:2378`) → POST → the
server emits `{"op": "remove"}` (`server.py:3305`), deleting the seed entity **and its material**.
`surfaceStyles` is rebuilt from the snapshot's real entities (`:765`), so the colour is gone for good and
the surface redraws at the default material whenever it returns.

Three different causes produce that identical appearance, and today's log cannot tell them apart:

| Cause | What actually happened | Where the fix would land |
|---|---|---|
| **device miss** | the Quest never emitted the plane in `detectedPlanes` for 3+ captures | the debounce count — or nothing, if it's a platform floor |
| **matcher miss** | the plane *was* detected, but `matchWall` / `matchInset` / `matchRef` rejected its `_ref` entry → a fresh id minted | the identity tolerances (`--wall-perp-tol`, `--wall-yaw-tol`, `--wall-overlap-slop`) |
| **style orphan** | identity held — `_ref` is never pruned, so `matchWall` re-inherited the *same* id — but the seed entity had already been pruned, so the colour is gone anyway | protect styling from the prune, the way `anchored` protects geometry |

The third is the trap, and the reason this needs instrumenting rather than guessing: it is **not an identity
bug at all**, and chasing it as one costs a week. `_ref` outliving the seed is deliberate (§6.1 — identity
resolves against the local constellation, not the lagging seed), so the same id genuinely can come back
stripped of its styling with the matcher working perfectly.

Instrumented by [the geometry event log](#instrumentation--the-geometry-event-log) below.

**Field status 2026-08-31: not yet reproduced.** Two sessions totalling ~25 minutes, 59 surfaces, produced
**zero** `churn.*` events — no miss, no mint, no prune, no lost styling. The reference held 59/59 throughout
(`cov=59/59 inl=59/59`), including across 12 `track.reset` events. So the identity side is quiet and the
probe is unexercised; nothing is confirmed about which of the three causes fires, because none of them did.
Worth noting the sessions were short and stationary-ish — the symptom is occasional by report.

### One room's floor sits 4–6 inches high — **diagnosed, device-side**

**Status:** cause established 2026-08-31. The campaign, its measurements and the three hypotheses it killed
are in [`investigations/raised-floor.md`](../investigations/raised-floor.md) — read that before proposing
anything here.

In one line: the Quest's stored room entity for the **bedroom** is anchored ~104 mm high, and every plane in
that room rides with it. Proven by two known-equal surface pairs (`floor_32` = `floor_8`, one continuous
wooden floor; `ceiling_13` = `ceiling_25`) reading +104 mm and +103 mm — agreeing within 1 mm, so the room
moves as a rigid unit. The tracking frame is sound and registration was flawless throughout. **Nothing to
fix in our code.**

**A re-scan does not clear it** (tried 2026-08-31), so this is a standing fault rather than a one-off — and
objects placed on that floor do visibly rise with it, as predicted. Hence the render-side mitigation:
`--fix-floating-rooms` (spec §10.4), off by default, which closed the two known-equal floors from 104 mm to
12 mm on the reference capture.

**Open — on-device verification of the correction.** It has never run in a headset. Enter with
`--fix-floating-rooms 0.06` and check three things: the bedroom floor lands under your feet, objects there
sit flat, and `level.correct` appears once in the log rather than repeatedly. The residual 12 mm is the
seed's own error and is expected to stay.


### Ground truth for this space — physical constraints worth keeping

Established 2026-08-31 by the owner, and worth writing down because every future height reading is read
against it:

- `floor_32` (bedroom) and `floor_8` (living room) are **one continuous wooden floor** — equal, always.
- `ceiling_13` (bedroom) and `ceiling_25` (living room) are the **same physical height**.
- `floor_10` (kitchen) is **+25 mm** above the other two; `ceiling_21` (kitchen) is genuinely **sunken**.

Two consequences:

- **The persisted seed has the kitchen floor wrong by ~46 mm** — it stores `floor_10` 21 mm *below* the
  living room, where physically it is 25 mm *above*. Sign error, small, separate from the bedroom fault, and
  present in the seed rather than the live capture (live gets it right to 4 mm). Nothing depends on it today.
- **These constraints would make the anomaly test exact.** `levelDeviation` infers "normal" statistically
  via the median, which can only ever say *something* moved relative to the rest. If the space record
  carried a few declared relationships, the same check becomes a flat assertion — *"`floor_32` and `floor_8`
  are one surface and they are 104 mm apart"* — with no median, no threshold, and no ambiguity about which
  room is the outlier. Proposed, not built.

### Walking micro-stutter — a platform limit, not our code

The residual "flick out and back" while walking is **dropped-frame positional reprojection during
translation**. WebXR gives rotation-only reprojection, so a dropped frame reprojects cleanly when you
turn your head and pops when you translate. This is diagnosed, not suspected:

- Tick self-time on a dropped frame: **0.2 ms of our JS in a 66 ms frame** — the stall is entirely
  outside our code.
- Sampled wall and content world positions stayed **flat** across every flick — our transforms never
  moved.
- `rebuilds=0` during a run where pops were seen; `--group-surface-relay off` made **no difference**.
- Rotation-only walking is clean; the effect is translation-only.
- Count-matched: one run had 23 drops, **6** coincident with a view-jerk >2 mm, and the user saw 5–10
  pops.

**The only clean symptom-fix** would be giving the compositor per-pixel depth or motion vectors so a
dropped frame reprojects translation correctly. Native Quest gets this via Application SpaceWarp
(`XR_FB_space_warp`); in WebXR it is very likely not exposed — default WebXR is colour-only, and the
`depth-sensing` we already request is real-world depth for occlusion, not reprojection. The only maybe
is the WebXR Layers API with a depth attachment, *if* Oculus Browser consumes it for reprojection.
**Undocumented and unverified — verify before promising it.**

### ~1 cm wall-hunting jitter — the target is noise

Distinct from the above. While walking, the group relay re-lays every wall to its **raw** (sensor-noisy)
pose whenever any one crosses the gate, so walls ease between values ~1 cm apart — below the gate
threshold. Slew smooths each hop, but since the *target* is noise the walls gently swim rather than pop.

Two candidate attacks, neither attempted:

1. **Denoise the raw wall pose** — a temporal EMA per surface, so the target is stable before the slew
   ever sees it.
2. **Solve content against the rendered (gated) poses** rather than the raw basis, so content inherits
   wall stability. Raised in-session and deferred because the walls themselves still chase raw noise —
   fixing (1) first makes (2) mostly unnecessary.

### The MARKER probe — built for geometry, still missing for jitter

Every correlation between what the user *saw* and what the data *recorded* has been inferred from counts.
The tool that fixes that is a **controller-button marker**: press the instant you see it, and the log gets
a dated record of what the system believed at that moment.

**Built 2026-08-30 for the geometry side** (`mark` binding, default **B**; see
[`specs/spaces-geometry.md` §10.3](../specs/spaces-geometry.md)) — it dumps the height census, registration
state, residual summary and the recent churn ring, stamped with the controller's own height, which for the
raised floor is the only ground truth that exists.

**Still missing for the jitter campaign**, which needs a different payload: `lastDt`, `lastJerk`, `rebuilds`
and the frame ring, at frame precision rather than capture precision. The button and the transport are now
there, so this is a payload, not a new mechanism. Build it if that investigation resumes.

### GC is not testable on Quest

`performance.memory` is frozen and quantized on Oculus Browser — constant heap, all deltas zero. So the
"GC pauses cause the drops" theory is plausible (the capture body allocates heavily every 0.5 s) but
**unconfirmable by heap sampling**. The `heapKB` / `heap` probe fields are effectively dead there.

The one lever purely in our hands: an **allocation-churn pass** — pool the per-capture `THREE`
temporaries and re-measure the **drop count** (was 23/run) to see whether GC-induced drops fall. Not
attempted.

### The `--foveation` default is undecided

Raising 0 → 0.3 → 0.5 cut the dropped-frame rate **monotonically**, and sustained GPU-bound bursts were
gone by 0.5. It did not eliminate residual isolated drops (those are external). `client/index.html`
ships `foveationLevel: 0` deliberately — full resolution kills moiré on the grid and surface edges — and
the knob overrides it at runtime.

The default is a **human visual call** (smoothness vs peripheral sharpness), not a data question: 0.5 is
meaningfully smoother, 0.3 a balance, 0 sharpest.

---

## Instrumentation — the geometry event log

**Status:** **shipped 2026-08-30; diagnosed the floor fault on its first day in the field, 2026-08-31.**
The design and event reference live in [`specs/spaces-geometry.md` §10](../specs/spaces-geometry.md) — this
entry keeps only what is *not* done and what the field changed.

Always-on and change-gated, to `temp/geometry-<date>.jsonl`, rotated daily and pruned past
`--geometry-log-days` (21). A settled room emits nothing. Both halves shipped together — churn and heights —
since a device-side map re-fit would produce both symptoms and the value is in reading them on one timeline.

### What it did on day one

- **`level.anomaly` fired unprompted at session entry**, before any button press, naming `real_floor_32`
  (+83 mm) and `real_ceiling_13` (+77 mm) — the right two surfaces, the right room, with nobody looking for
  it. That was the whole design bet and it paid immediately.
- **`dev` was corroborated by physical measurement.** It computed +78 mm for `floor_32`; the controller put
  the same floor ~80 mm high. Same order, same sign, agreeing **to within the ~2 cm gesture noise** — not
  more finely than that, since the grip bias was inferred from the same presses. The exact figure comes from
  the known-equal surface pair (104 mm), which needs no instrument at all.
- **The marker's characteristics, measured:** grip bias ~3–4 cm, gesture repeatability ~1 cm, and 1 mm
  hysteresis returning to the same spot after walking two rooms away. Comfortably sharp enough for a 10 cm
  signal.
- **The `err` sign flip at a room boundary is the sharpest single reading in the log** — it says which room
  is wrong, which no internal probe can. Worth reaching for first next time.

### A structural change during a displaced session corrupted the seed — **fixed 2026-08-31**

The gate and the payload were decoupled: any structural trigger caused `_surface_update_set` to write the
**whole record**, so an opening-count change — a legitimate edit to `holes` — rewrote the surface's
*position* with whatever frame that capture happened to be in.

Observed: at 07:37 a relocalization put the space ~93 mm low, a door appeared on two walls, and the seed
absorbed the offset into `real_wall_82`, `real_wall_37` and `real_ceiling_13` while its other 55 surfaces
kept the old frame. The reference was left internally inconsistent — and the seed is the baseline the
floating-room detector, guest registration, recovery and the server's own plane queries all measure against.

The consequence was not theoretical. Replaying the detector against both versions of the seed: with the
corrupted `ceiling_13` the bedroom read coherent (3 mm) and was corrected; with its pre-write value it read
25 mm — past the 20 mm gate — and would not have been corrected at all. **A single corrupted surface
decided whether the correction ran.**

**Fixed:** `_surface_changes` now returns *every* aspect that changed and `_surface_update_set` writes only
those. `extent` still carries `position` — a rectangle's size and centre are one measurement (§9.1's
matched-pair rule) — and the corner-relative inset anchors (`along`/`vertical`) ride the pose, since
refreshing them from an untrusted capture is how inset identity starts churning (§6.1).

**Seed repaired:** `real_ceiling_13` was reset to `real_ceiling_25`'s height (2.6634), the two being the
same physical ceiling. Reverting instead to its pre-write 2.677 was considered and rejected — that value is
itself of unknown provenance and makes the bedroom pair incoherent, whereas the ground-truth constraint
gives 12 mm coherence and a 91 mm correction. Backup alongside the space file.

**Still un-repaired, both pre-existing rather than from this fault:** `real_floor_32` and `real_floor_8` are
stored 9 mm apart though they are one continuous wooden floor (this is the residual the correction cannot
remove), and `real_floor_10` is stored 21 mm *below* the living-room floor where it is physically 25 mm
*above* — a ~46 mm sign error. Neither is urgent; both would need the same ground-truth treatment.

### Open — what still has to happen

- **A validity guard on the marker.** One press (07:01:25) read `grip_y = −0.173` where the same floor reads
  ~0.03 — the controller had drifted on IMU while out of camera view. It was caught only by cross-checking
  three other sources, which will not always be possible. The tell was in the record: head-to-controller
  distance was **954 mm** against 655–814 mm on the good presses, and the controller sat *below* the
  rendered floor. Either reject a press whose grip is implausibly far below the local floor, or log a
  confidence field, so a bad reading announces itself instead of being argued about.
- **On-device cost A/B — still not run.** The estimate is sub-0.1 ms on a capture that currently runs ~5 ms,
  and exactly 0 ms on non-capture frames: every probe is in the capture body (0.5 Hz) or on a transition,
  events are batched rather than one fetch per line, and the only per-frame addition is a rising-edge check
  on a pointer list `controller-beams` already builds. **That is an estimate, not a measurement.** Run
  `--debug-jitter` before and after and hold the spec's baseline (31/33 captures ≤6 ms, no per-capture
  drop). Until then the number in §10 is a claim.
- **The churn half is still unexercised.** Zero `churn.*` events across both field sessions, so the
  device-vs-matcher discriminator has never actually run on a real miss. It is tested but unproven.
- **A `track.reset` burst nobody has explained.** Twelve between 07:09 and 07:24 on 2026-08-31 — including
  **three inside one second** at 07:11:09, and pairs at 07:11:47, 07:17:21 and 07:24. Each is a recenter or
  a guardian re-entry, and each re-registers. Walking between three rooms across a boundary drawn round one
  of them explains the count but not the same-second triples, which look like a jitter in the event rather
  than three crossings. Every one is an opportunity for identity churn, so it is worth knowing which it is
  before the churn half is trusted.

### What the build changed about the plan

- **Wall bottoms do not come from `polyY`.** The plan said to reuse Pass A's `polyY` as a wall's world
  bottom. Wrong: a WebXR plane's polygon is in the plane's own X-Z frame, so `pt.y` is ~0 for every point —
  `polyY` is not a height at all. `heightCensus` derives the bottom from `_lp.y − extent[1]/2` instead.
- **Candidate ranking had to be plane-relative.** The first cut of `explainNoMatch` ranked candidates by
  centroid distance, which is exactly what `matchWall` refuses to do — a wall's centroid slides metres along
  the wall between captures, so a wall seen from the other end would have been reported as "the device never
  emitted it", the most misleading answer available. Caught by a unit test; it now ranks by how far through
  matchWall's gates each candidate gets.
- **No separate baseline store was needed.** Registration solves yaw about gravity plus x/z and never
  touches y, so height *differences* are frame-invariant and the existing seed is a valid baseline directly.
  The plan assumed a new persisted scalar per space.
- **`styled: true` would have been constant.** The obvious check — does the pruned entity have a material —
  is true for every real surface, because `_surface_entity` creates one from `_default_surface_material`.
  The field would have read `true` on every line and answered nothing at the moment it mattered. It now
  compares against that per-semantic default (a director edit, not a material) and records the actual
  colour; the client side reports the colour rather than a boolean for the same reason.
- **`bindings` had one default in two places.** Adding the `mark` action to the dataclass left
  `get_settings()` serving the old scheme; the literal is now a single `DEFAULT_BINDINGS` constant.
  Caught only by curling the running server — no test covered the injected value.
- **Tests write to their own log dir.** `conftest` now points `GEO_LOG_DIR` at `tmp_path` — `conjure.log`
  already suffers from a test run appending to the live dev log, and it would be worse here.

### Not done, deliberately

- **No per-frame plane re-pose.** That is [Pose smoothing Phase 2](#pose-smoothing-phase-2--per-frame-target-refresh),
  a different problem with its own trade-off.
- **No heap sampling** — dead on Oculus Browser, see [GC is not testable on Quest](#gc-is-not-testable-on-quest).
- **`--debug-registration` was not made the default.** ~6 fetches per capture; it would perturb what it
  measures, which is the mistake the jitter campaign already paid for once.

---

## Scaling — what the frame-budget work does not cover

The worker and the slice pump both apply one principle: cap per-frame cost, absorb load as latency,
never as a dropped frame. They cover two of the four axes that grow with scene size.

| Axis | State |
|---|---|
| **Solve cost** (`register`) | ✅ off-thread, decoupled from frame rate |
| **Geometry re-triangulation** | ✅ sliced, capped per frame regardless of surface count |
| **Element creation** | ❌ **not sliced** — `ensureEl` builds N A-Frame entities inline and grows linearly with space size. This is the *nearer* bottleneck for large spaces, and slicing the mesh rebuild does not help it |
| **`passB` / `matchRef`** | ❌ on the main thread in the reply continuation; grows with N and would need to migrate into the worker behind the same message boundary |

See [`docs/decisions.md`](../decisions.md) #16.

---

## Deferred by design

### Pose smoothing Phase 2 — per-frame target refresh

The ~0.5 Hz capture means the slew *target* is itself up to ~2 s stale. Slew smooths the transition but
cannot make tracking more current than the last capture. If fast walking exposes that lag, re-read
`frame.getPose(plane.planeSpace, refSpace)` **every frame** for surfaces already known, feeding a fresh
target that the slew low-passes — true live tracking rather than 2 s-stale.

Needs a plane→element map built at solve time. `XRPlane` instances are stable across frames while the
plane persists, so the map is reusable between solves and the heavy passes still run at capture cadence.
More moving parts (map lifetime across plane add/remove, entities with no live plane this frame) — hold
unless Phase 1 proves insufficient.

### Multi-client consensus seed

Instead of the seed being one headset's snapshot, the server could periodically fold **all connected
clients'** geometry into a better shared reference.

**The caveat is the whole difficulty:** the map is non-rigid, so this is *not* a rigid average — a rigid
mean would reintroduce exactly the one-sided shift the architecture exists to avoid. It must be a
**regional / soft** alignment: per-surface or per-region consensus of *planes*, weighted by how many
clients agree, used only to improve the **seed** the server solves against — **never** pushed back as
coordinates clients must render.

**Periodic seed refresh folds into this.** A single-client autosave-the-seed was considered and
deliberately not built; it is the degenerate case of the same problem and better solved once, properly.

### Missing-wall recovery

Recovery handles any non-wall surface a client failed to capture. **Walls are excluded** because a wall
is the *basis* of the anchor system — a plane, not a point — and its absence perturbs every nearest-wall
set, including the ones used to recover it. Genuinely harder, and deferred.

### Guest-proposes-a-surface

Only the owner authors the shared model. A guest that detects a surface the owner never captured cannot
contribute it. This interacts with space ownership — see [`backlogs/spaces.md`](./spaces.md).

### Avatar wall-set hysteresis

A moving head's "nearest 3 walls" set can flip as the source walks, jerking the solve as reference walls
swap. The design calls for keeping a wall in the set until another beats it by a margin (~0.3 m). Not
built — the over-specified solve smooths it enough for now. Add if avatars jitter.

### Content-move interpolation

A director repositioning an entity mid-session **snaps** it. Tweening would be nicer.

Explicitly **not** for captured surfaces: `detectedPlanes` is the persisted Space Setup, so a surface's
geometry is fixed for the session (a moved object needs a rescan), and within a session the only surface
pose changes are tracking corrections — which should snap or slew, not tween.

### Horizontal wall-end sealing

`sealWalls` closes the vertical case (wall top→ceiling, bottom→floor). The horizontal case — a wall
ending short of a corner that has **no second wall**, i.e. a doorway — is open, because `joinCorners`
needs two walls to define a corner. Closing those needs snapping wall ends to the **ceiling outline**,
which knows the room corner even where a doorway means no wall.

---

## Validation gaps

### Symmetric-room ambiguity

A symmetric space with equal opposite walls is genuinely ambiguous from a single vantage — the
registration vote can lock 180° off, the same class as the boundary flip. Distinguishing features
(differently-sized walls, doors, wall-art) resolve it in practice. There is no detection or warning for
the ambiguous case.

### Two live AR headsets

The matcher's guest tolerances (asymmetric size gate, coverage scoring, top-5 yaw peaks) are built and
unit-tested but have never run against a second real headset. Tracked in
[`backlogs/spaces.md`](./spaces.md) since the blocking work is co-location, not geometry.

### Corner-relative inset identity on a guest

Built and unit-tested; the two-headset case is the remaining real-world validation. Not a build gate —
the rare re-mint is not reproducible on demand.

### Headset regression for the space-sharing paths

"Space shared across worlds / styling per-world" and "void → empty capture" shipped covered by the unit
and JS suites but were never verified on-device.

---

## Harvested from the old flat `docs/backlog.md` (2026-08-26)

*Items filed against this subsystem before the per-area backlogs existed. Status lines
and dates are as originally written; none has been re-verified against today's code.*

## Void/outdoor worlds — canonical-frame refinements (core is shipped)

**Status:** open (refinements) · noted 2026-07-01 · **core shipped same day**

**Shipped:** outdoor/void worlds (`new_world(outdoor=True)` → `environment.space == "<void>"`) are live —
no room geometry, skybox + objects, geolocation won't yank them into a physical space. In AR, `room-capture`
derives the frame on the fly with `RoomSnap.canonicalFrame` (gravity-up + wall-grid axis + largest-wall
forward + centroid origin), never captures/posts, and `#world-root` + the skybox ride that frame → the same
physical room canonicalizes to the same orientation each visit (invariance unit-tested).

**Refinements left:**
1. **Symmetric-room ambiguity (inherent):** no unique largest wall ⇒ no unique canonical orientation
   (same 180° flip as `register()`). Low-stakes for a void world (only the skybox yaw moves, nothing pinned
   to real walls), but worth a tiebreaker (a door/opening, an L-shape corner) when one exists.
2. **Partial-capture stability:** a sparse capture (few walls) can pick a different frame than a full one.
   Prefer a fuller view (weight by covered wall area / require ≥N walls before locking; hysteresis so it
   doesn't hop once locked).
3. **Optional space tie:** let an outdoor world *optionally* bind to a stored space (robust registration)
   instead of canonicalizing — for rooms you revisit a lot and want rock-solid.
4. **Immersion polish:** a void world currently shows whatever skybox is set (or the void color until one
   is). Consider a sensible default / an explicit "outdoor" immersion that always occludes passthrough.

## `view_relative` can't tell you're looking at a placed OBJECT (only room surfaces)

**Status:** open · noted 2026-07-01 (diagnosed from a live session — "the LLM couldn't tell I was
looking at the tree")

**Symptom:** "what am I looking at / what model am I looking at" reliably names walls/doors but misses
placed models. In one session the director could tell the user was looking at the **dog** and the
**shell** but NOT the **tree** — same tool, opposite results.

**Diagnosis (two-part):**
1. **`view_relative` never ray-tests objects — only real surfaces.** `surface = _ray_surface(origin, vec)`
   skips anything without `meta.real`, so the "what you're looking at" result can only ever be a
   wall/door/floor. Placed models appear ONLY via `nearby = _nearby_entities(point, 1.5)` — things within
   1.5 m of a SINGLE probe point (`origin + forward·distance`, where the director guesses `distance`).
2. **`nearby` measures distance to the raw `transform.position` = the model's ORIGIN**, which is only a
   good proxy for "where the object is" when the origin sits at the visible object. It worked for the dog
   (`[1.5, 0.20, -2]`) and shell (`[0, 0, -2]`) — their origins are right where they visually are, ~2 m
   ahead near gaze level, so the probe sphere caught them. It failed for the tree (`[0.08, 5.01, 2.8]`,
   scale 4): that's the tree's GLB **origin ~5 m up** (the tree visually stands on the floor — NOT a
   placement bug, confirmed with the user), so the eye-level probe point is never within 1.5 m of it. The
   director then fell back to eyeballing coords — unreliable.

**Proposed fix:** replace the origin-point-sphere with a **gaze-ray vs. each object's world bounding box**
(position + bbox × scale) test; the nearest of {surface hit, object hit} is "what you're looking at".
Finds an object by its BODY regardless of where the GLB pivot sits, and along the true 3-D ray (so a tree
you tilt up to see is hit). Add an `object` field (id, title, distance) alongside `surface`.

**Prerequisite (why it's not a one-liner):** placed entities **don't currently store bbox** —
`meta.bbox_min`/`bbox_max` are `None` on the placed models (the catalog has them; `_model_entity_op`
takes them but doesn't write them onto the entity). So first thread the model extents onto the placed
entity (from the catalog at place time, or compute the world AABB client-side), then ray-test against it.

**Also noticed (separate):** the Beagle entity has `scale=0.00` — a degenerate/near-zero scale worth a
look on its own (may be a normalize/scale bug at placement).

**Open decision:** ray-vs-AABB (needs oriented handling) vs. ray-vs-bounding-sphere (simpler, looser);
and whether "looking at" should prefer the nearest hit or the smallest angular offset from gaze center.

## Multi-observer room fusion — refine the shared model from every headset (server-side)

**Status:** future feature · noted 2026-06-30 (deferred while building register-only guests, co-location §5)

**Idea:** today the room geometry has a single writer — the authority (space owner) captures + posts; a
guest **localizes against a frozen copy** of that geometry and never contributes (register-only — see
`specs/worlds-surfaces.md` §8b). That's correct for co-location: the shared `_ref` constellation *defines* the shared
frame, so a guest mutating it locally would only desync (its `/space/capture` posts are 403'd, so the change never
reaches the authority) and feed a drift loop. But a guest is *also* observing the same real room, so its
observations could legitimately **improve** the one model (better extents, corrected drift, "the room
changed since capture").

**Why it must be server-side (not local `_ref` mutation):** to stay co-located there must remain exactly
**one** authoritative model, **one** frame, and **one** id-owner. So fusion flows through the single
writer: guest sends observations → **server** fuses them into the one model → re-broadcasts → *both*
headsets re-seed `_ref` from the updated geometry. Local mutation isn't a cheap version of this — it's
silent divergence + feedback drift (the exact bug register-only fixes).

**Sketch:** a guest posts its registered observations to a NON-authoritative endpoint (e.g.
`/space/observe`, not `/space/capture`); the server fuses (weighted update of surface poses/extents,
conservative mint of genuinely-new surfaces) into the authoritative doc and broadcasts. Needs:
confidence weighting,
guard against a mis-registered guest corrupting the model, and the authority's right to override.

**Open decisions:** trust model (does a guest need the owner's consent to refine?); fuse continuously vs.
on explicit "rescan together"; how to reconcile a genuinely *changed* room (furniture moved) vs. noise.
Defer until register-only co-location is solid and the need is real.

## Models placed "facing me" come out 180° backwards

**Status:** open · noticed 2026-06-25 during live director testing · **sign needs Quest confirm**

**Symptom:** "lay out models of people in a circle around me, facing me" placed the circle correctly
but rotated every figure 180° so they faced *away*. Consistent 180° (not random per-model) ⇒ a single
convention error, not noise.

**Cause:** `place_asset`/`place_cached_asset` take an LLM-computed `rotation` (server.py:473), so the
director freehand-computes the yaw to face center — and the forward axis is inverted. The prompt says
"session forward is −Z," but a GLB character at rotation [0,0,0] faces +Z, so "rotate to face center"
flips sign and everyone turns their back. Images never hit this: `place_image` has **no rotation
param** — it plants the plane at a fixed server-side orientation, so the LLM does no facing trig.

**Proposed fix:** mirror the `on_surface` pattern (server computes orientation, LLM doesn't). Add a
`face` option to `place_asset`/`place_cached_asset` — `face_toward: [x,y,z]` or `face: "user"` — and
compute the yaw server-side so the model's forward points at the target. Then "facing me" needs zero
LLM trig and the convention lives in one function (a one-line flip to correct once verified on device).
Consistent with the prompt's existing "DON'T hand-compute a position or rotation" rule, which currently
only covers images-on-surfaces.

**Open decision:** the exact yaw **sign** is orientation math — confirm on a Quest before trusting it
(same caveat as the window-upside-down item).

## Rotated/placed objects clip through the floor

**Status:** open · noticed 2026-06-23 during live director testing

**Symptom:** "Turn the woman upside down" flipped the model but her **feet stayed on the floor and her
body went below ground**. More generally, rotating (or scaling) a floor-placed model can push part of
it through the floor.

**Cause:** the model's pivot is at its **base** (the GLB origin ≈ the feet, which is where we seat it
on the floor via `_normalize` in `conjure/server.py`). A rotation is applied about that pivot, so a
180° X-flip swings the body *down* through the floor while the feet stay at the pivot. Nothing
re-seats the object after the rotation.

**Proposed fix:** a client-side **`grounded` A-Frame component** (opt-in, flagged on objects that
auto-sit on the floor — `place_asset` / `place_cached_asset` with no explicit height). On a
transform change it computes the *rotated* model's world AABB (`THREE.Box3().setFromObject(mesh)`) and
offsets `position.y` so `box.min.y === 0` (floor). Notes:
- Ground on **rotation/scale**, but let **explicit height** placements win (don't yank "raise her 1 m"
  back to the floor).
- Guard the re-seat against re-triggering itself (one-shot flag).
- Floor = y=0 in the local-floor frame (rig at origin).
- Server-side alt (recompute the rotated AABB from the catalog bbox and emit a corrected position) is
  viable but bakes geometry math into the generic `update_entity` path — client component is cleaner.

**Open decision:** "flip upside down" → **stand on head** (re-seated on the floor, lean) vs. **hover
inverted** where she was (head down at original head height). Grounding gives the former.

---

---

## Harvested from the old `docs/known-issues.md` (2026-08-26)

*Field-observed problems and shelved work, moved here when the flat known-issues file was
retired. A parked branch is a property of the item, not a reason for a separate document.
Status lines are as originally written; not re-verified against today's code.*

## Shelved: wall-less-seed registration deadlock

**Status:** anticipated; observed **once** (during the inset-churn era), **not reproduced** since the churn
was fixed. Fix parked on branch **`deadlock-breaker`** (commit `f94dbd6`, branched off `main` @ `4027e9f`).
Abandoned on the mainline pending an actual recurrence.

**The problem.** `RoomSnap.register()` needs a wall basis (vertical plane pairs) to lock at all. If a room's
persisted *seed* ends up with **no walls**, it can never be registered against — and because a fresh
establish is gated on an **empty** `_ref` (`conjure-client.js`, the `canEstablish` line), an owner that has
already adopted such a seed is stranded in permanent `relocalizing`, with no path to rebuild the reference.

**How it happened (the once).** Before corner-relative inset identity was resolved against `_ref` (see
`docs/specs/spaces-geometry.md` §5.3), an inset-identity churn re-minted ids every capture; over a session the
churn pruned the architectural surfaces out of the seed until it decayed to *furniture-only* → wall-less →
deadlock. The churn fix removed that mechanism, so the decay — and thus the deadlock — no longer occurs on a
healthy seed. That's why this is shelved rather than merged: it guards a route that's currently unreachable.

**What the shelved fix does** (all keyed off `MIN_SEED_WALLS = 3`, matching `register`'s `ref<3` floor):
1. **Establish gate** — the owner only establishes a fresh reference from a capture that has ≥3 walls (never
   seed a wall-less room).
2. **Adopt gate (recovery)** — the owner only adopts a persisted seed that has ≥3 walls; otherwise it leaves
   `_ref` empty and establishes fresh, whose `replace`-POST then overwrites the bad seed.
3. **POST guard (prevention)** — never persist a wall-less surface set.
4. **Server backstop** — a wall-less `replace` post can't wipe a walled seed (`server._MIN_SEED_WALLS`);
   protects the persisted seed from any client. Unit-tested on the branch
   (`pytest -k wall_less` → `test_wall_less_replace_post_cannot_wipe_a_walled_seed`).

**Reproduce / verify (if it recurs).** With the server stopped, strip the walls from a persisted space:
`python3 -c "import json; f='.cache/spaces/<user>/<space>.json'; d=json.load(open(f)); d['surfaces']=[s for s in d['surfaces'] if (s.get('meta') or {}).get('semantic')!='wall']; json.dump(d, open(f,'w'))"`
then re-enter. **Without** the fix: hangs in `relocalizing` (`ref=<n> … dlt=0 … hold`). **With** it (the
branch): refuses the seed, establishes fresh, and the space file gets its walls back.

**To revive:** `git merge deadlock-breaker` (it's exactly `main` + the one commit).

---
