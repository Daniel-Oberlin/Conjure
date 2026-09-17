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
`.glb` is claimed by **two** handlers now, so the extension only narrows and the file decides: a GLB
with animation channels and no mesh is an `animation`, not a model ([`library.md §2a`](./library.md)).
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
| `rig_sig` | figures with a map | a fingerprint of the SKELETON — see below |
| `rig_sig_rev` | figures with a map | which definition of a signature produced it (§7) |

`frame_rev` is stamped on **every** model, not only figures: "we looked and it is a prop" is worth
recording for exactly the same reason a bone map is.

**`rig_sig` makes a figure and an animation CLIP comparable without either naming the other.**
`figures.rig_signature(doc, blob)` hashes the mapped humanoid bones as the file spells them, so the
same value comes out of a character's GLB and of a skeleton-only clip authored on it — a clip binds by
node name, so equal signatures mean it will drive that figure with nothing in between. Sixteen of
twenty captured figures share one signature.

Over the **mapped** bones and not every node, deliberately: two of those figures differ by 51 skirt and
anatomy bones while agreeing on all 37 core ones, and a fingerprint that split them would answer a
question nobody asks. `None` for a rig no map could be recovered from — a skeleton we cannot name is
one we cannot promise anything about. What the catalog does with it is
[`specs/library.md §2a`](./library.md).

A figure's own `clips`, `morph_targets` and `spring_bones` are **recorded and not yet read** — no
playback, no morph control and no spring-bone motion exists. (The `animation` KIND is a different
record and its clip names are read; see library.md §2a.)

## 3. Discovery — recovering the bone map

`figures.best_humanoid(doc, blob)` runs the layers that are built, cheapest first, and every candidate is
gated on `validate()`. A map that is plausible but wrong is worse than none, because posing inherits it
silently.

| Layer | Where | How it works | Cost |
|---|---|---|---|
| **stated** | `importer.vrm_humanoid` | VRM's `VRMC_vrm.humanoid.humanBones` (1.0 dict form) or `VRM.humanoid.humanBones` (0.x list form), stored as node **names** so a re-export that reorders nodes cannot break it | free, exact |
| **names** | `figures.CONVENTIONS` | five verified tables — `mixamo`, `rigify-fk` (what `blend_to_glb.py` emits from Daz/Rigify ports, both `upper_arm.fk.L` and `upper_arm_fk.L` spellings), `rigify-def` (the deform chain, with no FK controls in the file — what a PlayCanvas export bakes down to), `dot-side` (a free-asset-pack scheme), `cc-base` (Reallusion Character Creator). Exporter prefixes (`mixamorig:`, `Armature|`) are stripped before matching, and a side letter in the wrong CASE still matches when unambiguous (`CC_Base_r_Hand` beside `CC_Base_L_Hand`) | free, exact |
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
| 1 | `left*` is on the expected side of `right*` for hands, feet, upper arms, upper legs — **relative to the figure's FACING**, read off toes-vs-ankle | a side swap inverts every later pose. "+X is the left" is a property of how a model was authored, not of glTF: two captured figures are built facing −z, and the absolute rule rejected their correct name-based maps, so discovery fell through to inference and produced genuinely MIRRORED ones |
| 2 | vertical order down head→neck→chest→spine→hips and along each leg, with 5 mm of slack | a knee above a hip |
| 3 | `hips` is an ancestor of both feet | that is what makes a bone the root of a body |
| 3a | `hips` carries the legs but not the spine, while its **own parent** carries both | a bone one step too far down a fork. Reallusion forks `CC_Base_Hip` into `CC_Base_Pelvis` (thighs only) and `CC_Base_Waist` (spine only) at the **same world height**, so every ordering check passes either way and inference took the Pelvis — bending those hips swings the legs and leaves the torso upright. Deliberately NOT "hips is an ancestor of the spine": on a full Rigify export the trunk hangs off a `torso` control four levels away and that map is correct |
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

### 5a. Parts — which mesh is clothing

A capture's figure is one GLB carrying a body, a dress, hair, shoes and a pair of eyelashes as separate
meshes. Turning a garment off needs a list of the nodes that ARE the garment, and somewhere to put it
that a person can correct.

**Classified once, at import**, into `attributes.parts` — `{nodeName: category}` over six categories:
`body`, `face`, `hair`, `clothing`, `shoes`, `accessory`. Same argument as reading a normal map from the
registry instead of its pixels: a guess made at import can be inspected, overruled and versioned, while
one made at render time is invisible and fires again on every load.

**The vocabulary is data** — `parts/parts.json` on the user-first search path `config.PARTS_PATH`
([`specs/config.md §4`](./config.md)) — because it is never finished: the next capture arrives with a
word nobody listed. `attributes.parts_rev` records which revision classified an asset, so a change says
which rows are worth reclassifying instead of silently disagreeing with them.

A prefix rule is the obvious approach and does not work: `clothes_*` appears in only **8 of 20**
captures and the rest say `Dress`, `Shorts`, `underwear`, `Kimono`, `Strap_Top` — bare words. Rules are
matched as case-insensitive **substrings**, in order, first match wins, with `body` **last**: its names
(`model_britney`, `agnes`) are substrings of the specific ones, and with it first `model_britney_hair`
classified as body and could never be taken off.

Only nodes carrying a **mesh** are parts. A bone called `DEF_Skirt01` drives a garment and is not one;
hiding it would do nothing while implying it had.

`attributes.parts_unclassified` is **reported, not swallowed** — it is the vocabulary's backlog and the
only honest measure of its coverage, and it works as one: `Beer` sat on it as the single unclassified
name across the twenty captured figures until `held` was added for it (revision 2). What remains there is
one mesh literally named `New Entity`, and that stays unclassified, because inventing a category for a
name that means nothing is how a classifier starts lying.

**Scoped to the captures, and the scope shows.** The vocabulary was fitted to 96 mesh names from one
origin, and the dev-library models use a different dialect: 30 names across nine of them are
unclassified — `Genesis 8 Female Mesh`, `Grace_Mesh`, `Character`, `Woman`, `Shaun` are all bodies, and
`Eye_L`, `eyelid`, `Mouth`, `Face`, `Tearline` are all face. Neither is a defect in the classifier; it
is what "the vocabulary is data and is never finished" means in practice, and the backlog list is doing
exactly the job it exists for. The Quest controller and the VR hands are unclassified too and should
stay so — they are the app's own machinery rather than figures, and a controller button is not clothing.

