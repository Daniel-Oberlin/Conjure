// Unit tests for the surface debug overlay (client/surface-overlay.js), run with `node --test`.
//
// The module is DOM-side, so it is not in the typechecked set and cannot be required for its exports — it
// is an IIFE that assigns `window.SurfaceOverlay`. The stub below is the whole environment it actually
// touches: a `window` for its flags, and a `document.querySelector("a-scene")` returning something with an
// `object3D` it can add groups to. That is little enough to be worth doing, because the alternative is
// zero coverage on the one thing that decides whether the overlay is drawing the truth.
//
// The load-bearing test is the last one: a seed rectangle, authored the way the owner authors it and drawn
// through the group's `Tmat⁻¹`, must land exactly on the same surface's live device rectangle. That is the
// overlay's entire claim — if it fails, the three layers are in different frames and every gap read off
// the display is fiction.
const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");
const RS = require("../../client/room-snap.js");

const D2R = Math.PI / 180;
const scene = new THREE.Group();

global.window = global.window || {};
global.document = {
  querySelector: (sel) => (sel === "a-scene" ? { object3D: scene } : null),
  getElementById: () => null,
  createElement: () => ({ setAttribute() {}, appendChild() {} }),
};
require("../../client/surface-overlay.js");
const SO = global.window.SurfaceOverlay;

// A captured plane as Pass A builds it: local +Y is the outward normal, local +Z points world-up, and the
// surface lies in local X-Z. `poly` is in that same local frame (y ~ 0 — see the backlog note that polyY is
// not a height).
function plane(yawDeg, pos, ext, polyScale) {
  const yaw = yawDeg * D2R;
  const Y = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw));
  const Z = new THREE.Vector3(0, 1, 0);
  const X = new THREE.Vector3().crossVectors(Y, Z);
  const quat = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(X, Y, Z));
  const hw = (ext[0] / 2) * (polyScale || 1), hh = (ext[1] / 2) * (polyScale || 1);
  return {
    pos: new THREE.Vector3(pos[0], pos[1], pos[2]), quat, ext,
    sem: "wall", orient: "vertical",
    poly: [{ x: -hw, y: 0, z: -hh }, { x: hw, y: 0, z: -hh }, { x: hw, y: 0, z: hh }, { x: -hw, y: 0, z: hh }],
  };
}

// The layer groups in the order surface-overlay adds them: poly, rect, seed.
const layer = (i) => scene.children[i].children[0];
const drawn = (i) => layer(i).geometry.drawRange.count;
// Every drawn vertex of a layer, in that layer's own container space, de-duplicated to a comparable set.
function verts(i) {
  const g = layer(i).geometry, n = g.drawRange.count, a = g.getAttribute("position");
  const out = new Set();
  for (let k = 0; k < n; k++) {
    out.add([a.getX(k), a.getY(k), a.getZ(k)].map((v) => v.toFixed(6)).join(","));
  }
  return out;
}

test("the overlay draws nothing at all until the flag is on", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = false;
  SO.setDevice(THREE, [plane(0, [0, 1, -2], [3, 2.4])]);
  SO.setSeed(THREE, [], null, true);
  assert.strictEqual(scene.children.length, 0, "no groups, no buffers, no cost when off");
  assert.strictEqual(SO.armed(), false);
});

test("armed, it lays out one group per layer with the right vertex counts", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = true;
  const cur = [plane(0, [0, 1, -2], [3, 2.4]), plane(90, [2, 1, 0], [4, 2.4])];
  SO.setDevice(THREE, cur);
  assert.strictEqual(scene.children.length, 3, "poly, rect, seed");
  // A closed loop of n points emits n segments = 2n vertices. Both planes here are 4-point quads.
  assert.strictEqual(drawn(0), 2 * 4 * 2, "polygon layer: 2 planes x 4 points x 2");
  assert.strictEqual(drawn(1), 2 * 4 * 2, "rect layer: 2 planes x 4 corners x 2");
  assert.strictEqual(SO.counts().poly, 2);
  assert.strictEqual(SO.counts().rect, 2);
  // Depth-independent and never frustum-culled: a stale bounding sphere silently dropping the whole layer
  // is the worst failure mode a diagnostic can have, so culling is off by construction.
  assert.strictEqual(layer(1).frustumCulled, false);
  assert.strictEqual(layer(1).material.depthTest, false);
});

test("the polygon and rect layers coincide for a rectangular plane, and separate when it isn't one", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = true;
  // extent IS the polygon's bounding box here, so the two layers describe the same four corners — the
  // expected reading in a healthy room, and the reason solo layer toggles exist (they overlap exactly).
  SO.setDevice(THREE, [plane(37, [1.3, 1.05, -2.7], [3.2, 2.4])]);
  assert.deepStrictEqual([...verts(0)].sort(), [...verts(1)].sort(),
    "a plane whose polygon IS its bounding box draws identically in both layers");

  // Now the polygon covers only 60% of the extent we derived — the hypothesis-1 shape. The layers must
  // visibly disagree, which is the whole point of drawing both.
  SO.setDevice(THREE, [plane(37, [1.3, 1.05, -2.7], [3.2, 2.4], 0.6)]);
  assert.notDeepStrictEqual([...verts(0)].sort(), [...verts(1)].sort());
});

