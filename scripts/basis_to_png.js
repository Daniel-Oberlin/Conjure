#!/usr/bin/env node
'use strict';

/**
 * Decode the `.basis` textures in a captured build to PNGs beside them.
 *
 *     node scripts/basis_to_png.js temp/vrh/akari
 *     node scripts/basis_to_png.js <capture> --transcoder <dir-with-basis.wasm.js>
 *     node scripts/basis_to_png.js <capture> --dry-run
 *
 * PlayCanvas transcodes textures to Basis Universal and its engine asks for
 * that in preference to the original, so a capture made by browsing holds
 * `.basis` files and never the PNGs beside them — 91 of 105 textures in the
 * build this was written against. On a session-gated origin the original is
 * unreachable without the page's own credentials, which makes the compressed
 * form the ONLY obtainable version of those textures.
 *
 * A pre-pass rather than a step inside the rebuild, and deliberately so. It
 * writes `Foo.png` next to `Foo.basis`, which is the exact name the registry
 * already gives that texture — so `conjure/playcanvas.py` finds it with no
 * knowledge of Basis at all, the result is an ordinary PNG anyone can open and
 * check, and re-running is free because existing files are skipped.
 *
 * **The transcoder is found, not required.** A PlayCanvas build ships its own
 * (`basis.wasm.js` + `basis.wasm.wasm`) and that copy is preferred when
 * present, because a decoder shipped beside the data is the one certain to
 * read it. Failing that there is a vendored upstream build in `vendor/basis`,
 * so a capture that missed the site's — which is easy to do, and happened —
 * still decodes. `--transcoder` overrides both.
 */

const fs = require('fs');
const path = require('path');
const zlib = require('zlib');

/** The upstream build committed under `vendor/basis`, used when a capture has none. */
const VENDORED = path.join(__dirname, '..', 'vendor', 'basis', 'basis_transcoder.js');

// ---------------------------------------------------------------- PNG, by hand
//
// Node ships zlib and no image encoder. A true-colour-with-alpha PNG is a
// header, one deflate stream of unfiltered scanlines, and an end marker, which
// is cheaper to write than it is to justify adding a dependency for.

const CRC_TABLE = (() => {
  const table = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  return table;
})();

function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, 'ascii');
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, crc]);
}

/** An 8-bit RGBA PNG from tightly packed `rgba` of `width * height * 4` bytes. */
function encodePng(width, height, rgba) {
  const stride = width * 4;
  const raw = Buffer.alloc((stride + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (stride + 1)] = 0;                       // filter type 0: none
    Buffer.from(rgba.buffer, rgba.byteOffset + y * stride, stride)
      .copy(raw, y * (stride + 1) + 1);
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8;                                        // bit depth
  ihdr[9] = 6;                                        // colour type: truecolour + alpha
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', zlib.deflateSync(raw, { level: 9 })),
    chunk('IEND', Buffer.alloc(0))
  ]);
}

// ---------------------------------------------------------------- the transcoder

/** Every file under `root` whose name matches, cheaply and depth-limited. */
function findFiles(root, pattern, depth = 10) {
  const out = [];
  const walk = (dir, left) => {
    if (left < 0) return;
    let entries = [];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch (_) {
      return;
    }
    for (const entry of entries) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full, left - 1);
      else if (pattern.test(entry.name)) out.push(full);
    }
  };
  walk(root, depth);
  return out.sort();
}

/**
 * The transcoder's JS glue under `root`.
 *
 * Its `.wasm` is found separately and never assumed to be beside it: PlayCanvas
 * stores every file as its own numbered asset, so the pair lands in two
 * unrelated directories — `files/assets/191644736/1/basis.wasm.js` next to
 * `files/assets/191644734/1/basis.wasm.wasm`.
 */
function findTranscoder(root) {
  return findFiles(root, /^basis.*\.js$/i)[0] || '';
}

/**
 * The `.wasm` for a given glue file, searched outward from the glue and then
 * from the capture. Outward from the GLUE first because `--transcoder` may
 * point at another capture of the same site — which is exactly what you do
 * when the build you are decoding skipped its `wasm` assets.
 */
