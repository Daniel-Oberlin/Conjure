"""Headless-Blender conversion: a rigged-character .blend → a GLB Conjure can load.

Phase 1 of the figures pipeline (docs/backlogs/figures.md). Conversion is a **stripping pass**, not a
format change — nearly all the size in these files is apparatus that glTF cannot carry anyway.

    B=/Applications/Blender.app/Contents/MacOS/Blender
    $B --background <file.blend> --python scripts/blend_to_glb.py -- --list
    $B --background <file.blend> --python scripts/blend_to_glb.py -- out.glb \
        --collections "Body,Eyes,Default,Hair 2" [--keep-morphs] [--dry-run]

What it strips, and why (measured on the four sample models):
  - unselected collections   outfits and hair variants are alternatives; one set is worn at a time.
                             Grace ships 3 outfits + 3 hair variants ALL visible at once, so she would
                             otherwise export wearing three outfits stacked.
  - shape keys               Daz JCMs are fired by Blender DRIVERS, which glTF has no concept of, so
                             they are dead weight — ~90M vertex deltas on Grace alone. Dropped via the
                             exporter's export_morph flag rather than by mutating the mesh.
  - rig widgets              bone custom-shape objects: UI, not content. 175 of Eve's 182 objects.
  - non-mesh/armature        empties, cameras, lights, lattices.

**Widgets are found by reference, not by name.** Eve's use Rigify's `WGT-` prefix; Grace's use `GZM_`.
Any name-prefix rule is a guess that silently fails on the next rig, so instead we collect every object
referenced as a `pose_bone.custom_shape` — that is what a widget *is*, and it holds for any convention.

**Object hide flags are not enough either.** Grace's widgets sit in a collection named `Hidden` while
their own `hide_viewport` is False, so object-level visibility reports them as shown. Collection
visibility is checked separately and inherited down the tree.

Deliberately NOT stripped yet: control/IK bones. They arrive inert (glTF carries no constraints) and
bone reduction needs the humanoid map to know what is safe to remove — see the backlog.

Prints a before/after report to stdout so a conversion is auditable without opening the result.
"""
import math
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
positional = [a for a in argv if not a.startswith("--")]
out_path = positional[0] if positional else "/tmp/out.glb"
KEEP_MORPHS = "--keep-morphs" in argv
DRY_RUN = "--dry-run" in argv
LIST_ONLY = "--list" in argv


def opt(name, default=None):
    for a in argv:
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    if f"--{name}" in argv:
        i = argv.index(f"--{name}")
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            return argv[i + 1]
    return default


WANT = [c.strip() for c in (opt("collections") or "").split(",") if c.strip()]
MAX_TEX = int(opt("max-texture", "2048"))          # 0 disables; see the texture pass below
IMG_FMT = opt("image-format", "AUTO")              # AUTO | JPEG | WEBP
BAKE = int(opt("bake", "0"))                       # px; bake non-Principled materials (0 = off)
STRIP_CONSTRAINTS = "--strip-constraints" in argv      # opt-in; see strip_constraints()
REPARENT = "--no-reparent" not in argv                 # default ON; see reparent_deform_bones()
FIX_UDIM = "--fix-udim" in argv                        # opt-in; see normalize_udim_uvs()

# ---- what is a widget: referenced as a bone's custom shape, whatever it is called -----------------
widgets = {pb.custom_shape for ob in bpy.data.objects if ob.type == "ARMATURE"
           for pb in ob.pose.bones if pb.custom_shape}


def collection_hidden(coll):
    return bool(coll.hide_viewport or coll.hide_render)


hidden_colls = {c.name for c in bpy.data.collections if collection_hidden(c)}


def visible(ob):
    """Worn/used as the porter left it — object flags AND any collection it lives in."""
    if ob.hide_viewport or ob.hide_render:
        return False
    return not any(c.name in hidden_colls for c in ob.users_collection)


def rig_of(ob):
    return next((m.object for m in ob.modifiers if m.type == "ARMATURE" and m.object), None)


# ---- inventory --------------------------------------------------------------------------------
by_coll = {}
for ob in bpy.data.objects:
    if ob.type != "MESH" or ob in widgets or not len(ob.data.vertices):
        continue
    for c in ob.users_collection:
        by_coll.setdefault(c.name, []).append(ob)

