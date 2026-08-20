/* global AFRAME, THREE */
// Real-world depth occlusion for AR passthrough (docs/dynamic-content-plan.md §Real-world occlusion).
//
// THE PROBLEM: in passthrough the compositor draws the real world, then our opaque virtual layer on top
// with NO knowledge of real-world depth — so a virtual wall covers your real hand. The fix is a DEPTH
// PRE-PASS, not per-material shader occlusion: write real-world depth into the Z-buffer with COLOUR
// WRITE OFF, then render the scene normally. A virtual fragment behind a real surface fails the depth
// test → writes no colour → stays alpha 0 → the compositor fills it with passthrough (your hand shows
// through the hole). One place, every material occludes for free — modules never opt in.
//
// MODES (window.CONJURE_OCCLUSION from --occlusion; per-client override ?occlusion=off|hands|full):
//   off   — no occlusion; virtual always over passthrough (today's behaviour).
//   hands — a filled, depth-only HAND MESH per tracked hand (finger ribbons + a palm fan built from the
//           XR joint poses), added to the SCENE GRAPH so it renders inside A-Frame's own pass (after its
//           depth clear) and reliably seeds depth. Sharp + cheap; hands only. Needs 'hand-tracking' AND
//           the headset actually producing hand input sources (put controllers down / auto-switch on).
//   hands-solid — the SAME hand mesh, but drawn as opaque white polygons (a white-glove avatar) instead
//           of an invisible occluder. Still occludes (opaque, writes depth); just also visible.
//   full  — environment depth: opt the session into WebXR depth-sensing (gpu-optimized) so three r169's
//           built-in depth mesh writes real-world depth every frame (walls/furniture/people, coarse).
//
// Joints come straight from the XR frame (frame.getJointPose), not three's getHand — that never
// populated joints under A-Frame. The scene root is pinned to the XR reference space, so a joint pose
// (in that same space) maps 1:1 into scene coordinates.

