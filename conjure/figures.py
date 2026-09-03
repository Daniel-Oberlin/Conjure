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
    for node in doc.get("nodes") or []:
        name = node.get("name")
        if name:
            lookup.setdefault(_bare(name), name)      # first spelling wins; ties are vanishingly rare
    best: tuple[int, Optional[dict], Optional[str]] = (0, None, None)
    for scheme, table in CONVENTIONS.items():
        found: dict[str, str] = {}
        for slot, candidates in table.items():
            sides = (("left", "Left", "L"), ("right", "Right", "R")) if "{s}" in slot else ((None,) * 3,)
            for side, S, X in sides:
                key = slot.format(s=side) if side else slot
                for candidate in candidates.split("|"):
                    node = candidate.format(S=S, X=X) if side else candidate
                    if node in lookup:
                        found[key] = lookup[node]
                        break
        score = sum(1 for b in REQUIRED_BONES if b in found)
        if score > best[0]:
            best = (score, found, scheme)
    return (best[1], best[2]) if best[0] == len(REQUIRED_BONES) else (None, None)


# ---------------------------------------------------------------- inference


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


def infer_humanoid(doc: dict, blob: bytes = b"") -> Optional[dict[str, str]]:
    """`{semanticBone: nodeName}` inferred from skeleton shape, or None if it does not look humanoid.

    The identification order matters: extremities first, because they are unambiguous geometric extremes,
    then the joints between them by path. Feet are the lowest joints, hands the widest, head the highest;
    hips is then simply where the two leg paths meet.
    """
    nodes = doc.get("nodes") or []
    skins = doc.get("skins") or []
    if not nodes or not skins:
        return None
    # The humanoid skin is the one with the most joints — hair and cloth rigs are separate and smaller
    # (Hitomi ships five skins, four of them hair).
    joints = max((s.get("joints") or [] for s in skins), key=len)
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


def follow_bones(doc: dict, mapping: dict[str, str], blob: bytes = b"") -> dict[str, str]:
    """`{node: node it should ride}` — bones that deform the mesh but hang outside their own limb.

    An IK rig parents the FOOT to the armature root, beside a pole target, and lets an animation drive
    both. It is a perfectly good rig for playing clips and a broken one for posing: rotate the shin and
    the foot stays where it was, so the mesh stretches from a planted foot up to a raised ankle. That is
    what "her feet remain glued to the floor" looked like on two of three asset-pack characters.

    `prune_map` already refuses to CALL such a bone an ankle, because rotating it poses nothing. This
    says what to do about the tearing: the bone rides the last joint above it that is in the chain, at
    the offset it holds in the bind pose — a parent constraint, evaluated wherever the pose is applied.
    The file's own hierarchy is left alone, so its baked clips still mean what they meant.
    """
    nodes = doc.get("nodes") or []
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
    parent = parent_map(doc)
    deform = deform_joints(doc, blob) if blob else None
    out: dict[str, str] = {}
    for chain in LIMB_CHAINS:
        present = [b for b in chain if mapping.get(b) in by_name]
        anchor = None
        for bone in present:
            i = by_name[mapping[bone]]
            if anchor is None:
                anchor = bone
                continue
            ai = by_name[mapping[anchor]]
            if ai in _ancestors(i, parent):
                anchor = bone                              # a real link: nothing to do
            elif deform is None or i in deform:
                # Detached AND it moves geometry, so leaving it behind is visible.
                out[mapping[bone]] = mapping[anchor]
                anchor = bone          # what hangs below it rides along; toes need no entry of their own
    return out


def best_humanoid(doc: dict, blob: bytes = b"") -> tuple[Optional[dict], Optional[str], dict]:
    """`(map, source, follows)` — the discovery pipeline's cheap layers, in order, gated by `validate()`.

    Layer 1 (names) is free and exact where it hits, and works on a bind pose that defeats geometry.
    Layer 2 (shape) works on names that mean nothing. They fail on opposite inputs, which is the whole
    argument for having both. A stated map (VRM) is read before either — that is the caller's job, since
    it needs no doc-level guessing at all.

    `follows` is computed from the map BEFORE pruning, because it is precisely about the bones pruning
    removes: what the map cannot pose, the mesh still has to hang off something.
    """
    for candidate, source in ((convention_humanoid(doc), None), (None, "inferred")):
        if candidate is not None:
            raw, scheme = candidate
            source = f"convention:{scheme}" if raw else None
        else:
            raw = infer_humanoid(doc, blob)               # reads every vertex weight: not speculative
        if not raw:
            continue
        pruned = prune_map(doc, raw)
        if not validate(doc, pruned):
            return pruned, source, follow_bones(doc, raw, blob)
    return None, None, {}


def validate(doc: dict, mapping: dict[str, str]) -> list[str]:
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

    # 1. Sides. +X is the model's left in every sample; a swap here inverts every later pose.
    for l, r in (("leftHand", "rightHand"), ("leftFoot", "rightFoot"),
                 ("leftUpperArm", "rightUpperArm"), ("leftUpperLeg", "rightUpperLeg")):
        xl, xr = x(l), x(r)
        if xl is not None and xr is not None and xl <= xr:
            problems.append(f"{l} is not left of {r} ({xl:+.3f} vs {xr:+.3f}) — sides look swapped")

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


#: Bump when inference, the axes or `validate()` change in a way that should re-derive a stored frame.
#: A figure's map and axes are cached in the catalog, and the alternative to a version is asking "does
#: this stored frame carry the keys today's code needs" — which cannot express "the validator got
#: stricter", the change that actually mattered: two catalogued maps were rejected only after `validate`
#: learned that a limb has to be a chain.
FRAME_REV = 6

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
