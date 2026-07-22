"""Pure plane-relative anchor math — the Python twin of client/plane-anchor.js (docs §4-5, §13.1).

An anchor pins an entity to the room's stable planes (the FLOOR and its nearest WALLS) by storing, at
authoring time, the entity's signed distance to each plane plus its orientation relative to each wall. Any
solver — a client against its live capture, or THIS server against the seed — later re-solves that anchor
against its OWN planes (by shared surface id) and recovers a pose consistent with that geometry. Because an
anchor is defined only by relationships to planes (never absolute coordinates), it transfers exactly across
the Quest's locally-non-rigid maps.

This module is a **1:1 port** of client/plane-anchor.js: same algorithm (weighted-LS position solve +
per-wall quaternion-vote averaging for orientation), same knob defaults, same degeneracy handling. The two
implementations are pinned together by the SHARED golden vectors in tests/js/fixtures/plane-anchor-golden.json
(checked by both tests/js/plane-anchor.test.js and tests/test_plane_anchor.py) so they can't silently drift.

I/O is plain JSON-shaped dicts — the exact shape stored in the seed / streamed on the wire — so the server
can feed seed surfaces straight in:
  plane  = {"id": str, "kind": "floor"|"wall", "normal": [x,y,z], "point": [x,y,z]}
  entity = {"position": [x,y,z], "quaternion": [x,y,z,w], "mode": "grounded"|"free"}
  anchor = {"mode", "floor": {"id","offset"}|None, "walls": [{"id","offset","rel":[x,y,z,w]}, ...]}
Everything numeric is float; no numpy dependency (pure stdlib, mirroring the JS arithmetic operation-for-
operation so the numbers match to well under the cross-language tolerance).
"""

from __future__ import annotations

import math
from typing import Optional


# ---- minimal vector / quaternion math (mirrors the THREE.js ops the JS module leans on) ----

class Vec3:
    __slots__ = ("x", "y", "z")

    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)

    @staticmethod
    def of(a) -> "Vec3":
        return Vec3(a[0], a[1], a[2])

    def clone(self) -> "Vec3":
        return Vec3(self.x, self.y, self.z)

    def length_sq(self) -> float:
        return self.x * self.x + self.y * self.y + self.z * self.z

    def normalize(self) -> "Vec3":
        n = math.sqrt(self.length_sq())
        if n == 0:
            return Vec3(0.0, 0.0, 0.0)
        return Vec3(self.x / n, self.y / n, self.z / n)

    def distance_to(self, o: "Vec3") -> float:
        dx, dy, dz = self.x - o.x, self.y - o.y, self.z - o.z
        return math.sqrt(dx * dx + dy * dy + dz * dz)

    def cross(self, o: "Vec3") -> "Vec3":
        return Vec3(self.y * o.z - self.z * o.y,
                    self.z * o.x - self.x * o.z,
                    self.x * o.y - self.y * o.x)


