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
    Wt  = Ws · Ks⁻¹ · Kt       the source's orientation, respelled in the target's convention

**There is no whole-body term, and there was one.** `swing = Bt · Bs⁻¹` aligned the two rigs' rest body
frames on the reasoning that a figure whose armature rests leaning should perform the clip in ITS frame.
It is wrong twice over. glTF fixes the world frame at Y-up, so there is no world-frame CONVENTION to
divide out — a difference between two rest body frames is a difference in rest POSE, and not carrying
rest pose is the whole of this law. And the rest lean these rigs have is a lean of the spine BONES, so
the absolute carry already reproduces it; `swing` added it a second time. Measured: it was the whole of
the reported tilt (`scripts/clip_tilt.mjs`, plan § alignment). It survived as long as it did because the
probe grades a retarget on the angle between the two POSED body frames and `swing` is by construction
the term that nulls exactly that — four figures scored BELOW the channel-loss floor, which is the tell.
`scripts/retarget_probe.py --swing` restores it, and is the only place it still exists.

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

from .figures import (FINGER_JOINTS, FINGERS, _local_matrix, best_humanoid,
                      node_world_matrices, parent_map, split_glb, write_glb)

#: Bump when ANYTHING that changes a rewritten clip's contents changes. A retargeted clip is a DERIVED
#: artefact cached under a content address, and a content address fingerprints the bytes rather than the
#: method that made them — so without a stamp, a pair retargeted by an older build is handed back
#: forever and a fix reaches nobody. That is exactly what happened: the ancestor fix landed, the server
#: was restarted, and the figure kept swinging because the cache still held the clip made before it.
#:
#: `figures.FRAME_REV` exists for the same reason and this is the second artefact to need it, so the
#: rule is general: anything derived and cached carries the revision of the code that derived it.
RETARGET_REV = 7        # 7: the OUTPUT interpolates LINEAR — written STEP, it stuttered
                        # 6: the humanoid reaches the FINGERS, 22 bones -> 52
                        # 5: no `swing` — aligning the rest BODY FRAMES added the rest pose twice
                        # 4: resampling HOLDS the previous keyframe (STEP), not the nearest
                        # 3: a carried translation goes out to WORLD and back, so the armature's UNITS
                        #    are converted and not only its axes
                        # 2: unmapped ancestors count toward a mapped bone's world orientation, and
                        #    bones the clip SLIDES carry their translation
                        # 1: rotation only, mapped bones only

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

#: The fingers, appended rather than woven in so the body chain above stays readable. Hand -> Proximal
#: -> Intermediate -> Distal, five a side. A rig that spells only some of them maps only those, and a
#: link whose either end is unmapped is skipped everywhere this table is read.
LIMBS = LIMBS + tuple(
    (f"{_s}Hand" if _j == 0 else f"{_s}{_f}{FINGER_JOINTS[_j - 1]}", f"{_s}{_f}{FINGER_JOINTS[_j]}")
    for _s in ("left", "right") for _f in FINGERS for _j in range(len(FINGER_JOINTS)))

CHILD_OF: dict = {}
for _p, _c in LIMBS:
    CHILD_OF.setdefault(_p, _c)
#: Chain ends continue their parent's line. The HANDS stay ends even though fingers now hang off them:
#: a hand's canonical frame has always come from `lowerArm -> hand`, every retarget in the catalog was
#: measured against that, and letting a thumb redefine it would move every wrist in the corpus to fix
#: nothing. The finger TIPS are ends for the ordinary reason — nothing is below them.
for _end in (("leftHand", "rightHand", "leftToes", "rightToes", "head")
             + tuple(f"{_s}{_f}{FINGER_JOINTS[-1]}"
                     for _s in ("left", "right") for _f in FINGERS)):
    CHILD_OF[_end] = None
PARENT_OF = {c: p for p, c in LIMBS}

