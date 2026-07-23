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
| Surface **set** (ids, semantics) — incl. doors/windows/wall-art | ✅ | **detects & renders each locally** (headset-first) |
| Per-surface **styling** (material/colour/texture/visibility) | ✅ by id | applies by id to local geometry |
| On-surface **content** (photo, object on a wall/shelf) | ✅ by id, surface-relative (§5a) | rides its **local** host surface |
| **Free** content, skybox, and (streamed) **avatar** poses | ✅ as **plane-relative anchors** | **re-solved** against local planes |
| **Seed** (reference constellation + each surface's plane-relative anchor) | ✅ — **also the server's own solver geometry** | registers against it; **recovers missing surfaces** from it (§5.2) |
| Exact surface **pose/extent** (any detected surface) | ❌ never shared | ✅ each client's own live estimate |

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

### 4.2 Orientation — per-wall quaternion votes, averaged

Orientation is **3 rotational DOF**. The unifying idea: express the entity's rotation **relative to the two
things each client measures precisely and locally — gravity and the walls** — so any client can rebuild it
from its own geometry and it transfers across the non-rigid map. Concretely we do this the same way the
position solve does: **each reference wall casts one independent vote for the full orientation, and we
average the votes** (over-specified, redundant, robust to a missing or noisy wall).

First, be exact about **gravity**: it is **just a known world direction — down/up** — read from the headset
IMU and confirmed by the level floor (the floor normal). It is *not* a rotation; it's a precise,
already-shared reference **axis**.

**A wall's frame `W_k`** (`wallFrame` in client/plane-anchor.js) is the rotation whose local **+Z points
along that wall's outward normal** and local **+Y is up** (gravity). It is **entity-independent** — any
client rebuilds the *same* `W_k` from that wall's normal + gravity — which is the whole trick: it carries the
wall's heading, so anything stored relative to it rides the wall.

- **Authoring** — for each reference wall `k`, store one quaternion **vote**
  `rel_k = W_k⁻¹ · q_entity` — "the entity's orientation *as seen from that wall*." It strips out the wall's
  own heading, leaving only how the entity is turned relative to the wall. Being a quaternion it carries all
  three axes at once — **no yaw is ever extracted**, so there is no gimbal/degeneracy even when an object
  points straight up or down (the trap behind A-Frame's YXZ euler-order gotcha).
- **Solving** — for each wall the client actually has, rebuild `W_k′` from **its own** detected normal and
  reconstruct a candidate `q_k = W_k′ · rel_k`. Because the wall's (possibly shifted) heading flows through
  `W_k′`, the recovered orientation **tracks that local wall** — which is exactly why it survives the
  non-rigid map (it's tied to a wall, not to any absolute frame).
- **Average the votes** (`averageQuat`) — every present wall independently reconstructs the entity's full
  orientation; average them and normalise. Same over-specification as the position least-squares: one noisy
  wall gets diluted, and a **missing** wall just drops a vote instead of breaking the solve. Detail: `q` and
  `−q` are the *same* rotation, so each vote is first flipped into the first vote's hemisphere (else the sum
  could cancel); the normalised linear mean is then accurate because the votes cluster tightly (they all
  describe one entity).

**Placement mode (§5) decides what we keep of the averaged result** — this is where orientation and position
tie together:

- **Grounded (default) → yaw-only.** A thing resting on the floor **cannot lean**, so we keep only the
  rotation *about gravity* and force `pitch ≡ roll ≡ 0`. `twistAbout(q, up)` (the twist half of a
  swing-twist decomposition) extracts precisely that component and discards any residual tilt wall noise
  introduced — pinning upright *structurally* is more robust than trusting a measured near-zero tilt that
  could drift. *(Note: rotation-about-up is a **wall** DOF, not a gravity DOF — the easy thing to mis-group;
  gravity fixes the two tilts, the walls fix this heading.)*
- **Free / world-placed → full quaternion.** An object that genuinely rotates (real pitch and roll) keeps
  the averaged quaternion **as-is** — heading + pitch + roll, all first-class, carried together with no yaw
  extraction and so no gimbal.

Why quaternions rather than stored pitch/yaw/roll angles: no gimbal lock, no euler-order ambiguity (recall
A-Frame renders **YXZ**), and it's exactly what three/A-Frame hand us (`object3D.quaternion`). *(Conceptual
note: a single wall's `W_k` is itself a valid gravity-up reference frame; using **all** the reference walls
and averaging is just the robust, over-specified generalisation — it avoids having to define one shared
"forward," which is degenerate in a rectangular room where the walls face four different ways.)*

## 5. Placement modes

**Every placed entity declares a `mode` in the world model, and the mode fixes *both* its position solve and
its orientation form** (§4.2) as one consistent choice. This is the single knob that keeps geometry and
orientation aligned:

| `mode` | Position | Orientation | Typical |
|---|---|---|---|
| **grounded** *(default)* | XZ multilaterated (§4.1); **Y snapped to local floor** at that XZ | gravity-up + robust wall-yaw; **pitch≡roll≡0** | a model set on the floor/table |
| **free** | full 3-D multilateration incl. floor-height (§4.1) | **averaged per-wall quaternion** (§4.2), kept whole — heading+pitch+roll | a prop hanging/tilted in mid-air, a rotating object |
| **on-surface** | rides its host **surface** if that surface is detected locally; else in-plane (§5a) | host surface's normal + wall-yaw | photo/art hung on a wall or shelf |
| **skybox** | none | wall-yaw only | environment dome |

`grounded` is today's default and covers most placements; `free` is the explicitly-authored case for content
that leaves the floor or rotates arbitrarily. The world model stores `mode` per entity so authoring and every
client agree on how to solve it.

> **Detected surfaces are headset-first.** Walls, floors, **and doors / windows / wall-art** are all
> *detected WebXR surfaces*: each client renders them from **its own live capture** (matching its own
> passthrough exactly), applying only the shared **id + semantics + styling**. They are *not* placed by the
> server. An inset's hole-cut and `snapInsets` nudge are computed **locally** from the local detection. A
> client only falls back to a reconstructed pose when it **didn't detect** that surface — see §5.2, which is
> the *sole* consumer of in-plane placement for these.

- **(a) On-surface content.** Full-surface **textures** need no positioning (they span the surface).
  **Positioned** content (a hung photo, a placed object):
  - **On a *detected* host surface** (wall-art frame, a shelf): it **rides that surface's local pose** — the
    host is headset-first, so the content inherits exact passthrough alignment. No separate placement math.
  - **At an arbitrary bare-wall spot** (a photo on blank wall, with no detected surface of its own): placed
    **in-plane**, since there's nothing to detect. In-plane coordinates: **vertical** = height above the
    **floor** (gravity-precise); **lateral** = distance from the **corner** where the wall meets an adjoining
    wall (a plane∩plane intersection → stable, unlike the wall's own ends), over-specified with **both**
    adjoining corners and averaged. *This is the same math as §4/§5.2 with the host wall as a reference (the
    host wall pins it into the plane, adjacent walls fix the lateral, floor fixes height) — no separate code
    path; the corner is implicit in the wall references.*
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
   slightly), so stream the head's **per-wall quaternion votes** (`rel_k`, §4.2) — full-orientation, no yaw
   extraction, gimbal-safe when someone looks straight up/down. **Stream that anchor** (ids + signed
   distances + per-wall `rel_k`) over presence instead of a raw pose; each receiver averages the votes.
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

Every detected surface is rendered **headset-first** (§5, "Detected surfaces are headset-first"). This
section is the **only** exception: a **non-wall** surface (furniture, wall-art, a door, a window, a
floor/ceiling…) that exists in the seed but is **absent** from a client's live capture — the client missed
it. It's also the **sole consumer** of the in-plane / anchor placement for insets. Then:

- Once the client's map has **stabilised**, for each seed surface with **no local match**, the client
  **requests that surface's plane-relative anchor from the server** and **re-solves it against its own local
  walls** (§4) → the surface is recreated at a spot consistent with the client's *own* geometry (not a rigid
  guess). Content on it then rides that recovered surface.
