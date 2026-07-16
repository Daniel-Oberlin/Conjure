// Unit tests for the pure room-snapping geometry (client/room-snap.js), run with `node --test`.
// Two layers:
//  • Synthetic rooms (built with `vert`) give full control — known facings/positions to assert each
//    invariant precisely: insets land in front of their wall toward the interior (incl. junction doors),
//    openings cut where the inset sits, walls come out square, the frame solve recovers a known
//    transform, rotations are emitted in A-Frame's YXZ order, wall art renders upright.
//  • One golden room (fixtures/golden-room.json) is a REAL Quest capture (45 surfaces, two rooms). The
//    synthetic tests encode our assumptions about the device's conventions; the golden room pins them to
//    the actual hardware and guards against a Quest update changing plane orientation. See its test below.
const { test } = require("node:test");
const assert = require("node:assert");
const THREE = require("three");
const RS = require("../../client/room-snap.js");

const UP = new THREE.Vector3(0, 1, 0);
const D2R = Math.PI / 180;

// A vertical surface (wall/door/window/art) whose OUTWARD normal points at compass yaw `yawDeg`
// (yawOf(normal) === yawDeg). Mirrors a real captured plane's orientation: local +Y is the normal and
// local +Z points world-up (so the rendered rectangle is upright), which is also where euler ORDER bites.
function vert(id, sem, pos, yawDeg, ext) {
  const yaw = yawDeg * D2R;
  const Y = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw));   // normal (plane local +Y)
  const Z = new THREE.Vector3(0, 1, 0);                           // plane local +Z points world-up
  const X = new THREE.Vector3().crossVectors(Y, Z);               // right-handed basis
  const lq = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(X, Y, Z));
  const s = { id, semantic: sem, extent: ext || [1, 2.4],
              _lp: new THREE.Vector3(pos[0], pos[1], pos[2]), _lq: lq, debug: {} };
  s.rotation = RS.eulerYXZ(THREE, lq);
  return s;
}
const normalOf = (s) => UP.clone().applyQuaternion(s._lq);
const yawDegOf = (s) => { const n = normalOf(s); return Math.atan2(n.x, n.z) / D2R; };
// A wall's two endpoints in plan view (X-Z), from its current _lp / _lq / extent.
function planEnds(s) {
  const n = normalOf(s), L = Math.hypot(n.x, n.z) || 1, tx = n.z / L, tz = -n.x / L, hw = s.extent[0] / 2;
  return [new THREE.Vector2(s._lp.x + tx * hw, s._lp.z + tz * hw),
          new THREE.Vector2(s._lp.x - tx * hw, s._lp.z - tz * hw)];
}

test("eulerYXZ emits angles A-Frame reads in YXZ order (not XYZ)", () => {
  const Rx90 = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2);
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(0.7, 1.1, -0.4, "XYZ"));   // genuine 3-axis
  const [x, y, z] = RS.eulerYXZ(THREE, q).map((d) => d * D2R);
  // A-Frame reconstructs the entity orientation from these angles as YXZ; undoing eulerYXZ's -90°X
  // pre-rotation must then recover the original captured-plane quaternion.
  const recovered = new THREE.Quaternion().setFromEuler(new THREE.Euler(x, y, z, "YXZ")).multiply(Rx90);
  assert.ok(recovered.angleTo(q) < 1e-4, "YXZ round-trips to the captured orientation");
  // Reading the very same angles as XYZ does NOT — proving the order is load-bearing (the ~48° bug).
  const wrong = new THREE.Quaternion().setFromEuler(new THREE.Euler(x, y, z, "XYZ")).multiply(Rx90);
  assert.ok(wrong.angleTo(q) > 0.1, "XYZ misreads the same angles");
});

test("snapInsets places a door in front of its wall, toward the interior", () => {
  // +X wall (outward normal +X ⇒ room interior is -X); door in it with the same outward normal.
  const surfaces = [
    vert("real_wall_0", "wall", [2, 1.2, 0], 90, [4, 2.4]),
    vert("real_door_1", "door", [2, 1.0, 0.3], 90, [0.8, 2]),
  ];
  RS.snapInsets(THREE, surfaces);
  const door = surfaces[1];
  assert.ok(door.position[0] < 2, "door pushed to the interior (-X) side of the wall");
  assert.ok(Math.abs(door.position[0] - 2) < 0.05, "door sits within a couple cm of the wall");
  assert.match(door.debug.snap, /wall=.*clr=/);
});

