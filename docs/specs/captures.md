# Captures — the spec

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/captures.md`](../backlogs/captures.md); rejected alternatives
and the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

This is the **pipeline from someone else's 3D web app to a row in our catalog**: a browser extension
that captures a published build, a reconstruction step that turns its split-apart pieces back into
self-contained glTF, and an import that files the result. Three stages, each of which can fail in ways
the next one cannot detect, which is why they are specified together.

It exists as its own area because it is **not about figures**. Figures were merely the first thing we
pulled through it — the reconstruction section lived in [`specs/figures.md`](./figures.md) §9a until
2026-09-13 — and the same pipeline now carries rooms, props and audio. What a figure IS, how it poses
and what its bones mean is [`specs/figures.md`](./figures.md); what the catalog does with the result is
[`specs/library.md`](./library.md).

---

## 1. The three stages

```
 glb-grabber (Chrome MV3)        a live page  -> a capture directory on disk
   ↓                             config.json, scene JSON, containers, textures, audio
 playcanvas.py                   a capture    -> self-contained .glb files
   ↓                             materials rejoined, textures decoded and resized
 import_capture.py               .glb + .mp3  -> catalog rows, with relations
```

**Each stage's failures are invisible to the next.** A texture that never downloaded and a texture the
project never had produce the same flat-shaded output; a mesh no scene renders and a mesh whose material
failed to resolve both come out grey. So every stage REPORTS rather than repairs where it can, and the
reports are the specified surface as much as the files are.

## 2. The grabber

`browser-extension-glb-download` (separate repo). A Chrome MV3 extension that watches network traffic
for the pieces of a published build and writes them into one directory, preserving the URL path so the
registry's relative references still resolve.

**What it has to get right, learned by getting each one wrong:**

- **A JSON-typed XHR has no `responseText`.** Reading it throws, and a silent catch turned every scene
  file into a no-op. Read `response` when `responseType` is set, and never swallow the error.
- **A scene may be fetched from a different URL than the registry names** — a token in the path. An
  origin+path index, bound only to entries that were announced but arrived empty, recovers it.
- **Long filenames.** A 64-character cap truncated names *and dropped their extensions*, leaving a
  549 KB file on disk that matched nothing in the registry. The cap is 150 and preserves the extension.
- **A zero-byte download is a FAILURE**, not a save. `if (expected && bytes && …)` short-circuited to
  "ok" on a zero-length file; so did the test harness, whose fixture defaulted `fileSize: 0` and
  therefore encoded the bug it should have caught.

**Verify against `chrome.downloads`, not against intent.** The extension asks the downloads API what
actually landed and reports saved/failed from that, because every failure above reported success.

**What a capture must contain is specified elsewhere, and deliberately.** A capture is two things — the
assets we import here, and the only copy of someone's APP — and the second is not Conjure's concern.
`../playcanvas-unpack` rebuilds a capture into a standalone app, and its
[`docs/capture-format.md`](../../../playcanvas-unpack/docs/capture-format.md) is the contract the
grabber has to satisfy, defined by the consumer because the consumer can check it:
`python3 -m pcunpack check <capture>`.

It matters here for one reason. **Completeness for import and completeness for running are different
questions, and this pipeline only ever asked the first.** `capture_audit.py` checks assets against
`config.json`, which is exact — and of twenty captures, seven could not be rebuilt into a working app
and none of them reported a problem. Six had no engine and no bootstrap at all, which no manifest can
reveal: the engine and the five `__*.js` files are not assets, so nothing lists them.

**So `capture_audit.py` now asks both questions.** After the asset report it shells out to
`pcunpack check` and prints `RUNNABLE` / `CANNOT RUN` per capture (`--no-boot` opts out; a missing
sibling repo degrades to a line saying what the audit therefore cannot tell you). Shelled out to rather
than reimplemented, deliberately: the list of required files is a property of the CONSUMER, it is
already measured and tested over there, and a second copy here would be a second thing to be wrong —
with the stale-copy failure mode being precisely the one this check exists to remove, a stage reporting
success it has not earned.

## 3. Re-assembling a PlayCanvas build


`conjure/playcanvas.py` + `scripts/playcanvas_rebuild.py`. A second out-of-band ingest path, and the
only one that needs no Blender at all.

PlayCanvas's converter splits an upload deliberately: geometry and skinning into the GLB, materials and
textures into separate registry entries the engine rejoins at load time. A build downloaded from it
therefore hands over a model that renders **flat white in any ordinary viewer**, with every texture
sitting beside it and nothing in the file saying which goes where. Jane arrived exactly like that —
4.5 MB of correct geometry, zero materials, 87 MB of orphaned 4K PNGs.

The binding is stated outright, so this **transcribes rather than guesses**:

```
config.json   -> assets by id (containers, renders, materials, textures)
<scene>.json  -> entities, each with a `render` component holding
                   `asset`          -> a render asset -> (containerAsset, renderIndex)
                   `materialAssets` -> ONE PER PRIMITIVE, in order
