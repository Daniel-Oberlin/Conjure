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

| do | expect | because |
|---|---|---|
| hold a hand up, relaxed | **a beam from your index finger** | `controller-beams` keys off `armed()`, and a hand resolves actions now. The first thing that proves the layer sees the hand at all |
| keep it up and wait | the beam **goes out** after ~10 s | `armed()` lingers for `beam_timeout` after the last pull. A beam that never goes out means a *resting* hand is reading a pull |
| pinch at an object | `grab` **highlights** it | `select` binds `["trigger", "pinch"]` |
| make a fist at an object and move your hand | it **comes with you** | `grab: ["grip", "grasp"]` — so it is the fist that drags, not the pinch |
| **make a fist near water** | it does **not** ripple | the gesture exclusion. Measured before the fix: a fist read `pinch` 0.77 *and* `grasp` 0.80, so closing your hand fired both |
| **point at an object** | it is **not** grabbed | the same fault the other way: `grasp` was the mean of four fingers, so three curled fingers grabbed while you pointed |
| pick the controllers back up and grab something | exactly as before | **no module changed** to gain any of this, which is the payoff claim |

**The one number worth reading.** With `--debug-log` on, `temp/conjure.log` gets a line per hand:

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

| do | expect | because |
|---|---|---|
| touch the water with your **index fingertip** | ripples, from that point | the migration. `touchDepth` is 5 cm, so it triggers before you reach the plane |
| drag your fingertip across it | a continuous stroke | `_segment` interpolates between frames; a dotted line means frames are being dropped, not that contact is wrong |
| touch it with a **knuckle**, or the back of your hand | **nothing** | the query is restricted to `index-finger-tip`, so behaviour is identical to what it replaced. **This is the acceptance test**: if the whole hand ripples, the `joints` option is not being applied and the migration silently changed behaviour |
| hold a fingertip 10 cm off it | nothing | outside `touchDepth` |
| pick up a controller and pull the trigger at it | ripples, as always | the ray path is untouched |
| grab the water's frame and drag it past your other hand | it does **not** ripple while dragging | `opts.owner: "water"` defers to whoever holds the pointer |

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

| do | expect | because |
|---|---|---|
| wear a pair, then **reload the page** | still worn, both hands | the state is on the entity ([`decisions.md`](../decisions.md) §30), so it replays from the snapshot. A per-client setting could not do this, and this is the claim that justified the fork |
| `wear <id> --hand off` | the hand is back **where it was placed**, and grabbable again | unwearing restores the anchor and drops the `grab` exemption |
| wear with `--occlusion hands` | a log line, and your real hand showing through the model | the two features defeat each other by construction. Documented, not fixed — the point is that it now *says so* instead of looking like a transparency bug |
| dial `wear --tip-out off`, then `radius`, while wearing | the fingertips change, no reload | the component reads `tipOut` per frame and has no `update` handler |

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