test("snapInsets carves the wall the door is WITHIN, not a coplanar neighbour", () => {
  // Two collinear wall segments on the SAME plane (both face +Z at z=0), a door in front of the RIGHT one.
  // Perpendicular distance to the plane is a tie, so matching by distance alone could pick the far segment
  // and project the opening off its end — the real wall never gets carved (the door-50/wall-59 bug). The
  // door must attach to (and cut) the wall whose extent it actually sits within.
  const surfaces = [
    vert("real_wall_left",  "wall", [-2, 1.2, 0], 0, [3, 2.4]),    // spans x ∈ [-3.5, -0.5]
    vert("real_wall_right", "wall", [2, 1.2, 0], 0, [3, 2.4]),     // spans x ∈ [ 0.5,  3.5]
    vert("real_door_1",     "door", [2, 1.0, 0.29], 0, [0.8, 2]),  // in front of the RIGHT wall
  ];
  RS.snapInsets(THREE, surfaces);
  assert.strictEqual((surfaces[0].holes || []).length, 0, "the far coplanar wall is NOT carved");
  assert.strictEqual((surfaces[1].holes || []).length, 1, "the wall the door sits within IS carved");
  assert.ok(Math.abs(surfaces[1].holes[0].x) < 0.5, "opening sits near the wall centre, where the door is");
});

test("snapInsets sends each junction door into its OWN room (two separate parallel walls)", () => {
  // Two near-parallel junction walls 0.3 m apart, each facing out of its own room; a door in each.
  const surfaces = [
    vert("real_wall_0", "wall", [2.0, 1.2, 0], 90, [3, 2.4]),    // room A wall, normal +X
    vert("real_wall_1", "wall", [2.3, 1.2, 0], 270, [3, 2.4]),   // room B wall, normal -X
    vert("real_door_2", "door", [2.0, 1.0, 0], 90, [0.8, 2]),    // door in A's wall
    vert("real_door_3", "door", [2.3, 1.0, 0], 270, [0.8, 2]),   // door in B's wall
  ];
  RS.snapInsets(THREE, surfaces);
  assert.ok(surfaces[2].position[0] < 2.0, "door A → into room A (-X), in front of its own wall");
  assert.ok(surfaces[3].position[0] > 2.3, "door B → into room B (+X), in front of its own wall");
});

test("snapInsets cuts a door-shaped hole that round-trips to the door's spot on the wall", () => {
  const surfaces = [
    vert("real_wall_0", "wall", [2, 1.5, 0], 90, [4, 3]),
    vert("real_door_1", "door", [2, 1.0, 1.0], 90, [0.9, 2]),   // 1 m along the wall, 0.5 m below centre
  ];
  RS.snapInsets(THREE, surfaces);
  const wall = surfaces[0], door = surfaces[1];
  assert.equal(wall.holes.length, 1, "the wall got exactly one opening");
  const h = wall.holes[0];
  assert.ok(Math.abs(h.w - 0.9) < 1e-9 && Math.abs(h.h - 2) < 1e-9, "opening is the door's size");
  // Rebuild the opening centre in world from (x, y) via the wall's rendered frame (local +X width, -Z
  // height); it must land on the door's position projected onto the wall plane — i.e. exactly where the
  // door sits, ignoring only the tiny perpendicular clearance nudge.
  const wx = new THREE.Vector3(1, 0, 0).applyQuaternion(wall._lq);
  const wy = new THREE.Vector3(0, 0, -1).applyQuaternion(wall._lq);
  const n  = new THREE.Vector3(0, 1, 0).applyQuaternion(wall._lq);
  const rebuilt = wall._lp.clone().add(wx.clone().multiplyScalar(h.x)).add(wy.clone().multiplyScalar(h.y));
  const doorPos = new THREE.Vector3(...door.position);
  const inPlane = doorPos.clone().sub(n.clone().multiplyScalar(doorPos.clone().sub(wall._lp).dot(n)));
  assert.ok(rebuilt.distanceTo(inPlane) < 1e-6, "opening lands where the door sits on the wall");
  // The opening is within the wall outline — at most touching an edge (a door reaching the floor sits
  // flush against the wall's bottom, which is exactly the border case the renderer clamps before cutting).
  assert.ok(Math.abs(h.x) + h.w / 2 <= 4 / 2 + 1e-9 && Math.abs(h.y) + h.h / 2 <= 3 / 2 + 1e-9,
    "opening is within the wall outline");
});

