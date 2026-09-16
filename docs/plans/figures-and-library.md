# Plan — figures, their animations, and environments

**Status:** phase 1 DONE and settled into `specs/library.md` §2a/§5a · phase 2 DONE (closed 2026-09-15) · phase 3 DONE (untested on device) · phase 2b steps 0–5 DONE and imported ·
the capture side settled and the corpus recomposed + re-imported 2026-09-15 (see NEXT below) · phase 5 BUILT (untested on device) · phase 4 open
**Opened:** 2026-09-12 · **Phase 1 landed:** 2026-09-13 · **Phase 3 landed:** 2026-09-13 · **Phase 2 landed partial:** 2026-09-13

**This file is temporary.** A plan spans areas that the specs and backlogs deliberately keep apart, so
it exists to hold one sequence across them while it is being executed. Each phase names where its
content goes when it lands, and the file is deleted when the last phase has settled — finished work to
[`specs/`](../specs/), abandoned or deferred work to [`backlogs/`](../backlogs/), forks already taken
to [`decisions.md`](../decisions.md). If it outlives its phases it has become a backlog by another
name and should be dissolved on the spot.

**Dissolves to:** `specs/library.md` + `backlogs/library.md` (phases 1, 4) ·
`specs/figures.md` + `backlogs/figures.md` (phases 2, 3, 5) ·
**`specs/captures.md` + `backlogs/captures.md` (phase 2b)** — a new area opened 2026-09-13, because the
conversion model this plan needs is not about figures and had been living in `specs/figures.md` §9a ·
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

### Phase 2 — clothing on and off ✅ DONE

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

**Closed 2026-09-15.** The classifier, the vocabulary file, `figure-parts`, `/figure/parts` and the
`dress_figure` tool all work, and four figures were stripped and re-dressed on device. It was held open
because headset testing found four defects the mechanism did not explain. Re-examined against the
composed corpus, **three of the four were not defects in it** and the fourth was one word:

- **The separate-container half — dissolved by phase 2b, not built.** "Off means not placing it at all"
  was a statement about a pipeline where a container was the unit. A thing merges its containers, so
  Jane's `hair.glb` and `underwear.glb` are meshes inside one file and turning them off is the same
  `parts_hidden` flag as everything else. The second mechanism this phase named no longer has anything
  to be a second mechanism *for*.
- **Oktoberfest is neither file — fixed by composition.** She is one thing with her beer, confirmed on
  device. What remained was that `Beer` had no category, which is below.
- **Alice keeps a headband, and her scalp does not render — neither is ours.** She has no headband mesh
  at all; if one is visible it is geometry inside `CC_Base_Body`, which `dress` cannot and should not
  touch. And `aula_Scalp_Female` is a render asset **no scene entity draws**, so the site does not draw
  it either — the same shape as the cornea case below it in `specs/captures.md` § 3, and faithful
  rather than broken.
- **Clothing categories over-trigger — not the classifier.** Barbie classifies cleanly: `hair` is
  `hair`, apart from `skirt`/`top`/`underwear` as `clothing`. Removing her clothes took her hair because
  the DIRECTOR reached for `only_body`. Steering and a `hidden_by_category` report went in; whether the
  steering is *enough* is a director-eval question and not a code change, and it is recorded in
  `backlogs/figures.md` rather than holding a phase open.

**The one real gap: a thing the figure HOLDS is not a thing she wears.** `Beer` was the only mesh in
twenty captures the vocabulary had no word for. `accessory` means worn — filing a pint there means
"take off your beer" — and `clothing` is plainly wrong, so `held` is a category (vocabulary revision 2,
removable, ordered after `shoes` and before `accessory`). The tempting rule was structural: the beer is
the only unskinned mesh in the corpus parented to a HAND bone, so it rides her arm through a clip. But
bone-parenting alone is wrong three times in four — bride's two heels hang off `DEF-foot` and teacher's
glasses off the head — and the bone that distinguishes them needs the humanoid map, which the names
already answer correctly without. So the structure is why the category exists, not how it is decided.

After it, one mesh on a captured FIGURE is unclassified: one literally named `New Entity`. It stays
that way. The dev-library models are a separate dialect and a separate backlog — `specs/figures.md`
§5a and `backlogs/figures.md`.

**Done when** (all met): a figure with a classified wardrobe can be stripped to the body and restored,
the stripped state survives a reload, and a model whose clothing is a separate container reports that
rather than silently doing nothing — now by merging it, which is better than reporting it.

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

*Settles into [`specs/captures.md`](../specs/captures.md) — the whole thing model, merge and split —
with `specs/figures.md` §8a picking up whatever the `enabled: false` parts mean for a wardrobe, and
`specs/library.md` for what an imported thing is. The known problems it starts from are already in
[`backlogs/captures.md`](../backlogs/captures.md).*

**We are converting the wrong unit, and every symptom below is downstream of that.** A container is a
FILE — one artist's export. A *thing* is a scene entity subtree. We walk containers, so we emit meshes
the scene never renders, drop meshes it does, and lose everything that lives on the entity rather than
in the file: position, parent, instancing.

**What is FORMAT and what is CONVENTION — because the difference decides how much of this survives the
next site.** Two layers, and only one of them is a law:

*Format, and therefore a rule.* A PlayCanvas render component names `(container, renderIndex,
materialAssets)`; an entity carries `enabled`, a parent and a transform; glTF nodes carry hierarchy and
instancing. That a mesh no entity binds is not rendered is a consequence, not a guess. All of §Three
states rests here, and it holds for any PlayCanvas build.

*Convention, and therefore an assumption to be stated and checked.* **Which entity is a "thing"** is
not in the format. "A direct child of Root" holds in the content scenes and is **false in the app's own
scene**: `2049393.json` has 1,016 entities whose Root children are `ToolModeStore`, `TRASH`,
`DemoVRHoles`, `SampleStore` — machinery groupings nested several levels deep, not things. So thing
detection must PROPOSE and REPORT rather than decide silently, and a capture must be able to correct it
— the same discipline `parts/parts.json` already uses (vocabulary is data, not code) and the same one
`adopt_unbound` uses when it says INFERRED, look at it. `conjure/capture_set.py` is where per-capture
knowledge already lives.

That split is the honest answer to "is this a rational model or a pile of special cases": the reading
rules are the app's data model, and the one place we are pattern-matching on *this* site is named above
rather than buried.

**Verified across the captures that exposed it:**

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
| `Alice` | — | `aula_Aliceglb` only — no composition at all, and still wrong (below) |

**Three states, read off the graph rather than guessed:**

- an entity renders it and is **enabled** → LIVE. Emit it.
- an entity renders it and is **`enabled: false`** → OPTIONAL. Emit it hidden. *This is the site's own
  wardrobe switch* — `underwear` is exactly this in all three figures — and it is what phase 2's
  classifier has been reconstructing from mesh names. The source states it outright.
- **no entity renders it** → DEAD. Drop it. Every mystery in this file is one of these: office-babe's
  body twin, Oktoberfest's and bride's denser twins, bride's black `clothes_sexyunderwear_*`, the
  Japanese house's grey deck, and Alice's white scalp.

**Alice is the case with NO composition in it**, which is why she is in the table with a dash. One
container, one figure, one dead mesh. Her hair is `Side_Swept2` (mesh 13) and her real scalp is that
mesh's THIRD primitive, wearing `aula_Scalp_Transparency_Transparency2` — textured, alpha-masked,
cutoff 0.505. Mesh 14 `Scalp_Female` is in no scene entity at all; only the container's own template
binds it, to one of the FOUR scalp materials the registry holds and one of the two carrying **no maps
whatsoever**. No texture and no `baseColorFactor` is glTF's default: opaque WHITE.

She matters because nothing else would have caught her. The mesh is BOUND, so `adopt_unbound` never
looks at it; the symptom is white rather than black, so it resembles none of the others; and the only
thing that identifies it is **template-only binding while a scene exists** — diagnostic 1, now five
for five. Reported from the headset as *"the white part of her scalp is not visible in the web app"*,
and placed only when Daniel noticed that removing her hair removes the white patch too (the classifier
files `Scalp_Female` under `hair`, correctly).

**`enabled: false` means two different things and the level decides which.** On a PIECE inside a thing
it means optional — off now, switchable. On a THING itself (a direct child of Root) it means *in the
catalog, not placed in this scene*, and that is not a reason to skip it — it is the entire props
library. Every one of the 15 tools is a disabled Root child. Reading the flag uniformly would import
none of them.

