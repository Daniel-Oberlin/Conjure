// Unit tests for the XR input layer (client/conjure-pointers.js), run with `node --test`.
//
// The file is an IIFE that assigns `window.ConjurePointers` and reads its input from an `XRFrame`, so
// the frame is stubbed: a fake session with input sources, a fake `getJointPose` over a posed hand, and
// three's Vector3/Quaternion, which is all the arithmetic here touches.
//
// What is worth testing is the part that did not exist until 2026-09-17. A TRACKED HAND HAS NO BUTTONS,
// so until `handControls` there was no action to resolve on one at all — `controllers()` filtering hands
// out was never why hand tracking felt thin, because `CONTROLS` reads a gamepad and a hand has none.
const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");

global.THREE = THREE;
const components = {};
global.window = { AFRAME: { THREE, components, registerComponent: (n, d) => { components[n] = d; } },
                  performance: { now: () => Date.now() } };
global.AFRAME = global.window.AFRAME;
global.document = { querySelector: () => null };
require("../../client/conjure-pointers.js");
const CP = global.window.ConjurePointers;

const CHAIN = ["metacarpal", "phalanx-proximal", "phalanx-intermediate", "phalanx-distal", "tip"];

/**
 * A hand posed by two numbers: how far the thumb tip is from the index tip, and how curled each finger
 * is. Laid out as straight rays down −Z from the wrist so the arithmetic is checkable by hand.
 *
 * `curl` 0 puts a tip a full bone-length span from its metacarpal (extended); 1 folds it back onto it.
 */
function hand(pinchGap, curls = {}) {
  const pos = { wrist: new THREE.Vector3(0, 0, 0) };
  const SEG = 0.025;
  const put = (n, v) => { pos[n] = v; };
  // thumb: out to the side, its tip placed to give the requested gap to the index tip
  put("thumb-metacarpal", new THREE.Vector3(0.03, 0, -0.02));
  put("thumb-phalanx-proximal", new THREE.Vector3(0.035, 0, -0.045));
  put("thumb-phalanx-distal", new THREE.Vector3(0.04, 0, -0.07));
  ["index", "middle", "ring", "pinky"].forEach((f, i) => {
    const x = 0.015 - i * 0.015;
    const c = curls[f] == null ? 0 : curls[f];
    // A curl BENDS the chain, it does not shorten it — the first version of this fixture scaled every
    // segment uniformly, which kept the finger perfectly straight and made `grasp` read zero on a
    // fist. `grasp` is tip-to-metacarpal over the chain's own summed length, so only a bend moves it,
    // which is the property that makes it hand-size-independent in the first place.
    let at = new THREE.Vector3(x, 0, -0.03);
    let dir = new THREE.Vector3(0, 0, -1);
    const axis = new THREE.Vector3(1, 0, 0);
    CHAIN.forEach((part, k) => {
      if (k > 0) {
        dir.applyAxisAngle(axis, c * 0.9);            // ~51° per joint at full curl
        at = at.clone().add(dir.clone().multiplyScalar(SEG));
      }
      put(`${f}-finger-${part}`, at.clone());
    });
  });
  // the thumb tip sits `pinchGap` from the index tip
  const it = pos["index-finger-tip"];
  put("thumb-tip", new THREE.Vector3(it.x + pinchGap, it.y, it.z));
  return pos;
}

/** A pointer, built through the real `build()` by way of a stubbed XR frame. */
function pointers({ hands = [], gamepads = [] } = {}) {
  const sources = [];
  hands.forEach((h, i) => sources.push({
    handedness: i ? "right" : "left", targetRaySpace: { id: "ray" + i },
    hand: { get: (n) => (h.pos[n] ? { j: n } : null) }, gamepad: null, _pos: h.pos, _rad: h.rad || {},
  }));
  gamepads.forEach((gp, i) => sources.push({
    handedness: i ? "right" : "left", targetRaySpace: { id: "gray" + i }, hand: null, gamepad: gp,
  }));
  const frame = {
    getPose: () => ({ transform: { position: { x: 0, y: 0, z: 0 },
                                   orientation: { x: 0, y: 0, z: 0, w: 1 } } }),
    getJointPose: (space, _ref) => {
      const src = sources.find((s) => s.hand && s._pos[space.j]);
      if (!src) return null;
      const p = src._pos[space.j];
      return { transform: { position: { x: p.x, y: p.y, z: p.z },
                            orientation: { x: 0, y: 0, z: 0, w: 1 } },
               radius: src._rad[space.j] == null ? 0.008 : src._rad[space.j] };
    },
  };
  const sceneEl = { frame, renderer: { xr: { getReferenceSpace: () => ({}),
                                             getSession: () => ({ inputSources: sources }) } } };
  CP.list(sceneEl);                       // prime, then force a rebuild for the next call
  return { all: CP.list(sceneEl), sceneEl };
}

