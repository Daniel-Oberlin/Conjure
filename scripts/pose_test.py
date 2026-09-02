"""Pose a figure through its humanoid bone map and render the result — the FUNCTIONAL test of a map.

    B=/Applications/Blender.app/Contents/MacOS/Blender
    $B --background --python scripts/pose_test.py -- model.glb out/ '{"leftUpperArm": {"bend": 60}}'

`conjure.figures.validate()` checks a bone map for geometric self-consistency, and `score()` checks it
against VRM's stated answer. Both are STRUCTURAL: they can confirm a map is plausible and internally
sound while it still names the wrong joints, or names the right ones and rotates them the wrong way.
The only test that settles it is to drive the map and look — ask for a bent left elbow and see whether
the LEFT forearm comes FORWARD.

That is also why this is not throwaway: posing through the map is what /figure does, so this is the
same slice driven from a script instead of an MCP tool. It calls the real
`conjure.figures.anatomical_axes` and `resolve_pose` — a render that disagrees with the headset would
mean the two sides had drifted, which is the whole point of sharing the code.

Two pose vocabularies are accepted:

  {"leftUpperArm": {"bend": 60}}       ANATOMICAL — resolved through the humanoid map and its measured
                                       axes, exactly as the runtime does it. This is what to use.
  {"upper_arm.fk.L": [0, 0, -60]}      RAW — euler degrees on a rig bone by its own name, in Blender's
                                       bone space. The escape hatch for asking "what does this specific
                                       bone do", which is how several wrong maps were caught.
"""
import json
import math
import os
import sys

import bpy
import mathutils

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from conjure.figures import (anatomical_axes, infer_humanoid,   # noqa: E402 — after the path fix
                             resolve_pose, split_glb, validate)
from conjure.importer import vrm_humanoid                       # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
glb, outdir = argv[0], argv[1]
poses = json.loads(argv[2]) if len(argv) > 2 else {}
os.makedirs(outdir, exist_ok=True)

# The map and the axes come from the GLB by the same route the importer takes, so what is rendered is
# what a headset would be sent — not a second implementation that could agree by luck.
raw = open(glb, "rb").read()
doc, blob = split_glb(raw)
mapping = (vrm_humanoid(doc) or infer_humanoid(doc, blob) or {}) if doc else {}
anatomical = {b: r for b, r in poses.items() if isinstance(r, dict)}
euler_poses = {b: r for b, r in poses.items() if not isinstance(r, dict)}

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)

arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
if not arms:
    print("  NO ARMATURE"); sys.exit(1)
rig = max(arms, key=lambda a: len(a.data.bones))
print(f"\n=== pose_test: {os.path.basename(glb)} ===")
print(f"  armature {rig.name} ({len(rig.data.bones)} bones)")
if mapping:
    problems = validate(doc, mapping)
    print(f"  humanoid map: {len(mapping)} bones, "
          + ("clean" if not problems else f"{len(problems)} problem(s): {problems[0]}"))

bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="POSE")


def find(node_name):
    """A pose bone by its glTF node name, through the sanitizations Blender's importer applies."""
    for candidate in (node_name, node_name.replace(".", "_"), node_name.replace(" ", "_")):
        pb = rig.pose.bones.get(candidate)
        if pb:
            return pb
    return None


# Anatomical poses. `anatomical_axes(space="world")` gives an axis in the GLB's own model space; Blender
# imports glTF Y-up as Z-up, so (x, y, z) -> (x, -z, y). A pose bone's rotation is expressed in its REST
# frame, so the world rotation R becomes R_rest^-1 * R * R_rest — which is also why it composes correctly
# when an ancestor is posed too: matrix_basis is relative to the parent chain, exactly like glTF's.
if anatomical:
    axes = anatomical_axes(doc, mapping, space="world") if mapping else {}
    for bone, quat in resolve_pose(axes, anatomical).items():
        pb = find(mapping.get(bone, ""))
        if not pb:
            print(f"    ! {bone!r} -> {mapping.get(bone)!r} not in this rig"); continue
        q_world = mathutils.Quaternion((quat[3], quat[0], -quat[2], quat[1]))
        rest = pb.bone.matrix_local.to_quaternion()
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = rest.inverted() @ q_world @ rest
        print(f"    posed {bone:<16} ({pb.name:<24}) by {anatomical[bone]}")

for bone, euler in euler_poses.items():
    pb = rig.pose.bones.get(bone)
    if not pb:
        print(f"    ! {bone!r} not in this rig"); continue
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = [math.radians(a) for a in euler]
    print(f"    posed {bone:<28} by {euler} (raw)")
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
# Front AND side, always. A front view cannot tell a raised knee from a leg swung backwards, which is
# precisely the error this script exists to catch — the side view is the one that settles `bend`.
for label, offset in (("posed", (0.0, -r, 0.0)), ("posed_side", (r, 0.0, 0.0))):
    cam.location = (mid[0] + offset[0], mid[1] + offset[1], mid[2] + offset[2])
    d = mathutils.Vector(mid) - cam.location
    cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    scene.render.filepath = os.path.join(outdir, label + ".png")
    bpy.ops.render.render(write_still=True)
    print(f"  wrote {scene.render.filepath}")
print()

sys.stdout.flush()
import os as _os
_os._exit(0)
