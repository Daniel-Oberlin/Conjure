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

**Tracked hands have no controls.** They carry no gamepad, so every control reads 0 and therefore
**every action is inactive on a hand** — there is nothing for a binding to resolve. What a hand
publishes is pose and `fingertip` (§4). `water` is the one consumer that reads a hand today, and it
does so by fingertip proximity rather than by action. Synthesising controls for hands (`pinch`,
`grasp`, `poke`) is [`backlogs/input.md`](../backlogs/input.md).

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
- `ConjurePointers.controllers(sceneEl)` — controllers only, the common case for ray interaction. The
  filter is `!p.isHand && p.source.gamepad`, so **tracked hands are excluded by construction.**

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
| `dynamics/water/water.js` | `list()`, `isHand`, `fingertip` | the only hand-aware consumer |

**One reader of XR input, but not the only reader of XR joints.**
`client/occlusion.js` builds its own 25-joint hand mesh straight from `frame.getJointPose`
([`occlusion.md` §4](./occlusion.md)); `ConjurePointers` reads exactly one joint,
`index-finger-tip`, and publishes it as `pointer.fingertip`. The two do not share a read. Unifying them
is [`backlogs/input.md`](../backlogs/input.md).

---

## 8. Diagnostics

XR interaction cannot be unit-tested, so on-device tracing is the only way to see what input is doing,
and the same rule as a module applies ([`dynamics.md` §5](./dynamics.md)): mirror to the console **and**
to `POST /client_log`, gated by `window.CONJURE_DEBUG_LOG`, tagged `[pointers]`, one-shot messages
latched so a per-frame condition logs once rather than flooding.
