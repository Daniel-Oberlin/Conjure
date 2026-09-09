# Spaces ↔ geometry — backlog

Unfinished work, future directions, and known problems for capture, registration, placement and
stability. The current state is [`docs/specs/spaces-geometry.md`](../specs/spaces-geometry.md); the
reasoning behind rejected alternatives is [`docs/decisions.md`](../decisions.md). The debugging
campaigns that produced most of these — including what has been **ruled out** — are in
[`docs/investigations/`](../investigations/).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems

### A surface drops out and returns uncoloured — **cause found 2026-08-31, fix shipped**

Full campaign in [`investigations/surface-churn.md`](../investigations/surface-churn.md). In short: the
churn instrumentation fired for the first time and named it. Both replacement walls logged
`why: matcher` — the Quest **did** emit the planes; our own matcher rejected them, two of three on
perpendicular offset by 14–21 mm against the 150 mm tolerance. The originals were then pruned, destroying
three director-set colours (`#4B0082`, `#4f4f4f`, `#000000`).

The cause was **space load**, not the headset and not the floating-room fault (that one is vertical; this is
horizontal). Ordering by the client clock: sessions whose first capture held 4 and 16 planes of 58 both
churned; the one that held 58 did not. Registration locks at 30% coverage, and a frame solved from a third
of the space was ~17 cm out in x/z — past the identity tolerance. Fixed by the **load gate** (spec §4.0a).

**A design assumption this falsified**, worth keeping: `matchWall`'s comment says a tight tolerance is safe
because *"a missed match only mints a recoverable duplicate."* It is not recoverable — the old id goes
absent, the debounce prunes it, and the server deletes the entity **and its material**. Still open: whether
styling should survive a prune (keyed by id, so a returning or re-minted surface inherits it), which is the
deeper fix for "loses its colouring" whatever the reason the id churned.

### The original report, for the record

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

### A room's floor floats — **diagnosed, device-side; no fix on main**

**Status:** cause established. Detected and measured; **not corrected.** The full campaign, including four
attempted fixes and why each was reverted, is in
[`investigations/raised-floor.md`](../investigations/raised-floor.md) — read it before proposing anything.

The Quest anchors one room's stored entity high, and every plane in that room rides with it. Proven by
known-equal surface pairs: `floor_32` = `floor_8` (one continuous wooden floor) and
`ceiling_13` = `ceiling_25`. The room translates as a **rigid** unit — room heights are preserved — so it is
a mis-anchored space entity, not a distorted capture. The tracking frame is sound and registration is clean
throughout. **A Room Setup re-scan does not clear it.**

**It is not fixed to one room, and its size is not bounded.** Observed:

| date | room | displacement |
|---|---|---|
| 2026-08-31 morning | bedroom (`floor_32`) | ~90–104 mm |
| 2026-08-31 afternoon | **living room** (`floor_8`) | **~276 mm** |

That second reading matters twice over: it is 3× the first, and it moved to a different room. Any threshold
calibrated on one sample is calibrated on nothing — a mistake already made once (see the investigation's
guard 3).

**The visible consequence** is placed content: the floor rises, content does not follow it, and models on
that floor end up buried. Unverified inference: all three floors enter the anchor solve as separate floor
planes, so an anchor resolving against the wrong floor lands ~190 mm out. Worth confirming — **it would be a
bug on main independent of any correction, and fixing it would un-bury the content even while the floor
stays wrong.** Probably the highest-value next step here.

**A correction exists on `feat/fix-floating-rooms`, unmerged.** It would refuse today's fault anyway (its
offset ceiling is 0.15 m). See the investigation for what is on the branch and how to resume it.

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

## Planned — rooms as a first-class unit

**Status:** proposed 2026-09-07, **reviewed the same day — nothing built.** Branch
`feat/room-segmentation`. The four open questions are settled below; the *"room" rename* is a
prerequisite and lands on `main` first.

[`specs/spaces.md` §1](../specs/spaces.md) says it plainly: *"Nothing in the record models a 'room' as a
unit."* This plan gives it one. Two wants turn out to share a single primitive — a registration that stops
pretending the space is rigid, and a director that can say "the kitchen" — so the segmentation is the
foundation and both consumers hang off it independently.

### Why piecewise-rigid registration

