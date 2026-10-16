# Plan — put the hands work on a headset

**Status:** all phases open · **Opened:** 2026-09-18

**This file is temporary.** It holds one verification sequence across the input layer, the figure
runtime and a dynamic module, and is deleted once every phase has settled. Each phase names where its
result goes: a confirmation to [`specs/input.md`](../specs/input.md) or
[`specs/figures.md`](../specs/figures.md), a defect to [`backlogs/input.md`](../backlogs/input.md), a
campaign that produced negative knowledge to [`investigations/`](../investigations/).

**Dissolves to:** `specs/input.md` §2–§4 and §10 · `specs/figures.md` §8f · `backlogs/input.md`
(whatever fails) · and it **unblocks [`plans/hands.md`](./hands.md)'s own dissolution**, which should
not happen while two of its five phases have never been touched.

---

## 0. Why this plan exists

Five phases were built in a day. **Two have been read on a headset and two have never been run at
all** — and the two untouched ones are the layers the others sit on.

| built | verified how | what is missing |
|---|---|---|
| `?hands=fit` (phase 0) | **worn** — jitter, `s`, `cv`, L/R all read | nothing; this phase is done |
| worn hand models (phase 2) | **worn**, and corrected twice from what it showed | reload, take-off, the occlusion conflict |
| `pinch`/`grasp`/`poke` (phase 3) | 8 unit tests against synthetic hands | **never tried** — no hand has resolved an action on a device |
| `ConjureContact` (phase 4) | 11 unit tests against a stubbed frame | **never tried** — and `water` now depends on it |

**The synthetic hands are the thing to distrust.** Every threshold in phase 3 came from a fixture
written to match an idea of a hand, and that fixture was wrong twice before it was right: the first
version curled fingers by shortening them uniformly, which kept them perfectly straight, and the second
held a pinch at a distance that was a legitimate press. A green unit test here means the arithmetic does
what was meant — not that what was meant is a hand.

**Ordered so an early failure explains a later one.** Phase 4 reads its joints from the layer phase 3
changed, so a broken joint publish looks like broken contact. Phase 2 has its own read and is
independent of both, which is why its leftovers come last.

---

## 1. Prerequisite — the catalog is one revision behind

`FRAME_REV` went to **21** with the hand material fields. Until a refresh runs, `--pair` can put a grey
untextured right hand beside a textured left, which reads as a broken import.

**RESTART THE SERVER FIRST.** A refresh derives only what the *server process* knows, so a server
started before the hand work reports `0 updated` — which reads as "nothing needed doing".
`ctl refresh-models` warns when its own `FRAME_REV` is behind the code on disk; believe it.

```
python3 -m conjure.ctl refresh-models
python3 -m conjure.ctl wear                  # what is there to wear, and what each is dressed in
```

**Expect:** two hands whose material lists match (`ArmsVR, FingernailsVR`), and no row reading
`NO MATERIALS`. **If every row says that**, the refresh did not re-derive — stop here, because nothing
below will mean anything.

On the headset the LAN address now redirects to the tunnel, so one address does for everything:

```
http://<lan>:8080/tunnel?hands=fit
```

`?hands=fit` is the standing way to look at a hand and is worth leaving on for all of this.

---

## 2. Phase 3 — a hand resolves an action

**Never run.** Everything else here sits on it: `ConjureContact` reads the joints this phase publishes,
and `grab`, the beams and the surface overlay all moved onto `acting()`.

**Put the controllers down.** With controllers in hand there is nothing to synthesise, and every test
below passes for the wrong reason.