```

A glTF mesh is split into primitives precisely because each had its own material, so the ordering
survived conversion and the list drops straight back on. **The materials are on the SCENE, not the
container**: a container ships whatever its own import produced and the scene overrides it — Jane's hair
container carries an untextured grey, and reading it instead of the scene gives grey hair with the real
texture unused on disk.

**Which claim wins where a scene and a template disagree is a CHOICE**, and the only real judgment in
this module: `read_build(root, prefer=...)`, defaulting to `"scene"`.

A template is the default a container shipped with; a scene is what actually runs. So the scene wins —
defensible, and still a decision, and it is the decision that makes the Japanese house's deck and
Alice's `Scalp_Female` disappear rather than be painted. `prefer="template"` reverses it, which answers
a different and occasionally useful question: what did this container ship with?

**`prefer` outranks richness, and only across a scene/template split.** Where two claims share a
source — three scene entities bind Jane's right hand and the first-listed wears a placeholder with one
sphere map — the best-dressed still wins, which is what that tie-break exists for. Across the split it
does not: of **1,340** meshes both claim, 952 are equally dressed and 384 favour the scene either way,
but **4 have a better-dressed template**, and there "what runs" has to beat "what has more maps" or the
knob means nothing.

**Which claim won the MATERIALS is a different question from whether a scene claims the MESH**, and
conflating them produced a false positive on real data: `dead_meshes` called Alice's `CC_Base_Eye` dead
for half a day because the template's richer materials had won the tie-break, while a scene entity was
drawing it all along. `Build.scene_claims` records the second question directly.

**`template` assets carry the same bindings and are read too**, after the scenes so a scene wins where
both speak. Not a fallback bolted on: a template is a serialised entity hierarchy, which is how
PlayCanvas packages a reusable thing, and a character is exactly that. The second capture had NO scene
file on disk and sixteen templates — one per skin-tone variant, each binding its own container, so
nothing to disambiguate — and reading them is what makes it convertible at all. It also retired
`--adopt` for Jane, whose template binds the in-file hair copy to the textured hair material: the
build's own answer in place of a name-match guess.

The blend and cull constants and the `glossPS` shader chunk are read out of the `playcanvas-stable.min.js`
shipped **in the build being converted**, not remembered. Each is a silent wrongness if guessed: a wrong
blend mode is invisible until something stands behind the figure, and an inverted roughness map reads as
a lighting problem.

Five things it detects rather than assumes, all of which Jane exercises:

| | |
|---|---|
| an `opacityMap` on a texture with **no alpha channel** | a no-op — five of her eight materials do this, and believing them emits `MASK` and punches holes through her |
| an indexed PNG with a `tRNS` chunk | alpha that is not a channel; her eyelashes are one, and read as RGB become a rectangle across her face |
| `glossInvert` | decides whether the source is gloss or roughness. One build uses it **both ways** — her lips (invert, shininess 0) and her mouth (no invert, shininess 90) are both wet, from opposite settings |
| `alphaToCoverage` | a CUTOUT, so it becomes `MASK` whatever the blend mode says. One model shares a single atlas between shorts, shirt and hair with a separate mask selecting each garment's region; read as `BLEND`, the regions that should vanish came through as patches of the other garments' colours |
| a mesh no entity binds | left untextured, and the note says so — the scene does not render such a mesh at all, so grey is the one outcome the source never produces. `--adopt` takes the material from an identically-named render asset elsewhere, reported as INFERRED. Reading templates removed the need for it on both models here |

### Two things the conversion REPORTS rather than fixes

Both are the conversion telling you where a symptom came from, because both produce output that looks
like our bug and is not.

**A mesh bound only by a TEMPLATE, once a scene has been read, is probably not drawn at all.**
`dead_meshes(build)`. A template carries the container's own default binding and a scene is what runs,
so a surviving template binding is a mesh no scene entity asked for. That is the only signal for a
whole family of symptoms: the Japanese house's deck (`PLANE.002`, replaced in the scene by
`WOODout.glb`) and Alice's `Scalp_Female`, which is bound — to one of four scalp materials, the one
with no maps — so it converts opaque WHITE while the site never shows it. Her real scalp is a primitive
of her hair mesh.

Scoped to containers the scene DOES use, which is the difference between a signal and 725 rows of
noise: a container no scene mentions is not full of dead meshes, it is a container this scene does not
use, and the VR shell and the props library are template-bound in every capture. Across twenty captures
the rule reports **47** meshes. It says nothing at all where no scene was read, since the comparison it
rests on does not exist.

The other flavour of dead is a mesh bound by **nobody**, which `rebuild` already reports per container —
office-babe's body twin, bride's, Oktoberfest's. Between them the two notes cover every case found so
far.

**An asset id the registry NEVER HAD is reported apart from a file that is merely absent.**
`dangling(build)`. `missing_files` walks the registry looking for files not on disk; an id the registry
never defined is not walked, so it reports nothing absent and whatever points at it converts flat. The
deck's material `WOODout` points at texture `194421251`, which is in no `config.json` — and *"no
texture on the railing of the house"* cost a session and several re-downloads that could never have
helped. **This is the difference between "re-capture this" and "the site ships it this way",** and
nothing we produced could tell those apart.

Only what a BINDING depends on. Scanning the whole registry finds 1,011, almost all in props materials
nothing binds; reachable from an emitted mesh it is **42**.

**A whole MATERIAL the registry never had is reported and not failed.** `fucked.in.basement` binds a
doorframe and two chains to material ids in no `config.json` anywhere in the capture. The site draws
such a mesh with its default material and a glTF primitive with none gets the viewer's default, so the
file already matches what runs — the rebuild notes it, and `verify_thing` no longer counts it as a
problem, which it used to for something no re-download could change. A material that IS in the registry
and missing from the file is still the flat-white failure the verifier exists for, and still fails.

### Things — the unit a container is not

`things(build)` reads what each SCENE places, rather than what each FILE holds. A container is one
artist's export; a **thing** is a scene entity subtree, and the two disagree in both directions:
`office-babe` draws from three containers while `TOOLS LIBRARYblend5.glb` is split into **fifteen
separate props**. Measured on the captures:

```
office-babe          192 entities   9 live  1 optional   office-babe.glb×8 · manager_fixing.glb×1 · underwear.glb×1
Oktoberfest-milf     183            6      1            -milf.glb×2 (eyelashes, BEER) · -fixing.glb×4 · underwear.glb×1
Bride                237           12      1            model_britney_bride.glb×1 · bride_ready.glb×11 · underwear.glb×1
JAPANESEROOM BAKED    39           34      1            JAPANESEROOM BAKED.glb×34 · WOODout.glb×1 (the deck)
Banana                 2            1      0            TOOLS LIBRARYblend5.glb×1  (catalogued)
```

Each `Piece` carries what the container file cannot hold: the entity that draws it, the **parent** it
hangs off (`DEF-hand.R` for the beer, `DEF-foot.L`/`.R` for bride's two heels), the **path** from the
thing's root, and the **transform composed down that path** — a banana reads `scale 0.458` because its
catalogue wrapper's 0.5 multiplies the inner 0.9166, and `WOODout` reads `y = -0.1` because the scene's
`-10` passes through a house scaled 0.01. Every dead twin is simply absent, with no rule needed for it.

**Three states, and `enabled: false` means two different things by LEVEL.** On a piece it is *optional*
— the site's own wardrobe switch, which is what `underwear` is in all three figures and what the parts
classifier has been reconstructing from mesh names. On a **thing** it means *in the catalogue, not
placed in this scene*, which is the entire props library: all 15 tools and all 13 skin-tone variants
are disabled Root children. Composing the thing's own flag into its pieces would mark every prop
optional and import none of them — a test caught exactly that.

A non-zero **rotation** anywhere in a chain sets `Piece.rotated` rather than being folded into
`position`/`scale`, because euler order is a decision the summary does not get to guess at. `Piece.chain`
carries every link's own transform instead, and `Thing.tree` carries the whole subtree — which is what
the composer builds from, since **538 of 948 pieces across the twenty captures are rotated** and a
summary is wrong for the majority of them.

**A build with no scene falls back to its templates**, and the nesting differs: a scene wraps its
things in a `Root`, so the things are Root's CHILDREN, while a template IS one thing already —
`VR_hand_R` is 53 entities under a single root — so there the root is the thing. Treating them alike
returned a hand's fingers as three separate props.

**`captures/things.json` holds the corrections**, on the same search path as `parts.json` — a user file
shadows the bundled one, env `CONJURE_*` aside — and for the same reason: the answer is the captured
site's convention, so the next capture can arrive shaped a third way and fixing it should be an edit.

Two kinds of entry, and they are very different in size:

- **`exclude`** is SITE-WIDE and written once. Every capture ships the app's own scene identically —
  `2049393.json`, 1,016 entities whose Root children are `LeftController`, `Gestures`, `ToolModeStore`,
  `TRASH` and five more. They render meshes and nobody would place them. Nine names, all captures.
- **`captures`** is per capture, keyed by scene file, and **eighteen of twenty carry an entry — two of
  them to SPLIT, the rest only to RENAME**. susan's Root child `aula` is a classroom with Alice standing
  in it; `ModelParent` (the figure) and `EnvironmentVR` (the room) sit two levels down and are separately
  useful. Nothing in the graph says to split there. A LIST names the things at any depth; a MAP also
  renames, because `ModelParent` is no use as an asset label — and neither is `MainModelToiletAgnes`, so
  a figure takes the handle its capture is already known by, and five rooms named after the woman
  standing in them (`CheatingBride` → `church`) stop competing with her in the catalog.

That ratio is the answer to "will this need manual guidance for every import": one file written once,
plus two captures in twenty that need a SPLIT — and `_lift_figures` now does that from the file alone
(below), so what is left in the file is a label. It is a floor rather than a ceiling — twenty captures
from one origin is a narrow sample — which is why detection reports rather than decides.

**Which entity is a thing is CONVENTION, not format.** The rule — a direct child of a scene's Root
whose subtree renders something — holds in the content scenes and is **false in the app's own**:
`2049393.json` offers `Gestures` (380 entities, 1 piece), `ToolModeStore` and `TRASH`, which are
machinery groupings nobody places. So `things()` PROPOSES, `thing_notes()` reports what it proposed
with the counts that give it away, and `roots=` overrides it per scene. The same discipline
`parts/parts.json` uses for garment words. `scripts/playcanvas_rebuild.py --things` is the view.

**A figure standing in its room is lifted out, from the file alone.** The Root-child convention composed
cyberpunk's apartment, the hotel maid's room, both classrooms, the washroom, the shower scene, the vampire
manor and the school corridor as ONE thing each — walls, furniture and a woman — and a figure welded to a
room cannot be placed, posed or given a clip. `thing_notes` had detected the shape and asked for a
hand-written split per capture; `_lift_figures` now does it with no names. A **rig** is the smallest
subtree above a skinned piece that holds every bone its skin names. It is a **figure** when
`best_humanoid` recovers a humanoid map from its container — the import's own test for a character — so
the basement's 52-bone skinned `Chains` and underwater's school of fish stay where they are. It is lifted
only when the thing also draws something outside it, from the highest wrapper that draws nothing else, so
a placement node carrying her scale goes with her — on the classroom that is `ModelParent`, what the
hand-written entry for it named — and anything hanging off her bones goes too. A `things.json` entry for
the scene still wins. Measured 2026-09-21: 8 figures lifted across the 78 captures, every one life-size
(1.68–1.83 m), every room left with no skinned mesh; the original twenty byte-identical, since none of
them has the shape, and 747 of the new set's 755 files byte-identical, the other 8 being the rooms that
lost their figure.

> **That count predates the memo fix of 2026-09-25 and is worth re-running.** The skin-joint and
> humanoid memos were keyed on `id(build)` in a module-level dict, and a bulk run frees each build
> before allocating the next, so a capture could be handed its predecessor's humanoid verdict — which
> decides this exact question. A borrowed "not humanoid" lifts nobody. The measurement above was taken
> in one such run over all 78.

**Nothing in it is keyed to any particular model** — every string in the module is a PlayCanvas or glTF
schema key, and the one heuristic (`--adopt`) matches on render-asset names read from the build. But the
coverage was shaped by one character, so what it CANNOT carry is listed in `_UNCARRIED` and warned about
per material rather than dropped quietly: the specular/gloss workflow, light and environment maps,
height maps, clear coat, sheen, refraction, iridescence, depth write and depth test switched off, and
texture tiling/offset/rotation. Second UV sets, `emissiveIntensity` and `aoIntensity` are carried.

The sheen and iridescence tests gate on PlayCanvas's `use*` flag, never on a value, and that distinction
is measured: it
leaves `sheen` at a default WHITE with `useSheen: false`, so a check keyed on the colour fires for all
208 materials across the three builds here — and a warning that always fires is one nobody reads.
`useDynamicRefraction` is true exactly **once** in those 208, on Jane's eyes, which is the case the list
earns its keep for and the one visible thing this does not reproduce.

**Three ways a layer can be all environment and no content, and glTF expresses none of them**: a blend
mode that only LIGHTENS (`BLEND_ADDITIVE`, `_ADDITIVEALPHA`, `_SCREEN`, where black is the no-op), one
that MODULATES (`BLEND_MULTIPLICATIVE`, `_MULTIPLICATIVE2X`, where white is), and a see-through MIRROR —
`useMetalness` with metalness ≥ 0.9, a skybox or environment map, no diffuse map, opacity ≤ 0.6. Each is
made invisible when it carries no detail of its own, because drawn opaque it OCCLUDES the thing it was
meant to enhance — and in every case here that thing was an eye: one model's corneas went solid black,
another's solid white, over irises the body mesh was drawing all along.

**A fourth way: a refractive LENS.** No maps of its own, see-through, `useDynamicRefraction` set — its
whole appearance is the environment bent through it, which glTF core cannot express. The fallback is not
neutral: a flat colour at partial alpha is white PAINT over the thing it was meant to enhance. Bride's `Reflections-eyes` is 48% white over her irises and washed the
brown out to a pale tan. Five of 1,241 bound materials are this shape, four of them named for an eye
(`Reflections-eyes`, `Genesis 8 Female_EyeMoisture`, `Sclerea`, `eyes`); the fifth is a pane of glass,
likewise better seen through than fogged. Made invisible and warned about, exactly as the mirror and the
two blend families above.

It is **additive**: images become buffer views on the end of the existing binary chunk and nothing that
was in the file moves, so the geometry comes out bit-identical and a map derived from the original still
applies. Measured on Jane — same 22-bone `rigify-def` map, same 1.82 m, same 110,870 triangles, 17/17
named poses — with 8 materials where there were none.

### Composing — one GLB per thing

`compose_thing(build, thing)` writes the subtree out as a single self-contained GLB;
`scripts/playcanvas_rebuild.py --compose` runs it over a capture. It emits **alongside** the
per-container output rather than instead of it, so nothing downstream changes until the comparison is
done. Measured over the twenty captures: **258 things, 533 MB**, against 416 per-container files.

**The node tree IS the scene's entity subtree, one for one.** Not a merge of the containers'
hierarchies, which is where the first version went wrong: a container's own root transform is
*reproduced* by the entity that instantiates it — bride's `RootNode` is scale 0.01 and so is the entity
`bride_ready` — so copying both applied it twice and she came out 100× small. Building from the scene
also removes the skeleton merge entirely, because the scene expands every bone into an entity:
**181/181 for office-babe across three containers, 222/222 bride, 175/175 Oktoberfest, 85/85 Alice**.
There is one skeleton because there is one scene. A container whose bones the scene does NOT expand
falls back to carrying its own, and says so loudly; it has never happened here.

**One mesh drawn twice in the SAME place is a VARIANT; drawn twice in two places it is an instance.**
Both halves are real in this corpus, exactly one case each out of 948 pieces. Bride's heels are one mesh
on `DEF-foot.L` and `.R`, and flattening them loses a shoe. Alice's hair is `Side_Swept` and
`Side_Swept2` — one mesh, one transform, both enabled, two material sets — a colour switch the site
flips with a script the capture does not contain, and drawn together they z-fight: the pale locks at the
front of her otherwise brown head. The better-dressed claim wins (`how_dressed`) and the loser is kept
and flagged `variant`, because it is a wardrobe option the moment anything can switch it.

**`prefer="scene"` has NO exception, and the attempt to give it one is worth recording.** Four meshes in
the corpus have a scene claim and a better-dressed template claim. Three are obviously the scene's to
win — ebony's scene says `ArmsVR` where the template says `ArmsAR`, barbie's says `strands Copy` where
the template says `sclerea`: two distinct authored answers each, and this is a VR app. The fourth is
Alice's eyes, and it looked different. Her scene binds `Eye_R, Cornea_R, Eye_R, Cornea_R` over four
primitives where the template binds `Eye_R, Cornea_R, Eye_L, Cornea_L`, and the cornea it reaches for is
a MAPLESS duplicate: three assets are named `aula_Std_Cornea_R` and it takes one of the two empty ones.
Her corneas are where the blood vessels in the whites are drawn, so the scene's pair renders as nothing —
and the per-container output, which never applied `prefer`, looked *better* than the composed thing.

A rule called `filled_in` was written for that reading: strictly fewer DISTINCT materials over the same
primitives, and less well dressed with it. **It was wrong, and it fired on that one mesh and no other in
twenty captures.** Checked against the running site, Alice has no blood vessels in either eye — the site
draws the scene's claim, mapless corneas and all, and our Alice was the one with the extra detail. The
confirming fact is in the capture rather than in the pixels: **nothing anywhere in the build binds the
textured corneas**, so `aula_Std_Cornea_L` and the textured `aula_Std_Cornea_R` are unused Character
Creator leftovers sitting in the registry, and reaching for them was reaching for something the app
never draws.

The lesson is about the shape of the mistake, not the eye. "Fewer distinct materials than available"
described the data correctly and *inferred intent from it* — that an author who repeats a material must
have lost something. What actually runs is the only evidence of intent there is, which is what `prefer`
already said. `Piece.redressed` survives for the narrower case it is honest about: a scene entity that
names no materials at all falls back to the container's template.

**A base colour outranks a slot count** in `how_dressed`, and Alice's hair is why: her two claims fill
six map slots each, and one primitive of the losing set carries a SPECULAR map where the winning set
carries a diffuse, so it renders as flat paint. Slots alone score that a tie and pick by luck. The same
rule settles which entity's materials win a binding, where it fixed a second case of the same shape:
ebony's `handmodeltutorial` binds a sphere-map placeholder over both hand primitives and used to beat
the real `ArmsVR`/`FingernailsVR` on a 2-2 tie — the chrome-hand bug `how_dressed` was written for, one
capture further on.

**The thing's own scale travels with it and its position does not.** `Banana` is a 0.5 wrapper around a
0.9166 mesh and a banana that skips it is twice life size; where the scene happens to stand the vase is
not a property of the vase.

**Weldability is a question about the BONES, and it took two wrong shapes to find that.** Comparing two
containers' inverse bind matrices was the wrong question — an IBM maps a *mesh's* own space into bone
space, so `bride_ready.glb` (centimetres) and `model_britney_bride.glb` (metres) legitimately differ on
all 222 and can still share a skeleton. Comparing WORLD matrices was the wrong frame — it fails whenever
the scene scales the thing, and Oktoberfest is 1.25, so all 175 of her bones read as broken while nothing
was wrong. What has to agree is each bone's **own** transform relative to its parent, which is what
`joints_agree` compares.

**A thing can hold several rigs that reuse the same bone names**, and one skeleton keyed by name cannot
tell them apart. underwater's `MOVING` group is three fish, each its own subtree with its own `core`,
`spine01`, `spine02` and `tail01`: first-name-wins skinned every fish to the FIRST fish's bones and drew
them piled onto it, and the verifier — comparing by name too — reported all three as bound against a
different rest pose, which read as the retargeting problem and was not. Measured on those three: 5 bones
off each against the first fish, **0 against their own**. So a skin's bones are resolved inside the
smallest subtree above the piece that holds every name the skin asks for, and the skin is keyed by that
rig so a second fish does not reuse the first one's. Where every name occurs once — every figure in the
corpus — that is the node the name map already gave, and the output is byte-identical. The verifier
checks each skin against the bones it actually points at, and "the skeletons stacked" now means what it
was written for: a skeleton COPIED in beside the scene's, never two rigs the scene itself states.

**Where every container disagrees with the scene in the same way, the scene is holding a POSE.** A scene
entity's transform is usually the bind pose and is sometimes a saved pose, and only the containers can
tell the two apart. Alice's `CC_Base_L_Eye` and `CC_Base_R_Eye` are 90° out in the scene and nowhere
else — a composed asset takes the container's value there and records it in `extras.conjure.rebound`.
Where the containers disagree with EACH OTHER the scene keeps its value and the odd one out is reported:
letting bride's half-body donor rewrite the skeleton would break the eleven pieces bound to it to fix one.

**Every composed file carries provenance in `extras.conjure`** — the thing, the scene, the containers,
the `hidden` list, and per mesh node the piece index, its `(container, mesh)` and whether the scene had
it switched off. `hidden` is glTF node names, which is exactly what the runtime `figure-parts` component
already consumes (`specs/figures.md` § 8a).

**A hidden name is unique AS THE CLIENT WILL LOAD IT.** `figure-parts` hides by node name, after three's
loader has sanitised names (whitespace to `_`, `[ ] . : /` removed) and renamed repeats `_1`, `_2` in
load order. Scene entity names repeat freely: rave.wc's washroom has `Stall_Door` three times — a group,
a switched-off door and a drawn one — and Bianca has two switched-off `underwear` meshes. So a hidden
entry reached whichever copy loaded first: the washroom hid a door the scene draws and drew five pieces
it switches off, and Bianca's second `underwear` never hid at all. A HIDDEN piece whose name another node
shares is now written `name~2` (`~` survives the sanitiser), with the scene's name kept as `entity` in its
provenance. Nothing else is renamed — a name nothing hides by is harmless however often it repeats, and
renaming it would change the bytes of props files that are one catalog row precisely because their
bytes match — and a bone never is. The client side matches names in the loader's spelling
([`specs/figures.md`](./figures.md) §8a), and the verifier compares them that way, which is how it caught
Bianca: a raw-name check passed her because both copies were in the list. Measured 2026-09-21: 16 hidden
names shared by more than one node across the 78 captures, and 9 the client could never match.

**`verify_thing` judges the file against the `Thing` and the source containers**, and silence is the pass:
one node per piece and nothing else drawing, per-primitive vertex counts equal to the source mesh,
materials matching the registry BY NAME, the world transform equal to the entity chain with its
rotations, an instanced mesh still two nodes on one mesh, a bone-parented piece still on its bone,
optional pieces present and flagged, and every bone standing where the container was bound against it.
Across all twenty captures it reports **two problems, both on bride**: `underwear.glb` never downloaded
(a gap in the capture, and it says so), and `model_britney_bride.glb` is a different rig wearing the
same 222 bone names — the half-body the director was picking as "another bride".

**Over the full set of 58 captures (2026-09-21) it reports five**: bride's `underwear.glb` again missing
from one capture, and four from the bone check — `model_britney_bride.glb` in both bride captures, and
gothic.witch.hunt's `test2.glb` and `underwear.glb`. Those four are NOT pieces posed wrongly. Each is a
body or garment whose bone NODES are stored in a different pose (123 of 222 rotations differ), while the
binding the skinning actually uses — the inverse bind matrices — agrees with every other piece's to one
constant: body and dress move together on all 222 bones, 0.0 cm apart, and the same for the witch. The
check compares node transforms, which is the wrong thing where a file's nodes are not its bind pose
([`backlogs/captures.md`](../backlogs/captures.md)). And the "half-body" above is a misnomer: it is her
whole visible body, 41k vertices in metres, beside an unused 201k-vertex one in `bride_ready.glb`. 755
things, every one loading in `glb_check.mjs`. Before that day's fixes it reported 21 across 8 captures;
the other sixteen were the rename, hidden-name, several-rigs and dangling-material cases above, and none
of them was the capture's fault. The original twenty were re-run on the same inputs: **257 of 258
composed files byte-identical**, the 258th being Bianca's second `underwear` gaining the name that lets
it hide, and the six acceptance sizes unchanged.

**A glb viewer draws every node it is given**, and `hidden` lives in `extras`, so the asset looks wrong
in one: Alice's hidden pale hair and bride's switched-off underwear are both plainly visible and read as
bugs that were already fixed. `--as-shown` writes a copy with those left out — for LOOKING at, never for
importing, and recorded in `extras.conjure.shown` so the verifier judges it by what it claims to be and
so it can never be mistaken for the asset. The asset keeps them, because a part the runtime can show
again has to be in the file to be shown.

`scripts/glb_check.mjs` is the second, independent gate: it loads a GLB with the same three.js
`GLTFLoader` the client uses and prints the bounding box. A file can satisfy every structural check and
still throw in the loader, and it can load perfectly while being a hundred times the wrong size. All 258
load; the six acceptance cases measure 0.07 m (Banana), 1.73 m (Alice), 1.83 m (office-babe), 1.87 m
(bride), 2.04 m (Oktoberfest at her 1.25 scale) and 10.9 × 3.6 × 13.7 m (the Japanese room).

**Basis textures are decoded by a pre-pass**, `scripts/basis_to_png.js`, which writes `Foo.png` beside
`Foo.basis` — the exact name the registry already gives that texture, so the rebuild finds it knowing
nothing about Basis at all. It stays a separate script with its own output on disk, and both the rebuild
and the compose RUN it first (`--no-decode` opts out): a fresh capture overwrites the decoded PNGs, and
leaving the pre-pass to be remembered cost nancy her skin — 0 of 6 materials mapped, which reads exactly
like a failed download and is not one. The transcoder is **found, not required**: `--transcoder` if given, else the one the build ships
(`basis.wasm.js` + `basis.wasm.wasm`, two separate PlayCanvas assets in unrelated directories), else the
upstream build committed in `vendor/basis`. The capture's own copy is preferred because a decoder
shipped beside the data is the one certain to read it; the vendored one exists because a capture only
contains one if the grabber happened to save it, and that is the file everything else depends on. Their
output is byte-identical on the textures here. A capture's own copy is also a BROWSER bundle that may
not START in Node — one throws `$jscomp is not defined` on require, before any file is read, taking all
fourteen of that capture's textures down with it — so a transcoder that will not load falls back to the
vendored one rather than ending the run.
The PNG is written by hand over `zlib`, Node shipping no encoder
and a truecolour-with-alpha PNG being cheaper to write than to justify a dependency for.

**A normal map needs unpacking.** Basis stores one as X in RGB and Y in alpha, which transcodes to a
greyscale image with an independent alpha — and handed to glTF's `normalTexture` that way, grey remaps
to a ZERO-LENGTH normal and the lighting breaks out in dark blotches over every surface using it. Z is
recomputed and the alpha dropped. Which textures to treat this way needs the REGISTRY and the pixels to
agree, and neither alone: a genuine greyscale mask with an alpha channel is indistinguishable from a
packed normal, and one build has both — a pixel heuristic alone wrecked its specular maps while fixing
its normals — while a normal map may also be stored plainly, and unpacking one of those is worse than
leaving it alone. So the registry says which assets are normal maps, and the pixels (greyscale, with an
alpha that is not a solid 255) say which of those are packed.

Measured on Akari: 42 files decoded, 0 failed, and she rebuilt at 9 materials over 3 meshes.

**A texture may be on disk in a form nothing here can read.** PlayCanvas transcodes textures to Basis
Universal and the engine asks for that in preference, so a capture made by BROWSING holds `.basis` and
never the PNG beside it — 91 of Akari's 105 textures declare such a variant. Basis is GPU-compressed,
Pillow cannot open it, and decoding wants a transcoder this does not carry. So `variant_only` reports
those separately from absent ones, with the URL of the uncompressed original, and counting them as
missing (which an earlier pass did) overstated one capture's gap by eight files.

**A file the download RENAMED is found where it landed.** The grabber cleans every path segment before
Chrome saves it (`src/naming.js`, `cleanSegment`), and Chrome rewrites a little more on top, so five
kinds of name arrive on disk different from the registry's: a run of whitespace collapses
(`BAKEFLOOR  SpecularMap 2k.png`), `" * : < > ? |` become `_` (`door.001|door.001Action.glb`), leading
and trailing dots go, a run of dots collapses (`…VR holes dot com..mp3`), and a space before the
extension goes (`TOOLS .glb` saved as `TOOLS.glb`). Each left a file that was present, complete, and
reported NOT IN THIS CAPTURE. `Build.locate` tries those names after the exact ones — so a capture that
kept the true name is never second-guessed — and `_saved_as` is the mirror, kept as a copy rather than
imported because a capture outlives both repositories. Measured 2026-09-21: 199 registry files across
the original twenty captures and 592 across the full set of 58 were on disk and read as missing, **21
and 68 of them containers** — `TOOLS .glb`, the props container, in nearly every capture, so every
thing that draws from it had been composed without its props.

