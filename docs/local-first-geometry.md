# Local-first geometry — a shared semantic model over per-client geometry

**Status:** DESIGN (supersedes the deleted `geometry-adaptation.md`). Each client renders its **own** live
geometry locally; the server holds only the shared **semantic** model (ids, semantics, styling, and
**plane-relative anchors** for content). No geometry is broadcast. This drops the capture→server→capture
round-trip, removes the owner/guest asymmetry, and stops fighting the Quest's **non-rigid** map.

Placement of everything that isn't glued to a single surface uses a **plane-relative anchor** (technique:
**multilateration** — position fixed by distances to known references; look it up under that name). We use
the term "plane-relative anchor" throughout docs and code; "multilateration" is noted here once for the
reader who wants the background.

## 1. Why (the finding that forces this)

On-device (residual probe, `--debug-registration`): the Quest's map of a multi-room space is **locally
non-rigid** — a return session's live geometry differs from the persisted snapshot by **up to ~9 cm,
concentrated in one region**, which **no single rigid transform can reconcile**. Broadcasting one headset's
coordinates and forcing another to render them therefore *guarantees* a one-sided shift. Re-deriving per
session looks right because the rendered walls are *that* session's own geometry.

Resolution: **don't share geometry.** Share *identity + meaning*; keep *coordinates* local; and define the
placement of free content by **distances to the locally-precise planes**, not by any shared coordinate.

- Wall **planes** (normal + offset) and the **floor** are estimated far more stably across captures than
  wall **centers/extents** (which depend on how much you saw). So we anchor to the *planes*.
- A photo is anchored to **wall_3** (an id); a free model to **the planes around it** — each headset
  resolves them against **its own** geometry → things land at the right *physical* spot for every client,
  even though coordinate numbers differ by cm. **Physical consistency is preserved; coordinate agreement is
  neither needed nor observable.**
- **Registration's real job is *identity correspondence*** ("which of my local surfaces *is* wall_3"), not
  coordinate sharing.

## 2. Frames

| Frame | Scope | Stable? | Role |
|---|---|---|---|
| **F_real** | the physical room | ground truth | what every render must *visually* match (passthrough) |
| **F_track** | one headset, one session (WebXR `local-floor`) | no — drifts / can jump | **everything renders here**: live surfaces, on-surface content, and all plane-relative-anchored content, all in the client's own F_track |
| **F_ref** | the seed's frame | — | **internal to registration only** — used to match a live capture to the seed so we can assign **ids**. **Nothing is placed or rendered in F_ref.** |

**Consequence — there is no shared render frame.** Registration still solves `T : F_track → F_ref`, but its
*only* useful output is the **id correspondence** (`matchRef`). `#world-root` is effectively **identity**;
content is F_track-native. This means content **rides F_track drift/jumps for free**: when tracking jumps,
the detected walls jump, and each plane-relative anchor **re-solves against the jumped walls** → content
stays glued to the walls (and thus to passthrough) with no frame-transform bookkeeping.

## 3. What's shared vs. local

| | Shared (server, owner-authored) | Local (each client) |
|---|---|---|
| Surface **set** (ids, semantics) | ✅ | consumes it |
| Per-surface **styling** (material/colour/texture/visibility) | ✅ by id | applies by id to local geometry |
| On-surface **content / insets** (photo, wall art, door/window on wall_3) | ✅ by id, **in-plane** anchor (§5a) | renders in the **local** surface's plane |
| **Free** content, skybox, and (streamed) **avatar** poses | ✅ as **plane-relative anchors** | **re-solved** against local planes |
| **Seed** (reference constellation + each surface's plane-relative anchor) | ✅ — **also the server's own solver geometry** | registers against it; recovers missing surfaces from it |
| Exact surface **pose/extent** | ❌ never shared | ✅ each client's own live estimate |

**Client/server symmetry.** The **seed *is* the server's local geometry.** The server does some geometry
itself — locating things relative to a user's pose (`view_relative`, "the wall I'm looking at"). It runs the
*same* plane-relative solve (§4) against the **seed** that a client runs against its live capture. So the
server is "just another client whose walls are the seed" — one solver, two hosts (see §13 on where the code
lives).

## 4. The plane-relative anchor (the placement primitive)

A plane-relative anchor defines a pose by its **relationships to nearby stable planes** — the **floor** and
the **~3 nearest walls** (referenced by shared **id**) — so any client can re-solve it against its **own**
local planes. It is deliberately **over-determined**: more constraints than degrees of freedom, which (a)
reduces variance by averaging out any single plane's noise and (b) still solves if a client is **missing**
one of the reference walls. (Technique: multilateration.)

### 4.1 Position — weighted linear least-squares

Position has 3 DOF. Each plane gives one **linear** constraint (a plane is `n·p = c`), so the solve is a
clean weighted **linear least-squares** — a 3×3 system, exact and fast.

- **Authoring** (owner, once, at placement/update). For the floor and each of the ~3 nearest walls
  `k` (by id): store the entity's signed perpendicular offset `d_k = n_k · (p − q_k)` (`n_k` = that plane's
  unit normal, `q_k` = a point on it), and the floor **height** `h = p.y − floor.y`. Store `{id_k, d_k}` +
  `h`.
