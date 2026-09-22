// Unit tests for the contact query (client/conjure-contact.js), run with `node --test`.
//
// The query is the whole of phase 4 and its acceptance test is `water` migrating onto it with NO
// behaviour change — so most of what is worth checking here is geometry: that a bone inside a volume is
// found, that one outside is not, and that the answer arrives in the CONSUMER'S frame, which is what
// lets a module delete its own conversion rather than wrap it.
const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");

global.THREE = THREE;
global.window = { AFRAME: { THREE, components: {}, registerComponent() {} },
                  performance: { now: () => window.__t || 0 } };
global.AFRAME = global.window.AFRAME;
global.document = { querySelector: () => null };
// The GLOBAL one, not `window.performance`. The module guards on `window.performance` and then calls
// bare `performance.now()`, which is the same object in a browser and is Node's own clock here — so
// stubbing only the window property left the speed tests reading real elapsed time and measuring
// hundreds of metres per second.
global.performance = { now: () => window.__t || 0 };
require("../../client/conjure-contact.js");
const CC = global.window.ConjureContact;

/** A hand whose joints are all at `at`, except any named in `move`. Enough for a volume test. */
function hand(at, move = {}, radius = 0.008) {
  const joints = {}, radii = {};
  const names = new Set();
  CC.BONES.forEach(([a, b]) => { names.add(a); names.add(b); });
  names.forEach((n) => {
    joints[n] = (move[n] || at).clone();
    radii[n] = radius;
  });
  return { key: "left:hand", handedness: "left", isHand: true, joints, radii,
           availableTo: () => true };
}

function withHands(hands, t = 0) {
  window.__t = t;
  window.ConjurePointers = { acting: () => hands, list: () => hands };
}

/** A box entity somewhere unhelpful, so nothing can accidentally pass by working in world space. */
function box(pos = [0.4, 1.3, -0.9], yaw = 0.8) {
  const o = new THREE.Object3D();
  o.position.set(...pos);
  o.quaternion.setFromAxisAngle(new THREE.Vector3(0, 1, 0), yaw);
  o.updateMatrixWorld(true);
  return o;
}

test("the bone set is the same 24 as everywhere else", () => {
  // Same order and membership as conjure/hands.py's SEGMENTS and client/hands-fit.js, so a number
  // from one can be read against a number from another.
  assert.equal(CC.BONES.length, 24);
  assert.equal(CC.BONES.filter(([a]) => a === "wrist").length, 5);
  const children = CC.BONES.map(([, b]) => b);
  assert.equal(new Set(children).size, 24);
});

test("a hand far away touches nothing", () => {
  withHands([hand(new THREE.Vector3(5, 5, 5))]);
  assert.deepEqual(CC.inBox(box(), [0.3, 0.2, 0.01]), []);
});

test("a hand inside the volume is found, and the answer is in the BOX'S frame", () => {
  // The point of the query: a module gets coordinates it can use, not a world point to convert. This
  // is what let `water` delete `_toUV`'s proximity half rather than wrap it.
  const o = box();
  const inside = new THREE.Vector3(0.05, -0.03, 0.002).applyMatrix4(o.matrixWorld);
  withHands([hand(inside)]);
  const hits = CC.inBox(o, [0.3, 0.2, 0.01]);
  assert.ok(hits.length > 0, "an inside hand must register");
  const h = hits[0];
  assert.ok(Math.abs(h.local.x - 0.05) < 1e-5, `local.x ${h.local.x}`);
  assert.ok(Math.abs(h.local.y + 0.03) < 1e-5, `local.y ${h.local.y}`);
  assert.ok(h.depth > 0, "and report how far in it is");
  assert.equal(h.key, "left:hand");
  assert.equal(h.hand, "left");
});

test("a bone across the volume is caught even when BOTH its joints are outside", () => {
  // The reason capsules and not points. Joints are ~25 mm apart and the flesh ~8 mm thick, so a point
  // test leaves a centimetre of finger that touches nothing — and a thin volume is exactly where that
  // gap falls, which is exactly where `water` lives.
  const o = box([0, 1, 0], 0);
  const a = new THREE.Vector3(0, 1, 0.05);          // 5 cm one side of a 1 cm-thick plate
  const b = new THREE.Vector3(0, 1, -0.05);         // 5 cm the other
  const h = hand(new THREE.Vector3(9, 9, 9), { "index-finger-phalanx-distal": a,
                                               "index-finger-tip": b });
  withHands([h]);
  const hits = CC.inBox(o, [0.3, 0.2, 0.005], { joints: ["index-finger-tip"] });
  assert.equal(hits.length, 1, "the bone passes through the plate, so it is touching it");
  assert.ok(hits[0].t > 0.3 && hits[0].t < 0.7, `crossed near the middle, t=${hits[0].t}`);
  assert.ok(Math.abs(hits[0].local.z) < 0.01, "and the closest approach is in the plate");
});

test("`joints` narrows the query, which is how a module keeps its old behaviour exactly", () => {
  const o = box([0, 1, 0], 0);
  withHands([hand(new THREE.Vector3(0, 1, 0))]);
  const all = CC.inBox(o, [0.3, 0.2, 0.01]);
  const one = CC.inBox(o, [0.3, 0.2, 0.01], { joints: ["index-finger-tip"] });
  assert.equal(all.length, 24, "every bone is in the volume");
  assert.equal(one.length, 1, "...and asking for one gets one");
  assert.equal(one[0].bone, "index-finger-tip");
});

