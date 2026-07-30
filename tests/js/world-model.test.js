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

// ---- render apply-gate (surfaceSig / surfaceMoved) ----
const sig = (p, r, ext, holes) => WM.surfaceSig({ position: p, rotation: r }, { extent: ext, holes });

test("surfaceMoved: an identical surface is NOT re-applied (kills the pop)", () => {
  const a = sig([1, 1.2, 0], [0, 90, 0], [4, 2.4], []);
  const b = sig([1, 1.2, 0], [0, 90, 0], [4, 2.4], []);
  assert.equal(WM.surfaceMoved(THREE, a, b), false);
});

test("surfaceMoved: sub-tolerance jitter is skipped, past-tolerance moves re-apply", () => {
  const base = sig([1, 1.2, 0], [0, 90, 0], [4, 2.4], []);
  assert.equal(WM.surfaceMoved(THREE, base, sig([1.01, 1.2, 0], [0, 90, 0], [4, 2.4], [])), false, "1 cm < 2 cm");
  assert.equal(WM.surfaceMoved(THREE, base, sig([1.05, 1.2, 0], [0, 90, 0], [4, 2.4], [])), true, "5 cm > 2 cm");
});

test("surfaceMoved: a real rotation past tolerance re-applies; a tiny wobble does not", () => {
  const base = sig([0, 0, 0], [0, 90, 0], [4, 2.4], []);
  assert.equal(WM.surfaceMoved(THREE, base, sig([0, 0, 0], [0, 90.5, 0], [4, 2.4], [])), false, "0.5° < 1°");
  assert.equal(WM.surfaceMoved(THREE, base, sig([0, 0, 0], [0, 93, 0], [4, 2.4], [])), true, "3° > 1°");
});

test("surfaceMoved: resizing or re-shaping the extent re-applies", () => {
  const base = sig([0, 0, 0], [0, 0, 0], [4, 2.4], []);
  assert.equal(WM.surfaceMoved(THREE, base, sig([0, 0, 0], [0, 0, 0], [4.3, 2.4], [])), true);
});

test("surfaceMoved: opening changes (count or position) re-apply, jitter does not", () => {
  const noHole = sig([0, 0, 0], [0, 0, 0], [4, 2.4], []);
  const oneHole = sig([0, 0, 0], [0, 0, 0], [4, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]);
  assert.equal(WM.surfaceMoved(THREE, noHole, oneHole), true, "an opening appeared");
  const moved = sig([0, 0, 0], [0, 0, 0], [4, 2.4], [{ x: 0.9, y: 0, w: 0.9, h: 2 }]);
  assert.equal(WM.surfaceMoved(THREE, oneHole, moved), true, "the opening slid 40 cm");
  const wobble = sig([0, 0, 0], [0, 0, 0], [4, 2.4], [{ x: 0.505, y: 0, w: 0.9, h: 2 }]);
  assert.equal(WM.surfaceMoved(THREE, oneHole, wobble), false, "5 mm opening wobble < 2 cm");
});

test("surfaceMoved: tolerances are tunable", () => {
  const base = sig([0, 0, 0], [0, 0, 0], [4, 2.4], []);
  const nudged = sig([0.03, 0, 0], [0, 0, 0], [4, 2.4], []);
  assert.equal(WM.surfaceMoved(THREE, base, nudged), true, "3 cm > default 2 cm");
  assert.equal(WM.surfaceMoved(THREE, base, nudged, { pos: 0.10 }), false, "3 cm < loosened 10 cm");
});

// ---- pose/shape split (jitter fix): a DRIFT re-lays only the transform; only a SHAPE change rebuilds the
// mesh. This is what keeps tracking drift from re-triangulating the whole room every capture. ----
test("surfacePoseMoved: a drift moves the pose but NOT the shape", () => {
  const base = sig([1, 1.2, 0], [0, 90, 0], [4, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]);
  const drifted = sig([1.05, 1.2, 0], [0, 93, 0], [4, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]);
  assert.equal(WM.surfacePoseMoved(THREE, base, drifted), true, "5 cm + 3° drift → pose moved");
  assert.equal(WM.surfaceShapeChanged(base, drifted), false, "same extent + opening → shape unchanged");
});