class Quat:
    __slots__ = ("x", "y", "z", "w")

    def __init__(self, x: float = 0.0, y: float = 0.0, z: float = 0.0, w: float = 1.0) -> None:
        self.x, self.y, self.z, self.w = float(x), float(y), float(z), float(w)

    def clone(self) -> "Quat":
        return Quat(self.x, self.y, self.z, self.w)

    def length_sq(self) -> float:
        return self.x * self.x + self.y * self.y + self.z * self.z + self.w * self.w

    def normalize(self) -> "Quat":
        n = math.sqrt(self.length_sq())
        if n == 0:
            return Quat(0.0, 0.0, 0.0, 1.0)
        return Quat(self.x / n, self.y / n, self.z / n, self.w / n)

    def invert(self) -> "Quat":
        # THREE.Quaternion.invert() == conjugate() (assumes unit) — every quaternion we invert here is unit.
        return Quat(-self.x, -self.y, -self.z, self.w)

    def multiply(self, b: "Quat") -> "Quat":
        # THREE.Quaternion.multiplyQuaternions(this, b): the Hamilton product, this order/sign convention.
        ax, ay, az, aw = self.x, self.y, self.z, self.w
        bx, by, bz, bw = b.x, b.y, b.z, b.w
        return Quat(ax * bw + aw * bx + ay * bz - az * by,
                    ay * bw + aw * by + az * bx - ax * bz,
                    az * bw + aw * bz + ax * by - ay * bx,
                    aw * bw - ax * bx - ay * by - az * bz)

    @staticmethod
    def from_basis(right: Vec3, u: Vec3, fwd: Vec3) -> "Quat":
        # THREE.Matrix4.makeBasis(right, u, fwd) then Quaternion.setFromRotationMatrix — replicated exactly
        # (column-major basis: X=right, Y=u, Z=fwd; the standard trace-based extraction).
        m11, m21, m31 = right.x, right.y, right.z
        m12, m22, m32 = u.x, u.y, u.z
        m13, m23, m33 = fwd.x, fwd.y, fwd.z
        trace = m11 + m22 + m33
        if trace > 0:
            s = 0.5 / math.sqrt(trace + 1.0)
            return Quat((m32 - m23) * s, (m13 - m31) * s, (m21 - m12) * s, 0.25 / s)
        if m11 > m22 and m11 > m33:
            s = 2.0 * math.sqrt(1.0 + m11 - m22 - m33)
            return Quat(0.25 * s, (m12 + m21) / s, (m13 + m31) / s, (m32 - m23) / s)
        if m22 > m33:
            s = 2.0 * math.sqrt(1.0 + m22 - m11 - m33)
            return Quat((m12 + m21) / s, 0.25 * s, (m23 + m32) / s, (m13 - m31) / s)
        s = 2.0 * math.sqrt(1.0 + m33 - m11 - m22)
        return Quat((m13 + m31) / s, (m23 + m32) / s, 0.25 * s, (m21 - m12) / s)


# ---- small linear-algebra helpers (kept explicit so this mirrors the JS 1:1) ----

def solve_sym3(A, b):
    """Solve the symmetric 3×3 system A x = b, A given as its 6 upper-triangle entries
    [a00,a01,a02,a11,a12,a22]. Returns {"x": [x0,x1,x2], "det": det} or None when |det| ~ 0 (singular)."""
    m00, m01, m02, m11, m12, m22 = A[0], A[1], A[2], A[3], A[4], A[5]
    c00 = m11 * m22 - m12 * m12          # cofactors (also the adjugate, since symmetric)
    c01 = m02 * m12 - m01 * m22
    c02 = m01 * m12 - m02 * m11
    det = m00 * c00 + m01 * c01 + m02 * c02
    if abs(det) < 1e-12:
        return None
    c11 = m00 * m22 - m02 * m02
    c12 = m01 * m02 - m00 * m12
    c22 = m00 * m11 - m01 * m01
    return {"x": [(c00 * b[0] + c01 * b[1] + c02 * b[2]) / det,
                  (c01 * b[0] + c11 * b[1] + c12 * b[2]) / det,
                  (c02 * b[0] + c12 * b[1] + c22 * b[2]) / det], "det": det}


def cond2(a: float, b: float, c: float) -> float:
    """Conditioning of a symmetric 2×2 [[a,b],[b,c]] as λmin/λmax ∈ [0,1]. ~0 ⇒ the two directions it was
    built from are near-parallel (degenerate); ~1 ⇒ well-spread. Scale-free, so a robust degeneracy test."""
    tr = a + c
    dsc = math.sqrt(max(0.0, tr * tr - 4 * (a * c - b * b)))
    hi, lo = (tr + dsc) / 2, (tr - dsc) / 2
    return lo / hi if hi > 1e-12 else 0.0


def wall_frame(normal: Vec3, up: Vec3) -> Quat:
    """The gravity+normal frame of a wall: a quaternion whose local +Z is the wall's (horizontal) outward
    normal and local +Y is up (gravity). Entity-independent, so authoring and solving rebuild the SAME frame
    from the same wall + gravity — which is what lets the stored orientation vote transfer."""
    fwd = Vec3(normal.x, 0.0, normal.z)
    if fwd.length_sq() < 1e-9:
        fwd = Vec3(0.0, 0.0, 1.0)                 # (should never happen for a wall) guard
    fwd = fwd.normalize()
    u = up.normalize()
    right = u.cross(fwd).normalize()              # +X = up × forward (right-handed)
    return Quat.from_basis(right, u, fwd)


