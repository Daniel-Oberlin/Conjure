// Unit tests for the pure plane-relative anchor math (client/plane-anchor.js), run with `node --test`.
// The behavioural tests assert the properties that make anchors correct:
//   • round-trip — author then solve against the SAME planes recovers the pose exactly;
//   • frame-independence — solving against a rigidly moved room moves the pose by the SAME rigid motion
//     (this is the whole point: no shared coordinate, only relationships to planes);
//   • redundancy — a missing reference wall still solves; a perturbed wall barely moves the result;
//   • mode — grounded pins Y to the floor and zeroes pitch/roll; free recovers a real tilt;
//   • degeneracy — near-parallel walls are rejected (ok:false) rather than solved into a bogus pose.
// A golden-vector fixture (fixtures/plane-anchor-golden.json) pins the exact numbers so the future Python
// server port (docs/local-first-geometry.md §13.1) can be checked against identical cases.
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const THREE = require("three");
const PA = require("../../client/plane-anchor.js");

const UP = new THREE.Vector3(0, 1, 0);
const V = (x, y, z) => new THREE.Vector3(x, y, z);
const yawQ = (deg) => new THREE.Quaternion().setFromAxisAngle(UP, deg * Math.PI / 180);

// A rectangular room: floor + four walls (outward normals), centred on the origin, 4 m × 6 m, 2.4 m tall.
function room() {
  return [
    { id: "floor", kind: "floor", normal: V(0, 1, 0), point: V(0, 0, 0) },
    { id: "wall_xp", kind: "wall", normal: V(1, 0, 0), point: V(2, 1.2, 0) },
    { id: "wall_xn", kind: "wall", normal: V(-1, 0, 0), point: V(-2, 1.2, 0) },
    { id: "wall_zp", kind: "wall", normal: V(0, 0, 1), point: V(0, 1.2, 3) },
    { id: "wall_zn", kind: "wall", normal: V(0, 0, -1), point: V(0, 1.2, -3) },
  ];
}
// Apply a rigid yaw+translation to a whole set of planes (a stand-in for another client's frame).
function moveRoom(planes, yawDeg, t) {
  const R = yawQ(yawDeg);
  return planes.map((p) => ({
    id: p.id, kind: p.kind,
    normal: p.normal.clone().applyQuaternion(R),
    point: p.point.clone().applyQuaternion(R).add(t),
  }));
}

test("grounded: round-trip recovers position and yaw exactly", () => {
  const planes = room();
  const entity = { position: V(0.5, 0, 1.0), quaternion: yawQ(30), mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes);
  const s = PA.solveAnchor(THREE, anchor, planes);
  assert.ok(s.ok, s.stat);
  assert.ok(s.position.distanceTo(entity.position) < 1e-9, "position " + s.position.toArray());
  assert.ok(s.quaternion.angleTo(entity.quaternion) < 1e-6, "orientation");
});

test("frame-independence: solving against a rigidly moved room moves the pose by the same motion", () => {
  const planes = room();
  const entity = { position: V(-0.3, 0, 2.1), quaternion: yawQ(-50), mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes);

  const R = yawQ(40), t = V(1.0, 0, -2.0);
  const moved = moveRoom(planes, 40, t);
  const s = PA.solveAnchor(THREE, anchor, moved);
  assert.ok(s.ok, s.stat);
  const expectP = entity.position.clone().applyQuaternion(R).add(t);
  const expectQ = R.clone().multiply(entity.quaternion);
  assert.ok(s.position.distanceTo(expectP) < 1e-9, "moved position");
  assert.ok(s.quaternion.angleTo(expectQ) < 1e-6, "moved orientation");
});

test("redundancy: a missing reference wall still solves close to truth", () => {
  const planes = room();
  const entity = { position: V(0.7, 0, -1.4), quaternion: yawQ(10), mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes, { nRefWalls: 4 });
  // Drop one of the referenced walls from the solving client's geometry.
  const droppedId = anchor.walls[0].id;
  const partial = planes.filter((p) => p.id !== droppedId);
  const s = PA.solveAnchor(THREE, anchor, partial);
  assert.ok(s.ok, s.stat);
  assert.ok(s.used.walls < anchor.walls.length, "solved with fewer walls");
  assert.ok(s.position.distanceTo(entity.position) < 1e-6, "still near truth " + s.position.toArray());
});

