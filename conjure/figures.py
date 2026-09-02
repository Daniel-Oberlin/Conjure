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


def node_world_positions(doc: dict) -> dict[int, tuple[float, float, float]]:
    """Bind-pose world position of every node, by node index."""
    nodes = doc.get("nodes") or []
    out: dict[int, tuple[float, float, float]] = {}
    ident = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]

    def walk(idx: int, parent: list[float]) -> None:
        if idx in out or idx >= len(nodes):
            return
        world = _mul(parent, _local_matrix(nodes[idx]))
        out[idx] = (world[12], world[13], world[14])
        for child in nodes[idx].get("children", []):
            walk(child, world)

    scenes = doc.get("scenes") or []
    roots = scenes[doc.get("scene", 0)].get("nodes", []) if scenes else range(len(nodes))
    for r in roots:
        walk(r, ident)
    for i in range(len(nodes)):          # nodes outside the active scene still get a position
        walk(i, ident)
    return out


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
    children = {i: [c for c in (n.get("children") or []) if c in jset]
                for i, n in enumerate(nodes)}

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
    spine = _path(hips, head_top, parent)
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

    # Arms: the chain from the spine out to each hand. Its top is where it leaves the trunk.
    for side, hand in (("left", hand_l), ("right", hand_r)):
        chain = _ancestors(hand, parent)
        trunk = set(spine or []) | {hips}
        branch = []
        for j in chain:
            if j in trunk:
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
    by_name = {n.get("name"): i for i, n in enumerate(nodes) if n.get("name")}
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
        if i is None or i in deform or i not in pos:
            continue
        best, best_d = None, tol
        for j in deform:
            if j not in pos:
                continue
            d = math.dist(pos[i], pos[j])
            if d <= best_d and nodes[j].get("name") not in taken:
                best, best_d = j, d
        if best is not None:
            taken.discard(node_name)
            out[bone] = nodes[best].get("name")
            taken.add(out[bone])
    return out


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
    missing = [b for b in CORE_BONES if not mapping.get(b)]
    if missing:
        problems.append(f"{len(missing)} core bone(s) unmapped: {', '.join(missing[:6])}"
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
    hips_i = idx("hips")
    if hips_i is not None:
        for bone in ("leftFoot", "rightFoot", "head"):
            i = idx(bone)
            if i is not None and hips_i not in _ancestors(i, parent):
                problems.append(f"hips is not an ancestor of {bone}")

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
