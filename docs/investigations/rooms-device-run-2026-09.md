# Rooms — device run

**Status:** checklist, written before the run. Branch `feat/rooms`. Nothing here has been executed.

A **consumable**, in the sense [`investigations/README.md`](./README.md) sets out: it lives here until the run, then either grows into a real investigation — symptom, experiments, what was ruled out — or is deleted with its findings promoted to the backlog.

> ## ⚠️ This must be run in the three-room space
>
> Not any captured space — **the one with the written-down ground truth**: bedroom, living room, kitchen,
> ~59 surfaces, `daniel/spaces/space-1`. Section B's answer key, section G5's cross-check against the
> raised-floor fault, and the whole point of measuring inter-room residuals all depend on it.
>
> In any other space you can run A, E and F for smoke value and nothing else. **Wait until you are back
> there.**

Everything in the rooms work is unexercised on hardware. The suites cover the *rules*
(`tests/test_rooms.py`, `tests/js/space-snap.test.js`) and the golden capture pins segmentation against a
real 45-surface two-room fixture — but replayed offline, from a file, in a space that is not this one.

Two things make the run worth doing now rather than after more building:

- **Step 3 is gated on it by design.** Step 2's whole output is field data, and there is none. §1's ~9 cm
  is an aggregate from one space on one day.
