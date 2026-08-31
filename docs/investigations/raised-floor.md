# One room's floor renders four inches high

**Outcome:** cause established 2026-08-31 — the Quest's stored room entity for the bedroom is anchored
~104 mm high, and every plane in that room rides with it. **Device-side; there is nothing to fix in our
code.** Three competing hypotheses were killed by measurement, and the campaign is recorded here so none of
them is re-proposed.

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

**The fix is a Quest Room Setup re-scan of that room.** Nothing in our pipeline touches a floor: `sealWalls`
reads floors and writes only walls (`room-snap.js:554`), `joinCorners` writes walls, `snapInsets` writes
insets. A floor renders at its raw `detectedPlanes` pose, untouched.

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

## Remaining theories and open questions

| | Likelihood | How to test |
|---|---|---|
| A Room Setup re-scan clears it | **high** — everything points at the stored room entity | Re-scan the bedroom, re-enter, press the marker in bedroom and living room. `floor_32 − floor_8` should return to ~0 and the entry-time anomaly should stop |
| Content placed on the bedroom floor floats ~10 cm | **high**, unverified | Place an object in the bedroom and look at it. Content solves against *local* planes, so it should ride the displaced floor while living-room objects sit flat. Confirms the diagnosis from the other side |
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

## Fixes shipped

| Symptom | Cause | Fix | Knob | Commit |
|---|---|---|---|---|
| No way to detect or attribute a height fault | The system has no ground truth and no persistent record | The geometry event log: change-gated height census, median-deviation alarm, `explainNoMatch`, and the controller marker probe | `--no-geometry-log`, `--geometry-log-days` (21), `mark` binding (default **B**) | `371ff83` |
| The floor itself | The Quest's bedroom room entity is anchored ~104 mm high | **None in our code** — a Room Setup re-scan on the device | — | — |
| `[post]` logged only the first changed id; server `add`/`remove` were silent | first-hit `reason` string; only `update` had a `[seed]` line | full reason list; `seed.add` / `seed.prune` named by id | — | `371ff83` |

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
