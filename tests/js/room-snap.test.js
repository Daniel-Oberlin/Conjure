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

test("snapInsets pins EVERY inset to the given standoff in front of its wall (captured or recovered)", () => {
  // A door estimated 0.2 m off its wall (x=2, normal +X, interior −X) gets pinned to the passed standoff,
  // whether captured or recovered. Its along-wall/height stay put — only the perpendicular moves.
  const off = 0.05;
  for (const recovered of [false, true]) {
    const wall = vert("real_wall_0", "wall", [2, 1.2, 0], 90, [4, 2.4]);
    const door = vert("real_door_1", "door", [1.8, 1.0, 0.3], 90, [0.8, 2]);   // 0.2 m off, 0.3 m along
    if (recovered) door._recovered = true;
    RS.snapInsets(THREE, [wall, door], off);
    const who = recovered ? "recovered" : "captured";
    assert.ok(Math.abs(door.position[0] - (2 - off)) < 0.005, who + " door pinned to wall−standoff: " + door.position[0]);
    assert.ok(Math.abs(door.position[2] - 0.3) < 1e-9, who + " door keeps its live along-wall spot");
    assert.ok(Math.abs(door.position[1] - 1.0) < 1e-9, who + " door keeps its live height");
  }
});

test("snapInsets standoff defaults to 2 cm when unspecified", () => {
  const wall = vert("real_wall_0", "wall", [2, 1.2, 0], 90, [4, 2.4]);
  const door = vert("real_door_1", "door", [1.8, 1.0, 0], 90, [0.8, 2]);
  RS.snapInsets(THREE, [wall, door]);
  assert.ok(Math.abs(door.position[0] - (2 - 0.02)) < 0.005, "default standoff 2 cm: " + door.position[0]);
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

test("snapInsets honors a RECORDED hostWall over proximity (decision #1)", () => {
  // Two CO-facing walls; the door sits nearer wall_b, but its recorded association is wall_a → must win.
  const surfaces = [
    vert("real_wall_a", "wall", [0, 1.2, 0.00], 0, [3, 2.4]),
    vert("real_wall_b", "wall", [0, 1.2, 0.10], 0, [3, 2.4]),           // co-facing, 10 cm nearer the door
    vert("real_door_x", "door", [0, 1.0, 0.12], 0, [0.9, 2.0]),
  ];
  surfaces[2].hostWall = "real_wall_a";                                 // the recorded fact
  RS.snapInsets(THREE, surfaces);
  assert.equal(surfaces[2].hostWall, "real_wall_a", "the recorded host wins over the nearer wall");
  assert.equal(surfaces[0].holes.length, 1, "and the recorded wall is the one carved");
  assert.equal(surfaces[1].holes.length, 0, "not the nearer one");
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

test("wall squaring is removed: the seed pipeline keeps a near-square wall's RAW facing", () => {
  // Two ~perpendicular walls each 6° off the axis grid — well within the OLD ±12° squaring nudge that used
  // to snap them to 0°/90°. The seed pipeline the owner now runs is joinCorners + snapInsets ONLY (no
  // squareWalls), so their raw facings must survive — matching the raw geometry every headset renders. If
  // squaring is ever reintroduced into this pipeline, this fails.
  const facingDeg = (s) => { const n = normalOf(s); return Math.atan2(n.x, n.z) / D2R; };
  const surfaces = [
    vert("real_wall_0", "wall", [0, 1.2, -2], 6, [4, 2.4]),
    vert("real_wall_1", "wall", [2, 1.2, 0], 96, [4, 2.4]),
  ];
  const before = surfaces.map(facingDeg);
  RS.joinCorners(THREE, surfaces);
  RS.snapInsets(THREE, surfaces);
  surfaces.forEach((s, i) => {
    assert.ok(Math.abs(facingDeg(s) - before[i]) < 1e-6,
      s.id + " kept its raw facing " + before[i].toFixed(2) + "° (got " + facingDeg(s).toFixed(2) + "°)");
  });
  assert.equal(typeof RS.squareWalls, "undefined", "squareWalls is gone from the RoomSnap API");
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

// --- Multi-user co-location robustness (specs/spaces.md §7): a GUEST registers its own planes onto the
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

test("register: coverage threshold is tunable (guest robustness knob)", () => {
  // A guest that only overlaps 3 of the authority's surfaces is DECLINED at the default minCov=4, but
  // admitted when the knob is lowered to 3 — the mechanism behind --reg-min-cov for two-headset testing.
  const ref = rectRoom();                                                  // 4 walls + floor + ceiling
  const cur = asCapture(ref.slice(0, 3), 30, new THREE.Vector3(0.4, 0, -0.3));   // guest sees only 3 walls
  assert.strictEqual(RS.register(THREE, cur, ref).Tmat, null, "default minCov=4 declines a 3-surface view");
  const reg = RS.register(THREE, cur, ref, { minCov: 3 });
  assert.ok(reg.Tmat && reg.cov === 3, "lowering minCov to 3 admits the same partial view (cov=" + reg.cov + ")");
});

test("register reports per-wall residuals; a clean capture fits tightly (non-rigidity probe)", () => {
  const ref = rectRoom();
  const cur = asCapture(ref, 25, new THREE.Vector3(0.3, 0, -0.2));   // the SAME room, rigidly moved
  const reg = RS.register(THREE, cur, ref);
  assert.ok(reg.Tmat, "locks");
  assert.strictEqual(reg.residuals.length, reg.cov, "one residual per COVERED surface");
  reg.residuals.forEach((w) => {
    assert.ok(typeof w.res === "number" && w.res >= 0, "res = distance from the reference (m), ≥ 0");
    assert.ok(typeof w.dist === "number" && w.dist >= 0, "dist = reference's distance from origin (m), ≥ 0");
  });
  assert.ok(Math.max(...reg.residuals.map((w) => w.res)) < 0.02,
    "a truly-rigid capture fits within ~cm everywhere — the baseline that on-device non-rigidity breaks");
});

// --- selectSpace: the FINE stage of two-stage space selection (specs/spaces.md §6, D2/D7). Geolocation
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

// --- matchWall: identify a wall by its PLANE (normal + perpendicular offset + along-line overlap), NOT
// its centroid (§5.3/§10). A wall's centroid is a scan artifact that slides along the wall between
// captures/devices; matching by centroid re-mints the id (losing style + shifting every keyed inset). ---
test("matchWall matches a wall whose centroid slid ALONG its own length (centroid would re-mint)", () => {
  // Ref +Z wall at z=0 spanning x∈[-2,2]. A later/other capture sees the SAME wall but centred at x=1
  // (slid 1 m along) with a shorter extent and a little perpendicular noise. Centroid distance ~1 m blows
  // matchRef's 0.5 m cap → re-mint; matchWall keeps the id because it's the same plane with overlapping span.
  const ref = { id: "w", sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.2, 0), ext: [4, 2.4] };
  const cand = { sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(1, 1.2, 0.03), ext: [3, 2.4] };
  assert.strictEqual(RS.matchRef({ ...cand }, [ref], new Set()), null, "matchRef WOULD re-mint (centroid 1 m)");
  assert.strictEqual(RS.matchWall(cand, [ref], new Set()).id, "w", "matchWall keeps the id (same plane)");
});

test("matchWall does NOT merge two distinct colinear walls (a segment past a doorway)", () => {
  // Left wall spans x∈[-3.5,-0.5]; a separate right wall spans x∈[0.5,3.5] — same line & offset (z=0, +Z),
  // 1 m apart along the wall. The overlap guard must keep them distinct (else content lands on the wrong one).
  const left = { id: "L", sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(-2, 1.2, 0), ext: [3, 2.4] };
  const right = { sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(2, 1.2, 0), ext: [3, 2.4] };
  assert.strictEqual(RS.matchWall(right, [left], new Set()), null, "colinear-but-separate walls stay distinct");
});

test("matchWall keeps the two anti-parallel faces of a partition distinct", () => {
  const faceA = { id: "A", sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.2, 0), ext: [3, 2.4] };
  const candB = { sem: "wall", orient: "vertical", nyaw: Math.PI, pos: new THREE.Vector3(0, 1.2, -0.05), ext: [3, 2.4] };
  assert.strictEqual(RS.matchWall(candB, [faceA], new Set()), null, "opposite normal ⇒ different wall");
});

test("matchWall rejects a parallel wall at a different perpendicular offset, and honors claimed", () => {
  const r = { id: "a", sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.2, 0), ext: [3, 2.4] };
  const far = { sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0, 1.2, 1.0), ext: [3, 2.4] };
  assert.strictEqual(RS.matchWall(far, [r], new Set()), null, "1 m off the plane ⇒ not this wall");
  const near = { sem: "wall", orient: "vertical", nyaw: 0, pos: new THREE.Vector3(0.5, 1.2, 0.05), ext: [3, 2.4] };
  assert.strictEqual(RS.matchWall(near, [r], new Set([r])), null, "already claimed ⇒ no match");
  assert.strictEqual(RS.matchWall(near, [r], new Set()).id, "a", "otherwise matches the same plane");
});

// --- L2 structural anchor: an inset's place on its wall is stored as distances to SHARED features
// (corner points, floor/ceiling edges), never the wall's scan-artifact centroid, so any device (esp. a
// guest with a differently-centred scan) reconstructs the same physical spot (§5.3). ---
const cornersOf = (map, id) => map.get(id) || [];

test("wallCorners tags each corner with the PARTNER wall id (perpendicular pair)", () => {
  // Wall A (+Z, z=0, x∈[-2,2]) meets wall B (+X, x=2, z∈[0,3]) at (2,0).
  const A = vert("wall_A", "wall", [0, 1.2, 0], 0, [4, 2.4]);
  const B = vert("wall_B", "wall", [2, 1.2, 1.5], 90, [3, 2.4]);
  const map = RS.wallCorners(THREE, [A, B]);
  assert.strictEqual(cornersOf(map, "wall_A").length, 1);
  assert.strictEqual(cornersOf(map, "wall_A")[0].partner, "wall_B");
  assert.ok(Math.abs(cornersOf(map, "wall_A")[0].x - 2) < 1e-9 && Math.abs(cornersOf(map, "wall_A")[0].z - 0) < 1e-9);
  assert.strictEqual(cornersOf(map, "wall_B")[0].partner, "wall_A");
});

test("wallCorners gives collinear/parallel walls no corner", () => {
  const L = vert("wall_L", "wall", [-2, 1.2, 0], 0, [3, 2.4]);   // both face +Z at z=0, a doorway gap between
  const R = vert("wall_R", "wall", [2, 1.2, 0], 0, [3, 2.4]);
  assert.strictEqual(cornersOf(RS.wallCorners(THREE, [L, R]), "wall_L").length, 0);
});

test("authorInsetAnchor→reconstructInset round-trips a mid-wall door (2 corners → mean)", () => {
  // Wall A between two corners at x=±2; a door at x=0.5. Reconstruct against the SAME wall → same spot.
  const A = vert("wall_A", "wall", [0, 1.2, 0], 0, [4, 2.4]);
  const Bl = vert("wall_Bl", "wall", [-2, 1.2, 1.5], 90, [3, 2.4]);   // corner at (-2,0)
  const Br = vert("wall_Br", "wall", [2, 1.2, 1.5], 90, [3, 2.4]);    // corner at ( 2,0)
  const door = vert("real_door_1", "door", [0.5, 1.0, 0], 0, [0.8, 2]);
  const corners = cornersOf(RS.wallCorners(THREE, [A, Bl, Br]), "wall_A");
  assert.strictEqual(corners.length, 2, "two corners");
  const anchor = RS.authorInsetAnchor(THREE, door, A, corners, 0, 2.4);
  const sol = RS.reconstructInset(THREE, A, corners, 0, 2.4, anchor);
  assert.ok(sol.position.distanceTo(new THREE.Vector3(0.5, 1.0, 0)) < 1e-6, "round-trips to the door spot");
  assert.strictEqual(sol.fallback, null, "fully constrained");
});

test("reconstructInset lands the door right against a GUEST wall captured with a shifted centroid", () => {
  // Author against wall A centred at x=0 (span [-2,2]); a door at x=0.5. A GUEST captures the SAME wall
  // centred at x=0.5 (span [-1,2]) with perpendicular noise — but the A∩Br corner at (2,0) is structural.
  const A = vert("wall_A", "wall", [0, 1.2, 0], 0, [4, 2.4]);
  const Br = vert("wall_Br", "wall", [2, 1.2, 1.5], 90, [3, 2.4]);
  const door = vert("real_door_1", "door", [0.5, 1.0, 0], 0, [0.8, 2]);
  const anchor = RS.authorInsetAnchor(THREE, door, A, cornersOf(RS.wallCorners(THREE, [A, Br]), "wall_A"), 0, 2.4);
  // guest capture: shifted+shorter wall, same corner with Br (still meets at (2,0))
  const Ag = vert("wall_A", "wall", [0.5, 1.2, 0.03], 0, [3, 2.4]);
  const Brg = vert("wall_Br", "wall", [2, 1.2, 1.5], 90, [3, 2.4]);
  const gc = cornersOf(RS.wallCorners(THREE, [Ag, Brg]), "wall_A");
  const sol = RS.reconstructInset(THREE, Ag, gc, 0, 2.4, anchor);
  assert.ok(Math.abs(sol.position.x - 0.5) < 1e-6, "along-wall recovered from the corner, not the centroid: " + sol.position.x);
  assert.ok(Math.abs(sol.position.y - 1.0) < 1e-6, "height recovered from the floor/ceiling edges");
  // a naive centroid-ride would put it at guest_centre(0.5) + author_offset(0.5) = 1.0 — off by 0.5 m.
  assert.ok(Math.abs(sol.position.x - 1.0) > 0.4, "centroid-ride would have drifted ~0.5 m");
});

test("reconstructInset degenerate ladder: 1 corner → direct, 0 corners → wall-centre fallback (flagged)", () => {
  const A = vert("wall_A", "wall", [0, 1.2, 0], 0, [4, 2.4]);
  const Br = vert("wall_Br", "wall", [2, 1.2, 1.5], 90, [3, 2.4]);
  const door = vert("real_door_1", "door", [0.5, 1.0, 0], 0, [0.8, 2]);
  // 1 corner (just Br): still exact
  const one = cornersOf(RS.wallCorners(THREE, [A, Br]), "wall_A");
  assert.strictEqual(one.length, 1);
  const a1 = RS.authorInsetAnchor(THREE, door, A, one, 0, 2.4);
  assert.ok(Math.abs(RS.reconstructInset(THREE, A, one, 0, 2.4, a1).position.x - 0.5) < 1e-6, "1 corner → direct");
  // 0 corners (freestanding wall): author flags it; reconstruct falls back to the wall centre along-axis
  const a0 = RS.authorInsetAnchor(THREE, door, A, [], 0, 2.4);
  assert.strictEqual(a0.fallback, "freestanding");
  const s0 = RS.reconstructInset(THREE, A, [], 0, 2.4, a0);
  assert.ok(Math.abs(s0.position.x - 0) < 1e-6, "0 corners → wall-centre (x=0)");
  assert.match(s0.fallback, /along:wall-centre/);
});

test("reconstructInset with no ceiling captured falls back to the floor edge alone", () => {
  const A = vert("wall_A", "wall", [0, 1.2, 0], 0, [4, 2.4]);
  const Br = vert("wall_Br", "wall", [2, 1.2, 1.5], 90, [3, 2.4]);
  const door = vert("real_door_1", "door", [0.5, 1.0, 0], 0, [0.8, 2]);
  const corners = cornersOf(RS.wallCorners(THREE, [A, Br]), "wall_A");
  const anchor = RS.authorInsetAnchor(THREE, door, A, corners, 0, 2.4);   // authored with both
  const sol = RS.reconstructInset(THREE, A, corners, 0, null, anchor);    // guest has floor only
  assert.ok(Math.abs(sol.position.y - 1.0) < 1e-6, "height from the floor edge alone");
  assert.strictEqual(sol.fallback, null, "floor edge alone is still constrained");
});

// --- L3 inset identity = semantic + host_wall + slot (along-wall ordinal), resolved by nearest
// RECONSTRUCTED along-coord — never centroid — so an inset keeps its id (and director styling) as it
// slides along its wall, and a missing/extra inset doesn't cascade wrong ids onto its neighbours. ---
test("insetAlong is the signed along-wall coordinate; hostWallFor picks the wall the inset sits within", () => {
  const A = vert("wall_A", "wall", [0, 1.2, 0], 0, [4, 2.4]);
  assert.ok(Math.abs(RS.insetAlong(THREE, A, 0.5, 0) - 0.5) < 1e-9, "along = +0.5 for a point 0.5 m along +t");
  const far = vert("wall_far", "wall", [0, 1.2, 3], 0, [4, 2.4]);
  const door = vert("real_door_1", "door", [0.5, 1.0, 0.05], 0, [0.8, 2]);
  assert.strictEqual(RS.hostWallFor(THREE, door, [A, far]).id, "wall_A", "nearest ~parallel within-width wall");
});

test("matchInset resolves identity by nearest along, and honors claimed", () => {
  const seedRecon = [{ id: "w0", along: -0.3 }, { id: "w1", along: 0 }, { id: "w2", along: 0.3 }];
  assert.strictEqual(RS.matchInset({ along: 0.05 }, seedRecon, new Set()), "w1", "nearest along wins");
  assert.strictEqual(RS.matchInset({ along: 0.05 }, seedRecon, new Set(["w1"])), "w2",
    "claimed w1 ⇒ next-nearest within tol");
  assert.strictEqual(RS.matchInset({ along: 5 }, seedRecon, new Set()), null, "beyond tol ⇒ mint");
});

test("matchInset does NOT cascade wrong ids when a middle inset is missing this capture", () => {
  // Seed has three windows on one wall at along −1/0/+1; the guest captured only the OUTER two. Slot-index
  // matching would give the captured pair slots 0,1 and hand window-2 the id of window-1 (cascade). Nearest-
  // along matching keeps each captured window with its own id; the absent middle one is recovered elsewhere.
  const seedRecon = [{ id: "w0", along: -1 }, { id: "w1", along: 0 }, { id: "w2", along: 1 }];
  const claimed = new Set();
  const id0 = RS.matchInset({ along: -1.02 }, seedRecon, claimed); claimed.add(id0);
  const id2 = RS.matchInset({ along: 0.98 }, seedRecon, claimed); claimed.add(id2);
  assert.strictEqual(id0, "w0");
  assert.strictEqual(id2, "w2", "the captured far window keeps w2, NOT w1 (no cascade)");
});

test("matchInset keeps a window's id when the whole wall slides (centroid would re-mint)", () => {
  // One window; its wall (and thus its capture) shifts +0.3 m along, but its reconstructed along is
  // corner-relative, so cand.along ≈ seed.along and the id is preserved.
  assert.strictEqual(RS.matchInset({ along: 0.31 }, [{ id: "win", along: 0.30 }], new Set()), "win");
});

test("dupInsetIds shadows a near-coincident twin (same physical inset persisted under two ids)", () => {
  // real_door_170 / real_door_202: same semantic + host wall, centres ~3 cm apart — one physical door
  // persisted twice (a prior matchInset miss). The higher id is shadowed; the lower stays canonical.
  const insets = [
    { id: "real_door_170", semantic: "door", hostWall: "real_wall_4", pos: [2.973, 0.956, 0.767] },
    { id: "real_door_202", semantic: "door", hostWall: "real_wall_4", pos: [2.977, 0.961, 0.797] },
    { id: "real_window_9", semantic: "window", hostWall: "real_wall_4", pos: [0.0, 1.4, 0.0] },   // far ⇒ distinct
  ];
  const sh = RS.dupInsetIds(insets);
  assert.ok(sh.has("real_door_202") && !sh.has("real_door_170"), "canonical = lowest id; twin shadowed");
  assert.ok(!sh.has("real_window_9"), "a well-separated inset is not a duplicate");
});

test("dupInsetIds keeps insets on different walls / of different semantics distinct", () => {
  const insets = [
    { id: "d1", semantic: "door", hostWall: "wall_a", pos: [0, 1, 0] },
    { id: "d2", semantic: "door", hostWall: "wall_b", pos: [0.01, 1, 0.01] },   // same spot, DIFFERENT wall
    { id: "w1", semantic: "window", hostWall: "wall_a", pos: [0, 1, 0] },        // same spot, DIFFERENT semantic
    { id: "f1", semantic: "wall art", pos: [0, 1, 0] },                          // no host wall ⇒ never clustered
  ];
  assert.strictEqual(RS.dupInsetIds(insets).size, 0, "no false-positive merges across wall/semantic/hostless");
});

// --- Golden room: a REAL Quest capture (45 surfaces, two rooms via connecting doors). The synthetic
// tests above encode our assumptions about the device's conventions; this one pins those assumptions to
// the actual hardware — it feeds the captured planes (with their true normals/roll) through the same
// joinCorners → snapInsets the headset runs and asserts the geometry stays sane. It's the check that
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
  const origW = {}, origTop = {};
  surfaces.filter((s) => s.semantic === "wall").forEach((s) => {
    origW[s.id] = s.extent[0]; origTop[s.id] = s._lp.y + s.extent[1] / 2;
  });
  RS.joinCorners(THREE, surfaces);
  RS.sealWalls(THREE, surfaces);        // real client pipeline: join → SEAL → snap (§9.1)
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

  // (2) Corner-joining only nudges: each wall's width changed by at most 2×GAP (an end can move ≤ GAP).
  const walls = surfaces.filter((s) => s.semantic === "wall");
  walls.forEach(function (w) {
    assert.ok(Math.abs(w.extent[0] - origW[w.id]) <= 0.5 + 1e-6, "joinCorners only nudged " + w.id);
  });

  // (2b) sealWalls only ever snaps a wall's TOP onto an actual ceiling plane (or leaves it untouched) — it
  // never invents a height. Guards the seal + the multi-room (two-ceiling) pick on the REAL capture.
  const ceilYs = surfaces.filter((s) => s.semantic === "ceiling").map((s) => s._lp.y);
  let sealedTops = 0;
  walls.forEach(function (w) {
    const top = w._lp.y + w.extent[1] / 2;
    const onCeiling = ceilYs.some((cy) => Math.abs(cy - top) < 1e-6);
    const untouched = Math.abs(top - origTop[w.id]) < 1e-6;
    if (!untouched) sealedTops++;
    assert.ok(onCeiling || untouched, "wall top sits on a ceiling or was left alone: " + w.id);
  });
  assert.ok(sealedTops > 0, "sealWalls sealed at least one wall to a ceiling on the real capture");

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

// ---- sealWalls (§9.1): snap a wall's top→ceiling and bottom→floor so the shell has no open slit ----
// A horizontal surface (floor/ceiling) as sealWalls reads it: semantic + ref-frame centre _lp + orientation
// _lq (identity ⇒ normal up, axis-aligned footprint) + extent. sealWalls uses _lq/extent for its covers() test.
function horiz(sem, x, y, z, ext) {
  return { semantic: sem, _lp: new THREE.Vector3(x, y, z), _lq: new THREE.Quaternion(), extent: ext || [4, 4] };
}

test("sealWalls snaps a wall's top to the ceiling and bottom to the floor when within tolerance", () => {
  // Wall centred at y=1.30, height 2.55 → top 2.575 (25 mm short of a 2.60 ceiling), bottom 0.025 (25 mm high).
  const wall = vert("wall_1", "wall", [1, 1.30, 0], 0, [3, 2.55]);
  RS.sealWalls(THREE, [wall, horiz("ceiling", 1, 2.60, 0), horiz("floor", 1, 0.0, 0)], 0.15);
  assert.ok(Math.abs((wall._lp.y + wall.extent[1] / 2) - 2.60) < 1e-6, "top snapped to ceiling");
  assert.ok(Math.abs((wall._lp.y - wall.extent[1] / 2) - 0.00) < 1e-6, "bottom snapped to floor");
});

test("sealWalls leaves a genuinely short wall (beyond tolerance) alone", () => {
  const wall = vert("wall_1", "wall", [1, 1.30, 0], 0, [3, 1.0]);   // top 1.8, bottom 0.8 — both far from planes
  RS.sealWalls(THREE, [wall, horiz("ceiling", 1, 2.60, 0), horiz("floor", 1, 0.0, 0)], 0.15);
  assert.equal(wall.extent[1], 1.0, "height unchanged");
  assert.equal(wall._lp.y, 1.30, "centre unchanged");
});

test("sealWalls seals to the ceiling whose FOOTPRINT covers the wall, ignoring a higher non-covering one", () => {
  const wall = vert("wall_1", "wall", [10, 1.18, 0], 0, [3, 2.30]);   // top 2.33, at x=10
  const far = horiz("ceiling", 0, 2.70, 0);      // higher, but its footprint (x 0±2.3) does NOT cover x=10
  const over = horiz("ceiling", 10, 2.40, 0);    // covers x=10 (x 10±2.3), within tol of the top
  RS.sealWalls(THREE, [wall, far, over, horiz("floor", 10, 0.03, 0)], 0.15);
  assert.ok(Math.abs((wall._lp.y + wall.extent[1] / 2) - 2.40) < 1e-6, "sealed to the covering ceiling, not the taller far one");
});

// The wall_11 case: a wall shared between two rooms whose ceilings differ by a few mm. Both footprints cover
// it; nearest-by-centre would pick the lower and leave a slit under the taller. Seal to the HIGHER.
test("sealWalls on a shared boundary wall seals to the HIGHER of two covering ceilings", () => {
  const wall = vert("wall_1", "wall", [0, 1.34, 0], 0, [3, 2.66]);    // top 2.67, on the room boundary
  const lower = horiz("ceiling", 0.6, 2.690, 0);                      // covers x=0, 2.690
  const higher = horiz("ceiling", -0.6, 2.695, 0);                    // also covers x=0, 4 mm higher
  RS.sealWalls(THREE, [wall, lower, higher, horiz("floor", 0, 0.0, 0)], 0.15);
  assert.ok(Math.abs((wall._lp.y + wall.extent[1] / 2) - 2.695) < 1e-6, "sealed to the higher ceiling (no slit under the taller)");
});

test("sealWalls changes only vertical extent — normal, width, and horizontal position are untouched", () => {
  const wall = vert("wall_1", "wall", [1, 1.30, 2], 30, [3, 2.55]);
  const n0 = normalOf(wall).clone(), w0 = wall.extent[0], x0 = wall._lp.x, z0 = wall._lp.z;
  RS.sealWalls(THREE, [wall, horiz("ceiling", 1, 2.60, 2), horiz("floor", 1, 0.0, 2)], 0.15);
  assert.equal(wall.extent[0], w0, "width (extent[0]) unchanged");
  assert.equal(wall._lp.x, x0, "x unchanged");
  assert.equal(wall._lp.z, z0, "z unchanged");
  assert.ok(n0.distanceTo(normalOf(wall)) < 1e-9, "plane normal (_lq) unchanged");
});

test("sealWalls with tol<=0 is a no-op", () => {
  const wall = vert("wall_1", "wall", [1, 1.28, 0], 0, [3, 2.55]);
  RS.sealWalls(THREE, [wall, horiz("ceiling", 1, 2.60, 0), horiz("floor", 1, 0.0, 0)], 0);
  assert.equal(wall.extent[1], 2.55, "height unchanged");
  assert.equal(wall._lp.y, 1.28, "centre unchanged");
});

// PIPELINE GUARD: sealing (vertical) must not disturb a corner joinCorners closed (horizontal). We've broken
// corner joining before by adding features; this pins that join + seal compose.
test("joinCorners + sealWalls: the joined corner survives sealing, and tops still seal", () => {
  const A = vert("real_wall_0", "wall", [-1.025, 1.2, 0], 0, [1.95, 2.35]);   // along X, 5 cm short of x=0
  const B = vert("real_wall_1", "wall", [0, 1.2, -1.025], 90, [1.95, 2.35]);  // along Z, 5 cm short of z=0
  const room = [A, B, horiz("ceiling", -1, 2.40, -1, [4, 4]), horiz("floor", -1, 0.0, -1, [4, 4])];
  RS.joinCorners(THREE, room);
  RS.sealWalls(THREE, room, 0.15);
  const corner = new THREE.Vector2(0, 0), ea = planEnds(A), eb = planEnds(B);
  assert.ok(Math.min(ea[0].distanceTo(corner), ea[1].distanceTo(corner)) < 1e-6, "wall A still reaches the corner");
  assert.ok(Math.min(eb[0].distanceTo(corner), eb[1].distanceTo(corner)) < 1e-6, "wall B still reaches the corner");
  const pair = Math.min.apply(null, ea.flatMap((p) => eb.map((q) => p.distanceTo(q))));
  assert.ok(pair < 1e-6, "the joined corner survived sealing (vertical seal left the horizontal join intact)");
  assert.ok(Math.abs((A._lp.y + A.extent[1] / 2) - 2.40) < 1e-6, "A top sealed to the ceiling");
  assert.ok(Math.abs((B._lp.y + B.extent[1] / 2) - 2.40) < 1e-6, "B top sealed to the ceiling");
});

// PIPELINE GUARD: seal runs BEFORE snapInsets so a door's opening is cut against the SEALED wall. If the
// order flipped (snap then seal), the hole's stored y would be relative to the un-sealed centre and the
// opening would ride off the door when the wall re-centres. This pins the order.
test("sealWalls + snapInsets: a door's opening lands on the door even though sealing moved the wall centre", () => {
  const wall = vert("real_wall_0", "wall", [2, 1.15, 0], 90, [4, 2.3]);   // top 2.3 (short), bottom 0.0
  const door = vert("real_door_1", "door", [2, 1.0, 1.0], 90, [0.9, 2]);  // door at world y = 1.0
  const room = [wall, door, horiz("ceiling", 2, 2.40, 0, [6, 6]), horiz("floor", 2, 0.0, 0, [6, 6])];
  RS.sealWalls(THREE, room, 0.15);
  assert.ok(Math.abs(wall._lp.y - 1.2) < 1e-6, "sealing raised the wall's centre (1.15 → 1.2)");
  RS.snapInsets(THREE, room);
  const h = wall.holes && wall.holes[0];
  assert.ok(h, "the door cut an opening in the sealed wall");
  const wx = new THREE.Vector3(1, 0, 0).applyQuaternion(wall._lq);
  const wy = new THREE.Vector3(0, 0, -1).applyQuaternion(wall._lq);
  const n = new THREE.Vector3(0, 1, 0).applyQuaternion(wall._lq);
  const rebuilt = wall._lp.clone().add(wx.clone().multiplyScalar(h.x)).add(wy.clone().multiplyScalar(h.y));
  const doorPos = new THREE.Vector3(...door.position);
  const inPlane = doorPos.clone().sub(n.clone().multiplyScalar(doorPos.clone().sub(wall._lp).dot(n)));
  assert.ok(rebuilt.distanceTo(inPlane) < 1e-6, "opening lands on the door (hole cut against the sealed wall)");
});

// ---- heightCensus + explainNoMatch: the geometry event log's two measurements -------------------------
// Both exist for the field symptoms recorded in docs/backlogs/spaces-geometry.md. They are tested here,
// with the rest of the pure geometry, for the reason they live here at all: a diagnostic that quietly
// disagrees with the code it diagnoses sends you the wrong way, which is worse than no diagnostic.

test("heightCensus reports each floor/ceiling height and attributes every wall to the room below it", () => {
  // Two rooms side by side, room B's floor 13 cm high (the reported symptom), each wall over its own floor.
  const fA = horiz("floor", 0, 0.00, 0, [4, 4]), cA = horiz("ceiling", 0, 2.60, 0, [4, 4]);
  const fB = horiz("floor", 8, 0.13, 0, [4, 4]), cB = horiz("ceiling", 8, 2.73, 0, [4, 4]);
  fA.id = "floor_A"; cA.id = "ceil_A"; fB.id = "floor_B"; cB.id = "ceil_B";
  const wA = vert("wall_A", "wall", [0, 1.30, 1.9], 0, [4, 2.6]);      // bottom 0.00 → gap 0 over floor A
  const wB = vert("wall_B", "wall", [8, 1.43, 1.9], 0, [4, 2.6]);      // bottom 0.13 → gap 0 over floor B
  const cen = RS.heightCensus(THREE, [fA, cA, fB, cB, wA, wB]);

  assert.deepStrictEqual(cen.floors.map((f) => f.id), ["floor_A", "floor_B"], "both floors, sorted by id");
  assert.ok(Math.abs(cen.floors[1].y - 0.13) < 1e-9, "the raised floor's height is reported as-is");
  assert.deepStrictEqual(cen.ceilings.map((c) => c.id), ["ceil_A", "ceil_B"]);

  const byId = Object.fromEntries(cen.walls.map((w) => [w.id, w]));
  assert.strictEqual(byId.wall_A.floor, "floor_A", "wall A attributed to the room it stands in");
  assert.strictEqual(byId.wall_B.floor, "floor_B", "wall B attributed to the OTHER room, not the nearest floor by height");
  // The gap is the frame-independent quantity: both walls sit ON their own floor, so both read ~0 even
  // though their absolute bottoms differ by 13 cm. That is exactly what distinguishes "the whole region
  // moved" (gaps unchanged) from "the floor plane alone re-fit" (gap opens).
  assert.ok(Math.abs(byId.wall_A.gap) < 1e-9 && Math.abs(byId.wall_B.gap) < 1e-9,
            "a wall standing on its own floor reads gap 0 regardless of that floor's absolute height");
});

test("heightCensus is taken pre-seal: sealing erases the very gap it measures", () => {
  const floor = horiz("floor", 0, 0.00, 0, [6, 6]), ceil = horiz("ceiling", 0, 2.60, 0, [6, 6]);
  floor.id = "floor_1"; ceil.id = "ceil_1";
  const wall = vert("wall_1", "wall", [0, 1.34, 2], 0, [4, 2.5]);       // bottom 0.09 — 9 cm short of the floor
  const before = RS.heightCensus(THREE, [floor, ceil, wall]);
  assert.ok(Math.abs(before.walls[0].gap - 0.09) < 1e-9, "pre-seal, the wall's 9 cm gap to its floor is visible");
  RS.sealWalls(THREE, [floor, ceil, wall], 0.15);
  const after = RS.heightCensus(THREE, [floor, ceil, wall]);
  assert.ok(Math.abs(after.walls[0].gap) < 1e-9, "post-seal every wall agrees with its floor by construction");
});

test("explainNoMatch says 'device' only when no plane could plausibly BE this wall", () => {
  const probe = { pos: new THREE.Vector3(0, 1.2, 0), nyaw: 0, sem: "wall", orient: "vertical", ext: [3, 2.4] };
  assert.strictEqual(RS.explainNoMatch(THREE, probe, [], {}).why, "device", "nothing detected at all");

  // A wall facing 90° away and 9 m off-plane is a DIFFERENT wall, not a failed match of this one.
  const otherWall = { id: "r9", pos: new THREE.Vector3(9, 1.2, 0), nyaw: 90 * D2R, sem: "wall",
                      orient: "vertical", ext: [3, 2.4] };
  assert.strictEqual(RS.explainNoMatch(THREE, probe, [otherWall], {}).why, "device");

  // But a same-facing plane 9 m along the SAME infinite plane is NOT "device" — it is a real matchWall
  // rejection on the overlap guard, and calling it a device miss would send you hunting the Quest for a
  // plane it did emit. This is the case centroid-ranking gets wrong, which is why ranking is plane-relative.
  const sameplane = { id: "r1", pos: new THREE.Vector3(9, 1.2, 0), nyaw: 0, sem: "wall",
                      orient: "vertical", ext: [3, 2.4] };
  const r = RS.explainNoMatch(THREE, probe, [sameplane], {});
  assert.strictEqual(r.why, "matcher");
  assert.strictEqual(r.gate, "gap", "coincident + same-facing, rejected on along-line overlap");
});

test("explainNoMatch names the matchWall gate that rejected a candidate, with its margin", () => {
  const at = (x, z, yawDeg, ext) => ({ id: "r1", pos: new THREE.Vector3(x, 1.2, z), nyaw: yawDeg * D2R,
                                       sem: "wall", orient: "vertical", ext: ext || [3, 2.4] });
  const probe = { pos: new THREE.Vector3(0, 1.2, 0), nyaw: 0, sem: "wall", orient: "vertical", ext: [3, 2.4] };
  const opts = { perpTol: 0.15, yawTol: 30 * D2R, overlapSlop: 0.3 };

  // Gate 1 — facing. 40° apart, so it fails on yaw before anything else is even considered.
  const y = RS.explainNoMatch(THREE, probe, [at(0, 0.05, 40)], opts);
  assert.strictEqual(y.gate, "dyaw");
  assert.ok(Math.abs(y.val - 40) < 0.05 && y.tol === 30, "reports degrees against the degree tolerance");

  // Gate 2 — the coincident-plane test. The candidate's normal is +Z, so a 0.19 m offset in z is
  // perpendicular: 4 cm past tolerance, which is the number that would tell you what to set --wall-perp-tol to.
  const p = RS.explainNoMatch(THREE, probe, [at(0, 0.19, 0)], opts);
  assert.strictEqual(p.gate, "perp");
  assert.ok(Math.abs(p.val - 0.19) < 1e-6 && p.tol === 0.15);
  assert.strictEqual(p.id, "r1", "and names which reference surface it was");

  // Gate 3 — along-line overlap. Same plane, but slid 4 m along it: two colinear-but-separate walls.
  const g = RS.explainNoMatch(THREE, probe, [at(4, 0, 0)], opts);
  assert.strictEqual(g.gate, "gap");
  assert.ok(Math.abs(g.val - 1.0) < 1e-6, "4 m apart less the two half-widths = a 1 m gap");

  // Every gate passes ⇒ it WAS matchable, so it must already have been claimed by another plane this
  // capture. That is the id-swap shape and gets its own name rather than being reported as unexplained.
  assert.strictEqual(RS.explainNoMatch(THREE, probe, [at(0, 0.02, 2)], opts).gate, "claimed");
});

test("explainNoMatch falls back to matchRef's radius for horizontals", () => {
  const probe = { pos: new THREE.Vector3(0, 0, 0), nyaw: 0, sem: "floor", orient: "horizontal", ext: [4, 4] };
  const near = { id: "f1", pos: new THREE.Vector3(0.9, 0, 0), nyaw: 0, sem: "floor", orient: "horizontal", ext: [4, 4] };
  const r = RS.explainNoMatch(THREE, probe, [near], {});
  assert.strictEqual(r.gate, "dist");
  assert.ok(Math.abs(r.val - 0.9) < 1e-6 && r.tol === 0.5, "a floor is matched by centroid radius, not by plane");
});

test("floorUnder attributes a point to the room whose FOOTPRINT covers it, not the nearest floor by height", () => {
  // The raised-floor case: room B's floor renders 13 cm high, and you rest the controller on the real
  // floor in room B (y ~ 0). Nearest-by-height would pick room A's floor (0.00, 13 cm closer than 0.13)
  // and blame the wrong room — the one failure this helper exists to prevent.
  const a = horiz("floor", 0, 0.00, 0, [4, 4]); a.id = "floor_A";
  const b = horiz("floor", 8, 0.13, 0, [4, 4]); b.id = "floor_B";
  const under = RS.floorUnder(THREE, [a, b], 8, 0.5);       // standing in room B
  assert.strictEqual(under.id, "floor_B");
  assert.strictEqual(RS.floorUnder(THREE, [a, b], 0, 0.5).id, "floor_A");
  assert.strictEqual(RS.floorUnder(THREE, [a, b], 40, 0), null, "outside every room ⇒ no attribution");
});

// polyFit measures the ONE reduction the whole geometry pipeline is built on and nothing else checks:
// Pass A throws away `plane.polygon` and keeps only the two spans of its bounding box. These tests pin
// both quantities, including the sign-free cases that would otherwise pass by accident.

test("polyFit reports a centred rectangle as a perfect fit", () => {
  // The expected shape of a Quest wall plane: 4 points, symmetric about the pose origin.
  const rect = [{ x: -2, z: -1.2 }, { x: 2, z: -1.2 }, { x: 2, z: 1.2 }, { x: -2, z: 1.2 }];
  const f = RS.polyFit(rect);
  assert.ok(Math.abs(f.off) < 1e-9, "AABB centre sits on the pose origin ⇒ the rendered rect is not displaced");
  assert.ok(Math.abs(f.fill - 1) < 1e-9, "a rectangle fills its own bounding box exactly");
  assert.strictEqual(f.n, 4);
});

test("polyFit reports the offset when the polygon is not centred on the pose origin", () => {
  // The untested hypothesis this exists for: we take the AABB's DIMENSIONS but keep the plane's pose
  // origin as the rect's centre, so an off-centre polygon displaces every rendered surface by (cx, cz) —
  // identically in the render, the posted seed, registration and every anchor, which is exactly why it
  // would never be noticed. 0.3 along local X, 0.1 along local Z.
  const shifted = [{ x: -1.7, z: -1.1 }, { x: 2.3, z: -1.1 }, { x: 2.3, z: 1.3 }, { x: -1.7, z: 1.3 }];
  const f = RS.polyFit(shifted);
  assert.ok(Math.abs(f.cx - 0.3) < 1e-9 && Math.abs(f.cz - 0.1) < 1e-9);
  assert.ok(Math.abs(f.off - Math.hypot(0.3, 0.1)) < 1e-9);
  assert.ok(Math.abs(f.fill - 1) < 1e-9, "still a rectangle — displacement and shape are independent faults");
});

test("polyFit reports fill below 1 for a wall that is not a rectangle", () => {
  // An L-shaped wall: the bounding box is 4x4 (area 16) and the polygon is that box minus a 2x2 bite
  // (area 12), so a quarter of what we render and POST into the seed is surface that does not exist.
  const ell = [{ x: 0, z: 0 }, { x: 4, z: 0 }, { x: 4, z: 2 }, { x: 2, z: 2 }, { x: 2, z: 4 }, { x: 0, z: 4 }];
  const f = RS.polyFit(ell);
  assert.ok(Math.abs(f.fill - 0.75) < 1e-9);
  assert.strictEqual(f.n, 6);
});

test("polyFit is winding-independent and declines a degenerate polygon", () => {
  // Shoelace is signed, so a clockwise polygon would report a NEGATIVE fill and read as a fully broken
  // surface rather than a fine one. The Quest's winding is not something we control.
  const ccw = [{ x: -1, z: -1 }, { x: 1, z: -1 }, { x: 1, z: 1 }, { x: -1, z: 1 }];
  const cw = ccw.slice().reverse();
  assert.ok(Math.abs(RS.polyFit(ccw).fill - RS.polyFit(cw).fill) < 1e-12);
  assert.ok(RS.polyFit(ccw).fill > 0);
  // Below 3 points there is no polygon; Pass A already substitutes a 1x1 extent there, and reporting a
  // fit for it would be inventing a measurement.
  assert.strictEqual(RS.polyFit([{ x: 0, z: 0 }, { x: 1, z: 0 }]), null);
  assert.strictEqual(RS.polyFit(undefined), null);
});

test("polyFit on the golden room: the real device's planes are centred rectangles", () => {
  // The synthetic tests above encode what we EXPECT of the hardware; this asks the hardware. The golden
  // fixture is a posted capture, so it carries no polygons — reconstruct each surface's polygon from its
  // stored extent (the rectangle Pass A derived) and assert the fit is exact. That makes this a guard on
  // polyFit's arithmetic against real extents rather than evidence about the device: the on-device answer
  // comes from the [aabb] probe line, and if it ever disagrees with this, the polygon is the thing that
  // differs and the hypothesis is confirmed.
  const golden = require("./fixtures/golden-room.json");
  const list = golden.surfaces || golden;
  let checked = 0;
  list.forEach((e) => {
    const ext = (e.components && e.components.surface && e.components.surface.extent) || e.extent;
    if (!ext) return;
    const hw = ext[0] / 2, hh = ext[1] / 2;
    const f = RS.polyFit([{ x: -hw, z: -hh }, { x: hw, z: -hh }, { x: hw, z: hh }, { x: -hw, z: hh }]);
    assert.ok(Math.abs(f.off) < 1e-9 && Math.abs(f.fill - 1) < 1e-9, `${e.id} round-trips its extent`);
    checked++;
  });
  assert.ok(checked > 30, `expected the golden room's surfaces, got ${checked}`);
});

test("a seed surface carried back through Tmat⁻¹ lands exactly on its F_track plane", () => {
  // The round-trip the local-first architecture rests on, and the one the surface debug overlay draws:
  // the owner posts each plane at Tmat·plane and stores eulerYXZ() of that pose, and any client wanting
  // the seed in its OWN frame applies Tmat⁻¹. If the two halves disagree, the seed is silently stored in a
  // frame nothing can undo — which would look like registration error and be a convention bug.
  //
  // Pinned at the BOUNDARY-FLIP magnitude (167° yaw + 3 m, docs/specs/spaces-geometry.md §4.1) rather
  // than a gentle offset, and on a TILTED plane as well as an upright one, because the -90° X conversion
  // inside eulerYXZ is order-sensitive and a small-angle test passes on the wrong composition.
  const flip = new THREE.Matrix4().compose(
    new THREE.Vector3(4.1, 0, -1.9),
    new THREE.Quaternion().setFromAxisAngle(UP, 167 * D2R),
    new THREE.Vector3(1, 1, 1));

  for (const [name, s] of [["upright", vert("w", "wall", [1.3, 1.05, -2.7], 37, [3.2, 2.4])],
                           ["tilted", vert("a", "wall art", [-0.4, 1.6, 2.2], -114, [0.6, 0.9])]]) {
    if (name === "tilted") {   // multi-axis: the case that exposes a wrong euler ORDER
      s._lq.multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), 11 * D2R));
    }
    // Author: the F_ref pose the owner posts, stored as YXZ euler degrees.
    const refM = flip.clone().multiply(
      new THREE.Matrix4().compose(s._lp, s._lq, new THREE.Vector3(1, 1, 1)));
    const rp = new THREE.Vector3(), rq = new THREE.Quaternion(), rs = new THREE.Vector3();
    refM.decompose(rp, rq, rs);
    const stored = RS.eulerYXZ(THREE, rq);

    // Solve: read it back in F_track. Reconstruct the a-plane quaternion from the stored YXZ degrees,
    // then undo the frame — exactly what the overlay's seed group does with Tmat⁻¹ on its matrix.
    const back = new THREE.Matrix4()
      .compose(rp, new THREE.Quaternion().setFromEuler(
        new THREE.Euler(stored[0] * D2R, stored[1] * D2R, stored[2] * D2R, "YXZ")),
        new THREE.Vector3(1, 1, 1))
      .premultiply(flip.clone().invert());
    const bp = new THREE.Vector3(), bq = new THREE.Quaternion(), bs = new THREE.Vector3();
    back.decompose(bp, bq, bs);

    // The local render draws the same surface from eulerYXZ() of its raw F_track plane quaternion.
    const localRot = RS.eulerYXZ(THREE, s._lq);
    const lq = new THREE.Quaternion().setFromEuler(
      new THREE.Euler(localRot[0] * D2R, localRot[1] * D2R, localRot[2] * D2R, "YXZ"));

    assert.ok(bp.distanceTo(s._lp) < 1e-9, `${name}: position round-trips (${bp.distanceTo(s._lp)})`);
    assert.ok(Math.min(bq.angleTo(lq), bq.angleTo(lq.clone().set(-lq.x, -lq.y, -lq.z, -lq.w))) < 1e-7,
      `${name}: orientation round-trips`);
  }
});

