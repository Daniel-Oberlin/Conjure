/* global AFRAME */
// ?hands=fit — LOOK at the hand skeleton the runtime believes in, against the real hand under it.
// Spec: docs/specs/input.md §9. Plan: docs/plans/hands.md phase 0.
//
// WHY: in passthrough the compositor draws your real hand and our layer blends over it, so anything we
// draw at a joint is seen against the real joint underneath. That gap — the tracked skeleton versus your
// actual hand — is the question a worn hand model lives or dies by, and nobody has ever looked at it.
//
// It answers three things, in increasing order of how much you may trust the answer:
//
//  1. DOES IT FIT (by eye). A sphere at each of the 25 joints drawn at the runtime's OWN reported radius,
//     plus an axis triad per joint. Passthrough is reprojected, so judging alignment by eye has an error
//     floor of several millimetres: it resolves "my fingertip is 1.5 cm short", not 2 mm.
//
//  2. IS THE SKELETON A TABLE OR AN ESTIMATE (numeric, and PREMISE-FREE). Bones are rigid, so the 24
//     segment lengths are pose-invariant — measure them in a fist or a flat hand and they agree. If they
//     come back bit-identical frame after frame and acquisition after acquisition, the runtime is serving
//     a static hand model, which the WebXR privacy guidance explicitly permits: a UA that anonymises
//     "must not round each joint independently. Instead the correct way to round is to map each hand to a
//     static hand-model". Jitter means the numbers are coming from the camera image.
//
//  3. IS IT THE SAME HAND AS OURS (numeric, and it HAS a premise). Each tracked length over our own
//     model's bind length: all the same number ⇒ the runtime's skeleton is a uniform scale of our model,
//     and the median IS the `s` phase 2 needs. Scatter means they are different hands — it does NOT mean
//     the runtime is measuring you, because our own two hand models are not mirrors of each other
//     (measured: their index metacarpals differ by 6.3 mm, see BIND). Probe 2 has no premise. Read this
//     one only after it.
//
// The overlay draws whenever ?hands= is set. The numbers need --debug-log to reach temp/conjure.log, so
// the verdicts are also put on a small HUD — in a headset the terminal is not where you are looking.

