# Plan — figures, their animations, and environments

**Status:** phase 1 DONE and settled into `specs/library.md` §2a/§5a · phase 2 PARTIAL (works; open
defects listed below) · phase 3 DONE (untested on device) · phases 2b, 4–5 open
**Opened:** 2026-09-12 · **Phase 1 landed:** 2026-09-13 · **Phase 3 landed:** 2026-09-13 · **Phase 2 landed partial:** 2026-09-13

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

**Audio is many-to-many with clips, and some of it is not about clips at all.** Jane is the clean
case — `3_action.glb` ↔ `3_action.mp3`, 20 of 20 — and taking her for the rule was wrong. Across all
twenty captures only **200 of 898** audio files pair with a clip by stem. The remainder are:

| Shape | Example | Belongs to |
|---|---|---|
| one file, several clips | `1-5-8-9_idle`, `2-3-4-6-10_idle`, `2-10_action` | **many** clips — the name lists them |
| room ambience | `Washitsu soundtrack`, `Moon Base Ambience`, `Ceiling Fan` | the **environment** |
| scene action banks | `HotelAction0..5`, `WCAction0..5`, `AulaIdle0` | a scene, by index, not by clip filename |
| UI and marketing | `Click - compressed`, 24 promo lines identical in every capture | nothing worth importing |

So `voiced_by` is many-to-many, an environment needs its own `ambience` relation, and the importer
needs a skip rule or every capture drags in the same two dozen promo clips.

**A slot-numbered name is a per-figure label, not a motion identity.** `1_idle.glb` exists in 16
captures in **14 distinct versions**, and they are different motions rather than re-exports — duration
10.0 s to 43.4 s, 432 to 676 channels, 145 to 273 nodes. A clip library keyed on that name would
collide catastrophically.

**But most clips are named properly, and the split is per-capture.** Of 206 distinct names, **155 are
semantic** — `pc_leanOnSink_headLeft`, `LayTableIdle`, `KneelAwait`, `TableHangIdle`, `Blink`. By file
it is 322 of 524 slot-numbered, 61%, and it is bimodal rather than mixed: `ebony`, `ebony2`, `nancy`
and `susan` have **no** slot-numbered clips at all, while `jane`, `barbie`, `bride`, `goddess`,
`kawaii`, `manager`, `oktoberfest` and `stewardess` are 21 of 21. So naming is a per-set job that some
sets do not need, not a blanket pass.

**Nearly half the named clips depend on a PROP, which is why rig compatibility is not sufficiency.**
93 of the 206 names call out a fixture: bed 30, sink 18, toilet 12, floor 12, door 6, wall 6, chair 6,
table 3. `pc_leanOnSink_headLeft` will play on any `85e41f9b8e` figure and be *wrong* on one standing
in a field. Whether a clip can drive a skeleton and whether it belongs in a scene are different
questions, and only the first is mechanical.

**Byte duplication is real but already solved.** 124 animation files are byte-identical across more
than one capture, 14% of 387 MB. The catalog is content-addressed (`sha256(data)[:16] + ext`), so
those collapse on import for free and need no plan of their own.

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

### Phase 1 — import the new kinds, and link them ✅ DONE

*Settled into [`specs/library.md` §2a](../specs/library.md) (clips, audio, sets, the three axes) and
§5a (ownership). Delete the rest of this section when phases 2–5 are done and the file goes.*

**Verified end to end**, against four captures in a scratch catalog:

    figure jane_export   rig 85e41f9b8e
    AUTHORED   21 clips — what her own build gave her
    COMPATIBLE 187 more clips carry the same rig, and 72 of those name a fixture
               (sink 24, toilet 16, bed 10, wall 8, door 8, floor 4, chair 2)
    AUDIO      3_idle <- 3_idle (48.31s, 48000Hz)

which is the "17 more are rig-compatible, 6 of them want a bed" the plan asked for, at the corpus's
real scale. One acceptance criterion was wrong and is corrected below.

