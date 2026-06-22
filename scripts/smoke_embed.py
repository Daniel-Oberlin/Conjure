"""One-time smoke test for the SigLIP embedder (docs/asset-library-plan.md, Phase 1 caveat).

Prereq:  pip install -e ".[embed]"     (pulls torch + transformers; not in the core install)
Run:     python scripts/smoke_embed.py
         CONJURE_EMBED_MODEL=google/siglip2-base-patch16-224 python scripts/smoke_embed.py   # lighter

It exercises the REAL configured wiring (conjure.server.embedder) and checks three things:
  1. the model loads and embeddings are unit vectors of a stable dim (the torch path runs);
  2. the space is semantically meaningful — a related text pair scores higher than an unrelated one;
  3. the text↔image path runs and, if a cached image with a known prompt exists, its own prompt
     scores higher than an unrelated prompt (the shared text-image space actually aligns).
"""

from __future__ import annotations

import math
import sys

from conjure.server import ASSET_CACHE, embedder, library


def cos(a, b):                       # vectors are unit-norm, so dot product == cosine similarity
    return sum(x * y for x, y in zip(a, b))


def main() -> int:
    if embedder is None or type(embedder).__name__ != "SigLipEmbedder":
        print("✗ embedder is not the SigLIP backend. Is torch installed (pip install -e \".[embed]\") "
              "and CONJURE_EMBED_BACKEND not 'none'/'fake'?  Got:", type(embedder).__name__)
        return 1
    print(f"backend: {type(embedder).__name__}  model: {embedder.name}")
    print("loading model … (first run downloads weights to ~/.cache/huggingface — can be ~1 GB)")

    # 1. loads + unit norm + stable dim
    v = embedder.embed_text("a red sports car")
    norm = math.sqrt(sum(x * x for x in v))
    print(f"  embed dim = {len(v)}   |v| = {norm:.4f}  (expect ~1.000)")
    assert abs(norm - 1.0) < 1e-2, "embedding is not unit-normalized"

    # 2. text↔text semantics: related must beat unrelated
    car = embedder.embed_text("a red sports car")
    near = cos(car, embedder.embed_text("a fast crimson automobile"))
    far = cos(car, embedder.embed_text("a bowl of vegetable soup"))
    print(f"  text↔text: related={near:.3f}  unrelated={far:.3f}  -> {'OK' if near > far else 'FAIL'}")
    assert near > far, "semantic ordering wrong — the space looks broken"

    # 3. text↔image ALIGNMENT (the shared-space payoff) — controlled, always available: a red vs
    #    blue swatch must each align with its own colour word. Cheap proof that text & image share
    #    one space without needing real captioned photos.
    import io

    from PIL import Image

    def _png(color):
        b = io.BytesIO()
        Image.new("RGB", (96, 96), color).save(b, "PNG")
        return b.getvalue()

    red, blue = embedder.embed_image(_png("red")), embedder.embed_image(_png("blue"))
    tred, tblue = embedder.embed_text("a solid red image"), embedder.embed_text("a solid blue image")
    ok = cos(red, tred) > cos(red, tblue) and cos(blue, tblue) > cos(blue, tred)
    print(f"  text↔image: red→red {cos(red, tred):.3f}/{cos(red, tblue):.3f}  "
          f"blue→blue {cos(blue, tblue):.3f}/{cos(blue, tred):.3f}  -> {'OK' if ok else 'FAIL'}")
    assert ok, "text and image embeddings don't align — the shared space looks broken"

    # 4. bonus: if a cached image has a known prompt, check real-data alignment too
    row = library._db.execute(
        "SELECT id, prompt FROM assets WHERE kind='image' AND prompt IS NOT NULL AND prompt != '' "
        "LIMIT 1").fetchone()
    if row and (ASSET_CACHE / row["id"]).exists():
        iv = embedder.embed_image((ASSET_CACHE / row["id"]).read_bytes())
        match = cos(iv, embedder.embed_text(row["prompt"]))
        other = cos(iv, embedder.embed_text("an abstract geometric pattern"))
        verdict = "OK" if match > other else "?? (inspect)"
        print(f"  text↔image: '{row['id']}' (“{row['prompt'][:40]}”) own-prompt={match:.3f} "
              f"other={other:.3f}  -> {verdict}")
    else:
        import glob
        imgs = glob.glob(str(ASSET_CACHE / "*.png")) + glob.glob(str(ASSET_CACHE / "*.jpg"))
        if imgs:
            iv = embedder.embed_image(open(imgs[0], "rb").read())
            print(f"  text↔image: embed_image path OK (dim {len(iv)}); no known-prompt image to test "
                  "alignment — generate one via the app to fully verify")
        else:
            print("  text↔image: no cached images; generate one via the app to verify this path")

    print("\n✅ SMOKE TEST PASSED — the SigLIP embedder produces real, aligned vectors.")
    print("   (The server now embeds generated images & placed models automatically via write-through.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
