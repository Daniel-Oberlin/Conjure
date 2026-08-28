# Spaces ↔ geometry — capture, identity, placement, stability

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md);
rejected alternatives and the reasoning behind consequential forks live in
[`docs/decisions.md`](../decisions.md).

This is the layer that decides **where a surface is** and **which surface it is** — plane capture,
registration, identity correspondence, plane-relative placement, and keeping all of it off the frame
budget. The space *record* is [`specs/spaces.md`](./spaces.md); how a world *styles* those surfaces is
[`specs/worlds-surfaces.md`](./worlds-surfaces.md).

---

## 1. The finding that shapes everything

The Quest's map of a multi-room space is **locally non-rigid**. Measured on-device with the residual
probe (`--debug-registration`): a return session's live geometry differs from the persisted snapshot by
**up to ~9 cm, concentrated in one region**, which **no single rigid transform can reconcile**.

That one fact rules out the obvious architecture. Broadcasting one headset's coordinates and asking
another to render them *guarantees* a one-sided shift — there is no transform that makes both right.

So the system is **local-first**:

> Share **identity and meaning**. Keep **coordinates local**. Define the placement of free content by
> **distances to locally-precise planes**, never by a shared coordinate.

Two supporting observations make it work:

- Wall **planes** (normal + perpendicular offset) and the **floor** are estimated far more stably across
  captures than wall **centres and extents**, which depend on how much of the wall you happened to see.
  So anchor to *planes*, not to poses.
- **Registration's real job is identity correspondence** — "which of my local surfaces *is* `wall_3`" —
  not coordinate sharing.

The consequence to internalise: **physical consistency is preserved; coordinate agreement is neither
achieved, needed, nor observable.** Two headsets in one space hold numbers that differ by centimetres
and both render content onto the same real wall.

---

## 2. Frames

| Frame | Scope | Stable? | Role |
|---|---|---|---|
| **F_real** | the physical space | ground truth | what every render must *visually* match (passthrough) |
| **F_track** | one headset, one session (WebXR `local-floor`) | no — drifts, can jump | **everything renders here**: live surfaces, on-surface content, all plane-relative content |
| **F_ref** | the seed's frame | — | **internal to registration only**; also the frame head poses are reported in. Nothing is *rendered* in F_ref |

**There is no shared render frame.** Registration still solves `T : F_track → F_ref` (`Tmat` in code),
but its useful output is the **id correspondence**. `#world-root` is held at **identity** for a captured
space (`conjure-client.js:1407`); content is F_track-native.

This is what buys drift immunity for free: when tracking jumps, the detected walls jump, and every
plane-relative anchor **re-solves against the jumped walls** — content stays glued to the walls, and so
to passthrough, with no frame-transform bookkeeping.

### 2.1 What `Tmat` is still used for

It is not dead, just demoted. In a captured space it drives **no render transform**. It is used for:

1. **Id correspondence** — the transform that lets `matchRef` pair live planes with reference surfaces.
2. **Head pose in F_ref** — `presenceTick` applies `T · camera_F_track` so the server's `gaze` /
   `view_relative` ("the wall in front of me") resolves in the shared frame.
3. **Skybox heading** — `_pinSky` applies `T⁻¹`'s rotation so the sky holds a consistent space-relative
   heading despite world-root being identity.
4. **The void/outdoor world**, which has no real surfaces to anchor to and therefore *does* park
   `#world-root` at `Tmat⁻¹`.

### 2.2 Coordinate and normal conventions

Three.js / A-Frame convention: right-handed, **+X right, +Y up, camera forward −Z**.

Plane conventions differ between capture and render, and the conversion is a real source of bugs:

| | Plane lies in | Normal is |
|---|---|---|
| **WebXR plane** (capture) | local X-Z | local **+Y** |
| **A-Frame `a-plane`** (render) | local X-Y | local **+Z** |

Capture applies an extra −90° X rotation and emits Euler in **YXZ** order. YXZ is not a preference —
A-Frame stores and applies rotations with YXZ semantics, so any quaternion→Euler conversion in this
pipeline must use it or the result is silently wrong.