**One output directory, and file names that are not unique.** Per-container output writes every build
in a capture into one directory, and a container's file name is not unique across builds — nor always
within one: `model_britney_witch_ready.glb` is two different containers, 3.7 MB and 10.2 MB, in the same
build, and the launcher's `Dilda.glb` sits beside each scene's own. Each second write replaced the first
and nothing said so — 3 of the original twenty captures and 18 of the 58. The first keeps the plain
name; a later one is written as `<name>~<asset id>.glb` and reported, and the importer reads the id back
out of the suffix. `compose_build` had the same promise in its comment and the guard inside out: it only
looked for a new name when the file existed AND this run had not written it, so a second same-named
thing overwrote the first and a re-run suffixed every file instead of replacing it. It now asks only
whether this run wrote the name.

**What the capture does not hold is reported as a list, with URLs.** Akari's release references 105
textures and holds none of them; reported per use that was 200-odd identical lines burying the two
findings that mattered, so missing files are collected once and `--fetch-list` writes their addresses
for `curl` or `wget`.

**A capture without a registry is a different failure, and it is reported as one.** The second one to
arrive had the right layout, valid GLBs, and no `config.json` anywhere — so there was nothing saying
which material went where, and no texture files either. `find_orphans` reports that shape and derives
the address to fetch from the directory path, because a capture mirrors the URL it came from: a path
segment with a dot in it is the host, and everything between it and `files` is the build.

