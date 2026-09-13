# Basis Universal transcoder

`basis_transcoder.js` + `.wasm`, the upstream decoder from
[BinomialLLC/basis_universal](https://github.com/BinomialLLC/basis_universal),
taken from the `three` npm package (`examples/jsm/libs/basis/`) so the copy is
traceable to a published release rather than lifted off a CDN.

**Licence: Apache 2.0**, © Binomial LLC. Unmodified.

## Why it is committed

`scripts/basis_to_png.js` decodes the `.basis` textures in a captured
PlayCanvas build. A build ships its own transcoder, and using *that* copy is
better when it is present — it is guaranteed to match the version that encoded
the files. But a capture only contains one if the grabber happened to save it,
and it is the file everything else depends on: without a transcoder, a capture
whose textures are Basis-compressed cannot be read at all. One build's capture
arrived without it and had to borrow from another.

So this is the fallback, and the search order is:

1. `--transcoder <path>` if given
2. the transcoder inside the capture, matching by construction
3. this copy

Basis is a stable, widely-vendored format — three.js, Babylon and PlayCanvas
all ship the same upstream artifact, byte-identical across two projects on the
site this was written against — so a version mismatch is unlikely to matter.
The capture's own copy is still preferred on the principle that a decoder
shipped beside the data is the one that is certain to read it.