// planeCorners + canonicalFrame's origin option. The measurement behind the DEFAULT is in the spec (§4.1.3);
// these tests pin the behaviour either choice must have, and the fallback that keeps a two-corner room
// from getting a frame built on a single point.

test("planeCorners finds a rectangular room's four corners from the wall PLANES", () => {
  // A 6x4 room. Each corner is the intersection of two perpendicular wall planes, so it is exact and
  // independent of how much of either wall was scanned — the §4.2 property.
  const room = [vert("n", "wall", [0, 1.2, -2], 180, [6, 2.4]),
                vert("s", "wall", [0, 1.2, 2], 0, [6, 2.4]),
                vert("e", "wall", [3, 1.2, 0], 90, [4, 2.4]),
                vert("w", "wall", [-3, 1.2, 0], 270, [4, 2.4])];
  const cur = room.map((s) => ({ id: s.id, sem: s.semantic, orient: "vertical", ext: s.extent,
                                 pos: s._lp, nyaw: RS.yawOf(normalOf(s)) }));
  const k = RS.planeCorners(THREE, cur);
  assert.strictEqual(k.length, 4);
  const got = k.map((p) => `${p.x.toFixed(2)},${p.z.toFixed(2)}`).sort();
  assert.deepStrictEqual(got, ["-3.00,-2.00", "-3.00,2.00", "3.00,-2.00", "3.00,2.00"].sort());
  // The mean of the four corners is the room's true centre — and unlike the wall-centre mean it does not
  // care that (say) the north wall was only half scanned.
  const mx = k.reduce((a, p) => a + p.x, 0) / 4, mz = k.reduce((a, p) => a + p.z, 0) / 4;
  assert.ok(Math.abs(mx) < 1e-9 && Math.abs(mz) < 1e-9);
});