**The merge is cheap here, and the plan must not ASSUME it.** The skeletons are identical BY NAME across
containers — 181/181 for office-babe, 175/175 Oktoberfest, 222/222 bride, with zero bones on either
side that the other lacks. So joining two containers is a name-keyed skeleton join, the same operation
`figure-clip` already does to bind a clip — but that is a MEASUREMENT of this corpus, not a property of
the format, and the merge must verify it and refuse rather than take it on faith. A donor whose bones
are spelled differently needs retargeting (phase 5), and silently welding it would produce a figure that
is wrong in a way nothing reports. What the merge must preserve is stated by the cases: a node's
PARENT (the beer hangs off `DEF-hand.R` in its own GLB already), its TRANSFORM (`WOODout.glb` is
meaningless at the origin — the scene puts it at `[0, -10, 0]` scale 100), and its INSTANCING (bride's
heels are one mesh on two nodes, `DEF-foot.L` and `DEF-foot.R`).

**What this says about `adopt_unbound` (§28).** It is a patch, and the right model makes it
unnecessary: office-babe's body copy is DEAD, and a subtree conversion never emits it, so there is
nothing to adopt a material for. It was the right fix for the bug in hand and it should not be
load-bearing here.

Two diagnostics, **both built 2026-09-14** and settled into
[`specs/captures.md`](../specs/captures.md) § 3. They came first because they turn a mystery into a line
of output, and because everything after them is verifiable only if they exist:

1. ✅ **A mesh bound only by a TEMPLATE while the build has scenes** — `Binding.source` + `dead_meshes`.
   Scoped to containers the scene DOES use, which took it from 725 rows of noise to **47** of signal: a
   container no scene mentions is not full of dead meshes, it is one this scene does not use. Finds the
   Japanese deck and Alice's white scalp. Says nothing where no scene was read, since the comparison it
   rests on does not exist.
2. ✅ **A DANGLING asset id** — `dangling`. Only what a binding depends on, which took 1,011 down to
   **42**; the rest are props materials nothing binds. Finds `194421251`, the deck texture that made the
   railing grey and could never have been re-downloaded.