test("surfaceShapeChanged: resize/re-hole changes shape but a pure drift does not", () => {
  const base = sig([0, 0, 0], [0, 0, 0], [4, 2.4], []);
  assert.equal(WM.surfaceShapeChanged(base, sig([0, 0, 0], [0, 0, 0], [4.3, 2.4], [])), true, "resized");
  assert.equal(WM.surfaceShapeChanged(base, sig([0, 0, 0], [0, 0, 0], [4, 2.4], [{ x: 0, y: 0, w: 1, h: 2 }])), true, "opening appeared");
  assert.equal(WM.surfaceShapeChanged(base, sig([0.5, 0.5, 0.5], [0, 45, 0], [4, 2.4], [])), false, "moved/rotated only → shape unchanged");
});

// ---- advanceSig (wall∩ceiling seam regression): a POSE-ONLY re-lay must NOT advance the SHAPE baseline,
// or sub-tolerance extent drift accumulates into the baseline un-drawn and the rebuild never fires. ----
test("advanceSig: a shape change advances the WHOLE baseline (geometry was enqueued)", () => {
  const prev = sig([0, 0, 0], [0, 0, 0], [4, 2.4], []);
  const next = sig([0.5, 0, 0], [0, 0, 0], [4.5, 2.4], []);       // moved AND resized
  assert.deepStrictEqual(WM.advanceSig(prev, next, true, true), next);
});

test("advanceSig: first lay (no prev) takes the whole sig", () => {
  const next = sig([1, 1, 0], [0, 90, 0], [3, 2], []);
  assert.deepStrictEqual(WM.advanceSig(null, next, true, true), next);
});

test("advanceSig: a POSE-ONLY re-lay advances p/r but HOLDS the last-rendered ext/holes", () => {
  const prev = sig([0, 0, 0], [0, 0, 0], [4.0, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]);
  const next = sig([0.5, 0, 0], [0, 5, 0], [4.01, 2.4], [{ x: 0.51, y: 0, w: 0.9, h: 2 }]); // sub-tol shape drift
  const out = WM.advanceSig(prev, next, true, false);
  assert.deepStrictEqual(out.p, next.p, "pose advances");
  assert.deepStrictEqual(out.r, next.r, "rotation advances");
  assert.deepStrictEqual(out.ext, prev.ext, "extent held at last-rendered");
  assert.deepStrictEqual(out.holes, prev.holes, "holes held at last-rendered");
});

test("advanceSig: repeated pose-only relays don't let sub-tolerance extent drift run the baseline away", () => {
  // Each capture drifts the width by 5 mm (< 2 cm tol) with a real pose move. If the baseline chased the
  // un-drawn extent (the bug), shapeChanged would never fire; holding it, the drift eventually crosses tol.
  let baseline = sig([0, 0, 0], [0, 0, 0], [4.0, 2.4], []);
  let everRebuilt = false;
  for (let i = 1; i <= 8; i++) {
    const cap = sig([i * 0.05, 0, 0], [0, 0, 0], [4.0 + i * 0.005, 2.4], []); // 5 cm pose step, 5 mm width creep
    const shapeChanged = WM.surfaceShapeChanged(baseline, cap);
    const poseMoved = WM.surfacePoseMoved(THREE, baseline, cap);
    if (shapeChanged) everRebuilt = true;
    baseline = WM.advanceSig(baseline, cap, poseMoved, shapeChanged);
  }
  assert.equal(everRebuilt, true, "accumulated width drift must eventually trip a rebuild (self-heal), not vanish");
});

test("pose/shape split: surfaceMoved === shapeChanged OR poseMoved (equivalence preserved)", () => {
  const base = sig([1, 1.2, 0], [0, 90, 0], [4, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]);
  const cases = [
    sig([1, 1.2, 0], [0, 90, 0], [4, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]),   // identical
    sig([1.05, 1.2, 0], [0, 90, 0], [4, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]), // drift only
    sig([1, 1.2, 0], [0, 90, 0], [4.5, 2.4], [{ x: 0.5, y: 0, w: 0.9, h: 2 }]),  // resize only
    sig([1.05, 1.2, 0], [0, 93, 0], [4.5, 2.4], [{ x: 0.9, y: 0, w: 0.9, h: 2 }]), // both
  ];
  for (const b of cases) {
    assert.equal(
      WM.surfaceMoved(THREE, base, b),
      WM.surfaceShapeChanged(base, b) || WM.surfacePoseMoved(THREE, base, b),
      "surfaceMoved must equal the OR of its halves");
  }
});

