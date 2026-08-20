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
//   hands — per-joint depth-only occluders following the tracked hands, added to the SCENE GRAPH so
//           they render inside A-Frame's own pass (after its depth clear) and reliably seed depth.
//           Sharp + cheap; hands only. Needs the 'hand-tracking' session feature.
//   full  — environment depth: opt the WebXR session into depth-sensing (gpu-optimized) so three r169's
//           built-in WebXRManager depth mesh writes real-world depth every frame (walls/furniture/
//           people, + hands, coarse/laggy edges). Needs the Quest depth-sensing feature.
//
// WHY hands is scene-graph and not the three built-in mesh: three's WebXRManager renders its depth mesh
// (m.render) and THEN calls the app's animation callback, which runs A-Frame's renderer.render with
// autoClearDepth=true — clearing the mesh's depth before the scene draws. Occluders that live in the
// scene graph are drawn *inside* that render (after the clear), so they survive. For `full` we can't add
// three's private mesh to the graph, so we instead keep its depth by disabling autoClearDepth while
// presenting AR with depth-sensing active (see _fullSync).

(function () {
  "use strict";

  var MODES = { off: 1, hands: 1, full: 1 };

  function resolveMode() {
    var q = "";
    try { q = new URLSearchParams(location.search).get("occlusion") || ""; } catch (e) { /* no URL */ }
    q = (q || "").trim().toLowerCase();
    if (q && !MODES[q]) q = "";                                   // ignore a junk override, fall through
    var g = (window.CONJURE_OCCLUSION == null ? "" : String(window.CONJURE_OCCLUSION)).trim().toLowerCase();
    return q || (MODES[g] ? g : "off");                          // URL override → injected default → off
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

  // --- full mode: A-Frame lists depth-sensing as an OPTIONAL feature but never supplies the required
  //     `depthSensing` init dict, so the browser silently drops it and enabledFeatures never includes it.
  //     Wrap requestSession ONCE (before the user enters AR) to add the dict — then three r169 fetches the
  //     depth texture and renders its occlusion mesh automatically. Harmless if the device lacks the API
  //     (the runtime just omits the feature). --------------------------------------------------------
  var _wrapped = false;
  function enableDepthSensing() {
    if (_wrapped) return;
    if (!navigator.xr || !navigator.xr.requestSession) return;
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
      this._pool = {};                 // jointKey ("left:index-finger-tip") → depth-only occluder Mesh
      this._occMat = null;             // shared depth-only occluder material (hands)
      this._sphere = null;             // shared unit-sphere geometry (hands)
      this._loggedRuntime = false;
      this._t = 0;
      log("mode=" + this.mode + " (injected=" + window.CONJURE_OCCLUSION + ")");

      if (this.mode === "off") return;
      if (this.mode === "full") enableDepthSensing();
      // hands: nothing to do until the AR session (and its hand input sources) exist — see enter-vr/tick.

      // Log what the runtime actually granted us the moment we enter immersive mode — the fastest way to
      // see whether hand-tracking (hands mode) or depth-sensing (full mode) is present at all.
      var self = this;
      this.el.addEventListener("enter-vr", function () {
        try {
          var s = self.el.renderer && self.el.renderer.xr && self.el.renderer.xr.getSession();
          var feats = s && s.enabledFeatures ? Array.prototype.join.call(s.enabledFeatures, ",") : "?";
          log("enter-vr: mode=" + self.mode + " enabledFeatures=[" + feats + "]");
        } catch (e) { log("enter-vr: feature probe failed: " + e); }
      });
    },

    // A depth-only occluder: writes DEPTH but no COLOUR, so it never paints — it only carves holes that
    // let passthrough show through virtual content behind it. Drawn first (renderOrder very negative) so
    // its depth is laid down before ordinary opaque geometry tests against it.
    _occluderMesh: function () {
      var T = this.T;
      if (!this._occMat) {
        this._occMat = new T.MeshBasicMaterial({ colorWrite: false });   // depthWrite/depthTest default true
      }
      if (!this._sphere) {
        this._sphere = new T.SphereGeometry(1, 10, 8);                   // unit radius; scaled per joint
      }
      var m = new T.Mesh(this._sphere, this._occMat);
      m.renderOrder = -1000;                                              // lay occluder depth before content
      m.frustumCulled = false;
      m.matrixAutoUpdate = true;
      this.el.object3D.add(m);                                            // live in the scene root = XR ref space
      return m;
    },

    // Read hand-joint poses DIRECTLY from the XR frame each tick and drop a depth-only occluder sphere at
    // each — no dependence on three's getHand/controller machinery (which wasn't populating joints under
    // A-Frame). Occluders live in the scene root, which Conjure pins to the headset reference space, so a
    // joint pose (in that same reference space) positions 1:1. Joints not reported this frame are hidden.
    _syncHands: function () {
      var sc = this.el, xr = sc.renderer && sc.renderer.xr;
      var frame = sc.frame;
      var refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
      var session = xr && xr.getSession && xr.getSession();
      if (!frame || !refSpace || !session) return;

      for (var k in this._pool) {                                         // hide all; re-show what we place
        if (Object.prototype.hasOwnProperty.call(this._pool, k)) this._pool[k].visible = false;
      }
      var sources = session.inputSources || [], handsSeen = 0, placed = 0, self = this;
      for (var s = 0; s < sources.length; s++) {
        var src = sources[s];
        if (!src.hand) continue;                                          // a controller, not a tracked hand
        handsSeen++;
        var prefix = (src.handedness || ("hand" + s)) + ":";
        src.hand.forEach(function (jointSpace, jointName) {
          var pose = frame.getJointPose && frame.getJointPose(jointSpace, refSpace);
          if (!pose) return;
          var key = prefix + jointName, occ = self._pool[key];
          if (!occ) { occ = self._occluderMesh(); self._pool[key] = occ; }
          var p = pose.transform.position;
          occ.position.set(p.x, p.y, p.z);
          var r = (pose.radius || 0.012) * 1.4;                          // grow so adjacent joint blobs merge
          occ.scale.set(r, r, r);
          occ.visible = true;
          placed++;
        });
      }
      // Throttled status (~1 Hz). handsSeen counts input sources that ARE tracked hands; if it stays 0 you
      // were holding controllers (or hands weren't up). placed = occluder spheres positioned this frame.
      if (++this._t % 72 === 0) {
        if (!handsSeen) {
          var desc = [];
          for (var i = 0; i < sources.length; i++) {
            desc.push((sources[i].handedness || "?") + "/" + (sources[i].targetRayMode || "?"));
          }
          log("no tracked hands; inputSources=[" + desc.join(",") + "] (put controllers down to use hands)");
        } else {
          log("handsSeen=" + handsSeen + " occluders_placed=" + placed);
        }
      }
    },

    // full mode: keep three's depth mesh alive. It renders BEFORE A-Frame's scene render, which would
    // otherwise clear its depth — so while we're actually presenting AR with a live depth texture, turn
    // off autoClearDepth (three's own per-frame mesh reseeds the whole buffer). Restore it whenever we're
    // not (desktop, VR-only worlds, no depth-sensing) so normal rendering is untouched.
    _fullSync: function () {
      var r = this.el.renderer, xr = r && r.xr;
      if (!r || !xr) return;
      var active = !!(xr.isPresenting && xr.hasDepthSensing && xr.hasDepthSensing());
      r.autoClearDepth = !active;
      if (active && !this._loggedRuntime) { this._loggedRuntime = true; log("full: depth-sensing active, holding depth buffer"); }
    },

    tick: function () {
      if (this.mode === "hands") this._syncHands();
      else if (this.mode === "full") this._fullSync();
    },

    remove: function () {
      // Restore the default so toggling the component off (dev) doesn't leave the renderer stuck.
      var r = this.el.renderer;
      if (r) r.autoClearDepth = true;
    }
  });
})();
