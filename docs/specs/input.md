# Input — XR controls, as ACTIONS — the spec

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/input.md`](../backlogs/input.md); rejected alternatives and
the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md) — §29 for why
this is its own area.

`client/conjure-pointers.js` is the **one reader of XR input** and the seam that keeps controls out of
module code. Everything above it — dynamic modules, `grab`, the beams, the surface overlay, the gaze
picker — names an **action** and never a button.

This area is one file and one global. It gets its own spec because its consumers stopped being modules:
[`specs/dynamics.md`](./dynamics.md) is about *conjurable shared effects*, and input is read by
components that are not modules at all ([`decisions.md`](../decisions.md) §29).

---

## 1. Why the layer exists

Before it, every consumer walked `session.inputSources` itself and hard-coded button indices: four
places to fix when a mapping changed, a control scheme you could only discover by reading source, and
control *sharing* that "worked" only because `grab` happened to use GRIP while `water` used TRIGGER.

Two jobs:

1. Read the XR frame **once per frame** and publish a normalized snapshot per pointer, cached on the
   frame, so N consumers cost one read.
2. Resolve semantic **actions** through a binding table, so a module asks "is `resize` active?" and
   never names a button.

A third job belongs to the layer and is treated separately below: deciding **who** gets a pointer when
two consumers want the same control (§6).

---

## 2. Controls

The xr-standard gamepad mapping — the vocabulary a binding may refer to.

| Control | Source |
|---|---|
| `trigger` | button 0 |
| `grip` | button 1 |
| `stickPress` | button 3 |
| `a` / `b` | buttons 4 / 5 |
| `stickX` / `stickY` | axes 2 / 3 |

**A tracked hand has no gamepad, so its controls are SYNTHESISED from the shape of the hand.** Until
2026-09-17 there were none, which meant no action resolved on a hand at all — `controllers()` filtering
hands out was never why hand tracking felt thin, because there was nothing to filter.

| Control | From |
|---|---|
| `pinch` | thumb-tip ↔ index-tip, 70 mm open to 22 mm shut, **times how open the other three fingers are** |
| `grasp` | the **weakest** finger's curl — tip-to-metacarpal over *that finger's own* summed bone length, so it means the same on a large hand and a small one (the reasoning that puts `s` on a ratio in [`figures.md` §8f](./figures.md)) |
| `poke` | an index that is out **while the others are in** |

**Each gesture excludes the others, and it has to.** Measured on the synthetic hands the moment anyone
asked which of these were bound to anything:

- a **fist** read `pinch` 0.77 *and* `grasp` 0.80 — so closing your hand fired `select` and `grab`
  together, and `water` would ripple every time you reached for something. In a fist the thumb lies
  across the fingers and its tip is a few centimetres from the index tip, squarely inside the pinch
  span: **the distance alone cannot tell the two gestures apart.**
- **pointing** read `grasp` 0.64, because three of four fingers are curled and `grasp` was their
  *mean* — so pointing at an object grabbed it.

Both are one fault: a control that measures a quantity and rules nothing out. `grasp` is now the
weakest finger (a hand is closed when *every* finger is closed; an average lets three fingers vote for
a gesture the hand is not making) and `pinch` is gated on the others being open — the discriminator
`poke` already had.

They sit in the same namespace as `trigger` and `grip`: a binding refers to them the same way and a
module still never names either. The thresholds are first guesses from hand anatomy rather than
measurements; the raw per-finger straightness is logged beside the resolved controls under
`CONJURE_DEBUG_LOG`, because that is the number they would have to be dialled against.

**Hysteresis, and where it lives.** A gesture is a continuous distance held near its own threshold by a
human hand, so an unhysteresised control chatters at exactly the distance anyone holds. Press at 0.6,
release at 0.4 — and applied by **rescaling the control**, so that `value >= ACTIVE_AT` *is* the
hysteretic predicate:

    released:  raw [0, 0.6]  →  [0, 0.5)          held:  raw [0.4, 1]  →  [0.5, 1]

Monotonic, 0 is still nothing and 1 still fully closed, and nothing downstream — `active()` included —
has to carry a rule that three controls need. The first version only *raised* a latched value to
`ACTIVE_AT`, which was half a mechanism: it held a pinch through a dip and did nothing to stop one
engaging below the press threshold, because the raw distance crosses 0.5 well before it crosses 0.6.

---

## 3. Bindings

Bindings map control → action. They are config (`Settings.bindings`, injected as
`window.CONJURE_BINDINGS`), never hard-coded in a module. Defaults (`config.py` `DEFAULT_BINDINGS`, a
single constant that both the dataclass default and `get_settings()` read — they used to carry the
literal separately, and adding an action to one left the running server serving the old scheme):

```json
{"select": "trigger", "grab": "grip", "resize": "trigger", "reel": "right.stickY",
 "yaw": "right.stickX", "pitch": "left.stickY", "bank": "left.stickX",
 "mark": "b", "surfaces": "a"}