*What follows is the original scope, kept until the file is deleted.*

- `AnimationImporter` — a `.glb` with animations and no mesh. Records clip names, duration, target
  count, and the **rig signature** of the skeleton it animates.
- `AudioImporter` — `.mp3` first. Duration, channels, sample rate. **With a skip rule**: 24 promo
  lines and a UI click are identical in all twenty captures and are not content.
- **Naming clips — four stages, and the manual part is the last and smallest.** Prototyped, not
  assumed; each stage below was run.

  **0. Skip the sets that do not need it.** `ebony`, `ebony2`, `nancy` and `susan` carry no
  slot-numbered clips at all. A third of the corpus is already named.

  **1. A descriptor from the sampler data alone — no rendering, no model, no naming.** Summing
  successive quaternion angles per bone gives angular travel, and it separates clips objectively:

      3_idle     38.6s    8 deg/s   thigh 4°, spine 3°     — stands still, arms only
      1_idle     43.0s   44 deg/s   thigh 158°             — shifts weight
      6_action   38.3s   41 deg/s   upper_arm 686°, thigh 8°  — arm-driven, legs planted
      1_action   39.6s   66 deg/s   hand 1083°             — busy hands

  Duration, activity in deg/s, and which bones dominate are enough for a director to choose between
  clips **before any of them has a name**. They are also catalog attributes worth having regardless.

  **2. A filmstrip, for the ones a human or an LLM should name.** A clip binds to its figure by node
  name, so merging the two is mechanical — done here for Jane's `1_idle`: **666 channels bound, 0
  dropped**, rendered in Blender, action `1_idle` frames 0–1031. Frames are chosen where the
  *dominant* bone from stage 1 moves most, because evenly-spaced stills of a 43-second idle come out
  nearly identical — which the first strip demonstrated.

  **3. Caption, then confirm.** `conjure/captioner.py` already exists for precisely this — "backfill
  labels for assets that lack one" — and takes image bytes. It needs a clip mode beside its `skybox`
  flag. Its output is a **proposal**: a wrong name is worse than a slot number, because a director
  will act on it.

  The merge in stage 2 is the same name-binding phase 3 needs to play a clip at all, so building it
  here de-risks playback rather than being a detour.

  **What does NOT work: transcribing the audio.** It was the obvious idea — the promo files on this
  origin are literally named after their own transcripts. But the clip audio is non-verbal, and
  Whisper on non-speech invents text: `2_idle` transcribed to nothing, `3_idle` to "Hmm...", and
  `7_action` to `'Nous. AL Yue. AU Yue. AU! AU! AU!'`. Tested and rejected.
- **Dispatch fork:** `_BY_EXT` is one handler per extension, and `.glb` must now reach either the model
  or the animation handler. Selection has to consult content (`meshes == 0 and animations > 0`), not
  just the extension.
- `rig_sig` and `rig` on every rigged model's `attributes` — the fingerprint over the mapped humanoid
  bone names, versioned alongside `frame_rev` so a discovery change is detectable rather than silent.
- Relations, using the table that already exists and is unused:
  - `figure --shipped_with--> clip`, the **authored** set: what the original scene gave this model.
    Distinct from compatibility on purpose — see below.
  - `clip --voiced_by--> audio`, **many-to-many**; the `1-5-8-9_idle` form names its own clips and
    parses, the `HotelAction0` form does not and is left unlinked rather than guessed
  - `environment --ambience--> audio`, for the room soundtracks
  - `asset --part_of--> set`, provenance only, never the compatibility test
  - `environment --pairs_with--> environment`, for a room and the sky it shipped with — four captures
    have both (see phase 4)
- A `set` row per capture, asserted at import — the grouping is **not** derivable from the build, which
  is why it is an argument and not a guess.