def average_quat(quats) -> Quat:
    """Average a set of quaternions meant to be close (each is one wall's vote for the SAME orientation).
    Sign-align to the first (q and -q are the same rotation) then normalise the linear mean."""
    if not quats:
        return Quat()
    r = quats[0]
    x = y = z = w = 0.0
    for q in quats:
        s = -1.0 if (q.x * r.x + q.y * r.y + q.z * r.z + q.w * r.w) < 0 else 1.0   # hemisphere-align
        x += s * q.x; y += s * q.y; z += s * q.z; w += s * q.w
    q = Quat(x, y, z, w)
    if q.length_sq() < 1e-12:
        return quats[0].clone()
    return q.normalize()


def twist_about(q: Quat, axis: Vec3) -> Quat:
    """The twist (rotation ABOUT `axis`) of q — the swing-twist decomposition's twist. Used to flatten a
    grounded object's orientation to yaw-only: discard any pitch/roll, keep only rotation about gravity."""
    d = q.x * axis.x + q.y * axis.y + q.z * axis.z
    t = Quat(axis.x * d, axis.y * d, axis.z * d, q.w)
    if t.length_sq() < 1e-12:
        return Quat()                             # q is a 180° swing ⟂ axis → no twist
    return t.normalize()


def _dot3(n: Vec3, v: Vec3) -> float:
    return n.x * v.x + n.y * v.y + n.z * v.z


def signed_dist(plane, pt: Vec3) -> float:
    """Signed distance from pt to the plane (plane carries Vec3 normal/point)."""
    n, p = plane["normal"], plane["point"]
    return n.x * (pt.x - p.x) + n.y * (pt.y - p.y) + n.z * (pt.z - p.z)


def _plane_rhs(plane, offset: float) -> float:
    # RHS of the constraint n·p = c putting p at signed distance `offset`: n·(p−point)=offset ⇒ n·p = n·point+offset.
    return _dot3(plane["normal"], plane["point"]) + offset


def _to_plane(p) -> dict:
    """Normalise a JSON plane {id,kind,normal:[3],point:[3]} into one with Vec3 normal/point."""
    return {"id": p["id"], "kind": p["kind"], "normal": Vec3.of(p["normal"]), "point": Vec3.of(p["point"])}


# ---- authoring ----

def author_anchor(entity: dict, planes: list, opts: Optional[dict] = None) -> dict:
    """Turn an entity pose + the room's local planes into a stored Anchor. Picks the nearest `nRefWalls`
    walls (by centre distance), EXPANDING the set past that count if the chosen walls are near-parallel (so
    the XZ solve won't be degenerate — the 'reach for a farther wall' fallback, docs §4.1), then records the
    entity's signed distance to the floor + each wall and its orientation vote per wall."""
    opts = opts or {}
    N = opts.get("nRefWalls", 3)
    min_cond = opts.get("minCond", 0.05)
    mode = entity.get("mode", "grounded")
    pos = Vec3.of(entity["position"])
    quat = Quat(*entity["quaternion"])

    ps = [_to_plane(p) for p in planes]
    floor_p = next((p for p in ps if p["kind"] == "floor"), None)
    walls = [p for p in ps if p["kind"] == "wall"]
    up = floor_p["normal"].normalize() if floor_p else Vec3(0.0, 1.0, 0.0)

    walls = sorted(walls, key=lambda w: pos.distance_to(w["point"]))   # nearest walls first

    chosen: list = []

    def chosen_cond() -> float:
        a = b = c = 0.0
        for w in chosen:
            nx, nz = w["normal"].x, w["normal"].z
            a += nx * nx; b += nx * nz; c += nz * nz
        return cond2(a, b, c)

    # take N, then keep adding the next-nearest until the horizontal normals span 2-D (or we run out)
    for w in walls:
        if len(chosen) >= N and chosen_cond() >= min_cond:
            break
        chosen.append(w)

    def vote(w) -> dict:
        rel = wall_frame(w["normal"], up).invert().multiply(quat)
        return {"id": w["id"], "offset": signed_dist(w, pos), "rel": [rel.x, rel.y, rel.z, rel.w]}

    return {
        "mode": mode,
        "floor": ({"id": floor_p["id"], "offset": signed_dist(floor_p, pos)} if floor_p else None),
        "walls": [vote(w) for w in chosen],
    }