test("each junction wall gets its OWN door hole (openings aren't shared)", () => {
  const surfaces = [
    vert("real_wall_0", "wall", [2.0, 1.2, 0], 90, [3, 2.4]),
    vert("real_wall_1", "wall", [2.3, 1.2, 0], 270, [3, 2.4]),
    vert("real_door_2", "door", [2.0, 1.0, 0], 90, [0.8, 2]),
    vert("real_door_3", "door", [2.3, 1.0, 0], 270, [0.8, 2]),
  ];
  RS.snapInsets(THREE, surfaces);
  assert.equal(surfaces[0].holes.length, 1, "wall A is cut by door A only");
  assert.equal(surfaces[1].holes.length, 1, "wall B is cut by door B only");
});

test("wall art is laid on the wall, not cut through it (no hole)", () => {
  const surfaces = [
    vert("real_wall_0", "wall", [2, 1.2, 0], 90, [4, 2.4]),
    vert("real_art_1", "wall art", [2, 1.2, 0.5], 90, [0.6, 0.9]),
  ];
  RS.snapInsets(THREE, surfaces);
  assert.equal(surfaces[0].holes.length, 0, "wall art does not open the wall");
  assert.match(surfaces[1].debug.snap, /wall=/, "but it is still snapped onto the wall");
});

test("snapInsets orients wall art parallel to its wall (adopts the wall's orientation, not its own roll)", () => {
  // The art's own capture is rolled 180°, but it should adopt the WALL's clean orientation — so its stored
  // rotation matches the wall's (and its normal is the wall's true outward normal, consistent for matching).
  // The picture itself is hung upright toward the room by the placement path, not by the surface's roll.
  const surfaces = [
    vert("real_wall_0", "wall", [2, 1.2, 0], 90, [4, 2.4]),
    vert("real_art_1", "wall art", [2, 1.2, 0.5], 90, [0.6, 0.9]),
  ];
  surfaces[1]._lq.multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI));
  RS.snapInsets(THREE, surfaces);
  assert.deepStrictEqual(surfaces[1].rotation, surfaces[0].rotation, "wall art adopts the wall's orientation");
});

test("squareWalls snaps near-90° walls onto one orthogonal grid", () => {
  const surfaces = [
    vert("w0", "wall", [0, 1.2, -2], 1, [4, 2.4]),     // ~0°
    vert("w1", "wall", [2, 1.2, 0], 88, [4, 2.4]),     // ~90°
    vert("w2", "wall", [0, 1.2, 2], 179, [4, 2.4]),    // ~180°
    vert("w3", "wall", [-2, 1.2, 0], 272, [4, 2.4]),   // ~270°
  ];
  RS.squareWalls(THREE, surfaces);
  const mod90 = surfaces.map((s) => ((yawDegOf(s) % 90) + 90) % 90);
  for (const m of mod90) {
    assert.ok(Math.abs(((m - mod90[0] + 45) % 90) - 45) < 0.2, "every wall ends up mutually square");
  }
});

test("squareWalls leaves a genuinely angled (>12°) wall alone", () => {
  const surfaces = [
    vert("w0", "wall", [0, 1.2, -2], 0, [4, 2.4]),
    vert("w1", "wall", [2, 1.2, 0], 90, [4, 2.4]),
    vert("w2", "wall", [0, 1.2, 2], 180, [4, 2.4]),
    vert("a", "wall", [1, 1.2, 1], 35, [0.6, 2.4]),   // a 35°-off small wall — beyond the 12° nudge limit
  ];
  RS.squareWalls(THREE, surfaces);
  assert.ok(Math.abs(yawDegOf(surfaces[3]) - 35) < 0.001, "the 35° wall is untouched");
});

test("joinCorners extends two perpendicular walls that fall short to meet at the corner", () => {
  const A = vert("real_wall_0", "wall", [-1.025, 1.2, 0], 0, [1.95, 2.4]);   // along X, ends 5cm short of x=0
  const B = vert("real_wall_1", "wall", [0, 1.2, -1.025], 90, [1.95, 2.4]);  // along Z, ends 5cm short of z=0
  RS.joinCorners(THREE, [A, B]);
  const corner = new THREE.Vector2(0, 0), ea = planEnds(A), eb = planEnds(B);
  assert.ok(Math.min(ea[0].distanceTo(corner), ea[1].distanceTo(corner)) < 1e-6, "wall A reaches the corner");
  assert.ok(Math.min(eb[0].distanceTo(corner), eb[1].distanceTo(corner)) < 1e-6, "wall B reaches the corner");
  const pair = Math.min.apply(null, ea.flatMap((p) => eb.map((q) => p.distanceTo(q))));
  assert.ok(pair < 1e-6, "the two walls now actually touch (gap closed)");
});

