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

  function makePointer(key, src, ctrl, origin, dir, quat, tip) {
    function ctl(action) { return bindings()[action] || action; }
    // Resolve a control, honouring a HAND-QUALIFIED binding like "left.stickY". A two-handed scheme —
    // hold an object with one hand and shape it with the other hand's stick — otherwise can't be expressed
    // in config, and would have to be hard-coded in the module, which is what this layer exists to avoid.
    function raw(action) {
      var c = ctl(action), dot = c.indexOf(".");
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
      // 0..1 for buttons, -1..1 for axes — resolved through the bindings, so callers name ACTIONS.
      value: function (action) { return raw(action); },
      active: function (action) { return this.value(action) >= ACTIVE_AT; },
      started: function (action) {                       // rising edge this frame (own-hand controls)
        var c = ctl(action), now = ctrl[c] || 0, before = this._was ? (this._was[c] || 0) : 0;
        return now >= ACTIVE_AT && before < ACTIVE_AT;
      },
      ended: function (action) {                         // falling edge this frame (own-hand controls)
        var c = ctl(action), now = ctrl[c] || 0, before = this._was ? (this._was[c] || 0) : 0;
        return now < ACTIVE_AT && before >= ACTIVE_AT;
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
    /** Controllers only (skip tracked hands) — the common case for ray-driven interaction. */
    controllers: function (sceneEl) {
      return this.list(sceneEl).filter(function (p) { return !p.isHand && p.source.gamepad; });
    },
    bindings: bindings,
  };
})();
