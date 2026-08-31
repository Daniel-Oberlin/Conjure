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

// --- Pose-smoothing slew math (docs/specs/spaces-geometry.md §9.2, §11) --------------------------------------
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

// --- shouldSpawnGuest: the desktop-guest teleport must never touch a headset -----------------------
// Regression for an observed out-of-body bug. Owner on voice + a browser tab; guest in a headset. The
// session went private (guest evicted), then public. The guest's page reloaded, and for ~3 s it held a
// live socket and a known worldOwner while NOT yet in an XR session. The owner's browser was streaming
// presence at 10 Hz, so the first packet landed in that window and teleported the guest's RIG 1.2 m to
// the owner's right — then `guestSpawned` latched, so entering AR three seconds later inherited the
// offset for good. The camera hangs off the rig; world content and the raw-XR controller beams do not.
// You end up viewing the scene from a metre beside your own hands.
//
// The old guard asked "am I in a session right now", which is honestly false for the first seconds after
// EVERY page load — a headset's included. Capability is the question that has a stable answer.
const GUEST = { spawned: false, hasOwnerPose: true, me: "guest", owner: "daniel",
                presenting: false, arCapable: false };

test("shouldSpawnGuest: a desktop guest with the owner's pose does spawn", () => {
  assert.equal(WM.shouldSpawnGuest(GUEST), true);
});

test("shouldSpawnGuest: an AR-CAPABLE client is never spawned, even before it enters the session", () => {
  // the reported bug, exactly: not presenting yet, everything else identical to the desktop case
  assert.equal(WM.shouldSpawnGuest({ ...GUEST, arCapable: true }), false);
});

test("shouldSpawnGuest: a client already in a session is never spawned", () => {
  assert.equal(WM.shouldSpawnGuest({ ...GUEST, presenting: true }), false);
});

test("shouldSpawnGuest: the owner never spawns beside themselves, and it happens at most once", () => {
  assert.equal(WM.shouldSpawnGuest({ ...GUEST, me: "daniel" }), false);
  assert.equal(WM.shouldSpawnGuest({ ...GUEST, me: null }), false);
  assert.equal(WM.shouldSpawnGuest({ ...GUEST, spawned: true }), false);
});

test("shouldSpawnGuest: no owner pose yet ⇒ nothing to spawn relative to", () => {
  assert.equal(WM.shouldSpawnGuest({ ...GUEST, hasOwnerPose: false }), false);
});

// --- isCaptureAuthority: unknown owner must not read as "me" ---------------------------------------
// A guest that briefly believes it is the authority skips the wholesale re-seed from the authority
// (`if (!amOwner) self._ref = []`) and can ESTABLISH its own frame (`canEstablish = amOwner && !_ref`).
// It then registers against its own reference forever after, and the room renders wrong. `worldOwner`
// is null until the first snapshot, so this window opens on every entry — observed in the log as a
// guest POSTing geometry and taking a 403.
test("isCaptureAuthority: unknown owner is NOT me — even for the dev/default user", () => {
  assert.equal(WM.isCaptureAuthority("guest", null), false);
  assert.equal(WM.isCaptureAuthority("", null), false);          // dev user, but owner still unknown
  assert.equal(WM.isCaptureAuthority(null, undefined), false);
});

test("isCaptureAuthority: once the owner is known, the owner and the dev user author", () => {
  assert.equal(WM.isCaptureAuthority("daniel", "daniel"), true);
  assert.equal(WM.isCaptureAuthority("", "daniel"), true);       // empty user = dev/default = owner
  assert.equal(WM.isCaptureAuthority(null, "daniel"), true);
});

test("isCaptureAuthority: a named guest never authors", () => {
  assert.equal(WM.isCaptureAuthority("guest", "daniel"), false);
});

// --- relocStep: the lost-lock hint has to track the failure, not lag it ---------------------------
// Reported: "exiting the guardian usually shows no message" and "sometimes the room orientation is
// wrong after picking the headset up". Both come from the same place — a capture that can't register
// HOLDS the last good frame and skips the render, so the room is stale from that very capture, while
// the hint that explains it waited on a timer a single lucky capture could reset.
const R = (st, evs, t0 = 0, step = 2000) =>
  evs.reduce((s, ev, i) => WM.relocStep(s, ev, t0 + i * step), st);

