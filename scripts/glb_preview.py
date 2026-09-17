"""Render a GLB to PNGs from several angles, in headless Blender.

    B=/Applications/Blender.app/Contents/MacOS/Blender
    $B --background --python scripts/glb_preview.py -- model.glb outdir/ [--size 512] [--views 4]
                                                        [--shown] [--head]
                                                        [--morph "Fcl_ALL_Fun=1,Fcl_EYE_Close=0.4"]

Two jobs, both from docs/backlogs/figures.md:

  1. **Round-trip verification.** Re-importing our own export into a clean scene and rendering it is the
     cheapest way to catch a broken conversion — a collapsed rig, a lost material, a figure lying on its
     side — before it costs a headset trip. It reads the GLB the same way a client will.
  2. **The renderer for the multimodal pass** (layer 4): upright? facing which way? which mesh is the
     jacket? Also what *Describing and embedding models* in backlogs/library.md needs — that one wants
     `--shown`, which draws only what the scene draws.

Prints the imported bounding box and height so the "is it life size" question is answered numerically,
not just by eye. Uses Workbench rather than Cycles: this is a structural check, not a beauty shot.
"""
import json
import math
import os
import struct
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
pos = [a for a in argv if not a.startswith("--")]
glb, outdir = pos[0], (pos[1] if len(pos) > 1 else ".")


def opt(name, d):
    return argv[argv.index(f"--{name}") + 1] if f"--{name}" in argv else d


SIZE, VIEWS = int(opt("size", 512)), int(opt("views", 4))
# `--morph "Fcl_ALL_Fun=1,Fcl_EYE_Close=0.4"` — drive shape keys before rendering, so an EXPRESSION can
# be looked at rather than only measured. `conjure.expressions` verifies its tables geometrically (which
# band a target acts in, and which way it moves the mesh); this answers the other question, the one no
# arithmetic can: does that actually read as a smile.
MORPH = {}
for pair in (opt("morph", "") or "").split(","):
    if "=" in pair:
        k, _, v = pair.partition("=")
        try:
            MORPH[k.strip()] = float(v)
        except ValueError:
            pass
# Frame the HEAD rather than the whole figure. A 1.7 m body in a 512-px square puts the face in about
# forty pixels, which is too few to tell a smile from a grimace — and the face is the only thing worth
# looking at when what changed is a morph weight.
HEAD = "--head" in argv
os.makedirs(outdir, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)

# Blender's glTF importer parks placeholder objects (icosphere stand-ins for things it could not
# represent) in a collection literally named `glTF_not_exported`. They are NOT in the file and no web
# client ever sees them — but they are 2 m across, so counting them puts the bounding box and the
# camera framing a metre out. Importer artifact, not model content.
# WHAT THE SCENE ACTUALLY DRAWS, when asked for it. A composed thing carries every variant its source
# had — Alice ships three hair meshes and five skirt/underwear variants — and drawing them all stacks
# wardrobes on top of each other. The composer already knows which lose: `extras.conjure.hidden` is the
# list, written for importers and ignored by viewers. Read straight out of the GLB's own JSON chunk so
# this script keeps depending on nothing but Blender.
#
# Off by default, because the round-trip check wants to SEE everything: a mesh that came through
# mangled is still a defect when it happens to be a hidden one. `--shown` is for the multimodal pass,
# where a second hair mesh intersecting the first reads as "a striking white streak" and is described
# in earnest.
def hidden_in(path):
    try:
        with open(path, "rb") as fh:
            fh.read(12)
            length, kind = struct.unpack("<II", fh.read(8))
            if kind != 0x4E4F534A:
                return set()
            doc = json.loads(fh.read(length))
    except (OSError, ValueError, struct.error):
        return set()
    return set(((doc.get("extras") or {}).get("conjure") or {}).get("hidden") or ())


HIDE = hidden_in(glb) if "--shown" in argv else set()
objs = [o for o in bpy.context.scene.objects if o.type == "MESH"
        and not any(c.name == "glTF_not_exported" for c in o.users_collection)
        and o.name not in HIDE]
for o in bpy.context.scene.objects:
    if o.name in HIDE:
        o.hide_render = True
if not objs:
    print("  NO MESHES IMPORTED — the export is broken"); sys.exit(1)
for o in bpy.context.scene.objects:      # keep them out of the render too
    if any(c.name == "glTF_not_exported" for c in o.users_collection):
        o.hide_render = True