- **In-wall surfaces (door/window/wall-art) RIDE their recorded host wall.** Because the authority records
  each inset's `host_wall` (§5.2, persisted → `meta.host_wall`), recovery applies the inset's *seed
  offset-from-its-wall* onto the wall's **local rendered** pose: `inset_local = wall_local · (wall_seed⁻¹ ·
  inset_seed)`. This preserves the along-wall position **and** height exactly — a free multilateration
  under-constrains the along-wall axis for a mid-wall inset with no perpendicular wall nearby, which showed
  up as >10 cm lateral shifts. `snapInsets` then only nudges the perpendicular onto the wall plane. If the
  host wall isn't recorded or isn't locally rendered, recovery falls back to the ordinary `solveAnchor`
  multilateration (client/plane-anchor.js).
- **Log + console** each recovery for awareness/debugging, e.g.
  `[recover] surface window_9 reconstructed (ride wall=_a1b2c3)`.
- If the client later **detects** that surface for real, it **switches back to its own live pose**
  (headset-first wins the moment detection is available; id re-inherited via `matchRef`).
- **Walls are excluded** — they're the *basis* of the anchor system (a wall is a plane, not a point, and its
  absence perturbs everyone's nearest-wall sets). Missing-wall recovery is a separate, harder problem —
  deferred.
- **Inset→wall association is a recorded fact, for CAPTURED insets too.** `snapInsets` snaps an inset to its
  recorded `host_wall` by id; captured insets now carry that record from the seed (not just recovered ones).
  This is the reliable way to tell the two **near-coincident, anti-parallel faces of a room-partition** apart:
  when `snapInsets` must *derive* the host (fresh room, unrecorded) it falls back to nearest-parallel-within-
  width, which can't disambiguate a partition — and it **can't** use a co-facing rule to break the tie,
  because an inset's live normal may be **inward** (~180° from its host; see `matchRef`). See the intermittent
  "image behind its wall" bug in `docs/wall-art-45-flip.md`.

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

## 9. Wall squaring — removed

`squareWalls` snapped a wall's facing to a width-weighted orthogonal **grid** (only within ~12° of square).
It was a **vestige** of the pre-local-first design, when the **seed was the rendered geometry** and squaring
cleaned it up so walls met at right angles. Under local-first that job is gone — every headset renders its
**own raw capture**, so the seed is never drawn.

What remained was a hazard: squaring was applied ONLY to the posted **seed**, never to the local render, so
the shared model was **inconsistent with the raw geometry every client uses**. Everything that relates
raw-local to the seed inherited a systematic error equal to the squaring delta:

- **Anchors (§7c):** the server authors against the (squared) seed walls; clients solve against raw local
  walls. "Same surfaces" is the anchor's premise — squaring one side breaks it (it rotates a wall normal up
  to ~12°, and anchors use perpendicular distance to that normal, so a point a couple metres along the wall
  shifts up to ~0.4 m).
- **Registration:** guests match a raw capture against a squared reference — a systematic offset.
- **Recovery (§5.2):** a recovered inset rides its local wall using the seed pose as its offset reference;
  squared-seed vs raw-local injects the delta into the recovered position.

**Decision: removed entirely.** `squareWalls` and its `--square-walls` knob idea are gone; the seed is built
with the **same** treatment as the local render (`joinCorners` only — corner-gap closing, applied to both
paths — then `snapInsets`; **no** squaring). The shared model now matches the raw geometry every headset
draws — like-to-like across rendering, registration, anchors, and recovery. The function lived in
`room-snap.js`; it's in git history if the per-capture angular jitter it denoised ever proves worth
revisiting (it would need to run on the local render too, to stay consistent).

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
- **Head pose is still broadcast in F_ref.** With `#world-root` at identity, the client can't read the shared
  frame off world-root anymore, so `presenceTick` applies the registration transform `T` directly
  (`T · camera_F_track`) to send the head pose in **F_ref** — the frame the server's `gaze`/`view_relative`
  ("in front of me") and the world model use. (A single rigid `T` is fine for a *placement decision*; the
  placed content is then re-solved plane-relative for rendering, §5. Avatars move to fully plane-relative
  poses in step E.)