test("relocStep: a FLICKERING lock does not flap the hint off and on", () => {
  // fail, fail, ONE lucky capture, fail — the shape of a post-sleep relocalization. Assert the state
  // THROUGH the lucky capture, not just at the end: under the old single-success rule the hint drops
  // the moment one capture lands, so the user sees it blink out while the room is still stale.
  let s = WM.relocStep(WM.relocInit(), "ok", 0);            // locked once → the room is known-stale later
  s = R(s, ["lost", "lost"], 2000);
  assert.equal(s.showing, true, "two failures past the stale grace → hint up");
  s = WM.relocStep(s, "ok", 6000);                          // the lucky capture
  assert.equal(s.showing, true, "a lone good capture must not take the hint down");
  assert.notEqual(s.lostSince, null, "…nor reset the lost timer");
  s = WM.relocStep(s, "lost", 8000);
  assert.equal(s.showing, true, "still lost, still explained");
});

test("relocStep: recovery needs consecutive good captures, not one", () => {
  let s = WM.relocStep(WM.relocInit(), "ok", 0);
  s = R(s, ["lost", "lost"], 2000);
  assert.equal(s.showing, true);
  s = WM.relocStep(s, "ok", 8000);
  assert.equal(s.showing, true, "one good capture is not yet recovery");
  s = WM.relocStep(s, "ok", 10000);
  assert.equal(s.showing, false, "two consecutive good captures restore the world");
  assert.equal(s.lostSince, null);
});

test("relocStep: once a lock has been held, the hint comes fast — the room is already wrong", () => {
  let s = WM.relocStep(WM.relocInit(), "ok", 0);            // hadLock
  s = WM.relocStep(s, "lost", 1000);
  assert.equal(s.showing, false, "not on the very first failure — a single transient miss is normal");
  s = WM.relocStep(s, "lost", 3000);                        // 2 s lost, past the stale grace
  assert.equal(s.showing, true);
});

test("relocStep: a COLD start keeps the long grace — don't nag someone still walking in", () => {
  let s = R(WM.relocInit(), ["lost", "lost"], 0);           // never locked; 2 s elapsed
  assert.equal(s.showing, false, "still acquiring at 2 s");
  s = WM.relocStep(s, "lost", 4000);
  assert.equal(s.showing, true, "…but 3 s of never locking still explains itself");
});

// --------------------------------------------------------------------------- frame basis
//
// Observed 2026-08-28: models placed with grab in a VOID world teleported on release, and a headset
// reload snapped them back — so what was committed was right and only the live render was wrong. The
// commit had sent a raw local position PLUS a wall-relative anchor, because `anchorFor` needed only the
// LOCAL walls while `toRef` needed both; the receiving client then solved that anchor against a stale
// basis left over from the previous world. One predicate now governs both directions.

test("a frame basis needs BOTH wall sets — authoring an anchor you cannot convert back is never right", () => {
  const plane = { id: "w1" }, two = [plane, plane];
  assert.strictEqual(WM.hasFrameBasis({ local: two, ref: two }), true);
  assert.strictEqual(WM.hasFrameBasis({ local: two, ref: null }), false);   // ← the teleport case
  assert.strictEqual(WM.hasFrameBasis({ local: null, ref: two }), false);
});

test("a room-less world has no basis, and that is not a failure", () => {
  // A void/outdoor world never captures, so nothing ever populates this. The raw pose IS the pose there,
  // and the caller commits it unconverted rather than treating the absence as an error.
  assert.strictEqual(WM.hasFrameBasis({ local: null, ref: null }), false);
  assert.strictEqual(WM.hasFrameBasis({}), false);
  assert.strictEqual(WM.hasFrameBasis(null), false);
});

test("one plane is not a frame", () => {
  // solveAnchor needs two walls to define an orientation; one leaves the yaw free, and a basis that
  // silently half-works is worse than none.
  const one = [{ id: "w1" }];
  assert.strictEqual(WM.hasFrameBasis({ local: one, ref: one }), false);
  assert.strictEqual(WM.hasFrameBasis({ local: [], ref: [] }), false);
});

// ---- levelDeviation: catching one room's floor moving, without anyone noticing ------------------------
// The self-triggering half of the raised-floor investigation (docs/backlogs/spaces-geometry.md). It has to
// work with no ground truth and no extra persistence, which it does by leaning on registration never
// touching y — so the stored seed is directly comparable to a live capture.

test("levelDeviation ignores a whole-space offset but catches one floor moving alone", () => {
  const seed = {
    floor_A: { y: -0.005, sem: "floor" }, floor_B: { y: -0.026, sem: "floor" },
    floor_C: { y: 0.004, sem: "floor" }, ceil_A: { y: 2.663, sem: "ceiling" },
    ceil_B: { y: 2.445, sem: "ceiling" }, ceil_C: { y: 2.677, sem: "ceiling" },
  };
  // The whole space sits 4 cm higher this session (a different local-floor origin) AND floor_B is 13 cm
  // high on top of that — the reported symptom, hiding inside a legitimate global shift.
  const live = {};
  Object.keys(seed).forEach((id) => { live[id] = seed[id].y + 0.04; });
  live.floor_B += 0.13;

  const dev = WM.levelDeviation(live, seed);
  const byId = Object.fromEntries(dev.map((d) => [d.id, d.dev]));
  assert.ok(Math.abs(byId.floor_B - 0.13) < 1e-9, "the offending floor's deviation is the real 13 cm");
  Object.keys(byId).forEach((id) => {
    if (id === "floor_B") return;
    assert.ok(Math.abs(byId[id]) < 1e-9, `${id} reads 0 — the 4 cm global shift is absorbed, not reported`);
  });
});