test("joinCorners leaves a doorway gap between collinear walls alone", () => {
  const A = vert("real_wall_0", "wall", [-1.15, 1.2, 0], 0, [1.7, 2.4]);     // x ∈ [-2.0, -0.3]
  const B = vert("real_wall_1", "wall", [1.15, 1.2, 0], 0, [1.7, 2.4]);      // x ∈ [ 0.3,  2.0]
  RS.joinCorners(THREE, [A, B]);
  assert.equal(A.extent[0], 1.7, "collinear wall A untouched");
  assert.equal(B.extent[0], 1.7, "collinear wall B untouched");
});

test("joinCorners doesn't extend perpendicular walls whose ends are beyond the gap threshold", () => {
  const A = vert("real_wall_0", "wall", [-1.5, 1.2, 0], 0, [2.0, 2.4]);      // ends 0.5 m from the crossing
  const B = vert("real_wall_1", "wall", [0, 1.2, -1.5], 90, [2.0, 2.4]);
  RS.joinCorners(THREE, [A, B]);
  assert.equal(A.extent[0], 2.0, "far wall left alone (> GAP)");
  assert.equal(B.extent[0], 2.0, "far wall left alone (> GAP)");
});

test("register recovers a known yaw + translation from a rotated capture", () => {
  const refWall = (pos, yaw, ext) => ({ sem: "wall", pos: new THREE.Vector3(...pos), ext, nyaw: yaw, orient: "vertical" });
  // Rectangular room (4×3) — distinct long/short wall sizes break the 90° rotational symmetry.
  const ref = [
    refWall([0, 1.2, -1.5], 0, [4, 2.4]),
    refWall([0, 1.2, 1.5], Math.PI, [4, 2.4]),
    refWall([2, 1.2, 0], Math.PI / 2, [3, 2.4]),
    refWall([-2, 1.2, 0], -Math.PI / 2, [3, 2.4]),
  ];
  const THETA = 160 * D2R, t = new THREE.Vector3(2.5, 0, -1.0);   // ref = R(THETA)·cur + t
  const Rneg = new THREE.Quaternion().setFromAxisAngle(UP, -THETA);
  const cur = ref.map((r) => ({
    sem: r.sem, ext: r.ext, orient: "vertical",
    pos: r.pos.clone().sub(t).applyQuaternion(Rneg),
    nyaw: r.nyaw - THETA,
  }));
  const { Tmat, stat } = RS.register(THREE, cur, ref);
  assert.ok(Tmat, "registration was confident: " + stat);
  for (let i = 0; i < ref.length; i++) {
    const got = cur[i].pos.clone().applyMatrix4(Tmat);
    assert.ok(got.distanceTo(ref[i].pos) < 0.05, "cur surface " + i + " maps onto its ref position");
  }
});

test("register declines (returns null) when the reference is too small", () => {
  const { Tmat, stat } = RS.register(THREE, [], []);
  assert.strictEqual(Tmat, null);
  assert.strictEqual(stat, "ref<3");
});

// --- Multi-user co-location robustness (room-model §8): a GUEST registers its own planes onto the
// authority's reference from a different vantage, so it sees a PARTIAL/EXTRA plane set. ---
const rectRoom = () => {                          // room with 4 ALL-DISTINCT-width walls (breaks the 180°
  // rotational ambiguity a symmetric rectangle has from one vantage) + floor + ceiling. Opposite widths
  // differ by > SIZE_TOL so the size gate rules out the mirror solution → a unique transform.
  const wall = (pos, yaw, ext) => ({ sem: "wall", pos: new THREE.Vector3(...pos), ext, nyaw: yaw, orient: "vertical" });
  const horiz = (sem, pos, ext) => ({ sem, pos: new THREE.Vector3(...pos), ext, nyaw: 0, orient: "horizontal" });
  return [wall([0, 1.2, -1.5], 0, [4.0, 2.4]), wall([0, 1.2, 1.5], Math.PI, [3.0, 2.4]),
          wall([2, 1.2, 0], Math.PI / 2, [2.5, 2.4]), wall([-2, 1.2, 0], -Math.PI / 2, [1.5, 2.4]),
          horiz("floor", [0, 0, 0], [4, 3]), horiz("ceiling", [0, 2.4, 0], [4, 3])];
};
const asCapture = (ref, thetaDeg, t) => {         // cur seen after the frame jumped: ref = R(theta)·cur + t
  const Rneg = new THREE.Quaternion().setFromAxisAngle(UP, -thetaDeg * D2R);
  return ref.map((r) => ({ sem: r.sem, ext: r.ext.slice(), orient: r.orient,
    pos: r.pos.clone().sub(t).applyQuaternion(Rneg), nyaw: r.nyaw - thetaDeg * D2R }));
};
const mapsOnto = (Tmat, cur, ref, n) => {
  for (let i = 0; i < n; i++) assert.ok(cur[i].pos.clone().applyMatrix4(Tmat).distanceTo(ref[i].pos) < 0.05,
    "surface " + i + " maps onto its reference position");
};

