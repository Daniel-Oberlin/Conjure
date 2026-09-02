"""Human-readable digest of one or more scripts/inspect_blend.py dumps.

    python scripts/blend_summary.py out.json [more.json …]

Per armature: bone count, how many look like control/IK rather than deform, which naming conventions
match, roots, and the bind-pose Z extent (≈ the figure's height). Per mesh set: totals, and a flag
column [Hidden Render Armature Shapekeys scene] so the worn-vs-stored outfit split is visible at a
glance. Convention hit counts are a HINT, not a verdict — `unity_dotL` matches any `.L`/`.R` suffix
and will fire on rigs that follow no convention at all.
"""
import json
import re
import sys
from pathlib import Path

CONV = {  # bone-naming conventions the discovery pipeline's layer 1 would try
    "mixamo": r"^mixamorig[:_]",
    "rigify_def": r"^(DEF|ORG|MCH)-",
    "unreal": r"^(pelvis|spine_0\d|clavicle_[lr]|upperarm_[lr]|thigh_[lr])$",
    "valve_bip": r"^(bip_|ValveBiped)",
    "daz": r"^(hip|abdomen|chest|lCollar|rCollar)$",
    "vrm_ish": r"^(J_Bip_|Bip01)",
    "unity_dotL": r"\.(L|R)$",
    "underscore_lr": r"_(L|R)$",
}
IK_PAT = re.compile(r"(IK|Ctrl|CTR|_Master|Pole|Target|Line|Helper|Twist)", re.I)


def summarize(p):
    d = json.loads(Path(p).read_text())
    c = d["counts"]
    print(f"\n{'='*78}\n{Path(p).stem.upper()}  —  {Path(d['blend']).name}")
    print(f"  units={d['unit_system']} scale={d['unit_scale']}  "
          f"objects={c['objects']} mesh={c['meshes']} arm={c['armatures']} "
          f"mats={c['materials']} imgs={c['images']} actions={c['actions']}")

    for a in d["armatures"]:
        bones = a["bones"]
        names = [b["name"] for b in bones]
        ctrl = [n for n in names if IK_PAT.search(n)]
        print(f"\n  ARMATURE '{a['name']}'  bones={a['bone_count']}  "
              f"scale={a['scale']}  dims={a['dims']}  in_scene={a['in_scene']}")
        print(f"    control/IK-looking bones: {len(ctrl)}  "
              f"deform-ish: {a['bone_count']-len(ctrl)}")
        print(f"    constrained pose bones: {a['constrained_bones']}  "
              f"constraint types: {a['pose_constraints']}")
        hits = {k: sum(1 for n in names if re.search(v, n)) for k, v in CONV.items()}
        print(f"    convention hits: { {k:v for k,v in hits.items() if v} }")
        roots = [b["name"] for b in bones if not b["parent"]]
        print(f"    roots ({len(roots)}): {roots[:6]}")
        print(f"    sample: {names[:14]}")
        # height from bone extents
        ys = [b["head"][2] for b in bones] + [b["tail"][2] for b in bones]
        print(f"    bone Z extent: {min(ys):.3f} .. {max(ys):.3f}")

    ms = d["meshes"]
    tot_v = sum(m["verts"] for m in ms)
    skinned = [m for m in ms if any(x["type"] == "ARMATURE" for x in m["modifiers"])]
    hidden = [m for m in ms if m["hide_viewport"] or m["hide_render"]]
    withsk = [m for m in ms if m["shape_keys"]]
    print(f"\n  MESHES {len(ms)}  verts={tot_v:,}  skinned={len(skinned)}  "
          f"hidden={len(hidden)}  with-shapekeys={len(withsk)}")
    print(f"    collections: {sorted({x for m in ms for x in m['collections']})}")
    for m in sorted(ms, key=lambda m: -m["verts"])[:18]:
        flags = "".join(["H" if m["hide_viewport"] else ".",
                         "R" if m["hide_render"] else ".",
                         "A" if any(x["type"] == "ARMATURE" for x in m["modifiers"]) else ".",
                         "S" if m["shape_keys"] else ".",
                         "s" if m["in_scene"] else "-"])
        print(f"    [{flags}] {m['verts']:>7,}v {m['name'][:44]:<44} "
              f"vg={m['vertex_groups']:<4} mats={len(m['materials'])} "
              f"sk={len(m['shape_keys'])}")
    if len(ms) > 18:
        print(f"    … {len(ms)-18} more")
    if withsk:
        e = withsk[0]
        print(f"    shape-key sample ({e['name']}): {e['shape_keys'][:12]}")
    if d["actions"]:
        print(f"  ACTIONS: {[(a['name'], a['range']) for a in d['actions'][:8]]}")


for p in sys.argv[1:]:
    summarize(p)