test("levelDeviation stays quiet when the whole space shifts together", () => {
  const seed = { a: { y: 0 }, b: { y: 0 }, c: { y: 2.6 }, d: { y: 2.6 } };
  const live = { a: 0.09, b: 0.09, c: 2.69, d: 2.69 };
  const dev = WM.levelDeviation(live, seed);
  assert.ok(dev.every((x) => Math.abs(x.dev) < 1e-9),
            "a rigid whole-space shift is not an anomaly — it is what a relocalization looks like");
});

test("levelDeviation uses the MEDIAN so one bad floor cannot drag the baseline and hide", () => {
  // With a MEAN, a single 30 cm outlier across 4 surfaces pulls the baseline 7.5 cm toward itself and
  // reports every innocent surface as 7.5 cm wrong — the outlier camouflages itself in the noise.
  const seed = { a: { y: 0 }, b: { y: 0 }, c: { y: 2.6 }, d: { y: 2.6 } };
  const live = { a: 0, b: 0, c: 2.6, d: 2.9 };
  const dev = Object.fromEntries(WM.levelDeviation(live, seed).map((x) => [x.id, x.dev]));
  assert.ok(Math.abs(dev.d - 0.3) < 1e-9, "the outlier keeps its full 30 cm");
  assert.ok(Math.abs(dev.a) < 1e-9 && Math.abs(dev.b) < 1e-9 && Math.abs(dev.c) < 1e-9,
            "and the innocent surfaces are not smeared with a share of it");
});

test("levelDeviation reports nothing below three comparable surfaces", () => {
  // With two, "the space shifted" and "one floor is wrong" are the same number. Guessing is worse than
  // silence — a false anomaly in a log you read days later costs more than a missed one.
  assert.deepStrictEqual(WM.levelDeviation({ a: 0.2, b: 0.0 }, { a: { y: 0 }, b: { y: 0 } }), []);
  assert.deepStrictEqual(WM.levelDeviation({ a: 0.2 }, { a: { y: 0 } }), []);
});

test("levelDeviation only compares surfaces the seed actually knows", () => {
  const seed = { a: { y: 0 }, b: { y: 0 }, c: { y: 2.6 } };
  const live = { a: 0, b: 0, c: 2.6, fresh_mint: 1.4 };   // a just-minted id has no baseline yet
  const dev = WM.levelDeviation(live, seed);
  assert.deepStrictEqual(dev.map((d) => d.id).sort(), ["a", "b", "c"]);
});

// ---- loadGate: don't assign identity from a room that is still loading -------------------------------
// Measured on device: entering AR gave 4 and 16 planes against a 58-surface seed on two sessions, and 58
// on a third. The two partial ones re-minted walls and pruned the originals with their colours; the full
// one churned nothing.

test("loadGate holds a partial capture and passes a full one", () => {
  assert.strictEqual(WM.loadGate(4, 58, 0), "hold", "7% of the room — the session that lost two walls");
  assert.strictEqual(WM.loadGate(16, 58, 0), "hold", "28% — the other one");
  assert.strictEqual(WM.loadGate(58, 58, 0), "go", "the session that churned nothing");
  // 30% is where `register` will already accept a lock, so the gate has to sit clear of it
  assert.strictEqual(WM.loadGate(Math.ceil(0.31 * 58), 58, 0), "hold",
                     "a capture register would lock on is still not enough to name surfaces from");
});

test("loadGate never blocks when there is nothing to compare against", () => {
  // Establishing a fresh space, or a void world: no seed, so no expectation, so no gate.
  assert.strictEqual(WM.loadGate(4, 0, 0), "go");
  assert.strictEqual(WM.loadGate(1, 3, 0), "go", "too few seed surfaces to mean anything");
});

test("loadGate gives up rather than deadlocking on a room that genuinely shrank", () => {
  // If surfaces are really gone from Room Setup, the capture can never reach the threshold — and holding
  // forever would also block posting the removal. That is the wall-less-seed deadlock in a new costume,
  // so patience runs out and says so.
  assert.strictEqual(WM.loadGate(20, 58, 14), "hold");
  assert.strictEqual(WM.loadGate(20, 58, 15), "forced", "…and reports that it was forced, not healthy");
});
