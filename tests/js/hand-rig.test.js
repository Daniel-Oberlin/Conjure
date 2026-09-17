// Unit tests for the `hand-rig` component (client/hand-rig.js), run with `node --test`.
//
// Stubbed the way figure.test.js stubs its world: an `AFRAME.registerComponent` that keeps the
// definition, and a fake entity carrying a real three skeleton. Everything worth testing here is
// arithmetic on bones — the XR read itself needs a headset and says so.
//
// Two claims, and both are things the design would be silently wrong about if they failed:
//
//   1. a bone's WORLD scale comes out exactly `s`, however deep it sits. The scale is composed in
//      world space and divided back through the parent, which is what stops it compounding down a
//      chain. Setting locals directly would make a four-deep finger s⁴ thick.
//   2. `s` is the median over the 14 real BONES. `wrist -> metacarpal` is a frame offset and
//      `distal -> tip` is a runtime-derived surface point; measured on a Quest 3, including them
//      spread the ratios 36% on a hand whose bones agree to 5%.
const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");

const components = {};
global.THREE = THREE;
global.window = { AFRAME: { components, registerComponent: (n, d) => { components[n] = d; } },
                  performance: { now: () => 0 } };
global.AFRAME = global.window.AFRAME;
global.AFRAME.THREE = THREE;
require("../../client/hand-rig.js");
const DEF = components["hand-rig"];

const JOINTS = [
  "wrist",
  "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip",
  "index-finger-metacarpal", "index-finger-phalanx-proximal", "index-finger-phalanx-intermediate",
  "index-finger-phalanx-distal", "index-finger-tip",
  "middle-finger-metacarpal", "middle-finger-phalanx-proximal", "middle-finger-phalanx-intermediate",
  "middle-finger-phalanx-distal", "middle-finger-tip",
  "ring-finger-metacarpal", "ring-finger-phalanx-proximal", "ring-finger-phalanx-intermediate",
  "ring-finger-phalanx-distal", "ring-finger-tip",
  "pinky-finger-metacarpal", "pinky-finger-phalanx-proximal", "pinky-finger-phalanx-intermediate",
  "pinky-finger-phalanx-distal", "pinky-finger-tip",
];

/**
 * A component instance over a skeleton of `layout` shape.
 *
 * `flat` is what every real hand model is — 25 siblings under one node. `chain` is the shape the
 * feature was planned for and no file has; it is here because the world-space composition is supposed
 * to work for both, and a test only on the flat case could not tell.
 */
function rig(layout = "flat", spread = 0.03) {
  const root = new THREE.Object3D();
  root.position.set(0.4, 1.2, -0.7);                 // the entity is somewhere, and it must not matter
  root.quaternion.setFromAxisAngle(new THREE.Vector3(0, 1, 0), 0.7);
  const bones = {};
  let prev = null;
  JOINTS.forEach((name, i) => {
    const b = new THREE.Bone();
    b.name = name;
    // Both layouts must put the joint at the SAME world position — `-spread * i` down −Z — or the
    // two halves of this file would be testing different skeletons. Siblings carry the whole offset;
    // a chain carries one step each and accumulates.
    b.position.set(0, 0, layout === "chain" ? -(i === 0 ? 0 : spread) : -spread * i);
    (layout === "chain" && prev ? prev : root).add(b);
    bones[name] = b;
    prev = b;
  });
  root.updateMatrixWorld(true);

  const self = Object.create(DEF);
  self.T = THREE;
  self.data = { hand: "left", joints: "" };
  self._bones = null;
  self._tmp = { m: new THREE.Matrix4(), inv: new THREE.Matrix4(), p: new THREE.Vector3(),
                q: new THREE.Quaternion(), sc: new THREE.Vector3() };
  self.el = {
    getObject3D: () => root,
    addEventListener() {}, removeEventListener() {},
    components: {},
  };
  self._suspendOthers = () => {};
  return { self, root, bones };
}

