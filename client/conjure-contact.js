/* global AFRAME, THREE */
// ConjureContact — "what is my hand touching", as a QUERY a module asks rather than a cast it runs.
// Spec: docs/specs/input.md §10. Plan: docs/plans/hands.md phase 4.
//
// WHY A QUERY AND NOT A RAYCAST. Before this, the one module that reacted to a hand did it by taking
// `pointer.fingertip`, converting it into its own local frame, and testing a depth — thirty lines of
// geometry per module, each with its own idea of what "touching" means, and each reading exactly one of
// the twenty-five joints because that was the only one published. Three modules would have been three
// answers to the same question.
//
// WHY CAPSULES AND NOT POINTS. A fingertip JOINT can be outside a volume while the finger is inside it:
// the joints are ~25 mm apart and the flesh is ~8 mm thick, so testing points leaves a centimetre of
// finger that touches nothing. Each of the 24 bones is a capsule — its two joints and the larger of
// their reported radii — which is the shape the hand actually is.
//
// WHY IT READS THE JOINTS FROM `ConjurePointers` AND NOT FROM THE XR FRAME. One read per frame is the
// whole premise of that layer, and this is its fourth consumer. (`hand-rig` and `?hands=fit` keep their
// own reads on purpose: one drives a skeleton and the other exists to look at the raw frame.)
//
// WHAT IT DELIBERATELY DOES NOT DO: events, and the wire. A touch is a CAUSE, and `ConjureBus` already
// carries causes to peers (docs/specs/dynamics.md §2, tier B) — so a module broadcasts its own touch as
// it always did, every client simulates from it, and nothing about peers' fingers ever goes on the wire.