test("planeCorners tolerates a wall ending short of its corner, where wallCorners does not", () => {
  // The ONE difference that matters here. wallCorners requires both walls' nearest ENDS within 25 cm of
  // the crossing — right for anchoring an inset (a corner an inset measures from must physically exist)
  // and wrong for a global origin, because it makes membership depend on scan extent and a mean over a
  // churning set moves even though every member of it is exact.
  //
  // Note the property is WEAKER than "extent-independent": planeCorners still bounds the crossing by
  // hw + 0.6 m from each wall's centre, so a wall scanned down to a stub far from the corner still drops
  // out. planeCorners' own bound is an ABSOLUTE radius for exactly this reason — see the bound test
  // below and §4.1.3.
  const north = vert("n", "wall", [0, 1.2, -2], 180, [5, 2.4]);   // right end at x=2.5 → 0.5 m short
  const east = vert("e", "wall", [3, 1.2, 0], 90, [4, 2.4]);
  const cur = [north, east].map((s) => ({ id: s.id, sem: s.semantic, orient: "vertical",
    ext: s.extent, pos: s._lp, nyaw: RS.yawOf(normalOf(s)) }));

  const k = RS.planeCorners(THREE, cur);
  assert.strictEqual(k.length, 1, "the plane crossing is found despite the 0.5 m shortfall");
  assert.ok(Math.abs(k[0].x - 3) < 1e-9 && Math.abs(k[0].z + 2) < 1e-9, "and it is the true corner");

  // The same geometry through wallCorners: 0.5 m is past its 25 cm gap, so it reports no corner at all.
  const viaWallCorners = RS.wallCorners(THREE, [north, east]);
  const total = [...viaWallCorners.values()].reduce((n, arr) => n + arr.length, 0);
  assert.strictEqual(total, 0, "wallCorners declines it — the behaviour planeCorners exists to differ from");
});