/** A frame of joint poses: every joint `step` apart down −Z from `origin`. */
function frame(step, origin = new THREE.Vector3(0, 0, 0)) {
  const pos = {}, quat = {};
  JOINTS.forEach((n, i) => {
    pos[n] = origin.clone().add(new THREE.Vector3(0, 0, -step * i));
    quat[n] = new THREE.Quaternion();
  });
  return { pos, quat };
}

test("all 25 joints are bound, and the bind pose is captured before anything writes", () => {
  const { self, bones } = rig();
  assert.ok(self._collect(), "the skeleton must be found");
  assert.equal(Object.keys(self._bind).length, 25);
  assert.equal(Object.keys(self._bones).length, 25);
  // The bind positions are WORLD, taken while the skeleton is still untouched — the only moment it is
  // guaranteed to be at rest. A local position would make `s` depend on where the entity stands.
  const p = new THREE.Vector3().setFromMatrixPosition(bones["middle-finger-tip"].matrixWorld);
  assert.ok(self._bind["middle-finger-tip"].distanceTo(p) < 1e-9);
  assert.equal(self._rest.size, 25, "and a rest matrix each, so it can be put back");
});

test("a uniform scale of the bind pose reads back as exactly that scale", () => {
  const { self } = rig("flat", 0.03);
  self._collect();
  for (const k of [0.8, 1.0, 1.37]) {
    assert.ok(Math.abs(self._scale(frame(0.03 * k).pos) - k) < 1e-9, `scale ${k}`);
  }
});

test("a scale outside the plausible range is refused, not applied", () => {
  // A bad frame — a hand half-tracked, a joint at the origin — must not flick the girth. Refusing
  // leaves the previous value standing, which is always closer to right than a wild one.
  const { self } = rig("flat", 0.03);
  self._collect();
  assert.equal(self._scale(frame(0.0005).pos), null, "far too small");
  assert.equal(self._scale(frame(0.5).pos), null, "far too large");
});

test("the wrist offset and the tip CANNOT move the scale, because they are not bones", () => {
  const { self } = rig("flat", 0.03);
  self._collect();
  const f = frame(0.03);                                  // exactly the bind pose: s must be 1
  assert.ok(Math.abs(self._scale(f.pos) - 1) < 1e-9);
  // Now wreck both ends — the wrist origin elsewhere, every tip pushed out — as a real runtime does.
  f.pos.wrist = new THREE.Vector3(0, 0, 0.09);
  JOINTS.filter((n) => n.endsWith("-tip")).forEach((n) => { f.pos[n].z -= 0.04; });
  assert.ok(Math.abs(self._scale(f.pos) - 1) < 1e-9,
    "s moved: a frame offset or a surface point got into the bone set");
});

test("every bone's WORLD scale is exactly s, at any depth and under any entity transform", () => {
  for (const layout of ["flat", "chain"]) {
    const { self, root, bones } = rig(layout);
    self._collect();
    self._s = 1.42;
    self._drive(frame(0.03));
    root.updateMatrixWorld(true);
    const sc = new THREE.Vector3();
    JOINTS.forEach((n) => {
      bones[n].matrixWorld.decompose(new THREE.Vector3(), new THREE.Quaternion(), sc);
      assert.ok(Math.abs(sc.x - 1.42) < 1e-6,
        `${layout}: ${n} world scale ${sc.x} — composing in world space is what stops s compounding`);
    });
  }
});

test("every bone lands on the joint pose it was given, whatever the entity transform is", () => {
  for (const layout of ["flat", "chain"]) {
    const { self, root, bones } = rig(layout);
    self._collect();
    self._s = 1;
    const f = frame(0.037, new THREE.Vector3(-0.2, 1.5, -1.1));
    self._drive(f);
    root.updateMatrixWorld(true);
    const p = new THREE.Vector3();
    JOINTS.forEach((n) => {
      p.setFromMatrixPosition(bones[n].matrixWorld);
      assert.ok(p.distanceTo(f.pos[n]) < 1e-6,
        `${layout}: ${n} landed ${p.distanceTo(f.pos[n])} m from its joint`);
    });
  }
});

