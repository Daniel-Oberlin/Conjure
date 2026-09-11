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

### Named poses — tier 2

`POST /figure {"named": "kneel"}`, and `conjure/poses.py` is the library. **17 poses**: `kneel`,
`kneel-one`, `crouch`, `sit`, `t-pose`, `cheer`, `reach-out`, `hands-on-hips`, `arms-crossed`, `wave`,
`point`, `bow`, `hug`, `all-fours`, `bend-over`, `bend-over-wide`, `stand`.

A pose is a dict in the vocabulary above, so **one authored pose works on every figure** — that is what
the rig-independent axes buy rather than merely protect, and `scripts/pose_library.py` measures it:
every pose against every rig of the eval cast, 16 of 17 clean on all three (the exception is `crouch`'s
torso lean on Trish, whose spine does not carry her head).

The last four fold the trunk forward, and they only became possible once an aim resolved against the
parent frame *as posed* — see § *What is not built* for what that replaced. They also carry the one
naming rule this library has learned the hard way: `bend-over` is not called `touch-toes`, because
measured on the cast the hands stop at about knee height.

**Two verifiers, and they fail differently.**

- `Pose.signature` holds `pose_corpus` predicates, so "is this a kneel" is arithmetic. This is what
  **fails** a pose. The library and the eval corpus are the same kind of object over one evaluator,
  `pose_corpus.check_predicates`.
- `scripts/pose_library.py --identify` renders each pose and asks a vision model **which of the library
  it is looking at**. Recognition, never "is this pose any good" — that framing is a judgement and does
  not work (§ *The utterance layer*). Calibrated first against the wrong `kneel`, which it must decline.
  **Advisory**: it confuses poses that genuinely resemble one another (a deep `crouch` reads as a
  `sit`), so it reports rather than gates.

**The visual check is not decoration — it catches what a signature structurally cannot.** A signature
only asserts the bones a pose *sets*, so it is blind to the bones a pose *forgets*, and both defects of
that shape were found this way: leg poses left the arms at the rig's bind pose (a T-posed VRoid figure
knelt like a scarecrow), and one-armed poses never said what the other arm does. The fix for both is
`aim`, which is absolute and so lands an arm at the side from a T-pose and an A-pose alike.

A third defect of the same shape, found the same way: every pose that folds the trunk has to re-aim the
LEGS, because `hips` is the root of the whole figure and bending it carries the legs along with the
torso. Rendered, the figure was tipped over bodily and floating diagonally in the air, while its
signature passed. `points leftUpperLeg down` now guards it.

| | Behaviour |
|---|---|
| Expansion | server-side, so there is one definition of "kneel", in Python, beside its signature |
| `stand` | a **stance**, not a reset: arms at the sides, everything else back to rest (`Pose.clears`). Returning to the file's own bind pose is `clear=true` — and on a VRoid rig that is a T-pose, which is not what anyone means by "have her stand" |
| Durable state | the expansion **and** the name (`components.figure.named`), so the state reads "she is kneeling" |
| Overrides | `{"named": "kneel", "pose": {...}}` in one call — *"kneel, but with her arms out"* |
| Hand-editing after | drops the name: she is no longer kneeling, she is in a pose of her own |
| A bone the rig lacks | **skipped and reported**, not refused — the opposite of a hand-written pose, where an unknown bone is a typo and must be loud |
| No bones in common | refused. Filtered-to-nothing is not `stand`, and clearing her would be a wrong answer wearing a right one |
| `needs` | said out loud. `sit` makes the *shape* of sitting; a seat is tier 3 and is not built |

**Tier 3 — solving against the world** ("hand flat on that table") is not built and wants a solver.
Measured cost of its absence: `sit` leaves a figure floating above a real chair — about an inch on Grace,
several on the shorter Saka — so the error is rig-dependent and not a constant to subtract.

### Measuring the mesh

Until 2026-09-10 nothing in the pipeline had looked at a vertex, so a pose could put every joint exactly
where it belonged while the flesh around them was inside the chest. Three functions in `figures`:

| | |
|---|---|
| `body_profile(doc, blob, mapping)` | the torso's half-width and depth per height band |
| `limb_radius(doc, blob, mapping, bone)` | a limb's median thickness about its own axis |
| `deform_subtree(doc, mapping, bones)` | whose vertices belong to a bone — the mapped node **plus its descendants** |