The spec's §1 is the motivation: the Quest's map is locally non-rigid by up to ~9 cm, concentrated in one
region, which **no single rigid transform reconciles**. Registration nonetheless solves exactly one
transform for the whole space.

The raised-floor investigation says something sharper than "rooms move relative to each other": the Quest
anchors one room's stored entity high and **every plane in that room rides with it rigidly**, room heights
preserved. So the displacement's *unit* is the room and its *shape* is rigid — which is precisely the
assumption a piecewise-rigid registration needs. The observed failure mode and the proposed model agree,
and that agreement is the whole case.

It also rules out the smooth alternative. A locally-weighted or thin-plate warp needs no segmentation and
degrades gracefully, but it would **blur a discontinuity that is physically real**. Rooms are the right
partition because the physics is piecewise — not because segmentation is convenient.

**What it buys, precisely.** `Tmat` drives no render transform in a captured space (§2.1), so this is not a
rendering fix: a raised floor still renders raised. The payoff is **identity correspondence** — §4.3's
linchpin. The measured ~9 cm regional non-rigidity already consumes 60% of `matchWall`'s 150 mm
perpendicular budget.

Worth stating before building it: **this fixes no live bug.** The one churn case on record was traced to
room load and is already fixed by the load gate, and `churn.*` has never fired since. This is margin being
spent silently, plus a measurement we do not currently have.

Anchor *solving* is untouched — it is F_track-native and never passes through `Tmat`. Anchor *authoring*
does convert through the frame (`toRef`, §5.4a), so that is the one place to use the room-refined transform.

### The design: segment the seed, inherit the labels

The naive shape — segment the live capture *and* the seed, then match rooms to rooms — reintroduces a
correspondence problem one level up, and segmentation instability is the same class of bug as the
inset-identity churn of §6.1. Instead:

1. Segment the **seed** only. Persist the labels.
2. Register globally, **exactly as today** — yaw + x/z, unchanged.
3. Live surfaces **inherit their room label through the match they already got.** No live segmentation at
   all.
4. Group matched pairs by label; refine each room as a bounded delta off the global transform.
5. Re-match identity per room with the refined transform. This is the payoff.

Coarse-to-fine, never independent per-room solves — for one specific reason. **A single room is maximally
symmetric**: four walls, opposite pairs equal, which is exactly §4's known ambiguity where the yaw vote can
lock 180° off. Whole-space asymmetry is what saves the global solve; register each room alone and every room
gets the worst case. The acceptance gate inverts too — `MIN_COV` 4 against a six-surface room is a 0.67
fraction where the global gate is 0.3. Refining off a global yaw never re-solves yaw from scratch, so both
problems vanish rather than being tuned around.

**The refinement must be strictly non-degrading.** Skip a room below ~4 matched pairs; require the
refinement to *reduce* that room's residual; reject a delta past ~25 cm as a mis-fit rather than trusting
it; always fall back to the global transform. A room that cannot be refined must not be a room that fails to
register.

**`y` is fitted per room, and `levelDeviation` keeps comparing raw heights.** These are not in tension,
which is worth spelling out because the first instinct is that they are. The invariant §10.2 rests on is a
**consumer** rule, not a representation rule: `levelDeviation(live, seed, basisIds)`
(`client/world-model.js`) takes raw height scalars and never touches a transform, so a per-room refinement
carrying y cannot reach it unless corrected heights are deliberately routed in.

Including y is likely **better for the detector**, not merely harmless. The census skips any live surface
with no seed counterpart (`if (!s) return;`), so a floor displaced far enough to fail identity matching
drops out of it entirely — losing precisely the surface the check exists to report — and below three
comparable surfaces it returns nothing at all. A y-aware fit recovers that correspondence.

**y is nearly free, and separately constrained.** It is not one more DOF in the same solve: horizontal
surfaces constrain y directly and strongly — a floor is a pure y constraint — while verticals constrain x/z
and yaw and say nothing about y. So it is an independent 1-DOF fit over the horizontals, costing the
existing solve no conditioning. It takes its own guard accordingly: **a room with no captured floor or
ceiling has no y constraint at all, so skip y there rather than fit it to noise.**

**The two rules that must hold.** `levelDeviation` is never fed heights that passed through a refined
transform. And once y is used to *correct* anything, the detector keeps firing on a corrected room —
correctly, since the device data is still displaced — so the log must distinguish *the fault* from *the
residual after correction*, emitting the fit's y beside the detector's deviation. Two independent estimates
of one displacement are a cross-check, but only when labelled as such.

