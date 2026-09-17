/* global AFRAME, THREE */
// hand-rig — a placed hand model that CONFORMS to the wearer's tracked hand, every frame.
// Spec: docs/specs/figures.md §8f. Plan: docs/plans/hands.md phase 2. Forks: decisions.md §30, §31.
//
// WHY THIS IS NOT A RETARGET. These models are authored in the WebXR joint frame — their nodes carry
// the WebXR joint names exactly, and each bone lies along its own local -Z — so the two indirections
// the whole of figures.md rests on, WHICH NODE and WHICH WAY, collapse to identity here. There is no
// vocabulary to discover, no anatomical frame to derive, and no rest-pose agreement to establish. A
// bone's world transform IS the joint pose. Everything below is bookkeeping around that one fact.
//
// THE ONE THING THE JOINTS DO NOT CARRY IS GIRTH. Joint positions give length and never thickness, and
// skinning transports bound vertices rigidly — so without a scale a large hand gets long fingers of
// exactly the authored thickness. `s` is that scale and it does nothing else: every joint is PLACED at
// the pose the runtime reports, so positions are exact by construction and a scale error cannot
// accumulate down a chain. Measured on a Quest 3, the per-bone residual after a single scalar is ~5%,
// which is a fraction of a millimetre of girth. That is why one number is enough.
//
// `s` IS THE MEDIAN OVER 14 SEGMENTS, NOT 24. `wrist -> *-metacarpal` is the offset between an
// arbitrary frame origin and the hand, and `*-distal -> *-tip` compares a runtime-derived surface point
// against an authored tip bone. Neither is a bone length. Including them measured a 36% spread on a
// hand whose real bones agree to 5% (docs/plans/hands.md phase 0).

