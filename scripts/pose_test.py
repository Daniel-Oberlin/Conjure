"""Pose a figure through its humanoid bone map and render the result — the FUNCTIONAL test of a map.

    B=/Applications/Blender.app/Contents/MacOS/Blender
    $B --background --python scripts/pose_test.py -- model.glb out/ '{"leftUpperArm": {"bend": 60}}'
    $B --background --python scripts/pose_test.py -- model.glb rest/ '{}' 640 --clay --frame out/frame.json

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
from conjure.figures import (_mul, apply_pose, best_humanoid,   # noqa: E402 — after the path fix
                             follow_bones, node_world_matrices, split_glb, validate)
from conjure.importer import vrm_humanoid                       # noqa: E402

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
# Two flags, both for the eval harness (docs/backlogs/figures.md, slice 2), both harmless by hand:
#   --clay          untextured render. Materials are irrelevant to "which way did the arm go", several
#                   of the library's are still wrong, and a hosted judge may decline to look at an
#                   undressed figure at all. Clay removes all three questions at once.
#   --frame <path>  reuse a camera box written by an earlier run instead of computing one. A rest and a
#                   posed render are only comparable if the camera did not move between them, and this
#                   script frames on the POSED skeleton — so the harness renders the pose first and
#                   hands the frame back for the reference shot.
clay = "--clay" in argv
frame_in = argv[argv.index("--frame") + 1] if "--frame" in argv else None
skip = {i for i, a in enumerate(argv) if a in ("--clay", "--frame")}
skip |= {i + 1 for i, a in enumerate(argv) if a == "--frame"}
argv = [a for i, a in enumerate(argv) if i not in skip]
glb, outdir = argv[0], argv[1]
poses = json.loads(argv[2]) if len(argv) > 2 else {}
os.makedirs(outdir, exist_ok=True)

raw = open(glb, "rb").read()
doc, blob = split_glb(raw)
# The same discovery order the importer uses — stated map, then a known naming convention, then shape —
# so what is rendered is what a headset would be sent.
mapping, _source, follows = best_humanoid(doc, blob) if doc else ({}, None, {})
stated = vrm_humanoid(doc) if doc else None
if stated:
    mapping, follows = stated, follow_bones(doc, stated, blob)
mapping = mapping or {}
anatomical = {b: r for b, r in poses.items() if isinstance(r, dict)}
euler_poses = {b: r for b, r in poses.items() if not isinstance(r, dict)}

print(f"\n=== pose_test: {os.path.basename(glb)} ===")
if mapping:
    problems = validate(doc, mapping)
    print(f"  humanoid map: {len(mapping)} bones, "
          + ("clean" if not problems else f"{len(problems)} problem(s): {problems[0]}"))
else:
    print("  humanoid map: NONE — nothing to pose semantically (raw bone names still work)")


def _invert_rigid(m):
    """Inverse of an affine column-major 4x4 — general, because bones DO carry scale.

    The first version assumed rotation and translation only, which is true of every rig converted here
    and false of the asset-pack ones: their armatures sit at scale 100, so transposing the rotation part
    was wrong by a factor of ten thousand and the render came out blank.
    """
    a = [[m[0], m[4], m[8]], [m[1], m[5], m[9]], [m[2], m[6], m[10]]]
    det = (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
           - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
           + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))
    if abs(det) < 1e-20:
        return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    inv = [[(a[(r + 1) % 3][(c + 1) % 3] * a[(r + 2) % 3][(c + 2) % 3]
             - a[(r + 1) % 3][(c + 2) % 3] * a[(r + 2) % 3][(c + 1) % 3]) / det
            for r in range(3)] for c in range(3)]                      # adjugate, transposed
    t = [-(inv[r][0] * m[12] + inv[r][1] * m[13] + inv[r][2] * m[14]) for r in range(3)]
    return [inv[0][0], inv[1][0], inv[2][0], 0.0,
            inv[0][1], inv[1][1], inv[2][1], 0.0,
            inv[0][2], inv[1][2], inv[2][2], 0.0,
            t[0], t[1], t[2], 1.0]


def posed_glb(data, doc, blob, mapping, requests, follows):
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
    world = node_world_matrices(doc)                      # BIND pose, before anything is rotated
    notes: list = []
    posed_nodes = apply_pose(doc, mapping, requests, notes)   # the runtime's own arithmetic, shared
    for bone, i in posed_nodes.items():
        print(f"    posed {bone:<16} ({doc['nodes'][i]['name']:<24}) by {requests[bone]}")
    for bone in requests:
        if bone not in posed_nodes:
            print(f"    ! {bone!r} -> {mapping.get(bone)!r} not in this file")
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
    # Bones that ride a limb rather than belonging to it (an IK foot parented to the armature root):
    # re-parent them in the copy we render, which is the same relationship the runtime maintains as a
    # constraint. Local TRS is recomputed so the bind pose is unchanged.
    for child_name, lead_name in (follows or {}).items():
        ci, li = by_name.get(child_name), by_name.get(lead_name)
        if ci is None or li is None:
            continue
        old_parent = next((k for k, n in enumerate(doc["nodes"]) if ci in (n.get("children") or [])), None)
        if old_parent is not None:
            doc["nodes"][old_parent]["children"] = [c for c in doc["nodes"][old_parent]["children"]
                                                    if c != ci]
        doc["nodes"][li].setdefault("children", []).append(ci)
        local = _mul(_invert_rigid(world[li]), world[ci])
        doc["nodes"][ci].pop("matrix", None)
        doc["nodes"][ci].pop("translation", None)
        doc["nodes"][ci].pop("rotation", None)
        doc["nodes"][ci].pop("scale", None)
        doc["nodes"][ci]["matrix"] = [round(v, 8) for v in local]      # TRS cannot express shear; this can
        print(f"    {child_name} now rides {lead_name}")
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
    open(source, "wb").write(posed_glb(raw, doc, blob, mapping, anatomical, follows))

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=source)

arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
if not arms:
    print("  NO ARMATURE"); sys.exit(1)
# The BODY armature, which is not the biggest one. Trish carries a 679-bone `Hair` rig beside her
# 362-bone skeleton, and hair spring bones sprawl metres past the figure — picked by bone count, it won
# and framed the camera on a box twice the height of the body, leaving her a third of the frame. Choose
# by the map instead: the armature holding the most bones we can actually name.
rig = max(arms, key=lambda a: (sum(1 for n in mapping.values() if n in a.data.bones),
                               len(a.data.bones)))
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
    elif any(m.type == "ARMATURE" and m.object is not rig for m in o.modifiers):
        pass                    # skinned to a different armature (hair, clothing rigs) — not the body
    else:
        objs.append(o)

# Frame on the POSED SKELETON, not the mesh bounding boxes. A mesh's `bound_box` is its rest shape, so
# a raised arm falls outside it and the camera crops exactly the thing being checked — `Steve` came out
# filling the frame with his head. Bones are evaluated, so they already carry the pose; a margin covers
# the flesh around them.
if frame_in:
    saved = json.load(open(frame_in))
    lo, hi = saved["lo"], saved["hi"]
else:
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
# Written every run, so any render can become the reference frame for a later one without re-deriving.
json.dump({"lo": list(lo), "hi": list(hi)}, open(os.path.join(outdir, "frame.json"), "w"))

scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = scene.render.resolution_y = int(argv[3]) if len(argv) > 3 else 640
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "SINGLE" if clay else "TEXTURE"
if clay:
    # A single mid-grey with the studio light on it: shape reads, material does not. Slightly warm so
    # a limb in front of the torso still separates by shading rather than by colour.
    scene.display.shading.single_color = (0.62, 0.58, 0.55)
cam_d = bpy.data.cameras.new("c"); cam = bpy.data.objects.new("c", cam_d)
scene.collection.objects.link(cam); scene.camera = cam
r = max(size) * 1.5 + 0.5
# Front, three-quarter AND side, always. A front view cannot tell a raised knee from a leg swung
# backwards, which is precisely the error this script exists to catch — the side view is the one that
# settles `bend`. The three-quarter exists because both of the others are DEGENERATE for some pose: an
# arm aimed straight forward points at the front camera and foreshortens into what looks like a folded
# elbow, and a vision judge called a correct pose anatomically impossible on exactly that render
# (2026-09-05). At 35 degrees no limb aligns with the view axis, and left/right still read cleanly.
# The extra shot is nearly free: the 3 s this script costs is Blender starting and importing, and each
# additional render is ~30 ms.
# All three cameras sit on the model's LEFT half (+X after the glTF import, which is the figure's own
# left) or dead in front, so a caption can name the side without lying — "her front-left" and "her left
# side" are the same side, and a judge asked which way the RIGHT arm went is not being told the mirror.
q = math.radians(35.0)
for label, offset in (("posed", (0.0, -r, 0.0)),
                      ("posed_q", (r * math.sin(q), -r * math.cos(q), 0.0)),
                      ("posed_side", (r, 0.0, 0.0))):
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