This also makes it the better input to the floating-room correction: the unmerged corrector on
`feat/fix-floating-rooms` is threshold-gated, and its open question — *does it still fire when the
displacement changes size?* — dissolves against a continuous estimate.

### Segmentation basis — the device already did it

Floor planes are the partition, and that is observation rather than assumption. This space's `floor_8` /
`floor_10` / `floor_32` are living / kitchen / bedroom, and `ceiling_25` / `ceiling_21` / `ceiling_13` pair
off correctly — matching the ground truth recorded above exactly. Since the raised-floor evidence says the
displacement follows the Quest's *own* room entities, segmenting by floor plane recovers the partition that
actually moves rigidly.

- walls and ceilings → their floor by `covers()` (`space-snap.js:635`) — the same footprint test `sealWalls`
  (`:666`/`:672`) and `heightCensus` (`:981`) already use to decide which room a surface is in. The
  multi-room-correct machinery is built; it has only never been named.
- insets → the room of their `meta.host_wall`, a recorded fact (§6.1).
- leftovers → an explicit `unassigned` bucket. Never a crash, never a guess.

**Over-segmentation is safe; under-segmentation hurts.** Two sub-rooms that genuinely co-move refine to
equal deltas and cost nothing, so bias toward splitting.

### Room identity resolves by set overlap

The load-bearing rule, and the one place this can destroy *user data* rather than a colour: a
re-segmentation matches new groups against existing rooms by **surface-set overlap**, never by segmentation
order. Get it wrong and the user's "kitchen" migrates to a different room after a rescan.

Tiered, most robust first:

1. **surface-set overlap** against the stored labels — robust to any one surface churning, because a room is
   ~20 surfaces, not one.
2. **the defining floor's id**, recorded on the room — the fallback if the labels are ever lost wholesale
   (see the branch-compatibility note below for why this tier exists).
3. **mint** a new room. Ids are monotonic and never reused.

Keying *only* on the floor id was considered and rejected as the primary key: a floor that churns would take
the room's name with it. Keying on segmentation order was rejected outright — that is the inset-churn
mechanism, one level up and with worse consequences.

Empty rooms keep their names. Cheap, names are precious, and it is a small hedge against the empty-capture
wipe in [`backlogs/spaces.md`](./spaces.md).

### Schema — the name is stored exactly once

The space document gains one member:

```jsonc
"rooms": {
  "room_1": { "n": 1, "name": "kitchen", "floor": "real_floor_10" },
  "room_2": { "n": 2, "floor": "real_floor_8" }     // `name` ABSENT until the user sets one
}
```

Real surfaces gain `meta.room` — a scalar room id. Doors and windows additionally gain `meta.leads_to`.

- The name lives **only** in `space.rooms[<id>].name`. Surfaces carry the id, never the string — the same
  shape as `surfaceStyles` keyed by id, and as `meta.host_wall` being a recorded reference rather than a
  duplicated fact.
- **The default is derived, not stored:** display is `name ?? f"room #{n}"`. `{"n": 2}` renders as
  "room #2" with no string on disk, so a rename cannot leave a stale default anywhere — the default was
  never written. This mirrors `meta.friendly_id`, which is likewise the number the user reads off a label
  and speaks back (§1.1 of [`worlds-surfaces.md`](../specs/worlds-surfaces.md)).
- **The space is the home, not the world.** A room name is a fact about the physical environment — the same
  kitchen in every world — so it belongs beside `boundary` and `geolocation`, not inside
  `spacePresentation`. This is the same reasoning that keeps `boundary` a *sibling* of `spacePresentation`
  rather than a member of it.
- Consequence, and it falls out for free: a name is a space write, so it is **owner-gated** by the existing
  rule. A guest cannot rename your kitchen.
- `meta.room` is authoritative for membership; `rooms[].floor` exists solely as identity tier 2.

**Integration point not to miss:** `meta.room` must join `_surface_changes`' aspect set, and a room
reassignment must **not** drag `position` along — the 2026-08-31 rule that a change to one aspect must never
rewrite the pose.

### Connectivity — doors know what they lead to

Per door inset: step ±0.5 m along the host wall's normal from the door centre and `floorUnder` each side.