| do | expect | because | observed |
|---|---|---|---|
| hold a hand up, relaxed | **a beam appears** — from the runtime's own hand aim ray (`targetRaySpace`), which originates near the WRIST/palm and points where the hand points. **Not** an index-finger ray, and finger curl does not steer it | `controller-beams` keys off `armed()`, and a hand resolves actions now. The first thing that proves the layer sees the hand at all | Beams appear, but they are not coming exactly from my index finger.  They are generally pointed where my arms point, originating close to the palm of my hand and below my index finger. moving my wrist affects them the most, but not even completely.  Moving fingers does not adjust the beams at all. **→ correct behaviour; the expectation was wrong.** A hand's ray is `src.targetRaySpace` (`conjure-pointers.js:187`), the platform's aim ray — `specs/input.md` §4 has always said so and never claimed the finger. The row asked for an index-finger beam that was never built. What it actually proves is the thing worth proving: a beam appears at all, so the layer resolves actions for a hand. **PASSES** |
| keep it up and wait | the beam **goes out** after ~10 s | `armed()` lingers for `beam_timeout` after the last pull. A beam that never goes out means a *resting* hand is reading a pull | They do go out after about 10s, and they come back on if I move my hand significantly, though it is unclear to me exactly what is determining when they come back and how still I need to be to keep them off |
| pinch at an object | `grab` **highlights** it | `select` binds `["trigger", "pinch"]` | I can pull the corners of a model to resize it while I am pinching. |
| make a fist at an object and move your hand | it **comes with you** | `grab: ["grip", "grasp"]` — so it is the fist that drags, not the pinch | this works as described |
| **make a fist near water** | it does **not** ripple | the gesture exclusion. Measured before the fix: a fist read `pinch` 0.77 *and* `grasp` 0.80, so closing your hand fired both | it does not ripple |
| **point at an object** | it is **not** grabbed | the same fault the other way: `grasp` was the mean of four fingers, so three curled fingers grabbed while you pointed |  it is not grabbed |
| pick the controllers back up and grab something | exactly as before | **no module changed** to gain any of this, which is the payoff claim | works as expected |

**The one number worth reading.** Client diagnostics go to `temp/conjure.log` by **default** (`debug_log` is True; `CONJURE_DEBUG_LOG=0` turns them off). There is no `--debug-log` flag — passing one makes the server refuse to start, which reads as "I cannot get the log" when you already have it. A line per hand:

```
[pointers] hand left:hand — 25/25 joints, pinch/grasp/poke 0.00/0.00/0.00; straightness index 0.94 …
```

`CURL_OUT` is **0.90** and is the threshold to suspect: a relaxed hand is not a *straight* hand, so if
a comfortable grab reads `grasp` below 0.5, that number is too generous — and the straightness column
says what it should be instead of inviting another guess.

**What a failure means, by shape:**

- *No beam at all with hands up* → the hand is not in `acting()`, or `armed()` never fires. Check that
  the `[pointers] hand …` line exists; if it does not, the joint read is failing and **phase 4 will
  fail too, for this reason and not its own**.
- *A beam that never goes out* → a resting hand reads as `select` past `beam_trigger` (0.05). An open
  hand's pinch should be 0.00; if it is not, `PINCH_OPEN` is too small.
- *Grabs but never highlights* → `select` resolves and the reservation does not, which is `grab`'s
  arbitration rather than this layer.
- *Two actions at once* → the exclusion regressed. The three control values from the log are the whole
  diagnosis.

**Dissolves to:** `specs/input.md` §2–§3.

---

## 3. Phase 4 — a module reacts to a hand without knowing what a joint is

**Never run**, and `water` now depends on it: its hand path *is* `ConjureContact`, and its own
proximity test survives only as a stale-client fallback.

Conjure a water picture. Controllers down.

