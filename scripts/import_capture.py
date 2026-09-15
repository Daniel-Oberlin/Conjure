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
from conjure.importer import plan_import, read_glb_json                  # noqa: E402
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


def container_files(capture: str) -> dict[int, tuple[str, str]]:
    """Container asset id -> `(file stem, the build directory that registers it)`.

    Both views of the same fact, because two callers need different halves of it. Keying on IDENTITY is
    what makes the composed path work at all: a composed GLB is named after the THING it holds
    (`office-babe.glb`, `Banana.glb`) and not after any container, so there is no stem to match. It
    carries the container ids it drew from in `extras.conjure` instead — which is the fact rather than a
    coincidence of naming, since `underwear.glb` appears in eight captures under eight different ids and
    `office-babe` draws from three files at once.

    The STEM half is still needed, for two things: the older one-per-container output is named after it,
    and the rows a previous per-container import left in the catalog are LABELLED with it, which is how
    `_retire_containers` recognises them.
    """
    out: dict[int, tuple[str, str]] = {}
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
                out.setdefault(int(aid), (os.path.splitext(os.path.basename(path))[0], build))
    return out


def provenance(data: bytes) -> dict:
    """`extras.conjure` from a composed GLB, or `{}` for anything else.

    The composer writes it (`conjure/compose.py`) so that everything downstream can ask what a file IS
    rather than infer it from a name: which thing, which scene, which containers it merged, and which
    of its nodes the scene had switched off.
    """
    try:
        doc = read_glb_json(data)
    except Exception:                                                    # noqa: BLE001
        return {}
    return ((doc or {}).get("extras") or {}).get("conjure") or {}


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


def same_thing(library, scope: str, capture: str, kind, label) -> list[dict]:
    """The LIVE rows this import is about to replace: same kind, same label, already tagged `capture`.

    Identity above the bytes, which a content-addressed catalog has no other way to express — the whole
    problem is that the same logical asset gets a new id whenever the converter improves.

    **The capture tag is what makes it safe.** Two captures can ship different `computer_desk` bytes and
    each keeps its own row, because a row tagged only `jane` is never a candidate while importing
    `akari`. Without that, re-importing one capture would retire another's props.

    `by_user` rather than `query()`: the latter's scoped view now hides superseded rows, and this wants
    only what is currently live — retiring a tombstone twice means nothing.
    """
    if not (kind and label):
        return []
    out = []
    for row in library.by_user(scope.split("/", 1)[0], limit=100_000):
        if row.get("kind") != kind or (row.get("label") or "") != label:
            continue
        if capture in [t.strip() for t in (row.get("tags") or "").split(",")]:
            out.append(row)
    return out