test("planeCorners bounds crossings by an ABSOLUTE radius, not by scan extent", () => {
  // The bound is the only place membership can leak scan-dependence back in, and an earlier cut leaked it
  // badly: bounding by `hw + 0.6 m` from each wall centre made membership a function of the scan extent,
  // which is the artifact corners exist to be immune to. Measured on the golden room with every wall
  // present and extents re-scanned ±25 cm, that bound drifted the origin 4.1 cm; an absolute radius
  // drifts it 0.0000 m.
  //
  // Spurious crossings inside the radius are kept ON PURPOSE. The canonical origin is arbitrary, so it
  // must be REPRODUCIBLE, not meaningful — a phantom corner that is always there costs nothing, while a
  // real one that comes and goes costs everything.
  const near = [vert("a", "wall", [0, 1.2, 0], 0, [3, 2.4]),
                vert("b", "wall", [20, 1.2, 20], 90, [3, 2.4])];
  const asCur = (list) => list.map((s) => ({ id: s.id, sem: s.semantic, orient: "vertical",
    ext: s.extent, pos: s._lp, nyaw: RS.yawOf(normalOf(s)) }));
  assert.strictEqual(RS.planeCorners(THREE, asCur(near)).length, 1,
    "20 m apart is still within a building — kept, because consistency beats meaningfulness");

  // Far enough out that the crossing would drag the origin outside the building. That matters beyond
  // tidiness: content displacement scales with (theta error x distance from the origin), so a runaway
  // origin amplifies any later yaw error.
  const far = [vert("a", "wall", [0, 1.2, 0], 0, [3, 2.4]),
               vert("b", "wall", [90, 1.2, 90], 90, [3, 2.4])];
  assert.strictEqual(RS.planeCorners(THREE, asCur(far)).length, 0);

  // And the radius really is absolute: re-scanning both walls to a tenth of their length changes no
  // membership, where an extent-derived bound would have dropped the corner entirely.
  const stubs = [vert("a", "wall", [0, 1.2, 0], 0, [0.3, 2.4]),
                 vert("b", "wall", [20, 1.2, 20], 90, [0.3, 2.4])];
  assert.strictEqual(RS.planeCorners(THREE, asCur(stubs)).length, 1);
});