Vertices are classified by the bone they are most heavily weighted to, because skin weights are what
separate torso from limb; a bounding box or a name convention would be guesswork. `deform_subtree` is
what makes it work on a rig whose mapped bones are controls: Trish's `spine` is a control whose only
child is `spine.twk`, and matching the mapped node alone found zero torso and zero arm on her.

**What it found.** A shoulder sits almost exactly at the torso's edge, so an arm hanging straight down
overlaps by its own radius — the term joint positions structurally cannot see:

| rig | torso half-width | shoulder out | arm radius | overlap |
|---|---|---|---|---|
| Saka | 6.5 cm | 8.0 cm | 2.2 cm | 0.6 cm |
| Grace | 15.9 cm | 15.2 cm | 3.0 cm | 3.7 cm |
| Trish | 15.9 cm | 14.8 cm | 2.9 cm | 4.0 cm |

So `_ARMS_DOWN` aims 8° out rather than straight down, and the `clears` predicate asserts it — the only
predicate that reads a vertex.

**Three limits, recorded in the backlog rather than fixed:** the 8° is a hard-coded constant, so a figure
outside the sample is under- or over-corrected (Eve already is); `clears` measures **lateral** clearance
only, because the forward equivalent needs an origin at the centre of the torso's depth that nothing
computes; and it sits downstream of the bone map, so a bad map gives a confident wrong answer — Eve's
shifted map made her neck read as a 10 cm torso.

**Known limits of posing by joint**, all measured on device 2026-09-09 and none catchable by a signature,
which asserts where joints are and never whether flesh intersects flesh:

- ~~Arms aimed `down` **enter the body**, `arms-crossed` folds inside the chest~~ — **fixed 2026-09-10**
  by measuring the mesh (below). `hands-on-hips` still does not quite touch.
- `point` and `wave` read as *reaching*, because there is **no finger vocabulary** — fingers are not
  recoverable from topology (§3), so no inferred map has them.

### Re-grounding

Rotations cannot ground a figure: the hips do not move, so posing alone leaves the body wherever the
bind pose put it, and `grounded` placement will not save it — that snaps the entity by its **bind-pose**
bounds, the same stale box `grab` used to select with.

**The direction is the surprise.** A kneeling figure does not sink through the floor; she **floats
54 cm** (measured on Grace and Saka). Every joint hangs off hips that rotation cannot move, so a folded
leg can only raise the foot, and the knee that becomes the lowest joint sits well above where the toes
were. A lift-only correction — the obvious reading of "stop her sinking" — would do nothing at all for
the one pose it was written for.

So the rule is symmetric, lives in `figure.js`, and is one line of intent: **put the lowest mapped joint
back at the height the lowest mapped joint had at rest.** Raise one leg and the standing foot is still
the lowest, so nothing moves. Kneel and she settles onto her knees. It is measured in the model's own
frame, so placement and scale cancel; it is undone before each re-measure, so poses do not accumulate;
and it uses **mapped** bones only, because a hair rig's bones sprawl half a metre past the body and are
not what anything rests on.

Approximate by construction — joint positions, not skinned vertices, so a knee sinks by about its own
radius. Exact would mean skinning the mesh, and the error is centimetres against a decision measured in
tens of them.

**Verified on device 2026-09-09**, including the case static analysis could not settle: the settle
survives a space recapture. It moves the mesh *inside* the entity while the anchor solver moves the
entity, and the two do not fight. It also survives a reload, does not accumulate over repeated poses, and
stays silent when nothing rests any lower.

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

## 9a. Re-assembling a PlayCanvas build

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
| `tests/js/figure.test.js` (46) | rest composition on a deliberately non-identity rest rotation, clearing, riding, client-side clamping, and re-grounding (including that it does not accumulate and does not scale with placement) |

`tests/js/fixtures/figure-pose-golden.json` is **shared** by `tests/test_figures.py` and
`tests/js/figure.test.js`. Pose resolution exists twice — in Python, which renders the verification
images, and in the client, which drives the headset — and that is only safe while the two agree to the
digit. It covers what is easy to get subtly different: composition order, side mirroring, the
antiparallel half-turn, and every clamp. (Comparisons are by quaternion **dot product**: three's
`Quaternion.angleTo` has a ~3e-8 noise floor even between bit-identical quaternions.)

