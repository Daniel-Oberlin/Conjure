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