test("the rect layer is built in the PLANE's frame, matching what pushSurface hands downstream", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = true;
  // Deliberately not via eulerYXZ: keeping the rect independent of the render's euler conversion is what
  // makes green-vs-amber isolate the bounding-box reduction and amber-vs-cyan isolate the conversion.
  // Asserted against the raw quaternion construction so a refactor cannot quietly reroute it.
  const p = plane(-114, [-0.4, 1.6, 2.2], [1.8, 2.2]);
  SO.setDevice(THREE, [p]);
  const hw = p.ext[0] / 2, hh = p.ext[1] / 2;
  const expect = new Set([[-hw, -hh], [hw, -hh], [hw, hh], [-hw, hh]].map(([x, z]) =>
    new THREE.Vector3(x, 0, z).applyQuaternion(p.quat).add(p.pos)
      .toArray().map((v) => v.toFixed(6)).join(",")));
  assert.deepStrictEqual([...verts(1)].sort(), [...expect].sort());
});

test("the seed layer, drawn through Tmat⁻¹, lands on the live device rectangle", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = true;
  // The overlay's entire claim, end to end through the real module. Pinned at BOUNDARY-FLIP magnitude
  // (167° yaw + metres of translation, §4.1) rather than a gentle offset, because a small-angle test
  // passes on the wrong composition.
  const p = plane(37, [1.3, 1.05, -2.7], [3.2, 2.4]);
  const Tmat = new THREE.Matrix4().compose(
    new THREE.Vector3(4.1, 0, -1.9),
    new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), 167 * D2R),
    new THREE.Vector3(1, 1, 1));

  // Author the seed exactly as the owner does: refPose() composes Tmat . <plane pose>, and pushSurface
  // stores eulerYXZ() of it (which is where the -90° X plane→a-plane conversion is applied).
  const refM = Tmat.clone().multiply(
    new THREE.Matrix4().compose(p.pos, p.quat, new THREE.Vector3(1, 1, 1)));
  const rp = new THREE.Vector3(), rq = new THREE.Quaternion(), rs = new THREE.Vector3();
  refM.decompose(rp, rq, rs);
  const seed = [{
    id: "real_wall_1",
    transform: { position: rp.toArray(), rotation: RS.eulerYXZ(THREE, rq) },
    components: { surface: { extent: p.ext } },
    meta: { real: true, semantic: "wall" },
  }];

  SO.setDevice(THREE, [p]);
  SO.setSeed(THREE, seed, Tmat, true);
  assert.strictEqual(drawn(2), 8);
  assert.strictEqual(SO.counts().seed, 1);

  // The seed group carries the ONE transform in the comparison. Apply it to the layer's own vertices and
  // they must coincide with the device rect, which is drawn in F_track with no transform at all.
  const group = scene.children[2];
  const g = layer(2).geometry, a = g.getAttribute("position");
  const moved = new Set();
  for (let k = 0; k < g.drawRange.count; k++) {
    moved.add(new THREE.Vector3(a.getX(k), a.getY(k), a.getZ(k))
      .applyMatrix4(group.matrix).toArray().map((v) => v.toFixed(5)).join(","));
  }
  const device = new Set([...verts(1)].map((s) =>
    s.split(",").map((v) => (+v).toFixed(5)).join(",")));
  assert.deepStrictEqual([...moved].sort(), [...device].sort(),
    "seed via Tmat⁻¹ == device rect; if this fails the layers are in different frames");

  // The group must be the only thing carrying it — baked vertices plus a transformed container would
  // apply the conversion twice.
  assert.strictEqual(group.matrixAutoUpdate, false);
  assert.ok(group.matrix.equals(Tmat.clone().invert()));
});

test("a void world clears the seed layer instead of leaving the last room's rectangles hanging", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = true;
  assert.ok(drawn(2) > 0, "previous test left seed geometry drawn");
  SO.clearSeed();
  assert.strictEqual(drawn(2), 0);
  assert.strictEqual(SO.counts().seed, 0);
});

test("the position buffer grows past its initial cap instead of truncating the room", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = true;
  // The rect layer starts at 1024 vertices = 128 surfaces. A silent truncation here would drop surfaces
  // from a diagnostic without saying so, which reads as "the device did not report them".
  const many = [];
  for (let i = 0; i < 200; i++) many.push(plane(i * 1.7, [i * 0.1, 1, -2], [1, 2]));
  SO.setDevice(THREE, many);
  assert.strictEqual(drawn(1), 200 * 8);
  assert.strictEqual(drawn(0), 200 * 8);
  assert.ok(layer(1).geometry.getAttribute("position").count >= 200 * 8);
  assert.strictEqual(SO.counts().rect, 200);
});

test("cycling the layers is a closed loop that reaches every layer alone and off", () => {
  window.CONJURE_DEBUG_SURFACE_OVERLAY = true;
  // Solo modes are not a nicety: a good lock puts all four layers within millimetres and they read as one
  // line, so being able to drop layers is how you tell which is which.
  let hits = { poly: 0, rect: 0, seed: 0 };
  const seen = [];
  const press = { started: (a) => a === "surfaces" };
  window.ConjurePointers = { controllers: () => [press] };
  const start = SO.mode();
  for (let i = 0; i < 5; i++) {
    SO.poll({});
    seen.push(SO.mode());
    if (layer(0).visible) hits.poly++;
    if (layer(1).visible) hits.rect++;
    if (layer(2).visible) hits.seed++;
  }
  assert.strictEqual(SO.mode(), start, "five presses return to where it started");
  assert.deepStrictEqual(new Set(seen), new Set(["all", "seed", "device", "poly", "off"]));
  assert.deepStrictEqual(hits, { poly: 2, rect: 2, seed: 2 }, "each layer shows in `all` plus its own solo");
  delete window.ConjurePointers;
});
