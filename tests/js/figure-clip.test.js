// Retargeting a captured clip onto a model (`client/figure-clip.js`), run with `node --test`.
//
// Every rule here is a symptom that was seen in a headset, and all four came from ONE playback:
// Alice, animated with `WhiteboardIdleFIXFIXU`, grew to a hundred times her size, left the room
// through the ceiling, lost her clothes, and then appeared pinned to the viewer — a figure that large
// has no parallax, so walking moves the world and not her.
//
// The cause was four characters of data. Her `RootNode` carries `scale 0.01`, the centimetre-to-metre
// conversion baked into the glTF, and the clip carries a `RootNode` scale track of `[1, 1, 1]`. The
// mixer wrote 1 over 0.01. Measured across 120 captured clips: 22,714 of 22,718 scale tracks never
// change value at all, so they animate nothing and every one is the same trap waiting for a model
// whose rest scale is not 1.

const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");

const components = {};
global.THREE = THREE;
global.window = { AFRAME: { components, registerComponent: (n, d) => { components[n] = d; } },
                  addEventListener() {}, removeEventListener() {} };
global.AFRAME = global.window.AFRAME;
const { retarget, isConstant } = require("../../client/figure-clip.js");

// A model shaped like the real one: a root carrying the unit conversion, a bone under it at a rest
// offset the clip will disagree with.
function model() {
  const root = new THREE.Object3D();
  root.name = "RootNode";
  root.scale.set(0.01, 0.01, 0.01);
  const bone = new THREE.Bone();
  bone.name = "CC_Base_BoneRoot";
  bone.position.set(0, 0, 0);
  const hip = new THREE.Bone();
  hip.name = "CC_Base_Hip";
  hip.position.set(0, 0, 94.888);
  bone.add(hip);
  root.add(bone);
  return { root, bone, hip };
}

const vec = (name, times, values) => new THREE.VectorKeyframeTrack(name, times, values);
const clipOf = (...tracks) => new THREE.AnimationClip("test", -1, tracks);

test("a scale track on the model root is dropped, so the unit conversion survives", () => {
  const { root } = model();
  const out = retarget(clipOf(vec("RootNode.scale", [0, 1], [1, 1, 1, 1, 1, 1])), root);
  assert.equal(out.clip.tracks.length, 0);
  assert.equal(out.why.rootTransform, 1);
  assert.deepEqual(root.scale.toArray(), [0.01, 0.01, 0.01], "0.01, not 1 — this is the 100x bug");
});

test("the model root's position and rotation are off limits too — that is placement", () => {
  const { root } = model();
  const q = new THREE.QuaternionKeyframeTrack("RootNode.quaternion", [0, 1],
                                              [0, 0, 0, 1, 0, 1, 0, 0]);
  const out = retarget(clipOf(vec("RootNode.position", [0, 1], [0, 0, 0, 5, 5, 5]), q), root);
  assert.equal(out.clip.tracks.length, 0);
  assert.equal(out.why.rootTransform, 2);
});

test("a CONSTANT scale track is dropped wherever it sits, and a moving one is kept", () => {
  const { root } = model();
  const flat = retarget(clipOf(vec("CC_Base_Hip.scale", [0, 1], [1, 1, 1, 1, 1, 1])), root);
  assert.equal(flat.clip.tracks.length, 0);
  assert.equal(flat.why.flatScale, 1);

  const real = retarget(clipOf(vec("CC_Base_Hip.scale", [0, 1], [1, 1, 1, 2, 2, 2])), root);
  assert.equal(real.clip.tracks.length, 1, "4 of 22,718 do animate; they are not thrown away");
});

test("a moving position track is re-based onto the target's rest, keeping every delta", () => {
  // The clip says the figure stood at x=314 in the scene it was captured from and swayed 22 units.
  // Alice's own rest for that bone is the origin.
  const { root, bone } = model();
  const out = retarget(
    clipOf(vec("CC_Base_BoneRoot.position", [0, 1, 2], [314, 0, 13, 325, 0, 13, 336, 2, 13])), root);
  assert.equal(out.why.rebased, 1);
  const v = Array.from(out.clip.tracks[0].values);
  assert.deepEqual(v.slice(0, 3), [0, 0, 0], "frame 0 pinned to the model's own rest");
  assert.deepEqual(v.slice(3, 6), [11, 0, 0], "and the sway is untouched");
  assert.deepEqual(v.slice(6, 9), [22, 2, 0]);
  assert.deepEqual(bone.position.toArray(), [0, 0, 0]);
});

test("re-basing uses the TARGET's rest, so a clip does not impose its own proportions", () => {
  const { root, hip } = model();                      // hip rests at z = 94.888
  const out = retarget(
    clipOf(vec("CC_Base_Hip.position", [0, 1], [0, 0, 92.111, 0, 0, 93.111])), root);
  const v = Array.from(out.clip.tracks[0].values);
  assert.ok(Math.abs(v[2] - 94.888) < 1e-4, "starts at Alice's hip, not the clip's");
  assert.ok(Math.abs(v[5] - 95.888) < 1e-4, "and rises by the same 1 unit");
});

test("a constant position track is a rest offset restated, and goes", () => {
  const { root } = model();
  const out = retarget(clipOf(vec("CC_Base_Hip.position", [0, 1], [0, 0, 92, 0, 0, 92])), root);
  assert.equal(out.clip.tracks.length, 0);
  assert.equal(out.why.restPosition, 1);
});

test("a track for a bone this model does not have is dropped, not warned about 51 times", () => {
  const { root } = model();
  const out = retarget(clipOf(vec("NoSuchBone.position", [0, 1], [0, 0, 0, 1, 1, 1])), root);
  assert.equal(out.clip.tracks.length, 0);
  assert.equal(out.why.missing, 1);
});

test("rotation is the motion, and passes through untouched", () => {
  const { root } = model();
  const q = new THREE.QuaternionKeyframeTrack("CC_Base_Hip.quaternion", [0, 1],
                                              [0, 0, 0, 1, 0, 1, 0, 0]);
  const out = retarget(clipOf(q), root);
  assert.equal(out.clip.tracks.length, 1);
  assert.deepEqual(Array.from(out.clip.tracks[0].values), [0, 0, 0, 1, 0, 1, 0, 0]);
});

test("isConstant tolerates float noise but not movement", () => {
  assert.equal(isConstant(vec("a.position", [0, 1], [1, 2, 3, 1, 2, 3])), true);
  assert.equal(isConstant(vec("a.position", [0, 1], [1, 2, 3, 1.000001, 2, 3])), true);
  assert.equal(isConstant(vec("a.position", [0, 1], [1, 2, 3, 1.5, 2, 3])), false);
});
