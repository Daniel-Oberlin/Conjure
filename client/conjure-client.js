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

  // ----------------------------------------------------------------- immersion / room state
  // Two axes (docs/room-model.md §5): passthrough (real room visible) × surface visibility.
  var roomState = { active: false, passthrough: false, defaultVisible: false };

  function applyRealVisibility(el) {
    // A real surface shows if the room is active AND (its explicit material.visible, else the global
    // default). When passthrough is off but room inactive, real surfaces stay hidden.
    var explicit = el.dataset.matVisible;
    var vis = roomState.active && (explicit != null ? explicit === "true" : roomState.defaultVisible);
    el.setAttribute("visible", vis);
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
    document.querySelectorAll("[data-real]").forEach(applyRealVisibility);
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
    el.setAttribute("geometry", { primitive: "plane", width: (+ext[0] || 1), height: (+ext[1] || 1) });
    var mat = Object.assign({ shader: "flat", side: "double" }, comps.material || {});
    if ("visible" in mat) { el.dataset.matVisible = String(mat.visible); delete mat.visible; }
    el.setAttribute("material", mat);
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
      el.setAttribute("geometry", "width", (+value[0] || 1));
      el.setAttribute("geometry", "height", (+value[1] || 1));
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
      },
      _euler: function (q) {
        var THREE = AFRAME.THREE, e = new THREE.Euler().setFromQuaternion(
          new THREE.Quaternion(q.x, q.y, q.z, q.w), "YXZ");
        var d = THREE.MathUtils.radToDeg;
        return [d(e.x), d(e.y), d(e.z)];
      },
      tick: function (time) {
        var sceneEl = this.el.sceneEl, frame = sceneEl.frame;
        if (!frame || !frame.detectedPlanes) return;       // not an AR session with plane detection
        if (time - this.lastPost < 2000) return;            // throttle to ~0.5 Hz
        var refSpace = sceneEl.renderer.xr.getReferenceSpace();
        if (!refSpace) return;
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
          var p = pose.transform.position;
          var s = { id: ids.get(plane), semantic: label, position: [p.x, p.y, p.z],
                    rotation: self._euler(pose.transform.orientation), extent: [w, h] };
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

  window.addEventListener("load", connect);
})();
