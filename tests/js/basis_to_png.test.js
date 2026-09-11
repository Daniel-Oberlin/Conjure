// Decoding Basis Universal textures back to PNG (scripts/basis_to_png.js).
//
// The pre-pass exists because a browsing capture of a PlayCanvas build holds
// `.basis` and never the PNG beside it — the engine asks for the compressed
// variant, and on a session-gated origin the original is unreachable without
// the page's own credentials. Writing `Foo.png` next to `Foo.basis` is the
// whole trick: that is the name the registry already gives the texture, so the
// Python rebuild finds it knowing nothing about Basis.
//
// The transcoder is committed under `vendor/basis`, so nothing here depends on
// a capture having carried one. The PNG encoder and the file-finding are pure.

const { test } = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const zlib = require('zlib');

const { encodePng, findBasis, findWasm, findTranscoder,
  VENDORED } = require('../../scripts/basis_to_png.js');

function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'basis-test-'));
}

test('an encoded PNG is a real one, and the pixels survive', () => {
  const width = 3;
  const height = 2;
  const rgba = new Uint8Array(width * height * 4);
  for (let i = 0; i < rgba.length; i++) rgba[i] = (i * 7) & 0xff;
  const png = encodePng(width, height, rgba);

  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  assert.equal(png.subarray(12, 16).toString('ascii'), 'IHDR');
  assert.equal(png.readUInt32BE(16), width);
  assert.equal(png.readUInt32BE(20), height);
  assert.equal(png[24], 8, 'bit depth');
  assert.equal(png[25], 6, 'truecolour with alpha, because Basis textures carry one');

  // Walk the chunks, inflate IDAT, and strip the per-scanline filter byte. If
  // the stride or the filter byte is wrong the image still "opens" and comes
  // out sheared, which is exactly the bug a signature check would miss.
  let offset = 8;
  let idat = null;
  while (offset < png.length) {
    const length = png.readUInt32BE(offset);
    const type = png.subarray(offset + 4, offset + 8).toString('ascii');
    if (type === 'IDAT') idat = png.subarray(offset + 8, offset + 8 + length);
    offset += 12 + length;
  }
  assert.ok(idat, 'must have an IDAT');
  const raw = zlib.inflateSync(idat);
  assert.equal(raw.length, (width * 4 + 1) * height);
  const back = [];
  for (let y = 0; y < height; y++) {
    assert.equal(raw[y * (width * 4 + 1)], 0, 'filter type none');
    back.push(...raw.subarray(y * (width * 4 + 1) + 1, (y + 1) * (width * 4 + 1)));
  }
  assert.deepEqual(back, [...rgba]);
});

test('every chunk carries a CRC the format agrees with', () => {
  const png = encodePng(1, 1, new Uint8Array([1, 2, 3, 4]));
  const table = [];
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c;
  }
  const crc = (buf) => {
    let c = -1;
    for (const byte of buf) c = table[(c ^ byte) & 0xff] ^ (c >>> 8);
    return (c ^ -1) >>> 0;
  };
  let offset = 8;
  let chunks = 0;
  while (offset < png.length) {
    const length = png.readUInt32BE(offset);
    const body = png.subarray(offset + 4, offset + 8 + length);
    assert.equal(png.readUInt32BE(offset + 8 + length), crc(body),
      `chunk ${png.subarray(offset + 4, offset + 8)} has a bad CRC`);
    chunks += 1;
    offset += 12 + length;
  }
  assert.equal(chunks, 3, 'IHDR, IDAT, IEND');
});

test('the wasm is found when it is NOT beside its glue', () => {
  // PlayCanvas stores every file as its own numbered asset, so the transcoder
  // arrives as two halves in unrelated directories — `.../191644736/1/
  // basis.wasm.js` against `.../191644734/1/basis.wasm.wasm`. Assuming they
  // sit together is the obvious thing and it is wrong for every capture.
  const root = tmpdir();
  const glueDir = path.join(root, 'files', 'assets', '191644736', '1');
  const wasmDir = path.join(root, 'files', 'assets', '191644734', '1');
  fs.mkdirSync(glueDir, { recursive: true });
  fs.mkdirSync(wasmDir, { recursive: true });
  const glue = path.join(glueDir, 'basis.wasm.js');
  fs.writeFileSync(glue, '// glue');
  fs.writeFileSync(path.join(wasmDir, 'basis.wasm.wasm'), 'not really wasm');

  assert.equal(findTranscoder(root), glue);
  assert.equal(findWasm(glue, root), path.join(wasmDir, 'basis.wasm.wasm'));
});

test('a wasm beside the glue still wins', () => {
  const root = tmpdir();
  fs.writeFileSync(path.join(root, 'basis.js'), '// glue');
  fs.writeFileSync(path.join(root, 'basis.wasm'), 'not really wasm');
  assert.equal(findWasm(path.join(root, 'basis.js'), root), path.join(root, 'basis.wasm'));
});

test('a vendored transcoder is committed, so a capture without one still decodes', () => {
  // The one file everything else depends on. A capture only contains a
  // transcoder if the grabber happened to save it, and one build's capture
  // arrived without and had to borrow from another's. Without a decoder a
  // Basis-compressed capture cannot be read at all, so the fallback is
  // committed rather than fetched.
  assert.ok(fs.existsSync(VENDORED), `${VENDORED} must be committed`);
  assert.ok(fs.existsSync(VENDORED.replace(/\.js$/, '.wasm')), 'and its wasm beside it');
  // Node-capable: a browser-only Emscripten build cannot be loaded here at all.
  const glue = fs.readFileSync(VENDORED, 'utf8');
  assert.ok(/ENVIRONMENT_IS_NODE/.test(glue), 'the glue must support Node');
  assert.ok(/module\.exports/.test(glue), 'and export its factory');
});

test('basis files are found at the depth a capture actually nests them', () => {
  const root = tmpdir();
  const deep = path.join(root, 'api.example.com', 'release', 'abc', 'files', 'assets', '1', '1');
  fs.mkdirSync(deep, { recursive: true });
  fs.writeFileSync(path.join(deep, 'skin.basis'), 'x');
  fs.writeFileSync(path.join(deep, 'skin.png'), 'x');
  fs.writeFileSync(path.join(root, 'top.basis'), 'x');
  assert.deepEqual(findBasis(root).map((f) => path.basename(f)).sort(), ['skin.basis', 'top.basis']);
});