**`--max-texture` defaults to 1024, and that is a budget rather than an optimisation.** Her textures are
4096 square throughout: roughly 90 MB of VRAM each once mipmapped, nine of them for one character who
already costs 111k triangles. At the default the whole capture — 9 containers across 3 nested builds —
rebuilds in about four seconds and she lands at 6.8 MB.


### Positions — what the site states about its own animation

`conjure/positions.py`. Four of the twenty captures ship a per-position config, and it is not
decoration: it declares what the rest of this pipeline infers. `position_N_config.json` gives the clip
each position plays, the sound that goes with it, the RATE to play it at, and the secondary layers that
run on top; `main_config.json` gives `mainModelName`, `dressObjects` and a bone mask per layer.

**Where it speaks it beats the filename rule.** `capture_set.audio_role` pairs a clip with its voice by
stem, which is all the older builds give you — jane is 20 of 20 — and the four captures it links NOTHING
for (`susan` 0/8, `nancy` 0/62, `ebony` 0/140, `ebony2` 0/62) are exactly the four that ship configs.
The config is how the newer builds say what the older ones said by filename. Both mechanisms run; the
stated pairing is added after the stem match and never duplicates it.

**Verified per capture rather than trusted**, because a config that stops resolving is saying it
describes a version of the build no longer on disk: `resolves()` reports clips and sounds found against
named, and it is **98/98 and 30/30** on nancy, **100/100 and 30/30** on ebony, 98/98 on ebony2 and 6/6
on susan. A name matching no asset — or an asset of the wrong type — is counted, never guessed at.
Result: **216 new `voiced_by` edges** across those four, where the stem rule produced none.

