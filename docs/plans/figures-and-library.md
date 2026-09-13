# Plan — figures, their animations, and environments

**Status:** proposed, not started · **Opened:** 2026-09-12

**This file is temporary.** A plan spans areas that the specs and backlogs deliberately keep apart, so
it exists to hold one sequence across them while it is being executed. Each phase names where its
content goes when it lands, and the file is deleted when the last phase has settled — finished work to
[`specs/`](../specs/), abandoned or deferred work to [`backlogs/`](../backlogs/), forks already taken
to [`decisions.md`](../decisions.md). If it outlives its phases it has become a backlog by another
name and should be dissolved on the spot.

**Dissolves to:** `specs/library.md` + `backlogs/library.md` (phases 1, 4) ·
`specs/figures.md` + `backlogs/figures.md` (phases 2, 3, 5) ·
`specs/worlds-surfaces.md` (phase 4) · `specs/dynamics.md` (phase 4, the `grab` mode)

---

## 1. What this is built on

Twenty captures from one PlayCanvas origin, reconstructed by `conjure/playcanvas.py`: 208 GLBs,
617 MB rebuilt from 3.4 GB of source. Every figure carries a 22-bone humanoid map with anatomical axes
and validates clean. The numbers below are measured, not assumed, and the plan leans on them — where
one is wrong the phase that rests on it is wrong too.

**Clips are skeleton-only and bind by name.** An animation arrives as its own GLB with `meshes=0`,
`skins=0`, 228 nodes and exactly one named clip. Against `jane_export.glb`: 222 animated targets, and
**zero** of them absent from the figure. Loading one and binding by node name is all three.js needs —
no retargeting for a clip played on its own figure.

**Audio pairs with clips by string equality.** `3_action.glb` ↔ `3_action.mp3`, **20 of 20** on Jane.
One clip is unpaired (`1_idle_speaking`, a silent variant). Nothing here needs inference.

**Clips belong to the RIG, not to the figure.** Four distinct rig signatures across 72 rigged models,
and one of them — `85e41f9b8e` — covers **sixteen of the twenty captures**. Jane's clips already play
on Akari and Nancy. Filing a clip under the figure that shipped it would invent an ownership the data
does not have and would block the reuse this plan exists to enable.

**Cross-figure reuse within a signature still drops channels.** Jane's clips target 222 bones; Akari
and Nancy have 171 of them. The 51 missing are skirt bones, breast secondaries and anatomy extras —
**zero are face bones**, and **all 37 core body bones are present**. So the body moves correctly and
the absent channels simply stay in bind pose.

**The clothing naming convention is not reliable enough to trust at runtime.** A `clothes_*` prefix
appears in **8 of 20** captures. Elsewhere it is bare words: `Clothes`, `Hair`, `Shoes`, `Dress`,
`Skirt`, `Shorts`, `underwear`. A prefix rule would be wrong on more than half the set.

**There is no audio support anywhere.** Not in `importer.py`, not in the library's code paths, not in
the client. The `kind` column's comment mentions it; nothing implements it.

**`authored` immersion was designed for exactly this and is empty.** From `set_immersion`: *"captured
surfaces hidden, the space still in effect — intended for replacement geometry built to the real
footprint. No tool builds that geometry yet, so today this matches `ar`."* A room model is that
geometry.

**The grounded skybox is the precedent, and it needs BOTH frames.** `frameYaw`/`frameOffset` are a
rigid horizontal transform (`p' = R(yaw)·p + offset`); `skyYaw`/`skyScale` turn and size the panorama
*relative to that*. The two are deliberately one frame — "if a world moves, the sky it sits under moves
with it" — and `applyFrameDelta` is shared by `#world-root` and the sky for exactly that reason.

A room model is the same shape of thing: content with a ground you stand on, which has to be put in
the right place (offset), turned to line its walls up with the real ones (yaw), and sized to fit a
physical room (scale). So it follows the **grounded skybox**, not the plain one and not void content
alone — the grounded dome already demonstrates the full combination.

**And the readout lesson comes with it.** `GROUNDED_M = [3, 300]` bounds the *effective radius*,
"the number that describes what you see", because `height` is not a height in any sense a user means —
it is where the horizon sits. Reporting "height 0.2 m" for a dome whose ceiling was 3.95 m up read as a
broken scale gesture when it was only a broken label (2026-09-01). A room model needs its own bounds
and its own honest readout — a floor span, not an internal parameter.

---

## 2. Forks already taken

Recorded in [`decisions.md`](../decisions.md) §22–§25 rather than here, so they survive this file.

- **§22** Environments are a *facet*, not a class hierarchy — `kind` stays what the bytes are.
- **§23** A clip is keyed to a **rig signature**, not to a figure.
- **§24** Clip playback is an entity **component**, not a dynamic module.
- **§25** Plans live in `docs/plans/` and dissolve; they are not a fifth durable tier.

---

## 3. Phases

Ordering is deliberate: phases 1 and 2 change nothing that renders, so neither can break a scene, and
phase 2 is pure visibility with no timing. **Phase 4 shares no code with 1–3** and can be scheduled
independently.

### Phase 1 — import the new kinds, and link them

*Settles into `specs/library.md` §record + a new §linking; leftovers to `backlogs/library.md`.*

- `AnimationImporter` — a `.glb` with animations and no mesh. Records clip names, duration, target
  count, and the **rig signature** of the skeleton it animates.
- `AudioImporter` — `.mp3` first. Duration, channels, sample rate.
- **Dispatch fork:** `_BY_EXT` is one handler per extension, and `.glb` must now reach either the model
  or the animation handler. Selection has to consult content (`meshes == 0 and animations > 0`), not
  just the extension.
