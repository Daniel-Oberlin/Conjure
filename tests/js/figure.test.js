// Unit tests for the `figure` component (client/figure.js), run with `node --test`.
//
// The component is DOM-side, so it is stubbed the way surface-overlay.test.js stubs its world: an
// `AFRAME.registerComponent` that just keeps the definition, and a fake element carrying a three
// skeleton. That is enough, because everything worth testing here is arithmetic on bones.
//
// Two claims, and both were live bugs on device:
//
//   1. a pose COMPOSES onto the bone's rest rotation. Writing the rotation outright discards whatever
//      the rigger authored — 177 degrees on Grace's `thigh.fk.L` — so the leg flipped before the
//      requested angle was added, and clearing the pose left it flipped.
//   2. the axes shipped with the pose are what it rotates about, so "bend" is forward for every rig
//      rather than whatever that bone's own local X happens to mean.
const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");

const components = {};
global.THREE = THREE;
global.window = { AFRAME: { components, registerComponent: (n, d) => { components[n] = d; } } };
global.AFRAME = global.window.AFRAME;
require("../../client/figure.js");
const DEF = components.figure;

// A two-bone arm: shoulder at the origin with a non-identity REST rotation, elbow 1 m out along +X.
// The rest rotation is the whole point — an identity one would let the broken implementation pass.
const REST = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 3);

function figure(data) {
  const shoulder = new THREE.Bone(), elbow = new THREE.Bone(), root = new THREE.Object3D();
  shoulder.name = "upper_arm.L";
  elbow.name = "forearm.L";
  elbow.position.set(1, 0, 0);
  shoulder.quaternion.copy(REST);
  shoulder.add(elbow);
  root.add(shoulder);
  const comp = Object.create(DEF);
  comp.el = { id: "fig", getObject3D: () => root, addEventListener() {}, removeEventListener() {} };
  comp.data = Object.assign({ humanoid: "", axes: "", pose: "" }, data);
  comp.init();
  return { comp, shoulder, elbow, root };
}

const MAP = JSON.stringify({ leftUpperArm: "upper_arm.L" });
// The axes as the server measures them for THIS bone, in its parent's frame. The rest rotation puts the
// forearm along (cos60, sin60, 0), so that is the twist axis, and bend is perpendicular to it in the
// sagittal sense: cross(direction, forward). Deriving them rather than writing round numbers is the
// point — round numbers would only be right for a bone with no rest rotation, which is the case that
// hid the bug.
const DIR = new THREE.Vector3(1, 0, 0).applyQuaternion(REST);
const FORWARD = new THREE.Vector3(0, 0, 1), LEFT = new THREE.Vector3(1, 0, 0);
const AXES = JSON.stringify({ leftUpperArm: {
  bend: new THREE.Vector3().crossVectors(DIR, FORWARD).normalize().toArray(),
  spread: new THREE.Vector3().crossVectors(DIR, LEFT).normalize().toArray(),
  turn: DIR.toArray(),
} });

function elbowWorld(root, elbow) {
  root.updateMatrixWorld(true);
  return new THREE.Vector3().setFromMatrixPosition(elbow.matrixWorld);
}

test("an unposed figure is left exactly as the model authored it", () => {
  const { shoulder } = figure({ humanoid: MAP, axes: AXES });
  assert.ok(shoulder.quaternion.angleTo(REST) < 1e-9);
});

test("a pose composes onto the rest rotation instead of replacing it", () => {
  const { shoulder } = figure({ humanoid: MAP, axes: AXES, pose: JSON.stringify({ leftUpperArm: { bend: 0 } }) });
  // Zero degrees about a real axis must be a no-op, not a reset to identity.
  assert.ok(shoulder.quaternion.angleTo(REST) < 1e-9);

  const posed = figure({ humanoid: MAP, axes: AXES, pose: JSON.stringify({ leftUpperArm: { bend: 90 } }) });
  const bend = new THREE.Vector3().fromArray(JSON.parse(AXES).leftUpperArm.bend);
  const expected = new THREE.Quaternion().setFromAxisAngle(bend, Math.PI / 2).multiply(REST);
  assert.ok(posed.shoulder.quaternion.angleTo(expected) < 1e-9);
});

test("bend swings the joint below it forward, whatever the rest rotation was", () => {
  const rest = figure({ humanoid: MAP, axes: AXES });
  const before = elbowWorld(rest.root, rest.elbow);
  const bent = figure({ humanoid: MAP, axes: AXES, pose: JSON.stringify({ leftUpperArm: { bend: 90 } }) });
  const after = elbowWorld(bent.root, bent.elbow);
  assert.ok(after.z - before.z > 0.5, `elbow should travel forward, went ${after.toArray()}`);
});

test("clearing a pose restores the rest rotation, not identity", () => {
  const { comp, shoulder } = figure({ humanoid: MAP, axes: AXES,
                                      pose: JSON.stringify({ leftUpperArm: { bend: 90 } }) });
  assert.ok(shoulder.quaternion.angleTo(REST) > 0.5);
  comp.data.pose = "";
  comp.update();
  assert.ok(shoulder.quaternion.angleTo(REST) < 1e-9);
});

test("removing the component puts the figure back too", () => {
  const { comp, shoulder } = figure({ humanoid: MAP, axes: AXES,
                                      pose: JSON.stringify({ leftUpperArm: { bend: 45 } }) });
  comp.remove();
  assert.ok(shoulder.quaternion.angleTo(REST) < 1e-9);
});

test("a bone with no axes is left alone rather than rotated about a guess", () => {
  const { shoulder } = figure({ humanoid: MAP, axes: JSON.stringify({}),
                                pose: JSON.stringify({ leftUpperArm: { bend: 90 } }) });
  assert.ok(shoulder.quaternion.angleTo(REST) < 1e-9);
});

test("a non-finite angle is ignored, not written into the scene graph", () => {
  // NaN blanks that branch of the scene graph and STAYS blanked; a stale snapshot can carry one in.
  const { shoulder } = figure({ humanoid: MAP, axes: AXES,
                                pose: '{"leftUpperArm": {"bend": null}}' });
  assert.ok(shoulder.quaternion.angleTo(REST) < 1e-9);
});

test("turn twists about the bone's own length and leaves the joint below it where it was", () => {
  const rest = figure({ humanoid: MAP, axes: AXES });
  const before = elbowWorld(rest.root, rest.elbow);
  const turned = figure({ humanoid: MAP, axes: AXES, pose: JSON.stringify({ leftUpperArm: { turn: 60 } }) });
  assert.ok(elbowWorld(turned.root, turned.elbow).distanceTo(before) < 1e-9);
  assert.ok(turned.shoulder.quaternion.angleTo(REST) > 0.5, "the bone itself did rotate");
});

test("a pose that works out to no rotation puts the bone back on its rest pose", () => {
  // The server strips a cleared bone, but a stale snapshot can still carry {"bend": 0} — and leaving
  // the previous rotation stuck would look exactly like the pose being ignored.
  const { comp, shoulder } = figure({ humanoid: MAP, axes: AXES,
                                      pose: JSON.stringify({ leftUpperArm: { bend: 90 } }) });
  comp.data.pose = JSON.stringify({ leftUpperArm: {} });
  comp.update();
  assert.ok(shoulder.quaternion.angleTo(REST) < 1e-9);
});