if LIST_ONLY:
    print(f"\n=== collections in {os.path.basename(bpy.data.filepath)} ===")
    print(f"  ({len(widgets)} rig-widget objects excluded)\n")
    for name in sorted(by_coll, key=lambda n: -sum(len(o.data.vertices) for o in by_coll[n])):
        obs = by_coll[name]
        v = sum(len(o.data.vertices) for o in obs)
        vis = sum(1 for o in obs if visible(o))
        rigs = sorted({(rig_of(o).name if rig_of(o) else "—") for o in obs})
        print(f"  {name[:34]:<34} {v:>8,}v  {len(obs):>2} meshes  "
              f"visible={vis}/{len(obs)}  rig={','.join(rigs)[:28]}")
    print()
    sys.exit(0)

# ---- choose what to export --------------------------------------------------------------------
meshes, armatures, dropped = [], set(), {"hidden": 0, "widget": len(widgets),
                                         "not_selected": 0, "empty": 0, "other": 0}
for ob in bpy.data.objects:
    if ob.type == "MESH":
        if ob in widgets:
            continue
        # Naming a collection overrides ITS hidden flag — Grace's outfit alternates are all
        # collection-hidden, so an explicit "Default" must still select them. It does NOT override
        # per-object hiding: Yuffie's `Hair` holds two variants with one switched off, and "give me the
        # hair" plainly means the one she wears, not both stacked (136k wasted verts).
        picked = (any(c.name in WANT for c in ob.users_collection)
                  and not (ob.hide_viewport or ob.hide_render)) if WANT else visible(ob)
        if not len(ob.data.vertices):
            dropped["empty"] += 1
        elif not picked:
            dropped["not_selected" if WANT else "hidden"] += 1
        else:
            meshes.append(ob)
            if rig_of(ob):
                armatures.add(rig_of(ob))
    elif ob.type != "ARMATURE":
        dropped["other"] += 1

if not meshes:
    print("\n  NOTHING SELECTED — check --collections against --list\n")
    sys.exit(1)

keep = set(meshes) | armatures
verts = sum(len(m.data.vertices) for m in meshes)
polys = sum(len(m.data.polygons) for m in meshes)
morphs = sum(len(m.data.shape_keys.key_blocks) * len(m.data.vertices)
             for m in meshes if m.data.shape_keys)
bones = sum(len(a.data.bones) for a in armatures)

print("\n=== blend_to_glb ===")
print(f"  source        {os.path.basename(bpy.data.filepath)}")
print(f"  collections   {WANT or '(all visible)'}")
print(f"  dropped       widget={dropped['widget']} unselected={dropped['not_selected']} "
      f"hidden={dropped['hidden']} empty={dropped['empty']} other={dropped['other']}")
print(f"  KEEPING       {len(meshes)} meshes / {len(armatures)} armatures "
      f"({', '.join(sorted(a.name for a in armatures))})")
print(f"                {verts:,} verts  {polys:,} polys  {bones} bones")
print(f"  max texture   {MAX_TEX or 'unlimited'}px, format {IMG_FMT}")
print(f"  bake          {str(BAKE) + 'px' if BAKE else 'off'}")
print(f"  morph deltas  {morphs:,} " + ("(KEPT)" if KEEP_MORPHS else "(dropped at export)"))
for m in sorted(meshes, key=lambda m: -len(m.data.vertices)):
    sk = len(m.data.shape_keys.key_blocks) if m.data.shape_keys else 0
    r = rig_of(m)
    print(f"    {len(m.data.vertices):>7,}v  {m.name[:38]:<38} sk={sk:<4} rig={r.name if r else '—'}")

if DRY_RUN:
    print("\n  (dry run — nothing written)\n")
    sys.exit(0)

# ---- select exactly that set and export -------------------------------------------------------
# The exporter's use_selection walks the VIEW LAYER, so anything not linked into the active scene is
# invisible to it. These are "append model" files, so link the keepers in explicitly first.
scene = bpy.context.scene
for ob in keep:
    if ob.name not in scene.collection.objects:
        try:
            scene.collection.objects.link(ob)
        except RuntimeError:
            pass                                     # already linked somewhere in the scene tree