- `rig_sig` and `rig` on every rigged model's `attributes` — the fingerprint over the mapped humanoid
  bone names, versioned alongside `frame_rev` so a discovery change is detectable rather than silent.
- Relations, using the table that already exists and is unused:
  - `clip --voiced_by--> audio`, from the stem match
  - `asset --part_of--> set`, provenance only, never the compatibility test
- A `set` row per capture, asserted at import — the grouping is **not** derivable from the build, which
  is why it is an argument and not a guess.

**Done when:** Jane's 21 clips and 20 audio files import; 20 `voiced_by` relations exist; querying
clips by `rig_sig` returns them for Akari and Nancy without either figure being named.

### Phase 2 — clothing on and off

*Settles into `specs/figures.md` §runtime; the classifier's limits to `backlogs/figures.md`.*

- A `parts` classifier that runs **once at import** and writes `attributes.parts` — reviewable and
  correctable, never re-guessed at runtime. Same reason the normal-map decision reads the registry
  instead of the pixels: a heuristic that fires at import can be inspected, one that fires at render
  time cannot.
- Vocabulary, not prefix, given the 8-of-20 measurement: `clothes|hair|shoes|dress|skirt|shorts|`
  `underwear|item|glasses|jacket|jeans|top|scarf|hat|veil`, against the body's `Body|CC_Base_Body|model_*`.
- A generic **per-node visibility** list on the entity. The classifier proposes the default set; the
  entity holds the truth.
- **Two mechanisms, and the plan must say which applies:** some captures put clothing in separate
  *containers* (Jane's `hair.glb`, `underwear.glb`), where "off" means not placing it at all.

**Done when:** a figure with a classified wardrobe can be stripped to the body and restored, the
stripped state survives a reload, and a model whose clothing is a separate container reports that
rather than silently doing nothing.

### Phase 3 — play a clip

*Settles into `specs/figures.md` §runtime `figure-clip`; the precedence rule into the same section.*

- A `figure-clip` component beside `figure`, carrying the clip asset and play state, riding the
  existing entity/patch/snapshot path. Driven by `three.AnimationMixer`.
- **On the shared clock** that dynamic modules already use, so every headset is on the same frame.
- **Precedence, stated rather than discovered:** a playing clip wins; a pose applies when idle. Without
  a rule the two fight over the same bones.
- **Drop channels the target lacks** — 51 of them Jane→Akari, harmless and necessary.
- Director tool: play / stop / list clips for a figure, and the audio that goes with one.
- Tier-1 cross-figure reuse **arrives here, not in phase 5** — binding is by name, so the moment this
  works Jane's clips play on the other fifteen `85e41f9b8e` figures.

**Done when:** a clip plays on its own figure; the same clip plays on a different figure of the same
rig with the body moving correctly; its audio plays in sync; and a pose reasserts itself on stop.

### Phase 4 — a room model as the environment *(independent of 1–3)*

*Settles into `specs/worlds-surfaces.md`, `specs/dynamics.md` (the `grab` mode), `specs/library.md`
(the facet).*

- `attributes.environment = {projection: "equirect" | "grounded" | "mesh" | "cylinder"}` — the facet.
  `kind` stays `image` or `model`.
- A room model is **singleton** and replaces, like the sky: `set_environment(model_id)`, not
  `place_asset`.
- It **suppresses the scaffold**, on the same switch `presentation.skybox` and `.grounded` already use
  — the room *is* the walls.
- It is the content `authored` immersion was written for, which is why that mode currently falls back
  to `ar`.
- **`grab` follows the GROUNDED SKYBOX**, which is the mode that already solves this: offset and yaw
  to place and square the room against the real one, scale to fit it to the physical space. The reason
  is alignment for walking, not looks.
- **Known obstacle, and it is the central one.** The grab catalog says it plainly: *"Skybox scale and
  void mode need a void/outdoor world; skybox yaw works anywhere."* Inside a real space you get yaw and
  nothing else — and a room model inside a real space is precisely the case that needs offset and scale
  most, because that is what lining a virtual room up with a physical one *is*. The gate
  (`WF.isVoid()`) has to learn about an environment model, and this is the phase's real work rather
  than a footnote to it.
- **Open:** where the user stands in a room, and whether its floor snaps to the space's floor plane.
  `authored`'s "built to the real footprint" argues for snapping; `plane_anchor.py` already has the
  machinery.

**Done when:** a room model can be set as the environment, the scaffold gives way to it, and it can be
dragged and yawed into alignment with the real room from inside the headset.

### Phase 5 — retargeting across rigs

*Settles into `specs/figures.md` if it lands; to `backlogs/figures.md` if it is deferred.*

Only tier 2 remains after phase 3. Map both skeletons through the canonical humanoid, rewrite each
channel's target, then correct for the difference in bind pose between the two rigs — the same
rest-versus-posed composition `compose_frame` already does for aims. Reaches Susan (Reallusion, 78
joints) and the blondie/bianca pair.

**Done when:** a `85e41f9b8e` clip plays recognisably on Susan, and a measured comparison says how far
it drifts from the same clip on its own rig.

---

## 4. Open, and not yet worth deciding

- **Per-model arm clearance.** Akari's 18 cm torso already fails `clears` on `kneel` and `stand`. It
  predates this plan and will bite harder across twenty bodies.
- **A half-mapped figure validates clean.** `REQUIRED_BONES` has no hands or feet, so four unmapped
  core bones drew no complaint on blondie and bianca. Worth a rule; not a blocker.
- **Template-only bindings on mapless materials** — 188 across the captures, Susan's white skull-cap
  among them. Whether a template may dress a mesh the running scene never instantiates is unresolved,
  and the test protecting Jane's hair depends on the current answer.
