"""Headless-Blender structural dump of a .blend → JSON. Layer 0 of the figures import pipeline
(docs/backlogs/figures.md), and the tool that produced the measurements recorded there.

    /Applications/Blender.app/Contents/MacOS/Blender --background <file.blend> \
        --python scripts/inspect_blend.py -- out.json
    python scripts/blend_summary.py out.json      # human-readable digest

~1.5 s for an 87 MB file, so it is cheap enough to run on every import. Blender is NOT on PATH on
macOS — use the full app path above.

Reads `bpy.data`, **not** the scene, on purpose: these are "append model" files whose objects
frequently live in the file's data without being linked into any scene or view layer, so walking
`bpy.context.scene.objects` silently misses most of the content.

What it answers, and why each field is here (all four sample models informed this list):
  - collections + hide flags  → the OUTFIT vocabulary, already authored by the porter
  - modifiers[type=ARMATURE]  → which meshes are actually skinned (vs. rig-widget clutter)
  - bone head/tail + parent   → the bind-pose skeleton, for topological humanoid inference
  - pose constraints          → the control-rig apparatus that will NOT survive glTF export
  - shape_keys                → Daz JCM burden; on one sample this is ~1.4 GB of morph data
  - actions                   → animations shipped with the model (so far: always none)
"""
import json
import sys

import bpy

out_path = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "/dev/stdout"


def bone_tree(arm):
    """Bone name -> parent name, plus head/tail in armature space (bind pose)."""
    return [{
        "name": b.name,
        "parent": b.parent.name if b.parent else None,
        "head": [round(v, 4) for v in b.head_local],
        "tail": [round(v, 4) for v in b.tail_local],
        "len": round(b.length, 4),
    } for b in arm.bones]


meshes, armatures, others = [], [], []
for ob in bpy.data.objects:
    common = {
        "name": ob.name,
        "parent": ob.parent.name if ob.parent else None,
        "parent_type": ob.parent_type if ob.parent else None,
        "hide_viewport": bool(ob.hide_viewport),
        "hide_render": bool(ob.hide_render),
        "in_scene": bool(ob.users_scene),
        "collections": [c.name for c in ob.users_collection],
        "loc": [round(v, 4) for v in ob.location],
        "scale": [round(v, 4) for v in ob.scale],
        "dims": [round(v, 4) for v in ob.dimensions],
    }
    if ob.type == "MESH":
        me = ob.data
        sk = me.shape_keys
        meshes.append({**common,
            "verts": len(me.vertices), "polys": len(me.polygons),
            "materials": [m.name if m else None for m in me.materials],
            "uv_layers": [u.name for u in me.uv_layers],
            "vertex_groups": len(ob.vertex_groups),
            "shape_keys": [k.name for k in sk.key_blocks] if sk else [],
            "modifiers": [{"type": m.type, "name": m.name,
                           "object": getattr(getattr(m, "object", None), "name", None)}
                          for m in ob.modifiers],
        })
    elif ob.type == "ARMATURE":
        armatures.append({**common,
            "bone_count": len(ob.data.bones),
            "bones": bone_tree(ob.data),
            "pose_constraints": sorted({c.type for pb in ob.pose.bones for c in pb.constraints}),
            "constrained_bones": sum(1 for pb in ob.pose.bones if pb.constraints),
        })
    else:
        others.append({**common, "type": ob.type})

report = {
    "blend": bpy.data.filepath,
    "blender": bpy.app.version_string,
    "unit_system": bpy.context.scene.unit_settings.system,
    "unit_scale": round(bpy.context.scene.unit_settings.scale_length, 6),
    "counts": {"objects": len(bpy.data.objects), "meshes": len(meshes),
               "armatures": len(armatures), "other": len(others),
               "materials": len(bpy.data.materials), "images": len(bpy.data.images),
               "actions": len(bpy.data.actions), "collections": len(bpy.data.collections)},
    "actions": [{"name": a.name, "range": [round(v, 2) for v in a.frame_range],
                 "channels": len(a.fcurves)} for a in bpy.data.actions],
    "collections": [{"name": c.name, "objects": len(c.objects),
                     "children": [x.name for x in c.children]} for c in bpy.data.collections],
    "armatures": armatures,
    "meshes": meshes,
    "other_objects": others,
}
with open(out_path, "w") as f:
    json.dump(report, f, indent=1)
print(f"[inspect] wrote {out_path}")


# Blender's teardown of a multi-GB character scene can fault AFTER the work is done ("Attempt to free
# nullptr pointer", and a macOS crash dialog). The file on disk is already complete and valid at this
# point, so skip interpreter/Blender cleanup entirely rather than let a shutdown bug look like a
# conversion failure. Exit code stays 0 because the conversion genuinely succeeded.
import os as _os
_os._exit(0)