def run(capture: str, rebuilt: str, *, scope: str, library: AssetLibrary, cache: str, commit: bool,
        report=print) -> dict:
    name = os.path.basename(capture.rstrip("/"))
    stats: dict = defaultdict(int)
    set_id = f"set:{name}"

    def put(asset_id, data=None, **fields):
        stats[fields.get("kind", "?")] += 1
        if commit:
            # An asset this import is WRITING is current, whatever a previous run decided. An id is a
            # content address, so reverting a converter change brings the old bytes and the old id back
            # — onto a row a later run had already retired. Without this the import looks clean and the
            # asset stays invisible: 98 live models became 47, each re-import landing in its own
            # tombstone.
            if library.revive(asset_id):
                stats["revived"] += 1
            if data is not None and _store(cache, asset_id, data):
                stats["bytes_written"] += 1
            # TAG EVERY ASSET WITH ITS CAPTURE. A set is called `susan` and her figure is called
            # `Alice`, so searching the name a person actually uses found the set and no model —
            # `place_cached_asset` needs a model, and the director reported her missing from a catalog
            # she was in. Tags are FTS-indexed, so this is the one field that fixes it.
            #
            # MERGED, not replaced: the shared props arrive in every capture and a second import must
            # not take the first one's tag off them.
            existing = (library.get(asset_id) or {}).get("tags") or ""
            words = [w for w in (t.strip() for t in existing.split(",")) if w]
            if name not in words:
                words.append(name)
            library.upsert(asset_id, scope=scope, tags=", ".join(words), **fields)
        _supersede_prior(asset_id, fields.get("kind"), fields.get("label"))

    def _supersede_prior(asset_id: str, kind, label) -> None:
        """Retire the row this import replaces.

        An id is a content address, so re-converting a capture with a fixed converter writes DIFFERENT
        bytes and therefore a NEW row, while the old one stays — seen live when the roughness fix
        changed `Teacher_v1` and `bride_ready` and the catalog then held two of each, with a search
        returning both and nothing to choose between them.

        **Identity above the bytes is `(kind, label)` within THIS capture.** The capture tag is what
        makes it safe: two captures can ship different `computer_desk` bytes and each keeps its own row,
        because only rows already tagged with the capture being imported are considered. It is still an
        inference — which is why `supersede` marks rather than deletes, so being wrong costs a column
        and not an asset.
        """
        if not (commit and kind and label):
            return
        for row in same_thing(library, scope, name, kind, label):
            if row["id"] == asset_id:
                continue
            ok, _err = library.supersede(row["id"], asset_id)
            if ok:
                stats["superseded"] += 1
                report(f"    retired {row['id']} — replaced by {asset_id} ({label})")


    def _retire_containers() -> None:
        """Retire the rows a PER-CONTAINER import of this capture left behind.

        The switch from one asset per file to one per thing is not a re-import of the same rows: the
        labels change, so the ordinary `(kind, label)` supersession catches only the handful that happen
        to share a name (`office-babe` does, since her thing is named after her file). Everything else
        would sit in the catalog forever, and those leftovers are the ones that cause harm — asked to
        "switch to a different bride" the director offered `model_britney_bride`, a bare body with no
        clothes or hair, because nothing distinguished a figure from a piece of one.

        Recognised by LABEL matching a container file stem of this capture, which is what the old path
        named its rows after, and retired only into a thing that actually contains that container. A
        container no thing draws is left alone and reported: `computer_desk`, `magnet` and the Quest
        controller are bound only inside the app's own excluded machinery, so whether they are worth
        rescuing is a `captures/things.json` decision and not this script's to take.

        `TOOLS LIBRARYblend5` is the fan-out case — one file, fifteen things — and a tombstone points at
        one row. Where several things share a container it points at the SET, because that is what
        actually replaced it: the file's contents are now spread across the capture, and following the
        tombstone to the set finds all fifteen. Pointing at one of them would be picking arbitrarily
        between equals, and the first version of this announced that the cutlery drawer had become a
        cucumber.
        """
        if not commit:
            return
        stems = {stem: cid for cid, (stem, _build) in files.items()}
        orphans = []
        for stem, cid in sorted(stems.items()):
            if stem in labels_written:
                continue                          # a thing is named after this file; normal supersession has it
            heirs = sorted(things_seen.get(cid) or [], reverse=True)
            for row in same_thing(library, scope, name, "model", stem):
                if not heirs:
                    orphans.append(stem)
                    continue
                heir = heirs[0][1] if len(heirs) == 1 else set_id
                ok, _err = library.supersede(row["id"], heir)
                if ok:
                    stats["superseded:container"] += 1
                    became = (f"inside {library.get(heir).get('label')}" if len(heirs) == 1 else
                              f"spread across {len(heirs)} things in this set")
                    report(f"    retired {stem} — its geometry is now {became}")
        if orphans:
            report(f"    {len(orphans)} container row(s) left alone — no thing draws them, so they are "
                   f"the app's own machinery rather than props ({', '.join(orphans[:5])}"
                   f"{', …' if len(orphans) > 5 else ''})")

    def link(a, b, kind):
        stats[f"rel:{kind}"] += 1
        if commit:
            library.add_relation(a, b, kind)

    put(set_id, kind="set", label=name, source=f"capture://{name}",
        attributes={"capture": name, "origin": "playcanvas"})

    # ---- the models, which are what a world actually places --------------------------------------
    files = container_files(capture)
    by_stem = {stem: build for stem, build in files.values()}
    by_container = {cid: build for cid, (_stem, build) in files.items()}
    figures: dict[str, list[str]] = defaultdict(list)    # build dir -> [asset id]
    things_seen: dict[int, list[tuple[int, str]]] = defaultdict(list)   # container -> [(pieces, asset)]
    labels_written: set[str] = set()
    for file in sorted(f for f in os.listdir(rebuilt) if f.endswith(".glb")) if os.path.isdir(rebuilt) else []:
        path = os.path.join(rebuilt, file)
        data = open(path, "rb").read()
        result = plan_import(file, data, {})
        if not result:
            report(f"    ? {file}: nothing recognised it")
            continue
        asset_id = _asset_id(data, result.ext)
        mark = provenance(data)
        # The THING's own name, not the file's. `_safe` had to make the file name safe for a
        # filesystem and the label should not inherit that, and a per-container import labelled
        # `TOOLS LIBRARYblend5` where the catalog now holds fifteen separately placeable tools.
        label = mark.get("thing") or os.path.splitext(file)[0]
        attributes = {**result.attributes}
        if mark:
            # The capture comes from the IMPORT, not from the file — see `compose_thing`, which keeps
            # it out of the bytes so a prop shared by fifteen captures is one row and not fifteen.
            attributes["thing"] = {"scene": mark.get("scene"), "capture": name,
                                   "containers": sorted(mark.get("containers") or {})}
            # WHICH PARTS THE SOURCE HAS SWITCHED OFF, stated rather than classified. A different fact
            # from `parts` (which garment a mesh IS) and the runtime needs both: `figure-parts` hides
            # by node name, and until now the only answer came from reading mesh names.
            if mark.get("hidden"):
                attributes["parts_hidden"] = list(mark["hidden"])
        put(asset_id, data, kind=result.kind, label=label,
            source=f"cache://{asset_id}", filename=asset_id, attributes=attributes)
        labels_written.add(label)
        link(asset_id, set_id, "part_of")
        for cid in (mark.get("containers") or {}):
            things_seen[int(cid)].append(((mark.get("containers") or {}).get(cid, 0), asset_id))
        # A FIGURE, not merely something with a skin: `rig_sig` is only set when a humanoid map was
        # recovered, which is the difference between a character and a pair of disembodied hands.
        if result.attributes.get("rig_sig"):
            build = next((by_container[int(c)] for c in (mark.get("containers") or {})
                          if int(c) in by_container), None) or by_stem.get(os.path.splitext(file)[0])
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

    _retire_containers()
    report(f"\n  {name}: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())))
    return dict(stats)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("capture")
    ap.add_argument("--models", "--rebuilt", dest="models", default="",
                    help="the converted GLBs (default temp/things/<name>, one per THING — pass "
                         "temp/rebuilt/<name> for the older one-per-container output)")
    ap.add_argument("--scope", default="daniel/agents/builder")
    ap.add_argument("--library", default="", help="catalog path (default: the real one)")
    ap.add_argument("--cache", default="", help="asset bytes dir (default: beside the catalog)")
    ap.add_argument("--commit", action="store_true", help="actually write — otherwise this is a dry run")
    args = ap.parse_args()

    name = os.path.basename(args.capture.rstrip("/"))
    models = args.models or os.path.join("temp/things", name)
    if not os.path.isdir(args.capture):
        print(f"{args.capture} is not a directory")
        return 2
    from conjure.config import DATA_DIR
    path = args.library or os.path.join(str(DATA_DIR), "library.db")
    cache = args.cache or os.path.join(os.path.dirname(path), "assets")
    library = AssetLibrary(path)
    print(f"catalog: {path}\nbytes:   {cache}"
          f"{'' if args.commit else '   (DRY RUN — nothing will be written)'}")
    if not os.path.isdir(models):
        print(f"{models} is not a directory — compose it first with "
              f"`playcanvas_rebuild.py {args.capture} --out {models} --compose`")
        return 2
    run(args.capture, models, scope=args.scope, library=library, cache=cache, commit=args.commit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