- two rooms → `meta.leads_to` names the far one
- one room, nothing beyond → exterior (`leads_to: null`)

`floorUnder` is an x/z footprint test, so the probe height is irrelevant. This works whether the partition
was captured as one wall or as the two near-coincident anti-parallel faces of §6.1, and it does **not**
depend on the door having been captured twice — which is why it is preferred over reading the host wall's
room set or pairing duplicate door insets. Both of those were considered; both fail on a partition captured
as two walls, where each face knows only its own room.

Windows get the same probe free, which tells the director which walls are exterior — useful in its own right.

### Where each part runs

| Part | Where | Why |
|---|---|---|
| `segmentRooms(THREE, surfaces)` → groups + connectivity | **client**, `space-snap.js`, pure | shell geometry is JS-only (§12); reuses `covers`/`floorUnder`; unit-testable on `fixtures/golden-space.json` |
| room identity + name persistence | **server**, at ingest | pure set arithmetic against the stored labels — **no Python geometry port needed** |

Keeping the server free of geometry here is deliberate: §12's rule is that a server-side geometry query
ports to `conjure/space_snap.py` with a golden test, and this design avoids owing that.

### The director surface

`_real_surface_match` (`server.py:3297`) is the **single** target matcher — both `_surface_targets` (`:3310`)
and `_resolve_op_ids` (`:3316`) go through it, and it already handles a compound form (`wall 4`) via
`_SEM_NUM`. So room targeting is a one-function change and every surface tool gains it at once.

| Want | How |
|---|---|
| query room identities | `query_space()` groups its output by room and names each |
| which room am I in | the **client reports it in the presence tick**, using the `floorUnder` it already calls for `_markProbe` — free, and avoids the Python port |
| manipulate a space's surfaces | room accepted as a target through `_real_surface_match` |
| connectivity | an adjacency summary in `query_space` and `space://current` |
| rename | `name_room(room, name)`, owner-gated, rejecting reserved semantic words and duplicates |

**`space://current` is the highest-value integration point** — it is the per-turn injected resource and the
only place surfaces are described, so room grouping, the adjacency summary and a *"you are in the kitchen"*
line reach the director every turn with no tool call.

Honest limit on "which room am I in": only a physically-present AR client has one. CLI, voice and desktop
have no room and should say so rather than guess.

### What the primitive unblocks

| Item | Source |
|---|---|
| per-space boundary — the top open item, "make it honest" | [`backlogs/spaces.md`](./spaces.md) |
| `authored` immersion mode (needs a safe footprint to extrude) | [`backlogs/worlds-surfaces.md`](./worlds-surfaces.md) |
| multi-room culling for the surface overlay | this file, above |
| `levelDeviation` per-room instead of one global median | spec §10.2 |
| the floating-room corrector's input — an estimate, not a threshold | [`investigations/raised-floor.md`](../investigations/raised-floor.md) |

### Staging

1. **Segmentation + space record + names + connectivity + director queries and targeting.** Offline-testable,
   immediately user-visible, no registration change at all.
2. **Per-room residuals into the geometry event log** (`room.*`, change-gated), applying nothing. Field-read
   before correcting — the same `[aabb]`-first discipline.
3. **Apply the refinement**, with the guards above.
4. **Per-space boundary**, separately: it changes a schema with two consumers and deserves its own increment.

Step 1 is verifiable with no device. `fixtures/golden-space.json` is 45 surfaces across two rooms — a known
answer — and this space's three rooms have their ground truth written down above. The registration claim is
measurable the same way §4.1.2's table was: perturb one room of the golden fixture rigidly by 9 cm, then
compare global against per-room residuals and count which surfaces lose identity under the global fit and
are recovered under refinement.

### Branch compatibility — main and this branch share live data

Verified against the code, because the risk is not main *breaking* but main silently *dropping* the new
fields:

| Path | Behaviour | Verdict |
|---|---|---|
| `SpaceStore.load` / `.save` (`world.py`) | `json.loads` / `json.dumps` of the whole dict — schema-free | preserves unknown keys |
| `_save_active` (`server.py:919`) | **loads the existing doc and mutates named keys** | `space.rooms` survives untouched |
| `_space_from_world_doc` (`:2920`) | `copy.deepcopy(e)`, overwrites only `components.material` | `meta.room` survives |
| `_surface_update_set` | writes **dotted paths** for changed aspects only | `meta.room` never rewritten |
| a surface minted under main | no `meta.room` | benign — reassigned on the next branch run |
| a surface pruned under main | its label goes with it | benign — membership is derived |

