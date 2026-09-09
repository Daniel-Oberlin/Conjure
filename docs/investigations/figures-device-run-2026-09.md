# Device run — named poses and re-grounding

**Status: planned, not yet run.** Fill in the *Saw* column during the run.

*This does not yet meet this folder's bar.* [`README.md`](./README.md) says a doc earns a place here
once it has produced **negative knowledge** — a hypothesis killed, a fix rejected — and carries Symptom
/ Experiments / Tried and rejected / Remaining theories / Fixes shipped. This is a pre-registration: the
predictions are written down *before* the run so the run cannot quietly become a demo. Afterwards it
either grows those sections and stays, or it has found nothing, in which case the findings go to
[`backlogs/figures.md`](../backlogs/figures.md) and this file is deleted rather than kept as a passed
checklist.

Slice 3 (13 named poses, re-grounding) is built and verified in Python, Blender and stubbed-A-Frame unit
tests. **No part of it has executed in a headset.** Re-grounding in particular is client code whose tests
replace A-Frame entirely, and the one bug already found at that seam — the server sending a `named`
property the component never declared, so A-Frame dropped it silently — was found by reading, not by
running.

---

## 0. Before the headset

```bash
python -m conjure.ctl refresh-models          # FRAME_REV is 9; do this after any restart
python -m conjure.ctl world                   # confirm which world is active
```

**Re-place every figure.** A placed figure holds the meta it was placed with, and nothing already in a
world has been through today's code. An old entity will look like a bug that is really a stale row.

**Tooling gap, and it decides how this run feels.** There is no hands-free way to drive poses:
`conjure.ctl` has no `figure` verb, so it is either the director (an LLM in the loop — fine for §D,
wrong for §A where you want determinism) or one `curl` per pose:

```bash
curl -s localhost:8080/figure -H 'content-type: application/json' \
     -d '{"id":"grace","named":"kneel"}' | jq
```

`/figure` is owner-gated but only rejects a *mismatched* `X-Conjure-User`, so plain `curl` passes. One
command per pose means taking the headset off between poses, which is exactly the friction that makes a
run get cut short. **A small driver that cycles the library on a timer would fix that** — not built.

---

## A. Re-grounding — the untested code, and the priority

Measured expectations, from `scripts/pose_library.py`. These are the lift the client should apply; a
figure that ends up **45 cm in the air** is the bug this exists to prevent, and it is the *floating*
direction, not sinking.

| pose | Grace | Trish | Saka |
|---|---|---|---|
| `kneel` | −43 cm | −49 | −44 |
| `kneel-one` | −29 | −34 | −26 |
| `crouch` | −41 | *(signature fails, below)* | −37 |
| `sit` | −37 | −43 | −33 |
| every arm pose, `stand` | **0** | 0 | 0 |

| # | Do | Expect | Saw |
|---|---|---|---|
| A1 | Place a figure, note where her feet meet the floor. `named: "kneel"` | knees on the floor, shins along it. Not hovering, not sunk to the hips | kneeling looks good except for Trish, whos legs bones look deformed but in the right general orientation |
| A2 | `named: "cheer"` from standing | she does **not** move vertically at all | looks good, and from kneeling she raises her arms too but remains kneeling |
| A3 | `kneel`, then `kneel` again, then a third time | no creep. Same height every time — guards the accumulator | looks good |
| A4 | `kneel` → `stand` → `kneel` | back to the floor, then back to the same kneel height | looks goos |
| A5 | **Kneel her, then walk around and let the room recapture** | she stays put. *This is the real unknown:* the settle moves the MESH inside the entity while the anchor solver sets the ENTITY, so they should not fight — "should" is the word that has been wrong here repeatedly | looks good |
| A6 | Reload the page with her kneeling | comes back kneeling, at the same height | looks good |
| A7 | **Grab her while she is kneeling** | *predicted problem.* `grab` selects using `meta.bbox`, which is the BIND-pose box; the settle offsets the mesh inside the entity, so the selection box should sit ~43 cm off her body. If it does, that is a real finding and the same stale-box family as the original `grab` bug | grabbing Grace (and others) while kneeling works as expected, though the selection box is the same as when she is standing - wide enough to accomodate arms slightly spread but not deep enough to enclose her bent legs while kneeling |