- **No migration — start fresh.** Existing persisted worlds/spaces (`.cache/worlds/<scope>/<name>.json`,
  `.cache/spaces/<user>/<name>.json`) predate anchors and the mode field; they are **test worlds, cheap to
  recreate.** We will **delete them and rebuild** rather than write porting code. The new model needs no
  back-compat path; the first authority to capture a space re-mints its seed + anchors under the new schema.

## 12. Decisions & remaining open questions

**Decided:**

- **Large-move threshold — keep**, and **print to console + log** when it fires (§7.4).
- **Parallel-wall degeneracy — fall back to a farther perpendicular wall** (extend the reference set), log it
  (§4.1).
- **Placement modes — `grounded` (default) and `free` both supported**, mode declared per entity in the world
  model, mode drives position + orientation together (§5, §4.2).
- **Director geometry queries run against the seed** — accept the seed's approximation for placement
  *decisions*; rely on **future seed improvements** (§7.8, not yet planned) to sharpen it if ever needed.
- **Detected surfaces are headset-first** — walls, doors, windows, wall-art all render from each client's
  own live capture; the server never places them (§5). In-plane/anchor placement for insets is used *only*
  to **recover** a surface a client didn't detect (§5.2).
- **Missing-surface recovery — log it** (`[recover] …`, §5.2).
- **Avatars — stream anchors with hysteresis, solve per receiver** (§5.1).
- **Wall squaring — removed** (§9): a pre-local-first vestige; it only touched the seed, making the shared
  model inconsistent with the raw geometry every headset renders. `squareWalls` + the `--square-walls` knob
  are gone.