So the schema is genuinely additive and **no separate data copy is needed**. The one caveat worth recording:
main preserves `meta.room` because it *never writes meta it does not know about*, which is a property of
today's implementation rather than a guarantee. Identity tier 2 (`rooms[].floor`) exists precisely so that a
wholesale loss of the labels re-identifies the rooms instead of minting fresh ids and orphaning the user's
names.

### Decisions — settled at review 2026-09-07

1. **Target vocabulary: both.** `room=` as the composable filter (room ∩ semantic — "the kitchen walls"),
   *and* a room accepted as a bare `target` value, which is what the model will reach for first. Both
   resolve at `_real_surface_match` (`server.py:3297`), so it stays one chokepoint.
2. **Rename the misuse of "room" — first, and on `main`.** Not deferred, and not done on this branch. Scope
   and reasoning below.
3. **Merge: deferred.** Two floor planes that are one physical room will over-segment. The schema supports
   merging later — the name is keyed by room id, so an alias or a `merged_into` field is additive — and the
   bias toward splitting holds until it actually bites.
4. **`y` is fitted per room; the detector keeps its raw comparison.** *Revised 2026-09-07, same day,
   after reading the code.* The first position — keep y out of the transform entirely — was the wrong
   shape: it stated a representation rule where the real invariant is a **consumer** rule.
   `levelDeviation` compares raw height scalars and never touches a transform, so the two coexist. See
   the body above for why including y likely *helps* the detector (a displaced floor that fails identity
   is skipped by the census outright) and for the guard it needs of its own (no horizontals in a room ⇒
   no y constraint ⇒ skip y, do not fit noise).

### The "room" rename — a prerequisite, and it is done

`room` was used throughout the code, tools and prompts to mean *the whole space*, which
[`specs/spaces.md` §1](../specs/spaces.md) explicitly disclaims. Making rooms real turns that usage from
loose into **wrong**, so it was cleared first — otherwise the word means both things at once, which is the
confusion being removed.

**The inventory, the classification rule, the do-not-rename list and the commit-by-commit progress live in
[`investigations/room-rename-2026-09.md`](../investigations/room-rename-2026-09.md)** — kept in one place
rather than duplicated here, because a rename map that exists twice is a rename map that goes stale once.

Two things worth keeping at this level:

- **It touches no persisted data**, verified against the live tree — so `main` and this branch keep sharing
  live data throughout. That is what made the next point safe.
- **It ran on `feat/rooms`, not on `main`.** The original call was `main`-first, on the grounds that a broad
  mechanical rename conflicts with every later `main` commit touching the same lines. That cost is
  contingent on `main` moving, and it is not: the pose-eval work stays on its branch until it has been
  device-tested. A trial merge confirmed it — `main + rename ← feat/pose-eval-harness` merges clean, since
  none of that branch's ~3,200 changed lines contains a room-named identifier.

**Status: the identifier half is complete** (Tiers 2 and 3), `978 pytest / 204 JS` green throughout. What
remains is prose.


## Instrumentation — the geometry event log

**Status:** **shipped 2026-08-30; diagnosed the floor fault on its first day in the field, 2026-08-31.**
The design and event reference live in [`specs/spaces-geometry.md` §10](../specs/spaces-geometry.md) — this
entry keeps only what is *not* done and what the field changed.

Always-on and change-gated, to `temp/geometry-<date>.jsonl`, rotated daily and pruned past
`--geometry-log-days` (21). A settled space emits nothing. Both halves shipped together — churn and heights —
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
- **The `err` sign flip at a space boundary is the sharpest single reading in the log** — it says which room
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

### Open — shelved, watching

Neither of these blocks anything; both need time or a recurrence rather than work.

- **Does the correction still fire when the displacement changes size?** It has run clean exactly once
  (2026-08-31, ~91 mm, looked right in the headset). The fault's own headline is that it "goes back and
  forth every few days", so the untested case is a *smaller* recurrence — and the one to watch hardest is
  the room returning to normal, where `level.correct` must switch **off** rather than keep correcting a room
  that no longer needs it. The threshold has ~30 mm of headroom below today's 70 mm.
