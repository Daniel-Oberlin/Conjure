# Plan — hands: models you wear, and hands as input

**Status:** phases 0, 1 and 2 **built** 2026-09-17 — phase 0 read on device; phase 2 worn on device
and corrected twice · phases 3, 4 open · **Opened:** 2026-09-14

**This file is temporary.** A plan spans areas that the specs and backlogs deliberately keep apart, so
it exists to hold one sequence across them while it is being executed. Each phase names where its
content goes when it lands, and the file is deleted when the last phase has settled — finished work to
[`specs/`](../specs/), abandoned or deferred work to [`backlogs/`](../backlogs/), forks already taken
to [`decisions.md`](../decisions.md). If it outlives its phases it has become a backlog by another
name and should be dissolved on the spot.

**Dissolves to:** **[`specs/input.md`](../specs/input.md) + [`backlogs/input.md`](../backlogs/input.md)**
— a new area, opened by phase 1 on 2026-09-17, because `ConjurePointers` is the one reader of XR input
and had been living inside `specs/dynamics.md` §6 while its consumers stopped being modules
(phases 1, 3, 4) · `specs/figures.md` + `backlogs/figures.md`
(phase 2, the worn component and what import records) · `specs/dynamics.md` (phase 4, the module-facing
half of contact) · `specs/occlusion.md` (a cross-reference only — see *Open*).

---

## 1. What this is built on

Measured on the catalog and the files themselves, 2026-09-14. Where one of these is wrong, the phase
resting on it is wrong too.

**The hand models are already labelled in the vocabulary we would have had to invent.** All 25 joints
are named exactly the WebXR hand joint set — `wrist`, `thumb-metacarpal` … `pinky-finger-tip`, no
exporter prefixes. So the two indirections the whole of [`specs/figures.md`](../specs/figures.md) is
built on — *which node* (`humanoid`) and *which way* (`humanoid_axes`) — **collapse to identity here.**
There is no discovery layer to write, no convention table, no anatomical frame, and no retargeting.

**And the bind pose is authored in the WebXR joint frame**, which is what makes a transform copy legal
rather than approximate. Down the index chain, each bone lies along its own local **−Z**:

    wrist                     → index-finger-metacarpal      3.80 cm   dir·(−Z) = 0.958
    index-finger-metacarpal   → …-phalanx-proximal           5.86 cm   dir·(−Z) = 0.999
    …-phalanx-proximal        → …-phalanx-intermediate       4.06 cm   dir·(−Z) = 1.000
    …-phalanx-intermediate    → …-phalanx-distal             2.43 cm   dir·(−Z) = 1.000
    …-phalanx-distal          → index-finger-tip             1.16 cm   dir·(−Z) = 1.000

