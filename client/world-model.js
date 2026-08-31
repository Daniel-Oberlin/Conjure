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

  // A patch `env` op arrives as a FLAT map of dotted paths ({"spacePresentation.active": true, "sky.color": "#000"}).
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

  // May this client teleport its rig next to the owner (the desktop-guest spawn that `spawnRight` feeds)?
  //
  // The rig MUST stay at the origin in an XR session — the whole A-Frame world frame is aligned to the
  // headset's reference space by keeping it there (see index.html) — so moving it displaces the CAMERA
  // while world content and the raw-XR controller beams stay put: you view the scene from a metre to the
  // side of your own hands.
  //
  // The subtle part is WHEN this is asked. It used to gate on "am I in an XR session right now", which is
  // false for the first few seconds after EVERY page load — including a headset's. An owner's presence
  // arriving in that window teleported a headset guest, and the spawn latches, so entering AR moments
  // later inherited the offset permanently. So the question is CAPABILITY, not current state: a device
  // that can do immersive-ar is a headset that simply hasn't entered yet, and must never be spawned.
  /**
   * @param {{spawned: boolean, hasOwnerPose: boolean, me: string|null, owner: string|null,
   *          presenting: boolean, arCapable: boolean}} s
   * @returns {boolean}
   */
  function shouldSpawnGuest(s) {
    if (s.spawned) return false;              // once only, per page load
    if (!s.hasOwnerPose) return false;        // nothing to spawn relative to
    if (!s.me || s.me === s.owner) return false;   // only a GUEST spawns, and only beside someone else
    if (s.presenting) return false;           // already in a session — the headset positions you
    if (s.arCapable) return false;            // AR-capable ⇒ a headset pre-session, NOT a desktop viewer
    return true;
  }

  // Am I the capture authority — the client allowed to AUTHOR this space's geometry? Everyone else is a
  // register-only guest: it re-seeds its reference wholesale from the authority each capture and never
  // establishes a frame of its own.
  //
  // `owner` is null until the first snapshot lands. That used to read as "yes" so authoring was never
  // briefly locked out — but the same answer let a GUEST, capturing in that window, skip the re-seed and
  // establish its OWN frame. It then keeps registering against its own reference instead of the shared
  // one, and the room renders at the wrong orientation. Unknown must mean "not yet", not "yes": the
  // lockout it avoids is one snapshot long, and nothing should be authored into a world you can't name.
  //
  // An empty `me` IS the dev/default single user (matching the server treating a missing X-Conjure-User
  // as the owner) — but still only once the owner is known.
  /** @param {string|null|undefined} me  @param {string|null|undefined} owner  @returns {boolean} */
  function isCaptureAuthority(me, owner) {
    if (!owner) return false;          // no snapshot yet ⇒ we don't know; never assume it's us
    return !me || me === owner;        // dev/default user, or a name match
  }

  // --- Lost-lock tracking: when to reveal passthrough + the "step out and back in" hint ---------------
  // A capture that can't register HOLDS the last good frame and skips the render, so from that very
  // capture the room on screen is stale — after a relocalization (sleep, boundary trip) it is visibly
  // rotated. The hint is what explains that, so its timing has to track the failure, not lag it.
  //
  // Two things used to stop it appearing at all:
  //   • ONE lucky capture zeroed the elapsed timer, so a FLICKERING lock — the normal shape after a
  //     sleep — never accumulated the grace period. Recovery now needs OK_STREAK consecutive good
  //     captures, so a flicker keeps counting as lost.
  //   • The grace was a flat 3 s whether or not we had ever been locked. Once a lock HAS been held the
  //     room is known-stale, so the grace is short; while still acquiring it stays long, so a cold
  //     start doesn't flash a hint at someone who is simply walking in.
  var RELOC_STALE_MS = 1200;    // had a lock, lost it → the room is wrong NOW
  var RELOC_ACQUIRE_MS = 3000;  // never locked yet → still acquiring, don't nag
  var RELOC_OK_STREAK = 2;      // consecutive good captures required to declare recovery

  /**
   * @typedef {Object} RelocState
   * @property {number|null} lostSince   time of the first consecutive failure (null = locked)
   * @property {number} okStreak    consecutive successful captures
   * @property {boolean} hadLock    have we ever held a lock? (grace is shorter once we have)
   * @property {boolean} showing    is the passthrough fallback + hint up?
   */
  /** @returns {RelocState} */
  function relocInit() { return { lostSince: null, okStreak: 0, hadLock: false, showing: false }; }

  /**
   * @param {RelocState} st
   * @param {"lost"|"ok"} ev
   * @param {number} time   ms, monotonic (A-Frame tick time)
   * @returns {RelocState}
   */
  function relocStep(st, ev, time) {
    var s = { lostSince: st.lostSince, okStreak: st.okStreak, hadLock: st.hadLock, showing: st.showing };
    if (ev === "lost") {
      s.okStreak = 0;
      if (s.lostSince === null) s.lostSince = time;   // NOT `!s.lostSince`: tick time starts at ~0,
                                                      // and 0 is a real timestamp, not "still locked"
      if (!s.showing && time - s.lostSince >= (s.hadLock ? RELOC_STALE_MS : RELOC_ACQUIRE_MS)) s.showing = true;
      return s;
    }
    s.hadLock = true;
    s.okStreak += 1;
    if (s.okStreak >= RELOC_OK_STREAK) { s.lostSince = null; s.showing = false; }
    return s;
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

  // The render apply-gate (docs/specs/spaces-geometry.md §9.1): has a real surface changed ENOUGH to warrant
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
    return surfaceShapeChanged(a, b, tol) || surfacePoseMoved(THREE, a, b, tol);
  }

  // POSE half of the apply-gate: did the surface only DRIFT (position or orientation past tolerance) while
  // keeping the SAME physical shape? A drift needs just a cheap transform re-lay — NOT a mesh rebuild. This
  // is the common case under tracking refinement (esp. while walking), so gating the expensive geometry
  // rebuild out of it is what kills the per-capture "whole-room re-triangulation" frame spike.
  /**
   * @param {THREE_NS} THREE
   * @param {SurfaceSig} a   previously-applied signature
   * @param {SurfaceSig} b   candidate new signature
   * @param {{pos?: number, rotDeg?: number, ext?: number}} [tol]   pos m (0.02), rotDeg ° (1)
   * @returns {boolean}
   */
  function surfacePoseMoved(THREE, a, b, tol) {
    tol = tol || {};
    var pT = tol.pos != null ? tol.pos : 0.02;
    var rT = tol.rotDeg != null ? tol.rotDeg : 1.0;
    for (var i = 0; i < 3; i++) if (Math.abs((a.p[i] || 0) - (b.p[i] || 0)) > pT) return true;
    return eulerYXZQuat(THREE, a.r).angleTo(eulerYXZQuat(THREE, b.r)) > rT * Math.PI / 180;
  }

  // SHAPE half of the apply-gate: did the extent or an opening change past tolerance? Only THIS warrants
  // re-triangulating the (holed-)wall mesh via applySurfaceGeometry. Openings are matched by index
  // (snapInsets emits them in a stable pass). No THREE needed — pure scalar/box comparison.
  /**
   * @param {SurfaceSig} a   previously-applied signature
   * @param {SurfaceSig} b   candidate new signature
   * @param {{pos?: number, rotDeg?: number, ext?: number}} [tol]   ext m (0.02)
   * @returns {boolean}
   */
  function surfaceShapeChanged(a, b, tol) {
    tol = tol || {};
    var eT = tol.ext != null ? tol.ext : 0.02;
    for (var j = 0; j < 2; j++) if (Math.abs((a.ext[j] || 0) - (b.ext[j] || 0)) > eT) return true;
    if (a.holes.length !== b.holes.length) return true;              // an opening appeared/disappeared
    for (var k = 0; k < a.holes.length; k++) {
      var ha = a.holes[k], hb = b.holes[k];
      if (Math.abs(ha.x - hb.x) > eT || Math.abs(ha.y - hb.y) > eT ||
          Math.abs(ha.w - hb.w) > eT || Math.abs(ha.h - hb.h) > eT) return true;
    }
    return false;
  }

  // Advance the last-APPLIED signature baseline (el._geoSig) after an apply-gate re-lay — but ONLY the half
  // that was actually re-laid. A SHAPE change enqueues a geometry rebuild (the slice pump will materialize
  // it), so the whole signature becomes the new baseline. A POSE-ONLY re-lay redraws no geometry, so its
  // SHAPE half (ext/holes) MUST stay pinned to the last-RENDERED shape. Advancing the shape half on a
  // pose-only relay is the regression this fixes: it silently absorbs sub-tolerance extent drift into the
  // baseline every capture, so surfaceShapeChanged then measures against a shape the mesh never drew → the
  // rebuild NEVER fires → the rendered mesh runs away from the true shape over a session. For joinCorners
  // walls (whose position + width move as a matched pair) that splits the two across render epochs, so the
  // wall's ends miss the shared corners → wall∩wall and wall∩ceiling junction seams open (and grow). Holding
  // the shape baseline instead bounds the lag to the extent tolerance and lets it self-heal: once true drift
  // exceeds tolerance, surfaceShapeChanged fires and the mesh catches up.
  /**
   * @param {SurfaceSig|null} prev          the last-applied baseline (null/undefined on first lay)
   * @param {SurfaceSig} sig                this capture's signature
   * @param {boolean} poseMoved             the pose was re-laid this capture
   * @param {boolean} shapeChanged          the geometry was (re)enqueued this capture
   * @returns {SurfaceSig}                  the baseline to store as el._geoSig
   */
  function advanceSig(prev, sig, poseMoved, shapeChanged) {
    if (shapeChanged || !prev) return sig;                          // geometry (re)laid → whole sig is truth
    if (poseMoved) return { p: sig.p, r: sig.r, ext: prev.ext, holes: prev.holes };  // pose only → hold shape
    return prev;                                                    // nothing re-laid → baseline unchanged
  }

  // Pose-smoothing slew (docs/specs/spaces-geometry.md §9.2): the per-frame easing FRACTION for one step, from a
  // smoothing TIME CONSTANT `tau` (seconds) and this frame's `dt` (seconds). `a = 1 - exp(-dt/tau)` is one
  // exponential-moving-average step whose wall-clock settle time depends only on tau — NOT on frame rate or
  // frame-time jitter (two 8 ms steps close the same gap fraction as one 16 ms step). tau<=0 ⇒ a=1 (snap):
  // the disabled default, so an adopt with no tau lands instantly, matching today's behaviour. Clamped to
  // [0,1] so a huge dt (a stall) can't overshoot the target.
  /**
   * @param {number} dt    seconds since last frame
   * @param {number} tau   smoothing time constant (seconds); <=0 disables (returns 1)
   * @returns {number}     easing fraction in [0, 1]
   */
  function slewAlpha(dt, tau) {
    if (!(tau > 0)) return 1;                                       // disabled → snap the whole gap
    var a = 1 - Math.exp(-Math.max(0, dt) / tau);
    return a < 0 ? 0 : a > 1 ? 1 : a;
  }

  // Has an easing entity ARRIVED? The exponential never reaches the target exactly, so a small epsilon snap
  // terminates the slew (the entity leaves the slew set and steady-state cost returns to zero). True once the
  // position gap AND the angular gap are both below their epsilons.
  /**
   * @param {number} posGap   metres between object3D and target position
   * @param {number} angGap   radians between object3D and target orientation
   * @param {number} posEps   position epsilon (m), e.g. 0.001 (1 mm)
   * @param {number} angEps   angular epsilon (rad), e.g. 0.1° in radians
   * @returns {boolean}
   */
  function slewSettled(posGap, angGap, posEps, angEps) {
    return posGap < posEps && angGap < angEps;
  }

  /**
   * Is `framePlanes` a basis a pose may be converted THROUGH — both the local (F_track) walls and the
   * seed (F_ref) walls, each with enough planes to define a frame?
   *
   * One predicate because the two directions must agree. `ConjureFrames.anchorFor` reads only the local
   * walls and `toRef` needs both, so when the two disagreed a grab commit sent a raw local position
   * PLUS a wall-relative anchor — and the receiving client solved that anchor against different walls
   * and teleported the object. Authoring an anchor you cannot convert back through is never right.
   *
   * A room-less world legitimately has no basis at all; there the raw pose IS the pose.
   */
  function hasFrameBasis(fp) {
    var lp = fp && fp.local, rp = fp && fp.ref;
    return !!(lp && lp.length >= 2 && rp && rp.length >= 2);
  }

  /**
   * Per floor/ceiling, how far its LIVE height sits from the SEED's once the whole-space offset is removed
   * — the self-triggering half of the raised-floor investigation (docs/backlogs/spaces-geometry.md).
   *
   * Three facts make this work with no extra persistence and no ground truth:
   *  • Registration solves yaw about gravity plus an x/z translation and never touches y, so height
   *    DIFFERENCES are identical in F_track and F_ref — the stored seed is a valid baseline for a live
   *    capture, directly, across sessions and across devices.
   *  • Subtracting the MEDIAN of the per-surface deltas absorbs any whole-space offset (a different
   *    local-floor origin between sessions, the map settling as a block). Median, not mean, so one badly
   *    wrong floor cannot drag the baseline toward itself and hide.
   *  • What is left is "this surface moved relative to the rest of the space", which is the reported
   *    symptom exactly, and is the one thing that cannot be explained away as drift.
   *
   * Returns [] below 3 comparable surfaces: with two, a whole-space shift and one bad plane are the same
   * number, and guessing between them is worse than staying quiet.
   *
   * `basisIds` narrows which surfaces form the MEDIAN without narrowing which get a deviation. Floors and
   * ceilings are the only clean baseline — a wall's stored height went through `sealWalls` before it was
   * posted while a live one has not, and inset heights are sparse — so a caller that wants deviations for
   * everything still wants the baseline computed from those. Omitted ⇒ every entry counts, as before.
   *
   * @param {Record<string, number>} live            id → height this capture
   * @param {Record<string, {y: number, sem?: string}>} seed   id → height in the stored seed
   * @param {string[]} [basisIds]                    ids whose drifts form the median (default: all)
   * @returns {{id: string, sem: string|undefined, live: number, seed: number, d: number, dev: number}[]}
   */
  function levelDeviation(live, seed, basisIds) {
    /** @type {{id: string, sem: string|undefined, live: number, seed: number, d: number, dev: number}[]} */
    var out = [];
    Object.keys(live || {}).forEach(function (id) {
      var s = seed && seed[id];
      if (!s) return;
      out.push({ id: id, sem: s.sem, live: live[id], seed: s.y, d: live[id] - s.y, dev: 0 });
    });
    var basis = out;
    if (basisIds && basisIds.length) {
      /** @type {Record<string, number>} */ var want = {};
      basisIds.forEach(function (id) { want[id] = 1; });
      basis = out.filter(function (o) { return want[o.id]; });
    }
    if (basis.length < 3) return [];
    var ds = basis.map(function (o) { return o.d; }).sort(function (a, b) { return a - b; });
    var mid = ds.length >> 1;
    var median = ds.length % 2 ? ds[mid] : (ds[mid - 1] + ds[mid]) / 2;
    out.forEach(function (o) { o.dev = o.d - median; });
    return out;
  }

  return { hasFrameBasis: hasFrameBasis, levelDeviation: levelDeviation,
           nest: nest, holesAttr: holesAttr, v3: v3, avatarAim: avatarAim, spawnRight: spawnRight,
           shouldSpawnGuest: shouldSpawnGuest, isCaptureAuthority: isCaptureAuthority,
           relocInit: relocInit, relocStep: relocStep,
           surfaceSig: surfaceSig, surfaceMoved: surfaceMoved,
           surfacePoseMoved: surfacePoseMoved, surfaceShapeChanged: surfaceShapeChanged,
           advanceSig: advanceSig, slewAlpha: slewAlpha, slewSettled: slewSettled };
});
