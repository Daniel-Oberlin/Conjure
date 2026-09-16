"""Rewrite a captured clip so it plays on a figure it was not authored for (plan § phase 5, tier 2).

A clip binds to a figure BY NODE NAME, which is why one plays on sixteen of the captured figures and on
none of the rest: a rig that spells its bones differently resolves nothing. Tier 2 maps both skeletons
through the canonical humanoid and rewrites every channel, and this is where that happens.

**On the server, producing a normal clip.** The alternative was to send both skeletons to the client and
do the algebra there, and it is worse in every way that matters: the output is a function of two files
and nothing else, so it content-addresses and is computed once per (clip, rig) pair ever; the client
keeps its one code path; and the arithmetic stays next to the tests that pin it. A retargeted clip is
just a clip.

**Both inputs describe themselves.** A clip GLB carries no mesh and no skin but it does carry its
authoring rig's NODES, and their rest transforms match the figure they came from to within 0.075° —
measured across the corpus. So the source rig's rest pose and its humanoid map are both recoverable
from the clip alone, and nothing has to be looked up.

**The law: carry the pose ABSOLUTELY.** Preserving each bone's rotation relative to its own rest is the
obvious method and it is wrong, because two rigs rest differently: if one rests arms-down and the other
arms-out, a clip that puts the first's arms straight down sends the second's half way. What survives the
crossing is where the limb IS. So each rig's axis CONVENTION is divided out instead of its rest POSE:

    C   a convention-free rest frame per bone, built from where its limb POINTS
    K   = C⁻¹ · R              what is left of the authored rest once the physical part is removed
    Wt  = swing · Ws · Ks⁻¹ · Kt      the source's orientation, respelled in the target's convention

`swing` takes the source's rest body frame to the target's, so a figure whose armature rests leaning
performs the clip in ITS frame rather than inheriting the source's. When the two rigs rest the same way
this reduces exactly to `Ws`, which is why a clip played back on its own rig comes home unchanged.

Measured by `scripts/retarget_probe.py` over every rigged figure in the catalog: limb directions within
0.2–8.7° and whole-body orientation within 11°, against 5.5–89.5° and up to 84° for a naive copy. The
probe is the specification; this module is the part of it that ships.

**What it cannot carry.** The humanoid names 22 bones and a captured clip drives 222. The other 200 —
skirt, breast and secondary chains — have no bone on any other rig to receive them, and they are
dropped and counted rather than quietly lost.
"""

from __future__ import annotations

import json
import math
import struct
from typing import Callable, Optional

from .figures import (_local_matrix, best_humanoid, node_world_matrices, parent_map,
                      split_glb, write_glb)

IDENT = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]

#: The humanoid skeleton as parent → child, for reading a limb's DIRECTION.
LIMBS = (("hips", "spine"), ("spine", "chest"), ("chest", "neck"), ("neck", "head"),
         ("chest", "leftShoulder"), ("leftShoulder", "leftUpperArm"),
         ("leftUpperArm", "leftLowerArm"), ("leftLowerArm", "leftHand"),
         ("chest", "rightShoulder"), ("rightShoulder", "rightUpperArm"),
         ("rightUpperArm", "rightLowerArm"), ("rightLowerArm", "rightHand"),
         ("hips", "leftUpperLeg"), ("leftUpperLeg", "leftLowerLeg"),
         ("leftLowerLeg", "leftFoot"), ("leftFoot", "leftToes"),
         ("hips", "rightUpperLeg"), ("rightUpperLeg", "rightLowerLeg"),
         ("rightLowerLeg", "rightFoot"), ("rightFoot", "rightToes"))

CHILD_OF: dict = {}
for _p, _c in LIMBS:
    CHILD_OF.setdefault(_p, _c)
for _end in ("leftHand", "rightHand", "leftToes", "rightToes", "head"):
    CHILD_OF[_end] = None
PARENT_OF = {c: p for p, c in LIMBS}

