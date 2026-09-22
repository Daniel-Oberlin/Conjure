/* global AFRAME */
// ConjurePointers — the ONE reader of XR input, and the seam that keeps controls out of module code.
// Spec: docs/specs/input.md (its own area since 2026-09-17 — decisions.md §29).
//
// Before this, every consumer (controller-beams, grab, water, occlusion) walked `session.inputSources`
// itself and hard-coded button indices. That is the same duplication-breeds-drift trap as the
// server/client geometry math: four places to fix when a mapping changes, and a control scheme you can
// only discover by reading source. It also made control *sharing* impossible to express — two modules
// wanting the same button "worked" only because grab happened to use GRIP while water used TRIGGER.
//
// Two jobs:
//   1. Read the XR frame ONCE per frame and publish a normalized snapshot per pointer (pose + controls +
//      hand fingertip), cached on the XRFrame so N consumers cost one read.
//   2. Resolve semantic ACTIONS (select / grab / resize / reel) through a binding table, so a module asks
//      "is `resize` active?" and never names a button. Re-binding is then a config change
//      (config.py → window.CONJURE_BINDINGS), not an edit in every module.
//
// A module declares which actions it uses in its module.json `actions` — the same declarative spirit as
// `config_schema`. Arbitration (who gets an action when two modules want it) is the next layer, and lives
// in the focus/capture service, not here: this file reports state, it doesn't decide ownership.

