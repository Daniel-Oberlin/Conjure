/* global AFRAME */
// ConjurePointers — the ONE reader of XR input, and the seam that keeps controls out of module code.
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

  // Bindings the server injects from config.py; the fallback keeps a headset usable if injection is absent.
  var FALLBACK = { select: "trigger", grab: "grip", resize: "grip", reel: "stickY" };
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

  var cache = { frame: null, list: [] };
  var prev = {};               // key → last frame's control values, for rising/falling edges

  function build(frame, refSpace, session) {
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

      // Tracked hands have no buttons; their "input" is the fingertip, which a module can use for
      // proximity/touch (water does). Exposed here so hands and controllers arrive through one path.
      var tip = null;
      if (src.hand && src.hand.get && frame.getJointPose) {
        var j = src.hand.get("index-finger-tip");
        var jp = j && frame.getJointPose(j, refSpace);
        if (jp) tip = new THREE.Vector3(jp.transform.position.x, jp.transform.position.y, jp.transform.position.z);
      }
      out.push(makePointer(key, src, ctrl, new THREE.Vector3(o.x, o.y, o.z),
        new THREE.Vector3(0, 0, -1).applyQuaternion(quat).normalize(), quat, tip));
    }
    // Drop edge state for pointers that vanished, so a reconnect doesn't inherit a stale "held".
    var live = {};
    out.forEach(function (p) { live[p.key] = p.ctrl; });
    for (var k in prev) if (!live[k]) delete prev[k];
    var was = {};
    for (var k2 in prev) was[k2] = prev[k2];
    out.forEach(function (p) { p._was = was[p.key] || null; });
    prev = live;
    return out;
  }

  function makePointer(key, src, ctrl, origin, dir, quat, tip) {
    function ctl(action) { return bindings()[action] || action; }
    return {
      key: key, handedness: src.handedness || "", isHand: !!src.hand, source: src,
      origin: origin, dir: dir, quat: quat, fingertip: tip, ctrl: ctrl,
      // 0..1 for buttons, -1..1 for axes — resolved through the bindings, so callers name ACTIONS.
      value: function (action) { var v = ctrl[ctl(action)]; return v == null ? 0 : v; },
      active: function (action) { return this.value(action) >= ACTIVE_AT; },
      started: function (action) {                       // rising edge this frame
        var c = ctl(action), now = ctrl[c] || 0, before = this._was ? (this._was[c] || 0) : 0;
        return now >= ACTIVE_AT && before < ACTIVE_AT;
      },
      ended: function (action) {                         // falling edge this frame
        var c = ctl(action), now = ctrl[c] || 0, before = this._was ? (this._was[c] || 0) : 0;
        return now < ACTIVE_AT && before >= ACTIVE_AT;
      },
      // Is ANY bound action engaged? Lets presentation (the beam) follow intent without knowing which.
      anyActive: function () {
        var b = bindings();
        for (var a in b) { var v = ctrl[b[a]]; if (v != null && v >= ACTIVE_AT) return true; }
        return false;
      },
    };
  }

  window.ConjurePointers = {
    ACTIVE_AT: ACTIVE_AT,
    /** Every pointer this frame, or [] outside an XR session. Cached per XRFrame: call it from as many
     *  modules as you like and the input is still read once. */
    list: function (sceneEl) {
      sceneEl = sceneEl || document.querySelector("a-scene");
      var xr = sceneEl && sceneEl.renderer && sceneEl.renderer.xr;
      var frame = sceneEl && sceneEl.frame;
      var refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
      var session = xr && xr.getSession && xr.getSession();
      if (!frame || !refSpace || !session) { cache.frame = null; cache.list = []; return cache.list; }
      if (cache.frame === frame) return cache.list;
      cache.frame = frame;
      try { cache.list = build(frame, refSpace, session); } catch (e) { cache.list = []; }
      return cache.list;
    },
    /** Controllers only (skip tracked hands) — the common case for ray-driven interaction. */
    controllers: function (sceneEl) {
      return this.list(sceneEl).filter(function (p) { return !p.isHand && p.source.gamepad; });
    },
    bindings: bindings,
  };
})();
