#!/usr/bin/env python
"""Give every figure a searchable description, a thumbnail and an embedding.

    python scripts/describe_figures.py --list
    python scripts/describe_figures.py --commit                    # layer 1 only, free
    python scripts/describe_figures.py --commit --render --embed   # + thumbnails in the image space
    python scripts/describe_figures.py --commit --render --caption # + a written description

Three layers, from `backlogs/library.md` § *Describing and embedding models*, kept apart because they
fail differently:

  1. **Structured text** — `conjure.describe.structured_text`, a pure function of the catalog row.
     Deterministic and un-refusable. Always runs.
  2. **A thumbnail, embedded.** Models are kept out of the vector index because text-derived vectors
     bury the images (measured: model titles at ~0.69 against images at ~1.33). A rendered view joins
     the IMAGE space instead, at a consistent scale, and buys similarity search that no description can.
  3. **A written description**, from a multimodal model, into `notes` for FTS. Not the vector — putting
     text there is the thing layer 2 exists to avoid.

**Outside the server, because it needs Blender**, which `backlogs/library.md` is explicit the world
server must never depend on. This writes to the library directly, the way `import_capture.py` does.

**`--shown` is not optional and is not a flag here.** A render of every mesh stacks each figure's
wardrobe on top of itself, and a vision model describes the result in perfect earnest: Alice's hidden
underwear came back as "a bright pink fanny pack worn diagonally across her front" and the losing half
of a hair variant as "a striking white streak". Both were bugs, described as fashion.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conjure.describe import structured_text                      # noqa: E402
from conjure.library import AssetLibrary                          # noqa: E402
from conjure.server import ASSET_CACHE, LIBRARY_DB                # noqa: E402

SCOPE = "daniel/agents/builder"
BLENDER = os.environ.get("BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender")

#: Written for search, not for prose. The constraints earn their place: an A-pose render invites
#: "standing with arms out", which is true of every figure and therefore worth nothing; and a model
#: asked to speculate will, at length.
FIGURE_PROMPT = (
    "This is a plain render of a 3D character model from an asset library, in an A-pose on a white "
    "background. Write ONE paragraph, at most 80 words, that a person could search against: apparent "
    "age and build, hair (length, style, colour), what they are wearing from top to bottom including "
    "footwear, and the style or setting the outfit suggests. Describe only what you can see. Do not "
    "mention the pose, the background, the render, or that it is a 3D model. No preamble."
)

#: FTS scores longer documents differently; an unbounded description would quietly rank whichever
#: figure got the most verbose answer.
MAX_CAPTION = 700


def render(glb: Path, out: Path, *, size: int = 512) -> Path | None:
    """One front view of what the scene actually draws. `None` if Blender is not here or it failed."""
    if not Path(BLENDER).exists():
        return None
    out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [BLENDER, "--background", "--python", str(Path(__file__).parent / "glb_preview.py"),
         "--", str(glb), str(out), "--size", str(size), "--views", "1", "--shown"],
        capture_output=True, text=True)
    shot = out / "v0.png"
    if not shot.is_file():
        print(f"    render failed: {proc.stdout.strip().splitlines()[-1] if proc.stdout else proc.returncode}")
        return None
    return shot


async def caption(png: bytes) -> str:
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
    r = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=[types.Part.from_bytes(data=png, mime_type="image/png"),
                  types.Part(text=FIGURE_PROMPT)])
    return (r.text or "").strip().strip('"')[:MAX_CAPTION]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--commit", action="store_true", help="write; without it this is a dry run")
    ap.add_argument("--render", action="store_true", help="render a thumbnail (needs Blender)")
    ap.add_argument("--embed", action="store_true", help="embed the thumbnail (implies --render)")
    ap.add_argument("--caption", action="store_true", help="describe the thumbnail (implies --render)")
    ap.add_argument("--only", default="", help="one figure, by label")
    ap.add_argument("--list", action="store_true", help="show what would be written and stop")
    ap.add_argument("--thumbs", default="temp/figure-thumbs", help="where renders are kept")
    args = ap.parse_args()
    want_render = args.render or args.embed or args.caption

    lib = AssetLibrary(LIBRARY_DB)
    rows = lib.query("SELECT * FROM assets WHERE kind = 'model'", scope=SCOPE, limit=1000) or []
    figures = []
    for r in rows:
        try:
            a = json.loads(r.get("attributes") or "{}")
        except ValueError:
            continue
        if a.get("rigged") and (not args.only or r.get("label") == args.only):
            figures.append((r, a))
    print(f"{len(figures)} rigged figure(s)")

    embedder = None
    if args.embed:
        # The SAME embedder the server uses, taken from the server module rather than rebuilt — two
        # embedders configured differently would put figures in a space the search never queries.
        from conjure.server import embedder as _server_embedder
        embedder = _server_embedder
        print(f"  embedder  {getattr(embedder, 'name', None)}")

    wrote = 0
    for r, a in figures:
        label = r.get("label") or r["id"]
        clips = [x.get("label") for x in lib.related(r["id"], "shipped_with")
                 if x.get("kind") == "animation"]
        voiced = sum(1 for c in lib.related(r["id"], "shipped_with")
                     if any(v.get("kind") == "audio" for v in lib.related(c["id"], "voiced_by")))
        text = structured_text(label, a, clips=clips, voiced=voiced)
        described = ""
        vector = None

        if want_render:
            glb = ASSET_CACHE / r["id"]
            shot = render(glb, Path(args.thumbs) / label.replace("/", "-")) if glb.exists() else None
            if shot:
                png = shot.read_bytes()
                if args.caption:
                    try:
                        described = asyncio.run(caption(png))
                    except Exception as exc:              # noqa: BLE001 — enrichment, never fatal
                        print(f"    caption failed: {type(exc).__name__}: {exc}"[:140])
                if embedder is not None:
                    try:
                        vector = embedder.embed_image(png)
                    except Exception as exc:              # noqa: BLE001
                        print(f"    embed failed: {type(exc).__name__}: {exc}"[:140])

        # The description goes FIRST: it is what a person would have written, and the structured half
        # is the part they would not have bothered with. Both are indexed; only one reads well.
        notes = "\n\n".join(p for p in (described, text) if p)
        print(f"\n{label}")
        if notes:
            print("  " + notes.replace("\n\n", "\n  "))
        if vector is not None:
            print(f"  [embedded {len(vector)}d]")
        if args.list or not args.commit:
            continue
        if notes:
            lib.upsert(r["id"], notes=notes)
        if vector is not None and embedder is not None:
            lib.add_embedding(r["id"], vector, embedder.name)
        wrote += 1

    print(f"\n{wrote} figure(s) written" if args.commit and not args.list else
          f"\ndry run — pass --commit to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