---

## B. Do the poses read as themselves at human scale?

A 512-pixel clay render cannot answer this and a person standing next to her can. Cycle all 13 on
**Grace** first (A-posed, realistic proportions), then spot-check the two below.

| # | Do | Expect | Saw |
|---|---|---|---|
| B1 | All 13 on Grace | each reads as its name without being told | when standing, arms are straight down and go into her body a little; hands on hips, close but hands don't quite touch, arms are crossed but inside her chest, waving looks more like an upper outward reach, pointing is reaching not pulling the other fingers in, bow looks good |
| B2 | `crouch` then `sit`, back to back | distinguishable. A vision model confused these two — the question is whether a person does | distinguishable, crouching is leaned forward a little|
| B3 | `arms-crossed` | folded, not clasped, not surrender. Took four authoring passes; the last one only just crosses the midline | again, arms folded but inside her chest |
| B4 | On **Saka** (VRoid, rests in a T-pose): `kneel`, `wave`, `point` | arms at her sides, not straight out. This is the scarecrow fix — the defect the visual check found | kneeling her arms are straight down, they enter her hips a little; no scarecrow in any of those three poses for saka, again pointing and waving look a little more like reaching |
| B5 | On **Trish**: `bow` | **expected to barely bow.** Her spine bones are siblings rather than a chain, so the mapped `spine` carries her waist and not her shoulders. Confirms a known rig defect on device | confirmed, she is pushing her chest forward instead of bowing |
| B6 | `sit` on any rig | the *shape* of sitting, seated on nothing. Put a chair under her by hand and see how far off she is — that gap is the size of the tier-3 problem | Grace sits about an inch above the chair, saka sits several inches above the chair (even though she is shorter) , but the chair can be accomodated to fit by scaling with grab tool |

---

## C. The seam

Everything here is about whether server state arrives intact and cheaply. **The client logs its own
diagnostics to the server**, so most of this is read from `temp/conjure.log` rather than a browser —
`figure.js` posts to `/client_log` under the tag `figure`, gated on `window.CONJURE_DEBUG_LOG` (baked
into the page from `debug_log`, default on).

**Setup.** Restart the server with the frame probe on, because C4 needs it and it costs nothing when
idle:

```bash
python -m conjure --debug-jitter
tail -f temp/conjure.log | grep -E "\[patch\]|\[figure\]|\[bcast\]|/tool|PACE|RATE"
```

### C1 — does the pose's NAME reach the client?

Two halves, and they want different instruments.

**The failure signal is console-only.** A-Frame drops an undeclared component property with
`Unknown property \`named\` for component \`figure\`` — that is A-Frame's own warning, so it goes to the
browser console and nowhere else. **Do this half in the Mac browser** (⌘⌥J → Console), not the headset:
it is a wire-and-schema question, not an XR one, and Quest remote debugging costs ten minutes to set up
for the same answer. This is the bug fixed in `8648ef8`; its **absence** is the check.

**Which line proves it depends on how you drove it.** A `curl` straight to `/figure` produces
`[patch] update <id> found=true {components.figure}` and **nothing else** — no `/tool` line, because there
is no director in the loop, and often no `[figure]` line either (see `_once` below). Driving it by voice
or REPL adds the `/tool` pair. The curl's own JSON reply is the better signal in any case: it is
synchronous and says more than the log, including `needs` and `skipped`.

**The success signal from the client.** Pose a figure and watch for:

```
[figure] posed 7 bone(s) on grace
```

Two things about that line. It fires **once per component instance** (`_once`), so re-posing the same
figure will *not* log again — reload the page to get a fresh one. And the count is the number of bones
that actually resolved, so it is also the check that a named pose reached the whole figure: `kneel` is 11
bones on a rig that has them all, and fewer means bones were skipped.

The failure line names what it could not find:

```
[figure] NO BONE OR AXES for rightToes on grace — map has 21 entries, axes 21, model has 489 bones
```

