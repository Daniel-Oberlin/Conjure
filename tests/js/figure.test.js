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

// three's angleTo goes through acos, which has a ~3e-8 noise floor near zero even for bit-identical
// quaternions (the sqrt cliff at dot = 1). Compare by dot instead: exact, and sign-insensitive, since
// q and -q are the same rotation.
function same(a, b) { return Math.abs(a.dot(b)) > 1 - 1e-12; }

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
  assert.ok(same(shoulder.quaternion, REST));
});

test("a pose composes onto the rest rotation instead of replacing it", () => {
  const { shoulder } = figure({ humanoid: MAP, axes: AXES, pose: JSON.stringify({ leftUpperArm: { bend: 0 } }) });
  // Zero degrees about a real axis must be a no-op, not a reset to identity.
  assert.ok(same(shoulder.quaternion, REST));

  const posed = figure({ humanoid: MAP, axes: AXES, pose: JSON.stringify({ leftUpperArm: { bend: 90 } }) });
  const bend = new THREE.Vector3().fromArray(JSON.parse(AXES).leftUpperArm.bend);
  const expected = new THREE.Quaternion().setFromAxisAngle(bend, Math.PI / 2).multiply(REST);
  assert.ok(same(posed.shoulder.quaternion, expected));
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
  assert.ok(same(shoulder.quaternion, REST));
});

test("removing the component puts the figure back too", () => {
  const { comp, shoulder } = figure({ humanoid: MAP, axes: AXES,
                                      pose: JSON.stringify({ leftUpperArm: { bend: 45 } }) });
  comp.remove();
  assert.ok(same(shoulder.quaternion, REST));
});

test("a bone with no axes is left alone rather than rotated about a guess", () => {
  const { shoulder } = figure({ humanoid: MAP, axes: JSON.stringify({}),
                                pose: JSON.stringify({ leftUpperArm: { bend: 90 } }) });
  assert.ok(same(shoulder.quaternion, REST));
});

test("a non-finite angle is ignored, not written into the scene graph", () => {
  // NaN blanks that branch of the scene graph and STAYS blanked; a stale snapshot can carry one in.
  const { shoulder } = figure({ humanoid: MAP, axes: AXES,
                                pose: '{"leftUpperArm": {"bend": null}}' });
  assert.ok(same(shoulder.quaternion, REST));
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
  assert.ok(same(shoulder.quaternion, REST));
});

// ---- aiming ---------------------------------------------------------------------------------
// The absolute half. Resolution happens here, on the client, while scripts/pose_test.py resolves the
// same request in Python to render the verification images — so the two must agree to the digit. The
// shared golden fixture is what enforces that; tests/test_figures.py reads the same file.
const GOLDEN = require("./fixtures/figure-pose-golden.json");

// A bone with an IDENTITY rest rotation, so whatever lands on `bone.quaternion` is the delta itself.
function delta(frame, request) {
  const bone = new THREE.Bone(), root = new THREE.Object3D();
  bone.name = "b";
  root.add(bone);
  const comp = Object.create(DEF);
  comp.el = { id: "f", getObject3D: () => root, addEventListener() {}, removeEventListener() {} };
  comp.data = { humanoid: JSON.stringify({ bone: "b" }), axes: JSON.stringify({ bone: frame }),
                pose: JSON.stringify({ bone: request }) };
  comp.init();
  return bone.quaternion;
}

GOLDEN.cases.forEach((c) => {
  test(`golden: ${c.name}`, () => {
    const want = new THREE.Quaternion(c.quat[0], c.quat[1], c.quat[2], c.quat[3]).normalize();
    const got = delta(GOLDEN.frames[c.frame], c.request);
    assert.ok(same(got, want), `${c.name}: got ${got.toArray()} want ${c.quat}`);
  });
});