`body` and `face` are **never removable**: taking the eyes out of a head is not undressing it. `hair` is
removable and is **not** clothing — stripping a figure to check its integrity should not scalp it.

**`held` is what the figure carries, not what she wears.** Oktoberfest's beer is the case: `accessory`
means worn, so filing a pint there means "take off your beer". It is removable — putting it down is a
thing people ask for — and ordered after `shoes` and before `accessory`, which matters both ways.

The tempting rule was structural and is wrong. The beer is the only unskinned mesh in the corpus
parented to a HAND bone (`DEF-hand.R`), so it rides her arm through a clip — but bone-parenting alone
misclassifies three of the four cases that have it: bride's two heels hang off `DEF-foot` and teacher's
glasses off the head. Telling them apart needs the humanoid map, and the names answer it correctly
without one. **The structure is why the category exists; the vocabulary is how it is decided.**

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
| `parts` | — | `/figure/parts` expands a category into node names (§5a, §8a) |

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
rows do not. `figures.FRAME_REV` (**18** today) is bumped whenever anything that changes a derived
result changes — inference, the axes, `validate()`, the convention table, which skin is chosen, the
parts vocabulary.

`retarget.RETARGET_REV` (**7**) is the same idea for a different artefact. A retargeted clip is
DERIVED and cached under a content address, and a content address fingerprints the bytes rather than the
method that made them — so without a stamp, a pair retargeted by an older build is handed back forever.
That is not hypothetical: the fix for an unmapped rotating ancestor landed, the server was restarted,
and the figure kept swinging exactly as before, because the cache still held the clip made before it.
**Anything derived and cached carries the revision of the code that derived it**; this is the second
artefact to need that rule and it should be assumed for the third.

**A signature is a comparison between two rows, so backfilling one side is worse than backfilling
neither.** `RIG_SIG_REV` 1 → 2 respelled every FIGURE's signature when the humanoid grew fingers, and
`refresh-models` walked the models and stopped there, because the name says models. Every clip stayed
stamped with a signature computed over the old vocabulary — so Alice stopped matching her OWN clip, the
server read a mismatch and offered to retarget her onto herself, and every shipped-clip lookup came back
empty. A backfill that reported success and broke the catalog's playback. `refresh-models` now
re-derives both sides in one pass.

`figures.RIG_SIG_REV` (**3**) is a **separate** stamp, and the separation is the point: a discovery fix
can change a signature without changing what a signature *means*, and the two need telling apart when
regrouping a catalog. Bump it only when the definition changes — which bones the fingerprint covers, or
how it is spelled.

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

### 8a. The `figure-parts` component

A sibling to `figure`, and deliberately **generic**: it hides the glTF nodes it is handed and knows
nothing about garments. One field, `hidden` — a JSON array of node names — because what counts as
clothing was decided at import where the answer is recorded and correctable.

`inspect_figure` reports what comes **off** beside what bends, and that is not decoration: it is the
tool a director reaches for when asked "what is she wearing", and while it listed only bones the
director answered *"a unified mesh with no detachable parts"* about a figure carrying seven. A tool's
silence gets quoted back as fact.

`POST /figure/parts` (`dress_figure`) resolves the semantics: it takes **categories** or node names,
expands categories against the entity's `meta.parts`, and writes node names to the component. So the
classifier proposes and **the entity holds the truth** — a wrong grouping is corrected by naming the
mesh, not argued with. A word that is neither a category nor a mesh on this figure is reported back,
since silently ignoring it reads as a working command.

Hiding is **visibility, never removal**. The mesh stays in the scene, stays skinned and stays posed with
the rest of the figure, so showing it again needs no reload and a pose survives undressing. Re-applied
on `model-loaded`, exactly as `figure` re-applies a pose, or undressing a figure that is still loading
does nothing and looks like a broken tool.

**A figure with no classified parts is not a figure that cannot be undressed.** Its clothing may be a
separate container — Jane's `hair.glb` and `underwear.glb` are their own assets — which is a different
mechanism: remove that entity. The endpoint says so rather than returning success and doing nothing.

### 8b. The `figure-clip` component — playing a captured animation

A clip is a GLB containing **animation channels and no mesh at all** — which is how these builds ship
motion: 47.7 MB of clips against a 5 MB character, loaded only when one is played. It binds to a figure
**by node name**, and that is the whole reason it is reusable.

The numbers are measured, not assumed. Jane's `1_idle` names 222 target nodes; on three other figures
sharing her rig signature it resolves **222 of 222**, and on the rest of the `85e41f9b8e` cast 171–187.
Her model has **21 clips that shipped with it and 314 that merely fit**. Channels naming a bone the
target does not have are dropped before the mixer is built, because three's alternative is one console
warning per unresolved binding and no way to tell whether the clip half-played or not at all.

**What a clip is allowed to write, and it is less than it contains.** A captured clip states far more
than motion — the exporter writes a channel per bone per property — and the surplus is actively
dangerous. Four rules, and all four came from one playback: Alice animated with
`WhiteboardIdleFIXFIXU` grew a hundredfold, left the room through the ceiling, lost her clothes, and
appeared pinned to the viewer, because a figure that large has no parallax.

| dropped | why |
|---|---|
| any track on the node the mixer is ROOTED at | that node is placement and unit conversion. Alice's `RootNode` carries `scale 0.01`, cm→m, and the clip carries a `RootNode` scale track of `[1,1,1]`. Writing 1 over 0.01 is the whole bug |
| a CONSTANT scale track, anywhere | measured over 120 clips, **22,714 of 22,718 scale tracks never change value**. They animate nothing and each is the same trap waiting for a model whose rest scale is not 1. The 4 that move are kept |
| a CONSTANT position track | a rest offset restated. Dropping it leaves the TARGET's own proportions in place, which is what retargeting wants |
| a track whose node the model lacks | three warns once per unresolved binding — 51 lines for one clip — and the warning is all you get |

An **animating** position track is not dropped but **re-based**: frame 0 is pinned to the model's own
rest and the curve rides on top. `CC_Base_BoneRoot` sweeps 294→316 units where Alice rests at 0, which
is three metres of displacement before she has moved — the clip is stating where the figure stood in
the scene it was captured from. **Motion is the clip's business and location is the entity's.**

**Time comes from the shared clock, never from frame deltas.** The entity stores the instant the clip
STARTED and each client computes its own offset into it. Two headsets are then on the same frame with no
per-frame message, and a client that joins late, stalls, or backgrounds for a minute lands on the right
frame the next time it draws. Accumulating `delta` per client guarantees the opposite: they start at
different moments, drop different frames, and nothing ever pulls them back. So `tick` SEEKS —
`action.time = f(sharedClock)` then `mixer.update(0)` — rather than advancing.

