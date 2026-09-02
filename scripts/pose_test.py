"""Pose a figure through its humanoid bone map and render the result — the FUNCTIONAL test of a map.

    B=/Applications/Blender.app/Contents/MacOS/Blender
    $B --background --python scripts/pose_test.py -- model.glb out/ '{"leftUpperArm": [50,0,0]}'

`conjure.figures.validate()` checks a bone map for geometric self-consistency, and `score()` checks it
against VRM's stated answer. Both are STRUCTURAL: they can confirm a map is plausible and internally
sound while it still names the wrong joints. The only test that settles it is to drive the map and look
— rotate what a model calls `leftUpperArm` and see whether the LEFT arm lifts, at the shoulder.

That is also why this is not throwaway: posing through the map is what Phase 2/3 does, so this is the
first real slice of it, driven from a script instead of an MCP tool.

Rotations are per-bone Euler degrees in the bone's own space. The exact angle does not matter — what is
being tested is *which* limb moves and *where* it hinges, so a large obvious rotation is the point.
"""
import json
import math
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
glb, outdir = argv[0], argv[1]
poses = json.loads(argv[2]) if len(argv) > 2 else {}
os.makedirs(outdir, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)

arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
if not arms:
    print("  NO ARMATURE"); sys.exit(1)
rig = max(arms, key=lambda a: len(a.data.bones))
print(f"\n=== pose_test: {os.path.basename(glb)} ===")
print(f"  armature {rig.name} ({len(rig.data.bones)} bones)")

bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="POSE")
for bone, euler in poses.items():
    pb = rig.pose.bones.get(bone)
    if not pb:
        print(f"    ! {bone!r} not in this rig"); continue
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = [math.radians(a) for a in euler]
    print(f"    posed {bone:<28} by {euler}")
bpy.ops.object.mode_set(mode="OBJECT")
bpy.context.view_layer.update()

# Hide the importer's placeholder objects (see glb_preview.py) so they cannot skew framing.
objs = []
for o in bpy.context.scene.objects:
    if o.type != "MESH":
        continue
    if any(c.name == "glTF_not_exported" for c in o.users_collection):
        o.hide_render = True
    else:
        objs.append(o)

import mathutils
lo = [1e9] * 3; hi = [-1e9] * 3
for o in objs:
    for c in o.bound_box:
        w = o.matrix_world @ mathutils.Vector(c)
        for i in range(3):
            lo[i] = min(lo[i], w[i]); hi[i] = max(hi[i], w[i])
size = [hi[i] - lo[i] for i in range(3)]
mid = [(hi[i] + lo[i]) / 2 for i in range(3)]

scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = scene.render.resolution_y = int(argv[3]) if len(argv) > 3 else 640
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "TEXTURE"
cam_d = bpy.data.cameras.new("c"); cam = bpy.data.objects.new("c", cam_d)
scene.collection.objects.link(cam); scene.camera = cam
r = max(size) * 1.5 + 0.5
cam.location = (mid[0], mid[1] - r, mid[2])
d = mathutils.Vector(mid) - cam.location
cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
scene.render.filepath = os.path.join(outdir, "posed.png")
bpy.ops.render.render(write_still=True)
print(f"  wrote {scene.render.filepath}\n")

sys.stdout.flush()
import os as _os
_os._exit(0)