(function () {
  "use strict";
  if (!window.AFRAME || window.ConjurePointers) return;

  // xr-standard gamepad mapping → the control names a binding can refer to.
  var CONTROLS = {
    trigger: function (gp) { return btn(gp, 0); },
    grip: function (gp) { return btn(gp, 1); },
    a: function (gp) { return btn(gp, 4); },
    b: function (gp) { return btn(gp, 5); },
    stickPress: function (gp) { return btn(gp, 3); },
    stickX: function (gp) { return axis(gp, 2); },
    stickY: function (gp) { return axis(gp, 3); },
  };

  // ---------------------------------------------------------------- hand controls
  //
  // A TRACKED HAND HAS NO BUTTONS, and until this existed that meant no action resolved on one at all.
  // `controllers()` filtering hands out was never the reason hand tracking felt thin: there was nothing
  // to filter, because `CONTROLS` reads a gamepad and a hand has none. So these are synthesised from
  // the joint geometry and sit in the same namespace as `trigger` and `grip` — a binding refers to them
  // the same way, and a module still never names either.
  //
  // The numbers are FIRST GUESSES from hand anatomy, not measurements, and they are logged under
  // `CONJURE_DEBUG_LOG` so they can be dialled against a real hand rather than argued about. Each is a
  // distance normalised to 0..1 between a "released" and a "held" span.
  var PINCH_OPEN = 0.070, PINCH_SHUT = 0.022;   // thumb-tip ↔ index-tip, metres (two fingertip radii)
  var CURL_OUT = 0.90, CURL_IN = 0.45;          // tip-to-metacarpal over the finger's own bone length
  //: Hysteresis. A pinch is a continuous distance held near its own threshold by a human hand, so an
  //: unhysteresised control chatters at exactly the distance anyone holds. Press at 0.6, release at
  //: 0.4: while latched the value is reported as at least `ACTIVE_AT`, so `active()` holds through the
  //: dip without the analog value being faked upward anywhere it would be read for magnitude.
  var PRESS_AT = 0.6, RELEASE_AT = 0.4;

  var FINGERS = ["index", "middle", "ring", "pinky"];
  //: Every joint a synthesised control needs. Read per hand per frame; the other nine are published for
  //: consumers (a worn hand, a capsule chain) and cost nothing extra once the hand is being walked.
  var HAND_JOINTS = ["wrist", "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal",
                     "thumb-tip"];
  FINGERS.forEach(function (f) {
    ["metacarpal", "phalanx-proximal", "phalanx-intermediate", "phalanx-distal", "tip"]
      .forEach(function (part) { HAND_JOINTS.push(f + "-finger-" + part); });
  });

  function span(a, b, hi, lo) {         // a distance → 0 at `hi`, 1 at `lo`
    if (a == null || b == null) return 0;
    var d = a.distanceTo(b);
    return Math.max(0, Math.min(1, (hi - d) / (hi - lo)));
  }

  /**
   * `pinch`, `grasp` and `poke` from one hand's joints.
   *
   * `grasp` is normalised by the finger's OWN tracked bone length rather than by a constant, so it
   * means the same on a large hand and a small one — the same reasoning that put `s` on a ratio in
   * hand-rig.js. `poke` is an index that is out while the others are in, which is what distinguishes a
   * pointing hand from an open one.
   */
  function handControls(pos) {
    var out = { pinch: 0, grasp: 0, poke: 0 };
    if (!pos.wrist) return out;
    var curls = {}, straights = {}, n = 0, sum = 0;
    for (var i = 0; i < FINGERS.length; i++) {
      var f = FINGERS[i];
      var mc = pos[f + "-finger-metacarpal"], tip = pos[f + "-finger-tip"];
      if (!mc || !tip) continue;
      var reach = 0, chain = ["-finger-metacarpal", "-finger-phalanx-proximal",
                              "-finger-phalanx-intermediate", "-finger-phalanx-distal", "-finger-tip"];
      for (var k = 0; k < chain.length - 1; k++) {
        var a = pos[f + chain[k]], b = pos[f + chain[k + 1]];
        if (a && b) reach += a.distanceTo(b);
      }
      if (reach < 1e-4) continue;
      var straight = mc.distanceTo(tip) / reach;          // 1 = extended, ~0.45 = fisted
      curls[f] = Math.max(0, Math.min(1, (CURL_OUT - straight) / (CURL_OUT - CURL_IN)));
      straights[f] = straight;                           // the raw number, for the log
      sum += curls[f]; n++;
    }
    // EACH GESTURE EXCLUDES THE OTHERS, and it has to. Measured on the synthetic hands the moment
    // anyone asked which of these were bound to anything:
    //
    //   a FIST read pinch 0.77 AND grasp 0.80 — so closing your hand fired `select` and `grab` at
    //     once, and `water` would ripple every time you reached for something;
    //   POINTING read grasp 0.64 — because three of four fingers are curled and grasp was their MEAN,
    //     so pointing at an object grabbed it.
    //
    // Both are the same fault: a control that measures one quantity and rules nothing out. The fixes
    // are the discriminator `poke` already had.
    var others = 0, on = 0;
    ["middle", "ring", "pinky"].forEach(function (f) {
      if (curls[f] != null) { others += curls[f]; on++; }
    });
    others = on ? others / on : 0;

    // `grasp` is the WEAKEST finger, not the average: a hand is closed when every finger is closed,
    // and an average lets three fingers vote for a gesture the hand is not making.
    if (n) {
      var least = 1;
      for (var f2 in curls) least = Math.min(least, curls[f2]);
      out.grasp = least;
    }
    // `pinch` requires the other fingers to be comparatively OPEN. In a fist the thumb lies across
    // the fingers and its tip is a few centimetres from the index tip, which is squarely inside the
    // pinch span — the distance alone cannot tell the two gestures apart.
    out.pinch = span(pos["thumb-tip"], pos["index-finger-tip"], PINCH_OPEN, PINCH_SHUT)
              * Math.max(0, 1 - others);
    if (curls.index != null && on) {
      out.poke = Math.max(0, Math.min(1, (1 - curls.index) * others));
    }
    out.straights = straights;                           // not a control; see the log in `build`
    return out;
  }

  // Bindings the server injects from config.py; the fallback keeps a headset usable if injection is absent.
  var FALLBACK = { select: ["trigger", "pinch"], grab: ["grip", "grasp"],
                   resize: ["grip", "grasp"], reel: "stickY" };
  var ACTIVE_AT = 0.5;         // a button counts as held past this (analog triggers rest slightly above 0)

  function btn(gp, i) {
    var b = gp && gp.buttons && gp.buttons[i];
    if (!b) return 0;
    return b.value != null ? b.value : (b.pressed ? 1 : 0);
  }
  function axis(gp, i) {
    return (gp && gp.axes && gp.axes.length > i) ? (gp.axes[i] || 0) : 0;
  }
  function bindings() { return window.CONJURE_BINDINGS || FALLBACK; }
  function nowMs() { return (window.performance && performance.now) ? performance.now() : Date.now(); }

  var COALESCE_MS = 4;         // one read per frame across modules, without trusting XRFrame identity
  var cache = { frame: null, list: [], t: 0 };
  var prev = {};               // key → last frame's control values, for rising/falling edges
  var seen = {};               // one-shot diagnostic latches
  var frameId = 0;             // bumped per rebuild; dates reservations
  // Sharing a pointer between modules (hit-ownership arbitration):
  //   CAPTURE   — held for a whole gesture. While grab is dragging, that pointer is exclusively grab's, so
  //               nothing else reacts to its buttons mid-drag.
  //   RESERVE   — a claim on the NEXT press, renewed per frame by whoever is under the beam. Grab reserves
  //               while you hover one of its corner handles, so a trigger there means resize while the same
  //               trigger on the picture's body still means ripple. Needed because module tick ORDER isn't
  //               guaranteed: a reservation is honoured for one extra frame so a module ticking before the
  //               reserver still defers.
  var captured = {};           // key → owner, until released
  var reserved = {};           // key → {owner, f}
  var armedUntil = {};         // key → ms; a pointer is "in use" until then (see armed())

  // Diagnostics → console + temp/conjure.log, like [water]/[grab]. This layer failing silently is
  // indistinguishable from "no controllers in range", and every module downstream goes dead with it, so it
  // says WHY it produced nothing rather than swallowing it.
  function plog(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try { console.log("[pointers] " + msg); } catch (e) {}
    try { fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: "pointers", msg: msg }) }).catch(function () {}); } catch (e) {}
  }
  function once(key, msg) { if (seen[key]) return; seen[key] = 1; plog(msg); }

  function build(frame, refSpace, session) {
    frameId++;
    var THREE = AFRAME.THREE, out = [], sources = session.inputSources || [];
    for (var i = 0; i < sources.length; i++) {
      var src = sources[i];
      if (!src.targetRaySpace) continue;
      var key = (src.handedness || ("c" + i)) + (src.hand ? ":hand" : ":ctrl");
      var pp = frame.getPose(src.targetRaySpace, refSpace);
      if (!pp) continue;
      var o = pp.transform.position, q = pp.transform.orientation;
      var quat = new THREE.Quaternion(q.x, q.y, q.z, q.w);
      var gp = src.gamepad;
      var ctrl = {};
      for (var name in CONTROLS) ctrl[name] = CONTROLS[name](gp);

      // Tracked hands have no buttons; their input is their SHAPE. All 25 joints are read here — once
      // per frame, like everything else this file reads — and the synthesised controls go into the same
      // `ctrl` bag the gamepad ones do, so nothing downstream has to know which kind of hand it has.
      //
      // Publishing the whole joint set rather than just the fingertip is also the seam `occlusion.js`
      // and `hand-rig.js` would consume to stop reading the frame themselves (backlogs/input.md). They
      // are NOT changed here: `hand-rig` must keep its own read for the same reason `?hands=fit` does,
      // and occlusion works today.
      var tip = null, joints = null, radii = null;
      if (src.hand && src.hand.get && frame.getJointPose) {
        joints = {}; radii = {};
        for (var jn = 0; jn < HAND_JOINTS.length; jn++) {
          var name = HAND_JOINTS[jn];
          var space = src.hand.get(name);
          var jp = space && frame.getJointPose(space, refSpace);
          if (!jp) continue;
          var jpp = jp.transform.position;
          joints[name] = new THREE.Vector3(jpp.x, jpp.y, jpp.z);
          radii[name] = jp.radius == null ? null : jp.radius;
        }
        tip = joints["index-finger-tip"] || null;
        var synth = handControls(joints);
        var straights = synth.straights || {};
        delete synth.straights;
        for (var sc in synth) ctrl[sc] = synth[sc];
        hysteresis(key, ctrl);
        // The RAW straightness per finger beside the controls, because `CURL_OUT`/`CURL_IN` are first
        // guesses from anatomy and this is the number they would have to be dialled against. Same
        // discipline the fingertip offset ended up needing: log what a rule would be found in rather
        // than reason about where the threshold should sit.
        var st = [];
        for (var f3 in straights) st.push(f3 + " " + straights[f3].toFixed(2));
        once("hand:" + key, "hand " + key + " — " + Object.keys(joints).length + "/25 joints, "
          + "pinch/grasp/poke " + synth.pinch.toFixed(2) + "/" + synth.grasp.toFixed(2) + "/"
          + synth.poke.toFixed(2) + "; straightness " + st.join(" "));
      }
      out.push(makePointer(key, src, ctrl, new THREE.Vector3(o.x, o.y, o.z),
        new THREE.Vector3(0, 0, -1).applyQuaternion(quat).normalize(), quat, tip, joints, radii));
    }
    // Refresh the arm window: a light pull of `select`, or ANY bound action engaged (so it stays lit
    // through a grab or resize instead of dropping mid-gesture).
    var thresh = +window.CONJURE_BEAM_TRIGGER; if (!(thresh >= 0)) thresh = 0.05;
    var linger = +window.CONJURE_BEAM_MS; if (!(linger >= 0)) linger = 0;
    var tNow = nowMs();
    out.forEach(function (p) {
      if (p.value("select") >= thresh || p.anyActive()) armedUntil[p.key] = tNow + linger;
    });
    // Drop edge state for pointers that vanished, so a reconnect doesn't inherit a stale "held".
    var live = {};
    out.forEach(function (p) { live[p.key] = p.ctrl; });
    for (var k in prev) if (!live[k]) { delete prev[k]; delete captured[k]; delete reserved[k]; delete armedUntil[k]; }
    var was = {};
    for (var k2 in prev) was[k2] = prev[k2];
    out.forEach(function (p) { p._was = was[p.key] || null; p._all = out; });
    prev = live;
    return out;
  }

  function makePointer(key, src, ctrl, origin, dir, quat, tip, joints, radii) {
    function ctl(action) { return bindings()[action] || action; }
    // Resolve a control, honouring a HAND-QUALIFIED binding like "left.stickY". A two-handed scheme —
    // hold an object with one hand and shape it with the other hand's stick — otherwise can't be expressed
    // in config, and would have to be hard-coded in the module, which is what this layer exists to avoid.
    function raw(action) {
      var c = ctl(action);
      // A BINDING MAY NAME SEVERAL CONTROLS, and `max` over them is the whole of what that needs:
      // `select: ["trigger", "pinch"]` reads the trigger on a controller and the pinch on a hand,
      // because a controller's `pinch` is 0 and a hand's `trigger` is 0 — the two vocabularies are
      // disjoint, so no device test is required and none is written. One control can therefore mean
      // one action across both kinds of hand without a second binding table.
      if (Array.isArray(c)) {
        var best = 0;
        for (var n = 0; n < c.length; n++) {
          var v = one(c[n]);
          if (Math.abs(v) > Math.abs(best)) best = v;
        }
        return best;
      }
      return one(c);
    }

    function one(c) {
      var dot = c.indexOf(".");
      if (dot > 0) {
        var hand = c.slice(0, dot), name = c.slice(dot + 1), all = self._all || [];
        for (var i = 0; i < all.length; i++) {
          if (all[i].handedness === hand) { var v = all[i].ctrl[name]; return v == null ? 0 : v; }
        }
        return 0;                                        // that hand isn't present right now
      }
      var own = ctrl[c];
      return own == null ? 0 : own;
    }
    var self = {
      key: key, handedness: src.handedness || "", isHand: !!src.hand, source: src,
      origin: origin, dir: dir, quat: quat, fingertip: tip, ctrl: ctrl,
      // All 25 joints and their reported radii for a tracked hand, else null. Read once per frame with
      // everything else this layer reads, so N consumers cost one read.
      joints: joints || null, radii: radii || null,
      /** Can this pointer resolve an action at all — a gamepad, or a hand we synthesise controls for. */
      canAct: !!(src.gamepad || joints),
      // 0..1 for buttons, -1..1 for axes — resolved through the bindings, so callers name ACTIONS.
      value: function (action) { return raw(action); },
      active: function (action) { return this.value(action) >= ACTIVE_AT; },
      // Own-hand controls only, and over the whole list when a binding names several: the edge that
      // matters is "did THIS action start", whichever of its controls did it.
      started: function (action) {
        return own(ctl(action), ctrl) >= ACTIVE_AT && own(ctl(action), this._was) < ACTIVE_AT;
      },
      ended: function (action) {
        return own(ctl(action), ctrl) < ACTIVE_AT && own(ctl(action), this._was) >= ACTIVE_AT;
      },
      // Free for `owner` to act on? False while ANOTHER module holds or has reserved this pointer.
      availableTo: function (owner) { var o = ownerOf(key); return !o || o === owner; },
      // Is this pointer IN USE — i.e. is its beam showing? Armed by a light pull of `select` or any bound
      // action, and lingering after (config: CONJURE_BEAM_TRIGGER / CONJURE_BEAM_MS). Lives here rather
      // than in the beam so PRESENTATION and FOCUS agree: a highlight box shouldn't appear on an object
      // when there's no visible pointer aimed at it.
      armed: function () { return nowMs() < (armedUntil[key] || 0); },
      // Is ANY bound action engaged? Lets presentation (the beam) follow intent without knowing which.
      anyActive: function () {
        var b = bindings();
        for (var a in b) { if (raw(a) >= ACTIVE_AT) return true; }
        return false;
      },
    };
    return self;
  }

  //: key + "." + control → is it currently held. Module-level, because hysteresis is by definition a
  //: memory of the previous frame and the pointer objects are rebuilt each one.
  var held = {};

  /**
   * Latch each synthesised control, in place, by RESCALING it around its own thresholds.
   *
   * Applied to the CONTROL rather than to `active()`, because `active()` is generic — `value >=
   * ACTIVE_AT` for buttons and axes alike — and putting gesture-specific debouncing there would make
   * every control carry a rule that three of them need.
   *
   * The first version only raised a latched value to `ACTIVE_AT`, and it was half a mechanism: it held
   * a pinch through a dip and did nothing to stop one ENGAGING below the press threshold, because the
   * raw distance crosses 0.5 on its own well before it crosses 0.6. A hand held mid-range still
   * chattered — the exact thing hysteresis is for.
   *
   * So the value is remapped instead, so that `value >= ACTIVE_AT` *is* the hysteretic predicate:
   *
   *     released:  raw [0, PRESS_AT]    →  [0, ACTIVE_AT)
   *     held:      raw [RELEASE_AT, 1]  →  [ACTIVE_AT, 1]
   *
   * Monotonic, continuous within each state, 0 is still nothing and 1 is still fully closed. The scale
   * depends on the state, which is what a hysteretic control IS — and it means no consumer needs to
   * know these thresholds exist, including `active()`.
   */
  function hysteresis(key, ctrl) {
    ["pinch", "grasp", "poke"].forEach(function (name) {
      var k = key + "." + name, v = ctrl[name] || 0;
      var on = held[k] ? v >= RELEASE_AT : v >= PRESS_AT;
      held[k] = on;
      var f = on ? (v - RELEASE_AT) / (1 - RELEASE_AT) : v / PRESS_AT;
      f = Math.max(0, Math.min(1, f));
      ctrl[name] = on ? ACTIVE_AT + (1 - ACTIVE_AT) * f : ACTIVE_AT * f;
    });
  }

  //: The largest value any of `c`'s controls has in a given control bag. `null` reads as 0, which is
  //: what a pointer that did not exist last frame should look like to an edge test.
  function own(c, bag) {
    if (!bag) return 0;
    var names = Array.isArray(c) ? c : [c], best = 0;
    for (var i = 0; i < names.length; i++) {
      var v = bag[names[i]] || 0;
      if (Math.abs(v) > Math.abs(best)) best = v;
    }
    return best;
  }

  function ownerOf(key) {
    if (captured[key]) return captured[key];
    var r = reserved[key];
    return (r && r.f >= frameId - 1) ? r.owner : null;   // one frame of slack — see the note above
  }

  window.ConjurePointers = {
    ACTIVE_AT: ACTIVE_AT,
    /** Take a pointer for the duration of a gesture — nothing else sees its actions until released. */
    claim: function (key, owner) {
      if (captured[key] && captured[key] !== owner) return false;
      captured[key] = owner; return true;
    },
    release: function (key, owner) { if (captured[key] === owner) delete captured[key]; },
    /** "I'd take the next press on this pointer" — renew each frame while under the beam. */
    reserve: function (key, owner) { reserved[key] = { owner: owner, f: frameId }; },
    ownerOf: ownerOf,
    /** Free, or already ours. The one check a module needs before acting on a pointer. */
    availableTo: function (key, owner) { var o = ownerOf(key); return !o || o === owner; },
    /** Every pointer this frame, or [] outside an XR session. Cached per XRFrame: call it from as many
     *  modules as you like and the input is still read once. */
    list: function (sceneEl) {
      sceneEl = sceneEl || document.querySelector("a-scene");
      var xr = sceneEl && sceneEl.renderer && sceneEl.renderer.xr;
      var frame = sceneEl && sceneEl.frame;
      var refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
      var session = xr && xr.getSession && xr.getSession();
      if (!frame || !refSpace || !session) {
        // Distinct latches so the log separates "not in AR yet" (expected on the 2D page) from "in AR but
        // still getting nothing", which would be a real fault.
        once(session ? (frame ? "norefspace" : "noframe") : "nosession",
          "no pointers — scene=" + !!sceneEl + " renderer=" + !!(sceneEl && sceneEl.renderer)
          + " session=" + !!session + " frame=" + !!frame + " refSpace=" + !!refSpace);
        cache.frame = null; cache.list = []; return cache.list;
      }
      // Coalesce the several modules that ask each frame into ONE read — but never assume the browser
      // hands us a fresh XRFrame OBJECT every frame. If it reuses one, an identity-only cache never
      // invalidates and every consumer sees the first frame's buttons forever (beam never arms, nothing
      // grabs). So require identity AND recency: at 90 Hz frames are ~11 ms apart, so a few ms of slack
      // coalesces within a frame while always rebuilding on the next one.
      var t = (window.performance && performance.now) ? performance.now() : Date.now();
      if (cache.frame === frame && (t - cache.t) < COALESCE_MS) return cache.list;
      cache.frame = frame; cache.t = t;
      try {
        cache.list = build(frame, refSpace, session);
        once("built", "live — " + (session.inputSources || []).length + " input source(s) → "
          + cache.list.length + " pointer(s); bindings=" + JSON.stringify(bindings()));
      } catch (e) {
        cache.list = [];
        once("err", "build failed: " + (e && (e.stack || e.message) ? (e.stack || e.message) : e));
      }
      return cache.list;
    },
    /** Controllers only (skip tracked hands). For anything that genuinely needs a GAMEPAD. */
    controllers: function (sceneEl) {
      return this.list(sceneEl).filter(function (p) { return !p.isHand && p.source.gamepad; });
    },
    /**
     * Every pointer that can resolve an action — controllers, and tracked hands.
     *
     * The reader ray-driven interaction wants. A tracked hand has a `targetRaySpace` like a controller
     * does, so it has always had an aim; what it lacked was any control to resolve, which is what
     * `handControls` supplies. Every caller of `controllers()` that wanted "something I can point and
     * click with" wanted this.
     *
     * Added BESIDE `controllers()` rather than widening it, because `controllers` is named for
     * controllers and a function whose name stops being true is worse than one more function. The five
     * call sites moved; anything that really does need a gamepad still has the honest name to ask for.
     *
     * They call it as `(CP.acting || CP.controllers)`, deliberately. The Quest's cache has served a
     * stale `/static/*.js` through several reloads before now, and a consumer updated against a
     * pointers layer that has not updated would find `acting` undefined and throw inside its tick —
     * so it degrades to controllers-only instead, which is exactly the previous behaviour.
     */
    acting: function (sceneEl) {
      return this.list(sceneEl).filter(function (p) { return p.canAct; });
    },
    bindings: bindings,
  };
})();
