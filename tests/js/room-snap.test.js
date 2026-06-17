// Unit tests for the pure room-snapping geometry (client/room-snap.js), run with `node --test`.
// Synthetic rooms give full control: we build surfaces with known facings/positions (mirroring what a
// Quest capture produces), run the snapping, and assert the invariants we fought hard for — insets land
// in front of their wall toward the room interior (incl. junction doors), walls come out square, the
// frame solve recovers a known transform, and rotations are emitted in A-Frame's YXZ order.
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

test("uprightInset orients a plane gravity-up, facing the room", () => {
  // +X wall ⇒ interior is -X; art should face -X with its texture-top toward world +Y.
  const e = RS.uprightInset(THREE, new THREE.Vector3(-1, 0, 0));
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(e[0] * D2R, e[1] * D2R, e[2] * D2R, "YXZ"));
  const up = new THREE.Vector3(0, 1, 0).applyQuaternion(q);      // a-plane local +Y (texture top)
  const fwd = new THREE.Vector3(0, 0, 1).applyQuaternion(q);     // a-plane local +Z (front face)
  assert.ok(up.distanceTo(new THREE.Vector3(0, 1, 0)) < 1e-6, "texture-top points to world up");
  assert.ok(fwd.distanceTo(new THREE.Vector3(-1, 0, 0)) < 1e-6, "front faces into the room");
});

test("snapInsets renders wall art upright regardless of the captured plane's roll", () => {
  // Give the art a rolled-over capture (local +Z pointing DOWN) — the case that rendered images
  // upside-down when art adopted the wall's orientation.
  const surfaces = [
    vert("real_wall_0", "wall", [2, 1.2, 0], 90, [4, 2.4]),
    vert("real_art_1", "wall art", [2, 1.2, 0.5], 90, [0.6, 0.9]),
  ];
  surfaces[1]._lq.multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), Math.PI));
  RS.snapInsets(THREE, surfaces);
  const art = surfaces[1];
  const q = new THREE.Quaternion().setFromEuler(
    new THREE.Euler(art.rotation[0] * D2R, art.rotation[1] * D2R, art.rotation[2] * D2R, "YXZ"));
  const up = new THREE.Vector3(0, 1, 0).applyQuaternion(q);
  assert.ok(up.y > 0.999, "wall art's up axis points up in the world (image is upright)");
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
