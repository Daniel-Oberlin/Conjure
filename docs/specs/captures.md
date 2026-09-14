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

Four things it detects rather than assumes, all of which Jane exercises:

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

### Things — the unit a container is not

`things(build)` reads what each SCENE places, rather than what each FILE holds. A container is one
artist's export; a **thing** is a scene entity subtree, and the two disagree in both directions:
`office-babe` draws from three containers while `TOOLS LIBRARYblend5.glb` is split into **fifteen
separate props**. Measured on the captures:

```
office-babe          192 entities   9 live  1 optional   office-babe.glb×8 · manager_fixing.glb×1 · underwear.glb×1
Oktoberfest-milf     183            6      1            -milf.glb×2 (eyelashes, BEER) · -fixing.glb×4 · underwear.glb×1
bride_ready          237           12      1            model_britney_bride.glb×1 · bride_ready.glb×11 · underwear.glb×1
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

A non-zero **rotation** anywhere in a chain sets `Piece.rotated` rather than being folded in, because
euler order is a decision this does not get to guess at.

**A build with no scene falls back to its templates**, and the nesting differs: a scene wraps its
things in a `Root`, so the things are Root's CHILDREN, while a template IS one thing already —
`VR_hand_R` is 53 entities under a single root — so there the root is the thing. Treating them alike
returned a hand's fingers as three separate props.

**Which entity is a thing is CONVENTION, not format.** The rule — a direct child of a scene's Root
whose subtree renders something — holds in the content scenes and is **false in the app's own**:
`2049393.json` offers `Gestures` (380 entities, 1 piece), `ToolModeStore` and `TRASH`, which are
machinery groupings nobody places. So `things()` PROPOSES, `thing_notes()` reports what it proposed
with the counts that give it away, and `roots=` overrides it per scene. The same discipline
`parts/parts.json` uses for garment words. `scripts/playcanvas_rebuild.py --things` is the view.

**Nothing in it is keyed to any particular model** — every string in the module is a PlayCanvas or glTF
schema key, and the one heuristic (`--adopt`) matches on render-asset names read from the build. But the
coverage was shaped by one character, so what it CANNOT carry is listed in `_UNCARRIED` and warned about
per material rather than dropped quietly: the specular/gloss workflow, light and environment maps,
height maps, clear coat, sheen, refraction, iridescence, and texture tiling/offset/rotation. Second UV
sets, `emissiveIntensity` and `aoIntensity` are carried.

Those tests gate on PlayCanvas's `use*` flags, never on a value, and that distinction is measured: it
leaves `sheen` at a default WHITE with `useSheen: false`, so a check keyed on the colour fires for all
208 materials across the three builds here — and a warning that always fires is one nobody reads.
`useDynamicRefraction` is true exactly **once** in those 208, on Jane's eyes, which is the case the list
earns its keep for and the one visible thing this does not reproduce.

It is **additive**: images become buffer views on the end of the existing binary chunk and nothing that
was in the file moves, so the geometry comes out bit-identical and a map derived from the original still
applies. Measured on Jane — same 22-bone `rigify-def` map, same 1.82 m, same 110,870 triangles, 17/17
named poses — with 8 materials where there were none.

**Basis textures are decoded by a pre-pass**, `scripts/basis_to_png.js`, which writes `Foo.png` beside
`Foo.basis` — the exact name the registry already gives that texture, so the rebuild finds it knowing
nothing about Basis at all. The transcoder is **found, not required**: `--transcoder` if given, else the one the build ships
(`basis.wasm.js` + `basis.wasm.wasm`, two separate PlayCanvas assets in unrelated directories), else the
upstream build committed in `vendor/basis`. The capture's own copy is preferred because a decoder
shipped beside the data is the one certain to read it; the vendored one exists because a capture only
contains one if the grabber happened to save it, and that is the file everything else depends on. Their
output is byte-identical on the textures here. The PNG is written by hand over `zlib`, Node shipping no encoder
and a truecolour-with-alpha PNG being cheaper to write than to justify a dependency for.

**A normal map needs unpacking.** Basis stores one as X in RGB and Y in alpha, which transcodes to a
greyscale image with an independent alpha — and handed to glTF's `normalTexture` that way, grey remaps
to a ZERO-LENGTH normal and the lighting breaks out in dark blotches over every surface using it. Z is
recomputed and the alpha dropped. Which textures to treat this way comes from the REGISTRY, not from the
pixels: a genuine greyscale mask with an alpha channel is indistinguishable, and one build has both — a
pixel heuristic alone wrecked its specular maps while fixing its normals.

Measured on Akari: 42 files decoded, 0 failed, and she rebuilt at 9 materials over 3 meshes.

**A texture may be on disk in a form nothing here can read.** PlayCanvas transcodes textures to Basis
Universal and the engine asks for that in preference, so a capture made by BROWSING holds `.basis` and
never the PNG beside it — 91 of Akari's 105 textures declare such a variant. Basis is GPU-compressed,
Pillow cannot open it, and decoding wants a transcoder this does not carry. So `variant_only` reports
those separately from absent ones, with the URL of the uncompressed original, and counting them as
missing (which an earlier pass did) overstated one capture's gap by eight files.

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


## 4. Import — what a capture becomes

`scripts/import_capture.py`. Dry by default; `--commit` writes. One capture becomes many rows, and the
kinds are decided by content rather than by extension: a `.glb` holding animation channels and no mesh
is an `animation`, not a `model` (see [`specs/library.md`](./library.md) §2a).

**Containers are keyed by FILE name, not registry name.** They disagree — Susan's registry calls a
container `aula_Aliceglb` while the file is `Alice.glb` — and keying on the registry name silently
produced an empty authored set for a whole capture. The importer prints a warning when a container
cannot be matched to a file; that warning was printed and read past once, which is why it is now
counted in the summary line rather than only logged.

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
2. **Probe.** Export once, then read back which materials the exporter genuinely failed — no
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

Three companion scripts:

| Script | Job |
|---|---|
| `scripts/inspect_blend.py` | structural dump of a `.blend` to JSON — reads `bpy.data`, not the scene, because these are "append model" files whose objects are often linked into no scene at all |
| `scripts/blend_summary.py` | human-readable digest of those dumps |
| `scripts/glb_preview.py` | render a GLB from several angles (Workbench), printing the imported bbox and height so "is it life size" is answered numerically |
| `scripts/pose_test.py` | the **functional** test of a map: drives the real `best_humanoid` / `anatomical_axes` / `resolve_pose` and renders **front and side**, always — a front view cannot tell a raised knee from a leg swung backwards. Also accepts raw euler on a rig bone by its own name as an escape hatch |

`pose_test` writes the pose both as node rotations **and** as a one-keyframe animation, because Blender's
glTF importer reads a joint node's TRS as the bone's *rest* and reconciles the difference silently — an
animation channel is the one thing it applies over everything else.


## 6. Related specs

- [`specs/figures.md`](./figures.md) — what a figure IS once it arrives: bones, poses, parts, clips.
- [`specs/library.md`](./library.md) — the catalog row, the kinds, scope, and search.
- [`docs/running.md`](../running.md) — the runbook for actually pulling a capture through.