**Normal semantics:** captured surface normals point **outward from the space**, so the interior-facing
direction is `−normal`. **Wall-art is the exception** — its live normal may arrive *inward*, roughly
180° from its host wall. Consumers compensate (`_face_room`, and `matchRef`'s coincident-flip fallback)
rather than the convention being normalised at ingest; see
[`backlogs/worlds-surfaces.md`](../backlogs/worlds-surfaces.md).

---

## 3. What is shared, what is local

| | Shared (server, owner-authored) | Local (each client) |
|---|---|---|
| Surface **set** — ids, semantics, including insets | ✅ | detects and renders each locally |
| Per-surface **styling** | ✅ by id | applied by id to local geometry |
| **On-surface** content (a photo on a wall) | ✅ by id, surface-relative | rides its **local** host surface |
| **Free** content, skybox, streamed avatars | ✅ as **plane-relative anchors** | **re-solved** against local planes |
| **Seed** — the reference constellation + anchors | ✅ (also the server's own solver geometry) | registers against it; recovers missing surfaces from it |
| Exact surface **pose / extent** | ❌ **never shared** | ✅ each client's own live estimate |

**The server is a solver too.** The seed *is* the server's local geometry. When it answers a pose-relative
question ("the wall I'm looking at", `view_relative`), it runs the *same* plane-relative solve against
the seed that a client runs against its live capture. One algorithm, two hosts — the server is just
another client whose walls happen to be the seed.

---

## 4. Registration — recovering identity by consensus

The hard part is a chicken-and-egg: you need the transform to know which detected plane is which
reference surface, but you need correspondences to find the transform. It is solved by **consensus, not
proximity**, so it works no matter how far the frame jumped.

The trust gate upstream guarantees a level floor, so gravity, pitch and roll are pinned and the only
unknowns are **yaw about up plus an x/z translation**:

1. **Yaw — normal-direction histogram.** For every same-semantic, similar-size *vertical* pair (current ↔
   reference), record the delta of their normal yaws. A global rotation θ shifts *every* true pair's
   normal by the same θ, so real pairs pile into one 6° bin while mismatches scatter. The modal peaks are
   the candidate yaws — position-free, so any offset is fine. Top-5 peaks are kept, because clutter can
   dilute the true one.
2. **Translation — grid vote.** For each candidate yaw, rotate the current positions and bin the implied
   `ref.pos − R·cur.pos` over same-size pairs into a 0.25 m grid. The densest cell is the consensus
   translation.
3. **Score — coverage.** Build the transform, project all planes, and count how many **distinct
   reference surfaces are covered** within `INLIER_M` (0.4 m) of a same-semantic reference.

**Acceptance** requires `cov ≥ MIN_COV` (4) **and** `cov ≥ MIN_COV_FRAC · |ref|` (0.3). Otherwise it
returns no lock and the client holds its last good frame.

Two properties follow from scoring coverage rather than fraction-of-detected:

- **Extra clutter cannot sink a lock.** A guest seeing furniture the owner never captured does not get
  penalised for it.
- **Fragmentation cannot double-count.** Two live planes covering one reference surface score once.

**A failed registration doubles as a "you are not in this space" signal.** A genuinely different space
cannot consistently cover ≥4 reference surfaces, which is exactly what `selectSpace` exploits for space
selection ([`specs/spaces.md §6`](./spaces.md)). The caveat worth stating: *different space* and *bad
tracking* both surface as the same no-lock.

**Known limit — symmetry.** A symmetric room with equal opposite walls is genuinely ambiguous from one
vantage; the vote can lock 180° off. Distinguishing features (differently-sized walls, doors, wall-art)
resolve it. This is the same class as the boundary-flip below.

### 4.1 The boundary flip

Measured on-device: **leaving the guardian boundary and returning relocalizes the whole tracking frame**
— a single rigid jump of ~167° yaw plus ~3 m translation, gravity preserved. A WebXR anchor flips with
it, so anchor-relative storage does not survive. A re-detected plane is a brand-new `XRPlane`, so
object-identity caching cannot help either.

Consensus registration handles it, because recovering yaw from the *shift in normal directions* needs no
prior pairing — a ~180° flip is just another θ. Validated against real before/after captures: **43/44
surfaces keep their id across the flip**, against 1/47 before. A WebXR anchor is now only the
**bootstrap** frame for the very first capture.

### 4.1.1 While it can't lock

A capture that fails to register **holds the last good frame and skips the render** — deliberately, so a
tilted or wrong-frame snapshot is never shown, posted, or allowed to pollute the reference. The cost is
that the held frame is in the *old* F_track, so after a relocalization the room on screen is visibly
rotated until a lock returns.

Because the room is wrong from that first held capture, the fallback that explains it has to track the
failure rather than lag it. After a short grace the client reveals passthrough — hiding `#world-root`,
the sky and the scaffold — and shows a headset-locked hint to step out of the play area and back in,
which forces the Quest to re-localize. It restores on recovery.

Two rules make that trigger reliable (`WM.relocStep`):

- **Recovery needs consecutive good captures**, not one. A single lucky capture between failures used to
  reset the timer, so a *flickering* lock — the normal shape after a sleep or a boundary trip — never
  accumulated the grace, and the hint either never appeared or blinked out while the room was still
  stale.
- **The grace depends on whether a lock was ever held.** Once it has, the displayed room is known-stale
  and the hint comes quickly; while still acquiring it stays long, so a cold start doesn't nag someone
  who is simply walking in.

### 4.2 Multi-user: one space, different Quest data

Two headsets scanning one physical space produce **different plane sets** — missing surfaces, extra
clutter, and walls whose captured rectangles are centred differently because each saw a different amount
of the wall. This is the mechanism that makes a guest work, and it is deliberate design rather than
tolerance-fudging:

- **Asymmetric size gate.** `sizeCompat` admits a plane that is a *partial* (smaller) view of a
  reference and only rejects a notably larger one. A guest who saw half a wall still matches it.
- **Coverage scoring**, as above — the guest's extra clutter is free.
- **Structural features, not centroids.** A wall's **corners** (wall ∩ wall) and its **floor/ceiling
  edges** (wall ∩ floor/ceiling) are *shared* features: both devices derive the same physical point from
  where surfaces actually meet, independent of scan coverage. A wall's **centroid** is a scan artifact.
  So anything that must agree across devices is defined by distances to intersections.
- **Wall identity by plane, not centroid.** `matchWall` keys on normal + perpendicular offset with an
  along-line overlap guard. Floors and ceilings keep `matchRef`; insets use `matchInset`.
- **Register-only guests.** A guest re-seeds its reference wholesale from the authoritative broadcast
  each capture and never establishes, lerp-mutates, mints, or posts geometry. Before this, a guest
  evolving its own copy of the shared reference caused a feedback drift that read as "the world drifts
  more over time".

The thresholds are named and exposed (`--reg-size-tol`, `--reg-min-cov`, `--reg-min-cov-frac`,
`--reg-inlier-m`, `--reg-yaw-peaks`) precisely so this can be tuned with a second headset.

### 4.3 The linchpin risk

With geometry local and placement plane-relative, **correctness hinges on each client mapping its local
surfaces to the right shared ids.** A bad lock or a `matchRef` swap now puts content on the *wrong local
wall* and resolves anchors against the *wrong reference planes*. Identity correspondence is the thing to
get right, and the thing to watch whenever two maps genuinely differ.

---

## 5. The plane-relative anchor — the placement primitive

An anchor defines a pose by its **relationships to nearby stable planes** — the floor and the ~3 nearest
walls, referenced by shared **id** — so any client re-solves it against its **own** planes. It is
deliberately **over-determined**: more constraints than degrees of freedom, which averages out any one
plane's noise *and* still solves when a client is missing a reference wall. (The technique is
multilateration; the codebase says "plane-relative anchor".)

### 5.1 Position — weighted linear least-squares

Position has 3 DOF, and a plane is a *linear* constraint (`n·p = c`), so this is a clean 3×3 solve.

- **Authoring** (owner, once): for the floor and each of the ~3 nearest walls `k`, store the signed
  perpendicular offset `d_k = n_k · (p − q_k)` plus the floor height `h = p.y − floor.y`.
- **Solving** (any client, at render): using its **own** local plane for each stored id, each wall
  contributes `n_k′ · p = d_k + n_k′·q_k′` and the floor contributes `up′ · p = floor.y′ + h`. Minimise
  `Σ w_k (n_k′·p − e_k)²`; the normal equations are a 3×3 system.
- **Weights:** floor highest (gravity is precise); walls by proximity, since a far wall's small angular
  error becomes a large positional one.
- **Missing walls are simply omitted** — redundancy covers them.
- **Degeneracy:** if the reference walls are near-parallel (a hallway) the along-corridor axis is
  unconstrained. Detected by condition number, and handled by reaching for a farther *perpendicular*
  wall, extending the reference set until the axis is constrained — and logged.

### 5.2 Orientation — per-wall quaternion votes, averaged

Express the rotation relative to the two things every client measures precisely and locally: **gravity
and the walls**.

Gravity is **not a rotation** — it is a known world direction, read from the IMU and confirmed by the
level floor. A **wall's frame** `W_k` (`wallFrame`) is the rotation whose local +Z is that wall's
outward normal and local +Y is up. It is **entity-independent**: any client rebuilds the same `W_k` from
that wall's normal plus gravity, which is the whole trick — it carries the wall's heading, so anything
stored relative to it rides the wall.

- **Author:** store one quaternion vote per reference wall, `rel_k = W_k⁻¹ · q_entity` — the entity's
  orientation *as seen from that wall*.
- **Solve:** rebuild `W_k′` from the client's own detected normal, reconstruct `q_k = W_k′ · rel_k`.
- **Average the votes** (`averageQuat`), first flipping each into the first vote's hemisphere — `q` and
  `−q` are the same rotation, so an unflipped sum can cancel.

**Quaternions throughout, never stored Euler angles.** No gimbal lock, no euler-order ambiguity (recall
A-Frame is YXZ), and **no yaw is ever extracted**, so an object pointing straight up or down is safe.

### 5.3 Placement modes

Every placed entity declares a `mode`, and the mode fixes **both** the position solve and the
orientation form as one consistent choice:

| `mode` | Position | Orientation | Typical |
|---|---|---|---|
| **grounded** *(default)* | XZ multilaterated; **Y snapped to the local floor** at that XZ | wall-yaw only, **pitch ≡ roll ≡ 0** | a model set on the floor or a table |
| **free** | full 3-D, floor height as one constraint | the averaged quaternion kept whole | a prop hanging or tilted in mid-air |
| **on-surface** | rides its host surface if detected locally; else in-plane | host surface's normal + wall-yaw | a photo on a wall or shelf |
| **skybox** | none | wall-yaw only | the environment dome |

`grounded` forces upright **structurally** via `twistAbout(q, up)` — the twist half of a swing-twist
decomposition — rather than trusting a measured near-zero tilt that could drift. A thing resting on the
floor cannot lean, so this is more robust than believing the walls.

Worth not mis-grouping: **rotation about up is a *wall* DOF, not a gravity DOF.** Gravity fixes the two
tilts; the walls fix the heading.

### 5.4 Detected surfaces are headset-first

Walls, floors, ceilings **and** doors, windows, wall-art are all *detected* surfaces: each client renders
them from its **own live capture**, matching its own passthrough exactly, applying only the shared id,
semantics and styling. The server never places them. An inset's hole-cut and snap nudge are computed
locally.

The single exception is §6.

### 5.4a The basis is per-world, and a room-less world has none

Interaction modules convert a dragged pose through `ConjureFrames` (`anchorFor`, `toRef`), which read one
cached basis: `framePlanes` — the last capture's **local** walls (F_track) and **seed** walls (F_ref).
Two rules govern it, and both exist because breaking either produced the same symptom, an object
teleporting on release and snapping back on reload:

- **It is cleared on every world switch.** `_placeContent` is its only writer and runs only when the
  world has real surfaces, so a room-less world can neither refresh nor blank it. Left alone it carried
  the *previous* room's walls into a void world, where a grab commit authored an anchor against walls
  that world does not have, the inbound `meta.anchor` re-solved against them, and `contentPoseIsLocal`
  then claimed the local solve owned the pose — suppressing the server's correct raw transform in the
  same patch. Reload looked like a fix because a fresh page has no basis at all.
- **Both wall sets or neither** (`WorldModel.hasFrameBasis`). `anchorFor` reads only the local walls and
  `toRef` needs both, so a half-present basis let a commit send a raw local position *plus* a
  wall-relative anchor — two descriptions of the pose that disagree, in a message where one is supposed
  to be the conversion of the other.

**In a room-less world the raw pose IS the pose.** That is not a degraded path but the correct one, and
the server enforces it: `/manipulate` refuses to store `meta.anchor` while the live space is `<void>`
(a plane-relative anchor names surfaces that do not exist there). The guard is what keeps a client-side
basis fault local to that client instead of persisting it for every client and every reload.

### 5.5 Avatars

The moving, high-frequency case. Computed at the **source**, re-solved at each **receiver**:

- **Source**, every presence tick (~10 Hz): author the head's anchor in its own F_track — floor height
  plus signed distances to its nearest ~3 walls by shared id — and stream the anchor (ids, distances,
  per-wall `rel_k`) instead of a raw pose. Orientation is effectively **free** (gaze pitches and can
  roll), so full quaternion votes, gimbal-safe when someone looks straight up.
- **Receiver:** re-solve against **its own** walls → the avatar lands on the same real walls the receiver
  sees, with no shared-frame error. Missing a referenced wall just drops a constraint.
- **Fallback:** a desktop receiver or void world (no local walls) uses the streamed F_ref pose.
- Cost is trivial — one 3×3 solve per avatar per tick.

---

## 6. Recovering a surface the client did not capture

The **only** exception to headset-first. A **non-wall** surface that exists in the seed but is absent
from a client's live capture: the client simply missed it.

Once the map has stabilised, for each unmatched seed surface the client solves its anchor against its own
local walls and folds it into the render set, so it draws and can host content. Each recovery logs
`[recover] surface … reconstructed`.

- **In-wall surfaces ride their recorded host wall.** Recovery applies the inset's seed
  offset-from-its-wall onto the wall's **local rendered** pose: `inset_local = wall_local · (wall_seed⁻¹ ·
  inset_seed)`. This preserves along-wall position *and* height exactly. A free multilateration
  under-constrains the along-wall axis for a mid-wall inset with no perpendicular wall nearby, which
  showed up as >10 cm lateral shifts.
- **Snapping still runs** on a recovered inset, so it ends up co-planar with its wall rather than at the
  raw anchor depth.
- **Walls are excluded.** A wall is the *basis* of the anchor system — a plane, not a point — and its
  absence perturbs everyone's nearest-wall set. Missing-wall recovery is a separate, harder problem.
- **Live detection wins the moment it is available**, and the id is re-inherited via `matchRef`.

`--drop-surface SEMANTIC|ID[,…]` makes the client pretend it did not capture matching surfaces, so this
is exercisable with one headset.

### 6.1 Inset identity — corner-relative

An inset is identified as **`semantic + host_wall + slot`** (its ordinal along the wall), not "the door
near coordinate X". Its along-wall coordinate comes from distances to the wall's **corner points**, its
height from the wall∩floor and wall∩ceiling lines — over-determined, 2 references averaged, 1 used
directly, 0 falling back to the wall centre and **flagged** (a freestanding wall is a known-degraded
case).

This decouples identity from absolute position, so an inset can move along its wall, or shift with a
relocalization, **without re-minting** — and therefore without losing its director styling.

- Identity resolves against the **persistent local reference constellation**, not the server seed.
  Keying off the seed failed on device: the owner accretes its reference every capture while the seed
  round-trips with a lag and is empty right after establish, so every inset minted a fresh id each
  capture and the space churned into a relocalize deadlock. First capture after establish still mints
  once, then it is stable.
- **Inset→wall association is a recorded fact** (`meta.host_wall`), for captured insets too, not
  re-guessed by proximity. This is the only reliable way to tell apart the two near-coincident,
  anti-parallel faces of a room partition — and a co-facing rule *cannot* break that tie, because an
  inset's live normal may be inward. `hostWallFor` derives it geometrically when unrecorded, using
  `|dot|` and a within-width test.
- **Duplicate guard.** `dupInsetIds` finds same-semantic, same-host insets whose stored centres sit
  within 0.25 m — closer than two real insets could be — and shadows the non-canonical id
  deterministically. Without it, a `matchInset` miss can mint a twin, the two ids oscillate faster than
  the removal debounce, and recovery re-materialises whichever the capture did not claim: a flickering
  visual duplicate.

---

## 7. Shell geometry — corners, sealing, and what was removed

Applied identically to the local render **and** the posted seed, in this order:

1. **`joinCorners`** — closes wall∩wall side gaps, tagging each corner with its partner wall id
   (`wallCorners` exposes them for anchoring).
2. **`sealWalls`** — snaps each wall's **top** onto the ceiling above and **bottom** onto the floor
   below. The Quest fits walls a few mm to a few cm short, which is invisible in wireframe and shows as
   an open slit the moment fills are solid (measured: `wall_41` top at y=2.669 against `ceiling_13` at
   2.673 — 4 mm). **Vertical only**: it moves centre height and height, never the plane, normal,
   horizontal offset or width, so registration and anchors are unaffected. Guarded by `--wall-seal-tol`
   (0.15 m, `0` disables) so a genuine partial or knee wall is not stretched, and **multi-room safe** —
   it seals to the nearest *covering* ceiling by footprint test, not to another room's.
3. **`snapInsets`** — snaps insets onto their host wall and records the openings. Runs *after* sealing,
   so openings are cut against the sealed wall with no compensation needed.

**Wall squaring was removed.** `squareWalls` snapped a wall's facing to an orthogonal grid, a vestige
from when the seed *was* the rendered geometry. Under local-first every headset renders its own raw
capture, so the seed is never drawn — and squaring only ever touched the *seed*, making the shared model
systematically inconsistent with the raw geometry every client uses. It rotated a normal by up to ~12°,
and since anchors use perpendicular distance to that normal, a point a couple of metres along the wall
shifted by up to ~0.4 m. Everything relating raw-local to seed inherited that error: anchors,
registration, and recovery. Removed entirely rather than made toggleable.

---

## 8. What reaches the server

The owner pushes **only when the shared model genuinely changes**. Per-centimetre drift never
round-trips.

**Structural triggers:** a surface added; a surface confirmed removed; a semantic reclassification; a
**large move** beyond ~0.5 m or 20° (real furniture moved or a re-scan, not drift) — which prints and
logs, because it is rare and attention-worthy; a styling or content edit; a boundary change.

**Server side** (`_surface_structural_change`, `server.py:2884`): a surface is added when new, updated
only on a structural change, and pruned when absent. Those geometry ops are applied to the stored seed
and **never broadcast** — clients render locally. Only what clients actually consume goes out:
room-activation env, boundary, and on-surface image re-anchors.

**Client post-gating:** the owner keeps an authoritative known-set seeded from the persisted seed on
entry and POSTs only on a structural change, so **a settled space sends no `/space/capture` traffic at all**.
Removal confidence lives on the client (a 3-capture debounce), so a surface missing from a post is
genuinely gone and the server prunes it at once — no server-side absence counter.

Surfaces with content pinned to them (`anchored`) are **never pruned**, so a photo's host id cannot
orphan.

There is **no time-based establish-then-freeze**. An earlier design held a ~20 s establishing window and
then froze static surfaces; it is gone, along with `_ESTABLISH_SECS`, `_STATIC_SEMANTICS` and
`_static_frozen`. Clients render locally, so there is nothing shared to stabilise.

**Authority** is the space owner; `/space/capture` is owner-only. An idle authority is taken over after
`_AUTH_TTL` so a reconnecting owner is not locked out.

---

## 9. Keeping it off the frame budget

At 90 Hz the frame budget is ~11.1 ms. The capture pass originally did everything synchronously in one
A-Frame `tick` and measured ~22 ms, dropping a frame every ~2 s. While the head is translating, the
compositor's reprojection of that dropped frame reads as a ~1 cm world flick-and-return. Standing still
reprojects cleanly, which is why it only ever showed while walking.

Four fixes, ~22 ms → ~5 ms on-main:

1. **Solve off the render thread.** `register` runs in a module Web Worker (`client/room-worker.js`).
   The throttled tick posts compact planes plus the reference constellation — plain numbers, a few kB —
   and the reply drives the render continuation. The worker imports a standalone three (the math takes
   `THREE` as an argument, so a version-independent copy composes fine with A-Frame's on the main
   thread). A **synchronous fallback** runs inline if the worker cannot start. This is the property that
   matters: as the solve gets heavier, the render does not stutter, it just refreshes less often.
2. **Apply-gate pose/shape split.** `surfaceMoved` split into `surfacePoseMoved` (drift — same physical
   shape) and `surfaceShapeChanged` (extent, openings). Drift re-lays only the cheap transform; the
   expensive holed-wall re-triangulation runs only on a real shape change.
3. **Styling gate.** Material, visibility, edges and labels are global display state that never changes
   per capture, so they run only on first lay or an actual change — not for ~60 surfaces every capture
   (~9 ms → ~1.5 ms).
4. **Time-sliced mesh rebuild.** Re-triangulation is enqueued and drained a few per frame under
   `--geo-slice-ms` (3 ms default). A whole-space rebuild spreads across frames instead of overrunning
   one. Pose is applied immediately so positions are always correct; deferral is purely cosmetic. A
   throttled `[geo] backlog` line fires if the queue outgrows its warn threshold — no silent lag.

Result: 31/33 captures ≤6 ms, no per-capture frame drop.

### 9.1 The apply-gate, and why it has two subtleties

A per-surface deadband skips re-touching anything that has not moved past tolerance — this is what kills
the mesh-rebuild pop, with **no server in the loop**. It covers both halves: real surfaces *and* placed
content. Content re-solves against the raw plane basis every capture, so without its own deadband the
solved pose wanders a few millimetres and content shimmers while the gated walls sit still (measured:
~5–6 mm envelope against walls frozen to 4 dp). Content uses the **same** tolerance as the walls, so the
two agree — both re-place past tolerance, or neither moves.

**`advanceSig` — hold the shape baseline on a pose-only re-lay.** Advancing the *whole* signature on a
pose-only re-lay silently absorbed sub-tolerance extent drift into the baseline, so
`surfaceShapeChanged` measured against a shape the mesh had never drawn, the rebuild never fired, and the
rendered mesh ran away from the true shape over a session. Now a pose-only re-lay advances position and
rotation but **holds extent and holes** at the last-rendered value. Drift past tolerance still trips a
real rebuild, so it self-heals.

**Grouped surface re-lay** (`--group-surface-relay`, default on). The per-surface gate holds each surface
on its own epoch, so under drift a wall re-lays at one capture while its adjoining floor and ceiling — or
a door versus its wall's cutout — re-lay at another. Anything that must stay aligned *across* surfaces
then opens a seam over a session. Each capture is internally consistent, so the divergence is purely
render epoch, which is why a reset heals it. So when **any** real surface moves, all of them re-lay
together at one epoch, flagging both pose and geometry — a wall's centre and its `joinCorners` **width**
are a matched pair and must never materialise from different captures.

A **2 mm fill weld** (`--surface-weld`) inflates each surface *fill* so abutting fills overlap and
passthrough cannot flicker through float-rounding cracks. The wireframe outline stays true size.

### 9.2 Pose smoothing — the other half of the pop

Keeping the capture from dropping a frame does not change the fact that a surface crossing tolerance is
*snapped* to its new pose in one frame — a discrete step every ~2 s.

Three clocks, deliberately decoupled:

| Clock | Rate | Work |
|---|---|---|
| **solve** | ~0.5 Hz | `register` → id correspondence — off-thread |
| **shape** | event-driven | re-triangulation on a real shape change — sliced |
| **pose-follow** | 90 Hz | ease each surface toward its latest target |

The mechanism splits **adopt a target** (capture rate, gated by the deadband) from **move to it** (frame
rate). Per unsettled entity, each frame:

```
a = 1 - exp(-dt / tau)
position.lerp(target.pos, a);  quaternion.slerp(target.quat, a)
if (posGap < POS_EPS && angGap < ANG_EPS) { snap exactly; drop from slewSet }
```

- `1 - exp(-dt/tau)` rather than a fixed per-frame alpha, so *wall-clock* settle time depends only on
  `tau` — independent of frame rate and robust to jitter. This is the EMA/slew equivalence with the rate
  derived from a time constant.
- `POS_EPS` ≈ 1 mm, `ANG_EPS` ≈ 0.1°: the exponential never arrives exactly, so the epsilon snap
  guarantees termination and steady-state cost returns to **zero**.
- **The deadband is the free noise floor.** Sub-tolerance jitter never becomes a target, so an idle space
  does no slew work at all.
- **Content eases in lock-step** — a surface and the content glued to it adopt targets on the same
  capture epoch and share one `tau`, so wall-art does not visibly separate from its wall mid-transition.
  Content anchoring must keep solving against the *target* poses, not a mid-slew transform.
- `slerp` for rotation, never lerped Euler angles.

**Knob:** `--pose-tau`, **default 0 = off** (snap, as before), so it A/Bs on-headset like
`--geo-slice-ms`.

**Where not to smooth.** Smoothing a single room frame (`#world-root` / `Tmat`) is wrong for a captured
space and is recorded here so it is not re-proposed: world-root is identity by design, each surface
carries its own F_track pose, and `Tmat` drives no render transform — smoothing it would smooth nothing
visible. Smoothing must be **per-surface**. The void/outdoor regime is the exception, since it genuinely
does park world-root at `Tmat⁻¹`.