def reparent_deform_bones(arms):
    """Re-parent each deform bone to the control its CONSTRAINT names, rebuilding the deform chain.

    Rigify and Daz separate a joint into an FK control a human grabs (`upper_arm.L`) and deform bones
    carrying the vertex weights (`upper_arm.bend.L`), linked by a constraint. glTF drops constraints, so
    the exported skeleton has deformers hanging off the WRONG bone and the chain breaks at every joint —
    zig-zag arms and hips twisting toward the knees. Measured on Grace:

        upper_arm.bend.L   constraint -> upper_arm.L    but parented to arm_parent.L
        forearm.bend.L     constraint -> forearm.L      but parented to upper_arm.L
        thigh.bend.L       constraint -> thigh.L        but parented to leg_parent.L

    The constraint states the relationship the format cannot carry, so we convert it into one the format
    CAN carry: parenting. Afterwards, rotating a control moves its own deformer and everything below it,
    which is what posing needs.

    Not `export_def_bones=True`, which was tried and is worse: Blender can only preserve a hierarchy that
    already exists, so it flattens these to the armature root.

    Re-parenting happens in EDIT mode, where head/tail are absolute — the bones do not move.
    """
    total = 0
    for arm in arms:
        # Re-parent any bone that deforms OR carries deformers beneath it. Rigs nest this to different
        # depths: Grace is control -> deformer (two levels), Yuffie is
        # `upper_arm.L -> upper_arm.bend.L -> upper_arm.bend.twk.L` (three, and only the last deforms).
        # Fixing just the deform level left Yuffie's intermediate bone hanging off the wrong parent, so
        # rotating her upper arm carried the forearm and hand but not the upper arm itself — reported as
        # a forearm disconnected from its elbow.
        def carries_deform(bone):
            stack = [bone]
            while stack:
                b = stack.pop()
                if b.use_deform:
                    return True
                stack.extend(b.children)
            return False

        links = {}
        for pb in arm.pose.bones:
            if not carries_deform(pb.bone):
                continue
            for c in pb.constraints:
                tgt = getattr(c, "subtarget", "") or ""
                if tgt and tgt in arm.pose.bones and tgt != pb.name:
                    links[pb.name] = tgt
                    break                                 # first constraint wins; they agree in practice
        if not links:
            continue
        bpy.ops.object.select_all(action="DESELECT")
        arm.hide_viewport = False
        arm.hide_set(False)
        arm.select_set(True)
        bpy.context.view_layer.objects.active = arm
        bpy.ops.object.mode_set(mode="EDIT")
        eb = arm.data.edit_bones
        done = 0
        for name, target in links.items():
            b, t = eb.get(name), eb.get(target)
            if not b or not t or b.parent is t:
                continue
            # Never parent a bone to something already beneath it — that is a cycle, and Blender will
            # either refuse or silently produce a broken skeleton.
            anc, cyc = t, False
            while anc is not None:
                if anc is b:
                    cyc = True
                    break
                anc = anc.parent
            if cyc:
                continue
            b.parent = t
            b.use_connect = False
            done += 1
        bpy.ops.object.mode_set(mode="OBJECT")
        total += done
        print(f"  reparent      {done} deform bone(s) onto their constraint target ({arm.name})")
    return total


def strip_constraints(arms):
    """Bake each armature's constraint-resolved pose into its bones, then remove the constraints.

    glTF carries no constraints, so they are lost either way — but LOSING them is not the same as
    RESOLVING them first. Grace's rig has circular constraint dependencies on `forearm.L/R` and
    `hand.fk.L/R` (Blender reports the cycle 40 times per load). Blender resolves the cycle well enough
    to draw; the exporter evaluates it differently and writes garbage for those bones, which collapsed
    her fingers into cones. The source renders correctly, so the geometry was never the problem.

    `visual_transform_apply` writes the resolved result into the pose channels, so appearance is
    preserved and the exported skeleton is plain FK with no cycles left to misevaluate.
    """
    done = 0
    for arm in arms:
        n = sum(len(pb.constraints) for pb in arm.pose.bones)
        if not n:
            continue
        bpy.ops.object.select_all(action="DESELECT")
        arm.hide_viewport = False
        arm.hide_set(False)
        arm.select_set(True)
        bpy.context.view_layer.objects.active = arm
        try:
            bpy.ops.object.mode_set(mode="POSE")
            bpy.ops.pose.select_all(action="SELECT")
            bpy.ops.pose.visual_transform_apply()
            for pb in arm.pose.bones:
                for c in list(pb.constraints):
                    pb.constraints.remove(c)
            bpy.ops.object.mode_set(mode="OBJECT")
            done += n
        except Exception as exc:  # noqa: BLE001 — never lose the export over this
            print(f"    ! {arm.name}: could not strip constraints ({exc})")
    if done:
        print(f"  constraints   resolved and removed {done} bone constraint(s)")


def select_for_export():
    """Select exactly `keep` and make an armature active. Called AFTER the bake, never before: baking
    is per-object and does its own select_all(DESELECT), so running this first leaves only the last
    baked object selected and `use_selection` silently exports one mesh (a 0.16 s "successful" export)."""
    bpy.ops.object.select_all(action="DESELECT")
    for ob in keep:
        ob.hide_viewport = False                     # a hidden object cannot be selected
        ob.hide_set(False)
        ob.select_set(True)
    bpy.context.view_layer.objects.active = next(iter(armatures), meshes[0])


