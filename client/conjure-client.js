// Conjure WebXR client.
// Connects to the world server's state channel, renders the snapshot, and applies patches live by
// mapping the declarative world model onto A-Frame entities/components.
// See docs/architecture.md §3 (channels), §4 (world model), §5 (patch protocol); docs/room-model.md
// for the room/AR pieces (real surfaces, immersion, capture).
(function () {
  "use strict";

  // The single default "informational" / heads-up color — used for surface edges, annotation labels,
  // the re-localizing hint, and the default for future heads-up UI. Edge/annotation colors stay
  // overridable per-world (environment.room.edgeColor / annotationColor); this is just their default.
  var INFO_COLOR = "#35e0ff";

  // Mirror a diagnostic line to the console + the server (POST /client_log → temp/conjure.log), so
  // headset-side logs are captured without remote browser debugging. Gated by a server-injected flag
  // (window.CONJURE_DEBUG_LOG ← settings.debug_log); when off, nothing is logged or sent.
  function debugLog(tag, msg, on) {
    if (on === undefined) on = window.CONJURE_DEBUG_LOG;   // callers may gate on a different flag (e.g. registration)
    if (!on) return;
    console.log("[conjure][" + tag + "] " + msg);
    try {
      fetch("/client_log", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tag: tag, msg: msg }) }).catch(function () {});
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
                    edgesVisible: true, edgeColor: INFO_COLOR, edgeOpacity: 1,
                    annotationColor: INFO_COLOR, annotationOpacity: 1,
                    skybox: false, grounded: false };

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
    if (refused || awaitingSpace) {      // blanked to passthrough: hide everything renderable
      document.querySelectorAll("[data-scaffold]").forEach(function (el) { el.setAttribute("visible", false); });
      var sky0 = document.getElementById("sky"); if (sky0) sky0.setAttribute("visible", false);
      var g0 = document.getElementById("grounded-sky"); if (g0) g0.setAttribute("visible", false);
      return;
    }
    // The synthetic holodeck shell (grid floor/walls) + the void sky belong ONLY to an EMPTY "unbounded
    // VR" (room inactive AND no chosen skybox). Hide them whenever the room is active — AR passthrough or
    // a virtual room — OR a skybox IS the environment (an outdoor/void world), so the grid never competes
    // with the room or the sky. (In AR the void a-sky would also occlude passthrough, so it's hidden too.)
    var inRoom = roomState.active;
    var showScaffold = !inRoom && !roomState.skybox && !roomState.grounded;   // holodeck only in a bare void
    document.querySelectorAll("[data-scaffold]").forEach(function (el) {
      el.setAttribute("visible", showScaffold);
    });
    // Exception: a custom skybox IMAGE *is* the chosen environment, so keep it visible even with the
    // room active — its opaque sphere deliberately wraps/occludes passthrough so you see the skybox,
    // not the physical room. Only the void color sky is restricted to unbounded VR. A GROUNDED skybox
    // replaces the plain sphere with a ground-projected dome, so when it's active hide the sphere and
    // show the grounded mesh instead (it likewise wraps the scene whenever the room is active).
    var sky = document.getElementById("sky");
    if (sky) sky.setAttribute("visible", !roomState.grounded && (roomState.skybox || !inRoom));
    var grounded = document.getElementById("grounded-sky");
    if (grounded) grounded.setAttribute("visible", roomState.grounded);
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

  // Pure world-model / presence helpers (no DOM/A-Frame) live in world-model.js so they can be strict
  // type-checked + unit-tested; alias them here. See client/world-model.js.
  var WM = window.WorldModel, nest = WM.nest, holesAttr = WM.holesAttr, v3 = WM.v3;

  function ensureEl(id) {
    var el = document.getElementById(id);
    if (!el) {
      el = document.createElement("a-entity");
      el.setAttribute("id", id);
      root().appendChild(el);
    }
    return el;
  }

  // A real surface's MESH + dimensions — the part that rebuilds (and visibly "pops") when re-applied. Split
  // from styling so the render apply-gate (applyEntity) can skip it when the surface hasn't actually moved.
  function applySurfaceGeometry(el, comps) {
    var s = comps.surface || {};
    var ext = s.extent || [1, 1];
    var w = (+ext[0] || 1), h = (+ext[1] || 1);
    el.dataset.ext = w.toFixed(1) + " x " + h.toFixed(1) + " m";   // for the annotation label
    var hs = holesAttr(s.holes);
    el.dataset.holes = hs;
    if (hs) el.setAttribute("geometry", { primitive: "holed-wall", width: w, height: h, holes: hs });
    else el.setAttribute("geometry", { primitive: "plane", width: w, height: h });
    el.setAttribute("surface-edges", { width: w, height: h });   // outline the surface border
  }

  // A real surface's MATERIAL — cheap, director-driven, never rebuilds the mesh, so it is applied every
  // time (NOT gated). Kept separate from geometry above.
  function applySurfaceMaterial(el, comps) {
    var mat = Object.assign({ shader: "flat", side: "double" }, comps.material || {});
    if ("visible" in mat) { el.dataset.matVisible = String(mat.visible); delete mat.visible; }
    el.setAttribute("material", mat);
  }

  // The room's stable planes (floor + walls) as PlaneAnchor.Plane[], for placing content via plane-relative
  // anchors (docs/local-first-geometry.md §5). Two sources, keyed by the SAME shared surface ids:
  //   refToPlanes   — the seed/reference constellation in F_ref (this._ref) — where content is authored.
  //   localToPlanes — this client's live capture in F_track (localSurfaces) — where content is rendered.
  // Solving an F_ref-authored anchor against the local planes maps content room→room without a rigid frame.
  function refToPlanes(THREE, ref) {
    var out = [];
    (ref || []).forEach(function (r) {
      if (r.sem === "floor") out.push({ id: r.id, kind: "floor", normal: new THREE.Vector3(0, 1, 0), point: r.pos.clone() });
      else if (r.sem === "wall") out.push({ id: r.id, kind: "wall",
        normal: new THREE.Vector3(Math.sin(r.nyaw), 0, Math.cos(r.nyaw)), point: r.pos.clone() });
    });
    return out;
  }
  function localToPlanes(THREE, surfs) {
    var UP = new THREE.Vector3(0, 1, 0), out = [];
    (surfs || []).forEach(function (s) {
      if (!s._lp || !s._lq) return;
      if (s.semantic === "floor") out.push({ id: s.id, kind: "floor", normal: new THREE.Vector3(0, 1, 0), point: s._lp.clone() });
      else if (s.semantic === "wall") out.push({ id: s.id, kind: "wall", normal: UP.clone().applyQuaternion(s._lq), point: s._lp.clone() });
    });
    return out;
  }
  function eulerYXZToQuat(THREE, deg) {
    var d = THREE.MathUtils.degToRad; deg = deg || [0, 0, 0];
    return new THREE.Quaternion().setFromEuler(new THREE.Euler(d(deg[0] || 0), d(deg[1] || 0), d(deg[2] || 0), "YXZ"));
  }

  // Inflate (or update) one entity: transform + components map onto A-Frame.
  function applyEntity(ent) {
    var el = ensureEl(ent.id);
    var t = ent.transform || {};
    var comps = ent.components || {};
    var meta = ent.meta || {};
    if (meta.scaffold) el.dataset.scaffold = "1";
    if (meta.real) {                       // a captured real surface — special render path
      el.dataset.real = "1";
      if (meta.semantic) el.dataset.semantic = meta.semantic;
      if (meta.friendly_id != null) el.dataset.fid = meta.friendly_id;
      // Render apply-gate (docs/local-first-geometry.md §4-6): only (re)lay the mesh + transform when the
      // surface actually moved/resized/re-holed past tolerance, so sub-tolerance re-derivation doesn't
      // rebuild the geometry (the "pop"). el._geoSig remembers the last-applied shape across patches (the
      // id is stable, so ensureEl returns the same element). Styling below is always applied — it never pops.
      var sig = WM.surfaceSig(t, comps.surface);
      if (!el._geoSig || WM.surfaceMoved(AFRAME.THREE, el._geoSig, sig, window.CONJURE_APPLY_TOL)) {
        if (t.position) el.setAttribute("position", v3(t.position));
        if (t.rotation) el.setAttribute("rotation", v3(t.rotation));
        if (t.scale) el.setAttribute("scale", v3(t.scale));
        applySurfaceGeometry(el, comps);
        el._geoSig = sig;
      }
      applySurfaceMaterial(el, comps);
      applyRealVisibility(el);
      applyEdgeStyle(el);
      setSurfaceLabel(el, roomState.annotations);
      return;
    }
    if (t.position) el.setAttribute("position", v3(t.position));
    if (t.rotation) el.setAttribute("rotation", v3(t.rotation));
    if (t.scale) el.setAttribute("scale", v3(t.scale));
    Object.keys(comps).forEach(function (name) { el.setAttribute(name, comps[name]); });
    // Director-placed content: remember its AUTHORED (F_ref) pose so the capture tick can re-place it via a
    // plane-relative anchor solved against the LOCAL walls (docs §5). In a captured room #world-root is
    // identity, so the raw F_ref pose would be wrong; _placeContent corrects it. Scaffold is excluded.
    if (!meta.scaffold && t.position) el._frefPose = { position: t.position, rotation: t.rotation || [0, 0, 0] };
  }

  function applyEnv(env) {
    env = env || {};
    var sky = document.getElementById("sky");
    var groundedSky = document.getElementById("grounded-sky");
    if (sky && (env.sky || env.background)) {
      if (env.sky && env.sky.src && env.sky.grounded) {
        // Grounded skybox: a ground-projected dome (see grounded-skybox.js) replaces the plain sphere
        // so you stand on the scene's floor. Drive the component; immersion hides the sphere for it.
        if (groundedSky) groundedSky.setAttribute("grounded-sky", {
          src: env.sky.src,
          height: env.sky.height || 1.6,
          radius: env.sky.radius || 30,
        });
        roomState.skybox = true;
        roomState.grounded = true;
      } else if (env.sky && env.sky.src) {
        // 360 equirectangular image: set the full material so the texture isn't tinted and renders
        // on the inside of the sky sphere. Mark a custom skybox so immersion keeps it visible (it
        // wraps the scene even when the room is active — see applyImmersion).
        sky.setAttribute("material", { shader: "flat", side: "back", color: "#FFFFFF", src: env.sky.src });
        if (groundedSky) groundedSky.setAttribute("grounded-sky", { src: "" });   // tear down any grounded dome
        roomState.skybox = true;
        roomState.grounded = false;
      } else {
        var color = (env.sky && env.sky.color) || env.background;
        if (color) sky.setAttribute("material", { shader: "flat", side: "back", color: color, src: "" });
        if (groundedSky) groundedSky.setAttribute("grounded-sky", { src: "" });
        roomState.skybox = false;   // back to the void color sky → only shows in unbounded VR
        roomState.grounded = false;
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
  var docSurfaces = null, lastWorldKey = null;
  // True once THIS client is rendering its own captured surfaces (an AR headset, after its first capture —
  // see _renderLocal). While active, server real-surface ops are ignored for rendering (local render is the
  // sole author of real-surface geometry, so the two never fight the apply-gate). A desktop/spectator viewer
  // never captures, so this stays false and it renders the server's shared surfaces as before.
  var localRenderActive = false;
  // Shared per-surface STYLING (material/colour/texture/visibility), keyed by surface id — the styling half
  // of the model that IS server-authored (geometry is local; styling is shared, docs §3). Populated from
  // snapshots + patches; _renderLocal applies it by id so a locally-rendered surface keeps its director/
  // persisted look instead of a default material. Reset per world switch.
  var surfaceStyles = {};
  // A VOID/outdoor world (environment.space === "<void>") isn't tied to a captured room — it shows a
  // skybox + objects, and room-capture derives its frame on the fly from live walls (canonicalFrame)
  // instead of registering against stored geometry. Set from each snapshot.
  var VOID_SPACE = "<void>", isVoidWorld = false;
  // Two-stage space selection (new-space-flow §3). On entering AR we report our coarse location and get
  // back the geo-near candidate spaces; room-capture then votes its live geometry against them
  // (RoomSnap.selectSpace) and commits the verdict via /space/select. `pendingSelect` holds the candidates
  // while that vote is in flight (null when there's nothing to decide); `lastGeo` remembers the reported
  // location so a "no match" commit can stamp/mint a space there.
  var pendingSelect = null, lastGeo = null;
  // The location fix is acquired EAGERLY on page load (warmGeo), before the user enters AR, so it's usually
  // ready by the time they do — the acquisition (often several seconds on a headset) overlaps with reading
  // the page / clicking Enter AR instead of stalling after it. geoStatus: idle → pending → ready | failed.
  // A headset's GPS fix can take many seconds (or fail transiently), so on failure we RETRY up to
  // GEO_MAX_TRIES while staying blanked to passthrough — rather than dumping the user into the (possibly
  // void) active world mid-acquisition. geoTries resets on each AR entry and on a successful fix.
  var geoStatus = "idle", geoTries = 0, GEO_MAX_TRIES = 3;
  // Admission gate + occupancy (new-space-flow steps 4/7). `clientId` identifies this page-load to the
  // server so its select commits once (GPS jitter can't re-vote). `amHolding` = we passed the co-location
  // gate and are HOLDING the active space; we tell the server (`hold` over /ws) so it counts us as
  // occupying it — and re-tell it after a ws reconnect. On refusal we hide content and stay in passthrough.
  var clientId = "c_" + Math.random().toString(36).slice(2, 8), amHolding = false, refused = false;
  // While an AR headset is deciding WHICH space it's in (from enter-vr until /space/select resolves), we
  // blank to passthrough and show a "finding your space" notice — so a headset never renders the provisional
  // booted world (or anyone else's) misaligned to the real room before it has established/joined its space.
  var awaitingSpace = false, lastWorld = null;

  function applySnapshot(world) {
    if (refused) return;                 // we're not in this space — stay blanked to passthrough (steps 4/7)
    if (awaitingSpace) {                 // a snapshot = selection resolved → un-blank BEFORE rendering, so
      awaitingSpace = false;             // applyImmersion (below) sets the world up instead of hiding it
      hideHeadsetMessage(); hideInfo();
    }
    var key = worldOwner + "/" + (world && world.name);
    if (key !== lastWorldKey) {          // WORLD SWITCH → drop the previous room's capture frame so the next
      lastWorldKey = key;                // capture seeds/establishes for THIS world, not the last one
      var sc = document.querySelector("a-scene");
      var rc = sc && sc.components && sc.components["room-capture"];
      if (rc && rc.resetFrame) rc.resetFrame();
    }
    root().innerHTML = "";
    // LOCAL-FIRST: once a client renders its own capture (localRenderActive), real surfaces are NOT drawn
    // from the server — each headset draws its OWN live capture (_renderLocal), matching its passthrough. We
    // still consume the server's real surfaces below (docSurfaces) as the registration REFERENCE. A desktop
    // viewer (never captures) keeps rendering the server's surfaces. Non-real entities always render here.
    (world.entities || []).forEach(function (e) {
      if (localRenderActive && e.meta && e.meta.real) return;
      applyEntity(e);
    });
    applyEnv(world.environment);   // after entities, so immersion can toggle them
    isVoidWorld = ((world.environment || {}).space === VOID_SPACE);
    var reals = (world.entities || []).filter(function (e) { return e.meta && e.meta.real; });
    surfaceStyles = {};                  // rebuild the shared styling map for THIS world (id → material)
    reals.forEach(function (e) { var m = (e.components || {}).material; if (m) surfaceStyles[e.id] = m; });
    // Seed material for the room frame on reload (see the capture at ~L794). CLEAR it when a snapshot
    // carries no real surfaces — otherwise switching into an empty/void world (or a DIFFERENT room)
    // would leave the PREVIOUS room's surfaces here, and the next capture could register into the wrong
    // frame (new-space-flow §3 gap #5: the Harold's-house cross-room seeding). Empty ⇒ nothing to seed.
    docSurfaces = reals.length ? reals : null;
    lastWorld = world;                   // remember for endAwaitingSpace (restore after a no-switch resolve)
    console.log("[conjure] snapshot rev", world.rev, "(" + (world.entities || []).length + " entities)"
      + (isVoidWorld ? " [outdoor/void]" : ""));
  }

  // Geometry/transform paths that describe a real surface's SHAPE — owned locally now (each client renders
  // its own capture), so a server `update` carrying these is ignored for real surfaces (styling paths still
  // apply). Non-real entities are unaffected.
  var GEO_PATHS = { "transform.position": 1, "transform.rotation": 1, "transform.scale": 1,
                    "components.surface.extent": 1, "components.surface.holes": 1 };

  // Apply a single dotted-path set from an `update` op.
  function setPath(el, path, value) {
    if (localRenderActive && el.dataset.real && GEO_PATHS[path]) return;   // real-surface geometry is local —
                                                        // don't let the server move/reshape it (docs §2)
    if (el.dataset.real && path.indexOf("components.material") === 0) {    // director restyle → keep the shared
      var st = surfaceStyles[el.id] || (surfaceStyles[el.id] = {});        // styling in sync so a local re-render
      if (path === "components.material") Object.assign(st, value || {});  // preserves it (not the default)
      else st[path.split(".").slice(2).join(".")] = value;                 // e.g. components.material.color
    }
    // Content whose F_ref pose the server moves (a director "move object", or an on-surface photo re-pinned
    // when its wall moved) must update the anchor SOURCE too, else _placeContent re-solves the stale pose.
    if (el._frefPose && path === "transform.position") el._frefPose.position = value;
    if (el._frefPose && path === "transform.rotation") el._frefPose.rotation = value;
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


  function applyPatch(patch) {
    if (refused || awaitingSpace) return;   // ignore world updates while blanked to passthrough
    (patch.ops || []).forEach(function (op) {
      if (op.op === "add") {
        if (op.entity.meta && op.entity.meta.real) {
          var m = (op.entity.components || {}).material;      // keep the shared styling even when we
          if (m) surfaceStyles[op.entity.id] = m;             // don't render it (local render applies it)
          if (localRenderActive) return;                      // real surfaces render locally
        }
        applyEntity(op.entity);
        debugLog("patch", "add " + op.entity.id + " [" +
          Object.keys((op.entity.components) || {}).join(",") + "]");
      } else if (op.op === "remove") {
        var el = document.getElementById(op.id);
        if (localRenderActive && el && el.dataset.real) return;   // local render owns real-surface presence
        if (el && el.parentNode) el.parentNode.removeChild(el);
        debugLog("patch", "remove " + op.id + " found=" + !!el);   // found=false ⇒ silent no-op
      } else if (op.op === "update") {
        var t = document.getElementById(op.id);
        if (t) Object.keys(op.set).forEach(function (p) { setPath(t, p, op.set[p]); });
        debugLog("patch", "update " + op.id + " found=" + !!t + " {" +
          Object.keys(op.set || {}).join(",") + "}");           // found=false ⇒ silent no-op
      } else if (op.op === "env") {
        applyEnv(nest(op.set));
      }
    });
    console.log("[conjure] patch rev", patch.rev, "from", patch.origin);
  }

  // The logged-in user, from the /tunnel/<user> route (which redirects with ?user=). Default otherwise.
  function currentUser() { return new URLSearchParams(location.search).get("user") || ""; }

  // A simple info banner (info color), e.g. when a guest is refused a private world (Phase 4 §3). This is
  // an HTML overlay — visible on the 2D page only; NOT inside an immersive AR/VR session (WebXR renders the
  // scene, not the DOM). For headset-visible notices use showHeadsetMessage (below) as well.
  function showInfo(text) {
    var el = document.getElementById("conjure-info");
    if (!el) {
      el = document.createElement("div");
      el.id = "conjure-info";
      el.style.cssText = "position:fixed;top:0;left:0;right:0;padding:12px;text-align:center;z-index:9999;"
        + "font:16px/1.4 sans-serif;color:" + INFO_COLOR + ";background:rgba(0,0,0,0.72);";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.style.display = "";
  }

  function hideInfo() {
    var el = document.getElementById("conjure-info");
    if (el) el.style.display = "none";
  }

  // An IN-SCENE notice, parented to the camera so it floats ~1.2 m ahead of the gaze — visible INSIDE the
  // headset (unlike the HTML banner). Used for the admission-gate refusal so the message shows in AR.
  function showHeadsetMessage(text) {
    var cam = document.querySelector("a-camera") || document.querySelector("[camera]");
    if (!cam) return;
    var el = document.getElementById("conjure-notice");
    if (!el) {
      el = document.createElement("a-entity");
      el.setAttribute("id", "conjure-notice");
      el.setAttribute("position", "0 0 -1.2");
      el.setAttribute("geometry", "primitive: plane; width: 1.1; height: 0.3");
      el.setAttribute("material", "color: #000; opacity: 0.72; shader: flat; side: double");
      var t = document.createElement("a-entity");
      t.setAttribute("id", "conjure-notice-text");
      t.setAttribute("position", "0 0 0.01");
      el.appendChild(t);
      cam.appendChild(el);
    }
    document.getElementById("conjure-notice-text").setAttribute("text",
      { value: text, align: "center", width: 1.0, wrapCount: 26, color: INFO_COLOR });
    el.setAttribute("visible", true);
  }

  function hideHeadsetMessage() {
    var el = document.getElementById("conjure-notice");
    if (el) el.setAttribute("visible", false);
  }

  // Tear the world down to BARE PASSTHROUGH — clear placed/real entities and hide the skybox (plain +
  // grounded, which live OUTSIDE #world-root) and the holodeck scaffold. Used both while awaiting space
  // selection and on an admission-gate refusal; `applyImmersion` hides everything given refused||awaitingSpace.
  function blankToPassthrough() {
    root().innerHTML = "";
    roomState.skybox = false; roomState.grounded = false; roomState.active = false;
    var gs = document.getElementById("grounded-sky");
    if (gs) gs.setAttribute("grounded-sky", { src: "" });   // tear down any grounded dome
    applyImmersion();
  }

  // Admission-gate refusal (steps 4/7): blank to passthrough and STAY there (ignore world updates) until we
  // leave AR or get admitted.
  function passthroughBlank() {
    refused = true;
    amHolding = false;
    blankToPassthrough();
  }

  // The notice shown while blanked, by phase: waiting on the location fix vs. matching/establishing the
  // space once we have it. Sets both the 2D banner and the in-headset panel.
  function setAwaitMessage(kind) {
    var m = kind === "locating"
      ? "Getting your location…\nWorking out which space you're in."
      : "Finding your space…\nAligning to your room — one moment.";
    showInfo(m);
    showHeadsetMessage(m);
  }

  // Selection resolved WITHOUT a world switch (already-selected, or geolocation unavailable/denied), so no
  // fresh snapshot is coming — un-blank and re-render the world we blanked.
  function endAwaitingSpace() {
    if (!awaitingSpace) return;
    awaitingSpace = false;
    hideHeadsetMessage(); hideInfo();
    if (lastWorld) applySnapshot(lastWorld);
  }

  // --- presence (Phase 4 §7): show the other users as a sphere-on-box avatar, co-located in the shared
  // reference frame (#world-root), and broadcast our own head/camera pose ~10 Hz. Pose is expressed in
  // #world-root's local frame so it aligns for everyone (AR: world-root is parked at the registered
  // frame; desktop: world-root is at identity, so it's just the camera pose).
  var socket = null, R_AV = 0.13, GAP_AV = 0.03, worldOwner = null, guestSpawned = false;

  // Desktop-guest spawn (Phase 4 §6): a guest viewing on a desktop browser (no AR) isn't physically in
  // the space, so drop them just to the OWNER's right the first time the owner's pose arrives, then let
  // wasd/mouse take over. Desktop only (in AR the headset places you); #world-root is identity on
  // desktop, so the owner's world-frame pose is also the scene-frame position for the rig.
  function maybeSpawnGuest(ownerPose) {
    if (guestSpawned || !ownerPose || !ownerPose.p) return;
    var me = currentUser();
    if (!me || me === worldOwner) return;                 // only a *guest* spawns relative to the owner
    var sc = document.querySelector("a-scene");
    if (sc && sc.is && sc.is("vr-mode")) return;          // AR session → the headset positions you
    var rig = document.getElementById("rig"); if (!rig) return;
    var sp = WM.spawnRight(AFRAME.THREE, ownerPose, 1.2);   // 1.2 m to the owner's right, on the floor
    rig.object3D.position.set(sp[0], sp[1], sp[2]);
    guestSpawned = true;
  }

  // The avatar entity is parked at the HEAD position and yawed to the headset's heading, so the body box
  // and head turn as one. Two info-color "eyes" sit half-embedded on the front of the head sphere, 45° apart
  // (±22.5° from the look direction); they live on a child entity that pitches with the head, so you can
  // read both the yaw (whole avatar turns) and the up/down gaze (eyes ride up/down the sphere).
  //var EYE_R = 0.045, EYE_S = Math.sin(Math.PI / 8), EYE_C = Math.cos(Math.PI / 8);   // 22.5°
  var EYE_R = 0.03, EYE_S = Math.sin(Math.PI / 12), EYE_C = Math.cos(Math.PI / 12); 
  function setAvatar(user, pose) {
    if (!pose || !pose.p) return;
    var wr = document.getElementById("world-root"); if (!wr) return;
    var THREE = AFRAME.THREE;
    var el = document.getElementById("avatar-" + user);
    if (!el) {
      el = document.createElement("a-entity"); el.id = "avatar-" + user;
      var lx = (-EYE_S * R_AV).toFixed(4), rx = (EYE_S * R_AV).toFixed(4), ez = (-EYE_C * R_AV).toFixed(4);
      el.innerHTML = '<a-sphere class="head" radius="' + R_AV + '" color="' + INFO_COLOR + '"></a-sphere>'
        + '<a-box class="body" width="0.26" depth="0.26" color="' + INFO_COLOR + '" opacity="0.85"></a-box>'
        + '<a-entity class="eyes">'
        +   '<a-sphere class="eye" position="' + lx + ' 0 ' + ez + '" radius="' + EYE_R + '" color="' + INFO_COLOR + '"></a-sphere>'
        +   '<a-sphere class="eye" position="' + rx + ' 0 ' + ez + '" radius="' + EYE_R + '" color="' + INFO_COLOR + '"></a-sphere>'
        + '</a-entity>';
      wr.appendChild(el);
    }
    var hx = pose.p[0], hy = pose.p[1], hz = pose.p[2], h = Math.max(0.1, hy - R_AV - GAP_AV);
    el.setAttribute("position", hx + " " + hy + " " + hz);          // entity origin = the head
    var yawDeg = 0, pitchDeg = 0;
    if (pose.q) {                                                   // heading from the head orientation
      var aim = WM.avatarAim(THREE, pose.q);
      yawDeg = aim.yawDeg; pitchDeg = aim.pitchDeg;
    }
    el.setAttribute("rotation", "0 " + yawDeg + " 0");             // whole avatar yaws with the headset
    el.querySelector(".eyes").setAttribute("rotation", pitchDeg + " 0 0");  // eyes also pitch up/down
    var body = el.querySelector(".body");
    body.setAttribute("height", h);
    body.setAttribute("position", "0 " + ((h / 2) - hy) + " 0");   // box: floor → just below the head, local
  }
  function removeAvatar(user) {
    var el = document.getElementById("avatar-" + user);
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }
  function presenceTick() {
    if (!socket || socket.readyState !== 1) return;
    var sc = document.querySelector("a-scene");
    var cam = sc && sc.camera;
    if (!cam) return;
    var THREE = AFRAME.THREE, p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
    cam.updateMatrixWorld();
    // Broadcast the head pose in the SHARED reference frame (F_ref) the server + world model use, so
    // pose-relative director actions ("in front of me", view_relative) land correctly. #world-root is now
    // identity (local-first render, docs §2), so we apply the registration transform T (F_track → F_ref)
    // directly rather than reading world-root's (now identity) matrix. Falls back to the raw camera pose
    // before registration / on desktop (no capture) — where scene space is already the shared frame.
    var rc = sc.components && sc.components["room-capture"];
    var m = (rc && rc._haveT && rc._Tmat)
      ? new THREE.Matrix4().multiplyMatrices(rc._Tmat, cam.matrixWorld)   // F_track camera pose → F_ref
      : cam.matrixWorld;
    m.decompose(p, q, s);
    socket.send(JSON.stringify({ type: "presence", pose: { p: [p.x, p.y, p.z], q: [q.x, q.y, q.z, q.w] } }));
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss" : "ws";
    var u = currentUser();
    var ws = new WebSocket(proto + "://" + location.host + "/ws" + (u ? "?user=" + encodeURIComponent(u) : ""));
    socket = ws;
    ws.onopen = function () {
      console.log("[conjure] connected" + (u ? " as " + u : ""));
      if (amHolding) ws.send(JSON.stringify({ type: "hold" }));   // re-assert our hold after a reconnect
    };
    ws.onclose = function () { console.log("[conjure] disconnected — retrying in 2s"); setTimeout(connect, 2000); };
    ws.onmessage = function (ev) {
      var msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") { worldOwner = msg.owner || worldOwner; applySnapshot(msg.world); }
      else if (msg.type === "patch") applyPatch(msg.patch);
      else if (msg.type === "info") showInfo(msg.msg);    // e.g. "'<world>' is private — ask <owner>…"
      else if (msg.type === "presence") {
        setAvatar(msg.user, msg.pose);
        if (msg.user === worldOwner) maybeSpawnGuest(msg.pose);   // a guest drops in to the owner's right
      }
      else if (msg.type === "presence_leave") removeAvatar(msg.user);
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
        this._anchorInv = null;     // last-good registration frame, reused when establishing (= _Tmat once registered)
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
        this._lostSince = 0;        // when registration last lost its lock (0 = locked)
        this._reloc = false;        // showing the passthrough "re-localizing" fallback?
        this._lastDiag = null;      // last capture's frame (yaw/px/pz) — for the per-capture drift delta
        this._regRes = null;        // last register()'s per-wall residuals (--debug-registration probe)
        this._refSeq = 0;           // counter for minting brand-new surface ids
        var self = this;
        // A recenter (Meta button) / boundary re-entry fires a 'reset' on the reference space — force an
        // immediate re-capture so registration re-locks the frame within a frame instead of up to ~2 s.
        this._onReset = function () { self.lastPost = 0; };
      },
      // Force an immediate re-capture (manual realign — see the /room/realign signal below).
      recapture: function () { this.lastPost = 0; },
      // Drop the current room REFERENCE FRAME. Called on a WORLD SWITCH (applySnapshot) so the next capture
      // re-seeds from the NEW world's geometry — or establishes fresh in an empty/void world — instead of
      // registering the new room against the PREVIOUS world's constellation. Without this, a guest who was
      // briefly in the owner's world keeps the owner's `_ref` after minting their own world, so their real
      // room renders registered to the owner's room → a stable positional offset (new-space-flow).
      resetFrame: function () {
        this._ref = []; this._Tmat = null; this._haveT = false;
        this._anchorInv = null; this._refSeq = 0; this._lostSince = 0; this.lastPost = 0;
      },
      // Position #world-root. LOCAL-FIRST (docs/local-first-geometry.md §2): a captured room renders its
      // real surfaces at their OWN detected (refSpace/F_track) poses via _renderLocal, so content is
      // F_track-native and #world-root stays at IDENTITY. Registration still runs — but only to assign
      // STABLE IDS; it no longer drives a render transform, because one rigid frame can't reconcile the
      // Quest's locally-non-rigid map (the whole reason for this architecture). A VOID/outdoor world has no
      // real surfaces to anchor to, so it keeps the old behaviour: park world-root on the canonical frame
      // (_Tmat⁻¹) so its skybox + content orient consistently across visits.
      _updateWorldFrame: function (frame, refSpace) {
        var THREE = AFRAME.THREE;
        var wr = document.getElementById("world-root");
        if (!wr) return;
        if (!isVoidWorld) {                        // captured room → world-root identity (content is F_track-native)
          wr.object3D.position.set(0, 0, 0);
          wr.object3D.quaternion.set(0, 0, 0, 1);
          return;
        }
        if (this._haveT) {                         // void world → keep the canonical-frame parking
          var inv = this._Tmat.clone().invert();
          var ip = new THREE.Vector3(), iq = new THREE.Quaternion(), is = new THREE.Vector3();
          inv.decompose(ip, iq, is);
          wr.object3D.position.copy(ip); wr.object3D.quaternion.copy(iq);
        }
      },
      // Render THIS client's own captured surfaces (local-first). Each is drawn at its raw F_track pose via
      // the shared applyEntity — which runs the render apply-gate, so an unchanged surface isn't re-laid and
      // nothing "pops" (docs/local-first-geometry.md §5-6). Then debounce-prune any real surface that's been
      // absent a few captures, so a single missed capture doesn't flicker a wall away. world-root is
      // identity, so a surface at its captured pose renders at the real-world spot.
      _renderLocal: function (surfaces) {
        localRenderActive = true;                  // from now on, server real-surface ops are ignored (local owns them)
        var seen = {};
        surfaces.forEach(function (s) {
          seen[s.id] = 1;
          applyEntity({ id: s.id, transform: { position: s.position, rotation: s.rotation },
            components: { surface: { extent: s.extent, holes: s.holes || [] }, material: surfaceStyles[s.id] },
            meta: { real: true, semantic: s.semantic } });   // apply the SHARED styling by id (docs §3)
        });
        var abs = this._localAbsent || (this._localAbsent = {});
        var wr = document.getElementById("world-root"); if (!wr) return;
        Array.prototype.forEach.call(wr.querySelectorAll('[data-real="1"]'), function (el) {
          if (seen[el.id]) { delete abs[el.id]; return; }
          abs[el.id] = (abs[el.id] || 0) + 1;
          if (abs[el.id] >= 3 && el.parentNode) { el.parentNode.removeChild(el); delete abs[el.id]; }
        });
      },
      // Place director-authored content (models, props — anything with a remembered F_ref pose) via
      // plane-relative anchors (docs/local-first-geometry.md §5). Since #world-root is identity in a captured
      // room, we can't render content at its raw F_ref pose; instead, for each content entity, author an
      // anchor from its F_ref pose against the SEED walls (F_ref) and re-solve it against THIS client's LOCAL
      // walls (F_track) — so it lands at the right spot in the room, riding the same non-rigid geometry the
      // surfaces do. Re-solved every capture as the local walls refine. Free mode (full 3-D + orientation).
      _placeContent: function (localSurfaces) {
        var PA = window.PlaneAnchor; if (!PA) return;
        var THREE = AFRAME.THREE;
        var localPl = localToPlanes(THREE, localSurfaces), refPl = refToPlanes(THREE, this._ref);
        if (localPl.length < 2 || refPl.length < 2) return;         // not enough wall/floor basis yet
        var wr = document.getElementById("world-root"); if (!wr) return;
        var placed = 0;
        Array.prototype.forEach.call(wr.children, function (el) {
          if (!el._frefPose || !el.object3D) return;
          var fp = el._frefPose;
          var entity = { mode: "free", quaternion: eulerYXZToQuat(THREE, fp.rotation),
            position: new THREE.Vector3(fp.position[0] || 0, fp.position[1] || 0, fp.position[2] || 0) };
          var anchor = PA.authorAnchor(THREE, entity, refPl);
          var sol = PA.solveAnchor(THREE, anchor, localPl);
          if (!sol.ok) return;                                      // degenerate / missing walls → hold last pose
          el.object3D.position.copy(sol.position);
          el.object3D.quaternion.copy(sol.quaternion);
          placed++;
        });
        if (window.CONJURE_DEBUG_REGISTRATION && placed)
          debugLog("content", "placed " + placed + " via anchors (ref=" + refPl.length + " local=" + localPl.length + ")", true);
      },
      // The room-snapping geometry lives in the pure, unit-tested client/room-snap.js (RoomSnap). These
      // thin wrappers adapt it to the component's state (this._ref, this._regStat). See that file.
      _euler: function (q) { return window.RoomSnap.eulerYXZ(AFRAME.THREE, q); },
      _yawOf: function (n) { return window.RoomSnap.yawOf(n); },
      _register: function (cur) {
        var r = window.RoomSnap.register(AFRAME.THREE, cur, this._ref, window.CONJURE_REG);
        this._regStat = r.stat;
        this._regRes = r.residuals || null;   // per-wall fit residuals for the --debug-registration probe
        return r.Tmat;
      },
      // While registration can't lock (the Quest's planes are inconsistent after a sleep/relocalization),
      // the world is parked at a stale, WRONG frame. After a few seconds of that, reveal AR passthrough
      // (hide the virtual world + sky) with a headset-locked hint so you can safely step out of the play
      // boundary and back in — which forces the Quest to re-localize and lets registration re-lock.
      // Auto-restores the moment a confident lock returns.
      _markLost: function (time) {
        if (!this._lostSince) this._lostSince = time;
        if (!this._reloc && time - this._lostSince > 3000) this._relocalize(true);
      },
      _relocalize: function (on) {
        this._reloc = on;
        // A low-frequency, useful signal: tracking lost its lock (passthrough fallback shown) / recovered.
        debugLog("track", on ? "lost lock — showing passthrough + hint" : "re-locked — restoring world");
        var root = document.getElementById("world-root"), sky = document.getElementById("sky");
        var groundedSky = document.getElementById("grounded-sky");
        if (on) {
          if (root) root.setAttribute("visible", false);     // hide the stale/wrong virtual world…
          if (sky) sky.setAttribute("visible", false);       // …and the sky, so AR passthrough shows
          if (groundedSky) groundedSky.setAttribute("visible", false);   // …and any grounded skybox
          document.querySelectorAll("[data-scaffold]").forEach(function (el) { el.setAttribute("visible", false); });
        } else {
          if (root) root.setAttribute("visible", true);
          applyImmersion();                                  // restore sky/scaffold for the current mode
        }
        this._hint(on);
      },
      _hint: function (on) {
        var el = document.getElementById("reloc-hint");
        if (on && !el) {
          var cam = document.querySelector("a-camera") || document.querySelector("[camera]");
          if (!cam) return;
          el = document.createElement("a-entity");
          el.id = "reloc-hint";
          el.setAttribute("position", "0 0 -1.5");           // locked ~1.5 m in front of the headset
          el.setAttribute("text", { value: "Re-localizing…\nstep out of your play area and back in",
            align: "center", color: INFO_COLOR, width: 1.6, baseline: "center" });
          cam.appendChild(el);
        }
        if (el) el.setAttribute("visible", on);
      },
      // Co-location diagnostics (debug_log only): one line + a head-locked HUD PER capture, so drift is
      // measurable. Shows role, #reference vs #detected planes, the register vote's stat (inliers/total,
      // candidate-yaw count, solved yaw + translation, or the no-lock reason), whether it LOCKed, and the
      // frame's change since the previous capture (Δpos/Δyaw of _Tmat). Reading it:
      //   • Δpos≈0, Δyaw≈0 across captures ⇒ frame is STABLE. If content still looks off, it's a STATIC
      //     mis-registration (wrong frame) — check inliers (low ⇒ weak/ambiguous lock) → matcher work.
      //   • Δpos/Δyaw creeping one way ⇒ frame is WALKING ⇒ unstable registration → matcher work.
      //   • repeated "hold" with low inl=x/y ⇒ never locking (too little overlap from this vantage).
      _diag: function (amOwner, nCur, reg) {
        var m = reg || (this._haveT ? this._Tmat : null), dTxt = "";
        if (m) {
          var e = m.elements, yaw = Math.atan2(e[8], e[0]) * 180 / Math.PI, px = e[12], pz = e[14];
          if (this._lastDiag) {
            var dy = Math.abs(((yaw - this._lastDiag.yaw + 540) % 360) - 180);
            var dp = Math.hypot(px - this._lastDiag.px, pz - this._lastDiag.pz);
            dTxt = "  Δpos=" + dp.toFixed(3) + "m Δyaw=" + dy.toFixed(1) + "°";
          }
          this._lastDiag = { yaw: yaw, px: px, pz: pz };
        }
        // Residual probe (non-rigidity test): after the best rigid fit, how far is each covered plane from
        // its reference, and how does that vary with the wall's distance from the frame origin? A flat
        // offset everywhere ⇒ a bad-but-rigid lock; residuals GROWING with distance ⇒ the map is non-rigid
        // (a single rigid transform can't fit — the "frozen room goes askew" thread). HUD gets μ/max + the
        // worst wall's distance; the full per-wall list goes to the log for offline residual-vs-distance.
        var res = this._regRes || [], rTxt = "";
        if (res.length) {
          var sum = 0, mx = 0, mxD = 0;
          res.forEach(function (w) { sum += w.res; if (w.res > mx) { mx = w.res; mxD = w.dist; } });
          rTxt = "  res μ=" + Math.round(sum / res.length * 100) + "cm max=" + Math.round(mx * 100) + "cm @" + mxD.toFixed(1) + "m";
          debugLog("residual", res.map(function (w) {
            return (w.id ? String(w.id).slice(-7) : "?") + " " + Math.round(w.res * 100) + "cm@" + w.dist.toFixed(1) + "m";
          }).join(" | "), window.CONJURE_DEBUG_REGISTRATION);
        }
        var line = (amOwner ? "OWNER" : "GUEST") + " ref=" + this._ref.length + " cur=" + nCur
          + "  " + (this._regStat || "?") + (reg ? "  LOCK" : "  hold") + dTxt + rTxt;
        debugLog("coloc", line, window.CONJURE_DEBUG_REGISTRATION);   // gated by --debug-registration, not debug_log
        this._diagHud(line);
      },
      // Pin the skybox to the WORLD frame, not the headset's tracking origin. The <a-sky>/#grounded-sky
      // live as scene children (they can't go inside #world-root — applySnapshot clears its innerHTML),
      // so they'd otherwise render at identity in the arbitrary per-session tracking frame, making a
      // skybox's orientation change between visits while the (registered) room stays put. Copy #world-root's
      // orientation onto both so the sky rides the SAME persistent frame as the room. Rotation only:
      // #world-root's rotation is a pure gravity-aligned yaw (the register vote solves yaw + x/z only), so
      // a plain sky sphere stays viewer-centered and a grounded dome spins about vertical — its ground
      // stays flat on the floor, just re-oriented. In a void world (world-root ≈ identity) this is a no-op.
      _pinSky: function () {
        var wr = document.getElementById("world-root"); if (!wr) return;
        var q = wr.object3D.quaternion;
        var sky = document.getElementById("sky"); if (sky) sky.object3D.quaternion.copy(q);
        var g = document.getElementById("grounded-sky"); if (g) g.object3D.quaternion.copy(q);
      },
      _diagHud: function (text) {
        var el = document.getElementById("coloc-hud");
        if (!el) {
          var cam = document.querySelector("a-camera") || document.querySelector("[camera]");
          if (!cam) return;
          el = document.createElement("a-entity");
          el.id = "coloc-hud";
          el.setAttribute("position", "0 -0.35 -1");         // head-locked, lower-center, ~1 m ahead
          el.setAttribute("text", { value: "", align: "center", color: INFO_COLOR, width: 1.2, baseline: "center" });
          el.setAttribute("overlay", "");                    // draw on top so passthrough/room never hides it
          cam.appendChild(el);
        }
        el.setAttribute("text", "value", text);
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
        this._pinSky();                                     // …and pin the sky to that SAME frame (see below)
        // One-time render diagnostic: the actual foveation + XR framebuffer size + per-eye viewport. Tells
        // us whether foveation is really off (peripheral-blur suspect) and whether the used viewport is a
        // sub-rect of the framebuffer (the fuzzy right/bottom-band suspect). debug_log-gated, logged once.
        if (window.CONJURE_DEBUG_LOG && !this._loggedRender) {
          this._loggedRender = true;
          try {
            var xr = sceneEl.renderer.xr, session = xr.getSession(), bl = session && session.renderState.baseLayer;
            var vp = "", vpose = frame.getViewerPose(refSpace);
            if (vpose && bl) vpose.views.forEach(function (v, i) {
              var p = bl.getViewport(v); vp += " eye" + i + "=" + p.x + "," + p.y + " " + p.width + "x" + p.height; });
            debugLog("render", "foveation=" + xr.getFoveation()
              + " fb=" + (bl ? bl.framebufferWidth + "x" + bl.framebufferHeight : "?") + vp);
          } catch (e) { debugLog("render", "diag failed: " + e); }
        }
        if (!frame.detectedPlanes) return;                  // capture needs plane detection
        if (refused) return;                                // refused by the admission gate → don't capture,
                                                            // register, or post geometry into a space we're not in
        var CAPTURE_MS = window.CONJURE_CAPTURE_MS || 2000; // recapture cadence (--capture-interval), ~0.5 Hz
        var RETRY_MS = Math.max(0, CAPTURE_MS - 300);       // after a rejected capture, retry ~300 ms sooner
        if (time - this.lastPost < CAPTURE_MS) return;      // throttle
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

        // Fragmentation probe (--debug-registration): how many detected planes carry each semantic THIS
        // capture, and how many spatial CLUSTERS they form (planes within 0.25 m are one physical thing).
        // "wall art=9/3cl" ⇒ 9 planes but only 3 real pictures = the Quest emits ~3 planes per picture
        // simultaneously (confirms the triplication is device-side fragmentation, not our id logic). A bare
        // "wall art=3" (planes == clusters) would refute it.
        if (window.CONJURE_DEBUG_REGISTRATION) {
          var bySem = {};
          cur.forEach(function (c) { (bySem[c.sem] = bySem[c.sem] || []).push(c.pos); });
          var parts = Object.keys(bySem).sort().map(function (sem) {
            var pts = bySem[sem], reps = [];
            pts.forEach(function (p) {
              if (!reps.some(function (r) { return r.distanceTo(p) < 0.25; })) reps.push(p);
            });
            return sem + "=" + pts.length + (pts.length !== reps.length ? "/" + reps.length + "cl" : "");
          });
          debugLog("planes", parts.join("  "), true);

          // Normal-direction probe (--debug-registration): does each surface's normal point OUTWARD from
          // the room (away from the head, which is inside) or inward? Per semantic: "out"=away from head,
          // "in"=toward head. If walls are consistently N-out/0-in, the wall normal reliably marks the
          // interior side (so we could hang pictures via -normal, no viewpoint). "wall art" should read the
          // OPPOSITE (in) — confirming shell-vs-object normals differ. Any semantic that's MIXED (both
          // out and in across walls) means the sign isn't trustworthy → keep the viewpoint disambiguation.
          var vpose = frame.getViewerPose(refSpace);
          if (vpose) {
            var hp = vpose.transform.position, dir = {};
            cur.forEach(function (c) {
              var d = c.nrm.x * (hp.x - c.pos.x) + c.nrm.y * (hp.y - c.pos.y) + c.nrm.z * (hp.z - c.pos.z);
              var s = dir[c.sem] || (dir[c.sem] = { out: 0, in: 0 });
              if (d < 0) s.out++; else s.in++;            // d<0 ⇒ normal points AWAY from the head (outward)
            });
            debugLog("normals", Object.keys(dir).sort().map(function (sem) {
              return sem + "=" + dir[sem].out + "out/" + dir[sem].in + "in"; }).join("  "), true);
          }
        }

        // Am I the room AUTHORITY? The active world's owner authors the geometry; everyone else is a
        // register-only GUEST (room-model §8b). An empty currentUser() is the dev/default user = owner
        // (matches the server treating a missing X-Conjure-User as the owner); unknown worldOwner (no
        // snapshot yet) also defaults to owner so authoring is never briefly locked out.
        var me = currentUser(), amOwner = !me || !worldOwner || me === worldOwner;

        // Seed the reference constellation from the persisted/broadcast surfaces. The AUTHORITY seeds ONCE,
        // then owns and slowly evolves it (Pass B). A GUEST re-seeds EVERY capture straight from the
        // authoritative broadcast, so its reference always EQUALS the owner's current geometry and it never
        // contributes its own — this frozen, authority-owned target is what stops the shared frame drifting.
        if (docSurfaces && docSurfaces.length >= 3 && (!this._ref.length || !amOwner)) {
          var hadRef = this._ref.length;
          if (!amOwner) self._ref = [];                                  // guest: replace wholesale from authority
          var mx = 0;
          docSurfaces.forEach(function (e) {
            self._ref.push(window.RoomSnap.surfaceToRef(THREE, e));       // one source of truth (YXZ-correct normal)
            var mm = /_(\d+)$/.exec(e.id); if (mm) mx = Math.max(mx, +mm[1] + 1);   // keep new ids unique
          });
          self._refSeq = Math.max(self._refSeq, mx);
          if (!hadRef) console.log("[conjure] seeded room frame from " + self._ref.length + " surfaces"
            + (amOwner ? "" : " (guest, register-only)"));
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
          this._markLost(time);
          this.lastPost = time - RETRY_MS; return;
        }

        // Space selection, stage 2 (new-space-flow §3): while candidates are pending, vote THIS capture
        // against each one. A confident registration ⇒ we're in that space → join it (/space/select
        // matched). Once the capture is rich enough that a real match WOULD have locked (≥6 walls, a few
        // tries) but none did, we're somewhere new → commit "no match" so the server stamps/mints a space
        // here. The geometric vote decides — not a surface count — so a sparse early capture just stays
        // undecided and we fall through to normal behavior (register correctly DECLINES a non-matching
        // booted room, so nothing drifts) until the capture fills in. Never runs for a void world.
        if (pendingSelect && !isVoidWorld) {
          var pick = window.RoomSnap.selectSpace(THREE, cur, pendingSelect.candidates, window.CONJURE_REG);
          if (pick) { commitSelect({ matched: true, owner: pick.owner, name: pick.name }); return; }
          pendingSelect.tries++;
          var nWalls = cur.filter(function (c) { return c.orient === "vertical"; }).length;
          if (nWalls >= 6 && pendingSelect.tries >= 3) { commitSelect({ matched: false }); return; }
          // else undecided — fall through and keep rendering the booted world until the capture fills in
        }

        // VOID/outdoor world: not tied to a captured space, so there's no reference to register against.
        // Derive a deterministic frame from the live walls (canonicalFrame) — the same physical room
        // canonicalizes to the same frame every visit, so the skybox holds a consistent-but-arbitrary
        // orientation. Never capture/mint/post (a void world owns no geometry). Everyone (owner or guest)
        // canonicalizes identically. Hold if there aren't enough walls yet.
        if (isVoidWorld) {
          var cf = window.RoomSnap.canonicalFrame(THREE, cur);
          this._regStat = cf.stat;
          this._regRes = null;   // canonicalFrame doesn't register against a reference → no residuals
          if (window.CONJURE_DEBUG_REGISTRATION) this._diag(amOwner, cur.length, cf.Tmat);
          if (!cf.Tmat) { this._markLost(time); this.lastPost = time - RETRY_MS; return; }   // too few walls → hold
          if (this._lostSince) { this._lostSince = 0; if (this._reloc) this._relocalize(false); }
          this._Tmat = cf.Tmat; this._haveT = true; this._anchorInv = cf.Tmat;
          this.lastPost = time;
          return;
        }

        // Recover the frame transform; the AUTHORITY may bootstrap the reference on its first capture, but
        // otherwise (and ALWAYS for a guest) require a confident registration — a low-confidence result
        // means we're not locked, so hold + retry fast. A guest can never establish a fresh frame.
        var reg = this._register(cur), canEstablish = amOwner && this._ref.length === 0;
        if (window.CONJURE_DEBUG_REGISTRATION) this._diag(amOwner, cur.length, reg);   // opt-in: one line + HUD/capture
        if (!reg && !canEstablish) { this._markLost(time); this.lastPost = time - RETRY_MS; return; }   // not locked → hold
        if (this._lostSince) { this._lostSince = 0; if (this._reloc) this._relocalize(false); }   // re-locked → restore
        var registered = !!reg, Tmat;
        if (reg) { Tmat = this._Tmat = reg; this._haveT = true; }
        else { Tmat = this._anchorInv || new THREE.Matrix4(); this._Tmat = Tmat; this._haveT = true; }  // establish fresh
        this._anchorInv = Tmat;
        // Pass B — assign each plane a STABLE id via matchRef, and build TWO views of it: `localSurfaces`
        // (its raw F_track pose — what we RENDER, matching THIS headset's passthrough) and `surfaces` (the
        // reference-frame pose the OWNER posts to persist the shared model/seed). Both carry the same id.
        // Runs for owner AND guest now (unified): everyone renders their own capture; only the owner authors.
        // See docs/local-first-geometry.md §5-6.
        var surfaces = [], localSurfaces = [], floor = null, claimed = new Set();
        cur.forEach(function (c) {
          var planeMat = new THREE.Matrix4().compose(c.pos, c.quat, new THREE.Vector3(1, 1, 1));
          var lp = new THREE.Vector3(), lq = new THREE.Quaternion(), ls = new THREE.Vector3();
          Tmat.clone().multiply(planeMat).decompose(lp, lq, ls);
          // match to the nearest SAME-SEMANTIC, SAME-FACING reference (matchRef's normal gate stops the two
          // faces of a shared partition wall from swapping ids — see room-snap.js).
          var cyaw = self._yawOf(UP.clone().applyQuaternion(lq));
          var best = window.RoomSnap.matchRef({ pos: lp, nyaw: cyaw, sem: c.sem, orient: c.orient },
                                              self._ref, claimed);
          var sid;
          if (best) {                                                     // re-inherit the existing id
            sid = best.id; claimed.add(best);
            best.pos.lerp(lp, 0.3);                                       // track slow real drift
            best.ext = c.ext.slice(); best.nyaw = cyaw;
          } else {                                                        // genuinely new → mint + remember
            // Mint probe (--debug-registration): WHY did matchRef reject the existing ref? Log the nearest
            // same-semantic ref's distance / normal-yaw delta / orientations / claimed-state, so a re-mint
            // reveals its actual cause (too far? >60° facing gate? already claimed? no ref at all?).
            if (window.CONJURE_DEBUG_REGISTRATION) {
              var nr = null, nd = 1e9;
              self._ref.forEach(function (r) {
                if (r.sem !== c.sem) return;
                var d = lp.distanceTo(r.pos); if (d < nd) { nd = d; nr = r; }
              });
              if (nr) {
                var dy = Math.abs(((cyaw - nr.nyaw) * 180 / Math.PI + 540) % 360 - 180);
                debugLog("mint", c.sem + " → nearest " + nr.id + " d=" + nd.toFixed(2) + "m Δyaw=" + dy.toFixed(0)
                  + "° orient c/" + c.orient + " r/" + nr.orient + (claimed.has(nr) ? " CLAIMED" : ""), true);
              } else {
                debugLog("mint", c.sem + " → no same-semantic ref (ref=" + self._ref.length + ")", true);
              }
            }
            sid = "real_" + c.sem.replace(/\s+/g, "_") + "_" + (self._refSeq++);
            best = { id: sid, sem: c.sem, ext: c.ext.slice(), pos: lp.clone(),
                     nyaw: cyaw, orient: c.orient };
            self._ref.push(best); claimed.add(best);
          }
          surfaces.push({ id: sid, semantic: c.sem, position: [lp.x, lp.y, lp.z],
            rotation: self._euler(lq), extent: [c.ext[0], c.ext[1]], _lp: lp.clone(), _lq: lq.clone(),
            debug: { pos: c.raw.pos, quat: c.raw.quat, orient: c.orient, label: c.sem,
                     polyY: c.polyY, n: c.poly.length, registered: registered, regStat: self._regStat } });
          // Same surface, same id, but at its RAW captured (F_track) pose — this is what renders locally.
          localSurfaces.push({ id: sid, semantic: c.sem, position: [c.pos.x, c.pos.y, c.pos.z],
            rotation: self._euler(c.quat), extent: [c.ext[0], c.ext[1]],
            _lp: c.pos.clone(), _lq: c.quat.clone(), debug: {} });
          if (c.sem === "floor" && (!floor || c.ext[0] * c.ext[1] > floor._area)) {
            floor = { floorPolygon: c.poly.map(function (pt) { return [pt.x, pt.z]; }), height: 2.6, _area: c.ext[0] * c.ext[1] };
          }
        });
        if (!surfaces.length) return;

        // LOCAL RENDER (every client): snap corners + insets in F_track, then draw each surface at its OWN
        // captured pose — matching THIS headset's passthrough, with no shared rigid frame and no server
        // round-trip. Squaring is intentionally skipped (default off — trust the raw local planes; docs §9).
        // world-root stays identity (_updateWorldFrame). The apply-gate inside applyEntity means an unchanged
        // surface isn't re-laid, so nothing "pops".
        window.RoomSnap.joinCorners(THREE, localSurfaces);
        window.RoomSnap.snapInsets(THREE, localSurfaces);
        this._renderLocal(localSurfaces);
        this._placeContent(localSurfaces);                // director content → plane-relative anchors (docs §5)

        if (!amOwner) { this.lastPost = time; return; }   // guest: rendered its own capture; never authors/posts

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
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Conjure-User": currentUser() || "" },
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
      warmGeo();   // AR-capable device → start acquiring the location fix now (before Enter AR is clicked),
                   // so it's ready by the time they enter. Desktop (no immersive-ar) never geolocates.
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
    var s = document.querySelector('script[src*="conjure-client.js"]');
    var m = s && /[?&]v=(\d+)/.exec(s.src);
    var el = document.getElementById("conjure-version");
    if (el) el.textContent = el.textContent.trim() + (m ? "  ✓ js v" + m[1] : "  ✓ js");
    // Also record the loaded client version in the server log, so staleness is VISIBLE there (the Quest
    // Browser keeps a loaded page's JS across an AR re-entry — re-entering VR does NOT re-fetch it). Compare
    // this v against the file mtime to know instantly whether a headset is running the current build.
    debugLog("version", "client js v" + (m ? m[1] : "?") + " loaded", true);
  }

  // Acquire the headset's coarse location EAGERLY — called on page load for AR-capable devices (see
  // setupARButton), so the fix is usually cached before the user enters AR. This only ACQUIRES the fix;
  // space selection still happens on enter-vr (AR-only — a desktop viewer isn't physically in a space).
  // Best-effort: needs HTTPS + permission. If AR is already waiting on the fix when it lands, proceed.
  function warmGeo() {
    if (geoStatus === "pending" || geoStatus === "ready") return;
    if (window.CONJURE_FORCE_GEO) {          // TEST (--force-geo): synthesize a fix so space selection can
      lastGeo = { lat: 0, lon: 0, user: currentUser() || undefined };   // proceed without Quest GPS — the
      geoStatus = "ready";                   // server overrides these coords anyway (_apply_forced_geo)
      if (awaitingSpace) beginSpaceSelection();
      return;
    }
    if (!navigator.geolocation) { debugLog("sel", "warmGeo: navigator.geolocation ABSENT", true); return; }
    geoStatus = "pending"; geoTries++;
    debugLog("sel", "warmGeo: requesting a fix… (try " + geoTries + "/" + GEO_MAX_TRIES + ")", true);
    navigator.geolocation.getCurrentPosition(function (pos) {
      lastGeo = { lat: pos.coords.latitude, lon: pos.coords.longitude, user: currentUser() || undefined };
      geoStatus = "ready"; geoTries = 0;
      debugLog("sel", "warmGeo: fix OK (" + pos.coords.latitude.toFixed(4) + "," + pos.coords.longitude.toFixed(4)
        + ") awaiting=" + awaitingSpace, true);
      if (awaitingSpace) beginSpaceSelection();              // AR entered while we were locating → select now
    }, function (err) {
      geoStatus = "failed";
      debugLog("sel", "warmGeo: fix FAILED code=" + (err && err.code) + " " + (err && err.message)
        + " (try " + geoTries + "/" + GEO_MAX_TRIES + ")", true);
      // Don't dump the user into the (possibly void) active world while a slow fix might still land — stay
      // blanked to passthrough and RETRY; only give up (join the active world) after GEO_MAX_TRIES.
      if (!awaitingSpace) return;
      if (geoTries < GEO_MAX_TRIES) { setAwaitMessage("locating"); setTimeout(function () { if (awaitingSpace) warmGeo(); }, 1500); }
      else { debugLog("sel", "warmGeo: giving up after " + geoTries + " tries → join active world", true); endAwaitingSpace(); }
    }, { maximumAge: 600000, timeout: 20000 });
  }

  // Entering AR: we don't yet know which space we're in, so blank to passthrough and either select
  // immediately (fix already warm), wait for the fix (showing a "getting your location" notice), or — if
  // geolocation is unavailable/denied — just join the active world as-is.
  function onEnterAR() {
    awaitingSpace = true;
    blankToPassthrough();
    debugLog("sel", "onEnterAR geoStatus=" + geoStatus + " geoAPI=" + !!navigator.geolocation
      + " forceGeo=" + !!window.CONJURE_FORCE_GEO + " lastGeo=" + !!lastGeo, true);
    if (geoStatus === "ready") beginSpaceSelection();
    else if (window.CONJURE_FORCE_GEO) warmGeo();            // TEST: synthesize a fix now, then select
    else if (!navigator.geolocation) {                      // truly no geolocation API → can't identify a space
      debugLog("sel", "onEnterAR → endAwaitingSpace (no geolocation API; join active world)", true);
      endAwaitingSpace();
    } else {                                                // idle / pending / previously-failed → (re)try,
      setAwaitMessage("locating"); geoTries = 0; warmGeo();  // staying blanked to passthrough (not the void world)
    }
  }

  // Stage-1 discovery using the (warm) fix: ask the server for geo-near candidate spaces, then arm the
  // vote (candidates) or commit "no match" (nowhere near). Runs only in AR, once the fix is ready.
  function beginSpaceSelection() {
    if (!lastGeo) { debugLog("sel", "beginSpaceSelection: no lastGeo → endAwaitingSpace", true); endAwaitingSpace(); return; }
    setAwaitMessage("finding");                              // fix in hand → now matching/establishing
    var g = lastGeo;
    debugLog("sel", "beginSpaceSelection: POST /geolocation (" + g.lat.toFixed(4) + "," + g.lon.toFixed(4) + ")", true);
    fetch("/geolocation", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lat: g.lat, lon: g.lon, user: g.user, cid: clientId }),
    }).then(function (r) { return r.json(); }).then(function (resp) {
      var cands = (resp && resp.candidates) || [];
      debugLog("sel", "/geolocation resp: selected=" + (resp && resp.selected) + " cands=" + cands.length, true);
      if (!resp || resp.selected) { endAwaitingSpace(); return; }   // already established this session
      // No geo-near space at all ⇒ somewhere new — commit "no match" now so the server mints a fresh space
      // here. Otherwise arm the vote: room-capture picks the matching candidate as its capture fills in.
      if (!cands.length) commitSelect({ matched: false });
      else pendingSelect = { candidates: cands, tries: 0 };
    }).catch(function (e) { debugLog("sel", "/geolocation FETCH ERROR: " + e, true); endAwaitingSpace(); });
  }

  // Commit stage-2 of space selection: tell the server the verdict (matched a candidate, or none), with the
  // reported location so a no-match can stamp/mint a space there. Clears the pending vote either way.
  // The server's reply is the ADMISSION verdict (steps 4/7): `refused` ⇒ we're not in the active space —
  // show the message and blank the world so only passthrough shows (we never became a holder); otherwise
  // we're admitted / established the space ⇒ declare our HOLD so the server counts us as occupying it.
  function commitSelect(result) {
    pendingSelect = null;
    var g = lastGeo || {};
    fetch("/space/select", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ lat: g.lat, lon: g.lon, user: g.user, cid: clientId }, result)),
    }).then(function (r) { return r.json(); }).then(function (resp) {
      if (!resp || resp.selected === false) return;        // already committed this epoch — nothing to do
      if (resp.refused) {                                  // not in the active space → bare passthrough + notice
        var m = resp.msg || "You're not in this space — content stays hidden here.";
        showInfo(m);                                       // 2D page banner
        showHeadsetMessage(m);                             // + in-headset notice (visible in AR)
        passthroughBlank();                                // tear down world → passthrough, ignore updates
        return;
      }
      // admitted / established → we hold the space. A matched-existing-space JOIN or a mint SWITCHES worlds
      // → its fresh snapshot renders and clears awaitingSpace. An ADMIT to the ALREADY-active space sends no
      // snapshot, so restore the (blanked) view here.
      refused = false;
      amHolding = true;
      if (socket && socket.readyState === 1) socket.send(JSON.stringify({ type: "hold" }));
      if (awaitingSpace) endAwaitingSpace();
    }).catch(function () { endAwaitingSpace(); });
  }

  window.addEventListener("load", function () {
    connect(); setupARButton(); markVersion();
    setInterval(presenceTick, 100);                 // ~10 Hz head-pose broadcast (presence)
    var sc = document.querySelector("a-scene");      // space selection runs only from a real AR session
    if (sc) {
      sc.addEventListener("enter-vr", onEnterAR);
      sc.addEventListener("exit-vr", function () {  // left AR → release our hold so the space can unlock
        amHolding = false;
        awaitingSpace = false; hideHeadsetMessage();   // bailed out of AR mid-selection → drop the notice
        if (socket && socket.readyState === 1) socket.send(JSON.stringify({ type: "release" }));
        if (refused) {                              // we were blanked → resync a clean world now we're out of AR
          refused = false; hideInfo();
          if (socket) socket.close();               // onclose auto-reconnects → a fresh snapshot re-renders it
        }
      });
    }
  });
})();
