# Chronic ~32 fps with three figures in the room

**Status: opened 2026-09-09, one measurement, cause unknown.** This does not yet meet
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

## Remaining theories

| Theory | Likelihood | How to test |
|---|---|---|
| **The figures' skinned-mesh cost** — 226 k tris of GPU skinning across three characters | Medium. It is the obvious suspect and the reason this was noticed, but `rebuilds=0` and flat heap argue against churn | Run the same probe with **no figures in the room**, then one, then three. Three data points, ten minutes |
| **A static-scene throttle** — the compositor or browser coalescing frames when nothing moves, making "31 ms idle" not a cost at all | Medium, and it would explain the inversion | Same empty-room run: if idle sits at ~31 ms with nothing in the scene, the figures are exonerated entirely |
| **Probe artefact** — `PACE` measuring callback intervals rather than delivered frames | Low–medium | Compare against the Quest's own frame overlay for one session |
| **The two-room capture** — 57 real surfaces rendering, unrelated to figures | Low. Surfaces were present for the healthy sessions in `pops-and-jitters` too | Falls out of the empty-room run |

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

*(nothing yet — one measurement, no hypotheses eliminated)*