test("register locks despite EXTRA clutter planes that inflate the detected count", () => {
  const ref = rectRoom();
  const cur = asCapture(ref, 160, new THREE.Vector3(2.5, 0, -1.0));
  for (let i = 0; i < 12; i++) cur.push({ sem: "clutter", ext: [0.6, 0.6], orient: "vertical",  // furniture, no reference
    pos: new THREE.Vector3((i * 1.7) % 9 - 4, 0.4 + (i % 3) * 0.6, (i * 2.3) % 7 - 3), nyaw: i * 0.7 });
  const { Tmat, stat } = RS.register(THREE, cur, ref);
  assert.ok(Tmat, "locks on the 6 real surfaces despite 12 clutter planes: " + stat);
  mapsOnto(Tmat, cur, ref, ref.length);
  assert.ok(6 < 0.4 * cur.length, "sanity: the OLD fraction-of-detected rule (40%) would have rejected this");
});

test("register locks on a PARTIAL capture (some reference surfaces missing/occluded)", () => {
  const ref = rectRoom();
  const cur = asCapture(ref, -95, new THREE.Vector3(-1.0, 0, 3.0)).slice(0, 4);   // only the 4 walls seen
  const { Tmat, stat } = RS.register(THREE, cur, ref);
  assert.ok(Tmat, "locks with only 4 of 6 surfaces visible: " + stat);
  mapsOnto(Tmat, cur, ref, 4);
});

test("register declines when too few reference surfaces are covered (< MIN_COV)", () => {
  const ref = rectRoom();
  const cur = asCapture(ref, 30, new THREE.Vector3(1, 0, 1)).slice(0, 3);   // only 3 covered → below the floor
  assert.strictEqual(RS.register(THREE, cur, ref).Tmat, null);
});

// --- canonicalFrame: a deterministic frame from a room's own geometry, for VOID/outdoor worlds with no
// stored space. The point is INVARIANCE — the same physical room, seen from an arbitrary session origin,
// must canonicalize to the SAME frame, so a void world's skybox holds orientation across visits. ---
const mkWall = (pos, yawDeg, ext) => ({ sem: "wall", pos: new THREE.Vector3(...pos), ext,
  nyaw: yawDeg * D2R, orient: "vertical" });
const asymRoom = () => [mkWall([0, 1.2, -2], 0, [4.0, 2.4]), mkWall([0, 1.2, 2], 180, [3.0, 2.4]),
  mkWall([2, 1.2, 0], 90, [2.5, 2.4]), mkWall([-2, 1.2, 0], -90, [1.5, 2.4])];   // distinct widths ⇒ unique largest

test("canonicalFrame recovers the SAME canonical pose regardless of the session's yaw + offset", () => {
  const walls = asymRoom();
  const T1 = RS.canonicalFrame(THREE, walls).Tmat;
  assert.ok(T1, "canonical frame found");
  // the SAME physical room, seen next session with an arbitrary tracking-origin yaw φ + translation
  const phi = 137 * D2R, off = new THREE.Vector3(3, 0, -2);
  const Rphi = new THREE.Quaternion().setFromAxisAngle(UP, phi);
  const walls2 = walls.map((w) => ({ sem: w.sem, ext: w.ext, orient: "vertical",
    pos: w.pos.clone().applyQuaternion(Rphi).add(off), nyaw: w.nyaw + phi }));
  const T2 = RS.canonicalFrame(THREE, walls2).Tmat;
  for (let i = 0; i < walls.length; i++) {
    const p1 = walls[i].pos.clone().applyMatrix4(T1), p2 = walls2[i].pos.clone().applyMatrix4(T2);
    assert.ok(p1.distanceTo(p2) < 1e-5, "wall " + i + " canonicalizes to the same spot across sessions");
  }
});

test("canonicalFrame is deterministic and puts the room centroid at the origin", () => {
  const walls = asymRoom();
  const a = RS.canonicalFrame(THREE, walls).Tmat, b = RS.canonicalFrame(THREE, walls).Tmat;
  assert.ok(a.elements.every((e, i) => Math.abs(e - b.elements[i]) < 1e-12), "same input ⇒ identical frame");
  const c = new THREE.Vector3(); walls.forEach((w) => c.add(w.pos)); c.multiplyScalar(1 / walls.length);
  const cc = c.clone().applyMatrix4(a);
  assert.ok(Math.hypot(cc.x, cc.z) < 1e-6, "the room's HORIZONTAL center maps to the origin");
  assert.ok(Math.abs(cc.y - c.y) < 1e-6, "Y is preserved (floor stays at the floor, not canonicalized)");
});