function pad(buttons = {}, axes = []) {
  const b = [];
  for (let i = 0; i < 6; i++) b.push({ value: buttons[i] || 0, pressed: (buttons[i] || 0) > 0.5 });
  return { buttons: b, axes };
}

test("a tracked hand publishes all 25 joints and their radii", () => {
  // Read once per frame with everything else this layer reads, so N consumers cost one read. It is
  // also the seam occlusion.js and hand-rig.js would consume to stop reading the frame themselves.
  const { all } = pointers({ hands: [{ pos: hand(0.06) }] });
  const p = all.find((x) => x.isHand);
  assert.ok(p, "a hand must produce a pointer");
  assert.equal(Object.keys(p.joints).length, 25);
  assert.equal(Object.keys(p.radii).length, 25);
  assert.ok(p.fingertip, "and the one joint that was published before still is");
  assert.ok(p.joints["index-finger-tip"].equals(p.fingertip));
});

test("a hand can ACT, which is the thing it could not do before", () => {
  // `controllers()` filtering hands out was never the reason hand tracking felt thin: there was
  // nothing to filter, because a hand has no gamepad and so resolved no action at all.
  const { all, sceneEl } = pointers({ hands: [{ pos: hand(0.02) }], gamepads: [pad({ 0: 1 })] });
  assert.equal(all.length, 2);
  assert.equal(CP.controllers(sceneEl).length, 1, "controllers() still means controllers");
  assert.equal(CP.acting(sceneEl).length, 2, "acting() is the reader ray interaction wants");
  assert.ok(all.every((p) => p.canAct));
});

test("`select` resolves on a pinch AND on a trigger, from one binding", () => {
  // A binding may name several controls and the largest wins. A controller's `pinch` is 0 and a hand's
  // `trigger` is 0 — the vocabularies are disjoint, so no device test is needed and none is written.
  window.CONJURE_BINDINGS = { select: ["trigger", "pinch"], grab: ["grip", "grasp"] };
  try {
    const { all } = pointers({ hands: [{ pos: hand(0.015) }], gamepads: [pad({ 0: 1 })] });
    const h = all.find((p) => p.isHand), c = all.find((p) => !p.isHand);
    assert.ok(h.active("select"), "a shut pinch is a select");
    assert.ok(c.active("select"), "and so is a pulled trigger");
    assert.ok(!h.active("grab"), "an open hand is not a grab");
  } finally { delete window.CONJURE_BINDINGS; }
});

test("pinch, grasp and poke read the SHAPE of the hand", () => {
  const open = pointers({ hands: [{ pos: hand(0.070) }] }).all[0];
  assert.ok(open.ctrl.pinch < 0.1, `open pinch ${open.ctrl.pinch}`);
  const shut = pointers({ hands: [{ pos: hand(0.018) }] }).all[0];
  assert.ok(shut.ctrl.pinch > 0.9, `shut pinch ${shut.ctrl.pinch}`);

  // Expressed as the layer's own predicate rather than as a magnitude, because that is the claim that
  // matters and a magnitude threshold here would just be a second arbitrary number beside the two the
  // control already has.
  const flat = pointers({ hands: [{ pos: hand(0.07) }] }).all[0];
  assert.ok(!flat.active("grab"), `an open hand is not grabbing (${flat.ctrl.grasp})`);
  const fist = pointers({ hands: [{ pos: hand(0.03, { index: 1, middle: 1, ring: 1, pinky: 1 }) }] }).all[0];
  assert.ok(fist.active("grab"), `a fist is grabbing (${fist.ctrl.grasp})`);
  assert.ok(fist.ctrl.grasp > flat.ctrl.grasp + 0.5, "and by a wide margin, not by a hair");

  // `poke` is an index that is OUT while the others are IN — what distinguishes a pointing hand from
  // an open one, which a curl average alone cannot.
  const point = pointers({ hands: [{ pos: hand(0.06, { middle: 1, ring: 1, pinky: 1 }) }] }).all[0];
  assert.ok(point.ctrl.poke >= CP.ACTIVE_AT, `pointing poke ${point.ctrl.poke}`);
  assert.ok(flat.ctrl.poke < CP.ACTIVE_AT, `open hand poke ${flat.ctrl.poke}`);
  assert.ok(fist.ctrl.poke < CP.ACTIVE_AT, `fist poke ${fist.ctrl.poke}`);
});

