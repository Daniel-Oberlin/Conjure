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

test("the tip OFFSET is zero by default, so nothing is moved off the runtime's pose", () => {
  // The only defensible default. Everything else here is exact by construction — every joint lands on
  // the pose the runtime reports — and a non-zero default would quietly make that untrue.
  const { self, root, bones } = rig();
  self._collect();
  self._s = 1;
  assert.equal(self._tipOut('index-finger-tip', null), 0);
  const f = frame(0.03);
  self._drive(f);
  root.updateMatrixWorld(true);
  const p = new THREE.Vector3();
  JOINTS.forEach((n) => {
    p.setFromMatrixPosition(bones[n].matrixWorld);
    assert.ok(p.distanceTo(f.pos[n]) < 1e-6, `${n} moved with tipOut unset`);
  });
});

test("the tip offset pushes ONLY the five tips, along their own bone", () => {
  // Worn on a Quest 3 the real fingertips protruded ~5 mm past the virtual ones. This is a TUNABLE and
  // not a derivation: the gap is between the runtime's tip estimate and the wearer's actual
  // fingertip, and the runtime does not report the second.
  const { self, root, bones } = rig();
  self._collect();
  self._s = 1;
  self.data.tipOut = "5";
  const f = frame(0.03);
  self._drive(f);
  root.updateMatrixWorld(true);
  const p = new THREE.Vector3();
  JOINTS.forEach((n) => {
    p.setFromMatrixPosition(bones[n].matrixWorld);
    const moved = p.distanceTo(f.pos[n]);
    if (n.endsWith("-tip")) {
      assert.ok(Math.abs(moved - 0.005) < 1e-6, `${n} moved ${moved} m, wanted 0.005`);
      // ...along −Z, which is the bone direction away from the wrist, not just anywhere
      assert.ok(p.z < f.pos[n].z - 0.004, `${n} went the wrong way: ${p.z} vs ${f.pos[n].z}`);
    } else {
      assert.ok(moved < 1e-6, `${n} is not a tip and must not move`);
    }
  });
});

test("a preposterous offset is ignored rather than applied", () => {
  const { self } = rig();
  self.data.tipOut = "500";                     // half a metre of fingertip
  assert.equal(self._tipOut('index-finger-tip', null), 0);
  self.data.tipOut = "nonsense";
  assert.equal(self._tipOut('index-finger-tip', null), 0);
  self.data.tipOut = "-4";                      // pulling them IN is legitimate
  assert.ok(Math.abs(self._tipOut() + 0.004) < 1e-9);
});

test("the tip numbers are LOGGED, so the rule can be read rather than reasoned to", () => {
  // The first attempt scaled each tip by that finger's tracked/bind ratio, reasoning from phase 0
  // that `distal -> tip` was the unreliable segment. It is — but the ratio came out BELOW one, the
  // caps shrank, and the fingers went pointy. The reasoning was sound and the SIGN was an assumption.
  const { self } = rig();
  self._collect();
  const lines = [];
  global.window.CONJURE_DEBUG_LOG = true;
  const orig = console.log;
  console.log = (m) => lines.push(String(m));
  try {
    self._s = 1.02;
    self._report({ pos: frame(0.03).pos, quat: {}, rad: {} });
  } finally {
    console.log = orig;
    global.window.CONJURE_DEBUG_LOG = false;
  }
  const said = lines.join("\n");
  assert.match(said, /tracked/);
  assert.match(said, /bind/);
  assert.match(said, /ratio/);
  assert.match(said, /radius/, "the radius is the one candidate rule we can actually test");
  ["thumb", "index", "middle", "ring", "pinky"].forEach((f) => assert.match(said, new RegExp(f)));
});

test("changing tipOut takes effect with no re-init, so a number can be dialled live", () => {
  // The component has no `update` handler on purpose: A-Frame replaces `this.data` and `init` does
  // not re-run, so the skeleton, the bind pose and the captured rest survive a patch. `_drive` reads
  // `tipOut` per frame. If any of that changed, dialling a number in would cost a reload.
  assert.equal(DEF.update, undefined, "an update handler here would re-run nothing and risk re-init");
  const { self, root, bones } = rig();
  self._collect();
  self._s = 1;
  const bind = self._bind, rest = self._rest;
  const f = frame(0.03);
  const p = new THREE.Vector3();
  for (const [mm, want] of [["0", 0], ["5", 0.005], ["9", 0.009], ["-2", -0.002]]) {
    self.data.tipOut = mm;                        // what a patch does to `this.data`
    self._drive(f);
    root.updateMatrixWorld(true);
    p.setFromMatrixPosition(bones["index-finger-tip"].matrixWorld);
    const along = f.pos["index-finger-tip"].z - p.z;       // −Z is out, so this is positive when out
    assert.ok(Math.abs(along - want) < 1e-6, `tipOut ${mm} moved ${along} m, wanted ${want}`);
  }
  assert.equal(self._bind, bind, "the bind pose must survive a dial");
  assert.equal(self._rest, rest, "and so must the rest matrices, or it could not be put back");
});

