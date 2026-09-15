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

// ---- the VOICE's lifecycle -----------------------------------------------------------------------
//
// Reported from a headset: "when I issue new commands, sometimes I think the old sound is still
// playing." It was, and it stacked — one extra copy per call, playing on under everything after it.

// Enough of the component to exercise `_audio` without a browser. `made` records every media element
// ever constructed, so the test can ask what is STILL PLAYING rather than what is referenced.
function audioHarness(url) {
  const made = [];
  global.Audio = function () {
    const el = { paused: true, src: "", currentTime: 0, duration: 4, readyState: 1, loop: false,
                 play() { this.paused = false; return Promise.resolve(); },
                 pause() { this.paused = true; },
                 addEventListener() {} };
    made.push(el);
    return el;
  };
  // A REAL Object3D underneath: `Object3D.add` calls `removeFromParent` on what it is given, so a
  // hand-rolled `{ parent: null }` passes a test the browser would fail. Third time a stub laxer than
  // the platform has hidden a live path, so the rule is to subclass the real thing and add only what
  // is missing.
  THREE.PositionalAudio = function () {
    const o = new THREE.Object3D();
    o.setMediaElementSource = function () {};
    return o;
  };
  const object3D = new THREE.Object3D();
  const listener = { context: { state: "running" }, parent: {} };
  const self = {
    el: { sceneEl: { audioListener: listener, camera: null, addEventListener() {} },
          object3D: object3D, id: "fig", emit() {} },
    data: { playing: true, audio: url, loop: true, clip: "c.glb", speed: 1 },
    _started: 0, _sound: null, _media: null, _audioUrl: "", _seek: null,
    _silence: components["figure-clip"]._silence,
    _audio: components["figure-clip"]._audio
  };
  return { self, made, playing: () => made.filter((m) => !m.paused).length };
}

test("calling _audio again for the SAME voice re-aims it instead of starting a second copy", () => {
  const h = audioHarness("/assets/voice.mp3");
  h.self._audio.call(h.self);
  assert.strictEqual(h.made.length, 1, "one element for one voice");
  assert.strictEqual(h.playing(), 1);

  // The path that actually happens: `_audio` is called from `update` AND again when the clip finishes
  // loading, and `update` only tears down when the CLIP ID changes. Replaying one clip at a different
  // speed goes past both guards, so this used to build a second element and overwrite `_media` with
  // it — leaving the first unreachable, and therefore unpausable, and therefore audible forever.
  h.self._audio.call(h.self);
  h.self._audio.call(h.self);
  assert.strictEqual(h.made.length, 1, "no second element for a voice already playing");
  assert.strictEqual(h.playing(), 1, "and certainly not three voices at once");
});

test("a DIFFERENT voice silences the one before it, leaving exactly one playing", () => {
  const h = audioHarness("/assets/a.mp3");
  h.self._audio.call(h.self);
  h.self.data.audio = "/assets/b.mp3";
  h.self._audio.call(h.self);
  assert.strictEqual(h.made.length, 2);
  assert.strictEqual(h.playing(), 1, "the first was paused, not merely dropped on the floor");
  assert.strictEqual(h.made[0].paused, true);
});

test("stopping playback silences the voice rather than leaving it running", () => {
  const h = audioHarness("/assets/a.mp3");
  h.self._audio.call(h.self);
  h.self.data.playing = false;
  h.self._audio.call(h.self);
  assert.strictEqual(h.playing(), 0);
  assert.strictEqual(h.self._media, null);
  assert.strictEqual(h.self._audioUrl, "");
});
