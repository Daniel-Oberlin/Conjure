# Chronic ~32 fps with three figures in the room

**Status: opened 2026-09-09. Narrowed to two candidates the same day; four theories killed.** This does not yet meet
[this folder's bar](./README.md) — it has produced no negative knowledge. It is here because the
measurement was incidental to something else and would otherwise be lost, and because the first
experiment that would narrow it is cheap. It graduates or it is deleted.

## Symptom

With three figures placed (Trish + Grace + Saka, **226,410 triangles**), a headset session reports a
sustained frame interval of **~31 ms standing still** against an `ideal` of **11.1 ms** — roughly 32 fps
on a display asking for 90. Every frame trips the `drop(>1.5×ideal)` counter, because every frame is
over budget.

Found while running [C4 of the figures device run](./figures-device-run-2026-09.md) — an A/B on whether
*posing* a figure costs frames. It does not (that result stands). The baseline it was measured against
is the finding.

| Phase | frames | mean ms | jit(sd) | late | drop | rebuilds |
|---|---|---|---|---|---|---|
| unposed, still | 17 | 31.2 | 1.58 | 0 | 1115 | 0 |
| posed, still | 17 | 30.5 | 1.60 | 0 | 1142 | 0 |
| unposed, walking | 22 | 20.9 | 3.57 | 226 | 1214 | 0 |
| posed, walking | 22 | 22.8 | 3.74 | 156 | 1352 | 0 |

Conditions: `RATE current=90Hz ideal=11.1ms supported=[72,80,90,120]`, one client (`hs_d9hwvn`, a
headset — not the desktop tab), `rebuilds=0` throughout, `heap` flat at 132.6 MB, 57 real surfaces in a
two-room capture.

## Why this is not the recorded stutter

[`pops-and-jitters.md`](./pops-and-jitters.md) closed with the remaining stutter attributed to a
platform characteristic: **rare** dropped frames reprojected during head translation, on an otherwise
healthy 90 Hz session, with our JS shown to be off the critical path (`selfMs` ≈ 0.2 ms on a 66 ms
frame). Same probe, same log format — but a different shape of fault:

| | pops-and-jitters | this |
|---|---|---|
| Steady state | essentially perfect | **~32 fps sustained** |
| Drops | rare, and only visible during translation | **every frame** |
| Worse when | walking | **standing still** (see below) |

So the earlier conclusion is not in question; this is something else, and it is a fresh symptom rather
than a regression of a fixed one.

## The inversion, which is the strangest part

**Walking is faster than standing still** — 20.9 ms versus 31.2 ms unposed, and the same direction
posed. Every prior in the record runs the other way: translation is when frames get *worse*, because
that is when a dropped frame reprojects badly. A scene that speeds up when the head moves suggests the
renderer is doing *less* work when idle, not more — which points at something throttling or coalescing a
static scene rather than at a load problem. It could equally be an artefact of how the probe samples
frame callbacks. Unresolved, and the thing most likely to explain the rest.

## Per-model comparison, 2026-09-09 (subjective, and decisive anyway)

Each figure placed **alone** and walked around. Subjective, but an ordered comparison over a 4.6× range,
same room and same route, and monotonic:

| model | tris | **joints** | prims | texture MB | morphs | observed |
|---|---|---|---|---|---|---|
| Saka | 27,266 | **249** | 12 | 9.1 | 399 | smooth |
| Grace | 73,586 | **482** | 21 | 4.1 | 0 | occasional jitter |
| Trish | 125,558 | **1041** | 24 | 5.9 | 0 | steady jitter |

**This kills the static-scene throttle.** A throttle cannot vary by model. And if one 27k-triangle figure
is smooth, the idle baseline is healthy — so the figures own the cost, and the empty-room run is no longer
needed to establish that.

**It also exonerates textures and morph targets.** Saka carries the *most* texture data of the three and
399 morph targets, and she is the smooth one. Neither correlates with the symptom; both anti-correlate.

## The confound: triangles or bones?

Across those three, **triangles rise 4.6× and joints rise 4.2×**. The observation cannot separate them,
and "high-poly models at fault" is the natural reading only because triangles are the number we habitually
quote.

The prior should favour **bones**, on two grounds:

- 125 k triangles is *small* for a Quest 3 GPU; it draws far heavier scenes. Whereas **1041 bone matrices
  recomputed per frame in JavaScript** on a mobile CPU is real work, and it is CPU work on the critical
  path rather than GPU work that can overlap.
- Our own code does **no** per-frame work for a pose — `figure.js` writes quaternions once on `apply()`
  and `_ride` runs only then. So whatever is per-frame belongs to three's skinning, which is where the
  joint count lands.

Trish's 1041 is inflated by a **679-bone hair rig** — a second armature that earns nothing here, since
`VRMC_springBone` is unsupported and the hair merely rides. That is what makes the discriminator cheap.

## Next experiment — the discriminator

**Re-export Trish without the hair armature.** She keeps all 125 k triangles and drops to ~362 joints:

| Outcome | Conclusion | Fix |
|---|---|---|
| jitter goes | **bones** | strip unused armatures in `blend_to_glb.py`; nothing renders from them |
| jitter stays | **triangles** | decimation at conversion, or LOD |

**The variants are already built** (`scripts/glb_strip.py`, no Blender needed — Trish's hair node has an
identity world matrix, so unskinning leaves the geometry exactly in place):

```bash
python scripts/glb_strip.py ~/.local/share/conjure/assets/9c4b8c1c727f245a.glb \
    temp/frame-test/trish_A_hair_unskinned.glb --unskin Hair.001
python scripts/glb_strip.py ~/.local/share/conjure/assets/9c4b8c1c727f245a.glb \
    temp/frame-test/trish_B_no_hair.glb --unskin Hair.001 --drop Hair.001
```

| variant | triangles | skeletons | joints | skinned meshes |
|---|---|---|---|---|
| original | 125,558 | 2 | 1,041 | 6 |
| **A** hair kept, unskinned | 125,558 | 1 | **362** | 5 |
| **B** hair dropped | **41,487** | 1 | 362 | 5 |

Import each, place it alone, walk the same route, and judge it the way the per-model run was judged —
smooth / occasional / steady. **original vs A** isolates skinning; **A vs B** isolates drawing.

A caveat so the result is not over-read: `--unskin` stops `Skeleton.update()` recomputing 679 bone
matrices per frame, but the joint *nodes* remain in the scene graph and are still walked by
`updateMatrixWorld`. It isolates the skinning cost, not all per-bone cost.

**And a discovery from building it that may pre-empt the whole question: Trish's hair is 84,071 of her
125,558 triangles — 67% — as well as 679 of her 1041 joints.** Her body is 34,786. Her hair alone is
three times Saka's entire character, for a rig whose spring bones are unsupported so the hair only ever
rides. Whichever variable wins, the hair is the target.

## Remaining theories

| Theory | Likelihood | How to test |
|---|---|---|
| **Per-frame skeleton update, O(joints)** | **High** — best fit. Monotonic with joint count, and the only candidate that is CPU work in JS on the critical path | Trish without her hair rig: same triangles, 1041 → ~362 joints |
| **GPU skinning cost, O(vertices)** | Medium — also monotonic, but 125 k tris is small for this hardware | A decimated Grace: same joints, a third of the triangles |
| ~~A static-scene throttle~~ | **Killed** — a throttle cannot vary by model, and one light figure is smooth | — |
| ~~Textures / morph targets~~ | **Killed** — Saka has the most of both and is the smoothest | — |
| **Probe artefact** — `PACE` measuring callback intervals rather than delivered frames | Low–medium, and it would explain the still-vs-walking inversion while leaving the per-model result intact | Compare against the Quest's own frame overlay for one session |
| ~~The two-room capture~~ | **Unlikely** — 57 surfaces were present while Saka rendered smoothly | — |

## Next experiment

**Empty room, same probe, same route:**

```bash
python -m conjure.ctl remove <each figure>
python scripts/c4_frame_cost.py --empty          # one phase, nothing posed, ~90 s
```

If idle still sits near 31 ms with nothing placed, the figures are innocent and the question becomes what
the idle rate means — which would also settle the inversion above. If it jumps to ~11 ms, the figures own
it and the next question is how the cost scales with one versus three.

Cheap, and it splits the theory table in half whichever way it lands.

## Tried and rejected

**A static-scene throttle** explaining the ~31 ms idle. Killed by the per-model run: a throttle cannot
vary by model, and one 27 k-triangle figure walked smoothly. Retry only if the idle rate turns out to be
high with *nothing* in the room, which the per-model result now makes unlikely.

**Textures and morph targets.** Killed and in fact anti-correlated — Saka has 9.1 MB of texture and 399
morph targets against Grace's 4.1 MB and none, and Saka is the smooth one. Retry would need a case where
the texture *budget* rather than the byte count is at issue (a resolution cliff), for which there is no
evidence.

**The empty-room run as the next step.** Superseded before it was run: the per-model comparison answered
the same question more directly, because a single light figure rendering smoothly establishes both that
idle is healthy *and* that the cost scales with the figure.
