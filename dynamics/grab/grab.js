/* global AFRAME */
// grab — tier-C object manipulation (docs/dynamic-content-plan.md §Tier-C: grab, docs/dynamic-module-spec.md).
// A singleton, ambient module that lets you reposition/rotate/resize OTHER placed objects with the
// controllers. It's the first module that reads + writes scene entities beyond its own node.
//
// Controls are ACTIONS, never buttons: this module asks ConjurePointers whether `grab` / `resize` / `reel`
// are engaged, and the control→action map is config (window.CONJURE_BINDINGS). With the defaults:
//   • hover (pointer visible) → an oriented highlight box + corner handles on the pointed-at object
//   • `grab` (grip) on the body → move. Free objects: full 6DOF (move + wrist-twist), `reel` (thumbstick)
//                              pushes/pulls. Surface-attached: slide on the surface plane. Grounded models:
//                              slide on the floor, yaw only.
//   • `resize` (trigger) on a corner handle → uniform resize (transform.scale), proportions preserved.
//   • sticks, while holding a MODEL: `yaw` (right stick ←→) turns it about gravity-up; a FREE model also
//     takes `pitch` (left stick ↕) and `bank` (left stick ←→), measured against the VIEWER — tip it away
//     from you, roll it as you see it. Viewer-relative because nothing in a glTF says which way a model
//     faces, so its own axes can't define pitch or bank. Images are excluded: turning a picture edge-on is
//     only a way to lose it.
//   • release                → commit.
// `resize` shares the trigger with water's `select`; they coexist because we RESERVE the pointer while the
// beam is on one of our handles, so the same control resizes there and ripples on the picture's body.
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
  var BOX_TTL_MS = 500;                        // how long a cached selection box stays valid
  var HANDLE_SOFT = 0.06;                      // aim slop (m) for a corner when the ray hits nothing
  var STICK_DEAD = 0.15;                       // stick deflection ignored (rest drift)
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
    schema: { reelSpeed: { type: "number", default: 1.5 },
              rotateSpeed: { type: "number", default: 90 } },   // deg/sec at full deflection

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

    // The object's local box, cached on the element (the traverse + bounding-box work is wasted per frame).
    // Only a real box is cached, so a glTF that hasn't finished loading is retried rather than pinned null.
    _boxFor: function (el) {
      var t = (window.performance && performance.now) ? performance.now() : Date.now();
      // Short TTL rather than a permanent cache: an image's geometry changes when it's resized or re-fitted
      // to its surface, and a glTF's box only exists once the model has loaded. Recomputing a few boxes
      // twice a second is nothing, and it can never disagree for long with the box we DRAW.
      if (!el._grabLocalBox || (t - (el._grabBoxAt || 0)) > BOX_TTL_MS) {
        var b = this._localBox(el.object3D);
        if (b) { el._grabLocalBox = b; el._grabBoxAt = t; }
      }
      return el._grabLocalBox;
    },

    // FOCUS hit-test: does the ray pass through an object's SELECTION BOX? Used when the ray misses the
    // mesh. A model's corners stand off in empty space, so between the silhouette and a corner the ray hits
    // nothing — focus dropped, the HUD was destroyed, and the handles vanished before you could reach them
    // (worse for big models and oblique angles). The visible box is the affordance, so it should be the
    // focus region: track along it to a corner and focus never breaks. Mesh/handle hits still win, so what
    // you GRAB stays exact. Tested in the object's own space, so the box is oriented, not axis-aligned.
    _boxPick: function (origin, dir) {
      var THREE = AFRAME.THREE, els = this._manipulables(), best = null;
      var ray = new THREE.Ray(origin, dir), inv = new THREE.Matrix4();
      var local = new THREE.Ray(), at = new THREE.Vector3();
      for (var i = 0; i < els.length; i++) {
        var el = els[i], box = this._boxFor(el);
        if (!box) continue;
        el.object3D.updateWorldMatrix(true, false);
        inv.copy(el.object3D.matrixWorld).invert();
        local.copy(ray).applyMatrix4(inv);
        if (!local.intersectBox(box, at)) continue;
        var world = at.clone().applyMatrix4(el.object3D.matrixWorld);
        var d = origin.distanceTo(world);
        if (!best || d < best.dist) best = { el: el, obj: null, point: world, dist: d };
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

    // Fallback used ONLY when the ray hit nothing at all: is it near a corner handle of the object we're
    // already focused on? A model's box corners float in empty space away from the body, so past the mesh
    // the only target is a ~3.5 cm sphere — about 1° at arm's length — and missing it dropped focus, which
    // deleted the very handles being aimed at. (Flat images don't suffer: their corners sit on a big
    // picture that keeps focus alive.) Because a real hit always wins, this can't steal a body grab —
    // which is what went wrong when proximity was checked FIRST.
    _softHandle: function (origin, dir) {
      var h = this._hud;
      if (!h.el || !h.handles.length) return null;
      var THREE = AFRAME.THREE, best = null, bestD = HANDLE_SOFT;
      var p = new THREE.Vector3(), bestP = null;
      this._ray.set(origin, dir);
      for (var i = 0; i < h.handles.length; i++) {
        h.handles[i].getWorldPosition(p);
        if (p.clone().sub(origin).dot(dir) <= 0) continue;      // behind the controller
        var d = this._ray.ray.distanceToPoint(p);
        if (d < bestD) { bestD = d; best = h.handles[i]; bestP = p.clone(); }
      }
      return best ? { el: h.el, obj: best, point: bestP, dist: origin.distanceTo(bestP) } : null;
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
      var THREE = AFRAME.THREE, box = this._boxFor(el);   // same box focus is tested against
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
      // …but never let a handle swamp a SMALL object (they'd grow over the body and steal body grabs).
      // Capped at a quarter of the box's shortest side — measuring only NON-DEGENERATE axes: an image is a
      // flat plane, so its box has zero depth, and taking the plain min collapsed the radius to 0 — which is
      // why images had no grabbable corners at all.
      var size = box.getSize(new THREE.Vector3());
      var dims = [size.x, size.y, size.z].filter(function (d) { return d > 1e-6; });
      var cap = dims.length ? Math.min.apply(null, dims) * 0.25 : Infinity;
      var r = Math.min(HANDLE_R / s, cap);
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
      if (st.action === "resize") {            // the caller already decided, from what the beam is on
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
        var hp0 = hit.obj.getWorldPosition(new THREE.Vector3());
        st.cornerR0 = Math.max(1e-4, hp0.distanceTo(st.center));
        // The outward direction of the corner we grabbed. Progress is measured SIGNED along this axis, so
        // dragging toward the object keeps shrinking it straight through the centre and out the far side.
        // An unsigned distance bounced: it fell to zero at the centre and then GREW again while the hand
        // carried on the same way — "smaller, then bigger again".
        st.axis = hp0.clone().sub(st.center).normalize();
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
      // Content that BELONGS to a surface must never fall through to free 6DOF just because its host isn't
      // rendered right now (not captured this session) — that's how wall art ended up floating off its wall.
      // Refuse the grab instead; it becomes movable again as soon as the surface is captured.
      if (el.dataset.onSurface && !(st.surface && st.surface.object3D)) {
        st.mode = "idle"; st.target = null;
        this._once("nohost", "refusing to move " + el.id + " — its surface "
          + el.dataset.onSurface + " isn't rendered yet");
        return;
      }
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

    // Stick-driven rotation of a held MODEL (never a flat image — turning a picture edge-on is just a way
    // to lose it). Grounded models get YAW only, matching how they're re-solved: upright on the floor.
    // Free models also get PITCH and BANK, measured against the VIEWER — tip away from you, roll as you see
    // it. That's deliberate: nothing in a glTF records which way a model faces, so its own axes can't define
    // pitch or bank, while the viewer's frame is well-defined for every model and from wherever you stand.
    _stickRotate: function (st, p, dt) {
      var el = st.target, obj = el.object3D;
      if (!el.components || !el.components["gltf-model"]) return false;   // models only
      var THREE = AFRAME.THREE, rate = (this.data.rotateSpeed || 90) * Math.PI / 180 * (dt / 1000);
      var dz = function (v) { return Math.abs(v) < STICK_DEAD ? 0 : v; };
      var yaw = dz(p.value("yaw")), moved = false;
      if (yaw) {                                    // about gravity-up: unambiguous, needs no model facing
        if (st.grounded) st.stickYaw = (st.stickYaw || 0) - yaw * rate;   // folded into its upright yaw
        else this._spin(obj, new THREE.Vector3(0, 1, 0), -yaw * rate);    // push right → clockwise from above
        moved = true;
      }
      if (st.grounded) return moved;                // grounded stays upright — yaw is the whole story
      var cam = this.el.sceneEl && this.el.sceneEl.camera;
      if (!cam) return moved;
      var cq2 = cam.getWorldQuaternion(new THREE.Quaternion());
      var pitch = dz(p.value("pitch")), bank = dz(p.value("bank"));
      if (pitch) {                                  // about the viewer's RIGHT → tips away from / toward you
        this._spin(obj, new THREE.Vector3(1, 0, 0).applyQuaternion(cq2).normalize(), -pitch * rate);
        moved = true;
      }
      if (bank) {                                   // about the viewer's FORWARD → rolls as you see it
        this._spin(obj, new THREE.Vector3(0, 0, -1).applyQuaternion(cq2).normalize(), -bank * rate);
        moved = true;
      }
      return moved;
    },

    // Rotate `obj` about its OWN centre by `angle` around a world-space axis (position untouched).
    _spin: function (obj, axis, angle) {
      var THREE = AFRAME.THREE;
      obj.updateWorldMatrix(true, false);
      var pos = new THREE.Vector3().setFromMatrixPosition(obj.matrixWorld);
      var q = obj.getWorldQuaternion(new THREE.Quaternion());
      q.premultiply(new THREE.Quaternion().setFromAxisAngle(axis, angle));
      this._applyWorld(obj, new THREE.Matrix4().compose(pos, q, obj.scale.clone()));
    },

    _update: function (st, origin, cq, dir, p, dt) {
      var THREE = AFRAME.THREE, obj = st.target.object3D;
      if (st.mode === "scale") {
        // Where the ray points now, at the reach we grabbed at — i.e. where the user has dragged the corner
        // to. Its distance from the centre vs. the corner's original distance is the size ratio, so the
        // gesture reads as "pull the corner out to grow, push it in to shrink" from any direction.
        var at = origin.clone().add(dir.clone().multiplyScalar(st.grabDist));
        var reach = at.clone().sub(st.center).dot(st.axis) - st.cornerR0;   // SIGNED along the corner axis
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
        this._stickRotate(st, p, dt);            // stick yaw folds into the upright yaw below
        if (dir.y > -1e-5) return;               // ray parallel/upward → it never meets the floor
        var tg = (st.groundY - origin.y) / dir.y;
        if (tg <= 0) return;
        var gp2 = origin.clone().add(dir.clone().multiplyScalar(tg));
        gp2.y = st.groundY;                      // stays ON the floor — never lifted or sunk
        var yaw = new THREE.Euler().setFromQuaternion(cq, "YXZ").y + st.yawOff + (st.stickYaw || 0);
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
      var reel = p.value("reel");
      if (Math.abs(reel) > STICK_DEAD) st.rel.elements[14] += reel * this.data.reelSpeed * (dt / 1000);
      var ctrl = new THREE.Matrix4().compose(origin, cq, ONE);
      this._applyWorld(obj, ctrl.multiply(st.rel));
      // Stick rotation applies AFTER the rigid follow, then the attachment is re-derived from the result —
      // otherwise the next frame's follow would put the spin straight back where it was.
      if (this._stickRotate(st, p, dt)) {
        obj.updateWorldMatrix(true, false);
        st.rel = new THREE.Matrix4()
          .copy(new THREE.Matrix4().compose(origin, cq, ONE)).invert().multiply(obj.matrixWorld);
      }
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
      // Surface-attached content is positioned host-relative, not against the walls: send the host-local
      // offset we can compute exactly here, so the server stores it verbatim and the art stays where it
      // was put (deriving it server-side from a committed position adds the same drift models had).
      var hostEl = st.target.dataset.onSurface && document.getElementById(st.target.dataset.onSurface);
      var soff = (CF && hostEl && hostEl.object3D) ? CF.surfaceOffset(hostEl.object3D, wp, wq) : null;
      var pos = conv ? conv.position : obj.position;
      var quat = conv ? conv.quaternion : obj.quaternion;
      var e = new THREE.Euler().setFromQuaternion(quat, "YXZ"), R = 180 / Math.PI;
      var body = { id: st.target.id,
        position: [pos.x, pos.y, pos.z],
        rotation: [e.x * R, e.y * R, e.z * R],
        scale: [obj.scale.x, obj.scale.y, obj.scale.z] };
      if (anchor) body.anchor = anchor;
      if (soff) body.surface_offset = soff;
      glog("commit " + st.target.id + " frame=" + (conv ? "ref" : "local")
        + " anchor=" + (anchor ? "own" : "server") + (soff ? " surf=own" : "")
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
        // Input comes from the shared reader, in ACTIONS not buttons (see client/conjure-pointers.js), so
        // the control scheme is config (window.CONJURE_BINDINGS) rather than something baked in here.
        var CP = window.ConjurePointers;
        var pointers = CP ? CP.controllers(this.el.sceneEl) : [];
        if (!pointers.length) { this._setHud(null); return; }
        this._once("xr", "XR session live — " + this._manipulables().length + " manipulable object(s) in world-root");
        var hover = null;
        for (var i = 0; i < pointers.length; i++) {
          var p = pointers[i], origin = p.origin, dir = p.dir, cq = p.quat;
          this._once("ctrl", "controller seen (" + p.key + ")");
          var st = this._ctrl[p.key] || (this._ctrl[p.key] = { mode: "idle", target: null });

          if (st.mode === "idle") {
            if (!p.availableTo("grab")) continue;      // another module owns this pointer right now
            // No visible pointer, no highlight: a selection box appearing with no beam aimed at it reads
            // as the scene reacting to nothing.
            if (!p.armed()) continue;
            // Exact hit (mesh or handle) → what you grab. Else the selection BOX keeps focus across the gap
            // between silhouette and corner. Else a near-miss right at a corner, which only works because
            // the box kept the HUD alive long enough to get there.
            var hit = this._pick(origin, dir) || this._boxPick(origin, dir) || this._softHandle(origin, dir);
            if (hit) { hover = hit.el; this._once("hover", "first hover: " + hit.el.id + " at " + hit.dist.toFixed(2) + " m"); }
            // Reserve the pointer while the beam is on one of OUR corner handles, so a `resize` bound to
            // the same control as `select` goes to us here and to the content module everywhere else.
            if (hit && this._isHandle(hit.obj, hit.el)) CP.reserve(p.key, "grab");
            // The two gestures are separate ACTIONS, chosen by what the beam is on: a corner handle resizes,
            // the body moves. Both are bound to grip today, so this reads as it always did — but pointing
            // `resize` at the trigger later is then purely a binding change, with no edit here.
            var wantResize = hit && this._isHandle(hit.obj, hit.el) && p.active("resize");
            var wantGrab = hit && !wantResize && p.active("grab");
            if (wantResize || wantGrab) {
              if (!amOwner()) this._hint();
              else {
                st.action = wantResize ? "resize" : "grab";
                this._begin(st, hit, origin, cq, dir);
                if (st.mode === "idle") continue;       // _begin refused (e.g. host surface not rendered)
                CP.claim(p.key, "grab");                // ours for the whole gesture
                hover = st.target;
                glog("grab " + hit.el.id + " mode=" + st.mode + " via " + st.action);
              }
            }
          } else {
            hover = st.target;
            // Held for as long as the action that STARTED it is held — so a gesture can never be ended by
            // a different control, and the two can be bound to different buttons.
            if (!p.active(st.action || "grab")) {
              glog("release " + st.target.id + " → commit");
              this._commit(st); st.mode = "idle"; st.target = null;
              CP.release(p.key, "grab");                // hand the pointer back
            } else {
              this._update(st, origin, cq, dir, p, dt);
            }
          }
        }
        // Focus FOLLOWS THE BEAM: leave an object and its highlight goes. This was sticky while handles
        // were picked by proximity — aiming at a corner un-hovered the object and deleted the very handles
        // you were reaching for. Handles are children of the target and picked by identity now, so the beam
        // still hits the entity when it's on a corner and focus holds where it should.
        this._setHud(hover);
      } catch (e) {
        // Never break the render loop over a manipulation — but SAY SO once. Swallowing silently makes a
        // broken module look identical to one that was never conjured.
        this._once("err", "tick error: " + (e && (e.stack || e.message) ? (e.stack || e.message) : e));
      }
    },

    remove: function () { this._clearHud(); this._ctrl = {}; }
  });
})();