test("restore puts every bone back on the pose it loaded with", () => {
  const { self, root, bones } = rig();
  self._collect();
  const before = JOINTS.map((n) => bones[n].position.clone());
  self._s = 1.2;
  self._drive(frame(0.041));
  assert.ok(bones.wrist.position.distanceTo(before[0]) > 1e-4, "it moved, or the test proves nothing");
  self._restore();
  JOINTS.forEach((n, i) => {
    assert.ok(bones[n].position.distanceTo(before[i]) < 1e-9, `${n} did not go back`);
    assert.ok(Math.abs(bones[n].scale.x - 1) < 1e-9, `${n} kept a scale`);
  });
});

test("a model missing a joint is REFUSED rather than driven in part", () => {
  // A partial skeleton leaves some bones driven and the rest at rest, which renders as a hand tearing
  // itself apart — far worse than a hand that simply stops.
  const { self, root, bones } = rig();
  root.remove(bones["ring-finger-phalanx-distal"]);
  root.updateMatrixWorld(true);
  self._bones = null;
  assert.equal(self._collect(), null);
});

test("the TIP bones get their own derived scale, and it does not touch anything else", () => {
  // Reported on device: the wearer's real fingertips protrude ~5 mm beyond the virtual ones. The tip
  // JOINT lands exactly where the runtime says — every joint does — so this is the flesh, not the
  // placement: the fingertip cap is bound to the tip bone and `s` only scales it by the GIRTH ratio.
  //
  // Phase 0 measured why it would not help: `distal -> tip` is the one segment where the model and the
  // runtime measure different things, and it was the widest-varying column of the whole reading.
  const { self } = rig("flat", 0.03);
  self._collect();

  // A frame at exactly the bind pose: every tip ratio is 1, so nothing is stretched.
  const same = self._tipScale(frame(0.03).pos);
  assert.equal(Object.keys(same).length, 5, "one per finger, and only the fingers");
  Object.values(same).forEach((r) => assert.ok(Math.abs(r - 1) < 1e-9));

  // Now push every tip 5 mm further out, which is the reported symptom.
  const f = frame(0.03);
  Object.keys(same).forEach((tip) => { f.pos[tip].z -= 0.005; });
  const longer = self._tipScale(f.pos);
  Object.entries(longer).forEach(([tip, r]) => {
    assert.ok(Math.abs(r - 0.035 / 0.03) < 1e-9, `${tip} ratio ${r}`);
  });

  // ...and the BONE scale is untouched by it, or the whole hand would swell to fix a fingertip.
  assert.ok(Math.abs(self._scale(f.pos) - 1) < 1e-9);
});

test("a tip ratio is applied to the tip BONE only, and cannot propagate", () => {
  const { self, root, bones } = rig("chain");          // the shape where propagation would show
  self._collect();
  self._s = 1;
  const f = frame(0.03);
  ["thumb-tip", "index-finger-tip", "middle-finger-tip", "ring-finger-tip", "pinky-finger-tip"]
    .forEach((tip) => { f.pos[tip].z -= 0.006; });
  self._tips = self._tipScale(f.pos);
  self._drive(f);
  root.updateMatrixWorld(true);
  const sc = new THREE.Vector3();
  JOINTS.forEach((n) => {
    bones[n].matrixWorld.decompose(new THREE.Vector3(), new THREE.Quaternion(), sc);
    const want = n.endsWith("-tip") ? 0.036 / 0.03 : 1;
    assert.ok(Math.abs(sc.x - want) < 1e-6, `${n} world scale ${sc.x}, wanted ${want}`);
  });
});

test("a wild tip ratio is refused — that is a tracking artefact, not a finger", () => {
  const { self } = rig("flat", 0.03);
  self._collect();
  const f = frame(0.03);
  f.pos["index-finger-tip"] = f.pos["index-finger-phalanx-distal"].clone();   // collapsed onto it
  const r = self._tipScale(f.pos);
  assert.ok(!("index-finger-tip" in r), "a zero-length tip must not scale the cap to nothing");
  assert.equal(Object.keys(r).length, 4, "and the other four are unaffected");
});
