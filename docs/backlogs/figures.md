# Figures — rigged humanoid models — backlog

Unfinished work and design for **figures**: rigged humanoid models brought in from Blender and other
sources, placed in a world, dressed, posed, and eventually animated and touchable. Nothing here is built.
There is no `docs/specs/figures.md` yet — it gets written when the first slice ships, per the house rule
that a spec describes what the system *does*.

This spans two existing subsystems and is deliberately kept whole rather than split between them, because
the design rationale is the expensive part and it does not survive being cut in half:

- the **import half** touches [`backlogs/library.md`](./library.md) — conversion, catalog `attributes`,
  licence capture
- the **runtime half** touches [`backlogs/dynamics.md`](./dynamics.md) and
  [`specs/dynamics.md`](../specs/dynamics.md) — a new module, the sync tiers, `ConjurePointers`

Rejected alternatives and consequential forks belong in [`docs/decisions.md`](../decisions.md) once they
are settled.

---

## Why this is not just "place a model"

`.glb` import already works end to end. `ModelImporter` (`conjure/importer.py:120`) sniffs the glTF magic
bytes and records bbox + tri count; `_model_entity_op` (`conjure/server.py:1043`) emits a plain
`components: {"gltf-model": "/assets/<id>"}` entity with auto-scaling, a `grounded`/`free` placement mode,
and a plane-relative anchor. **A Blender-exported human loads and renders today.**

So the first slice is a *measurement*, not a build. What is genuinely new begins after that, and it all
reduces to one thing:

> Everything past "place it" — outfits, poses, dance, walk, touch — is **"name a part of the model and do
> something to it."** A chair has no parts worth naming. A human does, and the whole feature stands or
> falls on whether *"left upper arm"* means the same thing across two models from two different sites.

**The vocabulary is the product.** The import pipeline exists to manufacture it. That framing is what
puts most of the weight on import rather than on the runtime, which is the opposite of where it first
looks like it should sit.

### Four things that break for humans specifically

| | Symptom | Cause |
|---|---|---|
| **Scale is semantically wrong** | a child, an adult and a giant all arrive the same height | `TARGET_SIZE_M = 1.8` fits the *largest dimension* to 1.8 m (`server.py:589`). For props that normalization is a feature; for people it erases the one dimension that carries meaning. Humans want **life size**, or normalization to a stated height. |
| **Facing is semantic** | "have her look at me" is a real request within ten minutes | already a recorded scar — a GLB character at rotation `[0,0,0]` faces **+Z** while session forward is **−Z** ([`backlogs/spaces-geometry.md`](./spaces-geometry.md), *§ facing*). Cosmetic for a prop; meaningful for a person. |
| **The bounding box goes stale** | the selection box no longer matches the silhouette | `_normalize` sits the bbox base at `pos.y`, and `grab`'s `_boxPick` uses `Box3.setFromObject`. Both read the **bind pose**. Once a clip plays, the drawn box and the visible figure diverge. Expected to be visible but tolerable — needs a look. |
| **Skinned-mesh raycasting is unverified** | can you grab a dancing figure by the shoulder, or only by where its shoulder used to be? | `grab`'s `_pick` raycasts meshes exactly. Whether `SkinnedMesh.raycast` in A-Frame 1.5's bundled three accounts for the current skinned pose or only the bind pose is version-dependent enough not to assert. **A device check, not a design question.** |

---

## Decided (2026-09-01, with Daniel)

| Question | Decision |
|---|---|
| Import `.blend` directly? | **No** — impossible. Convert first (below). |
| Conversion vs. import | **Separate steps.** Conversion needs Blender; import needs only a GLB. |
| Model sources | Downloads from various sites, **Open3DLAB** first. So: only *existing, commonly adopted* conventions may be assumed — and often none will apply. |
| Rig convention | **Flexible / discovered per model**, not mandated. |
| Import cost budget | **Generous.** Never bulk; significant time and money per model is acceptable. |
| LLM / multimodal in the loop | **Yes** — at import time. See the pipeline below. |
| Figures are… | **puppets first, personas later** ([`vision.md`](../vision.md) § personas). |
| Concurrent figures | **1 typical, 3 maximum.** |
| Outfit mechanism | **Answered from real files** — see the measurements below. |

---

## What the sample models actually contain (measured 2026-09-01)

Five `.blend` files in `temp/3d-model-examples/` (87–252 MB each, game-character ports) dumped with
headless Blender — `bpy.data`, not the scene, because these are "append model" files whose objects often
are not linked into any scene. **Every prediction in the first draft of this document was either confirmed
or replaced by something more specific.** ~1.5 s per file, cheap enough to run on every import.

Reproduce with the two scripts kept from this pass — they are the concrete beginning of layer 0:

```
/Applications/Blender.app/Contents/MacOS/Blender --background <file.blend> \
    --python scripts/inspect_blend.py -- out.json
python scripts/blend_summary.py out.json
```

| Model | Rig convention | Bones | Outfits | Skinned verts | Shape-key deltas |
|---|---|---|---|---|---|
| **Eve Maccaro** | **Rigify** (`DEF-`/`ORG-`/`MCH-`/`WGT-`) | 796 (61 control, 299 constrained) | none — hair variants only | 154 k | 280 k |
| **Hitomi** | custom, **plain-English, space-separated** | 657 + **4 separate hair armatures** | **14 collections** | 141 k | 166 k |
| **Leifang** | same custom scheme | 728 + 3 hair armatures | **11 collections** | 117 k | 107 k |
| **Grace Ashcroft** | **Daz Genesis** (`hip`, `abdomenLower`, `lCollar`, `…(drv)`) | 482 (65 control, 249 constrained) + 2 hair | 5 `Default_*` sets | 526 k | **115,000,000** |

Nine findings, roughly in order of how much they change the plan.

**1 — Outfits are Blender collections, already named semantically.** This answers the open question
outright, and answers it *better* than the mechanism I guessed at. The porters grouped meshes into
collections like `Mandarin Dress`, `Bunny Outfit`, `Training clothes`, `Default_FBI`, `Hair Twintails` —
and the `hide_viewport`/`hide_render` flags mark which set is currently worn. Hitomi ships 88 skinned
meshes with 65 hidden; exactly one outfit and one hair variant are visible.

So the outfit slot vocabulary **already exists, authored by a human, and needs no AI to recover** — it is
mechanism 1 from the list below (separate meshes on a shared armature) with the grouping handed to us for
free. This becomes **layer 0** of the discovery pipeline and demotes the per-mesh visual grouping pass from
"required" to "fallback for models that lack collections" (Eve has only one collection and needs it).

Two wrinkles: collection names are not slot *types* (nothing says `Mandarin Dress` and `Bunny Outfit`
are alternatives while `Eyewear` is additive), and some are junk (`Collection.001`, `Sandwich`). Inferring
the exclusive-vs-additive structure is a genuinely good, cheap LLM job on the names alone.

**2 — None of the five files contains a single animation.** `bpy.data.actions` is empty in all of them.
This is the largest change to the plan: **motion does not come with the model.** Dance, idle and walk
clips must be sourced separately and **retargeted** onto each figure's own skeleton.

Retargeting requires a semantic bone map, so this promotes the map from "nice vocabulary" to **the load-
bearing artifact of the whole feature** — and it makes phase 4 substantially larger than drafted, since
it now includes an animation *source* and a retargeting step rather than "play clip N".

**3 — Rig conventions differ across four models from adjacent sources.** Rigify, Daz Genesis, and a
custom scheme shared by the two DOA ports. A single convention table would have covered two of four.
This is the layered pipeline's justification, measured rather than argued.

Happily, the DOA scheme (`root hips`, `pelvis`, `leg left thigh`, `leg left knee`, `arm right shoulder`)
is plain English with spaces — it matches **no** convention table and is close to ideal for LLM labeling.
The two failure modes are complementary, which is the best case for a layered design.

**4 — Grace's morph data is ~115 million vertex deltas, roughly 1.4 GB.** One mesh carries **876 shape
keys**; three others carry 300–470. These are Daz **JCMs** (joint-corrective morphs — `pJCMAbdomenFwd_35`,
`pJCMChestSide_24_L`) that Blender *drivers* fire from bone rotations to fix deformation at joints.

glTF has no drivers, so exporting them produces a gigabyte of data that nothing will ever evaluate.
**They must be stripped at conversion** — and the resulting loss of joint correction is a visual-quality
tradeoff to look at on device, not a free win.

**5 — Rig control apparatus does not survive, as predicted, and it is most of the file.** Eve carries 299
constrained pose bones and **175 `WGT-` widget meshes** (control shapes for the rig UI — 96 % of its 182
objects); Grace 249 constrained bones and driver-suffixed `(drv)` duplicates. Constraint types across the
set: `IK`, `STRETCH_TO`, `DAMPED_TRACK`, `LOCKED_TRACK`, `COPY_TRANSFORMS`, `LIMIT_*`.

None of it survives glTF. **Widget meshes must be stripped or they arrive as scene clutter**; control
bones become inert deadweight in the skin.

**6 — Multiple armatures per file.** Hitomi has **five** — one body rig plus four separate hair skeletons
(loose, loose-with-headband, ponytail, twin braids), one per hair variant. Leifang has four, Grace three.
glTF supports multiple skins, but the runtime must know **which armature is the humanoid one**. Cheap
heuristic: the skin the body mesh is bound to, cross-checked against bone count. Worth noting the hair
rigs are exactly what spring-bone secondary motion would drive later.

**7 — Bone counts are 482–796, and that is a rendering concern.** three.js falls back to bone *textures*
past the uniform limit, so it works, but it is not free. Most of those bones are face, cloth and detail
rigs. **Bone reduction at export is likely necessary** and needs a policy — which is another thing the
humanoid map enables, since it identifies what must be kept.

**8 — Everything is already life-size and metric.** All five: `unit_system=METRIC`, `unit_scale=1.0`,
with bone Z extents of 1.61–1.89 m. The feared centimetre-scaling problem does not appear here.

But this **confirms the `TARGET_SIZE_M = 1.8` concern rather than relieving it**: these models arrive
correct and the current placement path would actively *damage* them, squashing Eve (1.89 m) and stretching
Grace (1.61 m) to an identical 1.8 m — discarding a real, authored, meaningful height difference.

**9 — Vertex counts are 117 k–526 k for a single figure.** Against `Budget.maxTris = 500_000` for the
*entire world*, one Grace is already over budget before a room is drawn. Decimation is not optional at
these sizes, and hair dominates: Eve's two hair meshes are 116 k of her 154 k, and only one is worn.
**Stripping unworn outfits and unused hair is the single biggest lever**, and it is exactly what the
collection structure in finding 1 tells us how to do.

### What this implies for conversion

Conversion is not a format change — it is an **aggressive stripping pass**, and it is where nearly all the
size goes away:

| Strip | Why | Measured |
|---|---|---|
| `WGT-` widget meshes | rig UI, not content | 175 of Eve's 182 objects |
| control / `(drv)` / `MCH-` bones | no constraints or drivers in glTF | ~60–300 bones per rig |
| JCM shape keys | driver-fired; nothing evaluates them | ~1.4 GB on Grace |
| unworn outfit collections | one outfit is worn at a time | 65 of Hitomi's 88 meshes |
| unused hair variants | one is worn | 116 k of Eve's 154 k verts |

**Open: one GLB per outfit, or one GLB with everything and runtime visibility?** The whole outfit feature
wants the second; size wants the first. A middle path — export the worn set plus a chosen few alternates —
is probably right, which makes outfit selection partly a *conversion-time* decision. Needs deciding before
phase 1.

---

## Phase 0 — run on device 2026-09-01 (Grace Ashcroft, "New Wetness Lightweight")

Converted, imported, placed in the **Meadow** world, and viewed on the Quest. Verdict: **"the model
displays pretty well."** Everything structural worked on the first attempt; four defects found, three of
them in code that already shipped.