#: Which body axis squares up each bone's frame. Body FORWARD for almost everything, because almost
#: every humanoid limb points up, down or sideways; body UP only for the feet, whose limb IS forward.
#:
#: A FIXED TABLE, never a measurement. Choosing it with `abs(dot(along, up)) < 0.99` is the obvious way
#: and it is a trap: Jane's `hips → spine` reads 0.9684 and office-babe's 0.9975, so two rigs in the
#: same rest pose land either side of the cut, take different branches, and end a half-turn apart —
#: 179.8° on both shoulders, a flip rather than a drift.
REF_AGAINST_UP = {"leftFoot", "rightFoot", "leftToes", "rightToes"}


# ---------------------------------------------------------------- algebra

def qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def qconj(q):
    return (-q[0], -q[1], -q[2], q[3])


def quat_of(m) -> tuple:
    """Rotation of a column-major 4x4, with the scale divided out — several of these rigs bake a unit
    conversion into the armature, and a conversion that trusts the diagonal reads that as a rotation."""
    cols = [(m[0], m[1], m[2]), (m[4], m[5], m[6]), (m[8], m[9], m[10])]
    lens = [math.sqrt(sum(v * v for v in c)) or 1.0 for c in cols]
    r = [[cols[c][row] / lens[c] for c in range(3)] for row in range(3)]
    tr = r[0][0] + r[1][1] + r[2][2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        return ((r[2][1] - r[1][2]) / s, (r[0][2] - r[2][0]) / s, (r[1][0] - r[0][1]) / s, 0.25 * s)
    if r[0][0] > r[1][1] and r[0][0] > r[2][2]:
        s = math.sqrt(1.0 + r[0][0] - r[1][1] - r[2][2]) * 2
        return (0.25 * s, (r[0][1] + r[1][0]) / s, (r[0][2] + r[2][0]) / s, (r[2][1] - r[1][2]) / s)
    if r[1][1] > r[2][2]:
        s = math.sqrt(1.0 + r[1][1] - r[0][0] - r[2][2]) * 2
        return ((r[0][1] + r[1][0]) / s, 0.25 * s, (r[1][2] + r[2][1]) / s, (r[0][2] - r[2][0]) / s)
    s = math.sqrt(1.0 + r[2][2] - r[0][0] - r[1][1]) * 2
    return ((r[0][2] + r[2][0]) / s, (r[1][2] + r[2][1]) / s, 0.25 * s, (r[1][0] - r[0][1]) / s)


def qmat(q, scale=(1.0, 1.0, 1.0), t=(0.0, 0.0, 0.0)):
    x, y, z, w = q
    sx, sy, sz = scale
    return [(1 - 2 * (y * y + z * z)) * sx, (2 * (x * y + z * w)) * sx, (2 * (x * z - y * w)) * sx, 0,
            (2 * (x * y - z * w)) * sy, (1 - 2 * (x * x + z * z)) * sy, (2 * (y * z + x * w)) * sy, 0,
            (2 * (x * z + y * w)) * sz, (2 * (y * z - x * w)) * sz, (1 - 2 * (x * x + y * y)) * sz, 0,
            t[0], t[1], t[2], 1]


def mmul(a, b):
    out = [0.0] * 16
    for c in range(4):
        for r in range(4):
            out[c * 4 + r] = sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
    return out


def _norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / n, v[1] / n, v[2] / n)


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


# ---------------------------------------------------------------- one rig, as this module needs it

