// Per-capture cost of the geometry event log + floating-room correction, on a REAL room.
//
//   node scripts/geo_bench.mjs [path/to/space-N.json]
//
// The spec (§10) claims these probes add "sub-0.1 ms to a capture that runs ~5 ms". That claim was an
// argument from where the code sits — capture body at 0.5 Hz, transitions only, no per-frame work — not a
// measurement, and it stayed unmeasured long enough to be worth fixing.
//
// WHAT THIS COVERS: the pure JS added to every capture — heightCensus, levelDeviation over every surface,
// and floatingRoom. That is the whole steady-state addition; explainNoMatch runs only on a miss, and the
// log's fetch is batched on a timer rather than per event.
//
// WHAT IT DOES NOT COVER, and why the on-device A/B is still the real test: a Quest's CPU is several times
// slower than a laptop's, and this measures none of the browser-side cost (the fetch, JSON.stringify of a
// flushed batch). Treat the number as a floor with a known direction of error, not as the answer. The
// device measurement is `--debug-jitter` with and without, holding the spec's 31/33-captures-≤6 ms baseline.

import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.join(here, "..", "package.json"));
const THREE = require("three");
const RS = require(path.join(here, "..", "client", "room-snap.js"));
const WM = require(path.join(here, "..", "client", "world-model.js"));
const fs = require("fs");
const os = require("os");

const SPACE = process.argv[2]
  || path.join(os.homedir(), ".local/share/conjure/users/daniel/spaces/space-1.json");
const doc = JSON.parse(fs.readFileSync(SPACE, "utf8"));
const D2R = Math.PI / 180;
const RX90 = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), Math.PI / 2);

// A capture built from the stored room, displaced the way the field fault displaces it — so the detector
// does its full job (finds a room, builds a membership set) rather than bailing early on the cheap path.
const seed = {}, basis = [], live = {}, surfaces = [];
for (const e of doc.surfaces) {
  const m = e.meta || {}, sem = m.semantic, t = e.transform || {};
  const p = t.position, rot = t.rotation || [0, 0, 0];
  const ext = ((e.components || {}).surface || {}).extent;
  if (!p) continue;
  const low = (y) => y - ((ext && ext[1]) || 0) / 2;
  seed[e.id] = { y: sem === "wall" ? low(p[1]) : p[1], sem };
  if (sem === "floor" || sem === "ceiling") basis.push(e.id);
  const bedroom = Math.abs(p[0] - 2.43) < 2.4 && Math.abs(p[2] - 1.42) < 2.0;
  const y = p[1] + (bedroom ? 0.095 : 0.0) + 0.002;      // one room displaced, plus a little drift
  live[e.id] = sem === "wall" ? low(y) : y;
  surfaces.push({
    id: e.id, semantic: sem, extent: ext, hostWall: m.host_wall,
    _lp: new THREE.Vector3(p[0], y, p[2]),
    _lq: new THREE.Quaternion()
      .setFromEuler(new THREE.Euler(rot[0] * D2R, rot[1] * D2R, rot[2] * D2R, "YXZ")).multiply(RX90),
  });
}

function capture() {                                     // exactly what a capture adds, in order
  const cen = RS.heightCensus(THREE, surfaces);
  const flat = {};
  cen.floors.concat(cen.ceilings).forEach((f) => { flat[f.id] = f.y; });
  const dev = {};
  WM.levelDeviation(live, seed, basis).forEach((d) => { dev[d.id] = d.dev; });
  const fix = RS.floatingRoom(THREE, surfaces, dev, { minM: 0.04 });
  return { cen, flat, dev, fix };
}

const { cen, fix } = capture();
console.log(`space   : ${SPACE}`);
console.log(`surfaces: ${surfaces.length}  (${cen.floors.length} floors, ${cen.ceilings.length} ceilings, `
  + `${cen.walls.length} walls, ${cen.insets.length} insets)`);
console.log(`detector: ${fix ? `corrects ${fix.floor} by ${(fix.offset * 1000).toFixed(0)} mm, `
  + `${fix.ids.length} surfaces` : "no correction (fixture did not reproduce the fault)"}`);

for (let i = 0; i < 2000; i++) capture();                // warm the JIT
const N = 20000, t0 = process.hrtime.bigint();
for (let i = 0; i < N; i++) capture();
const us = Number(process.hrtime.bigint() - t0) / 1000 / N;

console.log(`\nper capture: ${us.toFixed(1)} µs  (${(us / 1000).toFixed(4)} ms) over ${N} runs`);
console.log(`  against an ~5 ms capture that is ${(us / 1000 / 5 * 100).toFixed(2)}% — desktop Node.`);
console.log(`  a Quest is several times slower, so scale accordingly; the browser-side fetch is`);
console.log(`  batched on a timer and is not measured here. On-device A/B remains the real test.`);
