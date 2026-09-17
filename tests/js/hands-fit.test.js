// Unit tests for the hand-fit overlay's numeric half (client/hands-fit.js), run with `node --test`.
//
// The drawing half needs a headset and is untestable here. The ARITHMETIC is not, and it is the part that
// produces a verdict a human will act on — "the runtime is serving a static hand model" is a claim about
// 24 numbers, and getting the statistic wrong would produce a confident wrong answer rather than no
// answer. So the thresholds and the two probes are pinned here.
//
// The load-bearing test is the last one: our own two hand models are NOT a mirrored pair, and that is
// encoded as an assertion because it is the premise the ratio probe rests on. If a re-import ever makes
// them mirrors, this test fails and the comment in BIND (and the caveat in the spec) must be re-measured
// rather than quietly inherited.
const { test } = require("node:test");
const assert = require("node:assert");

global.window = global.window || {};
require("../../client/hands-fit.js");
const H = global.window.HandsFit;

/** A synthetic hand: every joint on the +X axis, `step` metres apart down each chain from the wrist.
 *  Joints from different fingers coincide, which is fine — only the 24 PAIRS are ever measured. */
function ladder(step) {
  const pos = { wrist: { x: 0, y: 0, z: 0 } };
  H.CHAINS.forEach((chain) => {
    for (let i = 1; i < chain.length; i++) pos[chain[i]] = { x: i * step, y: 0, z: 0 };
  });
  return pos;
}

/** Index of the bone ENDING at `child`, so a test never has to count SEGMENTS by hand. */
function bone(child) {
  const i = H.SEGMENTS.findIndex((s) => s[1] === child);
  assert.ok(i >= 0, `no bone ends at ${child}`);
  return i;
}

/** A sampler accumulator as the component builds it, filled from a list of length-arrays. */
function accumulate(frames) {
  const n = H.SEGMENTS.length;
  const acc = { frames: 0, n: new Array(n).fill(0), sum: new Array(n).fill(0),
                min: new Array(n).fill(Infinity), max: new Array(n).fill(-Infinity) };
  frames.forEach((L) => {
    acc.frames++;
    for (let i = 0; i < n; i++) {
      if (L[i] == null) continue;
      acc.n[i]++; acc.sum[i] += L[i];
      acc.min[i] = Math.min(acc.min[i], L[i]);
      acc.max[i] = Math.max(acc.max[i], L[i]);
    }
  });
  return acc;
}

test("the joint set and the bone set are the WebXR ones, and the bind tables match", () => {
  assert.equal(H.JOINTS.length, 25);
  assert.equal(H.SEGMENTS.length, 24);
  assert.equal(H.BIND.left.length, 24);
  assert.equal(H.BIND.right.length, 24);
  for (const side of ["left", "right"]) {
    H.BIND[side].forEach((v, i) => {
      assert.ok(Number.isFinite(v) && v > 0.005 && v < 0.08,
        `${side}[${i}] = ${v} is not a plausible bone length in metres`);
    });
  }
  // Every joint except the wrist is some bone's child, exactly once; the wrist is the parent of five.
  const children = H.SEGMENTS.map((s) => s[1]);
  assert.equal(new Set(children).size, 24);
  assert.deepEqual(new Set([...children, "wrist"]), new Set(H.JOINTS));
  assert.equal(H.SEGMENTS.filter((s) => s[0] === "wrist").length, 5);
});

test("segmentLengths measures each bone, and reports a missing joint rather than guessing", () => {
  const L = H.segmentLengths(ladder(0.02));
  assert.equal(L.length, 24);
  L.forEach((v) => assert.ok(Math.abs(v - 0.02) < 1e-12));

  const holed = ladder(0.02);
  delete holed["index-finger-phalanx-intermediate"];
  const L2 = H.segmentLengths(holed);
  // The missing joint is the child of one bone and the parent of the next, so it costs exactly two.
  assert.equal(L2.filter((v) => v === null).length, 2);
  assert.equal(H.ratioStats(L2, H.BIND.left).n, 22);
});

test("a uniform scale of our model reads as UNIFORM, and the median IS the scale", () => {
  const scaled = H.BIND.left.map((v) => v * 1.0734);
  const r = H.ratioStats(scaled, H.BIND.left);
  assert.equal(r.n, 24);
  assert.ok(Math.abs(r.median - 1.0734) < 1e-9);
  assert.ok(r.cv < 1e-9, `cv ${r.cv} should be zero for a uniform scale`);
  assert.match(H.ratioVerdict(r), /^UNIFORM/);
  assert.match(H.ratioVerdict(r), /1\.0734/);
});

test("a hand of different PROPORTIONS does not read as uniform, whatever its size", () => {
  // Fingers 12% longer, thumb 4% shorter — a real difference in shape, not in scale.
  const odd = H.BIND.left.map((v, i) => v * (i < 4 ? 0.96 : 1.12));
  const r = H.ratioStats(odd, H.BIND.left);
  assert.ok(r.cv > 0.03, `cv ${r.cv} should exceed the CLOSE threshold`);
  assert.match(H.ratioVerdict(r), /^DIFFERENT HAND/);
  // And the verdict is monotone: scattering more never reads as more uniform.
  const worse = H.BIND.left.map((v, i) => v * (1 + 0.3 * Math.sin(i)));
  assert.ok(H.ratioStats(worse, H.BIND.left).cv > r.cv);
});

