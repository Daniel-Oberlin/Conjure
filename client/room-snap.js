// Pure room-snapping geometry — extracted from the room-capture component so it can be unit-tested
// (tests/js/room-snap.test.js) independently of A-Frame/WebXR/DOM. Every function takes the THREE
// module as its first argument: the browser passes AFRAME.THREE, node tests pass require('three').
// No state, no DOM, no globals — just the math that turns captured planes into placed surfaces.
//
// Surface objects here carry: _lp (THREE.Vector3, ref-frame position), _lq (THREE.Quaternion,
// ref-frame orientation), semantic, extent [w,h], rotation [x,y,z]°, id, debug{}. `cur`/`ref` entries
// for register() carry: pos (Vector3), sem, ext [w,h], nyaw (number), orient ("vertical"|"horizontal").
(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else root.RoomSnap = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // A captured plane lies in its local X-Z plane (normal +Y); our <a-plane> is X-Y (normal +Z). Compose
  // a -90° X rotation so the rendered plane aligns with the captured one, then convert to euler degrees.
  // A-Frame applies rotations in YXZ order (NOT THREE's default XYZ) — using XYZ here renders walls/insets
  // up to ~48° off-square. See docs/room-model.md.
  function eulerYXZ(THREE, q) {
    var quat = new THREE.Quaternion(q.x, q.y, q.z, q.w);
    quat.multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2));
    var e = new THREE.Euler().setFromQuaternion(quat, "YXZ");
    var d = THREE.MathUtils.radToDeg;
    return [d(e.x), d(e.y), d(e.z)];
  }

  function yawOf(n) { return Math.atan2(n.x, n.z); }   // compass yaw of a horizontal normal

  // Solve the single rigid yaw+translation transform mapping the newly detected planes (cur, in the
  // current refSpace) onto the persistent reference constellation (ref). Recovers how the Quest's frame
  // jumped using the room's own geometry — robust to the ~180° boundary flip because the yaw is read from
  // the SHIFT in surface-normal directions, needing no prior pairing. Returns {Tmat, stat}: Tmat is a
  // Matrix4 (refSpace → reference frame) when confident, else null (caller holds the last frame). `stat`
  // is a short diagnostic string.
  // Frame registration — recover the rigid transform (yaw about gravity + x/z translation) that maps the
  // CURRENT detected planes onto the persistent reference constellation, so surface ids survive a tracking
  // relocalization (boundary re-entry flips the frame ~167° + ~3 m; see docs/room-model.md §8a).
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
  //      yields no consensus, so a null doubles as a "not in this space" signal (room-model.md §8a).
  function register(THREE, cur, ref) {
    var UP = new THREE.Vector3(0, 1, 0);
    if (ref.length < 3) return { Tmat: null, stat: "ref<3" };
    function wrap(a) { while (a > Math.PI) a -= 2 * Math.PI; while (a < -Math.PI) a += 2 * Math.PI; return a; }
    // Robustness for a GUEST's partial/extra plane set (room-model §8, multi-user co-location). A guest
    // sees the room from a different vantage: some reference surfaces are MISSING (occluded) and there are
    // EXTRA planes (furniture/clutter) with no reference. Two ideas make the vote tolerate both:
    //  • size-compat is ASYMMETRIC — a detected plane may be a PARTIAL (smaller) view of a reference, so
    //    only reject one notably LARGER than its candidate reference (a bigger plane isn't a partial view).
    //  • acceptance scores DISTINCT reference surfaces COVERED (not detected-plane count): extras can't
    //    inflate it, fragmentation can't double-count it, and missing surfaces just lower coverage. We
    //    accept on coverage of the REFERENCE, so clutter never sinks an otherwise-solid lock.
    var SIZE_TOL = 0.5, MIN_COV = 4, MIN_COV_FRAC = 0.3;
    function sizeCompat(r, c) { return c.ext[0] <= r.ext[0] + SIZE_TOL && c.ext[1] <= r.ext[1] + SIZE_TOL; }
    // Step 1 — candidate yaw(s): histogram the normal-yaw delta over same-semantic, similar-size vertical
    // pairs; every true correspondence votes for the same delta, so the real yaw dominates.
    var deltas = [];
    cur.forEach(function (c) {
      if (c.orient !== "vertical") return;
      ref.forEach(function (r) {
        if (r.orient !== "vertical" || r.sem !== c.sem || !sizeCompat(r, c)) return;
        deltas.push(wrap(r.nyaw - c.nyaw));
      });
    });
    if (deltas.length < 3) return { Tmat: null, stat: "dlt=" + deltas.length };
    var bin = Math.PI / 30, hist = {};                          // 6° bins
    deltas.forEach(function (d) { var b = Math.round(d / bin); (hist[b] = hist[b] || []).push(d); });
    var keys = Object.keys(hist).sort(function (a, b) { return hist[b].length - hist[a].length; });
    var thetas = keys.slice(0, 5).map(function (k) {            // top 5 peaks (clutter can dilute the true one)
      var s = 0, c2 = 0; hist[k].forEach(function (d) { s += Math.sin(d); c2 += Math.cos(d); });
      return Math.atan2(s, c2);
    });
    // Step 2/3 — for each candidate yaw, solve translation (densest cell of ref.pos − R·cur.pos over
    // same-size pairs) and score by how many planes land on a same-semantic reference surface.
    var best = null;
    thetas.forEach(function (theta) {
      var qy = new THREE.Quaternion().setFromAxisAngle(UP, theta);
      // Same-FACING gate: after applying this candidate yaw, a true correspondence's normal aligns with its
      // reference (c.nyaw + theta ≈ r.nyaw). The two faces of a shared partition wall have centers ~0.5 m
      // apart and OPPOSITE normals, so without this a wall can pair with / cover the wrong face — polluting
      // the translation vote and coverage, which makes the lock jitter frame-to-frame. Only vertical faces
      // are gated (floor/ceiling separate by semantic). ~60° tolerance (cos > 0.5) leaves ample noise room.
      function sameFacing(c, r) {
        return !(c.orient === "vertical" && r.orient === "vertical" && Math.cos((c.nyaw + theta) - r.nyaw) < 0.5);
      }
      var grid = {}, bestCell = null, bestN = 0;
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
      var claimed = new Set(), rawInl = 0;   // distinct reference surfaces covered (extras/fragmentation don't inflate)
      cur.forEach(function (c) {
        var tp = c.pos.clone().applyMatrix4(Tmat), bd = 0.4, hit = null;
        ref.forEach(function (r) { if (r.sem === c.sem && sameFacing(c, r)) { var d = tp.distanceTo(r.pos); if (d < bd) { bd = d; hit = r; } } });
        if (hit) { claimed.add(hit); rawInl++; }
      });
      var cov = claimed.size;
      if (!best || cov > best.cov) best = { Tmat: Tmat, cov: cov, inl: rawInl };
    });
    var cov = best ? best.cov : 0;
    var stat = "cov=" + cov + "/" + ref.length + " inl=" + (best ? best.inl : 0) + "/" + cur.length + " dlt=" + deltas.length;
    // Accept on DISTINCT reference COVERAGE (not fraction-of-detected): enough of the known room explained
    // by ONE transform. Robust to EXTRA detected planes (absent from the formula) and MISSING ones (need
    // only a fraction of the reference). A genuinely different space can't cover ≥MIN_COV surfaces of the
    // reference under one consistent transform ⇒ null ("not in this space", room-model §8a).
    if (!best || cov < MIN_COV || cov < MIN_COV_FRAC * ref.length) return { Tmat: null, stat: stat, cov: cov };
    // Append the SOLVED transform (yaw about gravity + translation) so diagnostics can tell whether a
    // relocalization actually changed the frame (yaw jumps) or registration stayed put while the world
    // shifted. Matrix4 is column-major: e[0]=cosθ, e[8]=sinθ for the Y rotation; e[12],e[14]=tx,tz.
    var e = best.Tmat.elements;
    stat += " yaw=" + Math.round(Math.atan2(e[8], e[0]) * 180 / Math.PI) + "° t=("
      + e[12].toFixed(2) + "," + e[14].toFixed(2) + ")";
    return { Tmat: best.Tmat, stat: stat, cov: cov };
  }

  // Convert ONE stored/broadcast surface entity (a-plane form: transform.position/rotation in degrees,
  // components.surface.extent, meta.semantic) into the compact constellation form register() consumes
  // ({id, sem, ext, pos:Vector3, nyaw, orient}). The a-plane normal is its local +Z. Shared by the client's
  // reference-seeding and by selectSpace below, so the two never diverge.
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

  // Two-stage space selection, fine stage (new-space-flow §3, D2/D7). Geolocation already narrowed the
  // field to a few geo-near candidate spaces; this picks WHICH one the headset is physically in by trying
  // to register() the live capture (cur) against each candidate's stored constellation and keeping the one
  // with the highest reference COVERAGE. A candidate only qualifies if register() confidently locks (its
  // coverage clears MIN_COV/MIN_COV_FRAC) — so a null return means "none of these; you're somewhere new".
  // Robust to the sparse-capture bug by construction: it's the geometric vote, not a surface-count guess,
  // that decides, and a too-thin `cur` simply fails to cover any candidate (caller retries as capture grows).
  //   candidates: [{owner, name, surfaces:[a-plane entity]}]  →  {index, owner, name, cov, stat} | null
  function selectSpace(THREE, cur, candidates) {
    var best = null;
    (candidates || []).forEach(function (cand, i) {
      var ref = (cand.surfaces || []).map(function (e) { return surfaceToRef(THREE, e); });
      var reg = register(THREE, cur, ref);
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
  function matchRef(cand, refs, claimed) {
    var best = null, bd = 0.5;
    refs.forEach(function (r) {
      if (r.sem !== cand.sem || (claimed && claimed.has(r))) return;
      // same-facing gate: two parallel walls sharing a partition are opposite FACES (~0.4 m apart, normals
      // 180° apart) — reject the wrong one so ids don't swap. Now that all surfaces store their true
      // (outward) normal — wall art no longer negated — a surface's own detection re-matches cleanly, so
      // the old coincident-flip fallback isn't needed.
      if (cand.orient === "vertical" && r.orient === "vertical" && Math.cos(cand.nyaw - r.nyaw) < 0.5) return;
      var d = cand.pos.distanceTo(r.pos);
      if (d < bd) { bd = d; best = r; }
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

  // Square the walls. WebXR fits every plane independently, so small wall slivers around openings come
  // back a couple degrees off. The whole space shares one orthogonal grid (the rooms are square with each
  // other), so estimate it width-weighted from all walls — the big, accurate walls dominate — and snap
  // each vertical surface's facing onto the nearest 90° of it. Small nudges only (≤12°), so a genuinely
  // angled wall is left alone. Mutates s._lq and s.rotation in place.
  function squareWalls(THREE, surfaces) {
    var UP2 = new THREE.Vector3(0, 1, 0), gx = 0, gy = 0;
    var facing = function (s) { var n = UP2.clone().applyQuaternion(s._lq); return Math.atan2(n.x, n.z); };
    surfaces.forEach(function (s) {
      if (s.semantic !== "wall") return;
      var a = facing(s) * 4, w = (s.extent && s.extent[0]) || 1;  // ×4 folds the 90° grid into a full turn
      gx += w * Math.cos(a); gy += w * Math.sin(a);
    });
    if (!(gx || gy)) return;
    var grid = Math.atan2(gy, gx) / 4;                          // dominant facing, mod 90° (radians)
    surfaces.forEach(function (s) {
      if (["wall", "door", "window", "wall art"].indexOf(s.semantic) < 0) return;
      var yw = facing(s), d = (grid + Math.round((yw - grid) / (Math.PI / 2)) * (Math.PI / 2)) - yw;
      while (d > Math.PI) d -= 2 * Math.PI;
      while (d < -Math.PI) d += 2 * Math.PI;
      if (Math.abs(d) > 0.21) return;                           // >~12° ⇒ likely a real angle, leave it
      s._lq.premultiply(new THREE.Quaternion().setFromAxisAngle(UP2, d));  // rotate facing onto the grid
      s.rotation = eulerYXZ(THREE, s._lq);
    });
  }

  // Join wall corners. WebXR fits each wall independently, so two walls that should meet at a corner
  // often stop a few cm short (or overshoot). After squaring, perpendicular walls' planes intersect on a
  // clean vertical line; for each such pair whose nearest ENDS both fall within GAP of that intersection,
  // snap those ends exactly onto it — closing the corner with a small extend/trim. Works in plan view
  // (X-Z). Leaves parallel walls (a doorway gap, opposite walls) and T-junctions (only one wall ends
  // near the crossing) alone. Mutates wall _lp / extent / position in place; run after squareWalls.
  function joinCorners(THREE, surfaces) {
    var GAP = 0.25;                                              // only close gaps up to 25 cm
    var W = surfaces.filter(function (s) { return s.semantic === "wall"; }).map(function (s) {
      var n = new THREE.Vector3(0, 1, 0).applyQuaternion(s._lq);
      var L = Math.hypot(n.x, n.z) || 1, nx = n.x / L, nz = n.z / L;   // unit horizontal normal
      var hw = ((s.extent && s.extent[0]) || 0) / 2, cx = s._lp.x, cz = s._lp.z;
      var tx = nz, tz = -nx;                                     // width axis ⟂ normal (in plan view)
      return { s: s, cx: cx, cz: cz, nx: nx, nz: nz, hw: hw, cy: s._lp.y,
               ends: [{ x: cx + tx * hw, z: cz + tz * hw }, { x: cx - tx * hw, z: cz - tz * hw }],
               tgt: [null, null] };
    });
    function nearest(w, px, pz) {
      var d0 = Math.hypot(w.ends[0].x - px, w.ends[0].z - pz);
      var d1 = Math.hypot(w.ends[1].x - px, w.ends[1].z - pz);
      return d1 < d0 ? { i: 1, d: d1 } : { i: 0, d: d0 };
    }
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
        if (!a.tgt[ka.i] || ka.d < a.tgt[ka.i]._d) a.tgt[ka.i] = { x: px, z: pz, _d: ka.d };
        if (!b.tgt[kb.i] || kb.d < b.tgt[kb.i]._d) b.tgt[kb.i] = { x: px, z: pz, _d: kb.d };
      }
    }
    W.forEach(function (w) {
      if (!w.tgt[0] && !w.tgt[1]) return;
      var E0 = w.tgt[0] || w.ends[0], E1 = w.tgt[1] || w.ends[1];
      var cx = (E0.x + E1.x) / 2, cz = (E0.z + E1.z) / 2;
      w.s._lp.x = cx; w.s._lp.z = cz;
      w.s.extent = [Math.hypot(E1.x - E0.x, E1.z - E0.z), (w.s.extent && w.s.extent[1]) || 0];
      w.s.position = [cx, w.s._lp.y, cz];
    });
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
  function snapInsets(THREE, surfaces) {
    var V3 = THREE.Vector3;
    var walls = surfaces.filter(function (s) { return s.semantic === "wall"; });
    walls.forEach(function (wl) { wl.holes = []; });            // recomputed fresh every capture
    var INSET = { "door": 0.012, "window": 0.012, "wall art": 0.022 };
    surfaces.forEach(function (s) {
      var off = INSET[s.semantic];
      if (off == null || !walls.length) return;
      // A WebXR plane lies in its local X-Z plane, so its NORMAL is the +Y axis (not +Z).
      var sn = new V3(0, 1, 0).applyQuaternion(s._lq), best = null, bestD = 0.3;
      walls.forEach(function (wl) {
        var wn = new V3(0, 1, 0).applyQuaternion(wl._lq);
        if (Math.abs(wn.dot(sn)) < 0.9) return;                 // nearest ~parallel wall (clamp reference)
        var d = Math.abs(s._lp.clone().sub(wl._lp).dot(wn));
        if (d < bestD) { bestD = d; best = wl; }
      });
      if (!best) return;
      var nint = sn.clone().negate();                           // into the room = opposite outward normal
      var clr = s._lp.clone().sub(best._lp).dot(nint);
      var fp = clr < off ? s._lp.clone().add(nint.clone().multiplyScalar(off - clr)) : s._lp.clone();
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
        best.holes.push({ x: rel.dot(wx), y: rel.dot(wy), w: s.extent[0], h: s.extent[1] });
      }
    });
  }

  return { eulerYXZ: eulerYXZ, yawOf: yawOf, register: register,
           canonicalFrame: canonicalFrame, surfaceToRef: surfaceToRef, selectSpace: selectSpace,
           matchRef: matchRef,
           squareWalls: squareWalls, joinCorners: joinCorners, snapInsets: snapInsets };
});