**Precedence, stated rather than discovered: a playing clip wins, a pose applies when idle.** Both write
the same bones, so without a rule the mixer wins every frame simply by running later and the pose looks
broken rather than overridden. Stopping calls `figure.restore()`, which puts EVERY bone back on its bind
pose — rotation and position, since a clip translates and a pose never does — and re-applies the pose.
`figure.apply()` could not do this job: it only resets bones it posed, and a clip writes 222 of Jane's
while a pose names six.

**Across rig signatures, binding by name resolves nothing**, and it is the failure that does not
announce itself: the clip resolves whichever handful of names happen to coincide and drives the figure
by those, which reads as a broken figure rather than a mismatched clip. So the channels are **rewritten**
rather than bound — §8c — and `force` still exists for binding by name across the mismatch and looking
at the wreck deliberately.

**A label resolves to HER clip first.** `10_action` is eleven different clips across the library, but
asked of a particular figure the question is not ambiguous: it means the one that shipped with her. Only
when no shipped clip matches does the endpoint fall back to refusing and listing candidates — refusing
outright made the label path useless for the only caller that has one, since an asset id is not
something a person says out loud.

**The voice is part of the clip, and the tool surface has to SAY so.** On device the director
answered *"I don't have a way to add audio/sound to the scene — there's no audio tool available to me"*
while Barbie's voice was already playing: the capability existed, and nothing a director reads
mentioned it. So `play_clip`'s description states it outright, a silent clip reports how many of hers
are voiced rather than reporting no audio, and `list_clips(voiced=True)` exists because "animate her
with sound" is a FILTER, not a missing feature. A tool's silence gets quoted back to the user as a
limitation — the same failure as `inspect_figure` omitting parts and the director then calling a figure
with seven of them "a unified mesh".

**The voice travels with the clip.** 20 of Jane's 21 clips have one, and the link is many-to-many — one
file serves four clips — so it is a `voiced_by` relation rather than a column. It is sent in the same
patch, off the same stamped instant, because two arrivals would mean two start times and a body out of
sync with its own speech is the one thing an audience notices immediately. Played through a media
element rather than a decoded buffer for one reason: `currentTime` is writable, so a client joining
mid-clip starts the voice where the body already is. A browser may refuse to start audio without a
gesture; that is a refusal and not an error — the clip keeps playing and the voice joins on the next
one.

**A resolved `play()` is not a sound, and that is the trap.** An `AudioContext` created outside a user
gesture starts SUSPENDED, and routing a media element through it means the element plays, the promise
resolves, nothing throws — and the output goes into a stopped graph. Silence that reports success. It
shipped that way and the director duly announced *"her voice is playing along with it"* to a browser
making no noise. So the context state is re-checked AFTER the promise resolves, not only in the catch,
and the gesture hook resumes the context as well as replaying the element — and re-seeks, because the
body has moved on while the voice was waiting.

**One element per voice, and the guard is on the URL rather than on the clip.** `_audio` runs twice per
clip change — from `update`, and again when the GLB finishes loading — and `update` only tears down
when the CLIP ID changes. So replaying one clip at a different rate, or any patch touching the
component without changing the clip, reached `_audio` with nothing torn down; it built a second media
element for the same URL and overwrote `_media` with it, leaving the first unreachable. Nothing
referenced it, so no later `_silence()` could pause it, and it played on underneath everything after
it — reported from a headset as *"sometimes I think the old sound is still playing"*. A voice already
playing is now re-seeked in place, and the deferred `begin()` checks it has not been superseded, which
is the guard the gesture hook beside it always had.

`GET /figure/clips` answers with **two lists that are never merged**: what `shipped_with` this figure,
and what its `rig_sig` says can play. Compatibility is not sufficiency — 93 of 206 clip names call out a
fixture (bed 30, sink 18, toilet 12) — so a clip that binds perfectly still puts a figure leaning on a
sink that is not there. The authored set is the default and reaching past it takes `all=true`. See
[`decisions.md`](../decisions.md) §27.

### 8c. Retargeting — a clip rewritten for a rig it was not authored for

`conjure/retarget.py`, server-side, producing an ordinary clip. Until it existed, ten figures in the
catalog could be offered no clip at all.

**On the server, and the output is just a clip.** The alternative was to send both skeletons to the
client and do the algebra there, and it is worse in every way that matters: the output is a function of
two files and nothing else, so it content-addresses and is computed once per (clip, rig) pair ever; the
client keeps its one bind-by-name path; and the arithmetic stays next to the tests that pin it.

**Both inputs describe themselves.** A clip GLB carries no mesh and no skin, but it does carry its
authoring rig's NODES, so the source rig's rest pose and its humanoid map are both recoverable from the
clip alone and nothing has to be looked up.

#### The law: carry the pose ABSOLUTELY

Preserving each bone's rotation relative to its own rest is the obvious method and it is wrong, because
two rigs rest differently: if one rests arms-down and the other arms-out, a clip that puts the first's
arms straight down sends the second's half way. What survives the crossing is **where the limb IS**. So
each rig's axis CONVENTION is divided out instead of its rest POSE:

```
C   a convention-free rest frame per bone, built from where its limb POINTS
K   = C⁻¹ · R           what is left of the authored rest once the physical part is removed
Wt  = Ws · Ks⁻¹ · Kt    the source's orientation, respelled in the target's convention
```

Then, top-down, the local rotation that achieves `Wt` against the parent's **already-moved** world.
Using the parent's rest instead is the obvious shortcut and it fails the identity case by 32°.

**There is no whole-body term, and there was one.** `swing = Bt · Bs⁻¹` aligned the two rigs' rest body
frames, on the reasoning that a figure whose armature rests leaning should perform in HER frame. Wrong
twice. glTF fixes the world frame at Y-up, so there is no world-frame CONVENTION between two GLBs to
divide out — a difference between two rest body frames is a difference in rest POSE, and not carrying
rest pose is the whole of this law. Measured across every rig signature in the catalog: all eleven are
Y-up and Z-forward, their body frames 0.1°–9.6° off world, and every one of those is a lean about the
side axis, never a right angle. And the lean is a lean of the spine BONES, which the absolute carry
already reproduces — so `swing` added it a second time. On `LayTableIdle` that was the whole of a
reported tilt: figures spread over 11.7° from vertical, and 2.9° without the term.

