// Conjure WebXR client.
// Connects to the world server's state channel, renders the snapshot, and applies patches live by
// mapping the declarative world model onto A-Frame entities/components.
// See docs/architecture.md §3 (channels), §4 (world model), §5 (patch protocol); docs/room-model.md
// for the room/AR pieces (real surfaces, immersion, capture).
(function () {
  "use strict";

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
      schema: { width: { default: 1 }, height: { default: 1 }, color: { default: "#35e0ff" } },
      update: function () {
        var THREE = AFRAME.THREE, d = this.data, hw = d.width / 2, hh = d.height / 2;
        this.el.removeObject3D("edges");
        var pts = [-hw, -hh, 0, hw, -hh, 0, hw, hh, 0, -hw, hh, 0, -hw, -hh, 0];
        var geo = new THREE.BufferGeometry();
        geo.setAttribute("position", new THREE.Float32BufferAttribute(pts, 3));
        var mat = new THREE.LineBasicMaterial({
          color: d.color, depthTest: false, depthWrite: false, transparent: true });
        var line = new THREE.Line(geo, mat);
        line.renderOrder = 999;   // after the fills, so edges are never hidden by a surface
        this.el.setObject3D("edges", line);
      },
      remove: function () { this.el.removeObject3D("edges"); },
    });
  }

  // Show/hide a surface's FILL (plane mesh) + edges without hiding the entity — so child labels can
  // still render in AR (where the fill is hidden so passthrough shows the real room). Re-applies
  // when the mesh/edges are (re)created (object3dset), handling A-Frame's async setup.
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
        var e = this.el.getObject3D("edges"); if (e) e.visible = this.data;
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
  var roomState = { active: false, passthrough: false, defaultVisible: false, annotations: false };

  // A floating, camera-facing label on a surface: "<semantic> (<id>)" + dimensions. Toggled by
  // environment.room.annotations so you can read each surface's metadata and reference its id.
  function setSurfaceLabel(el, on) {
    var lbl = el.querySelector(".surface-label");
    if (!on) { if (lbl) el.removeChild(lbl); return; }
    var text = (el.dataset.semantic || "surface") + " (" + el.id + ")"
      + (el.dataset.ext ? "\n" + el.dataset.ext : "");
    if (lbl) { lbl.querySelector(".surface-label-text").setAttribute("text", "value", text); return; }
    // A camera-facing dark plate (so it's readable + clearly visible) with double-sided text on top
    // (double-sided so a wrong-facing billboard can't hide it).
    lbl = document.createElement("a-entity");
    lbl.setAttribute("class", "surface-label");
    lbl.setAttribute("position", "0 0 0.06");
    lbl.setAttribute("billboard", "");
    lbl.setAttribute("overlay", "");            // draw on top (fixes XR occlusion/depth-culling)
    lbl.setAttribute("geometry", { primitive: "plane", width: 1.3, height: 0.42 });
    lbl.setAttribute("material", { color: "#04141c", opacity: 0.85, transparent: true,
      side: "double", shader: "flat" });
    var t = document.createElement("a-entity");
    t.setAttribute("class", "surface-label-text");
    t.setAttribute("position", "0 0 0.01");
    t.setAttribute("overlay", "");
    t.setAttribute("text", { value: text, align: "center", color: "#bff3ff", width: 1.2,
      wrapCount: 20, baseline: "center", side: "double" });
    lbl.appendChild(t);
    el.appendChild(lbl);
  }

  function applyRealVisibility(el) {
    // The FILL (plane + edges) shows if the room is active AND (explicit material.visible, else the
    // global default). The ENTITY stays visible whenever the room is active so its annotation label
    // (a child) can render even in AR where the fill is hidden; only unbounded-VR hides it entirely.
    var explicit = el.dataset.matVisible;
    var fill = roomState.active && (explicit != null ? explicit === "true" : roomState.defaultVisible);
    el.setAttribute("visible", roomState.active);
    el.setAttribute("fill-visible", fill);
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
  function applySurface(el, comps) {
    var s = comps.surface || {};
    var ext = s.extent || [1, 1];
    var w = (+ext[0] || 1), h = (+ext[1] || 1);
    el.dataset.ext = w.toFixed(1) + " x " + h.toFixed(1) + " m";   // for the annotation label
    el.setAttribute("geometry", { primitive: "plane", width: w, height: h });
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
      applySurface(el, comps);
      applyRealVisibility(el);
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
    }
    applyImmersion();
  }

  function applySnapshot(world) {
    root().innerHTML = "";
    (world.entities || []).forEach(applyEntity);
    applyEnv(world.environment);   // after entities, so immersion can toggle them
    console.log("[conjure] snapshot rev", world.rev, "(" + (world.entities || []).length + " entities)");
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
      el.setAttribute("geometry", "width", sw);
      el.setAttribute("geometry", "height", sh);
      el.setAttribute("surface-edges", { width: sw, height: sh });
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
        this.ids = new WeakMap();   // stable id per XRPlane object (persists across frames)
        this.n = 0;
        this.lastPost = 0;
        this._resetSpace = null;
        var self = this;
        // A recenter (Meta button) / put-down fires a 'reset' on the reference space — re-capture
        // immediately so the room snaps back into alignment with the new tracking origin.
        this._onReset = function () { self.lastPost = 0; };
      },
      // Force an immediate re-capture (manual realign — see the /room/realign signal below).
      recapture: function () { this.lastPost = 0; },
      _euler: function (q) {
        // A captured plane lies in its local X-Z plane (normal +Y); our <a-plane> is X-Y (normal
        // +Z). Compose a -90° X rotation so the rendered plane aligns with the captured one, then
        // convert to A-Frame's euler degrees (XYZ order).
        var THREE = AFRAME.THREE;
        var quat = new THREE.Quaternion(q.x, q.y, q.z, q.w);
        quat.multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2));
        var e = new THREE.Euler().setFromQuaternion(quat);   // default XYZ, matches A-Frame
        var d = THREE.MathUtils.radToDeg;
        return [d(e.x), d(e.y), d(e.z)];
      },
      tick: function (time) {
        var sceneEl = this.el.sceneEl, frame = sceneEl.frame;
        if (!frame || !frame.detectedPlanes) return;       // not an AR session with plane detection
        if (time - this.lastPost < 2000) return;            // throttle to ~0.5 Hz
        var refSpace = sceneEl.renderer.xr.getReferenceSpace();
        if (!refSpace) return;
        if (refSpace !== this._resetSpace) {                // (re)subscribe to recenter events
          this._resetSpace = refSpace;
          if (refSpace.addEventListener) refSpace.addEventListener("reset", this._onReset);
        }
        var ids = this.ids, self = this, surfaces = [], floor = null;
        frame.detectedPlanes.forEach(function (plane) {
          var pose;
          try { pose = frame.getPose(plane.planeSpace, refSpace); } catch (e) { return; }
          if (!pose) return;
          var label = plane.semanticLabel || (plane.orientation === "horizontal" ? "floor" : "wall");
          if (!ids.has(plane)) ids.set(plane, "real_" + label + "_" + (self.n++));
          var poly = plane.polygon || [];
          var minx = 1e9, maxx = -1e9, minz = 1e9, maxz = -1e9;
          poly.forEach(function (pt) {
            minx = Math.min(minx, pt.x); maxx = Math.max(maxx, pt.x);
            minz = Math.min(minz, pt.z); maxz = Math.max(maxz, pt.z);
          });
          var w = poly.length ? (maxx - minx) : 1, h = poly.length ? (maxz - minz) : 1;
          var p = pose.transform.position, o = pose.transform.orientation;
          var miny = 1e9, maxy = -1e9;   // planes should be flat (y≈0); capture range as a sanity check
          poly.forEach(function (pt) { miny = Math.min(miny, pt.y); maxy = Math.max(maxy, pt.y); });
          var s = { id: ids.get(plane), semantic: label, position: [p.x, p.y, p.z],
                    rotation: self._euler(o), extent: [w, h],
                    // raw, untransformed plane data — for diagnosing the pose→entity mapping
                    debug: { pos: [p.x, p.y, p.z], quat: [o.x, o.y, o.z, o.w],
                             orient: plane.orientation || null, label: plane.semanticLabel || null,
                             polyY: [miny, maxy], n: poly.length } };
          surfaces.push(s);
          if (label === "floor" && (!floor || w * h > floor._area)) {
            floor = { floorPolygon: poly.map(function (pt) { return [pt.x, pt.z]; }), height: 2.6, _area: w * h };
          }
        });
        if (!surfaces.length) return;
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

  window.addEventListener("load", function () { connect(); setupARButton(); });
})();