def _hand_refs(pos: dict) -> dict:
    """The reference axis for each FINGER bone, taken from that hand rather than from the body.

    **A finger cannot be squared up against a body axis, and this was measured rather than assumed.**
    A hand turns freely at the wrist, so any fixed body direction eventually lines up with a finger and
    the frame goes degenerate. Across every rigged figure in the catalog, the worst |cos| between a
    finger's limb and the best body axis available is 0.82 (thumbs, against SIDE) and 0.95–0.97 for the
    other four, against FORWARD. `Characters Shaun`'s index finger reads 0.966 off body forward and
    `Bride`'s thumb 0.996 off body up. Arms and legs get away with a body axis because a rest pose holds
    them roughly fixed against the torso; fingers are one joint further out than that holds for.

    So the reference comes from the hand's own bones, and there are two because a thumb is not a finger:

      ACROSS THE KNUCKLES, `indexProximal -> littleProximal`, for index/middle/ring/little. Perpendicular
      to all four by construction — worst 0.251 over the corpus, on Yuffie.
      THE PALM NORMAL, that crossed with the middle finger's own direction, for the thumb. The thumb
      points across the palm, so the knuckle axis is exactly the wrong reference for it — 0.851 on
      Yuffie — while the palm normal is 0.382 at worst, on Bianca, and 0.229 median.

    `{}` for a hand that has not got the bones to say: one Mixamo rig in the catalog carries a thumb and
    an index and no others, so there is no across-palm direction to take. Those fall back to the body
    axis, which for that rig measures 0.524 — fine, and honest about being a fallback.
    """
    out: dict = {}
    for side in ("left", "right"):
        a, b = f"{side}IndexProximal", f"{side}LittleProximal"
        mid, mid_next = f"{side}MiddleProximal", f"{side}MiddleIntermediate"
        if a not in pos or b not in pos:
            continue
        knuckles = _norm(tuple(pos[b][i] - pos[a][i] for i in range(3)))
        for finger in ("Index", "Middle", "Ring", "Little"):
            for joint in FINGER_JOINTS:
                out[f"{side}{finger}{joint}"] = knuckles
        if mid in pos and mid_next in pos:
            along_mid = _norm(tuple(pos[mid_next][i] - pos[mid][i] for i in range(3)))
            palm = _norm(_cross(knuckles, along_mid))
            for joint in FINGER_JOINTS:
                out[f"{side}Thumb{joint}"] = palm
    return out


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
        """The rest body frame as a quaternion. **Nothing in the law uses this, deliberately** — see
        the module docstring. It is kept because the test that pins the law needs a way to say that two
        fixtures really do rest differently, and because the number is worth being able to print."""
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
        hand_ref = _hand_refs(pos)
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
            ref = hand_ref.get(bone) or (up if bone in REF_AGAINST_UP else fwd_body)
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