(function () {
  "use strict";

  var MODES = { off: 1, joints: 1, axes: 1, fit: 1 };

  // The 25 WebXR hand joints, in the spec's own order. Our hand models name their nodes EXACTLY these
  // (measured on b8f676bea0758536 / f586068580143caa — no exporter prefixes), which is why a worn hand
  // needs no convention table at all.
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

  // The 24 bones, as [parent, child] pairs: 4 down the thumb, 5 down each finger. Order is load-bearing —
  // BIND below is indexed by it, and so is every length array this file produces.
  var CHAINS = [
    ["wrist", "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip"],
    ["wrist", "index-finger-metacarpal", "index-finger-phalanx-proximal",
      "index-finger-phalanx-intermediate", "index-finger-phalanx-distal", "index-finger-tip"],
    ["wrist", "middle-finger-metacarpal", "middle-finger-phalanx-proximal",
      "middle-finger-phalanx-intermediate", "middle-finger-phalanx-distal", "middle-finger-tip"],
    ["wrist", "ring-finger-metacarpal", "ring-finger-phalanx-proximal",
      "ring-finger-phalanx-intermediate", "ring-finger-phalanx-distal", "ring-finger-tip"],
    ["wrist", "pinky-finger-metacarpal", "pinky-finger-phalanx-proximal",
      "pinky-finger-phalanx-intermediate", "pinky-finger-phalanx-distal", "pinky-finger-tip"]
  ];
  var SEGMENTS = (function () {
    var out = [];
    CHAINS.forEach(function (c) {
      for (var i = 0; i < c.length - 1; i++) out.push([c[i], c[i + 1]]);
    });
    return out;                                                    // 24, in chain order
  })();

  /**
   * Which of the 24 segments is a BONE, and which two kinds are not. **14 are; 10 are not.**
   *
   * Measured on a Quest 3, 2026-09-17, against our own hand model: the middle of every chain agrees to
   * about 1.02, and both ENDS disagree — the first column varies per finger and the last varies most
   * of all. That is not a hand of different proportions. It is two conventions meeting:
   *
   *   `wrist -> *-metacarpal`   the offset between an arbitrary FRAME ORIGIN and the hand. Our model's
   *                             wrist node and the runtime's wrist pivot need not coincide, and the
   *                             discrepancy points a different way for each finger, so this varies.
   *   `*-distal -> *-tip`       the WebXR tip sits at the fingertip SURFACE, derived from the
   *                             runtime's own estimate of the finger. Our model's tip bone is an
   *                             authored length. They are measuring different things.
   *
   * Neither carries information about how long a BONE is, so neither belongs in a scale estimate.
   * Including them is what produced `s = 1.017` with a 36% spread: a correct median arrived at through
   * ten numbers that should never have been in the set. On the 14 that are bones, the spread is zero.
   */
  function groupOf(seg) {
    if (seg[0] === "wrist") return "wrist";
    return /-tip$/.test(seg[1]) ? "tip" : "bone";
  }
  var GROUPS = SEGMENTS.map(groupOf);

  // Bind-pose lengths of those 24 bones, in METRES, measured from the catalog's clean hand pair on
  // 2026-09-17 — `b8f676bea0758536` (left) and `f586068580143caa` (right) — as world-space distances
  // between the named joint nodes. RE-MEASURE THESE if the models are re-imported; they are data about two
  // specific files, not about hands.
  //
  // Worth knowing before trusting a ratio: THE PAIR IS NOT A MIRROR. Middle, ring and pinky agree to
  // within 0.3 mm, but the right index metacarpal is 6.3 mm shorter than the left and its proximal 4.0 mm
  // longer. So the two files cannot both be the canonical WebXR skeleton, and a non-zero dispersion
  // against either one is as likely to be about our model as about the runtime.
  var BIND = {
    left: [
      0.04945, 0.03053, 0.03214, 0.01706,                          // thumb
      0.03802, 0.05857, 0.04059, 0.02430, 0.01158,                 // index
      0.02984, 0.06397, 0.04541, 0.02755, 0.01220,                 // middle
      0.02880, 0.05980, 0.04105, 0.02662, 0.01194,                 // ring
      0.04217, 0.04318, 0.03321, 0.02027, 0.01191                  // pinky
    ],
    right: [
      0.04973, 0.03123, 0.03342, 0.01574,
      0.03173, 0.06254, 0.04363, 0.02345, 0.01228,
      0.02993, 0.06444, 0.04519, 0.02752, 0.01245,
      0.02882, 0.06020, 0.04121, 0.02635, 0.01194,
      0.04217, 0.04347, 0.03331, 0.01985, 0.01191
    ]
  };

  var SAMPLES = 30;            // frames averaged per window — one frame of a fresh track is noise
  // TWO windows per acquisition, because the first reading conflated two different things. Measured on
  // a Quest 3: jitter came back ~2.5%, sampled over the 30 frames IMMEDIATELY after acquisition — which
  // is exactly when an estimate is still converging. A non-zero number there already rules out a stored
  // table (a table cannot converge), but its MAGNITUDE says nothing about steady state.
  //
  // So: frames 1..30 are the FRESH window, frames 61..90 the SETTLED one. The settled figure is the
  // headline, and the gap between the two is itself the measurement the plan calls re-acquisition — how
  // much the runtime revises its estimate once it has looked for a while.
  var SETTLE = 30;             // frames skipped between the two windows (~0.4 s at 72 Hz)
  var AXIS_LEN = 0.015;        // 15 mm; a joint radius is ~10 mm, so the triad must not swamp it
  var JITTER_RIGID = 1e-6;     // below this, the lengths are a stored table, not an estimate
  var JITTER_NOISE = 0.002;    // below this, float noise rather than re-estimation
  var CV_UNIFORM = 0.01;       // ratio spread below this ⇒ a uniform scale of our model
  var CV_CLOSE = 0.03;

  // ---------------------------------------------------------------- pure maths (tested in tests/js)

  function dist(a, b) {
    var dx = a.x - b.x, dy = a.y - b.y, dz = a.z - b.z;
    return Math.sqrt(dx * dx + dy * dy + dz * dz);
  }

  /** The 24 bone lengths from a {jointName: {x,y,z}} map. `null` for any bone missing an endpoint. */
  function segmentLengths(pos) {
    return SEGMENTS.map(function (s) {
      var a = pos[s[0]], b = pos[s[1]];
      return (a && b) ? dist(a, b) : null;
    });
  }

  function median(xs) {
    var v = xs.slice().sort(function (a, b) { return a - b; }), n = v.length;
    if (!n) return null;
    return n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
  }

  /**
   * Tracked ÷ bind, over the bones both agree on. `cv` is the spread that decides probe 3: the standard
   * deviation over the median, so it is scale-free and a uniform scale reads 0 whatever the scale is.
   */
  var COLLAPSED = 0.25;        // a ratio this far below 1 is not a short bone, it is a missing one

  function ratioStats(lengths, bind, group) {
    var r = [], names = [];
    for (var i = 0; i < SEGMENTS.length; i++) {
      var t = lengths[i], b = bind && bind[i];
      if (t == null || !b) continue;
      if (group && GROUPS[i] !== group) continue;
      r.push(t / b);
      names.push(SEGMENTS[i][1]);
    }
    if (!r.length) return null;
    var med = median(r), mean = r.reduce(function (a, b2) { return a + b2; }, 0) / r.length;
    var varr = r.reduce(function (a, x) { return a + (x - mean) * (x - mean); }, 0) / r.length;
    var sd = Math.sqrt(varr);
    // WHICH bones are the outliers, not only how wide the spread is. A summary number cannot tell a
    // hand of different proportions from a hand with joints the runtime does not report separately,
    // and those call for opposite responses — per-joint scale in the first case, and in the second,
    // not driving those bones from the tracked data at all.
    // Seeded from the FIRST ratio, not from the median. Seeded from the median, a spread that is
    // entirely on one side of it leaves the name null and the verdict says "widest at ?" — which is
    // exactly the case a wide spread produces.
    var lo = r[0], hi = r[0], loAt = names[0], hiAt = names[0], collapsed = [];
    for (var k = 0; k < r.length; k++) {
      if (r[k] < lo) { lo = r[k]; loAt = names[k]; }
      if (r[k] > hi) { hi = r[k]; hiAt = names[k]; }
      if (r[k] < COLLAPSED) collapsed.push(names[k]);
    }
    var out = { n: r.length, median: med, mean: mean, sd: sd, cv: med ? sd / med : 0,
                min: Math.min.apply(null, r), max: Math.max.apply(null, r),
                minAt: loAt, maxAt: hiAt, collapsed: collapsed,
                ratios: r, names: names, group: group || "all" };
    if (!group) {
      // The three populations, separately, because mixing them is what made the whole set look
      // wrong. `bone` is the only one that answers "how big is this hand".
      out.bone = ratioStats(lengths, bind, "bone");
      out.wrist = ratioStats(lengths, bind, "wrist");
      out.tip = ratioStats(lengths, bind, "tip");
      out.s = out.bone ? out.bone.median : med;        // THE scale a worn hand needs
    }
    return out;
  }

  /**
   * How much each bone's measured length MOVED across the sampled frames, as a fraction of its own mean.
   * This is the premise-free probe: a stored table cannot vary, an estimate must.
   */
  function jitterStats(acc) {
    var worst = -1, at = -1, n = 0, sdWorst = -1, sdAt = -1;
    for (var i = 0; i < SEGMENTS.length; i++) {
      if (!acc.n[i]) continue;
      n++;
      var mean = acc.sum[i] / acc.n[i];
      if (!mean) continue;
      var rel = (acc.max[i] - acc.min[i]) / mean;
      if (rel > worst) { worst = rel; at = i; }
      // RANGE and SD together, because range alone is dominated by a single frame. 29 identical
      // frames and one bad one give a 2.5% range on a table that never changed — which is how a
      // mirrored table first read as a per-joint estimate. SD says whether the movement is the
      // signal or the exception.
      if (acc.sq && acc.n[i] > 1) {
        var v = acc.sq[i] / acc.n[i] - mean * mean;
        var sd = Math.sqrt(v > 0 ? v : 0) / mean;
        if (sd > sdWorst) { sdWorst = sd; sdAt = i; }
      }
    }
    if (!n) return null;
    return { bones: n, max: worst, at: at, name: at >= 0 ? SEGMENTS[at][1] : null,
             sd: sdWorst >= 0 ? sdWorst : null, sdName: sdAt >= 0 ? SEGMENTS[sdAt][1] : null };
  }

  /** Bone-by-bone difference between two hands. A real left/right pair differs; a mirrored table does not. */
  function compareHands(a, b) {
    var worst = -1, at = -1, sq = 0, n = 0;
    for (var i = 0; i < SEGMENTS.length; i++) {
      if (a[i] == null || b[i] == null) continue;
      var d = Math.abs(a[i] - b[i]);
      sq += d * d; n++;
      if (d > worst) { worst = d; at = i; }
    }
    if (!n) return null;
    return { bones: n, maxAbs: worst, at: at, name: at >= 0 ? SEGMENTS[at][1] : null,
             rms: Math.sqrt(sq / n) };
  }

  /**
   * What a jitter figure licenses you to say, and no more.
   *
   * The sound inference is about RIGIDITY, not about measurement. A stored hand-model that is POSED
   * gives constant bone lengths however noisy the pose, because posing rotates bones and cannot
   * stretch them. So lengths that move mean the joints are positioned INDEPENDENTLY rather than read
   * off a rigid skeleton — which is exactly what the WebXR privacy guidance says an anonymising UA
   * must not do ("must not round each joint independently").
   *
   * It does NOT by itself establish that the size is *yours*. Independent per-joint positions could
   * still be a fixed skeleton plus noise. The probes that separate those are perturbation (a glove)
   * and a second pair of hands, and neither is this number.
   */
  function jitterVerdict(j) {
    if (!j) return "no bones measured";
    var m = (j.sd == null) ? j.max : j.sd;       // SD when we have it; range is the fallback
    if (m <= JITTER_RIGID) return "RIGID — bit-identical lengths; a stored skeleton, posed";
    if (m <= JITTER_NOISE) return "RIGID — float noise only; still a stored skeleton, posed";
    // A CLAUSE, not a different verdict. Over 30 samples a normal spread already gives a range about
    // four times the SD, so "range >> sd" is not a threshold that distinguishes an outlier from
    // ordinary noise — it only says WHERE to look. Asserting more than that would be inventing a
    // statistic, and the honest move is to report both numbers and name what a gap between them
    // means.
    var concentrated = (j.sd != null && j.max > j.sd * 5)
      ? " — and the movement is concentrated in a few frames (range " + (100 * j.max).toFixed(2)
        + "% against sd " + (100 * j.sd).toFixed(2) + "%), so look at dropped tracking before size"
      : "";
    return "NOT RIGID — bone lengths move, so joints are positioned independently, not posed "
         + "off a fixed skeleton (which says nothing yet about whose hand it is)" + concentrated;
  }

  function ratioVerdict(r) {
    if (!r) return "no bind length to compare against";
    // Judged on the 14 BONES. The other ten segments are frame and endpoint conventions, and letting
    // them into the verdict reported a perfectly uniform hand as "different proportions".
    if (r.bone && !r.collapsed.length) {
      var b = r.bone;
      var ends = (r.wrist && r.tip)
        ? "  (wrist offsets " + r.wrist.median.toFixed(2) + "\u00d7, tips "
          + r.tip.median.toFixed(2) + "\u00d7 \u2014 conventions, not bones)" : "";
      if (b.cv <= CV_UNIFORM) return "UNIFORM on the 14 bones; s = " + b.median.toFixed(4) + ends;
      if (b.cv <= CV_CLOSE) return "CLOSE on the 14 bones; s = " + b.median.toFixed(4) + ends;
      return "DIFFERENT PROPORTIONS on the 14 bones; widest at " + (b.maxAt || "?")
        + " (" + b.max.toFixed(2) + "\u00d7), narrowest at " + (b.minAt || "?")
        + " (" + b.min.toFixed(2) + "\u00d7)" + ends;
    }
    // Asked BEFORE the spread, because a collapsed bone explains a wide spread and a wide spread does
    // not explain a collapsed bone. Reporting `cv` alone on a hand with four zero-length bones reads
    // as "different proportions" and sends you to rescale a model that is fine.
    if (r.collapsed.length) {
      return "MISSING JOINTS — " + r.collapsed.length + " bone(s) read as ~0 length ("
        + r.collapsed.slice(0, 4).join(", ") + (r.collapsed.length > 4 ? ", …" : "")
        + "); the runtime is not reporting those joints apart from their parent, so they are not a "
        + "size difference and must not be driven from tracked data";
    }
    if (r.cv <= CV_UNIFORM) return "UNIFORM — a uniform scale of our model; s = " + r.median.toFixed(4);
    if (r.cv <= CV_CLOSE) return "CLOSE — same hand, slightly different proportions; s = " + r.median.toFixed(4);
    return "DIFFERENT PROPORTIONS — not a uniform scale of our model; widest at "
      + (r.maxAt || "?") + " (" + r.max.toFixed(2) + "\u00d7) and narrowest at "
      + (r.minAt || "?") + " (" + r.min.toFixed(2) + "\u00d7)";
  }

  function resolveMode() {
    var q = "";
    try { q = new URLSearchParams(location.search).get("hands") || ""; } catch (e) { /* no URL */ }
    q = q.trim().toLowerCase();
    if (q === "1" || q === "true" || q === "on") q = "fit";
    return MODES[q] ? q : "off";
  }

  function cm(x) { return x == null ? "—" : (x * 100).toFixed(2); }
  function pct(x) { return x == null ? "—" : (x * 100).toFixed(3) + "%"; }

  window.HandsFit = {
    JOINTS: JOINTS, SEGMENTS: SEGMENTS, CHAINS: CHAINS, BIND: BIND,
    SAMPLES: SAMPLES, SETTLE: SETTLE,
    segmentLengths: segmentLengths, ratioStats: ratioStats, jitterStats: jitterStats,
    compareHands: compareHands, jitterVerdict: jitterVerdict, ratioVerdict: ratioVerdict,
    resolveMode: resolveMode, median: median,
  };

  // ---------------------------------------------------------------- the overlay

  if (!window.AFRAME || AFRAME.components["hands-fit"]) return;

  function log(msg) {
    try { console.log("[hands] " + msg); } catch (e) {}
    if (!window.CONJURE_DEBUG_LOG) return;
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: "hands", msg: msg }) }).catch(function () {});
    } catch (e) { /* never let logging break a frame */ }
  }

  AFRAME.registerComponent("hands-fit", {
    init: function () {
      this.mode = resolveMode();
      this.T = AFRAME.THREE;
      this._hands = {};                 // handedness → { group, spheres, axes, axPos }
      this._st = {};                    // handedness → sampler + last report
      this._t = 0;
      this._hud = null;
      if (this.mode === "off") return;

      log("mode=" + this.mode + " — spheres at the REPORTED radius; triad is +X red, "
        + "-Y green (out of the palm), -Z blue (along the bone, away from the wrist)");
      var occ = (window.CONJURE_OCCLUSION == null ? "" : String(window.CONJURE_OCCLUSION)).toLowerCase();
      if (occ === "hands" || occ === "hands-solid") {
        log("! --occlusion " + occ + " is also on: the hand occluder carves depth exactly where this "
          + "overlay draws, so joints will disappear behind your real hand. Use ?occlusion=off to look.");
      }
      this._sphereGeo = new this.T.SphereGeometry(1, 10, 8);
    },

    remove: function () {
      for (var h in this._hands) {
        var rec = this._hands[h];
        this.el.object3D.remove(rec.group);
        rec.group.traverse(function (o) {
          if (o.geometry && o.geometry !== this._sphereGeo) o.geometry.dispose();
          if (o.material) o.material.dispose();
        }.bind(this));
      }
      if (this._sphereGeo) this._sphereGeo.dispose();
      this._hands = {};
      if (this._hud && this._hud.parentNode) this._hud.parentNode.removeChild(this._hud);
    },

    // One group per hand: 25 translucent spheres (so the real knuckle shows through) and one LineSegments
    // carrying every triad. depthWrite off so the spheres never hide each other; the triads skip depth
    // entirely, because a 1-pixel line behind a sphere is invisible and the direction is the point.
    _build: function (handed) {
      var T = this.T, group = new T.Group(), spheres = {};
      var tint = handed === "left" ? 0x00e5ff : 0xffa000;
      for (var i = 0; i < JOINTS.length; i++) {
        var name = JOINTS[i];
        // The wrist is the one joint the WebXR spec leaves loose ("SHOULD point roughly towards the
        // centre of the palm"), so it is drawn in its own colour — it is the one to look at first.
        var mat = new T.MeshBasicMaterial({
          color: name === "wrist" ? 0xff3d71 : tint,
          transparent: true, opacity: 0.35, depthWrite: false, side: T.DoubleSide,
        });
        var m = new T.Mesh(this._sphereGeo, mat);
        m.frustumCulled = false;
        m.visible = false;
        group.add(m);
        spheres[name] = m;
      }

      var n = JOINTS.length * 3 * 2, axPos = new Float32Array(n * 3), col = new Float32Array(n * 3);
      var AX = [[1, 0.27, 0.27], [0.27, 1, 0.33], [0.33, 0.55, 1]];   // +X red, -Y green, -Z blue
      for (var j = 0; j < JOINTS.length; j++) {
        for (var a = 0; a < 3; a++) {
          for (var v = 0; v < 2; v++) {
            var o = ((j * 3 + a) * 2 + v) * 3;
            col[o] = AX[a][0]; col[o + 1] = AX[a][1]; col[o + 2] = AX[a][2];
          }
        }
      }
      var geo = new T.BufferGeometry();
      geo.setAttribute("position", new T.BufferAttribute(axPos, 3));
      geo.setAttribute("color", new T.BufferAttribute(col, 3));
      var axes = new T.LineSegments(geo, new T.LineBasicMaterial({
        vertexColors: true, transparent: true, opacity: 0.95, depthWrite: false, depthTest: false,
      }));
      axes.frustumCulled = false;
      axes.renderOrder = 1000;
      axes.visible = false;
      group.add(axes);

      this.el.object3D.add(group);                                    // scene root = XR reference space
      return { group: group, spheres: spheres, axes: axes, axPos: axPos, geo: geo };
    },

    // Read all 25 joints for one hand. Returns null only if the WRIST is missing — a partial hand is still
    // worth drawing here, unlike the occluder, because a torn skeleton is itself the finding.
    _read: function (frame, refSpace, hand) {
      if (!frame.getJointPose || !hand) return null;
      var pos = {}, quat = {}, rad = {}, got = 0, noRadius = 0;
      for (var i = 0; i < JOINTS.length; i++) {
        var name = JOINTS[i];
        var space = hand.get ? hand.get(name) : null;
        var pose = space && frame.getJointPose(space, refSpace);
        if (!pose) continue;
        var p = pose.transform.position, q = pose.transform.orientation;
        pos[name] = { x: p.x, y: p.y, z: p.z };
        quat[name] = { x: q.x, y: q.y, z: q.z, w: q.w };
        if (pose.radius == null) { noRadius++; rad[name] = null; } else { rad[name] = pose.radius; }
        got++;
      }
      if (!pos.wrist) return null;
      return { pos: pos, quat: quat, rad: rad, got: got, noRadius: noRadius };
    },

    _draw: function (rec, jm) {
      var T = this.T, q = new T.Quaternion(), v = new T.Vector3();
      var drawJoints = this.mode === "fit" || this.mode === "joints";
      var drawAxes = this.mode === "fit" || this.mode === "axes";

      for (var i = 0; i < JOINTS.length; i++) {
        var name = JOINTS[i], p = jm.pos[name], m = rec.spheres[name];
        if (!p || !drawJoints) { m.visible = false; continue; }
        var r = jm.rad[name];
        m.visible = true;
        m.position.set(p.x, p.y, p.z);
        m.scale.setScalar(r == null ? 0.008 : r);                     // no radius ⇒ a fixed 8 mm, and the log says so
        m.updateMatrix();
      }

      rec.axes.visible = drawAxes;
      if (drawAxes) {
        for (var j = 0; j < JOINTS.length; j++) {
          var nm = JOINTS[j], pp = jm.pos[nm], qq = jm.quat[nm];
          for (var a = 0; a < 3; a++) {
            var base = ((j * 3 + a) * 2) * 3;
            if (!pp || !qq) {                                         // collapse the triad to a point
              for (var k = 0; k < 6; k++) rec.axPos[base + k] = 0;
              continue;
            }
            q.set(qq.x, qq.y, qq.z, qq.w);
            // The three directions the WebXR convention is stated in: +X, then -Y out through the palm,
            // then -Z along the bone away from the wrist. Drawing +Y/+Z instead would render the
            // convention backwards and read as a broken rig.
            v.set(a === 0 ? 1 : 0, a === 1 ? -1 : 0, a === 2 ? -1 : 0).applyQuaternion(q);
            rec.axPos[base] = pp.x; rec.axPos[base + 1] = pp.y; rec.axPos[base + 2] = pp.z;
            rec.axPos[base + 3] = pp.x + v.x * AXIS_LEN;
            rec.axPos[base + 4] = pp.y + v.y * AXIS_LEN;
            rec.axPos[base + 5] = pp.z + v.z * AXIS_LEN;
          }
        }
        rec.geo.attributes.position.needsUpdate = true;
      }
    },

    // ---- the numeric half: accumulate SAMPLES frames per acquisition, then report once.

    _window: function () {
      var n = SEGMENTS.length;
      return { frames: 0, n: new Array(n).fill(0), sum: new Array(n).fill(0),
               sq: new Array(n).fill(0),
               min: new Array(n).fill(Infinity), max: new Array(n).fill(-Infinity),
               radSum: {}, radN: {}, noRadius: 0 };
    },

    _sampler: function () {
      return { frames: 0, fresh: this._window(), settled: this._window(), reported: false };
    },

    _into: function (w, jm, L) {
      w.frames++;
      for (var i = 0; i < L.length; i++) {
        if (L[i] == null) continue;
        w.n[i]++; w.sum[i] += L[i]; w.sq[i] += L[i] * L[i];
        if (L[i] < w.min[i]) w.min[i] = L[i];
        if (L[i] > w.max[i]) w.max[i] = L[i];
      }
      w.noRadius += jm.noRadius;
      for (var name in jm.rad) {
        var r = jm.rad[name];
        if (r == null) continue;
        w.radSum[name] = (w.radSum[name] || 0) + r;
        w.radN[name] = (w.radN[name] || 0) + 1;
      }
    },

    _sample: function (st, jm) {
      var L = segmentLengths(jm.pos);
      st.frames++;
      if (st.frames <= SAMPLES) this._into(st.fresh, jm, L);
      else if (st.frames > SAMPLES + SETTLE) this._into(st.settled, jm, L);
      return L;
    },

    _done: function (st) { return st.frames >= SAMPLES + SETTLE + SAMPLES; },

    _report: function (handed, st, acq) {
      var w = st.settled, mean = [];
      for (var i = 0; i < SEGMENTS.length; i++) mean.push(w.n[i] ? w.sum[i] / w.n[i] : null);
      var jit = jitterStats(w);
      var fresh = jitterStats(st.fresh);
      var rat = ratioStats(mean, BIND[handed]);

      log(handed + " #" + acq + " — " + (jit ? jit.bones : 0) + "/24 bones, settled window "
        + "(frames " + (SAMPLES + SETTLE + 1) + "\u2013" + st.frames + ")");
      var at = 0;
      CHAINS.forEach(function (c) {
        var row = [];
        for (var k = 0; k < c.length - 1; k++) row.push(cm(mean[at++]));
        log("  " + c[1].split("-")[0] + " lengths(cm) " + row.join(" "));
      });
      log("  jitter: max " + pct(jit && jit.max) + (jit && jit.name ? " at " + jit.name : "")
        + "  ⇒ " + jitterVerdict(jit));
      // The FRESH window on its own says nothing a table could not; the two together say whether the
      // runtime revises its estimate once it has looked for a while, which is the plan's
      // re-acquisition probe asked without needing you to move.
      if (fresh) {
        log("  jitter on acquisition: max " + pct(fresh.max)
          + (jit && fresh.max > jit.max * 1.5 ? "  ⇒ CONVERGING — the fresh reading is the worse one"
             : jit && jit.max > fresh.max * 1.5 ? "  ⇒ the settled reading is worse, which is odd"
             : "  ⇒ steady from the start"));
      }
      // A means-of-means comparison, so it is about the estimate and not about frame noise.
      var fm = [];
      for (var k2 = 0; k2 < SEGMENTS.length; k2++) fm.push(st.fresh.n[k2] ? st.fresh.sum[k2] / st.fresh.n[k2] : null);
      var drift = compareHands(fm, mean);
      if (drift) {
        log("  revision fresh\u2192settled: max |\u0394| " + cm(drift.maxAbs) + " cm at " + drift.name
          + ", rms " + cm(drift.rms) + " cm");
      }
      if (rat) {
        log("  vs bind(" + handed + "): s median " + rat.median.toFixed(4) + " cv " + pct(rat.cv)
          + " range " + rat.min.toFixed(4) + "–" + rat.max.toFixed(4) + "  ⇒ " + ratioVerdict(rat));
      }
      var radii = [], distinct = {};
      for (var nm in w.radSum) {
        var r = w.radSum[nm] / w.radN[nm];
        radii.push(r);
        distinct[r.toFixed(5)] = 1;
      }
      if (w.noRadius) {
        log("  radius: MISSING on " + w.noRadius + " joint-reads — the UA is not supplying one, so the "
          + "spheres are a fixed 8 mm and girth is unknowable from the runtime");
      } else if (radii.length) {
        log("  radii(cm) " + cm(Math.min.apply(null, radii)) + "–" + cm(Math.max.apply(null, radii))
          + " over " + radii.length + " joints, " + Object.keys(distinct).length + " distinct value(s)"
          + (Object.keys(distinct).length <= 3 ? "  ⇒ a TABLE, not a per-joint measurement" : ""));
      }

      var prev = this._st[handed] && this._st[handed].history;
      var hist = (prev || []).concat(rat ? [rat.median] : []);
      if (hist.length > 1) {
        var lo = Math.min.apply(null, hist), hi = Math.max.apply(null, hist);
        log("  s across " + hist.length + " acquisitions: " + hist.map(function (x) { return x.toFixed(4); }).join(", ")
          + "  spread " + pct(lo ? (hi - lo) / lo : 0)
          + (hi - lo < 1e-9 ? "  ⇒ IDENTICAL — re-acquisition does not re-estimate" : ""));
      }

      var other = handed === "left" ? "right" : "left";
      var os = this._st[other];
      if (os && os.mean) {
        var cmp = compareHands(mean, os.mean);
        if (cmp) {
          log("  left vs right (tracked): max |Δ| " + cm(cmp.maxAbs) + " cm at " + cmp.name
            + ", rms " + cm(cmp.rms) + " cm  ⇒ "
            + (cmp.maxAbs < 0.0002 ? "MIRROR — the same table twice"
                                   : "NOT A MIRROR — the two hands differ, as a real pair must"));
        }
      }
      return { mean: mean, jit: jit, fresh: fresh, rat: rat, history: hist };
    },

    _hudLine: function () {
      var lines = ["hands=" + this.mode];
      ["left", "right"].forEach(function (h) {
        var st = this._st[h];
        if (!st) return;
        if (!st.rat && !st.jit) { lines.push(h + " sampling…"); return; }
        lines.push(h.charAt(0).toUpperCase() + "  s=" + (st.rat ? st.rat.s.toFixed(3) : "—")
          + "  cv=" + (st.rat && st.rat.bone ? pct(st.rat.bone.cv) : "—")
          + (st.rat && st.rat.collapsed.length ? " (" + st.rat.collapsed.length + " bones ~0)" : "")
          + "  jit=" + (st.jit && st.jit.sd != null ? pct(st.jit.sd) : "—")
          + (st.jit ? " sd / " + pct(st.jit.max) + " range" : ""));
        // Both verdicts, not only the jitter one. Putting a summary statistic on screen with no
        // reading of it is how three sessions went: `cv=36%` is a number you cannot act on, and the
        // sentence that says WHICH BONES is the one that ends the guessing.
        if (st.jit) lines.push("   " + jitterVerdict(st.jit).split(" \u2014 ")[0]);
        if (st.rat) lines.push("   " + ratioVerdict(st.rat).split(";")[0]);
        // And when the spread is wide, the ratios themselves — per chain, two figures each. A summary
        // cannot distinguish "every bone is 1.3x" from "five bones are 2x and the rest are 1.0", and
        // those are different findings with different fixes. Shown only when there is something to
        // explain, so a uniform hand stays a three-line HUD.
        if (st.rat && ((st.rat.bone && st.rat.bone.cv > CV_CLOSE) || st.rat.collapsed.length)) {
          var by = {};
          for (var k = 0; k < st.rat.names.length; k++) by[st.rat.names[k]] = st.rat.ratios[k];
          lines.push("   [wrist offset]  ...bones...  [tip]");
          CHAINS.forEach(function (c) {
            var row = [];
            for (var j = 1; j < c.length; j++) {
              var v = by[c[j]];
              row.push(v == null ? " -- " : (v < 10 ? v.toFixed(2) : ">9.9"));
            }
            lines.push("   " + (c[1].split("-")[0] + "      ").slice(0, 6) + " " + row.join(" "));
          });
        }
      }, this);
      var l = this._st.left, r = this._st.right;
      if (l && l.mean && r && r.mean) {
        var cmp = compareHands(l.mean, r.mean);
        // MILLIMETRES, to three places. In centimetres to two, everything below 0.05 mm printed as
        // "0.00" — and "the two hands agree exactly" is a completely different finding from "they
        // agree to a tenth of a millimetre", so the display must not be the thing that decides.
        if (cmp) lines.push("L/R max Δ " + (cmp.maxAbs * 1000).toFixed(3) + " mm at " + cmp.name
          + (cmp.maxAbs === 0 ? "  EXACT — one table, mirrored" : ""));
      }
      return lines.join("\n");
    },

    _hudShow: function (text) {
      if (!this._hud) {
        var cam = document.querySelector("a-camera") || document.querySelector("[camera]");
        if (!cam) return;
        var el = document.createElement("a-entity");
        el.id = "hands-fit-hud";
        el.setAttribute("position", "0 -0.22 -1");
        el.setAttribute("text", { value: "", align: "center", color: "#9fdcb0", width: 1.1,
                                  baseline: "center" });
        el.setAttribute("overlay", "");
        cam.appendChild(el);
        this._hud = el;
      }
      this._hud.setAttribute("text", "value", text);
    },

    tick: function () {
      if (this.mode === "off") return;
      var sc = this.el, xr = sc.renderer && sc.renderer.xr, frame = sc.frame;
      var refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
      var session = xr && xr.getSession && xr.getSession();
      if (!frame || !refSpace || !session) return;

      var sources = session.inputSources || [], seen = {}, handsSeen = 0;
      for (var s = 0; s < sources.length; s++) {
        var src = sources[s];
        if (!src.hand) continue;
        handsSeen++;
        var handed = src.handedness || ("hand" + s);
        seen[handed] = 1;
        var rec = this._hands[handed] || (this._hands[handed] = this._build(handed));
        var jm = this._read(frame, refSpace, src.hand);
        if (!jm) { rec.group.visible = false; continue; }
        rec.group.visible = true;
        this._draw(rec, jm);

        var st = this._st[handed];
        if (!st || !st.present) {                                    // absent → present: a new acquisition
          var acq = ((st && st.acq) || 0) + 1;
          var history = (st && st.history) || [];
          st = this._st[handed] = this._sampler();
          st.acq = acq; st.history = history; st.present = true;
          log(handed + " acquired (#" + acq + ") — sampling " + SAMPLES + " frames");
        }
        if (!st.reported) {
          this._sample(st, jm);
          if (this._done(st)) {
            st.reported = true;
            var out = this._report(handed, st, st.acq);
            st.mean = out.mean; st.jit = out.jit; st.fresh = out.fresh;
            st.rat = out.rat; st.history = out.history;
            this._hudShow(this._hudLine());
          }
        }
      }
      for (var h in this._hands) if (!seen[h]) this._hands[h].group.visible = false;
      for (var k in this._st) if (!seen[k]) this._st[k].present = false;

      if (++this._t % 72 === 0) {
        if (!handsSeen) {
          var desc = [];
          for (var i = 0; i < sources.length; i++) {
            desc.push((sources[i].handedness || "?") + "/" + (sources[i].targetRayMode || "?"));
          }
          log("no tracked hands; inputSources=[" + desc.join(",") + "] — put the controllers down");
          this._hudShow("hands=" + this.mode + "\nno tracked hands\nput the controllers down");
        } else {
          this._hudShow(this._hudLine());
        }
      }
    },
  });
})();