test("the same aim lands the same way from a T-pose and an A-pose", () => {
  // 90 degrees of travel on one, 135 on the other, and the arm ends up pointing up on both — which is
  // the entire reason aiming exists.
  const up = new THREE.Vector3(0, 1, 0);
  ["t_pose_left_arm", "a_pose_left_arm"].forEach((key) => {
    const frame = GOLDEN.frames[key];
    const rest = new THREE.Vector3().fromArray(frame.rest).normalize();
    const landed = rest.applyQuaternion(delta(frame, { aim: "up" }));
    assert.ok(landed.distanceTo(up) < 1e-6, `${key} landed at ${landed.toArray()}`);
  });
});

test("an aim replaces bend and spread, and composes with turn", () => {
  const frame = GOLDEN.frames.a_pose_left_arm;
  const aim = delta(frame, { aim: "up" }).clone();
  assert.ok(same(delta(frame, { aim: "up", bend: 40 }), aim));
  assert.ok(delta(frame, { aim: "up", turn: 30 }).angleTo(aim) > 0.1);
});

test("a frame with no aiming vectors leaves the bone alone", () => {
  // Placed before aiming shipped. The server refuses these, but a stale snapshot can still carry one,
  // and rotating about a guessed axis is worse than not moving.
  const frame = Object.assign({}, GOLDEN.frames.left_leg);
  ["rest", "up", "forward", "out"].forEach((k) => delete frame[k]);
  assert.ok(same(delta(frame, { aim: "up" }), new THREE.Quaternion()));
});

test("a joint limit is applied on the client too, from the frame it was sent", () => {
  // The limits ride WITH the frame (conjure/figures.py holds the one table), so this file has the
  // arithmetic and no anatomy — nothing here can drift out of step with the server's idea of a knee.
  const shin = GOLDEN.frames.left_shin;
  const bent = delta(shin, { bend: -90 });         // a knee does not bend that way
  const legal = delta(shin, { bend: 90 });         // ...and this way is how a knee folds
  assert.ok(2 * Math.acos(Math.abs(bent.w)) * 180 / Math.PI < 6, "clamped to a few degrees");
  assert.ok(2 * Math.acos(Math.abs(legal.w)) * 180 / Math.PI > 89, "and the other way is untouched");
});

test("a frame with no limits is left alone rather than clamped against a guess", () => {
  const bare = Object.assign({}, GOLDEN.frames.left_shin);
  delete bare.limits;
  assert.ok(2 * Math.acos(Math.abs(delta(bare, { bend: -90 }).w)) * 180 / Math.PI > 89);
});

test("a bone that hangs outside its limb rides it instead of staying planted", () => {
  // An IK foot: parented to the root, weighted to the mesh. Rotate the shin and it stays where it was,
  // stretching the figure from a planted foot to a raised ankle — reported from the headset.
  const root = new THREE.Object3D(), shin = new THREE.Bone(), foot = new THREE.Bone();
  shin.name = "LowerLeg.L"; foot.name = "Foot.L";
  shin.position.set(0, 1, 0);
  foot.position.set(0, 0.1, 0);                       // parented to the ROOT, not to the shin
  root.add(shin); root.add(foot);
  root.updateMatrixWorld(true);
  const before = new THREE.Vector3().setFromMatrixPosition(foot.matrixWorld);

  const comp = Object.create(DEF);
  comp.el = { id: "f", getObject3D: () => root, addEventListener() {}, removeEventListener() {} };
  comp.data = {
    humanoid: JSON.stringify({ leftLowerLeg: "LowerLeg.L" }),
    axes: JSON.stringify({ leftLowerLeg: GOLDEN.frames.left_shin }),
    follows: JSON.stringify({ "Foot.L": "LowerLeg.L" }),
    pose: JSON.stringify({ leftLowerLeg: { bend: 90 } }),
  };
  comp.init();
  root.updateMatrixWorld(true);
  const after = new THREE.Vector3().setFromMatrixPosition(foot.matrixWorld);
  assert.ok(after.distanceTo(before) > 0.05, `the foot should travel with the shin, moved ${after.distanceTo(before)}`);
  // ...and it keeps its offset from the shin, rather than being dumped on top of it.
  const shinPos = new THREE.Vector3().setFromMatrixPosition(shin.matrixWorld);
  assert.ok(Math.abs(after.distanceTo(shinPos) - before.distanceTo(shinPos)) < 1e-6);
});