test("`radius` mode pushes each finger out by its OWN reported radius", () => {
  // 8 mm landed it for one wearer, and a human fingertip radius is about 8 mm. There is a reason that
  // would be no coincidence: the WebXR tip joint sits at the CENTRE of the fingertip and
  // XRJointPose.radius is that fingertip's radius, so the surface is one radius further out. If that
  // is the rule then it is not one person's 8 mm — it is per-finger and it generalises to any hand.
  const { self, root, bones } = rig();
  self._collect();
  self._s = 1;
  const f = frame(0.03);
  f.rad = {};
  // A real hand's fingers differ: a thumb is fatter than a pinky, and that difference is the whole
  // test — a flat 8 mm cannot reproduce it, so the two modes are distinguishable on device.
  const radii = { "thumb-tip": 0.0105, "index-finger-tip": 0.0080, "middle-finger-tip": 0.0082,
                  "ring-finger-tip": 0.0075, "pinky-finger-tip": 0.0066 };
  Object.assign(f.rad, radii);

  self.data.tipOut = "radius";
  self._drive(f);
  root.updateMatrixWorld(true);
  const p = new THREE.Vector3();
  Object.entries(radii).forEach(([tip, r]) => {
    p.setFromMatrixPosition(bones[tip].matrixWorld);
    const along = f.pos[tip].z - p.z;
    assert.ok(Math.abs(along - r) < 1e-6, `${tip} moved ${along} m, wanted its radius ${r}`);
  });

  // A coefficient, for testing the multiple rather than assuming 1.
  self.data.tipOut = "radius:0.5";
  self._drive(f);
  root.updateMatrixWorld(true);
  p.setFromMatrixPosition(bones["thumb-tip"].matrixWorld);
  assert.ok(Math.abs((f.pos["thumb-tip"].z - p.z) - radii["thumb-tip"] / 2) < 1e-6);
});

test("`radius` falls back to NOTHING when the runtime supplies no radius", () => {
  // The WebXR spec lets a UA emulate a radius, but it does not have to be there — and a missing one
  // must mean "do not move it", never "move it by NaN".
  const { self, root, bones } = rig();
  self._collect();
  self._s = 1;
  self.data.tipOut = "radius";
  const f = frame(0.03);
  f.rad = { "index-finger-tip": null };
  assert.equal(self._tipOut("index-finger-tip", f), 0);
  assert.equal(self._tipOut("thumb-tip", f), 0, "absent, not merely null");
  self._drive(f);
  root.updateMatrixWorld(true);
  const p = new THREE.Vector3().setFromMatrixPosition(bones["index-finger-tip"].matrixWorld);
  assert.ok(p.distanceTo(f.pos["index-finger-tip"]) < 1e-6);
  // ...and a preposterous radius is refused too: 5 cm is not a fingertip.
  f.rad["index-finger-tip"] = 0.08;
  assert.equal(self._tipOut("index-finger-tip", f), 0);
});

test("`off` turns it off, and so do the other ways of saying nothing", () => {
  // An unparseable value already fell through to 0 by accident. Saying so on purpose is the
  // difference between a guard and a documented way to turn something off — and "how do I turn this
  // off?" is the first thing anyone asks after dialling it on.
  const { self } = rig();
  self._collect();
  const f = frame(0.03);
  f.rad = { "index-finger-tip": 0.008 };
  const tip = (v) => { self.data.tipOut = v; return self._tipOut("index-finger-tip", f); };
  assert.ok(Math.abs(tip("radius") - 0.008) < 1e-9, "on, so off means something");
  for (const v of ["off", "OFF", "none", "0", "", "  "]) {
    assert.equal(tip(v), 0, `${JSON.stringify(v)} should turn it off`);
  }
});

test("the THUMB takes its own coefficient, and that is anatomy rather than a preference", () => {
  // Measured on a Quest 3: one radius lands all four fingers and leaves the thumb slightly long. A
  // thumb has one fewer phalanx and a broad, flat pad — its reported radius is the half-width of that
  // pad, which OVERSTATES how far the tip protrudes, where on a rounder fingertip the two are nearly
  // the same. So it generalises for the same reason the radius rule does: it is about thumbs.
  const { self, root, bones } = rig();
  self._collect();
  self._s = 1;
  const f = frame(0.03);
  f.rad = { "thumb-tip": 0.0105, "index-finger-tip": 0.0080, "middle-finger-tip": 0.0082,
            "ring-finger-tip": 0.0075, "pinky-finger-tip": 0.0066 };
  self.data.tipOut = "radius";
  self.data.tipThumb = "0.8";
  self._drive(f);
  root.updateMatrixWorld(true);
  const p = new THREE.Vector3();
  const want = { "thumb-tip": 0.0105 * 0.8, "index-finger-tip": 0.0080,
                 "middle-finger-tip": 0.0082, "ring-finger-tip": 0.0075, "pinky-finger-tip": 0.0066 };
  Object.entries(want).forEach(([tip, d]) => {
    p.setFromMatrixPosition(bones[tip].matrixWorld);
    assert.ok(Math.abs((f.pos[tip].z - p.z) - d) < 1e-6, `${tip} moved ${f.pos[tip].z - p.z}, wanted ${d}`);
  });

  // It composes with the overall coefficient rather than replacing it.
  self.data.tipOut = "radius:0.5";
  assert.ok(Math.abs(self._tipOut("thumb-tip", f) - 0.0105 * 0.5 * 0.8) < 1e-9);
  assert.ok(Math.abs(self._tipOut("index-finger-tip", f) - 0.0080 * 0.5) < 1e-9);

  // ...and it does not touch the FLAT millimetre mode, where there is no radius to correct.
  self.data.tipOut = "8";
  assert.ok(Math.abs(self._tipOut("thumb-tip", f) - 0.008) < 1e-9);

  // A nonsense or out-of-range coefficient is ignored, not applied.
  self.data.tipOut = "radius";
  for (const bad of ["nonsense", "-1", "9"]) {
    self.data.tipThumb = bad;
    assert.ok(Math.abs(self._tipOut("thumb-tip", f) - 0.0105) < 1e-9, bad);
  }
});