- **Solve** (any client, at render). Using the client's **own** local plane `(n_k′, q_k′)` for each stored
  id it has: each wall contributes `n_k′ · p = d_k + n_k′·q_k′`; the floor contributes `up′ · p = floor.y′ + h`
  (`up′` = the client's floor normal ≈ gravity). Minimise `Σ_k w_k (n_k′·p − e_k)²`; the normal equations
  `(Σ w_k n_k′ n_k′ᵀ) p = Σ w_k e_k n_k′` are a 3×3 solve for `p`.
- **Weights `w_k`:** floor highest (gravity is precise), walls weighted by proximity (nearer = more
  reliable; a far wall's small angular error becomes a large positional error). Missing reference walls are
  simply omitted (redundancy covers them).
- **Robust fallback:** if one constraint's residual is a gross outlier, drop it and re-solve once (or take
  the median of the two-wall subset solutions — the outlier-robust version of the same idea).
- **Degeneracy:** if the 3×3 matrix is near-singular — the reference walls are all near-**parallel** (a
  hallway), so the along-corridor axis is unconstrained — detect it (small determinant / condition number)
  and fall back by **reaching for a farther perpendicular wall** (extend the reference set beyond the nearest
  ~3 until the axis is constrained; keep the seed estimate only if none exists) and **log** it.

### 4.2 Orientation — a reference frame `R`, then a stored rotation relative to it

Orientation is **3 rotational DOF**. The unifying idea: build a **reference frame `R`** from the two things
each client can measure *precisely and locally* — gravity and the walls — then store the entity's rotation
**relative to `R`**. Every client reconstructs `R` from its own gravity + walls, so the stored rotation
transfers exactly across non-rigid maps.

First, be exact about **gravity**: it is **just a known world direction — down/up** — read from the headset
IMU and confirmed by the level floor (the floor normal). It is *not* a rotation; it's a precise,
already-shared reference **axis**.

**The reference frame `R`** = **up** from gravity + **forward** from the walls (the weighted **circular
mean** of the reference walls' horizontal normals — over-specified/robust, same reference set as the
position solve). `R` is **entity-independent** (built from gravity + walls only), so authoring and solving
reconstruct the *same* `R` up to local precision.

How much of the entity's rotation we store relative to `R` depends on its **placement mode** (§5) — this is
where orientation and position tie together:

- **Grounded (default) → yaw-only.** A thing resting on the floor **cannot lean**, so `up ≡ gravity` and
  **pitch ≡ roll ≡ 0 by definition** — we don't store them (pinning to zero is *more robust* than trusting a
  measured near-zero tilt that could drift). Only **heading (yaw)** is free, and it's the one DOF gravity
  says nothing about (spin an upright object in place — still upright), so it comes from the **walls**:
  store the entity heading's offset from each reference wall's normal-yaw, `Δψ_k = ψ_entity − ψ_wall_k`; at
  solve, each wall proposes `ψ_entity = ψ_wall_k′ + Δψ_k`, combined by weighted **circular mean**. *(Note:
  rotation-about-up is a **wall** DOF, not a gravity DOF — the easy thing to mis-group.)*