```

The last two are diagnostics rather than interaction: `mark` writes the geometry ground-truth probe
([`spaces-geometry.md` §10.3](./spaces-geometry.md)) and `surfaces` cycles the surface debug overlay's
layers (§11 there).

**A binding may name SEVERAL controls, and the largest wins.** That is the whole of what it takes for
one action to mean one thing on a controller *and* on a tracked hand: a controller's `pinch` is 0 and a
hand's `trigger` is 0, so the two vocabularies are disjoint, no device test is required and none is
written. `select: ["trigger", "pinch"]`, `grab: ["grip", "grasp"]`.

Which is to say, as shipped:

| control | actions it resolves | what consumes them |
|---|---|---|
| `pinch` | `select`, `resize` | `water`'s ripples, `grab`'s corner handles, and arming the beam |
| `grasp` | `grab` | `grab`'s drag |
| `poke` | **nothing** | — |

`poke` is vocabulary with no consumer yet. It is kept because it is the one gesture that separates
intent to *touch* from intent to *hold*, which is what the contact query
([`backlogs/input.md`](../backlogs/input.md)) will want — and because it earns its place already as the
discriminator the other two borrow.

The stick-driven actions (`reel`, `yaw`, `pitch`, `bank`) deliberately gain nothing: a hand has no
analog axis, and faking one from a wrist angle would be a gesture pretending to be a stick, which feels
broken rather than missing.

A control may be **hand-qualified** (`"left.stickY"`), so one hand can hold an object while the other
shapes it. Re-binding is a config change, not an edit in every module.

**A hand-qualified action resolves globally**, and consumers must know it: `value`/`active` read the
named hand's pointer whichever pointer you ask, so every pointer reports the same deflection and a
per-pointer loop over `reel` would apply it twice. The tier-C gestures say so at each site
([`dynamics.md` §8, §8b](./dynamics.md)). `started`/`ended` are the exception — they compare
**own-hand** control values against last frame and ignore the qualifier, because an edge on someone
else's button is not an edge on yours. A hand that is not present resolves to 0.

A module *declares* which actions it consumes in its `module.json` `actions` field. That is a manifest
key and is specified with the manifest ([`dynamics.md` §4](./dynamics.md)); nothing reads it yet, and
runtime resolution comes entirely from `window.CONJURE_BINDINGS`.

If injection is absent the file falls back to `{select: trigger, grab: grip, resize: grip, reel:
stickY}` so a headset stays usable.

---

## 4. Reading pointers

- `ConjurePointers.list(sceneEl)` — every pointer this frame (controllers *and* tracked hands), `[]`
  outside an XR session.
- `ConjurePointers.controllers(sceneEl)` — controllers only, for anything that genuinely needs a
  **gamepad**. The filter is `!p.isHand && p.source.gamepad`.
- `ConjurePointers.acting(sceneEl)` — every pointer that can resolve an action: controllers **and**
  tracked hands. This is what ray-driven interaction wants, and every caller of `controllers()` that
  meant "something I can point and click with" now calls it. A tracked hand has always had a
  `targetRaySpace`, so it has always had an aim; what it lacked was a control.

  Added *beside* `controllers()` rather than widening it, because a function whose name stops being true
  is worse than one more function. Callers invoke it as `(CP.acting || CP.controllers)`: the Quest's
  cache has served a stale `/static/*.js` through several reloads before now, and a consumer updated
  against a pointers layer that has not would otherwise throw inside its tick — degrading to
  controllers-only is exactly the previous behaviour.

Both are cached per XRFrame with a 4 ms recency window. The recency check matters: the browser is not
guaranteed to hand out a fresh `XRFrame` object each frame, and an identity-only cache would never
invalidate — every consumer would see the first frame's buttons forever.

**Each pointer** carries pose and resolved controls:

| Member | Meaning |
|---|---|
| `key` | stable per input source — `"right:ctrl"`, `"left:hand"` |
| `handedness`, `isHand`, `source` | the raw XR input source and its kind |
| `origin`, `dir`, `quat` | target-ray pose in the world frame (the rig sits at the origin) |
| `fingertip` | index-finger-tip position for tracked hands, else `null` |
| `joints`, `radii` | all 25 WebXR joints and their reported radii for a tracked hand, else `null` — read once per frame with everything else here |
| `canAct` | a gamepad, or a hand we synthesise controls for. What `acting()` filters on |
| `value(action)` | 0..1 for buttons, −1..1 for axes, resolved through the bindings |
| `active(action)` | `value(action) >= 0.5` (`ACTIVE_AT`) |
| `started(action)` / `ended(action)` | rising / falling edge this frame (own-hand controls) |
| `armed()` | is this pointer **in use** — see §5 |
| `anyActive()` | is any bound action engaged |
| `availableTo(owner)` | free, or already this owner's (§6) |

The scene root is pinned to the XR reference space, so a pose read in that space maps 1:1 into scene
coordinates and needs no conversion here. Committing a *dragged* pose to durable state is a different
question and goes through `ConjureFrames` ([`dynamics.md` §8](./dynamics.md)).

---

## 5. `armed()` — one definition of "in use"

A pointer arms when `select` is pulled past `beam_trigger` (default 0.05) **or** any bound action is
engaged, and lingers for `beam_timeout` (default 10 s) after the most recent pull. Continuous use keeps
re-arming it, so a momentary release mid-gesture does not flicker it off. Both come from config as
`window.CONJURE_BEAM_TRIGGER` / `CONJURE_BEAM_MS`; `beam_timeout = 0` disables the linger, and so does
the absence of injection, which arms for the pulling frame only.

Arming lives in the input layer rather than in the beam so that **presentation and focus agree by
construction**: `controller-beams.js` shows a beam exactly when `armed()`, and `grab` refuses to
highlight anything when it is false. A selection box appearing with no visible beam aimed at it reads
as the scene reacting to nothing.

---

## 6. Sharing a pointer between two consumers

Tick order is not guaranteed, and two consumers can want the same control — `resize` and `select` are
both the trigger by default. Arbitration is explicit and lives here:

| Mechanism | Lifetime | Use |
|---|---|---|
| **capture** — `claim(key, owner)` / `release(key, owner)` | until released | held for a whole gesture. While `grab` is dragging, that pointer is exclusively grab's and nothing else reacts to its buttons. |
| **reservation** — `reserve(key, owner)` | the next press; **renewed every frame** | "I'd take the next press here." `grab` reserves while the beam is on one of its corner handles, so the same trigger resizes *there* and ripples on the picture's body. |

`ownerOf(key)` returns the capture if there is one, else a reservation **made this frame or last**. That
one frame of slack is what makes reservations order-independent: a consumer ticking before the reserver
still defers.

The contract is one line, before acting on a pointer:

```js
if (!p.availableTo("mymodule")) continue;      // someone else holds or has reserved it
```

Edge state (`_was`, captures, reservations, arm windows) is dropped when a pointer vanishes, so a
reconnecting controller never inherits a stale "held".

---

## 7. Who reads it

Everything that reacts to a controller, module or not. This is the list that motivated the split:

| Consumer | Reads | For |
|---|---|---|
| `client/controller-beams.js` | `controllers()`, `armed()` | the beam — not a module (`dynamics.md` §10) |
| `client/conjure-client.js` | `controllers()` | the gaze/ray picker |
| `client/surface-overlay.js` | `controllers()` | the surface debug overlay |
| `dynamics/grab/grab.js` | `controllers()`, `claim`, `reserve` | tier-C manipulation |
| `dynamics/water/water.js` | `list()`, and `ConjureContact` (§10) for the hand half | ripples |
| `client/conjure-contact.js` | `acting()`, `joints`, `radii` | the contact query every module asks (§10) |

**One reader of XR input, and `ConjurePointers` now publishes all 25 joints** — so `ConjureContact`
consumes them rather than reading the frame, which is what the seam was for. Two direct readers of the
XR frame remain and both are deliberate: `occlusion.js`, which works and has no second reason to change;
and `hand-rig.js`/`hands-fit.js`, one of which drives a skeleton and the other of which exists to look at
the raw frame. `ConjurePointers` still reads exactly one joint for `pointer.fingertip`. `client/occlusion.js` builds its own
25-joint hand mesh straight from `frame.getJointPose` ([`occlusion.md` §4](./occlusion.md)). And
`client/hands-fit.js` reads all 25 for the debug overlay (§9). None of the three share a read, which is
the duplication this layer removed for buttons and has not yet removed for joints —
[`backlogs/input.md`](../backlogs/input.md). The overlay is a deliberate exception: it exists to *look
at* the raw frame, so routing it through a cache would put the thing under test behind the thing it is
testing.

---

## 8. Diagnostics

XR interaction cannot be unit-tested, so on-device tracing is the only way to see what input is doing,
and the same rule as a module applies ([`dynamics.md` §5](./dynamics.md)): mirror to the console **and**
to `POST /client_log`, gated by `window.CONJURE_DEBUG_LOG`, tagged `[pointers]`, one-shot messages
latched so a per-frame condition logs once rather than flooding.

---

## 9. `?hands=fit` — looking at the tracked hand skeleton

`client/hands-fit.js`, on the scene, inert unless the query param is set. A debug mode, not a feature:
it renders the skeleton the runtime believes in so it can be read against the real hand underneath it.
In passthrough the compositor draws your hand and our layer blends over it, so anything drawn at a joint
is seen against the real joint.

| `?hands=` | Draws |
|---|---|
| `off` (default, and anything unrecognised) | nothing; the component returns from `init` |
| `joints` | a sphere at each of the 25 joints, **at the runtime's own reported `radius`** |
| `axes` | an axis triad per joint |
| `fit` (also `1`, `true`, `on`) | both |

Spheres are translucent (0.35) with `depthWrite: false`, so the real knuckle shows through and the
spheres never hide each other; the wrist is drawn in its own colour because it is the one joint the
WebXR spec leaves loose (*"SHOULD point roughly towards the centre of the palm"*) and therefore the one
to look at first. Left is cyan, right amber.

The triad draws **the three directions the convention is stated in** — `+X` red, **`−Y` green** (out
through the palm), **`−Z` blue** (along the bone, away from the wrist) — rather than the positive axes,
because drawing `+Y`/`+Z` would render the convention backwards and read as a broken rig. Lines skip
depth testing entirely; a one-pixel line behind a sphere is invisible and the direction is the point.

### What the numbers answer, and which of them carries a premise

Bones are rigid, so the 24 bone lengths are **pose-invariant** — measured in a fist or a flat hand they
agree. The overlay averages 30 frames per acquisition and reports:

| Probe | Reads | Premise |
|---|---|---|
| **jitter** — how much each length moved across the sampled frames | constant ⇒ the runtime is **posing a stored skeleton**, because posing rotates bones and cannot stretch them; movement ⇒ joints are positioned **independently**, which is exactly what the WebXR privacy guidance says an anonymising UA must not do | **none** |
| **`s`** — the median of tracked ÷ our model's bind length **over the 14 real bones**, and the spread (`cv`) around it | `cv ≈ 0` ⇒ the runtime's skeleton is a uniform scale of our model, and the median is the `s` a worn hand needs | **our model is the canonical skeleton** |
| **`s` across acquisitions** | unchanged after hands leave view and return ⇒ nothing is being re-estimated | none |
| **left vs right** | a real pair differs by a millimetre or two; the same table twice does not | none |
| **radii** | how many *distinct* values the 25 radii take. Three or fewer is a table, not a measurement | none |

Two windows per acquisition, because the first version conflated convergence with steady state:
frames 1–30 are the **fresh** reading, 61–90 the **settled** one. The settled figure is the headline and
the gap between them is the re-acquisition probe asked without making you move.

**Only 14 of the 24 segments are bones.** `wrist → *-metacarpal` is the offset between an arbitrary
frame origin and the hand, pointing a different way for each finger; `*-distal → *-tip` compares a
WebXR tip — at the fingertip *surface*, derived from the runtime's own estimate — against an authored
tip bone. Neither carries information about how long a bone is, so `s` is the median over the other
fourteen and the three groups are reported separately.

**Measured on a Quest 3, 2026-09-17.** Left and right differ by **0.240 mm** and the figures move
across re-acquisitions, so this runtime does not serve a static mirrored table — it re-estimates per
session, and hand size is real data here. Taken over all 24 segments the ratios spread by **36%** at a
median of **1.017**, which read as a differently-proportioned hand and is nothing of the kind.

Grouped, the spread falls from 36% to **5.0% (left) and 5.6% (right)** — so the ten non-bone segments
were most of it and not all of it. The residual is **not cleanly attributable**: our own two hand
models disagree by `cv` **2.86%** across those same 14 bones, so a 5% disagreement is being measured
against a reference that is itself 3% inconsistent.

It is small enough not to matter for what `s` is *for*. Joint positions are exact by construction — each
joint is placed at the pose the runtime reports — so `s` never positions anything and per-bone error
cannot accumulate along a chain. It scales girth, because joint positions carry length and never
thickness, and 5% of a finger's girth is under a millimetre.

**Read jitter first.** The `s` probe's premise is known to be shaky: our own two hand models are not
mirrors of each other — their index metacarpals differ by **6.3 mm**, measured — so a non-zero `cv` is
as likely to be about our model as about the runtime. A stale bind table produces the same symptom, which
is why `scripts/hand_bind.py --check` exists.

The overlay draws with no flags; the numbers need `--debug-log` to reach `temp/conjure.log`. The
verdicts therefore also go on a small HUD, because in a headset the terminal is not where you are
looking.

**It cannot settle alignment to better than several millimetres.** Passthrough is reprojected, so
judging fit by eye at close range has that error floor: it resolves *"my fingertip is 1.5 cm short"* and
cannot resolve the 2 mm that separates a real pair from a mirrored one. That is what the numeric half is
for.

**`--occlusion hands` defeats it** — the hand occluder carves a passthrough hole exactly where the
overlay draws, so joints vanish behind your real hand. The component logs this when both are on;
`?occlusion=off` is the per-client override.

---

## 10. `ConjureContact` — what a hand is touching, as a QUERY

`client/conjure-contact.js`. A consumer asks *"which of my hand's bones are inside this volume, how far
in, and how fast"* and gets an answer in **its own frame**. It never runs a cast and never learns what a
joint is.

Before it, the one module that reacted to a hand did that arithmetic itself: take `pointer.fingertip`,
convert it into the module's local frame, reject beyond a depth, scale to UV. Thirty lines per module,
each with its own idea of what *touching* means, and each reading exactly one of the twenty-five joints
because that was the only one published. Three modules would have been three answers to one question.

| | |
|---|---|
| `inBox(object3D, half, opts)` | the general query. A plane with a touch depth **is** a box, which is why there is no separate plane query |
| `inSphere(centre, radius, opts)` | a point query with the radius as slack, so its corners are round — a sphere approximated as a box over-reaches by 73% of the radius at the corners |
| `capsules(sceneEl)` | every bone as a world-space capsule, for a consumer that wants to draw them |

`opts.joints` restricts which joints may contribute, and `opts.owner` applies the same arbitration
every other consumer of this layer follows — `grab` holding a pointer mid-drag must not also be
rippling the water it is dragging past.

**Capsules, not points.** A fingertip *joint* can be outside a volume while the finger is inside it:
joints are ~25 mm apart and the flesh ~8 mm thick, so a point test leaves a centimetre of finger that
touches nothing — and a thin volume is exactly where that gap falls, which is exactly where `water`
lives. Each of the 24 bones is a capsule: its two joints and the larger of their reported radii. A
missing radius falls back to a stated 8 mm and says so once, never to `NaN`.

**Exact, not sampled.** Distance from a point to a convex box is convex and stays convex along a
straight segment, so a ternary search on the closest-approach parameter converges on the true minimum —
fourteen steps narrows a 25 mm bone to about 2 µm. Fixed-interval sampling was the first idea and is
worse in a specific way: it can only ever *miss* a contact, and the size of the miss scales with the
bone, so the false negatives would have been longest on the long bones, which are most of the hand.

**Speed needs one frame of memory and two slots to hold it.** A single `previous` map written at the end
of each query works for one consumer and breaks for the second: the first module of the frame overwrites
it with *this* frame, so the second compares the frame against itself and every speed reads zero.
`thisFrame` is therefore captured once per distinct frame and the one it displaces becomes `lastFrame`,
which every speed is measured against. A gap over 250 ms reads as zero — that is a dropped track, not
motion.

**No events, and nothing new on the wire.** A touch is a **cause**, and `ConjureBus` already carries
causes to peers ([`dynamics.md` §2](./dynamics.md), tier B): a module broadcasts its own touch exactly as
it did before, every client simulates from it, and nothing about anyone's fingers is ever transmitted.
That is why hands needed no sync tier.

**`water` is the migration that proves the shape.** It asks for `[width/2, height/2, touchDepth]` and
gets UV-ready local coordinates, having deleted the proximity half of its own `_toUV`. Restricted to
`index-finger-tip` so the behaviour is **identical** to what it replaced — the query returns every bone
that reaches the water, which is better and is a different feature, and widening it is deleting one
option deliberately.

---