test("canonicalFrame declines with too little geometry (< 2 walls)", () => {
  assert.strictEqual(RS.canonicalFrame(THREE, [mkWall([0, 1.2, -2], 0, [4, 2.4])]).Tmat, null);
});

test("register locks on a room with a thin partition (antiparallel near-duplicate walls) and covers both faces", () => {
  // Two walls at nearly the same center with OPPOSITE normals (the two faces of a partition, as between
  // adjacent rooms) + 3 others. The same-facing gate must keep the lock clean and count both faces.
  const w = (pos, yawDeg, ext) => ({ sem: "wall", pos: new THREE.Vector3(...pos), ext, nyaw: yawDeg * D2R, orient: "vertical" });
  const ref = [w([0, 1.2, 0], 0, [3, 2.4]), w([0, 1.2, 0.3], 180, [3.2, 2.4]),   // partition: +z and -z faces
               w([-2, 1.2, 1.2], 90, [2.6, 2.4]), w([2, 1.2, 1.2], -90, [2.2, 2.4]), w([0, 1.2, 2.6], 180, [4, 2.4])];
  const cur = asCapture(ref, 50, new THREE.Vector3(1, 0, -1));
  const { Tmat, cov } = RS.register(THREE, cur, ref);
  assert.ok(Tmat, "locks on a room containing a partition");
  assert.strictEqual(cov, 5, "each face covers its OWN reference (no cross-count), cov=" + cov);
});

test("register declines a genuinely different (differently-sized) room", () => {
  const ref = rectRoom();
  const w = (pos, yaw, ext) => ({ sem: "wall", pos: new THREE.Vector3(...pos), ext, nyaw: yaw, orient: "vertical" });
  const cur = [w([0, 1.2, -2.5], 0, [6, 2.4]), w([0, 1.2, 2.5], Math.PI, [6, 2.4]),   // a bigger room — walls
               w([3, 1.2, 0], Math.PI / 2, [5, 2.4]), w([-3, 1.2, 0], -Math.PI / 2, [5, 2.4])];  // too large to be partials
  assert.strictEqual(RS.register(THREE, cur, ref).Tmat, null, "different-scale room doesn't false-lock");
});

// --- selectSpace: the FINE stage of two-stage space selection (new-space-flow §3, D2/D7). Geolocation
// hands the client a few geo-near candidate spaces in stored a-plane form; selectSpace picks which one the
// headset is physically in via the register() coverage vote, or null ("somewhere new"). It's the geometric
// vote — not a surface-count guess — that decides, so it's immune to the sparse-capture bug. ---
const roomA = () => [mkWall([0, 1.2, -1.5], 0, [4, 2.4]), mkWall([0, 1.2, 1.5], 180, [3, 2.4]),
  mkWall([2, 1.2, 0], 90, [2.5, 2.4]), mkWall([-2, 1.2, 0], -90, [1.5, 2.4])];
const roomBsmall = () => [mkWall([0, 1, -0.8], 0, [1.2, 2]), mkWall([0, 1, 0.8], 180, [1.2, 2]),
  mkWall([0.8, 1, 0], 90, [1.2, 2]), mkWall([-0.8, 1, 0], -90, [1.2, 2])];   // clearly different (tiny) room
const asEntities = (room) => room.map((s, i) => ({           // cur-form → stored a-plane entity form
  id: s.sem + "_" + i, meta: { real: true, semantic: s.sem },
  transform: { position: [s.pos.x, s.pos.y, s.pos.z], rotation: [0, s.nyaw / D2R, 0] },
  components: { surface: { extent: s.ext.slice() } } }));

test("surfaceToRef round-trips a stored wall entity into register's constellation form", () => {
  const r = RS.surfaceToRef(THREE, asEntities(roomA())[2]);   // the +90° wall
  assert.strictEqual(r.orient, "vertical");
  assert.ok(Math.abs(r.nyaw - Math.PI / 2) < 1e-6, "recovers the wall's normal yaw");
  assert.deepStrictEqual(r.ext, [2.5, 2.4]);
});