**`hips → neck` is the chord of a curved spine, not an axis**, which is why the rest geometry cannot
name a convention to better than about 10°. A body is not square. Building the body frame from the legs
instead is no better (0.4°–7.9°).

#### What the law does not carry, and the number for it

Limb directions land within **4.9–7.2°** of the source across the catalog, against a **5.3° floor** —
the floor being `Jane → Jane`, the same clip on its own rig, where the only difference is channel loss.
Read every figure as its excess over that, never as an absolute.

The residual is **the law's approximation, not a defect**: `K = C⁻¹·R` is exact only where two rigs'
rest limbs point the same way, and where a limb bends away from that the conversion carries a few
degrees. It shows up in the time domain as a body wobble of 2–8° against a native 0.7°, alternating
perfectly with the pose — agreeing exactly at the ends of a cycle and differing in the middle.

**Three refusals**, all of which are better than an approximation: a clip whose own rig has no humanoid
map (there is nothing to map its channels *through*), a figure with no map (Tamaki — binding by name
would resolve nothing and play silence while reporting success), and a clip that drives no bone the
target has.

**Three notes it reports rather than swallows.** Channels that drive bones the humanoid does not name —
skirt, breast and secondary chains, 52 of them on a 104-channel clip. A target whose bone map is not a
CHAIN, so motion cannot compose through it and that part of the body lags; reported separately for the
body and for the fingers, because a broken torso is the performance and a broken finger is cosmetic.
And bones the clip SLIDES that have nowhere on the target to land.

#### What a rewrite has to carry beyond rotation

Each of these was a defect seen on a headset before it was a rule.

| | |
|---|---|
| **an unmapped ANCESTOR is part of the pose** | Alice's `CC_Base_BoneRoot` rotates 36.3° while the hips under it rotate 38.6° the other way; the two nearly cancel and what you see is a SLIDE. Reading the hips' world from the mapped subset reported the full 38.6° as real, and the figure swung bodily about her own axis |
| **translation, for every shared bone** | not only the rotated ones. A hip shifts weight without turning, and keying translation off the rotation set dropped exactly those |
| **the armature's UNITS, not only its axes** | a carried translation goes out to world and back. Alice's armature bakes centimetres (parent scale 0.01), Grace's metres; converting axes alone slid a figure a metre |
| **an ancestor's slide, into the hips** | when the sliding node has no counterpart on the target, its displacement folds into the target's hips rather than being lost |
| **LINEAR output** | keys are emitted at the union of every source key time, so between two output keys no source channel has one either — the source interpolates there and the target must too. Written `STEP`, the target held each pose and jumped |

#### The fingers

The humanoid names **52 bones, not 22**: five fingers × three joints × two hands, spelled as VRM 0.x
spells them, because `vrm_humanoid` already hands us `leftIndexProximal` from a file's own extension
block. Kept out of `CORE_BONES`, which is what a map is JUDGED on — most rigs are missing some finger or
other, and folding them in would report thirty bones missing on a figure whose body map is perfect.

**A finger cannot be squared up against a BODY axis.** A hand turns freely at the wrist, so no fixed
body direction stays perpendicular to a finger: measured, the best body axis available still reads 0.966
against one rig's index finger and 0.996 against another's thumb, either of which is a degenerate frame.
Arms and legs get away with it because a rest pose holds them roughly fixed against the torso. So the
reference comes from the hand's own bones — **across the knuckles** (`indexProximal → littleProximal`)
for the four fingers, worst 0.251, and **the palm normal** for the thumb, worst 0.382, because a thumb
points across the palm and the knuckle axis is exactly wrong for it.

The HANDS stay chain ends. A hand's canonical frame has always come from `lowerArm → hand`, and letting
a thumb redefine it would move every wrist in the corpus to fix nothing.

#### Caching, and why it carries a revision

Content-addressed: a (clip, rig) pair is computed once ever and every later play is a lookup — the
rewrite is seconds on a 40-second clip and it used to run on every play. But **a content address
fingerprints the bytes, not the method that made them**, so the cache lookup is gated on
`retargeted_from` + `rig_sig` + `RETARGET_REV`. Without the stamp a pair retargeted by an older build is
handed back forever and a fix reaches nobody — which happened: the fix landed, the server restarted, and
the figure kept swinging because the cache still held the clip made before it.

**The voice comes with it.** A rewritten clip is the same performance on another body, and the voice was
recorded against the performance rather than against the skeleton.

### 8d. The face — `figure-face`, and the two expression rigs

A figure who blinks and looks at you is a different presence. Driving one is small: a morph weight is
one scalar per target, so there is no new client path.

**Measured across 38 rigged figures. 22 carry morph targets; two carry a face.**

| figure | targets | facial | vocabulary |
|---|---|---|---|
| Saka | 57 | **57** | VRM presets — `Fcl_ALL_Joy`, `Fcl_BRW_Angry`, `Fcl_MTH_A` |
| Alice | 34 | **26** | Character Creator / ARKit-ish — `Brow_Raise_Inner_L`, `Jaw_Open` |
| Bianca, Blondie | 22, 21 | 0 | **tongue only** — no brows, no eyes |
| Moon Girl, Kawaii, Goddess | 14, 3, 3 | 1 | one stray `closed_eyes_correction` |
| everyone else | 2–17 | 0 | skin and clothing colour, anatomy |

So `smile` cannot be a target name. It is a semantic request — exactly as `leftUpperArm` is a semantic
bone — resolved per scheme by `conjure/expressions.py` into whatever that figure's author called it.
`set_expression` / `POST /figure/expression` take `{smile: 1}`, `{blink: 1, look_left: 1}` or a RAW
target name, which wins over the table so an inspected figure can be driven directly — including the
tongue-only rigs, which have no scheme and are perfectly drivable by name.

Weights compose **additively and clamp at 1**: a `surprised` and an `aa` both reach for `Jaw_Open`, and
letting whichever came last in a dict win would make the result depend on key order.

**A request a figure cannot meet is reported, never approximated.** VRM aims the eyes with BONES, so
that rig has no `look_*` morphs at all; substituting a head turn would be a lie and returning nothing
silently reads as the tool being broken.

Its own component and not a property of `figure`, for the reason `figure-parts` is: `figure` refuses to
act without a humanoid map and an anatomical frame, and a face needs neither.

#### Why a table here is not the thing §8c warns about

