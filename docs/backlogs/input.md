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

## ~~Tracked hands resolve no action~~ — BUILT 2026-09-17

`pinch`, `grasp` and `poke` are synthesised from the joint geometry and live in the same namespace as
`trigger` and `grip`; `select`, `grab` and `resize` bind to both vocabularies at once; and
`acting()` is the reader that includes hands. `grab`, the beams, the surface overlay and the gaze
picker changed one line each and now work with hands up — [`specs/input.md`](../specs/input.md) §2–§4.

Both sub-questions this item said to answer together were answered, and one of them differently than
expected:

- **A third reader, not a widening.** `controllers()` is named for controllers and a function whose
  name stops being true is worse than one more function. A tracked hand always had a `targetRaySpace`,
  so the ray was never the obstacle — the control was.
- **Hysteresis by RESCALING the control**, not by a rule in `active()`. Pressing at 0.6 and releasing
  at 0.4 while remapping the value so `>= ACTIVE_AT` is the predicate itself keeps the debouncing in
  the three controls that need it instead of in the generic reader.

What is left here is smaller and follows from having built it:

- **The thresholds are first guesses from hand anatomy, not measurements.** 70/22 mm for a pinch,
  0.90/0.45 for a curl. The raw per-finger straightness is logged beside the resolved controls under
  `CONJURE_DEBUG_LOG` and has not been read off a headset. `CURL_OUT = 0.90` is the one to suspect: a
  relaxed hand is not a straight hand, so a natural grab of a large object may read below threshold.
- **`poke` is bound to nothing.** Synthesised, exclusive, and consumed by no action — kept because it
  separates intent to *touch* from intent to *hold*, which is what the contact query below wants.
- **No `grasp`-to-stick equivalent, deliberately.** The stick-driven actions (`reel`, `yaw`, `pitch`,
  `bank`) have no hand binding, because a hand has no analog axis and faking one from a wrist angle
  would be a gesture pretending to be a stick.
- **Whether a beam from a fingertip is the right presentation.** `controller-beams` now draws for hands
  because it keys off `armed()` and actions resolve — which is correct and may still look wrong. Nobody
  has watched it.

## Opportunistic: a SECOND pair of hands

Everything measured about hand tracking here was read on one wearer (2026-09-17), and two claims rest
on that and would be cheap to check the next time anyone else puts the headset on:

- **`?hands=fit`** — whether the runtime's skeleton differs between two people at all. Left and right
  differ by 0.240 mm and the figures move across re-acquisitions, so it re-estimates *something*; what
  is unverified is that it re-estimates per PERSON.
- **`tipOut`** — whether `radius` and a thumb coefficient of 0.7 land someone else's fingertips
  ([`specs/figures.md` §8f](../specs/figures.md)). Neither value is a length, which is the whole reason
  they are defaults off one reading, but that is an argument and not a second measurement.

Nothing depends on either: `off` and a flat millimetre figure both remain, and no code branches on the
answer. Worth a minute when the opportunity arises and not worth arranging.

## Three unshared readers of XR joints

`client/occlusion.js` builds a 25-joint hand mesh from `frame.getJointPose` directly;
`client/hands-fit.js` reads all 25 for the `?hands=fit` overlay ([`specs/input.md`](../specs/input.md)
§9); `ConjurePointers` reads `index-finger-tip` only. None knows about the others — the same
duplication-breeds-drift shape the pointers layer was created to remove for buttons, now at three.

**Half done, 2026-09-17:** `ConjurePointers` now publishes the full joint set and the per-joint
`radius` on each hand pointer, read once per frame like everything else it reads. What has *not*
happened is the consumers moving onto it. Two qualifications, both worth stating:

- the occlusion mesh works today, so this is duplication-removal rather than a defect;
- the **overlay should stay outside** whatever gets built. It exists to look at the raw frame, and
  routing it through a cache would put the thing under test behind the thing testing it.

The radius turned out to be worth publishing for a reason nobody predicted: it is the fingertip offset
a worn hand needs (`specs/figures.md` §8f), which makes it load-bearing rather than diagnostic.

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