**The upper bound on `tau`** is not compute, it is AR lag: in passthrough the real wall is visible, so
too large a `tau` makes the virtual surface visibly trail the real one during a correction (~0.15 s
ballpark). Steady state has no lag, so the ceiling only bites during the transition.

**It cannot fix a noisy target.** If the target itself jitters, slew turns a sharp pop into a gentle
swim.

---

## 10. Knobs

| Knob | Default | Purpose |
|---|---|---|
| `--capture-interval` | 2 s | capture throttle |
| `--apply-tol-pos` / `--apply-tol-rot` / `--apply-tol-ext` | — | the apply-gate deadbands (shared by surfaces and content) |
| `--group-surface-relay` | on | re-lay all surfaces at one epoch; off reproduces the seams for A/B |
| `--geo-slice-ms` | 3 | re-triangulation budget per frame; ≤0 disables slicing |
| `--pose-tau` | 0 (off) | slew time constant |
| `--wall-seal-tol` | 0.15 m | max gap `sealWalls` will close; 0 disables |
| `--surface-weld` | 0.002 m | fill overlap against hairline cracks |
| `--on-surface-standoff` | — | how far an inset sits in front of its wall plane |
| `--reg-size-tol` / `--reg-min-cov` / `--reg-min-cov-frac` / `--reg-inlier-m` / `--reg-yaw-peaks` | 0.5 / 4 / 0.3 / 0.4 / 5 | registration acceptance |
| `--wall-perp-tol` / `--wall-yaw-tol` / `--wall-overlap-slop` | — | `matchWall` identity tolerances |
| `--drop-surface` | — | pretend a surface was not captured (exercise recovery solo) |
| `--foveation` | 0 | GPU headroom; raising it cut the dropped-frame rate monotonically |
| `--debug-registration` | off | registration/residual logging |
| `--debug-jitter` | off | frame-pacing probes (kept decoupled so heavy logging cannot contaminate timings) |