- **Nothing in absolute coordinates** — the design principle; every item is on-surface, grounded, free, or
  skybox (verified during build — see Build-time verifications below).
- **Solver code — port to Python, pin JS + Python with shared golden vectors** (not Node-in-server, §13.1).
- **In-plane reference — use corners** (plane∩plane), **over-specify with both adjoining corners and
  average**; realized by the general anchor with the host wall as a reference (no separate code path).
  **Recovery-only** for detected insets (§5.2); also the primary for arbitrary bare-wall content (§5a).
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
pure math (a 3×3 weighted least-squares for position + per-wall quaternion vote averaging for orientation);
the parity-test contract is far cheaper than embedding Node, and keeps server pure-Python / client pure-JS.

1. **✅ Plane-relative anchor module** (pure, testable like `room-snap`): author (pose + local planes →
   anchor) and solve (anchor + local planes → pose), with the weighted-LS position solve, gravity-up +
   wall-yaw orientation, weighting, degeneracy handling, and robust fallback — **fully documented in code**.
   Ships **golden test vectors** (§13.1) so the Python port stays in lockstep. (`client/plane-anchor.js`)
2. **✅ Local render of real surfaces + apply-gate** (client) — *Milestone A, validated on-device
   (2026-07-20): surfaces sit accurately on the real walls in passthrough, stable, no pop; only real
   refinements settle through over time.* Apply-gate (`world-model.js surfaceMoved`, tested), tunable via
   `--apply-tol-*`; every client renders its **own** live capture in F_track via `_renderLocal`,
   `#world-root` → **identity** for captured rooms (void worlds keep the canonical parking); owner/guest
   render paths **unified** (both render locally; only the owner posts); server real-surface ops are ignored
   once `localRenderActive` (a desktop viewer, which never captures, still renders the server's surfaces).
   *(Getting into a captured room depends on the GPS-gated space-selection flow; a slow/missed fix now keeps
   the view blanked-to-passthrough and retries instead of dropping into the void world — fixed.)*
3. **✅ Free content / skybox / grounded via anchors** (§5 b–d). `_placeContent`, each capture, handles all
   four modes: **free** content multilaterates its F_ref pose against the local walls (§5b); **grounded**
   content (`meta.placement:"grounded"` — set on dropped models, which auto-sit on the floor) snaps Y to the
   **local** floor + stays upright (§5c); **on-surface** content (`meta.on_surface`) **rides its local host
   surface** — offset-from-host (F_ref) re-applied to the host's local pose (§5a), no drift; **skybox** is
   oriented by the registration yaw (`_pinSky` applies `T⁻¹`'s rotation) so it holds a consistent
   room-relative heading with `#world-root` at identity (§5d). Head pose broadcast in F_ref (via `T`) so
   `view_relative`/"in front of me" resolves. *Validated on-device: models land where you look and hold;
   on-surface photos stay pinned.* **Deferred → step 7:** persisted anchors (content is re-authored
   client-side each capture from the F_ref pose today, not stored as an anchor server-side).
4. **✅ Server = model + seed:** styling-by-id on local surfaces ✅ (a surface keeps its shared material);
   `ingest_room` no longer broadcasts geometry — it updates the stored SEED only (add / meaningfully-changed
   / prune-absent) and broadcasts just env + on-surface re-anchors; the time-based **establish/freeze is
   retired** (`_ESTABLISH_SECS`/`_room_capture_start`/`_STATIC_SEMANTICS`, `--establishment-period` all
   removed — clients render locally, so there's nothing shared to stabilize). **Structural-only ingest
   (§7.4)** ✅ — the seed updates only on a semantic reclass, an opening add/remove, or a LARGE move/rotate/
   resize (`_surface_structural_change`, 0.5 m / 20°); per-capture cm-drift no longer churns the seed
   (previously ~¼ of surfaces were rewritten every 2 s). **Client post-gating** ✅ — the owner keeps an
   authoritative `_known` set (seeded from the persisted seed on entry) and POSTs **only** on a structural
   change (new / confirmed-removed / large-move / boundary), so a settled room sends **no `/room` traffic
   at all**; removal-confidence lives on the client (3-capture debounce) and the server prunes on first
   absence. *(Remaining server-side work is consolidated in step 7.)*
5. **✅ Missing-surface recovery** (§5.2) — `_recoverMissing`, each capture: for every seed surface
   (`docSurfaces`) absent from the live capture and not a wall/floor (the anchor basis), author its anchor
   from its F_ref pose against the seed walls and re-solve against the LOCAL walls, then fold it into the
   render set (so it draws and can host on-surface content); logs `[recover] surface … reconstructed`. A
   recovered **inset** (door/window/wall-art) then runs through `snapInsets` like a captured one — so the
   anchor gives the plane-relative position and the surface is **snapped co-planar to its wall** (projected
   onto the wall plane, not left at the raw anchor depth). The **inset→wall association is a recorded fact**,
   not re-guessed by proximity: the authority's `snapInsets` derives each inset's `hostWall` once and it's
   persisted (`meta.host_wall`), so recovery snaps the inset to THAT wall by id. Once the client detects it
   for real, the live capture wins. **Test knob
   `--drop-surface SEMANTIC|ID[,…]`** (comma-separated): the
   client pretends it didn't capture matching surfaces (kept in the seed, omitted from the local render), so
   recovery is exercisable with one headset.
6. **✅ Plane-relative avatars** (§5.1) — each source streams its head as a plane-relative anchor
   (`presenceTick` authors the head against its OWN local walls, free-mode orientation) alongside the F_ref
   pose; each receiver re-solves that anchor against ITS OWN local walls (`setAvatar`) → the avatar lands on
   the same real walls the receiver sees, no shared-frame offset. Falls back to the F_ref pose for a desktop
   receiver / void world (no local walls). **Deferred:** wall-set hysteresis (the nearest-3 set can flip as
   the source walks — add if avatars jitter; the over-specified solve smooths it for now).
7. **Server-side anchors & solver** (§13.1) — the deferred server-side pieces:
   - **✅ a. Python solver port** — `conjure/plane_anchor.py` is a 1:1 port of `plane-anchor.js` (weighted-LS
     position + per-wall quaternion vote), pure-stdlib, dict I/O matching the seed JSON. Pinned to the
     **shared** golden vectors: `tests/test_plane_anchor.py` checks the Python side against
     `tests/js/fixtures/plane-anchor-golden.json` (the same file the JS suite uses) to 1e-6 m / 1e-5 rad, so
     the two implementations can't silently drift. The server can now solve poses against the seed.
   - **b. Server-side pose-relative queries** — route `view_relative` / "the wall I'm looking at" through
     that solver against the seed (today they use the raw seed pose, which is approximate).
   - **c. Persisted anchors** — store each content entity's **plane-relative anchor** in the shared model
     (authored once, server-side) instead of the client re-authoring it from the F_ref pose every capture
     (§3, §5). Removes the client's dependence on its `docSurfaces` copy of the host/seed pose for
     placement and makes anchors first-class in persistence. *(Deferred here from steps 3 & 4.)*
     - **✅ (A) server authors + persists `meta.anchor`** — `_seed_planes` builds the seed's floor+walls in
       the client's plane convention; `_content_anchor` authors the anchor via `author_anchor` and
       `_model_entity_op` stamps it onto each placed model (logged `[anchor] authored …`). **No behavior
       change yet** — the client still uses the F_ref pose; this de-risks the solver on real seed geometry.
     - **✅ (B1) client consumes the persisted anchor (free/grounded)** — `_placeContent` solves a placed
       model's `meta.anchor` against the LOCAL walls directly, instead of re-authoring from its `_frefPose`
       against `this._ref` every capture. Legacy content (no `meta.anchor`) still falls back to the old
       author-from-F_ref path. Debug HUD shows `anchored N/M`.
     - **(B2) persist the on-surface offset** — store an on-surface photo's offset-from-host (host-local)
       server-side at `place_image` time so the client rides a STORED offset rather than recomputing it from
       its `docSurfaces` copy of the host seed pose — this is what removes the `docSurfaces` dependence and
       retires the `T⁻¹` fallback (§5a).
8. **~~`--square-walls on|off`~~ — dropped** (§9): squaring was a pre-local-first vestige (it only touched
   the seed, making the shared model inconsistent with each headset's raw render), so it was **removed
   outright** rather than made toggleable. No A/B needed — the decision it would have informed is made.
9. *(later)* render interpolation for genuine local moves; guest-proposes-surface; consensus seed (§7.8).

Steps build independently: (1) is a pure module with tests; (2) makes one headset render its own geometry
pop-free; (3)–(7) deliver the shared-model / local-geometry architecture; (8) is resolved by removal.
