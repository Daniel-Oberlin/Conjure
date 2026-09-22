# Plan — rooms as a first-class unit

**Status:** step 1 DONE (settled into `specs/spaces.md` §1.1) · step 2 BUILT but **never read on device**,
which is its entire output · step 3 gated on that reading · step 4 open and needs no headset
**Opened:** 2026-09-07 · **Steps 1–2 landed:** 2026-09-09, branch `feat/rooms`

**This file is temporary.** A plan spans areas that the specs and backlogs deliberately keep apart — this
one runs across client geometry, server identity, the space record, the director's tools and registration
at once — so it exists to hold that one sequence while it is executed, and is deleted when the last phase
settles.

It lived in [`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md) until 2026-09-22, where 300
lines of plan had stopped that file being a backlog. The rename it depended on is
[`plans/room-rename.md`](./room-rename.md).

**Dissolves to:**

| Part | Goes to | When |
|---|---|---|
| Steps 1 and 2 — built, and already described as built | **deleted**; [`specs/spaces.md` §1.1](../specs/spaces.md) and [`specs/spaces-geometry.md` §4.4](../specs/spaces-geometry.md) carry them | once step 3 is decided, since its argument still cites them |
| Step 3 — the guards, and the `y` decision | `specs/spaces-geometry.md` §4.4 if applied; [`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md) if dropped | after the device run |
| Step 4 — per-room boundary | [`backlogs/spaces.md`](../backlogs/spaces.md), where the boundary item already waits on it | when built or deferred |
| Why piecewise-rigid, and the alternatives rejected on measurement | [`decisions.md`](../decisions.md) | with step 3 |

If it outlives those, it has become a backlog by another name and should be dissolved on the spot.

---

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
wipe in [`backlogs/spaces.md`](../backlogs/spaces.md).

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
| manipulate a room's surfaces | room accepted as a target through `_real_surface_match` |
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
| per-room boundary — the top open item, "make it honest" | [`backlogs/spaces.md`](../backlogs/spaces.md) |
| `authored` immersion mode (needs a safe footprint to extrude) | [`backlogs/worlds-surfaces.md`](../backlogs/worlds-surfaces.md) |
| multi-room culling for the surface overlay | this file, above |
| `levelDeviation` per-room instead of one global median | spec §10.2 |
| the floating-room corrector's input — an estimate, not a threshold | [`investigations/raised-floor.md`](../investigations/raised-floor.md) |

### Staging

1. **Segmentation + room record + names + connectivity + director queries and targeting.** ✅ **Built
   2026-09-09** on `feat/rooms`: `SpaceSnap.segmentRooms` / `roomLinks` (client, pure, pinned against the
   golden capture — 2 rooms, 45/45 placed, the doorway resolving from both of its faces);
   `conjure/rooms.py` identity resolution (overlap → floor → mint, mutation-checked);
   `environment.rooms` riding the `boundary` path onto the space; a room as a target through
   `_real_surface_match`, so every surface tool gained it at once; `name_room`; room grouping and door
   destinations in the per-turn summary; and which room the user is standing in, from the presence tick.

   The `room=` filter for room ∩ semantic ("the kitchen walls") landed with it. **Step 1 is complete**, and
   [`specs/spaces.md` §1.1](../specs/spaces.md) now describes rooms as built rather than absent.
2. **Per-room residuals into the geometry event log** (`room.*`, change-gated), applying nothing. ✅ **Built
   2026-09-09**: `SpaceSnap.roomResiduals` + `WM.roomResidualGate`, emitting `room.residual` with the
   fit, its before/after, `dy` reported separately, and the `would_apply` verdict the guards give. See
   [`specs/spaces-geometry.md` §4.4](../specs/spaces-geometry.md).

   **Nothing has been read on device.** The whole point of this increment is the field data, and there is
   none yet — every number so far is from synthetic pairs and the golden capture.
3. **Apply the refinement**, with the guards above.
4. **Per-room boundary**, separately: it changes a schema with two consumers and deserves its own increment.

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
[`plans/room-rename.md`](../plans/room-rename.md)** — kept in one place
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
