// Conjure WebXR client.
// Connects to the world server's state channel, renders the snapshot, and applies patches live by
// mapping the declarative world model onto A-Frame entities/components.
// See docs/architecture.md §3 (channels), §4 (world model), §5 (patch protocol); docs/specs/worlds-surfaces.md
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

  // Compact wire form for the geometry worker (fix/pops-and-jitters): send only the fields RoomSnap.register
  // reads, as plain numbers, so a capture's planes + reference constellation cross to the worker in a few KB.
  function serCur(c) { var p = c.pos; return { p: [p.x, p.y, p.z], nyaw: c.nyaw, sem: c.sem, orient: c.orient, ext: [c.ext[0], c.ext[1]] }; }
  function serRef(r) { var p = r.pos; return { id: r.id, p: [p.x, p.y, p.z], nyaw: r.nyaw, sem: r.sem, orient: r.orient, ext: [r.ext[0], r.ext[1]] }; }

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
      // yaw: keep the entity upright and rotate only about the vertical axis — so a tall free-standing
      //   picture faces you without tipping its top toward you when you look up/down. Default off = full
      //   face-camera lookAt, which is what the surface labels want.
      schema: { yaw: { type: "boolean", default: false } },
      tick: function () {
        var cam = this.el.sceneEl && this.el.sceneEl.camera;
        if (!cam) return;
        this._t = this._t || new AFRAME.THREE.Vector3();
        cam.getWorldPosition(this._t);
        if (this.data.yaw) {                 // flatten the target to our own height → look stays horizontal
          this._w = this._w || new AFRAME.THREE.Vector3();
          this.el.object3D.getWorldPosition(this._w);
          this._t.y = this._w.y;
        }
        this.el.object3D.lookAt(this._t);   // A-Frame text reads correctly after lookAt (no flip)
      },
    });
  }

  // Show a packed stereo image (side-by-side or top-bottom) with true per-eye depth in XR. The trick is
  // render layers: three.js gives the left XR eye-camera layers {0,1} and the right {0,2}. We build two
  // half-UV eye meshes (left→layer 1, right→layer 2) so each eye sees its own half; the original
  // full-texture mesh stays on layer 0 for the 2D/desktop view and is parked on an unused layer while
  // presenting. All local to each client — never touches shared state, so it's correct for multi-user.
  if (window.AFRAME && !AFRAME.components.stereo) {
    AFRAME.registerComponent("stereo", {
      schema: { layout: { default: "sbs" } },   // "sbs" (left|right) | "tb" (top/bottom, top = left eye)
      init: function () {
        this._maybeBuild = this._maybeBuild.bind(this);
        this._sync = this._sync.bind(this);
        // Build once BOTH the mesh (object3dset) and its texture (materialtextureloaded) exist; either can
        // arrive first, and the mesh can be replaced later (geometry rebuild), so we listen to both and
        // rebuild idempotently. Scene listeners are added here ONCE and only removed in remove().
        this.el.addEventListener("object3dset", this._maybeBuild);
        this.el.addEventListener("materialtextureloaded", this._maybeBuild);
        var sc = this.el.sceneEl;
        if (sc) { sc.addEventListener("enter-vr", this._sync); sc.addEventListener("exit-vr", this._sync); }
        this._maybeBuild();
      },
      update: function () { this._teardown(); this._maybeBuild(); },   // e.g. layout changed → rebuild
      remove: function () {
        this.el.removeEventListener("object3dset", this._maybeBuild);
        this.el.removeEventListener("materialtextureloaded", this._maybeBuild);
        var sc = this.el.sceneEl;
        if (sc) { sc.removeEventListener("enter-vr", this._sync); sc.removeEventListener("exit-vr", this._sync); }
        this._teardown();
      },
      _teardown: function () {
        if (this._eyes) this._eyes.forEach(function (m) { if (m.parent) m.parent.remove(m); });
        this._eyes = null; this._builtMesh = null;
      },
      _maybeBuild: function () {
        var mesh = this.el.getObject3D("mesh");
        if (!mesh || !mesh.material || !mesh.material.map) return;   // need the mesh AND a loaded texture
        if (this._builtMesh === mesh && this._eyes) { this._sync(); return; }  // already built for this mesh
        this._teardown();                                            // mesh was replaced → drop stale eyes
        var THREE = AFRAME.THREE, tb = this.data.layout === "tb", base = mesh.material.map;
        var halfMap = function (offX, offY) {          // a texture clone sampling just one eye's half
          var t = base.clone(); t.needsUpdate = true;
          t.repeat.set(tb ? 1 : 0.5, tb ? 0.5 : 1);
          t.offset.set(offX, offY);
          return t;
        };
        var eyeMesh = function (map) {
          var mat = mesh.material.clone(); mat.map = map;
          mat.side = THREE.FrontSide;   // NEVER double-sided: the back face renders mirrored → broken stereo
          mat.needsUpdate = true;
          var m = new THREE.Mesh(mesh.geometry, mat);   // shares geometry + transform with the base mesh
          mesh.add(m);
          return m;
        };
        // UV origin is bottom-left, so for top-bottom the LEFT eye (top half) is offset y = 0.5.
        this._eyes = [eyeMesh(halfMap(0, tb ? 0.5 : 0)), eyeMesh(halfMap(tb ? 0 : 0.5, 0))];
        this._builtMesh = mesh;
        this._sync();
      },
      _sync: function () {   // toggle full-mesh (2D) vs eye-meshes (XR) by which cameras' layers they sit on
        if (!this._eyes) return;
        var sc = this.el.sceneEl, mesh = this.el.getObject3D("mesh");
        // isPresenting is the canonical WebXR flag (true for immersive AR *and* VR); is('vr-mode') is a
        // fallback for A-Frame builds that set the state slightly differently.
        var presenting = !!(sc && ((sc.renderer && sc.renderer.xr && sc.renderer.xr.isPresenting) ||
                                   (sc.is && sc.is("vr-mode"))));
        if (presenting) {
          if (mesh) mesh.layers.set(3);          // park the full mesh where no eye-camera looks
          this._eyes[0].layers.set(1);           // left eye  (camera layers {0,1})
          this._eyes[1].layers.set(2);           // right eye (camera layers {0,2})
        } else {
          // desktop / 2D: show ONE eye (the left) as a normal single picture — the full mesh would
          // otherwise show the whole side-by-side pair squished onto the per-eye-shaped plane.
          if (mesh) mesh.layers.set(3);          // hide the packed full mesh
          this._eyes[0].layers.set(0);           // left eye → the mono camera (layer 0)
          this._eyes[1].layers.set(3);           // hide the right eye
        }
      },
    });
  }

  // ----------------------------------------------------------------- immersion / room state
  // Two axes (docs/specs/worlds-surfaces.md §3): passthrough (real room visible) × surface visibility.
  var presentation = { active: false, passthrough: false, defaultVisible: false,
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
    var num = (el.dataset.fid || el.id || "").match(/(\d+)$/);   // trailing number of the friendly/real id
    var text = "[" + (el.dataset.semantic || "surface") + (num ? " " + num[1] : "") + "]"
      + (presentation.annotationDims && el.dataset.ext ? "\n" + el.dataset.ext : "");
    var style = { value: text, color: presentation.annotationColor, opacity: presentation.annotationOpacity };
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
    var fill = presentation.active && (explicit != null ? explicit === "true" : presentation.defaultVisible);
    el.setAttribute("visible", presentation.active);
    el.setAttribute("fill-visible", fill);
  }

  // The surface outline's color/alpha/visibility — global room display state, independent of the fill.
  function applyEdgeStyle(el) {
    el.setAttribute("surface-edges", { color: presentation.edgeColor,
      opacity: presentation.edgeOpacity, visible: presentation.edgesVisible });
  }

  function applyImmersion() {
    if (refused || awaitingSpace || evicted) {   // blanked to passthrough: hide everything renderable
      document.querySelectorAll("[data-scaffold]").forEach(function (el) { el.setAttribute("visible", false); });
      var sky0 = document.getElementById("sky"); if (sky0) sky0.setAttribute("visible", false);
      var g0 = document.getElementById("grounded-sky"); if (g0) g0.setAttribute("visible", false);
      return;
    }
    // The synthetic holodeck shell (grid floor/walls) + the void sky belong ONLY to an EMPTY "unbounded
    // VR" (room inactive AND no chosen skybox). Hide them whenever the room is active — AR passthrough or
    // a virtual room — OR a skybox IS the environment (an outdoor/void world), so the grid never competes
    // with the room or the sky. (In AR the void a-sky would also occlude passthrough, so it's hidden too.)
    var inRoom = presentation.active;
    var showScaffold = !inRoom && !presentation.skybox && !presentation.grounded;   // holodeck only in a bare void
    document.querySelectorAll("[data-scaffold]").forEach(function (el) {
      el.setAttribute("visible", showScaffold);
    });
    // Exception: a custom skybox IMAGE *is* the chosen environment, so keep it visible even with the
    // room active — its opaque sphere deliberately wraps/occludes passthrough so you see the skybox,
    // not the physical room. Only the void color sky is restricted to unbounded VR. A GROUNDED skybox
    // replaces the plain sphere with a ground-projected dome, so when it's active hide the sphere and
    // show the grounded mesh instead (it likewise wraps the scene whenever the room is active).
    var sky = document.getElementById("sky");
    if (sky) sky.setAttribute("visible", !presentation.grounded && (presentation.skybox || !inRoom));
    var grounded = document.getElementById("grounded-sky");
    if (grounded) grounded.setAttribute("visible", presentation.grounded);
    var reals = document.querySelectorAll("[data-real]");
    reals.forEach(function (el) {
      applyRealVisibility(el);
      applyEdgeStyle(el);
      setSurfaceLabel(el, presentation.annotations);
    });
    console.log("[conjure] immersion: active=" + presentation.active + " annotations=" +
      presentation.annotations + " surfaces=" + reals.length);
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
    // Fill weld (--surface-weld, default 2 mm): inflate ONLY the fill by `weld` (split evenly per side) so two
    // independently-triangulated surfaces that ABUT overlap by a hair instead of leaving a float-rounding
    // crack the passthrough flickers through ("noisy static"). The overlap tucks behind the neighbour (a
    // wall's extra mm pokes above the ceiling / behind the perpendicular wall), so it's hidden. The wireframe
    // outline (surface-edges) keeps the TRUE w/h — no overshoot there — so wireframe is unaffected. 0 = off.
    var weld = +window.CONJURE_SURFACE_WELD || 0, fw = w + weld, fh = h + weld;
    if (hs) el.setAttribute("geometry", { primitive: "holed-wall", width: fw, height: fh, holes: hs });
    else el.setAttribute("geometry", { primitive: "plane", width: fw, height: fh });
    el.setAttribute("surface-edges", { width: w, height: h });   // outline at TRUE size (not welded)
  }

  // Time-sliced geometry rebuild (branch fix/pops-and-jitters). applySurfaceGeometry re-triangulates the
  // (holed-)wall mesh — the one expensive per-surface op left in the render. When many surfaces need it in
  // ONE capture (first lay of the whole room, or several shapes crossing tolerance at once) it lands as a
  // ~7-14 ms frame → a dropped frame → the last capture-caused jitter. So `applyEntity` ENQUEUES a rebuild
  // (pose is still applied immediately — positions are always correct) and `pumpGeo`, run every frame from
  // tick, drains a FEW per frame under a small time budget. Meshes materialize progressively over ~100-170 ms
  // instead of one hitch; nothing else depends on the mesh (content placement + snapping use pose/data), so
  // the deferral is purely cosmetic. Coalesced by id (latest comps win); disconnected surfaces are skipped.
  var geoRebuilds = 0;      // [jitter] count of applySurfaceGeometry calls since last PACE (mesh-rebuild rate)
  var geoQueue = [];        // ids awaiting a mesh rebuild (FIFO order of first enqueue)
  var geoPending = {};      // id -> latest comps to rebuild with (coalesced; a re-enqueue just refreshes this)
  var surfDumped = false;   // [surf] diagnostic (--debug-registration): dumped once per room entry (see _renderLocal)
  // Pose-smoothing slew (docs/specs/spaces-geometry.md §9.2): entities currently EASING their transform toward a
  // captured target pose (surfaces AND the content glued to them), so slewPoses walks only what's moving —
  // an idle room adopts no targets and the set stays empty (zero steady-state cost). Cleared on world switch.
  var slewSet = new Set();
  var SLEW_POS_EPS = 0.001;                 // 1 mm  — snap the last sliver exactly + drop from slewSet (§4)
  var SLEW_ANG_EPS = 0.1 * Math.PI / 180;   // ~0.1° — same, for orientation
  var geoBacklogWarnAt = 0; // performance.now() of the last backlog warning (throttle)
  // Enqueue a surface for a deferred mesh rebuild. Coalesced by id: a surface that re-shapes again before the
  // pump reaches it keeps its queue slot and just refreshes its target comps — churn can't inflate the queue
  // or rebuild the same surface twice for one net change.
  function enqueueGeo(el, comps) {
    if (!geoPending[el.id]) geoQueue.push(el.id);
    geoPending[el.id] = comps;
  }
  // Drain the mesh-rebuild queue a few per frame under a per-frame time budget, so a whole-room
  // re-triangulation (first lay of N surfaces, or many shapes crossing tolerance at once) spreads across
  // frames instead of overrunning one and dropping it (docs/specs/spaces-geometry.md §9). Called every frame
  // from tick.
  //   budget  = window.CONJURE_GEO_SLICE_MS (server --geo-slice-ms; default 3 ms). <=0 ⇒ Infinity: drain the
  //             whole queue each frame — slicing OFF (the pre-slice / A-B-baseline behaviour).
  //   backlog = if the queue is deeper than CONJURE_GEO_BACKLOG_WARN (default 256) the pump is falling behind
  //             (rebuilds arriving faster than the budget drains them → materialization lag at scale). We LOG
  //             it rather than silently lag — the lever is a bigger budget or fewer rebuilds. Throttled to
  //             once / 2 s; a one-off big first lay can trip it, a *persistent* warning is the real signal.
  //             §14 "scaling" names the next levels (element creation, matchRef) this pump does NOT cover.
  function pumpGeo() {
    if (!geoQueue.length) return;
    var budget = window.CONJURE_GEO_SLICE_MS;
    if (budget == null) budget = 3;
    if (budget <= 0) budget = Infinity;              // slicing disabled → rebuild the whole queue this frame
    var warn = window.CONJURE_GEO_BACKLOG_WARN; if (warn == null) warn = 256;
    var t0 = performance.now();
    if (geoQueue.length > warn && t0 - geoBacklogWarnAt > 2000) {
      geoBacklogWarnAt = t0;
      debugLog("geo", "mesh-rebuild backlog " + geoQueue.length + " surfaces — pump behind (raise "
        + "--geo-slice-ms or reduce rebuilds); materializing progressively", true);
    }
    var JITg = window.CONJURE_DEBUG_JITTER;              // [geoslow] probe: name a single rebuild that overruns
    do {
      var id = geoQueue.shift(), comps = geoPending[id];
      delete geoPending[id];
      var el = document.getElementById(id);
      if (el && el.isConnected && comps) {
        var st = JITg ? performance.now() : 0;
        applySurfaceGeometry(el, comps); geoRebuilds++;  // the expensive holed-wall re-triangulation (counted for PACE)
        setSurfaceLabel(el, presentation.annotations);      // refresh dims: label reads dataset.ext, just set above
        // The pump caps cost BETWEEN surfaces, but the do..while always finishes the CURRENT one — so a single
        // heavy rebuild (a holed wall / wall art) can overshoot the whole slice in one frame. Log which surface
        // and how long when it exceeds the budget, so a "pop on wall art" can be pinned to a specific rebuild.
        if (JITg) {
          var ms = performance.now() - st;
          if (ms > budget) debugLog("geoslow", id + " rebuild=" + ms.toFixed(1) + "ms holes="
            + ((comps.surface && comps.surface.holes || []).length) + " (budget=" + budget + "ms)", true);
        }
      }
    } while (geoQueue.length && (performance.now() - t0) < budget);
  }

  // Pose-smoothing (docs/specs/spaces-geometry.md §9.2): ADOPT a captured pose as an entity's target and enqueue
  // it to ease there over frames (slewPoses), instead of snapping. `pos`/`quat` are THREE Vector3/Quaternion
  // (cloned in, since callers reuse scratch). Called only when smoothing is on and the entity isn't fresh —
  // the fresh/disabled paths snap at the call site so nothing eases in from the origin.
  function adoptTarget(el, pos, quat) {
    if (!el._tgtPos) { el._tgtPos = pos.clone(); el._tgtQuat = quat.clone(); }
    else { el._tgtPos.copy(pos); el._tgtQuat.copy(quat); }
    el._settled = false;
    slewSet.add(el);
  }

  // Place director / on-surface content at a solved pose (§5.5): SNAP on the first placement (nothing to
  // ease from) and whenever smoothing is off; otherwise adopt it as a slew target so content eases in
  // lock-step with the surface it's glued to. Content anchoring still solves against the surfaces' TARGET
  // (capture) poses upstream — this only governs HOW the solved pose is written to object3D, snap vs ease.
  function placeContent(el, pos, quat) {
    // Content apply-gate — the SAME deadband the walls use (docs/specs/spaces-geometry.md §9.1). _placeContent
    // re-solves every capture against the RAW, ungated, sensor-noisy plane basis, so with no gate the solved
    // pose wanders a few mm each capture and content shimmers while the gated walls sit dead-still (measured:
    // the sampled content's world pos drifted in a ~5-6 mm envelope while the wall's was frozen to 4 dp).
    // Hold unless the newly-solved pose moved past tolerance from the last COMMITTED pose, so content is as
    // stable as the walls and only re-places on a real move. Reusing CONJURE_APPLY_TOL means content and
    // walls share one deadband ⇒ they agree: both move together past tolerance, or neither moves.
    if (el._contentPlaced) {
      var tol = window.CONJURE_APPLY_TOL || {};
      var pT = tol.pos != null ? tol.pos : 0.02, rT = (tol.rotDeg != null ? tol.rotDeg : 1) * Math.PI / 180;
      if (pos.distanceTo(el._placedPos) <= pT && quat.angleTo(el._placedQuat) <= rT) return;   // sub-tolerance → hold
    }
    var tau = +window.CONJURE_POSE_TAU || 0;
    if (tau > 0 && el._contentPlaced) adoptTarget(el, pos, quat);
    else { el.object3D.position.copy(pos); el.object3D.quaternion.copy(quat); slewSet.delete(el); el._settled = true; }
    if (!el._placedPos) { el._placedPos = pos.clone(); el._placedQuat = quat.clone(); }   // baseline the gate compares against
    else { el._placedPos.copy(pos); el._placedQuat.copy(quat); }
    el._contentPlaced = true;
  }

  // The pose-follow clock (docs/specs/spaces-geometry.md §9.2): every frame, ease each unsettled entity's
  // object3D toward its adopted target by the frame-rate-independent fraction a = 1 - exp(-dt/tau), and drop
  // it the instant it arrives (epsilon snap) so steady-state cost returns to zero. `dt` is seconds. Writes
  // object3D directly (NOT setAttribute) — a 90 Hz setAttribute would re-parse strings + fire change events
  // per frame; the apply-gate keys off el._geoSig, not the DOM attribute, so direct writes are safe (§5.3).
  // Content already writes object3D directly, so this is consistent. tau<=0 means nothing was ever adopted
  // (call sites snap), so the set is empty and this is a no-op.
  function slewPoses(dt) {
    if (!slewSet.size) return;
    var tau = +window.CONJURE_POSE_TAU || 0;
    var a = WM.slewAlpha(dt, tau);
    if (!(a > 0)) return;                              // dt==0 (first frame) → no progress; keep targets
    slewSet.forEach(function (el) {
      var o = el.object3D;
      if (!o || !el.isConnected || !el._tgtPos) { slewSet.delete(el); return; }   // pruned/dead → drop (§10)
      o.position.lerp(el._tgtPos, a);
      o.quaternion.slerp(el._tgtQuat, a);              // shortest-arc; handles quaternion double-cover
      if (WM.slewSettled(o.position.distanceTo(el._tgtPos), o.quaternion.angleTo(el._tgtQuat),
          SLEW_POS_EPS, SLEW_ANG_EPS)) {
        o.position.copy(el._tgtPos); o.quaternion.copy(el._tgtQuat);   // snap the last sliver exactly
        el._settled = true; slewSet.delete(el);
      }
    });
  }

  // A real surface's MATERIAL — cheap, director-driven, never rebuilds the mesh, so it is applied every
  // time (NOT gated). Kept separate from geometry above.
  function applySurfaceMaterial(el, comps) {
    var mat = Object.assign({ shader: "flat", side: "double" }, comps.material || {});
    if ("visible" in mat) { el.dataset.matVisible = String(mat.visible); delete mat.visible; }
    el.setAttribute("material", mat);
  }

  // The room's stable planes (floor + walls) as PlaneAnchor.Plane[], for placing content via plane-relative
  // anchors (docs/specs/spaces-geometry.md §5.3). Two sources, keyed by the SAME shared surface ids:
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
      // Render apply-gate (docs/specs/spaces-geometry.md §9.1), POSE/SHAPE-split: sub-tolerance re-derivation
      // still doesn't touch the entity (the "pop"), but above tolerance we now separate a cheap POSE re-lay
      // (setAttribute position/rotation — a drifted surface, same shape) from the expensive SHAPE rebuild
      // (applySurfaceGeometry re-triangulates the holed wall — only when extent/openings change). Pose drift
      // under tracking refinement is the common case; gating the mesh rebuild out of it is what removes the
      // per-capture whole-room re-triangulation spike (the walking jitter). The group relay sets BOTH
      // `_forcePoseRelay` and `_forceGeoRelay` so junction surfaces re-lay pose AND geometry at one epoch
      // (exact seam closure); that co-relayed rebuild is absorbed by the time-slice pump, not dropped-frame.
      // el._geoSig remembers the last-applied signature across patches (id is stable → same element).
      var sig = WM.surfaceSig(t, comps.surface), _tol = window.CONJURE_APPLY_TOL, _fresh = !el._geoSig;
      var poseMoved = _fresh || el._forcePoseRelay || WM.surfacePoseMoved(AFRAME.THREE, el._geoSig, sig, _tol);
      // `_forceGeoRelay` (group relay) re-triangulates even on a SUB-tolerance shape drift, so a wall's
      // center and its joinCorners width materialize at the SAME epoch → junctions close exactly (part B).
      var shapeChanged = _fresh || el._forceGeoRelay || WM.surfaceShapeChanged(el._geoSig, sig, _tol);
      if (poseMoved) {
        // Pose-smoothing (docs/specs/spaces-geometry.md §9.2, §5.3): split ADOPT from MOVE. When smoothing is on
        // (CONJURE_POSE_TAU>0) and this isn't the surface's first lay, store the captured pose as a TARGET and
        // let slewPoses ease the transform there over frames — the ~2 s drift STEP becomes a short settle.
        // FRESH surfaces (and the tau=0 default) snap immediately: a brand-new entity has no sensible pose to
        // ease FROM (it would fly in from the origin), and tau=0 is today's behaviour. Scale is never eased.
        var _tau = +window.CONJURE_POSE_TAU || 0;
        if (_tau > 0 && !_fresh && el.object3D && t.position && t.rotation) {
          adoptTarget(el, new AFRAME.THREE.Vector3(t.position[0] || 0, t.position[1] || 0, t.position[2] || 0),
            eulerYXZToQuat(AFRAME.THREE, t.rotation));
          if (t.scale) el.setAttribute("scale", v3(t.scale));
        } else {
          if (t.position) el.setAttribute("position", v3(t.position));
          if (t.rotation) el.setAttribute("rotation", v3(t.rotation));
          if (t.scale) el.setAttribute("scale", v3(t.scale));
          slewSet.delete(el);                      // a snap supersedes any in-flight ease (fresh lay / tau off)
        }
      }
      if (shapeChanged) enqueueGeo(el, comps);   // expensive mesh re-triangulation → deferred, time-sliced (pumpGeo)
      // Advance the baseline only for the half we re-laid: shape → full advance (the slice queue materializes
      // it); pose-only → keep the last-RENDERED shape, so sub-tolerance extent drift can't run the baseline
      // away un-drawn (the wall∩ceiling seam regression — see WM.advanceSig).
      if (poseMoved || shapeChanged) el._geoSig = WM.advanceSig(el._geoSig, sig, poseMoved, shapeChanged);
      el._forcePoseRelay = false; el._forceGeoRelay = false;
      // Styling gate: visibility/edges/label are GLOBAL display state, and director material is per-surface —
      // none change per capture. A display-setting toggle re-applies visibility/edges/label to EVERY surface
      // via applyImmersion(), so here we only (re)style on first lay (`_fresh`), on a dims change (the label
      // shows extent when annotationDims is on), or when THIS surface's material actually changed. Running all
      // four every capture for every surface was ~9 ms of setAttribute churn (material diff, edge-geometry +
      // SDF-text rebuilds) — the second per-capture budget sink after the mesh rebuild.
      var matSig = JSON.stringify(comps.material || null);
      if (_fresh || el._matSig !== matSig) { applySurfaceMaterial(el, comps); el._matSig = matSig; }
      if (_fresh) { applyRealVisibility(el); applyEdgeStyle(el); }
      if (_fresh || shapeChanged) setSurfaceLabel(el, presentation.annotations);
      return;
    }
    // Stash the surface home id (if any) so the `grab` module can tell surface-attached content (constrain
    // to the plane) from free content (6DOF). Meta isn't otherwise mirrored onto the DOM. `placement`
    // likewise: "grounded" content re-solves onto the LOCAL floor every capture, so grab must keep it ON
    // the floor — a 6DOF drag of a grounded model would just be undone by the next solve.
    if (meta.on_surface) el.dataset.onSurface = meta.on_surface; else delete el.dataset.onSurface;
    if (meta.placement) el.dataset.placement = meta.placement; else delete el.dataset.placement;
    if (t.position) el.setAttribute("position", v3(t.position));
    if (t.rotation) el.setAttribute("rotation", v3(t.rotation));
    if (t.scale) el.setAttribute("scale", v3(t.scale));
    Object.keys(comps).forEach(function (name) { el.setAttribute(name, comps[name]); });
    // Director-placed content: remember its AUTHORED (F_ref) pose so the capture tick can re-place it via a
    // plane-relative anchor solved against the LOCAL walls (docs §5). In a captured room #world-root is
    // identity, so the raw F_ref pose would be wrong; _placeContent corrects it. Scaffold is excluded.
    if (!meta.scaffold && t.position) el._frefPose = { position: t.position, rotation: t.rotation || [0, 0, 0] };
    el._onSurface = meta.on_surface || null;   // content pinned to a surface rides that surface locally (§5a)
    el._placement = meta.placement || "free";  // "grounded" (Y snapped to floor, upright) | "free" (full 3-D) — §5b/c
    el._anchor = meta.anchor || null;          // server-authored plane-relative anchor (§7c) — solved as-is locally
    el._surfaceOffset = meta.surface_offset || null;  // §7c-B2: on-surface content's host-local offset {p,q} to ride
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
        presentation.skybox = true;
        presentation.grounded = true;
      } else if (env.sky && env.sky.src) {
        // 360 equirectangular image: set the full material so the texture isn't tinted and renders
        // on the inside of the sky sphere. Mark a custom skybox so immersion keeps it visible (it
        // wraps the scene even when the room is active — see applyImmersion).
        sky.setAttribute("material", { shader: "flat", side: "back", color: "#FFFFFF", src: env.sky.src });
        if (groundedSky) groundedSky.setAttribute("grounded-sky", { src: "" });   // tear down any grounded dome
        presentation.skybox = true;
        presentation.grounded = false;
      } else {
        var color = (env.sky && env.sky.color) || env.background;
        if (color) sky.setAttribute("material", { shader: "flat", side: "back", color: color, src: "" });
        if (groundedSky) groundedSky.setAttribute("grounded-sky", { src: "" });
        presentation.skybox = false;   // back to the void color sky → only shows in unbounded VR
        presentation.grounded = false;
      }
    }
    if (env.fog) document.querySelector("a-scene").setAttribute("fog", env.fog);
    // room / immersion (merge — patches may carry only one field)
    if ("passthrough" in env) presentation.passthrough = !!env.passthrough;
    if (env.spacePresentation) {
      if ("active" in env.spacePresentation) presentation.active = !!env.spacePresentation.active;
      if ("defaultSurfaceVisible" in env.spacePresentation) presentation.defaultVisible = !!env.spacePresentation.defaultSurfaceVisible;
      if ("annotations" in env.spacePresentation) presentation.annotations = !!env.spacePresentation.annotations;
      if ("annotationDims" in env.spacePresentation) presentation.annotationDims = !!env.spacePresentation.annotationDims;
      if ("annotationColor" in env.spacePresentation) presentation.annotationColor = env.spacePresentation.annotationColor;
      if ("annotationOpacity" in env.spacePresentation) presentation.annotationOpacity = +env.spacePresentation.annotationOpacity;
      if ("edgesVisible" in env.spacePresentation) presentation.edgesVisible = !!env.spacePresentation.edgesVisible;
      if ("edgeColor" in env.spacePresentation) presentation.edgeColor = env.spacePresentation.edgeColor;
      if ("edgeOpacity" in env.spacePresentation) presentation.edgeOpacity = +env.spacePresentation.edgeOpacity;
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
  // Two-stage space selection (specs/spaces.md §6). On entering AR we report our coarse location and get
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
  // Admission gate + occupancy (specs/spaces.md §6.2/§6.3). `clientId` identifies this page-load to the
  // server so its select commits once (GPS jitter can't re-vote). `amHolding` = we passed the co-location
  // gate and are HOLDING the active space; we tell the server (`hold` over /ws) so it counts us as
  // occupying it — and re-tell it after a ws reconnect. On refusal we hide content and stay in passthrough.
  var clientId = "c_" + Math.random().toString(36).slice(2, 8), amHolding = false, refused = false;
  // Bumped out of a now-PRIVATE live session (§6c). Distinct from `refused` (co-location): a session
  // eviction blanks to passthrough but a fresh snapshot (re-admit on go-public) clears it and re-renders.
  var evicted = false;
  // While an AR headset is deciding WHICH space it's in (from enter-vr until /space/select resolves), we
  // blank to passthrough and show a "finding your space" notice — so a headset never renders the provisional
  // booted world (or anyone else's) misaligned to the real room before it has established/joined its space.
  var awaitingSpace = false, lastWorld = null;

  function applySnapshot(world) {
    if (refused) return;                 // we're not in this space — stay blanked to passthrough (steps 4/7)
    if (evicted) {                       // re-admitted to a now-public session (§6c) → un-blank + re-render
      evicted = false;
      hideHeadsetMessage(); hideInfo();
    }
    if (awaitingSpace) {                 // a snapshot = selection resolved → un-blank BEFORE rendering, so
      awaitingSpace = false;             // applyImmersion (below) sets the world up instead of hiding it
      hideHeadsetMessage(); hideInfo();
    }
    // Key on the world's permanent ID, not its name: renaming a world would otherwise look like a world
    // SWITCH and needlessly reset the room capture frame.
    var key = worldOwner + "/" + (world && (world.id || world.name));
    if (key !== lastWorldKey) {          // WORLD SWITCH → drop the previous room's capture frame so the next
      lastWorldKey = key;                // capture seeds/establishes for THIS world, not the last one
      var sc = document.querySelector("a-scene");
      var rc = sc && sc.components && sc.components["room-capture"];
      if (rc && rc.resetFrame) rc.resetFrame();
    }
    root().innerHTML = "";
    geoQueue = []; geoPending = {}; surfDumped = false;   // world switch destroyed every surface el → drop stale rebuild entries + re-arm the [surf] dump
    slewSet.clear();                                      // …and every easing target: the entities are gone
    // LOCAL-FIRST: once a client renders its own capture (localRenderActive), real surfaces are NOT drawn
    // from the server — each headset draws its OWN live capture (_renderLocal), matching its passthrough. We
    // still consume the server's real surfaces below (docSurfaces) as the registration REFERENCE. A desktop
    // viewer (never captures) keeps rendering the server's surfaces. Non-real entities always render here.
    (world.entities || []).forEach(function (e) {
      if (localRenderActive && e.meta && e.meta.real) return;
      applyEntity(e);
    });
    // A SNAPSHOT is the COMPLETE world state, unlike an env PATCH (which merges only the fields it
    // carries — `applyEnv` is written for that merge). So a snapshot whose environment has no `room`
    // block means THIS world has no room at all (an outdoor/void world), and the room flags must RESET
    // rather than inherit the previous world's. Without this, switching from a captured room to an
    // outdoor world left `presentation.active` true, so the last room's surfaces (locally-rendered ones
    // included — applyImmersion drives them via [data-real]) kept drawing over the outdoor world.
    if (!(world.environment || {}).spacePresentation) {
      presentation.active = false;
      presentation.passthrough = false;
    }
    applyEnv(world.environment);   // after entities, so immersion can toggle them
    isVoidWorld = ((world.environment || {}).space === VOID_SPACE);
    var reals = (world.entities || []).filter(function (e) { return e.meta && e.meta.real; });
    surfaceStyles = {};                  // rebuild the shared styling map for THIS world (id → material)
    reals.forEach(function (e) { var m = (e.components || {}).material; if (m) surfaceStyles[e.id] = m; });
    // Seed material for the room frame on reload (see the capture at ~L794). CLEAR it when a snapshot
    // carries no real surfaces — otherwise switching into an empty/void world (or a DIFFERENT room)
    // would leave the PREVIOUS room's surfaces here, and the next capture could register into the wrong
    // frame (specs/spaces.md §6: the Harold's-house cross-room seeding). Empty ⇒ nothing to seed.
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
    // The server moved content whose pose we own locally → re-solve from the NEW F_ref pose right now, so
    // it lands correctly instead of showing the raw F_ref pose until the next capture.
    if ((path === "transform.position" || path === "transform.rotation") && contentPoseIsLocal(el))
      solveContentNow(el);
    if (path === "meta.anchor") {                              // server re-anchored (moved) content (§7c)
      el._anchor = value || null;
      solveContentNow(el);              // land it NOW in our own frame, not at the next capture
    }
    // ANCHORED content's rendered pose belongs to the local anchor solve (_placeContent / solveContentNow):
    // the server stores F_ref, we render F_track, so applying its raw transform literally teleports the
    // object by this client's registration offset until the next capture corrects it — the "disappears for
    // a second, then comes back" flicker after a grab. Keep the anchor SOURCE (above) and skip the render.
    // Same principle as the GEO_PATHS gate for real surfaces: local render owns local geometry.
    // …which covers every kind _placeContent owns: anchored, surface-attached (host · surface_offset), AND
    // plain free-standing content solved from its F_ref pose. Missing that last case is why a free-standing
    // image still flashed to the wrong spot on release. When there's NO local basis (void/outdoor world)
    // contentPoseIsLocal is false and the server transform is applied as usual.
    if (contentPoseIsLocal(el) && (path === "transform.position" || path === "transform.rotation")) return;
    if (path === "meta.surface_offset") {                      // §7c-B2 re-anchor
      el._surfaceOffset = value || null;
      solveContentNow(el);              // land it on the host NOW, not at the next capture
    }
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
    if (refused || awaitingSpace || evicted) {         // ignore world updates while blanked to passthrough
      debugLog("patch", "DROPPED rev " + (patch && patch.rev) + " ops=" + ((patch && patch.ops) || []).length
        + " (refused=" + refused + " awaitingSpace=" + awaitingSpace + " evicted=" + evicted + ")");
      return;
    }
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
    presentation.skybox = false; presentation.grounded = false; presentation.active = false;
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
    if (lastWorld) applySnapshot(lastWorld);   // immediate: show the last-known world…
    requestResync();                           // …then pull the CURRENT store, so any patch dropped while
                                               // blanked (content added/removed during selection) is recovered
  }

  // Ask the server to re-send the current snapshot. Used after a blanked window (space selection) clears
  // without a fresh snapshot, so world updates that arrived while we were dropping patches aren't lost.
  function requestResync() {
    if (socket && socket.readyState === 1) socket.send(JSON.stringify({ type: "resync" }));
  }

  // --- presence (Phase 4 §7): show the other users as a sphere-on-box avatar, co-located in the shared
  // reference frame (#world-root), and broadcast our own head/camera pose ~10 Hz. Pose is expressed in
  // #world-root's local frame so it aligns for everyone (AR: world-root is parked at the registered
  // frame; desktop: world-root is at identity, so it's just the camera pose).
  var socket = null, R_AV = 0.13, GAP_AV = 0.03, worldOwner = null, guestSpawned = false;
  // Set once the immersive-ar probe answers (setupARButton). A headset reports true BEFORE it enters
  // a session, which is exactly the window the desktop-guest spawn used to fire in.
  var arCapable = false;
  var ORIGIN = null;   // AFRAME.THREE.Vector3(0,0,0), built lazily — AFRAME isn't up at parse time
  // Last capture's plane bases: local walls (F_track) + seed walls (F_ref). Cached by _placeContent so
  // ConjureFrames.toRef can invert its solve for interaction modules.
  var framePlanes = { local: null, ref: null };

  // Minimal shared-event bus for dynamic modules (docs/specs/dynamics.md §6, tier B). `emitShared`
  // relays an event to PEERS via the ws (the server fans it to the OTHER clients); on()/off() subscribe;
  // inbound `module_event` messages are dispatched to subscribers. A module acts on its OWN input
  // immediately (local); this bus carries only the shared, cross-client traffic (e.g. water touches).
  // Frame bridge for INTERACTION modules (grab, and anything else that lets a user drag content).
  // In a captured room #world-root is identity, so a pose you SEE is in this client's LOCAL frame
  // (F_track) — but the server persists poses in the SEED frame (F_ref) and re-solves content from it
  // every capture (_placeContent). Committing a dragged F_track pose raw therefore makes the object JUMP
  // to wherever that solve lands. `toRef` is the exact inverse of that solve: author a plane-relative
  // anchor from the dragged pose against the LOCAL walls, then solve it against the SEED walls.
  // Returns null when there's no basis (no room captured yet / void world) — the frames coincide there,
  // so the caller should commit the raw pose.
  // Re-place ONE anchored entity right now, using the plane basis cached by the last capture — instead of
  // waiting up to a capture interval for _placeContent to come round. Used when the server sends a new
  // meta.anchor (a grab commit, a director move) so the object lands correctly on the spot, with no
  // intermediate wrong pose. Returns false when there's no basis yet (caller leaves the pose alone).
  // Re-place ONE piece of content right now with the basis the last capture cached, instead of waiting for
  // the pump to come round. Used when a server update changes what its pose derives from, so it lands
  // correctly on the spot with no intermediate wrong pose.
  function solveContentNow(el) {
    var r = contentPose(el, framePlanes.local, framePlanes.ref, false);
    if (!r) return false;
    placeContent(el, r.position, r.quaternion);
    return true;
  }

  // Is this element's rendered pose owned by the local solve (_placeContent) rather than by the server's
  // raw transform? True for director-placed content once we have a basis to solve against: surface-attached
  // content needs its host rendered; anchored/free content needs local walls. When there's no basis (a void
  // or outdoor world, or before the first capture) the server transform IS the pose, and must be applied.
  function contentPoseIsLocal(el) {
    if (!el._frefPose) return false;
    // Must match what contentPose can actually produce, or we'd suppress the server's transform and then
    // fail to compute a replacement — freezing the content at a stale pose.
    if (el._onSurface) return !!(el._surfaceOffset && document.getElementById(el._onSurface));
    return !!(framePlanes.local && framePlanes.local.length >= 2);
  }

  // Re-seat SURFACE-ATTACHED content on its host right now: content_world = host · surface_offset (the
  // same composition _placeContent's _onSurface branch does). Used when a new offset arrives so the
  // content lands on its wall immediately rather than after the next capture.
  // WHERE a piece of director-placed content belongs, in OUR frame. The single source of truth for content
  // placement: both the capture pump (_placeContent) and the immediate re-place after a server update go
  // through it, so the three kinds can't drift apart — which is what produced the same "flashes to the wrong
  // spot" bug three times (anchored, surface-attached, then free-standing).
  //   • surface-attached → host · surface_offset
  //   • anchored         → solve meta.anchor against our local walls
  //   • free (legacy)    → author from the F_ref pose against the seed walls, solve against ours
  // `useHostTarget` rides the host's slew TARGET instead of its current pose — the pump wants that so art
  // eases in lock-step with its wall; an immediate re-place wants what's on screen now.
  // Returns {position, quaternion, kind} or null when there's no basis (caller holds the current pose).
  function contentPose(el, localPl, refPl, useHostTarget) {
    var PA = window.PlaneAnchor, THREE = AFRAME.THREE;
    if (!PA || !el.object3D) return null;
    if (el._onSurface) {
      var hostEl = document.getElementById(el._onSurface), off = el._surfaceOffset;
      if (!hostEl || !hostEl.object3D || !off || !off.p || !off.q) return null;
      var slewing = useHostTarget && slewSet.has(hostEl) && hostEl._tgtPos;
      var hp = slewing ? hostEl._tgtPos : hostEl.object3D.position;
      var hq = slewing ? hostEl._tgtQuat : hostEl.object3D.quaternion;
      var m = new THREE.Matrix4().compose(hp.clone(), hq.clone(), new THREE.Vector3(1, 1, 1))
        .multiply(new THREE.Matrix4().compose(
          new THREE.Vector3(off.p[0], off.p[1], off.p[2]),
          new THREE.Quaternion(off.q[0], off.q[1], off.q[2], off.q[3]), new THREE.Vector3(1, 1, 1)));
      var p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
      m.decompose(p, q, s);
      return { position: p, quaternion: q, kind: "surface" };
    }
    if (!localPl || localPl.length < 2) return null;      // not enough local wall basis yet
    var sol;
    if (el._anchor) {
      sol = PA.solveAnchor(THREE, el._anchor, localPl);
    } else {
      var fp = el._frefPose;
      if (!fp || !refPl || refPl.length < 2) return null; // the legacy path needs the seed-wall basis too
      sol = PA.solveAnchor(THREE, PA.authorAnchor(THREE, {
        mode: el._placement || "free", quaternion: eulerYXZToQuat(THREE, fp.rotation),
        position: new THREE.Vector3(fp.position[0] || 0, fp.position[1] || 0, fp.position[2] || 0),
      }, refPl), localPl);
    }
    if (!sol || !sol.ok) return null;                     // degenerate / missing walls → hold last pose
    return { position: sol.position, quaternion: sol.quaternion, kind: el._anchor ? "anchored" : "legacy" };
  }

  window.ConjureFrames = {
    // The host-local offset (host⁻¹ · content) for surface-attached content, from poses in OUR frame.
    // Host-relative and therefore frame-independent: the server stores it verbatim and every client
    // re-applies it to its OWN rendered host pose. Computing it here (rather than letting the server derive
    // it from a committed F_ref position) keeps the drop exact — no wall-anchor round trip in between.
    surfaceOffset: function (hostObj, position, quaternion) {
      var THREE = AFRAME.THREE;
      hostObj.updateWorldMatrix(true, false);
      var inv = new THREE.Matrix4()
        .compose(hostObj.position.clone(), hostObj.quaternion.clone(), new THREE.Vector3(1, 1, 1)).invert();
      var m = inv.multiply(new THREE.Matrix4().compose(position, quaternion, new THREE.Vector3(1, 1, 1)));
      var p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
      m.decompose(p, q, s);
      return { p: [p.x, p.y, p.z], q: [q.x, q.y, q.z, q.w] };
    },
    // The plane-relative anchor for a pose in OUR frame, authored against the LOCAL walls. Anchors are
    // plane-relative (shared surface ids + offsets), so this is directly solvable by any client against
    // its own walls — send it to the server to store verbatim. Doing that beats committing a position and
    // letting the server re-author: local→ref→seed→local is four author/solve hops between plane sets that
    // are NOT rigidly related (that's the point of local-first geometry), and each hop leaves a little
    // residual — the object settling slightly off where it was dropped. Authoring here means the client
    // re-solves the SAME anchor against the SAME walls, which is exact.
    anchorFor: function (position, quaternion, mode) {
      var PA = window.PlaneAnchor, lp = framePlanes.local;
      if (!PA || !lp || lp.length < 2) return null;
      return PA.authorAnchor(AFRAME.THREE,
        { position: position, quaternion: quaternion, mode: mode || "free" }, lp);
    },
    toRef: function (position, quaternion, mode) {
      var PA = window.PlaneAnchor, lp = framePlanes.local, rp = framePlanes.ref;
      if (!PA || !lp || !rp || lp.length < 2 || rp.length < 2) return null;
      var anchor = PA.authorAnchor(AFRAME.THREE,
        { position: position, quaternion: quaternion, mode: mode || "free" }, lp);
      var sol = PA.solveAnchor(AFRAME.THREE, anchor, rp);
      return (sol && sol.ok) ? { position: sol.position, quaternion: sol.quaternion } : null;
    }
  };

  window.ConjureBus = {
    _subs: {},
    on: function (event, fn) { (this._subs[event] = this._subs[event] || []).push(fn); },
    off: function (event, fn) { var a = this._subs[event]; if (a) { var i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); } },
    emitShared: function (event, payload) {
      if (socket && socket.readyState === 1) socket.send(JSON.stringify({ type: "module_event", event: event, payload: payload }));
    },
    _dispatch: function (msg) { (this._subs[msg.event] || []).forEach(function (fn) { try { fn(msg); } catch (e) {} }); }
  };

  // Desktop-guest spawn (Phase 4 §6): a guest viewing on a desktop browser (no AR) isn't physically in
  // the space, so drop them just to the OWNER's right the first time the owner's pose arrives, then let
  // wasd/mouse take over. Desktop only (in AR the headset places you); #world-root is identity on
  // desktop, so the owner's world-frame pose is also the scene-frame position for the rig.
  function maybeSpawnGuest(ownerPose) {
    var sc = document.querySelector("a-scene");
    // `isPresenting` is the canonical WebXR flag (true for immersive AR *and* VR); is('vr-mode') is a
    // fallback for A-Frame builds that set the state slightly differently — same pairing as the eye
    // billboarding above. And `arCapable` is the load-bearing one: see WM.shouldSpawnGuest.
    var presenting = !!(sc && ((sc.renderer && sc.renderer.xr && sc.renderer.xr.isPresenting) ||
                               (sc.is && sc.is("vr-mode"))));
    if (!WM.shouldSpawnGuest({ spawned: guestSpawned, hasOwnerPose: !!(ownerPose && ownerPose.p),
                               me: currentUser(), owner: worldOwner,
                               presenting: presenting, arCapable: arCapable })) return;
    var rig = document.getElementById("rig"); if (!rig) return;
    var sp = WM.spawnRight(AFRAME.THREE, ownerPose, 1.2);   // 1.2 m to the owner's right, on the floor
    rig.object3D.position.set(sp[0], sp[1], sp[2]);
    guestSpawned = true;
  }

  // The rig MUST sit at the origin in a session (index.html): that's what aligns the A-Frame world frame
  // with the headset's reference space, so captured geometry lands around you instead of offset. Anything
  // that moved it — the desktop-guest spawn above, most likely — displaces the CAMERA while world content
  // and the raw-XR controller beams stay put, which reads as an out-of-body offset that never heals
  // (`guestSpawned` latches for the page). Re-assert the invariant on every session start so the whole
  // class of "something moved the rig" self-corrects instead of persisting.
  function resetRigForSession() {
    var rig = document.getElementById("rig");
    if (!ORIGIN) ORIGIN = new AFRAME.THREE.Vector3(0, 0, 0);
    if (rig && !rig.object3D.position.equals(ORIGIN)) {
      console.log("[conjure] rig was off-origin entering a session — reset", rig.object3D.position);
      rig.object3D.position.set(0, 0, 0);
    }
  }

  // The avatar entity is parked at the HEAD position and yawed to the headset's heading, so the body box
  // and head turn as one. Two info-color "eyes" sit half-embedded on the front of the head sphere, 45° apart
  // (±22.5° from the look direction); they live on a child entity that pitches with the head, so you can
  // read both the yaw (whole avatar turns) and the up/down gaze (eyes ride up/down the sphere).
  //var EYE_R = 0.045, EYE_S = Math.sin(Math.PI / 8), EYE_C = Math.cos(Math.PI / 8);   // 22.5°
  var EYE_R = 0.03, EYE_S = Math.sin(Math.PI / 12), EYE_C = Math.cos(Math.PI / 12); 
  function setAvatar(user, pose, anchor) {
    var THREE = AFRAME.THREE;
    var scA = document.querySelector("a-scene");
    var rcA = scA && scA.components && scA.components["room-capture"];
    var solved = false;
    // Prefer the plane-relative anchor solved against MY OWN local walls (§5.1) — the avatar then lands on
    // the same real walls I see, not a shared rigid frame. (headset ↔ headset)
    if (anchor && window.PlaneAnchor && rcA && rcA._localPlanes && rcA._localPlanes.length >= 2) {
      var sol = window.PlaneAnchor.solveAnchor(THREE, anchor, rcA._localPlanes);
      if (sol.ok) { pose = { p: [sol.position.x, sol.position.y, sol.position.z],
                             q: [sol.quaternion.x, sol.quaternion.y, sol.quaternion.z, sol.quaternion.w] };
                    solved = true; }
    }
    if (!pose || !pose.p) return;
    // Fallback pose is in the shared F_ref frame. On a HEADSET #world-root is identity and the scene is
    // F_track, so an anchor-less avatar (a desktop user has no walls to author one) must be brought
    // F_ref → F_track via T⁻¹ or it lands offset by the registration yaw. A desktop receiver has no T and
    // its scene already IS F_ref, so it uses the pose as-is.
    if (!solved && !isVoidWorld && rcA && rcA._haveT && rcA._Tmat) {   // captured room: world-root is identity
      var inv = rcA._Tmat.clone().invert();
      var fp = new THREE.Vector3(pose.p[0], pose.p[1], pose.p[2]).applyMatrix4(inv);
      var rot = new THREE.Quaternion(); inv.decompose(new THREE.Vector3(), rot, new THREE.Vector3());
      var q = pose.q || [0, 0, 0, 1];
      var fq = new THREE.Quaternion(q[0], q[1], q[2], q[3]).premultiply(rot);
      pose = { p: [fp.x, fp.y, fp.z], q: [fq.x, fq.y, fq.z, fq.w] };
    }
    var wr = document.getElementById("world-root"); if (!wr) return;
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
    var msg = { type: "presence", pose: { p: [p.x, p.y, p.z], q: [q.x, q.y, q.z, q.w] } };
    // Plane-relative head anchor (§5.1) for co-located AR avatars: author the head against MY OWN local
    // walls so each receiver re-solves it against ITS walls — the avatar lands on the SAME real walls I see,
    // with no shared rigid-frame error. Orientation is free (gaze pitches/rolls). The F_ref pose above stays
    // for the server's gaze/view_relative and for desktop receivers (no walls to solve against).
    if (rc && rc._localPlanes && rc._localPlanes.length >= 2 && window.PlaneAnchor) {
      var hp = new THREE.Vector3(), hq = new THREE.Quaternion(), hsc = new THREE.Vector3();
      cam.matrixWorld.decompose(hp, hq, hsc);            // head in F_track
      var anchor = window.PlaneAnchor.authorAnchor(THREE, { position: hp, quaternion: hq, mode: "free" }, rc._localPlanes);
      if (anchor.walls.length >= 2) msg.anchor = anchor;
    }
    socket.send(JSON.stringify(msg));
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
      // Delivery probe: log inbound world updates (not the high-frequency presence stream) so a
      // store↔client desync (director says added/removed but the scene didn't change) is localizable.
      if (msg.type !== "presence" && msg.type !== "presence_leave") {
        debugLog("ws", "recv " + msg.type + (
          msg.type === "patch" && msg.patch ? " rev " + msg.patch.rev + " ops=" + ((msg.patch.ops || []).length)
          : msg.type === "snapshot" && msg.world ? " rev " + msg.world.rev + " ents=" + ((msg.world.entities || []).length)
          : ""));
      }
      if (msg.type === "snapshot") { worldOwner = msg.owner || worldOwner; window.CONJURE_OWNER = worldOwner; applySnapshot(msg.world); }
      else if (msg.type === "patch") applyPatch(msg.patch);
      else if (msg.type === "info") showInfo(msg.msg);    // e.g. "'<world>' is private — ask <owner>…"
      else if (msg.type === "evicted") {                  // live session went private (§6c) → blank; a
        evicted = true; showInfo(msg.msg); showHeadsetMessage(msg.msg); blankToPassthrough();   // later
      }                                                   // snapshot (re-admit on go-public) un-blanks us

      else if (msg.type === "presence") {
        setAvatar(msg.user, msg.pose, msg.anchor);
        if (msg.user === worldOwner) maybeSpawnGuest(msg.pose);   // a guest drops in to the owner's right
      }
      else if (msg.type === "module_event") window.ConjureBus._dispatch(msg);   // peer's dynamic-module event
      else if (msg.type === "presence_leave") removeAvatar(msg.user);
      else if (msg.type === "recapture") {                // realign request → re-capture the room
        var sc = document.querySelector("a-scene");
        var rc = sc && sc.components && sc.components["room-capture"];
        if (rc && rc.recapture) rc.recapture();
      }
    };
  }

  // ----------------------------------------------------------------- WebXR room capture
  // ⚠ HEADSET-ONLY / NEEDS IN-HEADSET VERIFICATION (docs/backlogs/worlds-surfaces.md). Reads the Quest's
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
        // ~180° + several metres when you leave the room boundary and return (docs/specs/spaces-geometry.md §4.1), so
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
        // Client-owned model of what's in the room, for the POST gate (§7): only POST when this changes
        // structurally, so a settled room sends nothing. _known: id → last posted-frame surface record;
        // _absent: id → consecutive miss count (client owns removal-confidence); _posted / _postedBoundary:
        // the set/boundary as of the last POST, for structural diffing.
        this._known = {}; this._absent = {}; this._posted = {}; this._postedBoundary = null;
        this._recovered = {};       // seed-surface ids reconstructed via anchor (missing from capture) — for §5.2 logging
        this._localPlanes = null;   // last capture's local wall+floor planes (F_track) — for avatar anchors (§5.1)
        // --- JITTER PROBES (branch fix/pops-and-jitters) ------------------------------------------------
        // Diagnose the ~cm "flick out and back" seen while WALKING (not while standing + looking around).
        // Leading hypothesis: the ~0.5 Hz capture frame is heavy → a dropped frame → the compositor
        // reprojects the previous frame to the new head pose; rotation reprojects cleanly, TRANSLATION does
        // not → a one-frame positional error that snaps back next real frame. These probes accumulate in
        // memory (NO per-frame fetch — that would itself cause the hitch) and dump ONE line only when a
        // frame-time spike fires. _jPoses is the decisive discriminator: if the flick is visible but our
        // logged world poses are flat across it, the shift is compositor reprojection, not our transforms.
        this._jFrames = [];         // ring: {t, dt, cap, cost} — recent frame intervals (dropped-frame signal)
        this._jPoses = {};          // key → ring of {t,x,y,z} world positions of the probe entities (wall/obj)
        this._jPick = null;         // {wall, obj} currently-tracked probe entities
        this._jLastTick = 0;        // previous tick's `time` (for dt)
        this._jCapT0 = 0;           // performance.now() at capture-body start (for cost)
        this._jLastDumpT = 0;       // last dump's `time` (dump cooldown)
        this._jMarks = [];          // [{l,t}] performance.now() marks at each capture phase boundary (sub-cost)
        this._jCapSeq = 0;          // capture counter (bumped at capture start)
        this._jLoggedSeq = 0;       // last capture whose sub-cost breakdown was logged (log once, next frame)
        // Rolling frame-PACING window: unlike the spike dump (fires only on a hard >threshold frame), this
        // accumulates EVERY frame's dt over a ~2 s window and reports mean/jitter/percentiles + soft/dropped
        // counts, so the "smooth but not perfect" case (soft misses that never trip a hard spike) is measured.
        this._jWin = [];            // dt samples (ms) since the last pacing report
        this._jWinT0 = 0;           // `time` the current window opened
        this._jWinSlew = 0;         // max slewSet.size seen this window (attributes per-frame pose motion when tau>0)
        this._jLoggedRate = false;  // one-time display-rate log (session.frameRate + supportedFrameRates)
        // Per-LATE-frame forensics, BUFFERED in memory and flushed once per window inside the PACE fetch — so
        // every late frame is characterised WITHOUT a per-event fetch (a fetch storm would itself drop frames,
        // as an earlier probe did). Each record answers the two questions that pick the cause of a "flick out
        // and back": did OUR sampled pose move that frame (jump ⇒ our transform bug; flat ⇒ compositor
        // reprojected a dropped frame — outside our code), and did the JS heap just drop (⇒ a GC pause caused
        // the stall). `_jHeap` is the previous frame's used-heap so we can diff it; `performance.memory` is
        // Chromium-only (Oculus Browser has it) — absent ⇒ heap fields read 0.
        this._jHeap = 0;            // previous frame's performance.memory.usedJSHeapSize (bytes), for the GC diff
        this._jLate = [];           // buffered {dt, cap, dW, dO, dHeapKB, sT} for late frames this window
        this._jCur = null;          // this frame's _jFrames entry (so the throttle point can stamp its self-time)
        // Camera/view JERK: the one variable we never sampled. Where content lands on-screen is camera×world;
        // our entities are flat, so a translation-only "pop" that survives clean frame timing must be the VIEW.
        // Sample the WebXR head pose every frame and compute its "jerk" — the second difference of position
        // (p[n]-2p[n-1]+p[n-2]). Smooth walking = constant velocity = ~0 jerk; a jump-then-revert (the pop) =
        // a large spike. Buffer jerk events with whether the frame was ON-TIME vs late — the decisive split:
        // jerk on ON-TIME frames = a view/tracking stutter (platform positional prediction), NOT a dropped
        // frame or our render cost, and no rendering fix touches it (→ depth-submission territory).
        this._jHead = [];           // ring of recent head world positions {x,y,z} from frame.getViewerPose
        this._jJerk = [];           // buffered {j(mm), dt, on} jerk events this window
        this._jMaxJerk = 0;         // max per-frame jerk (mm) this window
        // --- GEOMETRY WORKER (fix/pops-and-jitters) ----------------------------------------------------
        // register() runs off the render thread so its ~7-11 ms solve never drops an XR frame. The capture
        // splits: the throttled tick reads planes and POSTS them; the worker's reply drives the render
        // continuation (see tick's `finish`). If the worker can't start (old browser, blocked, load error)
        // `_worker` stays null and we fall back to the synchronous solve — identical behaviour, old cost.
        // Force off with `window.CONJURE_WORKER = false`.
        this._worker = null;
        this._solveSeq = 0;         // monotonic id per posted solve; reply must match the latest (drop stale)
        this._pendingSolve = null;  // {seq, finish, cur} awaiting a worker reply
        var selfInit = this;
        try {
          if (window.CONJURE_WORKER !== false && typeof Worker !== "undefined") {
            var w = new Worker(window.CONJURE_WORKER_URL || "/static/room-worker.js", { type: "module" });
            w.onmessage = function (e) { selfInit._onSolve(e.data); };
            w.onerror = function (err) {                 // load/runtime failure → disable + re-capture sync
              debugLog("worker", "error, falling back to sync register: " + (err && (err.message || err.filename)), true);
              selfInit._worker = null; selfInit._pendingSolve = null; selfInit.lastPost = 0;
            };
            this._worker = w;
            debugLog("worker", "geometry worker started", window.CONJURE_DEBUG_REGISTRATION);
          }
        } catch (e) { this._worker = null; debugLog("worker", "spawn failed: " + e, true); }
        var self = this;
        // A recenter (Meta button) / boundary re-entry fires a 'reset' on the reference space — force an
        // immediate re-capture so registration re-locks the frame within a frame instead of up to ~2 s.
        this._onReset = function () { self.lastPost = 0; };
      },
      // Force an immediate re-capture (manual realign — see the /space/realign signal below).
      recapture: function () { this.lastPost = 0; },
      // Drop the current room REFERENCE FRAME. Called on a WORLD SWITCH (applySnapshot) so the next capture
      // re-seeds from the NEW world's geometry — or establishes fresh in an empty/void world — instead of
      // registering the new room against the PREVIOUS world's constellation. Without this, a guest who was
      // briefly in the owner's world keeps the owner's `_ref` after minting their own world, so their real
      // room renders registered to the owner's room → a stable positional offset (specs/spaces).
      resetFrame: function () {
        this._ref = []; this._Tmat = null; this._haveT = false;
        this._anchorInv = null; this._refSeq = 0; this._lostSince = 0; this.lastPost = 0;
        this._known = {}; this._absent = {}; this._posted = {}; this._postedBoundary = null;   // fresh post model
        this._recovered = {};
      },
      // Has surface `k` (a fresh record) changed STRUCTURALLY vs `p` (its last-posted snapshot)? Mirrors the
      // server's _surface_structural_change (0.5 m / 20° / opening-count / semantic) so the client only POSTs
      // real structural changes, not per-capture drift.
      _structMoved: function (p, k) {
        if (p.semantic !== k.semantic) return true;
        if ((k.holes || []).length !== (p.holes || 0)) return true;
        var pp = p.position || [0, 0, 0], kp = k.position || [0, 0, 0];
        if (Math.hypot(pp[0] - kp[0], pp[1] - kp[1], pp[2] - kp[2]) > 0.5) return true;
        var pe = p.extent || [0, 0], ke = k.extent || [0, 0];
        if (Math.abs(pe[0] - ke[0]) > 0.5 || Math.abs(pe[1] - ke[1]) > 0.5) return true;
        var THREE = AFRAME.THREE;
        return eulerYXZToQuat(THREE, p.rotation || [0, 0, 0]).angleTo(eulerYXZToQuat(THREE, k.rotation || [0, 0, 0]))
          > 20 * Math.PI / 180;
      },
      // Position #world-root. LOCAL-FIRST (docs/specs/spaces-geometry.md §2): a captured room renders its
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
      // nothing "pops" (docs/specs/spaces-geometry.md §9.1). Then debounce-prune any real surface that's been
      // absent a few captures, so a single missed capture doesn't flicker a wall away. world-root is
      // identity, so a surface at its captured pose renders at the real-world spot.
      _renderLocal: function (surfaces) {
        localRenderActive = true;                  // from now on, server real-surface ops are ignored (local owns them)
        var THREE = AFRAME.THREE;
        // [surf] DIAGNOSTIC (--debug-registration ONLY — never --debug-jitter, so it can't contaminate cost):
        // dump each FLOOR and WALL's 4 world-space corners (rectangle ±w/2 ±h/2 in local X-Y, posed by
        // position+rotation), ONCE per room entry (`surfDumped`, re-armed on world switch). Lets a specific
        // floor↔wall gap be read in numbers — is the wall's bottom at the floor plane (vertical, sealed) or is
        // the floor SHEET short of the wall (horizontal reach, not yet handled)? Cheap: one small burst, not
        // per-capture. Remove once read.
        if (window.CONJURE_DEBUG_REGISTRATION && !surfDumped) {
          surfDumped = true;
          var f3d = function (v) { return (+v).toFixed(3); };
          surfaces.forEach(function (s) {
            if (s.semantic !== "floor" && s.semantic !== "wall" && s.semantic !== "ceiling") return;
            var pos = s.position || [0, 0, 0], q = eulerYXZToQuat(THREE, s.rotation || [0, 0, 0]);
            var w = (s.extent && s.extent[0]) || 0, h = (s.extent && s.extent[1]) || 0;
            var P = new THREE.Vector3(pos[0], pos[1], pos[2]);
            var corner = function (sx, sy) {
              return new THREE.Vector3(sx * w / 2, sy * h / 2, 0).applyQuaternion(q).add(P);
            };
            var cs = [corner(-1, -1), corner(1, -1), corner(1, 1), corner(-1, 1)]
              .map(function (v) { return f3d(v.x) + "," + f3d(v.y) + "," + f3d(v.z); }).join(" | ");
            debugLog("surf", s.id + " [" + s.semantic + "] ext=" + f3d(w) + "x" + f3d(h) + " corners=" + cs, true);
          });
        }
        // Grouped surface re-lay (--group-surface-relay, default on): the per-surface apply-gate holds EACH
        // surface on its own epoch, so under tracking drift a wall re-lays at one capture while its adjoining
        // floor/ceiling — and a door/window vs its wall's cutout — re-lay at another. Anything that must stay
        // aligned ACROSS surfaces then opens a seam over a session: wall↔floor/ceiling junctions, and
        // inset↔cutout (the cutout lives in the wall mesh, the inset is its own surface). `snapInsets`/
        // `joinCorners` make each capture internally consistent, so the only divergence is the render epoch;
        // a reset re-lays everything at once, which is why it heals then. Fix: when ANY real surface moves
        // this capture, re-lay them ALL together at one epoch (the room shares one tracking frame). Both the
        // POSE (`_forcePoseRelay`) AND the GEOMETRY (`_forceGeoRelay`) are re-laid, so a wall's center and its
        // `joinCorners` width — a MATCHED pair — can never materialize from different captures. Under the
        // earlier pose-only relay they could: center advanced every capture while width lagged until it
        // crossed tolerance, leaving the wall's ends off the shared corner by up to a tolerance — the residual
        // wall∩wall / wall∩ceiling seam. The geometry rebuild is time-sliced (`pumpGeo`, §14), so co-relaying
        // it no longer risks the dropped frame that originally made us gate it out. Trigger is pose OR shape
        // (`surfaceMoved`) so a pure width drift with no pose move still re-closes. Off ⇒ each surface re-lays
        // independently (the A/B baseline that reproduces the seams).
        if (window.CONJURE_GROUP_SURFACE_RELAY !== false) {
          var anyMoved = surfaces.some(function (s) {
            var el = document.getElementById(s.id);
            if (!el || !el._geoSig) return true;   // new / never-laid surface counts as a change
            var sig = WM.surfaceSig({ position: s.position, rotation: s.rotation },
              { extent: s.extent, holes: s.holes || [] });
            return WM.surfaceMoved(THREE, el._geoSig, sig, window.CONJURE_APPLY_TOL);   // pose OR shape
          });
          if (anyMoved) {                          // re-lay every surface's POSE *and* GEOMETRY at one epoch
            surfaces.forEach(function (s) {
              var el = document.getElementById(s.id);
              if (el) { el._forcePoseRelay = true; el._forceGeoRelay = true; }
            });
          }
        }
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
          if (abs[el.id] >= 3 && el.parentNode) { slewSet.delete(el); el.parentNode.removeChild(el); delete abs[el.id]; }   // drop any in-flight ease (§10)
        });
      },
      // Place director-authored content (models, props — anything with a remembered F_ref pose) via
      // plane-relative anchors (docs/specs/spaces-geometry.md §5.3). Since #world-root is identity in a captured
      // room, we can't render content at its raw F_ref pose; instead, for each content entity, author an
      // anchor from its F_ref pose against the SEED walls (F_ref) and re-solve it against THIS client's LOCAL
      // walls (F_track) — so it lands at the right spot in the room, riding the same non-rigid geometry the
      // surfaces do. Re-solved every capture as the local walls refine. Free mode (full 3-D + orientation).
      _placeContent: function (localSurfaces) {
        var PA = window.PlaneAnchor; if (!PA) return;
        var THREE = AFRAME.THREE;
        var localPl = localToPlanes(THREE, localSurfaces), refPl = refToPlanes(THREE, this._ref);
        framePlanes.local = localPl; framePlanes.ref = refPl;   // the basis ConjureFrames.toRef inverts
        var wr = document.getElementById("world-root"); if (!wr) return;
        var ridden = 0, freed = 0, anchored = 0;
        Array.prototype.forEach.call(wr.children, function (el) {
          if (!el._frefPose || !el.object3D) return;
          // ONE placement rule for all three kinds — see contentPose. `true` rides the host's slew TARGET
          // so on-surface art eases in lock-step with its wall (§5.5) instead of lagging it through a
          // transition. A null result means no basis yet: hold the current pose.
          var r = contentPose(el, localPl, refPl, true);
          if (!r) return;
          placeContent(el, r.position, r.quaternion);   // snap first / when off, else ease (§5.5)
          if (r.kind === "surface") ridden++;
          else { freed++; if (r.kind === "anchored") anchored++; }
        });
        if (window.CONJURE_DEBUG_REGISTRATION && (ridden || freed))
          debugLog("content", "on-surface " + ridden + " + free " + freed + " (anchored " + anchored
            + "/" + freed + ", local=" + localPl.length + ")", true);
      },
      // Recover seed surfaces this client DIDN'T capture (§5.2): for each surface in the shared seed
      // (docSurfaces) that's absent from the live capture, author its anchor from its F_ref pose against the
      // seed walls and re-solve against the LOCAL walls — so a client missing a surface still sees it,
      // reconstructed consistently with its own geometry. Walls/floor are the anchor BASIS and are never
      // recovered. Returns the recovered surface records so they join the local render (and can host
      // on-surface content). Once the client actually detects the surface, it's in `localSurfaces` → skipped
      // here → the live capture wins.
      _recoverMissing: function (localSurfaces) {
        var PA = window.PlaneAnchor, RS = window.RoomSnap; if (!PA || !docSurfaces) return [];
        var THREE = AFRAME.THREE;
        var localPl = localToPlanes(THREE, localSurfaces), refPl = refToPlanes(THREE, this._ref);
        if (localPl.length < 2 || refPl.length < 2) return [];        // need a wall basis to solve against
        var have = {}, localById = {};
        localSurfaces.forEach(function (s) { have[s.id] = 1; localById[s.id] = s; });   // current-capture lookup
        var seedById = {}; docSurfaces.forEach(function (e) { seedById[e.id] = e; });   // seed poses (host-wall ride)
        // Local structural features for corner-relative reconstruction (§5.3): this client's own wall corners
        // and floor/ceiling edge heights, against which a missing inset's stored distances are solved.
        var localCorners = RS.wallCorners(THREE, localSurfaces), floorYL = null, ceilYL = null;
        localSurfaces.forEach(function (s) { if (s.semantic === "floor") floorYL = s._lp.y; else if (s.semantic === "ceiling") ceilYL = s._lp.y; });
        var INSET_SEMS = { "door": 1, "window": 1, "wall art": 1 };
        // Don't reconstruct a DUPLICATE seed inset (a shadow id) — its canonical twin is what renders (§5.3).
        var insetShadows = RS.dupInsetIds(docSurfaces.filter(function (e) { return INSET_SEMS[(e.meta || {}).semantic]; })
          .map(function (e) { var p = (e.transform || {}).position || [0, 0, 0];
            return { id: e.id, semantic: e.meta.semantic, hostWall: e.meta.host_wall, pos: p }; }));
        // …and don't recover an inset whose spot is already covered by a CAPTURED same-semantic inset (so a
        // just-minted duplicate id can't co-exist with the recovered seed id as a flickering twin).
        var presentInsets = localSurfaces.filter(function (s) { return INSET_SEMS[s.semantic]; });
        var mat = function (t) { t = t || {}; var p = t.position || [0, 0, 0];
          return new THREE.Matrix4().compose(new THREE.Vector3(p[0] || 0, p[1] || 0, p[2] || 0),
            eulerYXZToQuat(THREE, t.rotation || [0, 0, 0]), new THREE.Vector3(1, 1, 1)); };
        var out = [], self = this;
        // solveAnchor / the host-wall ride return the A-PLANE orientation (local +Z = normal), but snapInsets
        // reads a surface's normal as its RAW-plane local +Y (like a captured _lq). Convert with Rx(+90°) so a
        // recovered inset's normal is read correctly and it snaps to its wall.
        var RX90 = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2);
        docSurfaces.forEach(function (e) {
          var meta = e.meta || {}, sem = meta.semantic || "surface";
          if (have[e.id] || sem === "wall" || sem === "floor") return;   // captured, or a basis plane → skip
          if (insetShadows.has(e.id)) return;                            // duplicate seed inset → its twin renders
          var sf = (e.components || {}).surface || {};
          var hostId = meta.host_wall || undefined;
          var hostRec = hostId ? localById[hostId] : null;              // host wall's CURRENT capture record
          var hostSeed = hostId ? seedById[hostId] : null;
          var pos = new THREE.Vector3(), rawQ = new THREE.Quaternion(), how;   // rawQ: raw-plane (+Y=normal)
          if (hostRec && hostRec._lp && hostRec._lq && meta.along) {
            // CORNER-RELATIVE (§5.3): solve the inset's along-wall + height from its stored distances to the
            // host wall's CORNERS and the floor/ceiling edges — SHARED structural features derived from THIS
            // client's own capture — so it lands at the right physical spot regardless of how this device
            // (or a guest) happened to centre its scan of the wall. Orientation is the wall's (snapInsets
            // re-adopts it and pins the standoff). hostRec._lq is raw-plane (+Y=normal), what reconstructInset
            // expects. Beats the old centroid ride, which shifted the guest's mid-wall insets along the wall.
            var sol = RS.reconstructInset(THREE, hostRec, localCorners.get(hostId), floorYL, ceilYL,
                                          { along: meta.along, vertical: meta.vertical });
            if (!sol) return;
            pos.copy(sol.position); rawQ.copy(hostRec._lq);
            how = "corner-relative wall=" + hostId.slice(-7) + (sol.fallback ? " [" + sol.fallback + "]" : "");
          } else if (hostRec && hostRec._lp && hostRec._lq && hostSeed && hostSeed.transform) {
            // LEGACY RIDE (pre-§5.3 seeds without structural distances): apply the inset's seed offset-FROM-
            // its-wall onto the wall's LOCAL captured pose. Preserves along/height but rides the wall centroid,
            // so a guest's mid-wall inset can shift — kept only as a graceful fallback. _lq is raw-plane; undo
            // the Rx(+90°) to the a-plane frame the seed poses use.
            var hostQ = hostRec._lq.clone().multiply(RX90.clone().invert());
            var hostLocal = new THREE.Matrix4().compose(hostRec._lp.clone(), hostQ, new THREE.Vector3(1, 1, 1));
            var aq = new THREE.Quaternion(), is = new THREE.Vector3();
            hostLocal.multiply(mat(hostSeed.transform).invert().multiply(mat(e.transform))).decompose(pos, aq, is);
            rawQ.copy(aq.multiply(RX90)); how = "ride wall=" + hostId.slice(-7);
          } else {
            // No recorded/rendered host wall → fall back to a free multilateration against the local walls.
            var p = (e.transform || {}).position || [0, 0, 0];
            var entity = { mode: "free", quaternion: eulerYXZToQuat(THREE, (e.transform || {}).rotation || [0, 0, 0]),
              position: new THREE.Vector3(p[0], p[1], p[2]) };
            var solm = PA.solveAnchor(THREE, PA.authorAnchor(THREE, entity, refPl), localPl);
            if (!solm.ok) return;                                       // degenerate / too few walls → skip
            pos.copy(solm.position); rawQ.copy(solm.quaternion.clone().multiply(RX90)); how = "multilat";
          }
          // Skip if a CAPTURED same-semantic inset already sits at this spot (within 25 cm) — recovering it
          // would double the inset (the flickering-twin symptom). The live capture wins.
          var covered = presentInsets.some(function (s) {
            return s.semantic === sem && Math.hypot(s.position[0] - pos.x, s.position[1] - pos.y, s.position[2] - pos.z) < 0.25;
          });
          if (covered) return;
          // Carry _lp/_lq (raw-plane) so snapInsets snaps the recovered inset co-planar to its wall, and
          // hostWall (the recorded association, §5.2) so it snaps to THAT wall. rotation is the a-plane euler
          // eulerYXZ derives from the raw-plane quat.
          out.push({ id: e.id, semantic: sem, extent: sf.extent, holes: sf.holes, debug: {}, _recovered: true,
            hostWall: hostId,
            position: [pos.x, pos.y, pos.z],
            rotation: RS.eulerYXZ(THREE, { x: rawQ.x, y: rawQ.y, z: rawQ.z, w: rawQ.w }),
            _lp: pos.clone(), _lq: rawQ.clone() });
          if (!self._recovered[e.id]) {
            self._recovered[e.id] = 1;
            debugLog("recover", "surface " + e.id + " (" + sem + ") reconstructed (" + how + ")", true);
          }
        });
        return out;
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
      // Worker reply: the off-thread register() result. Reconstruct the Matrix4 and run the capture's render
      // continuation (`finish`, stashed at post time). A sequence guard drops a stale reply (belt-and-braces —
      // the 2 s throttle already means only one solve is ever in flight). Errors in finish are contained so a
      // single bad capture can't wedge the message pump.
      _onSolve: function (m) {
        if (!m || m.type !== "register") return;
        var p = this._pendingSolve;
        if (!p || m.seq !== p.seq) return;                 // stale / superseded / none pending → drop
        this._pendingSolve = null;
        this._regStat = m.stat; this._regRes = m.residuals || null;   // (sync path: _register sets these)
        var reg = m.els ? new AFRAME.THREE.Matrix4().fromArray(m.els) : null;
        try { p.finish(reg); }
        catch (e) { console.warn("[conjure] capture finish failed", e); debugLog("worker", "finish threw: " + e, true); }
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
      // Orient the skybox consistently relative to the ROOM (§5d — wall-relative yaw). The <a-sky>/
      // #grounded-sky live as scene children (not inside #world-root, which applySnapshot clears), so left
      // alone they'd hold the arbitrary per-session tracking yaw and spin between visits. #world-root is
      // identity in a captured room now, so instead of reading it we apply the registration transform's
      // INVERSE rotation (F_ref → F_track) directly — the same yaw #world-root used to carry — so the sky
      // rides the persistent room frame and keeps its orientation across sessions. Rotation only (a pure
      // gravity yaw from the register vote): a plain sky sphere stays viewer-centered; a grounded dome spins
      // about vertical, ground flat. Identity before the first lock.
      _pinSky: function () {
        var THREE = AFRAME.THREE, q = new THREE.Quaternion();
        if (this._haveT && this._Tmat) {
          var p = new THREE.Vector3(), s = new THREE.Vector3();
          this._Tmat.clone().invert().decompose(p, q, s);
        }
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
      // JITTER PROBE: on when `--debug-jitter` (server injects window.CONJURE_DEBUG_JITTER) — decoupled from
      // --debug-registration so the probes measure frame cost WITHOUT the heavy registration diagnostics
      // (residual/coloc logging + fetches) contaminating the numbers. Can also be forced from the console.
      _jitOn: function () { return !!window.CONJURE_DEBUG_JITTER; },
      // JITTER PROBE: sample the WORLD position of a representative real wall + a content object every frame.
      // world-root is identity in a captured room, so these should be dead-flat between captures regardless
      // of head motion — if they stay flat across a visible flick, the flick is compositor reprojection.
      _jSample: function (t) {
        var wr = document.getElementById("world-root"); if (!wr) return;
        var THREE = AFRAME.THREE;
        var pick = this._jPick || (this._jPick = { wall: null, obj: null });
        if (!pick.wall || !pick.wall.isConnected) pick.wall = wr.querySelector('[data-real="1"]');
        if (!pick.obj || !pick.obj.isConnected) {
          pick.obj = null;
          Array.prototype.some.call(wr.children, function (el) {
            if (el._frefPose) { pick.obj = el; return true; } return false; });
        }
        var self = this;
        [["wall", pick.wall], ["obj", pick.obj]].forEach(function (pair) {
          var key = pair[0], el = pair[1]; if (!el || !el.object3D) return;
          var p = new THREE.Vector3(); el.object3D.getWorldPosition(p);
          var ring = self._jPoses[key] || (self._jPoses[key] = []);
          ring.push({ t: t, x: +p.x.toFixed(4), y: +p.y.toFixed(4), z: +p.z.toFixed(4) });
          if (ring.length > 24) ring.shift();
        });
      },
      // JITTER PROBE: how far a probe entity's sampled WORLD position moved on the latest frame (mm) — the
      // last two entries of its _jPoses ring. ~0 on a late frame ⇒ our transform held (the shift was
      // compositor reprojection); nonzero ⇒ we moved it that frame (our bug, or legitimate slew if slew>0).
      _jRingDelta: function (key) {
        var r = this._jPoses[key]; if (!r || r.length < 2) return 0;
        var a = r[r.length - 1], b = r[r.length - 2];
        return Math.round(Math.hypot(a.x - b.x, a.y - b.y, a.z - b.z) * 1000);   // metres → mm
      },
      // JITTER PROBE: stamp a phase boundary inside the capture body (sub-cost breakdown). Cheap; caller
      // gates on JIT so there's zero cost when off.
      _jMark: function (label) { this._jMarks.push({ l: label, t: performance.now() }); },
      // JITTER PROBE: log the per-phase deltas of the capture that just ran, once, on the following frame.
      // `dbg` isolates the --debug-registration-only probes (planes/normals) so we can subtract the cost the
      // measurement itself adds vs. production. A phase missing from the line means its early-return fired.
      _jBreakdown: function () {
        var m = this._jMarks; if (!m || m.length < 2) return;
        var parts = [];
        for (var i = 1; i < m.length; i++) parts.push(m[i].l + "=" + (m[i].t - m[i - 1].t).toFixed(1));
        debugLog("jitter", "COST total=" + (m[m.length - 1].t - m[0].t).toFixed(1) + "ms | " + parts.join(" "), true);
      },
      // JITTER PROBE: the ROLLING pacing report (every ~2 s, independent of any spike). Characterises the
      // "smooth but not quite" case the one-shot spike dump misses: over the window, the mean frame interval,
      // the JITTER (stddev of dt — the actual smoothness metric), p95/max, and how many frames were merely
      // LATE (>1.2x ideal) vs a genuine DROP (>1.5x ideal = missed at least one vsync). `slew=` is the peak
      // slewSet size this window: with --pose-tau>0 the pump writes poses every frame, so nonzero slew means
      // some of any measured motion is EXPECTED easing, not a pipeline miss — the discriminator for tau>0 runs.
      _jPacing: function (ideal, heap) {
        var w = this._jWin, n = w.length;
        this._jWin = []; this._jWinT0 = 0; var peakSlew = this._jWinSlew; this._jWinSlew = 0;
        var late = this._jLate; this._jLate = [];
        var jerk = this._jJerk; this._jJerk = []; var maxJerk = this._jMaxJerk; this._jMaxJerk = 0;
        var rebuilds = geoRebuilds; geoRebuilds = 0;
        if (n < 2) return;
        var sum = 0, max = 0, lateN = 0, drop = 0;
        for (var i = 0; i < n; i++) { var d = w[i]; sum += d; if (d > max) max = d;
          if (d > 1.5 * ideal) drop++; else if (d > 1.2 * ideal) lateN++; }
        var mean = sum / n, varr = 0;
        for (var j = 0; j < n; j++) { var e = w[j] - mean; varr += e * e; }
        var sd = Math.sqrt(varr / n);
        var sorted = w.slice().sort(function (a, b) { return a - b; });
        var p95 = sorted[Math.min(n - 1, Math.floor(n * 0.95))];
        debugLog("jitter", "PACE n=" + n + " ideal=" + ideal.toFixed(1) + " mean=" + mean.toFixed(1)
          + " jit(sd)=" + sd.toFixed(1) + " p95=" + p95.toFixed(1) + " max=" + max.toFixed(1)
          + " late(>1.2x)=" + lateN + " drop(>1.5x)=" + drop + " slew=" + peakSlew
          + " rebuilds=" + rebuilds + " maxjerk=" + maxJerk + "mm"
          + " heap=" + (heap ? (heap / 1048576).toFixed(1) + "MB" : "n/a"), true);
        // Camera/view jerk events this window. Token: jerk-mm(on|late/dt). MANY "on" (on-time) jerks while
        // walking = the pop is a VIEW/tracking stutter, invisible to entity+timing probes, unfixable by
        // render tuning (points at depth-submission for positional reprojection). Only "late" jerks = it's
        // just dropped-frame reprojection after all. No jerks despite a felt pop = look elsewhere entirely.
        if (jerk.length) debugLog("jitter", "JERK(" + jerk.length + ") " + jerk.map(function (r) {
          return r.j + "(" + (r.on ? "on" : "late") + "/dt" + r.dt.toFixed(0) + ")"; }).join(" "), true);
        // Per-late-frame forensics for this window, one line (no per-event fetch). Token:
        //   dt(cap):dW/dO/heapKB/selfMs — dt ms; cap=prev frame ran capture; dW/dO = wall/obj move mm; heap
        //   delta KB; selfMs = the PREVIOUS (overrunning) frame's tick self-time (our JS). Reads:
        //   dW/dO ~0 ⇒ our transforms held → compositor reprojected a dropped frame (shift is outside our code).
        //   selfMs small (<~2) with a big dt ⇒ the stall was OUTSIDE our JS (render/compositor/GC-between-frames)
        //     — nothing left to fix in our code. selfMs ~= dt ⇒ our JS was on the critical path that frame.
        //   big negative heapKB ⇒ a GC pause (Chromium only; Oculus freezes performance.memory, so usually 0).
        if (late.length) debugLog("jitter", "LATE(" + late.length + ") " + late.map(function (r) {
          return r.dt.toFixed(0) + "(" + r.cap + "):" + r.dW + "/" + r.dO + "/" + r.dHeapKB
            + "/" + r.sT.toFixed(1); }).join(" "), true);
      },
      // JITTER PROBE: dump the recent frame-interval ring + the probe world-pose rings, once, on a spike.
      // Frame token: "<dt>" for a normal frame, "<dt>*<cost>" for a frame that ran the capture body (cap).
      // A cap-tagged frame lining up with the dt spike ⇒ the capture is dropping the frame.
      _jDump: function (time, dt) {
        if (time - this._jLastDumpT < 500) return;   // cooldown: ≤2 dumps/sec, so dumping never storms
        this._jLastDumpT = time;
        var frames = this._jFrames.map(function (f) {
          return f.dt.toFixed(1) + (f.cap ? ("*" + f.cost.toFixed(1)) : ""); }).join(" ");
        var pose = function (key) {
          return (this._jPoses[key] || []).map(function (p) {
            return p.x + "," + p.y + "," + p.z; }).join(" ");
        }.bind(this);
        debugLog("jitter", "SPIKE dt=" + dt.toFixed(1) + "ms slew=" + slewSet.size + " | frames[dt(*cap:cost)]: " + frames
          + " | wallW: " + pose("wall") + " | objW: " + pose("obj"), true);
      },
      tick: function (time, timeDelta) {
        var sceneEl = this.el.sceneEl, frame = sceneEl.frame;
        var refSpace = frame && sceneEl.renderer.xr.getReferenceSpace();
        if (!frame || !refSpace) {
          // Desktop / no XR session: A-Frame still ticks (rAF), but everything below (head-frame
          // parking, sky pin, foveation, room capture, jitter probes) is XR-only. The deferred
          // surface-mesh builder (pumpGeo) and pose easing (slewPoses) are frame-INDEPENDENT and MUST
          // still run here — otherwise a desktop viewer never drains the geo queue, so real-surface
          // planes are never built (walls invisible; only edges/wall-art/labels render).
          pumpGeo();
          slewPoses((timeDelta || 0) / 1000);
          return;
        }
        var _jt0 = performance.now();                       // tick self-time start (our JS work this frame)
        var JIT = this._jitOn();
        if (JIT) {                                          // JITTER PROBE: per-frame pacing + pose sampling
          var session = sceneEl.renderer.xr.getSession();
          var refresh = (session && session.frameRate) || 72;   // Quest browser is often 72 Hz, not 90 — the true budget
          var ideal = 1000 / refresh;                           // ideal inter-frame interval (ms)
          if (!this._jLoggedRate) {                             // one-time: what rate are we ACTUALLY running at?
            this._jLoggedRate = true;
            var sr = session && session.supportedFrameRates;
            debugLog("jitter", "RATE current=" + (session && session.frameRate || "?") + "Hz ideal=" + ideal.toFixed(1)
              + "ms supported=[" + (sr ? Array.prototype.join.call(sr, ",") : "?") + "]", true);
          }
          var dt = this._jLastTick ? (time - this._jLastTick) : 0;
          this._jLastTick = time;
          this._jCur = { t: time, dt: dt, cap: 0, cost: 0, self: 0 };
          this._jFrames.push(this._jCur);
          if (this._jFrames.length > 24) this._jFrames.shift();
          this._jSample(time);
          // Sample the JS heap: a used-heap DROP between frames means a GC just ran (Chromium/Oculus only).
          var mem = /** @type {any} */ (performance).memory, heap = (mem && mem.usedJSHeapSize) || 0;
          var dHeapKB = this._jHeap ? Math.round((heap - this._jHeap) / 1024) : 0;
          this._jHeap = heap;
          // Camera/view jerk: sample the head pose and compute the 2nd difference of its position (mm). A
          // large jerk on an ON-TIME frame (dt within budget) is a view/tracking stutter the entity/timing
          // probes can't see; a large jerk only on LATE frames = dropped-frame reprojection.
          var vp = frame.getViewerPose(refSpace);
          if (vp) {
            var hp = vp.transform.position, hr = this._jHead;
            hr.push({ x: hp.x, y: hp.y, z: hp.z });
            if (hr.length > 4) hr.shift();
            if (hr.length >= 3) {
              var a3 = hr[hr.length - 1], b3 = hr[hr.length - 2], c3 = hr[hr.length - 3];
              var jerk = Math.round(Math.hypot(a3.x - 2 * b3.x + c3.x, a3.y - 2 * b3.y + c3.y,
                a3.z - 2 * b3.z + c3.z) * 1000);            // metres → mm
              if (jerk > this._jMaxJerk) this._jMaxJerk = jerk;
              if (jerk > 2) this._jJerk.push({ j: jerk, dt: dt, on: dt <= 1.5 * ideal ? 1 : 0 });
            }
          }
          // Rolling pacing window: accumulate every dt, note peak slew activity, report every ~2 s (below).
          if (dt > 0) this._jWin.push(dt);
          if (slewSet.size > this._jWinSlew) this._jWinSlew = slewSet.size;
          if (!this._jWinT0) this._jWinT0 = time;
          // Per-late-frame forensics: a frame past 1.2x ideal is "late". Buffer a compact record (dt, whether
          // the PREVIOUS frame ran the capture body — the usual overrun source — how far our sampled wall/obj
          // moved THIS frame in mm, and the heap delta). dW/dO ~0 with a late dt ⇒ our transforms held and the
          // compositor reprojected (outside our code); a nonzero dW/dO ⇒ WE moved it that frame. Flushed in
          // the PACE line below (one fetch/window), so capturing every late frame adds no per-event fetch.
          if (dt > 1.2 * ideal) {
            var prev = this._jFrames[this._jFrames.length - 2];
            this._jLate.push({ dt: dt, cap: prev ? prev.cap : 0,
              dW: this._jRingDelta("wall"), dO: this._jRingDelta("obj"), dHeapKB: dHeapKB,
              sT: prev ? (prev.self || 0) : 0 });   // PREVIOUS frame's tick self-time — the frame that overran
          }
          if (time - this._jWinT0 >= 2000) this._jPacing(ideal, heap);
          // Hard-spike dump: refresh-relative by default (1.5x ideal = missed ≥1 vsync), CONJURE_JITTER_DT_MS overrides.
          var dtThresh = window.CONJURE_JITTER_DT_MS || (1.5 * ideal);
          if (dt > dtThresh) this._jDump(time, dt);
          if (this._jCapSeq !== this._jLoggedSeq && this._jMarks.length > 1) {    // sub-cost of the prev capture
            this._jLoggedSeq = this._jCapSeq; this._jBreakdown();
          }
        }
        if (refSpace !== this._resetSpace) {                // (re)subscribe to recenter events
          this._resetSpace = refSpace;
          if (refSpace.addEventListener) refSpace.addEventListener("reset", this._onReset);
        }
        this._updateWorldFrame(frame, refSpace);            // EVERY frame: park #world-root on the frame
        this._pinSky();                                     // …and pin the sky to that SAME frame (see below)
        pumpGeo();                                           // EVERY frame: drain a few deferred mesh rebuilds (time-sliced)
        slewPoses((timeDelta || 0) / 1000);                  // EVERY frame: ease surfaces/content toward their targets (pose-smoothing; no-op when disabled)
        // Apply the configured foveated-rendering level once the XR session exists — OVERRIDES index.html's
        // hardcoded foveationLevel (docs: GPU-bound dropped frames while walking). Higher = periphery drawn
        // at lower resolution = less GPU (fewer drops) at the cost of peripheral sharpness/moiré; 0 = full-res
        // everywhere. Set via --foveation (window.CONJURE_FOVEATION); default 0 leaves today's behaviour.
        if (!this._foveationSet) {
          var fov = window.CONJURE_FOVEATION;
          var xrm = sceneEl.renderer.xr;
          if (fov == null) { this._foveationSet = true; }        // not injected → keep index.html default
          else if (xrm && xrm.getSession()) {
            try { xrm.setFoveation(Math.max(0, Math.min(1, +fov || 0))); } catch (e) { /* older three */ }
            this._foveationSet = true;
            debugLog("render", "foveation applied=" + fov + " (now " + xrm.getFoveation() + ")");
          }
        }
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
        // Stamp this frame's tick self-time (our JS: updateWorldFrame + pinSky + pumpGeo + slewPoses + probes)
        // BEFORE the throttle return — so ordinary frames (which return here, the ones that drop with cap=0)
        // record their full per-frame JS cost. Capture frames continue past this; their heavier body is timed
        // separately by COST, so self here is their pre-capture floor.
        if (this._jCur) this._jCur.self = performance.now() - _jt0;
        if (time - this.lastPost < CAPTURE_MS) return;      // throttle
        if (JIT) {                                          // JITTER PROBE: this frame runs the capture body
          var _jfr = this._jFrames[this._jFrames.length - 1];
          if (_jfr) _jfr.cap = 1;
          this._jCapT0 = performance.now();                 // wall-clock cost of the synchronous capture work
          this._jCapSeq++;                                  // start a fresh sub-cost timeline for this capture
          this._jMarks = [{ l: "start", t: this._jCapT0 }];
        }
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
        if (JIT) this._jMark("passA");                      // read all detectedPlanes → cur

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

        if (JIT) this._jMark("dbg");                        // --debug-registration-only probes (NOT in prod)

        // Am I the room AUTHORITY? The active world's owner authors the geometry; everyone else is a
        // register-only GUEST (specs/spaces-geometry.md §4.2). Unknown-owner means "not yet", NOT "me" —
        // see WM.isCaptureAuthority for why that distinction is load-bearing.
        var me = currentUser(), amOwner = WM.isCaptureAuthority(me, worldOwner);

        // Seed the reference constellation from the persisted/broadcast surfaces. The AUTHORITY seeds ONCE,
        // then owns and slowly evolves it (Pass B). A GUEST re-seeds EVERY capture straight from the
        // authoritative broadcast, so its reference always EQUALS the owner's current geometry and it never
        // contributes its own — this frozen, authority-owned target is what stops the shared frame drifting.
        if (docSurfaces && docSurfaces.length >= 3 && (!this._ref.length || !amOwner)) {
          var hadRef = this._ref.length;
          if (!amOwner) self._ref = [];                                  // guest: replace wholesale from authority
          var mx = 0;
          docSurfaces.forEach(function (e) {
            var rr = window.RoomSnap.surfaceToRef(THREE, e);              // one source of truth (YXZ-correct normal)
            var m = e.meta || {};                                        // carry the inset's corner-relative
            if (m.host_wall) rr.hostWall = m.host_wall;                  // anchor onto its _ref entry, so inset
            if (m.along) rr.anchor = { along: m.along, vertical: m.vertical };   // IDENTITY resolves against _ref
            self._ref.push(rr);                                          // (immediate) — never the lagging seed
            var mm = /_(\d+)$/.exec(e.id); if (mm) mx = Math.max(mx, +mm[1] + 1);   // keep new ids unique
          });
          self._refSeq = Math.max(self._refSeq, mx);
          // Seed the POST model from the persisted seed too (owner, once), so on re-entry the client's first
          // posts don't look like "everything was removed" and prune the whole stored seed. Starts _known =
          // the seed; matching re-captures then cause no structural change → no post → seed preserved.
          if (amOwner && !hadRef) {
            docSurfaces.forEach(function (e) {
              var t = e.transform || {}, sf = (e.components || {}).surface || {}, sem = (e.meta || {}).semantic;
              self._known[e.id] = { id: e.id, semantic: sem, position: t.position, rotation: t.rotation,
                                    extent: sf.extent, holes: sf.holes };
              self._posted[e.id] = { position: t.position, rotation: t.rotation, extent: sf.extent,
                                     holes: (sf.holes || []).length, semantic: sem };
            });
          }
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

        // Space selection, stage 2 (specs/spaces.md §6): while candidates are pending, vote THIS capture
        // against each one. A confident registration ⇒ we're in that space → join it (/space/select
        // matched). Once the capture is rich enough that a real match WOULD have locked (≥6 walls, a few
        // tries) but none did, we're somewhere new → commit "no match" so the server stamps/mints a space
        // here. The geometric vote decides — not a surface count — so a sparse early capture just stays
        // undecided and we fall through to normal behavior (register correctly DECLINES a non-matching
        // booted room, so nothing drifts) until the capture fills in. Runs even when the active world is
        // VOID (the default Holodeck / an outdoor world): that's exactly when we must vote the live capture
        // against candidates to find/mint the physical room — otherwise selection can never resolve (the
        // old !isVoidWorld gate is what left an outdoor re-entry stuck on "finding").
        if (pendingSelect) {
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
          this._localPlanes = null;   // void world: no shared seed walls → avatars fall back to the F_ref pose
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
        // SOLVE off the render thread (fix/pops-and-jitters): register() was the last per-capture frame sink.
        // `finish` is the render continuation — run on the worker's reply (_onSolve) or synchronously if there
        // is no worker. Arrow ⇒ `this` stays the component, so the tail below is unchanged; it captures cur,
        // time, amOwner, RETRY_MS, THREE, UP, JIT, self from this tick's scope.
        var canEstablish = amOwner && this._ref.length === 0;
        var finish = (reg) => {
        if (JIT) { this._jCapT0 = performance.now(); this._jMarks = [{ l: "applyStart", t: this._jCapT0 }]; }  // on-main render cost (solve is off-thread)
        if (window.CONJURE_DEBUG_REGISTRATION) this._diag(amOwner, cur.length, reg);   // opt-in: one line + HUD/capture
        if (!reg && !canEstablish) { this._markLost(time); this.lastPost = time - RETRY_MS; return; }   // not locked → hold
        if (this._lostSince) { this._lostSince = 0; if (this._reloc) this._relocalize(false); }   // re-locked → restore
        var registered = !!reg, Tmat;
        if (reg) { Tmat = this._Tmat = reg; this._haveT = true; }
        else { Tmat = this._anchorInv || new THREE.Matrix4(); this._Tmat = Tmat; this._haveT = true; }  // establish fresh
        this._anchorInv = Tmat;
        if (JIT) this._jMark("applyReg");                   // on-main apply begins (register itself is off-thread)
        // Pass B — assign each plane a STABLE id via matchRef, and build TWO views of it: `localSurfaces`
        // (its raw F_track pose — what we RENDER, matching THIS headset's passthrough) and `surfaces` (the
        // reference-frame pose the OWNER posts to persist the shared model/seed). Both carry the same id.
        // Runs for owner AND guest now (unified): everyone renders their own capture; only the owner authors.
        // See docs/specs/spaces-geometry.md §9.1.
        var surfaces = [], localSurfaces = [], floor = null, claimed = new Set();
        var RS = window.RoomSnap, INSET_SEMS = { "door": 1, "window": 1, "wall art": 1 };
        // TEST (--drop-surface): a comma-separated list of semantics/ids to pretend we didn't capture.
        var dropList = (window.CONJURE_DROP_SURFACE || "").split(",").map(function (s) { return s.trim(); }).filter(Boolean);
        // A cur plane's pose in the shared reference frame (F_ref): T · plane.
        function refPose(c) {
          var planeMat = new THREE.Matrix4().compose(c.pos, c.quat, new THREE.Vector3(1, 1, 1));
          var lp = new THREE.Vector3(), lq = new THREE.Quaternion(), ls = new THREE.Vector3();
          Tmat.clone().multiply(planeMat).decompose(lp, lq, ls);
          return { lp: lp, lq: lq };
        }
        // Push both views of a resolved surface: `surfaces` (F_ref pose the OWNER posts to persist the seed)
        // and `localSurfaces` (its RAW F_track pose — what THIS headset renders), same id. hostWall (insets)
        // rides onto both so snapInsets snaps to the SAME wall identity resolved below.
        function pushSurface(c, sid, lp, lq, hostId) {
          surfaces.push({ id: sid, semantic: c.sem, position: [lp.x, lp.y, lp.z],
            rotation: self._euler(lq), extent: [c.ext[0], c.ext[1]], _lp: lp.clone(), _lq: lq.clone(),
            hostWall: hostId || undefined,
            debug: { pos: c.raw.pos, quat: c.raw.quat, orient: c.orient, label: c.sem,
                     polyY: c.polyY, n: c.poly.length, registered: registered, regStat: self._regStat } });
          var dropped = dropList.some(function (d) { return c.sem === d || sid.indexOf(d) >= 0; });
          if (!dropped) {
            localSurfaces.push({ id: sid, semantic: c.sem, position: [c.pos.x, c.pos.y, c.pos.z],
              rotation: self._euler(c.quat), extent: [c.ext[0], c.ext[1]],
              _lp: c.pos.clone(), _lq: c.quat.clone(), hostWall: hostId || undefined, debug: {} });
          }
          if (c.sem === "floor" && (!floor || c.ext[0] * c.ext[1] > floor._area)) {
            floor = { floorPolygon: c.poly.map(function (pt) { return [pt.x, pt.z]; }), height: 2.6, _area: c.ext[0] * c.ext[1] };
          }
        }
        // Inherit an existing _ref entry's id (and track its slow drift), or mint + remember a new one.
        function resolveRef(c, lp, cyaw, best) {
          if (best) { claimed.add(best); best.pos.lerp(lp, 0.3); best.ext = c.ext.slice(); best.nyaw = cyaw; return best.id; }
          var sid = "real_" + c.sem.replace(/\s+/g, "_") + "_" + (self._refSeq++);
          var r = { id: sid, sem: c.sem, ext: c.ext.slice(), pos: lp.clone(), nyaw: cyaw, orient: c.orient };
          self._ref.push(r); claimed.add(r);
          return sid;
        }
        // Pass B1 — WALLS + floor/ceiling first (insets need resolved wall ids). Walls get identity by
        // PLANE (matchWall, §5.3/§10) — invariant to the centroid sliding along a differently-captured wall;
        // horizontals keep matchRef (centroid+semantic is right for a floor/ceiling).
        cur.forEach(function (c) {
          if (INSET_SEMS[c.sem]) return;
          var rp = refPose(c), cyaw = self._yawOf(UP.clone().applyQuaternion(rp.lq));
          var best = c.orient === "vertical"
            ? RS.matchWall({ pos: rp.lp, nyaw: cyaw, sem: c.sem, orient: c.orient, ext: c.ext }, self._ref, claimed, window.CONJURE_WALL)
            : RS.matchRef({ pos: rp.lp, nyaw: cyaw, sem: c.sem, orient: c.orient }, self._ref, claimed);
          pushSurface(c, resolveRef(c, rp.lp, cyaw, best), rp.lp, rp.lq, null);
        });
        // Pass B2 — INSETS. IDENTITY re-inherits against the PERSISTENT reference constellation `_ref` (the
        // SAME immediate source walls use), matched CORNER-RELATIVELY (§5.3). Each _ref inset carries its
        // along-wall distances to its host wall's corners (the owner stamps them when it authors; a guest
        // copies them in when it seeds _ref from the space), so we reconstruct its expected along-position
        // against THIS capture's walls and match the captured inset by nearest along. Corner-relative because
        // corners are SHARED structural features — independent of each device's scan centroid (goal 1: a
        // guest) and of a single wall drifting within a session (goal 2: the inset + its corners move with the
        // wall). Crucially against `_ref`, NOT the server seed: the owner accretes _ref immediately while the
        // seed round-trips with a lag (empty right after establish), so keying identity off the seed made
        // every inset mint each capture → _ref grew +N/capture → churn → relocalize. First capture after
        // establish mints once (no anchor yet), then stable.
        var refWalls = surfaces.filter(function (s) { return s.semantic === "wall"; });
        var refCorners = RS.wallCorners(THREE, surfaces);
        var refFloorY = null, refCeilY = null;
        surfaces.forEach(function (s) { if (s.semantic === "floor") refFloorY = s._lp.y; else if (s.semantic === "ceiling") refCeilY = s._lp.y; });
        // Reconstruct each anchored _ref inset's expected along-position on its host wall, THIS capture.
        var refReconByKey = {}, refInsetById = {};
        self._ref.forEach(function (r) {
          if (!INSET_SEMS[r.sem] || !r.hostWall || !r.anchor) return;
          refInsetById[r.id] = r;
          var hwRec = null; refWalls.forEach(function (w) { if (w.id === r.hostWall) hwRec = w; });
          if (!hwRec) return;
          var sol = RS.reconstructInset(THREE, hwRec, refCorners.get(r.hostWall), refFloorY, refCeilY, r.anchor);
          if (!sol) return;
          (refReconByKey[r.sem + "|" + r.hostWall] = refReconByKey[r.sem + "|" + r.hostWall] || []).push({ id: r.id, along: sol.along });
        });
        var claimedInset = new Set();
        cur.forEach(function (c) {
          if (!INSET_SEMS[c.sem]) return;
          var rp = refPose(c), cyaw = self._yawOf(UP.clone().applyQuaternion(rp.lq));
          var hw = RS.hostWallFor(THREE, { _lp: rp.lp, _lq: rp.lq, extent: c.ext }, refWalls);
          var hostId = hw ? hw.id : null, sid = null;
          if (hw) {
            var along = RS.insetAlong(THREE, hw, rp.lp.x, rp.lp.z);
            sid = RS.matchInset({ along: along }, refReconByKey[c.sem + "|" + hostId], claimedInset);
          }
          if (sid) {                                                     // re-inherit + track drift
            claimedInset.add(sid);
            var rr = refInsetById[sid]; if (rr) { rr.pos.lerp(rp.lp, 0.3); rr.ext = c.ext.slice(); rr.nyaw = cyaw; }
          } else {
            sid = resolveRef(c, rp.lp, cyaw, null);                      // no structural match → mint (into _ref too)
          }
          pushSurface(c, sid, rp.lp, rp.lq, hostId);
        });
        if (!surfaces.length) return;
        if (JIT) this._jMark("passB");                      // matchRef: stable ids + F_ref/F_track views

        // LOCAL RENDER (every client): snap corners + insets in F_track, then draw each surface at its OWN
        // captured pose — matching THIS headset's passthrough, with no shared rigid frame and no server
        // round-trip. Squaring is intentionally skipped (default off — trust the raw local planes; docs §9).
        // world-root stays identity (_updateWorldFrame). The apply-gate inside applyEntity means an unchanged
        // surface isn't re-laid, so nothing "pops".
        window.RoomSnap.joinCorners(THREE, localSurfaces);        // close wall corners (the recovery + snap basis)
        window.RoomSnap.sealWalls(THREE, localSurfaces, window.CONJURE_WALL_SEAL_TOL);   // seal wall tops→ceiling, bottoms→floor (§9.1)
        this._localPlanes = localToPlanes(THREE, localSurfaces);   // stash for avatar anchors (§5.1) / presence
        // Reconstruct any seed surface this client didn't capture (§5.2) and fold it into the render set, so
        // recovered surfaces both draw and can host on-surface content just like captured ones.
        var recovered = this._recoverMissing(localSurfaces);
        var allSurfaces = recovered.length ? localSurfaces.concat(recovered) : localSurfaces;
        // Snap ALL insets (captured AND recovered) co-planar to their walls + carve openings — so a
        // recovered door/window/wall-art snaps to its wall instead of floating at the raw anchor pose (§5.2).
        window.RoomSnap.snapInsets(THREE, allSurfaces, window.CONJURE_INSET_STANDOFF);
        if (JIT) this._jMark("prepL");                    // joinCorners + recoverMissing + snapInsets (local)
        this._renderLocal(allSurfaces);
        if (JIT) this._jMark("renderL");                  // apply-gate + setAttribute + geometry rebuild
        this._placeContent(allSurfaces);                  // director content → plane-relative anchors (docs §5)
        if (JIT && this._jCapT0) {                         // JITTER PROBE: cost of PassA→register→snap→render
          this._jMark("placeC");                           // per-content anchor solve + object3D writes
          var _jf = this._jFrames[this._jFrames.length - 1];
          if (_jf) _jf.cost = performance.now() - this._jCapT0;   // guest total (owner authoring adds more below)
        }

        if (!amOwner) { this.lastPost = time; return; }   // guest: rendered its own capture; never authors/posts

        // Join wall corners that fall a few cm short, then snap the insets (door/window/art) in front of
        // their wall toward the room interior. Both mutate `surfaces` in place (position, extent, holes).
        // The seed is built with the SAME treatment as the local render (joinCorners only, NO squaring) so
        // the shared model stays consistent with the raw geometry every headset draws (docs §9). Pure
        // geometry, unit-tested in client/room-snap.js.
        window.RoomSnap.joinCorners(THREE, surfaces);
        window.RoomSnap.sealWalls(THREE, surfaces, window.CONJURE_WALL_SEAL_TOL);   // same treatment as local render (§9.1)
        window.RoomSnap.snapInsets(THREE, surfaces, window.CONJURE_INSET_STANDOFF);
        // Author each captured inset's CORNER-RELATIVE anchor (§5.3 L2): its along-wall distances to the host
        // wall's corner points + its floor/ceiling edge distances — SHARED structural features, so any client
        // (esp. a guest whose wall scan centres differently) reconstructs the same physical spot, never riding
        // the wall's scan-artifact centroid. Done here — walls settled by joinCorners, hostWall set by
        // snapInsets — and BEFORE _lp/_lq are dropped. Attached to the posted surface (→ persisted in the seed)
        // AND stamped onto the inset's `_ref` entry, so NEXT capture's identity match reconstructs against it
        // from _ref (immediate) rather than the lagging seed (docs/specs/spaces-geometry.md §6.1).
        var authorCorners = window.RoomSnap.wallCorners(THREE, surfaces);
        var authorFloorY = null, authorCeilY = null, authorWallById = {}, refById = {};
        surfaces.forEach(function (s) {
          if (s.semantic === "floor") authorFloorY = s._lp.y;
          else if (s.semantic === "ceiling") authorCeilY = s._lp.y;
          else if (s.semantic === "wall") authorWallById[s.id] = s;
        });
        self._ref.forEach(function (r) { refById[r.id] = r; });
        surfaces.forEach(function (s) {
          if (!INSET_SEMS[s.semantic] || !s.hostWall) return;
          var hw = authorWallById[s.hostWall]; if (!hw) return;
          var anc = window.RoomSnap.authorInsetAnchor(THREE, s, hw, authorCorners.get(s.hostWall), authorFloorY, authorCeilY);
          s.along = anc.along; s.vertical = anc.vertical;
          if (anc.fallback) s.structuralFallback = anc.fallback;
          var rr = refById[s.id];                                        // stamp onto _ref for next capture's match
          if (rr) { rr.hostWall = s.hostWall; rr.anchor = { along: anc.along, vertical: anc.vertical }; }
        });
        surfaces.forEach(function (s) { delete s._lp; delete s._lq; });
        if (JIT) this._jMark("authO");                    // owner: joinCorners + snapInsets + inset-anchor authoring
        this.lastPost = time;
        var boundary = null;
        if (floor) { delete floor._area; boundary = floor; }

        // Client-side POST gate (§7): keep our authoritative model of the room (_known) and POST it ONLY when
        // it changes structurally — a new surface, a confirmed removal (debounced here, so a one-capture miss
        // never prunes), a large move, or a boundary change. A settled room posts nothing. The server mirrors
        // the posted set and prunes anything absent from it (we own removal-confidence → no server debounce).
        var self2 = this, curById = {};
        surfaces.forEach(function (s) { curById[s.id] = s; self2._absent[s.id] = 0; self2._known[s.id] = s; });
        var changed = false, reason = "";
        Object.keys(self2._known).forEach(function (id) {                 // debounced removals (3 misses)
          if (curById[id]) return;
          self2._absent[id] = (self2._absent[id] || 0) + 1;
          if (self2._absent[id] >= 3) { delete self2._known[id]; delete self2._absent[id]; changed = true; reason = reason || ("removed " + id); }
        });
        Object.keys(self2._known).forEach(function (id) {                 // new / large-move vs last POST
          if (changed) return;
          var p = self2._posted[id];
          if (!p) { changed = true; reason = "new " + id; }
          else if (self2._structMoved(p, self2._known[id])) { changed = true; reason = "moved " + id; }
        });
        var bstr = boundary ? JSON.stringify(boundary) : null;
        if (bstr && bstr !== self2._postedBoundary) { changed = true; reason = reason || "boundary"; }
        if (!changed) return;                                             // settled → nothing to POST

        var payload = Object.keys(self2._known).map(function (id) { return self2._known[id]; });
        self2._posted = {};
        payload.forEach(function (s) { self2._posted[s.id] = { position: s.position, rotation: s.rotation,
          extent: s.extent, holes: (s.holes || []).length, semantic: s.semantic }; });
        if (bstr) self2._postedBoundary = bstr;
        if (window.CONJURE_DEBUG_REGISTRATION) debugLog("post", payload.length + " surfaces (" + reason + ")", true);
        fetch("/space/capture", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Conjure-User": currentUser() || "" },
          body: JSON.stringify({ client_id: this.clientId, surfaces: payload, boundary: boundary, replace: true }),
        }).catch(function (e) { console.warn("[conjure] room post failed", e); });
        if (JIT) {                                        // owner: POST gate (structural diff + fetch dispatch)
          this._jMark("postO");
          var _jf2 = this._jFrames[this._jFrames.length - 1];
          if (_jf2) _jf2.cost = performance.now() - this._jCapT0;   // upgrade cost to owner total (incl. authoring)
        }
        };   // end finish (render continuation)

        // Dispatch the solve. Worker: post the compact planes + reference, hold the throttle, and let
        // _onSolve → finish apply the capture when the reply lands (a few ms later — imperceptible at 0.5 Hz).
        // No worker: solve synchronously and finish inline (unchanged behaviour, old per-capture cost).
        if (this._worker) {
          var seq = ++this._solveSeq;
          this._pendingSolve = { seq: seq, finish: finish, cur: cur };
          this._worker.postMessage({ type: "register", seq: seq,
            cur: cur.map(serCur), ref: this._ref.map(serRef), opts: window.CONJURE_REG });
          this.lastPost = time;                           // hold; the reply drives the render continuation
          return;
        }
        finish(this._register(cur));                      // no worker → synchronous fallback
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
      arCapable = !!supported;          // a headset, even before it enters a session (maybeSpawnGuest)
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
      sc.addEventListener("enter-vr", resetRigForSession);   // before onEnterAR: the frame must be
      sc.addEventListener("enter-vr", onEnterAR);             // origin-aligned before we select a space
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