[`backlogs/figures.md`](../backlogs/figures.md) argues that a morph *retarget* has "names and nothing
else" — `Fcl_ALL_Joy` is not `Brow_Raise_Inner_L` in any sense a measurement can establish. That stands,
and it is about carrying one figure's PERFORMANCE onto another.

This is a different job, and unlike that one **it is checkable**. Mapping `smile` → `Fcl_ALL_Fun` reads
the author's own label (discovery layer 0, the same move as `vrm_humanoid()`), and a morph target
carries position deltas — so a claim about what it does can be tested against where and how it moves
the mesh. Two independent checks, both green on both rigs:

**`check_regions`** — brow above eye above mouth, which is anatomy, not convention. The bands do not
overlap at all, so anything looser would pass a table that has genuinely gone wrong:

| | brow | eye | mouth |
|---|---|---|---|
| Saka | 1.4228–1.4257 | 1.3973–1.4104 | 1.3387–1.3440 |
| Alice | 1.5989–1.6018 | 1.5658–1.5854 | 1.5135–1.5237 |

**`check_directions`** — a shape must move the mesh the way its NAME says. This catches what the region
check cannot: a rig with `Brow_Raise` and `Brow_Drop` modelled the wrong way round passes the regions
completely (both are brows, both in the brow band) and puts an angry face on every request for a
surprised one.

Three things these checks caught while being built, and each was a real defect rather than a hypothetical:

- **A vacuous pass.** `_read_vec3` returned early on an accessor with no `bufferView`, which is exactly
  how a **sparse** accessor is stored — and sparse is how a morph target is normally stored, because a
  target moves a few hundred vertices of a mesh with sixty thousand. Alice's entire face measured as
  moving nothing, so there were no bands, so there were no violations, so the check reported OK. A green
  result that cannot go red is worse than no check, because it is trusted. `check_regions` now
  distinguishes *not applicable* (a figure with one stray facial shape makes no ordering claim) from
  *not measured* (a recognised expression rig that yields no bands is a failure).
- **A composite is not a feature.** `Fcl_ALL_Joy` is facial — the most facial thing on the rig — and has
  no band of its own, because it moves brows, eyes and mouth together. The same shape of mistake was
  then made twice: the direction check asserted it must RISE, on the reasoning that joy is a smile.
  Measured, it falls (-0.00376), and correctly — `Fcl_EYE_Joy` is -0.00584, because happy eyes close and
  an eyelid closes downward, and `Fcl_MTH_Joy` is -0.00246 because the mouth opens. Both checks now
  exclude whole-face composites, for one reason.
- **The catalog count meant nothing.** `morph_targets` summed `len(primitive.targets)` over every
  primitive of every mesh, so a vocabulary shared by four primitives counted four times: **Saka read 399
  for 57 real targets and Alice 172 for 34.** "Who can smile" was already a query and it was querying
  that number. It is now a count of DISTINCT names, beside `morph_names`, `expression_scheme` and
  `facial_morphs` — and `facial_morphs` is the one that answers the question, because a count of targets
  says a figure is expressive when it can only change its shorts.

#### What has the least evidence behind it

Stated rather than implied, in `expressions.PROVENANCE`. The VRM table is one entry per expression and
the author states the emotion outright. The Character Creator table names muscles, so its **emotions are
composites we author** — FACS-ish readings (a smile is zygomatic; a real one crinkles the eyes; anger
drops the brows) and not measured. Its **visemes are weaker still**: that rig has no `A/I/U/E/O`, so
`ee` is a spread mouth and `ou` a pursed one, good enough to read as speech at conversational distance
and not claimed to be more.

### 8e. Carrying the FACE through a retarget

*The approaches this rejected on the way — and they are the reusable part — are in
[`investigations/facial-expression.md`](../investigations/facial-expression.md).*

§8c maps a clip through the humanoid map, and the map has **51 bones — core plus fingers — none of
them facial**. So every retargeted clip arrived with a dead face, and that was almost the whole corpus:
**519 of 543 captured clips rotate a facial bone**, and not the handful with facial names. `1_idle`
spends **9,031° on a face** with `DEF-lid.T.L`/`.R` as its top two movers at 15% and 14%; a figure
playing it *natively* has been blinking all along.

Facial bones are carried by their **local delta** — the rotation the clip applies to its own rest,
re-applied to the target's rest — and not through the law. That is forced, not preferred: the law
squares a bone up against a frame derived from **where it points**, and a facial bone frequently points
nowhere. Every `DEF-jaw` in the corpus is a leaf, and so is every one of Eve Maccaro's lids.

Never the source's absolute local, either. Two faces of the same build rest differently because they
*are* different faces; pasting one rest onto the other is a shape transplant, not a performance.

#### The gate, and the two frames that were wrong first

A name match is **not** evidence. Eve Maccaro's facial bones carry the identical Rigify names and rest
inverted; carried by name alone, a blink swings her lid the wrong way while every count says it worked.
So each bone is gated on how far the two rigs' rests disagree, and refused past `FACE_REST_TOLERANCE`
(30°, against a blink of 16–26°).

**Which rest** took two wrong answers to find:

| frame | why it fails |
|---|---|
| **world** | a clip and a figure disagree about the whole armature — `1_idle`'s head rests **180°** from Barbie's — so every facial bone reads ~175° apart and every one is refused, on rigs that agree to 14° |
| **head-relative** | depends on both humanoid maps choosing the same vertebra, and they do not: office-babe's `head` is `DEF-spine.007` where Barbie's and the clip's are `DEF-spine.006`, injecting 73° of pure bookkeeping |
| **local** ✓ | a local delta is indifferent to every ancestor above the bone, so this is the only frame neither the armature nor the bone-map's choice can corrupt |

Measured against `1_idle`'s 65 facial bones: Akari 11.2° median, Barbie 14.0°, office-babe 14.3° —
and **Eve Maccaro 142.6°**, refused. Per bone rather than per rig, so a figure carries what can be
justified and drops what cannot, and both counts appear in the clip's notes.

#### What it actually buys, measured

**The direction matters, and measuring only one of them understates it by an order of magnitude.**
Carrying the capture set's own clips outward reaches one figure. Carrying office-babe's clips *inward*
reaches all sixteen — and hers include `1_idle_speaking`, which is lip sync.

