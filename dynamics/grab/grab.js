/* global AFRAME */
// grab — tier-C object manipulation (docs/specs/dynamics.md §8).
// A singleton, ambient module that lets you reposition/rotate/resize OTHER placed objects with the
// controllers. It's the first module that reads + writes scene entities beyond its own node.
//
// THREE MODES, set by config (voice/CLI, never a button — see §8b). `object` is everything described below
// and the default. `skybox` and `void` adjust things that have no entity to grab, so they grab the FLOOR
// instead and reuse the grounded-object drag:
//   • `skybox` → the floor drag decomposed in POLAR coordinates about the sky's centre: radial = scale,
//                tangential = yaw, a diagonal does both. Plus `yaw` on the stick, as in object mode.
//   • `void`   → a plain horizontal slide of #world-root, so ALL content (and avatars) moves together.
//                Plus `yaw` on the stick, pivoting about the viewer.
// Neither commits a transform: both write a DELTA that the client's per-capture frame writers compose, or it
// would be erased within about two seconds. See the `_beginFrame` block for the full reasoning.
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
  // Arithmetic floor on the grab radius in the skybox mode's polar drag (metres). NOT an engage minimum —
  // a minimum engage radius was considered and rejected (you learn the feel of a turntable faster than you
  // learn a rule about where you may touch it), so sensitivity is deliberately non-uniform: grab far out for
  // fine control, near the centre for coarse. This exists only so the arithmetic stays finite. `r_now/r_grab`
  // at r_grab ≈ 0 is Infinity, and this file already carries two hard-won notes (_nearest, _boxPick) about a
  // non-finite value entering an accumulator and pinning it there permanently. Same hazard, same guard.
  var R_EPS = 0.05;

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
              rotateSpeed: { type: "number", default: 90 },     // deg/sec at full deflection
              mode: { type: "string", default: "object" } },    // object | skybox | void — see §8b

    init: function () {
      var THREE = AFRAME.THREE; ONE = new THREE.Vector3(1, 1, 1);
      this._ray = new THREE.Raycaster();
      this._ctrl = {};                       // per-controller state: { mode, target, ... }
      this._hud = { el: null, group: null, handles: [] };
      this._hinted = 0;
      this._seen = {};                       // one-shot diagnostic latches (see _once)
      this._modeHudTxt = null;               // last text written to the mode indicator (write-gated)
      this._stickDirty = null;               // mode whose standalone stick yaw is awaiting a commit
      glog("init — owner=" + amOwner() + " mode=" + this._mode()
        + " (point at an object and squeeze GRIP to move it)");
    },

    // A mode switch arrives as a CONFIG update: /module on a singleton reuses and reconfigures its one live
    // instance, so the director's `conjure_module(module="grab", config={mode:"skybox"})` lands here rather
    // than building a second entity. Any gesture in flight is abandoned — committing it under the new mode's
    // rules would be wrong, and the pointer must go back or the next mode cannot claim it.
    update: function (old) {
      if (!old || old.mode === this.data.mode) return;
      var CP = window.ConjurePointers;
      var self = this;
      Object.keys(this._ctrl).forEach(function (key) {
        var st = self._ctrl[key];
        if (st.mode !== "idle" && CP) CP.release(key, "grab");
        self._ctrl[key] = { mode: "idle", target: null };
      });
      this._clearHud();
      this._modeHudTxt = null;
      // A pending standalone stick yaw belongs to the mode that produced it; commit it before switching
      // rather than dropping the turn the user already saw happen.
      if (this._stickDirty && window.ConjureWorldFrame) {
        window.ConjureWorldFrame.commit(this._stickDirty === "void" ? "frame" : "sky");
        this._stickDirty = null;
      }
      // Log the RAW requested value beside the resolved one. Printing only the resolved mode actively hid a
      // real failure: a switch to the invalid "sky" logged as "→ object", which reads like a switch BACK to
      // object rather than a rejected value (2026-09-01).
      glog("mode " + (old.mode || "object") + " → " + this._mode()
        + (this._badMode() ? " (REJECTED raw value " + JSON.stringify(this.data.mode) + ")" : ""));
    },

    // The active mode. An unrecognised string resolves to `object` — but see _badMode: it must not do so
    // SILENTLY, which is the whole lesson of 2026-09-01.
    _mode: function () {
      var m = (this.data.mode || "object").toLowerCase();
      return (m === "skybox" || m === "void") ? m : "object";
    },

    // The raw config value if it was asked for and we could not honour it, else "".
    //
    // /module now rejects an out-of-enum value at conjure time, so this should be unreachable in practice.
    // It stays as the last line of defence because the failure it guards is the expensive kind: the caller
    // is told the mode changed, says so out loud, and the headset silently behaves as though nothing was
    // asked. Degrading quietly to `object` is the wrong default for a mode nobody can see.
    _badMode: function () {
      var raw = this.data.mode;
      if (!raw) return "";
      var m = String(raw).toLowerCase();
      return (m === "object" || m === "skybox" || m === "void") ? "" : String(raw);
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
    // Does the ray cross an element's cached selection box? Tested in the object's own space, so the box
    // is ORIENTED rather than axis-aligned. Shared by the _pick gate and _boxPick.
    _hitsBox: function (origin, dir, el, box) {
      var THREE = AFRAME.THREE;
      this._bpRay = this._bpRay || new THREE.Ray();
      this._bpInv = this._bpInv || new THREE.Matrix4();
      this._bpAt = this._bpAt || new THREE.Vector3();
      el.object3D.updateWorldMatrix(true, false);
      this._bpInv.copy(el.object3D.matrixWorld).invert();
      this._bpRay.set(origin, dir).applyMatrix4(this._bpInv);
      if (!this._bpRay.intersectBox(box, this._bpAt)) return null;
      return this._bpAt.clone().applyMatrix4(el.object3D.matrixWorld);
    },

    _pick: function (origin, dir) {
      var best = null;
      this._ray.set(origin, dir);
      var els = this._manipulables();
      for (var i = 0; i < els.length; i++) {
        var el = els[i], target = el.object3D;
        // Two cost guards, both added after a 348k-triangle rigged figure made this stutter on device
        // (2026-09-01). `intersectObject(…, true)` is CPU triangle work with no upper bound, and three's
        // SkinnedMesh raycast applies bone transforms PER VERTEX on top — so cost scales with the model,
        // every frame, for every armed pointer. Fine at a prop's ~5k triangles; a cliff at a figure's.
        var box = this._boxFor(el);
        // 1. Cheap gate. If the ray misses the (cached, oriented) box it cannot hit a triangle inside it,
        //    so the exact test is pure waste. This is most of the win when you are aimed at nothing.
        if (box && !this._hitsBox(origin, dir, el, box)) continue;
        // 2. A FIGURE's body is never triangle-tested. Its box IS the grab affordance — and that is the
        //    better affordance anyway: at arm's length, "did I catch her sleeve or her forearm" is not a
        //    distinction worth paying for, let alone paying for every frame. _boxPick then supplies the
        //    hit. The HUD is still tested exactly, because resize must land on an actual corner handle.
        if (el.dataset.rigged) {
          if (this._hud.el !== el || !this._hud.group) continue;
          target = this._hud.group;
        }
        var h = this._nearest(this._ray.intersectObject(target, true));
        if (h && (!best || h.distance < best.dist)) {
          best = { el: el, point: h.point.clone(), dist: h.distance, obj: h.object };
        }
      }
      return best;
    },

    // The nearest hit with a USABLE distance. Two reasons this isn't just `hits[0]`. three sorts each
    // element's intersects with (a.distance - b.distance), and that comparator returns NaN — ordering
    // NOTHING — as soon as one entry is non-finite, so hits[0] stops meaning "nearest" for the whole array.
    // And a non-finite distance must never reach an accumulator: NaN loses every `<` comparison, so once
    // it lands in `best` nothing can displace it, which pins focus to that object from any aim direction
    // and feeds NaN into every reach derived from it (st.grabDist, and the whole resize gesture with it).
    // Dropping the entry is right rather than clamping it: a hit we can't measure is a hit we can't honour.
    _nearest: function (hits) {
      var best = null;
      for (var i = 0; i < hits.length; i++) {
        var d = hits[i].distance;
        if (!isFinite(d)) continue;
        if (!best || d < best.distance) best = hits[i];
      }
      return best;
    },

    // The object's local box, cached on the element (the traverse + bounding-box work is wasted per frame).
    // Only a real box is cached, so a glTF that hasn't finished loading is retried rather than pinned null.
    // The AUTHORED bounds a figure carries on `data-bbox`, or null. Preferred over measuring the scene
    // graph, because a skinned mesh's node commonly hangs off a bone (Grace's hair is parented to `head`,
    // ten bones up the spine) while its vertices are already in skin space — so folding in matrixWorld
    // double-counts the skeleton and drew a box twice her height. The server computed the right answer at
    // import; this just uses it. Same reasoning as glb_bounds() on the Python side.
    _authoredBox: function (el) {
      var s = el.dataset.bbox;
      if (!s) return null;
      var n = s.split(",");
      if (n.length !== 6) return null;
      for (var i = 0; i < 6; i++) { n[i] = parseFloat(n[i]); if (!isFinite(n[i])) return null; }
      var THREE = AFRAME.THREE;
      return new THREE.Box3(new THREE.Vector3(n[0], n[1], n[2]), new THREE.Vector3(n[3], n[4], n[5]));
    },

    _boxFor: function (el) {
      if (el.dataset.bbox) {
        if (!el._grabLocalBox) el._grabLocalBox = this._authoredBox(el);   // fixed; never needs refresh
        if (el._grabLocalBox) return el._grabLocalBox;
      }
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
      var els = this._manipulables(), best = null;
      for (var i = 0; i < els.length; i++) {
        var el = els[i], box = this._boxFor(el);
        if (!box) continue;
        var world = this._hitsBox(origin, dir, el, box);
        if (!world) continue;
        var d = origin.distanceTo(world);
        if (!isFinite(d)) continue;        // same accumulator hazard as _pick — an image's box is FLAT, and
        //                                    a ray along its zero-depth axis takes intersectBox through
        //                                    0 × Infinity. Never let that reach `best`.
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
        // computeBoundingBox() re-walks EVERY vertex each call and does not check for a cached result,
        // so recomputing per refresh cost a 235k-vertex pass twice a second on a rigged figure. A
        // geometry's local box cannot change unless the geometry itself does — and a resized image gets
        // a NEW geometry, whose boundingBox is null and is therefore computed here on its first sight.
        if (!n.geometry.boundingBox) n.geometry.computeBoundingBox();
        var gb = n.geometry.boundingBox; if (!gb) return;
        var m = new THREE.Matrix4().multiplyMatrices(inv, n.matrixWorld);
        var xs = [gb.min.x, gb.max.x], ys = [gb.min.y, gb.max.y], zs = [gb.min.z, gb.max.z];
        for (var a = 0; a < 2; a++) for (var b = 0; b < 2; b++) for (var c = 0; c < 2; c++)
          box.expandByPoint(v.set(xs[a], ys[b], zs[c]).applyMatrix4(m));
      });
      return box.isEmpty() ? null : box;
    },

    // `handles` draws the eight corner spheres. Outside object mode they're omitted — resize is unreachable
    // there (it needs a handle to grab), so drawing them would advertise a control that does nothing. The
    // BOX is still drawn in every mode: it tells you what a grip would take, and it is also the focus region
    // (see the _boxPick note in tick).
    _setHud: function (el, withHandles) {
      withHandles = withHandles !== false;
      if (this._hud.el === el && this._hud.withHandles === withHandles) return;
      this._clearHud();
      if (!el || !el.object3D) return;
      var THREE = AFRAME.THREE, box = this._boxFor(el);   // same box focus is tested against
      if (!box) return;
      var group = new THREE.Group();
      var helper = new THREE.Box3Helper(box, HILITE);
      if (helper.material) { helper.material.depthTest = false; helper.material.transparent = true; }
      // The box is DECORATION — never a pick target. three raycasts LineSegments with a fat slop
      // (Raycaster.params.Line.threshold, default 1 unit), so the wireframe acts as a hit VOLUME several
      // times the object's own size and swallows the picks meant for the corner handles inside it. On a
      // FLAT object it's worse: an image's box has zero depth, so Box3Helper sets its scale.z to 0, its
      // world matrix is singular, and the inverse three takes to put the ray in local space is the zero
      // matrix — every hit comes back distance:NaN. NaN loses every `<`, so _pick's accumulator could never
      // replace it: focus pinned to the first flat object from any aim direction, and _isHandle never saw a
      // handle, so resize was unreachable while grip-move still worked.
      helper.raycast = function () {};
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
      if (withHandles) {
        for (var a = 0; a < 2; a++) for (var b = 0; b < 2; b++) for (var c = 0; c < 2; c++) {
          var h = new THREE.Mesh(new THREE.SphereGeometry(r, 8, 8), hmat);
          h.position.set(xs[a], ys[b], zs[c]);
          h.renderOrder = 999;
          h.userData.grabHud = true;          // never measured as part of the object (see _localBox)
          group.add(h); handles.push(h);
        }
      } else {
        hmat.dispose();                       // no handles drawn → nothing will ever reference this material
      }
      helper.renderOrder = 999;
      el.object3D.add(group);                 // child of the target → oriented + scaled with it
      this._hud = { el: el, group: group, handles: handles, withHandles: withHandles };
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
      // `st` is per-CONTROLLER and outlives a gesture, so every accumulator has to be cleared here. A
      // leftover stickYaw snapped a grounded model by the previous grab's rotation the instant you
      // re-gripped it.
      st.stickYaw = 0;
      st.gScale = false;
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

    // Stick-driven rotation of anything held with a BODY to turn — a loaded model, or a plane/primitive
    // (a free-standing image included). Grounded things get YAW only, matching how they're re-solved:
    // upright on the floor. Free ones also get PITCH and BANK, measured against the VIEWER — tip away from
    // you, roll as you see it. That's deliberate: nothing in a glTF records which way a model faces, so its
    // own axes can't define pitch or bank, while the viewer's frame is well-defined from wherever you stand.
    //
    // Surface-attached content never reaches here — `_update` returns from its own branch, so art stays
    // flush to its wall. Images WERE excluded outright, on the grounds that turning a picture edge-on is a
    // way to lose it; that's a real hazard but it's the user's to take, and it costs one stick nudge back.
    _stickRotate: function (st, p, dt) {
      var el = st.target, obj = el.object3D;
      if (!el.components) return false;
      // A billboard re-aims at each viewer every frame, so a spin would be overwritten on the next tick —
      // the stick would feel broken rather than merely inert.
      if (el.components.billboard) return false;
      // Needs a body: a model, or geometry (image plane, primitive). Excludes a dynamic module's entity —
      // it carries only its own component, with nothing of its own to turn.
      if (!el.components["gltf-model"] && !el.components.geometry) return false;
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
        this._spin(obj, new THREE.Vector3(1, 0, 0).applyQuaternion(cq2).normalize(), pitch * rate);
        moved = true;
      }
      if (bank) {                                   // about the viewer's FORWARD → rolls as you see it
        this._spin(obj, new THREE.Vector3(0, 0, -1).applyQuaternion(cq2).normalize(), bank * rate);
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

    // ---- frame gestures: skybox + void modes (docs/specs/dynamics.md §8b) -------------------------
    // Both modes grab the FLOOR rather than a scene object, which is what lets them reuse the grounded-object
    // drag above instead of needing a new pick target. For a grounded skybox the floor genuinely IS the
    // dome's lower projection, so dragging a ground point outward stretches the dome — the gesture is
    // physically honest. For a plain sky it is an implied plane, the same maths.
    //
    // Neither mode writes a transform. `_pinSky` and `_updateWorldFrame` rewrite the skybox pose and a void
    // world's #world-root parking from the derived frame on EVERY capture, so a direct write would be gone
    // within about two seconds. What we mutate is the DELTA those writers compose, via ConjureWorldFrame —
    // the same fields that get persisted, so the preview cannot disagree with the commit.

    // Where the beam meets the floor, or null. Requires pointing DOWNWARD (dir.y < 0): the same guard the
    // grounded-object branch uses, and it means aiming at the sky engages nothing.
    _floorPoint: function (origin, dir) {
      var WF = window.ConjureWorldFrame;
      if (!WF) return null;
      if (dir.y > -1e-5) return null;                     // parallel or upward → never meets the floor
      var y = WF.floorY(), t = (y - origin.y) / dir.y;
      if (!(t > 0) || !isFinite(t)) return null;
      var p = origin.clone().add(dir.clone().multiplyScalar(t));
      p.y = y;
      return p;
    },

    _beginFrame: function (st, origin, dir, mode) {
      var WF = window.ConjureWorldFrame;
      var f = this._floorPoint(origin, dir);
      if (!f) return false;
      if (mode === "void" && !WF.isVoid()) {
        this._once("novoid", "refusing void mode — this world has a captured room, where #world-root is "
          + "forced to identity (local-first) and any move is reverted at the next capture");
        return false;
      }
      st.mode = "frame"; st.fmode = mode; st.stickYaw = 0;
      if (mode === "skybox") {
        var sky = WF.sky();
        // Polar about the sky's CENTRE, projected to the floor. The centre is now pinned to the frame origin
        // (_pinSky), so it is the same physical point every visit and survives a recenter — without that this
        // measurement would drift with wherever `local-floor` happened to land.
        st.centre = [sky.centre.x, sky.centre.z];
        st.grab = [f.x, f.z];
        st.scale0 = sky.scale; st.yaw0 = sky.yaw;
        // Scale is blocked in a captured room: shrinking the sky's opaque sphere — which applyImmersion keeps
        // visible precisely to occlude passthrough — walks it into the real walls, and because it writes
        // depth that reads as a hard edge slicing across the room. The radial term simply goes inert, so the
        // same gesture still yaws rather than becoming a special case.
        st.canScale = WF.isVoid();
      } else {
        var fr = WF.frame();
        // Accumulated in place (st.yaw/st.off), not derived from a start value, because yawAboutPivot folds a
        // translation into the offset on every stick nudge — the two terms are not separable after the fact.
        st.yaw = fr.yaw; st.off = fr.offset.slice(); st.fPrev = f.clone();
      }
      return true;
    },

    _updateFrame: function (st, origin, dir, p, dt) {
      var WF = window.ConjureWorldFrame, WM = window.WorldModel;
      var f = this._floorPoint(origin, dir);
      var rate = (this.data.rotateSpeed || 90) * (dt / 1000);          // degrees this frame at full stick
      var sv = p.value("yaw");
      var stick = Math.abs(sv) < STICK_DEAD ? 0 : -sv * rate;          // push right → clockwise from above

      if (st.fmode === "skybox") {
        // Drag is measured ABSOLUTELY from the grab, not accumulated: the grabbed point tracks the hand
        // exactly, and there is no drift over a long gesture. Safe because neither the floor plane nor the
        // sky's centre moves when we change yaw or scale, so there is no feedback loop. Stick yaw is the one
        // accumulated term, since it has no absolute reference to be measured from.
        //
        // Recomputing scale from st.scale0 each frame also means a clamp inside setSky cannot accumulate:
        // drag past the limit and back, and you come back to where you were rather than to the limit.
        st.stickYaw = (st.stickYaw || 0) + stick;
        var yaw = st.yaw0 + st.stickYaw, scale = st.scale0;
        if (f) {
          var d = WM.polarDrag(st.centre, st.grab, [f.x, f.z], R_EPS);
          yaw += d.dYaw;
          if (st.canScale) scale = st.scale0 * d.scale;
        }
        WF.setSky({ yaw: yaw, scale: scale });
        return;
      }
      // Void: a rigid horizontal transform. Accumulated rather than absolute because stick yaw and drag mix
      // — a rotation about the viewer contributes its own translation term, so the two cannot be composed
      // independently from the gesture start.
      if (f) {
        st.off[0] += f.x - st.fPrev.x; st.off[1] += f.z - st.fPrev.z;
        st.fPrev.copy(f);
      }
      if (stick) {
        // Yaw about the VIEWER, so stick and drag stay independent instead of every nudge swinging you
        // through an arc you then have to drag back out. WM.yawAboutPivot resolves the pivot into the offset
        // (see there for why a stored pivot would make content drift as you walked).
        var cam = this.el.sceneEl && this.el.sceneEl.camera;
        var v = cam ? cam.getWorldPosition(new AFRAME.THREE.Vector3()) : new AFRAME.THREE.Vector3();
        var next = WM.yawAboutPivot(st.yaw, st.off, stick, [v.x, v.z]);
        st.yaw = next.yaw; st.off = next.offset;
      }
      WF.setFrame({ yaw: st.yaw, offset: st.off });
    },

    // STANDALONE stick yaw, outside any grab. In skybox/void mode the stick turns the sky or the world with
    // no grip at all — there is no object to be "holding", and needing to grip the floor first to use the
    // stick is a step with nothing behind it.
    //
    // Called ONCE PER TICK rather than per pointer: a hand-qualified binding like `right.stickX` resolves
    // globally (conjure-pointers.js — so you can hold with one hand and shape with the other's stick), which
    // means every pointer returns the SAME value and a per-pointer loop would apply it twice.
    //
    // A stick has no release event, so the commit fires when it returns to neutral — the analogue of letting
    // go of a grip, and it keeps a long slow turn to a single POST.
    _stickYaw: function (pointers, mode, dt) {
      var WF = window.ConjureWorldFrame, WM = window.WorldModel;
      if (!WF || !pointers.length) return;
      for (var k in this._ctrl) {                      // a held gesture already folds the stick in
        if (this._ctrl[k] && this._ctrl[k].mode === "frame") return;
      }
      var sv = pointers[0].value("yaw");
      var d = Math.abs(sv) < STICK_DEAD ? 0 : -sv * (this.data.rotateSpeed || 90) * (dt / 1000);
      if (d) {
        if (!amOwner()) { this._hint(); return; }
        if (mode === "skybox") {
          WF.setSky({ yaw: WF.sky().yaw + d });
        } else {
          if (!WF.isVoid()) return;
          var cam = this.el.sceneEl && this.el.sceneEl.camera;
          var v = cam ? cam.getWorldPosition(new AFRAME.THREE.Vector3()) : new AFRAME.THREE.Vector3();
          var fr = WF.frame();
          var next = WM.yawAboutPivot(fr.yaw, fr.offset, d, [v.x, v.z]);
          WF.setFrame({ yaw: next.yaw, offset: next.offset });
        }
        this._stickDirty = mode;
      } else if (this._stickDirty) {
        glog("stick yaw settled → commit " + this._stickDirty);
        WF.commit(this._stickDirty === "void" ? "frame" : "sky");
        this._stickDirty = null;
      }
    },

    _commitFrame: function (st) {
      var WF = window.ConjureWorldFrame;
      var what = st.fmode === "void" ? "frame" : "sky";
      glog("commit " + st.fmode + " " + JSON.stringify(what === "frame" ? WF.frame() : {
        yaw: +WF.sky().yaw.toFixed(1), scale: +WF.sky().scale.toFixed(4) }));
      WF.commit(what);
    },

    // ---- mode indicator --------------------------------------------------------------------------
    // Head-locked text, same pattern as the client's #coloc-hud: `overlay` so passthrough never hides it.
    // ALWAYS ON in skybox/void mode and absent entirely in object mode, so normal use gains no clutter.
    //
    // This is a safety mechanism, not decoration. Modes are set by voice, and the director sometimes reports
    // success without actually calling the tool — a failure that is silent here and expensive: you would grip
    // expecting to turn the sky and instead fling a chair across the room. The indicator APPEARING is the
    // confirmation that the tool fired, available before you touch anything.
    _modeHud: function (text) {
      if (text === this._modeHudTxt) return;              // setAttribute on a text component is not free
      this._modeHudTxt = text;
      var el = document.getElementById("grab-hud");
      if (!text) { if (el && el.parentNode) el.parentNode.removeChild(el); return; }
      if (!el) {
        var cam = document.querySelector("a-camera") || document.querySelector("[camera]");
        if (!cam) { this._modeHudTxt = null; return; }    // retry next tick rather than latch a miss
        el = document.createElement("a-entity");
        el.id = "grab-hud";
        el.setAttribute("position", "0 -0.45 -1");        // below the coloc HUD, so both can show at once
        el.setAttribute("text", { value: "", align: "center", color: "#66ccff", width: 1.2,
                                  baseline: "center" });
        el.setAttribute("overlay", "");
        cam.appendChild(el);
      }
      el.setAttribute("text", "value", text);
    },

    // What the indicator says. Reports the EFFECTIVE size in metres — the readout is the only feedback a
    // plain sky's scale has, since scaling it changes occlusion and parallax rather than apparent size.
    _modeLine: function (mode) {
      var WF = window.ConjureWorldFrame;
      if (!WF) return mode.toUpperCase() + "  — world frame unavailable";
      if (mode === "void") {
        if (!WF.isVoid()) return "VOID  — not available in a captured room";
        var fr = WF.frame();
        return "VOID        yaw " + Math.round(fr.yaw) + "°"
          + "   offset " + fr.offset[0].toFixed(2) + ", " + fr.offset[1].toFixed(2) + " m";
      }
      var sky = WF.sky();
      var line = "SKYBOX" + (sky.grounded ? " ⏚" : "") + "      yaw " + Math.round(sky.yaw) + "°";
      if (!sky.active) return line + "   — no skybox set";
      if (!WF.isVoid()) return line + "   — scale locked (room)";
      var m = function (v) { return v.toFixed(v < 10 ? 1 : 0) + " m"; };
      // RADIUS, not the dome's `height` parameter — that is where the horizon line sits, not how big the
      // world is, and reporting it as the size made a 3.95 m ceiling read as "0.2 m".
      return line + "   radius " + m(sky.radius)
        + (sky.horizon != null ? "   horizon " + m(sky.horizon) : "");
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
        var mode = this._mode(), bad = this._badMode();
        // Always on outside object mode, and gone in it — see _modeHud on why this is load-bearing. A mode we
        // were asked for but cannot honour shows too, naming the value: the indicator's job is to make the
        // headset's actual state visible, and "I was asked for something I don't understand" is part of that.
        this._modeHud(bad ? "GRAB  — unknown mode " + JSON.stringify(bad) + "; using object"
                          : (mode === "object" ? null : this._modeLine(mode)));
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
            // MODES ARE HYBRID: a hit on an object still grabs the object whatever the mode, so you keep
            // object nudging available while positioning a world; anything else engages the mode's target.
            //
            // Exact hit (mesh or handle) → what you grab. Else the selection BOX keeps focus across the gap
            // between silhouette and corner. Else a near-miss right at a corner, which only works because
            // the box kept the HUD alive long enough to get there.
            //
            // _boxPick runs in EVERY mode. It was briefly restricted to object mode, on the grounds that the
            // box was visual noise where you cannot resize — which missed that the box is not decoration, it
            // is the focus REGION. Without it you must strike the mesh triangles exactly, and a model a few
            // metres away is a small target: objects went from easy to effectively unmovable in the new
            // modes. Only _softHandle stays object-only, since it exists to reach corner handles and those
            // are not drawn here.
            var hit = this._pick(origin, dir) || this._boxPick(origin, dir)
              || (mode === "object" ? this._softHandle(origin, dir) : null);
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
            } else if (!hit && mode !== "object" && p.active("grab")) {
              // Nothing under the beam, and we are in a mode the user deliberately entered: grab the FLOOR
              // and adjust the sky or the world. Safe here in a way it would not be as default behaviour —
              // pointing at nothing is the resting state of a controller, so this only ever fires inside a
              // mode that is named on screen.
              if (!amOwner()) this._hint();
              else if (this._beginFrame(st, origin, dir, mode)) {
                st.action = "grab";
                CP.claim(p.key, "grab");
                glog("grab frame mode=" + mode);
              }
            }
          } else if (st.mode === "frame") {
            if (!p.active(st.action || "grab")) {
              glog("release frame " + st.fmode + " → commit");
              this._commitFrame(st);
              this._ctrl[p.key] = { mode: "idle", target: null };
              CP.release(p.key, "grab");
            } else {
              this._updateFrame(st, origin, dir, p, dt);
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
        // The box shows in every mode (it is what tells you a grip would take the object rather than the
        // world); the corner handles only in object mode, where resize is reachable.
        this._setHud(hover, mode === "object");
        if (mode !== "object") this._stickYaw(pointers, mode, dt);
      } catch (e) {
        // Never break the render loop over a manipulation — but SAY SO once. Swallowing silently makes a
        // broken module look identical to one that was never conjured.
        this._once("err", "tick error: " + (e && (e.stack || e.message) ? (e.stack || e.message) : e));
      }
    },

    remove: function () { this._clearHud(); this._modeHud(null); this._ctrl = {}; }
  });
})();
