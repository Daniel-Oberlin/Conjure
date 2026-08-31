# One room's floor renders four inches high

**Outcome:** cause established — the Quest anchors one room's stored entity high and every plane in that
room rides with it. Three competing hypotheses killed by measurement. **A Room Setup re-scan does not clear
it**, and objects on that floor end up buried, so a render-side correction was attempted.

**Four correction attempts, all reverted. There is no correction on `main`.** The work lives unmerged on
`feat/fix-floating-rooms` (§ *The correction* below, with what is on the branch and how to resume). What
shipped to `main` is the instrumentation that diagnosed it, and it stays because it earned its place.

The instrumentation built to answer this is [`specs/spaces-geometry.md` §10](../specs/spaces-geometry.md);
what remains open is in [`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md).

---

## Symptom

> "I've also noticed that the floor in one room in the space is sometimes raised by 4-6 inches, and
> sometimes it is accurate. Seems to go back and forth every few days." — 2026-08-30

Conditions: a three-room space (bedroom, living room, kitchen), 59 captured surfaces, single headset, owner
role. Visible in passthrough as the rendered floor sitting above the real one. No correlation reported with
any particular action.

The reporter's other symptom — surfaces occasionally dropping out and returning uncoloured — was
instrumented in the same pass and is **not** part of this campaign; it has never reproduced.

---

## Why nothing could be concluded before instrumenting

The system has **no ground truth**. It renders the floor wherever the Quest's plane says it is, and every
internal check — floor against ceiling, floor against wall bottoms, floor against the persisted seed — is a
*consistency* check. All of them pass when the whole room is uniformly displaced. Passthrough shows the
error instantly; the code cannot see it at all.

So no automatic probe could ever fire on it, and no log could be read after the fact. The campaign had to
begin by building a way to inject a physical measurement into the coordinate system.

---

## The ground truth that made it solvable

Supplied by the owner on 2026-08-31, and the single most valuable input in the campaign:

- `real_floor_32` (bedroom) and `real_floor_8` (living room) are **one continuous wooden floor** — their
  heights must be *equal*, always.
- `real_ceiling_13` (bedroom) and `real_ceiling_25` (living room) are at the **same physical height**.
- `real_floor_10` (kitchen) is **+25 mm** above the other two; `real_ceiling_21` (kitchen) is genuinely
  sunken.

Two known-equal pairs turn every reading from "is this plausible?" into "this differs from zero by N".
Everything below rests on them.

---

## Experiments and what each proved

| # | Experiment | Result | Conclusion |
|---|---|---|---|
| 1 | Ship an always-on, change-gated height census + median-deviation alarm; enter the space normally | `level.anomaly` fired **unprompted at session entry**, naming `real_floor_32` (dev +83 mm) and `real_ceiling_13` (+77 mm) | The fault is real, persistent, and localised to one room. Detected with nobody looking for it — the design bet paid on day one |
| 2 | Compare the two known-equal pairs in the live capture | `floor_32 − floor_8` = **+104 mm** (truth 0); `ceiling_13 − ceiling_25` = **+103 mm** (truth 0) | The bedroom is displaced as a **rigid unit**. Floor and ceiling agreeing within **1 mm** is the decisive number in the whole campaign |
| 3 | Marker probe: controller resting on the shared wooden floor, four presses across three rooms | grip_y = 0.047 (bedroom) / 0.032 (living) / 0.046 (bedroom) / 0.038 (kitchen) — **15 mm spread, 1 mm hysteresis** on the return | The **tracking frame is sound**. One continuous floor reads as one height everywhere. Kills the warped-frame hypothesis outright |
| 4 | Read the sign of `err` either side of the room boundary | bedroom **+0.042 / +0.045**; living **−0.044**; kitchen **−0.029** | The **bedroom** is the displaced room, not the others. Living and kitchen sit just *below* the controller — that is the grip bias — while only the bedroom sits above it |
| 5 | Check registration health throughout | `cov=59/59 inl=59/59`, residuals 3–5 mm mean, 7–14 mm max, both sessions | Not a lock failure, not a bad frame solve. Registration was flawless the entire time |
| 6 | Track the offset across four censuses over 25 minutes | 117 → 103 → 103 → 104 mm | Stable, not drifting. Absolute heights bob ±25 mm (the whole space breathing) while the *offset* holds |
| 7 | Look for corroboration in the wall geometry | `wall_81` carries a persistent **106–120 mm gap** above `floor_8` | The partition wall is over the living-room floor but its bottom sits at the *bedroom's* level — it rides with the displaced group. `sealWalls` had been silently stretching it down ~107 mm every capture, which is why no slit was ever visible |
| 8 | Check the persisted seed | `floor_32 − floor_8` = 9 mm there — correct | The seed **never got corrupted**: 104 mm is far below the 0.5 m structural-change threshold, so it never round-tripped. The post-gate design held under a real fault |

### The conclusion those force

Experiments 3 and 5 remove the frame and the solver. Experiment 2 removes independent per-plane error —
a floor and a ceiling four metres apart do not drift into agreement within 1 mm by coincidence. What is
left is the Quest's own room entity for the bedroom being anchored high, with its planes faithfully
following it.

Nothing in our pipeline touches a floor — `sealWalls` reads floors and writes only walls
(`room-snap.js:554`), `joinCorners` writes walls, `snapInsets` writes insets, and a floor renders at its raw
`detectedPlanes` pose. So the obvious remedy was a Room Setup re-scan on the device.

**That was tried, and it did not clear the fault.** Whatever anchors that room entity survives a re-scan.
With no source-side cure and a visible consequence — objects on that floor float — the remaining option is
to correct it at render, which is what §10.4 does. Note what that costs: it is the first and only place the
client deliberately draws something other than its raw capture, so the criterion is built to refuse rather
than guess (see *Precision* below, and the rules in §10.4).

---

## Tried and rejected

**A warped tracking frame (y non-rigidity across the space).**
Plausible on the first session, where a marker press in the living room read `grip_y = −0.173` against
0.05 in the bedroom — implying 22 cm of vertical disagreement on one floor. Killed by experiment 3: with
a clean gesture the same floor reads within 15 mm across all three rooms, and returns to 1 mm after
walking two rooms away. *What would justify revisiting:* a boundary-straddling press pair that reproducibly
differs by more than ~3 cm on a floor known to be continuous.

**The floor plane alone re-fitting (a rug, a threshold, a low object).**
The most intuitive explanation, and wrong. The ceiling moved by the same 103 mm. *What would justify
revisiting:* a census where a floor's `dev` moves and its ceiling's does not.

**Registration / a bad frame lock.**
Never supported — coverage was 59/59 with 14 mm worst-case residuals throughout. Worth stating explicitly
because "the room looks wrong" reads as a registration symptom and it is the first place anyone will look.
*Note:* small residuals do **not** contradict a large `dev`. They measure against `_ref`, which lerps toward
live geometry every capture; `dev` measures against the persisted seed. The reference had quietly followed
the drift.

**"The low ceiling identifies the room."**
An early guess: `ceiling_21` sits 23 cm below the other two in the seed, so it looked like the distinguishing
feature and therefore the affected room. It is simply the kitchen, which is genuinely sunken. The affected
room was `floor_32` / `ceiling_13`, which has nothing visually distinctive about it. Recorded because the
guess was confident and cost a round of analysis.

**One marker press (07:01:25) that read 20 cm low.**
Not a hypothesis but a false datum, and the campaign's main hazard. The controller had drifted on IMU while
out of camera view. It was caught only by cross-checking four other sources, which will not always be
possible. **The tell was in the record:** head-to-controller distance was 954 mm against 655–814 mm on the
good presses, and the controller sat *below* the rendered floor. A validity guard is filed in the backlog.

---

## The fault is not fixed to one room, and its size is not bounded

| when | room | displacement |
|---|---|---|
| 2026-08-31 morning | bedroom (`floor_32` / `ceiling_13`) | ~90–104 mm |
| 2026-08-31 afternoon | **living room** (`floor_8` / `ceiling_25`) | **~276 mm** |

Both verified the same way — two surfaces that are physically one thing reading as two. On the afternoon
capture `floor_8 − floor_32` was **+189 mm** where the seed has them 9 mm apart, and room heights were
preserved (2.668 m live vs 2.669 m seed), so the room is **translated**, not distorted.

**The seed was explicitly cleared of suspicion**, since it had been hand-edited that day: no seed write ever
touched `floor_8` or `ceiling_25`; the seed's two floors still read as one surface; and only the live
capture violates that. The distinguishing test needs no baseline and no median — two surfaces that are one
thing either read as one thing or they do not.

This is the single most important finding for anyone resuming: **any threshold calibrated on one occurrence
is calibrated on nothing.** That mistake was made, and is recorded below as guard 3.

## Remaining theories and open questions

| | Likelihood | How to test |
|---|---|---|
| ~~A Room Setup re-scan clears it~~ | **REFUTED 2026-08-31** | Re-scanned; the room came back displaced. Whatever anchors that room entity survives a re-scan, which rules out a stale scan and makes this a standing fault to live with rather than a one-off to clear |
| ~~Content on the bedroom floor floats ~10 cm~~ | **CONFIRMED 2026-08-31** | Objects on the bedroom floor rise with the displaced floor, exactly as predicted — the diagnosis confirmed from the second side, and the user-visible harm that justified building a correction |
| The reported "goes back and forth every few days" is the room entity re-anchoring | plausible, untested | Requires the fault to recur after a re-scan. The log now dates every occurrence automatically |
| The `track.reset` burst is involved | low | Twelve resets in 15 minutes, including **three inside one second**. Walking between rooms across a boundary drawn round one of them explains the count but not the same-second triples. Worth explaining on its own terms before it is dismissed |

---

## Precision — what each instrument is actually good for

Worth stating, because the two disagree and the difference is not error:

- **Plane-vs-plane against a known-equal pair is exact**: 104 mm, no instrument in the loop, no bias.
- **The marker is a room-identifier, not a precision gauge.** With the grip bias inferred from the
  living-room and kitchen presses (~3–4 cm), it puts the bedroom ~80 mm high — the same order and the same
  sign as the 104 mm, but the two agree only to **within the ~2 cm gesture noise**, not more finely.
  (An earlier draft of the backlog claimed `dev` and the controller agreed "to the millimetre". That was
  overstated: the bias was estimated from the same presses, so the agreement was partly circular.)

What the marker uniquely provides is the **sign of `err` either side of a boundary** — which room is wrong.
No internal probe can answer that, because internally the space is self-consistent either way.

---

## The correction — four attempts, and why each was reverted

Each version was validated against a *single stored capture* and then failed on a capture nobody had seen.
That is the pattern, and it is the main thing to internalise before touching this again.

| # | Membership rule | How it failed | Commit |
|---|---|---|---|
| 1 | **Proximity** — a wall joins if its bottom is within `MIN × 0.6` of the floor | admitted a near-arbitrary single wall. `wall_111` sat 18 mm from the *displaced* floor and was swept in, though its own drift was +16 mm against the room's +96 mm. `door_112` rode it **67 mm into the ground** | `391d8e9` |
| 2 | **Per-surface drift** — join only if your own drift matches the room's | excluded the walls entirely (a wall's drift cannot be measured honestly — the seed stores it *post*-`sealWalls`, a live capture is pre-seal). Leaving them behind made their gap to the corrected floor **grow by the offset**: `wall_82` went 57 mm → 152 mm, past `--wall-seal-tol`, opening a visible slit | `b0be5c3` |
| 3 | **Spatial + facing** — the room's floor, ceiling, walls that face into it, insets by host | correct in principle and still the right rule. But it fired on the **first capture of a session**, straight after a relocalization, read the living room as 229 mm displaced, moved 16 surfaces and left `wall_33` behind (facing −0.048 against a 0.05 gate — 2 mm of margin) | `bab67f9` |
| 4 | **#3 plus three guards** — confirmation over 5 consecutive captures, all-or-nothing membership, 0.15 m offset ceiling | never failed in the field; **never ran in the field either.** Reverted from `main` by choice, to separate the part that works from the part that has not | `f86d163` |

**The recurring failure, three times over, is partial room membership.** Leaving one surface behind while
its floor moves opens a gap the width of the correction — v1's doors, `wall_82`, `wall_33`. It is
structural, not a tolerance to tune: **move the whole room or none of it.**

### What is on `feat/fix-floating-rooms`

Branch tip `f86d163`; its history begins at `391d8e9`, the first implementation, so the whole arc is
readable in order. `main` was rewound to `b0f3d8d` (the revert), keeping everything else.

- `RoomSnap.floatingRoom` — detection and spatial membership
- `RoomSnap.confirmFloating` — the confirmation state machine (pure, so it is unit-tested)
- `RoomSnap.applyFloatingFix` — vertical-only application
- `conjure-client.js` `_fixFloating` / `_driftAll`, `--fix-floating-rooms`, `level.correct` events
- 9 JS tests including a replay of the 229 mm incident

**Detection is sound and worth keeping.** It is coherence-based: a room qualifies only when its floor *and*
ceiling have drifted from the seed by the same amount — a rigid body, which a noisy plane fit cannot fake.
On the reference capture the affected room read floor +77 / ceiling +71 mm while the kitchen read
+18 / −38 mm, so the kitchen was excluded from the candidate set *and* the baseline automatically. It found
the right room every time. **Every failure was in what it did next, never in what it found.**

### To resume

1. **`git merge feat/fix-floating-rooms`**, then re-read the three guards.
2. **Guard 3's ceiling (0.15 m) must be revisited first.** It was justified as "2.5× anything observed" —
   and then 276 mm was observed. As written, **it would refuse the very fault that is live today.** Make it
   a knob, or re-derive it from something that is not a sample of one.
3. **Verify guard 1 on device.** Confirmation over 5 captures has never run in a headset. Expect ~10 s of
   nothing happening on entry before `level.correct` appears — that is the guard, not a hang.
4. **Watch the release path.** No version has ever been observed switching *off* when a room returns to
   normal. It is the least-tested branch in the feature.
5. **Do not calibrate on one capture.** Replay against both stored fixtures — the 229 mm incident (must
   refuse) and the ~91 mm bedroom fault (must confirm on the fifth capture) — before trusting any change.

### Do this before resuming the correction at all

**Check whether placed content resolves against the wrong floor.** All three floors enter the anchor solve
as separate floor planes; an anchor resolving against the bedroom floor while the object stands over the
living-room floor lands ~190 mm out, which matches models being buried to the neck. Unverified.

If that is real it is a **bug on `main`, independent of any correction**, and fixing it removes the *visible
harm* — content in the wrong place — while leaving the floor cosmetically wrong. That is a far better
trade than moving room geometry, and it is the cheaper thing to be right about.

## Fixes shipped

| Symptom | Cause | Fix | Knob | Commit |
|---|---|---|---|---|
| No way to detect or attribute a height fault | The system has no ground truth and no persistent record | The geometry event log: change-gated height census, median-deviation alarm, `explainNoMatch`, and the controller marker probe | `--no-geometry-log`, `--geometry-log-days` (21), `mark` binding (default **B**) | `371ff83` |
| The floor itself | the Quest anchors one room's stored entity high | **Nothing on `main`.** Not fixable at source (a re-scan does not clear it); four render-side attempts were reverted, and the work sits unmerged on `feat/fix-floating-rooms` | — | reverted in `b0f3d8d` |
| `[post]` logged only the first changed id; server `add`/`remove` were silent | first-hit `reason` string; only `update` had a `[seed]` line | full reason list; `seed.add` / `seed.prune` named by id | — | `371ff83` |

**Follow-on fault, found and fixed the same day.** The first cut of the correction decided *which* surfaces
belong to a room by geometric proximity — a wall joined if its bottom sat near the floor. `real_wall_111`'s
bottom happened to land 18 mm from the *displaced* floor, so it was swept in and shifted the full 83 mm,
along with `real_door_112` riding it. Its own drift was **+16 mm against the room's +96 mm**: it had never
moved. The door ended up ~67 mm below where it belonged, reported from the headset as "a little low".

The first repair over-corrected in the other direction. Judging membership by each surface's own drift
excluded the walls — a wall's drift cannot be measured honestly, since the seed stores it post-`sealWalls`
while a live capture is pre-seal — and leaving them behind made their gap to the corrected floor grow by the
offset. `real_wall_82` sat 57 mm above its floor; at a 95 mm correction that became **152 mm, past
`--wall-seal-tol` (0.15)**, so `sealWalls` refused and a visible slit opened under a wall that had been fine.
It came and went with the offset, which wandered 74–99 mm across one session.

Membership is now **spatial**, which is what the rigid-body evidence supported all along: the room's floor,
ceiling, every wall of that room, and the insets on them. Adjacent rooms are separated by **facing** — a
partition is two anti-parallel planes, one per room, and the interior is the −normal side. Verified on the
real space: 10 surfaces move as one room, and none of the neighbours' walls come with them.

**A second, independent defect surfaced while chasing this.** At 07:37 — during a displaced session —
`seed.update` rewrote `real_wall_82`, `real_wall_37` and `real_ceiling_13` because an *opening count*
changed. `_surface_update_set` writes the full pose on any structural change, so ~90 mm of live displacement
was imported into the seed: the reference the correction measures against now partly contains the fault.
Filed in the backlog; not fixed here.

Two things made this hard to see, and both are now closed: the census logged floors, ceilings and wall gaps
but **no insets**, so the door was invisible in the log and had to be reasoned about through its host wall;
and membership used a different rule (proximity) from room selection (drift coherence), when the drift
evidence that would have excluded the wall was already being computed. Membership is now the same coherence
test, walls are left to `sealWalls`, and insets are judged on their own drift.

**Side findings recorded in the backlog, not fixed here:** a ~46 mm sign error in the seed's kitchen height
(it stores `floor_10` below the living room where physically it is above); the marker validity guard; the
unexplained `track.reset` triples; and the observation that declaring the space's known-equal surfaces would
turn the median heuristic into a flat assertion.

---

## Related

- [`specs/spaces-geometry.md` §10](../specs/spaces-geometry.md) — the event log, as built.
- [`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md) — what is still open.
- [`investigations/pops-and-jitters.md`](./pops-and-jitters.md) — the earlier campaign whose "build the
  MARKER probe first" recommendation this one finally acted on.