- **This space has an answer key** ([`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md)):
  `floor_8` = living, `floor_10` = kitchen, `floor_32` = bedroom; ceilings `25`, `21`, `13`;
  `floor_32` ≡ `floor_8` as one continuous floor; `floor_10` +25 mm; `ceiling_21` genuinely sunken.

Every command below is copy-paste. `ctl` is the control plane — plain HTTP to the world server, **no LLM
in the loop**, so nothing here depends on a director behaving.

---

## Setup — do these in order

```bash
cd ~/Desktop/dev/Conjure
git checkout feat/rooms

# 1. BACK UP THE SPACE. A capture writes to it, and an empty-capture post has wiped a seed before.
cp ~/.local/share/conjure/users/daniel/spaces/space-1.json /tmp/space-1.backup.json

# 2. Start the world server. Note what is NOT here: --debug-registration adds ~6 fetches per capture and
#    perturbs what it measures (the mistake the jitter campaign paid for once). The room.residual lane is
#    always-on and change-gated, so it needs no flag.
.venv/bin/python -m conjure

# 3. In a SECOND terminal, keep this open — it is where every ctl command below runs.
cd ~/Desktop/dev/Conjure

# 4. Put the headset on, enter AR, and stand still for ~10 seconds so the space finishes loading.
#    (Two gates hold captures first: selection until the space vote resolves — "Finding which space
#    you are in…" — then load until 60% of the seed is present. Both are correct; just wait.)
```

Everything below assumes those two terminals. Server log = terminal 1. Commands = terminal 2.

---

## A — does any of it run?

The cheapest failure is that segmentation throws and is swallowed. **If A fails, stop** — every later
section is meaningless.

| # | Run this | Pass if | Result |
|---|---|---|---|
| A1 | watch terminal 1 while entering AR | a line like `[space] rooms: 3 group(s) → room_1, room_2, room_3` | ✅ `rooms=3 … 3 group(s) → room_1, room_2, room_3 (71 label ops)` 15:39:03 |
| A2 | `.venv/bin/python scripts/room_residuals.py` | **no** `room.error` section — if one appears it names which step threw | ✅ no `room.error` |
| A3 | `.venv/bin/python -m conjure.ctl rooms` | three rooms listed, each with a `floor=` | ✅ three rooms, each with a floor |

A2 needs no devtools on purpose. Reaching a Quest's console means `chrome://inspect` against a headset someone is wearing, which is not a step anyone performs mid-session — so a swallowed exception goes to the always-on geometry log instead, where a grep finds it afterwards.

**If A1 never appears but A2 is clean**, there are now three causes, in the order they bite on a cold
entry:

1. **The selection gate is still holding.** Added 2026-09-22 (`0cdccb3`): until the location fix lands and
   the space vote resolves, every capture is held — no registration, and no segmentation either, which is
   correct, since segmenting against a space you may not be standing in is the same mistake. The headset
   shows *"Finding which space you are in…"* and the log carries `space.selecting`. Wait for
   `space.selected`; it is usually a few seconds.
2. **The load gate is still holding** — *"Restoring your room…"*, below 60% of the seed. Also correct, and
   also transient.
3. **The POST gate never fired** — a settled space posts nothing. Until 2026-09-23 this was fatal rather
   than transient: every POST reason was a change to the SURFACES, and a fully-captured space has none,
   so its rooms were never sent at all. A segmentation the server lacks is now a reason in its own
   right, so this should self-resolve within a capture or two. If it does not, walk around to force a
   structural change and say so — the reason was added for exactly this run's failure.

Only the third needs anything from you. If the headset instead says *"step out of your play area and back
in"*, that is the genuine lost-lock case and section A has failed for a reason that has nothing to do with
rooms.

**If A3 says `no rooms recorded`:** the capture landed without a segmentation. Check A1 again.

---

## B — is the segmentation right?

```bash
.venv/bin/python -m conjure.ctl rooms
```

Expected shape (names will be `room #1` etc. until section D):

```
room #1 [room_1] floor=real_floor_8 — 24 surfaces (1 ceiling, 1 floor, 3 door, 8 wall, …)
    door real_door_54 → room #2
room #2 [room_2] floor=real_floor_32 — 19 surfaces (…)
…
unassigned: 2 — wall real_wall_77, wall real_wall_81
```

| # | Check | Pass if | Result |
|---|---|---|---|
| B1 | how many rooms | **3** | ✅ **3** |
| B2 | each room's `floor=` | one of `real_floor_8` / `real_floor_10` / `real_floor_32`, no repeats | ✅ `floor_32` / `floor_8` / `floor_10`, no repeats |
| B3 | ceiling per room | living↔`ceiling_25`, kitchen↔`ceiling_21`, bedroom↔`ceiling_13` | ✅ `32↔13`, `8↔25`, `10↔21` — exactly the ground truth |
| B4 | walls per room | each ≥ 4 | ✅ 8 / 9 / 7 |
| B5 | the `unassigned:` line | **only walls**, and write down how many | ✅ **zero unassigned**, 66/66 labelled |

**B5 is the number nobody has:** unassigned walls are expected — a wall too edge-on to place is refused
rather than guessed — but the *cost* of that conservatism has never been measured. If a floor, ceiling or
inset is unassigned, that is a bug, not a tolerance.

**If B1 says 2:** two rooms merged. Check whether one floor's rectangle covers another room, or a floor is
missing from the capture.
**If B1 says 4+:** the Quest split a floor. Over-segmentation is safe by design, but record it.

---

## C — connectivity

Same `ctl rooms` output, the indented `door … → …` lines.

| # | Check | Pass if | Result |
|---|---|---|---|
| C1 | interior doors | name a room (`→ room #2`) | ✅ bedroom↔living (both faces), living↔kitchen (both faces), bedroom↔kitchen |
| C2 | exterior doors | say `→ outside` | ✅ 4 doors + 6 windows → outside |
| C3 | the adjacency overall | matches how the three rooms actually connect | ✅ matches the dwelling; contents confirm it (bed / couch / 10 shelves) |

**If EVERY door reads `→ outside`:** the far-side probe is not clearing the partition. On the golden
fixture that was traced to the two floor rectangles overlapping ~0.6 m across their shared boundary, so a
single probe inside the band read the near room. It is fixed by stepping outward until one floor answers —
but the 1.8 m ceiling is a guess against real wall thicknesses, and this is where it gets tested.

---

## D — identity across sessions (the one that can lose data)

Membership is derived and recomputable. A **name** is user data with no recovery path, and identity
resolution is the only thing protecting it.

```bash
# D1 — name the room you are standing in (check `ctl rooms` for which room_N that is)
.venv/bin/python -m conjure.ctl name-room "room 1" kitchen
.venv/bin/python -m conjure.ctl rooms          # expect: kitchen [room_1] …
```

| # | Run this | Pass if | Result |
|---|---|---|---|
| D1 | `ctl name-room "room 1" kitchen` then `ctl rooms` | the *right physical room* is now `kitchen` | ✅ `room #3 → kitchen`, and `style kitchen` then hit 27 surfaces, all room_3 |
| D2 | take the headset off, put it back on, `ctl rooms` | still `kitchen`, same room | |
| D3 | Ctrl-C the server, restart it, re-enter AR, `ctl rooms` | still `kitchen` — proves it reached the space file | |
| D4 | see below | **still `kitchen`** | |

```bash
# D4 — the real test. Restart the server pretending that room's FLOOR was never captured, which forces
# identity onto surface-set overlap alone (tier 1) because the recorded defining floor is gone.
# Use the floor id `ctl rooms` printed for the kitchen.
.venv/bin/python -m conjure --drop-surface real_floor_8
# …re-enter AR, wait ~10s, then:
.venv/bin/python -m conjure.ctl rooms
```

**D4 is why the design has three tiers.** Keying identity on the defining floor — the obvious approach —
fails exactly here. If the name survives, tier 1 works on real geometry. If it reverts to `room #N`, or
worse appears on a *different* room, **record which** and stop naming things until it is understood.

Afterwards, restart the server normally (no `--drop-surface`).

---

## E — targeting

```bash
.venv/bin/python -m conjure.ctl style all --color green --room attic    # E3 — must REFUSE (safe, run first)
.venv/bin/python -m conjure.ctl style kitchen --color blue              # E1
.venv/bin/python -m conjure.ctl style wall --room kitchen --color red   # E2
```

> **Do not "reset" with `style all`.** The earlier version of this step said
> `style all --color "#888888"`, which would have destroyed 20 distinct director-set colours on this very
> space — yellow, orange, dark blue, browns. Snapshot and restore instead:
>
> ```bash
> # before
> python - <<'PY'
> import json, urllib.request
> d = json.load(urllib.request.urlopen("http://localhost:8080/world"))
> json.dump({e["id"]: ((e.get("components") or {}).get("material") or {})
>            for e in d["entities"] if (e.get("meta") or {}).get("real")}, open("/tmp/mat.json", "w"))
> PY
> # after: diff against /tmp/mat.json and PATCH back only what changed (44 surfaces, restored exactly)
> ```

| # | Run this | Pass if | Result |
|---|---|---|---|
| E1 | `ctl style kitchen --color blue` | **in the headset**, only that room turns blue | ✅ 20 surfaces, **all** room_3, none outside (its other 7 were walls restyled after) |
| E2 | `ctl style wall --room kitchen --color red` | only that room's walls | ✅ 8 magenta = exactly room_1's 8 walls, no non-walls, no other room |
| E3 | `ctl style all --color green --room attic` | prints `failed:` and **nothing changes** | ✅ refused, nothing changed — though it blamed the target, now fixed to blame the scope |
| E4 | `ctl style all --color "#888888"` | back to default | ⚠️ **do not run as written** — see below |

**Watch E1 through the headset, not the command output.** The count can be right while the wrong surfaces
are painted, and only passthrough shows that. E3 is the important one: an unresolvable room matching
*everything* is the one failure here that repaints surfaces you never mentioned.

---

## F — which room am I in

```bash
.venv/bin/python -m conjure.ctl rooms      # the room you are in shows "← daniel is here"
```

| # | Do | Pass if | Result |
|---|---|---|---|
| F1 | stand in each room in turn, run `ctl rooms` each time | the `← is here` marker names the room you are in | ✅ `← daniel is here` on room #3 (kitchen) — confirm that is where you were |
| F2 | walk to another room, wait ~2 s, run it again | it follows you | |
| F3 | take the headset off, run it again | **no** `← is here` marker anywhere | |

F3 is a real assertion: only an AR client reports a floor, so the honest answer for a CLI user is that
nobody is standing anywhere. A marker that persists after the headset is off means stale state.

---

## G — the measurement this was all for

```bash
# The reader. Prints every room's residual, flags the worst, and prints the floating-room detector's
# lines beside it — the two halves of G5.
.venv/bin/python scripts/room_residuals.py

# a previous day, if you are reading after the fact
.venv/bin/python scripts/room_residuals.py 2026-09-12

# raw, if you want everything the events carry
grep '"room.residual"' temp/geometry-$(date +%F).jsonl
```

Expected shape:

```
=== room.residual — 2026-09-12 (3 events) ===
  room            n walls    before     after       dx       dz       dy  would_apply
  room_1         24     8     0.031     0.008   -0.021    0.011        -  True
  room_2         19     7     0.094     0.012    0.088   -0.019   -0.104  True

  worst room: room_2 at 0.094 m
```

| # | Question | Record | Result |
|---|---|---|---|
| G1 | What is `before` per room? | the numbers | **~0.015 m settled.** 0.15–0.17 m is the tracking transient; all three rooms decay together (×0.72/capture) to 1.5 cm |
| G2 | Is `after` < `before`, by how much? | the ratio | ✅ roughly halves it — 0.015 → 0.007 |
| G3 | Is one room consistently worst? | which | **room_1 (bedroom), worst in 7 of 7 epochs** — the same room `level.anomaly` names |
| G4 | Does a settled space stop emitting? | count lines, wait 2 min, count again — should not grow | ✅ settled — 24 events over 16 s, then silence |
| G5 | Does `dy` name the same room as `level.anomaly`, at a similar size? | both | **agree on the room, differ ~5× on size**: settled `dy` −9 mm vs `level.anomaly` −51 mm on `floor_32` |

**G1 is the decision.** 1–2 cm ⇒ the effect is real but not worth correcting, and step 3 should be
dropped. ~9 cm ⇒ it is eating most of `matchWall`'s 150 mm identity budget, and step 3 is justified.

**G5 is the sharpest reading available.** `level.anomaly` (live-minus-seed heights, median-subtracted) and
`room.residual`'s `dy` (a fit over that room's horizontals) share **no code path**. If the raised-floor
fault is active they should name the same room at a similar magnitude. Agreement validates both.
Disagreement says which room to look at — more than either gives alone. If the fault is *not* active,
`dy` should be small everywhere and `level.anomaly` silent, which is also a result.

---

## H — does a branch that predates rooms strip the labels? (optional, once)

**Superseded in its original form.** H used to ask whether `main` — which then had no rooms code — would
drop `meta.room` and `space.rooms` from the shared space file. Since the merge there is no such `main`, so
that version of the question answers itself.

The hazard moved rather than went away. **Seven unmerged branches predate the rooms work and share the same
live data dir**, and one of them is `feat/fix-floating-rooms` — the branch step 3 is most likely to reach
for, since it carries the floating-room corrector. So the check is worth running once, against any of them,
rather than discovered later by a lost room name.

It is **not** covered by D3. That restarts the same server and proves the record round-trips to disk; this
asks whether a writer that has never heard of `rooms` leaves it alone.

```bash
git stash list >/dev/null; git checkout feat/fix-floating-rooms
.venv/bin/python -m conjure            # enter AR, wait ~15 s, Ctrl-C
git checkout main
.venv/bin/python -c "
import json, os
d = json.load(open(os.path.expanduser('~/.local/share/conjure/users/daniel/spaces/space-1.json')))
print('rooms record:', len(d.get('rooms') or {}), 'rooms')
print('surfaces with meta.room:', sum(1 for s in d['surfaces'] if (s.get('meta') or {}).get('room')), '/', len(d['surfaces']))"
```

| # | Check | Pass if | Result |
|---|---|---|---|
| H1 | the old branch starts and the space loads | no error | |
| H2 | `rooms record:` | the same count as before the old-branch run | |
| H3 | `surfaces with meta.room:` | unchanged | |
| H4 | back on `main`, `ctl rooms` | names intact, no re-mint | |

**If H2 or H3 dropped,** that branch cleared what it did not recognise, and the identity tiers stop being a
hedge: tier 2 recovers the rooms by floor id on the next capture, which is what H4 shows. Then either merge
`main` into those branches before using them, or accept the re-derivation each time.

## If something goes badly wrong

```bash
# restore the space from the backup taken in Setup
cp /tmp/space-1.backup.json ~/.local/share/conjure/users/daniel/spaces/space-1.json
```

---

## Where it stands — 2026-09-23

**A, B, C, E and G are done and pass.** Segmentation reproduces the ground truth exactly: three rooms,
`32↔13` / `8↔25` / `10↔21`, 66 of 66 surfaces labelled, zero unassigned, and a door graph that matches the
dwelling from both faces of every interior doorway.

**The headline number is G1, and it argues against step 3.** Settled inter-room residual is **~1.5 cm**,
not the 15–17 cm a first reading suggests: that is the tracking transient, and all three rooms decay
together on one curve to the same value, which is a whole-space settle rather than per-room non-rigidity.
This document's own criterion is *"under ~0.02 m, the effect is real but not worth correcting"*. The
refinement does halve what remains (0.015 → 0.007), so the finding is "real and small", not "absent".

Anyone reading a single `room.residual` row will over-read it by an order of magnitude. **Read the last
epoch, not the first.**

**G3 and G5 are the interesting pair.** The bedroom is worst in 7 of 7 epochs, and `level.anomaly` names
the same room by a completely separate path — but at −51 mm against the fit's settled −9 mm. They agree on
*which* room and differ ~5× on *how much*, which is exactly the disagreement G5 was built to surface. That
is the open question this run produced.

**Still to run:** D2–D4 (does a name survive a re-entry, a restart, and losing its defining floor), F2–F3,
and H. D is the only section where failure loses something unrecoverable.

Two defects this run found, both fixed: segmentation ran after the capture pass strips `_lp`/`_lq` off the
surface array, so it silently found nothing on every capture; and an unresolvable `room=` blamed the target
rather than the scope.

## What a good run produces

Not a pass/fail. Three artifacts:

1. **A number for G1** — per-room residuals from a real space, the input step 3 was deferred for.
2. **A verdict on D4** — whether identity survives losing a room's defining floor.
3. **A count for B5** — how many walls the conservative facing rule declines to place.

Anything else that breaks is a bug to file, not a reason to stop the run.
