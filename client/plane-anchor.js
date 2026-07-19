// @ts-check
// Pure plane-relative anchor math (docs/local-first-geometry.md §4-5). An anchor pins an entity to the
// room's stable planes — the FLOOR and its nearest WALLS — by storing, at authoring time, the entity's
// signed distance to each plane plus its orientation relative to each wall. Any client (or the server,
// against the seed) later RE-SOLVES that anchor against ITS OWN local planes, by shared surface id, and
// recovers a pose consistent with its own geometry. Because the anchor is defined only by relationships to
// planes (never absolute coordinates), it transfers exactly across the Quest's locally-non-rigid maps.
//
// Technique: this is MULTILATERATION — a position fixed by distances to known references (look it up under
// that name). Deliberately OVER-DETERMINED (more planes than DOF): averaging cuts per-plane noise, and a
// missing reference wall just drops a constraint instead of breaking the solve.
//
// Placement MODE drives BOTH position and orientation as one choice (§5):
//   • "grounded" (default) — XZ from the walls; Y snapped to the local floor; orientation yaw-only (a thing
//     on the floor cannot lean, so pitch≡roll≡0 by definition — pinned, not measured).
//   • "free"               — full 3-D position (floor + walls); orientation a full quaternion (heading +
//     pitch + roll), gimbal-safe (no yaw extraction — cf. the A-Frame YXZ euler-order trap, docs/room-model).
//
// Pure like room-snap.js: THREE is the first arg (browser passes AFRAME.THREE, node passes require('three')),
// no state/DOM/globals. Golden vectors in tests/js/fixtures/plane-anchor-golden.json pin the numeric
// behaviour so the future Python server port (docs §13.1) can be checked against the identical cases.

/**
 * @typedef {typeof import('three')} THREE_NS
 * @typedef {import('three').Vector3} Vec3
 * @typedef {import('three').Quaternion} Quat
 */

/**
 * A stable reference plane the solver anchors to — a wall or the floor. Both authoring and solving supply
 * these from their OWN local geometry, keyed by the shared `id`.
 * @typedef {Object} Plane
 * @property {string} id                 shared surface id (survives across clients via registration/matchRef)
 * @property {"floor"|"wall"} kind
 * @property {Vec3} normal               UNIT normal — wall: horizontal & outward; floor: up (≈ gravity)
 * @property {Vec3} point                any point on the plane (wall centre / a floor point)
 */

/**
 * The entity pose being anchored (authoring input).
 * @typedef {Object} Entity
 * @property {Vec3} position
 * @property {Quat} quaternion
 * @property {"grounded"|"free"} [mode]  default "grounded"
 */

/**
 * The stored anchor (what lives in the shared model / seed). Only relationships to planes — no coordinates.
 * @typedef {Object} Anchor
 * @property {"grounded"|"free"} mode
 * @property {{id: string, offset: number}|null} floor   offset = signed distance to the floor = height above it
 * @property {{id: string, offset: number, rel: number[]}[]} walls
 *   per reference wall: `offset` = signed perpendicular distance to the wall plane; `rel` = the entity's
 *   orientation expressed in that wall's gravity+normal frame ([x,y,z,w]) — one full-quaternion vote.
 */