test("deepest first, so a consumer taking [0] gets the likely intent", () => {
  // The plate has to be THIN for depth to mean anything: in a volume everything is well inside, every
  // bone saturates at the full radius and the ordering is vacuous — which is what the first version of
  // this test measured.
  const o = box([0, 1, 0], 0);
  const h = hand(new THREE.Vector3(0, 1, 0.006));                   // 6 mm off a 2 mm-thick plate
  h.joints["index-finger-tip"] = new THREE.Vector3(0, 1, 0);        // this one dead centre
  withHands([h]);
  const hits = CC.inBox(o, [0.3, 0.2, 0.002]);
  assert.equal(hits[0].bone, "index-finger-tip", "the deepest bone is first");
  assert.ok(hits[0].depth > hits[hits.length - 1].depth, "and the order is not vacuous");
  for (let i = 1; i < hits.length; i++) assert.ok(hits[i].depth <= hits[i - 1].depth);
});

test("speed comes from ONE remembered frame, and a dropped track does not read as motion", () => {
  CC.forget();                    // the remembered frame is module-level and earlier tests filled it
  const o = box([0, 1, 0], 0);
  const at = (z, t) => {
    withHands([hand(new THREE.Vector3(0, 1, z))], t);
    return CC.inBox(o, [0.3, 0.2, 0.02], { joints: ["index-finger-tip"] })[0];
  };
  assert.equal(at(0.015, 1000).speed, 0, "the first frame has nothing to compare against");
  const moved = at(0.005, 1100);                    // 10 mm in 100 ms
  assert.ok(Math.abs(moved.speed - 0.1) < 1e-6, `speed ${moved.speed} m/s`);
  const still = at(0.005, 1200);
  assert.equal(still.speed, 0, "not moving is zero, not a residue");
  // A gap longer than a quarter second is a dropped track, and a re-acquired hand that read as having
  // crossed the room in one frame would be a false poke of enormous speed.
  const resumed = at(0.015, 4000);
  assert.equal(resumed.speed, 0);
});

test("two modules querying in ONE frame both get a speed", () => {
  // The frame stamp lived inside `previous` for one draft, and `remember`'s own cleanup loop — which
  // deletes any key that is not a live hand — deleted it every frame. The guard never fired, so the
  // second module of the frame compared the frame against itself and read every speed as zero.
  CC.forget();
  const o = box([0, 1, 0], 0);
  const place = (z, t) => withHands([hand(new THREE.Vector3(0, 1, z))], t);
  const ask = () => CC.inBox(o, [0.3, 0.2, 0.02], { joints: ["index-finger-tip"] })[0];
  place(0.015, 1000); ask();
  place(0.005, 1100);
  const first = ask(), second = ask();
  assert.ok(Math.abs(first.speed - 0.1) < 1e-6, `first module ${first.speed}`);
  assert.ok(Math.abs(second.speed - 0.1) < 1e-6, `second module in the same frame ${second.speed}`);
});

test("a hand another module has claimed is skipped", () => {
  // The same one-line contract every consumer of the input layer follows: `grab` holding a pointer
  // mid-drag must not also be rippling the water it is dragging past.
  const o = box([0, 1, 0], 0);
  const h = hand(new THREE.Vector3(0, 1, 0));
  h.availableTo = (owner) => owner === "grab";
  withHands([h]);
  assert.equal(CC.inBox(o, [0.3, 0.2, 0.01], { owner: "water" }).length, 0);
  assert.ok(CC.inBox(o, [0.3, 0.2, 0.01], { owner: "grab" }).length > 0);
  assert.ok(CC.inBox(o, [0.3, 0.2, 0.01]).length > 0, "and no owner asks for no arbitration");
});

test("inSphere is a point query with the radius as slack, so its corners are round", () => {
  // A sphere approximated as a box would be wrong by up to 73% of the radius at the corners, which on
  // a 10 cm query is 7 cm of phantom reach.
  const c = new THREE.Vector3(0, 1, 0);
  const corner = new THREE.Vector3(0.09, 1.09, 0.09);              // |corner - c| = 0.156 m
  withHands([hand(corner)]);
  assert.equal(CC.inSphere(c, 0.10).length, 0, "outside the sphere, inside a box of the same radius");
  withHands([hand(new THREE.Vector3(0, 1, 0.09))]);
  assert.ok(CC.inSphere(c, 0.10).length > 0, "and along an axis it is inside both");
});

test("no radius from the runtime falls back to a stated 8 mm rather than to NaN", () => {
  const o = box([0, 1, 0], 0);
  const h = hand(new THREE.Vector3(0, 1, 0.01));
  for (const k in h.radii) h.radii[k] = null;
  withHands([h]);
  const hits = CC.inBox(o, [0.3, 0.2, 0.004], { joints: ["index-finger-tip"] });
  assert.equal(hits.length, 1);
  assert.equal(hits[0].radius, CC.FALLBACK_RADIUS);
  assert.ok(hits[0].depth > 0 && isFinite(hits[0].depth));
});
