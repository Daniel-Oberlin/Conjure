/* global AFRAME */
// grab — tier-C object manipulation (docs/dynamic-content-plan.md §Tier-C: grab, docs/dynamic-module-spec.md).
// A singleton, ambient module that lets you reposition/rotate/resize OTHER placed objects with the
// controllers. It's the first module that reads + writes scene entities beyond its own node.
//
// Interaction is GRIP-centric so it never collides with the TRIGGER (= content interaction, e.g. rippling a
// water picture):
//   • hover (no button)      → an oriented highlight box + corner handles on the pointed-at object
//   • GRIP on the body       → grab. Free objects: full 6DOF (move + wrist-twist); thumbstick reels in/out.
//                              Surface-attached objects (meta.on_surface): slide on the surface plane.
//   • GRIP on a corner handle→ uniform resize (transform.scale), proportions preserved.
//   • release                → commit.
//
// Sync (tier C): dragging mutates the local object3D ONLY — nothing is broadcast mid-drag. On release the
// client POSTs the resting transform to /manipulate; the world server authorizes (owner), applies, persists,
// recomputes surface_offset for on-surface content, and broadcasts to all. The mover's echo is idempotent
// (it already holds these values) → no pop. Owner-only, gated here (guests get a hint, no grab) AND server-side.

