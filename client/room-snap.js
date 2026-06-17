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

  // Euler (deg, YXZ) that orients an <a-plane> upright and facing the room, given the direction the
  // FRONT should face (`nInto` = into the room). The plane's local +Z (front, where the texture reads
  // correctly) points along nInto toward the viewer, +Y is world-up projected onto the plane, +X follows
  // right-handed. Used for wall art: a captured plane carries an arbitrary in-plane roll, so adopting the
  // wall's exact orientation can render an image sideways/upside-down — this pins it to gravity instead.
  function uprightInset(THREE, nInto) {
    var UP = new THREE.Vector3(0, 1, 0);
    var F = nInto.clone().normalize();
    var U = UP.clone().sub(F.clone().multiplyScalar(UP.dot(F)));
    if (U.lengthSq() < 1e-6) U.set(0, 1, 0);            // degenerate (a floor/ceiling-facing art) fallback
    U.normalize();
    var R = U.clone().cross(F);                          // +X = +Y × +Z (right-handed)
    var q = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(R, U, F));
    var e = new THREE.Euler().setFromQuaternion(q, "YXZ"), d = THREE.MathUtils.radToDeg;
    return [d(e.x), d(e.y), d(e.z)];
  }

  // Solve the single rigid yaw+translation transform mapping the newly detected planes (cur, in the
  // current refSpace) onto the persistent reference constellation (ref). Recovers how the Quest's frame
  // jumped using the room's own geometry — robust to the ~180° boundary flip because the yaw is read from
  // the SHIFT in surface-normal directions, needing no prior pairing. Returns {Tmat, stat}: Tmat is a
  // Matrix4 (refSpace → reference frame) when confident, else null (caller holds the last frame). `stat`
  // is a short diagnostic string.
  function register(THREE, cur, ref) {
    var UP = new THREE.Vector3(0, 1, 0);
    if (ref.length < 3) return { Tmat: null, stat: "ref<3" };
    function wrap(a) { while (a > Math.PI) a -= 2 * Math.PI; while (a < -Math.PI) a += 2 * Math.PI; return a; }
    // Step 1 — candidate yaw(s): histogram the normal-yaw delta over same-semantic, similar-size vertical
    // pairs; every true correspondence votes for the same delta, so the real yaw dominates.
    var deltas = [];
    cur.forEach(function (c) {
      if (c.orient !== "vertical") return;
      ref.forEach(function (r) {
        if (r.orient !== "vertical" || r.sem !== c.sem) return;
        if (Math.abs(r.ext[0] - c.ext[0]) > 0.4 || Math.abs(r.ext[1] - c.ext[1]) > 0.4) return;
        deltas.push(wrap(r.nyaw - c.nyaw));
      });
    });
    if (deltas.length < 3) return { Tmat: null, stat: "dlt=" + deltas.length };
    var bin = Math.PI / 30, hist = {};                          // 6° bins
    deltas.forEach(function (d) { var b = Math.round(d / bin); (hist[b] = hist[b] || []).push(d); });
    var keys = Object.keys(hist).sort(function (a, b) { return hist[b].length - hist[a].length; });
    var thetas = keys.slice(0, 3).map(function (k) {            // top 3 peaks, circular-mean each
      var s = 0, c2 = 0; hist[k].forEach(function (d) { s += Math.sin(d); c2 += Math.cos(d); });
      return Math.atan2(s, c2);
    });
    // Step 2/3 — for each candidate yaw, solve translation (densest cell of ref.pos − R·cur.pos over
    // same-size pairs) and score by how many planes land on a same-semantic reference surface.
    var best = null;
    thetas.forEach(function (theta) {
      var qy = new THREE.Quaternion().setFromAxisAngle(UP, theta);
      var grid = {}, bestCell = null, bestN = 0;
      cur.forEach(function (c) {
        var rc = c.pos.clone().applyQuaternion(qy);
        ref.forEach(function (r) {
          if (r.sem !== c.sem) return;
          if (Math.abs(r.ext[0] - c.ext[0]) > 0.3 || Math.abs(r.ext[1] - c.ext[1]) > 0.3) return;
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
      var inl = 0;
      cur.forEach(function (c) {
        var tp = c.pos.clone().applyMatrix4(Tmat), bd = 0.4;
        ref.forEach(function (r) { if (r.sem === c.sem) { var d = tp.distanceTo(r.pos); if (d < bd) bd = d; } });
        if (bd < 0.4) inl++;
      });
      if (!best || inl > best.inl) best = { Tmat: Tmat, inl: inl };
    });
    var stat = "inl=" + (best ? best.inl : 0) + "/" + cur.length + " dlt=" + deltas.length;
    if (!best || best.inl < 4 || best.inl < 0.4 * cur.length) return { Tmat: null, stat: stat };
    return { Tmat: best.Tmat, stat: stat };
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
      // A door/window leaf adopts the wall's exact orientation so it fills the wall's opening. Wall art
      // doesn't cut a hole, so pin it upright and facing the room instead (a textured image must not
      // inherit the captured plane's arbitrary roll — otherwise it renders sideways/upside-down).
      s.rotation = s.semantic === "wall art" ? uprightInset(THREE, nint) : best.rotation.slice();
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

  return { eulerYXZ: eulerYXZ, yawOf: yawOf, uprightInset: uprightInset,
           register: register, squareWalls: squareWalls, snapInsets: snapInsets };
});