# ---- bake materials glTF cannot represent -------------------------------------------------------
# Grace's skin renders grey in the headset because `FACE` exported with baseColorTexture=null and
# baseColorFactor=[0,0,0,1]. The cause is upstream of the exporter: the material's surface output is a
# MIX_SHADER (a dry/wet skin blend) over nested Daz `PBGv5` node groups, with no Principled BSDF anywhere
# in the tree. A glTF material is ONE PBR metallic-roughness definition — a mix of two shaders has no
# representation at all — so the exporter cannot resolve a base colour and emits black.
#
# There is nothing to rewire to, so the only general fix is to BAKE: evaluate whatever the tree computes
# into a flat image and rebuild the material as a plain Principled BSDF around it. Lossy by nature (the
# wetness layer collapses into the diffuse), but glTF could not have carried it regardless.
#
# Scoped to materials that actually need it — a material already rooted in a Principled BSDF exports
# correctly and is left completely alone.
def is_translucent(mat):
    """Does this material physically transmit light? The signal for the black-bake fallback.

    A material that bakes black has no view-independent colour, but that has TWO causes: it is glass
    (a lens, a watch crystal, eye moisture) or its shader is merely too complex to resolve (Grace's
    fingernails, Yuffie's boots). The right fallback is opposite in each case, and getting it wrong is
    visible — ghostly white boots on one model, frosted spectacle lenses on the other.

    `Transmission Weight` separates them, and it is physically meaningful rather than a heuristic:
    measured 1.0 on every lens/glass/moisture material and 0.0 on fingernails. glTF's alphaMode does
    NOT work — Grace's lens exports OPAQUE, same as boots.

    No Principled BSDF at all (Yuffie's materials) means we cannot tell, and opaque is the safer default:
    a slightly-wrong solid surface reads as untextured, a wrongly-transparent one reads as broken.
    """
    if not mat or not mat.use_nodes or not mat.node_tree:
        return False
    for n in mat.node_tree.nodes:
        if n.type != "BSDF_PRINCIPLED":
            continue
        t = n.inputs.get("Transmission Weight")
        if t is not None and not t.links and t.default_value > 0.5:
            return True
        a = n.inputs.get("Alpha")
        if a is not None and not a.links and a.default_value < 0.5:
            return True
    return False


def colour_image(mat):
    """The one COLOUR-DATA image in a material's graph, or None if there is not exactly one.

    A material the exporter could not resolve usually still HAS its diffuse texture sitting in the node
    tree — it is just behind a shader group the exporter cannot reduce. Trish's arms, legs, torso and
    teeth each carry four or five images, and in every case exactly one of them is tagged sRGB while the
    bump, specular, normal and micro-detail maps are tagged Non-Color.

    **That tag is the file telling us which image is colour**, not a guess about filenames — `_B` means
    bump on this rig and base on the next. Where it answers, no bake is needed at all, which also skips
    the failure below it: these skin shaders bake to black, and the neutral fallback for that is what
    turned Trish's arms and Grace's legs into pale grey placeholder.
    """
    found, seen = [], set()

    def walk(tree, depth=0):
        for n in tree.nodes:
            if n.type == "TEX_IMAGE" and n.image:
                if n.image.colorspace_settings.name != "Non-Color" and n.image.name not in seen:
                    seen.add(n.image.name)
                    found.append(n)
            elif n.type == "GROUP" and n.node_tree and depth < 4:
                walk(n.node_tree, depth + 1)

    if mat.use_nodes and mat.node_tree:
        walk(mat.node_tree)
    return found[0] if len(found) == 1 else None


def use_colour_images(objs, only):
    """Rebuild every unresolved material that already owns a colour image, and return the rest.

    Runs BEFORE baking, because baking is the fallback and not the plan: it is slow, it is destructive
    to bystander textures, and on a layered skin shader it produces black. Reaching for the image the
    author already provided is both cheaper and better.
    """
    rebuilt = []
    for name in sorted(only):
        mat = bpy.data.materials.get(name)
        node = colour_image(mat) if mat else None
        if node is None:
            continue
        image = node.image
        layer = None                                  # keep whatever UV map the original sampled with
        if node.inputs["Vector"].links:
            src = node.inputs["Vector"].links[0].from_node
            layer = getattr(src, "uv_map", None) or None
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = image
        if layer:
            uvn = nt.nodes.new("ShaderNodeUVMap")
            uvn.uv_map = layer
            nt.links.new(uvn.outputs["UV"], tex.inputs["Vector"])
        nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
        rebuilt.append((name, image.name))
    if rebuilt:
        print(f"  colour        {len(rebuilt)} material(s) rebuilt from their own texture, no bake needed")
        for name, image in rebuilt[:6]:
            print(f"                  {name[:24]:26} <- {image[:36]}")
    return {n for n in only if n not in {r[0] for r in rebuilt}}