---

## 11. Where the solver lives

The solve runs on **both** the client (JS, against live geometry) and the server (Python, against the
seed). Rather than embedding Node in the Python server, the solver is **ported** and both
implementations are pinned by **shared golden vectors**: `tests/test_plane_anchor.py` checks
`conjure/plane_anchor.py` against `tests/js/fixtures/plane-anchor-golden.json` — the same file the JS
suite uses — to 1e-6 m / 1e-5 rad. The parity contract is far cheaper than a JS runtime, and keeps the
server pure-Python and the client pure-JS.

The shell geometry (corners, sealing, snapping, inset reconstruction) is **JS-only**; the server never
reconstructs an inset. If a server-side inset query ever appears, it ports to `conjure/room_snap.py`
with a golden test like `plane_anchor`'s.

---

## 12. Surface reference

**`client/room-snap.js`** — pure geometry, `THREE` passed in, unit-tested:

`register`, `selectSpace`, `canonicalFrame`, `surfaceToRef`, `matchRef`, `matchWall`, `matchInset`,
`dupInsetIds`, `wallCorners`, `authorInsetAnchor`, `reconstructInset`, `insetAlong`, `hostWallFor`,
`joinCorners`, `sealWalls`, `snapInsets`, `eulerYXZ`, `yawOf`.

