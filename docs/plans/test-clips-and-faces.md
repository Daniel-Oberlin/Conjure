# Plan — test clips and faces on a headset

**Status:** all phases open · **Opened:** 2026-09-17

**This file is temporary.** It exists to hold one verification sequence across areas the specs keep
apart, and is deleted once every phase has settled. Each phase names where its result goes: a
confirmation to [`specs/figures.md`](../specs/figures.md), a defect to
[`backlogs/figures.md`](../backlogs/figures.md), a debugging campaign that produced negative knowledge
to [`investigations/`](../investigations/).

**Dissolves to:** `specs/figures.md` §8b/§8c/§8d/§8e (confirmations, and any measurement that changes
what those sections claim) · `backlogs/figures.md` (whatever fails) · `investigations/facial-expression.md`
(anything that needs a campaign rather than a fix).

---

## 0. Why this plan exists

Everything below was built and verified **numerically or in Blender**, and none of it has been seen on
a headset. That is the whole gap. Specifically:

| built | how it was verified | what is missing |
|---|---|---|
| `set_expression` (§8d) | two geometric checks over the corpus; Blender renders of 8 expressions × 2 rigs | never applied to a live figure in a world |
| facial clips playing natively | name-resolution and track counts — `1_idle` drives 62 facial bones, all present on Barbie | nobody has watched a captured figure blink |
| facial retarget (§8e) | carried lid swings 26.38°, bit-identical to the source; 46 unit tests | never rendered, let alone worn |
| clip playback at all | `clip_diff.mjs` through the client's own code | **`backlogs/figures.md` still says clip playback has not been run on a headset** (2026-09-13) |

The honest framing: this is not "confirm it works". Four separate things could be wrong, and the plan
is ordered so that a failure in an early phase explains the later ones rather than being confused with
them.

**A render is not a headset.** `scripts/glb_preview.py --morph --head` renders an expression through
Blender, and `clip_diff.mjs` plays a clip through the client's own `retarget()`. Both have been green
on things the headset later disagreed with — the residual-swing chase, the 97% stall, the bride's
eyelid. Treat every green below as "not yet contradicted".

---

## 1. Prerequisite — the catalog is one revision behind

**Measured just now: 38 rigged figures, `frame_rev` 18, and NONE carry the face attributes.**
`FRAME_REV` went to 19 with the face work, so `morph_names`, `expression_scheme` and `facial_morphs`
do not exist on a single row yet. Until they do, `set_expression` will refuse every figure with
*"has no morph targets recorded"* — which looks exactly like the feature not working.

```
python3 -m conjure.ctl refresh-models
```

**Expect:** every rigged row re-derived, `frame_rev` 19. Then confirm from the library side, which is
the query the whole of §8d exists to make answerable:

```
python3 scripts/faces.py
python3 scripts/faces.py --check         # both geometric checks over the corpus
```

**Expect:** Saka `57 / 57 / vrm`, Alice `34 / 26 / cc`, everyone else `facial 0`. `--check` reports OK
for those two and `n/a` for the three with a single stray shape.

**If `facial` is 0 for Saka:** stop. Nothing downstream can work, and the fault is in extraction or in
the refresh, not in the runtime.

---

## 2. Faces, on the two figures that have them

`set_expression` reaches morph targets, and only Saka and Alice carry any. This phase is the cheapest
and the most likely to work, which is why it is first.

Place Saka, then:

| call | expect |
|---|---|
| `inspect_figure` | a **Face** line listing what she can do. If it is absent, `meta.morph_names` did not travel — place her again after the refresh |
| `set_expression(id, {"blink": 1})` | both eyes shut, and STAY shut |
| `set_expression(id, {"neutral": 1})` | back to rest |
| `set_expression(id, {"joy": 1})` | mouth open, eyes curved, brows up — visibly more than `smile` |
| `set_expression(id, {"smile": 0.4})` | a slight version of the same shape, not a different one |
| `set_expression(id, {"surprised": 1, "aa": 1})` | one face, not the last one to arrive — `Fcl_MTH_A` and the surprise compose |
| `set_expression(id, {"look_left": 1})` | **refused in words.** VRM aims eyes with bones; a silent no-op here is a bug |
| `set_expression(id, clear=true)` | rest |

Then Alice, where `look_left`/`look_right`/`look_up`/`look_down` **should** work — she is the only
figure in the catalog with eye-direction morphs.

**And a reload.** The expression lives on the entity, so re-entering should restore it. This is the
half that a unit test cannot reach.

**What a failure means, by shape:**

