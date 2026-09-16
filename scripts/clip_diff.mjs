#!/usr/bin/env node
/**
 * Play two (clip, figure) pairs through the CLIENT'S OWN pipeline and diff what the bones do.
 *
 *     node scripts/clip_diff.mjs <clipA> <figA> <bonesA> <clipB> <figB> <bonesB>
 *
 * Everything else in this repo models the client in Python and compares files. That has now hidden
 * four separate defects in a row — a rotating ancestor nothing carried, translation channels dropped
 * wholesale, an armature's units, and a cache serving a clip from an older build — because a model of
 * the client is not the client. This runs the real thing: `figure-clip.js`'s own `retarget()`, a real
 * `AnimationMixer`, seeked exactly the way `tick()` seeks it (set `action.time`, then `update(0)`, so
 * the frame is a pure function of the shared clock rather than an accumulation of deltas).
 *
 * What it reports, per sampled time, is what a person actually complains about:
 *
 *   HIPS      how far the hips have MOVED from where they started, in metres. A figure that slides
 *             across the room shows up here and nowhere else.
 *   BODY      how far the body has TURNED from where it started. A figure swinging about her own axis
 *             shows up here.
 *   LIMBS     the worst limb direction disagreement between the two, in each figure's own body frame.
 *
 * The first two are per-figure and absolute, deliberately: the question "are these two doing the same
 * thing" is not the same as "is either of them doing anything sane", and the second one is what a
 * headset answers.
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
if (args.length < 6) {
  console.error("usage: node scripts/clip_diff.mjs <clipA> <figA> <bonesA> <clipB> <figB> <bonesB>");
  console.error("  bones: 'hips=DEF-spine,neck=DEF-spine.004,...' from the catalog's humanoid map");
  process.exit(2);
}

const load = (p) => new Promise((res, rej) => {
  const b = readFileSync(p);
  new GLTFLoader().parse(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength), "", res, rej);
});

// The bone names come from the CALLER, as `bone=node` pairs, because the catalog already holds each
// figure's humanoid map and guessing names from patterns is how you end up measuring the wrong bone on
// one rig and crashing on the next.
function bones(root, spec, label) {
  // SANITIZED both sides. GLTFLoader renames nodes on load the way `PropertyBinding` needs them —
  // Grace's rigify bones are `thigh.fk.L` in the file and `thigh_fk_L` in the scene — so comparing a
  // catalog name against a loaded name finds the rigs without dots and silently misses the ones with.
  const clean = (n) => THREE.PropertyBinding.sanitizeNodeName(n);
  const want = Object.fromEntries(spec.split(",").map((p) => {
    const [k, n] = p.split("=");
    return [k, clean(n)];
  }));
  const found = {};
  root.traverse((o) => {
    for (const [key, name] of Object.entries(want)) {
      if (!found[key] && clean(o.name || "") === name) found[key] = o;
    }
  });
  const missing = Object.keys(want).filter((k) => !found[k]);
  if (missing.length) {
    console.error(`  ! ${label}: no such bone — ${missing.join(", ")}`);
    process.exit(1);
  }
  return found;
}

async function rig(clipPath, figPath, spec) {
  const [clip, fig] = [await load(clipPath), await load(figPath)];
  const root = fig.scene;
  const bound = retarget(clip.animations[0], root);
  const mixer = new THREE.AnimationMixer(root);
  const action = mixer.clipAction(bound.clip);
  action.loop = THREE.LoopRepeat;
  action.play();
  return { root, mixer, action, bones: bones(root, spec, figPath.split('/').pop()), duration: clip.animations[0].duration,
           kept: bound.clip.tracks.length, of: clip.animations[0].tracks.length };
}

function seek(r, t) {                      // exactly what `tick()` does
  const d = r.action.getClip().duration || 1;
  r.action.time = ((t % d) + d) % d;
  r.mixer.update(0);
  r.root.updateMatrixWorld(true);
}

const V = () => new THREE.Vector3();
const pos = (o) => o.getWorldPosition(V());
const bodyFrame = (b) => {
  const up = pos(b.neck).sub(pos(b.hips)).normalize();
  const side = pos(b.lUpLeg).sub(pos(b.rUpLeg)).normalize();
  const fwd = V().crossVectors(side, up).normalize();
  return new THREE.Matrix4().makeBasis(V().crossVectors(up, fwd).normalize(), up, fwd);
};
const dirs = (b) => {
  const inv = bodyFrame(b).clone().invert();
  const out = {};
  for (const [a, c] of [["hips", "neck"], ["neck", "head"], ["lUpLeg", "lFoot"],
                        ["rUpLeg", "rFoot"], ["hips", "lHand"], ["hips", "rHand"]]) {
    if (b[a] && b[c]) out[`${a}->${c}`] = pos(b[c]).sub(pos(b[a])).normalize().applyMatrix4(inv);
  }
  return out;
};

const A = await rig(args[0], args[1], args[2]);
const B = await rig(args[3], args[4], args[5]);
console.log(`A ${args[0].split("/").pop()}  keeps ${A.kept}/${A.of} tracks`);
console.log(`B ${args[3].split("/").pop()}  keeps ${B.kept}/${B.of} tracks\n`);

seek(A, 0); seek(B, 0);
const a0 = pos(A.bones.hips), b0 = pos(B.bones.hips);
const fa0 = bodyFrame(A.bones).clone(), fb0 = bodyFrame(B.bones).clone();
const qa0 = new THREE.Quaternion().setFromRotationMatrix(fa0);
const qb0 = new THREE.Quaternion().setFromRotationMatrix(fb0);
const deg = (q, q0) => THREE.MathUtils.radToDeg(2 * Math.acos(Math.min(1, Math.abs(q.dot(q0)))));

console.log(`${"t".padStart(6)} ${"A hips moved".padStart(13)} ${"A turned".padStart(9)} |`
          + ` ${"B hips moved".padStart(13)} ${"B turned".padStart(9)} | worst limb`);
const n = 16;
for (let i = 0; i < n; i++) {
  const t = (A.duration * i) / n;
  seek(A, t); seek(B, t);
  const da = pos(A.bones.hips).sub(a0).length(), db = pos(B.bones.hips).sub(b0).length();
  const ra = deg(new THREE.Quaternion().setFromRotationMatrix(bodyFrame(A.bones)), qa0);
  const rb = deg(new THREE.Quaternion().setFromRotationMatrix(bodyFrame(B.bones)), qb0);
  const da2 = dirs(A.bones), db2 = dirs(B.bones);
  let worst = 0, which = "";
  for (const k of Object.keys(da2)) {
    if (!db2[k]) continue;
    const ang = THREE.MathUtils.radToDeg(Math.acos(Math.min(1, Math.max(-1, da2[k].dot(db2[k])))));
    if (ang > worst) { worst = ang; which = k; }
  }
  console.log(`${t.toFixed(1).padStart(6)} ${(da * 100).toFixed(1).padStart(11)}cm`
            + ` ${ra.toFixed(1).padStart(8)}° | ${(db * 100).toFixed(1).padStart(11)}cm`
            + ` ${rb.toFixed(1).padStart(8)}° | ${worst.toFixed(0).padStart(3)}° ${which}`);
}
