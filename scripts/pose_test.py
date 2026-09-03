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
import struct
import sys

import bpy
import mathutils

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from conjure.figures import (anatomical_axes, best_humanoid,   # noqa: E402 — after the path fix
                             resolve_pose, split_glb, validate)
from conjure.importer import vrm_humanoid                       # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
glb, outdir = argv[0], argv[1]
poses = json.loads(argv[2]) if len(argv) > 2 else {}
os.makedirs(outdir, exist_ok=True)

raw = open(glb, "rb").read()
doc, blob = split_glb(raw)
# The same discovery order the importer uses — stated map, then a known naming convention, then shape —
# so what is rendered is what a headset would be sent.
mapping = ((vrm_humanoid(doc) or best_humanoid(doc, blob)[0]) or {}) if doc else {}
anatomical = {b: r for b, r in poses.items() if isinstance(r, dict)}
euler_poses = {b: r for b, r in poses.items() if not isinstance(r, dict)}

print(f"\n=== pose_test: {os.path.basename(glb)} ===")
if mapping:
    problems = validate(doc, mapping)
    print(f"  humanoid map: {len(mapping)} bones, "
          + ("clean" if not problems else f"{len(problems)} problem(s): {problems[0]}"))
else:
    print("  humanoid map: NONE — nothing to pose semantically (raw bone names still work)")


def posed_glb(data, doc, blob, mapping, requests):
    """The GLB with the pose written into its own node rotations — the artifact, not a reading of it.

    **Posing here rather than in Blender is the point.** A pose is a delta on a glTF node's local
    rotation, and that is precisely what the runtime applies; Blender's pose bones live in a different
    frame (Y-along-bone), reached through an armature object that the importer sometimes carries the
    up-axis conversion on and sometimes bakes into the bones. Converting between the two took three
    chained frame changes and was wrong on half the library — silently, since a wrong-space rotation
    still produces a plausible-looking figure. Writing the rotations into the file removes the question:
    Blender then renders a posed GLB, byte-for-byte the thing a headset would load.
    """
    by_name = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
    axes = anatomical_axes(doc, mapping)                  # PARENT space: where a node's rotation lives
    notes, posed_nodes = [], {}
    for bone, delta in resolve_pose(axes, requests, notes).items():
        i = by_name.get(mapping.get(bone, ""))
        if i is None:
            print(f"    ! {bone!r} -> {mapping.get(bone)!r} not in this file"); continue
        node = doc["nodes"][i]
        rest = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
        x, y, z, w = delta
        rx, ry, rz, rw = rest
        node["rotation"] = [w * rx + x * rw + y * rz - z * ry,
                            w * ry - x * rz + y * rw + z * rx,
                            w * rz + x * ry - y * rx + z * rw,
                            w * rw - x * rx - y * ry - z * rz]
        posed_nodes[bone] = i
        print(f"    posed {bone:<16} ({node['name']:<24}) by {requests[bone]}")
    for note in notes:
        print(f"    joint limit: {note}")

    # ...and the SAME rotations again as a one-keyframe animation, because Blender's glTF importer reads
    # a joint node's TRS as the bone's REST and reconciles the difference in the pose — so a file posed
    # the way the runtime poses it imports and renders as if nothing had happened (verified: two renders
    # byte-identical). An animation channel is the one thing it applies over everything else, which is
    # how the models' own idle clips were overriding this in the first place. Existing clips are dropped:
    # this is a still.
    buffers, views, accs = doc.setdefault("buffers", [{}]), doc.setdefault("bufferViews", []), \
        doc.setdefault("accessors", [])
    extra, samplers, channels = bytearray(), [], []
    base = len(blob) + (-len(blob) % 4)

    def add_view(payload):
        offset = base + len(extra)
        extra.extend(payload)
        extra.extend(b"\x00" * (-len(extra) % 4))
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(payload)})
        return len(views) - 1

    time_acc = len(accs)
    accs.append({"bufferView": add_view(struct.pack("<f", 0.0)), "componentType": 5126,
                 "count": 1, "type": "SCALAR", "min": [0.0], "max": [0.0]})
    for bone, node_index in posed_nodes.items():
        q = doc["nodes"][node_index]["rotation"]
        out = len(accs)
        accs.append({"bufferView": add_view(struct.pack("<4f", *q)), "componentType": 5126,
                     "count": 1, "type": "VEC4"})
        samplers.append({"input": time_acc, "output": out, "interpolation": "STEP"})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": node_index, "path": "rotation"}})
    doc["animations"] = [{"name": "pose", "samplers": samplers, "channels": channels}]
    blob = bytes(blob) + b"\x00" * (-len(blob) % 4) + bytes(extra)
    buffers[0]["byteLength"] = len(blob)

    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    chunks = struct.pack("<II", len(body), 0x4E4F534A) + body
    if blob:
        padded = blob + b"\x00" * (-len(blob) % 4)
        chunks += struct.pack("<II", len(padded), 0x004E4942) + padded
    return b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks


source = glb
if anatomical and mapping:
    source = os.path.join(outdir, "posed.glb")
    open(source, "wb").write(posed_glb(raw, doc, blob, mapping, anatomical))

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=source)

arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
if not arms:
    print("  NO ARMATURE"); sys.exit(1)
rig = max(arms, key=lambda a: len(a.data.bones))
print(f"  armature {rig.name} ({len(rig.data.bones)} bones)")

# A model that ships clips (the asset-pack characters carry ten to twenty-four each) imports with one
# ASSIGNED, and it drives the bones over anything else in the file. When we wrote a pose, ours replaced
# them and is the only clip left; when we did not, drop them all so the render shows the bind pose.
if not anatomical and bpy.data.actions:
    print(f"  clearing {len(bpy.data.actions)} imported clip(s) so the BIND pose is what renders")
    for obj in bpy.context.scene.objects:
        if obj.animation_data:
            obj.animation_data_clear()

bpy.ops.object.select_all(action="DESELECT")
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.mode_set(mode="POSE")


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

# Frame on the POSED SKELETON, not the mesh bounding boxes. A mesh's `bound_box` is its rest shape, so
# a raised arm falls outside it and the camera crops exactly the thing being checked — `Steve` came out
# filling the frame with his head. Bones are evaluated, so they already carry the pose; a margin covers
# the flesh around them.
lo = [1e9] * 3; hi = [-1e9] * 3
points = [rig.matrix_world @ p for pb in rig.pose.bones for p in (pb.head, pb.tail)]
points += [o.matrix_world @ mathutils.Vector(c) for o in objs for c in o.bound_box]
for w in points:
    for i in range(3):
        lo[i] = min(lo[i], w[i]); hi[i] = max(hi[i], w[i])
pad = 0.12 * max(hi[i] - lo[i] for i in range(3))
lo = [v - pad for v in lo]; hi = [v + pad for v in hi]
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
