#!/usr/bin/env python3
"""Import one reconstructed capture into the asset library, with the links its names support.

    python scripts/import_capture.py temp/vrh/jane --rebuilt temp/rebuilt/jane            # dry run
    python scripts/import_capture.py temp/vrh/jane --rebuilt temp/rebuilt/jane --commit
    python scripts/import_capture.py temp/vrh/jane --rebuilt temp/rebuilt/jane --library /tmp/t.db --commit

**Dry by default.** It writes to the real catalog otherwise, and an import that turns out wrong is
tedious to unpick — the bytes are content-addressed and shared, so deleting a row is not the inverse.

What it creates:

    set          one row per capture, ASSERTED. The grouping is not in the build — nothing links
                 Jane's registry to the room she appears in — so it is an argument, not a guess.
    model        the rebuilt GLBs, with their rig signature
    animation    the skeleton-only clips from the source build
    audio        the clips' voices and the rooms' soundtracks, minus the marketing

and the links `conjure/capture_set.py` can defend: `shipped_with` (a figure and the clips in its own
build — the authored set), `voiced_by` (many-to-many), `ambience`, and `part_of` for provenance.
Anything the names do not support is left unlinked rather than guessed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.parse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure import capture_set                                          # noqa: E402
from conjure.importer import plan_import                                 # noqa: E402
from conjure.library import AssetLibrary                                 # noqa: E402
from conjure.playcanvas import find_builds, read_build                               # noqa: E402


def _asset_id(data: bytes, ext: str) -> str:
    """The same content address the server mints, so an asset imported twice is one row."""
    return f"{hashlib.sha256(data).hexdigest()[:16]}{ext}"


def _store(cache: str, asset_id: str, data: bytes) -> bool:
    """Put the BYTES where a catalog row promises they are.

    A row says `cache://<id>` and `/assets/<id>` is how every consumer fetches it — the client's
    `gltf-model`, `place_cached_asset`, the frame-rev refresh. Cataloguing without writing them leaves
    a row that looks complete and resolves to nothing, and `gc` would then offer to tidy up the
    opposite end. Content-addressed, so writing twice is writing once.
    """
    path = os.path.join(cache, asset_id)
    if os.path.exists(path) and os.path.getsize(path) == len(data):
        return False
    os.makedirs(cache, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return True


def _containers_by_build(capture: str) -> dict[str, str]:
    """Rebuilt-GLB stem -> the build directory it came from.

    This is what makes `shipped_with` mean what it says. A capture holds several builds — a character
    in one, the shared hands and props in another — and the authored set is the clips that shipped in
    the FIGURE's OWN build. Without it, every rigged thing in the capture claims every clip and the VR
    hands end up owning Jane's twenty-one animations.

    Keyed on the container's FILE name, because that is what the rebuild names its output after
    (`playcanvas.rebuild_build`: `stem = basename(path)`). The registry's asset NAME is a different
    string and keying on it silently loses the match: Susan's container is filed as `Alice.glb` and
    called `aula_Aliceglb`, so her figure claimed none of her eight clips.
    """
    out: dict[str, str] = {}
    for build in find_builds(capture):
        try:
            read = read_build(build)
        except Exception:                                                # noqa: BLE001
            continue
        for aid, asset in read.assets.items():
            if asset.get("type") != "container":
                continue
            path = read.path(aid)
            if path:
                out.setdefault(os.path.splitext(os.path.basename(path))[0], build)
    return out


def _source_assets(capture: str, kinds=("animation", "audio")) -> list[dict]:
    """Every registry asset of the given types, with the build it came from and its path on disk."""
    out = []
    for build in find_builds(capture):
        try:
            cfg = json.load(open(os.path.join(build, "config.json")))
        except Exception:                                                # noqa: BLE001
            continue
        for aid, asset in (cfg.get("assets") or {}).items():
            if asset.get("type") not in kinds:
                continue
            url = ((asset.get("file") or {}).get("url") or "")
            path = os.path.join(build, urllib.parse.unquote(url))
            if not url or not os.path.exists(path) or os.path.getsize(path) == 0:
                continue
            out.append({"id": aid, "name": asset.get("name") or os.path.basename(path),
                        "type": asset.get("type"), "path": path, "build": build})
    return out


def run(capture: str, rebuilt: str, *, scope: str, library: AssetLibrary, cache: str, commit: bool,
        report=print) -> dict:
    name = os.path.basename(capture.rstrip("/"))
    stats: dict = defaultdict(int)
    set_id = f"set:{name}"

    def put(asset_id, data=None, **fields):
        stats[fields.get("kind", "?")] += 1
        if commit:
            if data is not None and _store(cache, asset_id, data):
                stats["bytes_written"] += 1
            library.upsert(asset_id, scope=scope, **fields)

    def link(a, b, kind):
        stats[f"rel:{kind}"] += 1
        if commit:
            library.add_relation(a, b, kind)

    put(set_id, kind="set", label=name, source=f"capture://{name}",
        attributes={"capture": name, "origin": "playcanvas"})

    # ---- the rebuilt models, which are what a world actually places -----------------------------
    by_build = _containers_by_build(capture)
    figures: dict[str, list[str]] = defaultdict(list)    # build dir -> [asset id]
    for file in sorted(f for f in os.listdir(rebuilt) if f.endswith(".glb")) if os.path.isdir(rebuilt) else []:
        path = os.path.join(rebuilt, file)
        data = open(path, "rb").read()
        result = plan_import(file, data, {})
        if not result:
            report(f"    ? {file}: nothing recognised it")
            continue
        asset_id = _asset_id(data, result.ext)
        put(asset_id, data, kind=result.kind, label=os.path.splitext(file)[0],
            source=f"cache://{asset_id}", filename=asset_id, attributes=result.attributes)
        link(asset_id, set_id, "part_of")
        # A FIGURE, not merely something with a skin: `rig_sig` is only set when a humanoid map was
        # recovered, which is the difference between a character and a pair of disembodied hands.
        if result.attributes.get("rig_sig"):
            build = by_build.get(os.path.splitext(file)[0])
            if build:
                figures[build].append(asset_id)
            else:
                report(f"    ? {file}: rigged, but no build claims it — no authored set recorded")

    # ---- clips and audio, which live only in the source build -----------------------------------
    source = _source_assets(capture)
    clip_names = [a["name"] for a in source if a["type"] == "animation"]
    clips: dict[str, str] = {}                           # registry name -> asset id
    clips_by_build: dict[str, list[str]] = defaultdict(list)
    for asset in source:
        if asset["type"] != "animation":
            continue
        data = open(asset["path"], "rb").read()
        result = plan_import(asset["name"], data, {"kind": "animation"})
        asset_id = _asset_id(data, ".glb")
        extra = {**result.attributes}
        if capture_set.clip_kind(asset["name"]):
            extra["clip_kind"] = capture_set.clip_kind(asset["name"])
        if capture_set.wants_props(asset["name"]):
            extra["wants_props"] = capture_set.wants_props(asset["name"])
        extra["slot_named"] = capture_set.is_slot_named(asset["name"])
        put(asset_id, data, kind="animation", label=capture_set.stem(asset["name"]),
            source=f"cache://{asset_id}", filename=asset_id, attributes=extra)
        link(asset_id, set_id, "part_of")
        clips[asset["name"]] = asset_id
        clips_by_build[asset["build"]].append(asset_id)

    for asset in source:
        if asset["type"] != "audio":
            continue
        role, voiced = capture_set.audio_role(asset["name"], clip_names)
        if role == "skip":
            stats["audio:skipped"] += 1
            continue
        data = open(asset["path"], "rb").read()
        result = plan_import(asset["name"], data, {"kind": "audio"})
        asset_id = _asset_id(data, ".mp3")
        put(asset_id, data, kind="audio", label=capture_set.stem(asset["name"]),
            source=f"cache://{asset_id}", filename=asset_id,
            attributes={**result.attributes, "role": role})
        link(asset_id, set_id, "part_of")
        for clip_name in voiced:
            link(clips[clip_name], asset_id, "voiced_by")
        if role == "ambience":
            stats["audio:ambience"] += 1
        elif role == "unlinked":
            stats["audio:unlinked"] += 1

    # ---- the AUTHORED set: a figure and the clips that shipped in its own build ------------------
    #
    # Not "every clip that will play on it" — that is `rig_sig`, and it is a different question with a
    # different answer. This is what the original scene gave this character, which is the only record
    # of intent and is not recoverable from the bytes once lost.
    for build, asset_ids in figures.items():
        for asset_id in asset_ids:
            for clip_id in clips_by_build.get(build, []):
                link(asset_id, clip_id, "shipped_with")

    report(f"\n  {name}: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())))
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("capture")
    ap.add_argument("--rebuilt", default="", help="the reconstructed GLBs (default temp/rebuilt/<name>)")
    ap.add_argument("--scope", default="daniel/agents/builder")
    ap.add_argument("--library", default="", help="catalog path (default: the real one)")
    ap.add_argument("--cache", default="", help="asset bytes dir (default: beside the catalog)")
    ap.add_argument("--commit", action="store_true", help="actually write — otherwise this is a dry run")
    args = ap.parse_args()

    name = os.path.basename(args.capture.rstrip("/"))
    rebuilt = args.rebuilt or os.path.join("temp/rebuilt", name)
    if not os.path.isdir(args.capture):
        print(f"{args.capture} is not a directory")
        return 2
    from conjure.config import DATA_DIR
    path = args.library or os.path.join(str(DATA_DIR), "library.db")
    cache = args.cache or os.path.join(os.path.dirname(path), "assets")
    library = AssetLibrary(path)
    print(f"catalog: {path}\nbytes:   {cache}"
          f"{'' if args.commit else '   (DRY RUN — nothing will be written)'}")
    run(args.capture, rebuilt, scope=args.scope, library=library, cache=cache, commit=args.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