test("canonicalFrame with origin:corners uses them, and falls back below three", () => {
  const mkCur = (surfaces) => surfaces.map((s) => ({ id: s.id, sem: s.semantic, orient: "vertical",
    ext: s.extent, pos: s._lp, nyaw: RS.yawOf(normalOf(s)) }));
  const room = mkCur([vert("n", "wall", [0, 1.2, -2], 180, [6, 2.4]),
                      vert("s", "wall", [0, 1.2, 2], 0, [6, 2.4]),
                      vert("e", "wall", [3, 1.2, 0], 90, [4, 2.4]),
                      vert("w", "wall", [-3, 1.2, 0], 270, [4, 2.4])]);
  const byCorners = RS.canonicalFrame(THREE, room, { origin: "corners" });
  assert.ok(/org=corners=4/.test(byCorners.stat), byCorners.stat);
  assert.ok(RS.canonicalFrame(THREE, room).stat.includes("org=centres"), "centres is the default");

  // Two walls give ONE corner, and "the mean of one corner" is that corner — which for an L of two walls
  // sits at their crossing, metres from the room. Falling back is not a nicety: a frame built on a single
  // point is exactly the metre-scale error this whole change exists to remove. The stat says which was
  // used, so a fallback is never silent.
  const two = mkCur([vert("n", "wall", [0, 1.2, -2], 180, [6, 2.4]),
                     vert("e", "wall", [3, 1.2, 0], 90, [4, 2.4])]);
  const f2 = RS.canonicalFrame(THREE, two, { origin: "corners" });
  assert.ok(f2.Tmat, "still produces a frame");
  assert.ok(/org=centres\(corners=1\)/.test(f2.stat), f2.stat);
});