// --- Pose-smoothing slew math (docs/pose-smoothing-plan.md §4, §11) --------------------------------------
// The two pure, DOM-free pieces of the per-surface slew: the frame-rate-independent easing fraction and the
// arrival predicate. (The object3D lerp/slerp themselves live in the client and are exercised on-headset.)

test("slewAlpha: tau<=0 disables → a=1 (snap the whole gap)", () => {
  assert.equal(WM.slewAlpha(0.011, 0), 1, "tau=0 snaps");
  assert.equal(WM.slewAlpha(0.011, -0.1), 1, "negative tau snaps");
});

test("slewAlpha: a = 1 - exp(-dt/tau), in [0,1] and monotonic in dt", () => {
  const tau = 0.1;
  assert.ok(Math.abs(WM.slewAlpha(0.1, tau) - (1 - Math.exp(-1))) < 1e-12, "one tau closes ~63%");
  assert.ok(Math.abs(WM.slewAlpha(0.3, tau) - (1 - Math.exp(-3))) < 1e-12, "3·tau closes ~95%");
  assert.ok(WM.slewAlpha(0.05, tau) < WM.slewAlpha(0.1, tau), "larger dt → larger fraction");
  assert.equal(WM.slewAlpha(0, tau), 0, "dt=0 → no progress");
});

test("slewAlpha: a huge dt (a stall) is clamped to 1, never overshoots", () => {
  assert.ok(WM.slewAlpha(10, 0.1) <= 1, "never exceeds 1");
  assert.equal(WM.slewAlpha(1e9, 0.1), 1, "astronomically large dt saturates at exactly 1");
  assert.equal(WM.slewAlpha(-5, 0.1), 0, "a negative dt (clock skew) floors at 0, no backward motion");
});

test("slewAlpha: frame-rate INDEPENDENT — two half-steps close the same gap as one full step", () => {
  const tau = 0.1;
  // Simulate a scalar gap of 1.0 eased with one 16 ms step vs two 8 ms steps; remaining gap must match.
  const one = 1 - WM.slewAlpha(0.016, tau);
  const a8 = WM.slewAlpha(0.008, tau);
  const two = (1 - a8) * (1 - a8);
  assert.ok(Math.abs(one - two) < 1e-9, `remaining gaps match (${one} vs ${two})`);
});

test("slewSettled: true only once BOTH gaps fall below their epsilons", () => {
  const pe = 0.001, ae = 0.1 * Math.PI / 180;
  assert.equal(WM.slewSettled(0.0005, ae * 0.5, pe, ae), true, "both under → settled");
  assert.equal(WM.slewSettled(0.002, ae * 0.5, pe, ae), false, "position still open → not settled");
  assert.equal(WM.slewSettled(0.0005, ae * 2, pe, ae), false, "angle still open → not settled");
});

test("slewSettled: an EMA of a fixed gap crosses the epsilon within ~tau·ln(gap/eps) of stepping", () => {
  // Step a 2 cm correction (a realistic per-capture drift) toward 0 at 90 Hz with tau=0.1; it must reach
  // POS_EPS (1 mm) by tau·ln(gap/eps) ≈ 3·tau (0.3 s) — the analytic settle time of an exponential ease —
  // and NOT already be there in the first couple of frames (the ease is visible, not an instant snap).
  const tau = 0.1, dt = 1 / 90, pe = 0.001, ae = 1e-4, gap0 = 0.02;   // ae>0 so the (angGap=0)<ae test passes
  let gap = gap0, tEarly = null, tSettle = null;
  for (let f = 1; f <= 90; f++) {          // up to 1 s of frames
    gap *= (1 - WM.slewAlpha(dt, tau));
    const t = f * dt;
    if (tEarly === null && f === 2) tEarly = gap;                         // still mid-ease after 2 frames
    if (tSettle === null && WM.slewSettled(gap, 0, pe, ae)) tSettle = t;  // first frame under epsilon
  }
  const analytic = tau * Math.log(gap0 / pe);                            // ≈ 0.3 s for a 2 cm→1 mm ease
  assert.ok(tEarly > pe, "not an instant snap — still easing after 2 frames");
  assert.ok(tSettle !== null && tSettle <= analytic + 3 * dt, `settled by ~tau·ln(gap/eps) (at ${tSettle}s)`);
});
