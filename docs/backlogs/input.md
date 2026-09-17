# Input — backlog

Unfinished work, future directions and known gaps for XR input. What is built is
[`docs/specs/input.md`](../specs/input.md); why input is its own area is
[`docs/decisions.md`](../decisions.md) §29.

**This backlog is young and deliberately thin.** The area was extracted from
[`specs/dynamics.md`](../specs/dynamics.md) §6 on 2026-09-17 with the code unchanged, so what follows is
the gaps that were already true rather than a fresh survey. The near-term sequence for most of it is
held by [`docs/plans/hands.md`](../plans/hands.md) while that plan is live, and lands here if it
dissolves without executing.

---

## Tracked hands resolve no action

A hand has no gamepad, so no control resolves and therefore no action does. `ConjurePointers.list()`
publishes hands, but `controllers()` — which `grab`, `controller-beams`, `surface-overlay` and the gaze
picker all call — filters them out by construction. The one consumer that reacts to a hand,
[`water`](../specs/dynamics.md), does it by fingertip proximity and its own distance test.

So with hands up, one module in the repo answers you. **This, and not the absence of a hand model, is
why hand tracking feels thin.** The fix is in the layer that already exists for it: synthesised hand
controls (`pinch`, `grasp`, `poke`) beside the xr-standard ones, and binding entries for them — after
which `grab` and the beams start working **with no module change**, because modules name actions.

Two sub-questions that should be answered together and not guessed:

- whether `controllers()`'s callers are widened or a third reader is added beside it — decide against
  what `grab` actually needs from a ray, since a pinch has no ray;
- hysteresis on a synthesised control. A pinch is a continuous distance, and an unhysteresised threshold
  chatters at exactly the distance a user holds.

Owned by [`plans/hands.md`](../plans/hands.md) phase 3 while that plan is live.

## Three unshared readers of XR joints

`client/occlusion.js` builds a 25-joint hand mesh from `frame.getJointPose` directly;
`client/hands-fit.js` reads all 25 for the `?hands=fit` overlay ([`specs/input.md`](../specs/input.md)
§9); `ConjurePointers` reads `index-finger-tip` only. None knows about the others — the same
duplication-breeds-drift shape the pointers layer was created to remove for buttons, now at three.

The seam is for `ConjurePointers` to publish the **full joint set plus per-joint `radius`**, read once
per frame and cached like everything else it reads, and for `occlusion.js` to consume that instead of
reading the frame. Two qualifications, both worth stating:

- the occlusion mesh works today, so this is duplication-removal rather than a defect;
- the **overlay should stay outside** whatever gets built. It exists to look at the raw frame, and
  routing it through a cache would put the thing under test behind the thing testing it.

The shape of the published snapshot is the real open question, and `?hands=fit` is what will answer it:
whether `radius` is worth publishing at all depends on whether the runtime supplies a per-joint value or
a table.

## Contact is a raycast every consumer would write itself

There is no shared way to ask "which joints are inside this volume, moving how fast". `water` answers
it privately with a fingertip distance test against its own plane. A joint **capsule chain** with the
radii the runtime already supplies, exposed as a query a consumer asks rather than a cast it runs, is
the general form; `water` migrating onto it **with no behaviour change** is the proof it is the right
shape.

Owned by [`plans/hands.md`](../plans/hands.md) phase 4 while that plan is live.

## `actions` tells the arbitration layer nothing

`module.json` `actions` is parsed into `DynamicModuleDef.actions` (`dynamics.py:152`) and read by
nothing — see [`backlogs/dynamics.md`](./dynamics.md) for the manifest-validation half. The part that
belongs to this layer is the prize: knowing which consumers contend for which action **before** a
frame, instead of discovering it through capture/reserve at runtime. Today arbitration is entirely
dynamic, which is correct but blind — nothing can warn at conjure time that two live modules both want
the trigger.

## Beyond the headset

[`architecture.md`](../architecture.md) §11 designs an input layer wider than XR — host devices
(yokes, pedals, MIDI), hotplug, capability-adaptive schemes, and per-device latency placement.
`ConjurePointers` is the client half of that abstraction proved out on the source that exists now;
drivers, hotplug and the host hop remain designed and unbuilt.

## Smaller things

- **`beam_trigger` and `beam_timeout` are global.** One arm window for every action. A 10 s linger is
  right for a beam and probably wrong for a momentary diagnostic control.
- **`armed()` has no test.** Nothing in `tests/js/` exercises the arm window, the edge detection or the
  reservation slack, all of which are pure functions of a fake input source and could be.
- **Rebinding needs a restart.** `window.CONJURE_BINDINGS` is injected into the page, so a binding
  change is a reload. Live rebinding is cheap and nobody has needed it.