test("canonicalFrame stays invariant to the session's tracking origin under BOTH origin bases", () => {
  // The property canonicalFrame exists for: the same physical room must canonicalize to the same frame
  // whatever arbitrary yaw/offset `local-floor` happened to establish. This is what a recenter changes,
  // and it must not move content — the invariance has to hold for whichever origin basis is selected.
  const base = [vert("n", "wall", [0, 1.2, -2], 180, [6, 2.4]),
                vert("s", "wall", [0, 1.2, 2], 0, [6, 2.4]),
                vert("e", "wall", [3, 1.2, 0], 90, [4, 2.4]),
                vert("w", "wall", [-3, 1.2, 0], 270, [4, 2.4])];
  const toCur = (surfaces) => surfaces.map((s) => ({ id: s.id, sem: s.semantic, orient: "vertical",
    ext: s.extent, pos: s._lp, nyaw: RS.yawOf(normalOf(s)) }));

  // Re-express the same room in a session frame rotated 137° and shifted 5 m — a recenter, in effect.
  const yaw = 137 * D2R;
  const R = new THREE.Quaternion().setFromAxisAngle(UP, yaw);
  const shift = new THREE.Vector3(5, 0, -3);
  const moved = base.map((s) => {
    const lq = R.clone().multiply(s._lq);
    return { id: s.id, semantic: s.semantic, extent: s.extent,
             _lp: s._lp.clone().applyQuaternion(R).add(shift), _lq: lq };
  });

  ["centres", "corners"].forEach((origin) => {
    const a = RS.canonicalFrame(THREE, toCur(base), { origin });
    const b = RS.canonicalFrame(THREE, toCur(moved), { origin });
    // A point of content at fixed CANONICAL coords must land on the same PHYSICAL spot either way, i.e.
    // Tmat⁻¹·K differs exactly by the session transform. Probe off the rotation axis, or a theta change
    // would be invisible.
    const K = new THREE.Vector3(2, 1.6, 1);
    const pa = K.clone().applyMatrix4(a.Tmat.clone().invert());
    const pb = K.clone().applyMatrix4(b.Tmat.clone().invert());
    const expect = pa.clone().applyQuaternion(R).add(shift);
    assert.ok(pb.distanceTo(expect) < 1e-6,
      `${origin}: frame is not invariant to the session origin (off by ${pb.distanceTo(expect)})`);
  });
});

