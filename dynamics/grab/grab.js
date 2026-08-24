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

  var HILITE = 0x66ccff, HANDLE = 0xffcc33, HANDLE_R = 0.035;  // handle sphere radius, WORLD metres (_setHud
  //                                                              divides out the target's scale). Hitting a
  //                                                              corner is an EXACT intersection (see
  //                                                              _isHandle), so the size is the target.
  // Bounds are RELATIVE to the size the object started at — never absolute. A glTF model is normalized to
  // fit a target size, so its transform.scale is whatever that took (a Beagle sits at ~0.0049). An absolute
  // floor of 0.05 was 10× ABOVE that, so resize snapped the model 10× bigger the instant it engaged,
  // before the hand moved at all.
  var SCALE_REL_MIN = 0.02, SCALE_REL_MAX = 50.0;   // total size range vs. the object's original scale
  var SCALE_DEAD = 0.02;                       // corner drag (m) ignored before a resize starts
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

    // Nearest manipulable the ray hits → { el, point, dist, obj } or null. `obj` is the exact child that was
    // hit — the model's mesh, or one of our HUD corner handles (they're children of the target), which is
    // how _begin tells a RESIZE from a body grab.
    _pick: function (origin, dir) {
      var best = null;
      this._ray.set(origin, dir);
      var els = this._manipulables();
      for (var i = 0; i < els.length; i++) {
        var hits = this._ray.intersectObject(els[i].object3D, true);
        if (hits.length && (!best || hits[0].distance < best.dist)) {
          best = { el: els[i], point: hits[0].point.clone(), dist: hits[0].distance, obj: hits[0].object };
        }
      }
      return best;
    },

    // Is `obj` (the exact thing the ray hit) one of the current HUD's corner handles? Identity, not
    // proximity: an earlier "nearest handle within 6 cm of the ray" test stole ordinary BODY grabs, because
    // a small model's box corners sit well inside that slop when you aim at its middle — every grab became
    // a resize. Handles are children of the target, so a hit on one is unambiguous; hittability comes from
    // their SIZE (HANDLE_R, in world metres) instead.
    _isHandle: function (obj, el) {
      return !!(obj && obj.userData && obj.userData.grabHud && this._hud.el === el);
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
      // …but never let a handle swamp a SMALL object: capped at a quarter of the box's shortest side, so
      // the corners can't grow over the body and start stealing body grabs again.
      var size = box.getSize(new THREE.Vector3());
      var r = Math.min(HANDLE_R / s, Math.min(size.x, Math.min(size.y, size.z)) * 0.25);
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
      if (this._isHandle(hit.obj, el)) {       // gripped a corner handle itself → uniform resize
        st.mode = "scale";
        st.startScale = obj.scale.clone();
        // The size it was FIRST seen at, remembered on the element — the band the relative clamp works in,
        // so repeated gestures can't compound their way to absurd sizes.
        if (!el._grabOrigScale) el._grabOrigScale = obj.scale.x || 1;
        st.origScale = el._grabOrigScale;
        obj.updateWorldMatrix(true, false);
        st.center = new THREE.Vector3().setFromMatrixPosition(obj.matrixWorld);
        // Resize follows the CORNER, not the controller's distance to the object. Dragging a corner outward
        // is mostly a LATERAL hand movement, which barely changes controller→centre distance — so a radial
        // measure sat inside the dead zone and nothing happened (the log showed mode=scale engaging with the
        // scale never moving). Instead hold the reach along the ray fixed and track where it now points:
        // how far that lands from the centre, versus where the corner started, IS the size ratio.
        st.grabDist = Math.max(1e-3, hit.dist);                        // reach along the ray, held constant
        st.cornerR0 = Math.max(1e-4,
          hit.obj.getWorldPosition(new THREE.Vector3()).distanceTo(st.center));
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
        // Where the ray points now, at the reach we grabbed at — i.e. where the user has dragged the corner
        // to. Its distance from the centre vs. the corner's original distance is the size ratio, so the
        // gesture reads as "pull the corner out to grow, push it in to shrink" from any direction.
        var at = origin.clone().add(dir.clone().multiplyScalar(st.grabDist));
        var reach = at.distanceTo(st.center) - st.cornerR0;
        // Dead zone: ignore the opening centimetres so closing your hand on a corner (or ordinary tremor)
        // doesn't resize on contact — only a deliberate drag does.
        reach = reach > SCALE_DEAD ? reach - SCALE_DEAD
              : (reach < -SCALE_DEAD ? reach + SCALE_DEAD : 0);
        var f = Math.min(SCALE_F_MAX, Math.max(SCALE_F_MIN, 1 + reach / st.cornerR0));
        // Total size stays within a band around the object's ORIGINAL scale (st.origScale), so repeated
        // gestures can't run away — and f=1 leaves the object at exactly the size you grabbed it at.
        var total = (st.origScale > 0) ? (st.startScale.x * f) / st.origScale : 1;
        if (total < SCALE_REL_MIN) f *= SCALE_REL_MIN / total;
        else if (total > SCALE_REL_MAX) f *= SCALE_REL_MAX / total;
        obj.scale.copy(st.startScale.clone().multiplyScalar(f));
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
      // FRAME: what we just dragged is in the LOCAL render frame (F_track). The server persists F_ref and
      // re-solves content from it every capture, so committing the raw local pose makes the object jump to
      // wherever that solve lands on release. Convert with ConjureFrames.toRef (the inverse of the client's
      // solve). With no room basis (void/outdoor world) the frames coincide → commit the local pose as-is.
      var wp = obj.getWorldPosition(new THREE.Vector3());
      var wq = obj.getWorldQuaternion(new THREE.Quaternion());
      var mode = st.target.dataset.placement === "grounded" ? "grounded" : "free";
      var CF = window.ConjureFrames;
      // Send the anchor we authored against OUR OWN walls, for the server to store verbatim. It's the same
      // anchor we'll re-solve against the same walls, so the object stays exactly where it was dropped —
      // letting the server re-author from a position instead costs two extra author/solve hops between
      // non-rigidly-related plane sets, which is the slight settle-after-release.
      var anchor = CF && CF.anchorFor(wp, wq, mode);
      var conv = CF && CF.toRef(wp, wq, mode);
      var pos = conv ? conv.position : obj.position;
      var quat = conv ? conv.quaternion : obj.quaternion;
      var e = new THREE.Euler().setFromQuaternion(quat, "YXZ"), R = 180 / Math.PI;
      var body = { id: st.target.id,
        position: [pos.x, pos.y, pos.z],
        rotation: [e.x * R, e.y * R, e.z * R],
        scale: [obj.scale.x, obj.scale.y, obj.scale.z] };
      if (anchor) body.anchor = anchor;
      glog("commit " + st.target.id + " frame=" + (conv ? "ref" : "local")
        + " anchor=" + (anchor ? "own" : "server")
        + " pos=" + pos.x.toFixed(2) + "," + pos.y.toFixed(2) + "," + pos.z.toFixed(2)
        + " scale=" + obj.scale.x.toFixed(4));
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