**The rate is authored and it is not 1.0.** 0.5, 0.6 and 1.2 appear in this corpus, so a clip played at
its own speed is played wrong. Stored as `attributes.speed` on the animation, and only when it differs
from 1.0 — recording the default would make "nobody said" indistinguishable from "they said one". A
clip two positions disagree about is reported rather than resolved; there is no basis for choosing.

`/figure/clip` applies it. The source's own code is
`assignAnimation(name, resource, "Base", animSpeed, true)` with `animSpeed` read straight from
`idle.speed` / `action.speedAction` / `action.speedRough`, so the rate belongs to the clip and not to
the request. `FigureClipRequest.speed` is therefore `None` rather than `1.0` by default, all the way
out through `conjure.ctl` and the `play_clip` tool: a caller sending a well-meant 1.0 would override
the author, and "nobody said" has to stay distinguishable from "play it at normal speed regardless".

The voice is deliberately NOT rate-shifted with it. The source plays the sound on its own slot with
`loop = true, overlap = true` and never seeks or re-rates it — the voice runs under the clip rather
than in lock-step with it — so slowing the body does not slow the speech.

**A blink is a LAYER STATE, not a clip you play.** That is the answer to the long-open *"Teacher's
`Blink.glb` is never played at placement"*: it is a bone-masked state that fires on its own timer —
every 5–8 seconds, at a weight, over whatever the position is running — with the mask coming from
`main_config.boneLayers[].enabledBones`. `Layer` carries all of it and nothing consumes it yet; the
runtime side is layered additive animation and has not been built.