| borrow | body bones | face carried |
|---|---|---|
| **office-babe's 21 face-driving clips → each of the 16 capture figures** | 116–131 | **~50: jaw 6, lips 6–8, eyelids 8–16, brows 16–18** |
| the capture set's 455 face-driving clips → office-babe | 127 | 61, incl. 16 eyelids |
| the Character Creator rig's 14 → 9 capture figures | ~55 | 2 eyelids — a fragment |
| → Eve Maccaro | 27 | 6 carried, **63 correctly refused** |
| → Grace, Trish, Yuffie, Alice, Blondie, Saka | — | **nothing** |

Across the whole library, **28 of 132 cross-rig figure pairings carry eyelids**; weighted by clips that
is roughly 1,400 (clip, figure) borrowings that now deliver a face where **none did before**.

Within the 16 figures sharing the capture rig nothing is borrowed at all — a native play keeps every
track whose name resolves, which is 38% of all pairings in the library and has always worked.

The figures that gain nothing are blocked on a different problem: Daz spells an eyelid
`eyelidUpper.L`, Character Creator `lEyelidUpper`, Rigify `DEF-lid.T.L`. Carrying between families
needs a name map, and the local-rest gate cannot justify one — across two different rig builds the
rests disagree by construction, so the check that makes this safe would refuse it.

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

### 8f. Hands you WEAR — `hand-rig`

A placed hand model driven by the headset's own hand tracking, all 25 joints, every frame. It is not a
figure: nothing about it is posable, no clip plays on it, and while worn it **is** the wearer's hand.

**It needs no discovery layer, and that is the whole reason it is cheap.** These models are authored in
the WebXR joint frame — their nodes carry the WebXR joint names exactly and each bone lies along its own
local −Z — so the two indirections the rest of this document rests on, *which node* (§3) and *which way*
(§4), collapse to identity. A bone's world transform **is** the joint pose. Nothing is inferred, and a
hand named any other way is refused with a reason rather than guessed at
([`decisions.md`](../decisions.md) §31).

**What import records** (`conjure/hands.py`, `FRAME_REV` 20):

| attribute | |
|---|---|
| `hand_joints` | `{webxrJointName: nodeName}` — an identity map today, written out anyway so a second naming scheme changes one place |
| `hand_side` | `left`/`right`, **measured** from the hand's own chirality, never read from the `_L` in a filename |
| `hand_wearable` | did it pass the gate |
| `hand_materials`, `hand_images` | what it is dressed in, so a **pair** can be chosen. The catalog's five hand files are not one pair: two are the textured VR set, one an orphaned AR left, and two rights carry no materials at all. Taken in order, the first wearing put a grey untextured right beside a textured left — which reads as a broken import rather than as two files that were never a pair. `conjure-ctl wear --pair` matches on material NAMES, which is a stronger signal than counting textures because the orphaned left is textured too. |
| `hand_problems` | why not, when it did not — recorded rather than swallowed, so "not a hand" and "a hand we rejected" are distinguishable |

The gate asks two things: all 25 joints present, and every bone along its own local −Z. **Not** that the
joints form a parent→child chain — the catalog's five hand files are two skeletons whose 25 joints are
*siblings* under one node, so that check would have refused the files it was written for. And the
**wrist is exempt** from the axis test: five bones leave one frame and it cannot point along all of them
(measured 0.706–0.975 there against ≥0.986 down every finger), which is exactly why the WebXR spec
leaves that joint loose.

**State** is `components.hand-rig = {hand, joints}` — durable world state, not a per-client setting, so
it persists, replays on reload, and taking it off names a real place to put it
([`decisions.md`](../decisions.md) §30).

**The runtime** composes each bone's **world** matrix from the joint pose and a scale, then divides it
back through the parent. Going through world is what stops the scale compounding: a bone's world scale
is exactly `s` however deep it sits, and the same code works whether a file's joints are a chain or
siblings.

**`s` is the median ratio over the 14 real BONES, not all 24 inter-joint distances.** `wrist →
*-metacarpal` is the offset between an arbitrary frame origin and the hand; `*-distal → *-tip` compares a
runtime-derived surface point against an authored tip bone. Neither is a bone length. Measured on a
Quest 3, including them spread the ratios **36%** on a hand whose real bones agree to **5%** — so a
scale taken over 24 would be biased by several per cent on every wearer.

`s` does one job and it is worth being precise about which: **joint positions carry length and never
girth.** Every joint is placed at the pose the runtime reports, so positions are exact by construction
and a scale error cannot accumulate down a chain; `s` scales the flesh, and 5% of a finger's girth is
under a millimetre. It is eased frame to frame, because girth is a slowly-varying property of a hand
rather than a per-frame measurement.

**`tipOut` — fingertips, and a tunable that is marked as one.** Worn on a Quest 3, the wearer's real
fingertips protruded about 5 mm beyond the virtual ones. Not a placement error: the tip joint lands
exactly where the runtime says, as every joint does. It is the *flesh* — the fingertip cap is bound to
the tip bone, and `s` scales it by the girth ratio, which says nothing about how far a fingertip sticks
out.

`tipOut` pushes each tip bone along its own local −Z (the bone direction away from the wrist) by a
number of millimetres. **Default 0**, which is the only defensible default: everything else here is
exact by construction, and a non-zero default would quietly make that untrue.

**It is dialled live.** `conjure-ctl wear --tip-out MM` with no id adjusts every worn hand and takes
effect on the next frame. The component reads `tipOut` per frame and has **no `update` handler** — so
A-Frame replaces `this.data` on the patch and `init` does not re-run, leaving the collected skeleton,
the captured bind positions and the rest matrices intact. Finding a number by trying numbers should not
cost a page reload each time, and it does not.

*It is a tunable and not a derivation, and the first attempt at making it one is worth keeping.* That
version scaled each tip bone by its finger's tracked ÷ bind ratio for `*-distal → *-tip`, reasoning from
§8f's phase-0 measurement that this is the one segment where the model and the runtime measure different
things. It is — but the ratio came out **below one**, so the caps shrank: the fingers went pointy and
got shorter still. The reasoning was sound and **the sign was an assumption**, and that segment being
unreliable does not tell you which way it is unreliable.

**`radius` — the candidate rule.** `tipOut` also takes `radius` (or `radius:0.8`), which pushes each
finger out by **its own** reported `XRJointPose.radius`. Worth trying because **8 mm** landed it for one
wearer and a human fingertip radius is about 8 mm — and there is a reason that would be no coincidence:
the WebXR tip joint sits at the **centre** of the fingertip and `radius` is that fingertip's radius, so
the surface is one radius further out. If that is the rule then it is not one person's 8 mm at all: it
is per-finger (a thumb is fatter than a pinky) and it generalises to any hand the runtime measures.

