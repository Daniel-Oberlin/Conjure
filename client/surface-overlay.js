// Surface debug overlay (--debug-surface-overlay) — three wireframe layers drawn TOGETHER in the viewer's
// own frame (F_track), so the persisted seed, the live device capture and our rectangle approximation of it
// can be read against each other and against passthrough.
//
// WHY this exists: the seed is never rendered (docs/specs/spaces-geometry.md §5.4 — every client draws its
// own live capture), so nobody has ever *looked* at how far the shared model sits from the geometry the
// headset is reporting right now. Every number we have about that gap is a residual from inside the solver.
//
//   device polygon  bright green   plane.polygon at the raw pose      — what the Quest actually reported
//   device rect     amber          the AABB we derive from it         — what everything downstream uses
//   seed rect       magenta        the stored F_ref pose, via Tmat⁻¹  — the persisted shared model
//   (joined+sealed) cyan           already drawn by `surface-edges`   — what you are actually looking at
//
// The fourth layer needs no code: the existing outline IS the joined + sealed geometry. Note when reading
// it that it is post-deadband and post-slew, so it is what is on screen rather than what this capture
// computed, and that it draws only the outer loop — never the hole cuts.
//
// THREE FRAME RULES, all load-bearing:
//
//  1. The layers hang off the SCENE, not #world-root. Scene space is F_track (index.html: #rig sits at the
//     origin and world-root is held at identity in a captured space), so the device layers need no
//     transform at all. Parenting under #world-root would be wrong twice over — it is hidden when a lock
//     fails (§4.1.1) and parked at Tmat⁻¹ in a void world, and both are states worth inspecting.
//  2. The seed layer converts ONCE, on its container's matrix. Its vertices are baked from the stored seed
//     numbers untouched and the whole F_ref→F_track conversion is Tmat⁻¹ on the group. That keeps the frame
//     transform a single inspectable object instead of smearing it across ~60 vertex computations, and it
//     is the honest structure: there is exactly one transform in play and it is exactly one matrix.
//  3. The two device layers are both built in the PLANE's own frame (origin `pos`, orientation `quat`,
//     surface in local X-Z). Deliberately not via the euler conversion the render path uses, so
//     polygon-vs-rect isolates ONLY the AABB reduction, and rect-vs-cyan isolates the euler conversion
//     plus joinCorners plus sealWalls. Routing the rect through eulerYXZ would have folded those together
//     and made a conversion bug invisible here — which matters, since §2.2 names that conversion as a real
//     source of bugs.
//
// WHAT IT CANNOT TELL YOU: the map is locally non-rigid by up to ~9 cm and no rigid transform reconciles
// the two frames (§1), so the magenta-to-green gap is registration error PLUS genuine non-rigidity and the
// display cannot separate them by eye. It localises disagreement; it does not attribute it. That is why the
// registration status and residual summary are part of the HUD rather than a decoration — read the gap
// against them or you will read a good lock in a non-rigid room as a fault.
//
// COST: two buffer rewrites and one matrix write per capture (~0.5 Hz), no tick work beyond a rising-edge
// button check, and nothing at all when the flag is off. One preallocated LineSegments per layer rather
// than an object per surface — ~60 geometries every 2 s is exactly the allocation churn that
// docs/backlogs/spaces-geometry.md ("GC is not testable on Quest") says we cannot measure on device.