def bake_materials(objs, size, only):
    """Bake the named materials on `objs` to a diffuse image and rebuild each as Principled.
    `only` comes from unresolved_base_colours() — never from guessing at the node tree."""
    only = use_colour_images(objs, only)
    targets = [(ob, i, sl.material) for ob in objs for i, sl in enumerate(ob.material_slots)
               if sl.material and sl.material.name in only]
    if not targets:
        print("  bake          nothing to bake (every material is Principled-rooted)")
        return
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = 1                    # flat colour: no lighting contribution wanted
    scene.render.bake.use_pass_direct = False
    scene.render.bake.use_pass_indirect = False
    scene.render.bake.margin = 4

    made, translucent = {}, {}
    for ob, _slot, mat in targets:
        if mat.name in made:
            continue
        img = bpy.data.images.new(f"bake_{mat.name}", size, size, alpha=True)
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        mat.node_tree.nodes.active = node       # `active` node per material is the bake destination
        made[mat.name] = (img, node)
        translucent[mat.name] = is_translucent(mat)

    # CRITICAL: `bake` writes into the active image node of EVERY material on the baked object, not just
    # the ones we asked for. Any bystander material whose active node happens to be its own diffuse
    # texture gets that texture OVERWRITTEN with the bake result — which is how baking `FACE` silently
    # replaced `Grace Arms D` with solid black and turned her hands black for six export attempts.
    # (Proof: the same texture is correct in an otherwise identical export with baking off.)
    # Give every bystander a throwaway destination so the bake has somewhere harmless to land.
    scratch = []
    for ob in {t[0] for t in targets}:
        for sl in ob.material_slots:
            m = sl.material
            if not m or m.name in made or not m.use_nodes or not m.node_tree:
                continue
            junk = bpy.data.images.new(f"scratch_{m.name}", 4, 4, alpha=True)
            node = m.node_tree.nodes.new("ShaderNodeTexImage")
            node.image = junk
            m.node_tree.nodes.active = node
            scratch.append((m, node, junk))

    def bake_uv_layer(ob):
        """A UV layer whose coordinates lie inside 0..1 — the bake writes into the ACTIVE layer, so if
        that is the UDIM atlas (Grace's `Base Female` spans u 0..7) every texel lands outside the image
        and the bake silently produces black. Prefer the render layer, else the first one that fits."""
        uvs = ob.data.uv_layers
        cands = [uvs.active] if uvs.active else []
        cands += [l for l in uvs if getattr(l, "active_render", False)]
        cands += list(uvs)
        for layer in cands:
            if layer is None:
                continue
            lo_ = hi_ = None
            for d in layer.data:
                u, v = d.uv
                lo_ = min(u, v) if lo_ is None else min(lo_, u, v)
                hi_ = max(u, v) if hi_ is None else max(hi_, u, v)
            if lo_ is not None and lo_ >= -0.001 and hi_ <= 1.001:
                return layer
        return None

    baked = failed = 0
    bake_uv = {}
    for ob in {t[0] for t in targets}:
        if not ob.data.uv_layers:
            print(f"    ! {ob.name}: no UV map — cannot bake"); failed += 1; continue
        layer = ob.data.uv_layers.active
        if layer is None or FIX_UDIM:
            layer = bake_uv_layer(ob)
        if layer is None:
            print(f"    ! {ob.name}: every UV layer spans outside 0..1 — cannot bake"); failed += 1; continue
        ob.data.uv_layers.active = layer
        bake_uv[ob.name] = layer.name
        bpy.ops.object.select_all(action="DESELECT")
        # hide_render blocks baking outright ("not enabled for rendering"), and these files
        # ship unworn pieces hidden — so clear BOTH flags, not just the viewport one.
        ob.hide_viewport = False; ob.hide_render = False
        ob.hide_set(False); ob.select_set(True)
        bpy.context.view_layer.objects.active = ob
        try:
            bpy.ops.object.bake(type="DIFFUSE")
            baked += 1
        except Exception as exc:                # noqa: BLE001 — one bad mesh must not lose the export
            print(f"    ! {ob.name}: bake failed ({exc})"); failed += 1

    # Remove the throwaway nodes; the bystander materials are otherwise untouched.
    for m, node, junk in scratch:
        m.node_tree.nodes.remove(node)
        bpy.data.images.remove(junk)

    # Rebuild each baked material as a minimal Principled BSDF the exporter can read directly —
    # UNLESS the bake came out black, which means it captured nothing useful.
    #
    # A transparent or refractive material (a watch crystal, a spectacle lens, the eye-moisture layer)
    # has no diffuse colour to capture, so DIFFUSE baking yields black — and shipping that turns clear
    # glass into an opaque black disc, which is worse than the grey it replaced. Blend mode cannot tell
    # these apart here: every material in this file reports HASHED, skin included. So rather than guess
    # at which materials are "glass", check the RESULT: a bake that produced black did not solve the
    # problem it was called for, and a neutral base is the safer fallback.
    dark = 0
    for name, (img, _node) in made.items():
        px = img.pixels[:]                            # RGBA floats; sample rather than sum 4M of them
        step = max(4, (len(px) // 4 // 4096)) * 4
        lit = [px[i] + px[i + 1] + px[i + 2] for i in range(0, len(px) - 3, step)]
        mean = (sum(lit) / len(lit) / 3.0) if lit else 0.0
        mat = bpy.data.materials[name]
        nt = mat.node_tree
        nt.nodes.clear()
        out = nt.nodes.new("ShaderNodeOutputMaterial")
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        if mean < 0.02:
            # A black diffuse bake means the material HAS no diffuse colour — which is what a lens, a
            # watch crystal or an eye-moisture layer is. So the fallback must be TRANSPARENT, not an
            # opaque neutral: shipping opaque grey turns clear spectacle lenses into frosted discs
            # (reported on device 2026-09-01). Faint grey rather than fully clear so the surface still
            # catches a highlight and the glasses read as glass instead of as empty frames.
            dark += 1
            if translucent.get(name):
                bsdf.inputs["Base Color"].default_value = (0.85, 0.85, 0.9, 1.0)
                bsdf.inputs["Alpha"].default_value = 0.14
                mat.blend_method = "BLEND"                   # ≤4.1
                if hasattr(mat, "surface_render_method"):
                    mat.surface_render_method = "BLENDED"    # 4.2+ — drives glTF alphaMode: BLEND
            else:
                bsdf.inputs["Base Color"].default_value = (0.62, 0.60, 0.58, 1.0)   # neutral, OPAQUE
        else:
            tex = nt.nodes.new("ShaderNodeTexImage"); tex.image = img
            # Point the rebuilt material at the SAME layer the bake wrote into. Without this the
            # exporter picks a UV set by default and can land back on the UDIM atlas the bake existed
            # to escape — the texture would be correct and sampled in the wrong place.
            layer = next((bake_uv[o.name] for o, _i, m2 in targets
                          if m2 and m2.name == name and o.name in bake_uv), None)
            if layer:
                uvn = nt.nodes.new("ShaderNodeUVMap"); uvn.uv_map = layer
                nt.links.new(uvn.outputs["UV"], tex.inputs["Vector"])
            nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    print(f"  bake          {len(made)} materials on {baked} objects at {size}px"
          + (f", {dark} came out black → neutral base" if dark else "")
          + (f", {failed} failed" if failed else ""))


select_for_export()

# ---- textures: the actual size problem ---------------------------------------------------------
# Measured on Grace: geometry exported to 19 MB and textures to 210 MB. These are 4K PBR maps authored
# for offline rendering; a Quest wants a fraction of that. Blender's glTF exporter can re-encode but
# will NOT resize, so scale the images here first. Done in-memory on the already-loaded datablocks —
# the source .blend on disk is never touched.
if MAX_TEX:
    scaled = before = after = 0
    for img in bpy.data.images:
        w, h = img.size
        if not w or not h:
            continue
        before += w * h
        if max(w, h) > MAX_TEX:
            f = MAX_TEX / max(w, h)
            img.scale(max(1, int(w * f)), max(1, int(h * f)))
            scaled += 1
        after += img.size[0] * img.size[1]
    print(f"  textures      scaled {scaled}/{len(bpy.data.images)} to ≤{MAX_TEX}px  "
          f"({before/1e6:.0f} → {after/1e6:.0f} Mpx, {after/before*100:.0f}%)")

def do_export(path):
    bpy.ops.export_scene.gltf(
        filepath=path,
        export_format="GLB",
        use_selection=True,
        export_yup=True,                # Blender Z-up → glTF Y-up
        export_apply=False,             # applying modifiers would collapse the armature binding
        export_skins=True,
        # TRIED AND REJECTED: export_def_bones=True, meant to fix zig-zag limbs by emitting only the
        # deformation bones "and needed bones for hierarchy". It makes things worse. Grace's limb
        # deformers are driven by CONSTRAINTS, not parenting, so Blender has no hierarchy to preserve
        # and flattens them all to the armature root (`upper_arm.bend.L <- Grace_RIG`). The spine, which
        # is genuinely parented, survives. Measured: 482 joints -> 175, limbs unusable.
        export_def_bones=False,
        export_morph=KEEP_MORPHS,       # the ~90M-delta decision
        export_animations=False,        # the samples ship none; sourced separately (backlog)
        export_cameras=False,
        export_lights=False,
        export_extras=False,
        export_image_format=IMG_FMT,
        export_jpeg_quality=int(opt("jpeg-quality", "85")),
    )


def unresolved_base_colours(path):
    """Material names the exporter could NOT resolve a base colour for — no baseColorTexture and a
    black/absent baseColorFactor. These are the ones that render grey-to-black in a client.

    **Ask the exporter rather than guessing from the node tree.** The first attempt baked every material
    not rooted in a Principled BSDF, which was wrong in both directions: Grace's hair is not
    Principled-rooted either, yet exports a perfectly good baseColorTexture, so baking *replaced a correct
    texture with a worse one* and turned her hair and hands dark. The exporter's own output is the only
    oracle that cannot disagree with the exporter.
    """
    import json as _json
    import struct as _struct
    with open(path, "rb") as fh:
        blob = fh.read()
    jl, _ = _struct.unpack_from("<II", blob, 12)
    doc = _json.loads(blob[20:20 + jl])
    bad = set()
    for m in doc.get("materials", []):
        pbr = m.get("pbrMetallicRoughness", {})
        if pbr.get("baseColorTexture"):
            continue
        f = pbr.get("baseColorFactor")
        # Not just exactly-black: Grace's fingernails exported [0.01, 0.04, 0.11] with no texture — a
        # near-black blue that the scene lights lift to a bluish grey, and visibly wrong on a hand. An
        # equality test missed it. Anything this dark with no texture is a base colour the exporter
        # failed to resolve, whatever the exact value.
        if f is None or (len(f) >= 3 and max(f[:3]) < 0.15):
            # NOTE: detecting these is right; the FALLBACK for them is not yet. Grace's fingernails land
            # here, bake black (like a lens), and so get the transparent fallback — but a nail is opaque.
            # "No diffuse colour" genuinely means transparent for a lens and merely means "shader too
            # complex" for a nail, and nothing measured here separates the two. Left detected-but-
            # imperfect rather than reverted, since a near-black factor IS a failure worth surfacing.
            bad.add(m.get("name"))
    return bad


def udim_materials(objs, texcoord_of):
    """Materials whose exported UVs fall outside 0..1 — i.e. they rely on UDIM tiling.

    **glTF has no UDIM concept at all.** Daz figures lay the body out as a UV atlas spanning many tiles
    in one set — Grace's is u 0..1 face, 1..2 torso, 2..3 legs, 3..4 arms, out to 6..7 — and a client
    given u=3.5 has no way to know which tile image that means. Her arms exported from that set and
    rendered black; her torso happened to export from a second, cleanly-unwrapped set and was fine.

    Baking fixes it because the bake resolves the material *in Blender*, where UDIM works, into a flat
    texture in the mesh's own 0..1 layout. `texcoord_of` maps material name -> the TEXCOORD index the
    probe export chose, so we test the set the exporter actually used rather than guessing.
    """
    bad = set()
    for ob in objs:
        uvs = ob.data.uv_layers
        if not uvs:
            continue
        slots = [s.material.name if s.material else None for s in ob.material_slots]
        span = {}
        for p in ob.data.polygons:
            name = slots[p.material_index] if p.material_index < len(slots) else None
            if name is None or name in bad:
                continue
            n = texcoord_of.get(name, 0)
            if n >= len(uvs):
                bad.add(name)                            # samples a set that does not exist
                continue
            layer = uvs[n].data
            for li in p.loop_indices:
                u, v = layer[li].uv
                lo_, hi_ = span.setdefault(name, [9e9, -9e9])
                span[name] = [min(lo_, u, v), max(hi_, u, v)]
        for name, (a, b) in span.items():
            if a < -0.001 or b > 1.001:
                bad.add(name)
    return bad



def normalize_udim_uvs(objs, texcoord_of, only):
    """Shift each UDIM material's UVs back into 0..1 by subtracting its integer tile offset.

    A UDIM tile number encodes an offset: 1001 is u 0..1, 1002 is u 1..2, and Grace's arms at 1004 sit
    at u 3..4. The image assigned to that material is already the tile's own image, so the only thing
    wrong for glTF is the offset. Subtract it and the existing texture samples correctly — no bake, no
    resampling, no quality loss.

    Per material rather than per layer: each material occupies its own tile, so each moves independently.
    They end up overlapping in UV space, which is fine — every one carries its own image.
    """
    moved = 0
    for ob in objs:
        uvs = ob.data.uv_layers
        if not uvs:
            continue
        slots = [sl.material.name if sl.material else None for sl in ob.material_slots]
        # Per (material index, uv layer): the integer tile the faces sit in.
        tiles = {}
        for p in ob.data.polygons:
            name = slots[p.material_index] if p.material_index < len(slots) else None
            if name is None or name not in only:
                continue
            n = texcoord_of.get(name, 0)
            if n >= len(uvs):
                continue
            layer = uvs[n].data
            for li in p.loop_indices:
                u, v = layer[li].uv
                key = (p.material_index, n)
                cur = tiles.get(key)
                tu, tv = math.floor(u + 1e-6), math.floor(v + 1e-6)
                if cur is None:
                    tiles[key] = [tu, tv, True]
                elif cur[0] != tu or cur[1] != tv:
                    cur[2] = False            # spans more than one tile — cannot be a simple offset
        usable = {k: v for k, v in tiles.items() if v[2] and (v[0] or v[1])}
        if not usable:
            continue
        for p in ob.data.polygons:
            key0 = p.material_index
            for n in {texcoord_of.get(slots[key0] if key0 < len(slots) else "", 0)}:
                off = usable.get((key0, n))
                if not off or n >= len(uvs):
                    continue
                layer = uvs[n].data
                for li in p.loop_indices:
                    uv = layer[li].uv
                    uv[0] -= off[0]
                    uv[1] -= off[1]
        moved += len({k[0] for k in usable})
    return moved


def texcoords_from(path):
    """material name -> the TEXCOORD index its baseColorTexture samples, per the probe export."""
    import json as _json
    import struct as _struct
    with open(path, "rb") as fh:
        blob = fh.read()
    jl, _ = _struct.unpack_from("<II", blob, 12)
    doc = _json.loads(blob[20:20 + jl])
    out = {}
    for m in doc.get("materials", []):
        t = (m.get("pbrMetallicRoughness") or {}).get("baseColorTexture")
        if m.get("name") and isinstance(t, dict):
            out[m["name"]] = t.get("texCoord", 0)
    return out


if REPARENT:
    reparent_deform_bones(armatures)
    select_for_export()

if STRIP_CONSTRAINTS:
    strip_constraints(armatures)
    select_for_export()

if BAKE:
    # Probe first, bake only what genuinely needs it, then export for real.
    probe = out_path + ".probe.glb"
    do_export(probe)
    failed = unresolved_base_colours(probe)
    tc = texcoords_from(probe)
    udim = udim_materials(meshes, tc)
    os.remove(probe)
    print(f"  probe         no base colour: {', '.join(sorted(failed)[:5]) or '—'}"
          f"  ({len(failed)})")
    print(f"                UV outside 0..1 (UDIM): {', '.join(sorted(udim)[:5]) or '—'}"
          f"  ({len(udim)})")
    # UDIM is fixed by MOVING THE UVs, not by baking. A tile is just an integer offset — a material at
    # u 3..4 is tile 1004, and its assigned image IS that tile — so subtracting the offset puts the
    # coordinates back in 0..1 where glTF can use them. Exact, instant, and lossless, where baking these
    # Daz node graphs produced black 15 times out of 17. Baking is kept only for materials whose base
    # colour the exporter genuinely cannot resolve at all.
    moved = normalize_udim_uvs(meshes, tc, udim) if FIX_UDIM else 0
    if moved:
        print(f"  udim          shifted {moved} material(s) back into 0..1 (no baking needed)")
    if failed:
        bake_materials(meshes, BAKE, only=failed)
    select_for_export()

do_export(out_path)
print(f"\n  WROTE {out_path}  ({os.path.getsize(out_path)/1e6:.1f} MB)\n")


# Blender's teardown of a multi-GB character scene can fault AFTER the work is done ("Attempt to free
# nullptr pointer", and a macOS crash dialog). The file on disk is already complete and valid at this
# point, so skip interpreter/Blender cleanup entirely rather than let a shutdown bug look like a
# conversion failure. Exit code stays 0 because the conversion genuinely succeeded.
# _exit skips stdio flushing, which silently ate the WROTE line the first time.
sys.stdout.flush()
sys.stderr.flush()
import os as _os
_os._exit(0)