- **Ownership by more than one agent.** `scope` is a single column today and an asset id is a content
  address (`sha256(data)[:16] + ext`), so two agents cannot hold the same bytes under one row — the id
  *is* the bytes. Two shapes, and they differ in one visible way:
  - an `asset_scopes(asset_id, scope, public)` join table — one row per asset, many owners, and
    **curation is shared**: one set of notes, tags and rating for everyone.
  - a composite key `(id, scope)` — a row per owner, so **each agent curates its own copy**, at the
    cost of every `WHERE id=?` in the catalog becoming ambiguous.

  Transfer is add-then-remove either way, which is the operation asked for. **Chosen: the join table**
  (decisions.md §26) — the smaller change, with curation shared. If two agents ever need to disagree
  about one asset, that is a later migration and the join table does not block it.
- **Three axes, and they must stay separate.** A clip is selectable on:
  1. **can it drive this skeleton** — `rig_sig` equality, mechanical and total;
  2. **was it authored for this figure** — `shipped_with`, which is the original authors' intent and
     is not recoverable from the bytes once lost;
  3. **does it need something in the room** — a hint parsed from the name, since 93 of 206 name a
     fixture. A hint, never a gate: the name is evidence, not a manifest.

  The measurement says these genuinely differ. Clips shared byte-identically across captures peak at
  **three**, and those three are the same character in three scenarios (`akari`/`arabic`/`geeky`) — so
  cross-*character* reuse is something we would be introducing, not something the authors did.
  **400 of 524 clips appear in exactly one capture.** Action clips are shared twice as often as idle
  ones (25% vs 12%), which is the intuition confirmed: idles are personal, actions travel.

  So the default view is the authored set, and reaching past it is deliberate and visible — "17 more
  clips are rig-compatible, 6 of them want a bed" rather than one undifferentiated list.
- **Shell listing.** The namespace already globs (`namespace.is_glob`/`match`); add filtering by
  `kind` and by relation so `ls .../assets/*idle* --kind animation --for <figure>` is expressible.
  Being able to see what goes with what is how we will check the linking is right at all.

**Done when:** Jane's 21 clips and 20 audio files import; her 20 `voiced_by` relations exist and
moon-girl's `1-4-5-7-10_idle` links to **five** clips (this said Bianca's `2-10_action` and two — she
has no clip 10, so linking one was the rule working, not failing; there are 120 many-to-many links in
the corpus and the largest is five); querying clips by `rig_sig` returns them for Akari and Nancy
without either figure being named; the shell can list a figure's clips and their audio; and the
authored set and the compatible set are separately visible for a figure that has both.

### Phase 2 — clothing on and off ⚠️ PARTIAL

*Settles into `specs/figures.md` §runtime; the classifier's limits to `backlogs/figures.md`.*

- A `parts` classifier that runs **once at import** and writes `attributes.parts` — reviewable and
  correctable, never re-guessed at runtime. Same reason the normal-map decision reads the registry
  instead of the pixels: a heuristic that fires at import can be inspected, one that fires at render
  time cannot.
- Vocabulary, not prefix, given the 8-of-20 measurement: `clothes|hair|shoes|dress|skirt|shorts|`
  `underwear|item|glasses|jacket|jeans|top|scarf|hat|veil`, against the body's `Body|CC_Base_Body|model_*`.
- **The vocabulary is DATA, not code**, on the user-first search path that `config.md` already
  defines for agents and dynamic modules — so a new garment word is an edit, not a release. Every
  import records which vocabulary revision classified it, so adding a word tells you which assets are
  worth reclassifying instead of silently disagreeing with the ones already in the catalog.
- An import that leaves meshes **unclassified reports them**. That list is the vocabulary's backlog and
  the only honest measure of its coverage.
- A generic **per-node visibility** list on the entity. The classifier proposes the default set; the
  entity holds the truth.