### C2 — what `inspect_figure` says about a posed figure

In the `-v` REPL (§D setup), ask *"what can you tell me about her?"*. The director's prose is not the
evidence — read the raw tool reply on the `->` line in the log.

**Predicted gap, worth confirming rather than assuming:** it will report *"Currently posed:
leftLowerLeg, leftUpperLeg, …"* — the bone names — and **not** that she is kneeling. The server stores
`named` beside the expansion for exactly this reason, and `inspect_figure` does not read it. If that is
what you see, the semantic half of the state is being kept and then not used, and the fix is one line in
`figure_description`.

### C3 — two figures, posed differently, no cross-talk

```bash
curl -s localhost:8080/figure -H 'content-type: application/json' -d '{"id":"grace","named":"kneel"}'
curl -s localhost:8080/figure -H 'content-type: application/json' -d '{"id":"trish","named":"cheer"}'
```

Three checks, cheapest first: the log shows a `[figure]` line naming **each id separately**; the stored
state differs per entity —

```bash
curl -s localhost:8080/world | jq '.entities[] | select(.components.figure)
    | {id, named: .components.figure.named, pose: (.components.figure.pose|fromjson|keys)}'
```

— and your eyes agree that one is kneeling and one has her arms up. The failure this looks for is a
component written to the wrong entity, or one figure's axes resolving against another's map.

### C4 — frame cost

**Run it with `scripts/c4_frame_cost.py`.** The measurement needs two matched phases — the same figures
unposed then posed, standing still then walking the same route — and the phase boundaries have to be
findable in the log afterwards. You cannot type a marker while wearing a headset, and a boundary
reconstructed from memory later is worth very little. So the script drives the whole thing: it **speaks**
each instruction through macOS `say`, writes an exact marker into the log at every boundary, and poses
the figures itself between the phases.

**Why a baseline at all:** [`pops-and-jitters.md`](./pops-and-jitters.md) established that dropped frames
are already present on this hardware as a platform characteristic. The question is not "are there drops"
but "did posing add any", and that is only answerable against the same room, same route, minutes apart.

**Steps.**

1. Server running with `--debug-jitter`. The script refuses to start without it, because a run with no
   `PACE` lines produces a confident-looking nothing.
2. Figures placed and in view. Mac audio somewhere you can hear it.
3. On the Mac: `python scripts/c4_frame_cost.py`
4. Put the headset on and enter AR. Do what it says: stand still ~30 s, then walk your route ~40 s.
   It poses the figures, then asks for the identical still-and-walk again.
5. **Walk the same route both times.** Drops correlate with head translation, so a different path is a
   different experiment.
6. It prints a time window at the end. Hand that over — *"read C4 from the log, window HH:MM to HH:MM"* —
   and the `PACE` lines get read for you.