test("the premise-free probe: constant lengths mean a POSED skeleton, moving ones do not", () => {
  const still = accumulate([0, 1, 2, 3].map(() => H.BIND.left.slice()));
  const j = H.jitterStats(still);
  assert.equal(j.bones, 24);
  assert.equal(j.max, 0);
  assert.match(H.jitterVerdict(j), /^RIGID/);

  const wander = bone("index-finger-phalanx-proximal");
  const moving = [H.BIND.left.slice(), H.BIND.left.slice()];
  moving[1][wander] = moving[1][wander] * 1.05;                // index proximal wanders 5%
  const j2 = H.jitterStats(accumulate(moving));
  assert.ok(j2.max > 0.02);
  assert.equal(j2.name, "index-finger-phalanx-proximal");      // it names the bone, not just the number
  assert.match(H.jitterVerdict(j2), /^NOT RIGID/);

  // Float noise is not re-estimation: one part in ten thousand still reads as a posed skeleton.
  const noisy = [H.BIND.left.slice(), H.BIND.left.map((v) => v * 1.0001)];
  assert.match(H.jitterVerdict(H.jitterStats(accumulate(noisy))), /^RIGID/);
});

test("the verdict claims RIGIDITY and not measurement — the distinction it got wrong first", () => {
  // Measured on a Quest 3: ~2.5%. The sound inference is that joints are positioned independently,
  // because posing a stored skeleton cannot change a bone's LENGTH however noisy the pose. That is not
  // the same as "this is your hand", and the wording must not claim it.
  const real = H.jitterStats(accumulate([H.BIND.left.slice(), H.BIND.left.map((v, i) => v * (1 + (i % 3) * 0.025))]));
  const said = H.jitterVerdict(real);
  assert.match(said, /positioned independently/);
  assert.match(said, /says nothing yet about whose hand/);
  assert.doesNotMatch(said, /from the image/, "that was an inference the number does not license");
});

test("jitterStats and ratioStats say NOTHING rather than something wrong when there is no data", () => {
  const empty = accumulate([]);
  assert.equal(H.jitterStats(empty), null);
  assert.match(H.jitterVerdict(null), /no bones measured/);
  assert.equal(H.ratioStats(new Array(24).fill(null), H.BIND.left), null);
  assert.match(H.ratioVerdict(null), /no bind length/);
  assert.equal(H.compareHands(new Array(24).fill(null), H.BIND.left), null);
});

test("?hands= parses to a mode, and anything unrecognised is off rather than a guess", () => {
  const at = (search) => { global.location = { search }; return H.resolveMode(); };
  assert.equal(at(""), "off");
  assert.equal(at("?hands=fit"), "fit");
  assert.equal(at("?hands=JOINTS"), "joints");
  assert.equal(at("?hands=axes"), "axes");
  assert.equal(at("?hands=1"), "fit");
  assert.equal(at("?hands=true"), "fit");
  assert.equal(at("?hands=on"), "fit");
  assert.equal(at("?hands=wear"), "off");
  assert.equal(at("?occlusion=hands"), "off");
});

test("a table compared with itself is a mirror; our OWN hand pair is not", () => {
  const same = H.compareHands(H.BIND.left, H.BIND.left);
  assert.equal(same.maxAbs, 0);
  assert.equal(same.rms, 0);

  // Measured 2026-09-17 from b8f676bea0758536 (L) and f586068580143caa (R). The pair agrees closely on
  // middle, ring and pinky and disagrees on the INDEX by more than 6 mm — so they cannot both be the
  // canonical WebXR skeleton, which is why the ratio probe carries a premise and the jitter probe does not.
  const pair = H.compareHands(H.BIND.left, H.BIND.right);
  assert.equal(pair.bones, 24);
  assert.equal(pair.name, "index-finger-metacarpal");
  assert.ok(pair.maxAbs > 0.006, `max |Δ| was ${pair.maxAbs} m; re-measure BIND and the spec's caveat`);
  assert.ok(pair.rms > 0.001);

  // ...and the agreement on the other three fingers is what makes the index disagreement notable.
  for (const child of ["middle-finger-metacarpal", "ring-finger-metacarpal", "pinky-finger-metacarpal"]) {
    const i = bone(child);
    assert.ok(Math.abs(H.BIND.left[i] - H.BIND.right[i]) < 0.0005, `${child} should agree to 0.5 mm`);
  }
});

test("two windows per acquisition, so convergence is not mistaken for steady state", () => {
  // The first reading conflated them: ~2.5% jitter, sampled over the 30 frames immediately after
  // acquisition, which is exactly when an estimate is still settling. A non-zero figure there already
  // rules out a stored skeleton — a stored one cannot converge — but its MAGNITUDE said nothing.
  assert.ok(H.SAMPLES > 0 && H.SETTLE > 0, "both windows must exist");
  // fresh = 1..SAMPLES, a gap of SETTLE, settled = SAMPLES+SETTLE+1 .. SAMPLES+SETTLE+SAMPLES
  const total = H.SAMPLES + H.SETTLE + H.SAMPLES;
  const which = (frame) => frame <= H.SAMPLES ? "fresh"
                         : frame > H.SAMPLES + H.SETTLE ? "settled" : "gap";
  assert.equal(which(1), "fresh");
  assert.equal(which(H.SAMPLES), "fresh");
  assert.equal(which(H.SAMPLES + 1), "gap");
  assert.equal(which(H.SAMPLES + H.SETTLE), "gap");
  assert.equal(which(H.SAMPLES + H.SETTLE + 1), "settled");
  assert.equal(which(total), "settled");
  // ~1.25 s at 72 Hz: long enough to settle, short enough that nobody holds a pose waiting for it.
  assert.ok(total / 72 < 2, `${total} frames is ${(total / 72).toFixed(2)} s — too long to hold still`);
});
