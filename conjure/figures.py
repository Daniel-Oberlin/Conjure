"""Humanoid skeleton inference — recover a semantic bone map from a rig that does not state one.

Discovery layer 2 of the figures pipeline (docs/backlogs/figures.md). A **humanoid bone map** translates
semantic names ("left upper arm") into a specific model's own node names, and every capability past
"place it" needs one: posing, retargeting an animation, "have her wave". VRM states it outright
(`importer.vrm_humanoid`, layer 1). Nothing else does — measured across five sample models, four rig
conventions, and only one of them said.

**This module reads shape, not names.** A humanoid skeleton has an unmistakable structure: one root, a
chain rising to a leaf at the top, two chains descending to the lowest leaves, two branching to the
widest. That holds whether the bones are called `J_Bip_L_UpperArm`, `mixamorig:LeftArm`,
`lShldrBend`, or `arm left shoulder` — and the sample models use all of those. Name tables (layer 1)
covered two of four non-VRM rigs; this covers the shape.

Pure stdlib over the glTF JSON, so it is unit-testable with no headset and no Blender — which matters,
because it is the part of the feature most likely to be quietly wrong.

The reference implementation of "quietly wrong" is why `validate()` exists: an inferred map that looks
plausible and puts the elbow in the wrong place is worse than no map at all, since everything downstream
inherits it. Saka (VRoid) states her 54 bones, so inference can be scored against a known-correct answer
before it is trusted on a rig where nothing does.
"""

from __future__ import annotations

import hashlib

import math
from typing import Optional

# The core set worth inferring. Deliberately not all 54 of VRM's — fingers and eyes are not recoverable
# from topology with any confidence, and a map is more useful honest than complete.
#: The bones a map must have to be worth keeping. Everything else in CORE_BONES is a bonus: plenty of
#: rigs have no toes, no separate clavicle and no distinct chest, and rejecting an otherwise-good
#: skeleton over a missing toe bone throws away a figure that could have posed perfectly well.
REQUIRED_BONES = ("hips", "spine", "head",
                  "leftUpperArm", "leftLowerArm", "rightUpperArm", "rightLowerArm",
                  "leftUpperLeg", "leftLowerLeg", "rightUpperLeg", "rightLowerLeg")

CORE_BONES = (
    "hips", "spine", "chest", "neck", "head",
    "leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes",
    "rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes",
    "leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand",
    "rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand",
)

#: The fingers, spelled as VRM 0.x spells them — which is not a preference but a match: `vrm_humanoid`
#: already hands us `leftIndexProximal` and the rest from the file's own extension block, and Saka is in
#: the catalog today with all thirty. A second vocabulary for the same joints would split her map from
#: everyone else's for no reason.
#:
#: **Separate from `CORE_BONES` on purpose.** Core is what a map is JUDGED on — completeness reports,
#: the probe's carried-bone count — and most rigs are missing some finger or other. Steve has none at
#: all, one Mixamo rig has only a thumb and an index, and one dot-side rig's thumb stops at two joints.
#: Folding them in would report thirty bones missing on a figure whose body map is perfect.
FINGERS = ("Thumb", "Index", "Middle", "Ring", "Little")
FINGER_JOINTS = ("Proximal", "Intermediate", "Distal")
FINGER_BONES = tuple(f"{side}{finger}{joint}"
                     for side in ("left", "right")
                     for finger in FINGERS for joint in FINGER_JOINTS)


# ---------------------------------------------------------------- node transforms


def _local_matrix(node: dict) -> list[float]:
    """A node's local transform as a column-major 4x4, from `matrix` or TRS."""
    if "matrix" in node:
        return [float(x) for x in node["matrix"]]
    t = node.get("translation", [0.0, 0.0, 0.0])
    s = node.get("scale", [1.0, 1.0, 1.0])
    x, y, z, w = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
    r = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
         2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
         2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]
    return [r[0] * s[0], r[1] * s[0], r[2] * s[0], 0.0,
            r[3] * s[1], r[4] * s[1], r[5] * s[1], 0.0,
            r[6] * s[2], r[7] * s[2], r[8] * s[2], 0.0,
            float(t[0]), float(t[1]), float(t[2]), 1.0]


def _mul(a: list[float], b: list[float]) -> list[float]:
    return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
            for c in range(4) for r in range(4)]


def node_world_matrices(doc: dict) -> dict[int, list[float]]:
    """Bind-pose world matrix of every node (column-major 4x4), by node index."""
    nodes = doc.get("nodes") or []
    out: dict[int, list[float]] = {}
    ident = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]

    def walk(idx: int, parent: list[float]) -> None:
        if idx in out or idx >= len(nodes):
            return
        world = _mul(parent, _local_matrix(nodes[idx]))
        out[idx] = world
        for child in nodes[idx].get("children", []):
            walk(child, world)

    scenes = doc.get("scenes") or []
    roots = scenes[doc.get("scene", 0)].get("nodes", []) if scenes else range(len(nodes))
    for r in roots:
        walk(r, ident)
    for i in range(len(nodes)):          # nodes outside the active scene still get a position
        walk(i, ident)
    return out


def node_world_positions(doc: dict) -> dict[int, tuple[float, float, float]]:
    """Bind-pose world position of every node, by node index."""
    return {i: (m[12], m[13], m[14]) for i, m in node_world_matrices(doc).items()}


def parent_map(doc: dict) -> dict[int, int]:
    parent: dict[int, int] = {}
    for i, n in enumerate(doc.get("nodes") or []):
        for c in n.get("children", []):
            parent[c] = i
    return parent


# ---------------------------------------------------------------- deform bones

_COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2),
              5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_NORMALIZE = {5121: 255.0, 5123: 65535.0}