**A registry `audio` asset is not always audio.** Three are `.mp4` promo videos, and they only started
arriving once the grabber stopped skipping declared audio — the first import after that fix crashed on
them. Content decides, as everywhere else here, and the mismatch is counted as `audio:not-audio`.

## 4. Import — what a capture becomes

`scripts/import_capture.py`. Dry by default; `--commit` writes. One capture becomes many rows, and the
kinds are decided by content rather than by extension: a `.glb` holding animation channels and no mesh
is an `animation`, not a `model` (see [`specs/library.md`](./library.md) §2a).

**One asset per THING, and the default input is `temp/things/<name>`.** `--models` points it elsewhere
(`--rebuilt` still works, for the older one-per-container output). What changes is not the count but the
unit: `TOOLS LIBRARYblend5` stops being one cutlery drawer and becomes fifteen separately placeable
tools with their own names, and `office-babe` arrives whole instead of as a body, a fixing pass and a
pair of underwear that nothing distinguished from a figure.

**A composed file is identified by its PROVENANCE, not by its name.** It is named after the thing, so
there is no container stem to match — it carries the container ids it drew from in `extras.conjure`
instead, which is the fact rather than a coincidence of naming. Keying `shipped_with` on the file name
silently lost every authored set the moment the output stopped being one file per asset. (The stem is
still read for the older path, and for recognising the rows a previous per-container import labelled.)
Containers are keyed by FILE name there rather than registry name because those disagree — Susan's
registry calls a container `aula_Aliceglb` while the file is `Alice.glb`.