- **Free / world-placed → full quaternion.** An object that rotates freely (pitch and roll are real, not
  zero) stores its **complete orientation as one quaternion `q_rel = R⁻¹ · q_entity`**. We deliberately do
  **not** split out a separate yaw here: a free object whose forward points near-vertical has a **degenerate
  heading** (gimbal — the same trap as A-Frame's YXZ euler-order gotcha), so a single quaternion relative to
  the entity-independent `R` is both fully general and gimbal-safe. Solve: `q_entity′ = R′ · q_rel`.
  Heading + pitch + roll are all first-class, carried together in `q_rel`.

Why a quaternion rather than stored pitch/yaw/roll angles: no gimbal lock, no euler-order ambiguity (recall
A-Frame renders **YXZ**), and it's exactly what three/A-Frame hand us (`object3D.quaternion`). Grounded's
yaw-only form is just the special case where `q_rel` is a pure yaw and we enforce that structurally.

## 5. Placement modes

**Every placed entity declares a `mode` in the world model, and the mode fixes *both* its position solve and
its orientation form** (§4.2) as one consistent choice. This is the single knob that keeps geometry and
orientation aligned:

| `mode` | Position | Orientation | Typical |
|---|---|---|---|
| **grounded** *(default)* | XZ multilaterated (§4.1); **Y snapped to local floor** at that XZ | gravity-up + robust wall-yaw; **pitch≡roll≡0** | a model set on the floor/table |
| **free** | full 3-D multilateration incl. floor-height (§4.1) | **full quaternion `q_rel`** rel. to `R` (§4.2) — heading+pitch+roll | a prop hanging/tilted in mid-air, a rotating object |
| **on-surface** | **in-plane** (§5a), anchored to a surface id | in-plane (surface normal + wall-yaw) | photo, wall art, door/window inset |
| **skybox** | none | wall-yaw only | environment dome |

`grounded` is today's default and covers most placements; `free` is the explicitly-authored case for content
that leaves the floor or rotates arbitrarily. The world model stores `mode` per entity so authoring and every
client agree on how to solve it.

- **(a) On-surface (in-plane plane-relative).** Full-surface **textures** need no positioning (they span the
  surface). **Positioned** on-surface content (hung photos, wall art) and **insets** (doors/windows) are
  anchored **in the surface's plane** — *not* offset from the surface **center**, because the plane and its
  alignment are stable across clients but the center/width are **not** (they depend on how much each client
  captured). Same thesis as §4, one level down. In-plane coordinates:
  - **vertical:** height above the **floor** (floor plane → gravity-precise);
  - **lateral (along the wall):** distance from a **stable in-plane reference** — the **corner** where this
    surface meets an adjoining wall (a corner is a plane∩plane intersection → stable, unlike the wall's own
    ends). Over-specify with **both** adjoining corners when present and average; fall back to
    center-relative only when no corner is captured.

  Result: a photo or door lands at the same **physical** spot on the wall for every client even when their
  detected wall centers/widths differ. Rendered in the client's **local** surface plane → exact passthrough
  match. (This subsumes today's `snapInsets` center-offset placement.)
- **(b) Free / world-placed.** Full 3-D position (§4.1, floor-height as one constraint) + full-quaternion
  orientation (§4.2). *The independent-3D-model case — a model that floats, hangs, or tilts.*
- **(c) Grounded (default).** XZ from the position solve (§4.1) but **Y re-seated to the local floor** at that
  XZ, so it never floats/sinks against a client's own floor; orientation is yaw-only (§4.2). *Grounded =
  floor-anchored in Y + upright, plane-relative in XZ + heading.*
- **(d) Skybox → yaw-only (§4.2).** No position; store just the wall-relative **yaw** so every client faces
  the scene the same way.
- **(e) Avatars → per-tick streamed plane-relative anchor (§5.1).**
- **Fallback — undetected surface → recovered plane-relative anchor (§5.2).**

### 5.1 Avatars (spelled out)

Avatars are the moving, high-frequency case; they use plane-relative anchors, computed at the **source** and
re-solved at each **receiver**:

1. **Source (the user being shown), every presence tick (~10 Hz):** compute the head's plane-relative anchor
   in the source's own F_track — floor height + signed distances to the source's **nearest ~3 walls (by
   shared id)** for position. Orientation is effectively **free** (gaze pitches up/down and can roll
   slightly), so stream the head's **full quaternion `q_rel` relative to `R`** (§4.2) rather than a
   decomposition — gimbal-safe when someone looks straight up/down. **Stream that anchor** (ids + distances +
   `q_rel`) over presence instead of a raw pose.
2. **Wall-set hysteresis (important):** a moving head's "nearest 3 walls" set would flip every tick, jerking
   the solve as reference walls swap. Keep a wall in the set until another beats it by a **margin** (e.g.
   0.3 m closer) so the reference set is stable frame-to-frame.
3. **Each receiver:** re-solve the streamed anchor against **its own** local walls (by id) + floor (§4) →
   the avatar's position + orientation in the receiver's F_track → render. Missing a referenced wall ⇒
   solve with the ones it has.
4. **Why it's correct:** the avatar lands relative to the receiver's **own** walls → aligned to the
   receiver's passthrough, with **no shared-frame rigid error**. Locality still bounds it — you mainly see
   avatars of people **near** you, where you share reference walls; distant people are wall-occluded.
5. **Cost:** source computes nearest-walls + distances per tick (cheap); receiver solves one 3×3 LS per
   avatar per tick (cheap). Fine at 10 Hz for a handful of users.
6. **Fallback:** if either end can't form a valid anchor this tick (too few / degenerate walls), skip the
   update (hold last) rather than fall back to a shared-frame pose.

### 5.2 Recovering a surface the client didn't capture

A **non-wall** surface (furniture, wall art, floor/ceiling, a window…) can exist in the seed but be **absent**
from a client's live capture (it missed it). Then:

- Once the client's map has **stabilised**, for each seed surface with **no local match**, the client
  **requests that surface's plane-relative anchor from the server** and **re-solves it against its own local
  walls** (§4) → the surface is recreated at a spot consistent with the client's *own* geometry (not a rigid
  guess). Content on it then rides that recovered surface.
- **Log + console** each recovery for awareness/debugging, e.g.
  `[recover] surface window_9 reconstructed from plane-anchor (refs: wall_3, wall_8, wall_11)`.
- If the client later **detects** that surface for real, it switches from the recovered anchor to its own
  live pose (id re-inherited via `matchRef`).
- **Walls are excluded** — they're the *basis* of the anchor system (a wall is a plane, not a point, and its
  absence perturbs everyone's nearest-wall sets). Missing-wall recovery is a separate, harder problem —
  deferred.

## 6. Client lifecycle

1. **Join / activate:** receive the shared model (surface set + ids/semantics, styling, on-surface content,
   free-item plane-relative anchors) + the **seed** reference constellation.
2. **Register (correspond ids):** match the live capture to the seed → assign each local surface its shared
   **id** (`matchRef`). (`T` is a byproduct; only the id mapping is used downstream.)
3. **Render locally, in F_track:** surfaces at their **local** poses; on-surface content relative to its
   local surface; free/skybox/avatars via **plane-relative solves** against local planes; undetected known
   surfaces via **recovered anchors** (§5.2).
4. **Adapt continuously & locally:** re-derive geometry each capture; a **local apply-gate** (skip
   re-touching a surface/anchor that didn't move past tolerance) kills the mesh-rebuild "pop" — **no server
   in the loop**.
5. **(Owner only) push structural changes** (§7) so the shared model + seed stay current. Guests never
   author.

## 7. Structural change → server (decision #1 — **for your evaluation**)

The **owner** pushes to the server *only* when the shared model genuinely changes; per-cm geometry drift
**never** round-trips. Proposed triggers:

1. **Surface added** — a detected surface with no matching id → mint id + semantic + default styling +
   compute its plane-relative anchor.
2. **Surface removed** — persistently absent (debounced prune; `anchored`-content surfaces protected).
3. **Semantic change** — a surface reclassified (rare).
4. **Large move** — a surface re-derived beyond a **LARGE** threshold (e.g. > ~0.5 m or a big rotation —
   real furniture-moved / re-scan, **not** cm drift) → recompute its anchor + seed pose. *Threshold tunable;
   distinct from the cm-level local apply-gate tolerance.* **Kept**, and it must **print to console + log**
   when it fires (`[large-move] surface X moved 0.8 m → seed pose updated`) — this is a rare, attention-worthy
   event we want visible, not silent.
5. **Styling / content edit** — director actions (authored, always shared).
6. **Boundary change** — floor polygon / height.
7. **Seed lifecycle** — first establishment mints the seed; `/room/realign` refreshes it. A **periodic seed
   refresh** (occasional autosave to a recent snapshot) is **deferred** — we'll likely fold it into the
   multi-client consensus-seed effort (§7.8) rather than build a single-client autosave now.
8. **(Future) Multi-client consensus seed** — instead of the seed being one headset's snapshot, the server
   could periodically fold **all connected clients'** geometry into a better shared reference. **Caveat:**
   the map is **non-rigid** (§1), so this is *not* a rigid average — a rigid mean would reintroduce the very
   one-sided shift we're avoiding. It must be a **regional / soft** alignment (per-surface or per-region
   consensus of *planes*, weighted by how many clients agree), used only to improve the **seed** the
   server solves against — never pushed back as coordinates clients must render. **Deferred**, and it's the
   natural home for the periodic seed refresh (§7.7) — noted so the design leaves room for it.

## 8. Authority (decision #2 — settled)

Only the **owner/authority** authors the shared model (set, seed, styling, content, anchors). Guests are
**local-only**: register, correspond ids, render. (Guest-proposes-a-missed-surface is deferred.)

## 9. Wall squaring — now optional (A/B), `--square-walls on|off`

`squareWalls` snaps a wall's facing to a width-weighted orthogonal **grid**, but **only when it's already
within ~12° of square** (clearly-angled walls are left alone — see the note at the end). Under local-first +
plane-relative anchors this is a genuine trade-off:

- **Against (why OFF may be right):** it rotates the wall **normal** up to ~12°, and since anchors use the
  perpendicular distance to that normal, a point a couple metres along the wall shifts by up to ~0.4 m if
  authoring and re-solving disagree on the normal. Each client computes its **own** grid, so squaring can
  make reference planes **diverge between clients**. And it overrides the local measurement we've chosen to
  trust.
- **For (why OFF may hurt):** squaring **denoises** per-capture angular jitter of wall normals; raw normals
  wobble a few degrees each capture, feeding placement jitter that the anchor redundancy only partly
  absorbs.
- It does **not** break `joinCorners` (real walls stay near-perpendicular); OFF just renders walls at their
  raw, slightly-non-orthogonal angles.

**Decision:** wire `--square-walls on|off` and A/B it. Default **OFF** (do *not* square) — consistent with
trusting the raw local planes we now build placement on. We may flip it to ON after A/B if leaving walls raw
causes problems (e.g. angular jitter visibly hurting placement); flipping the default is a one-liner.

## 10. The linchpin risk — id correspondence

With geometry local and placement plane-relative, **correctness hinges on each client mapping its local
surfaces to the right shared ids.** A bad lock or a `matchRef` swap (the wall-3/59 class) now puts content
on the **wrong local wall** / resolves anchors against the **wrong reference planes**. So this **raises the
stakes on registration / matchRef robustness** (the `--reg-*` knobs, same-facing gate, coincident-flip
fallback). It is *the* thing to get right, and to watch when maps genuinely differ (the missing-window case
is also an id-correspondence stress test).

## 11. What this removes / changes vs. today

- **Removed:** the geometry round-trip, establish-then-freeze (`_static_frozen` / #2), whole-set broadcasts
  and their "pop," the owner/guest render asymmetry, and — newly — the **shared render frame / `#world-root`
  transform** (content is F_track-native and rides drift automatically).
- **Core shift:** a headset renders real surfaces from **its own capture** (applying shared ids/styling by
  id); free content/avatars/skybox render via **plane-relative solves**; the server holds the **model + seed
  + anchors**, not a render source. `T`/registration survives **only** to assign ids.
- **Kept:** the local **apply-gate**; **persistence** (now explicitly *seed + anchors*, not truth);
  `matchRef` id stability; `anchored`-prune protection; the `--reg-*` knobs and `--capture-interval`.
- **Non-headset clients** (voice/CLI/desktop) have no passthrough → operate on the shared model + seed
  directly.
- **The server is a solver too:** its pose-relative queries run the *same* plane-relative solve (§4) against
  the **seed** as clients run against live geometry (§3, "Client/server symmetry").

## 12. Decisions & remaining open questions

**Decided:**

- **Large-move threshold — keep**, and **print to console + log** when it fires (§7.4).
- **Parallel-wall degeneracy — fall back to a farther perpendicular wall** (extend the reference set), log it
  (§4.1).
- **Placement modes — `grounded` (default) and `free` both supported**, mode declared per entity in the world
  model, mode drives position + orientation together (§5, §4.2).
- **Director geometry queries run against the seed** — accept the seed's approximation for placement
  *decisions*; rely on **future seed improvements** (§7.8, not yet planned) to sharpen it if ever needed.
- **Missing-surface recovery — log it** (`[recover] …`, §5.2).
- **Avatars — stream anchors with hysteresis, solve per receiver** (§5.1).
- **`--square-walls` default OFF** for now; revisit after A/B (§9).
- **Nothing in absolute coordinates** — the design principle; every item is on-surface, grounded, free, or
  skybox (verified during build — see Build-time verifications below).
- **Solver code — port to Python, pin JS + Python with shared golden vectors** (not Node-in-server, §13.1).
- **In-plane reference — use corners** (plane∩plane), **over-specify with both adjoining corners and
  average** when available; center-relative only when no corner is captured (§5a).
- **Consensus seed — deferred**, documented with a plan to investigate later; **periodic seed refresh
  folds into it** (§7.7–7.8).

**Build-time verifications** (things to confirm while implementing/testing, not design questions):

- **Grounded-model Y re-seat** (§5c) reads the local floor cleanly, no jitter.
- **Absolute-coordinate audit** — verify *nothing* is placed in raw F_track/F_ref; every item is on-surface,
  grounded, free, or skybox.

**Still open:** *(none — all design questions resolved; the items above are build-time checks.)*

## 13. Implementation sketch (incremental)

### 13.1 Where the solver lives (one algorithm, two hosts)

The solver runs on **both** the client (JS, against live geometry) and the server (Python, against the seed
— §3 symmetry). Options: **(a)** run the shared **JS via Node** on the server — single source of truth, but
a whole JS runtime / subprocess dependency bolted onto a Python server; **(b)** **port to Python** and pin
*both* implementations with **shared golden test vectors** (identical `{planes, anchor} → pose` cases
checked in the JS *and* Python suites) so they can't silently drift. **Decided: (b).** The solver is tiny
pure math (a 3×3 weighted least-squares + circular mean); the parity-test contract is far cheaper than
embedding Node, and keeps server pure-Python / client pure-JS.

1. **Plane-relative anchor module** (pure, testable like `room-snap`): author (pose + local planes → anchor)
   and solve (anchor + local planes → pose), with the weighted-LS position solve, gravity-up + wall-yaw
   orientation, weighting, degeneracy handling, and robust fallback — **fully documented in code**. Ship
   **golden test vectors** alongside it (§13.1) so the Python port stays in lockstep.
2. **Local render of real surfaces + apply-gate** (client): render surfaces from the live capture (ids/
   styling by id); skip un-moved surfaces (no pop). `#world-root` → identity.
3. **Free content / skybox / grounded via anchors** (§5 b–d), authored by the owner, solved per client.
4. **Server = model + seed + anchors:** stop broadcasting per-capture geometry; ingest only structural
   changes (§7); serve model + seed + anchors on join. Retire `_static_frozen` / #2.
5. **Missing-surface recovery** (§5.2) + its `[recover]` logging.
6. **Plane-relative avatars** (§5.1): stream anchors (with hysteresis), re-solve per receiver.
7. **Server-side solver** (Python port, §13.1): route pose-relative queries (`view_relative`, etc.) through
   the plane-relative solve against the seed.
8. **`--square-walls on|off`** (§9) for A/B.
9. *(later)* render interpolation for genuine local moves; guest-proposes-surface; consensus seed (§7.8).

Steps build independently: (1) is a pure module with tests; (2) makes one headset render its own geometry
pop-free; (3)–(7) deliver the shared-model / local-geometry architecture; (8) enables the squaring A/B.