- *Nothing visibly changes but the reply says it applied* → the weights reached the store and not the
  mesh. Look for `NO SUCH MORPH TARGET` or `NO MORPH TARGETS` in the client log; `figure-face` logs
  once per model and says which of those two it is.
- *The whole figure distorts* → a target is being driven that is not facial, i.e. `role_of` is
  mis-classifying. That is `investigations/facial-expression.md` territory, not a quick fix.
- *An expression looks wrong rather than absent* → the Character Creator emotion composites are the
  least-evidenced thing in the module (`expressions.PROVENANCE` says so). Record what it looks like;
  a composite is a table entry and cheap to change.

---

## 3. Clips, and whether the face comes with them

**This is the phase that has never been run at all**, per `backlogs/figures.md`.

Use a figure from the capture set — Barbie, Akari, Teacher, any of the 16 sharing rig signature
`81b51a075b`. Their clips are **native**: the same skeleton, bound by name, nothing retargeted.

```
list_clips(id)
play_clip(id, "1_idle")
```

| expect | because |
|---|---|
| the body moves | this is §8b, and it is the thing that has never been watched |
| **she blinks** | `1_idle` spends 9,031° on facial bones with `DEF-lid.T.L`/`.R` as its top two movers |
| `play_clip(id, "1_idle_speaking")` moves her **jaw** | 74.9% of that clip's motion is in `DEF-jaw_master` — it is recorded lip sync |
| `stop_clip(id)` returns her to her pose, not to the clip's last frame | `restore()` exists for exactly this |

**The blink is the claim worth the trip.** If the body moves and the face does not, the tracks are
being dropped somewhere between the file and the mixer — and since nothing retargets here, that is a
client-side binding question with a short list of causes.

Also worth one measurement while you are there: `pc_squint` is a **no-op** by measurement (0.13° from
rest). If it visibly does something, the travel metric is lying and §8e's numbers need re-checking.

---

## 4. The facial retarget — the part most likely to be wrong

§8e carries facial bones across rigs by local delta, gated per bone. Verified numerically and **not
once rendered**.

The valuable direction is **inward**: office-babe's rig has 21 clips that drive a face, and they carry
onto all 16 capture figures. So place **Barbie** and play a clip that belongs to **office-babe's** rig
(`list_clips(id, all=true)` will offer them as retargetable).

| expect | if not |
|---|---|
| the body performs | if the body is wrong, stop — this is §8c, not the face |
| eyelids and jaw move | 8–16 lids and 6 jaw bones are carried; the server's notes say how many |
| it does not look *wrong* — a lid closing sideways, a jaw sliding | this is the failure `FACE_REST_TOLERANCE` is supposed to prevent, and 30° may simply be too generous |

Then the case that **must** fail: a clip from the capture set onto **Eve Maccaro**. Her facial bones
carry identical Rigify names and rest inverted (median 142.6°), so the gate should refuse ~63 of 69 and
say so. **If Eve's face moves, the gate is not working** — and that is worse than the face not moving,
because every count will say it succeeded.

Read the server's notes either way; they are the point of the feature being honest:

```
carried 61 facial bone(s) — the face travels with the clip
63 facial bone(s) REFUSED: … rest them 143° apart …
```

**Decision to make here, and it is yours:** if the carried faces look poor at the top of the tolerance
range, the options are to lower `FACE_REST_TOLERANCE` (safer, carries less) or to drop §8e entirely.
It earns its keep on one measured direction; I said as much when it landed.

---

## 5. What is deliberately NOT in scope

Recorded so the plan is not read as a claim that these were tested:

- **No auto-blink.** A face holds what it was last set to, so `blink: 1` leaves her eyes shut. Nothing
  blinks on a timer; §8d says so.
- **No visemes from speech.** The five mouth shapes exist and the voice path exists; nothing connects
  them.
- **No cross-family facial retarget.** Grace, Trish, Yuffie, Alice, Blondie and Saka receive nothing
  from a Rigify clip — different naming families. `investigations/facial-expression.md` §7 says why a
  name map is not the obvious next step.
- **Morph-driven facial performance does not exist in the corpus.** 249 clips drive weights and none
  of them drive a facial target.

---

## 6. Where each result goes

| result | destination |
|---|---|
| a phase passes | a dated confirmation line in the matching `specs/figures.md` section |
| a phase fails cleanly (wrong output, clear cause) | `backlogs/figures.md`, with the measurement |
| a phase fails and resists diagnosis | a section in `investigations/facial-expression.md` — it already holds seven rejected approaches and is the right home for an eighth |
| a number here turns out wrong | fix it **here and in the spec**, and say which measurement replaced it |

Delete this file when phases 1–4 have all settled.
