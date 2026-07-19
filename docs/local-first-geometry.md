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
| On-surface **content** (photo→wall_3+offset; texture) | ✅ by id, surface-relative | renders on **local** surface |
| **Free** content, skybox, and (streamed) **avatar** poses | ✅ as **plane-relative anchors** | **re-solved** against local planes |
| **Seed** (reference constellation + each surface's plane-relative anchor) | ✅ | registers against it; recovers missing surfaces from it |
| Exact surface **pose/extent** | ❌ never shared | ✅ each client's own live estimate |

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
  and fall back for that axis (reach for a farther perpendicular wall, or keep the seed estimate) and
  **log** it.

### 4.2 Orientation — gravity-up + wall-yaw + residual

- **up ← gravity** (the client's floor normal). The level-floor trust gate makes this a *precise, already-
  shared* axis, so it fixes 2 of 3 rotational DOF for free — no wall reference, no error.
- **yaw ← wall-relative.** At authoring, store the entity heading's offset from each reference wall's
  normal-yaw, `Δψ_k = ψ_entity − ψ_wall_k`. At solve, each client wall proposes `ψ_entity = ψ_wall_k′ + Δψ_k`;
  combine by weighted **circular mean** over the reference walls. (The only DOF that needs walls.)
- **residual tilt** (only if the entity isn't world-upright — rare): the leftover rotation from the
  (gravity-up, authored-yaw) frame to the true orientation, stored as a quaternion and applied verbatim
  (identity for upright content). *This single residual is what a separate "elevation" + "roll about axis"
  parameterisation would encode — folded into one term.*

## 5. Placement cases

- **(a) On-surface → local surface.** Textures, hung photos, wall art: anchored to a surface **id +
  offset**, rendered relative to the client's **local** surface. Exact passthrough match.
- **(b) Free / world-placed → plane-relative anchor (§4).** A model floating in the room resolves its
  position + orientation against the client's local planes. *This is the independent-3D-model case.*
- **(c) Grounded models → floor-anchored.** A model resting on the floor/table: solve its **XZ** as a free
  point (§4.1) but re-seat its **Y to the local floor** at that XZ, so it never floats/sinks against a
  client's own floor. (Grounded = surface-anchored in Y, plane-relative in XZ.)
- **(d) Skybox → yaw-only (§4.2).** No position; store just the wall-relative **yaw** so every client faces
  the scene the same way.
- **(e) Avatars → per-tick streamed plane-relative anchor (§5.1).**
- **Fallback — undetected surface → recovered plane-relative anchor (§5.2).**

### 5.1 Avatars (spelled out)

Avatars are the moving, high-frequency case; they use plane-relative anchors, computed at the **source** and
re-solved at each **receiver**:

1. **Source (the user being shown), every presence tick (~10 Hz):** compute the head's plane-relative anchor
   in the source's own F_track — floor height + signed distances to the source's **nearest ~3 walls (by
   shared id)**; head **yaw** as offsets from those walls' normals; head **pitch** relative to **gravity**
   (gaze up/down); roll ≈ 0. **Stream that anchor** (ids + distances + yaw offsets + pitch) over presence
   instead of a raw pose.
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
   distinct from the cm-level local apply-gate tolerance.*
5. **Styling / content edit** — director actions (authored, always shared).
6. **Boundary change** — floor polygon / height.
7. **Seed lifecycle** — first establishment mints the seed; `/room/realign` refreshes it; optionally an
   occasional autosave refreshes the seed to a recent snapshot (kept infrequent so it doesn't thrash).

> Open for your call: the **large-move threshold** (§7.4) and whether to include the **periodic seed
> refresh** (§7.7).

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

**Decision:** wire `--square-walls on|off` and A/B it. Default **ON** initially (avoid an untested jitter
regression); OFF is well-motivated under local-first and is the A/B priority — flipping the default is a
one-liner once the data decides.

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

## 12. Open questions

1. **Large-move threshold** and **periodic seed refresh** (§7).
2. **Parallel-wall degeneracy** (§4.1) — hallways: confirm the detect-and-fallback behaves and logs.
3. **Grounded-model Y re-seat** (§5c) — reads the local floor cleanly, no jitter.
4. **Director geometry queries** (`view_relative`, "the wall I'm looking at") run against the **seed**
   (approximate) — fine for placement *decisions*; confirm nothing needs local precision server-side.
5. **`--square-walls` default** — decide after A/B (§9).
6. **Content authored in absolute coordinates** — audit that *nothing* is placed in raw F_track/F_ref; every
   item is on-surface, plane-relative, or grounded.

## 13. Implementation sketch (incremental)

1. **Plane-relative anchor module** (pure, testable like `room-snap`): author (pose + local planes → anchor)
   and solve (anchor + local planes → pose), with the weighted-LS position solve, gravity-up + wall-yaw
   orientation, weighting, degeneracy handling, and robust fallback — **fully documented in code**.
2. **Local render of real surfaces + apply-gate** (client): render surfaces from the live capture (ids/
   styling by id); skip un-moved surfaces (no pop). `#world-root` → identity.
3. **Free content / skybox / grounded via anchors** (§5 b–d), authored by the owner, solved per client.
4. **Server = model + seed + anchors:** stop broadcasting per-capture geometry; ingest only structural
   changes (§7); serve model + seed + anchors on join. Retire `_static_frozen` / #2.
5. **Missing-surface recovery** (§5.2) + its `[recover]` logging.
6. **Plane-relative avatars** (§5.1): stream anchors (with hysteresis), re-solve per receiver.
7. **`--square-walls on|off`** (§9) for A/B.
8. *(later)* render interpolation for genuine local moves; guest-proposes-surface.

Steps build independently: (1) is a pure module with tests; (2) makes one headset render its own geometry
pop-free; (3)–(6) deliver the shared-model / local-geometry architecture; (7) enables the squaring A/B.