# World-space bounds over every imported mesh.
lo = [1e9] * 3; hi = [-1e9] * 3
for o in objs:
    for c in o.bound_box:
        w = o.matrix_world @ __import__("mathutils").Vector(c)
        for i in range(3):
            lo[i] = min(lo[i], w[i]); hi[i] = max(hi[i], w[i])
size = [hi[i] - lo[i] for i in range(3)]
mid = [(hi[i] + lo[i]) / 2 for i in range(3)]
arms = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]

print(f"\n=== glb_preview: {os.path.basename(glb)} ===")
print(f"  meshes {len(objs)}  armatures {len(arms)} "
      f"({', '.join(f'{a.name}:{len(a.data.bones)}b' for a in arms)})")
print(f"  bounds  x {lo[0]:+.3f}..{hi[0]:+.3f}   y {lo[1]:+.3f}..{hi[1]:+.3f}   "
      f"z {lo[2]:+.3f}..{hi[2]:+.3f}")
print(f"  size    {size[0]:.3f} x {size[1]:.3f} x {size[2]:.3f} m")
# glTF is Y-up; Blender's importer converts back to Z-up, so height is Z here.
print(f"  HEIGHT  {size[2]:.3f} m   feet at z={lo[2]:+.4f}  (0 means it sits on the floor)")

if MORPH:
    # Applied by NAME across every mesh that has the key, for the same reason the client writes every
    # morphed mesh: a face is several meshes and one shape can live on more than one of them.
    hit, missing = {}, set(MORPH)
    for ob in bpy.data.objects:
        keys = getattr(getattr(ob.data, "shape_keys", None), "key_blocks", None)
        if not keys:
            continue
        for name, weight in MORPH.items():
            if name in keys:
                keys[name].value = weight
                hit[name] = hit.get(name, 0) + 1
                missing.discard(name)
    for name, n in sorted(hit.items()):
        print(f"  morph   {name} = {MORPH[name]:g}  on {n} mesh(es)")
    for name in sorted(missing):
        print(f"  morph   {name}  NOT FOUND — this model has no such shape key")

scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.render.resolution_x = scene.render.resolution_y = SIZE
scene.render.film_transparent = False
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "TEXTURE"

cam_data = bpy.data.cameras.new("cam")
cam = bpy.data.objects.new("cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

radius = max(size) * 1.5 + 0.5
target = list(mid)
if HEAD:
    # The top of the bounding box is the crown; a head is roughly an eighth of a standing figure, so
    # aiming a little below the crown centres the face rather than the hair.
    # A head is roughly an eighth of a standing figure. Aiming 0.8 of a head below the crown centres
    # the FACE rather than the hair — at 0.55 the chin fell off the bottom of the frame, which is the
    # half of an expression that carries the mouth.
    head = size[2] / 8.0
    target[2] = hi[2] - head * 0.8
    radius = head * 2.8
for i in range(VIEWS):
    a = 2 * math.pi * i / VIEWS
    cam.location = (target[0] + radius * math.sin(a), target[1] - radius * math.cos(a), target[2])
    # Aim at the framing target: point -Z at it, keeping the camera upright.
    d = __import__("mathutils").Vector(target) - cam.location
    cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    label = {0: "front", 1: "left", 2: "back", 3: "right"}.get(i, f"v{i}") if VIEWS == 4 else f"v{i}"
    scene.render.filepath = os.path.join(outdir, f"{label}.png")
    bpy.ops.render.render(write_still=True)
    print(f"  rendered {scene.render.filepath}")
print()


# Blender's teardown of a multi-GB character scene can fault AFTER the work is done ("Attempt to free
# nullptr pointer", and a macOS crash dialog). The file on disk is already complete and valid at this
# point, so skip interpreter/Blender cleanup entirely rather than let a shutdown bug look like a
# conversion failure. Exit code stays 0 because the conversion genuinely succeeded.
import os as _os
# FLUSH FIRST. `_exit` skips every buffer, and Python block-buffers stdout whenever it is not a tty —
# so this printed its bounds, height and morph report perfectly on a terminal and emitted NOTHING at
# all into a pipe or a file. Every measurement the script exists to make was being discarded by the
# line meant to hide a shutdown crash.
sys.stdout.flush()
sys.stderr.flush()
_os._exit(0)