Conversion: `.blend` → GLB in ~20 s, `scripts/blend_to_glb.py`. Result: 16 meshes, 2 skins (482 + 42
joints), 348 k tris, 0 morph targets, 0 animations, single scene root.

### Size: 229 MB → 36.9 MB, and geometry was never the problem

The first export was 229 MB, of which **210 MB was textures** — 4K PBR maps authored for offline
rendering. Downscaling to 1024 px took 2462 → 181 Mpx (7 %) with no visible cost at VR distances:

| | first export | 1 K textures |
|---|---|---|
| total | 228.9 MB | **36.9 MB** |
| images | 210.2 MB | 18.2 MB |
| geometry + rest | 18.7 MB | 18.7 MB |

Blender's glTF exporter re-encodes but will **not resize**, so the scaling happens on the loaded
datablocks before export. This is the single biggest lever in conversion, and it is not the one the
first draft of this document predicted — that draft expected geometry and morphs to dominate.

### She is life-size, and placement shrank her to 53 %

Measured by round-trip render: **1.757 m, feet at z = −0.013.** Correct as authored.

`/place_cached_asset` gave her `scale: 0.532` — she would have rendered **0.93 m tall**, a child-sized
doll. Two compounding causes:

1. `TARGET_SIZE_M = 1.8` normalization, as predicted in finding 8.
2. **`trimesh`'s bbox is wrong for skinned meshes** — *not* predicted. It reported **3.369 m** against
   Blender's 1.757 m, because it applies node transforms without applying skinning. `_normalize` divides
   by that number, so the error is ~2×, not the ~2 % normalization alone would cost.

Cause 2 is **a bug in the shipped import path**, not a figures-only concern: `ModelImporter` records this
bbox for every model, and `_normalize` trusts it. It is invisible for static props — which is exactly why
nothing has caught it — and it will mis-scale *any* rigged model.

### Face and hands render grey — a shader mix glTF cannot carry

Reported on device as "the face and near face area is gray, but other colors seem correct." Diagnosed
exactly: `FACE` exported with `baseColorTexture: null` and `baseColorFactor: [0,0,0,1]`, which the scene's
ambient + directional lights lift to grey.

The cause is upstream of the exporter. The material's surface output is a **`MIX_SHADER`** blending a dry
and a wet skin shader. A glTF material is *one* PBR metallic-roughness definition and has no
representation for a mix of two shaders, so the exporter cannot resolve a base colour and emits black.
`Jacket_Leather-1` and `BODY---torso` both exported their textures correctly — it is specifically the
layered Daz skin shader.

**The Injured variant has the identical setup**, so this is the porter's skin rig, not the "Wetness"
edition. The fix is **baking those materials to flat textures at conversion**; Blender can do it, and it
belongs in the conversion pass. Not built.

### Grabbing stutters — `_pick` is O(triangles) per frame

Reported as "grabbing and moving is manageable but not smooth, stutters a little."

`grab`'s `tick` runs `this._pick(origin, dir)` every frame, which calls
`Raycaster.intersectObject(el.object3D, true)` on every manipulable. three culls by bounding sphere first,
so it is nearly free while aiming elsewhere — but once the beam is on the figure it tests **348 k
triangles on the CPU at 90 Hz**, and for a `SkinnedMesh` three applies bone transforms per vertex, which
is costlier again. The symptom appearing *only when aimed at her* is the signature.

This is another case of **code that was correct because a value never changed**: exact per-frame
raycasting is invisible at a prop's ~5 k triangles and a cliff at a figure's 348 k. Note `_boxFor` is
already TTL-cached for exactly this reason — the box work was optimized, the raycast was not.

Candidate fixes, cheapest first, none built:

- **Box-first ordering.** `_boxPick` already computes a cached oriented-box test. Running it as a *gate*
  rather than a fallback — exact raycast only for elements whose box the ray already crosses — removes
  the cost while aiming away. It does **not** help while actually aimed at the figure.
- **Decimation.** 348 k tris is high in absolute terms; `Budget.maxTris` is 500 k for an entire world.
  Helps raycast and render together. A conversion setting, so it costs nothing at runtime.
- **Bounded raycast for figures** — a cheaper `raycast` (box-only) on figure meshes, or a BVH
  (`three-mesh-bvh`, a real dependency). Grabbing a figure by an exact triangle may not even be wanted:
  a box or a per-bone capsule is arguably the *better* affordance for a human.

### Two conversion traps found and fixed in `blend_to_glb.py`

- **Widget prefixes vary.** Eve's rig widgets use Rigify's `WGT-`; Grace's use `GZM_`. A name-prefix rule
  silently fails on the next rig, so widgets are detected **by reference** — every object appearing as a
  `pose_bone.custom_shape`. That is what a widget *is*, and it holds for any convention.
- **Collection visibility ≠ object visibility.** Grace's outfit alternates are hidden at the *collection*
  level while their own `hide_viewport` reads False, so the first export dressed her in **three outfits
  stacked**. Both levels must be checked; the porter's worn-set marking lives at the collection level.