| do | expect | because | observed |
|---|---|---|---|
| touch the water with your **index fingertip** | ripples, from that point | the migration. `touchDepth` is 5 cm, so it triggers before you reach the plane | It does trigger before I reach the pane |
| drag your fingertip across it | a continuous stroke | `_segment` interpolates between frames; a dotted line means frames are being dropped, not that contact is wrong | it is continuous not dropped |
| touch it with a **knuckle**, or the back of your hand | **nothing** | the query is restricted to `index-finger-tip`, so behaviour is identical to what it replaced. **This is the acceptance test**: if the whole hand ripples, the `joints` option is not being applied and the migration silently changed behaviour | nothing happens with knuckele or other fingertip |
| hold a fingertip 10 cm off it | nothing | outside `touchDepth` | nothing happens with fingertip further away |
| pick up a controller and pull the trigger at it | ripples, as always | the ray path is untouched | works as expected with the controller |
| grab the water's frame with ONE hand and bring **that same hand's** fingertip to the surface | it does **not** ripple from the hand that is dragging. A **free** second hand touching it SHOULD ripple — the gate is per-pointer, not global | `opts.owner: "water"` defers to whoever holds the pointer | I dragged the water picture frame and moved it so that it was dragging against my pointer finger tip and it did riopple, not as expected **→ correct behaviour; the row was testing the wrong hand.** `grab` claims only the pointer it drags with (`CP.claim(p.key, "grab")`, grab.js:1033) and `availableTo` excludes only that one, so a free hand genuinely touching the water ripples — as the spec says: *"grab holding a pointer mid-drag must not also be rippling the water it is dragging past"*. Re-test as restated, with the DRAGGING hand. **Not a defect** |

**Two things nothing here tests, said rather than implied:**

- **`speed` is computed and no consumer reads it.** It cannot be verified by watching and nothing
  depends on it; only unit tests exercise it.
- **`capsules()` has no consumer either.** Same.

**One log line to watch for:**

```
[contact] the runtime supplies no joint radius — falling back to 8 mm per bone
```

If that appears, every bone is 8 mm thick regardless of finger — which **contradicts `?hands=fit`**,
where per-finger radii were read and used for the fingertip offset. Two parts of the same codebase
disagreeing about whether the runtime supplies a radius is worth chasing, not working around.

**Dissolves to:** `specs/input.md` §10, and one line in `specs/dynamics.md` §6.

---

## 4. Phase 2 leftovers — the three things wearing a hand has not been asked

Worn and corrected twice already, so this is only what that session did not cover.

### Getting a pair on

A hand has to be **placed** before it can be worn, and nothing else in the CLI places a library model,
so `wear` does both. One command is the whole setup:

```
.venv/bin/python -m conjure.ctl wear --pair
```

That places both hands as `hand_left` and `hand_right` and wears them — those are the entity ids the
table below wants. `--hand auto` is implied and is almost always right: which hand a model IS was
**measured from its geometry** at import, not read from the `_L` in its name.

**It matches on MATERIAL NAMES, not on order, and that matters here.** The catalog holds five hand
files and only one of them is a pair:

| asset | label | side | materials |
|---|---|---|---|
| `b8f676bea0758536.glb` | VR_hand_L | left | `ArmsVR, FingernailsVR` ← **the pair** |
| `f586068580143caa.glb` | VR_hand_R | right | `ArmsVR, FingernailsVR` ← **the pair** |
| `4494f48be0fd8cfb.glb` | VR_hand_L | left | `ArmsAR, FingernailsAR` — orphaned AR left |
| `119aad62cfc6f256.glb` | VR_hand_R | right | `New Material` |
| `5a36d74c3bc4b802.glb` | VR_hand_R | right | **none** |

Taking the first of each side puts the materials-less right beside a textured left, which reads as a
broken import rather than as two files that were never a pair. If `--pair` prints *"no matching pair"*,
the catalog has not been refreshed — go back to §1.

To see the list without placing anything, or to do it a side at a time:

```
.venv/bin/python -m conjure.ctl wear                                               # what is there to wear
.venv/bin/python -m conjure.ctl wear b8f676bea0758536.glb --place --name hand_left
.venv/bin/python -m conjure.ctl wear f586068580143caa.glb --place --name hand_right
```

`--place` is what says "this id is a LIBRARY asset, place it first". Without it the id is read as an
**entity** that is already in the scene — which is the form the `--hand off` row below uses.

### Taking them off