against the spec's own words — "the `-Z` direction pointing along their associated bone, away from the
wrist" and "the native origin has its `-Y` direction pointing perpendicular to the skin, outwards from
the palm". ±Y measures as the palm normal on both hands (0.975 / −0.969, the sign flipping with the
mirror as it must). The wrist is the only joint the spec leaves loose ("SHOULD point roughly towards
the centre of the palm"), so it is the one to check on device first.

**Five catalog rows, and only one clean pair among them.** All five are 13 320 tris, 0.2094 m, one
25-joint skin, wrist→middle-tip 17.75 cm (L) / 17.80 cm (R), tagged across eleven capture sets
(`bride, jane, barbie, manager, blondie, stewardess, granny, kawaii, goddess, akari, oktoberfest`).
Content-addressing did not collapse them because they are not the same bytes:

| id | side | textures | materials |
|---|---|---|---|
| `b8f676bea0758536` | L | 1 image | `ArmsVR`, `FingernailsVR` |
| `f586068580143caa` | R | 1 image | `ArmsVR`, `FingernailsVR` |
| `eb43564f5f93126f` | L | same image | `ArmsAR`, `FingernailsAR` — no right twin |
| `ef25dc89e8a1de74` | R | none | none at all |
| `119aad62cfc6f256` | R | none | `New Material` |

So the pair to wear is `b8f676…` + `f586…`; the rest are an orphaned AR-material left and two
untextured rights. Worth stating because "we imported a lot of hand models" is true by row count and
false by content.

**Re-measured 2026-09-17 (`scripts/hand_bind.py`), and two of the claims above need correcting.** The
catalog now holds `5a36d74c…`/`4494f48b…` where it held `eb43564f…`/`ef25dc89…`, so the ids drifted; more
importantly, **all five rows carry one of exactly two skeletons** — every left agrees with every other
left to 0.005 mm, and likewise the rights. Five rows, two hands.

1. **The 25 joints are SIBLINGS, not a chain.** Every joint is parented directly to `hand_L`/`hand_R`,
   each with a Blender `<name>_end` tail node beside it. Phase 2's import gate below asks for "each finger
   a real parent→child chain" — **that check would refuse the very files it was written for.** The bind
   pose is still exact; a flat hierarchy says nothing about where the joints *are*. But the gate has to
   ask something else: the chain exists in the *skin's* joint list and in the geometry, not in the node
   tree.
2. **The pair is not a mirror.** The right index metacarpal is **6.3 mm** shorter than the left and its
   proximal 4.0 mm longer, while middle, ring and pinky agree to within 0.3 mm. So the two files cannot
   both be the canonical WebXR skeleton, and the dispersion probe below inherits a premise it does not
   quite have — see phase 0.

The `−Z` claim **holds, with one refinement**: down every finger `dir·(−Z)` is ≥ 0.986 on both hands,
and at the **wrist** it falls to 0.706–0.975. That is not a defect. The wrist has one frame and five
bones leaving it, which is exactly why the WebXR spec leaves that joint loose — and why it is the first
thing the overlay draws in its own colour.

**Today they are inert.** `rigged: true`, no `humanoid` map, so `/figure` refuses them, `inspect_figure`
has nothing to say, and `parts_unclassified` is `["model_hand_L"]`. They can be placed as props and
that is all.

**Two readers of XR hand joints already exist, and neither is shared.** `client/occlusion.js` builds
its own 25-joint mesh straight from `frame.getJointPose`; `client/conjure-pointers.js` reads exactly
one joint, `index-finger-tip`, and publishes it as `pointer.fingertip`. A worn hand would be the third
reader of the same data.

**With hands up, one module in the repo answers you.** `water` branches on `p.isHand` and uses
fingertip proximity, broadcasting the touch as a tier-B cause. `grab`, `controller-beams`,
`surface-overlay` and `conjure-client`'s gaze picker all call `ConjurePointers.controllers()`, which
filters tracked hands out by construction. **This, and not the model, is the reason wearing hands feels
thin today** — and it is fixed in the layer that already exists to fix it, because modules name
*actions* and never buttons.

**The skeleton already has two writers and a stated precedence** — a playing clip wins, a pose applies
when idle ([`figures.md` §8b](../specs/figures.md)) — and `figure.restore()` is the existing mechanism
for handing the bones back. A worn hand is the third writer and needs one more line of the same rule,
not a new mechanism.

**Two things will fight a worn entity if not exempted.** `_placeContent` re-solves a plane-relative
anchor for *every* `#world-root` child carrying `_frefPose`, every capture; and `grab` picks against
`data-bbox`. A hand on your wrist must leave both, and unwearing must restore the anchor — otherwise
taking it off loses it.

**XR gives size as well as pose.** `XRJointPose` carries a **`radius` in metres** per joint (the UA
must supply an emulated value when the device cannot determine one). But the spec's privacy guidance
says that a UA which anonymises **"must not round each joint independently. Instead the correct way to
round is to map each hand to a static hand-model"** — so hand size may be a fiction, identical for
every user. Whether Quest's browser does this is undocumented either way, and it cannot be settled by
reading: Meta contributed the canonical `generic-hand` assets to the WebXR input-profiles library, so
a runtime serving that skeleton and a model authored against it would agree *by construction*. Phase 0
settles it from one pair of hands.

---

## 2. Forks already taken

Recorded in [`decisions.md`](../decisions.md) rather than here, so they survive this file.

- **§24** (existing) Playback is an entity **component**, not a dynamic module. A worn hand is the same
  shape of thing — it decorates a placed model and has no independent existence — so this is cited, not
  re-litigated.
- **§29** (new) `ConjurePointers` gets its own spec. The alternative that looks cheapest — a
  `specs/hands.md` — splits the one reader of XR input across two documents, hand controls in one and
  controller controls in the other, which is precisely what the layer exists to prevent. Same move
  `captures.md` made on 2026-09-13, for the same reason: the consumers stopped matching the file name.
- **§30** (new) A worn hand is an **occupied entity**, not a per-client setting. It rides the existing
  patch/snapshot path, persists, replays on reload, and "put it down" names a real place in the world.
  A per-user default ("always wear these") is a later wrapper over it, not a parallel mechanism.
- **§31** (new) The model **conforms to the tracked hand** — all 25 joints, position and orientation —
  and **contact is computed from the tracked joints, never from the skinned mesh.** The two halves argue
  each other: conforming is what makes the rendered fingertip and the collider the same point, and
  reading contact off the joints is what keeps a 13 320-triangle skinned mesh out of every module's
  per-frame path (the rule `grab` already follows for figures, `figures.md` §6).

---

## 3. Phases

Phase 0 is a measurement and phase 1 is documentation; neither ships code that renders. Phases 3 and 4
are where hands become useful and are worth scheduling independently of 2 — **nothing in 3 or 4 needs a
hand model to exist.**

### Phase 0 — see the fit, then check the numbers — 🔶 BUILT 2026-09-17, UNREAD

*The overlay is documented in [`specs/input.md`](../specs/input.md) §9. The measurement it exists to make
has not been taken — that needs a headset. It settles into `backlogs/input.md` as a recorded
measurement; if it turns into a campaign, to `investigations/`.*

**What to do with it, in order.** `?hands=fit` on the client URL, controllers down, `--debug-log` on if
you want the full numbers in `temp/conjure.log` (the verdicts are on the HUD either way):

1. **Look.** Do 25 spheres sit on your 25 knuckles? The wrist is the pink one — does its blue line point
   along your forearm and its green line out through your palm?
2. **Read the HUD.** `jit=0.000%` means the runtime is serving a stored skeleton. That is the answer to
   the question this phase was opened for, and it needs no second person.
3. **Put your hands behind your back and bring them out again.** `s` unchanged across acquisitions is the
   same finding from a second direction.
4. **Put a glove on.** If nothing moves, nothing is being measured from the image.

**Measured 2026-09-17, and it corrects this plan twice.**

| probe | reading | what it settles |
|---|---|---|
| left vs right | **0.240 mm** apart | not one table mirrored — a shared skeleton would be identical |
| across re-acquisitions | moves | the runtime **re-estimates**; a stored table cannot |
| ratio, all 24 segments | `s` 1.017, spread **36%** | looked like a differently-proportioned hand, and was not |
| ratio, all 24 vs by group | 36% → **5.0% (L) / 5.6% (R)** on the 14 bones | most of the spread was ten segments that are not bone lengths — but not all of it |

So **hand size is real per-session data here**, which answers the question this phase was opened for:
the privacy clause's static-hand-model does not describe this runtime, and `s` has to be derived at
runtime rather than baked in. The code was the same either way, as §0 predicted; the *claim* changed.

**And 10 of the 24 segments are not bones.** `wrist → *-metacarpal` is the offset between an arbitrary
frame origin and the hand — our model's wrist node and the runtime's wrist pivot need not coincide, and
the discrepancy points a different way per finger. `*-distal → *-tip` compares a WebXR tip, which sits
at the fingertip *surface* and is derived from the runtime's own estimate, against an authored tip bone.
Neither says anything about how long a bone is.

**The grouping is confirmed and "the 14 are uniform" is FALSE.** Regrouping took the spread from 36% to
**5.0% (L) and 5.6% (R)** — so the ten non-bone segments were most of it, and something real is left.

Some of that residual is **ours**: our own two hand models disagree by **cv 2.86%** across the same 14
bones, concentrated in the index finger (`index-finger-phalanx-proximal` 0.937, `…-intermediate`
0.930). A 5% disagreement with a reference that is itself 3% inconsistent cannot be cleanly attributed,
and nothing here tries to.

**This does not change phase 2, and the reason is worth stating because the number looks alarming.**
`s` is not positioning anything — every joint is placed at the pose the runtime reports, so joint
positions are exact by construction and per-bone error cannot accumulate down a chain. `s` exists only
because joint positions carry **length and never girth**: it scales the flesh. 5% of a finger's girth is
well under a millimetre. A single scalar therefore stands, and per-joint scale stays where it is, in
*Open*.

What the 5% does buy is a number to check against: if a conformed hand looks wrong **at the knuckles**
rather than overall, that is this residual showing up as stretch in the skinning blend, and it is
`5%` of a bone — a couple of millimetres — not a scale bug.

That is a one-line correction to phase 2 with a real consequence: taking the median over all 24 would
have biased `s` by several per cent on every figure, in a direction that depends on the wearer's hand.

**Read the jitter probe before the ratio probe.** The ratio (`s`, `cv`) divides the tracked lengths by
*our model's* bind lengths, and §1 above now records that our own two models are not mirrors of each
other — so a non-zero `cv` is as likely to be about our file as about the runtime. Jitter has no premise.
A stale `BIND` table produces the same symptom as a mismatched hand, which is what
`python3 scripts/hand_bind.py --check` is for.

*Implementation note that changed the shape of the phase:* the overlay is now the **third** unshared
reader of XR joints, and it should stay that way — it exists to look at the raw frame, so routing it
through a cache would put the thing under test behind the thing testing it
([`backlogs/input.md`](../backlogs/input.md)).

The question is whether Quest reports a **per-user** hand or the static hand-model the privacy clause
permits — and the obvious experiment, two people in the headset, is not available. It does not have to
be. **No code branches on the answer**, which is the first thing to settle: `s` is derived from the
data either way, and if the runtime is serving a canonical skeleton then `s` simply comes out the same
for everyone. The casualty is a *claim* in the spec, not a design. And the worst case is mild — the
worn hand matches the skeleton the runtime believes in, which is the same skeleton the occlusion mesh
and every other WebXR app draw against, so rendering and contact still agree with each other.

**`?hands=fit` — look at the skeleton the runtime believes in, against the hand it belongs to.** In
passthrough the compositor draws the real world and our layer blends over it, so anything we draw at a
joint is seen against the real joint underneath. **That gap — tracked skeleton versus your real hand —
is the question**, asked in the terms the feature is for rather than as a claim about a spec clause.

The mode builds in two steps, because **the model half cannot come first**: conforming a mesh is phase
2's runtime, while markers need nothing but the joint read.

| Overlay | Settles | Lands |
|---|---|---|
| a sphere at each joint, **drawn at the reported `radius`** | whether the radii are plausible per joint or a repeated table — the channel renders itself — and whether 25 spheres sit on your 25 real knuckles | **built** (`?hands=joints`) |
| a small axis triad per joint | the −Z / −Y convention, and the **wrist**, the one joint the spec leaves loose (`SHOULD point roughly towards the centre of the palm`) | **built** (`?hands=axes`) |
| the conformed mesh, flat unlit colour at ~0.35 alpha, `depthWrite: false` | does the whole hand FIT — the flesh as well as the joints. Skin-over-skin is unreadable, so a contrasting colour or wireframe, never the texture; no depth write, or the hand occludes its own far side and fingers vanish | phase 2, as its acceptance check |

**What it cannot do**, and the reason the numbers below survive: passthrough is reprojected, so judging
alignment by eye at close range has an error floor of several millimetres. It resolves *"my fingertip is
1.5 cm short"* — the scale of a static-model mismatch on hands that are not average — and it cannot
resolve the 2 mm that separates a real left-and-right pair from a mirrored one.

So the numeric half stays, off the same `[pointers]` log of 24 segment lengths and 25 radii per hand:

| Probe | A static model looks like | A real measurement looks like |
|---|---|---|
| **Dispersion** of the 24 segment ratios (tracked ÷ bind) | every ratio the *same* number, σ ≈ 0 — the runtime's skeleton and an XR-authored model are both the canonical hand | a few per cent of scatter: nobody's finger proportions match a reference exactly |
| **Left vs right** | a bit-identical mirror | a millimetre or two of difference, which every real pair has |
| **Re-acquisition**: hands out of view, back; then a fresh session | the same value bit-for-bit, every time | a small wobble as the estimate re-converges |
| **Perturbation**: a thin glove or a wrapped finger | nothing moves | radii at least change — the estimate is coming from the image |

Dispersion is the sharp one: these models are authored to the same WebXR skeleton the runtime would be
serving, so *uniform* ratios are the tell, and it needs no ruler and no second person.

**`?hands=fit` is not throwaway.** It is the standing way to look at a worn hand for phases 2–4, in the
same spirit as `?stereodebug=` and `hands-solid`, and it is how any later complaint about alignment gets
localised. A mixed-reality capture of it belongs in `investigations/` as the durable artifact.

**Left standing:** a one-line opportunistic check in `backlogs/input.md` — when a second person is ever
in the headset, wear the hands and read the log line. Until then the spec says what was measured, on
whose hand, and that per-user variation is unverified.

### Phase 1 — extract `specs/input.md` — ✅ LANDED 2026-09-17

*Doc-only. `specs/dynamics.md` §6 moved out; `decisions.md` §29 records the fork.*

- [`specs/input.md`](../specs/input.md) + [`backlogs/input.md`](../backlogs/input.md): controls,
  bindings and actions, reading pointers, `armed()`, and the capture/reserve arbitration — moved, not
  rewritten.
- `specs/dynamics.md` keeps the `actions` **manifest field** (it is a `module.json` key) and links out;
  §6 keeps the clock and the bus and carries a pointer where the section was.
- `docs/README.md` gains the area entry; the top-level `README.md` lists it among the specs;
  `specs/occlusion.md`, `architecture.md` §11, `backlogs/figures.md` and
  `client/conjure-pointers.js`'s own header now point at it.
- `decisions.md` gained **§29, §30 and §31** — §29 resolved, the two phase-2 forks marked DESIGNED. They
  now survive this file, which is what §2 above claims.

**Two things the move documented rather than moved**, both facts about the code that §6 cited and never
stated. Neither is a new claim about behaviour:

- **A hand-qualified action resolves globally.** `value`/`active` read the named hand's pointer whichever
  pointer you ask, so a per-pointer loop over `reel` applies it twice — which §8 and §8b of
  `dynamics.md` each warned about locally, pointing at a §6 that did not say it. `started`/`ended` are
  the exception: own-hand only, ignoring the qualifier.
- **Tracked hands resolve no action at all**, because they have no gamepad — so `controllers()` filtering
  them out is not the whole reason hands feel thin; there would be nothing to resolve either way. That
  reframes phase 3: it is a *control synthesis* job, not a filter widening.

**Done:** `specs/input.md` describes what is built today; `dynamics.md`'s remaining `§6` references are
the clock and the bus only; every relative link in the touched files resolves.

### Phase 2 — wear a hand model — 🔶 BUILT 2026-09-17, UNWORN

*Settles into `specs/figures.md` as a §8c sibling to `figure-clip`, its attributes into §2; the
joint-reading half into `specs/input.md`; limits into `backlogs/figures.md`. Top-level `README.md`
status line when it lands.*

- **Import** records `attributes.hand_joints` (`{webxrJointName: nodeName}`) and a **measured**
  `hand_side` — from which side of the wrist frame the thumb sits on, never from the `_L` in the
  filename. Gated by a `validate`-shaped check: all 25 present, and each bone along its own −Z **down
  the fingers only** — at the wrist that dot product is 0.706–0.975 because five bones leave one frame,
  so a flat threshold there would refuse every hand we have. **The parent→child chain requirement is
  dropped**: §1 measures these files as 25 siblings under `hand_L`, and the chain lives in the skin's
  joint list, not the node tree. Costs a `FRAME_REV` bump; `_DERIVED_MODEL_ATTRS` and the tripwire test
  come with it.
- **Discovery is deliberately not built.** These files arrived pre-labelled; a hand model named any
  other way gets no map and is refused with a reason. Conventions and inference are
  `backlogs/figures.md` material until a second naming scheme actually shows up.
- **State** is `components.hand-rig = {hand: "left"}` — ordinary durable world state, semantic, the same
  as a pose.
- **Runtime** composes each bone's world matrix from the joint pose: `compose(xrPosition, xrQuaternion,
  s)`, where `s` is the median segment-length ratio (tracked ÷ bind) **over the 14 real bones, not all
  24** — see phase 0. Median because it is pose-invariant — a fist and a flat hand give the same answer
  where a whole-skeleton fit would not.
  `s` exists because joint positions carry **length and never girth**: skinning transports bound
  vertices rigidly, so without it a large hand gets longer fingers of exactly the authored thickness.
  Phase 0 says whether `s` varies per user; the code is the same either way.
- **Precedence: live hand > clip > pose.** Release calls `figure.restore()`, exactly as stopping a clip
  does.
- **Exemptions:** out of `_placeContent`'s anchor re-solve and out of `grab`'s picking while worn; the
  entity's own transform is meaningless while worn; unwearing restores the anchor.
- **Surface:** `POST /figure/hand`, `wear_hand` / `take_off` for the director, `conjure-ctl wear
  <entity> --hand left|right|auto` (auto pairs on measured `hand_side`).
- **Verification:** extraction in `tests/test_figures.py`; the bone writing against a fake skeleton in
  `tests/js/`; the XR read itself needs the headset and says so.

**Built** (`conjure/hands.py`, `client/hand-rig.js`, `POST /figure/hand`, `wear_hand`,
`conjure-ctl wear`, `specs/figures.md` §8f). `FRAME_REV` 19→20, so **the library needs a refresh with
the server restarted** before any hand carries a joint map.

**To try it:**

```
# RESTART THE SERVER FIRST — a refresh derives what the server PROCESS knows, not what is on disk
python3 -m conjure.ctl refresh-models