- **The churn half has never fired.** Zero `churn.*` events in any session, so the device-vs-matcher
  discriminator has never run on a real miss. Tested, unproven — and the `track.reset` bursts are the most
  likely thing to eventually exercise it.
- **A `track.reset` burst nobody has explained.** Twelve between 07:09 and 07:24 on 2026-08-31 — including
  **three inside one second** at 07:11:09, and pairs at 07:11:47, 07:17:21 and 07:24. Each is a recenter or
  a guardian re-entry, and each re-registers. Walking between three rooms across a boundary drawn round one
  of them explains the count but not the same-second triples.
- **A validity guard on the marker.** One press (07:01:25) read `grip_y = −0.173` where the same floor reads
  ~0.03 — the controller had drifted on IMU while out of camera view. It was caught only by cross-checking
  three other sources, which will not always be possible. The tell was in the record: head-to-controller
  distance was **954 mm** against 655–814 mm on the good presses, and the controller sat *below* the
  rendered floor. Either reject such a press or log a confidence field, so a bad reading announces itself.

### The seed heights: one repaired, one deliberately left alone

`real_floor_10` was **repaired 2026-08-31** to `real_floor_8` + 25 mm, the kitchen step as measured. It had
been stored 21 mm *below* the living-room floor where it is physically 25 mm *above* — a ~46 mm sign error
predating everything here. Repairing it also promoted the kitchen from *excluded* (floor/ceiling incoherent
at 55 mm) to a **second coherent reference room** at 10 mm, which is a stronger baseline than one.

`real_floor_32` is still stored **9 mm** from `real_floor_8` though the two are one continuous wooden floor,
and it is **deliberately not repaired**. Forcing them equal moves `floor_32`'s deviation 9 mm away from
`ceiling_13`'s, taking the bedroom pair from 12 mm to **20.8 mm — past the 20 mm coherence gate, so the
correction stops firing entirely.**

That is worth understanding rather than working around. The seed's value is being **self-consistent**, not
absolutely accurate: every surface captured in one frame, so relative geometry is comparable. Hand-patching
one surface from ground truth improves its accuracy while breaking that consistency, and the detector needs
consistency more. The 9 mm is capture noise on a real floor, and it is also the residual the correction
cannot remove. **If it is ever repaired, the honest way is a fresh capture of the whole space taken while no
room is displaced — not a per-surface edit.**

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

## Instrumentation — the surface overlay

**Status:** **shipped 2026-09-02**, unexercised on device. The design and how to read it live in
[`specs/spaces-geometry.md` §10.4](../specs/spaces-geometry.md); this entry keeps what the build changed
about the plan and what is still open.

Three wireframe layers drawn together **in the viewer's own frame (F_track)**, so the seed, the live device
capture, and our rectangle approximation of it can be read against each other and against passthrough. The
question it answers is the one §1 makes unanswerable by inspection: the seed is never rendered, so nobody
has ever *looked* at how far the persisted shared model sits from the geometry the headset is reporting
right now.

### What the build changed about the plan

- **The polygon does not survive Pass A, and the seed has none.** `plane.polygon` is read once
  (`conjure-client.js:2539`), swept for an AABB, and discarded — never stored, posted, or passed to
  `SpaceSnap`. The server *accepts* a `polygon` field (`server.py:2991`, written at `:3023` / `:3354`) that
  the client has never sent; it is `None` on every surface in every space file. `joinCorners`/`sealWalls`
  work on position-plus-extent rectangles and have no polygon to join even in principle. So **only the raw
  layer can be a true outline** and part of any green-vs-magenta shape mismatch is representation rather
  than error. Worth knowing before reading the display.
- **The colours are three distinct hues, not two shades of green.** Grouping the device layers as
  light/dark green reads better on paper and worse in the headset: passthrough is low-contrast grey-brown
  and a desaturated line disappears into it. Green / amber / magenta, with cyan already taken.
- **Both device layers are built in the plane's own frame, not through `eulerYXZ`.** Not in the plan, and it
  changes what the display means: the rect is now independent of the render's euler conversion, so
  green↔amber isolates *only* the AABB reduction and amber↔cyan isolates the conversion plus
  `joinCorners`/`sealWalls`. Routing the rect through `eulerYXZ` (the obvious "match the render exactly"
  choice) would have folded those together and made a conversion bug invisible in the one tool built to see
  geometry faults.