def _moving_translations(doc, blob, anim, rest_of) -> dict:
    """`{node name: [(time, xyz)]}` for translation channels that actually MOVE.

    Most do not. Measured on `LayTableIdle`: 104 translation channels, of which **4** move at all — the
    root by 4 cm, the hip by 6 mm, two rib twists by less. The rest restate the rest pose every frame.
    Carrying only the movers keeps a retargeted clip the size of the motion in it rather than the size
    of the skeleton, and means a bone with no authored translation is left exactly where the target rig
    puts it instead of being nudged by a rounding difference between two rigs.
    """
    out = {}
    for ch in anim.get("channels") or []:
        if ch["target"]["path"] != "translation" or ch["target"].get("node") is None:
            continue
        name = (doc["nodes"][ch["target"]["node"]] or {}).get("name")
        if not name:
            continue
        sam = anim["samplers"][ch["sampler"]]
        t = _read_accessor(doc, blob, sam["input"])
        v = _read_accessor(doc, blob, sam["output"])
        if not t or not v:
            continue
        span = max(math.dist(p, v[0]) for p in v)
        if span > 1e-3 * max(1.0, abs(rest_of.get(name, 1.0))):
            out[name] = list(zip(t, v))
    return out


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
    """The last keyframe AT OR BEFORE `t` — STEP semantics, which is how this corpus is exported.

    NEAREST is the obvious reading of "no interpolation" and it is wrong, because every channel is
    resampled onto the UNION of all their times. Each channel keeps its own keyframes in the source, so
    at a union time that belongs to one channel's grid and falls between another's, nearest rounds the
    second one FORWARD while its neighbours hold — and the pose alternates between two states on
    successive frames. On device that read as a figure oscillating: measured through the client's own
    mixer, her body flipped between 0.5° and 7.2° on alternating samples while the same clip on its own
    rig stayed under 2°. Holding the previous value is what three.js does with `InterpolateDiscrete`,
    so this now resamples to exactly what the original plays.
    """
    lo, hi = 0, len(track) - 1
    while lo < hi:                                     # first index with time > t
        mid = (lo + hi) // 2
        if track[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    if track[lo][0] > t and lo:
        lo -= 1
    return track[lo][1]


def _posed_place(rig: Rig, rots: dict, slides: dict) -> dict:
    """World POSITION of every mapped bone, with both the clip's rotations and its translations applied.

    Separate from `_posed_world` because they answer different questions and this one needs the slides:
    where a bone ENDS UP depends on every translation above it, and the translations above a mapped bone
    are mostly on bones the humanoid does not name.
    """
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
            if name in rots or name in slides:
                m = mmul(m, qmat(rots.get(name) or tuple(nd.get("rotation") or (0, 0, 0, 1)),
                                 nd.get("scale") or (1.0, 1.0, 1.0),
                                 slides.get(name) or nd.get("translation") or (0.0, 0.0, 0.0)))
            else:
                m = mmul(m, _local_matrix(nd))
        out[bone] = (m[12], m[13], m[14])
    return out


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


def _target_locals(src: Rig, dst: Rig, carried: dict, everything: dict) -> dict:
    """Local rotations on the TARGET that put each mapped bone where the source's is.

    Two steps, and the second is what makes this a walk rather than a formula. First the desired WORLD
    rotation per bone, `Ws · Ks⁻¹ · Kt`. Then, top-down, the local that achieves it against the
    parent's ALREADY-MOVED world — using the parent's REST instead is the obvious shortcut and it fails
    the identity case by 32°, which is the only cheap test this has.

    **`everything` is every rotation the clip carries, not only the mapped ones, and that distinction is
    load-bearing.** A bone the humanoid does not name can still be a mapped bone's ANCESTOR, and then it
    is part of that bone's world orientation whether we can carry it onward or not. Alice's
    `CC_Base_BoneRoot` is the case: it rotates 36.3° over `LayTableIdle` while `CC_Base_Hip` under it
    rotates 38.6° the other way, so the two very nearly cancel and what you see is her SLIDING. Reading
    the hips' world from the mapped subset alone reported the full 38.6° as if it were real, and the
    retargeted figure swung bodily about her own axis at the cadence of a motion that does not rotate
    her at all.
    """
    s_world = _posed_world(src, everything)
    want = {}
    for bone, _q in carried.items():
        k_s, k_t = src.convention(bone), dst.convention(bone)
        if bone not in s_world or k_s is None or k_t is None:
            continue
        want[dst.mapping[bone]] = qmul(qmul(s_world[bone], qconj(k_s)), k_t)

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

    def __init__(self, data: bytes, *, bones: int, times: int, dropped: int, slid: int = 0,
                 notes: Optional[list] = None):
        self.data = data
        self.bones = bones            # humanoid bones actually rewritten
        self.times = times            # keyframes emitted
        self.dropped = dropped        # source channels with no bone on the target to receive them
        self.slid = slid              # bones carrying a TRANSLATION, not only a rotation
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
    rest_of = {n.get("name"): max(abs(c) for c in (n.get("translation") or [1.0]))
               for n in clip_doc.get("nodes") or [] if n.get("name")}
    slides = _moving_translations(clip_doc, clip_blob, anim, rest_of)
    shared = [b for b in src.mapping if b in dst.mapping and src.mapping[b] in tracks
              and dst.index(b) is not None and src.index(b) is not None]
    if not shared or not times:
        say("no humanoid bone the clip drives exists on that figure")
        return None
    dropped = len(tracks) - len(shared)

    notes = []
    # BODY breaks and FINGER breaks are the same defect and not the same news, so they are reported
    # apart. A broken torso link wrecks the whole performance — Trish's `spine->chest` is the case the
    # note was written for. A broken finger link is cosmetic and COMMON: Grace's rig exports its three
    # FK finger joints as SIBLINGS under one palm bone, because what chained them in Blender was a
    # constraint and a GLB carries none. Each joint still reaches its right absolute orientation, since
    # the walk solves every node against its own real parent; what it cannot do is carry the bend
    # onward. Reported together, twelve finger links pushed the torso out of a note that shows three.
    parts = {"body": [], "finger": []}
    for link in dst.chain_breaks():
        parts["finger" if any(f in link for f in FINGERS) else "body"].append(link)
    if parts["body"]:
        notes.append(f"that figure's bone map is not a CHAIN at {', '.join(parts['body'][:3])} — those "
                     f"bones sit in different branches of its skeleton, so motion cannot compose "
                     f"through them and that part of the body will lag")
    if parts["finger"]:
        notes.append(f"{len(parts['finger'])} finger link(s) are not a chain on that figure — its "
                     f"finger joints are siblings rather than a chain, so each one is posed correctly "
                     f"but a bend does not carry to the joint beyond it")
    if dropped:
        notes.append(f"{dropped} channel(s) drive bones the humanoid does not name — skirt, breast and "
                     f"secondary chains — and no rig has anything to receive them")

    # One pass per keyframe. The output is keyed on the TARGET's node names, so what comes back is an
    # ordinary clip and the client's existing name binding resolves it with no new code path.
    per_bone: dict[str, list] = {}
    for t in times:
        carried = {b: _sample(tracks[src.mapping[b]], t) for b in shared}
        everything = {node: _sample(tr, t) for node, tr in tracks.items()}
        for name, q in _target_locals(src, dst, carried, everything).items():
            per_bone.setdefault(name, []).append(q)
    if not per_bone:
        say("nothing survived the mapping")
        return None
    # Every bone the two rigs SHARE, not only the ones the clip rotates: a bone can slide without
    # turning — a hip that shifts weight, a root that drifts — and keying translation off the rotation
    # set silently dropped exactly those.
    both = [b for b in src.mapping if b in dst.mapping
            and src.index(b) is not None and dst.index(b) is not None]
    moved = _carry_translations(
        src, dst, slides,
        [(t, {n: _sample(tr, t) for n, tr in tracks.items()}) for t in times], both)
    if slides and not moved:
        notes.append(f"{len(slides)} bone(s) SLIDE in this clip and none of them maps onto that figure, "
                     f"so that much of the motion is lost")
    return Retargeted(_write_clip(anim.get("name") or "clip", times, per_bone, moved),
                      bones=len(per_bone), times=len(times), dropped=dropped, slid=len(moved),
                      notes=notes)


def _carry_translations(src: Rig, dst: Rig, slides: dict, times_and_rots: list,
                        bones: list) -> dict:
    """Translation tracks for the target, for the bones the clip actually SLIDES.

    Rotation alone is not the whole of a pose. Alice's clips move her root a few centimetres and her hip
    a few millimetres, and dropping that made a retargeted figure stiller than the original — the
    client's own `retarget()` has always kept these, re-basing each onto the model's rest so the clip's
    authored ADDRESS is discarded and its movement is not.

    Carried as a delta from the clip's own first frame, for the same reason: what the source says about
    where the figure stood belongs to the scene it was captured from.

    **The delta is taken all the way out to WORLD and back**, through each parent's full bind matrix
    rather than through its rotation alone. A translation lives in its parent's coordinates, and those
    coordinates differ in UNITS as well as in direction: Alice's armature bakes centimetres — a parent
    world scale of 0.01 — where Grace's is metres at 1.0. Turning the delta without rescaling it made a
    0.64 cm hip sway into 0.61 m, and the figure slid back and forth across the room.

    Measured against HEIGHT first, which looks like the same thing and is not: both figures are about
    1.7 m tall in world, so the height ratio was 0.96 and corrected nothing. The unit difference is in
    the armature, not in the body.

    World displacement is preserved rather than scaled by build. A 4 cm sway is 4 cm on anyone; making
    it proportional to height would be a second guess on top of a first.
    """
    if not slides:
        return {}
    # THE HIPS CARRY EVERYTHING ABOVE THEM. A translation on a bone the humanoid does not name still
    # moves the figure, and the biggest one always is: Alice's `CC_Base_BoneRoot` slides 4 cm and is the
    # hips' parent, so on her own rig the hips travel 4 cm while the body barely turns. Carried
    # per-bone, that slide had nowhere to go — the target has no equivalent root — and the retargeted
    # figure held still and SWUNG instead, which is the same complaint in a third form.
    #
    # So the hips' WORLD displacement is measured on the source with every translation applied, and
    # folded into the target's hips. Ancestors it cannot name are then carried by the one bone that can.
    place = [_posed_place(src, rots, {n: _sample(tr, t) for n, tr in slides.items()})
             for t, rots in times_and_rots]
    out: dict[str, list] = {}
    if "hips" in src.mapping and dst.index("hips") is not None and place:
        t_parent = dst.parent.get(dst.index("hips"))
        t_rot, t_scale = _frame_of(dst.bind.get(t_parent))
        rest = dst.doc["nodes"][dst.index("hips")].get("translation") or [0.0, 0.0, 0.0]
        base = place[0].get("hips")
        row = []
        for frame in place:
            here = frame.get("hips", base)
            world = tuple(here[i] - base[i] for i in range(3))   # already metres: bind is world
            back = _rotate(world, qconj(t_rot))
            row.append(tuple(rest[i] + back[i] / t_scale[i] for i in range(3)))
        if any(math.dist(v, row[0]) > 1e-6 for v in row):
            out[dst.mapping["hips"]] = row
    return out


def _frame_of(m) -> tuple:
    """`(rotation, scale)` of a bind matrix — the scale is what carries the UNITS."""
    if m is None:
        return ((0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0))
    scale = tuple(math.sqrt(sum(m[c * 4 + k] ** 2 for k in range(3))) or 1.0 for c in range(3))
    return (quat_of(m), scale)


def _rotate(v, q):
    m = qmat(q)
    return (m[0] * v[0] + m[4] * v[1] + m[8] * v[2],
            m[1] * v[0] + m[5] * v[1] + m[9] * v[2],
            m[2] * v[0] + m[6] * v[1] + m[10] * v[2])


def _write_clip(name: str, times: list, per_bone: dict, moved: Optional[dict] = None) -> bytes:
    """A GLB carrying nothing but rotation channels, one node per driven bone.

    No hierarchy and no mesh, because a clip needs neither: three.js binds a track to the model by the
    NODE NAME the track carries, which is the whole reason a captured clip plays on its own figure at
    all. Keeping the shape the corpus already uses means the client sees nothing new.
    """
    blob = bytearray()
    views, accessors = [], []

    def add(values, kind: str, count: int) -> int:
        flat = [c for v in values for c in v] if kind in ("VEC4", "VEC3") else list(values)
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

    # LINEAR, and this was STEP until it was measured. A key is emitted at the UNION of every source
    # key time, so between two consecutive output keys no source channel has a key either — the source
    # is interpolating linearly there and the target has to as well. Written STEP, the target instead
    # HELD each pose and jumped to the next, which on a 134-key clip is a visible stutter and reads as
    # a wobble: against Alice playing natively, `clip_diff` showed Akari's body turn agreeing EXACTLY
    # on every sample that landed on a keyframe and off by ~2.3° on every sample between two, in a
    # perfect alternation. Sixteen samples, eight exact, eight wrong; a physical defect does not do
    # that. It also explains why a range metric could not see it: STEP visits the same extremes as
    # LINEAR, so min/max over the clip is unchanged and only a comparison AT AN INSTANT exposes it.
    #
    # `_sample` still HOLDS when reading the source, and that is a different question — there it is
    # reconstructing the source's own value at a time, and NEAREST was measured wrong for it.
    time_acc = add([float(t) for t in times], "SCALAR", len(times))
    nodes, channels, samplers = [], [], []
    index: dict = {}
    for bone_name, quats in sorted(per_bone.items()):
        if len(quats) != len(times):
            continue                                   # a bone the walk could not place every frame
        index[bone_name] = len(nodes)
        nodes.append({"name": bone_name})
        samplers.append({"input": time_acc, "interpolation": "LINEAR",
                         "output": add(quats, "VEC4", len(quats))})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": len(nodes) - 1, "path": "rotation"}})
    for bone_name, xyz in sorted((moved or {}).items()):
        if len(xyz) != len(times):
            continue
        if bone_name not in index:
            index[bone_name] = len(nodes)
            nodes.append({"name": bone_name})
        samplers.append({"input": time_acc, "interpolation": "LINEAR",
                         "output": add(xyz, "VEC3", len(xyz))})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": index[bone_name], "path": "translation"}})
    doc = {
        "asset": {"version": "2.0", "generator": "conjure retarget"},
        "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes,
        "animations": [{"name": name, "channels": channels, "samplers": samplers}],
        "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(blob)}],
    }
    return write_glb(doc, bytes(blob))