test("redundancy: perturbing one wall moves the result less than the perturbation (averaging)", () => {
  const planes = room();
  const entity = { position: V(0, 0, 0), quaternion: yawQ(0), mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes, { nRefWalls: 4 });
  const perturbed = planes.map((p) =>
    p.id === "wall_xp" ? { ...p, point: p.point.clone().add(V(0.2, 0, 0)) } : p);   // shove one wall 20 cm
  const s = PA.solveAnchor(THREE, anchor, perturbed);
  assert.ok(s.ok, s.stat);
  const err = s.position.distanceTo(entity.position);
  assert.ok(err > 0, "the perturbation does move it");
  assert.ok(err < 0.15, "but less than the 0.20 m wall shove (redundancy absorbs it): " + err.toFixed(3));
});

test("grounded pins Y to the local floor even when the floor sits at a different height", () => {
  const planes = room();
  const entity = { position: V(0.2, 0, 0.5), quaternion: yawQ(0), mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes);
  const raised = planes.map((p) => p.id === "floor" ? { ...p, point: V(0, 0.5, 0) } : p);  // floor +0.5 m
  const s = PA.solveAnchor(THREE, anchor, raised);
  assert.ok(s.ok, s.stat);
  assert.ok(Math.abs(s.position.y - 0.5) < 1e-9, "rests on the raised floor, y=" + s.position.y);
});

test("grounded zeroes pitch/roll even if the entity was authored with a tilt", () => {
  const planes = room();
  const tilt = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.3, 0.5, 0.2, "YXZ"));  // 3-axis
  const entity = { position: V(0, 0, 0), quaternion: tilt, mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes);
  const s = PA.solveAnchor(THREE, anchor, planes);
  assert.ok(s.ok, s.stat);
  const up2 = UP.clone().applyQuaternion(s.quaternion);
  assert.ok(up2.angleTo(UP) < 1e-6, "solved up is vertical (no lean): " + up2.toArray());
});

test("free: round-trip recovers a genuinely tilted orientation (pitch+roll+yaw)", () => {
  const planes = room();
  const tilt = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.4, -0.9, 0.25, "YXZ"));
  const entity = { position: V(0.3, 1.5, -0.8), quaternion: tilt, mode: "free" };
  const anchor = PA.authorAnchor(THREE, entity, planes);
  const s = PA.solveAnchor(THREE, anchor, planes);
  assert.ok(s.ok, s.stat);
  assert.ok(s.position.distanceTo(entity.position) < 1e-9, "free position (incl. height)");
  assert.ok(s.quaternion.angleTo(tilt) < 1e-6, "free orientation keeps the tilt");
});

test("free transfers a tilt through a rigidly moved room", () => {
  const planes = room();
  const tilt = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.2, 1.2, -0.3, "YXZ"));
  const entity = { position: V(-0.4, 1.1, 0.6), quaternion: tilt, mode: "free" };
  const anchor = PA.authorAnchor(THREE, entity, planes);
  const R = yawQ(-70), t = V(-1.5, 0, 0.8);
  const s = PA.solveAnchor(THREE, anchor, moveRoom(planes, -70, t));
  assert.ok(s.ok, s.stat);
  assert.ok(s.position.distanceTo(entity.position.clone().applyQuaternion(R).add(t)) < 1e-9, "moved free pos");
  assert.ok(s.quaternion.angleTo(R.clone().multiply(tilt)) < 1e-6, "moved free orientation");
});

test("degenerate: near-parallel walls are rejected, not solved", () => {
  // Only the two X-facing walls present (normals ±X span one direction) → XZ underdetermined.
  const planes = room();
  const entity = { position: V(0, 0, 0), quaternion: yawQ(0), mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes, { nRefWalls: 4 });
  const parallelOnly = planes.filter((p) => p.kind === "floor" || p.id === "wall_xp" || p.id === "wall_xn");
  const s = PA.solveAnchor(THREE, anchor, parallelOnly);
  assert.ok(!s.ok, "should reject");
  assert.match(s.stat, /degenerate/);
  assert.equal(s.position, null);
});