# ---- solving ----

def solve_anchor(anchor: dict, planes: list, opts: Optional[dict] = None) -> dict:
    """Re-solve a stored Anchor against a solver's OWN local planes (by shared id). Returns the recovered
    pose plus a status. ok=False (with a reason in `stat`) means the present planes can't constrain the pose
    — too few walls, or near-parallel (degenerate) — so the caller should log it and hold/skip rather than
    emit a bogus pose. Output: {ok, position:[x,y,z]|None, quaternion:[x,y,z,w]|None, stat, used}."""
    opts = opts or {}
    floor_weight = opts.get("floorWeight", 6)
    near_bias = opts.get("nearBias", 0.4)
    min_cond = opts.get("minCond", 0.05)

    by_id = {}
    for p in planes:
        by_id[p["id"]] = _to_plane(p)
    floor_ref = anchor.get("floor")
    floor_p = by_id.get(floor_ref["id"]) if floor_ref else None
    up = floor_p["normal"].normalize() if floor_p else Vec3(0.0, 1.0, 0.0)

    # Weighted linear least-squares for position: each plane gives one constraint n·p = c. Normal equations
    # A p = b with A = Σ w n nᵀ (symmetric, 6 entries) and b = Σ w c n.
    A = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    b = [0.0, 0.0, 0.0]
    axz = bxz = cxz = 0.0                 # wall-only XZ matrix, for the scale-free degeneracy test
    used_walls = 0

    def add_constraint(n: Vec3, c: float, w: float) -> None:
        A[0] += w * n.x * n.x; A[1] += w * n.x * n.y; A[2] += w * n.x * n.z
        A[3] += w * n.y * n.y; A[4] += w * n.y * n.z; A[5] += w * n.z * n.z
        b[0] += w * c * n.x; b[1] += w * c * n.y; b[2] += w * c * n.z

    if floor_p and floor_ref:
        add_constraint(floor_p["normal"], _plane_rhs(floor_p, floor_ref["offset"]), floor_weight)

    votes: list = []
    for wa in anchor.get("walls", []):
        wp = by_id.get(wa["id"])
        if not wp:
            continue                      # this solver didn't capture that wall — skip it
        w = 1.0 / (near_bias + abs(wa["offset"]))          # nearer walls (smaller |offset|) weigh more
        add_constraint(wp["normal"], _plane_rhs(wp, wa["offset"]), w)
        nx, nz = wp["normal"].x, wp["normal"].z
        axz += w * nx * nx; bxz += w * nx * nz; cxz += w * nz * nz
        rel = wa["rel"]
        votes.append(wall_frame(wp["normal"], up).multiply(Quat(rel[0], rel[1], rel[2], rel[3])))
        used_walls += 1

    used = {"walls": used_walls, "floor": bool(floor_p)}
    xz_cond = cond2(axz, bxz, cxz)
    if used_walls < 2 or xz_cond < min_cond:
        return {"ok": False, "position": None, "quaternion": None,
                "stat": "degenerate: walls=%d cond=%.3f" % (used_walls, xz_cond), "used": used}
    if anchor.get("mode") == "free" and not floor_p:
        return {"ok": False, "position": None, "quaternion": None, "stat": "free: floor missing", "used": used}

    sol = solve_sym3(A, b)
    if not sol:
        return {"ok": False, "position": None, "quaternion": None, "stat": "singular", "used": used}
    p = Vec3(sol["x"][0], sol["x"][1], sol["x"][2])
    # Grounded: pin Y exactly to the local floor (never float/sink) — the floor's stored offset IS the height
    # above it. XZ still comes from the wall solve above.
    if anchor.get("mode") == "grounded" and floor_p and floor_ref:
        p.y = floor_p["point"].y + floor_ref["offset"]

    q = average_quat(votes)
    if anchor.get("mode") == "grounded":
        q = twist_about(q, up)            # yaw-only; discard any pitch/roll

    stat = "ok walls=%d%s mode=%s" % (used_walls, "+floor" if floor_p else "", anchor.get("mode"))
    return {"ok": True, "position": [p.x, p.y, p.z], "quaternion": [q.x, q.y, q.z, q.w],
            "stat": stat, "used": used}