- **Seed vertices rebuild every capture**, not on a change signature as planned. Measured: 58 rects is ~460
  vertex writes, and the signature costs more code than it saves work. Correctness lives on the matrix,
  which has to be rewritten every capture regardless.
- **The `[aabb]` probe is the cheaper half and shipped alongside.** `SpaceSnap.polyFit` + one
  `--debug-registration` line per capture answers the displacement question with no overlay at all. It
  should be read **first**; if `off` is 0 everywhere the overlay is still worth having for the seed
  comparison, but the urgency drops a lot.
- **One test was added for a convention, not a feature.** `space-snap.test.js` now pins the seed→F_track
  round-trip through `Tmat⁻¹` at boundary-flip magnitude (167°) on both an upright and a tilted plane. It
  was written because the first attempt at verifying the overlay by hand got the composition wrong in
  exactly the plausible way — applying the −90° X plane→a-plane rotation twice — and a small-angle,
  upright-only check would have passed anyway. Mutation-checked: reconstructing the stored euler as XYZ
  fails it.

### The two hypotheses it exists to test — both still open

Both live in the AABB reduction (`conjure-client.js:2540-2552`), neither was checked anywhere before, and
the `[aabb]` probe now measures both. **Nothing has been read on device yet.**

1. **The polygon is not a rectangle** (`fill` < 1). An L-shaped wall, or one with an angled corner, is
   rendered as its bounding box — so we draw surface where there is none, and post that phantom extent into
   the seed.
2. **The polygon is not centred on the plane origin** (`off` > 0). We take the AABB's *dimensions*
   (`maxx-minx`, `maxz-minz`) but keep `c.pos` — the plane's own pose origin — as the centre. If the AABB
   midpoint is not `(0,0)` in plane-local X-Z, every rendered rect is displaced from its true surface by
   exactly that vector. (Plane-local X-Z maps to the rendered rect's X-Y via the −90° X rotation of §2.2, so
   the x term displaces along the wall and the z term up it.)

The second is the higher-value one and the reason any of this was built: a systematic centring offset would
propagate **identically** into the local render, the posted seed, registration and every anchor — the class
of fault that is invisible precisely because everything downstream agrees with it. It also needs no overlay,
so read the `[aabb]` line before looking at wireframes. If `off` is 0 everywhere, hypothesis 2 dies and the
overlay's remaining value is hypothesis 1 plus the seed comparison.

### Seed, not `_ref` — a decision worth not re-litigating

They differ, and §6.1 records a real bug born of that difference. `_ref` lerps 0.3 toward live geometry every
capture, so its offset from the device layer is **artificially small** — it would understate exactly the
quantity being measured. The seed is the honest persisted model and carries full rotations, where a `_ref`
entry keeps only `nyaw` and would need orientation rebuilt by yaw extraction (which §5.2 avoids everywhere
else). `_ref` as a second, opt-in layer is still reasonable — matcher misses are explained against it — but
it is a different question from "how far is the shared model from what I see".

### Open — not built

- **Nothing has been read on device.** The whole feature is unexercised: no `[aabb]` line has been seen, and
  the overlay has never been looked at in a headset. Both are cheap, and until then the two hypotheses above
  are hypotheses.
- **Hole cuts.** No outline layer shows them; the cyan edges draw the outer loop only.
- **Multi-room culling.** The seed spans the whole space, so ~58 magenta rects draw at once including rooms
  you are not standing in. `SpaceSnap.floorUnder` (already used by `_markProbe`) is the room-scoped cull if it
  turns out to be a thicket — but a cull is a rule that can itself mislead, so it ships without one.
- **Seed↔device pairing.** Highlighting seed surfaces with no live plane behind them is nearly free from Pass
  B's `claimed` set, and is arguably the more interesting half of the signal — a missing partner says more
  than a displaced one. Deliberately deferred until the basic view is known to be legible.
- **Per-surface delta labels.** The wireframes say *where*, the HUD residuals say *how much* in aggregate;
  a per-surface number is the obvious next increment if the aggregate turns out to be too coarse.

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
no space geometry, skybox + objects, geolocation won't yank them into a physical space. In AR, `space-capture`
derives the frame on the fly with `SpaceSnap.canonicalFrame` (gravity-up + wall-grid axis + largest-wall
forward + centroid origin), never captures/posts, and `#world-root` + the skybox ride that frame → the same
physical space canonicalizes to the same orientation each visit (invariance unit-tested).