function findWasm(glue, root) {
  const beside = glue.replace(/\.js$/i, '.wasm');
  if (fs.existsSync(beside)) return beside;
  const roots = [];
  let dir = path.dirname(path.resolve(glue));
  for (let up = 0; up < 5; up++) {
    roots.push(dir);
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  if (root) roots.push(path.resolve(root));
  for (const candidate of roots) {
    const hit = findFiles(candidate, /^basis.*\.wasm$/i, 6)[0];
    if (hit) return hit;
  }
  return '';
}

async function loadTranscoder(glue, root) {
  const wasmPath = findWasm(glue, root);
  if (!wasmPath) throw new Error(`found ${glue} but no basis .wasm anywhere under the capture`);
  const factory = require(path.resolve(glue));
  // `wasmBinary` hands Emscripten the bytes outright, so it never tries to
  // locate the file itself — which is where a browser build trips in Node.
  const Module = await factory({ wasmBinary: fs.readFileSync(wasmPath) });
  Module.initializeBasis();
  return Module;
}

/**
 * Every texture a build uses as a NORMAL map, by the path the registry records.
 *
 * Guessing this from pixels does not work. Basis packs a normal map's X into
 * RGB and Y into alpha, which decodes to a greyscale image with an independent
 * alpha — and a genuine greyscale mask with an alpha channel looks exactly the
 * same. One build has both, and a heuristic wrecked its specular maps while
 * fixing its normals. The registry simply says which is which.
 */
function normalMapPaths(root) {
  var out = new Set();
  for (const config of findFiles(root, /^config\.json$/i, 10)) {
    var registry;
    try {
      registry = JSON.parse(fs.readFileSync(config, 'utf8')).assets;
    } catch (_) {
      continue;
    }
    if (!registry) continue;
    var base = path.dirname(config);
    var wanted = new Set();
    for (const asset of Object.values(registry)) {
      var data = asset && asset.data;
      if (data && typeof data.normalMap === 'number') wanted.add(String(data.normalMap));
      if (data && typeof data.clearCoatNormalMap === 'number') {
        wanted.add(String(data.clearCoatNormalMap));
      }
    }
    for (const [id, asset] of Object.entries(registry)) {
      if (!wanted.has(String(id)) || !asset.file) continue;
      // Both forms: the registry names the original and we decode the variant.
      for (const url of [asset.file.url].concat(
        Object.values(asset.file.variants || {}).map((v) => v.url))) {
        if (url) out.add(path.resolve(base, decodeURIComponent(url)));
      }
    }
  }
  return out;
}

/**
 * Is this RGBA a normal map in Basis's packed form — X in RGB, Y in alpha?
 *
 * The encoder has a mode for normal maps that stores the two meaningful
 * channels this way, because a tangent-space normal's Z is recoverable and
 * compressing it wastes bits. Transcoded to RGBA32 the result LOOKS like a
 * greyscale image with an unrelated alpha, and handed to glTF's normalTexture
 * as-is it is worse than useless: grey means (0,0,0) after the usual remap,
 * a zero-length normal, and the lighting breaks out in dark blotches over
 * every surface that uses it.
 *
 * Detected rather than declared, because the flag does not survive into the
 * file: an honest colour texture is not grey in RGB, and a genuinely grey one
 * rarely carries an alpha that disagrees with it.
 */
function looksPackedNormal(rgba) {
  var pixels = rgba.length / 4;
  var step = Math.max(1, Math.floor(pixels / 4096));
  var checked = 0;
  var grey = 0;
  var differs = 0;
  for (var i = 0; i < pixels; i += step) {
    var o = i * 4;
    checked += 1;
    if (rgba[o] === rgba[o + 1] && rgba[o + 1] === rgba[o + 2]) grey += 1;
    if (Math.abs(rgba[o + 3] - rgba[o]) > 8) differs += 1;
  }
  return checked > 64 && grey / checked > 0.98 && differs / checked > 0.2;
}

/** Rebuild (x, y, z) into RGB from the packed form, and drop the alpha. */
function unpackNormal(rgba) {
  for (var i = 0; i < rgba.length; i += 4) {
    var x = rgba[i] / 127.5 - 1;
    var y = rgba[i + 3] / 127.5 - 1;
    var z = Math.sqrt(Math.max(0, 1 - x * x - y * y));
    rgba[i] = Math.round((x * 0.5 + 0.5) * 255);
    rgba[i + 1] = Math.round((y * 0.5 + 0.5) * 255);
    rgba[i + 2] = Math.round((z * 0.5 + 0.5) * 255);
    rgba[i + 3] = 255;
  }
}

/** `{width, height, rgba}` for image 0, mip 0 — the full-size level. */
function transcode(Module, bytes, isNormal) {
  // Read the format off the module rather than hardcoding it: the enum's
  // numbering has moved between Basis releases, and the capture chooses the
  // version.
  const format = Module.transcoder_texture_format.cTFRGBA32.value;
  const file = new Module.BasisFile(new Uint8Array(bytes));
  try {
    if (!file.getNumImages()) throw new Error('no images');
    const width = file.getImageWidth(0, 0);
    const height = file.getImageHeight(0, 0);
    if (!file.startTranscoding()) throw new Error('startTranscoding refused it');
    const size = file.getImageTranscodedSizeInBytes(0, 0, format);
    const rgba = new Uint8Array(size);
    if (!file.transcodeImage(rgba, 0, 0, format, 0, 0)) throw new Error('transcodeImage failed');
    if (size !== width * height * 4) {
      throw new Error(`expected ${width * height * 4} bytes of RGBA, got ${size}`);
    }
    // The registry says it is a normal map AND the pixels agree it is packed.
    // Both, because a normal map may also be stored plainly.
    var packed = !!isNormal && looksPackedNormal(rgba);
    if (packed) unpackNormal(rgba);
    return { width, height, rgba, packedNormal: packed };
  } finally {
    file.close();
    file.delete();
  }
}

// ---------------------------------------------------------------- the pass

function findBasis(root) {
  return findFiles(root, /\.basis$/i, 12);
}

async function main() {
  const args = process.argv.slice(2);
  const flag = (name) => {
    const i = args.indexOf(name);
    return i >= 0 ? args[i + 1] : '';
  };
  const root = args.find((a) => !a.startsWith('--') && args[args.indexOf(a) - 1] !== '--transcoder');
  if (!root || !fs.existsSync(root)) {
    console.error('usage: node scripts/basis_to_png.js <capture-dir> [--transcoder <basis.wasm.js>]'
      + ' [--force] [--dry-run]');
    return 2;
  }
  const force = args.includes('--force');
  const dryRun = args.includes('--dry-run');

  const files = findBasis(root);
  if (!files.length) {
    console.log(`no .basis files under ${root}`);
    return 0;
  }
  // Skipped only when the PNG beside it has BYTES. A failed download leaves a
  // zero-length file behind, and treating that as "already decoded" is how ten
  // textures stayed empty while a perfectly good `.basis` sat next to each one —
  // the model rebuilt with no textures at all and nothing said why.
  const decoded = (f) => {
    try {
      return fs.statSync(f.replace(/\.basis$/i, '.png')).size > 0;
    } catch (_) {
      return false;
    }
  };
  const todo = files.filter((f) => force || !decoded(f));
  console.log(`${files.length} .basis file(s), ${files.length - todo.length} already decoded`);
  if (!todo.length || dryRun) {
    for (const f of todo) console.log(`  would decode ${path.relative(root, f)}`);
    return 0;
  }

  let glue = flag('--transcoder');
  if (glue && fs.existsSync(glue) && fs.statSync(glue).isDirectory()) glue = findTranscoder(glue);
  let source = 'given';
  if (!glue) {
    glue = findTranscoder(root);
    source = 'from the capture';
  }
  if (!glue) {
    glue = VENDORED;
    source = 'vendored';
  }
  if (!glue || !fs.existsSync(glue)) {
    console.error('no Basis transcoder: none in the capture, none vendored, none given.\n'
      + 'Point at one with --transcoder — a PlayCanvas build ships it as a `wasm` asset.');
    return 2;
  }
  console.log(`transcoder: ${glue} (${source})`);
  const Module = await loadTranscoder(glue, root);
  const normals = normalMapPaths(root);
  if (normals.size) console.log(`${normals.size} texture(s) are used as normal maps`);

  let ok = 0;
  const failures = [];
  for (const file of todo) {
    const dest = file.replace(/\.basis$/i, '.png');
    try {
      const { width, height, rgba, packedNormal } =
        transcode(Module, fs.readFileSync(file), normals.has(path.resolve(file)));
      fs.writeFileSync(dest, encodePng(width, height, rgba));
      ok += 1;
      console.log(`  ${path.relative(root, dest)}  ${width}x${height}`
        + (packedNormal ? '  [packed normal map, X/Y unpacked]' : ''));
    } catch (err) {
      failures.push(`${path.relative(root, file)}: ${err.message}`);
    }
  }
  console.log(`\n${ok} decoded, ${failures.length} failed`);
  for (const f of failures) console.log(`  ! ${f}`);
  return failures.length && !ok ? 1 : 0;
}

if (require.main === module) {
  main().then((code) => process.exit(code), (err) => {
    console.error(err && err.stack ? err.stack : err);
    process.exit(1);
  });
}

module.exports = { encodePng, transcode, loadTranscoder, findBasis, findTranscoder, findWasm,
  normalMapPaths, looksPackedNormal, VENDORED };