The two modes are distinguishable on device precisely because of that per-finger difference, which a
flat 8 mm cannot reproduce. A missing or preposterous radius falls back to **no offset**, never to `NaN`.

The component logs what is knowable once per wearing under `--debug-log` — each finger's tracked and
bind `distal → tip`, their ratio, the radius reported at the tip, and the offset each mode resolves to
— so the question can be read off a headset rather than reasoned to.

**Still open:** whether the default becomes `radius`. It stays `0` until `radius` and the wearer's
millimetre figure are compared on a head, because shipping a default on the strength of an argument is
exactly what the tip-scale attempt did.

**Precedence: live hand > clip > pose.** Settled by taking the other two writers *off* — `figure-clip`
is paused and `figure.restore()` is called on wear — rather than by winning a race with them, since
A-Frame gives no tick order between components on one entity.

**All or nothing per frame.** A partial joint read is discarded: some bones driven and the rest at rest
renders as a hand tearing itself apart, which is worse than a hand that stops. Tracking loss is held for
400 ms before the skeleton goes back to rest, because hand tracking blinks out constantly and snapping
on every gap reads as a twitch rather than as loss.

**Two exemptions while worn**, both because the entity's transform is meaningless then: it is skipped by
`_placeContent`'s plane-relative anchor re-solve (skipped, not cleared — `_frefPose` is where it goes
back to), and excluded from `grab`'s picking, where a selection box would otherwise sit metres from the
hand you can see.

**Surface:** `POST /figure/hand` · `wear_hand` for the director · `conjure-ctl wear <id> --hand
auto|left|right|off`. `auto` pairs on the measured side; asking for the **wrong** side is refused, because
a mirrored glove reads as broken tracking rather than as a mistake anyone made.

`GET /figure/hands` answers *which* — wearable hands in the library, and hands already placed, with
which of them is worn. It exists because `wear <entity>` is otherwise unanswerable: a hand has to be
placed before it can be worn and nothing in the CLI placed a library model, so `conjure-ctl wear` with
no id lists both and `--pair` places both sides and wears them. Same gap `scripts/faces.py` filled for
faces — `dir` lists assets and `inspect_figure` answers for one already placed, and neither answers
*which of these can do the thing*.

**Not verified on a headset.** The arithmetic is tested against a fake skeleton in `tests/js/` and the
extraction in `tests/test_hands.py`; the XR read itself needs the device.

## 9. How a figure gets here

Two out-of-band ingest paths, and **both moved to [`specs/captures.md`](./captures.md)** on 2026-09-13:
re-assembling a captured PlayCanvas build (§3 there) and the Blender conversion for a `.blend` or a
Daz/Reallusion export (§5). They left because the same pipeline now carries rooms, props and audio, so
describing it under "figures" sent anyone looking for a captured prop to the wrong spec.

What stays here is everything that happens once the bytes arrive — what makes a model a figure (§1),
what import records about one (§2), and how its bone map is recovered (§3).

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

### Measuring a retarget — and why a model of the client is not the client

Everything else here models the client in Python and compares files. For retargeting that hid **four
separate defects in a row** — a rotating ancestor nothing carried, translation channels dropped
wholesale, an armature's units, and a cache serving a clip from an older build — because a model of the
client is not the client. So these run the real thing: `figure-clip.js`'s own `retarget()`, a real
`AnimationMixer`, seeked exactly the way `tick()` seeks it.

| harness | the question it answers |
|---|---|
| `scripts/retarget_probe.py` | **the specification.** Every rigged figure in the catalog, naive vs delta vs absolute, limbs and whole-body. `--control N` rolls every bone's rest, a case whose right answer is known to be 0. `--swing` restores the term the law dropped, and is the only place it still exists |
| `scripts/clip_diff.mjs` | are two (clip, figure) pairs doing the same thing over time — hips movement, body turn, worst single-segment limb |
| `scripts/clip_tilt.mjs` | the ABSOLUTE body axis, which `clip_diff` structurally cannot see: it reports every angle relative to each figure's own `t=0`, so a constant lean reads clean |
| `scripts/clip_stage.py` | resolves names against the library and writes clip, figure and retarget as files, so two builds can be compared with everything else held identical |
| `scripts/clip_check.mjs` | binding, plus the client's own drop report |

**Read every row as its excess over the floor.** `Jane → Jane` — the same clip on its own rig — scores
5.3° on limbs and 4.0° on the body, because channel loss is not retargeting's to fix. A figure scoring
*below* the floor is not doing well; it is the tell that the metric is measuring the thing it grades.

#### Eight ways a metric has been wrong here, and only twice was the code

This campaign's record is that **every hypothesis reasoned to was wrong and every one a measurement
found was right** — but a measurement is only as good as what it measures, and these cost more time than
the defects did.

- A limb metric in each figure's **own body frame** cannot see a figure turned bodily, which is the
  largest error there is. The naive copy left Alice 79° out and still scored well.
- `hips → hand` spans the whole torso AND the whole arm, so its direction is set by **proportion**, not
  pose: one figure's arm-to-torso ratio is 1.24 against another's 0.96, and the two differ by 31° at rest
  with nothing playing. Reported as retargeting error it made a correct pair look 50° wrong.
- Posing the two sides with **different bone sets** fakes an identity match.
- Measuring a bone against its parent's **rest** rather than where the parent actually moved: 32°.
- A threshold chosen by geometry — `abs(dot(along, up)) < 0.99` — put two rigs in the same rest pose on
  either side of the cut and ended them a half-turn apart. **A fixed table, never a measurement.**
- The body metric is the angle between two POSED body frames, and `swing` was constructed to null
  exactly that, so **the term graded itself**. Four figures scored below the floor.
- A **range** metric (min/max over a clip) cannot see an interpolation defect, because `STEP` and
  `LINEAR` visit the same extremes. Only a comparison at an instant exposes it.
- A **rendered thumbnail of every mesh** describes a figure's whole wardrobe stacked on itself, and a
  vision model narrates the result fluently and with confidence. Not a retargeting metric, but the same
  failure: the instrument was pointed at the wrong thing and answered anyway.

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
| `POST /figure/parts` | hide/show parts of a figure by category or mesh name (§8a) |
| `POST /figure/clip` | play a clip on a figure, or `stop=true`. Refuses a clip from another rig unless `force` (§8b) |
| `GET /figure/clips` | what shipped with a figure; `all=true` reaches past it, `voiced=true` keeps only clips with a voice |
| `POST /library/import` | ingest a `.glb`/`.vrm` — the figure attributes come out of this path |
| `POST /library/refresh-models` | re-derive every model row's attributes |

