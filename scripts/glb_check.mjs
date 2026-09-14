#!/usr/bin/env node
/**
 * Load a GLB with the SAME loader the client uses and report what came out.
 *
 *     node scripts/glb_check.mjs temp/things/manager/office-babe.glb
 *     node scripts/glb_check.mjs temp/things/*\/*.glb
 *
 * `conjure/compose.py`'s verifier checks a composed file against the scene that described it, which is
 * the question "is this the right thing". This asks the other one: "will three.js load it at all, and
 * how big is it when it does". They fail differently — a file can satisfy every structural check and
 * still throw in GLTFLoader over an accessor written short, and a file can load perfectly while being
 * a hundred times the wrong size — so both are cheap and neither replaces the other.
 *
 * The bounding box is the useful number in practice: a figure is ~1.7 m, a banana ~0.2 m, a room ~10 m.
 * A 100× error is the single most common way a composed scene subtree comes out wrong, and it is
 * instantly visible here without opening anything.
 */
import { readFileSync } from "node:fs";

// three's texture path reaches for `self` and for a DOM image decoder, neither of which node has. The
// textures are not what this is checking — the structural verifier already compared every material
// against the registry — so the window is stubbed and images are left undecoded rather than skipping
// the load entirely, which would be the one thing worth knowing.
globalThis.self = globalThis.self || globalThis;
globalThis.createImageBitmap = globalThis.createImageBitmap || (async () => ({ width: 1, height: 1, close() {} }));

import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

const files = process.argv.slice(2);
if (!files.length) {
  console.error("usage: node scripts/glb_check.mjs <file.glb> [...]");
  process.exit(2);
}

let failed = 0;
for (const path of files) {
  const buf = readFileSync(path);
  const body = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  await new Promise((done) => {
    new GLTFLoader().parse(body, "", (gltf) => {
      let meshes = 0, skinned = 0, bones = 0, maps = 0, hidden = 0;
      const materials = new Set();
      gltf.scene.traverse((o) => {
        if (o.isSkinnedMesh) { skinned += 1; meshes += 1; } else if (o.isMesh) meshes += 1;
        if (o.isBone) bones += 1;
        for (const m of [].concat(o.material || [])) { materials.add(m.name); if (m.map) maps += 1; }
      });
      // `extras` is where a composed file records which parts the scene had switched off — the
      // wardrobe, and the reason `underwear` is in the file at all. A viewer shows them; the runtime
      // `figure-parts` component hides them by node name.
      const mark = (gltf.parser.json.extras || {}).conjure;
      if (mark) hidden = (mark.hidden || []).length;
      gltf.scene.updateMatrixWorld(true);
      const size = new THREE.Box3().setFromObject(gltf.scene).getSize(new THREE.Vector3());
      console.log(
        `${path.split("/").pop().padEnd(34)} ${String(meshes).padStart(3)} mesh` +
        `${skinned ? ` (${skinned} skinned, ${bones} bones)` : ""}` +
        `  ${materials.size} materials, ${maps} mapped${hidden ? `, ${hidden} hidden` : ""}` +
        `  ${size.x.toFixed(2)}×${size.y.toFixed(2)}×${size.z.toFixed(2)} m`
      );
      done();
    }, (err) => {
      failed += 1;
      console.log(`${path}: FAILED TO LOAD — ${err}`);
      done();
    });
  });
}
process.exit(failed ? 1 : 0);