- **Two mechanisms, and the plan must say which applies:** some captures put clothing in separate
  *containers* (Jane's `hair.glb`, `underwear.glb`), where "off" means not placing it at all.

**Done when:** a figure with a classified wardrobe can be stripped to the body and restored, the
stripped state survives a reload, and a model whose clothing is a separate container reports that
rather than silently doing nothing.

**Where it actually is (2026-09-13).** The classifier, the vocabulary file, `figure-parts`,
`/figure/parts` and the `dress_figure` tool all work, and four figures were stripped and re-dressed on
device. Held open rather than closed because headset testing found defects the mechanism itself does
not explain, and closing the phase would file them as finished:

- **The separate-container half is not built at all.** Jane's `hair.glb` and `underwear.glb` are still
  place-it-or-do-not, which is the second of the two mechanisms this phase named.
- **Clothing categories over-trigger.** Removing Barbie's clothes took her hair, with `hair` correctly
  classified apart from two `clothing` meshes — the director reached for `only_body`. Steering and a
  `hidden_by_category` report went in; whether that is enough is unmeasured.
- **Alice keeps a headband** when stripped, and her scalp's white part does not render.
- **Oktoberfest is neither file, and this is the sharpest case for the separate-container work.** The
  scene composes her from BOTH: dress, eyebrows, hair and body from `Oktoberfest-milf-fixing.glb`,
  eyelashes and **a beer** from `Oktoberfest-milf.glb`. So `-fixing` is a woman with no eyelashes and
  empty hands, and the plain file is a woman holding a beer with a black dress and black skin. Both are
  in the catalog and neither is her.

  Her two bodies are the same character at two mesh densities — `[50012, 316, 2649, 5737, 3472]` against
  `[23532, 316, 2649, 5737, 3472]`, four primitives matching vertex-for-vertex and only the skin
  differing — so `adopt_unbound` refuses, correctly: it cannot tell a decimated twin from a different
  mesh, and the rule that stops it here is the rule that stops `computer_desk` taking `cool_button`'s
  material.

  **The beer is already bone-parented inside the GLB** — node 160 sits under `DEF-hand.R`, unskinned,
  with its own translation — so a merge across containers would carry it correctly and it would ride her
  arm through a clip. It is the only mesh in the corpus attached to a *character's* skeleton this way
  (Jane's bone-parented meshes are the VR rig's own hands). Composition, not attachment, is the missing
  piece.

Two black-figure defects found the same way turned out NOT to be this phase and are fixed: office-babe's
body was bound by no entity (`adopt_unbound` could not see a mesh with no render asset), and metal had
no environment to reflect. See `decisions.md` §28.

### Phase 3 — play a clip ✅ DONE

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

**Landed 2026-09-13**, settled into `specs/figures.md` §8b. `figure-clip`, `POST /figure/clip`,
`GET /figure/clips`, and `play_clip` / `stop_clip` / `list_clips`. Tier-1 reuse arrived with it as the
plan said it would: Jane's model has 21 clips that shipped with it and 314 that merely fit, and the two
are listed apart. Not yet confirmed on device — the headset pass is what will say whether the bodies
actually move correctly.

### Phase 2b — convert SCENES, not containers *(blocks parts of 2 and 4)*

*Settles into `specs/figures.md` §9a (the conversion model) and §8a; `specs/library.md` for what an
imported thing is.*

**We are converting the wrong unit, and every symptom below is downstream of that.** A container is a
FILE — one artist's export. A *thing* is a scene entity subtree. We walk containers, so we emit meshes
the scene never renders, drop meshes it does, and lose everything that lives on the entity rather than
in the file: position, parent, instancing.

The app's own data model already states all of it, so this is not a heuristic we have to invent.
**Verified across the four captures that exposed it:**

```
a build has several SCENE files, and each Root's direct children are the THINGS
  2049393.json   LeftController, Camera Offset, Gestures, DemoVRHoles …   the VR shell
  2249318.json   Fastfood Blondie (19 meshes), office-babe (10)           the characters
  2041460.json   VASEonly (1), JAPANESEROOM BAKED (35)                    the rooms
  2192829.json   13 × MainModelToilet*VER2, all disabled                  skin-tone variants
  2167323.json   19 props — Stick, Carrot, BOTTLEBEER … all disabled      the props library
```

A thing's subtree spans containers, which is the whole point:

| thing | subtree | containers it draws from |
|---|---|---|
| `office-babe` | 192 entities | `office-babe.glb` 8 · `manager_fixing.glb` 1 · `underwear.glb` 1 |
| `Oktoberfest-milf` | 183 | `-fixing.glb` 4 · `-milf.glb` 2 (eyelashes, **beer**) · `underwear.glb` 1 |
| `bride_ready` | 237 | `bride_ready.glb` 11 · `model_britney_bride.glb` 1 · `underwear.glb` 1 |
| `JAPANESEROOM BAKED` | 39 | `JAPANESEROOM BAKED.glb` 34 · `WOODout.glb` 1 (the deck) |

**Three states, read off the graph rather than guessed:**

- an entity renders it and is **enabled** → LIVE. Emit it.
- an entity renders it and is **`enabled: false`** → OPTIONAL. Emit it hidden. *This is the site's own
  wardrobe switch* — `underwear` is exactly this in all three figures — and it is what phase 2's
  classifier has been reconstructing from mesh names. The source states it outright.
- **no entity renders it** → DEAD. Drop it. Every mystery in this file is one of these: office-babe's
  body twin, Oktoberfest's and bride's denser twins, bride's black `clothes_sexyunderwear_*`, and the
  Japanese house's grey deck.

**The merge is cheap, which was the thing worth checking.** The skeletons are identical BY NAME across
containers — 181/181 for office-babe, 175/175 Oktoberfest, 222/222 bride, with zero bones on either
side that the other lacks. So joining two containers is a name-keyed skeleton join, the same operation
`figure-clip` already does to bind a clip. What the merge must preserve is stated by the cases: a node's
PARENT (the beer hangs off `DEF-hand.R` in its own GLB already), its TRANSFORM (`WOODout.glb` is
meaningless at the origin — the scene puts it at `[0, -10, 0]` scale 100), and its INSTANCING (bride's
heels are one mesh on two nodes, `DEF-foot.L` and `DEF-foot.R`).

**What this says about `adopt_unbound` (§28).** It is a patch, and the right model makes it
unnecessary: office-babe's body copy is DEAD, and a subtree conversion never emits it, so there is
nothing to adopt a material for. It was the right fix for the bug in hand and it should not be
load-bearing here.

Two diagnostics we do not have, both cheap, both worth landing first because they turn a mystery into a
line of output:

1. **A mesh bound only by a TEMPLATE while the build has scenes** is the dead-mesh signal, and all four
   cases carry it. `read_build` reads templates as a fallback — necessary, one capture had sixteen
   templates and no scene at all — and does not distinguish a template-only binding once a scene exists.
2. **A DANGLING asset id.** `WOODout`'s material points at texture `194421251`, which is not in
   `config.json` at all, so `missing_files` walks the registry and reports nothing absent. This is the
   difference between "re-download this capture" and "the site ships it this way", and it is the question
   that cost a session: *"no texture on the railing of the house."*

**Done when:** the four cases each import as ONE asset matching what the site renders — her beer in her
hand, the deck textured and in place, no black underwear under the dress — dead meshes are reported
rather than carried, and `enabled: false` pieces arrive as hidden parts instead of being re-derived from
their names.

**It is wrong in the OTHER direction too, and that case is bigger.** One container can hold many
things: `TOOLS LIBRARYblend5.glb` is a single 7.5 MB file that the props scene splits into **15 separate
Root-level things** — Banana, Carrot, Sausage, BOTTLEBEER, CAN, Toothbrush, ClockRemesh, DILDOX and the
rest — each one entity drawing one mesh from it. `TOOLS%2520UPGRADE2.glb` is another three (Fork,
Lollipop, Vibrator1). Today we import that file as ONE asset, so there is no banana in the library,
only a cutlery drawer. Same error as the figure case, less visible because nothing renders black.

Sharing is normal rather than exceptional: `watchesbuttonsglb` serves 4 things, `VR_hand_R.glb` 3,
`cool_button.glb` 3. **So the container is not the unit in either direction** — a thing can span files
and a file can hold many things — which is the strongest form of the argument for reading scenes.

(`WOODout.glb` needs no standalone representation; it is a piece of the room and nothing else. Settled
2026-09-13 with Daniel — the props library, not the deck, is where one-container-many-things bites.)

**The no-scene case is confirmed, not assumed.** 2 of 58 builds are missing a declared scene file
(`nancy` and `susan` share the build). A PlayCanvas TEMPLATE is a saved entity hierarchy — the same
structure as a scene, stored as an asset — and the templates in that build carry `enabled` on every
entity, children, render components and a single root, exactly like a scene's. So the rule reads them
unchanged, and a template is simply one saved thing.

### Phase 4 — a room model as the environment *(independent of 1–3)*

*Settles into `specs/worlds-surfaces.md`, `specs/dynamics.md` (the `grab` mode), `specs/library.md`
(the facet).*

- `attributes.environment = {projection: "equirect" | "grounded" | "cubemap" | "mesh" | "cylinder"}`
  — the facet. `kind` stays `image` or `model`. **`cubemap` is not speculative**: four captures ship a
  room model *and* a cube-mapped sky (`akari`/JAPANESEROOM, `stewardess`/PrivateJetInterior,
  `moon-girl`/MOONLANDBASE, `susan`/aula), which is a projection the library has never held.
- **A room and its sky are two environments that travel together**, linked by `pairs_with` (phase 1).
  Setting the room offers its sky; neither requires the other, because the pairing is how the scene
  shipped and not a constraint we should inherit.
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
rest-versus-posed composition `compose_frame` already does for aims.

**The dev library is the real target, and it is a harder one than the captures.** Inside the captures
tier 2 reaches three figures across two rigs (Susan; blondie and bianca). The models that came from
elsewhere are **six more distinct signatures, none shared with any capture**:

    c6e3c61972   Grace, Yuffie, Trish      rigify-fk
    cf605bb3ac   Animated Woman            mixamo
    8fa3fa4c17   Animated Woman x2         dot-side
    c773b69506   Steve                     dot-side
    7a839313d9   Characters Shaun          dot-side
    9b9a660f1d   Saka                      vrm, inferred
    fe4965ce0f   Eve Maccaro               inferred
    (Tamaki has no map at all)

That is the point of including them rather than an extension of it: retargeting that works only
between two rigs from one vendor has proved nothing. Trish, Saka and Eve are three vendors, two
discovery layers and a VRM, and they are already in the catalog — so they are the cheapest honest test
of whether the transform generalises.

**Done when:** a `85e41f9b8e` clip plays recognisably on Susan, on Trish and on Saka; a measured
comparison says how far each drifts from the same clip on its own rig; and a rig that should fail
(Tamaki, no map) fails cleanly rather than producing a mangled pose.

---

## 4. Open, and not yet worth deciding

- **Per-model arm clearance.** Akari's 18 cm torso already fails `clears` on `kneel` and `stand`. It
  predates this plan and will bite harder across twenty bodies.
- **A half-mapped figure validates clean.** `REQUIRED_BONES` has no hands or feet, so four unmapped
  core bones drew no complaint on blondie and bianca. Worth a rule; not a blocker.
- **Whether the promo-audio skip rule generalises.** It is a name match against one origin's
  marketing lines. It will not survive a second site, and a skip list keyed on content hash would —
  worth doing only once there is a second site to test it against.
- **Template-only bindings on mapless materials** — 188 across the captures, Susan's white skull-cap
  among them. Whether a template may dress a mesh the running scene never instantiates is unresolved,
  and the test protecting Jane's hair depends on the current answer.