**The capture name is deliberately NOT in the composed bytes.** A composed file is content and the
capture it came from is catalog metadata; putting it in `extras` defeated the content addressing the
whole catalog rests on. The props build is the same PlayCanvas release in 15 captures, so `Banana` is
one row tagged with all fifteen — and with the capture in the bytes it was fifteen rows with fifteen
ids and fifteen copies of the geometry. 311 live models against 98.

**`parts_hidden` is stated by the source rather than classified.** The scene says which of a figure's
parts it has switched off, and the composer records those node names — a different fact from `parts`
(which garment a mesh *is*) and the one the runtime `figure-parts` component actually consumes. Until
now the only answer came from reading mesh names.

**And PLACEMENT applies it, which is a separate step that was missing.** Recording the wardrobe is not
showing the right half of it: `_model_entity_op` now sets the `figure-parts` component from
`parts_hidden`, so a composed figure is dressed correctly from the first frame. Without that a placed
Alice drew every variant at once — two heads of hair superimposed and a spare pair of underwear — which
is not a defect in the asset but the asset being complete with nobody choosing. It is the same field
`dress_figure` writes, so the two compose: this is the opening state and that edits it.

**Switching the unit RETIRES the old rows, and that is a separate operation from re-importing.** The
labels change, so ordinary `(kind, label)` supersession catches only the few that happen to share a name
(`office-babe` does; her thing is named after her file). `_retire_containers` recognises the rest by
their label being a container file stem of the capture, and retires each into the thing that now holds
its geometry: 43 rows over the twenty captures. `manager_fixing`, `Oktoberfest-milf-fixing`,
`model_britney_bride`, eight `underwear`s and `WOODout` all go — the half-models that were selectable
and shouldn't have been, which is why asked for "a different bride" the director offered a bare body.
Where one container became several things the tombstone points at the SET, because that is what
replaced it; pointing at one of fifteen would be picking arbitrarily between equals.

