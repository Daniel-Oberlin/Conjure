// @ts-check
// Pure world-model & presence helpers — extracted from conjure-client.js so the parts that are just data
// transforms and geometry (no DOM, no A-Frame) can be strict TYPE-CHECKED (npm run typecheck) and
// unit-tested (tests/js/world-model.test.js). The A-Frame/DOM glue stays in conjure-client.js, which loads
// this as window.WorldModel; the geometry helpers take the THREE module as their first arg (browser passes
// AFRAME.THREE, node tests pass require('three')) — same convention as space-snap.js.

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
   * Is this capture allowed to REGISTER yet, while space selection is unresolved?
   *
   * Entering AR before the location fix lands leaves the client holding the space from its LAST session.
   * Registering against that is registering your room against a different building: measured 2026-09-22,
   * a three-room home matched 9 of the 57 surfaces of a space 700 km away, which reads as a lost lock and
   * raises the "step out of your play area and back in" hint — advice that cannot help, because tracking
   * was never the problem. Six seconds later the fix arrived, the right space matched, and it locked.
   *
   * So: hold while we do not know WHERE we are. The world is already blanked to passthrough in that
   * window (`awaitingSpace`), so nothing is hidden that was not hidden anyway, and the candidate vote
   * runs on its own path — this gates only what comes after it.
   *
   * PATIENCE is the escape hatch, as everywhere else here: if selection never resolves, fall through to
   * the old behaviour rather than holding for ever with nothing said.
   *
   * @param {boolean} awaiting  selection unresolved — no fix yet, or the verdict is not in
   * @param {number} held       consecutive captures already held by this gate
   * @param {number} [patience] captures to hold before giving up on selection
   * @returns {"go"|"hold"|"forced"}
   */
  function selectGate(awaiting, held, patience) {
    if (!awaiting) return "go";
    return held >= (patience != null ? patience : 8) ? "forced" : "hold";
  }

  /**
   * What the passthrough hint should SAY, from the registration status that caused it.
   *
   * One hint served three different causes and gave all of them the same advice. "Step out and back in"
   * forces the headset to re-localize, which is the remedy for a genuinely lost lock and useless for the
   * other two: a room still materializing needs a moment, and a room that does not match the space we
   * hold needs a different space, not a different pose. Following the wrong advice restarts the AR
   * session, which is what made a six-second wait into three attempts.
   *
   * @param {string} stat  `_regStat` — "loading 1/66", "void-loading 3/12", "selecting", "cov=9/57 …"
   * @returns {{text: string, advise: boolean}}
   */
  function relocHint(stat) {
    var s = String(stat || "");
    if (/^(void-)?loading/.test(s)) {
      return { text: "Restoring your room…", advise: false };
    }
    if (/^selecting/.test(s)) {
      return { text: "Finding which space you are in…", advise: false };
    }
    return { text: "Re-localizing…\nstep out of your play area and back in", advise: true };
  }

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
   * A space-less world legitimately has no basis at all; there the raw pose IS the pose.
   */
  function hasFrameBasis(fp) {
    var lp = fp && fp.local, rp = fp && fp.ref;
    return !!(lp && lp.length >= 2 && rp && rp.length >= 2);
  }

  
  /**
   * Which per-room residuals are worth writing to the geometry log this capture?
   *
   * The log's affordability rests on being change-gated — a settled space emits nothing, which is what
   * makes it safe to leave running for weeks (§10). A per-capture residual would post at ~0.5 Hz forever
   * and break that, so a room is reported only when it first has a fit, when its residual moves past
   * `tol`, or when the accept/refuse verdict flips.
   *
   * The verdict is part of the gate deliberately: a room crossing from "would apply" to "would not" is
   * the interesting moment even when the number barely moved, because that is the boundary a future
   * correction would be switching on.
   *
   * Returns the rows to emit and the state to carry forward. Pure, so the decision is testable while the
   * emitter around it is not.
   *
   * @param {Record<string, {before: number, ok: boolean}>} last
   * @param {{room: string, before: number, ok: boolean}[]} rows
   * @param {{tolM?: number}} [opts]
   * @returns {{emit: any[], next: Record<string, {before: number, ok: boolean}>}}
   */
  function roomResidualGate(last, rows, opts) {
    var TOL = (opts && opts.tolM != null) ? opts.tolM : 0.01;
    /** @type {Record<string, {before: number, ok: boolean}>} */ var next = {};
    Object.keys(last || {}).forEach(function (k) { next[k] = last[k]; });
    /** @type {any[]} */ var emit = [];
    (rows || []).forEach(function (r) {
      var was = next[r.room];
      if (was && Math.abs(was.before - r.before) < TOL && was.ok === r.ok) return;
      next[r.room] = { before: r.before, ok: r.ok };
      emit.push(r);
    });
    return { emit: emit, next: next };
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

  /**
   * Throw away the ids minted while the model was moving, the moment it stops.
   *
   * The first version of this RELEASED them instead, on the reasoning that a provisional mint "either
   * matched something real or is a genuine new surface". That is wrong, and 2026-10-08 is what it costs:
   * eleven ids minted at 2.8 s on a bad frame were withheld, then promoted into the space record nine
   * seconds later when the gate opened. The seed grew 68 → 78 while the device only ever reported 66
   * planes, so a dozen surfaces could never be matched again — and the churn that eventually pruned them
   * took genuine styled neighbours with it, including the kitchen's defining floor. One of the false mints
   * is now that room's identity anchor.
   *
   * A mint made on a bad frame does not become true when the frame improves. It has to be **re-decided**:
   * drop it, let the next match run against the good frame, and mint again only if the plane still
   * answers to nothing. By then it almost always matches the reference it should have matched first time.
   *
   * @param {{id?: string}[]} ref            the reference constellation
   * @param {Record<string, any>} provisional  ids minted while unsettled
   * @returns {{ref: {id?: string}[], dropped: string[]}}
   */
  function discardProvisional(ref, provisional) {
    var drop = provisional || {};
    var dropped = /** @type {string[]} */ ([]);
    var kept = (ref || []).filter(function (r) {
      if (r && r.id && drop[r.id]) { dropped.push(r.id); return false; }
      return true;
    });
    return { ref: kept, dropped: dropped };
  }

  /**
   * Is this capture's plane movement EVIDENCE at all, or merely an absence of news?
   *
   * Written because the first version of this gate had no such rule and opened after 906 ms on
   * 2026-09-30, letting through the two styled surfaces and three displaced objects it existed to
   * protect. It read a movement of **exactly zero** and counted it as calm. It was not calm: during
   * loading and selection the capture retries every ~300 ms rather than the usual 2 s, and the device
   * does not republish plane poses that fast, so the probe compared two samples taken between updates.
   * Zero movement over 300 ms means NO NEW DATA, not convergence — and the refinement's own spread was
   * still 64 mm at six seconds, which is what the model was actually doing.
   *
   * So a sample must clear three bars before it counts, and the first is the one that mattered:
   *
   *   - `moveN` pairs must have been compared **across a real interval**. The caller holds each plane's
   *     previous pose until the gap is wide enough, rather than overwriting it every retry — otherwise
   *     the baseline is refreshed faster than the device moves and the gap never opens at all.
   *   - **enough planes**, so one stale plane cannot speak for a room.
   *   - the plane set is **not still growing**. 26 planes of an expected 67 at 1 ms and 66 by 300 ms is
   *     the loading burst; nothing that arrives mid-burst says anything about whether the model is done.
   *
   * Zero IS allowed to count once those hold: a device that has converged may legitimately republish
   * identical poses, and refusing that would mean never settling and never recording a new surface again.
   *
   * @param {number} moveSum   summed squared displacement over compared planes (m²)
   * @param {number} moveN     how many planes were compared across a wide-enough gap
   * @param {number} seen      planes in this capture
   * @param {boolean} grew     the plane set is still filling in
   * @param {{minPlanes?: number, minFrac?: number}} [opts]
   * @returns {number|null}    RMS movement (m), or null for "no evidence either way"
   */
  function settleRms(moveSum, moveN, seen, grew, opts) {
    opts = opts || {};
    var minPlanes = opts.minPlanes != null ? opts.minPlanes : 4;
    var minFrac = opts.minFrac != null ? opts.minFrac : 0.5;
    if (grew) return null;
    if (moveN < minPlanes) return null;
    if (moveN < minFrac * (seen || 0)) return null;
    return Math.sqrt(moveSum / moveN);
  }

  /**
   * Has the headset's own model stopped moving?
   *
   * `rms` is how far the DEVICE moved its own planes since the last capture — not how far they sit from
   * our stored copy. The difference decides whether this gate can ever open: distance-from-stored stays
   * large when the room genuinely changed, so a gate built on it holds for ever or needs a timeout, and
   * a timeout is exactly what ate `real_wall_157` on 2026-09-29. Self-movement goes quiet as soon as the
   * device stops revising, whatever the room did — and it needs no reference at all, so it works on a
   * first capture, in a new space, and for a guest with nothing stored.
   *
   * A RUN of quiet captures, never one: a settle passes through small values on its way down, and one
   * sample cannot tell "converged" from "momentarily crossing zero". `rms == null` (nothing comparable
   * yet) resets rather than counts — absence of evidence is not calm.
   *
   * Leaving is immediate where arriving is patient, because the costs are not symmetric: one capture of
   * real movement is enough to make a prune wrong, while one quiet capture is not enough to make it right.
   *
   * @param {number} run        consecutive quiet captures so far
   * @param {boolean} settled   whether it currently counts as settled
   * @param {number|null} rms   metres the device moved its own planes since the last capture
   * @param {{quiet?: number, need?: number}} [opts]
   * @returns {{run: number, settled: boolean, became: boolean|null}}  `became` is non-null only on a change
   */
  function settleGate(run, settled, rms, opts) {
    opts = opts || {};
    var quiet = opts.quiet != null ? opts.quiet : 0.004;
    var need = opts.need != null ? opts.need : 3;
    var next = (rms == null) ? 0 : (rms <= quiet ? (run || 0) + 1 : 0);
    var now = next >= need;
    return { run: next, settled: now, became: now === !!settled ? null : now };
  }

  /**
   * Is this capture complete enough to assign IDENTITY from? `detectedPlanes` is the persisted Room Setup
   * delivered wholesale rather than filling in as you look around, so a settled session sees essentially
   * all of it and a small fraction means the Quest has not finished restoring the room.
   *
   * Acting on a partial capture is what re-mints walls: `register` accepts a lock at 30% coverage, and a
   * frame solved from a third of the geometry was measured ~17 cm out in x/z — past `matchWall`'s
   * perpendicular tolerance, so the wall is rejected, a new id minted, and the original pruned with its
   * styling. Two device sessions that started at 4 and 16 planes (of 58) churned; one that started at 58
   * did not.
   *
   * `"forced"` is a deadlock escape, not a tuning outcome: a room that has genuinely SHRUNK would otherwise
   * never clear the gate and never be able to post the removal — the wall-less-seed deadlock in a new
   * costume. It reports itself so a forced pass is never mistaken for a healthy one.
   *
   * @param {number} planes    planes in this capture
   * @param {number} expect    surfaces the persisted seed says exist (0 when there is no seed yet)
   * @param {number} held      consecutive captures already held by this gate
   * @param {{frac?: number, patience?: number, minSeed?: number}} [opts]
   * @returns {"go"|"hold"|"forced"}
   */
  function loadGate(planes, expect, held, opts) {
    opts = opts || {};
    var frac = opts.frac != null ? opts.frac : 0.6;
    var patience = opts.patience != null ? opts.patience : 15;
    var minSeed = opts.minSeed != null ? opts.minSeed : 8;
    if (!(expect >= minSeed)) return "go";          // nothing to compare against — establishing, or a void world
    if (planes >= frac * expect) return "go";
    return held >= patience ? "forced" : "hold";
  }

  /**
   * @typedef {Object} VoidGateState
   * @property {number} prev     vertical-plane count at the previous capture
   * @property {number} max      count at the last successful establish (0 = never established)
   * @property {boolean} everGo  has this session ever established a frame
   * @property {number} held     consecutive captures already held by this gate
   */

  /**
   * The void-world sibling of `loadGate`: is this capture complete enough to ESTABLISH the canonical frame
   * from? `loadGate` cannot answer it — it derives `expect` from the world doc's surfaces and a void world
   * has none, so `expect = 0` and it returns "go" immediately, which is why the protection built for
   * surface-churn never applied here.
   *
   * The fault it prevents is bigger than the identity churn `loadGate` guards. `canonicalFrame` picks its
   * forward direction as the grid direction nearest the LARGEST wall's normal, and on a partial capture a
   * different wall is largest — measured on the golden room, every subset from 3 to 12 of 30 verticals
   * flipped theta by 180°, displacing content 4.5–5.1 m at 2.2 m from the frame origin. The centroid shift
   * (0.2–1.8 m) rides on top. At the full 30 both are exactly 0, so the whole fault is acting on a partial
   * capture — which is what a Meta-button recenter causes, since the reset forces a capture on the very
   * next frame.
   *
   * Two modes, because the right test depends on whether the session knows anything yet:
   *
   * - **Never established** — there is no expectation to compare against, so wait for the count to STOP
   *   GROWING (`walls <= prev`). Costs one extra capture in the steady case and rides out an arrival
   *   sequence like 4 → 16 → 30 without acting on the first two.
   * - **Established before** — the room did not change when you pressed a button, so the count at the last
   *   establish is a real expectation and a fraction of it is enough. This is what makes recovery after a
   *   recenter fast (usually one or two captures) rather than making you wait out the plateau again.
   *
   * `minWalls` is an absolute floor under both: two or three planes cannot pin a room's frame however
   * settled they look. `"forced"` is the same deadlock escape `loadGate` has, for a room that genuinely
   * shrank (a re-scan with fewer walls would otherwise never clear the fraction test), and it reports
   * itself so a forced establish is never mistaken for a healthy one.
   *
   * Pure: the caller owns the state and advances it (`voidGateAdvance`).
   *
   * @param {number} walls   vertical planes in this capture
   * @param {VoidGateState} st
   * @param {{frac?: number, minWalls?: number, patience?: number}} [opts]
   * @returns {"go"|"hold"|"forced"}
   */
  function voidFrameGate(walls, st, opts) {
    opts = opts || {};
    var frac = opts.frac != null ? opts.frac : 0.6;
    var minWalls = opts.minWalls != null ? opts.minWalls : 6;
    var patience = opts.patience != null ? opts.patience : 8;
    var no = /** @type {"forced"|"hold"} */ (st.held >= patience ? "forced" : "hold");
    if (walls < minWalls) return no;                       // too few to pin a frame, however stable
    if (st.everGo) return walls >= frac * st.max ? "go" : no;
    return walls <= st.prev ? "go" : no;                   // still filling in ⇒ the count is still growing
  }

  /**
   * Advance the gate's state after a verdict. `prev` and `held` track the current attempt; `max`/`everGo`
   * are the session's memory and deliberately SURVIVE a tracking reset — the room does not change when you
   * press the recenter button, so what the room looked like is still the right expectation. They are
   * cleared on a world switch instead (`resetFrame`), where the room genuinely may be a different one.
   *
   * @param {VoidGateState} st
   * @param {number} walls
   * @param {"go"|"hold"|"forced"} verdict
   * @returns {VoidGateState}
   */
  function voidGateAdvance(st, walls, verdict) {
    if (verdict === "hold") return { prev: walls, max: st.max, everGo: st.everGo, held: st.held + 1 };
    // A healthy establish RATCHETS UP only: taking `max = walls` unconditionally would let a run of
    // slightly-thin captures walk the expectation down one capture at a time until the gate stopped
    // gating. A FORCED establish is the one case that re-baselines downward, because that is precisely
    // the "the room genuinely shrank" escape and leaving `max` high would deadlock the next reset too.
    return { prev: walls,
             max: verdict === "forced" ? walls : Math.max(st.max, walls),
             everGo: true,
             held: 0 };
  }

  /** @returns {VoidGateState} */
  function voidGateInit() { return { prev: 0, max: 0, everGo: false, held: 0 }; }

  // ---- derived-frame gestures (docs/specs/dynamics.md §8b — `grab`'s skybox/void modes) ------------
  // Pure planar maths, extracted here rather than left in grab.js so it can be tested: XR interaction can't
  // be exercised in a unit test, but the two identities below are exactly where a sign error hides, and a
  // sign error in either one sends the sky or the whole world the wrong way with no clue as to which term
  // was wrong.

  // Rotate a horizontal [x, z] pair by `a` radians about gravity (+Y), matching Vector3.applyAxisAngle:
  // Rodrigues with k = (0,1,0) gives (x·cos + z·sin, y, z·cos − x·sin).
  /** @param {number} x  @param {number} z  @param {number} a  @returns {number[]} */
  function rotXZ(x, z, a) {
    var c = Math.cos(a), s = Math.sin(a);
    return [x * c + z * s, -x * s + z * c];
  }

  // A void world's content rides a rigid horizontal transform, p' = R(yaw)·p + offset. Apply an additional
  // yaw of `dDeg` about the horizontal point `pivot` ([x, z]) and return the equivalent transform.
  //
  // The pivot is RESOLVED INTO THE OFFSET rather than stored: rotating about v equals rotating about the
  // origin plus a translation (v − R·v). Storing the pivot would mean re-deriving it from the LIVE viewer
  // position on every capture, so content would drift as you walked around. Composing:
  //   p'' = v + R_d(p' − v) = R_d·R·p + R_d·T + (v − R_d·v)
  /**
   * @param {number} yawDeg  @param {number[]} offset  @param {number} dDeg  @param {number[]} pivot
   * @returns {{yaw: number, offset: number[]}}
   */
  function yawAboutPivot(yawDeg, offset, dDeg, pivot) {
    var a = dDeg * Math.PI / 180;
    var t = rotXZ(offset[0], offset[1], a), v = rotXZ(pivot[0], pivot[1], a);
    return { yaw: yawDeg + dDeg, offset: [t[0] + pivot[0] - v[0], t[1] + pivot[1] - v[1]] };
  }

  // The skybox mode's floor drag, decomposed in POLAR coordinates about the sky's centre: how far the
  // grabbed point moved radially is a SCALE ratio, how far it swung is a YAW. A diagonal drag does both.
  //
  // `eps` floors both radii for the ARITHMETIC only — it is not a minimum engage radius (deliberately
  // rejected: you learn a turntable's feel faster than a rule about where you may touch it). Without it
  // r/r0 at r0 ≈ 0 is Infinity, and a non-finite value that reaches a transform blanks the scene branch and
  // stays blanked.
  //
  // dYaw is (a0 − a) because a +Y rotation by θ carries a direction at atan2(z, x) = a to a − θ, so to drag
  // the grabbed feature from a0 round to a the sky must turn by a0 − a.
  /**
   * @param {number[]} centre  @param {number[]} grab  @param {number[]} now  @param {number} [eps]
   * @returns {{dYaw: number, scale: number}}
   */
  function polarDrag(centre, grab, now, eps) {
    var e = typeof eps === "number" && eps > 0 ? eps : 1e-6;
    var r0 = Math.max(e, Math.hypot(grab[0] - centre[0], grab[1] - centre[1]));
    var r = Math.max(e, Math.hypot(now[0] - centre[0], now[1] - centre[1]));
    var a0 = Math.atan2(grab[1] - centre[1], grab[0] - centre[0]);
    var a = Math.atan2(now[1] - centre[1], now[0] - centre[0]);
    return { dYaw: (a0 - a) * 180 / Math.PI, scale: r / r0 };
  }

  return { hasFrameBasis: hasFrameBasis, levelDeviation: levelDeviation, roomResidualGate: roomResidualGate, loadGate: loadGate, settleGate: settleGate, settleRms: settleRms, discardProvisional: discardProvisional,
           rotXZ: rotXZ, yawAboutPivot: yawAboutPivot, polarDrag: polarDrag,
           nest: nest, holesAttr: holesAttr, v3: v3, avatarAim: avatarAim, spawnRight: spawnRight,
           shouldSpawnGuest: shouldSpawnGuest, isCaptureAuthority: isCaptureAuthority,
           relocInit: relocInit, relocStep: relocStep, relocHint: relocHint,
           selectGate: selectGate,
           surfaceSig: surfaceSig, surfaceMoved: surfaceMoved,
           surfacePoseMoved: surfacePoseMoved, surfaceShapeChanged: surfaceShapeChanged,
           advanceSig: advanceSig, slewAlpha: slewAlpha, slewSettled: slewSettled,
           voidFrameGate: voidFrameGate, voidGateAdvance: voidGateAdvance, voidGateInit: voidGateInit };
});
