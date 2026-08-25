# Occlusion — backlog

Unfinished work and shelved directions for real-world occlusion. The current state is
[`docs/specs/occlusion.md`](../specs/occlusion.md); why `full` was shelved is
[`docs/decisions.md`](../decisions.md) §19.

---

## Shelved: `full` — environment-depth occlusion

`full` would occlude *everything* real and dynamic — furniture, people, objects you hold — not just
hands, via the Quest depth sensor. Shelved after on-device investigation; see §19 for the reasoning.
What it would take, recorded so the work does not have to be rediscovered:

- Create our own `XRWebGLBinding(session, gl)`.
- Each frame, per eye view: `getDepthInformation(view)` → an `unsigned-short` texture plus
  `rawValueToMeters` and `normDepthBufferFromNormView`.
- A custom fullscreen occluder shader converts raw → metres → clip-space depth and writes `gl_FragDepth`.
- Add it to the **scene graph** so it survives A-Frame's depth clear — the same trick the hand mesh uses.
- Handle both eye viewports and foveation.

Confirmed on device: the Quest *does* provide the data (`depthUsage=gpu-optimized`,
`depthDataFormat=unsigned-short`), so this is genuinely possible on the hardware. It is device-specific
WebGL that cannot be unit-tested — every pass needs a headset round trip.

## Cheaper alternative: mesh-detection occluders

Render the **`mesh-detection` captured room mesh** (walls plus furniture) as depth-only occluders. Sharp
and stable, no depth sensor, no per-frame texture wrangling — but **static only**: no hands, people, or
moving objects. Pairs well with `hands`, which covers exactly what it cannot.

If static-furniture occlusion is the goal, this is a better next step than `full`.

## Soft edges

A hard z-write gives hard-edged occlusion, which is the right global default. Feathered edges would
require the per-material path the depth pre-pass exists to avoid, so if soft edges are ever wanted they
should be added **only where they matter** (hands), as a local exception rather than a change of
approach.

## Not yet verified

- Behaviour under **foveated rendering** at higher foveation levels.
- Interaction with **transparent** content: a transparent virtual surface in front of a real occluder
  currently depth-tests like any other material, which is correct, but has not been checked on device.