**A container no thing draws is left alone and reported.** `computer_desk`, `magnet`, `cool_button`,
the Quest controller model and the VR hands are bound only inside the app's own excluded machinery, so
the old path was cataloguing a toolbar as placeable props. Whether any is worth rescuing is a
`captures/things.json` decision and not the importer's to take.

**Relations are the point of importing a capture as a SET** rather than as loose files
(`conjure/capture_set.py`): `shipped_with` records which clips a figure was given, `voiced_by` links a
clip to its audio, `part_of` groups a set. See [`decisions.md`](../decisions.md) §27 for why the
authored set and the compatible set are recorded separately and never merged.

Every asset is tagged with the capture's name, merged rather than replaced, so a file shared by eleven
captures carries all eleven tags.


## 5. Blender — the other out-of-band path

`.blend` cannot be loaded by a browser and has no third-party reader worth trusting, so something must
convert. That something is headless Blender — and it stays **outside the server**: `importer.py` carries
no server dependency and must never need a 3 GB application, so a machine without Blender imports GLBs
fine and simply cannot convert. The scripts are invoked by hand; nothing in `conjure/` calls them and
there is no config setting or `doctor` row for the Blender path.

`scripts/blend_to_glb.py` — the conversion pass, which is mostly **stripping**:

| Stripped | Why |
|---|---|
| rig widget meshes | UI, not content. Found **by reference** (`pose_bone.custom_shape`), never by name prefix — one rig spells them `WGT-`, another `GZM_` |
| unselected collections | outfits and hair variants are alternatives; one set is worn at a time |
| shape keys | Daz JCMs are driver-fired and glTF has no drivers (`--keep-morphs` to retain) |
| non-mesh/armature objects | empties, cameras, lights, lattices |

Visibility is checked at **both** the object and the collection level, because one porter marks the worn
set at one and another at the other. Naming a collection overrides *its* hidden flag but not per-object
hiding.

`reparent_deform_bones` (on by default, `--no-reparent` to disable) converts the relationship glTF cannot
carry into one it can: every bone that deforms **or carries deformers beneath it** is re-parented onto its
constraint target. `export_def_bones=True` was tried and is worse — Blender can only preserve a hierarchy
that exists, so it flattens constraint-linked deformers to the armature root.

Materials are resolved in a ladder, and the discipline is the same at every rung — **measure the artifact,
do not reason about the pipeline**:

1. **`--max-texture N`** rescales image datablocks before export (Blender's exporter re-encodes but will
   not resize). The single biggest size lever: 229 MB → 37 MB on one model, of which 210 MB was textures.
2. **Probe** — this rung and the three below it run only under `--bake N`, which is off by default.
   Export once, then read back which materials the exporter genuinely failed — no
   `baseColorTexture` and a near-black (`max < 0.15`) or absent `baseColorFactor`. The exporter's own
   output is the only oracle that cannot disagree with the exporter.
3. **`use_colour_images`.** Before baking, rebuild each failed material as a plain Principled around the
   one image the file itself tags as **colour data** (sRGB, against the Non-Color bump/spec/normal maps).
   A colour-space tag is authored metadata; a `_B` filename suffix is a guess.
4. **`bake_materials`.** Only what is left. Every non-target material on the baked object is handed a
   throwaway 4×4 destination first, because `bpy.ops.object.bake()` writes into the active image node of
   **every** material on the object — that is what silently blackened bystander textures.
5. **Black-bake fallback.** A bake that comes out black had no view-independent colour. `is_translucent`
   separates a lens from a fingernail by the Principled `Transmission Weight` (glTF's `alphaMode` does
   not: a lens exports OPAQUE); transparent for the former, a neutral base for the latter.

`--fix-udim` and `--strip-constraints` exist and default **off**. Both are sound for a genuine multi-tile
or cyclic case; neither should run speculatively, since each perturbed a bake and cost a debugging cycle.
`export_animations` is hard-coded **off**.

The script `os._exit(0)` after flushing, because Blender can fault during *shutdown* on multi-GB scenes
long after the file is written and valid — a teardown bug must not masquerade as a conversion failure.

Four companion scripts:

| Script | Job |
|---|---|
| `scripts/inspect_blend.py` | structural dump of a `.blend` to JSON — reads `bpy.data`, not the scene, because these are "append model" files whose objects are often linked into no scene at all |
| `scripts/blend_summary.py` | human-readable digest of those dumps |
| `scripts/glb_preview.py` | render a GLB from several angles (Workbench), printing the imported bbox and height so "is it life size" is answered numerically |
| `scripts/pose_test.py` | the **functional** test of a map: drives the real `best_humanoid` / `anatomical_axes` / `resolve_pose` and renders **front, three-quarter and side**, always — a front view cannot tell a raised knee from a leg swung backwards, and an arm aimed straight forward foreshortens into a folded elbow there; the three-quarter sits at 35° so no limb aligns with the view axis. Also accepts raw euler on a rig bone by its own name as an escape hatch |

`pose_test` writes the pose both as node rotations **and** as a one-keyframe animation, because Blender's
glTF importer reads a joint node's TRS as the bone's *rest* and reconciles the difference silently — an
animation channel is the one thing it applies over everything else.


## 6. Related specs

- [`specs/figures.md`](./figures.md) — what a figure IS once it arrives: bones, poses, parts, clips.
- [`specs/library.md`](./library.md) — the catalog row, the kinds, scope, and search.
- [`docs/running.md`](../running.md) — the runbook for actually pulling a capture through.
