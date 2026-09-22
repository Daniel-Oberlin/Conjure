#!/usr/bin/env node
/**
 * How far from UPRIGHT is each figure, in WORLD space, playing the same clip?
 *
 *     node scripts/clip_tilt.mjs <clipA> <figA> <bonesA> [<clipB> <figB> <bonesB> ...]
 *
 * `clip_diff.mjs` answers "are these two doing the same thing over time", and it does that by
 * reporting every angle RELATIVE to each figure's own t=0. That makes a CONSTANT lean invisible to it:
 * a figure who lies down 12° further back than the others turns by the same amount as they do for the
 * whole clip and reads clean. The device report was a constant lean — "Akari is tilted back compared to
 * Grace and Alice" — so this measures the absolute thing instead.
 *
 * Per figure:
 *   REST   the body's up-axis against world +Y with NOTHING playing — the armature's own lean.
 *   t=0    the same axis on the clip's first frame. For a clip that lays a figure on her back this is
 *          near 90°, and the question is whether every figure agrees.
 *   range  min/max across the clip, so a lean can be told apart from a wobble.
 *
 * Takes any number of triples so the comparison is one run and one table.
 */
import { readFileSync } from "node:fs";

globalThis.self = globalThis.self || globalThis;
globalThis.createImageBitmap = globalThis.createImageBitmap || (async () => ({ width: 1, height: 1, close() {} }));

import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

globalThis.THREE = THREE;
const _c = {};
globalThis.window = { AFRAME: { components: _c, registerComponent: (n, d) => { _c[n] = d; } },
                      addEventListener() {}, removeEventListener() {} };
globalThis.AFRAME = globalThis.window.AFRAME;
const { retarget } = await import("../client/figure-clip.js");

const args = process.argv.slice(2);
if (args.length < 3 || args.length % 3) {
  console.error("usage: node scripts/clip_tilt.mjs <clip> <figure> <bones> [<clip> <figure> <bones> ...]");
  process.exit(2);
}

const load = (p) => new Promise((res, rej) => {
  const b = readFileSync(p);
  new GLTFLoader().parse(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength), "", res, rej);
});

function bones(root, spec, label) {
  const clean = (n) => THREE.PropertyBinding.sanitizeNodeName(n);
  const want = Object.fromEntries(spec.split(",").map((p) => { const [k, n] = p.split("="); return [k, clean(n)]; }));
  const found = {};
  root.traverse((o) => {
    for (const [key, name] of Object.entries(want)) if (!found[key] && clean(o.name || "") === name) found[key] = o;
  });
  const missing = Object.keys(want).filter((k) => !found[k]);
  if (missing.length) { console.error(`  ! ${label}: no such bone — ${missing.join(", ")}`); process.exit(1); }
  return found;
}

const V = () => new THREE.Vector3();
const pos = (o) => o.getWorldPosition(V());
// The body's own up, in WORLD space and deliberately not in its body frame: hips -> neck is the axis a
// person reads as "standing" or "lying", and it is the one the device report was about.
const upAxis = (b) => pos(b.neck).sub(pos(b.hips)).normalize();
const fromVertical = (v) => THREE.MathUtils.radToDeg(Math.acos(Math.min(1, Math.max(-1, v.y))));

console.log(`${"figure".padEnd(22)} ${"REST".padStart(7)} ${"t=0".padStart(7)} `
          + `${"min".padStart(7)} ${"max".padStart(7)}  tracks`);
for (let i = 0; i < args.length; i += 3) {
  const [clipPath, figPath, spec] = args.slice(i, i + 3);
  const [clipGltf, fig] = [await load(clipPath), await load(figPath)];
  const root = fig.scene;
  const label = figPath.split("/").pop().replace(/\.glb$/, "");
  const b = bones(root, spec, label);

  root.updateMatrixWorld(true);
  const rest = fromVertical(upAxis(b));

  const bound = retarget(clipGltf.animations[0], root);
  const mixer = new THREE.AnimationMixer(root);
  const action = mixer.clipAction(bound.clip);
  action.loop = THREE.LoopRepeat;
  action.play();
  const dur = clipGltf.animations[0].duration || 1;
  const seek = (t) => { action.time = ((t % dur) + dur) % dur; mixer.update(0); root.updateMatrixWorld(true); };

  seek(0);
  const at0 = fromVertical(upAxis(b));
  let lo = Infinity, hi = -Infinity;
  for (let k = 0; k < 64; k++) { seek((dur * k) / 64); const a = fromVertical(upAxis(b)); lo = Math.min(lo, a); hi = Math.max(hi, a); }
  console.log(`${label.padEnd(22)} ${rest.toFixed(1).padStart(6)}° ${at0.toFixed(1).padStart(6)}° `
            + `${lo.toFixed(1).padStart(6)}° ${hi.toFixed(1).padStart(6)}°  `
            + `${bound.clip.tracks.length}/${clipGltf.animations[0].tracks.length}`);
}