**What is being compared**, for the record: `jit(sd)` (frame-interval standard deviation, the real
smoothness metric), `late`, `drop`, `rebuilds` and `heap`, baseline versus posed, still and walking kept
separate. A pose is a one-off quaternion write per bone with no per-frame work, so the honest expectation
is **no difference at all**. A rise in `jit`/`drop` between the phases would mean something in the pose
path is running every frame — exactly the hazard the design flagged (*"the mixer rewrites bones every
frame"*).

| # | Do | Expect | Saw |
|---|---|---|---|
| C1a | Mac browser console while posing | **no** `Unknown property \`named\`` warning | no unknown property displayed in browser console |
| C1b | `[figure]` lines in the log | `posed N bone(s) on <id>`, N = the pose's bone count; no `NO BONE OR AXES` | I saw [figure] posed 7 bone(s) on grace_new, [figure] posed 10 bone(s) on sak, no failure mode error displayed in console |
| C2 | `inspect_figure` on a posed figure | posed bone names — and probably **not** the pose's name (gap above) | **Spine** — bent/adjusted (torso not fully upright); **Both upper arms** — moved from rest (arms not at sides); **Both upper legs** — adjusted (legs not in a neutral stand); **Both lower legs** — bent (knees have some flex) — **and it never says she is kneeling**, which confirms the predicted gap: `named` is stored and not read |
| C3 | grace `kneel` + trish `cheer` | separate log lines, distinct stored state, both correct in the headset | **state layer passed** — saka `sit` + 5f83f1 `stand`: distinct stored state, separate `[patch]` ops each naming its own id. Render looks good |
| C4 | `python scripts/c4_frame_cost.py`, then hand over the window it prints | no change in `jit(sd)` / `late` / `drop` between baseline and posed | |

---

## D. The tool surface, which the eval harness cannot test

The harness measures `pose_figure`'s *description*. `named` and `list_poses` are new and have never been
in front of a director with a real world behind it.

**How to run it.** Two terminals on the Mac, headset on:

```bash
python -m conjure.cli -v                                    # the director; -v prints each tool call
tail -f temp/conjure.log | grep -E "/tool|\[figure\]"       # the same calls PLUS their results
```

`-v` is the whole trick — without it the REPL prints only the conversation, and which tool was called is
invisible. Each call appears as one line, straight from the director's own `on_tool` callback:

```
  · pose_figure({"id": "grace", "named": "kneel"})
```

**That line is the evaluation.** A pass is `named` carrying the work; a fail is a `pose` dict of seven
invented bone rotations, which means the director ignored the library and improvised — the tool
description did not steer it. Per row:

| Row | A pass looks like | A fail looks like |
|---|---|---|
| D1 | `pose_figure({"id":…,"named":"kneel"})` | a `pose` dict of bone entries, no `named` — it improvised instead of using the library |
| D2 | `named:"sit"` | `named` right but the seat caveat never reaches you — compare the `->` line against what it says back |
| D3 | **one** call carrying both `named` and `pose` | two calls, or `named` dropped and the whole thing hand-built |
| D4 | `named:"stand"` | `clear:true` — close, and wrong: that returns her to the FILE's bind pose, which on a VRoid rig is a T-pose |
| D5 | a refusal you can act on | a call that lands silently at the ±45° hip clamp and looks like nothing happened |
| D6 | `{"aim":"up"}` on the right bone | wrong bone, or an inverted sign — the 2026-09-03 failure exactly |

Terminal 2 adds what the REPL omits: the tool's **reply**, on the `->` line. That is where D2's evidence
lives (`She needs something under her — a seat is tier 3…`), because the question there is whether the
director passes that on to you or swallows it, and only the two side by side can say.

Logging is gated on `debug_log` (default on). An empty Terminal 2 means that flag, not an absent call.

**And the headset is the other half.** The REPL says what was *asked*; only the headset says whether she
did it. A call that reads perfectly and produces nothing visible is the failure this feature keeps
rediscovering.

| # | Say | Expect | Saw |
|---|---|---|---|
| D1 | "have her kneel" | one `pose_figure(named="kneel")` — **not** seven invented bone rotations | |
| D2 | "have her sit down" | `named="sit"`, and the reply passes on that she needs something under her | |
| D3 | "have her kneel with her arms out" | one call: `named="kneel"` plus a `pose` override | |
| D4 | "stand her back up" | `named="stand"`, and she actually stands — not just her arms dropping | |
| D5 | "have her lie down" | *not expressible.* `hips` is clamped to ±45°. Watch what it does instead — a graceful refusal or a silent nonsense pose is the difference between a limit and a bug | |
| D6 | The three 2026-09-03 failures verbatim: "raise her arm up", "point her arm down", "spread her legs apart" | all three correct. The harness says they pass; the headset is what they failed in | |

---

## Expected failures — so they are not surprises

- **`crouch` on Trish** — signature fails (her torso will not lean; same spine defect as B5).
- **Grace's legs, Yuffie's torso** — materials still wrong, unrelated to this work.
- **Tamaki** — still cannot be posed at all.

## What to write down

For each row: what it actually looked like, not just pass/fail. Any console warning, verbatim. If
something is wrong, the useful thing is usually the *number* — how far off the floor, how many
centimetres of creep — because that is what separates a stale box from a wrong axis.