/**
 * Tunable knobs (all optional; absent → the default).
 * @typedef {Object} AnchorOpts
 * @property {number} [nRefWalls]   reference walls to store at authoring (default 3; over-specified)
 * @property {number} [floorWeight] weight of the floor constraint in the position solve (default 6)
 * @property {number} [nearBias]    wall weight is 1/(nearBias + |offset|) — nearer walls weigh more (default 0.4)
 * @property {number} [minCond]     min 2×2 XZ conditioning (λmin/λmax) to accept the wall geometry (default 0.05)
 */

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else (/** @type {any} */ (root)).PlaneAnchor = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // ---- small linear-algebra helpers (kept explicit so the Python port mirrors them 1:1) ----

  // Solve the symmetric 3×3 system A x = b, A given as its 6 upper-triangle entries
  // [a00,a01,a02,a11,a12,a22]. Returns {x:[x0,x1,x2], det} or null when |det| is ~0 (singular).
  /**
   * @param {number[]} A  [a00,a01,a02,a11,a12,a22]
   * @param {number[]} b  [b0,b1,b2]
   * @returns {{x: number[], det: number}|null}
   */
  function solveSym3(A, b) {
    var m00 = A[0], m01 = A[1], m02 = A[2], m11 = A[3], m12 = A[4], m22 = A[5];
    var c00 = m11 * m22 - m12 * m12;      // cofactors (also the adjugate, since symmetric)
    var c01 = m02 * m12 - m01 * m22;
    var c02 = m01 * m12 - m02 * m11;
    var det = m00 * c00 + m01 * c01 + m02 * c02;
    if (Math.abs(det) < 1e-12) return null;
    var c11 = m00 * m22 - m02 * m02;
    var c12 = m01 * m02 - m00 * m12;
    var c22 = m00 * m11 - m01 * m01;
    return { x: [(c00 * b[0] + c01 * b[1] + c02 * b[2]) / det,
                 (c01 * b[0] + c11 * b[1] + c12 * b[2]) / det,
                 (c02 * b[0] + c12 * b[1] + c22 * b[2]) / det], det: det };
  }

  // Conditioning of a symmetric 2×2 [[a,b],[b,c]] as λmin/λmax ∈ [0,1]. ~0 ⇒ the two directions it was built
  // from are near-parallel (degenerate); ~1 ⇒ well-spread. Scale-free, so it's a robust degeneracy test.
  /** @param {number} a @param {number} b @param {number} c @returns {number} */
  function cond2(a, b, c) {
    var tr = a + c, dsc = Math.sqrt(Math.max(0, tr * tr - 4 * (a * c - b * b)));
    var hi = (tr + dsc) / 2, lo = (tr - dsc) / 2;
    return hi > 1e-12 ? lo / hi : 0;
  }

  // The gravity+normal frame of a wall: a quaternion whose local +Z is the wall's (horizontal) outward
  // normal and local +Y is up (gravity). Entity-independent, so authoring and solving rebuild the SAME
  // frame from the same wall + gravity — which is what lets the stored orientation vote transfer. (Wall
  // normal ⟂ up by construction, so the basis is orthonormal.)
  /**
   * @param {THREE_NS} THREE
   * @param {Vec3} normal   wall normal (will be projected horizontal + normalised)
   * @param {Vec3} up       gravity up
   * @returns {Quat}
   */
  function wallFrame(THREE, normal, up) {
    var fwd = new THREE.Vector3(normal.x, 0, normal.z);
    if (fwd.lengthSq() < 1e-9) fwd.set(0, 0, 1);          // (should never happen for a wall) guard
    fwd.normalize();
    var u = up.clone().normalize();
    var right = new THREE.Vector3().crossVectors(u, fwd).normalize();   // +X = up × forward (right-handed)
    var m = new THREE.Matrix4().makeBasis(right, u, fwd);
    return new THREE.Quaternion().setFromRotationMatrix(m);
  }

  // Average a set of quaternions that are meant to be close (each is one wall's vote for the SAME entity
  // orientation). Sign-align to the first (q and -q are the same rotation) then normalise the linear mean —
  // accurate for tight clusters and dependency-free. Returns identity for an empty set.
  /**
   * @param {THREE_NS} THREE
   * @param {Quat[]} quats
   * @returns {Quat}
   */
  function averageQuat(THREE, quats) {
    if (!quats.length) return new THREE.Quaternion();
    var r = quats[0], x = 0, y = 0, z = 0, w = 0;
    quats.forEach(function (q) {
      var s = (q.x * r.x + q.y * r.y + q.z * r.z + q.w * r.w) < 0 ? -1 : 1;   // hemisphere-align
      x += s * q.x; y += s * q.y; z += s * q.z; w += s * q.w;
    });
    var q = new THREE.Quaternion(x, y, z, w);
    if (q.lengthSq() < 1e-12) return quats[0].clone();
    return q.normalize();
  }

  // The twist (rotation ABOUT `axis`) of q — the swing-twist decomposition's twist. Projects q's vector
  // part onto the axis and renormalises. Used to flatten a grounded object's orientation to yaw-only:
  // discard any pitch/roll, keep only rotation about gravity.
  /**
   * @param {THREE_NS} THREE
   * @param {Quat} q
   * @param {Vec3} axis   unit
   * @returns {Quat}
   */
  function twistAbout(THREE, q, axis) {
    var d = q.x * axis.x + q.y * axis.y + q.z * axis.z;
    var t = new THREE.Quaternion(axis.x * d, axis.y * d, axis.z * d, q.w);
    if (t.lengthSq() < 1e-12) return new THREE.Quaternion();     // q is a 180° swing ⟂ axis → no twist
    return t.normalize();
  }

  /** @param {Plane} p @param {Vec3} pt @returns {number} signed distance from pt to the plane */
  function signedDist(p, pt) {
    return p.normal.x * (pt.x - p.point.x) + p.normal.y * (pt.y - p.point.y) + p.normal.z * (pt.z - p.point.z);
  }
  /** @param {Vec3} n @param {Vec3} v @returns {number} */
  function dot3(n, v) { return n.x * v.x + n.y * v.y + n.z * v.z; }
  // The RHS of a plane constraint n·p = c that puts p at signed distance `offset` from plane `p`:
  //   n·(p − point) = offset  ⇒  n·p = n·point + offset.
  /** @param {Plane} p @param {number} offset @returns {number} */
  function planeRHS(p, offset) { return dot3(p.normal, p.point) + offset; }

  // ---- authoring ----

  // Turn an entity pose + the room's local planes into a stored Anchor. Picks the nearest `nRefWalls` walls
  // (by centre distance), EXPANDING the set past that count if the chosen walls are near-parallel (so the
  // XZ solve won't be degenerate — the "reach for a farther wall" fallback, docs §4.1), then records the
  // entity's signed distance to the floor + each wall and its orientation vote per wall.
  /**
   * @param {THREE_NS} THREE
   * @param {Entity} entity
   * @param {Plane[]} planes    the authoring client's local floor + walls
   * @param {AnchorOpts} [opts]
   * @returns {Anchor}
   */
  function authorAnchor(THREE, entity, planes, opts) {
    opts = opts || {};
    var N = opts.nRefWalls != null ? opts.nRefWalls : 3;
    var minCond = opts.minCond != null ? opts.minCond : 0.05;
    var mode = entity.mode || "grounded";
    var floorP = /** @type {Plane|null} */ (null);
    /** @type {Plane[]} */
    var walls = [];
    planes.forEach(function (p) { if (p.kind === "floor") floorP = p; else if (p.kind === "wall") walls.push(p); });
    var up = floorP ? floorP.normal.clone().normalize() : new THREE.Vector3(0, 1, 0);

    // nearest walls first
    walls = walls.slice().sort(function (a, b) {
      return entity.position.distanceTo(a.point) - entity.position.distanceTo(b.point);
    });
    // take N, then keep adding the next-nearest until the horizontal normals span 2-D (or we run out)
    /** @type {Plane[]} */
    var chosen = [];
    /** @returns {number} conditioning of the current chosen set's XZ normal matrix */
    function chosenCond() {
      var a = 0, b = 0, c = 0;
      chosen.forEach(function (w) { var nx = w.normal.x, nz = w.normal.z; a += nx * nx; b += nx * nz; c += nz * nz; });
      return cond2(a, b, c);
    }
    for (var i = 0; i < walls.length; i++) {
      if (chosen.length >= N && chosenCond() >= minCond) break;
      chosen.push(walls[i]);
    }

    return {
      mode: mode,
      floor: floorP ? { id: floorP.id, offset: signedDist(floorP, entity.position) } : null,
      walls: chosen.map(function (w) {
        var rel = wallFrame(THREE, w.normal, up).invert().multiply(entity.quaternion);
        return { id: w.id, offset: signedDist(w, entity.position), rel: [rel.x, rel.y, rel.z, rel.w] };
      })
    };
  }

  // ---- solving ----

  // Re-solve a stored Anchor against a client's OWN local planes (by shared id). Returns the recovered pose
  // in the client's frame plus a status. `ok:false` (with a reason in `stat`) means the client's present
  // planes can't constrain the pose — too few walls, or they're near-parallel (degenerate); the caller
  // should log it (docs §4.1/§5.2) and hold/skip rather than render a bogus pose.
  /**
   * @param {THREE_NS} THREE
   * @param {Anchor} anchor
   * @param {Plane[]} planes    the solving client's local floor + walls
   * @param {AnchorOpts} [opts]
   * @returns {{ok: boolean, position: Vec3|null, quaternion: Quat|null, stat: string,
   *            used: {walls: number, floor: boolean}}}
   */
  function solveAnchor(THREE, anchor, planes, opts) {
    opts = opts || {};
    var floorWeight = opts.floorWeight != null ? opts.floorWeight : 6;
    var nearBias = opts.nearBias != null ? opts.nearBias : 0.4;
    var minCond = opts.minCond != null ? opts.minCond : 0.05;
    /** @type {Record<string, Plane>} */
    var byId = {};
    planes.forEach(function (p) { byId[p.id] = p; });
    var floorP = anchor.floor ? byId[anchor.floor.id] : null;
    var up = floorP ? floorP.normal.clone().normalize() : new THREE.Vector3(0, 1, 0);

    // Assemble the weighted linear least-squares for position: each plane gives one constraint n·p = c.
    // Normal equations A p = b with A = Σ w n nᵀ (symmetric, 6 entries) and b = Σ w c n.
    var A = [0, 0, 0, 0, 0, 0], b = [0, 0, 0];
    var axz = 0, bxz = 0, cxz = 0;         // wall-only XZ matrix, for the scale-free degeneracy test
    var usedWalls = 0;
    /** @param {Vec3} n @param {number} c @param {number} w */
    function addConstraint(n, c, w) {
      A[0] += w * n.x * n.x; A[1] += w * n.x * n.y; A[2] += w * n.x * n.z;
      A[3] += w * n.y * n.y; A[4] += w * n.y * n.z; A[5] += w * n.z * n.z;
      b[0] += w * c * n.x; b[1] += w * c * n.y; b[2] += w * c * n.z;
    }
    if (floorP && anchor.floor) addConstraint(floorP.normal, planeRHS(floorP, anchor.floor.offset), floorWeight);
    /** @type {Quat[]} */
    var votes = [];
    anchor.walls.forEach(function (wa) {
      var wp = byId[wa.id];
      if (!wp) return;                                   // this client didn't capture that wall — skip it
      var w = 1 / (nearBias + Math.abs(wa.offset));      // nearer walls (smaller |offset|) weigh more
      addConstraint(wp.normal, planeRHS(wp, wa.offset), w);
      var nx = wp.normal.x, nz = wp.normal.z;
      axz += w * nx * nx; bxz += w * nx * nz; cxz += w * nz * nz;
      votes.push(wallFrame(THREE, wp.normal, up).multiply(new THREE.Quaternion(wa.rel[0], wa.rel[1], wa.rel[2], wa.rel[3])));
      usedWalls++;
    });

    var used = { walls: usedWalls, floor: !!floorP };
    if (usedWalls < 2 || cond2(axz, bxz, cxz) < minCond)
      return { ok: false, position: null, quaternion: null,
               stat: "degenerate: walls=" + usedWalls + " cond=" + cond2(axz, bxz, cxz).toFixed(3), used: used };
    if (anchor.mode === "free" && !floorP)
      return { ok: false, position: null, quaternion: null, stat: "free: floor missing", used: used };

    var sol = solveSym3(A, b);
    if (!sol) return { ok: false, position: null, quaternion: null, stat: "singular", used: used };
    var p = new THREE.Vector3(sol.x[0], sol.x[1], sol.x[2]);
    // Grounded: pin Y exactly to the local floor (never float/sink) — the floor's stored offset IS the
    // height above it. XZ still comes from the wall solve above.
    if (anchor.mode === "grounded" && floorP && anchor.floor) p.y = floorP.point.y + anchor.floor.offset;

    var q = averageQuat(THREE, votes);
    if (anchor.mode === "grounded") q = twistAbout(THREE, q, up);   // yaw-only; discard any pitch/roll

    return { ok: true, position: p, quaternion: q,
             stat: "ok walls=" + usedWalls + (floorP ? "+floor" : "") + " mode=" + anchor.mode, used: used };
  }

  return { authorAnchor: authorAnchor, solveAnchor: solveAnchor,
           // exposed for unit tests / the Python-port parity checks:
           solveSym3: solveSym3, cond2: cond2, wallFrame: wallFrame, averageQuat: averageQuat,
           twistAbout: twistAbout, signedDist: signedDist };
});
