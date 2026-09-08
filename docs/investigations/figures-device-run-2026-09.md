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
| A7 | **Grab her while she is kneeling** | *predicted problem.* `grab` selects using `meta.bbox`, which is the BIND-pose box; the settle offsets the mesh inside the entity, so the selection box should sit ~43 cm off her body. If it does, that is a real finding and the same stale-box family as the original `grab` bug | |

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
| B5 | On **Trish**: `bow` | **expected to barely bow.** Her spine bones are siblings rather than a chain, so the mapped `spine` carries her waist and not her shoulders. Confirms a known rig defect on device | |
| B6 | `sit` on any rig | the *shape* of sitting, seated on nothing. Put a chair under her by hand and see how far off she is — that gap is the size of the tier-3 problem | |

---

## C. The seam

| # | Do | Expect | Saw |
|---|---|---|---|
| C1 | Watch the browser console while posing | **no** `Unknown property named for component figure`. That warning was the bug fixed in `8648ef8`; its absence is the check | |
| C2 | `inspect_figure` after a named pose (ask "what can you tell me about her?") | reports the posed bones; watch the `->` line in Terminal 2 for the raw reply | |
| C3 | Pose two figures at once, differently | no cross-talk; each holds its own | |
| C4 | Frame cost with 2–3 figures posed | no new stutter. [`investigations/pops-and-jitters.md`](./pops-and-jitters.md) says dropped frames are already live on this hardware — the question is whether posing adds any | |

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
description did not steer it.

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