(function () {
  "use strict";

  var MODES = { off: 1, hands: 1, "hands-solid": 1, full: 1 };
  function isHands(mode) { return mode === "hands" || mode === "hands-solid"; }

  // --- Hand skeleton we fill. Fingers are palm-plane "ribbons" (2 verts per joint, offset ± half-width
  //     along the in-plane perpendicular to the bone); the palm is a triangle fan from the wrist across
  //     the thumb base + the four proximal knuckles. One BufferGeometry per hand, FIXED topology, vertex
  //     positions rewritten each frame → one draw call, a true silhouette, all depth-only. -----------
  var RIBBONS = [
    ["thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip"],
    ["index-finger-phalanx-proximal", "index-finger-phalanx-intermediate", "index-finger-phalanx-distal", "index-finger-tip"],
    ["middle-finger-phalanx-proximal", "middle-finger-phalanx-intermediate", "middle-finger-phalanx-distal", "middle-finger-tip"],
    ["ring-finger-phalanx-proximal", "ring-finger-phalanx-intermediate", "ring-finger-phalanx-distal", "ring-finger-tip"],
    ["pinky-finger-phalanx-proximal", "pinky-finger-phalanx-intermediate", "pinky-finger-phalanx-distal", "pinky-finger-tip"]
  ];
  // Palm fan: [0] is the fan centre (wrist); [1..] are the rim, in order around the palm.
  var PALM = ["wrist", "thumb-metacarpal", "index-finger-phalanx-proximal",
    "middle-finger-phalanx-proximal", "ring-finger-phalanx-proximal", "pinky-finger-phalanx-proximal"];
  // Three roughly-coplanar joints that define the palm plane → the ribbon offset direction.
  var NORMAL_REF = ["wrist", "index-finger-metacarpal", "pinky-finger-metacarpal"];

  var NEEDED = {};                                                     // every joint we read (skip the rest)
  RIBBONS.forEach(function (c) { c.forEach(function (n) { NEEDED[n] = 1; }); });
  PALM.forEach(function (n) { NEEDED[n] = 1; });
  NORMAL_REF.forEach(function (n) { NEEDED[n] = 1; });

  // Precompute the shared, hand-independent index buffer + vertex layout once.
  var TOPO = (function () {
    var index = [], vbase = 0, ribbonBase = [];
    for (var r = 0; r < RIBBONS.length; r++) {
      ribbonBase[r] = vbase;
      var n = RIBBONS[r].length;
      for (var j = 0; j < n - 1; j++) {                               // quad between joint j and j+1
        var a = vbase + 2 * j, b = a + 1, c = a + 2, d = a + 3;       // a,b = this joint; c,d = next
        index.push(a, b, c, b, d, c);                                 // two tris (winding moot: DoubleSide)
      }
      vbase += 2 * n;
    }
    var palmBase = vbase; vbase += PALM.length;
    for (var k = 1; k < PALM.length - 1; k++) index.push(palmBase, palmBase + k, palmBase + k + 1);
    return { index: index, vertCount: vbase, ribbonBase: ribbonBase, palmBase: palmBase };
  })();

  function resolveMode() {
    var q = "";
    try { q = new URLSearchParams(location.search).get("occlusion") || ""; } catch (e) { /* no URL */ }
    q = (q || "").trim().toLowerCase();
    if (q && !MODES[q]) q = "";                                       // ignore a junk override, fall through
    var g = (window.CONJURE_OCCLUSION == null ? "" : String(window.CONJURE_OCCLUSION)).trim().toLowerCase();
    return q || (MODES[g] ? g : "off");                              // URL override → injected default → off
  }

  // Mirror to console AND the server log (POST /client_log → temp/conjure.log), like conjure-client's
  // debugLog — headset-side console is invisible, so status has to reach the terminal.
  function log(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try { console.log("[occlusion] " + msg); } catch (e) {}
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: "occlusion", msg: msg }) }).catch(function () {});
    } catch (e) { /* never let logging break a frame */ }
  }

  // full mode: A-Frame lists depth-sensing as OPTIONAL but never supplies the required `depthSensing`
  // init dict, so the browser drops it. Wrap requestSession once (before entering AR) to add it — then
  // three r169 fetches the depth texture and renders its occlusion mesh. Harmless if unsupported.
  var _wrapped = false;
  function enableDepthSensing() {
    if (_wrapped || !navigator.xr || !navigator.xr.requestSession) return;
    _wrapped = true;
    var orig = navigator.xr.requestSession.bind(navigator.xr);
    navigator.xr.requestSession = function (mode, opts) {
      opts = opts || {};
      if (mode === "immersive-ar" && !opts.depthSensing) {
        opts.depthSensing = {
          usagePreference: ["gpu-optimized", "cpu-optimized"],
          dataFormatPreference: ["luminance-alpha", "float32"]
        };
        log("requestSession: added depthSensing (gpu-optimized) init dict");
      }
      return orig(mode, opts);
    };
  }

  AFRAME.registerComponent("occlusion", {
    init: function () {
      this.mode = resolveMode();
      this.T = AFRAME.THREE;
      this._hands = {};                // handedness → { mesh, geo, pos }
      this._material = null;           // shared depth-only material
      this._loggedRuntime = false;
      this._t = 0;
      var V = this.T.Vector3;
      this._v = { a: new V(), b: new V(), n: new V(), dir: new V(), side: new V() };
      log("mode=" + this.mode + " (injected=" + window.CONJURE_OCCLUSION + ")");

      if (this.mode === "off") return;
      if (this.mode === "full") enableDepthSensing();

      var self = this;
      this.el.addEventListener("enter-vr", function () {              // what did the runtime actually grant?
        try {
          var s = self.el.renderer && self.el.renderer.xr && self.el.renderer.xr.getSession();
          var feats = s && s.enabledFeatures ? Array.prototype.join.call(s.enabledFeatures, ",") : "?";
          log("enter-vr: mode=" + self.mode + " enabledFeatures=[" + feats + "]");
        } catch (e) { log("enter-vr: feature probe failed: " + e); }
      });
    },

    // hands: a depth-only material — writes DEPTH but no COLOUR, so it never paints, only carves holes
    // that let passthrough show through virtual content behind it. hands-solid: opaque white instead, a
    // visible white-glove avatar that still occludes. DoubleSide so winding never culls it.
    _mat: function () {
      if (!this._material) {
        this._material = this.mode === "hands-solid"
          ? new this.T.MeshBasicMaterial({ color: 0xffffff, side: this.T.DoubleSide })
          : new this.T.MeshBasicMaterial({ colorWrite: false, side: this.T.DoubleSide });
      }
      return this._material;
    },

    _handMesh: function () {
      var T = this.T, geo = new T.BufferGeometry(), pos = new Float32Array(TOPO.vertCount * 3);
      geo.setAttribute("position", new T.BufferAttribute(pos, 3));
      geo.setIndex(TOPO.index);
      var mesh = new T.Mesh(geo, this._mat());
      mesh.frustumCulled = false;                                     // positions are world-space; skip culling
      mesh.renderOrder = -1000;                                       // lay occluder depth before content
      this.el.object3D.add(mesh);                                     // scene root = XR reference space
      return { mesh: mesh, geo: geo, pos: pos };
    },

    // Read the joints we need for one hand from the XR frame. Returns null if any are missing this frame
    // (partial tracking) so we hide rather than draw a torn mesh.
    _readJoints: function (frame, refSpace, hand) {
      if (!frame.getJointPose) return null;
      var map = {};
      hand.forEach(function (jointSpace, name) {
        if (!NEEDED[name]) return;
        var pose = frame.getJointPose(jointSpace, refSpace);
        if (!pose) return;
        var p = pose.transform.position;
        map[name] = { x: p.x, y: p.y, z: p.z, r: pose.radius || 0.01 };
      });
      for (var n in NEEDED) if (!map[n]) return null;
      return map;
    },

    // Write this hand's vertex positions from its joint map: finger ribbons offset ± half-width in the
    // palm plane, palm fan at the joint centres.
    _fillHand: function (rec, jm) {
      var v = this._v, pos = rec.pos;
      var w = jm[NORMAL_REF[0]], im = jm[NORMAL_REF[1]], pm = jm[NORMAL_REF[2]];
      v.a.set(im.x - w.x, im.y - w.y, im.z - w.z);
      v.b.set(pm.x - w.x, pm.y - w.y, pm.z - w.z);
      v.n.crossVectors(v.a, v.b);
      if (v.n.lengthSq() < 1e-12) v.n.set(0, 1, 0); else v.n.normalize();

      for (var r = 0; r < RIBBONS.length; r++) {
        var chain = RIBBONS[r], base = TOPO.ribbonBase[r], n = chain.length;
        for (var j = 0; j < n; j++) {
          var cur = jm[chain[j]], nx = j < n - 1 ? jm[chain[j + 1]] : null, pv = j > 0 ? jm[chain[j - 1]] : null;
          if (nx) v.dir.set(nx.x - cur.x, nx.y - cur.y, nx.z - cur.z);
          else v.dir.set(cur.x - pv.x, cur.y - pv.y, cur.z - pv.z);
          v.side.crossVectors(v.dir, v.n);
          if (v.side.lengthSq() < 1e-12) v.side.set(1, 0, 0); else v.side.normalize();
          var hw = cur.r * 1.1;                                       // half-width; slight over-fill
          var li = (base + 2 * j) * 3, ri = (base + 2 * j + 1) * 3;
          pos[li] = cur.x + v.side.x * hw; pos[li + 1] = cur.y + v.side.y * hw; pos[li + 2] = cur.z + v.side.z * hw;
          pos[ri] = cur.x - v.side.x * hw; pos[ri + 1] = cur.y - v.side.y * hw; pos[ri + 2] = cur.z - v.side.z * hw;
        }
      }
      for (var k = 0; k < PALM.length; k++) {
        var c = jm[PALM[k]], pi = (TOPO.palmBase + k) * 3;
        pos[pi] = c.x; pos[pi + 1] = c.y; pos[pi + 2] = c.z;
      }
      rec.geo.attributes.position.needsUpdate = true;
    },

    _syncHands: function () {
      var sc = this.el, xr = sc.renderer && sc.renderer.xr;
      var frame = sc.frame;
      var refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
      var session = xr && xr.getSession && xr.getSession();
      if (!frame || !refSpace || !session) return;

      var sources = session.inputSources || [], handsSeen = 0, filled = 0, seen = {};
      for (var s = 0; s < sources.length; s++) {
        var src = sources[s];
        if (!src.hand) continue;                                      // a controller, not a tracked hand
        handsSeen++;
        var handed = src.handedness || ("hand" + s);
        var rec = this._hands[handed] || (this._hands[handed] = this._handMesh());
        var jm = this._readJoints(frame, refSpace, src.hand);
        if (!jm) { rec.mesh.visible = false; continue; }
        this._fillHand(rec, jm);
        rec.mesh.visible = true; seen[handed] = true; filled++;
      }
      for (var h in this._hands) if (!seen[h]) this._hands[h].mesh.visible = false;

      // Throttled status (~1 Hz). handsSeen counts input sources that ARE tracked hands; if it stays 0 you
      // were holding controllers (or hands weren't up). filled = hand meshes drawn this frame.
      if (++this._t % 72 === 0) {
        if (!handsSeen) {
          var desc = [];
          for (var i = 0; i < sources.length; i++) desc.push((sources[i].handedness || "?") + "/" + (sources[i].targetRayMode || "?"));
          log("no tracked hands; inputSources=[" + desc.join(",") + "] (put controllers down to use hands)");
        } else {
          log("handsSeen=" + handsSeen + " meshes_filled=" + filled);
        }
      }
    },

    // full mode: keep three's depth mesh alive. It renders BEFORE A-Frame's scene render, which would
    // otherwise clear its depth — so while presenting AR with a live depth texture, turn off
    // autoClearDepth (three's own per-frame mesh reseeds the whole buffer). Restore it otherwise so
    // normal rendering is untouched. Also log the granted depth usage/format so we can see WHY a device
    // isn't giving three a GPU texture (hasDepthSensing stays false → no occlusion).
    _fullSync: function () {
      var r = this.el.renderer, xr = r && r.xr;
      if (!r || !xr) return;
      var has = !!(xr.hasDepthSensing && xr.hasDepthSensing());
      var active = !!(xr.isPresenting && has);
      r.autoClearDepth = !active;
      if (active && !this._loggedRuntime) { this._loggedRuntime = true; log("full: depth-sensing active, holding depth buffer"); }
      if (++this._t % 72 === 0) {
        var s = xr.getSession && xr.getSession();
        log("full: hasDepthSensing=" + has + " depthUsage=" + (s && s.depthUsage) + " depthDataFormat=" + (s && s.depthDataFormat));
      }
    },

    tick: function () {
      if (isHands(this.mode)) this._syncHands();
      else if (this.mode === "full") this._fullSync();
    },

    remove: function () {
      var r = this.el.renderer;                                       // don't leave the renderer stuck off
      if (r) r.autoClearDepth = true;
    }
  });
})();