### The utterance layer — `scripts/pose_eval.py`

Everything above verifies the **frame**: given `{"leftUpperArm": {"bend": 45}}`, does the right joint
move the right way. None of it verifies the **tool surface**: given *"raise her right arm up"*, does the
director produce that call at all. The two fail independently, and the second is the one with no other
net — a change to a sentence of English in `pose_figure`'s description is otherwise unfalsifiable.

| | Verify the frame | Verify the tool surface |
|---|---|---|
| Input | `{"leftUpperArm": {"bend": 45}}` | *"raise her right arm up"* |
| Runs | every commit, free | on demand — real API calls, real Blender |
| Catches | a bone mapped to a fingertip, an axis that is self-consistently wrong | the director picking the wrong bone or sign |

**The corpus is data** (`conjure/pose_corpus.py`): 20 phrases × the 3-rig cast, with the expectations
written per *phrase* and never per rig — the claim the whole vocabulary rests on is that one sentence
means the same thing on every skeleton. Distances are fractions of the figure's own height, so a 1.55 m
rig and a 1.87 m one score alike.

**Three layers, cheapest first, and each fails for a different reason:**

| Layer | Checks | A failure means | Gates? |
|---|---|---|---|
| `check_call` | `touches` / `leaves` / `signs` — which bones were set, and which way | the tool description is wrong | yes |
| `check_geometry` | where the joints landed once the pose is applied to a real bind pose | the words and the frame disagree | yes |
| the judge | up to two multiple-choice questions about a rendered pair | *see below — it did not reproduce* | no |

**`points` for an absolute request, `moved` for a relative one.** Scoring *"hold both arms out to the
sides"* by displacement marks Saka wrong, because she rests in a T-pose and correctly does not move: an
`aim` is a claim about where the limb **ends up**. That distinction is the corpus's half of the same
argument that produced `aim` itself.

**What the harness does not do is execute anything.** It reads the live tool schemas off the MCP server
(never a transcription — a transcribed description would pass forever after the first edit), runs one
real director turn per cell, and answers the tool calls from the model file: `inspect_figure` returns
`figures.figure_description`, the same wording the real tool returns, and `pose_figure` runs
`figures.clean_pose` and reports the joint limits the endpoint would have reported. No world, no server,
no headset.

**The judge** (`conjure/judge.py`) is a seam beside `Captioner`, returning a structured verdict rather
than prose. Every question is **multiple choice against a reference render from fixed viewpoints** —
never free-form spatial description, because vision models are unreliable at 3-D reasoning and reliable
at recognition. A model that will not pick an option returns `choice == -1`, which is an abstention and
never scores as a pass. Gemini 2.5 Flash by default (`judge_provider` / `judge_model`), Claude for
rubric-heavy work, `FakeJudge` offline.

It asks two questions:

- **Which way did it move** — asked only where the corpus claims one word is true of the motion on every
  rig (`Phrase.moves`; empty means do not ask). A trunk bone barely translates, an absolute request
  means something else on a rig already resting in the target pose, and a lifted knee goes up *and*
  forward — none of those has a one-word answer, so none is asked.
- **Could a body hold this** — asked of every cell.

**Neither one gates a cell.** (Both are *judgement* framings. Asking the same models a **recognition**
question — "which of these poses is this?" — works, and is used by the pose library; see
§ *Named poses*. The failure below is of the question, not of vision models.) As of 2026-09-05 the judge layer is advisory (`--judge-gates` to make it
count), because it does not reproduce: across four full runs its disagreements were 29, 8, 2 and 7 out
of 60, with cells moving in and out of the failure column while nothing but the model changed. Before
each battery the harness renders a **calibration pose** — a 170° twist of the skull, so the figure faces
forward with the back of its head to the camera — and asks the same plausibility question. Gemini 2.5
Flash and Claude Sonnet 4.6 both answer *"yes, a person could hold this"*.

**The calibration is the point, not a footnote.** An instrument that cannot detect the defect it exists
to find does not become trustworthy by being run over sixty cells, and the only way to know is to ask it
something whose answer is already known. What gates a cell is `check_call` and `check_geometry`, which
were 60/60 on every one of those runs.