**MCP tools:** `inspect_figure` (height, triangle count, the bones this figure actually has, current
pose), `list_poses` (the named library, read from the data rather than written into a prompt),
`pose_figure`, `dress_figure` (§8a), and `list_clips` / `play_clip` / `stop_clip` (§8b). Neither is in `_READONLY_TOOLS`, so a `access: "read"` agent gets neither.
`search_library` annotates a rigged hit with `[figure 1.76 m, 348k tris]` — the two facts that decide
which of six near-identical figures to place.

**CLI — the deterministic figure surface, no LLM.** One verb per director tool, so anything the
director can do to a figure can be reproduced without one; this is the path for debugging a figure when
the answer is "it looked wrong in the headset".

| Command | |
|---|---|
| `conjure-ctl pose <entity> [named] [--bone N:AXIS=V] [--clear]` | `--bone` repeats and composes onto a named pose; it reports joints that clamped |
| `conjure-ctl dress <entity> [--hide C] [--show C] [--only-body]` | categories or mesh names; reports what is still ON, not what the figure owns |
| `conjure-ctl clips <entity> [--all] [--kind K]` | what shipped with her, apart from what merely fits |
| `conjure-ctl clip <entity> <label\|id> [--stop] [--once] [--speed] [--force]` | naming no clip is the same request as `--stop` |

These live in `ctl` and not in the shell because the shell proxies only the world server's CONTROL
surface — `/admin/{tree,show,match,file,delete}`, `/admin/gc`, `/admin/settings`, `/agent/last`,
`/scope/activate` — and every one of those calls carries a namespace PATH. A figure verb carries an
entity id, hits a content route, and authenticates with `x-conjure-scope` rather than `X-Conjure-User`.

`conjure-import` (ingest; `--label` names the asset, defaulting to the filename stem, and
is distinct from `--creator`, which is whoever made it), `conjure-ctl refresh-models [--force]`,
`python scripts/pose_eval.py` (the utterance-layer battery), `scripts/pose_test.py` (render one pose).

**Deps:** none new. GLB reading is stdlib; `trimesh` was already there. Blender is a soft dependency of
the conversion scripts only, never of the world server.

## 12. What is not built

Recorded here so the spec can be trusted about its own edges; the design work is in
[`backlogs/figures.md`](../backlogs/figures.md).

- ~~**No animation.**~~ Built 2026-09-13 as §8b: `figure-clip`, `POST /figure/clip`, and
  `play_clip` / `list_clips` for the director. A pose and a clip compose by the rule in §8b — the clip
  wins, the pose returns on stop. **Retargeting** followed on 2026-09-16 (§8c), so a clip is rewritten
  for a rig it was not authored for rather than refused — which is what the last ten figures in the
  catalog needed to be offered a clip at all. The
  teacher's shut eyes are now a thing that can be fixed by playing her `Blink.glb` rather than a thing
  with no mechanism, though nothing plays it automatically at placement.
- ~~**No outfits.**~~ Built 2026-09-13 as §8a: a parts vocabulary classifies each mesh at import, and
  `dress_figure` / `POST /figure/parts` turn categories off and on at runtime. What is still absent is
  the other mechanism — clothing that arrived as a SEPARATE CONTAINER (Jane's `hair.glb`) is a separate
  entity, and taking it off means removing that, which the endpoint reports rather than does.
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
- ~~**No morph support.**~~ Built 2026-09-16 as §8d: `figure-face`, `POST /figure/expression` and
  `set_expression`, over a semantic vocabulary resolved per expression rig. **Driving** a face, not
  retargeting one — §8d says why that distinction is what makes the tables checkable. Two figures in the
  catalog can use it, which is a fact about the corpus and not a limit of the mechanism.
- ~~**Bone-driven facial performance is unreachable.**~~ Carried since 2026-09-16 — see §8e. What is
  still unreachable is a face across a NAMING FAMILY: Daz spells an eyelid `eyelidUpper.L` and
  Character Creator `lEyelidUpper`, neither of which a Rigify clip names, so nothing maps.
- **No MORPH-driven facial performance to carry.**
  Corrected 2026-09-16 after measuring rather than counting channels. 249 of 543 captured clips drive
  morph weights and **none of them drive a facial target** — those channels are `Dress` and `Body`, and
  a clip GLB carries no meshes, so the weights are anonymous anyway.

  The facial performance in this corpus is on **bones**. These captures use a Rigify facial rig, and
  `pc_blink` spends 83° of its travel on `DEF-lid.T.L`/`.R` with the top six movers all eyelids;
  `1_idle_speaking` puts **74.9% of all its motion into `DEF-jaw_master`**, which is lip sync. 29 of 543
  clips have a facial name.

  Two things follow, and both are open:
  - **They play natively but cannot be retargeted.** `pc_blink` matches 16 figures by rig signature, so
    `play_clip` offers it on all of them today. But the humanoid map covers 51 bones — core plus
    fingers — and **none are facial**, so §8c cannot carry a facial clip onto a rig that does not
    already share the skeleton.
  - **`clip_activity` cannot see a held pose.** It measures angular TRAVEL, so a two-keyframe clip that
    holds an expression reads as motionless: `pc_mouth_opened` has 3° of travel and sits 16° off rest.
    Measured this way `pc_squint` is a genuine no-op — 0.13° from rest — so a facial-sounding name is
    not evidence that a clip does anything.
- **No spring-bone or MToon support.** VRM material data is in the file and A-Frame's plain glTF
  loader ignores it.

## 13. Related specs

- [`specs/captures.md`](./captures.md) — how a captured build becomes a figure at all: the
  grabber, PlayCanvas reconstruction, import, and the Blender path. Was §9a/§9 here until
  2026-09-13.
- [`specs/library.md`](./library.md) — the catalog, the `attributes` bag, and the one ingest
  write-through these attributes ride.
- [`specs/worlds-surfaces.md`](./worlds-surfaces.md) — how a placed asset becomes an entity.
- [`specs/spaces-geometry.md`](./spaces-geometry.md) — placement modes and the plane-relative anchor a
  figure is placed with; also the +Z facing convention the body frame measures rather than assumes.
- [`specs/dynamics.md`](./dynamics.md) — the sync tiers, and why `figure` is ordinary world state rather
  than a dynamic module.