(function () {
  "use strict";
  if (!window.AFRAME || window.ConjureContact) return;

  var FINGERS = ["index", "middle", "ring", "pinky"];
  var CHAINS = [["wrist", "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal",
                 "thumb-tip"]];
  FINGERS.forEach(function (f) {
    CHAINS.push(["wrist", f + "-finger-metacarpal", f + "-finger-phalanx-proximal",
                 f + "-finger-phalanx-intermediate", f + "-finger-phalanx-distal", f + "-finger-tip"]);
  });
  //: The 24 bones as [parent, child]. Same order and same membership as `conjure/hands.py`'s SEGMENTS
  //: and `client/hands-fit.js`, so a number from one can be read against a number from another.
  var BONES = [];
  CHAINS.forEach(function (c) {
    for (var i = 0; i < c.length - 1; i++) BONES.push([c[i], c[i + 1]]);
  });

  var FALLBACK_RADIUS = 0.008;   // when the UA supplies none: ~a fingertip, and the log says it happened
  var TERNARY_STEPS = 14;        // see `_segmentToBox` — 24 bones × 14 is ~340 cheap tests per hand

  //: Speed is the only thing here that needs memory, and it needs exactly one frame of it — but it
  //: needs TWO SLOTS to hold one frame, which took two goes to get right.
  //:
  //: A single `previous` map, written at the end of each query, works for one consumer and breaks for
  //: the second: the first module of the frame overwrites `previous` with THIS frame's positions, so
  //: the second module compares the frame against itself and every speed reads zero. Guarding the
  //: write on a frame stamp does not help — by then the damage is the same write.
  //:
  //: So `thisFrame` is captured once per distinct frame and the one it displaces becomes `lastFrame`,
  //: which every speed is measured against. However many modules ask, they all compare against the
  //: same genuinely-previous frame.
  var lastFrame = {}, thisFrame = {}, rememberedAt = -1;
  var seen = {};

  function log(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try { console.log("[contact] " + msg); } catch (e) {}
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: "contact", msg: msg }) }).catch(function () {});
    } catch (e) {}
  }
  function once(k, msg) { if (seen[k]) return; seen[k] = 1; log(msg); }
  function now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

  /** Distance from a point to an axis-aligned box centred at the origin with half-extents `h`. */
  function pointToBox(p, h) {
    var dx = Math.abs(p.x) - h.x, dy = Math.abs(p.y) - h.y, dz = Math.abs(p.z) - h.z;
    dx = Math.max(dx, 0); dy = Math.max(dy, 0); dz = Math.max(dz, 0);
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
  }

  /**
   * Closest approach of the segment a→b to the box, and where on the segment it happens.
   *
   * Distance from a point to a convex box is a convex function, and it stays convex along a straight
   * segment — so a ternary search on `t` converges on the true minimum rather than hunting around one.
   * Fourteen steps narrows a 25 mm bone to about 2 µm, which is four orders of magnitude finer than the
   * 8 mm radius being compared against.
   *
   * Sampling at fixed intervals was the first idea and is worse for a reason worth keeping: it can only
   * ever MISS a contact, never invent one, and the size of the miss depends on the bone — so the false
   * negatives would have been longest exactly on the long bones, which are most of the hand.
   */
  function _segmentToBox(a, b, h, tmp) {
    var lo = 0, hi = 1;
    for (var i = 0; i < TERNARY_STEPS; i++) {
      var m1 = lo + (hi - lo) / 3, m2 = hi - (hi - lo) / 3;
      var d1 = pointToBox(tmp.copy(a).lerp(b, m1), h);
      var d2 = pointToBox(tmp.copy(a).lerp(b, m2), h);
      if (d1 < d2) hi = m2; else lo = m1;
    }
    var t = (lo + hi) / 2;
    return { t: t, distance: pointToBox(tmp.copy(a).lerp(b, t), h), at: tmp.clone() };
  }

  /** Every tracked hand's joints and radii this frame, from the one reader. */
  function handsOf(sceneEl) {
    var CP = window.ConjurePointers;
    if (!CP) return [];
    var list = (CP.acting || CP.list).call(CP, sceneEl) || [];
    return list.filter(function (p) { return p.isHand && p.joints; });
  }

  /**
   * How fast a joint is moving, in m/s, from the one previous frame kept per hand.
   *
   * A speed rather than a velocity because every consumer so far wants "was that a poke or a rest" and
   * none wants the direction — and a direction invites a consumer to integrate it, which is the sort of
   * thing that belongs in the consumer's own frame rather than in a shared reading.
   */
  function speedOf(key, joint, at, t) {
    var prev = lastFrame[key];
    if (!prev || !prev.joints[joint] || prev.t >= t) return 0;
    var dt = (t - prev.t) / 1000;
    if (dt <= 0 || dt > 0.25) return 0;                 // a gap that long is a dropped track, not motion
    return prev.joints[joint].distanceTo(at) / dt;
  }

  /**
   * Roll the frame, at most once per frame, BEFORE any speed is read.
   *
   * A hand that stopped being tracked drops out for free, because `thisFrame` is rebuilt from the live
   * hands each time — and it has to drop out: re-acquired, a stale position reads as having crossed
   * the room in one frame, which is the shape of a very fast false poke.
   */
  function roll(hands, t) {
    if (rememberedAt === t) return;
    lastFrame = thisFrame;
    thisFrame = {};
    for (var i = 0; i < hands.length; i++) {
      var p = hands[i], keep = {};
      for (var n in p.joints) keep[n] = p.joints[n].clone();
      thisFrame[p.key] = { joints: keep, t: t };
    }
    rememberedAt = t;
  }

  window.ConjureContact = {
    BONES: BONES,
    FALLBACK_RADIUS: FALLBACK_RADIUS,

    /** Drop the remembered frame. Only a test needs this; a live hand self-cleans when it vanishes. */
    forget: function () { lastFrame = {}; thisFrame = {}; rememberedAt = -1; },

    /**
     * Which of a hand's bones are inside (or within `slack` of) a box, in that box's OWN frame.
     *
     * `object3D` is anything with a world matrix — a module's own entity, usually — and `half` its
     * half-extents in its local frame. A plane with a touch depth IS a box, which is why there is no
     * separate plane query: `water` asks for `[width/2, height/2, touchDepth]` and gets back UV-ready
     * local coordinates, having deleted its own thirty lines of frame conversion.
     *
     * Returns one hit per BONE, not per joint, because a bone is what a hand touches with. Each hit
     * carries the point in local space (the consumer's own frame, ready to use), the depth of
     * penetration, and the speed of the nearer joint.
     *
     * `opts.joints` restricts which joints may contribute — how a module that already had narrower
     * behaviour keeps it exactly, rather than silently gaining a hand's worth of new contacts the day
     * it migrates.
     */
    inBox: function (object3D, half, opts) {
      opts = opts || {};
      var sceneEl = opts.sceneEl || (object3D && object3D.el && object3D.el.sceneEl)
        || document.querySelector("a-scene");
      var hands = handsOf(sceneEl);
      if (!object3D || !hands.length) return [];
      var T = AFRAME.THREE;
      var h = { x: Math.abs(half[0]), y: Math.abs(half[1]), z: Math.abs(half[2]) };
      var slack = opts.slack == null ? 0 : opts.slack;
      var only = opts.joints || null;
      var inv = new T.Matrix4().copy(object3D.matrixWorld).invert();
      var a = new T.Vector3(), b = new T.Vector3(), tmp = new T.Vector3();
      var t = now(), out = [];
      roll(hands, t);                    // before any speed is read, and once per frame however many ask

      for (var i = 0; i < hands.length; i++) {
        var p = hands[i];
        if (opts.owner && !p.availableTo(opts.owner)) continue;   // someone else holds this pointer
        for (var k = 0; k < BONES.length; k++) {
          var an = BONES[k][0], bn = BONES[k][1];
          if (only && only.indexOf(an) === -1 && only.indexOf(bn) === -1) continue;
          var aw = p.joints[an], bw = p.joints[bn];
          if (!aw || !bw) continue;
          // Into the box's frame FIRST, so the box is axis-aligned and the arithmetic below is the
          // simple case. It is also the frame the consumer wants the answer in.
          a.copy(aw).applyMatrix4(inv);
          b.copy(bw).applyMatrix4(inv);
          var ra = p.radii ? p.radii[an] : null, rb = p.radii ? p.radii[bn] : null;
          if (ra == null && rb == null) once("noradius", "the runtime supplies no joint radius — "
            + "falling back to " + (FALLBACK_RADIUS * 1000) + " mm per bone");
          var r = Math.max(ra == null ? FALLBACK_RADIUS : ra, rb == null ? FALLBACK_RADIUS : rb);
          var near = _segmentToBox(a, b, h, tmp);
          var reach = r + slack;
          if (near.distance > reach) continue;
          out.push({
            key: p.key, hand: p.handedness, bone: bn, from: an,
            local: near.at.clone(),                    // in the box's frame: UV-ready, no conversion
            t: near.t,                                 // where along the bone, 0 at `from`
            distance: near.distance,                   // surface-to-surface would be this minus r
            depth: reach - near.distance,              // how far in; > 0 by construction here
            radius: r,
            speed: speedOf(p.key, near.t < 0.5 ? an : bn, near.t < 0.5 ? aw : bw, t)
          });
        }
      }
      out.sort(function (x, y) { return y.depth - x.depth; });     // deepest first: the likely intent
      return out;
    },

    /** The same, for a sphere. `centre` is world-space. */
    inSphere: function (centre, radius, opts) {
      opts = opts || {};
      var T = AFRAME.THREE;
      var o = new T.Object3D();
      o.position.copy(centre);
      o.updateMatrixWorld(true);
      // A sphere as a box would be wrong at the corners, so the box is a point and `slack` carries the
      // radius: distance-to-a-point is exactly what a sphere test is.
      return this.inBox(o, [0, 0, 0], Object.assign({}, opts, {
        slack: radius + (opts.slack || 0),
        sceneEl: opts.sceneEl || document.querySelector("a-scene")
      }));
    },

    /** Every tracked hand's bones as capsules in WORLD space — for a consumer that wants to draw them. */
    capsules: function (sceneEl) {
      var out = [];
      handsOf(sceneEl).forEach(function (p) {
        BONES.forEach(function (pair) {
          var a = p.joints[pair[0]], b = p.joints[pair[1]];
          if (!a || !b) return;
          var ra = p.radii ? p.radii[pair[0]] : null, rb = p.radii ? p.radii[pair[1]] : null;
          out.push({ key: p.key, from: pair[0], to: pair[1], a: a, b: b,
                     radius: Math.max(ra == null ? FALLBACK_RADIUS : ra,
                                      rb == null ? FALLBACK_RADIUS : rb) });
        });
      });
      return out;
    },
  };
})();
