// @ts-check
// Pure world-model & presence helpers — extracted from conjure-client.js so the parts that are just data
// transforms and geometry (no DOM, no A-Frame) can be strict TYPE-CHECKED (npm run typecheck) and
// unit-tested (tests/js/world-model.test.js). The A-Frame/DOM glue stays in conjure-client.js, which loads
// this as window.WorldModel; the geometry helpers take the THREE module as their first arg (browser passes
// AFRAME.THREE, node tests pass require('three')) — same convention as room-snap.js.

/**
 * @typedef {typeof import('three')} THREE_NS
 */

/**
 * The geometry-defining fields of a real surface — its transform + shape — as a comparable signature for
 * the render apply-gate. Styling (material/colour/visibility) is deliberately absent: it never rebuilds the
 * mesh, so it isn't gated.
 * @typedef {Object} SurfaceSig
 * @property {number[]} p     position [x, y, z] (metres)
 * @property {number[]} r     rotation euler degrees [x, y, z] (A-Frame YXZ order)
 * @property {number[]} ext   extent [w, h] (metres)
 * @property {{x:number, y:number, w:number, h:number}[]} holes   openings in the wall's local X-Y frame
 */

(function (root, factory) {
  if (typeof module !== "undefined" && module.exports) module.exports = factory();
  else (/** @type {any} */ (root)).WorldModel = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // A patch `env` op arrives as a FLAT map of dotted paths ({"room.active": true, "sky.color": "#000"}).
  // Rebuild the nested object the renderer applies ({room:{active:true}, sky:{color:"#000"}}). Intermediate
  // objects are created on the way down; the leaf takes the value.
  /**
   * @param {Record<string, any>} flat
   * @returns {Record<string, any>}
   */
  function nest(flat) {
    /** @type {Record<string, any>} */
    var out = {};
    Object.keys(flat).forEach(function (k) {
      var ks = k.split("."), cur = out;
      for (var i = 0; i < ks.length - 1; i++) cur = cur[ks[i]] = cur[ks[i]] || {};
      cur[ks[ks.length - 1]] = flat[k];
    });
    return out;
  }

  // Encode a wall's openings as the holed-wall geometry's component-string-safe list: "x y w h, x y w h, …"
  // (metres, in the wall's local X-Y frame). Empty ⇒ no openings ⇒ a plain plane. Fixed 4-dp so the string
  // is stable and parser-safe (no ':' / ';' to clash with A-Frame's component syntax).
  /**
   * @param {{x:number, y:number, w:number, h:number}[]|null|undefined} holes
   * @returns {string}
   */
  function holesAttr(holes) {
    return (Array.isArray(holes) ? holes : []).map(function (ho) {
      return [ho.x, ho.y, ho.w, ho.h].map(function (n) { return (+n).toFixed(4); }).join(" ");
    }).join(", ");
  }

  // Format a value for an A-Frame vec3-ish attribute: an [x,y,z] array becomes "x y z"; anything else
  // (a string color, a number) passes through unchanged.
  /**
   * @param {any} a
   * @returns {any}
   */
  function v3(a) { return Array.isArray(a) ? a.join(" ") : a; }

  // A remote head pose's orientation → the avatar's { yawDeg, pitchDeg }. The head looks down its local -Z;
  // yaw is that direction's compass angle (whole avatar turns), pitch its elevation (the eyes ride up/down
  // the head sphere). Pitch is clamped to asin's domain so a noisy quaternion can't NaN it.
  /**
   * @param {THREE_NS} THREE
   * @param {[number, number, number, number]} q   head orientation quaternion [x, y, z, w]
   * @returns {{yawDeg: number, pitchDeg: number}}
   */
  function avatarAim(THREE, q) {
    var f = new THREE.Vector3(0, 0, -1).applyQuaternion(new THREE.Quaternion(q[0], q[1], q[2], q[3]));
    return {
      yawDeg: THREE.MathUtils.radToDeg(Math.atan2(-f.x, -f.z)),
      pitchDeg: THREE.MathUtils.radToDeg(Math.asin(Math.max(-1, Math.min(1, f.y)))),
    };
  }

  // A floor-level position `dist` metres to the RIGHT of an owner's head pose — where a desktop guest drops
  // in beside the owner (§Phase 4). "Right" is the head's local +X flattened onto the floor (y=0) so the
  // guest lands level regardless of how the owner is looking up/down.
  /**
   * @param {THREE_NS} THREE
   * @param {{p: number[], q: number[]}} ownerPose
   * @param {number} dist
   * @returns {[number, number, number]}
   */
  function spawnRight(THREE, ownerPose, dist) {
    var q = new THREE.Quaternion(ownerPose.q[0], ownerPose.q[1], ownerPose.q[2], ownerPose.q[3]);
    var right = new THREE.Vector3(1, 0, 0).applyQuaternion(q); right.y = 0; right.normalize();
    var p = ownerPose.p;
    return [p[0] + right.x * dist, 0, p[2] + right.z * dist];
  }

  // Capture a real surface's geometry-defining fields (transform + shape) as a SurfaceSig, for the render
  // apply-gate (surfaceMoved). Snapshots position, rotation, extent, and openings — nothing about styling.
  /**
   * @param {{position?: number[], rotation?: number[]}|undefined} transform
   * @param {{extent?: number[], holes?: {x:number,y:number,w:number,h:number}[]}|undefined} surface
   * @returns {SurfaceSig}
   */
  function surfaceSig(transform, surface) {
    var t = transform || {}, s = surface || {};
    return {
      p: (t.position || [0, 0, 0]).slice(0, 3),
      r: (t.rotation || [0, 0, 0]).slice(0, 3),
      ext: (s.extent || [1, 1]).slice(0, 2),
      holes: (Array.isArray(s.holes) ? s.holes : []).map(function (h) {
        return { x: +h.x, y: +h.y, w: +h.w, h: +h.h };
      }),
    };
  }

  /** @param {THREE_NS} THREE @param {number[]} r  euler degrees [x,y,z] (YXZ) @returns {import('three').Quaternion} */
  function eulerYXZQuat(THREE, r) {
    var d = THREE.MathUtils.degToRad;
    return new THREE.Quaternion().setFromEuler(new THREE.Euler(d(r[0] || 0), d(r[1] || 0), d(r[2] || 0), "YXZ"));
  }

  // The render apply-gate (docs/local-first-geometry.md §4-6): has a real surface changed ENOUGH to warrant
  // re-laying its mesh + transform? Returns true if position, orientation, extent, or any opening moved past
  // tolerance — the caller then re-applies the WHOLE surface; false ⇒ skip it entirely, so sub-tolerance
  // capture jitter never rebuilds the geometry (the "pop"). Orientation is compared as a true angular
  // distance (euler → quaternion in A-Frame's YXZ order) so euler aliasing/wrap can't hide a real turn.
  // Holes are matched by index (snapInsets emits them in a stable pass). All thresholds tunable.
  /**
   * @param {THREE_NS} THREE
   * @param {SurfaceSig} a   previously-applied signature
   * @param {SurfaceSig} b   candidate new signature
   * @param {{pos?: number, rotDeg?: number, ext?: number}} [tol]   pos m (0.02), rotDeg ° (1), ext m (0.02)
   * @returns {boolean}
   */
  function surfaceMoved(THREE, a, b, tol) {
    tol = tol || {};
    var pT = tol.pos != null ? tol.pos : 0.02;
    var rT = tol.rotDeg != null ? tol.rotDeg : 1.0;
    var eT = tol.ext != null ? tol.ext : 0.02;
    for (var i = 0; i < 3; i++) if (Math.abs((a.p[i] || 0) - (b.p[i] || 0)) > pT) return true;
    for (var j = 0; j < 2; j++) if (Math.abs((a.ext[j] || 0) - (b.ext[j] || 0)) > eT) return true;
    if (a.holes.length !== b.holes.length) return true;              // an opening appeared/disappeared
    for (var k = 0; k < a.holes.length; k++) {
      var ha = a.holes[k], hb = b.holes[k];
      if (Math.abs(ha.x - hb.x) > eT || Math.abs(ha.y - hb.y) > eT ||
          Math.abs(ha.w - hb.w) > eT || Math.abs(ha.h - hb.h) > eT) return true;
    }
    return eulerYXZQuat(THREE, a.r).angleTo(eulerYXZQuat(THREE, b.r)) > rT * Math.PI / 180;
  }

  return { nest: nest, holesAttr: holesAttr, v3: v3, avatarAim: avatarAim, spawnRight: spawnRight,
           surfaceSig: surfaceSig, surfaceMoved: surfaceMoved };
});
