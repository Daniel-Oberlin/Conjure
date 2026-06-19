// Conjure WebXR client.
// Connects to the world server's state channel, renders the snapshot, and applies patches live by
// mapping the declarative world model onto A-Frame entities/components.
// See docs/architecture.md §3 (channels), §4 (world model), §5 (patch protocol); docs/room-model.md
// for the room/AR pieces (real surfaces, immersion, capture).
(function () {
  "use strict";

  // DEBUG (orientation-on-resume): mirror a diagnostic line to the console AND the server terminal
  // (POST /client_log), so headset-side logs are readable without remote browser debugging. Temporary.
  function orientLog(msg) {
    console.log("[conjure][orient] " + msg);
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: "orient", msg: msg }) }).catch(function () {});
    } catch (e) { /* never let logging break a frame */ }
  }

  // Custom component: a flat grid of lines in the entity's local X-Y plane (rotate -90 on X for a
  // floor). Used for the holodeck grid; also available to generated content.
  if (window.AFRAME && !AFRAME.components.grid) {
    AFRAME.registerComponent("grid", {
      schema: {
        width: { default: 20 },
        height: { default: 20 },
        cell: { default: 1 },
        color: { default: "#ffffff" },
      },
      init: function () {
        var THREE = AFRAME.THREE, d = this.data, pts = [], hw = d.width / 2, hh = d.height / 2, i;
        for (i = -hw; i <= hw + 1e-6; i += d.cell) pts.push(i, -hh, 0, i, hh, 0);
        for (i = -hh; i <= hh + 1e-6; i += d.cell) pts.push(-hw, i, 0, hw, i, 0);
        var geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
        this.el.setObject3D("grid", new THREE.LineSegments(geo, new THREE.LineBasicMaterial({ color: d.color })));
      },
      remove: function () { this.el.removeObject3D("grid"); },
    });
  }

  // Outline for a room surface: a bright line loop around the plane's border. Drawn as an
  // always-on-top overlay (depthTest off, high renderOrder) so EVERY surface's edges show —
  // otherwise a surface facing away from you occludes its own outline (you'd see only the floor's).
  // The result is a full room wireframe with crisp corners and ceiling joins.
  if (window.AFRAME && !AFRAME.components["surface-edges"]) {
    AFRAME.registerComponent("surface-edges", {
      schema: { width: { default: 1 }, height: { default: 1 }, color: { default: "#35e0ff" },
                opacity: { default: 1 }, visible: { default: true } },
      update: function () {
        var THREE = AFRAME.THREE, d = this.data, hw = d.width / 2, hh = d.height / 2;
        this.el.removeObject3D("edges");
        var pts = [-hw, -hh, 0, hw, -hh, 0, hw, hh, 0, -hw, hh, 0, -hw, -hh, 0];
        var geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
        var mat = new THREE.LineBasicMaterial({
          color: d.color, opacity: d.opacity, depthTest: false, depthWrite: false, transparent: true });
        var line = new THREE.Line(geo, mat);
        line.renderOrder = 999;   // after the fills, so edges are never hidden by a surface
        line.visible = d.visible;
        this.el.setObject3D("edges", line);
      },
      remove: function () { this.el.removeObject3D("edges"); },
    });
  }

  // Custom geometry: a wall rectangle with rectangular openings cut out of it. The openings come from
  // room-snap (snapInsets) as {x, y, w, h} in the wall's local X-Y frame — doors/windows cut through so
  // you can see into the next room / outside. They arrive as a component-string-safe list of "x y w h"
  // groups (space/comma separated — no ':' or ';' to clash with A-Frame's parser). A door reaching the
  // floor sits flush against the wall's bottom edge, which would break ShapeGeometry triangulation, so
  // each opening is kept a hair (M) inside the outline.
  if (window.AFRAME && !AFRAME.geometries["holed-wall"]) {
    AFRAME.registerGeometry("holed-wall", {
      schema: {
        width: { default: 1, min: 0 },
        height: { default: 1, min: 0 },
        holes: { default: "" },   // "x y w h, x y w h, …" in wall-local metres
      },
      init: function (data) {
        var THREE = AFRAME.THREE, hw = data.width / 2, hh = data.height / 2, M = 0.02;
        var shape = new THREE.Shape();   // outer contour, counter-clockwise
        shape.moveTo(-hw, -hh); shape.lineTo(hw, -hh); shape.lineTo(hw, hh); shape.lineTo(-hw, hh); shape.lineTo(-hw, -hh);
        var clamp = function (v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; };
        (data.holes ? data.holes.split(",") : []).forEach(function (g) {
          var p = g.trim().split(/\s+/).map(Number);
          if (p.length < 4 || p.some(function (n) { return isNaN(n); })) return;
          var ohw = p[2] / 2, ohh = p[3] / 2;
          var x0 = clamp(p[0] - ohw, -hw + M, hw - M), x1 = clamp(p[0] + ohw, -hw + M, hw - M);
          var y0 = clamp(p[1] - ohh, -hh + M, hh - M), y1 = clamp(p[1] + ohh, -hh + M, hh - M);
          if (x1 - x0 < 1e-3 || y1 - y0 < 1e-3) return;
          var path = new THREE.Path();     // hole, wound clockwise (opposite the contour)
          path.moveTo(x0, y0); path.lineTo(x0, y1); path.lineTo(x1, y1); path.lineTo(x1, y0); path.lineTo(x0, y0);
          shape.holes.push(path);
        });
        var geo = new THREE.ShapeGeometry(shape);
        // ShapeGeometry emits UVs in shape (metre) coordinates; remap to 0..1 across the wall so a
        // director's wall texture maps like it would on a plain <a-plane>.
        var uv = geo.attributes.uv;
        for (var i = 0; i < uv.count; i++) uv.setXY(i, (uv.getX(i) + hw) / data.width, (uv.getY(i) + hh) / data.height);
        uv.needsUpdate = true;
        this.geometry = geo;
      },
    });
  }

  // Show/hide a surface's FILL (plane mesh) without hiding the entity — so child labels and the edge
  // outline still render in AR (where the fill is hidden so passthrough shows the real room). The
  // edges are governed independently by `surface-edges` (room.edgesVisible). Re-applies when the mesh
  // is (re)created (object3dset), handling A-Frame's async setup.
  if (window.AFRAME && !AFRAME.components["fill-visible"]) {
    AFRAME.registerComponent("fill-visible", {
      schema: { default: true },
      init: function () {
        var self = this;
        this.el.addEventListener("object3dset", function () { self.apply(); });
      },
      update: function () { this.apply(); },
      apply: function () {
        var m = this.el.getObject3D("mesh"); if (m) m.visible = this.data;
      },
    });
  }

  // Render an entity's meshes "on top" (depthTest off, high renderOrder) so they're never occluded
  // or depth-culled — the edges use this and show in the Quest where normal-depth labels didn't.
  if (window.AFRAME && !AFRAME.components.overlay) {
    AFRAME.registerComponent("overlay", {
      init: function () { var s = this; this.el.addEventListener("object3dset", function () { s.apply(); }); this.apply(); },
      apply: function () {
        this.el.object3D.traverse(function (o) {
          if (o.material) { o.material.depthTest = false; o.material.needsUpdate = true; }
          o.renderOrder = 1000;
        });
      },
    });
  }

  // Keep an entity turned to face the camera (for readable surface labels regardless of the
  // surface's orientation). Cheap: just a lookAt per frame, only on annotation labels.
  if (window.AFRAME && !AFRAME.components.billboard) {
    AFRAME.registerComponent("billboard", {
      tick: function () {
        var cam = this.el.sceneEl && this.el.sceneEl.camera;
        if (!cam) return;
        this._t = this._t || new AFRAME.THREE.Vector3();
        cam.getWorldPosition(this._t);
        this.el.object3D.lookAt(this._t);   // A-Frame text reads correctly after lookAt (no flip)
      },
    });
  }

  // ----------------------------------------------------------------- immersion / room state
  // Two axes (docs/room-model.md §5): passthrough (real room visible) × surface visibility.
  var roomState = { active: false, passthrough: false, defaultVisible: false,
                    annotations: false, annotationDims: false,
                    edgesVisible: true, edgeColor: "#35e0ff", edgeOpacity: 1,
                    annotationColor: "#bff3ff", annotationOpacity: 1 };

  // A floating, camera-facing label on a surface: "<semantic> (<friendly id>)", with dimensions only
  // when room.annotationDims is on. Toggled by environment.room.annotations so you can read each
  // surface's name and reference its short id (e.g. "window (12)" → "make 12 blue").
  function setSurfaceLabel(el, on) {
    var lbl = el.querySelector(".surface-label");
    if (!on) { if (lbl) el.removeChild(lbl); return; }
    var text = (el.dataset.semantic || "surface") + " (" + (el.dataset.fid || el.id) + ")"
      + (roomState.annotationDims && el.dataset.ext ? "\n" + el.dataset.ext : "");
    var style = { value: text, color: roomState.annotationColor, opacity: roomState.annotationOpacity };
    if (lbl) { lbl.setAttribute("text", style); return; }
    // Just the text — camera-facing, double-sided, drawn on top (no background plate).
    lbl = document.createElement("a-entity");
    lbl.setAttribute("class", "surface-label");
    lbl.setAttribute("position", "0 0 0.06");
    lbl.setAttribute("billboard", "");
    lbl.setAttribute("overlay", "");            // draw on top (fixes XR occlusion/depth-culling)
    lbl.setAttribute("text", Object.assign(style, { align: "center", width: 1.3,
      wrapCount: 20, baseline: "center", side: "double" }));
    el.appendChild(lbl);
  }

  function applyRealVisibility(el) {
    // The FILL (plane mesh) shows if the room is active AND (explicit material.visible, else the
    // global default). The ENTITY stays visible whenever the room is active so its annotation label
    // and edge outline (children) can render even in AR where the fill is hidden; only unbounded-VR
    // hides it entirely.
    var explicit = el.dataset.matVisible;
    var fill = roomState.active && (explicit != null ? explicit === "true" : roomState.defaultVisible);
    el.setAttribute("visible", roomState.active);
    el.setAttribute("fill-visible", fill);
  }

  // The surface outline's color/alpha/visibility — global room display state, independent of the fill.
  function applyEdgeStyle(el) {
    el.setAttribute("surface-edges", { color: roomState.edgeColor,
      opacity: roomState.edgeOpacity, visible: roomState.edgesVisible });
  }

  function applyImmersion() {
    // The synthetic holodeck shell (grid floor/walls) + the void sky belong ONLY to "unbounded VR"
    // (room inactive). Whenever the room is active — AR passthrough OR a virtual room — hide them so
    // you see the room, not the grid/void competing with it. (In AR the a-sky would also occlude the
    // passthrough camera, so it must be hidden there too.)
    var inRoom = roomState.active;
    document.querySelectorAll("[data-scaffold]").forEach(function (el) {
      el.setAttribute("visible", !inRoom);
    });
    var sky = document.getElementById("sky");
    if (sky) sky.setAttribute("visible", !inRoom);
    var reals = document.querySelectorAll("[data-real]");
    reals.forEach(function (el) {
      applyRealVisibility(el);
      applyEdgeStyle(el);
      setSurfaceLabel(el, roomState.annotations);
    });
    console.log("[conjure] immersion: active=" + roomState.active + " annotations=" +
      roomState.annotations + " surfaces=" + reals.length);
  }

  // ----------------------------------------------------------------- entity / env rendering
  var root = function () { return document.getElementById("world-root"); };

  function v3(a) { return Array.isArray(a) ? a.join(" ") : a; }

  function ensureEl(id) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("a-entity");
      el.setAttribute("id", id);
      root().appendChild(el);
    }
    return el;
  }

  // A real room surface → a plane sized by its extent, styled by its material. Visibility is the
  // entity attribute (driven by the room rules), so material.visible is pulled out here.
  // Encode a wall's openings as the holed-wall geometry's component-string-safe holes list (see the
  // geometry above): "x y w h, x y w h, …". Empty string ⇒ no openings ⇒ a plain plane.
  function holesAttr(holes) {
    return (Array.isArray(holes) ? holes : []).map(function (ho) {
      return [ho.x, ho.y, ho.w, ho.h].map(function (n) { return (+n).toFixed(4); }).join(" ");
    }).join(", ");
  }

  function applySurface(el, comps) {
    var s = comps.surface || {};
    var ext = s.extent || [1, 1];
    var w = (+ext[0] || 1), h = (+ext[1] || 1);
    el.dataset.ext = w.toFixed(1) + " x " + h.toFixed(1) + " m";   // for the annotation label
    var hs = holesAttr(s.holes);
    el.dataset.holes = hs;
    if (hs) el.setAttribute("geometry", { primitive: "holed-wall", width: w, height: h, holes: hs });
    else el.setAttribute("geometry", { primitive: "plane", width: w, height: h });
    var mat = Object.assign({ shader: "flat", side: "double" }, comps.material || {});
    if ("visible" in mat) { el.dataset.matVisible = String(mat.visible); delete mat.visible; }
    el.setAttribute("material", mat);
    el.setAttribute("surface-edges", { width: w, height: h });   // outline the surface border
  }

  // Inflate (or update) one entity: transform + components map onto A-Frame.
  function applyEntity(ent) {
    var el = ensureEl(ent.id);
    var t = ent.transform || {};
    if (t.position) el.setAttribute("position", v3(t.position));
    if (t.rotation) el.setAttribute("rotation", v3(t.rotation));
    if (t.scale) el.setAttribute("scale", v3(t.scale));
    var comps = ent.components || {};
    var meta = ent.meta || {};
    if (meta.scaffold) el.dataset.scaffold = "1";
    if (meta.real) {                       // a captured real surface — special render path
      el.dataset.real = "1";
      if (meta.semantic) el.dataset.semantic = meta.semantic;
      if (meta.friendly_id != null) el.dataset.fid = meta.friendly_id;
      applySurface(el, comps);
      applyRealVisibility(el);
      applyEdgeStyle(el);
      setSurfaceLabel(el, roomState.annotations);
      return;
    }
    Object.keys(comps).forEach(function (name) { el.setAttribute(name, comps[name]); });
  }

  function applyEnv(env) {
    env = env || {};
    var sky = document.getElementById("sky");
    if (sky) {
      if (env.sky && env.sky.src) {
        // 360 equirectangular image: set the full material so the texture isn't tinted and renders
        // on the inside of the sky sphere.
        sky.setAttribute("material", { shader: "flat", side: "back", color: "#FFFFFF", src: env.sky.src });
      } else {
        var color = (env.sky && env.sky.color) || env.background;
        if (color) sky.setAttribute("material", { shader: "flat", side: "back", color: color, src: "" });
      }
    }
    if (env.fog) document.querySelector("a-scene").setAttribute("fog", env.fog);
    // room / immersion (merge — patches may carry only one field)
    if ("passthrough" in env) roomState.passthrough = !!env.passthrough;
    if (env.room) {
      if ("active" in env.room) roomState.active = !!env.room.active;
      if ("defaultSurfaceVisible" in env.room) roomState.defaultVisible = !!env.room.defaultSurfaceVisible;
      if ("annotations" in env.room) roomState.annotations = !!env.room.annotations;
      if ("annotationDims" in env.room) roomState.annotationDims = !!env.room.annotationDims;
      if ("annotationColor" in env.room) roomState.annotationColor = env.room.annotationColor;
      if ("annotationOpacity" in env.room) roomState.annotationOpacity = +env.room.annotationOpacity;
      if ("edgesVisible" in env.room) roomState.edgesVisible = !!env.room.edgesVisible;
      if ("edgeColor" in env.room) roomState.edgeColor = env.room.edgeColor;
      if ("edgeOpacity" in env.room) roomState.edgeOpacity = +env.room.edgeOpacity;
    }
    applyImmersion();
  }

  // The persisted real surfaces from the latest server snapshot. On a page (re)load the room-capture
  // component re-inits with an EMPTY reference and would otherwise establish a brand-new world frame
  // (jumping you out of the room); instead it seeds its reference from these so its first capture
  // registers INTO the persisted frame. Stays null until a snapshot carrying real surfaces arrives.
  var docSurfaces = null;

  function applySnapshot(world) {
    root().innerHTML = "";
    (world.entities || []).forEach(applyEntity);
    applyEnv(world.environment);   // after entities, so immersion can toggle them
    var reals = (world.entities || []).filter(function (e) { return e.meta && e.meta.real; });
    if (reals.length) docSurfaces = reals;     // available to seed the room frame on reload
    console.log("[conjure] snapshot rev", world.rev, "(" + (world.entities || []).length + " entities)");
    orientLog("snapshot carried " + reals.length + " real surfaces → seed "
      + (reals.length >= 3 ? "READY" : "too small (won't seed)"));  // DEBUG
  }

  // Apply a single dotted-path set from an `update` op.
  function setPath(el, path, value) {
    if (path === "components.material.visible") {       // real-surface visibility → entity attribute
      el.dataset.matVisible = String(value);
      applyRealVisibility(el);
      return;
    }
    if (path === "components.surface.extent") {         // re-capture resized a surface
      var sw = (+value[0] || 1), sh = (+value[1] || 1);
      el.setAttribute("geometry", "width", sw);         // holed-wall keeps its holes across this
      el.setAttribute("geometry", "height", sh);
      el.setAttribute("surface-edges", { width: sw, height: sh });
      return;
    }
    if (path === "components.surface.holes") {           // re-capture changed a wall's openings
      var hs = holesAttr(value);
      if (hs === (el.dataset.holes || "")) return;       // unchanged ⇒ skip the geometry rebuild
      el.dataset.holes = hs;
      var g = el.getAttribute("geometry") || {}, gw = +g.width || 1, gh = +g.height || 1;
      if (hs) el.setAttribute("geometry", { primitive: "holed-wall", width: gw, height: gh, holes: hs });
      else el.setAttribute("geometry", { primitive: "plane", width: gw, height: gh });
      return;
    }
    var parts = path.split(".");
    if (parts[0] === "transform") {
      el.setAttribute(parts[1], v3(value));
    } else if (parts[0] === "components") {
      var comp = parts[1];
      if (comp === "surface") return;                   // surface is data, not an A-Frame component
      if (parts.length === 2) el.setAttribute(comp, value);
      else el.setAttribute(comp, parts.slice(2).join("."), value);
    }
  }

  // {"fog.density": 0.1} -> {fog: {density: 0.1}} so applyEnv can consume it.
  function nest(flat) {
    var out = {};
    Object.keys(flat).forEach(function (k) {
      var ks = k.split("."), cur = out;
      for (var i = 0; i < ks.length - 1; i++) cur = cur[ks[i]] = cur[ks[i]] || {};
      cur[ks[ks.length - 1]] = flat[k];
    });
    return out;
  }

  function applyPatch(patch) {
    (patch.ops || []).forEach(function (op) {
      if (op.op === "add") {
        applyEntity(op.entity);
      } else if (op.op === "remove") {
        var el = document.getElementById(op.id);
        if (el && el.parentNode) el.parentNode.removeChild(el);
      } else if (op.op === "update") {
        var t = document.getElementById(op.id);
        if (t) Object.keys(op.set).forEach(function (p) { setPath(t, p, op.set[p]); });
      } else if (op.op === "env") {
        applyEnv(nest(op.set));
      }
    });
    console.log("[conjure] patch rev", patch.rev, "from", patch.origin);
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var ws = new WebSocket(proto + "://" + location.host + "/ws");
    ws.onopen = function () { console.log("[conjure] connected"); };
    ws.onclose = function () { console.log("[conjure] disconnected — retrying in 2s"); setTimeout(connect, 2000); };
    ws.onmessage = function (ev) {
      var msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") applySnapshot(msg.world);
      else if (msg.type === "patch") applyPatch(msg.patch);
      else if (msg.type === "recapture") {                // realign request → re-capture the room
        var sc = document.querySelector("a-scene");
        var rc = sc && sc.components && sc.components["room-capture"];
        if (rc && rc.recapture) rc.recapture();
      }
    };
  }

  // ----------------------------------------------------------------- WebXR room capture
  // ⚠ HEADSET-ONLY / NEEDS IN-HEADSET VERIFICATION (docs/room-model.md §13). Reads the Quest's
  // detected planes (+ semantic labels) in an immersive session and POSTs them to /room as this
  // headset's room model. No-ops gracefully when the features aren't available (desktop / VR-only),
  // so it never breaks the normal path. Mesh detection + anchors are later slices.
  if (window.AFRAME && !AFRAME.components["room-capture"]) {
    AFRAME.registerComponent("room-capture", {
      init: function () {
        this.clientId = "hs_" + Math.random().toString(36).slice(2, 8);
        this.lastPost = 0;
        this._resetSpace = null;
        this._anchor = null;        // a WebXR anchor — only the BOOTSTRAP frame now (see below)
        this._anchorReq = false;
        this._anchorInv = null;     // refSpace → world frame, used by the capture (= _Tmat once registered)
        // Geometry-registered world frame. The Quest's tracking origin (and any WebXR anchor) can flip
        // ~180° + several metres when you leave the room boundary and return (docs/room-model.md §8a), so
        // we don't trust it for identity. Instead we keep a REFERENCE constellation of the room's own
        // surfaces and, each capture, solve the single yaw+translation transform that aligns the newly
        // detected planes onto it (_register). That transform (_Tmat: refSpace → reference frame) IS the
        // world frame: surface ids stay put across the jump, and #world-root is parked at its inverse so
        // placed content stays locked to the real room too.
        this._ref = [];             // [{id, sem, ext:[w,h], pos:Vector3, nyaw, orient}] in the reference frame
        this._Tmat = null;          // Matrix4: refSpace → reference frame (authoritative once _haveT)
        this._haveT = false;
        this._refSeq = 0;           // counter for minting brand-new surface ids
        var self = this;
        orientLog("room-capture init — page (re)loaded, reference reset (ref=0)");  // DEBUG
        // A recenter (Meta button) / boundary re-entry fires a 'reset' on the reference space — force an
        // immediate re-capture so registration re-locks the frame within a frame instead of up to ~2 s.
        this._onReset = function () {
          orientLog("refspace RESET (recenter / boundary re-entry) → forcing re-capture");  // DEBUG
          self.lastPost = 0;
        };
      },
      // Force an immediate re-capture (manual realign — see the /room/realign signal below).
      recapture: function () { this.lastPost = 0; },
      // Park #world-root so content stored in the REFERENCE frame renders at the right real-world spot.
      // Once registration has a frame (_haveT) that frame is authoritative — world-root = _Tmat⁻¹ and
      // the capture expresses planes via _Tmat. Before the first capture we bootstrap from a WebXR
      // anchor if available, else identity (desktop / no support); the reference is then snapped from
      // that first capture, so the hand-off is seamless.
      _updateWorldFrame: function (frame, refSpace) {
        var THREE = AFRAME.THREE;
        if (this._haveT) {
          var inv = this._Tmat.clone().invert();   // reference frame → refSpace = world-root's pose
          var ip = new THREE.Vector3(), iq = new THREE.Quaternion(), is = new THREE.Vector3();
          inv.decompose(ip, iq, is);
          var wr = document.getElementById("world-root");
          if (wr) { wr.object3D.position.copy(ip); wr.object3D.quaternion.copy(iq); }
          this._anchorInv = this._Tmat;
          return;
        }
        if (!this._anchor && !this._anchorReq && frame.createAnchor && window.XRRigidTransform) {
          var self = this; this._anchorReq = true;
          try {
            frame.createAnchor(new XRRigidTransform(), refSpace).then(
              function (a) { self._anchor = a; self._anchorReq = false; console.log("[conjure] world anchor created"); },
              function () { self._anchorReq = false; });
          } catch (e) { this._anchorReq = false; }
        }
        if (!this._anchor) { this._anchorInv = null; return; }
        var pose;
        try { pose = frame.getPose(this._anchor.anchorSpace, refSpace); } catch (e) { return; }
        if (!pose) return;
        var p = pose.transform.position, o = pose.transform.orientation;
        var root = document.getElementById("world-root");
        if (root) {
          root.object3D.position.set(p.x, p.y, p.z);
          root.object3D.quaternion.set(o.x, o.y, o.z, o.w);
        }
        this._anchorInv = new THREE.Matrix4().compose(
          new THREE.Vector3(p.x, p.y, p.z), new THREE.Quaternion(o.x, o.y, o.z, o.w),
          new THREE.Vector3(1, 1, 1)).invert();
      },
      // The room-snapping geometry lives in the pure, unit-tested client/room-snap.js (RoomSnap). These
      // thin wrappers adapt it to the component's state (this._ref, this._regStat). See that file.
      _euler: function (q) { return window.RoomSnap.eulerYXZ(AFRAME.THREE, q); },
      _yawOf: function (n) { return window.RoomSnap.yawOf(n); },
      _register: function (cur) {
        var r = window.RoomSnap.register(AFRAME.THREE, cur, this._ref);
        this._regStat = r.stat;
        return r.Tmat;
      },
      tick: function (time) {
        var sceneEl = this.el.sceneEl, frame = sceneEl.frame;
        if (!frame) return;
        var refSpace = sceneEl.renderer.xr.getReferenceSpace();
        if (!refSpace) return;
        if (refSpace !== this._resetSpace) {                // (re)subscribe to recenter events
          this._resetSpace = refSpace;
          if (refSpace.addEventListener) refSpace.addEventListener("reset", this._onReset);
        }
        this._updateWorldFrame(frame, refSpace);            // EVERY frame: park #world-root on the frame
        if (!frame.detectedPlanes) return;                  // capture needs plane detection
        if (time - this.lastPost < 2000) return;            // throttle to ~0.5 Hz
        var THREE = AFRAME.THREE, self = this, UP = new THREE.Vector3(0, 1, 0);

        // Pass A — read every detected plane in the CURRENT refSpace (no world frame applied yet).
        var cur = [];
        frame.detectedPlanes.forEach(function (plane) {
          var pose;
          try { pose = frame.getPose(plane.planeSpace, refSpace); } catch (e) { return; }
          if (!pose) return;
          var rp = pose.transform.position, ro = pose.transform.orientation;
          var quat = new THREE.Quaternion(ro.x, ro.y, ro.z, ro.w);
          var nrm = UP.clone().applyQuaternion(quat);       // the plane's normal (its local +Y), in refSpace
          var poly = plane.polygon || [];
          var minx = 1e9, maxx = -1e9, minz = 1e9, maxz = -1e9, miny = 1e9, maxy = -1e9;
          poly.forEach(function (pt) {
            minx = Math.min(minx, pt.x); maxx = Math.max(maxx, pt.x);
            minz = Math.min(minz, pt.z); maxz = Math.max(maxz, pt.z);
            miny = Math.min(miny, pt.y); maxy = Math.max(maxy, pt.y);
          });
          cur.push({
            pos: new THREE.Vector3(rp.x, rp.y, rp.z), quat: quat, nrm: nrm,
            nyaw: Math.atan2(nrm.x, nrm.z),
            sem: plane.semanticLabel || (plane.orientation === "horizontal" ? "floor" : "wall"),
            orient: plane.orientation || (Math.abs(nrm.y) > 0.7 ? "horizontal" : "vertical"),
            ext: [poly.length ? (maxx - minx) : 1, poly.length ? (maxz - minz) : 1],
            poly: poly, polyY: [miny, maxy], raw: { pos: [rp.x, rp.y, rp.z], quat: [ro.x, ro.y, ro.z, ro.w] }
          });
        });
        if (!cur.length) return;

        // Seed the reference from the persisted world doc on a fresh (re)load: the server still holds the
        // room in its frame, so the first capture REGISTERS into that frame instead of establishing a new
        // one — no jump, and surfaces re-inherit their ids. (No-op after capturing starts, or when the
        // doc is empty, e.g. a just-restarted server — then we establish fresh below.)
        if (!this._ref.length && docSurfaces && docSurfaces.length >= 3) {
          var Z = new THREE.Vector3(0, 0, 1), d2r = THREE.MathUtils.degToRad, mx = 0;
          docSurfaces.forEach(function (e) {
            var t = e.transform || {}, p = t.position || [0, 0, 0], r = t.rotation || [0, 0, 0];
            var q = new THREE.Quaternion().setFromEuler(new THREE.Euler(d2r(r[0]), d2r(r[1]), d2r(r[2]), "XYZ"));
            var nrm = Z.clone().applyQuaternion(q);                       // a-plane normal is its local +Z
            var ex = (e.components && e.components.surface && e.components.surface.extent) || [1, 1];
            self._ref.push({ id: e.id, sem: (e.meta && e.meta.semantic) || "surface", ext: [ex[0], ex[1]],
              pos: new THREE.Vector3(p[0], p[1], p[2]), nyaw: Math.atan2(nrm.x, nrm.z),
              orient: Math.abs(nrm.y) > 0.7 ? "horizontal" : "vertical" });
            var mm = /_(\d+)$/.exec(e.id); if (mm) mx = Math.max(mx, +mm[1] + 1);   // keep new ids unique
          });
          self._refSeq = Math.max(self._refSeq, mx);
          console.log("[conjure] seeded room frame from " + self._ref.length + " persisted surfaces");
        }

        // Trust gate — reject captures taken mid-relocalization (boundary re-entry, recenter) so a
        // tilted / wrong-frame snapshot is never displayed, posted, OR allowed to pollute the reference
        // (a bad capture drifting _ref is what made the tilt persist until a second trip). A settled
        // floor/ceiling reads |normal.y|≈1; while it's tilted, gravity hasn't reconverged → hold the last
        // good frame and retry quickly (don't wait the full throttle) until tracking stabilizes.
        var levelA = 0, levelY = 1;
        cur.forEach(function (c) {
          if (c.orient !== "horizontal") return;
          var a = c.ext[0] * c.ext[1];
          if (a > levelA) { levelA = a; levelY = Math.abs(c.nrm.y); }
        });
        if (levelA > 0 && levelY < 0.98) {
          this._regStat = "settling ny=" + levelY.toFixed(2);
          orientLog("SETTLING ny=" + levelY.toFixed(2) + " — gravity not reconverged, holding");  // DEBUG
          this.lastPost = time - 1700; return;
        }

        // Recover the frame transform; on the first capture bootstrap the reference, otherwise require a
        // confident registration — a low-confidence result means we're not locked, so hold + retry fast.
        var reg = this._register(cur), canEstablish = this._ref.length === 0;
        orientLog("frame: " + (reg ? "REGISTERED (" + this._regStat + ")"
          : (canEstablish ? "ESTABLISH-FRESH — no seed yet (ref=0, docSurfaces="
              + (docSurfaces ? docSurfaces.length : "none") + ")"
            : "HOLD not-locked (" + this._regStat + ")"))
          + " | ref=" + this._ref.length + " haveT=" + this._haveT);  // DEBUG
        if (!reg && !canEstablish) { this.lastPost = time - 1700; return; }   // seeded/running but not locked → hold
        var registered = !!reg, Tmat;
        if (reg) { Tmat = this._Tmat = reg; this._haveT = true; }
        else { Tmat = this._anchorInv || new THREE.Matrix4(); this._Tmat = Tmat; this._haveT = true; }  // establish fresh
        this._anchorInv = Tmat;

        // Pass B — express each plane in the reference frame and assign a STABLE id by the nearest
        // reference surface of the same semantic; genuinely-new surfaces mint an id and join the reference.
        var surfaces = [], floor = null, claimed = new Set();
        cur.forEach(function (c) {
          var planeMat = new THREE.Matrix4().compose(c.pos, c.quat, new THREE.Vector3(1, 1, 1));
          var lp = new THREE.Vector3(), lq = new THREE.Quaternion(), ls = new THREE.Vector3();
          Tmat.clone().multiply(planeMat).decompose(lp, lq, ls);
          var best = null, bd = 0.5;
          self._ref.forEach(function (r) {
            if (r.sem !== c.sem || claimed.has(r)) return;
            var d = lp.distanceTo(r.pos); if (d < bd) { bd = d; best = r; }
          });
          var sid;
          if (best) {                                                     // re-inherit the existing id
            sid = best.id; claimed.add(best);
            best.pos.lerp(lp, 0.3);                                       // track slow real drift
            best.ext = c.ext.slice(); best.nyaw = self._yawOf(UP.clone().applyQuaternion(lq));
          } else {                                                        // genuinely new → mint + remember
            sid = "real_" + c.sem.replace(/\s+/g, "_") + "_" + (self._refSeq++);
            best = { id: sid, sem: c.sem, ext: c.ext.slice(), pos: lp.clone(),
                     nyaw: self._yawOf(UP.clone().applyQuaternion(lq)), orient: c.orient };
            self._ref.push(best); claimed.add(best);
          }
          surfaces.push({ id: sid, semantic: c.sem, position: [lp.x, lp.y, lp.z],
            rotation: self._euler(lq), extent: [c.ext[0], c.ext[1]], _lp: lp.clone(), _lq: lq.clone(),
            debug: { pos: c.raw.pos, quat: c.raw.quat, orient: c.orient, label: c.sem,
                     polyY: c.polyY, n: c.poly.length, registered: registered, regStat: self._regStat } });
          if (c.sem === "floor" && (!floor || c.ext[0] * c.ext[1] > floor._area)) {
            floor = { floorPolygon: c.poly.map(function (pt) { return [pt.x, pt.z]; }), height: 2.6, _area: c.ext[0] * c.ext[1] };
          }
        });
        if (!surfaces.length) return;

        // Square the walls onto one orthogonal grid, join wall corners that fall a few cm short, then
        // snap the insets (door/window/art) in front of their wall toward the room interior. All mutate
        // `surfaces` in place (orientation, position, extent, holes). Pure geometry, unit-tested in
        // client/room-snap.js.
        window.RoomSnap.squareWalls(THREE, surfaces);
        window.RoomSnap.joinCorners(THREE, surfaces);
        window.RoomSnap.snapInsets(THREE, surfaces);
        surfaces.forEach(function (s) { delete s._lp; delete s._lq; });
        this.lastPost = time;
        var boundary = null;
        if (floor) { delete floor._area; boundary = floor; }
        fetch("/room", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ client_id: this.clientId, surfaces: surfaces, boundary: boundary, replace: true }),
        }).catch(function (e) { console.warn("[conjure] room post failed", e); });
      },
    });
  }

  // A-Frame 1.4–1.6 has a bug where the "Enter AR" button doesn't render on the Quest browser
  // (aframevr/aframe#5533), so only "VR" shows. Add our own button that calls A-Frame's AR entry
  // directly — sceneEl.enterVR(true) (the `true` requests immersive-ar) — which still passes the
  // plane/mesh/depth optionalFeatures from the <a-scene webxr=...> config.
  function setupARButton() {
    if (!navigator.xr || !navigator.xr.isSessionSupported) return;
    navigator.xr.isSessionSupported("immersive-ar").then(function (supported) {
      if (!supported) return;
      var scene = document.querySelector("a-scene");
      var btn = document.createElement("button");
      btn.textContent = "ENTER AR";
      btn.style.cssText =
        "position:fixed;bottom:20px;left:20px;z-index:99999;padding:12px 22px;" +
        "font:bold 14px sans-serif;color:#fff;background:#1a8cff;border:none;" +
        "border-radius:8px;cursor:pointer;opacity:0.9;";
      btn.addEventListener("click", function () {
        if (scene && scene.enterVR) scene.enterVR(true);   // useAR = true → immersive-ar
      });
      document.body.appendChild(btn);
      // Tidy up: hide our button while in a session, restore on exit.
      scene.addEventListener("enter-vr", function () { btn.style.display = "none"; });
      scene.addEventListener("exit-vr", function () { btn.style.display = ""; });
    }).catch(function () {});
  }

  // Tick the build badge so you can see the live client actually executed (and which ?v= it loaded
  // from). If the badge shows an old time or never gets the ✓, the page/JS is stale-cached.
  function markVersion() {
    var el = document.getElementById("conjure-version");
    if (!el) return;
    var s = document.querySelector('script[src*="conjure-client.js"]');
    var m = s && /[?&]v=(\d+)/.exec(s.src);
    el.textContent = el.textContent.trim() + (m ? "  ✓ js v" + m[1] : "  ✓ js");
  }

  window.addEventListener("load", function () { connect(); setupARButton(); markVersion(); });
})();
