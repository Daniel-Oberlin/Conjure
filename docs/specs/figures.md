# Figures — rigged humanoids — the spec

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/figures.md`](../backlogs/figures.md); rejected alternatives
and the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

A **figure** is a rigged humanoid model — imported, placed at life size, and posable by name: *"raise
her right arm", "bend his left knee", "turn her head"*. It rides the existing model pipeline
([`specs/library.md`](./library.md), [`specs/worlds-surfaces.md`](./worlds-surfaces.md)) and adds one
thing on top of it: a **per-model vocabulary**, discovered at import and frozen into the catalog, that
lets a caller name a body part and a direction without knowing anything about that model's rig.

Two indirections carry the whole feature, and neither is optional:

| Question | Answered by | Because |
|---|---|---|
| **Which** node is "leftUpperArm"? | `attributes.humanoid` — a semantic-name → node-name map | one rig calls it `upper_arm.fk.L`, the next `J_Bip_L_UpperArm`, a third `lShldrBend` |
| **Which way** does it rotate? | `attributes.humanoid_axes` — an anatomical frame per bone | a bone's local axes are whatever its rigger chose: `leftUpperLeg` rests 177° from identity on two rigs here and 6° on a third |

Everything below follows the pipeline's one architectural rule: **import is expensive and produces a
durable artifact; runtime is data lookup.** No LLM, no Blender and no geometry search runs at pose time.

---

## 1. What makes a model a figure

`ModelImporter` (`conjure/importer.py`) claims `.glb` and `.vrm`, confirms both by the glTF magic bytes,
and stores either as `.glb` — a `.vrm` *is* a GLB, so the client's `gltf-model` needs no special case.
A model is a **figure** when its glTF document contains a `skins` array; that single fact sets
`attributes.rigged`, and everything else in this spec is gated on it.

The whole document is read with stdlib `struct` + `json` (`read_glb_json`, `figures.split_glb`): the node
tree, skins, weights, animations, materials and the VRM extension all live in the GLB's JSON and BIN
chunks. `trimesh` is used for the triangle count only.

### `glb_bounds` — never derive a skinned mesh's extent from the scene graph

The bounds are computed by `importer.glb_bounds`, not by trimesh, and the rule it encodes has been wrong
three different ways in the field:

- A **skinned** primitive's vertices are already in skin space, so the mesh node's transform must *not*
  be applied. trimesh applies it anyway and reported one figure at 3.369 m against a true 1.757 m.
- But those vertices reach the world **through the joints**, so a scale on the armature or baked into the
  inverse bind matrices *does* apply — two library models carry ×100 and were recorded at centimetres.
- And no single scale factor recovers it either: some rigs author every body part as a small cluster near
  the origin and let each joint carry it into place. So skinned primitives are **actually skinned**,
  sampled to 50 000 vertices, using the joint world matrices times the inverse bind matrices.

Unskinned primitives take the ordinary path: accessor `min`/`max` corners through the node's world
transform. Without a BIN chunk to read, a skinned primitive falls back to its accessor box times the
skin's own scale.

## 2. What import records

Everything rides the catalog's per-kind JSON `attributes` bag — no schema change
([`specs/library.md §2`](./library.md)).

| Attribute | For every model | Meaning |
|---|---|---|
| `bbox_min` / `bbox_max` | ✓ | authored model-space bounds, per above |
| `tris` | ✓ | triangle count (trimesh) |
| `frame_rev` | ✓ | which build looked at this file (§7) |
| `rigged` | figures | the file contains a skin |
| `height_m` | figures | the Y extent — glTF is Y-up |
| `joints` | figures | joint count per skin, e.g. `[42, 482]` |
| `clips` | figures | animation names |
| `morph_targets` | figures | total morph-target count |
| `spring_bones` | figures | `VRMC_springBone` present |
| `humanoid` | figures with a map | `{semanticBone: nodeName}` |
| `humanoid_source` | figures with a map | `vrm` \| `convention:<name>` \| `inferred` |
| `humanoid_axes` | figures with a map | the anatomical frame, per bone (§4) |
| `humanoid_follows` | when needed | `{nodeName: nodeName it rides}` (§3) |

`frame_rev` is stamped on **every** model, not only figures: "we looked and it is a prop" is worth
recording for exactly the same reason a bone map is.

`clips`, `morph_targets` and `spring_bones` are **recorded and not yet read** by anything — no animation
playback, no morph control and no spring-bone motion exists.

## 3. Discovery — recovering the bone map

`figures.best_humanoid(doc, blob)` runs the layers that are built, cheapest first, and every candidate is
gated on `validate()`. A map that is plausible but wrong is worse than none, because posing inherits it
silently.

| Layer | Where | How it works | Cost |
|---|---|---|---|
| **stated** | `importer.vrm_humanoid` | VRM's `VRMC_vrm.humanoid.humanBones` (1.0 dict form) or `VRM.humanoid.humanBones` (0.x list form), stored as node **names** so a re-export that reorders nodes cannot break it | free, exact |
| **names** | `figures.CONVENTIONS` | three verified tables — `mixamo`, `rigify-fk` (what `blend_to_glb.py` emits from Daz/Rigify ports, both `upper_arm.fk.L` and `upper_arm_fk.L` spellings), `dot-side` (a free-asset-pack scheme). Exporter prefixes (`mixamorig:`, `Armature|`) are stripped before matching | free, exact |
| **shape** | `figures.infer_humanoid` | pure topology and geometry: feet are the lowest joints, hands the widest (walked up to the first branch point, since the widest joint is a fingertip), the head is the common ancestor of the tallest trunk joints, hips is where the two leg chains meet. Joints along a limb are picked by **fraction of height or reach**, never by index, because chains vary from 4 to 12 joints | reads every vertex weight |

A stated map is read by the importer before either. Names are tried before shape and shape is not run at
all when a table hits — inference reads the whole weight buffer, which is not work to do speculatively.
Only conventions **verified against a file on disk** are in the table: a speculative row cannot be
checked, and a name that happens to match is how a control bone gets mapped over the deform bone it
drives.

**Which skin is the body** is not decided by a heuristic. `humanoid_skin_order` orders skins by how many
meshes each deforms, and `best_humanoid` tries each in turn, keeping the first whose skeleton validates —
because joint count and vertex count are both rules about a rigger's habits (one model here has a
679-joint hair-and-cloth rig beside a 362-joint body).

Three post-passes complete a raw map:

- **`deform_joints` / `prefer_deform`.** A bone with no vertex weights cannot be a humanoid bone worth
  posing, and rigs split every joint into an FK control and the deform bones a constraint links to it —
  glTF drops constraints, so rotating the control moves nothing. `prefer_deform` swaps any mapped bone
  that drives nothing for a **co-located** bone (within 2 mm) that drives more of the limb, never
  substituting upward and never onto a node already taken. The *chain between* extremities is
  deliberately not weight-filtered: some rigs' real `upper_arm` carries no weights at all.
- **`prune_map`.** A broken link drops the distal bone and everything below it on that chain rather than
  discarding the map — losing a bone rather than gaining a lie. Completeness is then judged against
  `REQUIRED_BONES` (11) rather than all of `CORE_BONES` (21), since plenty of rigs have no toes, no
  clavicle and no separate chest.
- **`follow_bones`.** A deform bone that **no mapped bone can move** — an IK foot or hand parented to the
  armature root — is walked up to the top of its detached subtree and recorded as riding the nearest
  mapped joint, bounded to 40 % of the figure's height and never across the midline or onto its own
  descendant. Without it, a raised leg leaves the foot planted and stretches the mesh. The file's own
  hierarchy is left untouched so its baked clips keep meaning what they meant.

### `validate()` — LLM proposes, geometry disposes

Pure Python over the glTF JSON, unit-testable with no headset. Empty list means self-consistent:

| # | Check | The failure it catches |
|---|---|---|
| 0 | distinctness, and every `REQUIRED_BONES` entry present | three leg bones mapped to one IK control — every ordering comparison equal-not-less, laundered as clean |
| 1 | `left*` is at +X of `right*` for hands, feet, upper arms, upper legs | a side swap inverts every later pose |
| 2 | vertical order down head→neck→chest→spine→hips and along each leg, with 5 mm of slack | a knee above a hip |
| 3 | `hips` is an ancestor of both feet | that is what makes a bone the root of a body |
| 3b | each **limb** is a real parent-child chain | the zig-zag arm: a forearm parented to the armature root passed every positional check for a week. **Limbs only** — conversion legitimately re-parents the trunk onto a torso control |
| 3c | mapped upper arms and legs **drive some geometry** (needs the BIN chunk) | a stock Rigify FK control sits exactly where an upper arm belongs, in a proper chain, and moves nothing |
| 4 | limb segments within 0.4–2.5× of each other | a twist helper mistaken for a joint |

`score(inferred, stated)` compares a guess against a VRM's stated answer, returning the actual
disagreements rather than a percentage.

**Not in the import path:** no LLM labelling, no rendered verification, no human confirmation step. Every
layer above is deterministic arithmetic.

## 4. The anatomical frame

Semantic names are half the vocabulary; semantic **axes** are the other half, and without them the first
is nearly useless. `figures.anatomical_axes(doc, mapping)` measures three rotation axes per bone from the
bind pose, choosing each so a **positive** angle produces the named motion on either side of the body.

- **`body_frame`** measures the figure's own `up` (hips→head), `left` (the vector between the first
  available paired joints, Gram-Schmidt'd against `up`) and `forward` (their cross product). Measured,
  not assumed — every sample comes out at glTF's +Z anyway, which is the point.
- **`bone_directions`** gives each bone's direction as the vector to the next **mapped** joint down its
  chain, so a rig missing `chest` falls through to `neck` rather than losing the bone. A bone at the end
  of a chain continues the direction it arrived on: a hand points the way the forearm did.

| Axis | Motion | Definition | Degenerate case |
|---|---|---|---|
| `bend` | the joint **folds the way it folds** | `direction × forward`, negated for bones in `_FOLDS_BACK` | a bone already pointing forward (a foot) falls back to `direction × up`, so bend lifts the toes |
| `spread` | the far end swings **outward**, away from the midline | `direction × outward`, where outward is the body's left for left bones | a bone already pointing outward (a T-posed arm) falls back to `direction × up` |
| `turn` | the bone twists about its own length, **inward** | the bone's own direction, negated on the right | never |

`_FOLDS_BACK` holds exactly one entry: `LowerLeg`. `bend` as "the far end swings forward" coincides with
flexion at the hip, elbow, spine and neck and is backwards at the knee, so the knee's axis is flipped and
`bend` means flexion everywhere. The mirroring belongs in the frame, not in the caller's head.

Three properties are load-bearing:

- **Mirror symmetry is by construction**, from one sign per side. The same numbers on `leftUpperArm` and
  `rightUpperArm` produce mirrored motion. `bend` is deliberately *not* mirrored — flexing both hips
  moves both knees the same way.
- **The axes are stored in each bone's PARENT frame** (`space="parent"`, the default), which is the frame
  its own local rotation lives in. Applying one is a single multiplication onto the rest quaternion and
  the result rides the parent chain for free. `space="world"` exists for the offline render check.
- **`bend` and `spread` are orthonormalized** against each other at measurement time. Both are already
  perpendicular to the bone but not necessarily to each other — an A-posed forearm tilts them ~8° apart,
  which is invisible until a rotation is read *back* out of them, which is what clamping does.

Each frame also carries the four vectors an absolute aim needs — `rest`, `up`, `forward`, `out`, all in
the same space — plus that bone's `limits`, so the runtime needs no anatomy table of its own. Components
are rounded to 5 decimal places. A bone with no measurable direction gets no frame and is not posable.

## 5. The pose vocabulary

`POST /figure` takes `{bone: {bend|spread|turn: degrees}}` and/or `{bone: {aim: …}}`, resolved by
`figures.resolve_pose` on the server (to report) and by `figure.js` on the client (for real).

**Relative — `bend`, `spread`, `turn`**, in degrees from wherever that bone rests. Right for an
adjustment, wrong for a destination: a relative number asks the caller to know the rest pose, and the
rigs here disagree by 48° about where an arm rests.

**Absolute — `aim`**, a named body direction (`up`, `down`, `forward`, `back`, `out`, `in`) or a free
`[out, up, forward]` vector. It resolves as the swing from the bone's measured rest direction onto the
target, so the identical request rotates a T-posed arm 90° and an A-posed one 138° and lands both
straight up. `out`/`in` are side-aware, so a symmetric request stays symmetric with no signs to get
wrong.

- The **antiparallel** case is resolved deliberately, not left to a generic shortest-arc: a hanging arm
  aimed `up` is a half-turn with no unique axis, so the rotation is taken about the body's forward — the
  frontal plane — and the arm goes up through the side rather than through the torso.
- `aim` **replaces** `bend` and `spread` (they set the same swing) and **composes** with `turn`.
- `aim` is **refused on the trunk** (`TRUNK_BONES`: hips, spine, chest, upperChest, neck, head), because
  it points a bone along its own length and on a head that would mean aiming the top of the skull.
- Composition order is **turn, then bend, then spread** — twist innermost, the swing-twist decomposition
  — mirrored exactly in `figures.resolve_pose` and `figure.js` so a Blender render and a headset agree.

### Joint limits

The vocabulary can express poses a body cannot make. Limits are per **semantic** bone — one table is
correct for every rig, exactly as one `bend` is — and generous on purpose: they exclude the grotesque
rather than enforce realism on a puppet.

| Bone | bend | spread | turn |
|---|---|---|---|
| `*Shoulder` | −20 … 20 | −20 … 35 | −20 … 20 |
| `*UpperArm` | −140 … 190 | −100 … 190 | −95 … 95 |
| `*LowerArm` | −5 … 155 | −8 … 8 | −95 … 95 |
| `*Hand` | −75 … 85 | −25 … 35 | −35 … 35 |
| `*UpperLeg` | −35 … 130 | −30 … 75 | −50 … 50 |
| `*LowerLeg` | −5 … 155 | −5 … 5 | −15 … 15 |
| `*Foot` | −55 … 30 | −18 … 18 | −25 … 25 |
| `*Toes` | −35 … 65 | −10 … 10 | −10 … 10 |
| `hips` | −45 … 45 | −45 … 45 | −45 … 45 |
| `spine`, `chest` | −25 … 50 | −30 … 30 | −40 … 40 |
| `upperChest` | −20 … 40 | −25 … 25 | −35 … 35 |
| `neck` | −45 … 45 | −40 … 40 | −65 … 65 |
| `head` | −35 … 35 | −30 … 30 | −50 … 50 |
| any finger joint | −15 … 95 | −18 … 18 | −12 … 12 |

Left and right share a row because the axes are already mirrored. **The shoulder is barely limited, and
deliberately so:** rest-relative bounds only work where rest *is* the anatomical neutral, which holds for
hinges and the trunk on every rig measured and not at the shoulder, where the rigs differ by 48°. Only
the twist has a neutral all of them agree on.

Two clamping rules, each fixing a measured bug:

- **A number the caller supplied is clamped as a number** (`clamp_angle`). Recovering it from the
  resulting rotation reads `bend: 200` back as −160 — the same quaternion — and clamps it to nearly
  straight, the opposite of what was asked.
- **An `aim` is clamped from its rotation** (`clamp_to_joint`), because it arrives as a direction, not an
  angle. The rotation is split swing-from-twist, the swing resolved into bend and spread, each clamped,
  the three rebuilt. When nothing is out of range the original quaternion is returned untouched.

A clamp **answers back**. `/figure` resolves the pose once more purely to report what the joints refused,
and `pose_figure` relays it: *"Joint limits applied: rightUpperLeg.bend −86° → −35°"*. When a joint is
asymmetric the message says which way — `bend −90° → −5° (it folds the other way)` — because "→ +5°"
alone reads as *nearly at its limit*.

Every other refusal is **loud** too: an unknown bone lists the bones this figure has; an unknown
direction lists the six; a figure with no map or no frame says which it is missing and what to do about
it. A pose that silently does nothing is indistinguishable from one the user cannot see from where they
are standing.

## 6. Placement

`_normalize(record, pos, target_m, rigged=…)` in `server.py` treats a figure differently in two ways:

- **Life size.** With no explicit `size_m`, a figure keeps its native scale — normalizing every human to
  `TARGET_SIZE_M` (1.8) erases the difference between a child and a giant.
- **Height, not largest extent.** A T-posed figure's arm span rivals its height and a seated one's
  exceeds it, so when a caller *does* give a size, that size means height.

Life size is honoured only within `HUMAN_HEIGHT_M` = **0.5 – 2.5 m**. Not every rigged model is authored
metric: measured in the library, two come out at 4.82 m and 0.37 m. Outside the range a figure is
normalized like anything else — but still by height.

`_model_entity_op` then ships what the client needs on `meta`, and `conjure-client.js` mirrors two of
them onto DOM data attributes:

| `meta` | `data-` | Consumer |
|---|---|---|
| `rigged: true` | `data-rigged` | `grab` skips triangle-testing a figure's body |
| `bbox` | `data-bbox` | `grab`'s selection box and hit test |
| `humanoid` | — | `/figure` resolves bone names without a catalog lookup |
| `humanoid_axes` | — | the `figure` component resolves poses against it |
| `humanoid_follows` | — | the `figure` component's parent constraint |

**`grab` and figures.** Two changes, both in `dynamics/grab/grab.js`, after a 348 k-triangle figure made
grabbing stutter at 90 Hz:

1. The cached oriented box is a **gate** on the exact raycast, not a fallback — a ray that misses the box
   cannot hit a triangle inside it. This benefits **all content**.
2. A figure's body is **never triangle-tested**; its box is the affordance. The resize HUD is still
   tested exactly, so a handle grab lands on a real corner.

And `_boxFor` prefers the **authored** `data-bbox` over measuring the scene graph, because a skinned
mesh's node commonly hangs off a bone while its vertices are already in skin space — folding in
`matrixWorld` double-counts the whole skeleton and drew a box twice the figure's height. Same rule as
`glb_bounds`, on the other side of the wire: **never derive a skinned mesh's extent from the scene
graph.**

## 7. `FRAME_REV` — a catalog row is a snapshot of what we understood

A figure's map, frame and limits are **cached in the catalog**, and understanding keeps changing while
rows do not. `figures.FRAME_REV` (**9** today) is bumped whenever anything that changes a derived result
changes — inference, the axes, `validate()`, the convention table, which skin is chosen.

- `_refresh_model_attrs` re-extracts any row whose `frame_rev` is stale, on **first placement**, and
  writes the result back. Extraction is authoritative for everything in `_DERIVED_MODEL_ATTRS`, including
  **clearing** a map it can no longer justify — `{}` rather than absent, since the catalog merges and
  skips `None`. Curation (label, tags, licence, rating) is untouched.
- `POST /library/refresh-models` (`conjure-ctl refresh-models [--force]`) is the batch form, for after a
  build that changes what extraction knows.
- `_catalog_asset` — the one write-through every ingest path shares — extracts model attributes for any
  model catalogued without them, whatever fetched it. Three rigged characters had sat in the catalog as
  props because the Poly Pizza fetch path recorded a triangle count and never looked at the skeleton.

A version stamp rather than "does this row carry the keys today's code needs", because the change that
mattered was **the validator getting stricter**, which no key can express. A tripwire test pins
`FRAME_REV` and `_DERIVED_MODEL_ATTRS` to each other: adding a derived field without bumping the
revision once left every row marked current and the fix reached nobody.

Note the ordering on the fetch path: `/place_asset` catalogues the corrected figure attributes but places
**that first instance** as a prop, from the resolver's own trimesh bounds. Placing it again from the
library (`/place_cached_asset`) is what gives it the figure treatment.

## 8. The runtime — the `figure` component

`client/figure.js` is an ordinary A-Frame component on the placed model entity, **not** a dynamic module
([`specs/dynamics.md`](./dynamics.md)) — it has no independent existence, it decorates a placed model. So
a pose is shared, persisted and replayed on reload for free, over the existing patch/snapshot path.

Four fields, all JSON **strings**, because their keys differ per model and A-Frame's flat schema types
cannot express that:

```
humanoid   {leftUpperArm: "upper_arm.fk.L"}                   which node
follows    {"Foot.L": "LowerLeg.L"}                           which bones ride another
axes       {leftUpperArm: {bend: [x,y,z], …, limits: {…}}}     which way, and how far it may go
pose       {leftUpperArm: {bend: 45}}                          how far, in degrees
```

The durable state is **semantic** — the pose is stored in exactly the terms it was asked for, never as
quaternions, because that is what a reload replays and what a persona layer would later reason about.

Four behaviours worth stating, each the fix to a measured defect:

- **A pose composes onto the rest rotation, never replaces it.** `bone.rotation.set(...)` discards what
  the rigger authored — measured at 177° on one rig's `thigh.fk.L` — so the leg went upside-down before
  the requested angle was added, and `clear` left it there. Rest quaternions are captured once per loaded
  model, in a `Map` of the component's own (three deep-copies `userData` through JSON when it clones).
- **Bone names are looked up through spelling variants.** three's `PropertyBinding.sanitizeNodeName`
  *removes* `[ ] . : /` rather than replacing them, so `upper_arm.bend.L` becomes `upper_armbendL`. Every
  candidate spelling is tried, since the rule has shifted between three releases.
- **`_ride` applies `follows` as a parent constraint after the pose**, using the offset captured from the
  **bind** pose (read live, it measures the offset after the limb has already moved and the bone never
  budges) and a general affine inverse (some armatures sit at scale 100).
- **Clearing restores the rest quaternion, not identity** — on a bone dropped from the pose, on
  `clear=true`, and on component removal.

The component re-applies on `model-loaded`, since `gltf-model` loads asynchronously and a pose that
arrives first would find no skeleton. Every `/static/*.js` reference is mtime-stamped by one regex in
`server.py`; `figure.js` shipped without a stamp once and the headset served a stale copy through several
reloads, so three fixes never ran.

## 9. Conversion — out of band, and Blender-only

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

## 10. Verification

| Suite | Covers |
|---|---|
| `tests/test_figures.py` (55) | inference, `validate`'s every rule, pruning, `follow_bones`, the frame, forward kinematics through a posed bone, aiming, limits, the convention table |
| `tests/test_importer.py` (27) | `glb_bounds`' skinned/unskinned/armature-scale cases, the VRM maps, what a rigged model records |
| `tests/test_server.py` (~35) | life-size placement, the catalog revision and its tripwire, `refresh-models`, and `/figure` end to end through the real import → place → pose path |
| `tests/js/figure.test.js` (40) | rest composition on a deliberately non-identity rest rotation, clearing, riding, client-side clamping |

`tests/js/fixtures/figure-pose-golden.json` is **shared** by `tests/test_figures.py` and
`tests/js/figure.test.js`. Pose resolution exists twice — in Python, which renders the verification
images, and in the client, which drives the headset — and that is only safe while the two agree to the
digit. It covers what is easy to get subtly different: composition order, side mirroring, the
antiparallel half-turn, and every clamp. (Comparisons are by quaternion **dot product**: three's
`Quaternion.angleTo` has a ~3e-8 noise floor even between bit-identical quaternions.)

## 11. Surface reference

| Endpoint | Purpose |
|---|---|
| `POST /figure` | pose a placed figure, or `clear=true` to return it to rest. Owner-gated (`_OWNER_ONLY_PATHS`) |
| `POST /library/import` | ingest a `.glb`/`.vrm` — the figure attributes come out of this path |
| `POST /library/refresh-models` | re-derive every model row's attributes |

**MCP tools:** `inspect_figure` (height, triangle count, the bones this figure actually has, current
pose) and `pose_figure`. Neither is in `_READONLY_TOOLS`, so a `access: "read"` agent gets neither.
`search_library` annotates a rigged hit with `[figure 1.76 m, 348k tris]` — the two facts that decide
which of six near-identical figures to place.

**CLI:** `conjure-import` (ingest), `conjure-ctl refresh-models [--force]`.

**Deps:** none new. GLB reading is stdlib; `trimesh` was already there. Blender is a soft dependency of
the conversion scripts only, never of the world server.

## 12. What is not built

Recorded here so the spec can be trusted about its own edges; the design work is in
[`backlogs/figures.md`](../backlogs/figures.md).

- **No animation.** `clips` is recorded and never read. There is no mixer component, no `animate_model`,
  no retargeting, and no decision yet on how a pose and a clip compose.
- **No outfits.** Collection structure is used at *conversion* time to choose what to export; there is no
  runtime show/hide, no slot vocabulary, and no `set_model_parts`.
- **No named poses** ("kneel", "sit"), and therefore no re-grounding — rotations cannot ground a figure,
  so a pose that lowers the body would leave it standing in the floor.
- **No discovery layers 3–6:** no LLM labelling, no multimodal verification, no human confirmation.
- **No evaluation of the utterance layer.** `validate()` checks the bone map, renders check the axes, and
  nothing at all checks whether a change to a tool description still steers the director correctly.
- **No FBX front door**, so no Mixamo.
- **No morph, spring-bone or MToon support.** VRM material data is in the file and A-Frame's plain glTF
  loader ignores it.

## 13. Related specs

- [`specs/library.md`](./library.md) — the catalog, the `attributes` bag, and the one ingest
  write-through these attributes ride.
- [`specs/worlds-surfaces.md`](./worlds-surfaces.md) — how a placed asset becomes an entity.
- [`specs/spaces-geometry.md`](./spaces-geometry.md) — placement modes and the plane-relative anchor a
  figure is placed with; also the +Z facing convention the body frame measures rather than assumes.
- [`specs/dynamics.md`](./dynamics.md) — the sync tiers, and why `figure` is ordinary world state rather
  than a dynamic module.