test("a pinch does not CHATTER at the distance a hand actually holds it", () => {
  // The whole reason hysteresis is here. A gesture is a continuous distance held near its own
  // threshold by a human hand; an unhysteresised control flickers at exactly that distance.
  // 0.046 m resolves to a pinch of exactly 0.5 — above RELEASE_AT and below PRESS_AT, which is the
  // hold region and the only place the test means anything. 0.036 was the first guess and resolves to
  // 0.71, i.e. a legitimate press: the test was failing because it was asking the wrong question.
  const near = 0.046;
  let engaged = [];
  for (const gap of [0.070, 0.020, near, near, near, 0.070, near, near]) {
    engaged.push(pointers({ hands: [{ pos: hand(gap) }] }).all[0].active("select"));
  }
  // open, shut, then HELD near the threshold: it must stay engaged through all three.
  assert.deepEqual(engaged.slice(0, 5), [false, true, true, true, true], engaged.join(","));
  // ...and once released it must not re-engage by hovering there.
  assert.deepEqual(engaged.slice(5), [false, false, false], engaged.join(","));
});

test("the value is RESCALED around the thresholds, so `active()` needs to know nothing", () => {
  // The first version only raised a latched value to ACTIVE_AT, and that was half a mechanism: it held
  // a pinch through a dip and did nothing to stop one ENGAGING below the press threshold, because the
  // raw distance crosses 0.5 on its own well before 0.6. A hand held mid-range still chattered.
  //
  // Remapping makes `value >= ACTIVE_AT` the hysteretic predicate itself, so no consumer — `active()`
  // included — has to carry a rule that only three controls need.
  const at = (gap) => pointers({ hands: [{ pos: hand(gap) }] }).all[0].ctrl.pinch;
  pointers({ hands: [{ pos: hand(0.070) }] });                    // start released
  assert.equal(at(0.070), 0, "wide open is still exactly nothing");
  assert.ok(at(0.050) < CP.ACTIVE_AT, "and below the press threshold cannot read engaged");
  assert.ok(at(0.050) > 0, "...while still being monotonic in the distance");
  assert.equal(at(0.018), 1, "shut is still exactly one");
  assert.ok(at(0.046) >= CP.ACTIVE_AT, "held through a dip, because it is latched");
  assert.ok(at(0.046) < 0.8, "...and not pretending to be nearly shut while it does");
});

test("grasp is normalised by the finger's OWN length, so it means the same on any hand", () => {
  // The same reasoning that put `s` on a ratio in hand-rig.js: a constant would make a small hand read
  // as permanently half-closed.
  const big = hand(0.06, { index: 1, middle: 1, ring: 1, pinky: 1 });
  const small = {};
  for (const k in big) small[k] = big[k].clone().multiplyScalar(0.7);
  const a = pointers({ hands: [{ pos: big }] }).all[0].ctrl.grasp;
  const b = pointers({ hands: [{ pos: small }] }).all[0].ctrl.grasp;
  assert.ok(Math.abs(a - b) < 0.02, `${a} vs ${b} — a 30% smaller hand must read the same`);
});

test("`controllers()` keeps its honest name, and callers degrade if this file is stale", () => {
  // `acting()` was added BESIDE `controllers()` rather than widening it: a function whose name stops
  // being true is worse than one more function, and something may genuinely need a gamepad.
  //
  // The call sites use `(CP.acting || CP.controllers)`. The Quest's cache has served a stale
  // /static/*.js through several reloads before now, and a consumer updated against a pointers layer
  // that has not would find `acting` undefined and throw inside its tick. Degrading to
  // controllers-only is exactly the previous behaviour; dying is not.
  assert.equal(typeof CP.controllers, "function");
  assert.equal(typeof CP.acting, "function");
  const fs = require("node:fs"), path = require("node:path");
  const root = path.join(__dirname, "..", "..");
  for (const f of ["client/surface-overlay.js", "client/conjure-client.js",
                   "client/controller-beams.js", "dynamics/grab/grab.js"]) {
    const src = fs.readFileSync(path.join(root, f), "utf8");
    assert.ok(src.includes("(CP.acting || CP.controllers)"), `${f} must degrade, not throw`);
    assert.ok(!/CP\.acting\(/.test(src), `${f} must not call acting() unguarded`);
  }
});