def _read_vec4(doc: dict, blob: bytes, accessor_index: int, limit: int = 200000):
    """Yield up to `limit` VEC4 tuples from an accessor. Enough of a glTF reader for skin weights."""
    import struct
    acc = (doc.get("accessors") or [])[accessor_index]
    fmt, size = _COMPONENT.get(acc.get("componentType"), (None, 0))
    if not fmt or acc.get("type") != "VEC4" or "bufferView" not in acc:
        return
    bv = (doc.get("bufferViews") or [])[acc["bufferView"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or (size * 4)
    for i in range(min(acc.get("count", 0), limit)):
        off = base + i * stride
        if off + size * 4 > len(blob):
            return
        yield struct.unpack_from("<" + fmt * 4, blob, off)


def deform_joints(doc: dict, blob: bytes, min_weight: float = 0.02) -> set[int]:
    """Node indices that actually move geometry — i.e. some vertex is weighted to them.

    **The name-free way to tell a deform bone from a control bone.** Daz and Rigify rigs put IK targets,
    pole vectors and helper bones in the same skin as the real skeleton: Grace ships 482 joints and
    Yuffie 741, of which only a fraction deform anything. Those controls sit at extreme positions —
    `foot.ik.L` is below the foot, `master` is at the origin — so shape-based inference latches onto them
    and returns nonsense (measured: hips→`master`, and upper leg, lower leg and foot all →`foot.ik.L`).

    Filtering by name would mean guessing at `ik`/`ctrl`/`MCH` conventions, which differ per rig and are
    exactly what layer 2 exists to avoid. Vertex weights say it outright, and a bone with no weight
    cannot affect the mesh by definition — so it cannot be a humanoid bone we would want to pose.
    """
    # JOINTS_0 holds indices into the joint list of the skin used by the NODE that draws this mesh —
    # not a global index. Resolving them against every skin (Grace has two, 482 and 42 joints) maps each
    # index to several unrelated bones and lets control bones back in: `foot.ik.L` survived that way.
    mesh_skin: dict[int, int] = {}
    for n in doc.get("nodes") or []:
        if "mesh" in n and "skin" in n:
            mesh_skin.setdefault(n["mesh"], n["skin"])

    skins = doc.get("skins") or []
    out: set[int] = set()
    for mi, mesh in enumerate(doc.get("meshes") or []):
        si = mesh_skin.get(mi)
        if si is None or si >= len(skins):
            continue
        js = skins[si].get("joints") or []
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            ji, wi = attrs.get("JOINTS_0"), attrs.get("WEIGHTS_0")
            if ji is None or wi is None:
                continue
            wtype = (doc.get("accessors") or [])[wi].get("componentType")
            scale = _NORMALIZE.get(wtype, 1.0)
            for jj, ww in zip(_read_vec4(doc, blob, ji), _read_vec4(doc, blob, wi)):
                for k in range(4):
                    if ww[k] / scale > min_weight and 0 <= jj[k] < len(js):
                        out.add(js[jj[k]])
    return out


def split_glb(data: bytes):
    """`(json_doc, bin_chunk)` for a GLB, or `(None, b"")`."""
    import json as _json
    import struct
    if len(data) < 20 or data[:4] != b"glTF":
        return None, b""
    jl, _ = struct.unpack_from("<II", data, 12)
    doc = _json.loads(data[20:20 + jl])
    off = 20 + jl
    if off + 8 <= len(data):
        bl, _ = struct.unpack_from("<II", data, off)
        return doc, data[off + 8:off + 8 + bl]
    return doc, b""


def write_glb(doc: dict, blob: bytes = b"") -> bytes:
    """The inverse of `split_glb` — a container-relocatable GLB, chunks padded as the spec requires.

    Here rather than in a script because three callers now write GLBs (`scripts/glb_strip.py`,
    `conjure.playcanvas`, and the tests that check both), and a second copy of a binary format is a
    second place for the padding rules to be got subtly wrong.
    """
    import json as _json
    import struct
    body = _json.dumps(doc, separators=(",", ":")).encode()
    body += b" " * (-len(body) % 4)                      # JSON pads with SPACES, BIN with zeros
    out = struct.pack("<II", len(body), 0x4E4F534A) + body
    if blob:
        pad = blob + b"\x00" * (-len(blob) % 4)
        out += struct.pack("<II", len(pad), 0x004E4942) + pad
    return b"glTF" + struct.pack("<II", 2, 12 + len(out)) + out


# ---------------------------------------------------------------- layer 1: known conventions
#
# Names are free and exact where they hit, and they work on a rig whose BIND POSE defeats geometry —
# which is not hypothetical: three characters in the dev library stand with their arms at their sides,
# so the hands are no wider than the feet and layer 2's "the widest joints are the hands" collapses.
# One of them is bone-for-bone Mixamo.
#
# Only conventions verified against a real file live here. A speculative row is worse than none: it
# cannot be checked, and a name that happens to match is exactly how a control bone gets mapped over
# the deform bone it drives. `validate()` is what makes trying names safe at all.
#
# `{S}` is Left/Right, `{X}` is L/R, `|` separates alternatives tried in order.
CONVENTIONS: dict[str, dict[str, str]] = {
    # Mixamo, and everything that copies it (ReadyPlayerMe, most auto-riggers). Often exported with a
    # `mixamorig:` prefix, which `_bare` strips before matching.
    "mixamo": {
        "hips": "Hips", "spine": "Spine", "chest": "Spine1", "upperChest": "Spine2",
        "neck": "Neck", "head": "Head",
        "{s}Shoulder": "{S}Shoulder", "{s}UpperArm": "{S}Arm", "{s}LowerArm": "{S}ForeArm",
        "{s}Hand": "{S}Hand",
        "{s}UpperLeg": "{S}UpLeg", "{s}LowerLeg": "{S}Leg", "{s}Foot": "{S}Foot",
        "{s}Toes": "{S}ToeBase",
    },
    # The Rigify-derived FK naming that Daz ports carry and that `scripts/blend_to_glb.py` preserves.
    # Verified identical across three files — Grace, Yuffie and Trish — and it is worth a row even
    # though layer 2 already maps two of them: inference put Trish's whole arm on `hand.ik.L`, and on
    # all three it picked a FINGERTIP for the hand, which the names get right for free.
    # Rigify spells its FK controls two ways depending on version — `upper_arm.fk.L` on the Daz ports,
    # `upper_arm_fk.L` on a stock modern rig — and both turn up in the same library, so both are listed.
    "rigify-fk": {
        "hips": "pelvis|hip|hips", "spine": "spine|chestLower|torso",
        "chest": "chest-1|chestUpper|chest",
        "neck": "neckhead|neck",              # `neckhead` PARENTS the head; `neck` is a sibling control
        "head": "head",
        "{s}Shoulder": "clavicle.{X}|shoulder.{X}",
        "{s}UpperArm": "upper_arm.fk.{X}|upper_arm_fk.{X}",
        "{s}LowerArm": "forearm.fk.{X}|forearm_fk.{X}",
        "{s}Hand": "hand.fk.{X}|hand_fk.{X}|hand.{X}",
        "{s}UpperLeg": "thigh.fk.{X}|thigh_fk.{X}",
        "{s}LowerLeg": "shin.fk.{X}|shin_fk.{X}",
        "{s}Foot": "foot.fk.{X}|foot_fk.{X}",
        "{s}Toes": "toe.fk.{X}|toe_fk.{X}|toe.{X}",
    },
    # Rigify's DEFORM chain, with no FK controls in the file at all — what you get when the export
    # bakes the rig down (`temp/vrh/jane` came through PlayCanvas that way). Its own scheme rather than
    # extra alternates on `rigify-fk`, because a rig carrying BOTH chains should keep preferring the FK
    # controls, and equal scores keep the earlier scheme.
    #
    # The spine is addressed by INDEX, which is the one soft spot: Rigify numbers the deform chain from
    # the pelvis up, so a rig with a different number of spine or neck segments spells its head
    # differently. `neck` is stable at `.004` across the lengths seen so far, and `head` is listed
    # deepest-first so the longest chain present wins. A chain shorter than five leaves `head` unmatched
    # and drops the whole row on the floor — which is the safe failure, since it falls through to
    # inference rather than mapping a neck as a head.
    "rigify-def": {
        "hips": "DEF-spine", "spine": "DEF-spine.001", "chest": "DEF-spine.002",
        "upperChest": "DEF-spine.003", "neck": "DEF-spine.004",
        "head": "DEF-spine.007|DEF-spine.006|DEF-spine.005",
        "{s}Shoulder": "DEF-shoulder.{X}",
        "{s}UpperArm": "DEF-upper_arm.{X}",
        "{s}LowerArm": "DEF-forearm.{X}",
        "{s}Hand": "DEF-hand.{X}",
        "{s}UpperLeg": "DEF-thigh.{X}",
        "{s}LowerLeg": "DEF-shin.{X}",
        "{s}Foot": "DEF-foot.{X}",
        "{s}Toes": "DEF-toe.{X}",
    },
    # Reallusion Character Creator, which exports every bone under a `CC_Base_` prefix. Verified
    # against `Alice.glb` — a 78-joint body skin among fifteen skins in one 57 MB file.
    #
    # The row exists mainly for the HIPS. CC forks the body immediately below `CC_Base_Hip` into two
    # siblings: `CC_Base_Pelvis` carries the thighs and nothing else, `CC_Base_Waist` carries the
    # spine and nothing else. They sit at the SAME world height, so every ordering check in
    # `validate()` passes whichever one is chosen — inference picked `Pelvis`, and bending those
    # "hips" would have swung her legs while her torso stayed upright. Naming them outright removes
    # the choice.
    #
    # `head` and `toes` were wrong the same quiet way: inference stopped at `NeckTwist02` (3.4 cm
    # below the real head) and at `BigToe1` (a child of the toe base rather than the base itself).
    "cc-base": {
        "hips": "CC_Base_Hip", "spine": "CC_Base_Waist",
        "chest": "CC_Base_Spine01", "upperChest": "CC_Base_Spine02",
        # Two neck segments, named as twists; the FIRST parents the chain, as `rigify-fk` reads it.
        "neck": "CC_Base_NeckTwist01", "head": "CC_Base_Head",
        "{s}Shoulder": "CC_Base_{X}_Clavicle",
        "{s}UpperArm": "CC_Base_{X}_Upperarm",
        "{s}LowerArm": "CC_Base_{X}_Forearm",
        "{s}Hand": "CC_Base_{X}_Hand",
        "{s}UpperLeg": "CC_Base_{X}_Thigh",
        "{s}LowerLeg": "CC_Base_{X}_Calf",
        "{s}Foot": "CC_Base_{X}_Foot",
        "{s}Toes": "CC_Base_{X}_ToeBase",
    },
    # The Blender-side-suffix scheme used across several free asset packs (both `Animated Woman` models
    # and `Steve` in the dev library). Torso/Abdomen rather than Spine1/Spine2, and the side is a suffix.
    "dot-side": {
        "hips": "Hips", "spine": "Abdomen", "chest": "Torso", "upperChest": "Chest",
        "neck": "Neck", "head": "Head",
        "{s}Shoulder": "Shoulder.{X}", "{s}UpperArm": "UpperArm.{X}", "{s}LowerArm": "LowerArm.{X}",
        "{s}Hand": "Wrist.{X}|Hand.{X}|Fist.{X}",
        "{s}UpperLeg": "UpperLeg.{X}", "{s}LowerLeg": "LowerLeg.{X}", "{s}Foot": "Foot.{X}",
        "{s}Toes": "Toe.{X}|ToeBase.{X}",
    },
}


#: Finger spellings per convention, **measured off the files and not extrapolated from the body rows**,
#: because the fingers are where each of these schemes stops being regular:
#:
#: - `cc-base` calls the middle finger `Mid`, not `Middle`, and one base rig spells its whole right side
#:   lowercase — `CC_Base_r_Mid1` beside `CC_Base_L_Mid1`. The case-insensitive fallback in `resolve`
#:   already covers that; it was written for the same rig's hands.
#: - `rigify` prefixes four fingers with `f_` and the THUMB with nothing: `f_index.01.L`, `thumb.01.L`.
#: - everyone except VRM calls the little finger `Pinky`.
#: - `dot-side` has fingers after all. The plan recorded "no finger bones at all" from Steve, who has
#:   none — but `Animated Woman` and `Characters Shaun` are the same scheme and have the full set.
#:
#: What none of them promise is that a chain is COMPLETE. One Mixamo rig carries only a thumb and an
#: index; one dot-side thumb stops at two joints. A slot that matches nothing is simply absent from the
#: map, which is the same way a missing toe has always been handled.
#:
#: `{N}` is the joint 1..3 and is substituted before `{S}`/`{X}`, so a pattern reaches `resolve` looking
#: like every other candidate.
_FINGERS_BY_SCHEME: dict[str, dict[str, str]] = {
    "mixamo": {
        "Thumb": "{S}HandThumb{N}", "Index": "{S}HandIndex{N}", "Middle": "{S}HandMiddle{N}",
        "Ring": "{S}HandRing{N}", "Little": "{S}HandPinky{N}",
    },
    "rigify-fk": {
        "Thumb": "thumb.0{N}.{X}", "Index": "f_index.0{N}.{X}", "Middle": "f_middle.0{N}.{X}",
        "Ring": "f_ring.0{N}.{X}", "Little": "f_pinky.0{N}.{X}",
    },
    "rigify-def": {
        "Thumb": "DEF-thumb.0{N}.{X}", "Index": "DEF-f_index.0{N}.{X}",
        "Middle": "DEF-f_middle.0{N}.{X}", "Ring": "DEF-f_ring.0{N}.{X}",
        "Little": "DEF-f_pinky.0{N}.{X}",
    },
    "cc-base": {
        "Thumb": "CC_Base_{X}_Thumb{N}", "Index": "CC_Base_{X}_Index{N}",
        "Middle": "CC_Base_{X}_Mid{N}", "Ring": "CC_Base_{X}_Ring{N}",
        "Little": "CC_Base_{X}_Pinky{N}",
    },
    "dot-side": {
        "Thumb": "Thumb{N}.{X}", "Index": "Index{N}.{X}", "Middle": "Middle{N}.{X}",
        "Ring": "Ring{N}.{X}", "Little": "Pinky{N}.{X}",
    },
}

for _scheme, _spelling in _FINGERS_BY_SCHEME.items():
    for _finger, _pattern in _spelling.items():
        for _n, _joint in enumerate(FINGER_JOINTS, start=1):
            CONVENTIONS[_scheme][f"{{s}}{_finger}{_joint}"] = _pattern.replace("{N}", str(_n))


def _bare(name: str) -> str:
    """A node name without the prefix exporters bolt on — `mixamorig:Hips`, `Armature|Hips`."""
    for sep in (":", "|"):
        if sep in name:
            name = name.rsplit(sep, 1)[1]
    return name


def convention_humanoid(doc: dict) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """`({semanticBone: nodeName}, conventionName)` from a known naming scheme, or `(None, None)`.

    Returns the map as the FILE spells each node, not as the table does, so a prefixed export still
    hands downstream code a name it can look up.
    """
    lookup: dict[str, str] = {}
    by_index: dict[str, int] = {}
    # A second index that ignores CASE, consulted only when the exact spelling misses and only when it
    # is unambiguous. One base rig in the capture set spells nineteen of its right-side bones with a
    # LOWERCASE side letter — `CC_Base_r_Hand` beside `CC_Base_L_Hand`, and `R_Forearm` uppercase two
    # bones above it — so a strict match lost the right hand, foot, clavicle, toes and every finger,
    # on two different characters built from it. The rig is complete and symmetric; only the spelling
    # is not, and `validate()` said nothing because hands and feet are not REQUIRED_BONES.
    folded: dict[str, set[str]] = {}
    for i, node in enumerate(doc.get("nodes") or []):
        name = node.get("name")
        if name:
            lookup.setdefault(_bare(name), name)      # first spelling wins; ties are vanishingly rare
            folded.setdefault(_bare(name).lower(), set()).add(name)
            by_index.setdefault(name, i)

    def resolve(node: str) -> Optional[str]:
        """The file's own spelling of `node`, exact first then case-insensitively.

        Ambiguity is refused rather than guessed: if two nodes differ only by case, neither is worth
        more than the other and a wrong bone is worse than a missing one.
        """
        if node in lookup:
            return lookup[node]
        same = folded.get(node.lower())
        return next(iter(same)) if same and len(same) == 1 else None
    best: tuple[int, Optional[dict], Optional[str]] = (0, None, None)
    for scheme, table in CONVENTIONS.items():
        found: dict[str, str] = {}
        for slot, candidates in table.items():
            sides = (("left", "Left", "L"), ("right", "Right", "R")) if "{s}" in slot else ((None,) * 3,)
            for side, S, X in sides:
                key = slot.format(s=side) if side else slot
                for candidate in candidates.split("|"):
                    node = candidate.format(S=S, X=X) if side else candidate
                    spelled = resolve(node)
                    if spelled is not None:
                        found[key] = spelled
                        break
        # The hips slot is chosen by ANATOMY where the names are ambiguous: a rig may carry `pelvis`,
        # `hip`, `hips` and `torso`, and only one of them is the root of the body. Tamaki's `pelvis` is
        # a tweak bone off to one side; her `hips` is what the thighs actually hang from.
        #
        # The legs are not enough to say which, and getting this wrong is expensive rather than
        # cosmetic. On both Daz ports `pelvis` and `hip` are BOTH above the feet, `pelvis` is listed
        # first, and it is a SIBLING of the spine — so `hips` landed on a bone that carries the legs and
        # nothing else, and `{"hips": {"bend": 45}}` moved Grace's trunk by exactly zero degrees while
        # moving Saka's by 122. Every fold-forward pose inherited that as "the trunk is rig-dependent".
        # The hips are what the legs AND the spine hang from; requiring both picks `hip` on those two
        # rigs and changes nothing on any other, because a rig that names its hips sensibly has the
        # spine under them already.
        below = [by_index.get(found.get(b)) for b in ("leftFoot", "rightFoot", "spine")]
        below = [x for x in below if x is not None]
        if below and "hips" in table:
            parent = parent_map(doc)
            for candidate in table["hips"].split("|"):
                node = lookup.get(candidate)
                i = by_index.get(node)
                if i is not None and all(i in _ancestors(f, parent) for f in below):
                    found["hips"] = node
                    break
        score = sum(1 for b in REQUIRED_BONES if b in found)
        if score > best[0]:
            best = (score, found, scheme)
    return (best[1], best[2]) if best[0] == len(REQUIRED_BONES) else (None, None)


# ---------------------------------------------------------------- inference


def humanoid_skin(doc: dict) -> dict:
    """The skin that deforms the BODY, when a file ships several.

    Picked by how much geometry each skin carries, not by how many joints it has. Joint count was the
    first heuristic and it is wrong in the wild: `Trish` ships a 679-joint HAIR-AND-CLOTH rig beside a
    362-joint body rig, so "the biggest skeleton is the body" mapped her whole skeleton onto
    `B_HairCloth03_*` and inference returned an elbow and a knee on the same strand of hair. The backlog
    predicted the opposite (Hitomi's four hair rigs are all small) — which is the point: a rule about
    which skeleton is BIGGER is a rule about the rigger's habits, and vertex count is a rule about what
    the mesh actually is.

    Ordered by how many MESHES each skin deforms, which is only a hint: `best_humanoid` tries each in
    turn and keeps the first whose skeleton actually validates as a humanoid, because the one reliable
    test of "is this the body rig" is whether it looks like a body. Vertex count was tried and is no
    better than joint count — Trish's single hair mesh outweighs her body and all her clothes together.
    """
    skins = doc.get("skins") or []
    if len(skins) < 2:
        return skins[0] if skins else {}
    return humanoid_skin_order(doc)[0]


def humanoid_skin_order(doc: dict) -> list[dict]:
    """Every skin, most-likely-to-be-the-body first: by mesh count, then by joint count."""
    skins = doc.get("skins") or []
    meshes = [0] * len(skins)
    for node in doc.get("nodes") or []:
        si = node.get("skin")
        if node.get("mesh") is not None and si is not None and 0 <= si < len(skins):
            meshes[si] += 1
    return sorted(skins, key=lambda sk: (meshes[skins.index(sk)], len(sk.get("joints") or [])),
                  reverse=True)


def _ancestors(idx: int, parent: dict[int, int]) -> list[int]:
    """idx, then each parent up to the root."""
    chain, seen = [idx], {idx}
    while idx in parent and parent[idx] not in seen:
        idx = parent[idx]
        chain.append(idx)
        seen.add(idx)
    return chain


def _path(top: int, bottom: int, parent: dict[int, int]) -> Optional[list[int]]:
    """Nodes from `top` down to `bottom` inclusive, or None if `top` is not an ancestor."""
    chain = _ancestors(bottom, parent)
    if top not in chain:
        return None
    return list(reversed(chain[:chain.index(top) + 1]))


def _pick_by_height(chain: list[int], pos: dict, frac: float) -> int:
    """The chain joint sitting closest to `frac` of the way down it, by height.

    Chains are not a fixed length: a VRM leg is hip→knee→ankle→toe, while a Daz leg interleaves twist
    and bend helpers and can run a dozen joints. Indexing by position in the list would therefore pick
    a twist bone on one rig and the knee on another. Height is what "knee" actually means.
    """
    ys = [pos[j][1] for j in chain]
    hi, lo = max(ys), min(ys)
    if hi - lo < 1e-6:
        return chain[min(int(frac * (len(chain) - 1)), len(chain) - 1)]
    target = hi - frac * (hi - lo)
    return min(chain, key=lambda j: abs(pos[j][1] - target))


def _pick_by_reach(chain: list[int], pos: dict, frac: float) -> int:
    """The chain joint closest to `frac` of the cumulative distance along it, from the first joint.

    The limb counterpart of `_pick_by_height`. An arm runs sideways, so height cannot order it; path
    length can, and it is invariant to how many twist or parent helpers a rig interleaves.
    """
    if len(chain) < 2:
        return chain[0]
    acc, total = [0.0], 0.0
    for a, b in zip(chain, chain[1:]):
        total += math.dist(pos[a], pos[b])
        acc.append(total)
    if total < 1e-9:
        return chain[0]
    target = frac * total
    return chain[min(range(len(chain)), key=lambda i: abs(acc[i] - target))]


def infer_humanoid(doc: dict, blob: bytes = b"", skin: Optional[dict] = None) -> Optional[dict[str, str]]:
    """`{semanticBone: nodeName}` inferred from skeleton shape, or None if it does not look humanoid.

    The identification order matters: extremities first, because they are unambiguous geometric extremes,
    then the joints between them by path. Feet are the lowest joints, hands the widest, head the highest;
    hips is then simply where the two leg paths meet.
    """
    nodes = doc.get("nodes") or []
    skins = doc.get("skins") or []
    if not nodes or not skins:
        return None
    joints = (skin if skin is not None else humanoid_skin(doc)).get("joints") or []
    if len(joints) < 8:
        return None
    pos = node_world_positions(doc)
    parent = parent_map(doc)
    joints = [j for j in joints if j in pos]
    # Keep only bones that actually deform geometry, when the binary chunk is available to tell us.
    # Without this, control bones dominate every geometric extreme and inference returns nonsense.
    if blob:
        deform = deform_joints(doc, blob)
        kept = [j for j in joints if j in deform]
        if len(kept) >= 8:
            joints = kept
    jset = set(joints)

    def name(i: int) -> Optional[str]:
        return nodes[i].get("name") if 0 <= i < len(nodes) else None

    # Extremities. Feet/hands are picked per side so a lopsided bind pose cannot return two left feet.
    left = [j for j in joints if pos[j][0] > 0]
    right = [j for j in joints if pos[j][0] < 0]
    if not left or not right:
        return None
    # glTF is Y-up and +X is the model's LEFT when it faces +Z (the convention every sample follows).
    children = {i: list(n.get("children") or []) for i, n in enumerate(nodes)}

    def branch_up(idx: int, want: int) -> int:
        """Walk up from `idx` to the highest ancestor with at least `want` children, else `idx`.

        Extremes are the wrong answer by one joint, consistently. The widest joint in the rig is a
        FINGERTIP, not the hand — but a hand is where the finger chains diverge, so walking up to the
        FIRST joint that branches finds it exactly. Measured on Saka: the widest joint is
        `J_Bip_L_Middle3`, three above it is `J_Bip_L_Hand` with five children.

        First, not last: the chest also has five children, so continuing up past the hand lands on the
        trunk and the arm disappears entirely.
        """
        for a in _ancestors(idx, parent)[1:]:
            if a not in jset:
                break
            if len(children.get(a, [])) >= want:
                return a
        return idx

    foot_l = min(left, key=lambda j: pos[j][1])
    foot_r = min(right, key=lambda j: pos[j][1])
    hand_l = branch_up(max(left, key=lambda j: pos[j][0]), 3)
    hand_r = branch_up(min(right, key=lambda j: pos[j][0]), 3)
    # The head, in two steps, because both failure modes are real.
    #
    # The highest joint is often a HAIR bone — hair and cloth rigs ride the same skin as the body. A hair
    # strand is an unbranching chain hanging off the head, so walking up to the first branch point lands
    # on the head exactly (Saka: highest joint is `J_Sec_Hair1_01`, its branch point is `J_Bip_C_Head`).
    #
    # But a rig whose head has no children at all would then walk right past the neck and chest to the
    # HIPS, which branch into four limbs. So the branch point is only accepted while it is still in the
    # upper body; below that we keep the plain highest joint. Limb subtrees are excluded first, since a
    # hips: where the two leg chains meet.
    anc_l, anc_r = _ancestors(foot_l, parent), _ancestors(foot_r, parent)
    common = [a for a in anc_l if a in anc_r]
    if not common:
        return None
    hips = common[0]

    # The head, in two steps, because both failure modes are real.
    #
    # The highest joint is often a HAIR bone — hair and cloth rigs ride the same skin as the body. A hair
    # strand is an unbranching chain hanging off the head, so walking up to the first branch point lands
    # on the head exactly (Saka: highest joint is `J_Sec_Hair1_01`, its branch point is `J_Bip_C_Head`).
    #
    # But a rig whose head has no children would then walk right past the neck and chest to the HIPS,
    # which branch into four limbs. So a branch point is only accepted while it is still in the UPPER
    # BODY. Limb subtrees are excluded first, since a raised arm can otherwise out-rank the head.
    limb_roots = {hand_l, hand_r, foot_l, foot_r}
    excluded = set(limb_roots)
    for r in limb_roots:
        stack = [r]
        while stack:                                    # everything below a hand/foot: fingers, toes
            cur = stack.pop()
            for c in children.get(cur, []):
                if c not in excluded:
                    excluded.add(c)
                    stack.append(c)
    trunk_joints = [j for j in joints if j not in excluded] or joints
    highest = max(trunk_joints, key=lambda j: pos[j][1])
    trunk_joints = [j for j in joints if j not in excluded] or joints
    highest = max(trunk_joints, key=lambda j: pos[j][1])
    # The head is the COMMON ANCESTOR of the tallest joints: hair strands, eye bones and face-rig
    # controls all attach to the skull, so whatever they share IS the skull, whatever they are named.
    #
    # KNOWN WEAK POINT — correct on 2 of 3 real rigs. Four rules were tried and each failed on a
    # different model, which is the signature of fitting noise rather than finding structure:
    #   - first branch point walking up  -> stopped at `upperFaceRig` (Grace), `hair right upper 3a`
    #     (Yuffie); hair chains branch among themselves.
    #   - common ancestor of the top 8%  -> correct on Saka and Grace, one joint too deep on Yuffie,
    #     whose tallest joints all sit under a single hair group.
    #   - the same with a wider cut      -> collapses to the hips; the usable window differs per model.
    #   - highest CENTRED ancestor       -> the tallest joint is itself centred on Saka, so it stops
    #     immediately on a hair bone.
    # Every failing variant still validated CLEAN, because a hair bone above the neck is plausible
    # geometry. Only driving the map in an actual POSE exposed any of it — which is the real lesson.
    span = pos[highest][1] - pos[hips][1]
    cut = pos[highest][1] - max(0.08 * span, 1e-4)
    top = [j for j in trunk_joints if pos[j][1] >= cut] or [highest]
    common_chain = _ancestors(top[0], parent)
    for j in top[1:]:
        anc = set(_ancestors(j, parent))
        common_chain = [a for a in common_chain if a in anc]
    head_top = common_chain[0] if common_chain else highest
    if head_top == hips and highest != hips:      # a rig with no hair or face bones yields itself
        head_top = highest

    out: dict[str, str] = {}

    def put(bone: str, idx: Optional[int]) -> None:
        if idx is not None and name(idx):
            out[bone] = name(idx)

    put("hips", hips)

    # Legs: hips → foot. The toe is the child beyond the foot, if the rig has one.
    for side, foot in (("left", foot_l), ("right", foot_r)):
        chain = _path(hips, foot, parent)
        if not chain or len(chain) < 3:
            continue
        body = chain[1:]                                   # drop hips itself
        # The lowest joint is the toe when there is one below the ankle, else the foot.
        toe = body[-1]
        ankle = _pick_by_height(body, pos, 0.88)
        if ankle == toe and len(body) > 2:
            ankle = body[-2]
        put(f"{side}UpperLeg", _pick_by_height(body, pos, 0.0))
        put(f"{side}LowerLeg", _pick_by_height(body, pos, 0.5))
        put(f"{side}Foot", ankle)
        if toe != ankle:
            put(f"{side}Toes", toe)

    # Spine: hips → head. Named stops by height fraction, same reasoning as the legs.
    # The spine runs from hips to head — but conversion can re-parent the head chain so that hips is no
    # longer its ancestor, and a strict path lookup then returns nothing and silently drops spine, chest,
    # neck AND head. Fall back to the lowest joint the two share, which is the spine's base either way.
    spine = _path(hips, head_top, parent)
    if not spine:
        anc_head = _ancestors(head_top, parent)
        anc_hips = set(_ancestors(hips, parent))
        base = next((a for a in anc_head if a in anc_hips), None)
        spine = _path(base, head_top, parent) if base is not None else None
    if spine and len(spine) >= 3:
        body = spine[1:]
        put("spine", _pick_by_height(body, pos, 1.0))       # lowest of the rising chain
        put("chest", _pick_by_height(body, pos, 0.55))
        put("head", body[-1])
        # The neck is simply the highest trunk joint BELOW the head. A height fraction was tuned on one
        # model and picked the head itself on another whose neck sits proportionally lower — the same
        # brittleness as index-based ordering, in a different disguise.
        below_head = [j for j in body if j != body[-1]]
        if below_head:
            put("neck", max(below_head, key=lambda j: pos[j][1]))

    # Arms: the chain out to each hand, ending where the two arms meet.
    _al, _ar = _ancestors(hand_l, parent), set(_ancestors(hand_r, parent))
    shared_torso = set()
    for a in _al:
        if a in _ar:
            shared_torso = set(_ancestors(a, parent))     # that joint and everything above it
            break
    for side, hand in (("left", hand_l), ("right", hand_r)):
        # An arm ends where the two arms MEET. The common ancestor of the left and right hands is the
        # upper torso by definition, so everything below it on each side is that arm — exact, and with
        # no threshold to tune.
        #
        # "Walk up to the spine path" was used first and broke once conversion re-parented the shoulder
        # off the hips->head chain, collapsing both Daz arms to the armature root. A laterality
        # threshold replaced it and was worse: any fraction that keeps a shoulder on one rig cuts above
        # it on another.
        chain = _ancestors(hand, parent)
        branch = []
        for j in chain:
            if j == hips or j in shared_torso:
                break
            branch.append(j)
        branch = list(reversed(branch))                     # shoulder-most first
        # Deliberately NOT filtered to deform bones. The deform set is right for picking EXTREMES (it is
        # what stopped `foot.ik.L` winning), but wrong for the path between them: Grace's actual
        # `upper_arm.L` and `forearm.L` carry no vertex weights at all — her rig deforms through separate
        # DEF- twist bones — so filtering the chain deleted the entire arm. A joint on the structural
        # path from hand to trunk is an arm joint whether or not any vertex happens to hang off it.
        if len(branch) < 2:
            continue
        put(f"{side}Hand", branch[-1])
        # Same idea as the legs: pick by FRACTION ALONG the limb, not by index in the list. Chains vary
        # wildly in length — Saka's arm is 4 joints, Grace's is 9 with parent/twist helpers interleaved
        # — so `branch[1]` is the upper arm on one rig and a helper on the next. Distance along the
        # chain is what "elbow" actually means. Index-based ordering shifted Grace's whole arm by one.
        put(f"{side}Shoulder", _pick_by_reach(branch, pos, 0.0))
        put(f"{side}UpperArm", _pick_by_reach(branch, pos, 0.15))
        put(f"{side}LowerArm", _pick_by_reach(branch, pos, 0.55))
    return (prefer_deform(out, doc, blob) if out else None) or None


# ---------------------------------------------------------------- validation


def prefer_deform(mapping: dict, doc: dict, blob: bytes, tol: float = 0.002) -> dict:
    """Swap any mapped bone that deforms NOTHING for the co-located bone that does.

    Structure identifies the right *joint*; it does not guarantee the right *bone to rotate*. Rigify and
    Daz rigs split every joint in two — an FK control (`upper_arm.L`, `thigh.L`) that a human poses, and
    the deform bones (`upper_arm.bend.L`, `thigh.bend.L`) that actually carry vertex weights. In Blender
    a constraint links them. **glTF drops constraints**, so rotating the control moves nothing at all:
    reported from the headset as Grace's arms and legs being immovable while her head — which happens to
    be a deform bone itself — worked fine.

    The rigs place the deformer at exactly the same point as its control (measured distance 0.000), so
    the substitution is unambiguous: same joint, but the half of it that moves geometry.

    Skipped when the target is already taken, since two bones mapped to one node is precisely the
    degenerate map `validate()` exists to reject.
    """
    if not blob or not mapping:
        return mapping
    deform = deform_joints(doc, blob)
    if not deform:
        return mapping
    nodes = doc.get("nodes") or []
    pos = node_world_positions(doc)
    parent = parent_map(doc)
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    # A bone with deform DESCENDANTS already moves geometry when rotated — substituting is not just
    # unnecessary there, it is harmful: it swaps a control that drives a whole limb for one segment of
    # it (`leftLowerArm` -> `upper_arm.twist.L`, still inside the upper arm). Only bones that move
    # nothing at all need replacing. Which of the two applies depends on whether the conversion rebuilt
    # the deform hierarchy, so it must be checked rather than assumed.
    kids: dict = {}
    for i, n in enumerate(nodes):
        for c in n.get("children") or []:
            kids.setdefault(i, []).append(c)

    def deform_reach(i):
        """How many deform bones this one drives — the size of its deform subtree.

        Depth and deform-ness are both PROXIES and each picks a different wrong bone. Measured on Grace,
        three co-located candidates for the upper leg:

            thigh.fk.L    drives only the foot   (FK chain; the thigh deformers are not under it)
            thigh.bend.L  drives only itself     (one segment — this is the zig-zag)
            thigh.L       drives the whole leg   (thigh deformers as children, shin chain below)

        What posing wants is the bone that moves the most of the limb, so count that directly instead of
        guessing at a correlate of it.
        """
        stack, seen, n = [i], {i}, 0
        while stack:
            cur = stack.pop()
            if cur in deform:
                n += 1
            for c in kids.get(cur, []):
                if c not in seen:
                    seen.add(c)
                    stack.append(c)
        return n

    def deform_depth(i, limit=4):
        """How far below `i` the nearest deform bone is, or None. DEPTH, not mere presence.

        Rigify carries two parallel chains that diverge high up: an FK chain
        (`thigh.fk.L -> shin.fk.L -> foot.fk.L`) and an ORG chain (`thigh.L -> shin.L`) that the thigh
        and shin deformers actually hang from. Only the FOOT deformer hangs off the FK chain. So
        `thigh.fk.L` has a deform descendant and is still the wrong bone — rotating it moved Grace's
        foot and nothing else, reported as feet disconnected from ankles.

        The bone worth rotating is the one the deformers hang from CLOSELY, so depth is the tiebreak.
        """
        frontier, seen, depth = [i], {i}, 0
        while frontier and depth <= limit:
            if any(j in deform for j in frontier):
                return depth
            nxt = []
            for cur in frontier:
                for c in kids.get(cur, []):
                    if c not in seen:
                        seen.add(c)
                        nxt.append(c)
            frontier = nxt
            depth += 1
        return None

    taken = set(mapping.values())
    out = dict(mapping)
    # Claim order matters: a shoulder and an upper arm are often CO-LOCATED (Grace's `arm_parent.L` sits
    # exactly on `upper_arm.L`), so whichever is processed first takes the single nearby deformer. The
    # upper arm is the one worth moving — a shoulder rotation is a subtlety, an arm rotation is the
    # gesture — so shoulders claim last.
    order = sorted(mapping, key=lambda b: ("Shoulder" in b, b))
    for bone in order:
        node_name = mapping[bone]
        i = by_name.get(node_name)
        if i is None or i not in pos:
            continue
        mine = deform_reach(i)
        # Prefer a CO-LOCATED node that drives MORE of the limb, breaking ties toward the shallower one.
        best, best_reach, best_depth = None, mine, deform_depth(i)
        ancestors_of_i = set(_ancestors(i, parent))
        for j, pj in pos.items():
            if j == i or nodes[j].get("name") in taken or not nodes[j].get("name"):
                continue
            if math.dist(pos[i], pj) > tol:
                continue
            # Never substitute UPWARD. An ancestor always drives more of the body by definition, so a
            # reach metric alone happily replaces an upper arm with the armature root. Moving up the
            # tree can only lose specificity: it is a different, larger joint, not a better spelling of
            # the same one.
            if j in ancestors_of_i:
                continue
            rj, dj = deform_reach(j), deform_depth(j)
            if rj > best_reach or (rj == best_reach and dj is not None
                                   and (best_depth is None or dj < best_depth)):
                best, best_reach, best_depth = j, rj, dj
        if best is not None:
            taken.discard(node_name)
            out[bone] = nodes[best].get("name")
            taken.add(out[bone])
    return out


#: The limb chains, top-down. `validate` checks each link is a real parent-child relationship and
#: `prune_map` drops what fails, so both have to be reading the same list.
LIMB_CHAINS = (("leftShoulder", "leftUpperArm", "leftLowerArm", "leftHand"),
               ("rightShoulder", "rightUpperArm", "rightLowerArm", "rightHand"),
               ("leftUpperLeg", "leftLowerLeg", "leftFoot", "leftToes"),
               ("rightUpperLeg", "rightLowerLeg", "rightFoot", "rightToes"))


def prune_map(doc: dict, mapping: dict[str, str]) -> dict[str, str]:
    """A map with the entries that cannot be posed removed, rather than the whole map thrown away.

    A rig often names a bone that is not the one it looks like. Both `Animated Woman` models and `Steve`
    have a `Foot.L` — an IK TARGET parented to the armature root, sitting beside a `PoleTarget.L` — so
    rotating the shin would leave it behind. Before this, one such bonefailing the whole map and cost a
    figure its arms, legs and spine along with its ankle.

    So a broken link drops the DISTAL bone and everything below it on that chain, which is the
    conservative direction: you lose a bone rather than gain a lie. What survives is a map that poses
    everything it claims to.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    parent = parent_map(doc)
    out = {b: n for b, n in mapping.items() if n in by_name}
    for chain in LIMB_CHAINS:
        present = [b for b in chain if b in out]
        for upper, lower in zip(present, present[1:]):
            iu, il = by_name[out[upper]], by_name[out[lower]]
            if iu != il and iu not in _ancestors(il, parent):
                for b in chain[chain.index(lower):]:
                    out.pop(b, None)
                break
    return out


def follow_bones(doc: dict, mapping: dict[str, str], blob: bytes = b"",
                 reach: float = 0.4) -> dict[str, str]:
    """`{node: node it should ride}` — bones that deform the mesh but hang outside the posable skeleton.

    An IK rig parents the hand or the foot to the armature ROOT and lets an animation drive it. Perfectly
    good for playing clips, and for posing it means the limb rotates while the extremity stays where it
    was, stretching the mesh between them. Reported twice from the headset in the same day: feet glued to
    the floor on two asset-pack characters, then Trish's hands hanging in the air as her arms went up.

    The rule is about ORPHANS rather than chains, because the second case was a level deeper than the
    first: Trish's fingers hang off `Fist.L` off `hand.ik.L` off `master`, and only `hand.ik.L` sits
    where the wrist does. So: find every deform bone that no mapped bone can move, walk up to the top of
    its detached subtree, and ride the nearest mapped bone — which for that subtree root means the joint
    it is standing on.

    `reach` bounds it as a fraction of the figure's height: a subtree root further than that from any
    mapped bone is not an extremity that got detached, it is something else in the file, and moving it
    would be a guess. Set generously — a detached ankle is a whole shin away from the nearest joint the
    map still holds, and on stylised proportions that is a third of the figure (0.30 on one asset-pack
    character, which a tighter bound had just excluded). The failure modes are asymmetric: too generous
    attaches a stray bone to a nearby joint, which is roughly where it belongs anyway, while too tight
    leaves a hand hanging in the air.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    parent = parent_map(doc)
    pos = node_world_positions(doc)
    deform = deform_joints(doc, blob) if blob else None
    mapped = {by_name[v]: b for b, v in mapping.items() if v in by_name}
    if not mapped:
        return {}
    ys = [pos[i][1] for i in pos]
    limit = reach * ((max(ys) - min(ys)) if ys else 1.0)

    # Nodes that have a mapped bone somewhere BELOW them: the armature root does, an IK hand does not.
    # Climbing stops at the first of these, which is what separates "the top of a detached subtree" from
    # "the root of the whole skeleton" — the two look identical from underneath.
    above_mapped: set[int] = set()
    for i in mapped:
        for a in _ancestors(i, parent)[1:]:
            above_mapped.add(a)

    def orphan(i: int) -> bool:
        return not (set(_ancestors(i, parent)) & set(mapped))

    roots: set[int] = set()
    for i in (deform if deform is not None else set(pos)):
        if i not in pos or not orphan(i):
            continue
        top = i
        for a in _ancestors(i, parent)[1:]:
            if a in above_mapped or a not in pos:
                break
            top = a
        # ...and never a bone that has a mapped one BELOW it. Being an orphan is about what is above
        # you; a torso control with the whole skeleton underneath it is not a detached extremity, and
        # making it ride its own descendant is a cycle. Caught on two rigs at once: `Body` was about to
        # follow the `Hips` it parents.
        if parent.get(top) is not None and top not in above_mapped:
            roots.add(top)

    # Nearest joint, but not across the body: a left ankle rides a left knee. Distance alone picks the
    # OTHER foot on a narrow stance — measured on the test skeleton, where the feet are closer to each
    # other than either is to its own knee.
    side = 0.02 * ((max(ys) - min(ys)) if ys else 1.0)

    def rank(orphan: int, candidate: int):
        ox, cx = pos[orphan][0], pos[candidate][0]
        crossed = abs(ox) > side and abs(cx) > side and (ox > 0) != (cx > 0)
        return (crossed, math.dist(pos[orphan], pos[candidate]))

    out: dict[str, str] = {}
    for top in roots:
        lead = min(mapped, key=lambda j: rank(top, j))
        if math.dist(pos[top], pos[lead]) <= limit:
            out[nodes[top].get("name")] = nodes[lead].get("name")
    return out


def best_humanoid(doc: dict, blob: bytes = b"") -> tuple[Optional[dict], Optional[str], dict]:
    """`(map, source, follows)` — the discovery pipeline's cheap layers, in order, gated by `validate()`.

    Layer 0 (STATED) is the file telling us outright — a VRM's own `humanBones` block. Layer 1 (names)
    is free and exact where it hits, and works on a bind pose that defeats geometry. Layer 2 (shape)
    works on names that mean nothing. 1 and 2 fail on opposite inputs, which is the whole argument for
    having both.

    **Layer 0 used to be the caller's job, and that quietly split one figure in two.** The importer read
    the VRM block and stored it as `humanoid`, then called `rig_signature`, which calls THIS — so Saka's
    catalog row held a 54-bone stated map beside a fingerprint computed over a 21-bone inferred one, and
    every consumer that compares by signature was comparing over a map nobody had stored. The retarget
    had it worse: `Rig` has bytes and no caller, so it could not reach the stated map at all and drove
    her with 21 bones while her file names 54, fingers included. A stated map is the best evidence there
    is; the only reason to hold it at arm's length was that nothing checked it, and `validate()` does.

    `follows` is computed from the PRUNED map, because it is about exactly the bones pruning removed:
    what the map cannot pose, the mesh still has to hang off something.
    """
    from .importer import vrm_humanoid                  # local: importer imports this module
    for candidate, source in (((vrm_humanoid(doc) or {}, "vrm"), None),
                              (convention_humanoid(doc), None), (None, "inferred")):
        if candidate is not None:
            raw, scheme = candidate
            if scheme == "vrm":
                source = "vrm" if raw else None
            else:
                source = f"convention:{scheme}" if raw else None
        else:
            # Every skin in turn, likeliest first. Which one is the BODY cannot be told from joint count
            # (Trish's hair rig has 679 to her body's 362) or from vertex count (her hair mesh outweighs
            # everything else), and the one test that does not depend on a rigger's habits is whether
            # the skeleton validates as a humanoid. So: try, and let the validator answer.
            for candidate_skin in humanoid_skin_order(doc) or [{}]:
                raw = infer_humanoid(doc, blob, candidate_skin)   # reads that skin's vertex weights
                if not raw:
                    continue
                pruned = prune_map(doc, raw)
                if not validate(doc, pruned, blob):
                    return pruned, "inferred", follow_bones(doc, pruned, blob)
            continue
        if not raw:
            continue
        pruned = prune_map(doc, raw)
        if not validate(doc, pruned, blob):
            return pruned, source, follow_bones(doc, pruned, blob)
    return None, None, {}


def validate(doc: dict, mapping: dict[str, str], blob: bytes = b"") -> list[str]:
    """Geometric problems with a bone map — empty means it is self-consistent.

    **This is the load-bearing half.** An inferred map that is plausible but wrong is worse than none,
    because posing and retargeting both inherit it silently. These checks are cheap, name-independent,
    and catch the failures that actually happen: a left/right swap, an elbow above a shoulder, a hips
    that is not really the root.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    pos = node_world_positions(doc)
    parent = parent_map(doc)
    problems: list[str] = []

    def idx(bone: str) -> Optional[int]:
        return by_name.get(mapping.get(bone or "", ""))

    def y(bone: str) -> Optional[float]:
        i = idx(bone)
        return pos[i][1] if i is not None and i in pos else None

    def x(bone: str) -> Optional[float]:
        i = idx(bone)
        return pos[i][0] if i is not None and i in pos else None

    def z(bone: str) -> Optional[float]:
        i = idx(bone)
        return pos[i][2] if i is not None and i in pos else None

    # WHICH WAY THE FIGURE FACES, because "+X is the model's left" is only true of one facing and is
    # not a property of glTF — it is a property of how the model happened to be authored. Two figures
    # in the corpus are built facing -Z, a clean 180 degrees from the rest, and for them the left hand
    # is correctly at NEGATIVE x. The absolute rule rejected their name-based maps on four counts at
    # once ("sides look swapped"), so `best_humanoid` discarded a correct map and fell through to
    # inference — which then honoured +X and produced a genuinely MIRRORED one. Asking either of them
    # for a left hand returned the right.
    #
    # Read off the FEET: toes are forward of the ankle on any figure, whichever way it faces, and that
    # holds under a side swap because each toe is compared against its own foot. Measured across every
    # rigged model in the corpus the two sides agree unanimously, with a margin of 2.2 cm at worst and
    # 9 cm typically — so it is read as a direction, not trusted as a magnitude. With no toes mapped it
    # falls back to +Z, which is what the rule assumed all along.
    #
    # Taken as a VECTOR in the ground plane rather than as a sign along Z, because a figure can be
    # turned any amount and not only by 180 degrees. Stewardess is composed from a scene entity yawed
    # 90 degrees, so her forward is -X and her sides separate along Z; the sign-along-Z version read
    # her facing as -Z and then compared her hands along X, where they sit at the same coordinate to
    # three decimals. It rejected her name-based map on all four side checks, inference could not
    # replace it, and she came out with NO rig signature at all — no `shipped_with`, no compatible
    # clips, the one figure of twenty that could not be animated.
    forward = [0.0, 0.0]                            # (x, z) — the ground plane; Y is up and not in it
    for side in ("left", "right"):
        toe, ankle = idx(f"{side}Toes"), idx(f"{side}Foot")
        if toe is not None and ankle is not None and toe in pos and ankle in pos:
            forward[0] += pos[toe][0] - pos[ankle][0]
            forward[1] += pos[toe][2] - pos[ankle][2]
    if abs(forward[0]) < 1e-6 and abs(forward[1]) < 1e-6:
        forward = [0.0, 1.0]                        # nothing to read: +Z, the original assumption
    # +X is the model's LEFT when it faces +Z, so left is forward turned a quarter turn: (fz, -fx).
    # For a figure facing ±Z this is (±1, 0) and `side()` below reduces to ±x — the rule it replaces.
    left = (forward[1], -forward[0])

    def side(bone: str) -> Optional[float]:
        """How far along the figure's OWN left the bone sits. Sign is all that is read."""
        i = idx(bone)
        if i is None or i not in pos:
            return None
        return pos[i][0] * left[0] + pos[i][2] * left[1]

    def _axis(vec) -> str:
        """`(x, z)` as the nearest axis name, for saying which way the check was taken."""
        return ("+x" if vec[0] > 0 else "-x") if abs(vec[0]) >= abs(vec[1]) else \
               ("+z" if vec[1] > 0 else "-z")

    # 0. Distinctness and completeness. These fire FIRST because they are what let a hopeless map pass
    #    as clean: Grace's inference mapped leftUpperLeg, leftLowerLeg and leftFoot all to the same IK
    #    control, so every ordering comparison was equal-not-less and every segment length was zero —
    #    and the length checks skipped themselves on a `> 1e-4` guard. A validator that stays silent on
    #    a degenerate map is worse than no validator, because it launders the map as verified.
    seen: dict[str, str] = {}
    for bone in CORE_BONES:
        node = mapping.get(bone)
        if not node:
            continue
        if node in seen:
            problems.append(f"{bone} and {seen[node]} are both mapped to {node!r}")
        else:
            seen[node] = bone
    missing = [b for b in REQUIRED_BONES if not mapping.get(b)]
    if missing:
        problems.append(f"{len(missing)} required bone(s) unmapped: {', '.join(missing[:6])}"
                        + (" …" if len(missing) > 6 else ""))

    # 1. Sides, RELATIVE TO THE FACING computed above. A swap here inverts every later pose.
    for l, r in (("leftHand", "rightHand"), ("leftFoot", "rightFoot"),
                 ("leftUpperArm", "rightUpperArm"), ("leftUpperLeg", "rightUpperLeg")):
        sl, sr = side(l), side(r)
        if sl is not None and sr is not None and sl - sr <= 0:
            problems.append(f"{l} is not on the {_axis(left)} side of {r} ({sl:+.3f} vs {sr:+.3f}) "
                            f"— sides look swapped (this figure faces {_axis(forward)})")

    # 2. Vertical order along the body.
    for upper, lower in (("head", "neck"), ("neck", "chest"), ("chest", "spine"), ("spine", "hips"),
                         ("leftUpperLeg", "leftLowerLeg"), ("leftLowerLeg", "leftFoot"),
                         ("rightUpperLeg", "rightLowerLeg"), ("rightLowerLeg", "rightFoot")):
        yu, yl = y(upper), y(lower)
        # 5 mm of slack: Yuffie's `spine` sits a fraction of a millimetre below her `hip`, which is a
        # real ordering but not a real problem. A validator that cries over float noise gets ignored.
        if yu is not None and yl is not None and yu < yl - 0.005:
            problems.append(f"{upper} sits below {lower} ({yu:+.3f} < {yl:+.3f})")

    # 3. Hips must be an ancestor of the feet and the head — the definition of the root of a body.
    # Hips must be an ancestor of the FEET — that is what makes it the root of the body. NOT of the
    # head: conversion re-parents the head chain onto a torso control, so hips legitimately stops being
    # its ancestor while the map stays correct. Keeping that check turned a good map into a rejected one.
    hips_i = idx("hips")
    if hips_i is not None:
        for bone in ("leftFoot", "rightFoot"):
            i = idx(bone)
            if i is not None and hips_i not in _ancestors(i, parent):
                problems.append(f"hips is not an ancestor of {bone}")

    # 3a. Hips ONE LEVEL TOO LOW. Not "hips must be an ancestor of the spine" — that is the check this
    #     deliberately is not, because conversion re-parents the trunk onto a torso control and the two
    #     legitimately sit on separate branches (Eve Maccaro: `ORG-spine` carries the legs while `chest`
    #     hangs off `torso`, four levels away, and the map is fine).
    #
    #     The failure this DOES catch is narrower and has a signature: the chosen hips carries the legs
    #     and not the spine, while its OWN PARENT carries both. That is a bone one step too far down a
    #     fork, and it is what Reallusion's rig invites — `CC_Base_Hip` forks into `CC_Base_Pelvis`
    #     (thighs only) and `CC_Base_Waist` (spine only), the two sit at the SAME world height so every
    #     ordering check passes, and inference took the Pelvis. Bending those hips swings the legs and
    #     leaves the torso upright, which is the 0°-versus-122° trunk bug wearing different names.
    #
    #     Measured across all eleven rigged models in the dev library plus the four captured figures:
    #     it fires on the bad map and on nothing else.
    if hips_i is not None:
        spine_i = idx("spine")
        above = parent.get(hips_i)
        if (spine_i is not None and above is not None
                and hips_i not in _ancestors(spine_i, parent)
                and above in _ancestors(spine_i, parent)):
            problems.append(f"hips {mapping.get('hips')!r} carries the legs but not the spine, while its "
                            f"parent {(nodes[above] or {}).get('name')!r} carries both — hips is one "
                            f"level too low, and bending it would leave the torso behind")

    # 3b. A limb must be a CHAIN — the forearm's node has to sit UNDER the upper arm's, or rotating the
    #     upper arm leaves the forearm behind. That is not a hypothetical: it is the zig-zag arm reported
    #     from the headset, and the maps that produced it validated CLEAN here for a week. Every other
    #     check in this function looks at where joints ARE; this one asks whether moving one moves the
    #     next, which is the only question posing actually cares about.
    #
    #     Measured on the catalogue: it separates the maps inferred before conversion rebuilt the deform
    #     hierarchy (forearm parented to the armature root — broken) from every other map, with no
    #     threshold and no false positives.
    #     LIMBS ONLY, and that restriction is not caution — it is measured. Conversion re-parents the
    #     trunk onto a torso control, so `spine` legitimately stops being a child of `hips` on both Daz
    #     rigs while the map stays correct and poses correctly on device. Including the trunk here would
    #     reject two maps that work, which is the same mistake the hips-ancestor-of-head check made.
    for chain in LIMB_CHAINS:
        for upper, lower in zip(chain, chain[1:]):
            iu, il = idx(upper), idx(lower)
            if iu is None or il is None or iu == il:
                continue
            if iu not in _ancestors(il, parent):
                problems.append(f"{lower} is not below {upper} in the skeleton — rotating {upper} "
                                f"would leave it behind")

    # 3c. A mapped limb bone must MOVE something. Positions and parenting can all be right while the
    #     bone drives no vertices at all: Tamaki's `upper_arm_fk.L` sits exactly where an upper arm
    #     belongs, in a proper chain, and her deform bones are linked to it by CONSTRAINTS that glTF
    #     drops — so the map validated clean and posing her did nothing whatsoever. That is the original
    #     Phase-2 finding, reaching the validator two conventions later.
    #
    #     Needs the BIN chunk to read weights, so it is skipped without one rather than guessed at.
    if blob:
        deform = deform_joints(doc, blob)
        if deform:
            kids: dict = {}
            for i, n in enumerate(nodes):
                for c in n.get("children") or []:
                    kids.setdefault(i, []).append(c)

            def moves_anything(i: int) -> bool:
                stack, seen = [i], {i}
                while stack:
                    cur = stack.pop()
                    if cur in deform:
                        return True
                    for c in kids.get(cur, []):
                        if c not in seen:
                            seen.add(c)
                            stack.append(c)
                return False

            inert = [b for b in ("leftUpperArm", "rightUpperArm", "leftUpperLeg", "rightUpperLeg")
                     if idx(b) is not None and not moves_anything(idx(b))]
            if inert:
                problems.append(f"{', '.join(inert)} drive no geometry — rotating them would do nothing")

    # 4. Limb proportions. Upper and lower segments of a limb are within ~2.5x of each other on a human;
    #    a wild ratio means a twist/helper bone was mistaken for a joint.
    for a, b, c in (("leftUpperArm", "leftLowerArm", "leftHand"),
                    ("rightUpperArm", "rightLowerArm", "rightHand"),
                    ("leftUpperLeg", "leftLowerLeg", "leftFoot"),
                    ("rightUpperLeg", "rightLowerLeg", "rightFoot")):
        ia, ib, ic = idx(a), idx(b), idx(c)
        if None in (ia, ib, ic) or not {ia, ib, ic} <= set(pos):
            continue
        d1 = math.dist(pos[ia], pos[ib])
        d2 = math.dist(pos[ib], pos[ic])
        if d1 > 1e-4 and d2 > 1e-4 and not (0.4 <= d1 / d2 <= 2.5):
            problems.append(f"{a}->{b}->{c} segments are lopsided ({d1:.3f} vs {d2:.3f})")
    return problems


def score(inferred: dict[str, str], stated: dict[str, str]) -> dict:
    """Compare an inferred map against a known-correct one. The Saka harness.

    Returns counts plus the actual disagreements, because a bare percentage hides whether the misses are
    harmless (a twist bone chosen for the knee) or fatal (left and right swapped).
    """
    shared = [b for b in inferred if b in stated]
    hits = [b for b in shared if inferred[b] == stated[b]]
    return {
        "checked": len(shared),
        "correct": len(hits),
        "missing": sorted(b for b in stated if b in CORE_BONES and b not in inferred),
        "wrong": {b: {"inferred": inferred[b], "stated": stated[b]}
                  for b in sorted(set(shared) - set(hits))},
    }


# ---------------------------------------------------------------- the anatomical frame
#
# Semantic bone NAMES were the first half of the vocabulary; semantic AXES are the second, and without
# them the first is nearly useless. Posing took euler degrees in each bone's OWN local space, and a
# bone's rest orientation is whatever its rigger chose — measured across the three figures on disk,
# `leftUpperLeg` rests 177 degrees from identity on Grace and Yuffie and 6 degrees on Saka. So one
# number meant three different motions, and "raise her legs" put them behind her.
#
# The fix is the move that solved names: measure the structure instead of assuming a convention. A
# bone's rest DIRECTION is computable from the joint positions the map already gives us, and with the
# body's own forward and up that yields three anatomical axes per bone:
#
#     bend    flexion/extension   — the far end of the bone swings FORWARD (+) / backward (-)
#     spread  abduction/adduction — the far end swings OUTWARD, away from the midline (+)
#     turn    axial rotation      — the bone twists about its own length, + inward (medial)
#
# All three are mirror-symmetric by construction: the same numbers on `leftUpperArm` and `rightUpperArm`
# produce mirrored motion, which is what lets `{"leftUpperLeg": {"bend": 45}}` mean the same thing on
# every rig — the point of the whole exercise.

# The skeleton read as chains rather than a tree, which is what makes "the far end of this bone" a
# question with an answer. A bone's direction is the vector to the next MAPPED joint down its chain, so
# a rig missing `chest` or `leftToes` falls through to the one after it rather than losing the bone.
_CHAINS: list[tuple[str, ...]] = [("hips", "spine", "chest", "upperChest", "neck", "head")]
for _side in ("left", "right"):
    _CHAINS.append((f"{_side}UpperLeg", f"{_side}LowerLeg", f"{_side}Foot", f"{_side}Toes"))
    _CHAINS.append((f"{_side}Shoulder", f"{_side}UpperArm", f"{_side}LowerArm", f"{_side}Hand"))
    # Fingers, for the rigs that state them (VRM names all fifteen). Not inferred by this module, but a
    # stated map carries them and "make a fist" should not need a second mechanism.
    #
    # MIDDLE FIRST, and that ordering is load-bearing: the hand heads all five finger chains, so the
    # first one listed is what gives the hand its own direction. Through the middle finger is the hand's
    # anatomical axis; through the thumb — which points sideways — it is not.
    for _finger, _joints in (("Middle", ("Proximal", "Intermediate", "Distal")),
                             ("Thumb", ("Metacarpal", "Proximal", "Distal")),
                             ("Index", ("Proximal", "Intermediate", "Distal")),
                             ("Ring", ("Proximal", "Intermediate", "Distal")),
                             ("Little", ("Proximal", "Intermediate", "Distal"))):
        _CHAINS.append((f"{_side}Hand",) + tuple(f"{_side}{_finger}{j}" for j in _joints))

#: bone -> (candidates below it in its chain, candidates above it) — both nearest-first.
_ALONG: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {}
for _chain in _CHAINS:
    for _k, _bone in enumerate(_chain):
        _down, _up = _chain[_k + 1:], tuple(reversed(_chain[:_k]))
        _prev = _ALONG.get(_bone)
        # A bone can sit on several chains (a hand heads five finger chains); keep the first non-empty
        # answer in each direction so the arm's own chain wins for the hand and the fingers still resolve.
        _ALONG[_bone] = ((_prev[0] or _down, _prev[1] or _up) if _prev else (_down, _up))


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _unit(v, eps: float = 1e-9):
    n = math.sqrt(_dot(v, v))
    return None if n < eps else (v[0] / n, v[1] / n, v[2] / n)


def _scaled(v, k: float):
    return (v[0] * k, v[1] * k, v[2] * k)


def _perp(direction, reference, floor: float = 0.15):
    """A unit axis perpendicular to both, or None when the two are too nearly parallel to trust.

    `floor` is a sine: below ~8.6 degrees of separation the cross product's DIRECTION is noise, not just
    its length, so the caller falls back to a different reference rather than normalizing a rounding
    error into an axis. That case is real — a foot points forward, so its bend cannot be defined against
    the body's forward.
    """
    c = _cross(direction, reference)
    return _unit(c) if math.sqrt(_dot(c, c)) >= floor else None


def body_frame(doc: dict, mapping: dict[str, str]) -> dict[str, tuple[float, float, float]]:
    """The figure's own `up`, `left` and `forward`, measured from its bind pose.

    Not assumed. `up` is hips to head, `left` is the vector between the paired joints (hips, then
    shoulders, then hands), and forward is their cross product — which for every sample model comes out
    as glTF's +Z, the facing convention already recorded in docs/backlogs/spaces-geometry.md. Measuring
    it costs three subtractions and means a rig baked a few degrees off vertical poses correctly rather
    than approximately.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    pos = node_world_positions(doc)

    def at(bone: str):
        i = by_name.get(mapping.get(bone, ""))
        return pos.get(i) if i is not None else None

    up = None
    lo, hi = at("hips"), at("head")
    if lo and hi:
        up = _unit(_sub(hi, lo))
    up = up or (0.0, 1.0, 0.0)

    left = None
    for l, r in (("leftUpperLeg", "rightUpperLeg"), ("leftShoulder", "rightShoulder"),
                 ("leftUpperArm", "rightUpperArm"), ("leftHand", "rightHand")):
        a, b = at(l), at(r)
        if a and b:
            left = _unit(_sub(a, b))
            if left:
                break
    left = left or (1.0, 0.0, 0.0)
    # Gram-Schmidt: a hip line is never exactly horizontal, and an `up` that is not exactly vertical is
    # the whole reason for measuring. Orthogonalizing against `up` keeps the frame square.
    left = _unit(_sub(left, _scaled(up, _dot(left, up)))) or (1.0, 0.0, 0.0)
    forward = _unit(_cross(left, up)) or (0.0, 0.0, 1.0)
    return {"up": up, "left": left, "forward": forward}


def bone_directions(doc: dict, mapping: dict[str, str]) -> dict[str, tuple[float, float, float]]:
    """`{bone: unit vector along it}` in model space — from each joint toward the far end of its bone.

    The far end is the next mapped joint down the chain (`leftUpperLeg` -> `leftLowerLeg`). A bone at the
    END of a chain has no joint beyond it, so it CONTINUES the direction it arrived on: a hand points the
    way the forearm did, a head the way the neck did. Both are anatomically right and neither needs the
    node tree, which conversion rewrites.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    pos = node_world_positions(doc)

    def at(bone: str):
        i = by_name.get(mapping.get(bone, ""))
        return pos.get(i) if i is not None else None

    out: dict[str, tuple[float, float, float]] = {}
    for bone in mapping:
        here = at(bone)
        if here is None:
            continue
        down, up = _ALONG.get(bone, ((), ()))
        axis = None
        for nxt in down:
            there = at(nxt)
            if there:
                axis = _unit(_sub(there, here))
                if axis:
                    break
        if axis is None:
            for prev in up:
                there = at(prev)
                if there:
                    axis = _unit(_sub(here, there))
                    if axis:
                        break
        if axis:
            out[bone] = axis
    return out


def anatomical_axes(doc: dict, mapping: dict[str, str], space: str = "parent",
                    places: int = 5) -> dict[str, dict[str, list[float]]]:
    """`{bone: {"bend": [x,y,z], "spread": [...], "turn": [...]}}` — the axes to rotate each bone about.

    `space="parent"` (the default) returns each axis in the frame the bone's own local rotation lives in,
    i.e. its glTF PARENT's. That is the form a runtime wants, because applying it is one multiplication
    onto the bone's rest quaternion and the result rides the parent chain: bending a hip and then the
    knee does what a leg does, with no re-derivation. `space="world"` returns model space, which is what
    an offline check (scripts/pose_test.py) needs.

    Each axis is chosen so that a POSITIVE angle produces the named motion, on either side of the body:

    * `bend`   rotates the far end forward — cross(direction, forward). Degenerate for a bone that
               already points forward (a foot), which falls back to `up` so bend lifts the toes.
    * `spread` rotates the far end outward — cross(direction, outward), where outward is the body's left
               for left bones and its right for right ones. Degenerate for a bone that already points
               outward (a T-posed arm), which falls back to `up` so spread keeps raising it.
    * `turn`   is the bone's own direction, negated on the right so both sides rotate inward together.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    frame = body_frame(doc, mapping)
    up, left, forward = frame["up"], frame["left"], frame["forward"]
    directions = bone_directions(doc, mapping)
    mats = node_world_matrices(doc) if space == "parent" else {}
    parent = parent_map(doc) if space == "parent" else {}

    def to_parent(i: int, v):
        """A model-space axis in node `i`'s parent frame — R_parent transposed, columns normalized.

        Normalizing rather than inverting is deliberate: a rig can carry scale on a parent node, and a
        rotation axis must stay a unit direction through it. Shear would defeat this, and no exporter
        emits it for a skeleton.
        """
        p = parent.get(i)
        m = mats.get(p) if p is not None else None
        if not m:
            return v
        cols = [_unit((m[0], m[1], m[2])), _unit((m[4], m[5], m[6])), _unit((m[8], m[9], m[10]))]
        if not all(cols):
            return v
        return (_dot(v, cols[0]), _dot(v, cols[1]), _dot(v, cols[2]))

    out: dict[str, dict[str, list[float]]] = {}
    for bone, direction in directions.items():
        i = by_name.get(mapping.get(bone, ""))
        if i is None:
            continue
        sign = -1.0 if bone.startswith("right") else 1.0
        outward = _scaled(left, sign)
        bend = _perp(direction, forward) or _perp(direction, up)
        spread = _perp(direction, outward) or _perp(direction, up)
        turn = _scaled(direction, sign)
        if bend and bone.removeprefix("left").removeprefix("right") in _FOLDS_BACK:
            bend = _scaled(bend, -1.0)                  # positive bend = the way this joint folds
        if bend and spread:
            # Square the two swings against each other. Both are already perpendicular to the bone, but
            # not necessarily to EACH OTHER: an A-posed forearm rests slightly forward, which tilts them
            # about 8 degrees apart. That is invisible while the axes are only ever used one at a time,
            # and wrong the moment a rotation is decomposed back into them — a legal 90-degree elbow bend
            # read as 16 degrees of impossible elbow abduction, and the joint limits clamped it. A frame
            # meant to be read in both directions has to be an orthonormal basis.
            spread = _unit(_sub(spread, _scaled(bend, _dot(spread, bend)))) or spread
        axes = {"bend": bend, "spread": spread, "turn": turn}
        if not all(axes.values()):
            continue                                    # a bone we cannot frame is better left unposable
        # The three ROTATION axes above are relative — they say which way to swing from wherever this
        # bone happens to rest. The four vectors below are what an ABSOLUTE aim needs: where the bone
        # points now, and where "up", "forward" and "outward" are for this body. Both live in the same
        # payload because both are properties of the same bind pose, measured in the same pass.
        axes.update({"rest": direction, "up": up, "forward": forward, "out": outward})
        if space == "parent":
            axes = {k: to_parent(i, v) for k, v in axes.items()}
        framed = {k: [round(c, places) + 0.0 for c in v] for k, v in axes.items()}
        # What this joint can actually do, travelling WITH the frame rather than looked up beside it.
        # The runtime clamps client-side and the render pipeline clamps in Python; shipping the numbers
        # means one table rather than two that can drift apart silently.
        limits = joint_limits(bone)
        if limits:
            framed["limits"] = {k: [float(lo), float(hi)] for k, (lo, hi) in limits.items()}
        out[bone] = framed
    return out


#: Bump when the SIGNATURE's definition changes — which bones it covers, or how it is spelled. It is
#: stamped beside every signature so a changed definition is detectable rather than silently splitting
#: one rig into two. Distinct from FRAME_REV: a discovery fix can change a signature without changing
#: what a signature MEANS, and the two need to be told apart when regrouping a catalog.
RIG_SIG_REV = 3         # 3: a stated VRM map is fingerprinted, where inference was before
                        # 2: fingers joined the map, so every signature is respelled


def rig_signature(doc: dict, blob: bytes = b"") -> Optional[str]:
    """A stable fingerprint of a skeleton, over the MAPPED humanoid bones only.

    This is what makes a clip and a figure comparable without either naming the other. Two skeletons
    with the same signature spell their humanoid bones identically, so a clip authored on one binds to
    the other by node name with nothing in between — which is measured, not assumed: sixteen of twenty
    captured figures share one signature, and a clip from any of them plays on the rest.

    Over the MAPPED bones and not every node, deliberately. Jane and Akari differ by 51 nodes — skirt
    bones, breast secondaries, anatomy extras — while agreeing on all 37 core body bones, and a
    fingerprint that split them would answer a question nobody asks.

    `None` when no map can be recovered: a skeleton we cannot name is one we cannot promise anything
    about, and a signature over an unmapped rig would be a fingerprint of our own failure.
    """
    mapping, _, _ = best_humanoid(doc, blob)
    if not mapping:
        return None
    spelled = "|".join(f"{k}={mapping[k]}" for k in sorted(mapping))
    return f"{hashlib.sha1(spelled.encode()).hexdigest()[:10]}"


#: Bump when ANYTHING that changes a derived result changes: inference, the axes, `validate()`, the
#: convention table, which skin is chosen. Forgotten once already — `humanoid_follows` shipped without a
#: bump, every row was stamped current, and the fix reached no model at all while the tests stayed green.
#: A figure's map and axes are cached in the catalog, and the alternative to a version is asking "does
#: this stored frame carry the keys today's code needs" — which cannot express "the validator got
#: stricter", the change that actually mattered: two catalogued maps were rejected only after `validate`
#: learned that a limb has to be a chain.
FRAME_REV = 18          # 18: a STATED (VRM) map is layer 0 here, not the caller's job
                        # 17: the convention tables reach the FINGERS
                        # 16: rig_sig is a DERIVED attribute, so refresh backfills and clears it
                        # 15: the side rule follows the figure's facing round a YAW, not just a 180
                        # 14: extraction classifies a figure's PARTS (which mesh is clothing)
                        # 13: a side letter in the wrong case still matches
                        # 12: the side rule is read off the figure's FACING, not absolute +X
                        # 11: the `cc-base` convention, and hips rejected one level below a fork
                        # 10: the hips must be above the SPINE as well as the feet

#: The relative rotations, in the order they compose (see `resolve_pose`).
POSE_AXES = ("turn", "bend", "spread")

#: The bind-pose vectors that ride alongside them, for absolute aiming.
FRAME_VECTORS = ("rest", "up", "forward", "out")

#: Bones an `aim` is refused for. Aim points a bone along its own LENGTH, so on a head it would mean
#: aiming the top of the skull — "look up" would come out as a no-op, since the skull already points up.
#: A silent no-op on something nobody can see is the failure this feature keeps rediscovering, so the
#: trunk is refused loudly and keeps the relative rotations, which say what it means there anyway.
TRUNK_BONES = ("hips", "spine", "chest", "upperChest", "neck", "head")

#: Where each named direction points, as (out, up, forward) components of the body's own frame. `out` is
#: side-aware — the body's left for a left bone, its right for a right one — so a symmetric request stays
#: symmetric with no signs for a caller to get wrong. A free vector is read in the same three components.
AIM_DIRECTIONS = {
    "up": (0.0, 1.0, 0.0), "down": (0.0, -1.0, 0.0),
    "forward": (0.0, 0.0, 1.0), "back": (0.0, 0.0, -1.0),
    "out": (1.0, 0.0, 0.0), "in": (-1.0, 0.0, 0.0),
}


def aim_target(frame: dict, aim) -> Optional[tuple[float, float, float]]:
    """The unit direction a named (or vector) aim asks for, in the same space as `frame`."""
    comps = AIM_DIRECTIONS.get(aim) if isinstance(aim, str) else aim
    if not comps or len(comps) != 3:
        return None
    try:
        o, u, f = (float(c) for c in comps)
    except (TypeError, ValueError):
        return None
    basis = [frame.get(k) for k in ("out", "up", "forward")]
    if not all(b and len(b) == 3 for b in basis):
        return None                       # a frame measured before aiming existed; caller reports it
    return _unit(tuple(o * basis[0][k] + u * basis[1][k] + f * basis[2][k] for k in range(3)))


def swing(rest, target, fallback) -> list[float]:
    """The rotation taking `rest` onto `target` — the shortest arc, except when there isn't one.

    Antiparallel is the case that matters and it is not an edge case here: a hanging arm aimed `up` is
    a half-turn, and a half-turn has no unique axis. Left to a generic "shortest arc" routine it picks
    an arbitrary perpendicular, which for an arm means swinging it through the torso as often as not.
    So the caller names the axis to fall back on — the body's forward, giving a rotation in the FRONTAL
    plane: the arm goes up through the side, the way a person raises one.
    """
    d = max(-1.0, min(1.0, _dot(rest, target)))
    if d > 1.0 - 1e-9:
        return [0.0, 0.0, 0.0, 1.0]
    if d < -1.0 + 1e-9:
        # Perpendicular component of the fallback, since it need not be square to the bone.
        axis = _unit(_sub(fallback, _scaled(rest, _dot(fallback, rest))))
        if axis is None:                        # fallback is parallel to the bone: any perpendicular
            other = (1.0, 0.0, 0.0) if abs(rest[0]) < 0.9 else (0.0, 1.0, 0.0)
            axis = _unit(_cross(rest, other)) or (0.0, 1.0, 0.0)
        return [axis[0], axis[1], axis[2], 0.0]
    c = _cross(rest, target)
    q = [c[0], c[1], c[2], 1.0 + d]
    n = math.sqrt(sum(v * v for v in q))
    return [v / n for v in q]


# ---------------------------------------------------------------- joint limits
#
# The vocabulary can express poses a body cannot make, and until now it executed them faithfully:
# measured on device, "raise her left arm" folded the elbow 180 degrees behind her head, and "bend her
# right leg backward" put 90 degrees of extension through a hip that manages about 20. Both were the
# director asking for something impossible in perfectly good syntax.
#
# Limits are per SEMANTIC bone, which is the whole point of having a semantic vocabulary: one table is
# correct for Saka, Grace, Yuffie and everything after them, the same way one `bend` is. They are
# **generous on purpose** — they exist to exclude the grotesque, not to enforce realism on what is, after
# all, a puppet. Where a real joint manages 20 degrees, these allow 35.
#
# Rest-relative, like the rotations they bound. That is exact for the hinges and the trunk, whose rest
# pose IS the anatomical neutral on every rig measured (a knee is straight, a shin hangs down). It is
# loosest at the SHOULDER, where rest varies from horizontal on Saka to 48 degrees below on Grace — so
# the shoulder's limits are wide enough to be right from either, which costs little because a shoulder
# genuinely does reach nearly everywhere.
_LIMITS = {
    "Shoulder":  {"bend": (-20, 20), "spread": (-20, 35), "turn": (-20, 20)},   # the clavicle
    # The shoulder is barely limited, and deliberately so. Rest-relative bounds only work where rest IS
    # the anatomical neutral; at the shoulder it is not, and it differs by 48 degrees between the rigs
    # here — so any tight bound would be wrong on one of them. A T-posed arm brought down to the side is
    # -90 of spread and completely ordinary; on Grace the same destination is -44. Only the TWIST has a
    # neutral that every rig agrees on, so only the twist is really constrained.
    "UpperArm":  {"bend": (-140, 190), "spread": (-100, 190), "turn": (-95, 95)},
    "LowerArm":  {"bend": (-5, 155), "spread": (-8, 8), "turn": (-95, 95)},     # elbow: one way only
    "Hand":      {"bend": (-75, 85), "spread": (-25, 35), "turn": (-35, 35)},
    "UpperLeg":  {"bend": (-35, 130), "spread": (-30, 75), "turn": (-50, 50)},
    "LowerLeg":  {"bend": (-5, 155), "spread": (-5, 5), "turn": (-15, 15)},     # knee: see _FOLDS_BACK
    "Foot":      {"bend": (-55, 30), "spread": (-18, 18), "turn": (-25, 25)},
    "Toes":      {"bend": (-35, 65), "spread": (-10, 10), "turn": (-10, 10)},
    "hips":      {"bend": (-45, 45), "spread": (-45, 45), "turn": (-45, 45)},
    "spine":     {"bend": (-25, 50), "spread": (-30, 30), "turn": (-40, 40)},
    "chest":     {"bend": (-25, 50), "spread": (-30, 30), "turn": (-40, 40)},
    "upperChest": {"bend": (-20, 40), "spread": (-25, 25), "turn": (-35, 35)},
    "neck":      {"bend": (-45, 45), "spread": (-40, 40), "turn": (-65, 65)},
    "head":      {"bend": (-35, 35), "spread": (-30, 30), "turn": (-50, 50)},
}
_FINGER = {"bend": (-15, 95), "spread": (-18, 18), "turn": (-12, 12)}


#: Joints whose flexion carries the far end BACKWARD, so `bend` is negated for them when the frame is
#: measured. There is exactly one on a human: the knee.
#:
#: `bend` is otherwise "the far end swings forward", which is a geometric rule and reads correctly at the
#: hip, the elbow, the spine and the neck — everywhere the two happen to coincide. At the knee they do
#: not, and the geometric rule made "bend her knee" mean hyperextension: measured, the director asked
#: with the obvious positive number, was clamped to 5 degrees, and reasoned itself into a corner about
#: why a knee could not bend. Flipping the axis makes `bend` mean FLEXION — the way the joint actually
#: folds — on every joint that has one, which is both the anatomical definition and the plain English
#: reading of the word. Same principle as `spread` being side-aware: the mirroring belongs in the frame,
#: not in the caller's head.
_FOLDS_BACK = ("LowerLeg",)


def joint_limits(bone: str) -> dict[str, tuple[float, float]]:
    """The degree range each rotation of `bone` may take, sides folded together.

    Left and right share a row because the axes are already mirrored: `spread` is outward on both sides
    and `turn` inward on both, so one range describes the joint rather than the side.
    """
    if bone in _LIMITS:
        return _LIMITS[bone]
    bare = bone.removeprefix("left").removeprefix("right")
    if bare in _LIMITS:
        return _LIMITS[bare]
    return dict(_FINGER) if any(f in bare for f in
                                ("Thumb", "Index", "Middle", "Ring", "Little")) else {}


def _quat_conj(q):
    return [-q[0], -q[1], -q[2], q[3]]


def _axis_angle(axis, radians: float) -> list[float]:
    h = radians / 2.0
    s = math.sin(h)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(h)]


def clamp_angle(bone: str, name: str, degrees: float, frame: dict,
                notes: Optional[list] = None) -> float:
    """One requested rotation, reduced to what the joint allows.

    Numbers are clamped as NUMBERS wherever the caller gave one, rather than by decomposing the
    resulting rotation: 200 degrees of knee bend and -160 are the same quaternion, so a decomposition
    reads the request back as "160 the wrong way" and clamps it to nearly straight — the opposite of
    what was asked. Only an `aim`, which arrives as a direction rather than an angle, has to be
    recovered from its rotation (`clamp_to_joint`).
    """
    limits = frame.get("limits") or joint_limits(bone)
    lo, hi = (limits.get(name) or (-360.0, 360.0))
    capped = max(lo, min(hi, degrees))
    if abs(capped - degrees) > 0.5 and notes is not None:
        # Name the direction when a joint is asymmetric: hitting the 5-degree end of a knee means the
        # request was the wrong way round, and "→ +5°" alone reads as "nearly at its limit", which is
        # how a caller talks itself into believing a knee cannot bend.
        other = " (it folds the other way)" if abs(capped) < abs(hi if capped < 0 else lo) / 4 else ""
        notes.append(f"{bone}.{name} {degrees:+.0f}° → {capped:+.0f}°{other}")
    return capped


def clamp_to_joint(bone: str, q: list[float], frame: dict, notes: Optional[list] = None) -> list[float]:
    """`q` reduced to what this joint can actually do, in the bone's own anatomical frame.

    Works on the RESULT rather than the request, so it does not matter whether the caller said
    `{"bend": 200}` or `{"aim": "up"}` — an impossible destination and an impossible angle are the same
    thing once resolved, and there is one place to get it right.

    The rotation is split swing-from-twist about the bone's own length, the swing's axis is resolved into
    its bend and spread components, each is clamped, and the three are rebuilt. When nothing is out of
    range the ORIGINAL quaternion is returned untouched — two perpendicular swings compose into a small
    amount of twist, and a round trip through this decomposition would quietly rewrite a legal pose.
    """
    limits = frame.get("limits") or joint_limits(bone)
    b, sp, t = frame.get("bend"), frame.get("spread"), frame.get("turn")
    if not limits or not (b and sp and t):
        return q
    twist_dot = _dot(q[:3], t)
    twist = [t[0] * twist_dot, t[1] * twist_dot, t[2] * twist_dot, q[3]]
    n = math.sqrt(sum(v * v for v in twist))
    twist = [v / n for v in twist] if n > 1e-9 else [0.0, 0.0, 0.0, 1.0]
    if twist[3] < 0:                                   # q and -q are the same rotation; +w is the
        twist = [-v for v in twist]                    # short way round, and 2*atan2 needs it
    swing = _quat_mul(q, _quat_conj(twist))
    if swing[3] < 0:                                   # keep the swing on the short way round
        swing = [-v for v in swing]
    turn_deg = math.degrees(2.0 * math.atan2(_dot(twist[:3], t), twist[3]))
    axis = _unit(swing[:3])
    angle = math.degrees(2.0 * math.atan2(math.sqrt(_dot(swing[:3], swing[:3])),
                                          max(-1.0, min(1.0, swing[3])))) if axis else 0.0
    bend_deg = angle * _dot(axis, b) if axis else 0.0
    spread_deg = angle * _dot(axis, sp) if axis else 0.0

    hit: list = []
    bend_deg, spread_deg, turn_deg = (clamp_angle(bone, "bend", bend_deg, frame, hit),
                                      clamp_angle(bone, "spread", spread_deg, frame, hit),
                                      clamp_angle(bone, "turn", turn_deg, frame, hit))
    if not hit:
        return q
    if notes is not None:
        notes.extend(hit)
    swing_angle = math.hypot(bend_deg, spread_deg)
    if swing_angle > 1e-6:
        mix = _unit((b[0] * bend_deg + sp[0] * spread_deg, b[1] * bend_deg + sp[1] * spread_deg,
                     b[2] * bend_deg + sp[2] * spread_deg))
        swing = _axis_angle(mix, math.radians(swing_angle)) if mix else [0.0, 0.0, 0.0, 1.0]
    else:
        swing = [0.0, 0.0, 0.0, 1.0]
    return _quat_mul(swing, _axis_angle(t, math.radians(turn_deg)))


def resolve_pose(axes: dict, pose: dict, notes: Optional[list] = None) -> dict[str, list[float]]:
    """`{bone: [x, y, z, w]}` — one delta quaternion per posed bone, in whatever space `axes` are in.

    `pose` is `{bone: {"bend": degrees, ...}}`. Missing axes are zero, so a one-axis request stays a
    one-axis rotation.

    A request may instead carry `aim` — a named body direction or a vector — which is ABSOLUTE: it
    rotates the bone from wherever it rests onto that direction, so the same request means the same thing
    on a T-posed rig and an A-posed one. It REPLACES bend and spread, which set the same swing relatively;
    `turn` still composes, because a twist about the bone's own length is orthogonal to where it points.

    Composition order is turn, then the swing — twist innermost, swings outermost. That is the swing-twist
    decomposition every animation system uses, and it is what makes a twist mean the same thing regardless
    of how the limb is currently swung. The reverse order would make "turn 20" describe a different motion
    depending on the bend that happened to accompany it.
    """
    out: dict[str, list[float]] = {}
    for bone, request in (pose or {}).items():
        frame = axes.get(bone)
        if not frame or not isinstance(request, dict):
            continue
        q = [0.0, 0.0, 0.0, 1.0]
        for name in POSE_AXES:
            if name != "turn" and request.get("aim") is not None:
                continue                                  # an aim sets the swing; bend/spread do not
            deg = request.get(name)
            if deg in (None, 0) or not isinstance(deg, (int, float)):
                continue
            axis = frame.get(name)
            if not axis:
                continue
            deg = clamp_angle(bone, name, float(deg), frame, notes)
            if not deg:
                continue
            half = math.radians(deg) / 2.0
            s = math.sin(half)
            q = _quat_mul([axis[0] * s, axis[1] * s, axis[2] * s, math.cos(half)], q)
        if request.get("aim") is not None:
            target = aim_target(frame, request["aim"])
            rest = frame.get("rest")
            if target and rest:
                # An aim names a destination, not an angle, so what it asks of the joint is only
                # legible once resolved — this is the one path that needs the decomposition.
                reach = swing(rest, target, frame.get("forward") or (0.0, 0.0, 1.0))
                q = _quat_mul(clamp_to_joint(bone, reach, frame, notes), q)
        out[bone] = q
    return out


def _quat_mul(a: list[float], b: list[float]) -> list[float]:
    """a then b, in the a*b convention three.js and glTF use (b applied first)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


# ---------------------------------------------------------------- what the caller is told, and applied
#
# The two functions below exist because two callers need them and a second copy of either would rot.
# `figure_description` is the wording the director reads before posing, and the eval harness
# (`conjure.pose_corpus`) has to read the SAME wording or it is measuring a paraphrase. `apply_pose`
# is the arithmetic the client does per frame, needed offline by both the renderer
# (`scripts/pose_test.py`) and the harness's geometry pass.


def figure_description(*, label: str, height_m: Optional[float] = None, tris=None,
                       bones=(), has_map: bool = False, posed=(), removable=None,
                       hidden=()) -> str:
    """What `inspect_figure` says about a figure — the bones it has and how they can be asked to move.

    Kept here rather than in the MCP tool because this text is part of the tool SURFACE under test: the
    harness answers a director's `inspect_figure` call from a file rather than a live world, and a
    harness that invented its own phrasing would pass while the real thing failed.
    """
    bones = sorted(bones)
    height = f"{height_m:.2f} m tall" if height_m else "unknown height"
    lines = [f"{label} — {height}, {tris or '?'} triangles."]
    if bones:
        limbs = [b for b in bones if b not in TRUNK_BONES]
        lines.append(f"Posable bones ({len(bones)}): {', '.join(bones)}")
        if limbs:
            lines.append("Arms and legs take aim (up, down, forward, back, out, in) — where the limb "
                         "should point, which is what you want for \"raise her arm\".")
    elif has_map:
        # A map but no measured frame: the figure was placed before poses had one. Say which it is —
        # "cannot be posed" would send the caller looking for a missing skeleton.
        lines.append("Bones are named but this figure has no anatomical frame — place it again to "
                     "measure one, then it can be posed.")
    else:
        lines.append("No humanoid bone map, so it cannot be posed.")
    # WHAT COMES OFF. Absent from this text until 2026-09-13, and the omission read as an answer: asked
    # what a figure was wearing, the director called this tool, got bones, and told the user she was a
    # unified mesh with no detachable parts — about a figure carrying seven classified ones. A tool that
    # describes a figure has to describe the whole figure, or its silence gets quoted as fact.
    if removable:
        summary = ", ".join(f"{c} ({len(n)})" for c, n in sorted(removable.items()))
        lines.append(f"Removable: {summary}. Take them off by CATEGORY, or name one mesh.")
        if hidden:
            lines.append(f"Currently hidden ({len(hidden)}): {', '.join(sorted(hidden))}")
    elif removable is not None:
        lines.append("Nothing on this figure is removable — its meshes are all body or face, or it was "
                     "placed before parts were classified. Clothing that arrived as a SEPARATE model is "
                     "its own entity: remove that instead.")
    if bones:
        lines.append("Every bone also takes bend (forward +/back -), spread (out from the body +) and "
                     "turn (inward +), in degrees, as a rotation from where it rests. out/in and spread "
                     "are already mirrored: the same sign on both sides gives a symmetric pose.")
    if posed:
        lines.append(f"Currently posed: {', '.join(sorted(posed))}")
    return "\n".join(lines)


def _basis(m) -> Optional[list]:
    """A world matrix's three axis directions, as unit vectors in world space.

    Normalized rather than inverted for the same reason `anatomical_axes.to_parent` does it: a rig can
    carry scale on a parent node, and a direction has to stay a direction through it.
    """
    cols = [_unit((m[0], m[1], m[2])), _unit((m[4], m[5], m[6])), _unit((m[8], m[9], m[10]))]
    return cols if all(cols) else None


def compose_frame(frame: dict, rest, now) -> dict:
    """`frame` with its BODY DIRECTIONS re-expressed for a parent that has since rotated.

    A frame is measured once, at bind time, and every vector in it is written in the coordinates of the
    bone's parent. Three of them — `up`, `forward`, `out` — describe where the BODY faces, and those are
    the ones that go stale: rotate the chest and the arm's parent frame turns with it, so the numbers
    that meant "world up" now mean something else entirely. An arm asked to aim `down` under a folded
    trunk came out pointing UP on Saka, which is how this was found.

    So: read each of those three through the parent's REST basis to recover the world direction it was
    always meant to name, then write it back through the parent's CURRENT basis.

    `rest` is deliberately NOT corrected, and neither are `bend`, `spread` and `turn`. A bone's rest
    direction is a property of its own local bind rotation and does not change when its parent moves —
    the bone rides along. And the three swing axes are relative BY DESIGN: "bend her elbow 90 degrees"
    should mean the same thing whatever her trunk is doing, and it does.
    """
    rest_cols, now_cols = _basis(rest), _basis(now)
    if not rest_cols or not now_cols:
        return frame
    out = dict(frame)
    for key in ("up", "forward", "out"):
        v = frame.get(key)
        if not v:
            continue
        world = [sum(v[k] * rest_cols[k][c] for k in range(3)) for c in range(3)]
        out[key] = [_dot(world, now_cols[c]) for c in range(3)]
    return out


def apply_pose(doc: dict, mapping: dict[str, str], pose: dict,
               notes: Optional[list] = None) -> dict[str, int]:
    """Write `pose` into `doc`'s node rotations IN PLACE. Returns `{bone: node index}` for what moved.

    A pose is a delta on a node's local rotation, which is exactly what the client applies — so a doc
    mutated here is the posed figure, and `node_world_positions` on it gives the joint positions the
    headset would show. Bones the file does not have are skipped; the caller reports them.

    `doc` must arrive at its BIND pose, because the frame is measured from it.

    **Aims are resolved LAST and root-first**, against the parent frame as posed rather than as bound —
    see `compose_frame`. Relative requests are order-free (each writes one node's local rotation and
    reads nothing), so they go first in a single pass; an aim has to see what its ancestors did, so the
    world matrices are recomputed as the trunk lands. A frame is only composed when an ancestor of that
    bone actually moved, which keeps every pose that leaves the trunk alone bit-identical to before.
    """
    by_name = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
    axes = anatomical_axes(doc, mapping)                  # PARENT space: where a node's rotation lives
    parent = parent_map(doc)
    rest_mats = node_world_matrices(doc)
    moved: dict[str, int] = {}

    def write(bone: str, request: dict, frame: dict) -> None:
        i = by_name.get(mapping.get(bone, ""))
        if i is None:
            return
        delta = resolve_pose({bone: frame}, {bone: request}, notes).get(bone)
        if delta is None:
            return
        node = doc["nodes"][i]
        node["rotation"] = _quat_mul(delta, node.get("rotation", [0.0, 0.0, 0.0, 1.0]))
        moved[bone] = i

    aiming = []
    for bone, request in (pose or {}).items():
        frame = axes.get(bone)
        if not frame or not isinstance(request, dict):
            continue
        if request.get("aim") is not None:
            aiming.append((bone, request, frame))
        else:
            write(bone, request, frame)

    depth = {i: len(_ancestors(i, parent)) for i in range(len(doc.get("nodes") or []))}
    aiming.sort(key=lambda t: depth.get(by_name.get(mapping.get(t[0], ""), -1), 0))
    for bone, request, frame in aiming:
        i = by_name.get(mapping.get(bone, ""))
        p = parent.get(i) if i is not None else None
        if p is not None and any(j in set(moved.values()) for j in _ancestors(i, parent)):
            frame = compose_frame(frame, rest_mats.get(p), node_world_matrices(doc).get(p))
        write(bone, request, frame)
    return moved


# ---------------------------------------------------------------- checking a pose REQUEST
#
# Between the director's words and the arithmetic sits a layer that says no. It lives here rather than
# in the endpoint because a refusal is part of the tool SURFACE — the director reads it and tries again
# — so the eval harness has to produce the same one the server would, from a file and with no world.


def aim_problem(bone: str, aim, frame: dict, rot: dict) -> Optional[str]:
    """What is wrong with an `aim` request, or None. Every branch refuses LOUDLY rather than no-op.

    That is not politeness. A pose that silently does nothing is indistinguishable from a pose the user
    simply cannot see from where they are standing, and this feature has now shipped that failure twice
    (grab's unreachable modes; three fixes the headset never ran)."""
    if bone in TRUNK_BONES:
        return (f"{bone}: aim points a bone along its own LENGTH, so on the trunk it would mean aiming "
                f"the top of the skull — use bend, spread or turn there")
    for other in ("bend", "spread"):
        if other in rot:
            return f"{bone}: aim and {other} both set the swing — use one or the other"
    if isinstance(aim, str):
        if aim not in AIM_DIRECTIONS:
            return (f"{bone}: unknown direction {aim!r} — use {', '.join(AIM_DIRECTIONS)}, "
                    "or a vector [out, up, forward]")
    else:
        if not isinstance(aim, (list, tuple)) or len(aim) != 3:
            return (f"{bone}: aim takes a direction ({', '.join(AIM_DIRECTIONS)}) or a vector "
                    "[out, up, forward]")
        try:
            vals = [float(c) for c in aim]
        except (TypeError, ValueError):
            return f"{bone}: aim vector must be three numbers"
        if not all(math.isfinite(v) for v in vals) or not any(vals):
            return f"{bone}: aim vector must be finite and not all zero"
    if not all(k in frame for k in FRAME_VECTORS):
        # The bone map is fine; the FRAME was measured by an older build. Placing again re-measures it.
        return f"{bone}: this figure's frame predates aiming — place it again to measure one"
    return None


def clean_pose(pose: dict, axes: dict) -> tuple[dict, Optional[str]]:
    """`(clean, None)` for a usable pose request, or `({}, why not)`. Refuses on the FIRST problem.

    `clean` is the same request with angles coerced to floats and nothing else changed — an empty `{}`
    for a bone is legal and means "return that bone to rest".
    """
    # A bone with a name but no frame is not posable: two of Saka's 54 have no measurable direction.
    # Saying so is the point — a silent no-op on something nobody can see is the failure mode this
    # feature keeps rediscovering (docs/backlogs/figures.md, grab's mode fiasco).
    unknown = [b for b in pose if b not in axes]
    if unknown:
        return {}, (f"unknown bone(s) {', '.join(sorted(unknown))}; "
                    f"this figure has: {', '.join(sorted(axes))}")
    clean: dict = {}
    for bone, rot in pose.items():
        if not isinstance(rot, dict):
            return {}, (f"{bone}: expected {{{', '.join(sorted(POSE_AXES))}}} in degrees "
                        "or {\"aim\": \"up\"}")
        bad = [k for k in rot if k not in POSE_AXES and k != "aim"]
        if bad:
            return {}, (f"{bone}: unknown rotation(s) {', '.join(sorted(bad))} — "
                        f"use {', '.join(sorted(POSE_AXES))} or aim")
        vals: dict = {}
        if rot.get("aim") is not None:
            problem = aim_problem(bone, rot["aim"], axes.get(bone) or {}, rot)
            if problem:
                return {}, problem
            vals["aim"] = rot["aim"] if isinstance(rot["aim"], str) else [float(c) for c in rot["aim"]]
        for k, v in rot.items():
            if k == "aim":
                continue
            try:
                angle = float(v)
            except (TypeError, ValueError):
                return {}, f"{bone}.{k}: expected degrees, got {v!r}"
            if not math.isfinite(angle):
                # Same hazard as /world_frame: a non-finite angle blanks that branch of the scene graph
                # and stays blanked, and a persisted one comes back on every reload.
                return {}, f"{bone}.{k}: angles must be finite"
            vals[k] = angle
        clean[bone] = vals                      # an empty {} is legal: it returns that bone to rest
    return clean, None


# ---------------------------------------------------------------- consulting the MESH
#
# Everything above reasons about joints. A pose authored and checked that way can be geometrically
# perfect and still look wrong, because flesh intersects flesh: measured on device 2026-09-09, arms
# aimed `down` enter the body, `arms-crossed` folds inside the chest, `hands-on-hips` does not touch.
# No signature catches any of it — a signature asserts where a joint IS, never what is already there.
#
# So: read the skinned vertices and ask how wide the body is. Cheap, because the question is narrow —
# not "do these two meshes intersect" but "how far from the body's axis is its surface, at this height".


def _read_vec3(doc: dict, blob: bytes, accessor_index: int, limit: int = 400000):
    """Yield up to `limit` VEC3 float triples from an accessor — the vertex-position counterpart to
    `_read_vec4`. Skinned positions are stored in BIND space, which is exactly the frame the bone map
    and `anatomical_axes` are measured in, so no transform is needed to compare them."""
    import struct
    acc = (doc.get("accessors") or [])[accessor_index]
    fmt, size = _COMPONENT.get(acc.get("componentType"), (None, 0))
    if not fmt or acc.get("type") != "VEC3" or "bufferView" not in acc:
        return
    bv = (doc.get("bufferViews") or [])[acc["bufferView"]]
    base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride") or (size * 3)
    for i in range(min(acc.get("count", 0), limit)):
        off = base + i * stride
        if off + size * 3 > len(blob):
            return
        yield struct.unpack_from("<" + fmt * 3, blob, off)


#: The bones whose vertices are the TORSO — what a hanging arm has to clear. Deliberately not the whole
#: body: including arm vertices would inflate the profile with the very limb being tested, and a T-posed
#: rig would report a body two metres wide.
TORSO_BONES = ("hips", "spine", "chest", "upperChest", "neck")


def deform_subtree(doc: dict, mapping: dict[str, str], bones) -> set[int]:
    """The node indices whose vertices belong to `bones` — the mapped nodes plus their descendants.

    **A mapped bone is not necessarily a DEFORM bone.** Trish's `spine` is a control whose only child is
    `spine.twk`, and every torso vertex is weighted to the twk, so matching the mapped node alone found
    zero torso on her rig — and zero arm, for the same reason. The subtree fixes both.

    It stops at any node belonging to a DIFFERENT mapped bone, or the arms (which descend from the chest)
    would count as torso and report a T-posed figure two metres wide.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    stop = {by_name.get(n) for b, n in mapping.items() if b not in bones} - {None}
    out: set[int] = set()
    for bone in bones:
        root = by_name.get(mapping.get(bone, ""))
        if root is None:
            continue
        stack = [root]
        while stack:
            i = stack.pop()
            if i in out:
                continue
            out.add(i)
            for c in nodes[i].get("children") or []:
                if c not in stop:
                    stack.append(c)
    return out


def body_profile(doc: dict, blob: bytes, mapping: dict[str, str], bands: int = 24,
                 bones=TORSO_BONES) -> list[tuple[float, float, float]]:
    """`[(height, half_width, depth)]` per height band through the torso, in the model's own units.

    Each vertex is assigned to the bone it is most heavily weighted to, and only vertices belonging to
    `bones` are counted — skin weights are what separate torso from limb, and they are exact where a
    name convention or a bounding box would be guesswork. Within a band, `half_width` is the larger of
    the two sides' extents from the body's midline and `depth` the front-to-back extent.

    Empty when the file has no skin or no weights, which is the honest answer for an unrigged mesh; the
    caller then has nothing to clear and should not invent a number.
    """
    skins = doc.get("skins") or []
    if not skins:
        return []
    wanted = deform_subtree(doc, mapping, bones)
    if not wanted:
        return []
    frame = body_frame(doc, mapping)
    up, left, forward = frame["up"], frame["left"], frame["forward"]
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))              # noqa: E731

    mesh_skin: dict[int, int] = {}
    for n in doc.get("nodes") or []:
        if "mesh" in n and "skin" in n:
            mesh_skin.setdefault(n["mesh"], n["skin"])

    pts: list[tuple[float, float, float]] = []                       # (height, lateral, depth)
    for mi, mesh in enumerate(doc.get("meshes") or []):
        si = mesh_skin.get(mi)
        if si is None or si >= len(skins):
            continue
        joints = skins[si].get("joints") or []
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            if not {"POSITION", "JOINTS_0", "WEIGHTS_0"} <= set(attrs):
                continue
            positions = _read_vec3(doc, blob, attrs["POSITION"])
            js = _read_vec4(doc, blob, attrs["JOINTS_0"])
            ws = _read_vec4(doc, blob, attrs["WEIGHTS_0"])
            for pos, j4, w4 in zip(positions, js, ws):
                k = max(range(4), key=lambda n: w4[n])               # the dominant bone
                if w4[k] <= 0:
                    continue
                ji = int(j4[k])
                if ji >= len(joints) or joints[ji] not in wanted:
                    continue
                pts.append((dot(pos, up), dot(pos, left), dot(pos, forward)))
    if not pts:
        return []

    lo = min(p[0] for p in pts)
    hi = max(p[0] for p in pts)
    span = (hi - lo) or 1.0
    buckets: dict[int, list] = {}
    for h, lat, dep in pts:
        buckets.setdefault(min(bands - 1, int((h - lo) / span * bands)), []).append((h, lat, dep))
    out = []
    for b in sorted(buckets):
        rows = buckets[b]
        out.append((sum(r[0] for r in rows) / len(rows),
                    max(abs(r[1]) for r in rows),
                    max(r[2] for r in rows) - min(r[2] for r in rows)))
    return out


def limb_radius(doc: dict, blob: bytes, mapping: dict[str, str], bone: str) -> float:
    """How thick a limb is — the median perpendicular distance from its own axis to its surface.

    **The term that was missing.** A shoulder sits almost exactly at the torso's edge, so an arm hanging
    straight down from it looks clear on joint positions alone and still overlaps, by its own radius.
    Median rather than max: a max picks up the shoulder cap where the arm meets the torso, which is the
    one place the limb is legitimately as wide as the body.
    """
    skins = doc.get("skins") or []
    by_name = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
    node = by_name.get(mapping.get(bone, ""))
    if not skins or node is None:
        return 0.0
    own = deform_subtree(doc, mapping, (bone,))
    directions = bone_directions(doc, mapping)
    axis = directions.get(bone)
    origin = node_world_positions(doc).get(node)
    if not axis or not origin:
        return 0.0

    mesh_skin: dict[int, int] = {}
    for n in doc.get("nodes") or []:
        if "mesh" in n and "skin" in n:
            mesh_skin.setdefault(n["mesh"], n["skin"])
    radii: list[float] = []
    for mi, mesh in enumerate(doc.get("meshes") or []):
        si = mesh_skin.get(mi)
        if si is None or si >= len(skins):
            continue
        joints = skins[si].get("joints") or []
        for prim in mesh.get("primitives") or []:
            attrs = prim.get("attributes") or {}
            if not {"POSITION", "JOINTS_0", "WEIGHTS_0"} <= set(attrs):
                continue
            for pos, j4, w4 in zip(_read_vec3(doc, blob, attrs["POSITION"]),
                                   _read_vec4(doc, blob, attrs["JOINTS_0"]),
                                   _read_vec4(doc, blob, attrs["WEIGHTS_0"])):
                k = max(range(4), key=lambda n: w4[n])
                if w4[k] <= 0:
                    continue
                ji = int(j4[k])
                if ji >= len(joints) or joints[ji] not in own:
                    continue
                v = _sub(pos, origin)
                along = _dot(v, axis)
                perp = _sub(v, _scaled(axis, along))
                radii.append(math.sqrt(_dot(perp, perp)))
    if not radii:
        return 0.0
    radii.sort()
    return radii[len(radii) // 2]


def torso_half_width(profile, height: float) -> float:
    """The torso's half-width at `height`, from the nearest band. 0.0 for an empty profile — a caller
    with no measurement should do nothing rather than apply a default."""
    if not profile:
        return 0.0
    return min(profile, key=lambda row: abs(row[0] - height))[1]