Renders come from `scripts/pose_test.py --clay`, which also gained `--frame` so the posed and rest shots
share one camera — *"compared to the first image"* means nothing between two differently-zoomed pictures.
Clay was chosen over textures for three reasons at once: materials are irrelevant to which way an arm
went, several of the library's are still wrong, and a hosted judge might decline to look at an undressed
figure. Measured 2026-09-05: it does not decline, and it distinguishes up / down / forward / back /
barely-moved correctly on a four-way probe.

Three views, not two: front, **three-quarter** and side. A dead-on view is degenerate for any limb aimed
along it — an arm pointing forward foreshortens into what looks like a folded elbow — and the extra shot
is nearly free, because the ~3 s the script costs is Blender starting and importing a GLB while each
render is about 30 ms.

    python scripts/pose_eval.py --calls-only          # the fast loop after editing a description
    python scripts/pose_eval.py --phrases arm-up --rigs Saka --keep out/

A full battery is 60 cells in ~20 minutes and a few cents. `--calls-only` runs the layer a
tool-description edit can actually break, needs no Blender and no judge, and takes about twenty seconds.

## 11. Surface reference

| Endpoint | Purpose |
|---|---|
| `POST /figure` | pose a placed figure by bone or by `named` pose, or `clear=true` to return it to rest. Owner-gated (`_OWNER_ONLY_PATHS`) |
| `POST /library/import` | ingest a `.glb`/`.vrm` — the figure attributes come out of this path |
| `POST /library/refresh-models` | re-derive every model row's attributes |

**MCP tools:** `inspect_figure` (height, triangle count, the bones this figure actually has, current
pose), `list_poses` (the named library, read from the data rather than written into a prompt) and
`pose_figure`. Neither is in `_READONLY_TOOLS`, so a `access: "read"` agent gets neither.
`search_library` annotates a rigged hit with `[figure 1.76 m, 348k tris]` — the two facts that decide
which of six near-identical figures to place.

**CLI:** `conjure-import` (ingest; `--label` names the asset, defaulting to the filename stem, and is
distinct from `--creator`, which is whoever made it), `conjure-ctl refresh-models [--force]`,
`python scripts/pose_eval.py` (the utterance-layer battery), `scripts/pose_test.py` (render one pose).

**Deps:** none new. GLB reading is stdlib; `trimesh` was already there. Blender is a soft dependency of
the conversion scripts only, never of the world server.

## 12. What is not built

Recorded here so the spec can be trusted about its own edges; the design work is in
[`backlogs/figures.md`](../backlogs/figures.md).

- **No animation.** `clips` is recorded and never read. There is no mixer component, no `animate_model`,
  no retargeting, and no decision yet on how a pose and a clip compose.
- **No outfits.** Collection structure is used at *conversion* time to choose what to export; there is no
  runtime show/hide, no slot vocabulary, and no `set_model_parts`.
- **No tier 3.** Nothing solves against the world: "sit on that chair" makes the shape of sitting and
  says so; "hand flat on the table" is not expressible at all.
- **No discovery layers 3–6:** no LLM labelling, no multimodal verification, no human confirmation.
- ~~**`aim` is not absolute once the trunk is posed.**~~ Built 2026-09-10 as `figures.compose_frame`
  and its mirror in `figure.js`: an aim resolves against the parent frame *as posed*, so a limb aimed
  `down` under a folded trunk hangs plumb on every rig in the cast. `bend`, `spread` and `turn` stay
  relative, which is the point of having both.
- **`downward-dog` is not expressible**, and it is the clearest measure of tier 3's absence in tier 1's
  own terms: the pose needs hands and feet both on the floor, and at the fullest trunk fold the joint
  limits allow, with the arms plumb, the hands are still 0.15–0.41h above it across the cast.
- **Forward self-intersection is invisible.** `clears` reads lateral clearance only; a forearm inside the
  chest is not detectable, and `arms-crossed` was fixed by rendering and looking.
- ~~**No named-pose authoring loop.**~~ Built as `scripts/pose_library.py`: propose → check the signature
  → have a model say which pose it sees → glance → freeze. The older text follows.
- The judge exists and the renderer exists, but nothing yet proposes
  a pose, renders it, verifies it and freezes it into a library.
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