**Between them and the existing "bound by no entity" note, all five known cases are now reported**, in
two flavours: template-only (the deck, Alice's scalp) and bound-by-nobody (the three body twins). A
sixth turned up while testing — `aula_Aliceglb` mesh 5 `CC_Base_Eye` is template-only too, which is
worth a look next to her long-standing eye trouble.

3. ✅ **Walk the things** — BUILT 2026-09-14, settled into [`specs/captures.md`](../specs/captures.md)
   § 3. `things(build)` → `Thing` / `Piece`, each piece carrying the entity that draws it, the parent it
   hangs off, the path from the thing's root and the transform composed down it. Verified against all
   five cases: office-babe reads 3 containers / 9 live / 1 optional, the beer sits on `DEF-hand.R`,
   bride's two heels stay two pieces of one mesh, the deck composes to `y = -0.1`, and every dead twin
   is simply absent with no rule needed for it. `thing_notes` reports the convention and `roots=`
   overrides it; `playcanvas_rebuild.py --things` is the view.

   A test caught the bug this plan predicted: composing the THING's own `enabled` into its pieces marks
   every prop optional and imports none of them.

   **Two corrections that came out of asking how a twin gets chosen.** The scene-over-template
   precedence is now an explicit switch (`read_build(prefer=…)`, default `"scene"`) that outranks the
   best-dressed tie-break, which it did not before — and the "bonus finding" above about
   `CC_Base_Eye` was a FALSE POSITIVE of my own diagnostic: a scene draws it, and it only looked dead
   because the template's richer materials won. `dead_meshes` now asks whether a scene claims the mesh
   rather than which claim won its materials. 47 distinct dead meshes → 45.

0. ✅ **Name the real things** — BUILT 2026-09-14. `captures/things.json`: nine site-wide exclusions
   plus one capture override (susan). 509 candidates → 369.

4. ✅ **COMPOSE** — BUILT 2026-09-14, settled into [`specs/captures.md`](../specs/captures.md) § 3
   *Composing*. `conjure/compose.py`: `compose_thing` writes one GLB per thing and `verify_thing` judges
   it against the `Thing` and the source containers. **263 things over the twenty captures, 516 MB, and
   the verifier reports two problems — both on bride, both facts about the source rather than the
   compose.** `scripts/glb_check.mjs` is the second gate: all 263 load in the client's own three.js
   `GLTFLoader`, and the six acceptance cases measure the sizes they should.

   The verifier went first, as agreed, and earned it three times over. It caught a **100× bride**
   (the composer copied the container's root transform AND the entity that reproduces it), then
   **175 false alarms on Oktoberfest** (a world-space rest-pose comparison cannot survive the scene
   scaling a figure 1.25), then **2 real ones on Alice** — her eye bones are 90° out in the scene and
   nowhere else, which is very likely her long-standing eye trouble.

   **Three things the plan had wrong, all found by building it.** The skeleton merge was named the
   hard part and it does not exist: the scene expands every bone into an entity (181/181, 222/222,
   175/175, 85/85), so composing from the SCENE rather than merging containers means there is one
   skeleton because there is one scene. Weldability is not IBM equality — bride's two containers
   differ on all 222 because one is in centimetres, and they still share a rig. And `Piece.position`
   / `scale` is not enough to place anything: 538 of 948 pieces have a rotation in their chain, so
   `Thing.tree` and `Piece.chain` carry the subtree and the composer rebuilds it.

   **The viewer pass ran 2026-09-14 and found four things, which is what it was for.** Daniel opened
   the six in a web glb viewer: Banana, office-babe, Oktoberfest (beer in hand) and the Japanese room
   were right first time. The rest:

   - **bride's irises washed out** — `Reflections-eyes` is a refractive lens with no maps, and a flat
     48% white is not an approximation of one. Made invisible; four more materials in the corpus are
     the same shape and all four are eyes. (Her veil is real and does lighten her — that one is the
     site, not us.)
   - **Alice's pale locks** — `Side_Swept` and `Side_Swept2` draw one mesh at one transform with two
     material sets, a colour switch the site flips with a script the capture lacks. Same mesh in the
     same PLACE is a variant; in two places it is an instance. Deciding which wins needed
     `how_dressed` sharpened so a base colour outranks a slot count, which also fixed the chrome-hand
     bug in ebony that `dressed()` was written for one capture earlier.
   - **Alice's eyes lost their blood vessels** — and the tell was that `temp/rebuilt/` looked BETTER
     than the composed thing. Her scene claim binds `Eye_R, Cornea_R, Eye_R, Cornea_R` where the
     container's template binds four distinct materials, and reaches for one of three same-named
     `Cornea_R` assets that has no maps. A rule (`filled_in`) was added to let the template win there.
     **Reverted 2026-09-15: it was wrong.** Checked against the running site, Alice has no blood
     vessels in either eye, and nothing in the whole build binds the textured corneas — they are unused
     Character Creator leftovers. The sparse claim IS what the app draws, and `prefer="scene"` has no
     exception. `specs/captures.md` § 3 keeps the full account. The genuine half of the fix stands:
     `things()` was bypassing the binding resolution entirely.
   - **the file looks wrong in a viewer even when it is right** — `hidden` lives in `extras`, so a
     viewer draws the whole wardrobe at once. `--as-shown` writes a copy without it. Two of the four
     findings above took an extra round trip because of this, which is the cost of not having it.

   Also confirmed from the same pass: BOTTLEBEER, CAN and the other white props are a CAPTURE gap, not
   a conversion one. 51 files of the shared props build were never downloaded by any of the 15 captures
   that contain it, because browsing only fetches what the page draws and nobody took those tools out
   of the drawer. Bride's `underwear.glb` is likewise three fetchable URLs, and cannot be borrowed from
   another capture: eight captures ship that name and all eight are byte-different, one per rig.

5. ✅ **SWITCH THE IMPORT OVER** — BUILT 2026-09-14, settled into
   [`specs/captures.md`](../specs/captures.md) § 4. `import_capture.py` reads `temp/things/<name>` by
   default and identifies a composed file by its PROVENANCE rather than its name, because it is named
   after the thing and there is no container stem to match — keying `shipped_with` on the file name
   silently lost every authored set. Rehearsed against a real snapshot of the catalog (`Connection.backup`,
   not `cp`): **98 live models where the old unit gave 90 containers, 43 rows retired, 20 figures each
   holding its own clips, and zero relations left on a tombstone.**

   Two bugs found by rehearsing rather than by reasoning:

   - **the capture name was in the composed bytes**, which defeated content addressing. The props build
     is the same release in 15 captures, so `Banana` should be one row tagged fifteen times; it was
     **fifteen rows with fifteen ids** and fifteen copies of the geometry. 311 live models against 98.
   - **teacher would have lost her figure entirely.** `SchoolCorridor` is 394 entities holding both the
     corridor and the teacher, so the switch retired `Teacher_v1` into a room and she stopped being
     placeable — clips, rig and all. Same shape as susan, found weeks apart and both by hand, so
     `thing_notes` now REPORTS it: a thing drawing skinned and rigid pieces together is very likely a
     figure welded to its environment. One case in the corpus, and it fires on teacher with the
     override removed. `captures/things.json` revision 2 splits her (`Teacher`, `school-corridor`).

   The half-models go, as agreed 2026-09-13: `manager_fixing`, `Oktoberfest-milf-fixing`,
   `model_britney_bride`, eight `underwear`s and `WOODout` are retired into the things that now hold
   them. That is the director's "there's another bride — `model_britney_bride`" answer fixed at the
   source. Also new: `parts_hidden` comes from the scene instead of from mesh names.

   Still open here: the labels for eight captures are the app's internal entity names
   (`MainModelToiletAgnes`, `MainModelHotelBlack`), which `things.json` can rename the way susan's
   `ModelParent` became `Alice` — worth doing before anyone has to read a catalog listing.

**Open, from device testing 2026-09-15:**

- **Bride's left lower eyelid does not show while she animates** — and the obvious fix made her much
  worse. Full record, including what was tried and why it failed, in
  [`investigations/bride-eyelid.md`](../investigations/bride-eyelid.md). Do not re-propose per-mesh IBM
  correction for a welded donor without reading it.
- **A placed composed figure drew its whole wardrobe at once** — two heads of hair and a spare pair of
  underwear on Alice. The import recorded `parts_hidden` and NOTHING read it; placement now sets the
  `figure-parts` component from it. Fixed.
- **Alice reads as taller and larger than expected.** Measured: her file is 1.73 m, within
  `HUMAN_HEIGHT_M`, so she is placed at native scale and is the SHORTEST of the three figures with a
  device sighting (Teacher 1.81, Jane 1.82). The per-container Alice is the same 1.73 m, so nothing
  changed at the switchover. Unresolved — needs a comparison against something of known size in the
  room rather than another measurement of the file.

### NEXT, agreed 2026-09-15 — steps 1 and 2 DONE, 3 remains

1. ~~**Recompose the whole corpus WITH the Basis decode, and re-import once.**~~ **Done.** 20 captures
   recomposed (258 things) and re-imported. What it actually found is worth keeping, because most of
   the stated premise was wrong:

   - **The decode premise was stale.** Every composed thing came out BYTE-IDENTICAL to the previous
     run — the decode had already been applied by whatever ran last, so the corpus was not
     undertextured. Confirmed rather than assumed: with the decoded PNGs stashed away, `Sausage` composes
     with 0 of 1 materials mapped and with them 1 of 1, so the pre-pass does carry the textures and had
     simply already run. Every figure now reads MAPPED in `glb_check.mjs`; nancy, the one the bug was
     found on, is 4 of 6 with the remaining two authored as flat colour.
   - **Compose into a CLEAN directory.** The composer refuses to overwrite (a thing name repeats across
     scenes, and a silent loss is worse), so composing over a previous run writes every thing a second
     time as `Name-2.glb` beside its stale twin, and the importer would then see both. `rm -rf` the
     output first.
   - **moon-girl's re-download had dropped a whole BUILD** — the moon-base release, 62 assets, and with
     it `MOONLANDBASElastVER` and `MOONLANDBASEventsmove`, two things that silently stopped composing.
     `capture_audit --against` could not see it, because builds are matched by asset-id set and a build
     that is simply absent has no fingerprint to compare; fixed (`af80d48`), and it now says `BUILD GONE`.
     Restored by copying that one directory across, which beats either copy: 340 of 718 present against
     the backup's 328, since the re-download had also GAINED 12 files elsewhere. Those two things are
     the only ones in the corpus whose bytes changed.
   - **Stewardess had no rig signature at all** and was only working on stale `shipped_with` edges from
     an import predating composed things — a clean import would not have recreated them. Her thing is
     yawed 90°, and the side rule could only tell +z from -z (`b8b6d4a`). She now reads `85e41f9b8e`.

   Catalog after: 98 models, 524 animations, 279 audio, 20 sets, **`voiced_by` 252 → 468** (+216, exactly
   the stated edges), `part_of` 1594 → 1600, `shipped_with` 707, no relation pointing at a dead asset,
   and **21 of 21 figures carry a rig signature** where it was 20.

   `FRAME_REV` went 14 → 15 for the side rule, so catalogued maps re-derive on first placement. Run
   `conjure-ctl refresh-models` to do the whole library at once instead.

2. ~~**Nothing consumes `attributes.speed`.**~~ **Done** (`bf2733f`). The source plays every clip through
   `assignAnimation(name, resource, "Base", animSpeed, true)` with the rate straight out of the position
   config, so the rate belongs to the clip; `FigureClipRequest.speed` is `None` rather than `1.0` by
   default, through `ctl` and `play_clip` as well, because a well-meant 1.0 would override the author.
   The voice is deliberately not rate-shifted: the source plays it on its own slot with `loop`/`overlap`
   and never seeks or re-rates it.

3. **Push.** Both repos — Conjure, and `browser-extension-glb-download` (0.19.0 popup/job ownership,
   0.20.0 declared audio, 0.20.1 the 401 retry).

**Not next, and recorded elsewhere:** blink and the layered idles (`positions.Layer` holds the data,
the runtime is unbuilt — `specs/captures.md` § 3 *Positions*), bride's eyelid
([`investigations/bride-eyelid.md`](../investigations/bride-eyelid.md)), Alice reading large,
phase 4 rooms-as-environments, phase 5 retargeting, and running the captured app as a reference
renderer ([`backlogs/captures.md`](../backlogs/captures.md)).

**What the capture side learned, so it is not re-litigated:** a capture is only as complete as what the
browsing session actually LOADED, because the server answers 401 to a re-fetch of anything the session
is not entitled to (576 assets in one report — other characters' content in a shared build). A declared
`variants` file satisfies an asset, so a `.basis` with no `.png` is not a gap. And a re-download
REPLACES, so audit before discarding the previous copy: `scripts/capture_audit.py --against`, which
reports both the files a matched build lost and a build that has gone entirely.

**What remains is 4's inverse. The agreed approach for 4, 2026-09-14, was:**

**Plan A — compose alongside, do not replace.** `--things` writes one GLB per thing into its own
directory; `temp/rebuilt/` and its 416 per-container files stay exactly as they are, and the importer
keeps pointing at them. Composed files are just FILES: Daniel reads them in a web glb viewer by
dropping them in, so comparing old against new needs no import, no catalog and no headset. The
comparison is not a state to live in — it exists so the single import at the end happens once instead
of being discovered piecemeal. When the verifier is silent and the six cases look right, this becomes
the only output.

*(Note: `--out` is already required and has no default, so no path is hard-coded there. `import_capture`
DOES default `--rebuilt` to `temp/rebuilt/<name>`, at `scripts/import_capture.py:286` — the one baked-in
path, and the place to change when the switch happens.)*

**Write the VERIFIER first.** A composed GLB is directly comparable to the `Thing` that described it:
mesh count equals live + optional, every optional piece is present and flagged hidden, no
`(container, mesh)` outside the piece list appears, a bone-parented node still has that parent, an
instanced mesh still has two nodes, transforms match the composed values. That is a script, not a
person — and it is what makes the loop fast, because it catches most of a regression in seconds and
defines the contract before any geometry is written.

**The six cases are the acceptance test**, each chosen because it exercises a different requirement:

| case | proves |
|---|---|
| `office-babe` | pieces merge across 3 containers; the dead twin stays out |
| `Oktoberfest-milf` | a piece keeps the BONE it hangs off (`DEF-hand.R`) |
| `bride_ready` | one mesh INSTANCED twice survives as two |
| `JAPANESEROOM BAKED` | a piece keeps its TRANSFORM (`y = -0.1`) |
| `Banana` | the SCALE chain (0.5 × 0.9166 = 0.458), and one file → many things |
| `Alice` | thing detection was WRONG here, not merely noisy; the white `Scalp_Female` must be absent and her real scalp (a primitive of hair mesh 13) present |

**The loop:** compose one capture → verify by machine → open the six in a viewer → fix → repeat.
Steps 2 and 4 are where nearly all the iterations happen and neither needs a person. Then compose
everything, spot-check, import ONCE — where step 2's supersession makes it reversible.

**The hard part, and it is real.** A figure's pieces come from several containers and must end up
skinned to ONE skeleton. Measured: the donor skeletons are identical BY NAME (181/181 office-babe,
175/175 Oktoberfest, 222/222 bride), so the join is name-keyed — the same operation `figure-clip`
already does to bind a clip. It must VERIFY that and refuse rather than weld silently; a donor spelled
differently needs retargeting, which is phase 5.


**Splitting is the other half of the job, and it is not the same code.** Composition merges several
containers into one file; the props need the inverse — one mesh, its materials and its node chain
extracted into a standalone asset. The props scene shows exactly what that has to carry:

```
'Banana'      enabled=False  scale [0.5, 0.5, 0.5]          <- the catalog wrapper
  'BANANA'    enabled=True   scale [0.917, 0.939, 1.289]    <- RENDER: TOOLS LIBRARYblend5.glb mesh 2, material BANANA
'Gothic'      enabled=False  scale [0.5, 0.5, 0.5]
  'gothic_steel_dildo'       scale [0.920, 1.048, 0.934]    <- RENDER: gothic_steel_dildo.glb mesh 0
```

So: the LABEL is the thing's entity name (`Banana`), not the container's and not a mesh index. The
TRANSFORM is the product of the chain — two levels of scale here, and a banana that skips the wrapper's
0.5 comes out twice life size. And the props scene mixes sources: most draw from the shared
`TOOLS LIBRARYblend5.glb`, `Gothic` has a container to itself, so "split the props file" is the wrong
framing — walk the things, and where a thing happens to be alone in its file the split is a no-op.

**Prerequisite, and it is a blocker rather than a footnote.** An asset id is `sha256(bytes)`, so a
merged file is a NEW id and this is a re-import that supersedes, not an edit. Two things must be solved
first or the library grows instead of shrinking:

- ✅ **Superseded rows are left behind** — BUILT 2026-09-14. `library.supersede` leaves a tombstone
  reachable only by id; `import_capture.same_thing` decides identity as `(kind, label)` within one
  capture, so a re-import cannot retire another capture's props.
- ✅ **Relations are keyed to the old model id** — same change. They MOVE to the new row rather than
  being inherited or copied, so a figure's 21 `shipped_with` edges follow it and `dir --with` does not
  answer twice.

`rig_sig` should survive the merge, which is what keeps the clips working: it fingerprints humanoid bone
NAMES and the donors' skeletons are identical by name. Verify it, do not assume it.

The half-models go: `manager_fixing`, `Oktoberfest-milf-fixing`, `model_britney_bride` are bare bodies
and `WOODout` is a deck, none of them useful alone (confirmed with Daniel 2026-09-13).

**Until then they are selectable, and the director picks them.** Asked to "switch to a different
bride" it answered *"There's another bride in the library — model_britney_bride"* and placed the
body-only half of the bride already standing there: one mesh, no clothes, no hair. Nothing in the
catalog distinguishes a figure from a piece of one, so this is not a director error — it is the
missing fact, surfacing as a plausible wrong answer rather than as a rendering artefact. `underwear.glb`
is shared by several figures, so merging copies it into each — more bytes, and it turns the underwear
from a separate entity nothing can control into a hideable PART of the figure, which is what phase 2
wants anyway.

**Done when:** the four composition cases each import as ONE asset matching what the site renders — her
beer in her hand, the deck textured and in place, no black underwear under the dress; the props scene
imports as **15 separately placeable tools with their own names**, not one cutlery drawer; dead meshes
are reported rather than carried; and `enabled: false` pieces arrive as hidden parts instead of being
re-derived from their names.

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

### Phase 5 — retargeting across rigs ✅ BUILT (untested on device)

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

**What it is worth, now that every figure's signature is actually in a row.** 30 mapped figures, 524
clips, eleven rig signatures:

| rig | figures | clips |
|---|---|---|
| `85e41f9b8e` | 16 | 455 |
| `c6e3c61972` (Grace, Yuffie, Trish) | 3 | 0 |
| `8fa3fa4c17` (Animated Woman ×2) | 2 | 0 |
| `790c5edf3b` (Bianca, Blondie) | 2 | 16 |
| `156ac3838f` (Alice) | 1 | 8 |
| `8ef7eb494a` (office-babe) | 1 | 21 |
| `c773b69506` · `fe4965ce0f` · `9b9a660f1d` · `7a839313d9` · `cf605bb3ac` | 1 each | 0 |

**Today 20 figures can be offered a clip and 10 can be offered none at all** — every dev-library model
except Alice's family, because no clip in the catalog shares their signature and tier 1 cannot cross
one. Tier 2 is what takes all 30 to all 524. That is the phase's value stated in rows rather than in
principle, and it was invisible until `rig_sig` became a derived attribute: those ten figures read as
having no signature rather than as having no clips, which are different problems.

### What the measurement says, 2026-09-15 — and it is not what this phase assumed

`scripts/retarget_probe.py` plays one of Jane's clips onto every rigged figure in the catalog and
measures two things. Written before any correction, because a correction is only worth building if the
number is large, and the same number is the only thing that can say whether one worked.

**Two costs, and the first is not retargeting's to fix.** The clip drives 222 bones; the humanoid names
22. The other **200 channels — skirt, breast and secondary chains — have no bone on any other rig to
receive them**, whatever the method. That is a ceiling on tier 2 and it should be stated to a caller
rather than discovered on device.

**Bone ROTATION is the wrong thing to measure.** The first version compared each bone's world
orientation, and by that measure Alice is 103° from Jane — which sounds fatal and is not, because most
of it is ROLL about the bone's own length, which does not move the limb. What judges a retarget is
where each limb POINTS, taken in the figure's own body frame so that proportions and heading do not
count as error. Both numbers are below; only the second one means anything.

| target | rig | binds by name | rest gap | naive dir | corrected dir |
|---|---|---|---|---|---|
| Jane (rest rolled 90°) — **control** | — | 222 | 90.0° | 47.9° | **0.0°** |
| Jane | `85e41f9b8e` | 222 | 0.0° | 0.0° | 0.0° |
| Akari | `85e41f9b8e` | 171 | 15.6° | **8.8°** | 9.5° |
| office-babe | `8ef7eb494a` | 173 | 14.9° | 7.5° | **6.0°** |
| Blondie | `790c5edf3b` | 0 | 90.2° | **9.7°** | 18.6° |
| Alice | `156ac3838f` | 0 | 103.3° | **6.3°** | 16.5° |
| Grace | `c6e3c61972` | 1 | 17.3° | 18.6° | **11.2°** |
| Trish | `c6e3c61972` | 0 | 101.3° | **39.1°** | 53.1° |
| Saka | `9b9a660f1d` | 0 | 138.2° | 88.3° | **15.2°** |
| Eve Maccaro | `fe4965ce0f` | 160 | 28.1° | 38.5° | 38.6° |
| Steve | `c773b69506` | 0 | 41.3° | **22.7°** | 39.5° |
| Animated Woman | `cf605bb3ac` | 0 | 120.5° | **9.4°** | 22.9° |
| Tamaki | — | — | — | refused, no map | refused |

**The correction is CORRECT and it is not the answer.** The control settles the first half: roll every
bone's rest by 90° while leaving the geometry identical, and the naive copy is wrong by 47.9° while the
correction recovers **0.0°**. It does exactly what it claims. But across real rigs it wins five times
and loses six, and on Alice and Blondie it is nearly three times worse than doing nothing.

**Why, and this is the fork phase 5 actually turns on.** Two rigs can differ in two unrelated ways:

  · **bone AXIS convention** — the same rest pose, different local frames. This is what the control
    isolates, it is what Saka has (VRM spells its axes its own way), and the rest correction is exactly
    right for it: 88.3° → 15.2°.
  · **rest POSE** — one rig rests in an A-pose and another nearer a T-pose. Preserving each bone's
    delta-from-rest then faithfully reproduces *the wrong pose*, because the thing being preserved is
    measured from two different starting points. Alice is this case, and the correction makes her
    worse.

A single closed form cannot serve both, which is why the plan's one-line statement of the method —
"map both skeletons through the canonical humanoid, rewrite each channel's target, then correct for the
difference in bind pose" — is not implementable as written. **The next question is not how to correct
but how to tell the two differences apart**, and the probe already has the material: the rest gap and
the naive direction error disagree exactly where the difference is pose rather than axis.

### The law that works, 2026-09-15 — carry the pose ABSOLUTELY

The delta correction fails because *delta from rest* is the wrong invariant. If Jane rests arms-down
and Alice rests arms-out, and a clip puts Jane's arms straight down, preserving the delta sends Alice's
arms half way. What has to survive the crossing is where the limb actually IS.

So: divide out each rig's axis convention instead of its rest pose.

    C   a convention-free rest frame per bone, built from where its limb POINTS
    K   = C⁻¹ · R           what is left of the authored rest once the physical part is removed
    Wt  = Ws · Ks⁻¹ · Kt    the source's orientation, respelled in the target's convention

When the two rests agree, `Cs = Ct` and this reduces exactly to the delta correction — which is why
that one was right for Saka and for the control, and only for them.

| target | rig | naive | delta | **absolute** | abs worst |
|---|---|---|---|---|---|
| Jane, rest rolled 90° — **control** | — | 56.1° | 0.0° | **0.0°** | 0.0° |
| Jane — identity | `85e41f9b8e` | 0.0° | 0.0° | **0.0°** | 0.0° |
| office-babe | `8ef7eb494a` | 7.3° | 6.0° | **0.3°** | 0.4° |
| Grace | `c6e3c61972` | 14.4° | 9.6° | **0.2°** | 1.2° |
| Akari | `85e41f9b8e` | 9.2° | 9.7° | **0.5°** | 0.5° |
| Eve Maccaro | `fe4965ce0f` | 37.1° | 23.8° | **0.8°** | 58.5° ⚠ |
| Animated Woman | `cf605bb3ac` | 9.3° | 22.9° | **1.4°** | 1.5° |
| Saka | `9b9a660f1d` | 89.5° | 15.2° | **3.2°** | 4.1° |
| Trish | `c6e3c61972` | 38.2° | 51.8° | **3.9°** | 10.3° |
| Alice | `156ac3838f` | **5.5°** | 15.9° | 8.4° | 9.1° |
| Blondie | `790c5edf3b` | **6.5°** | 14.4° | 8.4° | 9.2° |
| Steve | `c773b69506` | 19.5° | 16.1° | **8.7°** | 9.0° |
| Tamaki | — | refused, no map | | | |

**Nine of eleven, and the medians collapse from tens of degrees to under four on eight rigs.** Saka,
the case the delta law was built for, improves again: 15.2° → 3.2°. Alice and Blondie still prefer the
naive copy by about two degrees, which is the remaining question rather than a settled result.

**Two traps, both found by a number that should have been zero and was not.**

*The reference axis must be chosen by the BONE, never by measurement.* Squaring a bone's frame needs a
second axis, and picking it with `abs(dot(along, up)) < 0.99` is the obvious way. Jane's `hips → spine`
reads 0.9684 and office-babe's reads 0.9975 — two rigs in the same rest pose, either side of the cut,
taking different branches, ending a half-turn apart. It showed as **179.8° on both shoulders**: a flip,
not a drift. A table keyed on the bone name gives every rig the same answer.

*Where a joint is ATTACHED is build, not pose.* `chest → shoulder` and `hips → upperLeg` are shoulder
width and leg splay. At rest with no clip at all, Trish's `chest → leftShoulder` is already 152° from
Jane's and every rig's `hips → upperLeg` is 8–67° out. No retarget can change that and none should try;
counting it made four rigs look broken while their articulated limbs were within a few degrees.

**Both open questions closed, 2026-09-15, and the second one reverses the verdict.**

*Alice and Blondie did not prefer the naive copy.* The limb metric could not see the error that matters
most. `limb_dirs` works in each figure's own body frame so that proportions and heading do not count —
correct, and it means **a figure turned BODILY the wrong way scores perfectly on limbs**, because every
limb is right relative to a body facing the wrong direction. Measured: the naive copy leaves Alice's
and Blondie's whole bodies **79°** from Jane's, and Animated Woman's **84°**, while scoring 5.5–9.3° on
limbs. The absolute law holds every body within 11°. So the body frame is now reported beside the limbs
and a retarget is only as good as the worse of the two:

| target | naive limbs | naive BODY | absolute limbs | absolute BODY |
|---|---|---|---|---|
| Akari | 9.2° | 5.4° | **0.5°** | **0.5°** |
| office-babe | 7.3° | 3.0° | **0.3°** | 3.1° |
| Alice | 5.5° | **79.2°** | 8.4° | **4.6°** |
| Blondie | 6.5° | **78.8°** | 8.4° | **5.0°** |
| Animated Woman | 9.3° | **83.9°** | 1.4° | **4.1°** |
| Grace | 14.4° | 3.3° | **0.2°** | 6.5° |
| Trish | 38.2° | 46.3° | **3.9°** | **10.9°** |
| Saka | 89.5° | 11.1° | **3.2°** | 8.6° |
| Steve | 19.5° | 6.2° | **8.7°** | **1.5°** |
| Eve Maccaro | 37.1° | 37.3° | **0.6°** | **3.4°** |

**The absolute law wins on every rig once the body is counted.** There is no remaining case for the
naive copy.

*Eve's outlier is a MAP defect, not a retargeting one — and it is not only hers.* `validate()` asks
where bones ARE: in the right places, sides not swapped, limbs ordered. It never asks whether a bone is
actually **under** its humanoid parent. Eve's inferred map puts `hips` on `ORG-spine` (beneath
`MCH-spine`) and `spine` on `chest` (beneath `torso`) — different branches of a rigify control rig — so
rotating her hips cannot move her spine. Her torso stays behind while her pelvis turns.

It costs nothing when posing one bone at a time, which is why it has gone unnoticed, and it breaks
retargeting, where a chain's motion has to compose. **6 of 28 mapped figures have at least one break,
and every one of them is an outlier in the table above:**

| figure | map source | broken links |
|---|---|---|
| Eve Maccaro | `inferred` | `hips→spine`, `chest→leftShoulder`, `chest→rightShoulder` |
| Steve, Characters Shaun | `convention:dot-side` | `hips→leftUpperLeg`, `hips→rightUpperLeg` |
| Trish, Yuffie | `convention:rigify-fk` | `spine→chest` |

`chain_breaks()` reports it and the probe prints it per figure. Whether it belongs in `validate()` is a
separate decision: it would reject six maps that pose perfectly well today, so the honest first step is
to report it rather than to fail on it.

**Do not ship the correction inside a rig signature.** Naive copy on Akari is 8.8° and that is the path
that already runs on device and looks right, so 8.8° is roughly what "correct" costs in this metric.
The correction there is 9.5° — no better, and it would rewrite 16 working figures.

### Shipped, 2026-09-15 — `conjure/retarget.py` and `/figure/clip`

**On the server, producing an ordinary clip.** The alternative was to send both skeletons to the client
and do the algebra there, and it is worse in every way that matters: the output is a function of two
files and nothing else, so it content-addresses and is computed once per (clip, rig) pair *ever*; the
client keeps its one bind-by-name path; and the arithmetic stays beside the tests that pin it. What
comes back is a clip spelled in the TARGET's node names. **The client was not changed at all.**

**Both inputs describe themselves.** A clip GLB carries no mesh and no skin, but it does carry its
authoring rig's NODES — and their rest transforms match the figure they came from to within **0.075°**,
measured. So the source rig's rest pose and its humanoid map are recoverable from the clip alone and
nothing has to be looked up.

**The proof it works, in four lines.** Jane's `10_action`, loaded with the client's own `GLTFLoader`
(`scripts/clip_check.mjs`, new):

    original clip -> Jane      666 tracks, 666 BIND
    original clip -> Trish     666 tracks,   0 BIND     ← every cross-rig figure, today
    original clip -> Alice     666 tracks,   0 BIND
    retargeted    -> Trish      21 tracks,  21 BIND
    retargeted    -> Alice      22 tracks,  22 BIND

Verified end to end against the corpus: **Jane → Jane is 0.0° on both limbs and body**, which is the
only cheap test this has and which caught two bugs that had looked like facts about rigs — posing the
two sides with different bone SETS, and measuring a bone against its parent's REST rather than where
the parent had actually moved to.

**Three lists, not two.** `retargetable` is a third tier beside `shipped` and `compatible`, deliberately
not merged into the second: a rewritten clip and a native one are not the same thing, because the
rewrite drops every channel the humanoid does not name — 200 of 222 on a captured clip. `list_clips`
says so, and `/figure/clip` returns `retargeted: true` with the notes.

**What is still refused, and now says why.** A figure with no recoverable bone map: there is nothing to
map channels through. That also closed a silent one found on the way — a rigged figure with no map and
a clip that HAS a signature used to bind by name, resolve nothing, and report success while playing a
statue.

**Known limits, all measured and all reported to the caller:**

- 200 of 222 channels have no receiver on any other rig. A skirt or a ponytail stays still.
- Six of 28 maps are not CHAINS (`Rig.chain_breaks`), so part of the body lags. Named in the notes.
- Worst-case limb error over the corpus: Akari 0.6°, Grace 0.2°, Saka 4.2°, Steve 9.1°, Trish 10.4°,
  Alice and Blondie 14.6°, Eve 61.8° — Eve being the broken-chain case rather than a retargeting one.

### Open after device testing, 2026-09-16

Six defects came out of one clip played on one figure, every one real and every one invisible to the
Python model of the client that found none of them. They are in the git log; what follows is what is
still wrong. `scripts/clip_diff.mjs` measures all of it in one run — it plays two (clip, figure) pairs
through `figure-clip.js`'s own `retarget()` and a real `AnimationMixer`, seeked the way `tick()` seeks.

**The retarget aligns to the CLIP's rest, and the figure it was authored for no longer has that rest.**
This is the tilt, reported as *"Akari is tilted back compared to Grace and Alice"*. Measured on Alice's
`LayTableIdle`, at `t=0`, where all three should lie flat together:

| | tilt from vertical | rest spine lean |
|---|---|---|
| Alice, playing it NATIVELY | 94.1° | 4.5° |
| Grace, retargeted | 97.3° (+3.2°) | 6.7° |
| Akari, retargeted | 105.8° (+11.7°) | 0.1° |
| **the clip file's own rig** | — | **10.7°** |

The clip carries its authoring rig and `swing` aligns to it, which is right in principle and is not what
Alice shows: her COMPOSED figure and the clip's rig recover the identical bone map — `CC_Base_Hip`,
`CC_Base_Waist`, … — and still differ by 6° of spine lean, because composing a thing bakes the scene
entity's transform into it and a clip has never been through that. So "match the clip" and "match the
figure it shipped with" are two different targets and this one picks the first.

Fixing it needs a decision rather than a patch. Either the retarget looks up a figure with the clip's
`rig_sig` and aligns to THAT — which reintroduces the lookup this module was built to avoid, and picks
one of sixteen figures arbitrarily — or the composer stops baking a rotation a clip cannot know about.
The second is probably right and reaches further than phase 5.

**The residual body swing.** Grace oscillates 7.0° and Akari 4.2° where Alice oscillates 1.8°, same
period. It halves between a 21-bone `rigify-fk` map and a clean 22-bone `rigify-def` one, which points
at map quality rather than at the law — worth testing against `office-babe`, the last clean captured
rig, before theorising.

~~**The hands.**~~ **Not a defect — the metric was.** `hips → rightHand` spans the whole torso AND the
whole arm, so its direction is set by PROPORTION rather than by pose: Saka's arm-to-torso ratio is 1.24
against Alice's 0.96 — long arms on a short torso — and the two differ by 31° AT REST with nothing
playing at all. Reported as retargeting error it made Saka's hands read 43–52° wrong when they looked
fine on device, which they were. `clip_diff.mjs` now measures SINGLE SEGMENTS only, and the worst limb
across every rig drops to 6–12°, all of it `neck → head`.

This is the same attachment-versus-articulation distinction the probe already makes, applied a step too
late: `retarget_probe.py` excludes `chest → shoulder` and `hips → upperLeg` for exactly this reason, and
the new harness was written with its own limb list and did not inherit the lesson. **Seventh time in
this campaign that a metric, not the code, was what was wrong.**

**Fingers do not move, and that is the vocabulary rather than a defect.** The humanoid names 22 bones
and fingers are not among them, so every finger channel is in the 82 dropped. Alice keeps hers only
because she plays natively. Extending the map to fingers is real work in `figures.py` — VRM and most
humanoid standards do define them — and would pay off across posing as well.

### THE ORDER OF WORK, agreed 2026-09-16 — do these, then delete this file

Device testing is finished; Daniel is done looking at figures for now. Four things, in this order, and
the last one ends the plan.

**1. ALIGNMENT — make Akari lie on her back like the others.** ✅ **DONE, untested on device.** The
answer was experiment 2 and it was the whole of it: `swing` was the static tilt. Written up below under
*Alignment* — the term is gone, `RETARGET_REV` is 5, and the composed-rest problem turned out **not** to
need fixing for this bar, which is worth knowing before anyone opens it.

**2. FINGERS.** ✅ **DONE, seen on device 2026-09-16** — *"fingers are working for the most part"*.
22 bones → 52, dropped 82 → 52, body bit-identical. Fingertip swing against Alice's native 3.8°: Akari
5.4°, office-babe 4.0°, Saka 8.8°. Grace reads 0.0° and it is her file, not the map — written up below.
Chasing Saka through it turned up two further defects, both fixed: `refresh-models` respelled the
figures and left every clip behind (a signature is a comparison, so one side is worse than neither), and
a STATED VRM map was nobody's job, so her row held a 54-bone map beside a 21-bone fingerprint.

**2a. FACES** — raised here, measured, and NOT started. See *Faces* below. Driving one is small and
worth doing; retargeting one has no evidence to build on and should wait for a reason.

**2b. DESCRIBING AND EMBEDDING FIGURES** ✅ **DONE 2026-09-16** — all three layers, 38 figures,
no failures. Written up in
[`backlogs/library.md`](../backlogs/library.md) § *Describing and embedding models*. Not part of phase 5
and not blocked by it. The state: 94 assets carry an embedding and **not one is a model**; every
figure's `notes` and `tags` are empty, so her whole searchable self is a label, and three figures are
called `Animated Woman`. Three layers — free structured text, a rendered thumbnail in the image vector
space, and a multimodal description into `notes` — kept apart because only the first is free and the
third may be refused on this corpus. The renderer (`glb_preview.py`), the captioner and the embedder all
already exist.

**3. WHATEVER ELSE PHASE 5 NEEDS** ✅ **DONE 2026-09-16.** The residual body swing was chased and is
**not a defect** — it is the `limbs` approximation the probe has always reported, seen in the time
domain; the account is under *Alignment* below, along with what to measure if anyone reopens it. One
real defect fell out of the chase (the output interpolated `STEP` where the source is `LINEAR`,
`RETARGET_REV` 7). The `Done when` bar: a clip plays on Susan, Trish and Saka; the drift is measured at
4.9–7.2° limbs against a 5.3° channel-loss floor; and Tamaki is refused with the reason named.

**4. DISSOLVE THIS FILE.** It has outlived its phases, which is the condition its own header names for
dissolving it. Where each part goes:

| content | destination |
|---|---|
| the retargeting law, `RETARGET_REV`, the three clip lists, what a rewrite cannot carry | `specs/figures.md` |
| the composed-rest problem, and alignment as a capture concern | `backlogs/captures.md` § Known problems |
| the residual swing, the map-chain breaks, the parts-vocabulary dialect gap | `backlogs/figures.md` |
| faces — driving them (small) and retargeting them (not yet), with the corpus measurement | `backlogs/figures.md` |
| phase 4, rooms as environments — untouched and still wanted | `backlogs/library.md` + `specs/worlds-surfaces.md` |
| the measurement harnesses and why a model of the client is not the client | `specs/figures.md`, beside the law |

**What must not be lost in the move**, because each cost a device round trip: a rotation track REPLACES
a node's rest, so a wrong rest is invisible until you retarget; a metric that works in a figure's own
body frame cannot see the figure turned bodily; `hips → hand` measures PROPORTION, not pose; and
anything derived and cached carries the revision of the code that derived it.

### How to finish phase 5 — agreed 2026-09-16

**Measure first, in this order, and stop when a run explains it.** Every hypothesis this campaign has
reasoned its way to has been wrong, and every one a measurement found has been right;
`scripts/clip_diff.mjs` answers each of these in one run because it plays both sides through the
client's own `retarget()` and mixer.

1. ~~Is `swing` the residual oscillation?~~ **No — measured, ruled out.** Disabled, Akari reads 4.1°
   against 4.2° with it. Do not spend more on `swing` for the swing.
2. ~~Is `swing` the static TILT?~~ **Yes, all of it — measured, and the term is gone.** See *Alignment*.
3. ~~If not, the `K`s are next.~~ Not needed; the `K`s are fine.

**(4) The premise this module was built on does not hold for every capture — and it was NOT the tilt.**
Recorded before *Alignment* below was written, and left standing because the composed-rest problem is
real and still belongs in a backlog. What changed is its priority: it is not blocking anything now. A clip GLB carries its
authoring rig's nodes, and on Jane those match her figure's rest to **0.075°** — measured, and then
generalised, which was the mistake. On Alice, her composed thing and her clip's rig recover the
IDENTICAL bone map and their rests differ on **all 22 bones, several by more than 100°**
(`rightUpperArm` 117°, `leftLowerArm` 113°).

Why it is survivable at all: **a rotation track REPLACES a node's rotation**, so for a bone the clip
drives the rest is irrelevant to playback — which is why Alice looks right natively. The rest matters
only for bones the clip does NOT drive, and for the `K`/`swing` arithmetic, which is built from nothing
else. So a wrong rest is invisible until you retarget.

The cause is in the COMPOSER, not here: a thing's node tree is built from the scene entity tree, keeping
each entity's rotation and scale, so a composed figure's rest is *the scene's pose of the skeleton*
rather than the container's bind. `rebound` already exists to adopt the container's bone transform and
did not fire for Alice (`rebound: []`). **Home: `backlogs/captures.md` § Known problems**, with a
pointer from `backlogs/figures.md` because the symptom shows up in retargeting and nobody chasing a
tilted figure will look in the capture backlog. Anything proposed there has to explain why it is not
the per-mesh IBM rewrite that was tried and rejected on device
([`investigations/bride-eyelid.md`](../investigations/bride-eyelid.md)).

### Alignment, 2026-09-16 — `swing` was the tilt, and the metric that graded it was its own

*"Akari is tilted back compared to Grace and Alice."* Measured, fixed, and the term is deleted rather
than tuned. `RETARGET_REV` 4 → 5, so every cached retarget rebuilds on the next play.

**What it was.** The law carried a whole-body term, `swing = Bt · Bs⁻¹`, aligning the source's rest body
frame to the target's — the reasoning being that a figure whose armature rests leaning should perform in
HER frame rather than inherit the source's. Wrong twice:

- **glTF fixes the world frame at Y-up**, so there is no world-frame CONVENTION between two GLBs to
  divide out. A difference between two rest body frames is a difference in rest POSE — and not carrying
  rest pose is the entire law. Measured across every rig signature in the catalog: all eleven are Y-up
  and Z-forward, their body frames 0.1°–9.6° off world, and every one of those is a lean about the side
  axis. There is no rig here whose frame differs by a right angle, which is what a convention would be.
- **The lean is a lean of the spine BONES**, so an absolute carry already reproduces it. `swing` added
  it a second time. That is the arithmetic of the tilt exactly: the clip file's rig leans 10.7°, Akari
  0.1°, and Akari read 11.7° further back than Alice.

**Why nobody caught it.** `hips → neck` is the chord of a curved spine, not an axis — Akari 0.1°,
office-babe 2.9°, Alice 4.5°, Grace 6.7°, Saka 4.8°, Steve 7.3° — so a body frame built from it reads
anatomy as orientation. Building it from the legs instead is no better (0.4°–7.9°). **The rest geometry
cannot tell you a convention to better than about 10°, because a body is not square.**

And the metric agreed with the term because it *was* the term. The probe grades a retarget on `body`,
the angle between the two POSED body frames, and `swing` is by construction what nulls that. With it on,
four figures score BELOW the Jane → Jane channel-loss floor of 4.0°, which is the tell — nothing can beat
the floor. Read as excess over the floor, the total across the corpus **halves**, 42.0° → 21.9°, and the
spread collapses from −3.4…+10.7 to −0.5…+4.0. `limbs` is unchanged, because it already worked in each
figure's own body frame. **Eighth time in this campaign that a metric, not the code, was what was
wrong** — and the first time one was graded by the thing it was measuring.

**End to end**, `LayTableIdle`, angle of `hips → neck` from vertical at `t=0`, through the client's own
`retarget()` and a real mixer:

| | with `swing` | without | Alice plays it NATIVELY at 94.1° |
|---|---|---|---|
| Grace | 97.3° | **93.5°** | −0.6 |
| Akari | 105.8° | **95.6°** | +1.5 |
| office-babe | 100.2° | **92.7°** | −1.4 |
| Saka | 99.5° | **93.8°** | −0.3 |
| **spread** | **11.7°** | **2.9°** | |

**The composed-rest problem (4) is NOT what this was**, which is the thing to carry into the backlog. It
is real — Alice's composed thing and her clip's rig differ on all 22 bones — and it cost nothing here,
because a rotation track replaces a node's rotation and the absolute law never consults the rest except
through `K`, which is convention and not pose.

**Harnesses.** `scripts/clip_stage.py` resolves names against the library and writes clip, figure and
retarget as files; `scripts/clip_tilt.mjs` reports the ABSOLUTE body axis, which is what `clip_diff.mjs`
structurally cannot see — it reports every angle relative to each figure's own `t=0`, so a constant lean
reads clean. `scripts/retarget_probe.py --swing` restores the term, and is the only place it still
exists. Pinned by `test_a_rig_that_RESTS_LEANING_does_not_lean_the_target_a_second_time`, which fails by
5.8° if the term comes back, with a companion control asserting the two fixtures really do rest
differently.

**The residual body swing, chased 2026-09-16 and I think it is not a defect.** Recorded here rather
than in a backlog because the next person will otherwise chase it again.

What it looks like: Grace's range across the clip is 86.1°–93.7° and Saka's 85.7°–94.1°, against Alice's
93.4°–94.1°. `swing` was ruled out as the cause, and removing it confirmed that — the wobble is the same
size with the term and without.

`clip_diff` against Alice playing natively shows the shape of it, and the shape is the finding:

```
t=0.0  A 0.0°  B 0.0°      t=6.6  A 1.2°  B 1.2°
t=0.8  A 1.8°  B 4.1°      t=7.5  A 1.6°  B 4.0°
t=1.7  A 1.0°  B 0.9°      t=8.3  A 0.7°  B 0.7°
t=2.5  A 1.8°  B 3.9°      t=9.1  A 1.6°  B 3.6°
```

**Perfectly alternating** — eight samples agree to a tenth of a degree, eight are off by ~2.3°, and it
never varies. That looked exactly like a sampling artefact, and there was one to find: the output was
written `STEP` where every source sampler is `LINEAR`, so the target held each pose and jumped. **Fixing
it changed nothing** — the alternation survives unchanged (`RETARGET_REV` 7; the fix is right anyway).

So it is pose-dependent, not sample-dependent: at the cycle's ends the two rigs agree exactly, and in the
middle they differ. The limb directions alternate the same way, 2° against 5–6° on `neck → head`. Which
is the ordinary reading: **this is the `limbs` error the probe has reported all along, 4.9–7.2° against a
5.3° floor, seen in the time domain rather than as a median.** `K = C⁻¹·R` is exact only where two rigs'
rest limbs point the same way; where a limb bends away from that, the conversion carries a few degrees.
That is the law's known approximation, not a bug in it.

**If someone wants to reopen it**, the decisive measurement is bone WORLD ORIENTATIONS over time rather
than chords or body frames — both of the metrics used so far mix pose with proportion, and this campaign
has now been caught by that eight times. Do not start from a range metric: STEP and LINEAR visit the
same extremes, so min/max over a clip cannot see a difference that a comparison at an instant makes
obvious.

### Fingers — DONE 2026-09-16, and the naming table in the original write-up was wrong twice

Reported on device: *"neither Grace nor Akari's fingers move but Alice's does"*. Correct, and it was the
vocabulary rather than a defect. The humanoid now names **52 bones**, not 22: `FINGER_BONES`, five
fingers × three joints × two hands, spelled the way VRM 0.x spells them because `vrm_humanoid` already
hands us `leftIndexProximal` from a VRM's own extension block and Saka is in the catalog with all
thirty. `FRAME_REV` 17, `RIG_SIG_REV` 2, `RETARGET_REV` 6.

Measured on `LayTableIdle`: **22 bones carried → 52, dropped 82 → 52**, and the body is bit-identical —
93.5° / 95.6° / 92.7° with the same min/max as before, because the HANDS were deliberately kept as chain
ends. A hand's canonical frame has always come from `lowerArm → hand`; letting a thumb redefine it would
have moved every wrist in the corpus to fix nothing.

**The naming table this plan wrote from memory was wrong in two places**, which is why the tables in
`figures.py` are measured off the files:

| what the plan said | what the files say |
|---|---|
| `cc-base`: `CC_Base_L_Index1/2/3` | right, but the middle finger is `Mid`, not `Middle` — and one base rig spells its whole right side lowercase, `CC_Base_r_Mid1` |
| `rigify`: `DEF-f_index.01.L` | right for four fingers; the THUMB has no `f_` — `DEF-thumb.01.L` |
| `mixamo`: `LeftHandIndex1/2/3` | right, but the little finger is `Pinky`, and one rig carries only a thumb and an index |
| `dot-side`: **no finger bones at all** | **wrong.** Steve has none; `Animated Woman` and `Characters Shaun` are the same scheme with the full set |

**A finger cannot be squared up against a BODY axis** — the one real discovery, and it is measured. A
hand turns freely at the wrist, so no fixed body direction stays perpendicular to a finger: the best
body axis available still reads **0.966** against `Characters Shaun`'s index and **0.996** against
`Bride`'s thumb, either of which is a degenerate frame. Arms and legs get away with a body axis because
a rest pose holds them roughly fixed against the torso; fingers are one joint further out than that
holds for. So `_hand_refs` takes the reference from the hand's own bones instead:

- **across the knuckles**, `indexProximal → littleProximal`, for index/middle/ring/little — worst 0.251
- **the palm normal**, that crossed with the middle finger's direction, for the thumb — worst 0.382,
  median 0.229, where the knuckle axis would have been 0.851 because a thumb points across the palm

**Grace's fingers still will not bend through**, and it is her file rather than the map. Her three FK
finger joints are exported as SIBLINGS under one palm bone — what chained them in Blender was a
constraint, and a GLB carries none — and she has no `DEF-` finger chain to prefer instead. Each joint
still reaches its correct absolute orientation, because the walk solves every node against its own real
parent; what cannot happen is a bend carrying to the joint beyond it. Now reported as its own note,
separately from a BODY chain break, because twenty finger links would otherwise push a broken torso out
of a message that shows three.

**Open:** Saka's fingers are in her stored VRM map and `best_humanoid` does not read it — it takes the
inferred 21-bone body map instead, so she retargets with no fingers at all. Pre-existing and not caused
by this work; `best_humanoid`'s docstring says a stated map is the caller's job. Belongs in
`backlogs/figures.md`.

### Faces — measured 2026-09-16, NOT started, and the two halves are not the same job

Raised while the fingers were landing, and recorded here so it is not lost. **Nothing below is built.**

**What the corpus actually has.** Morph targets are everywhere — 30 of 32 rigged models carry some — but
almost none of them are a face:

| figure | targets | facial | vocabulary |
|---|---|---|---|
| Saka | 57 | **57** | VRM's own preset set — `Fcl_ALL_Joy`, `Fcl_BRW_Angry`, `Fcl_EYE_Close_L` |
| Alice | 36 | **31** | Character Creator / ARKit-ish — `Brow_Raise_Inner_L`, `Eye_Blink_L`, `Eye_L_Look_Up` |
| Bianca, Blondie | 22, 21 | 19 | **tongue only** — `Tongue_Out`, `T01_Tongue_Up`. No brows, no eyes |
| everyone else | 3–17 | 0–1 | skin and clothing colour (`Body_Asian`, `Shirt_Blue`), anatomy (`pussy_open`), and the odd `closed_eyes_correction` |

So the expression rigs number **two**, and they speak different languages.

**The captures do not contain performances of them.** 249 of 539 clips drive morph weights, which looks
promising and is not: the channels target `Body`, `Dress`, `Shorts`, `Hair`, `Shirt` — wardrobe and body
shape. Corpus-wide, **15 channels are facial** (`CC_Base_Tongue` 7, `CC_Base_Body` 8). Whatever made
these characters emote on the source site, it was not in the animation files.

**Half one — DRIVE a face ourselves. Small, and worth doing.** A morph weight is one scalar per target
and three.js applies it through the same mixer that plays a clip, so there is no new client path. It is
`pose_figure`'s shape: a `set_expression` tool taking `{target: weight}` or a named preset, over the
figures that have the targets. `morph_targets` is already a catalog attribute, so "who can smile" is a
query we can answer today. The payoff is not cosmetic — a figure who blinks and looks at you is a
different presence in a headset, and the director has nothing to work with now.

**Half two — RETARGET a face across rigs. Harder than bones, and the reason is structural.** A bone
retarget maps through a shared skeleton with geometry to check it against; `validate()` can say an elbow
is above a shoulder. Morph targets have **names and nothing else**. `Fcl_ALL_Joy` and
`Brow_Raise_Inner_L` are not the same vocabulary and there is no measurement that relates them — the
whole discipline this campaign has run on (measure, never extrapolate) has nothing to measure. VRM's
preset set is the obvious standard to map TO, and that is a table per scheme with no geometric check
behind it, which is exactly the shape of thing `REF_AGAINST_UP` warns about.

Worse, with two expression rigs in the catalog and no facial performances to carry, there is no evidence
to build the tables from and nothing to carry across them. **Do not start this until something needs
it.** If half one ships, the natural source of facial performance is our own director rather than a
capture, and then the question changes from "retarget" to "author", which is a different and easier job.

**Home:** half one to `backlogs/figures.md` as a sized item; half two to the same file as a NOT YET, with
the measurement above so nobody re-derives it.

### The original write-up, kept for the naming tables it got right

Reported on device: *"neither Grace nor Akari's fingers move but Alice's does"*. Correct, and it is the
vocabulary rather than a defect — fingers are not humanoid bones, so all 82 of their channels are
dropped. Alice keeps hers only because she plays natively.

**Feasible.** Every convention in the corpus names them regularly, so this is table work in
`figures.py` rather than inference:

| convention | scheme |
|---|---|
| `cc-base` | `CC_Base_L_Index1` / `2` / `3` |
| `rigify-def` | `DEF-f_index.01.L` / `.02.L` / `.03.L` |
| `vrm` | `J_Bip_L_Index1` / `2` / `3` |
| `mixamo` | `LeftHandIndex1` / `2` / `3` |
| `dot-side` | **no finger bones at all** — Steve has none, so that rig maps none |

Thirty bones: five fingers × three joints × two hands. It pays off beyond retargeting — `pose_figure`
cannot ask for a fist or a point today either.

**The cost is the signature, and it is the reason to decide deliberately.** `rig_signature` is computed
over the MAPPED bones, so adding fingers changes **every signature in the catalog**. That is what
`RIG_SIG_REV` exists for, and the bump has consequences: `shipped_with` survives (it is a relation),
but every compatible-clip lookup is keyed on `rig_sig`, and every cached retarget is stamped with the
signature it was built for. Plan on re-deriving all of it in one pass, as the `FRAME_REV` 15→16
backfill already did.

**Also needs:** a reference axis per finger bone in `REF_AGAINST_UP`'s table — a finger points along the
hand, not up or forward — and `LIMBS` entries so the canonical frame can be built. Neither is hard; both
are the kind of table that must not be guessed from geometry, for the reason `REF_AGAINST_UP` already
documents.

**Done when** (the original bar): *a `85e41f9b8e` clip plays recognisably on Susan, on Trish and on
Saka* — mechanically yes, on device untested; *a measured comparison says how far each drifts* — the
table above and `scripts/retarget_probe.py`; *and a rig that should fail fails cleanly* — Tamaki is
refused with the reason named.

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