Also recorded: Blender faults during **shutdown** on these multi-GB scenes ("Attempt to free nullptr
pointer", plus a macOS crash dialog) *after* the file is written and valid. The scripts `os._exit(0)`
once the work is done, so a teardown bug cannot masquerade as a conversion failure.

And: at rotation `[0,0,0]` she stood with her back to the viewer, confirming the +Z facing convention
(finding in [`backlogs/spaces-geometry.md`](./spaces-geometry.md)); `[0,180,0]` faces her forward.

### Fixed 2026-09-01

**Two of these are bugs in shipped code that figures merely exposed** — neither is figures-specific, and
neither could have been found with a static prop.

| Fix | Where | Scope |
|---|---|---|
| `glb_bounds()` — bounds read from the GLB's own JSON, skinned primitives using POSITION accessor min/max verbatim | `conjure/importer.py` | **every GLB**, not just figures — trimesh mis-sizes *any* skinned mesh |
| `_normalize(rigged=…)` — a figure keeps native life size; an explicit `size_m` means **height**, not largest extent | `conjure/server.py` | all rigged models |
| `_pick` box gate — the cached oriented box gates the exact raycast | `dynamics/grab/grab.js` | **all content** — any high-poly model was paying this |
| `_pick` skips triangle-testing a figure's body; the box is the affordance, HUD handles still exact | `dynamics/grab/grab.js` | figures |
| `meta.rigged` → `data-rigged` so the client can know a figure is a figure | `server.py`, `conjure-client.js` | figures |

Verified end to end afterwards: re-import gave `rigged=True height_m=1.7569 joints=[42,482]`, and
`/place_cached_asset` with **no size hint** produced `scale [1,1,1]` with feet at `y=0.0131`. No hand-editing.

Tests: `tests/test_importer.py` (+7, synthetic GLBs — the skinned/unskinned node-transform pair is the
regression that matters) and `tests/test_server.py` (+3, through the real `/library/import` path).
845 Python / 137 JS green.

### Baking, and two wrong oracles

The grey face is fixed by baking, but getting there cost two wrong answers worth recording, because both
are the same mistake in different clothes: **guessing at a condition instead of measuring it.**

**Wrong oracle 1 — "not rooted in a Principled BSDF" ⇒ needs baking.** Plausible, and wrong in both
directions. It baked 28 materials, including `GraceHair-2`, which is *also* not Principled-rooted yet
exports a perfectly good `baseColorTexture`. Baking replaced a correct texture with a worse one and
turned her hair dark.

The fix is to **ask the exporter instead of predicting it**: export once as a probe, read the resulting
GLB, and bake exactly those materials with no `baseColorTexture` and a black/absent `baseColorFactor`.
That dropped 28 → **8**, and hair and torso were left untouched. The exporter's own output is the only
oracle that cannot disagree with the exporter.

**Wrong oracle 2 — bake everything that failed.** Of those 8, seven are transparent or refractive: a
watch crystal, a spectacle lens, the eye-moisture layer, and so on. They have no diffuse colour to
capture, so a DIFFUSE bake yields black — and an opaque black lens is *worse* than the grey it replaced.
Blend mode cannot separate them: every material in this file reports `HASHED`, skin included.

So again, check the result rather than the input: **a bake that comes out black did not solve the problem
it was called for.** Those materials get a neutral base instead. Final tally on Grace: 8 probed, 1 baked
usefully (`FACE`), 7 given a neutral fallback — face correct, hair correct, lenses transparent.

The general shape, worth carrying into the discovery pipeline: *where a cheap measurement exists, measure;
a heuristic over the input is a guess that fails silently on the next model.*

### The black hands: six wrong answers and one right one (2026-09-02)

Resolved, and the resolution is a lesson rather than a line of code.

**Root cause: `bpy.ops.object.bake()` writes into the active image node of EVERY material on the baked
object**, not only the targeted ones. Grace's body mesh carries 15 materials; baking `FACE` therefore
overwrote `Grace Arms D` — whose active node was its own diffuse texture — with solid black. Every baked
export since the second was silently corrupting bystander textures. The fix is to hand every non-target
material a throwaway 4×4 destination so the bake has somewhere harmless to land.

The proof took one command and should have been the *second* thing tried, not the eighth:

| Export | Arms texture | Mean RGB |
|---|---|---|
| no downscale, no bake | 4096² | (147, 121, 112) — correct skin |
| downscaled, no bake | 1024² | (147, 121, 112) — still correct |
| downscaled **+ baked** | 1024² | **(0, 0, 0)** |

**Four hypotheses were pursued and all four were false**, each with real supporting evidence:

| Hypothesis | Why it was believable | Why it was wrong |
|---|---|---|
| the probe oracle missed the material | the hair *was* mis-detected earlier | `BODY---arms` exported a correct `Grace Arms D` reference all along |
| the bake wrote to the wrong UV layer | the active layer genuinely was the UDIM atlas | forcing a 0..1 layer changed nothing, and broke the FACE bake |
| **UDIM tiling** | the atlas is real — u 0..1 face, 1..2 torso, 3..4 arms — and arms really did sample it | glTF's default REPEAT wrapping already handles a *single-tile* material; `grace1k` rendered correct arms at u 3–4 |
| constraint dependency cycles | Blender reports them 40× per load, on `forearm.L/R` and `hand.fk.L/R` **exactly** | resolving and removing all 609 constraints changed nothing |

The UDIM one is the instructive one. Every step of the reasoning was true — the tiles exist, the material
samples them, glTF has no UDIM — and the conclusion was still wrong, because a true premise chain says
nothing about whether *this* is the cause. **A mechanism that could explain the symptom is not evidence
that it did.**

What broke the loop was comparing one artifact across three builds instead of reasoning about the
pipeline. The rule to carry forward, which is the same one that made the probe oracle work and was
abandoned exactly when it mattered most: **measure the artifact, do not reason about the pipeline.**

The UDIM shift (`--fix-udim`) and constraint stripping (`--strip-constraints`) are kept but default
**off**. Both are sound if a genuine multi-tile or cyclic case appears; neither should run speculatively,
since each perturbed the bake and cost a cycle of confusion.

### Known limitation: the black-bake fallback cannot tell a lens from a fingernail

A material that bakes black has no view-independent colour. For a spectacle lens or an eye-moisture layer
that means *transparent*, and the transparent fallback is right. For Grace's **fingernails** it means only
"the shader is too complex to resolve", and they are opaque — so the same fallback makes them see-through,
which is worse than the near-black blue they had before.

Detection is right and is kept (a near-black `baseColorFactor` with no texture is a real failure, and an
exact-zero test missed it). The *fallback* needs a signal that separates the two cases — likely the
material's actual alpha/transmission in Blender rather than the bake result alone. Not built; the shipped
conversion leaves nails faintly blue-grey and opaque, which is unobtrusive.

### Not faults at all

Two of the four things reported on device turned out to be correct behaviour, and both were confirmed by
rendering the **source** `.blend` with full material evaluation — which is now `scripts/glb_preview.py`'s
sibling job and should be the first move on any "is this wrong?" question:

- **Grace's hair is authored blonde.** The pale ash-blonde is right; only the assumption that the
  character has dark hair was wrong.
- **Saka's grey fingernails** are visible in VRoid Studio too.

### glTF cannot carry a constraint-driven control rig (2026-09-02)

**The most consequential finding of Phase 2, and it constrains what "posing" can mean per model.**

Rigify and Daz rigs separate every joint into an FK **control** a human grabs (`upper_arm.L`, `thigh.L`)
and **deform** bones that carry the vertex weights (`upper_arm.bend.L`, `upper_arm.twist.L`). In Blender a
CONSTRAINT links them. glTF has no constraints, so after export:

- rotating the **control** moves nothing — its link to the deformers is gone;
- rotating **one deformer** moves only its own segment — reported from the headset as zig-zag arms and
  hips twisting toward the knees, because `forearm.bend.L`'s parent is `upper_arm.L`, a control nobody
  is rotating. The deform chain is broken at every joint.

`export_def_bones=True` ("deformation bones only, and needed bones for hierarchy") looked like the fix and
is worse. Blender can only preserve a hierarchy that exists, and these deformers are related by
constraints rather than parenting — so it flattens every limb deformer to the armature root
(`upper_arm.bend.L <- Grace_RIG`). Measured: 482 joints → 175, spine intact, limbs unusable. Reverted,
with the reasoning kept in place so it is not retried.

**Saka poses correctly because VRM rigs are a plain FK hierarchy** — no control/deform split, so every
bone both poses and deforms. That is not a coincidence of one model; it is what the format requires.

This is a **sourcing consequence**, and it sharpens the earlier survey: a VRM or Mixamo-class rig is
posable through glTF; a Daz/Rigify control rig is not, without a conversion step that rebuilds the deform
hierarchy. Three ways forward, none built:

1. **Rebuild the hierarchy at conversion.** Walk each deform bone's constraint targets in Blender and
   re-parent the deformers into a real chain before export. Tractable — the constraint graph is right
   there in `pose.bones[].constraints` — and it would make Daz figures posable like any other.
2. **Bake poses as animation clips.** Pose in Blender, export clips, and "posing" becomes clip playback.
   Loses arbitrary posing; gains everything the (still absent) animation path needs anyway.
3. **Accept the split.** Pose VRM-class figures; treat Daz figures as static, dressed props.

### Resolved: option 1 works, and the right bones now move (2026-09-02)

**Confirmed on device across all three figures: the correct arms and legs move, and the head turns
correctly, on Saka (VRM), Grace (Daz Genesis 8) and Yuffie (Genesis 8.1).** Angles are still wrong — that
is the separate axis problem below — but bone *selection* is solved.

Two fixes did it, and the second is the transferable one.

**Conversion rebuilds the deform hierarchy from the constraint graph.** A constraint states exactly the
relationship glTF cannot carry, so it is converted into one glTF can: parenting. Every bone that deforms
**or carries deformers beneath it** is re-parented onto its constraint target — 185 bones on Grace, 206 on
Yuffie. Filtering on `use_deform` alone was not enough: rigs nest the split to different depths (Grace
control→deformer, Yuffie `upper_arm.L → upper_arm.bend.L → upper_arm.bend.twk.L`), and fixing only the
deepest level left the intermediate hanging off the wrong parent — a disconnected elbow.

This also unified Grace's two parallel chains. `thigh.fk.L` now drives the thigh deformers via `thigh.L`,
the shin via `shin.fk.L`, and the foot below that: one master chain with the deform chains hanging off it.
**The legs became correct with no inference change at all.**

**Inference now uses anatomical boundaries, not topological ones.** Every remaining failure was the same
mistake — defining a boundary by where nodes sit in the tree, when the tree is exactly what conversion
rewrites:

| Boundary | Was | Now |
|---|---|---|
| where an arm ends | "walk up to the spine path" — collapsed both Daz arms to the armature root once re-parenting moved the shoulder off it. A laterality threshold was worse: any fraction keeping a shoulder on one rig cuts above it on another. | **where the two arms meet.** The common ancestor of the hands is the upper torso by definition. Exact, no threshold. |
| where the spine starts | strict `hips → head` path — returned nothing after re-parenting and silently dropped spine, chest, neck AND head together | the lowest joint hips and head share |
| `validate()`'s hips check | required hips to be an ancestor of the head — a legitimate casualty of re-parenting, so it was **rejecting correct maps** | ancestry of the FEET, which is what makes hips the root of a body |

> **The through-line, after four rounds of getting it wrong: prefer boundaries anatomy defines over
> boundaries the skeleton graph happens to have.** Conversion rewrites the graph; it does not rewrite the
> anatomy. This is the same insight that made the naming layer work, relearned for topology.

**What it cost, recorded because the pattern repeats.** Bone selection took roughly a dozen attempts, and
the failure modes were consistent enough to name:

- **A rule right for identifying a bone was wrong for using one, four separate times.** Deform weights are
  the correct filter for picking extremes, the wrong filter for walking a path, and the correct filter
  again for choosing what to rotate. Structure tells you *which joint*; it never tells you *what to do
  with it*.
- **Four selection criteria in a row each fixed one limb and broke another** (deform-ness, depth, subtree
  reach, ancestor-guarded reach). That oscillation is the signature of fitting noise, and the answer was
  never a fifth criterion — it was to stop using graph statistics as a proxy.
- **Three fixes shipped that the headset never ran**, because `figure.js` had no cache-bust stamp. The
  signal was there and misread: after the first fix the client logs went *completely silent*, which is
  not neutral — a component that runs and fails says so. Now every `/static/*.js` is stamped by one regex.
- **The bug that finally mattered was found by reading three's source rule**, not by inferring it from
  behaviour: `sanitizeNodeName` REMOVES `[ ] . : /` rather than replacing them, so `upper_arm.bend.L`
  becomes `upper_armbendL`. The first guess was plausible, and plausible is what kept it alive two rounds.

### The axis problem — posing needs an anatomical frame

Separate from the above and true even for Saka, whose bones move correctly. `pose_figure` took euler
degrees in each bone's OWN local space, and a bone's rest orientation is whatever its rigger chose. So
`[0, 0, -70]` flexed one figure's hip and swung another's leg backward, and "raise her legs" put them
behind her.

Semantic bone NAMES were solved; semantic AXES were not. The fix is the same move that solved names —
measure the structure instead of assuming a convention.

### Resolved: the anatomical frame, and a second bug hiding behind it (2026-09-02)

**Verified in render across all three rigs: the same request produces the same motion on Saka (VRM),
Grace (Daz Genesis 8) and Yuffie (Genesis 8.1).** `{"leftUpperLeg": {"bend": 45}}` raises the knee
forward on each; `{"spread": 60}` on both upper arms raises them symmetrically; `bend: +40` and `-40` on
the two legs gives a stride.

**There were two defects, and only one of them was the axis convention.** The other is the more
instructive:

> **A pose was REPLACING each bone's rest rotation, not composing onto it.** `bone.rotation.set(x,y,z)`
> discards whatever the rigger authored. Measured: `thigh.fk.L` rests **177 degrees** from identity on
> Grace and Yuffie, `upper_arm.fk.L` **137**, `foot.fk.L` **72** — against **6** on Saka. So the leg was
> flipped upside-down *before* the requested angle was added, and `clear` left it flipped.

That spread explains why the symptom read as an axis-convention problem: Saka's rest rotations are small
enough that only the axes looked wrong, while on the Daz rigs the rest pose was being thrown away
wholesale. One measurement — print the rest quaternion of every mapped bone — would have separated them
on day one, and it is the same lesson as the black hands: **measure the artifact, do not reason about
the pipeline.**

**The frame itself.** Three rotations per bone, measured from the bind pose (`figures.anatomical_axes`),
each an axis chosen so a POSITIVE angle produces the named motion on either side of the body:

| | Motion | Axis | Degenerate when |
|---|---|---|---|
| `bend` | the far end swings **forward** — lift a knee, bend an elbow, bow the spine | `direction × forward` | the bone already points forward (a foot) — falls back to `direction × up`, so bend lifts the toes |
| `spread` | the far end swings **outward**, away from the midline | `direction × outward` | the bone already points outward (a T-posed arm) — falls back to `direction × up`, so spread keeps raising it |
| `turn` | the bone twists about its own length, **inward** | the bone's own direction, negated on the right | never |

`direction` is the vector to the next mapped joint down the chain, and a bone at the end of a chain
CONTINUES the direction it arrived on — a hand points the way the forearm did. `forward` and `up` come
from `body_frame()`, which measures them (hips→head, and the vector between the paired hip joints)
rather than assuming glTF's +Z. Every sample model comes out at +Z anyway, which is the point: measuring
costs three subtractions and cannot be wrong about a rig that stands a few degrees off axis.

Three properties fall out of this, and each is load-bearing:

- **Mirror symmetry is by construction**, from one sign per side. The same numbers on `leftUpperArm` and
  `rightUpperArm` produce mirrored motion, so a symmetric pose is symmetric without a caller thinking
  about it. `bend` is deliberately NOT mirrored — flexing both hips moves both knees the same way.
- **The axes are stored in each bone's PARENT frame**, which is the frame its own local rotation lives
  in. Applying one is then a single multiplication onto the rest quaternion (`delta * rest`) and the
  result rides the parent chain for free: bend a hip and then a knee and you get a leg. Model-space axes
  would need re-deriving against a live scene graph, which is the class of mistake that produced the
  double-counted selection box.
- **The durable state stays semantic.** The `figure` component persists `{"leftUpperArm": {"bend": 45}}`,
  never quaternions, because that is what a reload replays and what a persona will later reason about
  (§ *Keep the state semantic*). The axes ride alongside as derived data, measured once at import.

**Where it lives.** `importer` → `attributes.humanoid_axes` (a property of the file, measured beside the
map) → `meta.humanoid_axes` at placement → the `figure` component's `axes`, which the client resolves
against. A model catalogued before the field existed gets its axes measured on first placement and
written back, so nothing needs re-converting. Composition order is turn, then bend, then spread — twist
innermost, the swing-twist decomposition — mirrored exactly in `figures.resolve_pose` and `figure.js` so
a Blender render and a headset agree.

**Verification.** `scripts/pose_test.py` now takes the anatomical vocabulary and drives it through the
real `anatomical_axes`/`resolve_pose`, so what it renders is what a headset is sent rather than a second
implementation that could agree by luck. It writes **front and side** views, always: a front view cannot
tell a raised knee from a leg swung backwards, which is the exact error this exists to catch.

```
$B --background --python scripts/pose_test.py -- out.glb outdir/ \
   '{"leftUpperLeg": {"bend": 45}, "rightUpperArm": {"spread": 70}}'
```

Raw euler on a named rig bone still works as an escape hatch (`{"upper_arm.fk.L": [0,0,-60]}`) — it is
how several wrong maps were caught — but it is no longer a pose the tool surface accepts, because it
meant a different motion on every figure.

Tests: `tests/test_figures.py` (+12, forward-kinematics checks that a posed bone moves the joint below
it in the named direction), `tests/test_server.py` (the posing block rewritten in the new vocabulary),
`tests/js/figure.test.js` (+8, new — the rest-composition claim, on a bone with a deliberately
non-identity rest rotation, since an identity one lets the broken version pass).

**Still to verify on device**, since a render is not a headset: that a pose survives a reload and a late
joiner, and that the axes riding the component are not too chunky in practice (6 KB for Saka's 52 bones,
1.5 KB for Grace's 21).

**On using a multimodal model for this** (asked 2026-09-02): the right role is *verification*, not
search. Trigonometry gives the axes exactly, and vision would be a slow, fragile way to hunt for them.
But rendering a posed figure and asking "is this a raised arm or a dislocated shoulder?" is precisely
the check that caught the head-mapped-to-a-hair-bone error, which `validate()` had passed as clean —
and now that the frame exists there is something concrete to verify: drive a fixed set of poses
(`bend 45` on each limb, in turn) and ask whether each looks like the motion it names. That is a
buildable pass, and it is what the two remaining known-wrong cases below would have been caught by.

### Device run 2026-09-03 — the geometry is right and the vocabulary is not

All three figures re-imported to their neutral poses, are posable through the director, survive a late
joiner, and still `grab` correctly once posed. **"Turn her head right" and "raise her left/right leg" are
correct on Saka, Grace and Yuffie.** The arms are not: *"raise her right arm up"* points the arm **behind
her** on all three.

The cause is not in the axes. It is in the layer above them, and the log says so outright:

```
"raise yuffi's right arm up"    -> rightUpperArm {bend: -90, spread: -90}
"raise grace's right arm up"    -> rightUpperArm {bend: -60}
"raise saka's right arm up"     -> rightUpperArm {bend: -80}
"point Grace's right arm down"  -> rightUpperArm {bend: -90}
"point Saka's left arm up"      -> leftUpperArm  {bend: -90}      <- identical, opposite intents
```

`bend: -90` **means** "swing the far end backward", so the figures did exactly as told. The fifth line is
the finding: the director emitted the same numbers for *up* and for *down*. It is guessing, and it cannot
do otherwise, because **a relative vocabulary asks the caller to know the rest pose**:

| rest direction of… | Saka | Grace | Yuffie |
|---|---|---|---|
| upper **leg** | −89° | −81° | −82° |
| upper **arm** | **0°** (a true T-pose) | **−48°** | **−46°** |

The three rigs agree about where a leg starts to within 8°, so `bend: +80` reads as "raised" on all of
them — **the legs worked by luck, not by correctness.** The arms differ by 48°, and "up" is 90° away from
rest on Saka and 138° on Grace. No single relative number can mean "up" for an arm.

A second failure sits in the same log, unreported because it was buried in a busy pose. Asked to *"spread
Grace's legs slightly apart"*, the director first called `inspect_figure`, **read** the line saying
`spread` is positive outward, and then emitted `spread: +20` on the left and `-20` on the right — both
legs toward her left. The mirror-by-negating reflex beat the description it had just read.

> **The vocabulary is the product — and "the vocabulary" now means the mapping from an utterance to
> numbers, which is the one layer in this feature with no test at all.** Bone names are checked by
> `validate()`, axes by rendering. What the director does with them is checked by a person in a headset,
> once, by hand.

Also worth recording, because it shapes the fix: the deterministic path (`curl /figure` with an explicit
pose) exists and **goes unused** — it is too clumsy in practice. So the guard on this layer has to be
automated; it will not be a habit.

The three slices that follow from this are designed under
[*Aiming, evaluation and named poses*](#aiming-evaluation-and-named-poses).

### Still open from Phase 0

- ~~Dark, pointed hands.~~ **Fixed 2026-09-02** — see above. (The "pointed silhouette" was an artifact of
  reading shape into a black mass; the geometry was never wrong.)
- ~~Whether the raycast change removes the stutter.~~ **Confirmed fixed on device 2026-09-02.** Grabbing a
  348 k-triangle figure is now smooth. **Decimation is therefore unnecessary** — the cost was never the
  GPU, so the quality/size trade never has to be made.
- Baking costs ~3 min per conversion, most of it Cycles. Fine at this cadence, worth noting.

### VRM: discovery layer 1, working (2026-09-02)

A VRoid Studio export (`Saka.vrm`, 16 MB) imported with **no conversion at all** — a `.vrm` *is* a GLB, so
it is now a first-class import extension, stored as `.glb` so the client needs no special case.

It carries `VRMC_vrm` with **54 humanBones stated outright** — `hips → J_Bip_C_Hips`, `leftUpperArm →
J_Bip_L_UpperArm`, down to every finger joint — plus `VRMC_springBone`. `vrm_humanoid()` extracts it
(both 1.0's dict form and 0.x's list form), storing node **names** rather than indices so the map survives
a re-export that reorders nodes, alongside `humanoid_source: "vrm"` so a stated map and an inferred one
can be trusted differently.

| | Grace | Saka |
|---|---|---|
| tris | 348,027 | **27,266** |
| joints | 482 | 83 |
| height | 1.757 m | 1.545 m |
| humanoid map | must be inferred | **stated** |
| conversion | ~3 min Blender pipeline | none |

**Saka is now the pipeline's control**, and that is worth more than the model itself: she came through
clean, which is what proved Grace's black hands were a conversion fault rather than an importer or client
one. She also gives Phase 1 a **labelled example** — inference (layer 2) can be built and checked bone-by-
bone against a known-correct 54-entry answer before being pointed at Grace's 482-bone rig, where nothing
states the truth.

Caveat: VRM materials are **MToon** (`VRMC_materials_mtoon`, all 12 of Saka's carry it). A-Frame's plain
glTF loader ignores that and falls back to `KHR_materials_unlit`, so she renders flatter than in VRoid
Studio. The MToon data is already in the file — shade colours, rim lighting, outline, matcap — so toon
rendering is reachable later without re-exporting anything. Re-importing the `.vroid` project would gain
nothing: it is a ZIP holding a protobuf *recipe* (base model `N00`, slider values) and **no geometry at
all**, cookable only by VRoid Studio.

### The selection box was the same bug as the bbox, on the client

Reported as "about twice as tall as she is". `_localBox` folded `mesh.matrixWorld` into each geometry's
bind-space bounding box — but a skinned mesh's *node* frequently hangs off a bone (Grace's hair is
parented to `head`, ten bones up the spine) while its vertices are already in skin space, so the whole
skeleton chain got counted twice.

Rather than fight three's bind matrices client-side, the server ships the bounds it already computes
correctly at import as `meta.bbox` → `data-bbox`, and `grab` uses them directly. Exactly the same
insight as `glb_bounds()` on the Python side, arrived at independently a day later — which suggests the
underlying rule deserves stating once, loudly: **never derive a skinned mesh's extent from the scene
graph.**

### Is any of this Grace-specific?

Asked directly during the work, and worth recording because it shaped the design. Almost none of it, and
deliberately so: **each time something turned out to be Grace-specific, the fix was to find the underlying
invariant rather than special-case it.**

- Widget detection asks what a widget *is* (`pose_bone.custom_shape`) rather than what it is called,
  because Eve's are `WGT-` and Grace's are `GZM_`.
- Visibility checks both object and collection level, because Hitomi marks the worn set at one and Grace
  at the other.
- `glb_bounds` encodes *why* a skinned vertex needs no node transform, so it holds for any exporter.

The only Grace-specific input is the `--collections` argument, which is per-model **by design** — choosing
it automatically is what the discovery pipeline is for.

**The caveat that matters:** all of it is validated on **one source family** (Daz-derived Open3DLAB /
SmutBase ports). The widget and visibility rules survived two conventions because Eve and Grace disagreed;
baking has met exactly one model. The VRoid and MakeHuman fixtures proposed under *Sourcing* are the cheap
way to learn whether these generalize or are overfitted to one network's habits.

---

## Conversion recipes for the sample models

The `--collections` argument is per-model and is the one piece of genuinely manual input in the pipeline
(choosing it automatically is what the discovery layers exist for). Recorded so it need not be
rediscovered. Run `--list` first on anything new.

```
B=/Applications/Blender.app/Contents/MacOS/Blender
S=temp/3d-model-examples

# what a file offers, before choosing
$B --background "<file>.blend" --python scripts/blend_to_glb.py -- --list

# Grace Ashcroft (Daz Genesis 8) — dressed, then body-only for joint work
$B --background "$S/Grace_Ashcroft_-_1_3a_New_Wetness_Lightweight_wTextures/Grace Ashcroft - Gen Rig.blend" \
   --python scripts/blend_to_glb.py -- out.glb --max-texture 1024 --bake 1024 \
   --collections "Grace_Ashcroft_APPEND,Eyes,Default,Hair 2,Accessories"
   #  body only:  --collections "Grace_Ashcroft_APPEND,Eyes"

# Yuffie (Daz Genesis 8.1)
   --collections "Yuffie,Hair,Original Clothing"      # dressed
   --collections "Yuffie"                             # body only

# Saka (VRoid) — .vrm needs NO conversion; import it directly
python -m conjure.importer temp/3d-model-examples/Saka.vrm

# render checks
$B --background --python scripts/glb_preview.py -- out.glb outdir/ --size 640
$B --background --python scripts/pose_test.py  -- out.glb outdir/ '{"leftUpperArm":{"bend":60}}'
#  raw escape hatch, a rig bone by its own name:  '{"upper_arm.fk.L":[0,0,-60]}'
```

`--max-texture 1024 --bake 1024` are the settings every conversion has used: 1 K textures are the single
biggest size lever (229 MB → 37 MB on Grace), and baking is what recovers materials whose base colour the
exporter cannot resolve. `--fix-udim` and `--strip-constraints` exist and default OFF — both were fixes
for problems that turned out not to exist, kept only for a genuine future case.

Eve, Hitomi and Leifang were inspected but never converted. Eve is Rigify (a third convention);
Hitomi and Leifang share a custom DOA scheme with space-separated bone names and 14/11 outfit
collections each — the best available test of outfit switching when that lands.

## Sourcing figures

*Surveyed 2026-09-01. Prices, licences and URLs change — re-verify before relying on any of it.*

The measurements above make the sourcing question an **engineering** one, not just a taste one. Four
models from adjacent sources produced three rig conventions, no animations, and a 4.5× spread in vertex
count. Nearly all of the import pipeline's cost exists to absorb that heterogeneity.

> **A single-source base figure would collapse most of this document.** One constant skeleton means the
> humanoid bone map is a fixed constant rather than a per-model discovery problem, retargeting is authored
> once instead of per figure, and morph and outfit naming follow one known system. Layers 1–5 of the
> discovery pipeline largely evaporate.

The counterweight is that generated figures do not look like specific characters, which is exactly why one
would go to the ports in the first place. So this is unlikely to be either/or — but it is worth knowing
that *every figure from a new source is a fresh discovery problem, and every figure from a known base is
nearly free.*

### Parametric base figures — one rig, many characters

| Source | Where | Cost | Licence |
|---|---|---|---|
| **MakeHuman / MPFB2** | [makehumancommunity.org](http://www.makehumancommunity.org/) · addon: [extensions.blender.org/add-ons/mpfb](https://extensions.blender.org/add-ons/mpfb/) | free | **core assets CC0** |
| **Daz Genesis 9** | [daz3d.com](https://www.daz3d.com/) — *Genesis 9 Starter Essentials* free with Daz Studio | base free | **see the licence problem below** |
| **MB-Lab** | Blender addon, community forks on GitHub | free | GPL/AGPL |

### The Daz licence problem — the reason not to standardize there

Worth recording in full, because Daz is otherwise the **best technical fit**: Grace already uses this rig,
the character ecosystem is enormous, and the Blender bridge is mature.

Daz's **Interactive License** is required to use their 3D content "in video games or other applications" —
specifically when an application *uses the 3D data* rather than only publishing 2D rendered images. Conjure
is exactly that case: a real-time application loading the mesh. Two consequences:

- The **free** Genesis 9 Starter Essentials carries only the **base** licence, not an interactive one.
- Per product, **the creator chooses whether an Interactive License is even offered.** Where none is
  offered, real-time use is not available at any price.

Private, personal, never-distributed use is a question to read the EULA on rather than take a summary of —
but Conjure has a **public-world mode that auto-publishes referenced assets**, which lands squarely in the
interactive case. That makes Daz a poor foundation to standardize on here, despite fitting best technically.

**This reverses an earlier lean toward Daz** recorded in conversation before the licence was checked.

### MakeHuman's licence, precisely

The cleanest position of anything surveyed, and worth stating exactly rather than as "it's free":

- **All core assets — base mesh, targets, skins — are CC0.** No attribution, no fee, derivatives fine.
- The **addon code** is AGPL; that governs the code, **not** characters you export. MPFB-generated
  characters may ship in closed-source projects.
- Exports are nominally AGPL, but a user producing them with an **unmodified official build** may elect
  CC0 instead.
- **Third-party community assets may carry other licences** — that obligation is the user's, so per-asset
  licence still needs capturing at import.

### The network already in use

[Open3DLab](https://open3dlab.com/) (general purpose), [SmutBase](https://smutba.se/) (adult sister site),
and [SFMLab](https://sfmlab.com/) (Source Filmmaker, founded 2014) are one operation sharing
infrastructure. All four sample models bear its hallmarks — Blender collections as outfit slots, hidden
unworn sets, `.blend` distribution.

That consistency is a real argument for staying within the network: **layer 0 works because these files
share authoring conventions**, and it should keep working across the site. Licences are **per model**, set
by the uploader, and not stated centrally — so they must be captured at conversion, per model.

Note that game-character ports are derivative works of copyrighted assets regardless of what licence an
uploader attached. See *Licence capture* under the conversion section; this matters at the moment a world
is made public, not before.

### Other sources considered

- **Nexus Mods body replacers** (CBBE, BHUNP, 3BA) — anatomically detailed, mature rigs, large outfit
  ecosystems on consistent base meshes. Mod permissions are per-author and frequently restrict
  redistribution, which is a worse licensing position than the network above, not a better one.
- **Renderpeople / 3D Scan Store** — a few free scanned samples. Photoreal, but clothed and unrigged or
  lightly rigged. Not a fit.
- **Reallusion Character Creator, Human Generator** — paid; not evaluated.

### Animation sources — a separate problem

Finding 2: the models ship none. Clips must be sourced and retargeted.

- **[Mixamo](https://www.mixamo.com/)** — free with an Adobe account, large library, auto-rigger. Its bone
  naming is already a layer-1 convention, so its output is self-describing.
- **Daz motion packs** — native to Genesis rigs, so no retargeting *if* the licence issue above were
  resolved.
- **VRMA** — the VRM animation format, if the VRoid path is taken.

### Proposed acquisitions — two test fixtures

Cheapest way to test Phase 0 against the **full range** of what the pipeline must survive: perfect
convention, no convention, and everything between. Both free, neither dependent on any open decision.

**1 — One VRoid Studio export** — *the unambiguous-rig fixture.*

- **Where:** [vroid.com/en/studio](https://vroid.com/en/studio). Free, no paid tier. Windows/macOS via
  Steam, also iPad.
- **Why:** exports **VRM** (both 0.0 and 1.0 since v1.20.0), which carries an *explicit*
  `VRMC_vrm.humanoid.humanBones` map. It is the one input where layer 1 is guaranteed to succeed, which
  makes it the fixture that proves the VRM path works before we trust it on anything ambiguous. Also
  exercises spring bones — the hair-rig secondary motion the DOA models will want later.
- **Licence:** the creator sets their own terms of use for what they make, so a self-made avatar is
  unencumbered.
- **Caveat:** stylized/anime, not photoreal. This is a **pipeline fixture**, not content.

**2 — One MakeHuman/MPFB2 figure** — *the CC0 parametric fixture.*

- **Where:** MPFB2 installs **inside Blender** — Preferences → Extensions → search "MPFB" → Install
  ([extensions.blender.org/add-ons/mpfb](https://extensions.blender.org/add-ons/mpfb/)). Needs Blender
  ≥ 4.2; **5.2.0 LTS is installed here, so this works as-is.** Manual builds:
  [static.makehumancommunity.org/mpfb/downloads.html](https://static.makehumancommunity.org/mpfb/downloads.html).
- **Why:** answers whether a single-source strategy is viable *aesthetically*, which is the only part of
  the strategic question that cannot be settled on paper. Generating in Blender skips the conversion hop
  entirely, so it also isolates the extraction stage from the conversion stage during Phase 1 debugging.
- **Licence:** CC0 core assets — the cleanest available, and the only source here with no public-world
  concern at all.

Together with the four ports already on disk, that gives six figures spanning Rigify, Daz Genesis, two
custom game rigs, VRM, and MakeHuman — enough to know whether the discovery pipeline generalizes or is
being overfitted to one network's conventions.

---

## The conversion front door — headless Blender

`.blend` is essentially Blender's internal memory dumped to disk. There is no third-party reader worth
trusting and no browser will ever load one, so something must convert. The right something is Blender
itself, run headless:

```
Blender --background model.blend --python export_glb.py
```

### It is a universal front door, not a `.blend` workaround

Downloads arrive as `.blend`, `.fbx`, `.dae`, `.obj`, `.vrm`, `.glb`. Blender imports all of them. One
headless step normalizes the lot into the single format the client already renders — which is a much
stronger position than "please export to GLB first," and it is the reason to build this rather than push
the work onto the user forever.

It also puts the **export settings under version control** instead of in someone's memory: +Y up, apply
modifiers, texture size caps, and — critically — **in-place vs. baked root motion** (see *root motion*
below). A script makes that reproducible; a checkbox does not.

### Blender is free — there is no limited edition

Worth recording because it came up as a planning concern. Blender is entirely free and open source under
the GPL. There is no paid tier and no feature gating: the full Python API, `bpy.ops.export_scene.gltf`,
and `--background` are all in the stock build. The paid product people encounter is **Blender Studio**
(`cloud.blender.org`) — a subscription for training courses and production `.blend` files, not software.
**Nothing planned here is limited by using the free build.**

Installed here: **Blender 5.2.0 LTS**, at `/Applications/Blender.app/Contents/MacOS/Blender` — note it is
**not on `PATH`**, so the path needs to be a config setting with a `doctor` row rather than a bare
`which blender`.

### If Blender is absent

Relevant for a second machine, CI, or a future NAS scanner. The honest answer is that **for rigged
humanoids there is no adequate substitute**:

| Option | Covers | Why it is not enough |
|---|---|---|
| Require GLB/glTF only | the glTF family | zero cost — but `.blend` is impossible and the user exports by hand forever |
| `trimesh` (already a dependency) | OBJ, STL, PLY, some glTF | **meshes only — no skeletons, no skinning, no animation.** Disqualifying. |
| `assimp` via `pyassimp` | FBX, DAE, OBJ, many | brittle bindings; skinning/animation fidelity is patchy and version-dependent |
| `pygltflib` / raw JSON | glTF/GLB | fine for *reading* what we already have; converts nothing |
| **headless Blender** | everything, with real rig and animation fidelity | a ~3 GB application dependency, not on `PATH` |

Hence the split Daniel called: conversion is an **optional prerequisite step** requiring Blender; import
proper requires only a GLB and stays dependency-light. A machine without Blender imports GLBs fine — it
just cannot convert. Blender is therefore a soft dependency of **one CLI**, never of the world server.

`importer.py` deliberately carries no server dependency (stdlib, Pillow, lazy trimesh), so shelling out to
a 3 GB application does not belong inside it. Conversion wants its own module, called by the
`conjure-import` CLI *before* upload.

### Keep the original

Content-address and keep the source file alongside the GLB. Re-importing with better export settings must
never mean re-downloading — and for Open3DLAB in particular the **source page is the only place the licence
and attribution exist**. The importer already has `licence` / `attribution` / `creator` fields
(`ImportResult`); they need populating at conversion time, when that context is still in hand.

### What glTF does *not* carry

Expected to bite with Open3DLAB and Blender-native rigs. glTF transports the **deform** skeleton, skinned
meshes, morph targets, and *baked* animation. It does **not** carry IK constraints, drivers, bone
constraints, custom bone shapes, or bone layers.

Mostly fine — the deform bones are what we want. But a model whose posing story is "use the IK control rig
in Blender" arrives as a bare FK skeleton, so any pose must be authored as FK bone rotations, or baked into
a clip during conversion while the control rig still exists. **That is a conversion-time decision, and it is
irreversible afterwards.**

---

## The discovery pipeline

The architecture, in one line:

> **Import is slow, expensive, LLM-assisted, and human-confirmed once. It produces a durable artifact in
> the catalog. Runtime is pure data lookup and never calls an LLM.**

Everything below follows from that. The generous per-model budget is what makes it affordable, and the
"never in bulk" constraint is what makes a human confirmation step reasonable rather than a treadmill.

Five layers, cheapest first, each falling through to the next.

### 0 — Read what the porter already authored (free, exact, measured)

**Blender collections and visibility flags.** Added after measuring the sample models, where this alone
recovers the outfit vocabulary for three of four — semantically named, human-authored, no inference. Also
the armature/mesh binding graph (which skin is the humanoid one), object naming prefixes (`WGT-`, `DEF-`,
`MCH-`, `(drv)`), and unit settings.

The general principle is worth stating because it will keep paying: **before inferring anything, read what
a human already wrote down.** A porter who grouped meshes into `Mandarin Dress` and `Bunny Outfit` did the
semantic work already; spending a vision model to rediscover it would be both slower and worse.

### 1 — Known conventions (free, exact)

A lookup table of bone-naming conventions. VRM's `VRMC_vrm.humanoid.humanBones` is an **explicit** map —
when present we have simply been handed the answer. Then Mixamo (`mixamorig:Hips`), Rigify
(`DEF-`/`ORG-`), Unreal (`pelvis`, `spine_01`, `clavicle_l`), Daz, ReadyPlayerMe (Mixamo names).

**Measured hit rate on the samples: two of four.** Rigify catches Eve, Daz Genesis catches Grace; the two
DOA ports match nothing. Worth building — it is a lookup table — but it is the cheap win, not the plan.

Still not built, and now with a case waiting: a Mixamo-named character in the dev library that layer 2
cannot infer because its bind pose has the arms down (see *Ingest paths that disagree*).

### 2 — Topology and geometry (free, needs no names)

A humanoid skeleton has an unmistakable graph shape: one root; a chain rising to a leaf at the top
(spine → neck → head); two symmetric chains branching high (arms); two descending (legs). Bind-pose X sign
gives left/right; bone lengths order upper vs. lower limb. This is what Unity's avatar auto-mapper does.

Works on rigs whose names are gibberish. Fails on unusual topologies (tails, digitigrade legs, extra
spine segments, quadrupeds).

### 3 — LLM labeling (a genuinely good fit)

Given eighty bone names — possibly `Bip01 L UpperArm`, possibly `j_sk_kosi`, possibly Japanese — mapping
them onto a standard humanoid set is a **text-labeling task with a checkable answer**. That is a strong LLM
job rather than a speculative one, and the checkability (layer 5) is what makes it safe.

Same shape for grouping mesh names into outfit slots.

### 4 — Multimodal verification

Blender is already in the pipeline and renders headlessly, so images are nearly free. `conjure/captioner.py`
already establishes the seam: a pluggable `Captioner` protocol over Gemini multimodal, with a
`FakeCaptioner` for tests. The same shape extends here.

- Render front / side / three-quarter → *is it upright? which way is it facing? adult or child? how tall?*
  Facing alone justifies the pass, given the +Z scar above.
- Render **once per mesh with that mesh isolated** → *which of these is the jacket, the hair, the shoes,
  the body underneath?* This answers the outfit question **visually**, without needing mesh names to be
  sensible — which they will not be.
- Apply the candidate bone map, drive it to a **T-pose**, render → *does this look correct, or is something
  twisted?* This verifies the map itself, and is what makes the rest trustworthy.

> Synergy worth noting: a headless GLB→PNG renderer is exactly what
> [*Visual model embedding via rendered thumbnails*](./library.md) needs and does not have. Building this
> hands that item its renderer for free.

### 5 — The validator: LLM proposes, geometry disposes

**The discipline that makes the LLM safe.** A proposed bone map is checkable in pure maths against the bind
pose:

- is the proposed `hips` an ancestor of both feet **and** the head?
- is `leftHand` actually on the +X side?
- do proposed mirror pairs mirror across X within tolerance?
- are limb proportions plausible (upper arm ≈ lower arm, both shorter than the spine)?

This validator is **pure Python over the glTF JSON — fully unit-testable with no headset**, which matters
enormously: it is the one part of this feature that *can* be tested. An LLM's wrong guess should die in a
geometric assertion, not surface as a figure whose elbow bends backwards three weeks later.

The lesson from `grab`'s mode fiasco applies directly: **a silent fallback to a plausible default is not a
safe default for something nobody can see.** An unverifiable bone map must fail loudly, not degrade.

### 6 — One human confirmation

Rendered T-pose plus proposed outfit slots, shown back once; on yes, frozen into the catalog `attributes`.
This is where "not in bulk" pays for itself, and it is cheaper than any amount of additional automation.

### What gets stored

Everything above lands in the catalog's JSON `attributes` bag — the extension point `importer.py` was
explicitly designed for ("kind-specific fields ride the catalog's `attributes` bag"), so **no schema
change**. Sketch, not settled:

```jsonc
{
  "humanoid": { "hips": "<nodeName>", "leftUpperArm": "<nodeName>", … },
  "humanoid_source": "vrm" | "convention:mixamo" | "topology" | "llm",   // provenance matters
  "confirmed": true,                                                     // a human said yes
  "height_m": 1.72,
  "facing": "+z",
  "clips": [ { "name": "walk", "duration": 1.2, "in_place": true }, … ],
  "slots": { "torso": { "show": [...], "hide": [...] }, … },
  "morphs": [ … ]
}
```

Recording `humanoid_source` is deliberate: a VRM-derived map and an LLM-guessed one warrant different
trust, and the field is what lets a later bug be attributed rather than guessed at.

**A useful property of GLB:** the node tree, skins, morph targets, clip names, materials, and the VRM
extension all live in the **JSON chunk** — reachable with stdlib `struct` + `json`, no new dependency.
trimesh stays for the bbox only.

---

## Outfits — how they actually work

**Answered by measurement** (finding 1 above): in these models an outfit is a **Blender collection of
separate meshes bound to the shared armature**, with `hide_viewport`/`hide_render` marking the worn set.
Mechanism 1 below, with the grouping already authored by the porter.

The three mechanisms in general, which downloaded characters usually **mix**:

1. **Separate meshes on a shared armature** — most common. The jacket is its own mesh skinned to the same
   bones; in glTF, several `nodes` each with a `mesh`, all referencing the same `skin`. Changing outfits is
   showing and hiding meshes. *This is why outfits come before poses: it is the easiest real capability in
   the feature.*
2. **Morph targets (shape keys)** — glTF `primitives.targets` with `weights`. Mostly faces and body shape;
   sometimes used to shrink the body *under* clothing.
3. **Material variants** — `KHR_materials_variants`, or simply multiple materials. A texture swap, not a
   geometry swap.

**The gotcha: poke-through.** Show a shirt without hiding the torso beneath it and the body pushes through
the cloth. So an outfit is almost never one toggle — it is a **slot holding a set of meshes to show *and* a
set to hide**. Which is precisely why the per-mesh visual grouping pass earns its cost: it converts an
opaque list of mesh names into named slots with coherent combinations, once.

---

## Runtime — the `figure` component

### Where the motion lives

Mapped onto the existing sync tiers ([`specs/dynamics.md` §2](../specs/dynamics.md)), it falls out cleanly:

| Motion | Tier | Why |
|---|---|---|
| Dance, idle, repetitive loops | **A** | `f(sharedClock, clipName, startTime)` — zero runtime sync; every headset plays the identical clip |
| Touch / proximity reaction | **B** | broadcast the touch, each headset runs its own response — `water`'s exact pattern |
| Walking a **scripted path** | **A** | the *path* is the shared state; position is `f(clock, path)` |
| Walking driven by live input | **C** | must commit — a late joiner has to find the figure where it actually is |

That third row is the good news, and it is just *sync causes, never effects* applied again: **a walk along a
known path needs no sync at all.** Locomotion is only expensive when it is genuinely reactive.

### Not events, for the durable part

A figure that starts dancing must **still be dancing** after a reload or a peer joining, and §1's invariant
is that simulation state is shared. Events do not survive a reload, so an event-only design breaks
late-joiners.

So the durable state lives in a **`figure` component on the model entity itself** — which clip, its phase,
pose overrides, outfit selection — persisting, syncing and replaying on the existing patch/snapshot path
with no new storage model. The bus carries the tier-B reactive layer *on top*, exactly as `water` does.

### Keep the state semantic — this is what makes personas possible later

Puppets first, personas later (decided). The consequence **now**: the figure's persisted state must be
**declarative and semantic** — a named clip, a named pose, a named outfit slot — never raw bone
quaternions. When a persona later expresses intent ("look worried", "sit down"), it addresses the same
vocabulary the puppet layer already speaks. If the durable state were quaternions, the persona layer would
have to invent them and nothing would be reusable.

### Hazard: the mixer rewrites bones every frame

**The `grab` derived-frame lesson, applying to a completely different subsystem.** Something *does* rewrite
a model's sub-node transforms every frame: the animation mixer. If a pose override and a playing clip both
touch the same bone, the mixer wins every frame and the pose silently does nothing — the same failure shape
as writing a transform that `_pinSky` overwrites two seconds later.

Two ways out, to be chosen **before** writing it rather than discovered in the headset:

- poses compose *after* `mixer.update()` as an additive layer, or
- clips and poses are mutually exclusive by construction.

### Hazard: root motion

If a walk clip has translation baked into the root, the figure drifts *inside* its own entity and the
entity transform **lies about where it is** — `grab` grabs the wrong place, the plane-relative anchor is
wrong, and a peer sees it somewhere else. Standard fix is in-place clips with the entity driven separately.
This is decided at **conversion** time, which is another reason conversion settings belong in a script.

### Open: `/module` cannot attach to an existing entity

`/module` always creates or reuses **its own** entity (`server.py:4537`). There is no way to attach a
component to an entity that already exists, which is exactly what a `figure` component on a placed model
needs. Options:

- a `target` param on `/module`, or
- its own owner-gated endpoint, mirroring `/manipulate` and `/world_frame`

Leaning toward the second: the tool surface below is figure-specific anyway, and `/module`'s
create-or-reuse semantics do not fit "decorate a thing that is already there."

### A-Frame has no animation mixer

`animation-mixer` is `aframe-extras`, not core A-Frame 1.5. The repo vendors nothing but A-Frame itself, so
a small purpose-built component over `THREE.AnimationMixer` is likely better than the dependency — and it
has to be custom anyway to handle the clip/pose composition above.

---

### Device run 2026-09-03b — "add grace" gives you an unposable Grace

Reported: `add grace in front of me` → `raise her right arm` → *"this Grace model doesn't have a
poseable humanoid skeleton"*. Three separate things, and only one of them is a bug in posing.

**1. The removal worked; the director lost track of its own placement.** Three `remove_entity` calls, all
`found=true`. Sixty seconds later it read `query_world`, found the Grace it had *just placed*, concluded
the placement had failed, and placed a second one. Not a system fault — but a good fixture for the
utterance-layer harness, which would catch it as "asked for one figure, got two".

**2. `search_library` gives the director no way to choose.** Six near-identical Graces came back as bare
labels; it took the first. That one, `grace_ashcroft`, is the original 348 k-triangle conversion imported
*before* inference existed, so it carries no bone map at all. Search hits for rigged models now say
`[figure 1.76 m, 348k tris]` — the two facts that actually distinguish them.

**3. The real finding: a catalog row is a snapshot of what we UNDERSTOOD about a model, not of the
model.** The dev library holds thirteen figures and they disagree about which era of this code measured
them:

| | in the library | what it means |
|---|---|---|
| no map at all | 4 | imported before inference existed — **unposable** |
| a map today's validator REJECTS | 2 | inferred before conversion rebuilt the deform hierarchy: the forearm hangs off the armature root, so rotating the upper arm leaves it behind. **The zig-zag arm, cached.** |
| a good map, no anatomical frame | 4 | pre-2026-09-02 — poseable, not aimable |
| current | 3 | |

So placement now **re-measures anything stamped older than the current revision** (`figures.FRAME_REV`)
and writes the result back: a map is inferred if missing, RE-inferred if the stored one no longer
validates, and cleared rather than kept if nothing valid can be recovered. A stated (VRM) map is never
second-guessed. Dry-run over the live catalog: four gain a map, two are repaired, one is kept as stated,
six are already current. Nothing needs re-converting or re-importing.

> A version stamp rather than "does this row carry the keys today's code needs", because the check that
> mattered was not a missing field — it was **the validator getting stricter**, which no key can express.

**And the validator gained the check that makes that possible.** `validate()` asked where joints ARE:
sides, vertical order, ancestry of the feet, limb proportions. It never asked whether **moving one moves
the next** — so a map whose forearm is not below the upper arm passed as clean, which is exactly how the
zig-zag shipped. A limb must now be a CHAIN, and the rule separates the two bad maps from the eleven good
ones with no threshold and no false positives.

Deliberately **limbs only**: conversion re-parents the trunk onto a torso control, so `spine` legitimately
stops being a child of `hips` on both Daz rigs while the map stays correct and poses correctly on device.
Including the trunk would have rejected two maps that work — the same mistake the old
hips-ancestor-of-head check made, recorded above as a fix and nearly repeated here within the hour.

### Device run 2026-09-03c — joint limits, because the vocabulary could ask for the impossible

`aim` works. Two poses looked wrong, and reading the log showed the system had done exactly as it was
told — the requests were the problem:

```
"raise grace's left arm"        -> {leftUpperArm: {aim: up}, leftLowerArm: {aim: up}}
"bend saka's right leg backward"-> {rightUpperLeg: {aim: back}, rightLowerLeg: {bend: 90}}
```

Aiming the forearm "up" as well as the upper arm folds the elbow 180° behind her head — "point the
forearm at the sky" while the upper arm already points there means bend it right back. And `aim: back`
on a thigh is **90 degrees of hip extension**, against the ~20 a person manages, with a knee bend in the
hyperextension direction on top. Rendered from the logged calls, both reproduce exactly.

> **The vocabulary could express poses a body cannot make, and executed them faithfully.** Nothing in the
> system knew that an elbow does not bend sideways.

**Limits are per SEMANTIC bone**, which is the dividend a semantic vocabulary keeps paying: one table is
correct for Saka, Grace, Yuffie and everything after, exactly as one `bend` is. They are generous on
purpose — they exist to exclude the grotesque, not to enforce realism on a puppet: where a real hip
extends 20°, this allows 35.

They **ride with the frame** rather than sitting beside it. The runtime clamps client-side and the render
pipeline clamps in Python, so shipping the numbers means one table instead of two that drift.

And a request past a limit lands AT the limit and **says so** — `/figure` resolves the pose once more
purely to report it, and the tool relays *"Joint limits applied: rightUpperLeg.bend -86° → -35°"*. A
clamp nobody is told about is the silent-degradation failure this document keeps warning against; a clamp
that answers back is feedback the director can act on. Measured on the two reported poses:

| request | before | after |
|---|---|---|
| `leftLowerArm aim up` | elbow folded behind her head | `spread +135° → +8°` — arm raised nearly straight |
| `rightUpperLeg aim back` | thigh horizontal behind her | `bend −86° → −35°` — leg extended behind |
| `rightLowerLeg bend 90` | knee hyperextended | `bend +90° → +5°` — shin straight |

Two things this cost, both worth recording because both were invisible until the decomposition existed:

- **`bend` and `spread` were not orthogonal.** Both are perpendicular to the bone, but not to each other:
  an A-posed forearm rests slightly forward, which tilts them ~8° apart. Harmless while the axes are only
  ever used one at a time — and wrong the moment a rotation is read BACK out of them, which is what
  clamping does. A legal 90° elbow bend decomposed as 16° of impossible elbow abduction and was clamped.
  The frame is now orthonormalized at measurement time, so it can be read in both directions.
- **The shoulder is barely limited, deliberately.** Rest-relative bounds only work where rest IS the
  anatomical neutral. For hips, knees, ankles and the trunk it is, on every rig measured. At the shoulder
  it is not, and the rigs disagree by 48° — a T-posed arm brought down to the side is an ordinary −90° of
  spread that a tight bound would clamp. Only the twist has a neutral all three agree on, so only the
  twist is really constrained.

Also: **aim the limb, not every bone in it.** The tool description now says so outright, since aiming
both bones of an arm is what produced the fold. That is the third description change made to steer the
director, and — still — nothing measures whether any of them worked. See slice 2.

### Ingest paths that disagree about how hard they look (2026-09-03d)

Three rigged characters sat in the catalog as **props** — `Animated Woman` twice (41 and 62 joints) and
`Steve` (32). Not a figures bug: they arrived through the **Poly Pizza fetch path**, which recorded a
triangle count and a bounding box and never looked at the skeleton. So they were normalized to 1.8 m
instead of kept at life size, unmarked in search, and unposable.

That is the same shape as the catalog-revision finding earlier the same day — two paths into the library
that know different amounts about a file — so the fix goes in the **one write-through they share**
(`_catalog_asset`): any model catalogued without extraction gets extracted there, once, whatever fetched
it. Plus `ctl refresh-models`, the batch form of what placement already does one model at a time, for
after a build that changes what extraction knows.

**And the interesting half: inference refused all three, correctly.** It produced obvious nonsense —
`leftFoot` and `leftLowerLeg` both on `RightToeBase` — and `validate()` threw it out, which is the
validator doing exactly its job. The cause is layer 2's stated weakness, met for the first time in the
wild: these characters are bound with their **arms at their sides**, so the hands are no wider than the
feet (±0.69 against ±0.61) and "the widest joints are the hands" collapses. Every sample so far had been
T- or A-posed.

> **`Animated Woman` is bone-for-bone Mixamo** — `Hips`, `Spine`, `LeftArm`, `LeftForeArm`, `LeftHand`.
> [Layer 1](#1--known-conventions-free-exact), the convention table, has never been built, and it would
> map that model exactly and for free. Two of the three would probably still need layer 2 or 3, but the
> cheapest layer in the pipeline is now the one with a concrete case waiting for it.

## Aiming, evaluation and named poses

The three slices the 2026-09-03 device run calls for, in order. Designed here before any of it is built,
because the first two are cheap only if their scope stays honest and the third has a structural gap in it
that is easy to paper over badly.

Read them as one argument: **1** removes the guessing, **2** is the only way to know it stayed removed,
and **3** is the first capability the rig-independent vocabulary actually buys — as opposed to the bugs it
prevents.

### 1 — `aim`: absolute directions, because rest poses differ

`bend`/`spread`/`turn` are **relative to rest**, which is right for an adjustment ("bend the elbow a bit
more") and wrong for a destination ("arm up"). `aim` is the destination form:

```jsonc
{"rightUpperArm": {"aim": "up"}}       // up | down | forward | back | out | in
{"leftUpperArm":  {"aim": [0, 1, 1]}}  // or a body-space vector, for anything between
```

The vocabulary is **fixed and model-independent** — the same six words on every figure and every limb.
What varies per model is arithmetic the director never sees: each bone's rest direction is already
measured (`bone_directions`), and the delta is the shortest arc from it to the requested direction. Saka's
arm rotates 90°, Grace's 138°, from the identical request. That is the same move the bone names made, one
level up: one word for every rig, with the per-rig fact hidden in metadata no LLM reads.

Details that have to be decided now rather than discovered:

- **The 180° case is degenerate and must be resolved deliberately.** A hanging arm aimed `up` is a
  half-turn with no unique axis, so the shortest arc is undefined. Resolve it in the **frontal plane** —
  rotate about the body's forward axis, i.e. raise through the side, the way a person does. Falling into
  an arbitrary axis here would produce an arm swung through the torso, which is exactly the kind of
  plausible-looking wrongness this feature keeps producing.
- **`out` and `in` are side-aware**, like `spread`, so a symmetric request stays symmetric with no signs
  for the director to get wrong — the failure measured above.
- **Aim is relative to the parent chain.** Aiming the upper arm swings the whole arm rigidly with the
  elbow straight, which is what "raise her arm" means. Aiming a forearm means "relative to the upper
  arm", not to the world. Worth stating in the tool description, since it is the one place the mental
  model can break.
- **It sets direction, not twist.** `{"aim": "up", "turn": 30}` is the full expression.
- **Aim the limb, not every bone in it.** Aiming a forearm as well as an upper arm folds the elbow;
  measured on device, and now stated in the tool description.
- **Limbs only.** `aim` points a bone along its own length; on a head that would mean aiming the top of
  the skull, which is not what "have her look left" means. Head, neck and spine keep the relative axes —
  and head `turn` is already confirmed correct on all three rigs.
- **Not world-space targeting.** "Reach for that chair" is tier 3 below.

What ships: the per-bone axes payload gains the bone's **rest direction** and the **body frame**, both in
parent space, so the client can resolve an aim itself. The stored state stays semantic — `{"aim": "up"}`,
not a quaternion.

And the tool description gains **worked examples**, including a symmetric pair carrying the *same* sign on
both sides. A one-line statement of the convention demonstrably lost to the model's mirror-by-negating
reflex; an example of the exact shape it gets wrong is the cheapest counter. Whether that works is not
knowable without slice 2, which is the point of slice 2.

#### Built 2026-09-03

Shipped as designed, with one addition worth naming. **Verified in render:** the same
`{"rightUpperArm": {"aim": "up"}}` puts the arm straight up on Saka and on Grace — 90 degrees of travel on
one, 138 on the other — and every named direction lands the bone within 0.1° of where it was asked to
point, on all three rigs.

| | Saka (T-pose) | Grace (A-pose) | Yuffie (A-pose) |
|---|---|---|---|
| upper arm rests at | 0° | −48° | −46° |
| rotation for `aim: "up"` | 90° | 138° | 136° |
| lands within | 0.0° | 0.0° | 0.0° |
| axis of that rotation | the body's forward — through the side, not through the torso | | |

The addition: **a shared golden fixture**, `tests/js/fixtures/figure-pose-golden.json`, read by both
`tests/test_figures.py` and `tests/js/figure.test.js`. Resolving a pose now exists twice — in Python,
which renders the verification images, and in the client, which drives the headset — and that is only
safe while the two agree to the digit. It is the same device `plane_anchor` uses for the same reason, and
it covers the cases that are easy to get subtly different: composition order, side mirroring, and the
antiparallel half-turn.

Two things learned in passing, both about testing rather than posing:

- **three's `Quaternion.angleTo` has a noise floor of ~3e-8**, even between bit-identical quaternions —
  `acos` near a dot product of 1 amplifies the last bits. Comparisons are by dot product instead, which
  is exact and sign-insensitive (`q` and `-q` are the same rotation). An assertion tight enough to be
  worth writing was failing on arithmetic that was already correct.
- **The frame payload is now 11.6 KB for Saka's 52 bones** (5.3 KB for Grace's 21), up from 6.4 KB, and
  it rides every pose patch because it lives in the `figure` component. Still small against a room
  snapshot and fine at director cadence, but this is the point at which the fix is worth naming: the map
  and the frame are static per model and belong in `meta` → a `data-` attribute, the way `bbox` already
  does, leaving the patch to carry only the pose.

### 2 — the eval harness: verify the utterance, not the axis

The verification pass sketched under [*discovery layer 4*](#4--multimodal-verification) would have
**passed clean** on the arms bug, because the axes are correct — the words above them were not. So the
target moves up one layer:

| | Verify the frame | Verify the tool surface |
|---|---|---|
| Input | `{"leftUpperArm": {"bend": 45}}` | *"raise her right arm up"* |
| Runs | per model, at import | per phrase × 3 rigs, on demand / in CI |
| Catches | a bone mapped to a fingertip, a limb through the torso, an axis that is self-consistently wrong | the director picking the wrong axis or sign — **the measured bug** |
| Would have caught the arms | no | yes, in an afternoon, with no headset |

Both are worth having and they share all their machinery. Build the second first.

**The harness is a regression net, not a debugger.** The arms took thirty seconds to diagnose by reading
the log, and no rendering pass would have beaten that. Its actual value is that a change to a *tool
description* is otherwise entirely unfalsifiable: there is no test that fails when a sentence of English
stops steering an LLM correctly, and that is now the layer most likely to be wrong.

**Geometry judges first; the model judges what geometry cannot name.** Where a joint ends up is already
computable — `tests/test_figures.py` does exactly this — so "is the wrist above the shoulder" is an
assertion, not a question for a vision model. What no assertion expresses: *is that a raised arm or a
dislocated shoulder; is the hand hinging at the wrist or at a fingertip; is the elbow inside the ribcage.*
That is the same discipline as [layer 5](#5--the-validator-llm-proposes-geometry-disposes), pointed at
pictures instead of bone maps.

The shape of one check:

1. Blender renders **front and side** of the rest pose and the posed figure (built, <1 s each, free).
2. Both go up **side by side** with a **multiple-choice** question — *"compared to the first image, the
   right arm has moved: (a) up and out to the side (b) forward (c) backward (d) down (e) barely moved."*
3. A second question for sanity — *"does this look like a person doing this, or is a joint bent in a way
   a body cannot?"*

Constrained choice against a reference render, from fixed viewpoints. **Never free-form spatial
description**: VLMs are unreliable at 3-D spatial reasoning and reliable at recognition, so the job is to
turn the former into the latter.

**Which model.** The seam exists — `conjure/captioner.py` is a `Captioner` protocol over Gemini with a
`FakeCaptioner` for tests — but the return type here is a *structured verdict*, not a caption, so this
wants **its own protocol beside it** rather than an extension (open question 11, answered). Default to
**Gemini 2.5 Flash** for the bulk battery: already wired, already keyed, cheap enough to run every mapped
bone at import, where the budget is explicitly generous. Use **Claude** — already the roster's default
director, so no new key — where the judgment is rubric-heavy and instruction-following matters more than
cost. A `FakeJudge` keeps the harness testable offline.

**A trap to check before building around it:** Grace and Saka's current exports are **undressed**, and a
hosted vision model may decline to judge those renders at all. Either verify against the clothed
conversions or render the battery in clay/matcap. Cheaper to learn now than after the harness assumes an
answer.

### 3 — named poses, and the root problem they expose

Three tiers, and the extension point is deliberately only the middle one:

| | What it is | How it extends | Example |
|---|---|---|---|
| **1. Axes** | per-bone rotations: `bend`, `spread`, `turn`, `aim` | **it does not** — closed by design | "bend her elbow", "spread her legs" |
| **2. Named poses** | multi-bone configurations, stored as **data** in tier-1 terms | add an entry | "kneel", "sit", "hands on hips" |
| **3. Solved poses** | constraints against the world — wants a solver, not a vocabulary | not yet | "reach for that chair", "hand flat on the table" |

A pose is a dict in the vocabulary that already exists:

```jsonc
"kneel": {
  "leftUpperLeg":  {"bend": -75}, "leftLowerLeg":  {"bend": 105}, "leftFoot":  {"bend": -20},
  "rightUpperLeg": {"bend": -75}, "rightLowerLeg": {"bend": 105}, "rightFoot": {"bend": -20},
  "spine": {"bend": 5}
}
```

**Because tier 1 is rig-independent, one authored pose works on every figure.** That is the first thing
the axis work *buys* rather than merely protects, and it is why poses are data: adding "sit" is an entry,
not code. The component stores `{"pose": "kneel"}` plus any per-bone overrides, so the durable state stays
semantic and small, and the director learns what exists the way it learns what dynamics modules exist
(`dynamics://available`) rather than by being told in a prompt.

**The gap kneeling exposes: rotations cannot ground a figure.** Rotate a standing figure into a kneel and
her hips stay where they were, so her knees go through the floor. `grounded` placement will not save her —
it snaps the entity by its **bind-pose** bounds, which is the same stale-box problem `grab` had.

Fix it with a measurement rather than a per-pose fudge: **re-ground after posing.** Find the lowest
contact joint in the posed skeleton and settle the figure so it rests on the floor. One rule covers the
cases, including poses nobody authored:

- raise one leg → the lowest point is still the standing foot → she does not move
- kneel → the lowest point becomes the knees → she settles onto them
- sit → onto the pelvis, once there is something to sit on (a seat is tier 3)

It is approximate by construction — joint positions plus a small contact radius, not skinned vertices —
and that is the right trade: exact would mean posing the mesh server-side, and the error is millimetres
against a decision measured in tens of centimetres.

**Authoring loop, and it is one this document already defines.** An LLM proposes the numbers, Blender
renders front and side, the judge from slice 2 says whether that is a kneel, and a human confirms once
before it is frozen into the library — *propose → render → verify → freeze*, exactly the discipline
[layers 5 and 6](#5--the-validator-llm-proposes-geometry-disposes) apply to bone maps. A new pose then
costs a call and a glance.

**What stays closed: the axis words.** Four rotations cover every single-bone request, and both examples
that prompted this were already expressible — "spread her legs apart" is `spread` on both sides (the
director simply got the sign wrong), and "knees in" is negative `spread`. Every time this feature has
grown a fifth criterion at the primitive layer it has been fitting noise; that is recorded twice above. A
request that genuinely cannot be said in tier 1 is a signal that it belongs in a different tier, not that
tier 1 is missing a word.

## The tool surface

One coherent family rather than a tool per capability — the way `conjure_module` is one generic tool — with
**all of them writing to the same `figure` component**, so persistence is solved once.

| Tool | Purpose |
|---|---|
| `inspect_model(id)` | the "navigate the tree" request: groups, meshes, morphs, clips, bone map |
| `set_model_parts(id, show=[], hide=[])` | outfits |
| `pose_model(id, {bone: {bend, spread, turn}})` | **semantic** bone names AND semantic axes (relative) |
| `pose_model(id, {bone: {aim: "up"}})` | the same tool, absolute — see § *Aiming, evaluation and named poses* |
| `pose_model(id, pose="kneel")` | a named multi-bone pose, resolved from the library |
| `animate_model(id, clip=, speed=, loop=)` | clip playback |

**`inspect_model` must summarize, not dump.** A two-hundred-node tree wrecks the director's context window.
The clip list should reach the director the way `dynamics://available` does — read from the catalog, so it
knows what a figure *can* do before it asks.

---

## Performance

At **≤3 figures** the budget is generous: spring bones for hair and cloth, IK, per-figure mixers, denser
meshes than a crowd would permit. Crowd-scale optimization is off the table entirely, which is a real
simplification.

Not free, though — a skinned human costs per-frame GPU skinning plus CPU mixer work, and
[`investigations/pops-and-jitters.md`](../investigations/pops-and-jitters.md) records that dropped frames
are already a live issue on this hardware. `Budget.maxTris` (500k) exists in the schema and nothing enforces
it. **Numbers to be measured in the first slice, not guessed at here.**

---

## Open questions

1. ~~What are these models made of?~~ **Answered 2026-09-01** — see the measurement section. Re-run the
   inspection on any new source before assuming it generalizes; four models produced three rig conventions.
2. **Standardize on a base figure, or stay per-model?** A single source makes the bone map constant and
   retargeting one-time; ports make each figure a fresh discovery problem but are the only way to get a
   *specific* character. Almost certainly both, but the ratio decides how much the discovery pipeline has
   to earn its keep. The two proposed test fixtures exist to answer the aesthetic half of this.
3. **Where does motion come from?** Finding 2: the models ship **no animations at all**, so clips must be
   sourced (Mixamo library, purchased packs, hand-authored) and **retargeted** onto each figure's skeleton.
   This is now the biggest unplanned piece of work in the feature, and it is what the humanoid bone map is
   *for*. Retargeting quality — foot sliding, proportion mismatch, hand contact — is its own problem.
4. **Export granularity** — one GLB per outfit, or one GLB with everything and runtime visibility?
   The outfit feature wants the latter; 117–526 k verts per figure wants the former. Probably "worn set
   plus chosen alternates", which makes outfit selection partly a conversion-time decision.
5. **Decimation policy and bone reduction.** One Grace (526 k verts) exceeds `Budget.maxTris` for the whole
   world. What target, and driven by what — a fixed budget, or measured frame time on device?
6. **Life size vs. normalized height.** Finding 8: these arrive correct and metric, and `TARGET_SIZE_M`
   would damage them. Confirms life-size — but placement needs a path that *skips* normalization for
   figures, plus a sanity clamp for a future source that does ship centimetre scaling.
7. **JCM stripping cost.** Removing driver-fired corrective morphs is required for size; how bad the
   joints look without them is a device question.
8. **Pose composition** — additive-after-mixer, or mutually exclusive. Decide before building.
9. **Attach mechanism** — `/module target` vs. a dedicated endpoint.
10. **Facing normalization** — bake the correction into the GLB at conversion (one canonical forward for
    every figure), or record `facing` in `attributes` and correct at placement? Baking is simpler downstream
    and destroys information; recording is reversible and pushes the concern into every consumer.
11. ~~**Which multimodal model** for the verification passes, and whether it extends `Captioner`?~~
    **Proposed 2026-09-03** — Gemini 2.5 Flash for the bulk battery (the seam and the key exist), Claude
    for rubric-heavy judging (already the default director), and its **own** protocol beside `Captioner`
    because the return type is a structured verdict. Unresolved sub-question: whether a hosted model will
    judge renders of the undressed exports at all.
12. **Which armature is the humanoid one** when a file ships several (Hitomi has five). Body-mesh binding
    cross-checked against bone count is the obvious heuristic; the hair rigs are separate skeletons and are
    exactly what spring-bone secondary motion would drive later.
13. **Touch response mechanism** — canned reaction clip, IK reach toward the contact point, or spring-bone
    secondary motion. Relates to the open physics-vs-parametric question ([`decisions.md`](../decisions.md) #12).
14. **Where the pose library lives** — shipped with the repo like the bundled dynamics modules, per-user
    under the data dir, or in the catalog beside the figures. It is data either way; the question is who
    can add one and whether a pose can be world- or user-scoped.
15. **Re-grounding vs. an authored root offset** for poses that change where the body's base is. Measuring
    the lowest contact joint generalizes to poses nobody authored, which is the argument for it; an
    authored offset is exact for the poses we do author and silent for the rest.
16. **Does a re-grounded pose fight `grab` or the plane anchor?** Both write the entity transform, and a
    pose that moves it is a third writer — the derived-frame hazard the rest of this document keeps
    hitting. Likely wants the drop to live on a wrapper node inside the entity rather than on the entity.

---

## Where to pick up

**Posing works end to end on device, and the next three slices are designed** under
[*Aiming, evaluation and named poses*](#aiming-evaluation-and-named-poses). Build them in that order —
the second exists to keep the first true, and the third is the first thing the vocabulary buys rather
than protects:

1. ~~**`aim`** — absolute directions, plus worked examples in the tool description.~~ **Built
   2026-09-03**, verified in render on all three rigs. Whether the *tool description* now steers the
   director correctly is exactly what is not yet known — that is slice 2's job, and the first thing to
   point it at is the three utterances that failed: "raise her arm up", "point her arm down", "spread
   her legs apart".
1b. ~~**Joint limits**~~ **Built 2026-09-03**, after `aim` made it easy to ask for the impossible. Per
   semantic bone, shipped with the frame, clamped on both sides of the wire and reported back.
2. **The eval harness at the utterance layer** — ~20 phrases × 3 rigs, rendered and judged. The only
   guard on the one layer of this feature that has no test, and the only way to know whether a change to
   a sentence of English did what it was supposed to.
3. **Named poses + re-grounding** — "kneel", "sit". Data, not code, because tier 1 is rig-independent;
   and the re-grounding rule is what stops a kneeling figure standing in the floor.

**Outfits (Phase 2) are deliberately deferred** — simpler, and they also want a re-conversion with
clothing, so nothing is lost by waiting.

State to be aware of when resuming:

- Grace and Yuffie in the world are the **body-only** exports (no clothing). That was for joint
  diagnosis; re-adding clothing is a `--collections` change, see the recipes above. This now blocks
  slice 2 as well, if a hosted vision model declines to judge an undressed render.
- Yuffie's boots, shoulder guard, eyes and eyebrows render flat grey — 22 of her 26 baked materials fail,
  and the neutral fallback papers over it. Recorded under the black-bake limitation, not chased.
- `leftHand` resolves to a fingertip on the re-parented Daz rigs, so a hand rotation hinges in the wrong
  place there (Saka is correct). Arms and legs were the reported problem and are fixed; the hand was
  deliberately not chased after the head rule showed what iterating on a secondary defect costs. It is
  the obvious first catch for slice 2's per-bone battery.
- `validate()` checks the bone MAP and knows nothing about the axes, and nothing at all checks the
  utterance layer. Those are slice 2's two jobs, in that order of difficulty.
- A figure's map and frame are re-measured on the next placement whenever the catalog's are stamped
  older than `figures.FRAME_REV` — so a fix reaches the library without re-importing, but an entity
  ALREADY in a world keeps whatever it was placed with. Re-place it.
- The dev library holds six Graces and five Yuffies from different eras of this code. They all work now,
  but `grace_c` / `yuffie_c` (body-only, ~73 k and ~57 k tris) are the current conversions and the
  cheapest; `saka` is the VRM control. The rest are worth deleting when convenient.
- The anatomical frame and `aim` are on `feat/figures-anatomical-pose`, branched from `main`, not merged.
- Aiming is refused for the trunk (`hips`, `spine`, `chest`, `upperChest`, `neck`, `head`) and for a
  bone whose frame was measured before aiming existed — the latter re-measures itself on the next
  placement, so it is a "place it again", not a re-import.

## Phasing

| Phase | Work | Why here |
|---|---|---|
| **0** | One model, end to end, **no new abstractions.** Convert by hand, import, place, look. | Answers what design cannot: life size, facing, feet on floor, frame cost, and whether `grab` behaves on a skinned mesh. Almost every remaining uncertainty is empirical. |
| **1** | Conversion + discovery pipeline: headless Blender, GLB JSON extraction, convention table, topology inference, LLM labeling, visual verification, validator, one human confirm. | The heart, and the largest piece. Mostly pure Python ⇒ mostly testable. |
| **2** | `figure` component + endpoint + tools. Outfits and clips. | First user-visible payoff: *"change her jacket"*, *"have her wave"*. |
| **3** | Posing, with the composition question already settled. | **Under way.** The anatomical frame shipped 2026-09-02; `aim`, the eval harness and named poses are designed and next. |
| **4** | Locomotion (A for scripted, C for reactive) and touch (B). | |

Phase 0 should start as soon as a real model file exists, and does not depend on any decision above.