(function () {
  "use strict";
  if (window.SurfaceOverlay) return;

  // Saturated, distinct HUES rather than shades of one colour. Grouping the two device layers as light/dark
  // green reads better on paper and worse in the headset: passthrough is low-contrast grey-brown and a
  // desaturated line disappears into it. Cyan is taken by `surface-edges`.
  var COLOR = { poly: "#00ff66", rect: "#ffb300", seed: "#ff2fa0" };
  var ORDER = { poly: 1003, rect: 1002, seed: 1001 };     // all above the cyan edges' 999

  // Cycle order puts `all` first: passing the flag is a deliberate act, so the useful thing on entry is the
  // comparison, not an empty scene. The solo modes matter more than they look — when the lock is good all
  // four layers land within millimetres and read as ONE line, so dropping layers is how you tell which is
  // which.
  var MODES = ["all", "seed", "device", "poly", "off"];
  var VIS = {
    all:    { poly: true,  rect: true,  seed: true  },
    seed:   { poly: false, rect: false, seed: true  },
    device: { poly: false, rect: true,  seed: false },
    poly:   { poly: true,  rect: false, seed: false },
    off:    { poly: false, rect: false, seed: false },
  };

  var layers = null;          // { poly, rect, seed } — built lazily on the first update
  var mode = "all";
  var counts = { poly: 0, rect: 0, seed: 0 };
  var seedStale = false;      // the seed layer is drawn through the last-known Tmat, which may be stale
  var hudText = "";

  function armed() { return !!window.CONJURE_DEBUG_SURFACE_OVERLAY; }

  // One layer = a group (the only thing that may carry a transform) holding one LineSegments over a
  // preallocated position buffer. `frustumCulled = false` because the bounding sphere would need
  // recomputing on every rewrite and culling saves nothing for room-scale debug lines — a stale sphere
  // silently drops the whole layer, which is the worst failure mode a diagnostic can have.
  function makeLayer(THREE, scene, key, cap) {
    var geo = new THREE.BufferGeometry();
    var buf = new Float32Array(cap * 3);
    geo.setAttribute("position", new THREE.BufferAttribute(buf, 3));
    geo.setDrawRange(0, 0);
    var seg = new THREE.LineSegments(geo, new THREE.LineBasicMaterial({
      color: COLOR[key], depthTest: false, depthWrite: false, transparent: true }));
    seg.renderOrder = ORDER[key];
    seg.frustumCulled = false;
    var group = new THREE.Group();
    group.matrixAutoUpdate = false;         // identity for the device layers; Tmat⁻¹ for the seed
    group.add(seg);
    scene.add(group);
    return { group: group, seg: seg, geo: geo, buf: buf, cap: cap };
  }

  function ensure(THREE) {
    if (layers) return layers;
    var sceneEl = document.querySelector("a-scene");
    if (!sceneEl || !sceneEl.object3D) return null;
    var scene = sceneEl.object3D;
    layers = {
      poly: makeLayer(THREE, scene, "poly", 2048),
      rect: makeLayer(THREE, scene, "rect", 1024),
      seed: makeLayer(THREE, scene, "seed", 1024),
    };
    applyMode();
    return layers;
  }

  // Grow the position buffer (doubling) when a room needs more vertices than the initial cap. Rare — a
  // 58-surface room needs ~500 for the rect layers — and cheaper to handle than to size for the worst case.
  function room(THREE, L, verts) {
    if (verts <= L.cap) return;
    var cap = L.cap;
    while (cap < verts) cap *= 2;
    L.cap = cap;
    L.buf = new Float32Array(cap * 3);
    L.geo.setAttribute("position", new THREE.BufferAttribute(L.buf, 3));
  }

  // A loop has to be fully transformed before it can be emitted as SEGMENTS (each point appears twice, as
  // one segment's end and the next one's start), so the corners need somewhere to live. This pool is that
  // somewhere, reused across captures: without it each capture allocated ~500 short-lived Vector3s, which
  // is small but is exactly the allocation churn we cannot measure on device ("GC is not testable on
  // Quest") and pointless to add inside a diagnostic. Grows to the largest polygon ever seen, then stops.
  var pool = [];
  function corner(THREE, i) {
    return pool[i] || (pool[i] = new THREE.Vector3());
  }

  // Write a closed loop of already-transformed pool points [0, n) as line segments: n points → n segments.
  function writeLoop(buf, at, n) {
    for (var i = 0; i < n; i++) {
      var a = pool[i], b = pool[(i + 1) % n];
      buf[at++] = a.x; buf[at++] = a.y; buf[at++] = a.z;
      buf[at++] = b.x; buf[at++] = b.y; buf[at++] = b.z;
    }
    return at;
  }

  function commit(L, verts) {
    L.geo.setDrawRange(0, verts);
    var attr = /** @type {any} */ (L.geo.getAttribute("position"));
    attr.needsUpdate = true;
  }

  function applyMode() {
    if (!layers) return;
    var v = VIS[mode] || VIS.off;
    layers.poly.seg.visible = !!v.poly && armed();
    layers.rect.seg.visible = !!v.rect && armed();
    layers.seed.seg.visible = !!v.seed && armed();
  }

  // ---- device layers: Pass A's `cur`, in F_track ------------------------------------------------------
  //
  // Both loops build in the plane's own frame (rule 3 above): a polygon point is (pt.x, 0, pt.z) and an
  // AABB corner is (±ext[0]/2, 0, ±ext[1]/2), each rotated by the plane's quaternion and offset by its
  // pose origin. The rect uses the AABB's DIMENSIONS centred on the pose ORIGIN because that is exactly
  // what Pass A hands downstream — drawing it centred on the AABB's own midpoint instead would hide the
  // very displacement this layer exists to show.
  function setDevice(THREE, cur) {
    if (!armed()) return;
    var L = ensure(THREE);
    if (!L) return;

    var need = 0, i, j;
    for (i = 0; i < cur.length; i++) need += 2 * ((cur[i].poly && cur[i].poly.length >= 3) ? cur[i].poly.length : 0);
    room(THREE, L.poly, need);
    var at = 0, np = 0;
    for (i = 0; i < cur.length; i++) {
      var c = cur[i], poly = c.poly;
      if (!poly || poly.length < 3) continue;
      for (j = 0; j < poly.length; j++) {
        corner(THREE, j).set(poly[j].x, 0, poly[j].z).applyQuaternion(c.quat).add(c.pos);
      }
      at = writeLoop(L.poly.buf, at, poly.length);
      np++;
    }
    commit(L.poly, at / 3);
    counts.poly = np;

    room(THREE, L.rect, cur.length * 8);
    at = 0;
    var RECT = [[-1, -1], [1, -1], [1, 1], [-1, 1]];
    for (i = 0; i < cur.length; i++) {
      var s = cur[i], hw = s.ext[0] / 2, hh = s.ext[1] / 2;
      for (j = 0; j < 4; j++) {
        corner(THREE, j).set(RECT[j][0] * hw, 0, RECT[j][1] * hh).applyQuaternion(s.quat).add(s.pos);
      }
      at = writeLoop(L.rect.buf, at, 4);
    }
    commit(L.rect, at / 3);
    counts.rect = cur.length;
  }

  // ---- seed layer: stored F_ref poses, converted once on the group matrix --------------------------
  //
  // A seed surface is a-plane form: its rectangle lies in local X-Y with normal +Z, and its rotation is
  // euler DEGREES in YXZ order — A-Frame's order, and reading it as XYZ corrupts the normal of any
  // multi-axis (tilted) surface, which is the bug room-snap's surfaceToRef comment records. Same
  // reconstruction here.
  //
  // Vertices are rebuilt every capture rather than only when the snapshot changes. The plan was to gate
  // them on a change signature; measured, 58 rects is ~460 vertex writes and the signature costs more code
  // than it saves work. Correctness lives on the matrix, which has to be rewritten every capture anyway.
  function setSeed(THREE, docSurfaces, Tmat, locked) {
    if (!armed()) return;
    var L = ensure(THREE);
    if (!L) return;
    seedStale = !locked;
    var surfaces = docSurfaces || [];
    room(THREE, L.seed, surfaces.length * 8);
    var q = new THREE.Quaternion(), p = new THREE.Vector3(), eu = new THREE.Euler();
    var d2r = THREE.MathUtils.degToRad, at = 0, n = 0;
    var RECT = [[-1, -1], [1, -1], [1, 1], [-1, 1]];
    for (var i = 0; i < surfaces.length; i++) {
      var e = surfaces[i], t = e.transform || {};
      var pos = t.position, rot = t.rotation || [0, 0, 0];
      var ext = ((e.components || {}).surface || {}).extent;
      if (!pos || !ext) continue;
      eu.set(d2r(rot[0] || 0), d2r(rot[1] || 0), d2r(rot[2] || 0), "YXZ");
      q.setFromEuler(eu);
      p.set(pos[0], pos[1], pos[2]);
      var hw = ext[0] / 2, hh = ext[1] / 2;
      // a-plane form: the rectangle is in local X-Y with normal +Z (unlike a captured plane's X-Z).
      for (var j = 0; j < 4; j++) {
        corner(THREE, j).set(RECT[j][0] * hw, RECT[j][1] * hh, 0).applyQuaternion(q).add(p);
      }
      at = writeLoop(L.seed.buf, at, 4);
      n++;
    }
    commit(L.seed, at / 3);
    counts.seed = n;

    // The one conversion. Tmat is F_track → F_ref, so its inverse takes the stored seed into the frame the
    // other two layers (and passthrough) live in.
    if (Tmat) {
      L.seed.group.matrix.copy(Tmat).invert();
      L.seed.group.matrixWorldNeedsUpdate = true;
    }
  }

  // A void world has no seed to compare against, and a world switch must not leave the previous room's
  // rectangles hanging in the new one.
  function clearSeed() {
    if (!layers) return;
    commit(layers.seed, 0);
    counts.seed = 0;
  }

  // ---- toggle + HUD ---------------------------------------------------------------------------------

  // Rising edge of the `surfaces` action (config.py DEFAULT_BINDINGS, default `a`). Input is already read
  // and cached per XRFrame by controller-beams, so the per-frame cost here is a button comparison.
  function poll(sceneEl) {
    if (!armed()) return;
    var CP = window.ConjurePointers;
    if (!CP) return;
    var ptrs;
    try { ptrs = CP.controllers(sceneEl); } catch (e) { return; }
    for (var i = 0; i < ptrs.length; i++) {
      if (!ptrs[i].started("surfaces")) continue;
      mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
      applyMode();
      hud(null);
      if (window.CONJURE_DEBUG_LOG) { try { console.log("[overlay] mode=" + mode); } catch (e) {} }
      return;
    }
  }

  // Head-locked, and its own entity rather than sharing the registration HUD — the two flags are
  // independent, so --debug-surface-overlay alone must still show a readout. Sits just above the coloc HUD
  // so both are legible when both flags are on.
  function hud(reg) {
    if (!armed()) return;
    if (reg) hudText = reg;
    var el = document.getElementById("surfovl-hud");
    if (!el) {
      var cam = document.querySelector("a-camera") || document.querySelector("[camera]");
      if (!cam) return;
      el = document.createElement("a-entity");
      el.id = "surfovl-hud";
      el.setAttribute("position", "0 -0.28 -1");
      el.setAttribute("text", { value: "", align: "center", color: "#ffb300", width: 1.2, baseline: "center" });
      el.setAttribute("overlay", "");
      cam.appendChild(el);
    }
    var line = "overlay " + mode + "  poly=" + counts.poly + " rect=" + counts.rect
      + " seed=" + counts.seed + (seedStale ? " STALE-T" : "");
    el.setAttribute("text", "value", line + (hudText ? "\n" + hudText : ""));
  }

  window.SurfaceOverlay = {
    armed: armed,
    setDevice: setDevice,
    setSeed: setSeed,
    clearSeed: clearSeed,
    poll: poll,
    hud: hud,
    mode: function () { return mode; },
    counts: function () { return counts; },
  };
})();
