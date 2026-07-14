// Unit tests for the pure world-model / presence helpers (client/world-model.js), run with `node --test`.
// These are the DOM-free, A-Frame-free bits extracted from conjure-client.js: the patch-env nesting, the
// holed-wall string encoding, and the quaternion/euler pose math for avatars + guest spawn placement.
const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");
const WM = require("../../client/world-model.js");

test("nest rebuilds a nested object from flat dotted-path keys", () => {
  const out = WM.nest({ "room.active": true, "room.edgeColor": "#fff", "sky.color": "#000", "fog": 0.1 });
  assert.deepStrictEqual(out, { room: { active: true, edgeColor: "#fff" }, sky: { color: "#000" }, fog: 0.1 });
});

test("nest merges siblings under a shared parent (no clobber)", () => {
  const out = WM.nest({ "a.b.c": 1, "a.b.d": 2, "a.e": 3 });
  assert.deepStrictEqual(out, { a: { b: { c: 1, d: 2 }, e: 3 } });
});

test("holesAttr encodes openings as a fixed-4dp 'x y w h, …' string; empty ⇒ ''", () => {
  assert.strictEqual(WM.holesAttr([]), "");
  assert.strictEqual(WM.holesAttr(null), "");
  assert.strictEqual(
    WM.holesAttr([{ x: 0.5, y: -1, w: 0.9, h: 2.1 }, { x: -1.25, y: 0, w: 0.6, h: 0.6 }]),
    "0.5000 -1.0000 0.9000 2.1000, -1.2500 0.0000 0.6000 0.6000");
});

test("v3 joins an [x,y,z] array to a string and passes non-arrays through", () => {
  assert.strictEqual(WM.v3([1, 2, 3]), "1 2 3");
  assert.strictEqual(WM.v3("#ff0000"), "#ff0000");
});

test("avatarAim: identity orientation looks down -Z → yaw 0, pitch 0", () => {
  const aim = WM.avatarAim(THREE, [0, 0, 0, 1]);
  assert.ok(Math.abs(aim.yawDeg) < 1e-6);
  assert.ok(Math.abs(aim.pitchDeg) < 1e-6);
});

test("avatarAim: a +90° yaw about Y reads back as 90° yaw", () => {
  const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI / 2);
  const aim = WM.avatarAim(THREE, [q.x, q.y, q.z, q.w]);
  assert.ok(Math.abs(Math.abs(aim.yawDeg) - 90) < 1e-4);
  assert.ok(Math.abs(aim.pitchDeg) < 1e-4);
});

test("avatarAim: looking up (pitch about X) yields a positive pitch, yaw unchanged", () => {
  const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 6);  // 30° up
  const aim = WM.avatarAim(THREE, [q.x, q.y, q.z, q.w]);
  assert.ok(Math.abs(aim.pitchDeg - 30) < 1e-4);
  assert.ok(Math.abs(aim.yawDeg) < 1e-4);
});

test("spawnRight: with identity pose, 'right' is +X and the spawn drops to the floor (y=0)", () => {
  const sp = WM.spawnRight(THREE, { p: [2, 1.6, -3], q: [0, 0, 0, 1] }, 1.2);
  assert.ok(Math.abs(sp[0] - 3.2) < 1e-4);   // 2 + 1.2 to the right
  assert.strictEqual(sp[1], 0);              // floored
  assert.ok(Math.abs(sp[2] - (-3)) < 1e-4);  // no z shift when facing -Z
});

test("spawnRight: a 90°-yawed owner puts 'right' along -Z", () => {
  const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI / 2);
  const sp = WM.spawnRight(THREE, { p: [0, 1.6, 0], q: [q.x, q.y, q.z, q.w] }, 1.0);
  assert.ok(Math.abs(sp[0]) < 1e-4);
  assert.ok(Math.abs(sp[2] - (-1)) < 1e-4);  // right rotated to -Z
});