python3 -m conjure.ctl wear            # what is there to wear, in the library and in the world
python3 -m conjure.ctl wear --pair     # place both hands and wear them — the whole test, one command
python3 -m conjure.ctl wear <entity> --hand off      # put one down
```

**Done when:** `b8f676…` worn on the left hand follows every finger; it survives a reload because the
state is durable; taking it off returns it to where it was placed; the occlusion conflict is logged when
`--occlusion hands` is also on; and `?hands=fit` shows the translucent mesh sitting on the real hand,
which is phase 0's fit question finally asked of the flesh and not only the joints.

**Worn on a Quest 3, 2026-09-17 — it works, and it found two things.**

| seen | what it was | fixed by |
|---|---|---|
| one hand grey and untextured | the catalog's five hand files are **not one pair**: two textured, one orphaned AR left, two rights with no materials at all. `--pair` took the first of each side. | record `hand_materials`; match a pair on material NAMES, which the orphaned AR left fails and texture-counting would not |
| real fingertips protrude ~**5 mm** past the virtual ones | not placement — the tip joint lands exactly where the runtime says. The fingertip FLESH: the cap is bound to the tip bone and `s` scales it by the GIRTH ratio, which says nothing about how far a fingertip sticks out | a per-finger scale on the five **tip** bones, from that finger's own tracked ÷ bind ratio for `distal → tip`. A tip bone is a leaf, so it cannot propagate |

The second one is phase 0 paying for itself: `*-distal → *-tip` was already measured as the one segment
where the model and the runtime measure different things, and as the widest-varying column of the
reading. Without that, 5 mm would have been fixed with 5 mm.

**What to watch for, given what phase 0 measured.** The per-bone residual after a single `s` is ~5%, and
it shows up as stretch in the skinning blend **at the knuckles** rather than as a hand of the wrong size
— so a hand that looks right overall and odd at the joints is that, not a scale bug. The **tip** and the
**wrist** are the two places to expect disagreement: the tip is derived from the runtime's own estimate
and the wrist is a frame convention, and neither is driven by `s`.

### Phase 3 — hands get the vocabulary controllers already have

*Settles into `specs/input.md`; the module-facing consequences are one line each in
`specs/dynamics.md`.*

- `ConjurePointers` publishes the **full joint set plus radii**, read once per frame and cached like
  everything else it reads. This is the seam that lets `occlusion.js` stop reading joints itself
  later — **not touched in this plan.**
- Synthesised hand controls beside the xr-standard gamepad ones: `pinch` (thumb-tip ↔ index-tip, with
  hysteresis), `grasp` (curl), `poke`. Hands have no buttons, so this is the only way an action ever
  resolves for them.
- Bindings gain hand entries, so `select` and `grab` mean something with hands up — and because modules
  name actions, **`grab` and the beams start working with no module change.** That is the payoff the
  pointers layer was built for.
- The `controllers()` filter is then the open question of the phase: widen the existing callers, or add
  a hands-and-controllers reader beside it. Decide in the phase, against what `grab` needs from a ray.

**Done when:** an object can be grabbed and moved with tracked hands, `water` behaves exactly as before,
and no module changed to get either.

### Phase 4 — contact, for content that reacts

*Settles into `specs/input.md` (the query) and `specs/dynamics.md` (what a module does with it).*

- A joint **capsule chain** per hand — 24 capsules with the radii the runtime already hands us — and a
  query a module asks ("which joints are inside this volume, moving how fast"), not a raycast it runs.
- Touch **events** follow the existing tier-B rule: broadcast the cause, let each client sim from it.
  This is also why v1 needs nothing new on the wire for peers — they never see my fingers and still see
  the ripple.
- `water` migrates from its own fingertip proximity to the shared query **with no behaviour change**,
  which is the proof the query is the right shape.

**Done when:** a dynamic module reacts to a poke without knowing what a joint is, and `water` is one
call shorter than it was.

---

## 4. Open, and not yet worth deciding

- **What peers see.** v1 is local-only: only the wearer drives the hand, and for everyone else the
  entity sits where it was worn. The cheap next step is the wrist pose on the presence stream (7 floats,
  fingers at rest); the full 25-joint stream is a sync tier nothing in `dynamics.md` has and "sync
  causes, never effects" says not to build.
- **Per-joint scale.** If phase 0 finds the radii and the segment lengths disagreeing about hand size,
  that is a real proportion mismatch and worth logging rather than averaging. Per-joint scale is the
  fix; it is not worth building before the numbers exist.
- **Occlusion and worn hands defeat each other.** `--occlusion hands` carves a passthrough hole exactly
  where the worn model is, so your real hand shows through it. Documented and logged, not fixed — the
  joint-source unification in phase 3 is what would eventually let `occlusion.js` and a worn hand agree
  about the same skeleton.
- **Taking a hand off needs a command,** because hands have no buttons. A gesture is possible once
  phase 3 exists; v1 is director or CLI.
- **No haptics on a tracked hand.** Nothing to do; worth writing down before someone designs against it.
- **A figure's own hands are still not puppeteerable.** Humanoid maps carry no finger bones — fingers
  are not recoverable from topology ([`figures.md` §3](../specs/figures.md)) — so this plan does not
  touch the standing "hand poses do not exist" item in `backlogs/figures.md`.
- **Jitter.** Copying joint positions transmits tracking noise into the mesh. If it shows on device,
  filter **presentation only** and never the poses contact reads, which would trade latency for
  smoothness in the one place that cannot afford it. Measure before writing a filter.
