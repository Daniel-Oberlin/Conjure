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
      joints: { type: "string", default: "" },   // {webxrJoint: nodeName}; identity today, see hands.py
      // How far to push each fingertip out along its own bone: a number of MILLIMETRES, or `radius`
      // for one tip radius as the runtime reports it (`radius:0.8` to scale that). See `_tipOut`.
      // `off` is "exactly where the runtime says the tip is", which was the default until the radius
      // rule was read on device. THESE TWO MUST MATCH conjure/hands.py's TIP_OUT_DEFAULT and
      // TIP_THUMB_DEFAULT, which carry the provenance and which a test pins them against — every path
      // that sets this component goes through the server, so a drift here would be invisible.
      tipOut: { type: "string", default: "radius" },
      // A coefficient applied to the THUMB's radius only, in `radius` mode. Its own axis because the
      // thumb is anatomically its own case — see `_tipOut`.
      tipThumb: { type: "string", default: "0.7" }
    },

    init: function () {
      var self = this;
      this.T = AFRAME.THREE;
      this._bones = null;
      this._bind = null;             // {joint: world position at rest}
      this._rest = null;             // bone -> local matrix at rest, for putting it back
      this._s = 1;
      this._said = 0;
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
      var pos = {}, quat = {}, rad = {}, got = 0;
      for (var i = 0; i < JOINTS.length; i++) {
        var space = hand.get(JOINTS[i]);
        var pose = space && frame.getJointPose(space, refSpace);
        if (!pose) continue;
        var p = pose.transform.position, q = pose.transform.orientation;
        pos[JOINTS[i]] = new this.T.Vector3(p.x, p.y, p.z);
        quat[JOINTS[i]] = new this.T.Quaternion(q.x, q.y, q.z, q.w);
        rad[JOINTS[i]] = pose.radius == null ? null : pose.radius;
        got++;
      }
      // All or nothing. A partial skeleton would leave some bones driven and the rest at rest, which
      // renders as a hand tearing itself apart — far worse than a hand that simply stops.
      return got === JOINTS.length ? { pos: pos, quat: quat, rad: rad } : null;
    },

    /**
     * The numbers a tip rule would have to be found in, logged once per wearing.
     *
     * Deliberately not a verdict. What it prints is what is actually knowable — each finger's tracked
     * and bind `distal → tip`, their ratio, and the radius the runtime reports at the tip — so that
     * whether the right rule is a constant, a multiple of the radius, or per-finger can be READ off a
     * headset instead of reasoned to. Reasoning to it is what produced pointy fingers.
     */
    _report: function (jm) {
      if (this._said || !window.CONJURE_DEBUG_LOG) return;
      this._said = 1;
      var bind = this._bind, rows = [];
      for (var i = 0; i < TIPS.length; i++) {
        var tip = TIPS[i], par = TIP_PARENT[tip];
        if (!jm.pos[tip] || !bind[tip]) continue;
        var track = jm.pos[par].distanceTo(jm.pos[tip]) * 100;
        var was = bind[par].distanceTo(bind[tip]) * 100;
        var r = jm.rad[tip] == null ? "—" : (jm.rad[tip] * 100).toFixed(2);
        rows.push(tip.split("-")[0] + " tracked " + track.toFixed(2) + " bind " + was.toFixed(2)
          + " ratio " + (was ? (track / was).toFixed(2) : "—") + " radius " + r);
      }
      log("tips (cm), s=" + this._s.toFixed(3) + ", tipOut=" + this.data.tipOut
        + " thumb×" + this.data.tipThumb
        + " → " + TIPS.map(function (x) {
            return (this._tipOut(x, jm) * 1000).toFixed(1);
          }, this).join("/") + "mm:\n  " + rows.join("\n  "));
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
     * How far each tip bone is pushed out along its own −Z, in metres. **A tunable, not a derivation,
     * and marked as one** — which is the point of it.
     *
     * Reported on device: the wearer's real fingertips protruded ~5 mm beyond the virtual ones. The
     * tip JOINT lands exactly where the runtime says, as every joint does, so this is the FLESH.
     *
     * The first attempt at it was wrong, in a way worth keeping written down. It scaled each tip bone
     * by that finger's tracked ÷ bind ratio for `distal → tip`, reasoning from phase 0 that this was
     * the one segment where the model and the runtime measure different things. It is — but the ratio
     * came out BELOW one, so the caps shrank: the fingers went pointy and got shorter still. The
     * reasoning was sound and the sign was an assumption, and the difference between those two is the
     * whole lesson. `*-distal → *-tip` being unreliable does not tell you which way it is unreliable.
     *
     * There is no measurement available that would settle it — the gap is between the runtime's tip
     * estimate and the wearer's actual fingertip, and the runtime does not report the second. So this
     * is a number someone sets, defaulting to zero, with the tip radii logged beside it so a rule can
     * be found if one exists (a constant? proportional to the radius? per finger?).
     */
    _tipOut: function (joint, jm) {
      var v = String(this.data.tipOut == null ? "0" : this.data.tipOut).trim().toLowerCase();
      // `off` and `none` mean 0, spelled the way someone asks for it. An unparseable value already
      // fell through to 0 by accident; saying so on purpose is the difference between a guard and a
      // documented way to turn something off.
      if (v === "off" || v === "none" || v === "") return 0;
      // `radius` — ONE TIP RADIUS out, per finger, from the number the runtime itself reports.
      //
      // Worth trying because 8 mm, the value that landed it for one wearer, is about a human
      // fingertip radius — and there is a reason it would be: the WebXR tip joint sits at the CENTRE
      // of the fingertip, and `XRJointPose.radius` is that fingertip's radius, so the surface is one
      // radius further out. If that is the rule then it is not one person's 8 mm at all; it is
      // per-finger (a thumb is fatter than a pinky) and it generalises to any hand the runtime
      // measures. `radius:0.8` scales it, for testing the coefficient rather than assuming 1.
      if (v.charAt(0) === "r") {
        var k = parseFloat(v.split(":")[1]);
        if (!isFinite(k)) k = 1;
        // THE THUMB IS ITS OWN CASE, and not because of one wearer. Measured on a Quest 3: one radius
        // lands all four fingers and leaves the thumb slightly long. That is a fact about thumbs. A
        // thumb has one fewer phalanx and a broad, flat pad — its reported radius is the half-width
        // of that pad, which OVERSTATES how far the tip protrudes, where on a rounder fingertip the
        // two are nearly the same. So it takes its own coefficient, and it generalises for the same
        // reason the radius rule does: it is about anatomy, not about a hand.
        if (joint === "thumb-tip") {
          var tk = parseFloat(this.data.tipThumb);
          if (isFinite(tk) && tk >= 0 && tk <= 2) k *= tk;
        }
        var r = jm && jm.rad ? jm.rad[joint] : null;
        return (r == null || !(r > 0 && r < 0.05)) ? 0 : r * k;
      }
      var mm = parseFloat(v);
      return (isFinite(mm) && mm > -30 && mm < 30) ? mm / 1000 : 0;
    },

    _drive: function (jm) {
      var bones = this._bones, t = this._tmp, s = this._s;
      for (var i = 0; i < JOINTS.length; i++) {
        var j = JOINTS[i], bone = bones[j];
        t.sc.set(s, s, s);
        var at = jm.pos[j];
        var out = TIP_PARENT[j] ? this._tipOut(j, jm) : 0;
        if (out) {
          // Along the JOINT'S OWN −Z, which is the bone direction away from the wrist — measured on
          // both catalog skeletons at ≥0.986 down every finger, so it is the axis to push along.
          t.p.set(0, 0, -1).applyQuaternion(jm.quat[j]).multiplyScalar(out);
          at = t.p.add(at);
        }
        t.m.compose(at, jm.quat[j], t.sc);
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
      this._report(jm);
      this._drive(jm);
    }
  });
})();
