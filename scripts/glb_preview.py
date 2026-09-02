"""Render a GLB to PNGs from several angles, in headless Blender.

    B=/Applications/Blender.app/Contents/MacOS/Blender
    $B --background --python scripts/glb_preview.py -- model.glb outdir/ [--size 512] [--views 4]

Two jobs, both from docs/backlogs/figures.md:

  1. **Round-trip verification.** Re-importing our own export into a clean scene and rendering it is the
     cheapest way to catch a broken conversion — a collapsed rig, a lost material, a figure lying on its
     side — before it costs a headset trip. It reads the GLB the same way a client will.
  2. **The renderer for the multimodal pass** (layer 4): upright? facing which way? which mesh is the
     jacket? Also what *Visual model embedding via rendered thumbnails* in backlogs/library.md needs.

Prints the imported bounding box and height so the "is it life size" question is answered numerically,
not just by eye. Uses Workbench rather than Cycles: this is a structural check, not a beauty shot.
"""
import math
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
pos = [a for a in argv if not a.startswith("--")]
glb, outdir = pos[0], (pos[1] if len(pos) > 1 else ".")


def opt(name, d):
    return argv[argv.index(f"--{name}") + 1] if f"--{name}" in argv else d


SIZE, VIEWS = int(opt("size", 512)), int(opt("views", 4))
os.makedirs(outdir, exist_ok=True)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=glb)

# Blender's glTF importer parks placeholder objects (icosphere stand-ins for things it could not
# represent) in a collection literally named `glTF_not_exported`. They are NOT in the file and no web
# client ever sees them — but they are 2 m across, so counting them puts the bounding box and the
# camera framing a metre out. Importer artifact, not model content.
objs = [o for o in bpy.context.scene.objects if o.type == "MESH"
        and not any(c.name == "glTF_not_exported" for c in o.users_collection)]
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
for i in range(VIEWS):
    a = 2 * math.pi * i / VIEWS
    cam.location = (mid[0] + radius * math.sin(a), mid[1] - radius * math.cos(a), mid[2])
    # Aim at the model's centre: point -Z at it, keeping the camera upright.
    d = __import__("mathutils").Vector(mid) - cam.location
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
_os._exit(0)
