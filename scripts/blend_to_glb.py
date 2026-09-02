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
        # Naming a collection is an explicit choice and OVERRIDES its hidden flag — an outfit you
        # asked for by name is one you want, and Grace's alternates are all collection-hidden. With
        # no --collections, fall back to "whatever the porter left visible".
        picked = any(c.name in WANT for c in ob.users_collection) if WANT else visible(ob)
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
def bake_materials(objs, size, only):
    """Bake the named materials on `objs` to a diffuse image and rebuild each as Principled.
    `only` comes from unresolved_base_colours() — never from guessing at the node tree."""
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

    made = {}
    for ob, _slot, mat in targets:
        if mat.name in made:
            continue
        img = bpy.data.images.new(f"bake_{mat.name}", size, size, alpha=True)
        node = mat.node_tree.nodes.new("ShaderNodeTexImage")
        node.image = img
        mat.node_tree.nodes.active = node       # `active` node per material is the bake destination
        made[mat.name] = (img, node)

    baked = failed = 0
    for ob in {t[0] for t in targets}:
        if not ob.data.uv_layers:
            print(f"    ! {ob.name}: no UV map — cannot bake"); failed += 1; continue
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
            bsdf.inputs["Base Color"].default_value = (0.85, 0.85, 0.9, 1.0)
            bsdf.inputs["Alpha"].default_value = 0.14
            mat.blend_method = "BLEND"                       # ≤4.1
            if hasattr(mat, "surface_render_method"):
                mat.surface_render_method = "BLENDED"        # 4.2+ — drives glTF alphaMode: BLEND
        else:
            tex = nt.nodes.new("ShaderNodeTexImage"); tex.image = img
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
        if f is None or (len(f) >= 3 and f[0] == f[1] == f[2] == 0):
            bad.add(m.get("name"))
    return bad


if BAKE:
    # Probe first, bake only what genuinely failed, then export for real.
    probe = out_path + ".probe.glb"
    do_export(probe)
    failed = unresolved_base_colours(probe)
    os.remove(probe)
    print(f"  probe         {len(failed)} material(s) with no resolvable base colour: "
          f"{', '.join(sorted(failed)[:6]) or '—'}")
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