test("selectSpace picks the candidate the capture actually matches, regardless of order", () => {
  const cur = asCapture(roomA(), 160, new THREE.Vector3(2.5, 0, -1.0));   // a rotated/offset view of room A
  const candidates = [{ owner: "bob", name: "b", surfaces: asEntities(roomBsmall()) },
                      { owner: "daniel", name: "home", surfaces: asEntities(roomA()) }];
  const hit = RS.selectSpace(THREE, cur, candidates);
  assert.ok(hit, "a candidate matched: " + (hit && hit.stat));
  assert.strictEqual(hit.owner, "daniel");
  assert.strictEqual(hit.name, "home");
});

test("selectSpace returns null when the capture matches no candidate (somewhere new)", () => {
  const cur = asCapture(roomA(), 40, new THREE.Vector3(1, 0, 1));
  assert.strictEqual(RS.selectSpace(THREE, cur, [
    { owner: "bob", name: "b", surfaces: asEntities(roomBsmall()) }]), null);
  assert.strictEqual(RS.selectSpace(THREE, cur, []), null);   // and for an empty candidate set
});

// --- matchRef: id re-inheritance must keep the two FACES of a shared partition wall distinct. Two
// parallel walls between adjacent rooms have centers ~0.5 m apart and OPPOSITE normals; a center-only
// match can swap their ids (the bug seen live: wall 3 ↔ wall 59). The normal gate prevents it. ---
test("matchRef picks the SAME-FACING reference, not merely the nearest center", () => {
  const refOld = { id: "old", sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.2, 0.05) };
  const refNew = { id: "new", sem: "wall", orient: "vertical", nyaw: Math.PI, pos: new THREE.Vector3(0, 1.2, -0.05) };
  const refs = [refOld, refNew];
  // a NEW-room wall (normal -z ⇒ nyaw π) whose detected center drifted TOWARD the old wall (z=0.02, so
  // "old" is nearer by center). Center-only matching would grab "old"; the normal gate must pick "new".
  assert.strictEqual(
    RS.matchRef({ sem: "wall", orient: "vertical", nyaw: Math.PI, pos: new THREE.Vector3(0, 1.2, 0.02) }, refs, new Set()).id,
    "new");
  // the old-room wall (normal +z) picks "old"
  assert.strictEqual(
    RS.matchRef({ sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.2, 0.05) }, refs, new Set()).id,
    "old");
});

test("matchRef re-matches a COINCIDENT antiparallel surface (wall art faces inward, 180° from its wall)", () => {
  // A mounted picture's live normal faces the viewer (inward), but it's stored with the wall's outward
  // normal — 180° apart at the SAME spot. It must re-match (same surface), not mint a duplicate each session.
  const ref = { id: "art", sem: "wall art", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.5, -2) };
  const cand = { sem: "wall art", orient: "vertical", nyaw: Math.PI, pos: new THREE.Vector3(0, 1.5, -2.03) };
  assert.strictEqual(RS.matchRef(cand, [ref], new Set()).id, "art");     // matched despite the 180° flip
  // a genuinely different opposite face 0.4 m away stays DISTINCT (partition id-swap fix intact)
  const faceA = { id: "A", sem: "wall", orient: "vertical", nyaw: Math.PI, pos: new THREE.Vector3(0, 1.2, -2) };
  const candB = { sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.2, -1.6) };
  assert.strictEqual(RS.matchRef(candB, [faceA], new Set()), null);      // → mint its own id, no swap
});

test("matchRef honors the claimed set and the 0.5 m distance cap", () => {
  const r = { id: "a", sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 0, 0) };
  const near = { sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 0, 0.1) };
  assert.strictEqual(RS.matchRef(near, [r], new Set([r])), null);                 // already claimed
  assert.strictEqual(RS.matchRef({ sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 0, 1.0) },
                                 [r], new Set()), null);                          // beyond 0.5 m
  assert.strictEqual(RS.matchRef(near, [r], new Set()).id, "a");                  // otherwise matches
});