**`wear` with no arguments is also how you find out what is on.** It lists every PLACED hand with its
entity id, its measured side, and whether it is worn:

```
placed, and wearable right now:
  hand_left              left   VR_hand_L              WORN on the left
  hand_right             right  VR_hand_R              WORN on the right
```

Wearing does not consume the model, it **occupies** it, so taking it off puts it back exactly where it
was placed. **`--hand off` needs the entity id**, and there is **no bulk off** — one command per hand:

```
.venv/bin/python -m conjure.ctl wear hand_left  --hand off
.venv/bin/python -m conjure.ctl wear hand_right --hand off
```

A bare `wear --hand off` with no id does **not** take everything off; it falls through and just prints
the list. Note the asymmetry, because it is easy to assume otherwise: `--tip-out` with no id adjusts
EVERY worn hand, while `--hand off` with no id adjusts none. If the ids are not `hand_left`/`hand_right`
— they only are when `--pair` placed them — read them off the list above.

| do | expect | because | observed |
|---|---|---|---|
| wear a pair, then **reload the page** | still worn, both hands | the state is on the entity ([`decisions.md`](../decisions.md) §30), so it replays from the snapshot. A per-client setting could not do this, and this is the claim that justified the fork | still worn after refresh |
| `wear <id> --hand off` | the hand is back **where it was placed**, and grabbable again | unwearing restores the anchor and drops the `grab` exemption | the hands remain exactly where they were when the command was executed, probably not where they were originally placed **→ a real defect, FIXED 2026-10-16.** `--hand off` patches `hand: ""` and the tick returned on the empty value before reaching `_restore()`, so every bone stayed frozen at the last pose it was driven to. The entity never moved; the bones had. One line, plus two tests that fail without it |
| `?occlusion=hands` — see **The occlusion conflict** below for the order | **your real hand visible through the virtual one**, and solid again at `?occlusion=off` | the two features defeat each other by construction. Documented, not fixed — the point is that it now *says so* instead of looking like a transparency bug | **PASSES, after a fix.** Real hand visible through the model, confirmed by eye, and the warning lands once per hand. **It did not warn at first:** `hand-rig` and `hands-fit` read `window.CONJURE_OCCLUSION` (the SERVER's `--occlusion`, `off` here) while `occlusion.js` resolved `?occlusion=hands` from the URL — so the occluder ran (`handsSeen=2 meshes_filled=2`) and both warnings stayed silent, which is the transparency bug this row exists to pre-empt. Fixed 2026-10-16: the resolved mode is published once as `window.ConjureOcclusionMode()` and both read it. See `backlogs/input.md` |
| dial `wear --tip-out off`, then `radius`, while wearing | the fingertips change, no reload | the component reads `tipOut` per frame and has no `update` handler | works as expected |

### Dialling the fingertips

Load `?hands=fit` with **no** `?occlusion=` — the model wants to be solid so you can see its tips, and
the overlay draws a sphere at each joint's reported radius as a reference. Controllers down, pair worn.
Then run these three **with no reload between them**, which is the actual claim:

```
.venv/bin/python -m conjure.ctl wear --tip-out off       # 0 — where `wear --pair` leaves you
.venv/bin/python -m conjure.ctl wear --tip-out radius    # one tip radius out, per finger
.venv/bin/python -m conjure.ctl wear --tip-thumb 0.8     # the thumb alone; the offset is untouched
```

No id, deliberately: `--tip-out` with no id adjusts **every worn hand**. (That is the one bulk path
here — `--hand off` needs an id per hand.)

**Expectations are exact, because the radii are measured.** This wearer's tips, from the `?hands=fit`
read: thumb **0.88**, index **0.66**, middle **0.66**, ring **0.62**, pinky **0.54** cm. So:

| command | `temp/conjure.log` should read | by eye |
|---|---|---|
| `--tip-out off` | `tipOut=off … 0.0/0.0/0.0/0.0/0.0mm` | **nothing changes** — the baseline, not a test |
| `--tip-out radius` | `tipOut=radius thumb×1 → ~8.8/6.6/6.6/6.2/5.4mm` | tips extend 5–9 mm; thumb most, pinky least |
| `--tip-thumb 0.8` | `tipOut=radius thumb×0.8 → ~7.0/6.6/6.6/6.2/5.4mm` | **only the thumb** retracts, ~1.8 mm |

These are small — half a centimetre at most, under 2 mm for the thumb. The usable visual judgement is
whether the model's fingertips stop falling **short** of your real ones at `radius`, and whether the
thumb stops **overshooting** at `0.8`.

**The log will NOT show the dial, so do not wait for it.** `_report` is gated on `this._said` and fires
once per wearing, at the first tracked frame — the millimetres above are what it would print *if* that
value had been set before the hand was first tracked. Dialling live changes the hand and emits nothing,
so an unchanged `tipOut=0` line means only that the report already ran. Your eye is the only instrument
for this row; the numbers say what size of change to look for, not what to read back.

**What makes it pass or fail is cruder than the numbers: each command must take effect with no reload.**
The component reads `tipOut` per frame and has no `update` handler, so A-Frame replacing `this.data`
does not re-run `init` and the skeleton, bind pose and captured rest all survive a dial. If a change
only appears after a reload, the row fails whatever the millimetres say.

### The occlusion conflict

`--occlusion` is a **server** flag, but there is a per-client override in the URL — use that: no
restart, and the worn hands survive it.

**The order matters, and getting it wrong looks exactly like the feature failing to warn.**

1. **Wear the pair.** `.venv/bin/python -m conjure.ctl wear --pair`
2. **Put the controllers DOWN.** Occlusion needs tracked hands. With controllers up you get
   `[occlusion] no tracked hands; inputSources=[]` and nothing else happens at all.
3. **Reload the headset** at `http://<lan>:8080/tunnel?occlusion=hands`

Wearing *after* the page has loaded will not do it either: `hand-rig`'s `init()` reads
`window.CONJURE_OCCLUSION` **once**, so the hand has to be worn before the component initialises. Wear,
then reload — not load, then wear.

Leave `?hands=fit` **off** for this one. `hands-fit.js` carries its own copy of the same warning, so with
both on you read two conflict messages and an overlay whose joints also vanish behind your real hand.

**What you judge, by eye:** your real hand showing through the virtual one, and the model going solid
again at `?occlusion=off`. The matching `[hand-rig] ! --occlusion hands is on …` line lands in
`temp/conjure.log` once per hand — that is a check for whoever is at the terminal, not something to go
hunting for in the headset.

**Dissolves to:** `specs/figures.md` §8f.

---

## 5. What is deliberately NOT in scope

- **A second pair of hands.** `radius` + thumb `0.7`, and every phase-0 number, were read on one
  wearer. Recorded as an opportunistic check in `backlogs/input.md`; nothing branches on the answer.
- **`poke`.** Synthesised, exclusive, and bound to nothing. There is nothing to watch.
- **Peers.** A worn hand is local-only: for everyone else the entity sits where it was worn, and
  nothing about anyone's fingers goes on the wire. Not a defect, and not testable with one headset.
- **The joint-read seam.** `occlusion.js` still reads the XR frame itself. Deliberate, and untouched.

---

## 6. Where each result goes

| result | destination |
|---|---|
| a phase passes | a dated confirmation line in the matching spec section |
| a phase fails cleanly (wrong output, clear cause) | `backlogs/input.md`, with the numbers from the log |
| a threshold is merely wrong | change the constant, and record the measurement that replaced the guess — `CURL_OUT` is the expected one |
| a phase fails and resists diagnosis | a section in `investigations/` |
| a number in this file turns out wrong | fix it **here and in the spec**, and say which measurement replaced it |

Delete this file when §2, §3 and §4 have settled — and then `plans/hands.md` can dissolve too.
