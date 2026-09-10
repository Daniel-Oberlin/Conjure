# Rooms — device run

**Status:** plan, written before the run. Branch `feat/rooms`. Nothing here has been executed.

Everything in the rooms work is unexercised on hardware. The unit and JS suites cover the *rules*
(`tests/test_rooms.py`, `tests/js/space-snap.test.js`), and the golden capture pins segmentation against
one real 45-surface two-room fixture — but replayed offline, from a file, in a space that is not this one.

Two things make the run worth doing now rather than after more building:

- **Step 3 is gated on it by design.** The whole point of step 2 was to measure inter-room non-rigidity
  before correcting anything, and there is no measurement yet. §1's ~9 cm is an aggregate from one space
  on one day.
- **This space has written-down ground truth** ([`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md)):
  three rooms, the floor/ceiling pairs named, `floor_32`≡`floor_8` as one continuous floor, `floor_10`
  +25 mm, `ceiling_21` genuinely sunken. Almost nothing else we test has an answer key.

---

## Before starting

- **Back up the space file.** `~/.local/share/conjure/users/daniel/spaces/space-1.json`. A capture writes
  to it, and an empty-capture post has wiped a seed once already
  ([`backlogs/spaces.md`](../backlogs/spaces.md)).
- **Do not pass `--debug-registration`.** It adds ~6 fetches per capture and perturbs what it measures —
  the mistake the jitter campaign already paid for once. The `room.residual` lane is always-on and
  change-gated; it needs no flag.
- Leave the geometry log on (i.e. no `--no-geometry-log`). Output: `temp/geometry-<date>.jsonl`.
- `main` and this branch share live data. The schema is additive, so a later `main` run is safe — §
  *Branch compatibility* in the backlog — but H below confirms it rather than assuming it.

---

## A — does any of it run?

The cheapest possible failure is that segmentation throws and is swallowed, or the POST never carries it.
Check this before anything else; every later section is meaningless if A fails.

| # | Do | Look at | Pass |
|---|---|---|---|
| A1 | Enter AR in the captured space | server log | `[space] rooms: 3 group(s) → room_1, room_2, room_3` |
| A2 | — | browser console | **no** `room segmentation failed` |
| A3 | — | `GET /world` → `environment.rooms` | three entries, each with `n` and `floor` |

**If A1 is absent but A2 is clean**, the capture posted with `rooms: null` — the segmentation ran but the
POST gate never fired (a settled space posts nothing). Move something, or re-enter AR.

---

## B — is the segmentation right?

The answer key, from the backlog's ground truth: `floor_8` = living room, `floor_10` = kitchen,
`floor_32` = bedroom; ceilings `25`, `21`, `13` respectively.

| # | Do | Pass | Fail means |
|---|---|---|---|
| B1 | `query_space` | **3** rooms | 2 ⇒ two rooms merged (a floor missing, or one room's floor covering both); 4+ ⇒ the Quest split a floor |
| B2 | read each room's floor + ceiling | the pairs above | a ceiling on the wrong floor ⇒ the `covers` footprint test is picking the wrong one |
| B3 | count walls per room | each ≥ 4, and they sum near the space's wall count | many missing ⇒ the facing rule is rejecting real walls |
| B4 | count *unassigned* | small, and **only** walls | insets or floors unassigned ⇒ a real bug, not a tolerance |

B4 is the interesting one. Unassigned walls are expected and safe by design — a wall too edge-on to place
is refused rather than guessed. **How many** is the number worth recording: it is the cost of that
conservatism, and it has never been measured.

---

## C — connectivity

| # | Do | Pass |
|---|---|---|
| C1 | `query_space`, read the door lines | interior doors show `→ <room>`; exterior doors show `→ outside` |
| C2 | compare against the real dwelling | the adjacency matches how the rooms actually connect |

**If every door reads `→ outside`**, the far-side probe is not clearing the partition. That exact failure
appeared on the golden fixture and was traced to the two floor rectangles *overlapping* by ~0.6 m across
their shared boundary, so a single probe inside the band read the near room. It is fixed by stepping
outward until one floor answers — but the step ceiling (1.8 m) is a guess against real wall thicknesses,
and this is where it gets tested.

---

## D — identity across sessions (the one that can lose data)

Membership is derived and cheap to recompute. A **name** is user data with no recovery path, and identity
resolution is the only thing protecting it. This section matters more than the rest combined.

| # | Do | Pass |
|---|---|---|
| D1 | `name_room("room 1", "kitchen")`, then `query_space` | the right physical room is now "kitchen" |
| D2 | leave AR, re-enter | still "kitchen", still the same room |
| D3 | restart the server, re-enter | ditto — proves it round-tripped to the space file, not just memory |
| D4 | `--drop-surface <that room's floor id>`, re-enter | **still "kitchen"** |

D4 is the real test. It forces tier 1 (surface-set overlap) to carry the identity alone, because the
recorded defining floor is gone. Keying only on the floor — the obvious design — fails exactly here, which
is why it was rejected and why the rejection is worth confirming on hardware rather than only in a unit
test.

**Fail:** the name reverts to `room #N`, or worse, appears on a *different* room. Record which.

---

## E — targeting

| # | Do | Pass | Fail means |
|---|---|---|---|
| E1 | `style_surface("kitchen", color="blue")` | only that room changes, visibly | — |
| E2 | `style_surface("wall", room="kitchen")` | only that room's **walls** | if all walls change, the filter is not reaching `_surface_targets` |
| E3 | `style_surface("all", color="red", room="attic")` | **refused**, nothing changes | if the space repaints, the unresolvable-room guard is not firing — the one failure that touches surfaces nobody mentioned |
| E4 | `show_surface("ceiling", room="kitchen")` | one ceiling | — |

Watch E1 in the headset rather than in the tool output. The count can be right while the wrong surfaces
are painted, and only passthrough shows that.

---

## F — which room am I in

| # | Do | Pass |
|---|---|---|
| F1 | stand in each room, ask the director where you are | names the room you are in |
| F2 | walk to another room, ask again | updates within a second or two |
| F3 | ask from the CLI with no headset present | says it does not know — **not** a guess |

F3 is a real assertion, not a formality: only an AR client reports a floor, and the honest answer for a
CLI or voice user is that they are standing nowhere.

---

## G — the measurement this was all for

Read `temp/geometry-<date>.jsonl`, filter `ev == "room.residual"`.

```
grep '"room.residual"' temp/geometry-$(date +%Y-%m-%d).jsonl | python3 -m json.tool --json-lines
```

Record per room: `before`, `after`, `dx`, `dz`, `dy`, `n`, `walls`, `would_apply`.

**The open question, in plain terms: how far apart are the rooms, really?** §1 measured ~9 cm as a
whole-space aggregate. Nobody has ever seen it split by room.

| # | Question | Why it matters |
|---|---|---|
| G1 | What is `before` per room? | 1–2 cm ⇒ the effect is real but not worth correcting. ~9 cm ⇒ it is eating most of `matchWall`'s 150 mm budget, and step 3 is justified |
| G2 | Does `after` < `before`, and by how much? | a refinement that barely improves is a refinement not worth applying |
| G3 | Is one room consistently the outlier? | §1 says the non-rigidity is *concentrated in one region*; this is the first chance to name it |
| G4 | Does a settled space stop emitting? | the change-gate. If `room.residual` repeats every capture the log stops being affordable to leave running |
| G5 | **Does `dy` agree with `level.anomaly`?** | see below |

**G5 is the cross-check, and the sharpest reading available.** If the raised-floor fault is active during
the run, two entirely independent estimates should name the same room with a similar magnitude:
`level.anomaly` (live-minus-seed heights, median-subtracted) and `room.residual`'s `dy` (a fit over that
room's horizontal surfaces). They share no code path. If they agree, both are working. **If they disagree,
one is wrong and the log says which room to look at** — which is more than either gives alone.

If the fault is *not* active, `dy` should be small everywhere and `level.anomaly` silent. That is also a
result: it says the two agree on "nothing to see".

---

## H — did we break `main`?

The schema is additive by construction and was verified by reading `main`'s write paths, but the point of
a device run is to stop trusting that kind of verification alone.

| # | Do | Pass |
|---|---|---|
| H1 | after the run, `git checkout main`, start the server, enter AR | no error; the space still loads |
| H2 | inspect the space file | `rooms` still present; surfaces still carry `meta.room` |
| H3 | back to `feat/rooms`, re-enter | room names intact, no re-mint |

**If H2 shows `rooms` gone**, `main` cleared it and the identity tiers just became load-bearing rather
than a hedge — tier 2 should recover the rooms by floor id on the next capture, and H3 is where that shows.

---

## What a good run produces

Not a pass/fail. Three artifacts:

1. **A number for G1** — per-room residuals from a real space, which is the input step 3 was deferred for.
2. **A verdict on D4** — whether identity genuinely survives losing a room's defining floor.
3. **A count for B4** — how many walls the conservative facing rule declines to place, which nothing has
   ever measured.

Anything else that breaks is a bug to file, not a reason to stop the run.
