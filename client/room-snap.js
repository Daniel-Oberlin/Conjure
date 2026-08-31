// @ts-check
// Pure room-snapping geometry — extracted from the room-capture component so it can be unit-tested
// (tests/js/room-snap.test.js) independently of A-Frame/WebXR/DOM. Every function takes the THREE
// module as its first argument: the browser passes AFRAME.THREE, node tests pass require('three').
// No state, no DOM, no globals — just the math that turns captured planes into placed surfaces.
//
// TYPE-CHECKED with `// @ts-check` + JSDoc (a trial — `npm run typecheck`, tsconfig.json). No build step:
// the annotations are comments, the file still ships verbatim. The typedefs below capture the two shapes
// the math flows through, so vector/euler/tuple mixups (the class of bug we kept hitting) fail the check.

/**
 * @typedef {typeof import('three')} THREE_NS   the three.js module (browser: AFRAME.THREE; node: require('three'))
 * @typedef {import('three').Vector3} Vec3
 * @typedef {import('three').Quaternion} Quat
 * @typedef {import('three').Matrix4} Mat4
 */

/**
 * Constellation form — what register()/matchRef()/canonicalFrame() consume (a compact plane).
 * @typedef {Object} RefSurface
 * @property {string} [id]
 * @property {string} sem                                semantic ("wall", "window", …)
 * @property {[number, number]} ext                      extent [w, h] (metres)
 * @property {Vec3} pos                                  centre in the reference frame
 * @property {number} nyaw                               compass yaw of the surface normal (radians)
 * @property {"vertical"|"horizontal"} orient
 */

/**
 * Stored/broadcast surface entity — a-plane form (transform in degrees, extent under components.surface).
 * @typedef {Object} SurfaceEntity
 * @property {string} [id]
 * @property {{position?: number[], rotation?: number[]}} [transform]
 * @property {{surface?: {extent?: number[]}}} [components]
 * @property {{semantic?: string}} [meta]
 */