(function () {
  "use strict";
  if (!window.AFRAME || AFRAME.components["hand-rig"]) return;

  var JOINTS = [
    "wrist",
    "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip",
    "index-finger-metacarpal", "index-finger-phalanx-proximal", "index-finger-phalanx-intermediate",
    "index-finger-phalanx-distal", "index-finger-tip",
    "middle-finger-metacarpal", "middle-finger-phalanx-proximal", "middle-finger-phalanx-intermediate",
    "middle-finger-phalanx-distal", "middle-finger-tip",
    "ring-finger-metacarpal", "ring-finger-phalanx-proximal", "ring-finger-phalanx-intermediate",
    "ring-finger-phalanx-distal", "ring-finger-tip",
    "pinky-finger-metacarpal", "pinky-finger-phalanx-proximal", "pinky-finger-phalanx-intermediate",
    "pinky-finger-phalanx-distal", "pinky-finger-tip"
  ];

  // Parent -> child pairs whose length is a BONE. The wrist-rooted and tip segments are left out on
  // purpose; see the header. 14 of 24.
  var BONE_PAIRS = [];
  [["thumb-metacarpal", "thumb-phalanx-proximal"], ["thumb-phalanx-proximal", "thumb-phalanx-distal"]]
    .forEach(function (p) { BONE_PAIRS.push(p); });
  ["index", "middle", "ring", "pinky"].forEach(function (f) {
    BONE_PAIRS.push([f + "-finger-metacarpal", f + "-finger-phalanx-proximal"]);
    BONE_PAIRS.push([f + "-finger-phalanx-proximal", f + "-finger-phalanx-intermediate"]);
    BONE_PAIRS.push([f + "-finger-phalanx-intermediate", f + "-finger-phalanx-distal"]);
  });

  // The five `*-distal -> *-tip` segments, which are NOT bones and get their own treatment below.
  var TIPS = ["thumb-tip", "index-finger-tip", "middle-finger-tip", "ring-finger-tip",
              "pinky-finger-tip"];
  var TIP_PARENT = {
    "thumb-tip": "thumb-phalanx-distal", "index-finger-tip": "index-finger-phalanx-distal",
    "middle-finger-tip": "middle-finger-phalanx-distal", "ring-finger-tip": "ring-finger-phalanx-distal",
    "pinky-finger-tip": "pinky-finger-phalanx-distal"
  };

  var S_MIN = 0.5, S_MAX = 2.0;      // a scale outside this is a bad frame, not a big hand
  var TIP_MAX = 3.0;                 // a tip ratio beyond this is a tracking artefact, not a finger
  var LOST_MS = 400;                 // tracking gone this long → hand back to its rest pose

  function log(msg) {
    try { console.log("[hand-rig] " + msg); } catch (e) {}
    if (!window.CONJURE_DEBUG_LOG) return;
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: "hand-rig", msg: msg }) }).catch(function () {});
    } catch (e) {}
  }

  function median(xs) {
    var v = xs.slice().sort(function (a, b) { return a - b; });
    if (!v.length) return null;
    return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
  }

  AFRAME.registerComponent("hand-rig", {
    schema: {
      hand: { type: "string", default: "" },     // "left" | "right" — which tracked hand drives this
      joints: { type: "string", default: "" }    // {webxrJoint: nodeName}; identity today, see hands.py
    },

    init: function () {
      var self = this;
      this.T = AFRAME.THREE;
      this._bones = null;
      this._bind = null;             // {joint: world position at rest}
      this._rest = null;             // bone -> local matrix at rest, for putting it back
      this._s = 1;
      this._tips = null;             // {tipJoint: its own finger's distal→tip ratio}
      this._lastSeen = 0;
      this._restored = true;
      this._tmp = { m: new this.T.Matrix4(), inv: new this.T.Matrix4(), p: new this.T.Vector3(),
                    q: new this.T.Quaternion(), sc: new this.T.Vector3() };
      this._onLoad = function () { self._bones = null; self._bind = null; };
      this.el.addEventListener("model-loaded", this._onLoad);
      // LIVE HAND > CLIP > POSE (docs/specs/figures.md §8b gains one line for this). Settled by taking
      // the other two writers OFF rather than by winning a race with them: A-Frame gives no tick order
      // between components on one entity, so "we write last" is not something this could rely on.
      this._suspendOthers();
    },

    remove: function () {
      this.el.removeEventListener("model-loaded", this._onLoad);
      this._restore();
      this._resumeOthers();
    },

    _suspendOthers: function () {
      var clip = this.el.components["figure-clip"];
      if (clip && clip.pause) { try { clip.pause(); } catch (e) {} }
      var fig = this.el.components.figure;
      if (fig && fig.restore) { try { fig.restore(); } catch (e) {} }
    },

    _resumeOthers: function () {
      var clip = this.el.components["figure-clip"];
      if (clip && clip.play) { try { clip.play(); } catch (e) {} }
      var fig = this.el.components.figure;
      if (fig && fig.apply) { try { fig.apply(); } catch (e) {} }
    },

    // Bone objects by joint name, plus the BIND world positions and rest local matrices, all captured
    // in one pass on a freshly loaded model — the only moment the skeleton is guaranteed untouched.
    _collect: function () {
      if (this._bones) return this._bones;
      var obj = this.el.getObject3D("mesh");
      if (!obj) return null;
      obj.updateMatrixWorld(true);
      var byName = {};
      obj.traverse(function (n) {
        if (n.isBone && n.name && !byName[n.name]) byName[n.name] = n;
        if (n.isSkinnedMesh && n.skeleton) {
          n.skeleton.bones.forEach(function (b) { if (b.name && !byName[b.name]) byName[b.name] = b; });
        }
      });
      var map = {};
      try { map = JSON.parse(this.data.joints || "{}") || {}; } catch (e) { map = {}; }
      var bones = {}, bind = {}, rest = new Map(), missing = [];
      for (var i = 0; i < JOINTS.length; i++) {
        var j = JOINTS[i], b = byName[map[j] || j];
        if (!b) { missing.push(j); continue; }
        bones[j] = b;
        bind[j] = new this.T.Vector3().setFromMatrixPosition(b.matrixWorld);
        rest.set(b, b.matrix.clone());
      }
      if (missing.length) {
        log("NO SUCH JOINT on this model: " + missing.slice(0, 6).join(", ")
          + (missing.length > 6 ? " …" : "") + " — refusing to drive it");
        return null;
      }
      this._bones = bones;
      this._bind = bind;
      this._rest = rest;
      return bones;
    },

    /**
     * The scale that makes this model's FLESH the right thickness for the hand wearing it: the median
     * of tracked ÷ bind over the 14 real bones. Median because it is pose-invariant — a fist and a
     * flat hand give the same answer where a whole-skeleton fit would not.
     */
    _scale: function (pos) {
      var bind = this._bind, r = [];
      for (var i = 0; i < BONE_PAIRS.length; i++) {
        var a = BONE_PAIRS[i][0], b = BONE_PAIRS[i][1];
        if (!pos[a] || !pos[b] || !bind[a] || !bind[b]) continue;
        var was = bind[a].distanceTo(bind[b]);
        if (was < 1e-5) continue;
        r.push(pos[a].distanceTo(pos[b]) / was);
      }
      var m = median(r);
      if (m == null || !(m >= S_MIN && m <= S_MAX)) return null;
      return m;
    },

    _read: function (frame, refSpace, hand) {
      if (!frame.getJointPose || !hand || !hand.get) return null;
      var pos = {}, quat = {}, got = 0;
      for (var i = 0; i < JOINTS.length; i++) {
        var space = hand.get(JOINTS[i]);
        var pose = space && frame.getJointPose(space, refSpace);
        if (!pose) continue;
        var p = pose.transform.position, q = pose.transform.orientation;
        pos[JOINTS[i]] = new this.T.Vector3(p.x, p.y, p.z);
        quat[JOINTS[i]] = new this.T.Quaternion(q.x, q.y, q.z, q.w);
        got++;
      }
      // All or nothing. A partial skeleton would leave some bones driven and the rest at rest, which
      // renders as a hand tearing itself apart — far worse than a hand that simply stops.
      return got === JOINTS.length ? { pos: pos, quat: quat } : null;
    },

    /**
     * Write the skeleton from one frame's joint poses.
     *
     * Each bone's WORLD matrix is composed from the joint pose and `s`, then converted into the bone's
     * own parent frame. Going through world rather than setting locals directly is what makes the
     * scale non-cumulative: a bone's world scale is exactly `s` however deep it sits, because the
     * parent's contribution is divided out. It also means this works whether the file's joints are a
     * chain or — as in every hand we have — twenty-five siblings under one node.
     *
     * Walked in `JOINTS` order, which is parents before children, so a parent's `matrixWorld` is
     * already the one we just wrote by the time a child asks for it.
     */
    /**
     * Per-finger scale for the five TIP bones, derived rather than dialled.
     *
     * Reported on device: the wearer's real fingertips protrude about 5 mm beyond the virtual ones.
     * The tip JOINT lands exactly where the runtime says — every joint does — so this is not a
     * placement error. It is the flesh: the fingertip cap is bound to the tip bone, and `s` only
     * scales it by the GIRTH ratio, which says nothing about how far a fingertip sticks out.
     *
     * And phase 0 already measured why it would not: `*-distal -> *-tip` is the one segment where our
     * model and the runtime are measuring different things — the WebXR tip sits at the fingertip
     * SURFACE, derived from the runtime's own estimate of the finger, while the model's tip bone is an
     * authored length. That ratio was the widest-varying column of the whole reading.
     *
     * So each tip bone is scaled by its OWN finger's tracked ÷ bind ratio for that segment. A tip bone
     * is a leaf, so nothing inherits the scale and it cannot propagate; and the number comes from the
     * same measurement that predicted the problem, rather than from a millimetre figure typed in.
     */
    _tipScale: function (pos) {
      var bind = this._bind, out = {};
      for (var i = 0; i < TIPS.length; i++) {
        var tip = TIPS[i], par = TIP_PARENT[tip];
        if (!pos[tip] || !pos[par] || !bind[tip] || !bind[par]) continue;
        var was = bind[par].distanceTo(bind[tip]);
        if (was < 1e-5) continue;
        var r = pos[par].distanceTo(pos[tip]) / was;
        if (r > 0 && r < TIP_MAX) out[tip] = r;
      }
      return out;
    },

    _drive: function (jm) {
      var bones = this._bones, t = this._tmp, s = this._s, tips = this._tips || {};
      for (var i = 0; i < JOINTS.length; i++) {
        var j = JOINTS[i], bone = bones[j];
        var k = tips[j] != null ? tips[j] : s;
        t.sc.set(k, k, k);
        t.m.compose(jm.pos[j], jm.quat[j], t.sc);
        if (bone.parent) {
          t.inv.copy(bone.parent.matrixWorld).invert();
          t.m.premultiply(t.inv);
        }
        t.m.decompose(bone.position, bone.quaternion, bone.scale);
        bone.updateMatrixWorld(false);
      }
      this._restored = false;
    },

    _restore: function () {
      if (this._restored || !this._rest) return;
      var T = this.T, p = new T.Vector3(), q = new T.Quaternion(), sc = new T.Vector3();
      this._rest.forEach(function (m, bone) {
        m.decompose(p, q, sc);
        bone.position.copy(p); bone.quaternion.copy(q); bone.scale.copy(sc);
      });
      this._restored = true;
      this._tips = null;
    },

    tick: function () {
      var want = (this.data.hand || "").toLowerCase();
      if (!want) return;
      var sc = this.el.sceneEl, xr = sc.renderer && sc.renderer.xr, frame = sc.frame;
      var refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
      var session = xr && xr.getSession && xr.getSession();
      if (!frame || !refSpace || !session) return;
      if (!this._collect()) return;

      var sources = session.inputSources || [], jm = null;
      for (var i = 0; i < sources.length; i++) {
        if (sources[i].hand && (sources[i].handedness || "") === want) {
          jm = this._read(frame, refSpace, sources[i].hand);
          break;
        }
      }
      var now = (window.performance && performance.now) ? performance.now() : Date.now();
      if (!jm) {
        // Held, briefly, rather than dropped. Hand tracking blinks out constantly — a hand behind the
        // other hand, a hand at the edge of the cameras — and snapping to the rest pose on every gap
        // would read as a twitch, not as loss.
        if (now - this._lastSeen > LOST_MS) this._restore();
        return;
      }
      this._lastSeen = now;
      var s = this._scale(jm.pos);
      if (s != null) {
        // Eased, so one odd frame cannot flick the girth. It is a slowly-varying property of a hand,
        // not a per-frame measurement.
        this._s = this._s ? this._s * 0.9 + s * 0.1 : s;
      }
      var tips = this._tipScale(jm.pos);
      if (!this._tips) this._tips = tips;
      else {
        for (var k in tips) {
          this._tips[k] = this._tips[k] != null ? this._tips[k] * 0.9 + tips[k] * 0.1 : tips[k];
        }
      }
      this._drive(jm);
    }
  });
})();