test("the corner origin is EXACTLY invariant to how much of each wall was scanned", () => {
  // This is the property corners were wanted for, and the one an earlier extent-derived bound quietly
  // destroyed. Same room, same wall planes, every wall re-scanned to a different length and slid along
  // itself — which is precisely what "a wall's centre is a scan artifact" (§4.2) describes. The corner
  // origin must not move AT ALL, because a plane crossing does not know how much of either wall was seen.
  //
  // The wall-centre origin does move, and is measured here alongside so the trade is explicit rather than
  // asserted: on the golden room the same perturbation moves it ~2.5 cm.
  const mk = (spec) => spec.map(([id, pos, yaw, ext]) =>
    vert(id, "wall", pos, yaw, ext));
  const asCur = (list) => list.map((s) => ({ id: s.id, sem: s.semantic, orient: "vertical",
    ext: s.extent, pos: s._lp, nyaw: RS.yawOf(normalOf(s)) }));
  const originOf = (pts) => pts.length
    ? { x: pts.reduce((a, p) => a + p.x, 0) / pts.length, z: pts.reduce((a, p) => a + p.z, 0) / pts.length }
    : null;

  // A 6x4 room, fully scanned.
  const full = asCur(mk([["n", [0, 1.2, -2], 180, [6, 2.4]], ["s", [0, 1.2, 2], 0, [6, 2.4]],
                         ["e", [3, 1.2, 0], 90, [4, 2.4]], ["w", [-3, 1.2, 0], 270, [4, 2.4]]]));
  // The same room, each wall seen partially and from a different part of its length. The PLANES are
  // untouched — only the centre-plus-extent description of each wall differs.
  const rescanned = asCur(mk([["n", [-1.5, 1.2, -2], 180, [3, 2.4]], ["s", [2, 1.2, 2], 0, [2, 2.4]],
                              ["e", [3, 1.2, 1], 90, [2, 2.4]], ["w", [-3, 1.2, -0.5], 270, [3, 2.4]]]));

  const kA = originOf(RS.planeCorners(THREE, full)), kB = originOf(RS.planeCorners(THREE, rescanned));
  assert.strictEqual(RS.planeCorners(THREE, full).length, 4);
  assert.strictEqual(RS.planeCorners(THREE, rescanned).length, 4, "membership is unchanged by the re-scan");
  assert.ok(Math.hypot(kA.x - kB.x, kA.z - kB.z) < 1e-12,
    `corner origin must be exactly invariant, moved ${Math.hypot(kA.x - kB.x, kA.z - kB.z)}`);

  // The contrast, for the record: the wall-centre origin follows the scan.
  const cA = originOf(full.map((c) => ({ x: c.pos.x, z: c.pos.z })));
  const cB = originOf(rescanned.map((c) => ({ x: c.pos.x, z: c.pos.z })));
  assert.ok(Math.hypot(cA.x - cB.x, cA.z - cB.z) > 0.1,
    "the wall-centre origin does move — that is the trade --void-origin selects");
});
