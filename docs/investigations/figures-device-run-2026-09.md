# Device run — named poses and re-grounding (2026-09-09)

**SPENT.** The run is complete and its findings are integrated. This file is kept only as the raw
record — what was asked and what was seen, verbatim. **Do not read it for conclusions**; those are in
[`backlogs/figures.md` § *Device run 2026-09-09*](../backlogs/figures.md), with the verified behaviour
and the new limits in [`specs/figures.md`](../specs/figures.md).

It never met [this folder's bar](./README.md) and does not now: it is a test log, not a debugging
campaign, and it produced no rejected hypotheses. It survives because the raw observations are hard to
paraphrase and one of them — D5 — is worth reading in full.

**Headline.** 22 checks. Re-grounding passed completely, including surviving a room recapture and a
reload. The tool surface passed: plain English reaches a named pose, overrides arrive in one call, and
the three utterances that failed on 2026-09-03 are correct. Posing costs no measurable frames. What it
found instead: **the poses are geometrically right and the mesh interpenetrates** — arms enter the body,
crossed arms fold inside the chest, `sit` floats above a seat — and none of it is catchable by a
geometric signature.

The method, for anyone repeating it: `python -m conjure.cli -v` shows each tool call; `[patch]` in
`temp/conjure.log` proves a curl-driven pose landed; `scripts/c4_frame_cost.py` runs the frame-cost A/B
hands-free and prints a window to hand over.

## What was asked, and what was seen

| # | Check | Observed |
|---|---|---|
| A1 | Place a figure, note where her feet meet the floor. `named: "kneel"` | kneeling looks good except for Trish, whos legs bones look deformed but in the right general orientation |
| A2 | `named: "cheer"` from standing | looks good, and from kneeling she raises her arms too but remains kneeling |
| A3 | `kneel`, then `kneel` again, then a third time | looks good |
| A4 | `kneel` → `stand` → `kneel` | looks goos |
| A5 | **Kneel her, then walk around and let the room recapture** | looks good |
| A6 | Reload the page with her kneeling | looks good |
| A7 | **Grab her while she is kneeling** | grabbing Grace (and others) while kneeling works as expected, though the selection box is the same as when she is standing - wide enough to accomodate arms slightly spread but not deep enough to enclose her bent legs while kneeling |
| B1 | All 13 on Grace | when standing, arms are straight down and go into her body a little; hands on hips, close but hands don't quite touch, arms are crossed but inside her chest, waving looks more like an upper outward reach, pointing is reaching not pulling the other fingers in, bow looks good |
| B2 | `crouch` then `sit`, back to back | distinguishable, crouching is leaned forward a little |
| B3 | `arms-crossed` | again, arms folded but inside her chest |
| B4 | On **Saka** (VRoid, rests in a T-pose): `kneel`, `wave`, `point` | kneeling her arms are straight down, they enter her hips a little; no scarecrow in any of those three poses for saka, again pointing and waving look a little more like reaching |
| B5 | On **Trish**: `bow` | confirmed, she is pushing her chest forward instead of bowing |
| B6 | `sit` on any rig | Grace sits about an inch above the chair, saka sits several inches above the chair (even though she is shorter) , but the chair can be accomodated to fit by scaling with grab tool |
| C1a | Mac browser console while posing | no unknown property displayed in browser console |
| C1b | `[figure]` lines in the log | I saw [figure] posed 7 bone(s) on grace_new, [figure] posed 10 bone(s) on sak, no failure mode error displayed in console |
| C2 | `inspect_figure` on a posed figure | **Spine** — bent/adjusted (torso not fully upright); **Both upper arms** — moved from rest (arms not at sides); **Both upper legs** — adjusted (legs not in a neutral stand); **Both lower legs** — bent (knees have some flex) — **and it never says she is kneeling**, which confirms the predicted gap: `named` is stored and not read |
| C3 | grace `kneel` + trish `cheer` | **state layer passed** — saka `sit` + 5f83f1 `stand`: distinct stored state, separate `[patch]` ops each naming its own id. Render looks good |
| C4 | `python scripts/c4_frame_cost.py`, then hand over the window it prints | **passed, with a caveat.** 15:21–15:24, Trish + Grace + Saka (226k tris), one client at 90 Hz. Still: jit 1.58 → 1.60, posed mean a hair *faster*. Walking: jit +5% and drops +11% but `late` down 31% — two metrics disagreeing in direction on 22 samples is noise. `rebuilds=0` throughout. **Caveat:** the baseline is ~31 ms against an 11.1 ms budget, so a 1 ms cost would be invisible — see [`figures-frame-rate.md`](./figures-frame-rate.md) |
| D1 | "have her kneel" | 2026-09-09 15:42:27 [builder.claude/tool] pose_figure({"id": "trish", "named": "kneel"}) 2026-09-09 15:42:27 [builder.claude/tool]   -> kneel. |
| D2 | "have her sit down" | 2026-09-09 15:44:29 [builder.claude/tool] pose_figure({"id": "trish", "named": "sit"}) 2026-09-09 15:44:29 [builder.claude/tool]   -> sit. She needs something under her — a seat is tier 3, so place a |
| D3 | "have her kneel with her arms out" | 2026-09-09 15:45:20 [builder.claude/tool] pose_figure({"id": "trish", "named": "kneel", "pose": {"leftUpperArm": {"aim": "out"}, "rightUpperArm": {"aim": "out"}}}) 2026-09-09 15:45:20 [builder.claude/tool]   -> Moved leftFoot, leftLowerArm, leftLowerLeg, leftUpperArm, leftUpperLeg, rightFoot, rightLowerArm, rightLowerLeg, rightUpperArm, rightUpperLeg, spine. |
| D4 | "stand her back up" | 2026-09-09 15:47:08 [builder.claude/tool] pose_figure({"id": "trish", "named": "stand"}) 2026-09-09 15:47:08 [builder.claude/tool]   -> stand.; n |
| D5 | "have her lie down" | "There isn't a built-in "lie down" pose, but I can approximate it by bending the spine and arms to simulate lying flat. Let me try this with manual bone adjustments."; 2026-09-09 15:48:48 [builder.claude/tool] list_poses({}) 2026-09-09 15:48:48 [builder.claude/tool]   -> Poses for pose_figure(id, named=…): 2026-09-09 15:48:52 [builder.claude/tool] pose_figure({"id": "trish", "named": "stand", "pose": {"spine": {"bend": 90}, "leftUpperArm": {"aim": "out"}, "rightUpperArm": {"aim": "out"}, "leftUpperLeg": {"aim": "down"}, "rightUpperLeg": {"aim": "down"}}}) 2026-09-09 15:48:52 [builder.claude/tool]   -> Moved chest, head, hips, leftFoot, leftHand, leftLowerArm, leftLowerLeg, leftShoulder, leftToes, leftUpperArm, leftUpperLeg, neck, rightFoot, rightHand, rightLowerArm, rightLowerLeg, rightShoulder, rightToes, rightUpperArm, rightUpperLeg, spine. Joint limits applied: spine.bend +90° → +50°. 2026-09-09 15:49:29 [figure] posed 5 bone(s) on trish; she's as close to lying down as the rig allows — bent forward at the waist with arms and legs extended. The figure rig doesn't fully support a flat lying pose, so it's a best approximation! |
| D6 | The three 2026-09-03 failures verbatim: "raise her arm up", "point her arm down", "spread her legs apart" | those were all good in the headset |