test("authoring expands the reference set past nRefWalls to avoid a degenerate (parallel) pick", () => {
  // A narrow alcove: the two NEAREST walls are parallel (both Z-facing, ±1 m), the only spanning wall is
  // farther (X-facing, 3 m). With nRefWalls=2 the naive pick (the two Z-walls) is degenerate, so authoring
  // must reach past 2 for the X-wall to span XZ.
  const planes = [
    { id: "floor", kind: "floor", normal: V(0, 1, 0), point: V(0, 0, 0) },
    { id: "wall_zp", kind: "wall", normal: V(0, 0, 1), point: V(0, 1.2, 1) },
    { id: "wall_zn", kind: "wall", normal: V(0, 0, -1), point: V(0, 1.2, -1) },
    { id: "wall_xp", kind: "wall", normal: V(1, 0, 0), point: V(3, 1.2, 0) },
  ];
  const entity = { position: V(0, 0, 0), quaternion: yawQ(0), mode: "grounded" };
  const anchor = PA.authorAnchor(THREE, entity, planes, { nRefWalls: 2 });
  const s = PA.solveAnchor(THREE, anchor, planes);
  assert.ok(s.ok, "spanned set solves: " + s.stat);
  assert.ok(anchor.walls.some((w) => w.id === "wall_xp"), "reached for the spanning X-wall");
  assert.ok(anchor.walls.length >= 3, "expanded beyond 2 (" + anchor.walls.length + ")");
});

// ---- golden-vector regression / cross-language contract ----
test("golden vectors reproduce the recorded outputs (JS/Python parity contract)", () => {
  const file = path.join(__dirname, "fixtures", "plane-anchor-golden.json");
  const gold = JSON.parse(fs.readFileSync(file, "utf8"));
  const toPlanes = (ps) => ps.map((p) => ({ id: p.id, kind: p.kind, normal: V(...p.normal), point: V(...p.point) }));
  gold.cases.forEach((c) => {
    const s = PA.solveAnchor(THREE, c.anchor, toPlanes(c.planes));
    assert.equal(s.ok, c.expect.ok, c.name + ": ok");
    if (c.expect.ok) {
      const p = V(...c.expect.position), q = new THREE.Quaternion(...c.expect.quaternion).normalize();
      assert.ok(s.position.distanceTo(p) < 1e-6, c.name + ": position " + s.position.toArray());
      // Cross-language tolerance: quaternion ANGLE error scales as √(component error), so 1e-5 rad (~6e-4°)
      // is a tight-but-realistic bar for the Python port to meet — far below anything visible.
      assert.ok(s.quaternion.angleTo(q) < 1e-5, c.name + ": quaternion (" + s.quaternion.angleTo(q) + ")");
    }
  });
});

// The grab-commit contract (docs/specs/dynamics.md §8): a user drags content in THIS client's
// LOCAL frame (F_track), but the server persists poses in the SEED frame (F_ref) and re-solves content
// from them every capture. So a commit must convert local→ref (ConjureFrames.toRef = author against the
// local walls, solve against the seed walls). These two tests pin why that conversion is required.
test("grab commit: local→ref conversion survives the server's re-author and re-solve (no jump)", () => {
  const refPl = room();                                   // seed walls (F_ref, what the server stores)
  const localPl = moveRoom(refPl, 25, V(0.7, 0, -0.4));   // this client's registration of the same room
  const dropped = { position: V(0.5, 0, 1.0), quaternion: yawQ(30), mode: "grounded" };  // where the user let go

  // client: convert the dragged LOCAL pose into the server's frame before committing
  const toRef = PA.solveAnchor(THREE, PA.authorAnchor(THREE, dropped, localPl), refPl);
  assert.ok(toRef.ok, toRef.stat);
  // server: re-authors meta.anchor from the committed F_ref pose against the SEED walls
  const stored = PA.authorAnchor(THREE, { position: toRef.position, quaternion: toRef.quaternion, mode: "grounded" }, refPl);
  // client: re-solves that anchor against its LOCAL walls → must land back where the user dropped it
  const back = PA.solveAnchor(THREE, stored, localPl);
  assert.ok(back.ok, back.stat);
  assert.ok(back.position.distanceTo(dropped.position) < 1e-6, "jumped to " + back.position.toArray());
  assert.ok(back.quaternion.angleTo(dropped.quaternion) < 1e-6, "orientation changed");
});

test("grab commit: committing the RAW local pose instead makes it jump by the registration offset", () => {
  const refPl = room();
  const localPl = moveRoom(refPl, 25, V(0.7, 0, -0.4));
  const dropped = { position: V(0.5, 0, 1.0), quaternion: yawQ(30), mode: "grounded" };
  // the bug: commit the local pose as if it were already F_ref, then let the client re-solve it
  const stored = PA.authorAnchor(THREE, dropped, refPl);
  const back = PA.solveAnchor(THREE, stored, localPl);
  assert.ok(back.ok, back.stat);
  assert.ok(back.position.distanceTo(dropped.position) > 0.2,
    "expected a visible jump, got " + back.position.distanceTo(dropped.position));
});