(function () {
  "use strict";
  if (!window.AFRAME) return;
  if (AFRAME.components.grab) return;

  var HILITE = 0x66ccff, HANDLE = 0xffcc33, HANDLE_R = 0.02;   // handle sphere radius, WORLD metres (_setHud
  //                                                              divides out the target's scale)
  var SCALE_MIN = 0.05, SCALE_MAX = 50.0;      // absolute clamp on the resulting transform.scale
  var HANDLE_PICK = 0.06;                      // world metres of aim slop around a corner handle
  var SCALE_REF = 0.5;                         // hand travel (m) that doubles/halves the size
  var SCALE_F_MIN = 0.25, SCALE_F_MAX = 4.0;   // clamp on ONE resize gesture
  var ONE = null;   // set once AFRAME.THREE exists

  // Diagnostics → console + the world server's /client_log (same temp/conjure.log as [water]/[room]), gated
  // by CONJURE_DEBUG_LOG. XR interaction can't be unit-tested, so on-device tracing is the only way to see
  // what this module is doing — a silent failure here is indistinguishable from "not conjured".
  function glog(msg) {
    if (!window.CONJURE_DEBUG_LOG) return;
    try { console.log("[grab] " + msg); } catch (e) {}
    try { fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tag: "grab", msg: msg }) }).catch(function () {}); } catch (e) {}
  }

  function urlUser() { return new URLSearchParams(location.search).get("user") || ""; }
  // Empty user or unknown owner ⇒ treated as owner (matches the server's missing-header rule).
  function amOwner() { var me = urlUser(), o = window.CONJURE_OWNER; return !me || !o || me === o; }

  AFRAME.registerComponent("grab", {
    schema: { reelSpeed: { type: "number", default: 1.5 } },

    init: function () {
      var THREE = AFRAME.THREE; ONE = new THREE.Vector3(1, 1, 1);
      this._ray = new THREE.Raycaster();
      this._ctrl = {};                       // per-controller state: { mode, target, ... }
      this._hud = { el: null, group: null, handles: [] };
      this._hinted = 0;
      this._seen = {};                       // one-shot diagnostic latches (see _once)
      glog("init — owner=" + amOwner() + " (point at an object and squeeze GRIP to move it)");
    },

    // Log `msg` the FIRST time this `key` fires — a per-frame trace would flood the log.
    _once: function (key, msg) {
      if (this._seen[key]) return;
      this._seen[key] = 1;
      glog(msg);
    },

    // ---- manipulable discovery -------------------------------------------------------------------
    // Direct world entities that aren't the room, scaffold, or this module — models, images, water, etc.
    _manipulables: function () {
      var root = document.getElementById("world-root");
      if (!root) return [];
      var out = [], kids = root.children;
      for (var i = 0; i < kids.length; i++) {
        var el = kids[i];
        if (!el.id || el.dataset.real || el.dataset.scaffold) continue;
        if (el.components && el.components.grab) continue;    // never grab the grab entity itself
        if (el.object3D) out.push(el);
      }
      return out;
    },

    // Nearest manipulable the ray hits → { el, point, dist } or null.
    _pick: function (origin, dir) {
      var best = null;
      this._ray.set(origin, dir);
      var els = this._manipulables();
      for (var i = 0; i < els.length; i++) {
        var hits = this._ray.intersectObject(els[i].object3D, true);
        if (hits.length && (!best || hits[0].distance < best.dist)) {
          best = { el: els[i], point: hits[0].point.clone(), dist: hits[0].distance };
        }
      }
      return best;
    },

    // The corner handle of the current HUD nearest the ray → the handle mesh, or null. Uses ray-to-POINT
    // distance with `HANDLE_PICK` slop rather than an exact sphere intersection: a 2 cm sphere at arm's
    // length is a punishing target, and a miss fell through to a BODY grab — so resize felt unreachable,
    // while the occasional lucky catch produced a wild resize. Aiming NEAR a corner is now enough.
    _pickHandle: function (origin, dir, el) {
      if (!this._hud.el || this._hud.el !== el || !this._hud.handles.length) return null;
      var THREE = AFRAME.THREE;
      this._ray.set(origin, dir);
      var best = null, bestD = HANDLE_PICK, p = new THREE.Vector3();
      for (var i = 0; i < this._hud.handles.length; i++) {
        var h = this._hud.handles[i];
        h.getWorldPosition(p);
        if (p.clone().sub(origin).dot(dir) <= 0) continue;        // behind the controller — not aimed at
        var d = this._ray.ray.distanceToPoint(p);
        if (d < bestD) { bestD = d; best = h; }
      }
      return best;
    },

    // ---- HUD (oriented highlight box + corner handles, parented to the target) --------------------
    _localBox: function (obj) {
      var THREE = AFRAME.THREE, box = new THREE.Box3(), v = new THREE.Vector3();
      obj.updateWorldMatrix(true, true);
      var inv = new THREE.Matrix4().copy(obj.matrixWorld).invert();
      obj.traverse(function (n) {
        // Skip our own HUD: it's a CHILD of the target, so measuring with it attached (which _begin does
        // on a resize grab) would inflate the box by the handle spheres and mis-seat the model.
        if (!n.isMesh || !n.geometry || n.userData.grabHud) return;
        n.geometry.computeBoundingBox();
        var gb = n.geometry.boundingBox; if (!gb) return;
        var m = new THREE.Matrix4().multiplyMatrices(inv, n.matrixWorld);
        var xs = [gb.min.x, gb.max.x], ys = [gb.min.y, gb.max.y], zs = [gb.min.z, gb.max.z];
        for (var a = 0; a < 2; a++) for (var b = 0; b < 2; b++) for (var c = 0; c < 2; c++)
          box.expandByPoint(v.set(xs[a], ys[b], zs[c]).applyMatrix4(m));
      });
      return box.isEmpty() ? null : box;
    },

    _setHud: function (el) {
      if (this._hud.el === el) return;
      this._clearHud();
      if (!el || !el.object3D) return;
      var THREE = AFRAME.THREE, box = this._localBox(el.object3D);
      if (!box) return;
      var group = new THREE.Group();
      var helper = new THREE.Box3Helper(box, HILITE);
      if (helper.material) { helper.material.depthTest = false; helper.material.transparent = true; }
      group.add(helper);
      var hmat = new THREE.MeshBasicMaterial({ color: HANDLE, depthTest: false, transparent: true });
      var handles = [];
      // The HUD is a CHILD of the target, so it inherits the target's scale — and a glTF model is usually
      // normalized by a tiny scale (a Beagle sits at ~0.005). A fixed local radius would render the handles
      // at 0.005×0.02 ≈ 0.1 mm: invisible, and impossible to point at (why resize looked "missing"). Size
      // them in WORLD metres by dividing out the world scale, so they're the same real size on any object.
      var ws = new THREE.Vector3();
      el.object3D.getWorldScale(ws);
      var s = Math.max(1e-6, Math.max(ws.x, Math.max(ws.y, ws.z)));
      var r = HANDLE_R / s;
      var xs = [box.min.x, box.max.x], ys = [box.min.y, box.max.y], zs = [box.min.z, box.max.z];
      for (var a = 0; a < 2; a++) for (var b = 0; b < 2; b++) for (var c = 0; c < 2; c++) {
        var h = new THREE.Mesh(new THREE.SphereGeometry(r, 8, 8), hmat);
        h.position.set(xs[a], ys[b], zs[c]);
        h.renderOrder = 999;
        h.userData.grabHud = true;            // never measured as part of the object (see _localBox)
        group.add(h); handles.push(h);
      }
      helper.renderOrder = 999;
      el.object3D.add(group);                 // child of the target → oriented + scaled with it
      this._hud = { el: el, group: group, handles: handles };
    },

    _clearHud: function () {
      var h = this._hud;
      if (h.group) {
        if (h.group.parent) h.group.parent.remove(h.group);
        h.group.traverse(function (n) { if (n.geometry) n.geometry.dispose(); if (n.material) n.material.dispose(); });
      }
      this._hud = { el: null, group: null, handles: [] };
    },

    // ---- transform helpers -----------------------------------------------------------------------
    // Write a target-WORLD matrix onto an object as its LOCAL transform (parent-aware).
    _applyWorld: function (obj, worldMat) {
      var THREE = AFRAME.THREE, local = worldMat;
      if (obj.parent) {
        obj.parent.updateWorldMatrix(true, false);
        local = new THREE.Matrix4().copy(obj.parent.matrixWorld).invert().multiply(worldMat);
      }
      var p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
      local.decompose(p, q, s);
      obj.position.copy(p); obj.quaternion.copy(q); obj.scale.copy(s);
    },

    // ---- gesture start / update / commit ---------------------------------------------------------
    _begin: function (st, hit, origin, cq, dir) {
      var THREE = AFRAME.THREE, el = hit.el, obj = el.object3D;
      st.target = el;
      var handle = this._pickHandle(origin, dir, el);
      if (handle) {                            // grip on a corner handle → uniform resize
        st.mode = "scale";
        st.startScale = obj.scale.clone();
        obj.updateWorldMatrix(true, false);
        st.center = new THREE.Vector3().setFromMatrixPosition(obj.matrixWorld);
        st.startDist = Math.max(1e-3, origin.distanceTo(st.center));
        // A GROUNDED model must keep its BASE on the floor while resizing — scaling about the origin would
        // otherwise sink it or leave it hovering until the next capture re-grounds it. Remember where the
        // floor is (base = origin.y + boxMin.y × worldScale) so _update can re-seat it at any new size.
        st.gScale = el.dataset.placement === "grounded" && !el.dataset.onSurface;
        if (st.gScale) {
          var sbox = this._localBox(obj);
          st.boxMinY = sbox ? sbox.min.y : 0;
          st.floorY = st.center.y + st.boxMinY * obj.getWorldScale(new THREE.Vector3()).y;
        }
        return;
      }
      st.mode = "grab";
      st.surface = el.dataset.onSurface ? document.getElementById(el.dataset.onSurface) : null;
      // GROUNDED content (a placed model, meta.placement="grounded") is re-solved onto the LOCAL floor,
      // upright, on every client capture. Dragging it in 6DOF would therefore be undone by the next solve
      // — so constrain it the way it will be re-derived: slide on the horizontal plane it rests on, and
      // rotate yaw-only (twist the wrist to turn it). Free content keeps full 6DOF.
      st.grounded = !st.surface && el.dataset.placement === "grounded";
      if (st.grounded) {
        obj.updateWorldMatrix(true, false);
        var gp = new THREE.Vector3().setFromMatrixPosition(obj.matrixWorld);
        st.groundY = gp.y;                                                    // its resting height on the floor
        // getWorldQuaternion, NOT setFromRotationMatrix(matrixWorld): the latter needs a PURE (unscaled)
        // rotation matrix, and a glTF model carries its normalizing scale (~0.005) in matrixWorld. Feeding
        // it a scaled matrix collapses the read-back angle toward zero (90° reads as ~1°), so the yaw offset
        // was wrong and the model snapped to a canonical facing on grab — the "180° flip".
        var gq = obj.getWorldQuaternion(new THREE.Quaternion());
        var objYaw = new THREE.Euler().setFromQuaternion(gq, "YXZ").y;
        st.yawOff = objYaw - new THREE.Euler().setFromQuaternion(cq, "YXZ").y;   // wrist twist → object yaw
      } else if (st.surface && st.surface.object3D) {   // surface-constrained: slide on the plane
        var s3 = st.surface.object3D; s3.updateWorldMatrix(true, false);
        st.sPos = new THREE.Vector3().setFromMatrixPosition(s3.matrixWorld);
        st.sQuat = s3.getWorldQuaternion(new THREE.Quaternion());   // unscaled — see the grounded branch
        st.normal = new THREE.Vector3(0, 0, 1).applyQuaternion(st.sQuat).normalize();   // surface outward +Z
        obj.updateWorldMatrix(true, false);
        var oPos = new THREE.Vector3().setFromMatrixPosition(obj.matrixWorld);
        st.standoff = oPos.clone().sub(st.sPos).dot(st.normal);                          // keep its stand-off
        st.half = this._localBox(s3);                                                    // clamp bounds (local)
      } else {                                   // free: rigid 6DOF (object attached to the controller)
        obj.updateWorldMatrix(true, false);
        var cInv = new THREE.Matrix4().compose(origin, cq, ONE).invert();
        st.rel = new THREE.Matrix4().multiplyMatrices(cInv, obj.matrixWorld);   // controller⁻¹ · objectWorld
      }
    },

    _update: function (st, origin, cq, dir, thumbY, dt) {
      var THREE = AFRAME.THREE, obj = st.target.object3D;
      if (st.mode === "scale") {
        // Linear in HAND TRAVEL, not a distance RATIO. The ratio form (d/d0) explodes when you grab close
        // to an object — grabbing at 0.5 m and pulling back 1 m was a 3× jump, and a momentary accidental
        // catch ballooned a model ~10×. Now ~SCALE_REF metres of travel doubles it, clamped per gesture.
        var f = 1 + (origin.distanceTo(st.center) - st.startDist) / SCALE_REF;
        f = Math.min(SCALE_F_MAX, Math.max(SCALE_F_MIN, f));
        var s = st.startScale.clone().multiplyScalar(f);
        s.set(Math.min(SCALE_MAX, Math.max(SCALE_MIN, s.x)),
              Math.min(SCALE_MAX, Math.max(SCALE_MIN, s.y)),
              Math.min(SCALE_MAX, Math.max(SCALE_MIN, s.z)));
        obj.scale.copy(s);
        if (st.gScale) {   // re-seat the base on the floor for the new size, so it never sinks or hovers
          var nws = obj.getWorldScale(new THREE.Vector3());
          var targetY = st.floorY - st.boxMinY * nws.y;
          obj.position.y += targetY - obj.getWorldPosition(new THREE.Vector3()).y;
        }
        return;
      }
      if (st.grounded) {                         // slide along the floor plane it rests on; yaw-only turn
        if (dir.y > -1e-5) return;               // ray parallel/upward → it never meets the floor
        var tg = (st.groundY - origin.y) / dir.y;
        if (tg <= 0) return;
        var gp2 = origin.clone().add(dir.clone().multiplyScalar(tg));
        gp2.y = st.groundY;                      // stays ON the floor — never lifted or sunk
        var yaw = new THREE.Euler().setFromQuaternion(cq, "YXZ").y + st.yawOff;
        var gq2 = new THREE.Quaternion().setFromEuler(new THREE.Euler(0, yaw, 0, "YXZ"));  // upright
        this._applyWorld(obj, new THREE.Matrix4().compose(gp2, gq2, obj.scale));
        return;
      }
      if (st.surface) {                          // slide the object where the ray meets the surface plane
        var denom = dir.dot(st.normal);
        if (Math.abs(denom) < 1e-5) return;
        var t = st.sPos.clone().sub(origin).dot(st.normal) / denom;
        if (t <= 0) return;
        var p = origin.clone().add(dir.clone().multiplyScalar(t));
        if (st.half) {                           // clamp to the surface rectangle
          var right = new THREE.Vector3(1, 0, 0).applyQuaternion(st.sQuat);
          var up = new THREE.Vector3(0, 1, 0).applyQuaternion(st.sQuat);
          var rel = p.clone().sub(st.sPos);
          var du = Math.max(st.half.min.x, Math.min(st.half.max.x, rel.dot(right)));
          var dv = Math.max(st.half.min.y, Math.min(st.half.max.y, rel.dot(up)));
          p = st.sPos.clone().add(right.multiplyScalar(du)).add(up.multiplyScalar(dv));
        }
        p.add(st.normal.clone().multiplyScalar(st.standoff));   // keep its stand-off in front of the surface
        var world = new THREE.Matrix4().compose(p, obj.getWorldQuaternion(new THREE.Quaternion()), obj.scale);
        this._applyWorld(obj, world);
        return;
      }
      // free rigid: reel first (push/pull along the controller's forward), then re-attach
      if (thumbY) st.rel.elements[14] += thumbY * this.data.reelSpeed * (dt / 1000);
      var ctrl = new THREE.Matrix4().compose(origin, cq, ONE);
      this._applyWorld(obj, ctrl.multiply(st.rel));
    },

    _commit: function (st) {
      var THREE = AFRAME.THREE, obj = st.target && st.target.object3D;
      if (!obj) return;
      var e = new THREE.Euler().setFromQuaternion(obj.quaternion, "YXZ"), R = 180 / Math.PI;
      var body = { id: st.target.id,
        position: [obj.position.x, obj.position.y, obj.position.z],
        rotation: [e.x * R, e.y * R, e.z * R],
        scale: [obj.scale.x, obj.scale.y, obj.scale.z] };
      fetch("/manipulate", { method: "POST",
        headers: { "Content-Type": "application/json", "X-Conjure-User": urlUser() || "" },
        body: JSON.stringify(body) }).catch(function () { /* local state stands; a snapshot will reconcile */ });
    },

    _hint: function () {
      var now = (window.performance && performance.now) ? performance.now() : Date.now();
      if (now - this._hinted < 3000) return;
      this._hinted = now;
      console.warn("[grab] only the owner can move objects in this world");
    },

    tick: function (time, dt) {
      try {
        var sc = this.el.sceneEl, xr = sc.renderer && sc.renderer.xr;
        var frame = sc.frame, refSpace = xr && xr.getReferenceSpace && xr.getReferenceSpace();
        var session = xr && xr.getSession && xr.getSession();
        if (!session || !frame || !refSpace) { this._setHud(null); return; }
        this._once("xr", "XR session live — " + this._manipulables().length + " manipulable object(s) in world-root");
        var THREE = AFRAME.THREE, sources = session.inputSources || [], hover = null;
        for (var i = 0; i < sources.length; i++) {
          var src = sources[i];
          if (src.hand || !src.targetRaySpace || !src.gamepad) continue;
          this._once("ctrl", "controller seen (" + (src.handedness || i) + ") — grip to grab");
          var pp = frame.getPose(src.targetRaySpace, refSpace);
          if (!pp) continue;
          var key = src.handedness || ("c" + i);
          var o = pp.transform.position, q = pp.transform.orientation;
          var cq = new THREE.Quaternion(q.x, q.y, q.z, q.w);
          var origin = new THREE.Vector3(o.x, o.y, o.z);
          var dir = new THREE.Vector3(0, 0, -1).applyQuaternion(cq).normalize();
          var grip = src.gamepad.buttons[1];         // xr-standard mapping: button 1 = grip/squeeze
          var pressed = !!(grip && (grip.pressed || grip.value > 0.5));
          var thumbY = (src.gamepad.axes && src.gamepad.axes.length >= 4) ? src.gamepad.axes[3] : 0;
          var st = this._ctrl[key] || (this._ctrl[key] = { mode: "idle", target: null });

          if (st.mode === "idle") {
            var hit = this._pick(origin, dir);
            if (hit) { hover = hit.el; this._once("hover", "first hover: " + hit.el.id + " at " + hit.dist.toFixed(2) + " m"); }
            if (pressed) this._once("grip", "grip pressed — hit=" + (hit ? hit.el.id : "nothing"));
            if (pressed && hit) {
              if (!amOwner()) this._hint();
              else { this._begin(st, hit, origin, cq, dir); hover = st.target; glog("grab " + hit.el.id + " mode=" + st.mode); }
            }
          } else {
            hover = st.target;
            if (!pressed) { glog("release " + st.target.id + " → commit"); this._commit(st); st.mode = "idle"; st.target = null; }
            else this._update(st, origin, cq, dir, thumbY, dt);
          }
        }
        // STICKY highlight: keep it on the last object when the ray leaves it, rather than clearing. The
        // corner handles sit OUTSIDE the mesh, so aiming at one stopped hovering the object and deleted the
        // very handles you were reaching for — the box "disappearing" as the beam moved onto a corner. It
        // still switches the moment you hover a DIFFERENT object, and drops if the object goes away.
        var keep = (this._hud.el && this._hud.el.isConnected) ? this._hud.el : null;
        this._setHud(hover || keep);
      } catch (e) {
        // Never break the render loop over a manipulation — but SAY SO once. Swallowing silently makes a
        // broken module look identical to one that was never conjured.
        this._once("err", "tick error: " + (e && (e.stack || e.message) ? (e.stack || e.message) : e));
      }
    },

    remove: function () { this._clearHud(); this._ctrl = {}; }
  });
})();