class Rig:
    """A skeleton and its humanoid map, read once from a GLB — a figure or a clip, indifferently."""

    def __init__(self, doc: dict, blob: bytes):
        self.doc = doc
        mapping, source, _f = best_humanoid(doc, blob)
        self.mapping = mapping or {}
        self.source = source or ""
        self.by_name = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
        self.parent = parent_map(doc)
        self.bind = node_world_matrices(doc)
        self._canon: Optional[dict] = None

    def index(self, bone: str) -> Optional[int]:
        return self.by_name.get(self.mapping.get(bone, ""))

    def rest(self, bone: str) -> tuple:
        return quat_of(self.bind[self.index(bone)])

    def positions(self) -> dict:
        return {b: (self.bind[i][12], self.bind[i][13], self.bind[i][14])
                for b in self.mapping if (i := self.index(b)) is not None}

    def body_axes(self):
        """`(side, up, forward)` from the figure's own bones — up hips→neck, across the legs."""
        pos = self.positions()
        if any(b not in pos for b in ("hips", "neck", "leftUpperLeg", "rightUpperLeg")):
            return None
        up = _norm(tuple(pos["neck"][i] - pos["hips"][i] for i in range(3)))
        side = _norm(tuple(pos["leftUpperLeg"][i] - pos["rightUpperLeg"][i] for i in range(3)))
        fwd = _norm(_cross(side, up))
        return (_norm(_cross(up, fwd)), up, fwd)       # re-orthogonalise; the hips are not square

    def body_frame(self) -> tuple:
        axes = self.body_axes()
        if not axes:
            return (0.0, 0.0, 0.0, 1.0)
        s, u, f = axes
        return quat_of([s[0], s[1], s[2], 0, u[0], u[1], u[2], 0, f[0], f[1], f[2], 0, 0, 0, 0, 1])

    def canonical(self) -> dict:
        """A convention-free rest orientation per bone: where its limb POINTS, not how it is spelled."""
        if self._canon is not None:
            return self._canon
        axes = self.body_axes()
        self._canon = {}
        if not axes:
            return self._canon
        _side, up, fwd_body = axes
        pos = self.positions()
        for bone in pos:
            child = CHILD_OF.get(bone)
            a, b = bone, child
            if not child or child not in pos:           # a chain end continues its parent's line
                parent = PARENT_OF.get(bone)
                a, b = (parent, bone) if parent in pos else (None, None)
            d = tuple(pos[b][i] - pos[a][i] for i in range(3)) if a and b else up
            if sum(c * c for c in d) < 1e-12:
                d = up
            along = _norm(d)
            ref = up if bone in REF_AGAINST_UP else fwd_body
            right = _norm(_cross(ref, along))
            upper = _cross(along, right)
            self._canon[bone] = quat_of([right[0], right[1], right[2], 0,
                                         upper[0], upper[1], upper[2], 0,
                                         along[0], along[1], along[2], 0, 0, 0, 0, 1])
        return self._canon

    def convention(self, bone: str) -> Optional[tuple]:
        """`K = C⁻¹ · R` — what is left of the authored rest once the physical part is divided out."""
        canon = self.canonical()
        if bone not in canon or self.index(bone) is None:
            return None
        return qmul(qconj(canon[bone]), self.rest(bone))

    def chain_breaks(self) -> list[str]:
        """Humanoid links whose child is NOT actually under its parent in the skeleton.

        A map can pass every geometric check `validate()` makes and still name bones from different
        BRANCHES of a control rig — Eve's inferred map puts `hips` on `ORG-spine` and `spine` on
        `chest`, so rotating her hips cannot move her spine. Costs nothing when posing one bone at a
        time; breaks retargeting, where a chain's motion has to compose.
        """
        out = []
        for parent, child in LIMBS:
            pi, ci = self.index(parent), self.index(child)
            if pi is None or ci is None:
                continue
            j, ok = self.parent.get(ci), False
            while j is not None:
                if j == pi:
                    ok = True
                    break
                j = self.parent.get(j)
            if not ok:
                out.append(f"{parent}->{child}")
        return out


# ---------------------------------------------------------------- reading and writing glTF animation

def _read_accessor(doc, blob, idx) -> list:
    acc = doc["accessors"][idx]
    view = doc["bufferViews"][acc["bufferView"]]
    size = {"SCALAR": 1, "VEC3": 3, "VEC4": 4}[acc["type"]]
    fmt = {5126: "f", 5123: "H", 5121: "B", 5122: "h", 5120: "b"}[acc["componentType"]]
    width = struct.calcsize(fmt)
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = acc["count"] * size
    vals = list(struct.unpack_from("<" + fmt * n, blob, start))
    if fmt != "f":                                     # normalised integer quaternions are legal glTF
        top = float((1 << (8 * width - 1)) - 1) if fmt in "hb" else float((1 << (8 * width)) - 1)
        vals = [max(-1.0, v / top) for v in vals]
    return [tuple(vals[i:i + size]) for i in range(0, len(vals), size)] if size > 1 else vals