**`client/plane-anchor.js`** — `authorAnchor`, `solveAnchor`; internals `wallFrame`, `averageQuat`,
`twistAbout`, `solveSym3`, `cond2`.

**`client/world-model.js`** — `surfaceSig`, `surfaceMoved`, `surfacePoseMoved`, `surfaceShapeChanged`,
`advanceSig`, `slewAlpha`, `slewSettled`, `eulerYXZQuat`, `holesAttr`, `spawnRight`, `avatarAim`.

**`client/room-worker.js`** — the off-thread `register` host.

**`conjure/plane_anchor.py`** — the Python port, golden-pinned.

| Concern | Where |
|---|---|
| structural-change gate | `conjure/server.py:2884` `_surface_structural_change` |
| ingest (seed-only, no broadcast) | `conjure/server.py:2909` `ingest_room` |
| seed planes for server solves | `conjure/server.py` `_seed_planes` |
| server-side anchor authoring | `conjure/server.py` `_content_anchor` |
| pose-relative queries | `conjure/server.py:3898` `/view_relative` |
| local render + world-root identity | `client/conjure-client.js:1396` |
| content placement | `client/conjure-client.js` `_placeContent` |
| recovery | `client/conjure-client.js` `_recoverMissing` |

**Tests:** `tests/js/room-snap.test.js` (synthetic rooms plus `fixtures/golden-room.json`, a real
45-surface two-room Quest capture), `tests/js/plane-anchor.test.js`, `tests/js/world-model.test.js`,
`tests/test_plane_anchor.py`.

---

## 13. Related specs

- [`specs/spaces.md`](./spaces.md) — the space record, selection, admission, authority.
- [`specs/worlds-surfaces.md`](./worlds-surfaces.md) — styling, visibility, immersion, openings.
- [`specs/occlusion.md`](./occlusion.md) — real-world depth occlusion.
- [`docs/investigations/`](../investigations/) — the debugging campaigns behind §9, and what was ruled
  out.
