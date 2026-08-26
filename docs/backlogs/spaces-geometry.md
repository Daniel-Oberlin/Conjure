# Spaces ↔ geometry — backlog

Unfinished work, future directions, and known problems for capture, registration, placement and
stability. The current state is [`docs/specs/spaces-geometry.md`](../specs/spaces-geometry.md); the
reasoning behind rejected alternatives is [`docs/decisions.md`](../decisions.md). The debugging
campaigns that produced most of these — including what has been **ruled out** — are in
[`docs/investigations/`](../investigations/).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems

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

### The MARKER probe was never built

Every correlation between what the user *saw* and what the data *recorded* has been inferred from
counts. The missing tool is a **controller-button marker**: press the instant you see a pop, log
`MARK t=… lastDt=… lastJerk=… rebuilds=…` plus the recent ring. That gives frame-exact
perception↔data correspondence. **Build this first if the investigation resumes.**

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