def _tracks(doc, blob, anim) -> tuple[dict, list]:
    """`({node name: [(time, quaternion)]}, sorted union of every keyframe time)`."""
    out, times = {}, set()
    for ch in anim.get("channels") or []:
        if ch["target"]["path"] != "rotation" or ch["target"].get("node") is None:
            continue
        name = (doc["nodes"][ch["target"]["node"]] or {}).get("name")
        if not name:
            continue
        sam = anim["samplers"][ch["sampler"]]
        t = _read_accessor(doc, blob, sam["input"])
        q = _read_accessor(doc, blob, sam["output"])
        if not t or not q:
            continue
        out[name] = list(zip(t, q))
        times.update(t)
    return out, sorted(times)


def _sample(track: list, t: float) -> tuple:
    """Nearest keyframe. The corpus is exported STEP — measured, not assumed — so interpolating would
    invent frames the original never played."""
    lo, hi = 0, len(track) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if track[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    if lo and abs(track[lo - 1][0] - t) <= abs(track[lo][0] - t):
        lo -= 1
    return track[lo][1]


def _posed_world(rig: Rig, locals_by_name: dict) -> dict:
    """World rotation of every mapped bone with `locals_by_name` substituted as local rotations."""
    nodes = rig.doc["nodes"]
    out = {}
    for bone in rig.mapping:
        idx = rig.index(bone)
        if idx is None:
            continue
        chain, j = [], idx
        while j is not None:
            chain.append(j)
            j = rig.parent.get(j)
        m = IDENT
        for j in reversed(chain):
            nd = nodes[j]
            name = nd.get("name")
            if name in locals_by_name:
                m = mmul(m, qmat(locals_by_name[name], nd.get("scale") or (1.0, 1.0, 1.0),
                                 nd.get("translation") or (0.0, 0.0, 0.0)))
            else:
                m = mmul(m, _local_matrix(nd))
        out[bone] = quat_of(m)
    return out


def _target_locals(src: Rig, dst: Rig, carried: dict, swing: tuple) -> dict:
    """Local rotations on the TARGET that put each mapped bone where the source's is.

    Two steps, and the second is what makes this a walk rather than a formula. First the desired WORLD
    rotation per bone, `swing · Ws · Ks⁻¹ · Kt`. Then, top-down, the local that achieves it against the
    parent's ALREADY-MOVED world — using the parent's REST instead is the obvious shortcut and it fails
    the identity case by 32°, which is the only cheap test this has.
    """
    s_world = _posed_world(src, {src.mapping[b]: q for b, q in carried.items()})
    want = {}
    for bone, _q in carried.items():
        k_s, k_t = src.convention(bone), dst.convention(bone)
        if bone not in s_world or k_s is None or k_t is None:
            continue
        want[dst.mapping[bone]] = qmul(qmul(qmul(swing, s_world[bone]), qconj(k_s)), k_t)

    nodes = dst.doc["nodes"]
    scenes = dst.doc.get("scenes") or []
    roots = scenes[dst.doc.get("scene", 0)].get("nodes", []) if scenes else range(len(nodes))
    out, seen = {}, set()
    stack = [(int(r), IDENT) for r in roots]
    while stack:
        idx, parent_world = stack.pop()
        if idx in seen or idx >= len(nodes):
            continue
        seen.add(idx)
        nd = nodes[idx]
        name = nd.get("name")
        if name in want:
            out[name] = qmul(qconj(quat_of(parent_world)), want[name])
            local = qmat(out[name], nd.get("scale") or (1.0, 1.0, 1.0),
                         nd.get("translation") or (0.0, 0.0, 0.0))
        else:
            local = _local_matrix(nd)
        world = mmul(parent_world, local)
        for child in nd.get("children") or []:
            stack.append((int(child), world))
    return out


# ---------------------------------------------------------------- the whole job

class Retargeted:
    """The rewritten clip and an honest account of what did and did not survive."""

    def __init__(self, data: bytes, *, bones: int, times: int, dropped: int,
                 notes: Optional[list] = None):
        self.data = data
        self.bones = bones            # humanoid bones actually rewritten
        self.times = times            # keyframes emitted
        self.dropped = dropped        # source channels with no bone on the target to receive them
        self.notes = notes or []


def retarget_clip(clip_bytes: bytes, figure_bytes: bytes,
                  report: Optional[Callable[[str], None]] = None) -> Optional[Retargeted]:
    """Rewrite `clip_bytes` so its channels drive the skeleton in `figure_bytes`. `None` if it cannot.

    Refused rather than approximated when either side has no humanoid map: a clip bound through a map
    we could not recover would be a figure folded into a knot, and "no" is a better answer than that.
    """
    say = report or (lambda _m: None)
    clip_doc, clip_blob = split_glb(clip_bytes)
    fig_doc, fig_blob = split_glb(figure_bytes)
    anims = clip_doc.get("animations") or []
    if not anims:
        say("that file carries no animation")
        return None
    src, dst = Rig(clip_doc, clip_blob), Rig(fig_doc, fig_blob)
    if not src.mapping:
        say("the clip's own rig has no humanoid map, so there is nothing to map its channels THROUGH")
        return None
    if not dst.mapping:
        say("that figure has no humanoid map — a clip cannot be aimed at a skeleton we cannot name")
        return None

    anim = anims[0]
    tracks, times = _tracks(clip_doc, clip_blob, anim)
    shared = [b for b in src.mapping if b in dst.mapping and src.mapping[b] in tracks
              and dst.index(b) is not None and src.index(b) is not None]
    if not shared or not times:
        say("no humanoid bone the clip drives exists on that figure")
        return None
    dropped = len(tracks) - len(shared)
    swing = qmul(dst.body_frame(), qconj(src.body_frame()))

    notes = []
    breaks = dst.chain_breaks()
    if breaks:
        notes.append(f"that figure's bone map is not a CHAIN at {', '.join(breaks[:3])} — those bones "
                     f"sit in different branches of its skeleton, so motion cannot compose through "
                     f"them and that part of the body will lag")
    if dropped:
        notes.append(f"{dropped} channel(s) drive bones the humanoid does not name — skirt, breast and "
                     f"secondary chains — and no rig has anything to receive them")

    # One pass per keyframe. The output is keyed on the TARGET's node names, so what comes back is an
    # ordinary clip and the client's existing name binding resolves it with no new code path.
    per_bone: dict[str, list] = {}
    for t in times:
        carried = {b: _sample(tracks[src.mapping[b]], t) for b in shared}
        for name, q in _target_locals(src, dst, carried, swing).items():
            per_bone.setdefault(name, []).append(q)
    if not per_bone:
        say("nothing survived the mapping")
        return None
    return Retargeted(_write_clip(anim.get("name") or "clip", times, per_bone),
                      bones=len(per_bone), times=len(times), dropped=dropped, notes=notes)


def _write_clip(name: str, times: list, per_bone: dict) -> bytes:
    """A GLB carrying nothing but rotation channels, one node per driven bone.

    No hierarchy and no mesh, because a clip needs neither: three.js binds a track to the model by the
    NODE NAME the track carries, which is the whole reason a captured clip plays on its own figure at
    all. Keeping the shape the corpus already uses means the client sees nothing new.
    """
    blob = bytearray()
    views, accessors = [], []

    def add(values, kind: str, count: int) -> int:
        flat = [c for v in values for c in v] if kind == "VEC4" else list(values)
        data = struct.pack("<" + "f" * len(flat), *flat)
        while len(blob) % 4:
            blob.append(0)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob.extend(data)
        acc = {"bufferView": len(views) - 1, "componentType": 5126, "count": count, "type": kind}
        if kind == "SCALAR":
            acc["min"], acc["max"] = [min(flat)], [max(flat)]
        accessors.append(acc)
        return len(accessors) - 1

    time_acc = add([float(t) for t in times], "SCALAR", len(times))
    nodes, channels, samplers = [], [], []
    for bone_name, quats in sorted(per_bone.items()):
        if len(quats) != len(times):
            continue                                   # a bone the walk could not place every frame
        nodes.append({"name": bone_name})
        samplers.append({"input": time_acc, "interpolation": "STEP",
                         "output": add(quats, "VEC4", len(quats))})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": len(nodes) - 1, "path": "rotation"}})
    doc = {
        "asset": {"version": "2.0", "generator": "conjure retarget"},
        "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes,
        "animations": [{"name": name, "channels": channels, "samplers": samplers}],
        "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(blob)}],
    }
    return write_glb(doc, bytes(blob))