**Refinements left:**
1. **Symmetric-room ambiguity (inherent):** no unique largest wall ⇒ no unique canonical orientation
   (same 180° flip as `register()`). Low-stakes for a void world (only the skybox yaw moves, nothing pinned
   to real walls), but worth a tiebreaker (a door/opening, an L-shape corner) when one exists. The origin is
   unaffected — the wall centroid is the same point at any θ — but note that once
   [`grab` void mode](./dynamics.md) stores a user offset in frame coordinates, a flip reverses that
   offset's direction, so the stakes stop being only cosmetic.
2. **Partial-capture stability — FIXED 2026-09-02.** Reported from the field as "in a void world, every
   time I press the Meta button to reset the view, I jump a meter or two in a different direction." The
   design and the measurements are now [`specs/spaces-geometry.md` §4.1.2/§4.1.3](../specs/spaces-geometry.md);
   what this entry keeps is what the investigation changed about the plan.

   **The fault was bigger than this entry said, and in a different term.** It read "both the origin (mean of
   wall centres) and θ (largest wall) can shift", implying comparable stakes. Measured on the golden space:
   every subset from 3 to 12 of 30 verticals flipped **θ by 180°**, moving content **4.5–5.1 m** at 2.2 m
   from the origin, while the centroid contributed 0.2–1.8 m. θ is the fault; the centroid rides along. A
   recenter triggers it because `_onReset` sets `lastPost = 0`, forcing a capture on the very next frame —
   the moment the Quest has restored least of the space.

   **Fixed by `WM.voidFrameGate` plus establish-once-and-hold**, not by making the derivation more robust.
   Holding is the larger half: the walls do not move within a session, so re-deriving every capture only
   creates fresh opportunities to pick a different largest wall. `_onReset` invalidates the held frame
   (refSpace was re-origined, so it is stale by the recenter delta) and the gate decides when the fresh
   capture is good enough.

   **Two ideas from this entry were tried and rejected on measurement:**
   - *"Weight by covered wall area"* for θ — generalised to an area-weighted **vote** over all walls
     instead of the single largest. **Far worse: 12/18 single-wall drops flipped θ, against 1/18 for the
     largest-wall rule**, because opposite walls cancel in the sum. The largest-wall tiebreak is actually
     stable; it is only partial captures that break it, which is what the gate handles.
   - *Hysteresis on the derived value* — unnecessary once the frame is simply held. Hysteresis would have
     been a threshold to tune; holding has none.

   **Still open:** the symmetric-room ambiguity (1 above) is untouched, and nothing has been confirmed on
   device — the numbers are all from `fixtures/golden-space.json` plus a perturbation model.

   Raised in priority by [`dynamics` → `grab` modes](./dynamics.md): void mode stores a user offset against
   this frame and pins the skybox's *position* to its origin, so a frame shift moves both. Holding the frame
   removes the within-session case entirely.
3. **Optional space tie:** let an outdoor world *optionally* bind to a stored space (robust registration)
   instead of canonicalizing — for rooms you revisit a lot and want rock-solid.
4. **Immersion polish:** a void world currently shows whatever skybox is set (or the void color until one
   is). Consider a sensible default / an explicit "outdoor" immersion that always occludes passthrough.

## `view_relative` can't tell you're looking at a placed OBJECT (only real surfaces)

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

## Multi-observer space fusion — refine the shared model from every headset (server-side)

**Status:** future feature · noted 2026-06-30 (deferred while building register-only guests, co-location §5)

**Idea:** today the space geometry has a single writer — the authority (space owner) captures + posts; a
guest **localizes against a frozen copy** of that geometry and never contributes (register-only — see
`specs/worlds-surfaces.md` §8b). That's correct for co-location: the shared `_ref` constellation *defines* the shared
frame, so a guest mutating it locally would only desync (its `/space/capture` posts are 403'd, so the change never
reaches the authority) and feed a drift loop. But a guest is *also* observing the same real space, so its
observations could legitimately **improve** the one model (better extents, corrected drift, "the space
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
on explicit "rescan together"; how to reconcile a genuinely *changed* space (furniture moved) vs. noise.
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

**The problem.** `SpaceSnap.register()` needs a wall basis (vertical plane pairs) to lock at all. If a space's
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
   seed a wall-less space).
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
