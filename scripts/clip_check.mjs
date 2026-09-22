#!/usr/bin/env node
/**
 * Will this clip actually DRIVE this figure? Loads both with the client's own loader and reports how
 * many of the clip's tracks resolve to a bone on the model.
 *
 *     node scripts/clip_check.mjs <clip.glb> <figure.glb>
 *
 * `glb_check.mjs` answers "will three.js load this at all". This asks the question that matters for a
 * RETARGETED clip, and it is a different one: a rewritten clip can load perfectly and still bind
 * nothing, because binding is by node NAME and the whole point of the rewrite is to change those names
 * to the target's. A clip that binds 0 of 22 plays a figure standing perfectly still while every log
 * line says success — which is exactly the failure this pipeline keeps rediscovering in other forms.
 *
 * It then runs the CLIENT'S OWN `retarget()` over the result and reports what that drops, which is a
 * second question again: the client has rules of its own — nothing may write the mixer root's
 * transform, a constant scale track is discarded, a moving position track is re-based — and a clip can
 * bind every track and still lose half of them there. That is how the missing translation channels were
 * found: a retargeted clip kept 21 of 21 tracks while the native one kept 204 of 312, and the 101
 * re-based position tracks in the difference were motion nothing was carrying across.
 */
import { readFileSync } from "node:fs";

globalThis.self = globalThis.self || globalThis;
globalThis.createImageBitmap = globalThis.createImageBitmap || (async () => ({ width: 1, height: 1, close() {} }));

import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

// `figure-clip.js` is a plain browser script that registers itself against AFRAME. Stub just enough of
// that for it to load, so the rules under test are the ones that actually ship.
globalThis.THREE = THREE;
const _components = {};
globalThis.window = { AFRAME: { components: _components, registerComponent: (n, d) => { _components[n] = d; } },
                      addEventListener() {}, removeEventListener() {} };
globalThis.AFRAME = globalThis.window.AFRAME;
const { retarget } = await import("../client/figure-clip.js");

const [clipPath, figPath] = process.argv.slice(2);
if (!figPath) {
  console.error("usage: node scripts/clip_check.mjs <clip.glb> <figure.glb>");
  process.exit(2);
}

const load = (path) => new Promise((res, rej) => {
  const buf = readFileSync(path);
  new GLTFLoader().parse(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength), "", res, rej);
});

const [clip, fig] = [await load(clipPath), await load(figPath)];
const have = new Set();
fig.scene.traverse((o) => { if (o.name) have.add(THREE.PropertyBinding.sanitizeNodeName(o.name)); });

const anim = (clip.animations || [])[0];
if (!anim) {
  console.log(`${clipPath}: no animation in this file`);
  process.exit(1);
}
const miss = [];
let hit = 0;
for (const track of anim.tracks) {
  const node = THREE.PropertyBinding.parseTrackName(track.name).nodeName;
  if (have.has(node)) hit++;
  else miss.push(node);
}
const name = clipPath.split("/").pop();
console.log(`${name.padEnd(16)} "${anim.name}" ${anim.duration.toFixed(1)}s  ${anim.tracks.length} track(s), `
  + `${hit} BIND` + (miss.length ? `, ${miss.length} miss (${miss.slice(0, 3).join(", ")})` : ""));

const kept = retarget(anim, fig.scene);
const why = Object.entries(kept.why).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ");
console.log(`${"".padEnd(16)} the client keeps ${kept.clip.tracks.length} of ${anim.tracks.length}`
  + (why ? ` — dropped ${why}` : ""));
process.exit(hit ? 0 : 1);
