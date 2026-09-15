# Bride's missing left lower eyelid

**Status:** OPEN — cause not established. One fix **tried and rejected on device**.
**Opened:** 2026-09-15

## Symptom

Reported from the headset, 2026-09-15, after placing the composed `Bride`
(`e985c81c661a46c3.glb`) and playing clips on her:

> "many (or all) of the bride animations are animating with her left lower eyelid not showing which is
> strange. otherwise she is looking good."

The figure is composed from `bride_ready.glb` (11 pieces — dress, veil, hair, eyelashes, heels,
jewellery) plus `model_britney_bride.glb` (1 piece — her BODY, which carries the eyelids).
`underwear.glb` is referenced by the scene and was never downloaded. She had looked right in an earlier
viewer pass apart from washed-out irises, since fixed.

## Experiments and what each proved

| # | Experiment | Result | Conclusion |
|---|---|---|---|
| 1 | `joints_agree` — the donor's bone rest against the scene's | 165 of 222 bones differ | The donor is a different rig wearing the same names. **Sound, and long known** — `verify_thing` has printed it since the composer landed |
| 2 | Per-bone magnitude over the eye region | 4 lid ROOT bones ~6.5–7.0 mm out (`DEF-lid.B.L` worst at 6.95); the `.001`–`.003` chains all under 1 mm; `DEF-eye.*`/`DEF-eye_master.*` differ in rotation with zero translation | The disagreement is concentrated in the lid roots, not spread evenly |
| 3 | Lid VERTEX displacement: `G_scene · IBM` vs `G_britney · IBM` over the vertices each lid bone dominates | 23–31 mm | Arithmetically sound, and **does not establish the symptom** — see *Remaining theories* |
| 4 | `rootBone` on every render component in that scene | **all of them name `rig_deform`**, the donor body included | The site binds every piece to ONE skeleton. Sharing the scene's bones is what the source does, so "her body is 165 bones out" is a fact about the source rather than a defect to correct at the mesh. **This should have been experiment 1** |

## Tried and rejected

**Rebind the donor's inverse bind matrices** — `IBM' = G_here⁻¹ · G_there · IBM`, so `G_here · IBM'`
equals `G_there · IBM` by construction and the donor's geometry reproduces its own rest exactly.

*Result:* exact as arithmetic — all 222 joints within 8e-8, lids at 0.0000 mm — and **much worse in the
headset**:

> "her body is no longer congruent with her clothing, arms out with bare skin, clothing arms down,
> breasts sticking out of dress, and the animation is grotesque with body parts stretching in weird
> ways."

*Why it failed, and both reasons were in the algebra before it shipped:*

1. **The clothes are posed by the scene's bones.** Anchoring the body to the donor's own rest puts body
   and clothing in two different frames, so they diverge by the full 165-bone difference. Matching its
   own authored rest is worth nothing next to being congruent with the dress.
2. **It shears under animation.** The animation drives `G_s`, so the body's transform becomes
   `G_s · A · G_s⁻¹ · G_b` — a conjugation, not a rotation. That is the stretching.

*Shipped in `2ea4149`, reverted in `faadb54`.* **Do not re-propose per-mesh IBM correction for a welded
donor.** What would justify revisiting it: a case where the donor is the ONLY container binding the
bones in question, so there is nothing for it to become incongruent with.

*Collateral, and worth keeping:* the revert brought the previous bytes back, and therefore the previous
id, onto a row the rebind's import had retired. `upsert` never cleared `superseded_by`, so every
re-import looked clean while the asset stayed invisible — 98 live models became 47. Fixed by
`library.revive` (`d0ee885`), which is a real bug the experiment paid for.

## Remaining theories

| Theory | Likelihood | How to test (all read-only) |
|---|---|---|
| The lid geometry is **detached from the face** in the shared frame | medium | Compare lid vertices against the NEIGHBOURING vertices of the same mesh, not against the donor's rest. Experiment 3 measured the wrong reference: it measures a divergence the site shares, so it cannot explain ONE missing lid on a figure that otherwise looks right |
| It is a **material**, not skinning | medium | `Eyelashes` sets `depthWrite` off, which glTF cannot express and this pipeline reports and drops; `Reflections-eyes` is now made invisible as a refractive lens. Either could read as a missing lid |
| The lid **primitive is not in the file**, or wears a degenerate material claim | low–medium | Find which primitive and material carry the lower lid, and whether the scene's claim on it is degenerate in the way Alice's eye claim looked (note that the Alice case turned out NOT to be a defect — the site draws the sparse claim; `specs/captures.md` § 3) |
| It is **static, not animated** — absent at rest too and only noticed during a clip | medium | One headset observation: place her and look without playing anything. Separates a skinning fault from a drawing fault and costs nothing |

## Fixes shipped

None yet for the symptom. Shipped from the campaign:

| Symptom | Cause | Fix | Commit |
|---|---|---|---|
| A revert leaves the asset in its own tombstone; 98 live models → 47 | `upsert` never cleared `superseded_by`, and an id is a content address in both directions | `library.revive`, called from the ingest path only | `d0ee885` |

## Related

- [`specs/captures.md`](../specs/captures.md) § 3 — composing, the rest-pose test, and why weldability
  is judged on local rather than world matrices
- [`plans/figures-and-library.md`](../plans/figures-and-library.md) phase 5 — retargeting, which is what
  making these two rigs genuinely one requires
- [`backlogs/captures.md`](../backlogs/captures.md) — `underwear.glb` and the 51 props files no capture
  ever downloaded