// --- Golden room: a REAL Quest capture (45 surfaces, two rooms via connecting doors). The synthetic
// tests above encode our assumptions about the device's conventions; this one pins those assumptions to
// the actual hardware — it feeds the captured planes (with their true normals/roll) through the same
// squareWalls → snapInsets the headset runs and asserts the geometry stays sane. It's the check that
// would have caught the wall-art roll bug, and it'd catch a Quest OS update changing plane conventions.
test("golden room (real capture): pipeline holds on real geometry", () => {
  const fixture = require("./fixtures/golden-room.json");
  // No active registration ⇒ Tmat = identity, so the local frame is the raw pose (lp = pos, lq = quat),
  // exactly as the client builds it on the first capture.
  const surfaces = fixture.surfaces.map(function (s, i) {
    const lq = new THREE.Quaternion(s.quat[0], s.quat[1], s.quat[2], s.quat[3]);
    return { id: "real_" + s.semantic.replace(/\s+/g, "_") + "_" + i, semantic: s.semantic,
             extent: s.extent, _lp: new THREE.Vector3(s.pos[0], s.pos[1], s.pos[2]), _lq: lq,
             rotation: RS.eulerYXZ(THREE, lq), debug: {} };
  });
  const origW = {};
  surfaces.filter((s) => s.semantic === "wall").forEach((s) => { origW[s.id] = s.extent[0]; });
  RS.squareWalls(THREE, surfaces);
  RS.joinCorners(THREE, surfaces);
  RS.snapInsets(THREE, surfaces);
  const finite = (a) => a.every(Number.isFinite);

  // (1) Nothing degenerated to NaN/Infinity.
  surfaces.forEach(function (s) {
    assert.ok(finite(s.rotation), "finite rotation: " + s.id);
    if (s.position) assert.ok(finite(s.position), "finite position: " + s.id);
    (s.holes || []).forEach(function (h) {
      assert.ok(finite([h.x, h.y, h.w, h.h]) && h.w > 0 && h.h > 0, "sane hole on " + s.id);
    });
  });

  // (2) Walls come out mutually square: fold each wall's facing into the 90° grid (×4 trick) and confirm
  // the overwhelming majority cluster on one orientation — i.e. squareWalls aligned the real room.
  const walls = surfaces.filter((s) => s.semantic === "wall");
  const facings = walls.map(yawDegOf).map((d) => d * D2R);
  let cx = 0, cy = 0;
  facings.forEach((f) => { cx += Math.cos(f * 4); cy += Math.sin(f * 4); });
  const grid = Math.atan2(cy, cx);
  const squared = facings.filter(function (f) {
    let d = f * 4 - grid; while (d > Math.PI) d -= 2 * Math.PI; while (d < -Math.PI) d += 2 * Math.PI;
    return Math.abs(d) / 4 < 2 * D2R;     // within 2° of the grid
  }).length;
  assert.ok(squared >= 0.8 * walls.length, squared + "/" + walls.length + " walls squared onto one grid");

  // (2b) Corner-joining only nudges: each wall's width changed by at most 2×GAP (an end can move ≤ GAP).
  walls.forEach(function (w) {
    assert.ok(Math.abs(w.extent[0] - origW[w.id]) <= 0.5 + 1e-6, "joinCorners only nudged " + w.id);
  });

  // (3) Doors/windows cut openings, each centred on a real wall (within its extent).
  let holeCount = 0;
  walls.forEach(function (wl) {
    (wl.holes || []).forEach(function (h) {
      holeCount++;
      assert.ok(Math.abs(h.x) <= wl.extent[0] / 2 + 1e-6, "opening centre on wall (x): " + wl.id);
      assert.ok(Math.abs(h.y) <= wl.extent[1] / 2 + 1e-6, "opening centre on wall (y): " + wl.id);
    });
  });
  const insets = surfaces.filter((s) => s.semantic === "door" || s.semantic === "window").length;
  assert.ok(holeCount >= 0.7 * insets, holeCount + "/" + insets + " doors+windows cut an opening");

  // (4) Wall art that snapped to a wall adopts that wall's orientation — so its normal (a-plane +Z) comes
  // out HORIZONTAL (a vertical-wall normal), regardless of the plane's captured roll. Upright-facing of the
  // CONTENT hung on it is now handled at placement (server _face_room), not baked into the surface.
  surfaces.filter((s) => s.semantic === "wall art" && s.debug && s.debug.snap).forEach(function (s) {
    const q = new THREE.Quaternion().setFromEuler(
      new THREE.Euler(s.rotation[0] * D2R, s.rotation[1] * D2R, s.rotation[2] * D2R, "YXZ"));
    assert.ok(Math.abs(new THREE.Vector3(0, 0, 1).applyQuaternion(q).y) < 0.2,
      "wall art adopted its wall (horizontal normal): " + s.id);
  });

  // (5) The frame solve converges on this real 18-wall constellation (cur === ref ⇒ ≈ identity).
  const constellation = walls.map((s) => ({
    sem: s.semantic, ext: s.extent, pos: s._lp.clone(), orient: "vertical",
    nyaw: RS.yawOf(new THREE.Vector3(0, 1, 0).applyQuaternion(s._lq)) }));
  const reg = RS.register(THREE, constellation, constellation);
  assert.ok(reg.Tmat, "register converges on the real constellation: " + reg.stat);
  constellation.forEach(function (c) {
    assert.ok(c.pos.clone().applyMatrix4(reg.Tmat).distanceTo(c.pos) < 0.1, "register ≈ identity");
  });
});