/**
 * Working surface during snapping — a-plane form plus the scratch ref-frame pose (_lp/_lq).
 * @typedef {Object} SnapSurface
 * @property {string} id
 * @property {string} semantic
 * @property {number[]} [extent]
 * @property {number[]} rotation                         euler degrees [x, y, z] (YXZ order)
 * @property {number[]} [position]
 * @property {Vec3} _lp                                  ref-frame position
 * @property {Quat} _lq                                  ref-frame orientation
 * @property {{x:number, y:number, w:number, h:number}[]} [holes]   openings cut into a wall
 * @property {boolean} [_recovered]                      reconstructed via anchor (§5.2) — project onto its wall plane
 * @property {string} [hostWall]                         for an inset: the wall id it belongs to (recorded by snapInsets)
 * @property {any} [debug]
 */

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else (/** @type {any} */ (root)).RoomSnap = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // A captured plane lies in its local X-Z plane (normal +Y); our <a-plane> is X-Y (normal +Z). Compose
  // a -90° X rotation so the rendered plane aligns with the captured one, then convert to euler degrees.
  // A-Frame applies rotations in YXZ order (NOT THREE's default XYZ) — using XYZ here renders walls/insets
  // up to ~48° off-square. See docs/specs/worlds-surfaces.md.
  /**
   * @param {THREE_NS} THREE
   * @param {{x:number, y:number, z:number, w:number}} q   quaternion-like (captured plane orientation)
   * @returns {number[]} euler degrees [x, y, z] in YXZ order
   */
  function eulerYXZ(THREE, q) {
    var quat = new THREE.Quaternion(q.x, q.y, q.z, q.w);
    quat.multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2));
    var e = new THREE.Euler().setFromQuaternion(quat, "YXZ");
    var d = THREE.MathUtils.radToDeg;
    return [d(e.x), d(e.y), d(e.z)];
  }

  /** @param {{x:number, z:number}} n  @returns {number} compass yaw of a horizontal normal */
  function yawOf(n) { return Math.atan2(n.x, n.z); }

  // Solve the single rigid yaw+translation transform mapping the newly detected planes (cur, in the
  // current refSpace) onto the persistent reference constellation (ref). Recovers how the Quest's frame
  // jumped using the room's own geometry — robust to the ~180° boundary flip because the yaw is read from
  // the SHIFT in surface-normal directions, needing no prior pairing. Returns {Tmat, stat}: Tmat is a
  // Matrix4 (refSpace → reference frame) when confident, else null (caller holds the last frame). `stat`
  // is a short diagnostic string.
  // Frame registration — recover the rigid transform (yaw about gravity + x/z translation) that maps the
  // CURRENT detected planes onto the persistent reference constellation, so surface ids survive a tracking
  // relocalization (boundary re-entry flips the frame ~167° + ~3 m; see docs/specs/spaces-geometry.md §4.1).
  //
  // A Hough/RANSAC-style VOTE, not a nearest-match: it must run before any correspondence is known and no
  // matter how far the frame jumped, so it never relies on proximity (the 0.5 m id match happens AFTER, in
  // conjure-client.js Pass B). The upstream trust gate guarantees a level floor, so only yaw + x/z
  // translation are free:
  //   1. yaw — histogram the normal-yaw delta over same-semantic, similar-size vertical pairs: a global
  //      rotation shifts every true pair by the same θ, so real pairs pile into one bin (top-3 peaks tried).
  //   2. translation — per candidate yaw, grid-vote the densest (ref.pos − R·cur.pos) over same-size pairs.
  //   3. score — count planes landing within 0.4 m of a same-semantic reference; accept the best only if
  //      >=4 inliers AND >=40%, else null (caller holds the last good frame). A genuinely different space
  //      yields no consensus, so a null doubles as a "not in this space" signal (specs/spaces-geometry.md §4.1).
  /**
   * The tunable robustness knobs for register()/selectSpace(). Any field may be overridden (from the
   * server-injected window.CONJURE_REG — see conjure-client.js); an absent/undefined field keeps the
   * default. These govern how tolerantly a GUEST locks onto the authority's shared room.
   * @typedef {Object} RegOpts
   * @property {number} [minCov]      min DISTINCT reference surfaces covered to accept a lock (default 4)
   * @property {number} [minCovFrac]  min fraction of the reference covered, 0..1 (default 0.3)
   * @property {number} [sizeTol]     how much LARGER (m) a detected plane may be than a reference (default 0.5)
   * @property {number} [inlierM]     max distance (m) a plane may sit from a same-kind reference to count (0.4)
   * @property {number} [yawPeaks]    candidate room rotations tried when solving orientation (default 5)
   */
  /**
   * @param {THREE_NS} THREE
   * @param {RefSurface[]} cur   the newly detected planes (current refSpace)
   * @param {RefSurface[]} ref   the persistent reference constellation
   * @param {RegOpts} [opts]     robustness overrides (default: the built-in constants)
   * @returns {{Tmat: Mat4|null, stat: string, cov: number, inl?: number,
   *            residuals?: {id: string|undefined, res: number, dist: number}[]}}
   *   `residuals` (winning transform only): per covered plane, its distance from the matched reference
   *   after the fit (`res`, m) + that reference's horizontal distance from the frame origin (`dist`, m).
   *   Diagnostic only — tells whether misalignment is uniform or grows with distance (a non-rigid map).
   */
  function register(THREE, cur, ref, opts) {
    var UP = new THREE.Vector3(0, 1, 0);
    if (ref.length < 3) return { Tmat: null, stat: "ref<3", cov: 0 };
    /** @param {number} a  @returns {number} */
    function wrap(a) { while (a > Math.PI) a -= 2 * Math.PI; while (a < -Math.PI) a += 2 * Math.PI; return a; }
    // Robustness for a GUEST's partial/extra plane set (specs/spaces.md §7, multi-user co-location). A guest
    // sees the room from a different vantage: some reference surfaces are MISSING (occluded) and there are
    // EXTRA planes (furniture/clutter) with no reference. Two ideas make the vote tolerate both:
    //  • size-compat is ASYMMETRIC — a detected plane may be a PARTIAL (smaller) view of a reference, so
    //    only reject one notably LARGER than its candidate reference (a bigger plane isn't a partial view).
    //  • acceptance scores DISTINCT reference surfaces COVERED (not detected-plane count): extras can't
    //    inflate it, fragmentation can't double-count it, and missing surfaces just lower coverage. We
    //    accept on coverage of the REFERENCE, so clutter never sinks an otherwise-solid lock.
    // The four thresholds + candidate-yaw count are tunable (opts) for two-headset guest testing.
    opts = opts || {};
    var SIZE_TOL = opts.sizeTol != null ? opts.sizeTol : 0.5;
    var MIN_COV = opts.minCov != null ? opts.minCov : 4;
    var MIN_COV_FRAC = opts.minCovFrac != null ? opts.minCovFrac : 0.3;
    var INLIER_M = opts.inlierM != null ? opts.inlierM : 0.4;
    var YAW_PEAKS = opts.yawPeaks != null ? opts.yawPeaks : 5;
    /** @param {RefSurface} r  @param {RefSurface} c  @returns {boolean} */
    function sizeCompat(r, c) { return c.ext[0] <= r.ext[0] + SIZE_TOL && c.ext[1] <= r.ext[1] + SIZE_TOL; }
    // Step 1 — candidate yaw(s): histogram the normal-yaw delta over same-semantic, similar-size vertical
    // pairs; every true correspondence votes for the same delta, so the real yaw dominates.
    /** @type {number[]} */
    var deltas = [];
    cur.forEach(function (c) {
      if (c.orient !== "vertical") return;
      ref.forEach(function (r) {
        if (r.orient !== "vertical" || r.sem !== c.sem || !sizeCompat(r, c)) return;
        deltas.push(wrap(r.nyaw - c.nyaw));
      });
    });
    if (deltas.length < 3) return { Tmat: null, stat: "dlt=" + deltas.length, cov: 0 };
    var bin = Math.PI / 30;                                     // 6° bins
    /** @type {Record<string, number[]>} */
    var hist = {};
    deltas.forEach(function (d) { var b = Math.round(d / bin); (hist[b] = hist[b] || []).push(d); });
    var keys = Object.keys(hist).sort(function (a, b) { return hist[b].length - hist[a].length; });
    var thetas = keys.slice(0, YAW_PEAKS).map(function (k) {    // top-N peaks (clutter can dilute the true one)
      var s = 0, c2 = 0; hist[k].forEach(function (d) { s += Math.sin(d); c2 += Math.cos(d); });
      return Math.atan2(s, c2);
    });
    // Step 2/3 — for each candidate yaw, solve translation (densest cell of ref.pos − R·cur.pos over
    // same-size pairs) and score by how many planes land on a same-semantic reference surface.
    var best = /** @type {{Tmat: Mat4, cov: number, inl: number, res: {id: string|undefined, res: number, dist: number}[]}|null} */ (null);
    thetas.forEach(function (theta) {
      var qy = new THREE.Quaternion().setFromAxisAngle(UP, theta);
      // Same-FACING gate: after applying this candidate yaw, a true correspondence's normal aligns with its
      // reference (c.nyaw + theta ≈ r.nyaw). The two faces of a shared partition wall have centers ~0.5 m
      // apart and OPPOSITE normals, so without this a wall can pair with / cover the wrong face — polluting
      // the translation vote and coverage, which makes the lock jitter frame-to-frame. Only vertical faces
      // are gated (floor/ceiling separate by semantic). ~60° tolerance (cos > 0.5) leaves ample noise room.
      /** @param {RefSurface} c  @param {RefSurface} r  @returns {boolean} */
      function sameFacing(c, r) {
        return !(c.orient === "vertical" && r.orient === "vertical" && Math.cos((c.nyaw + theta) - r.nyaw) < 0.5);
      }
      /** @type {Record<string, {sx:number, sz:number, n:number}>} */
      var grid = {};
      var bestCell = /** @type {{sx:number, sz:number, n:number}|null} */ (null);
      var bestN = 0;
      cur.forEach(function (c) {
        var rc = c.pos.clone().applyQuaternion(qy);
        ref.forEach(function (r) {
          if (r.sem !== c.sem || !sizeCompat(r, c) || !sameFacing(c, r)) return;
          var tx = r.pos.x - rc.x, tz = r.pos.z - rc.z;
          var k = Math.round(tx / 0.25) + "," + Math.round(tz / 0.25);
          var cell = grid[k] || (grid[k] = { sx: 0, sz: 0, n: 0 });
          cell.sx += tx; cell.sz += tz; cell.n++;
          if (cell.n > bestN) { bestN = cell.n; bestCell = cell; }
        });
      });
      if (!bestCell) return;
      var Tmat = new THREE.Matrix4().compose(
        new THREE.Vector3(bestCell.sx / bestCell.n, 0, bestCell.sz / bestCell.n), qy, new THREE.Vector3(1, 1, 1));
      /** @type {Set<RefSurface>} */
      var claimed = new Set();   // distinct reference surfaces covered (extras/fragmentation don't inflate)
      var rawInl = 0;
      /** @type {{id: string|undefined, res: number, dist: number}[]} */
      var res = [];              // per-covered-plane residual + the reference's distance from origin (diag)
      cur.forEach(function (c) {
        var tp = c.pos.clone().applyMatrix4(Tmat), bd = INLIER_M;
        var hit = /** @type {RefSurface|null} */ (null);
        ref.forEach(function (r) { if (r.sem === c.sem && sameFacing(c, r)) { var d = tp.distanceTo(r.pos); if (d < bd) { bd = d; hit = r; } } });
        if (hit) { claimed.add(hit); rawInl++; res.push({ id: hit.id, res: bd, dist: Math.hypot(hit.pos.x, hit.pos.z) }); }
      });
      var cov = claimed.size;
      if (!best || cov > best.cov) best = { Tmat: Tmat, cov: cov, inl: rawInl, res: res };
    });
    var cov = best ? best.cov : 0;
    var stat = "cov=" + cov + "/" + ref.length + " inl=" + (best ? best.inl : 0) + "/" + cur.length + " dlt=" + deltas.length;
    // Accept on DISTINCT reference COVERAGE (not fraction-of-detected): enough of the known room explained
    // by ONE transform. Robust to EXTRA detected planes (absent from the formula) and MISSING ones (need
    // only a fraction of the reference). A genuinely different space can't cover ≥MIN_COV surfaces of the
    // reference under one consistent transform ⇒ null ("not in this space", specs/spaces-geometry.md §4.1).
    if (!best || cov < MIN_COV || cov < MIN_COV_FRAC * ref.length) return { Tmat: null, stat: stat, cov: cov, residuals: best ? best.res : [] };
    // Append the SOLVED transform (yaw about gravity + translation) so diagnostics can tell whether a
    // relocalization actually changed the frame (yaw jumps) or registration stayed put while the world
    // shifted. Matrix4 is column-major: e[0]=cosθ, e[8]=sinθ for the Y rotation; e[12],e[14]=tx,tz.
    var e = best.Tmat.elements;
    stat += " yaw=" + Math.round(Math.atan2(e[8], e[0]) * 180 / Math.PI) + "° t=("
      + e[12].toFixed(2) + "," + e[14].toFixed(2) + ")";
    return { Tmat: best.Tmat, stat: stat, cov: cov, inl: best.inl, residuals: best.res };
  }

  // Convert ONE stored/broadcast surface entity (a-plane form: transform.position/rotation in degrees,
  // components.surface.extent, meta.semantic) into the compact constellation form register() consumes
  // ({id, sem, ext, pos:Vector3, nyaw, orient}). The a-plane normal is its local +Z. Shared by the client's
  // reference-seeding and by selectSpace below, so the two never diverge.
  /**
   * @param {THREE_NS} THREE
   * @param {SurfaceEntity} e
   * @returns {RefSurface}
   */
  function surfaceToRef(THREE, e) {
    var Z = new THREE.Vector3(0, 0, 1), d2r = THREE.MathUtils.degToRad;
    var t = e.transform || {}, p = t.position || [0, 0, 0], r = t.rotation || [0, 0, 0];
    // A-Frame stores/renders euler in YXZ order (surfaces are written via eulerYXZ). Reconstruct with the
    // SAME order — reading YXZ-stored angles as XYZ corrupts the normal for any multi-axis (tilted) surface
    // (e.g. a picture frame), which made the same-facing gate reject it and mint a new id every session.
    var q = new THREE.Quaternion().setFromEuler(new THREE.Euler(d2r(r[0]), d2r(r[1]), d2r(r[2]), "YXZ"));
    var nrm = Z.clone().applyQuaternion(q);
    var ex = (e.components && e.components.surface && e.components.surface.extent) || [1, 1];
    return { id: e.id, sem: (e.meta && e.meta.semantic) || "surface", ext: [ex[0], ex[1]],
      pos: new THREE.Vector3(p[0], p[1], p[2]), nyaw: Math.atan2(nrm.x, nrm.z),
      orient: Math.abs(nrm.y) > 0.7 ? "horizontal" : "vertical" };
  }

  // Two-stage space selection, fine stage (specs/spaces.md §6, D2/D7). Geolocation already narrowed the
  // field to a few geo-near candidate spaces; this picks WHICH one the headset is physically in by trying
  // to register() the live capture (cur) against each candidate's stored constellation and keeping the one
  // with the highest reference COVERAGE. A candidate only qualifies if register() confidently locks (its
  // coverage clears MIN_COV/MIN_COV_FRAC) — so a null return means "none of these; you're somewhere new".
  // Robust to the sparse-capture bug by construction: it's the geometric vote, not a surface-count guess,
  // that decides, and a too-thin `cur` simply fails to cover any candidate (caller retries as capture grows).
  //   candidates: [{owner, name, surfaces:[a-plane entity]}]  →  {index, owner, name, cov, stat} | null
  /**
   * @param {THREE_NS} THREE
   * @param {RefSurface[]} cur
   * @param {{owner: string, name: string, surfaces?: SurfaceEntity[]}[]} candidates
   * @param {RegOpts} [opts]     same robustness overrides as register() (admission uses the same vote)
   * @returns {{index: number, owner: string, name: string, cov: number, stat: string}|null}
   */
  function selectSpace(THREE, cur, candidates, opts) {
    var best = /** @type {{index: number, owner: string, name: string, cov: number, stat: string}|null} */ (null);
    (candidates || []).forEach(function (cand, i) {
      var ref = (cand.surfaces || []).map(function (e) { return surfaceToRef(THREE, e); });
      var reg = register(THREE, cur, ref, opts);
      if (reg.Tmat && (!best || reg.cov > best.cov)) {
        best = { index: i, owner: cand.owner, name: cand.name, cov: reg.cov, stat: reg.stat };
      }
    });
    return best;
  }

  // Assign a detected plane to the reference surface it re-inherits its id from (conjure-client Pass B).
  // Same semantic, nearest CENTER within 0.5 m — but ALSO SAME-FACING. Two parallel walls that share a
  // partition are the two faces of one wall (one per room): their centers sit ~0.5 m apart and their
  // normals point OPPOSITE. A center-only match can therefore swap them — a new-room wall grabbing the
  // old-room wall's id (and colour/style), the old wall then grabbing the new one — especially when
  // capture noise nudges a center toward the wrong face. Requiring the normals to agree (within ~60°,
  // cos > 0.5) keeps the two faces distinct. `claimed` is the Set of refs already taken this pass; returns
  // the chosen ref, or null ⇒ a genuinely new surface. (Only vertical faces are normal-gated; a floor and
  // ceiling are already separated by semantic + height.)
  /**
   * @param {{pos: Vec3, nyaw: number, sem: string, orient: string}} cand
   * @param {RefSurface[]} refs
   * @param {Set<RefSurface>} [claimed]   refs already taken this pass
   * @returns {RefSurface|null}
   */
  function matchRef(cand, refs, claimed) {
    // Prefer the nearest SAME-FACING reference within 0.5 m. If none faces the same way, fall back to a
    // near-COINCIDENT (<0.15 m) antiparallel one — the SAME physical surface seen with an opposite normal.
    // This is REQUIRED for wall art: a mounted picture is an object whose live normal faces the viewer
    // (INWARD), 180° from the wall's outward orientation it's stored with — so its own detection looks
    // antiparallel to its ref and would re-mint a new id every session without this. The two faces of a
    // partition wall sit ~0.4 m apart (not coincident), so they stay DISTINCT — the id-swap fix is intact.
    /** @type {RefSurface|null} */
    var same = null;
    /** @type {RefSurface|null} */
    var flip = null;
    var dSame = 0.5, dFlip = 0.15;
    refs.forEach(function (r) {
      if (r.sem !== cand.sem || (claimed && claimed.has(r))) return;
      var d = cand.pos.distanceTo(r.pos);
      var antiparallel = cand.orient === "vertical" && r.orient === "vertical" && Math.cos(cand.nyaw - r.nyaw) < 0.5;
      if (!antiparallel && d < dSame) { dSame = d; same = r; }
      else if (antiparallel && d < dFlip) { dFlip = d; flip = r; }
    });
    return same || flip;
  }

  // Identify a WALL by its PLANE, not its centroid (docs/specs/spaces-geometry.md §4.2/§4.3). A wall's
  // centroid is a scan artifact — it's the centre of whatever rectangle the Quest captured, so it slides
  // ALONG the wall between captures and between devices. Matching walls by centroid therefore re-mints an
  // id (losing style, and shifting every inset keyed to that wall) whenever the captured extent changes.
  // Instead a wall is its plane: same-facing normal + perpendicular offset from the origin, plus an
  // along-line OVERLAP guard so two DISTINCT walls on the same line & offset (a segment past a doorway)
  // don't collapse to one id. Conservative by design — a wrong wall match is the §10 catastrophe (content
  // on the wrong wall); a missed match only mints a recoverable duplicate. Floor/ceiling keep matchRef
  // (centroid+semantic is right for horizontals); insets use matchInset (identity by host_wall+slot).
  /**
   * Tunable knobs for matchWall (server-injected window.CONJURE_WALL — see conjure-client.js). Absent
   * fields keep the defaults.
   * @typedef {Object} WallOpts
   * @property {number} [perpTol]      max plane-offset gap (m) to call two walls the same plane (default 0.15)
   * @property {number} [yawTol]       max normal-yaw difference (rad) for a wall match (default 30° — tighter
   *                                   than matchRef's 60° same-facing gate: a wall's normal is stable across scans)
   * @property {number} [overlapSlop]  max along-line gap (m) between the two walls' spans and still one wall (0.3)
   */
  /**
   * @param {{pos: Vec3, nyaw: number, sem: string, orient: string, ext: [number, number]}} cand
   * @param {RefSurface[]} refs
   * @param {Set<RefSurface>} [claimed]
   * @param {WallOpts} [opts]
   * @returns {RefSurface|null}
   */
  function matchWall(cand, refs, claimed, opts) {
    opts = opts || {};
    var PERP_TOL = opts.perpTol != null ? opts.perpTol : 0.15;
    var YAW_TOL = opts.yawTol != null ? opts.yawTol : Math.PI / 6;   // 30°
    var OVERLAP_SLOP = opts.overlapSlop != null ? opts.overlapSlop : 0.3;
    if (cand.orient !== "vertical") return null;                     // walls are vertical
    // Candidate's plane basis from its own normal (n = outward, t = along the wall, both in plan view).
    var cnx = Math.sin(cand.nyaw), cnz = Math.cos(cand.nyaw);        // yawOf(n) = atan2(n.x, n.z)
    var cHw = ((cand.ext && cand.ext[0]) || 0) / 2;
    var best = /** @type {RefSurface|null} */ (null), bestScore = Infinity;
    refs.forEach(function (r) {
      if (r.sem !== cand.sem || r.orient !== "vertical" || (claimed && claimed.has(r))) return;
      // 1. same-facing / near-parallel — a wall's outward normal is physically fixed, so demand a tight
      //    agreement (keeps the two anti-parallel faces of a partition distinct, like matchRef's gate).
      var dyaw = Math.abs(Math.atan2(Math.sin(cand.nyaw - r.nyaw), Math.cos(cand.nyaw - r.nyaw)));
      if (dyaw > YAW_TOL) return;
      // 2. coincident plane — project BOTH centres onto the ref's normal; their offsets must nearly match
      //    (perpendicular gap). Invariant to sliding either centre ALONG the wall (that's the whole point).
      var rnx = Math.sin(r.nyaw), rnz = Math.cos(r.nyaw);
      var perp = Math.abs((cand.pos.x - r.pos.x) * rnx + (cand.pos.z - r.pos.z) * rnz);
      if (perp > PERP_TOL) return;
      // 3. along-line overlap guard — spans on the width axis t=(n.z,-n.x) must overlap (gap < slop), else
      //    these are two colinear-but-separate walls, not one wall captured differently.
      var tx = rnz, tz = -rnx;
      var ca = cand.pos.x * tx + cand.pos.z * tz, ra = r.pos.x * tx + r.pos.z * tz;
      var rHw = ((r.ext && r.ext[0]) || 0) / 2;
      var gap = Math.abs(ca - ra) - (cHw + rHw);                     // <0 ⇒ spans overlap
      if (gap > OVERLAP_SLOP) return;
      var score = perp + Math.max(0, gap);                           // both in metres; prefer the tightest plane
      if (score < bestScore) { bestScore = score; best = r; }
    });
    return best;
  }

  // On-the-fly CANONICAL frame from live geometry — for VOID/outdoor worlds not tied to a stored space
  // (nothing to register against). Derives a deterministic frame from the room's OWN planes, INVARIANT to
  // the session's arbitrary tracking-origin yaw, so revisiting the same physical room recovers the same
  // (arbitrary-but-consistent) orientation — no stored space, no identification, no persistent-anchor API.
  // up = gravity (the trust gate guarantees a level floor); the wall grid gives the axis; the LARGEST wall
  // picks the canonical forward; the wall centroid is the origin. Returns {Tmat: refSpace→canonical, stat}
  // or null. KNOWN LIMIT: a symmetric room (no unique largest wall) has no unique canonical orientation —
  // same ambiguity as register()'s 180° flip, but low-stakes for a void world (only the skybox yaw moves,
  // no content pinned to real walls). Partial-capture stability (a fuller view winning) is a follow-up.
  /**
   * @param {THREE_NS} THREE
   * @param {RefSurface[]} cur
   * @returns {{Tmat: Mat4|null, stat: string}}
   */
  function canonicalFrame(THREE, cur) {
    var UP = new THREE.Vector3(0, 1, 0);
    var walls = cur.filter(function (c) { return c.orient === "vertical"; });
    if (walls.length < 2) return { Tmat: null, stat: "walls=" + walls.length };
    // Wall-grid axis (mod 90°), area-weighted so big/accurate walls dominate — the 4θ sum handles the 90° wrap.
    var s4 = 0, c4 = 0;
    walls.forEach(function (w) { var a = w.ext[0] * w.ext[1]; s4 += a * Math.sin(4 * w.nyaw); c4 += a * Math.cos(4 * w.nyaw); });
    var grid = Math.atan2(s4, c4) / 4;
    // Canonical forward = the grid direction nearest the LARGEST wall's outward normal (unique when one wall
    // is biggest). Because it's the nearest of the full {grid + k·90°} set, it rotates rigidly with the
    // session frame — so theta shifts exactly by the session yaw, which is what makes the frame invariant.
    var big = walls[0];
    walls.forEach(function (w) { if (w.ext[0] * w.ext[1] > big.ext[0] * big.ext[1]) big = w; });
    var HALF_PI = Math.PI / 2, theta = grid + Math.round((big.nyaw - grid) / HALF_PI) * HALF_PI;
    var c = new THREE.Vector3();
    walls.forEach(function (w) { c.add(w.pos); });
    c.multiplyScalar(1 / walls.length);
    c.y = 0;   // canonicalize the HORIZONTAL center + yaw only (like register) — keep the FLOOR at the floor;
               // wall positions sit at mid-height, so translating by their y would float the world ~1.2 m up
    // Tmat: rotate refSpace by -theta about gravity (the forward wall's normal → +Z), then bring the
    // centroid to the origin. The room's canonical pose is identical every session ⇒ consistent orientation.
    var R = new THREE.Quaternion().setFromAxisAngle(UP, -theta);
    var Tmat = new THREE.Matrix4().compose(c.clone().applyQuaternion(R).negate(), R, new THREE.Vector3(1, 1, 1));
    return { Tmat: Tmat, stat: "walls=" + walls.length + " grid=" + Math.round(grid * 180 / Math.PI)
      + "° theta=" + Math.round(theta * 180 / Math.PI) + "°" };
  }

  // Build the plan-view (X-Z) segment for each wall: centre, unit horizontal normal, half-width, the two
  // endpoints. Shared by wallCorners + joinCorners so the corners the anchor sees are exactly the ones the
  // snap uses.
  /**
   * @typedef {{s: SnapSurface, cx: number, cz: number, nx: number, nz: number, hw: number, cy: number,
   *            ends: {x: number, z: number}[]}} WallSeg
   */
  /** @param {THREE_NS} THREE  @param {SnapSurface[]} surfaces  @returns {WallSeg[]} */
  function wallSegs(THREE, surfaces) {
    return surfaces.filter(function (s) { return s.semantic === "wall"; }).map(function (s) {
      var n = new THREE.Vector3(0, 1, 0).applyQuaternion(s._lq);
      var L = Math.hypot(n.x, n.z) || 1, nx = n.x / L, nz = n.z / L;   // unit horizontal normal
      var hw = ((s.extent && s.extent[0]) || 0) / 2, cx = s._lp.x, cz = s._lp.z;
      var tx = nz, tz = -nx;                                     // width axis ⟂ normal (in plan view)
      return /** @type {WallSeg} */ ({ s: s, cx: cx, cz: cz, nx: nx, nz: nz, hw: hw, cy: s._lp.y,
               ends: [{ x: cx + tx * hw, z: cz + tz * hw }, { x: cx - tx * hw, z: cz - tz * hw }] });
    });
  }

  // The structural CORNER points of each wall (wall∩wall intersections), keyed by wall id. A corner is a
  // SHARED feature — both a device and a guest derive the same physical point from where two walls actually
  // meet, independent of how much of either wall each captured — so it's the reference an inset's along-wall
  // place is anchored to (§5.3), unlike the scan-artifact centroid. For each ~perpendicular wall pair whose
  // planes cross on a vertical line within GAP of BOTH walls' nearest ends, record that intersection on each
  // wall's near end (keeping the closest per end → ≤2 corners/wall), tagged with the PARTNER wall's id (the
  // stable name an inset stores to find the same corner on any capture). Collinear/parallel walls (a doorway
  // gap, opposite walls) and T-junctions (only one wall ends there) contribute none. Read-only.
  /**
   * @typedef {{x: number, z: number, partner: string, end: number}} WallCorner
   * @param {THREE_NS} THREE
   * @param {SnapSurface[]} surfaces
   * @returns {Map<string, WallCorner[]>}
   */
  function wallCorners(THREE, surfaces) {
    var GAP = 0.25;                                             // only join gaps up to 25 cm (matches joinCorners)
    var W = wallSegs(THREE, surfaces);
    /** @param {WallSeg} w  @param {number} px  @param {number} pz  @returns {{i: number, d: number}} */
    function nearest(w, px, pz) {
      var d0 = Math.hypot(w.ends[0].x - px, w.ends[0].z - pz);
      var d1 = Math.hypot(w.ends[1].x - px, w.ends[1].z - pz);
      return d1 < d0 ? { i: 1, d: d1 } : { i: 0, d: d0 };
    }
    /** @type {(({x:number,z:number,partner:string,d:number}|null)[])[]} */
    var slots = W.map(function () { return [null, null]; });   // closest corner per [end0, end1] per wall
    for (var i = 0; i < W.length; i++) {
      for (var j = i + 1; j < W.length; j++) {
        var a = W[i], b = W[j];
        if (Math.abs(a.cy - b.cy) > 0.5) continue;               // different wall band
        if (Math.abs(a.nx * b.nx + a.nz * b.nz) > 0.3) continue; // normals not ⟂ ⇒ not a corner
        var det = a.nx * b.nz - a.nz * b.nx;                     // intersect the two wall lines (X·n = c·n)
        if (Math.abs(det) < 1e-3) continue;
        var da = a.cx * a.nx + a.cz * a.nz, db = b.cx * b.nx + b.cz * b.nz;
        var px = (da * b.nz - db * a.nz) / det, pz = (a.nx * db - b.nx * da) / det;
        var ka = nearest(a, px, pz), kb = nearest(b, px, pz);
        if (ka.d > GAP || kb.d > GAP) continue;                  // both ends must reach this corner
        var ta = slots[i][ka.i], tb = slots[j][kb.i];            // keep the CLOSEST corner per end
        if (!ta || ka.d < ta.d) slots[i][ka.i] = { x: px, z: pz, partner: b.s.id, d: ka.d };
        if (!tb || kb.d < tb.d) slots[j][kb.i] = { x: px, z: pz, partner: a.s.id, d: kb.d };
      }
    }
    /** @type {Map<string, WallCorner[]>} */
    var map = new Map();
    W.forEach(function (w, wi) {
      /** @type {WallCorner[]} */
      var arr = [];
      [0, 1].forEach(function (e) {
        var c = slots[wi][e];
        if (c) arr.push({ x: c.x, z: c.z, partner: c.partner, end: e });
      });
      map.set(w.s.id, arr);
    });
    return map;
  }

  // Join wall corners. WebXR fits each wall independently, so two walls that should meet at a corner often
  // stop a few cm short (or overshoot). Using wallCorners' shared intersection points, snap each wall's end
  // that reaches a corner exactly onto it — closing the corner with a small extend/trim. Leaves parallel
  // walls and T-junctions alone. Mutates wall _lp / extent / position in place.
  /**
   * @param {THREE_NS} THREE
   * @param {SnapSurface[]} surfaces
   * @returns {void}
   */
  function joinCorners(THREE, surfaces) {
    var W = wallSegs(THREE, surfaces);
    var corners = wallCorners(THREE, surfaces);
    W.forEach(function (w) {
      var cs = corners.get(w.s.id) || [];
      if (!cs.length) return;
      var E0 = w.ends[0], E1 = w.ends[1];
      cs.forEach(function (c) { if (c.end === 0) E0 = { x: c.x, z: c.z }; else E1 = { x: c.x, z: c.z }; });
      var cx = (E0.x + E1.x) / 2, cz = (E0.z + E1.z) / 2;
      w.s._lp.x = cx; w.s._lp.z = cz;
      w.s.extent = [Math.hypot(E1.x - E0.x, E1.z - E0.z), (w.s.extent && w.s.extent[1]) || 0];
      w.s.position = [cx, w.s._lp.y, cz];
    });
  }

  // Seal each wall's TOP to the ceiling plane above it and its BOTTOM to the floor below it — the vertical
  // analogue of joinCorners (which only closes wall∩wall SIDES). The Quest often fits a wall a few mm–cm short
  // of the ceiling/floor, which is invisible in wireframe but shows as an open slit ("outside shows through")
  // once fills are solid. This snaps the wall's vertical extent onto those planes so the shell seals.
  //
  // VERTICAL ONLY: mutates a wall's centre height (`_lp.y` / `position[1]`) and height (`extent[1]`). The
  // wall's PLANE — normal, horizontal offset, and width (`extent[0]`) — is untouched, so registration and
  // plane-relative anchors (which use the horizontal plane + floor/ceiling edges) are unaffected; if anything
  // the wall now matches the floor/ceiling edge references those anchors already assume.
  //
  // Guarded by `tol`: a wall edge is snapped ONLY if it is already within `tol` of the plane, so a genuine
  // partial/knee wall — or a wall in a room whose ceiling wasn't captured — is left alone (a 29 cm-short wall
  // won't be stretched to a 2.7 m ceiling). Multi-room safe: a wall seals to the ceiling/floor whose FOOTPRINT
  // covers it — and for a wall shared between two rooms, to the HIGHEST covering ceiling (and LOWEST covering
  // floor), so it reaches the taller room and only pokes hidden into the shorter (nearest-by-centre would seal
  // it to the wrong room's ceiling and leave a slit). Runs BEFORE snapInsets so door/window holes — placed
  // relative to the wall centre — are computed against the sealed wall (no separate hole compensation).
  // Mutates in place.
  // A wall on a shared room boundary counts as under BOTH adjoining rooms' footprints.
  var COVER_MARGIN = 0.3;

  /** @type {Record<string, number>} */
  var INSET_SEM = { "door": 1, "window": 1, "wall art": 1 };

  // Does a horizontal surface `h`'s footprint (its rectangle, grown by `margin`) cover the plan point
  // (wx,wz)? Project the point into h's own axes (its raw-plane local X and Z, both horizontal for a
  // floor/ceiling) so a rotated/non-axis-aligned room is handled correctly. Shared by sealWalls (which
  // room does this wall reach into) and heightCensus (which room is this wall's height measured against) —
  // one definition, so the seal and the diagnostic can never disagree about which room a wall is in.
  /**
   * @param {THREE_NS} THREE  @param {SnapSurface} h  @param {number} wx  @param {number} wz
   * @param {number} [margin]  @returns {boolean}
   */
  function covers(THREE, h, wx, wz, margin) {
    if (!h._lp || !h._lq) return false;
    var m = margin == null ? COVER_MARGIN : margin;
    var ax = new THREE.Vector3(1, 0, 0).applyQuaternion(h._lq);
    var az = new THREE.Vector3(0, 0, 1).applyQuaternion(h._lq);
    var dx = wx - h._lp.x, dz = wz - h._lp.z;
    var u = dx * ax.x + dz * ax.z, v = dx * az.x + dz * az.z;
    var e = h.extent || [0, 0];
    return Math.abs(u) <= (e[0] || 0) / 2 + m && Math.abs(v) <= (e[1] || 0) / 2 + m;
  }

  /**
   * @param {THREE_NS} THREE
   * @param {SnapSurface[]} surfaces
   * @param {number} [tol]   max gap (m) between a wall edge and its plane to still snap (default 0.15; <=0 = off)
   * @returns {void}
   */
  function sealWalls(THREE, surfaces, tol) {
    var T = tol == null ? 0.15 : tol;
    if (!(T > 0)) return;
    var ceils = surfaces.filter(function (s) { return s.semantic === "ceiling" && s._lp && s._lq && s.extent; });
    var floors = surfaces.filter(function (s) { return s.semantic === "floor" && s._lp && s._lq && s.extent; });
    surfaces.forEach(function (s) {
      if (s.semantic !== "wall" || !s._lp || !s.extent) return;
      var cy = s._lp.y, h = s.extent[1] || 0, top = cy + h / 2, bot = cy - h / 2;
      // TOP → the HIGHEST ceiling that COVERS the wall and whose plane is within tol of the top. Highest so a
      // wall shared between two rooms of slightly different ceiling height reaches the TALLER one and merely
      // pokes (hidden, above the shorter ceiling) rather than leaving a visible slit under the taller. Only
      // raised, never lowered: a wall already poking above a ceiling is hidden by it, so no need to trim.
      var newTop = top;
      ceils.forEach(function (c) {
        if (c._lp.y > newTop && Math.abs(c._lp.y - top) <= T && covers(THREE, c, s._lp.x, s._lp.z)) newTop = c._lp.y;
      });
      // BOTTOM → symmetric: the LOWEST covering floor within tol (reach the lower floor; poke hidden below a
      // higher one). Only lowered.
      var newBot = bot;
      floors.forEach(function (f) {
        if (f._lp.y < newBot && Math.abs(f._lp.y - bot) <= T && covers(THREE, f, s._lp.x, s._lp.z)) newBot = f._lp.y;
      });
      if (newTop === top && newBot === bot) return;             // nothing covering within tol → leave it alone
      var nh = newTop - newBot;
      if (nh <= 0) return;                                      // degenerate → skip
      var nc = (newTop + newBot) / 2;
      s._lp.y = nc;
      s.extent = [s.extent[0], nh];
      if (s.position) s.position = [s.position[0], nc, s.position[2]];
    });
  }

  // Author an inset's CORNER-RELATIVE anchor (§5.3): its place on the wall expressed as distances to
  // SHARED structural features — the host wall's corner points (along-wall) and the floor/ceiling edges
  // (vertical) — never the wall's scan-artifact centroid. Any client reconstructs the same physical spot
  // from its OWN captured corners/edges (reconstructInset), so a guest whose wall scan centres differently
  // still lands the inset right. Perpendicular depth + orientation are NOT stored — snapInsets pins the
  // uniform standoff and adopts the wall's orientation. Pure; call after joinCorners has settled the wall.
  /**
   * @typedef {{corner: string, dist: number}} AlongRef      signed along-wall distance from a partner corner
   * @typedef {{edge: "floor"|"ceiling", dist: number}} VertRef
   * @typedef {{along: AlongRef[], vertical: VertRef[], fallback: string|null}} InsetAnchor
   * @param {THREE_NS} THREE
   * @param {SnapSurface} inset       the inset (uses its ref-frame centre _lp)
   * @param {SnapSurface} wall        its host wall (uses _lp/_lq)
   * @param {WallCorner[]} corners    the host wall's corners (from wallCorners)
   * @param {number|null} floorY      world Y of the wall∩floor edge, or null if no floor captured
   * @param {number|null} ceilY       world Y of the wall∩ceiling edge, or null
   * @returns {InsetAnchor}
   */
  function authorInsetAnchor(THREE, inset, wall, corners, floorY, ceilY) {
    corners = corners || [];
    var n = new THREE.Vector3(0, 1, 0).applyQuaternion(wall._lq);
    var L = Math.hypot(n.x, n.z) || 1, nx = n.x / L, nz = n.z / L, tx = nz, tz = -nx;   // width axis
    var c = wall._lp, ic = inset._lp;
    var a = (ic.x - c.x) * tx + (ic.z - c.z) * tz;             // inset's along-wall coordinate
    /** @type {AlongRef[]} */
    var along = corners.map(function (cor) {
      var ak = (cor.x - c.x) * tx + (cor.z - c.z) * tz;
      return { corner: cor.partner, dist: a - ak };             // signed (t is shared once wall normals agree)
    });
    /** @type {VertRef[]} */
    var vertical = [];
    var fb = [];
    if (floorY != null) vertical.push({ edge: "floor", dist: ic.y - floorY });
    if (ceilY != null) vertical.push({ edge: "ceiling", dist: ceilY - ic.y });
    if (!along.length) fb.push("freestanding");                 // no captured corner → guest-misplacement risk
    if (!vertical.length) fb.push("no-vertical-ref");
    return { along: along, vertical: vertical, fallback: fb.length ? fb.join("+") : null };
  }

  // Reconstruct an inset's centre from its corner-relative anchor against THIS client's live wall (§5.3).
  // Solves two 1-D coordinates: along-wall from the stored corner distances (2 present → mean = 1-D
  // least-squares, robust to a differently-captured wall length; 1 → direct; 0 → wall-centre fallback,
  // flagged) and height from the floor/ceiling edge distances (same 2→1→0). Depth/orientation are the
  // wall's (snapInsets pins the standoff). Pure. Returns null only if the wall itself is unusable.
  /**
   * @param {THREE_NS} THREE
   * @param {SnapSurface} wall
   * @param {WallCorner[]} corners
   * @param {number|null} floorY
   * @param {number|null} ceilY
   * @param {InsetAnchor} anchor
   * @returns {{position: Vec3, along: number, fallback: string|null}|null}
   */
  function reconstructInset(THREE, wall, corners, floorY, ceilY, anchor) {
    if (!wall || !wall._lp || !wall._lq || !anchor) return null;
    corners = corners || [];
    var n = new THREE.Vector3(0, 1, 0).applyQuaternion(wall._lq);
    var L = Math.hypot(n.x, n.z) || 1, nx = n.x / L, nz = n.z / L, tx = nz, tz = -nx;
    var c = wall._lp, fb = [];
    /** @type {number[]} */
    var aEst = [];
    (anchor.along || []).forEach(function (ar) {
      for (var k = 0; k < corners.length; k++) {
        if (corners[k].partner === ar.corner) {
          var ak = (corners[k].x - c.x) * tx + (corners[k].z - c.z) * tz;
          aEst.push(ak + ar.dist);
          break;
        }
      }
    });
    var along;
    if (aEst.length) along = aEst.reduce(function (s, v) { return s + v; }, 0) / aEst.length;
    else { along = 0; fb.push("along:wall-centre"); }
    /** @type {number[]} */
    var yEst = [];
    (anchor.vertical || []).forEach(function (vr) {
      if (vr.edge === "floor" && floorY != null) yEst.push(floorY + vr.dist);
      else if (vr.edge === "ceiling" && ceilY != null) yEst.push(ceilY - vr.dist);
    });
    var y;
    if (yEst.length) y = yEst.reduce(function (s, v) { return s + v; }, 0) / yEst.length;
    else { y = c.y; fb.push("vertical:wall-centre"); }
    return { position: new THREE.Vector3(c.x + tx * along, y, c.z + tz * along), along: along,
             fallback: fb.length ? fb.join("+") : null };
  }

  // The along-wall coordinate of a point (world x,z) in a wall's plan-view frame: its signed distance
  // along the wall's width axis t=(n.z,-n.x) from the wall centre. The ordinal that defines an inset's
  // SLOT (§5.3 L3) and the axis matchInset resolves identity in. Shared with author/reconstruct so all
  // three agree on the coordinate.
  /** @param {THREE_NS} THREE  @param {SnapSurface} wall  @param {number} x  @param {number} z  @returns {number} */
  function insetAlong(THREE, wall, x, z) {
    var n = new THREE.Vector3(0, 1, 0).applyQuaternion(wall._lq);
    var L = Math.hypot(n.x, n.z) || 1, nx = n.x / L, nz = n.z / L;
    return (x - wall._lp.x) * nz + (z - wall._lp.z) * (-nx);
  }

  // Which wall an inset belongs to, derived geometrically: the nearest ~parallel (co- OR anti-facing —
  // an inset's live normal can be inward, so |dot|) wall the inset sits WITHIN (the within-width test stops
  // a coplanar/collinear neighbour from stealing it — the door-50/wall-59 bug). Factored out of snapInsets
  // so identity resolution (host_wall + slot, L3) and snapping agree on the host. Returns the wall or null.
  /** @param {THREE_NS} THREE  @param {SnapSurface} inset  @param {SnapSurface[]} walls  @returns {SnapSurface|null} */
  function hostWallFor(THREE, inset, walls) {
    var V3 = THREE.Vector3;
    var sn = new V3(0, 1, 0).applyQuaternion(inset._lq), bestD = 0.3;
    var best = /** @type {SnapSurface|null} */ (null);
    walls.forEach(function (wl) {
      var wn = new V3(0, 1, 0).applyQuaternion(wl._lq);
      if (Math.abs(wn.dot(sn)) < 0.9) return;                   // ~parallel (co-facing OR anti — insets can be either)
      var rel = inset._lp.clone().sub(wl._lp);
      var d = Math.abs(rel.dot(wn));                            // perpendicular distance to the wall's plane
      if (d >= bestD) return;
      var wx = new V3(1, 0, 0).applyQuaternion(wl._lq);         // wall's local width axis (world)
      if (Math.abs(rel.dot(wx)) > ((wl.extent && wl.extent[0]) || 0) / 2 + 0.3) return;
      bestD = d; best = wl;
    });
    return best;
  }

  // Find DUPLICATE seed insets — the same physical inset persisted under more than one id (§5.3). A
  // matchInset miss can mint a fresh id for an inset that's already in the seed; if the ids then oscillate
  // faster than the removal debounce, BOTH persist, and thereafter matchInset swaps between them frame-to-
  // frame while _recoverMissing re-materialises whichever the capture didn't claim — a flickering visual
  // duplicate. Two insets of the SAME semantic on the SAME host wall whose stored centres sit within `eps`
  // (default 0.25 m — far closer than two real insets could, since they'd physically overlap) are the same
  // inset. Returns the Set of NON-canonical ("shadow") ids to ignore for both identity and recovery, keeping
  // the lowest id per cluster as canonical (deterministic — so every device drops the same shadows). Pure.
  /**
   * @param {{id: string, semantic: string, hostWall?: string, pos: number[]}[]} insets
   * @param {number} [eps]   metres; centres closer than this on one wall ⇒ the same inset (default 0.25)
   * @returns {Set<string>}  the shadow ids to ignore
   */
  function dupInsetIds(insets, eps) {
    var EPS = (typeof eps === "number" && eps > 0) ? eps : 0.25;
    /** @type {Record<string, {id: string, pos: number[]}[]>} */
    var groups = {};
    (insets || []).forEach(function (s) {
      if (!s.hostWall) return;                                   // a freestanding inset has no wall to cluster on
      var k = s.semantic + "|" + s.hostWall;
      (groups[k] = groups[k] || []).push({ id: s.id, pos: s.pos });
    });
    /** @type {Set<string>} */
    var shadows = new Set();
    Object.keys(groups).forEach(function (k) {
      var arr = groups[k].slice().sort(function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; });
      /** @type {{id: string, pos: number[]}[]} */
      var canon = [];
      arr.forEach(function (s) {
        var dup = null;
        for (var i = 0; i < canon.length; i++) {
          var p = canon[i].pos, q = s.pos;
          if (Math.hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]) < EPS) { dup = canon[i]; break; }
        }
        if (dup) shadows.add(s.id);                              // a near-coincident twin → shadow the higher id
        else canon.push(s);
      });
    });
    return shadows;
  }

  // Resolve a captured inset's IDENTITY by its structural place, not its centroid (§5.3 L3). Its id comes
  // from the seed inset (same semantic + host wall) whose RECONSTRUCTED along-wall coordinate is nearest —
  // reconstruction is corner-relative, so the seed inset's expected local spot is centroid-independent.
  // Nearest-in-along (not raw slot index) is what stops a missing/extra inset from cascading wrong ids:
  // a captured window at along≈1 matches the seed window reconstructed at ≈1, even if a middle one is
  // absent this capture. `claimed` holds seed ids already taken this pass; returns the id or null (mint).
  /**
   * @param {{along: number}} cand
   * @param {{id: string, along: number}[]} seedRecon   seed insets' reconstructed along-coords (same sem+wall)
   * @param {Set<string>} [claimed]
   * @param {{tol?: number}} [opts]                     max along gap (m) to accept a match (default 0.4)
   * @returns {string|null}
   */
  function matchInset(cand, seedRecon, claimed, opts) {
    opts = opts || {};
    var TOL = opts.tol != null ? opts.tol : 0.4;
    var best = /** @type {string|null} */ (null), bestD = TOL;
    (seedRecon || []).forEach(function (s) {
      if (claimed && claimed.has(s.id)) return;
      var d = Math.abs(cand.along - s.along);
      if (d < bestD) { bestD = d; best = s.id; }
    });
    return best;
  }

  // Snap each inset (door/window/wall art) so it reads as part of its wall: keep its own (locally
  // accurate) depth, nudge it just in front of the wall toward the room interior, and adopt the wall's
  // exact orientation (parallel). "Into the room" is the OPPOSITE of the inset's own normal — the Quest
  // orients every surface's normal OUTWARD from its room, so this is correct even at a junction.
  //
  // Doors and windows also CUT their wall: each records a hole on its host wall (wl.holes), the inset's
  // rectangle projected into the wall's rendered local X-Y frame ({x, y, w, h}, metres, centred on the
  // wall). The renderer turns those into actual openings so you can see through. Wall art does NOT cut —
  // it's a picture laid on the wall, not an opening. Mutates s.position / s.rotation / s.debug.snap and
  // wl.holes in place.
  /**
   * @param {THREE_NS} THREE
   * @param {SnapSurface[]} surfaces
   * @param {number} [standoff]   perpendicular distance (m) every inset sits in front of its wall (default 0.02)
   * @returns {void}
   */
  function snapInsets(THREE, surfaces, standoff) {
    var V3 = THREE.Vector3;
    var walls = surfaces.filter(function (s) { return s.semantic === "wall"; });
    walls.forEach(function (wl) { wl.holes = []; });            // recomputed fresh every capture
    // Doors/windows/wall-art are pinned to a UNIFORM perpendicular standoff `off` (m) in front of their
    // wall — tunable via --inset-standoff (window.CONJURE_INSET_STANDOFF, passed in here).
    var INSETS = /** @type {Record<string, boolean>} */ ({ "door": true, "window": true, "wall art": true });
    var off = (typeof standoff === "number" && standoff >= 0) ? standoff : 0.02;
    surfaces.forEach(function (s) {
      if (!INSETS[s.semantic] || !walls.length) return;
      // A WebXR plane lies in its local X-Z plane, so its NORMAL is the +Y axis (not +Z).
      var sn = new V3(0, 1, 0).applyQuaternion(s._lq);
      var best = /** @type {SnapSurface|null} */ (null);
      // If the inset already KNOWS its wall (hostWall — recorded by the authority's snapInsets, reused on
      // recovery §5.2 AND carried onto captured insets from the seed), snap to THAT wall by id — don't
      // re-guess by proximity. Otherwise derive it: the nearest ~parallel wall the inset sits WITHIN (the
      // within-width test stops a coplanar/collinear neighbour from stealing it — the door-50/wall-59 bug),
      // and RECORD the choice. NB: the parallel test is `|dot|` (parallel OR ANTI-parallel), deliberately —
      // an inset's LIVE normal can be INWARD, ~180° from its host wall's outward normal (see matchRef, and
      // docs/investigations/wall-art-behind-wall.md). A co-facing test (dot > 0) would REJECT the true host for such insets.
      // The flip side: proximity+|dot| can't tell the two faces of a room-partition apart when they're
      // near-coincident — the recorded host_wall above is the only reliable disambiguator there.
      if (s.hostWall) walls.forEach(function (wl) { if (wl.id === s.hostWall) best = wl; });
      if (!best) best = hostWallFor(THREE, s, walls);           // derive by proximity+within-width, and RECORD it
      if (!best) return;
      s.hostWall = best.id;                                     // record the association (persisted by the authority)
      var nint = sn.clone().negate();                           // into the room = opposite outward normal
      var clr = s._lp.clone().sub(best._lp).dot(nint);          // current perpendicular clearance in front of the wall
      // EVERY inset is pinned to the fixed, uniform standoff `off` in front of its wall plane — NOT its own
      // captured depth (a door detected a few cm proud of its wall would otherwise float). The along-wall
      // position and height stay live from `s._lp` (nint is perpendicular, so this only moves depth); we just
      // set the perpendicular clearance to exactly `off`. (Was: captured insets kept their own depth,
      // recovered ones projected — now uniform.)
      var fp = s._lp.clone().add(nint.clone().multiplyScalar(off - clr));
      s.position = [fp.x, fp.y, fp.z];
      // Every inset adopts its wall's exact orientation — so its stored normal is the wall's TRUE (outward)
      // normal, consistent with all other surfaces. Wall art no longer gets a special upright/negated
      // orientation (that flipped its normal 180°, causing per-session re-minting): the surface is an
      // invisible reference, and the CONTENT hung on it is oriented upright toward the room at placement
      // time (server _face_room), independent of the surface's own roll.
      s.rotation = best.rotation.slice();
      s.debug.snap = "wall=" + best.id.slice(-7) + " clr=" + Math.round(clr * 100) + "cm";

      // Cut the opening. The rendered wall is an <a-plane> oriented by best._lq·Rx(-90°); its local width
      // axis is the captured +X and its height axis the captured -Z (see eulerYXZ). Project the inset's
      // centre offset onto those to place the hole; the inset adopted the wall's orientation above, so its
      // extent already lines up with that frame.
      if ((s.semantic === "door" || s.semantic === "window") && s.extent) {
        var wx = new V3(1, 0, 0).applyQuaternion(best._lq);     // wall local width axis (world)
        var wy = new V3(0, 0, -1).applyQuaternion(best._lq);    // wall local height axis (world)
        var rel = fp.clone().sub(best._lp);
        (best.holes || (best.holes = [])).push({ x: rel.dot(wx), y: rel.dot(wy), w: s.extent[0], h: s.extent[1] });
      }
    });
  }

  // --- diagnostics (docs/backlogs/spaces-geometry.md — "Instrumentation") -------------------------------
  // These two answer the two field symptoms. They live here, with the rest of the pure geometry, so they
  // are unit-testable and so they reuse the SAME definitions the real code uses (`covers`, matchWall's
  // gates) — a diagnostic that reimplements the thing it is diagnosing will eventually disagree with it and
  // send you the wrong way.

  // The room's HEIGHTS, as numbers: every floor's and ceiling's y, and every wall's bottom/top with the
  // floor it sits over. Answers "when that room's floor is four inches high, what moves WITH it?" — the
  // floor alone (the Quest re-fit the plane), or the floor + its ceiling + its walls (the device map
  // shifted regionally, spec §1).
  //
  // Call BEFORE sealWalls: sealing rewrites a wall's centre height and height to close the slit against
  // whatever floor/ceiling it finds, which is exactly the measurement we want to take. After sealing, every
  // wall bottom agrees with its floor by construction and the census says nothing.
  /**
   * @param {THREE_NS} THREE
   * @param {SnapSurface[]} surfaces   local (F_track) surfaces, pre-seal
   * @returns {{floors: {id: string, y: number}[], ceilings: {id: string, y: number}[],
   *            walls: {id: string, bot: number, top: number, floor: string|null, gap: number|null}[],
   *            insets: {id: string, sem: string, y: number, host: string|undefined, h: number}[]}}
   */
  function heightCensus(THREE, surfaces) {
    /** @type {{id: string, y: number}[]} */ var floors = [];
    /** @type {{id: string, y: number}[]} */ var ceilings = [];
    /** @type {{id: string, sem: string, y: number, host: string|undefined, h: number}[]} */ var insets = [];
    /** @type {{id: string, bot: number, top: number, floor: string|null, gap: number|null}[]} */
    var walls = [];
    var floorSurfs = surfaces.filter(function (s) { return s.semantic === "floor" && s._lp && s._lq && s.extent; });
    surfaces.forEach(function (s) {
      if (!s._lp) return;
      if (s.semantic === "floor") floors.push({ id: s.id, y: s._lp.y });
      else if (s.semantic === "ceiling") ceilings.push({ id: s.id, y: s._lp.y });
      else if (INSET_SEM[s.semantic]) insets.push({ id: s.id, sem: s.semantic, y: s._lp.y, host: s.hostWall,
                                                    h: (s.extent && s.extent[1]) || 0 });
      else if (s.semantic === "wall" && s.extent) {
        var h = s.extent[1] || 0, bot = s._lp.y - h / 2, top = s._lp.y + h / 2;
        // Which room is this wall in? The LOWEST covering floor — the same rule sealWalls uses to pick the
        // floor it reaches down to, so `gap` is the very quantity sealing would close.
        var f = /** @type {SnapSurface|null} */ (null);
        floorSurfs.forEach(function (c) {
          if (!covers(THREE, c, s._lp.x, s._lp.z)) return;
          if (!f || c._lp.y < f._lp.y) f = c;
        });
        walls.push({ id: s.id, bot: bot, top: top, floor: f ? f.id : null,
                     gap: f ? bot - f._lp.y : null });
      }
    });
    /** @param {{id: string}} a  @param {{id: string}} b  @returns {number} */
    var byId = function (a, b) { return a.id < b.id ? -1 : a.id > b.id ? 1 : 0; };
    return { floors: floors.sort(byId), ceilings: ceilings.sort(byId), walls: walls.sort(byId),
             insets: insets.sort(byId) };
  }

  // WHY didn't this surface match? The single most valuable line in the churn log: it separates "the Quest
  // never emitted a plane here" (nothing we can tune) from "a plane was right there and the matcher
  // rejected it" (a tolerance we can change) — and in the second case names the failing gate with its
  // actual margin, so `perp=0.19/0.15` tells you what to set --wall-perp-tol to.
  //
  // `probe` and `others` are both constellation-form and the call is used in BOTH directions: a MISSING ref
  // probed against this capture's planes, and a freshly-MINTED plane probed against the reference. The gates
  // below mirror matchWall's, evaluated against the other side's normal; since gate 1 already bounds the two
  // normals to within yawTol, the perpendicular offset differs between the two directions only by cos(dyaw)
  // — immaterial for a diagnostic margin, and it keeps this one function honest in both roles.
  /**
   * @param {THREE_NS} THREE
   * @param {RefSurface} probe                  the surface that failed to match
   * @param {RefSurface[]} others               what it was matched against
   * @param {{perpTol?: number, yawTol?: number, overlapSlop?: number, farM?: number}} [opts]
   * @returns {{why: "device"|"matcher", gate?: string, val?: number, tol?: number,
   *            id?: string, dist?: number}}
   *   why="device" — nothing of that semantic was detected within `farM`, so there was nothing to match.
   *   why="matcher" — the nearest plausible candidate, the gate that rejected it, and by how much.
   */
  function explainNoMatch(THREE, probe, others, opts) {
    opts = opts || {};
    var PERP_TOL = opts.perpTol != null ? opts.perpTol : 0.15;
    var YAW_TOL = opts.yawTol != null ? opts.yawTol : Math.PI / 6;
    var OVERLAP_SLOP = opts.overlapSlop != null ? opts.overlapSlop : 0.3;
    var FAR = opts.farM != null ? opts.farM : 1.5;
    var same = (others || []).filter(function (o) { return o.sem === probe.sem; });
    var vertical = probe.orient === "vertical";
    if (!vertical) {
      // Horizontals go through matchRef, whose only gate is a same-facing centroid radius — so for a floor
      // or ceiling the nearest centroid IS the right candidate and the right margin.
      var hBest = /** @type {RefSurface|null} */ (null), hD = Infinity;
      same.forEach(function (o) { var d = probe.pos.distanceTo(o.pos); if (d < hD) { hD = d; hBest = o; } });
      if (!hBest || hD > FAR) return { why: "device", dist: hBest ? +hD.toFixed(3) : undefined };
      return { why: "matcher", id: hBest.id, dist: +hD.toFixed(3), gate: "dist",
               val: +hD.toFixed(3), tol: 0.5 };
    }
    // For a WALL, "nearest" must be measured the way matchWall measures identity — by PLANE, not centroid.
    // A wall's centroid is a scan artifact that slides metres along the wall between captures, so ranking
    // candidates by centroid distance would report a wall we saw from the other end as "never detected",
    // which is the single most misleading thing this function could say.
    //
    // So: rank by how far through matchWall's gates each candidate gets (a candidate that clears facing and
    // the coincident-plane test is nearly this wall; one that fails facing is a different wall), tie-broken
    // by matchWall's own score. Candidates wildly off in facing or plane are discarded outright — they are
    // other walls in the room, not failed matches of this one.
    var best = /** @type {RefSurface|null} */ (null);
    /** @type {{dyaw: number, perp: number, gap: number}} */
    var bestFacts = { dyaw: 0, perp: 0, gap: 0 };
    var bestRank = -1, bestScore = Infinity;
    same.forEach(function (o) {
      var dyaw = Math.abs(Math.atan2(Math.sin(probe.nyaw - o.nyaw), Math.cos(probe.nyaw - o.nyaw)));
      var onx = Math.sin(o.nyaw), onz = Math.cos(o.nyaw);
      var perp = Math.abs((probe.pos.x - o.pos.x) * onx + (probe.pos.z - o.pos.z) * onz);
      if (dyaw > 2 * YAW_TOL || perp > 5 * PERP_TOL) return;    // a different wall, not a missed match
      var tx = onz, tz = -onx;
      var pa = probe.pos.x * tx + probe.pos.z * tz, oa = o.pos.x * tx + o.pos.z * tz;
      var gap = Math.abs(pa - oa) - (((probe.ext && probe.ext[0]) || 0) / 2 + ((o.ext && o.ext[0]) || 0) / 2);
      var rank = dyaw > YAW_TOL ? 0 : perp > PERP_TOL ? 1 : gap > OVERLAP_SLOP ? 2 : 3;
      var score = perp + Math.max(0, gap);                      // matchWall's own tie-break
      if (rank > bestRank || (rank === bestRank && score < bestScore)) {
        bestRank = rank; bestScore = score; best = o;
        bestFacts = { dyaw: dyaw, perp: perp, gap: gap };
      }
    });
    if (!best) return { why: "device" };
    var out = { why: /** @type {"matcher"} */ ("matcher"), id: best.id,
                dist: +probe.pos.distanceTo(best.pos).toFixed(3) };
    if (bestRank === 0)
      return Object.assign(out, { gate: "dyaw", val: +(bestFacts.dyaw * 180 / Math.PI).toFixed(1),
                                  tol: +(YAW_TOL * 180 / Math.PI).toFixed(1) });
    if (bestRank === 1) return Object.assign(out, { gate: "perp", val: +bestFacts.perp.toFixed(3), tol: PERP_TOL });
    if (bestRank === 2) return Object.assign(out, { gate: "gap", val: +bestFacts.gap.toFixed(3), tol: OVERLAP_SLOP });
    // Every gate passes, so this candidate WAS matchable — it must already have been claimed by another
    // plane this capture. That is the id-swap shape, and worth its own name.
    return Object.assign(out, { gate: "claimed" });
  }

  // Which floor is the plan point (x,z) standing over? The same footprint test sealWalls and heightCensus
  // use, exposed so the marker probe can attribute a measured error to a ROOM. Nearest-by-height would be
  // wrong in precisely the case the marker exists for: when one room's floor is rendered 13 cm high and you
  // rest the controller on the real floor, the nearest floor plane by height is the one in the OTHER room.
  /**
   * @param {THREE_NS} THREE  @param {SnapSurface[]} floors  @param {number} x  @param {number} z
   * @returns {SnapSurface|null}
   */
  function floorUnder(THREE, floors, x, z) {
    var best = /** @type {SnapSurface|null} */ (null);
    (floors || []).forEach(function (f) {
      if (f.semantic !== "floor" || !covers(THREE, f, x, z)) return;
      if (!best || f._lp.y < best._lp.y) best = f;          // lowest covering floor — sealWalls' rule
    });
    return best;
  }

  // --- floating-room correction (docs/investigations/raised-floor.md) ----------------------------------
  // A measured, reproduced fault: the Quest's stored room entity for one room can be anchored ~10 cm high,
  // so every plane in that room renders above the real floor and anything placed there floats. Confirmed
  // on-device against two known-equal surfaces, and NOT fixable by a Room Setup re-scan.
  //
  // This is the one place we knowingly render something other than the raw capture, so the bar is high: it
  // must fire only when the evidence is unambiguous, and do nothing at all otherwise.
  //
  // THE TIGHT CRITERION is coherence, not magnitude. A room is only a candidate when its floor AND its
  // ceiling have drifted from the seed by the SAME amount — that is the signature of a room entity moving
  // as a rigid body, and it is what a noisy plane fit cannot fake. On the reference capture the affected
  // room read floor +77 mm / ceiling +71 mm (coherent to 6 mm) while the kitchen read floor +18 mm /
  // ceiling −38 mm — incoherent, so the kitchen is excluded from both the candidate set and the baseline
  // rather than being "corrected" into something worse.
  //
  // On top of that: exactly ONE candidate (two and we cannot tell which is the outlier and which the
  // reference), at least one coherent reference room, and those references agreeing among themselves.
  /**
   * @param {THREE_NS} THREE
   * @param {SnapSurface[]} surfaces        live, PRE-seal (needs true wall bottoms)
   * @param {Record<string, number>} devById   id → height deviation vs the seed, for EVERY surface that has
   *   a seed counterpart, all offset by the same median (WorldModel.levelDeviation with a floor/ceiling basis)
   * @param {{minM?: number, cohM?: number, faceM?: number}} [opts]
   * @returns {{floor: string, ceiling: string, offset: number, ids: string[]}|null}
   *   `offset` is how far the room sits ABOVE the rest of the space; subtract it to correct.
   */
  function floatingRoom(THREE, surfaces, devById, opts) {
    opts = opts || {};
    var MIN = opts.minM != null ? opts.minM : 0.06;    // displacement that counts as definite
    var COH = opts.cohM != null ? opts.cohM : 0.02;    // floor and ceiling drifts must agree this closely
    var FACE = opts.faceM != null ? opts.faceM : 0.05; // min clearance (m) for a wall's facing to be decided
    if (!(MIN > 0) || !devById) return null;

    var ceils = surfaces.filter(function (s) { return s.semantic === "ceiling" && s._lp && s._lq && s.extent; });
    /** @type {{floor: SnapSurface, ceiling: SnapSurface, coh: number, off: number}[]} */
    var rooms = [];
    surfaces.forEach(function (f) {
      if (f.semantic !== "floor" || !f._lp || !f._lq || !f.extent) return;
      var c = /** @type {SnapSurface|null} */ (null);
      ceils.forEach(function (cc) { if (!c && covers(THREE, cc, f._lp.x, f._lp.z)) c = cc; });
      if (!c) return;                                   // a floor with no ceiling over it proves nothing
      var df = devById[f.id], dc = devById[c.id];
      if (df == null || dc == null) return;             // not in the seed → no baseline for it
      rooms.push({ floor: f, ceiling: c, coh: Math.abs(df - dc), off: (df + dc) / 2 });
    });
    if (rooms.length < 2) return null;                  // need a room to measure against

    var coherent = rooms.filter(function (r) { return r.coh <= COH; });
    var cand = coherent.filter(function (r) { return Math.abs(r.off) >= MIN; });
    if (cand.length !== 1) return null;                 // none = healthy; several = no way to say which is right
    var ref = coherent.filter(function (r) { return r !== cand[0]; });
    if (!ref.length) return null;                       // nothing trustworthy left to be the baseline
    var lo = Infinity, hi = -Infinity, sum = 0;
    ref.forEach(function (r) { lo = Math.min(lo, r.off); hi = Math.max(hi, r.off); sum += r.off; });
    if (hi - lo > COH * 2) return null;                 // the references disagree ⇒ no consensus to correct toward
    var shift = cand[0].off - sum / ref.length;
    if (Math.abs(shift) < MIN) return null;

    // MEMBERSHIP IS SPATIAL, because the fault is. The floor and ceiling agreeing within 1 mm proves the
    // room moved as a RIGID BODY — so everything physically in that room moved with it, by definition, and
    // "which surfaces are in the bedroom" is a question about geometry, not about measurement.
    //
    // Two earlier answers were both wrong, and are worth not repeating:
    //   • bottom-proximity ("is your bottom near the floor") admitted a near-arbitrary single wall and put
    //     a door two and a half inches into the ground;
    //   • per-surface DRIFT looked principled but cannot be measured honestly for a wall — the seed stores
    //     it post-`sealWalls` while a live capture is pre-seal — and it left the walls behind, so their gap
    //     to the corrected floor GREW by the offset and pushed past `--wall-seal-tol`, opening a visible
    //     15 cm slit under a wall that had been fine.
    //
    // FACING IS WHAT SEPARATES ADJACENT ROOMS. A partition is captured as TWO near-coincident planes, one
    // per room, anti-parallel — so footprint alone claims both and would drag the neighbour's wall down
    // with this room. A captured normal points OUTWARD from its room (§2.2), so the room's interior lies on
    // the −normal side: the floor centre must satisfy `(centre − wall) · n < 0`. Measured on the reference
    // space, the bedroom/kitchen partition pair reads −1.62 and +1.74 — the same wall, cleanly split.
    var fc = cand[0].floor._lp;
    /** @type {Record<string, number>} */ var ids = {};
    /** @type {Record<string, number>} */ var walls = {};
    ids[cand[0].floor.id] = 1; ids[cand[0].ceiling.id] = 1;
    surfaces.forEach(function (w) {
      if (w.semantic !== "wall" || !w._lp || !w._lq) return;
      if (!covers(THREE, cand[0].floor, w._lp.x, w._lp.z)) return;
      var n = new THREE.Vector3(0, 1, 0).applyQuaternion(w._lq);          // captured normal: plane-local +Y
      var facing = (fc.x - w._lp.x) * n.x + (fc.z - w._lp.z) * n.z;
      if (facing > -FACE) return;      // faces away (the neighbour's copy), or too near the centre to tell
      ids[w.id] = 1; walls[w.id] = 1;
    });
    // Insets ride their recorded host wall rather than their own normal: a wall-art normal can arrive
    // INWARD (§2.2), so the facing test would reject exactly the insets it is meant to carry.
    surfaces.forEach(function (s) { if (s.hostWall && walls[s.hostWall]) ids[s.id] = 1; });
    return { floor: cand[0].floor.id, ceiling: cand[0].ceiling.id, offset: shift, ids: Object.keys(ids) };
  }

  // Apply a `floatingRoom` result: lower every member surface by the offset. Vertical ONLY — plane normals,
  // horizontal position and extent are untouched, so registration (yaw + x/z) and every plane-relative
  // anchor are unaffected, exactly as with sealWalls. Mutates in place.
  /** @param {SnapSurface[]} surfaces  @param {{offset: number, ids: string[]}} fix  @returns {number} moved */
  function applyFloatingFix(surfaces, fix) {
    if (!fix || !fix.offset) return 0;
    /** @type {Record<string, number>} */ var want = {};
    var n = 0;
    fix.ids.forEach(function (id) { want[id] = 1; });
    surfaces.forEach(function (s) {
      if (!want[s.id] || !s._lp) return;
      s._lp.y -= fix.offset;
      if (s.position) s.position = [s.position[0], s.position[1] - fix.offset, s.position[2]];
      n++;
    });
    return n;
  }

  return { eulerYXZ: eulerYXZ, yawOf: yawOf, register: register,
           heightCensus: heightCensus, explainNoMatch: explainNoMatch, floorUnder: floorUnder,
           floatingRoom: floatingRoom, applyFloatingFix: applyFloatingFix,
           canonicalFrame: canonicalFrame, surfaceToRef: surfaceToRef, selectSpace: selectSpace,
           matchRef: matchRef, matchWall: matchWall, matchInset: matchInset, dupInsetIds: dupInsetIds,
           wallCorners: wallCorners, authorInsetAnchor: authorInsetAnchor, reconstructInset: reconstructInset,
           insetAlong: insetAlong, hostWallFor: hostWallFor,
           joinCorners: joinCorners, sealWalls: sealWalls, snapInsets: snapInsets };
});
