#!/usr/bin/env python3
"""What every figure's face can do, and which clips move one — read straight from the library.

    python3 scripts/faces.py                 every rigged figure, one line each
    python3 scripts/faces.py --morphs        ...and every morph target name
    python3 scripts/faces.py --clips         the clips that move a face, and which bones carry it
    python3 scripts/faces.py --check         run the geometric checks over the whole corpus
    python3 scripts/faces.py Saka Alice      just these

Exists because "who can smile" is a question about the CONTENTS of a GLB and there was no way to ask
it. `dir` lists assets and `inspect_figure` answers for a figure already placed in a world; neither
answers it across the library, which is where you stand when deciding what to place.

Two things it deliberately separates, because conflating them is the mistake the catalog made for
months: how many morph targets a figure has, and whether any of them are a FACE. 22 of 38 rigged
figures carry targets and **two** carry a face — the rest are `Body_Alabaster`, `Shirt_Blue`,
`Vagina_Open`: wardrobe, skin tone and anatomy.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conjure import expressions as X                                    # noqa: E402
from conjure.figures import (morph_centroids, morph_displacements,      # noqa: E402
                             morph_names, split_glb)
from conjure.library import AssetLibrary                                # noqa: E402
from conjure.server import ASSET_CACHE, LIBRARY_DB                      # noqa: E402

argv = sys.argv[1:]
FLAGS = {a for a in argv if a.startswith("--")}
ONLY = {a.lower() for a in argv if not a.startswith("--")}

#: Bones whose names say they move a face. Rigify spells them `DEF-lid.T.L`, `DEF-jaw_master`; a
#: Character Creator export spells the eyes `CC_Base_L_Eye`. Deliberately a name test: the humanoid map
#: has 51 bones and none are facial, so there is nothing semantic to ask instead (specs/figures.md §8d).
FACE_BONES = ("lid", "brow", "lip", "jaw", "cheek", "eye", "tongue", "nose", "teeth", "mouth")


def library():
    return AssetLibrary(LIBRARY_DB)


def path_of(row) -> Path:
    p = Path(row["filename"])
    return p if p.is_absolute() else Path(ASSET_CACHE) / row["filename"]


def doc_of(row):
    doc, blob = split_glb(path_of(row).read_bytes())
    if isinstance(doc, (bytes, bytearray, str)):
        doc = json.loads(doc)
    return doc, blob


def figures(lib):
    for r in lib.search(kind="model", limit=5000):
        attrs = r.get("attributes")
        attrs = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
        if not attrs.get("rigged") or not path_of(r).exists():
            continue
        if ONLY and (r["label"] or "").lower() not in ONLY:
            continue
        yield r, attrs


def show_figures(lib) -> None:
    print(f"{'figure':20s} {'targets':>7s} {'facial':>6s} {'scheme':>7s}  can do")
    rows = sorted(figures(lib), key=lambda ra: (ra[0]["label"] or "").lower())
    for r, _ in rows:
        doc, _blob = doc_of(r)
        names = morph_names(doc)
        face = X.facial(names)
        scheme = X.scheme_of(names)
        can = [e for e in X.EXPRESSIONS if X.resolve(names, {e: 1.0})[0]]
        print(f"{(r['label'] or '?')[:20]:20s} {len(names):>7d} {len(face):>6d} {scheme or '-':>7s}  "
              + (", ".join(can) if can else "—"))
        if "--morphs" in FLAGS and names:
            for n in names:
                print(f"{'':22s}{X.role_of(n):7s} {n}")


def show_clips(lib) -> None:
    """Which clips move a face, and on what — bones or morph weights.

    The answer is BONES, and it is the whole reason this listing is worth having. 249 of 543 clips
    drive morph weights and not one drives a facial target; the facial performance in this corpus is
    carried on a Rigify facial rig, and nothing in the catalog said so.
    """
    from conjure.retarget import _read_accessor

    print(f"{'clip':26s} {'secs':>5s} {'face bones':>10s} {'travel':>8s}  what carries it")
    rows = []
    for r in lib.search(kind="animation", limit=5000):
        if not path_of(r).exists():
            continue
        try:
            doc, blob = doc_of(r)
        except Exception:                               # noqa: BLE001 — one bad file, not the run
            continue
        nodes = doc.get("nodes") or []
        travel: dict[str, float] = {}
        weights = 0
        for anim in doc.get("animations") or []:
            samplers = anim.get("samplers") or []
            for ch in anim.get("channels") or []:
                target = ch.get("target") or {}
                if target.get("path") == "weights":
                    weights += 1
                if target.get("path") != "rotation":
                    continue
                idx = target.get("node")
                name = nodes[idx].get("name", "?") if isinstance(idx, int) and idx < len(nodes) else "?"
                if not any(k in name.lower() for k in FACE_BONES):
                    continue
                try:
                    q = list(_read_accessor(doc, blob, samplers[ch["sampler"]]["output"]))
                except Exception:                       # noqa: BLE001
                    continue
                if q and not isinstance(q[0], (tuple, list)):
                    q = [tuple(q[i:i + 4]) for i in range(0, len(q), 4)]
                total = 0.0
                for a, b in zip(q, q[1:]):
                    dot = abs(sum(float(x) * float(y) for x, y in zip(a, b)))
                    total += math.degrees(2 * math.acos(min(1.0, dot)))
                travel[name] = travel.get(name, 0.0) + total
        if not travel:
            continue
        attrs = r.get("attributes")
        attrs = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
        rows.append((r["label"], attrs.get("duration_s") or 0, travel, weights))

    rows.sort(key=lambda t: -sum(t[2].values()))
    for label, secs, travel, _weights in rows[:40]:
        total = sum(travel.values()) or 1.0
        top = sorted(travel.items(), key=lambda kv: -kv[1])[:2]
        carry = ", ".join(f"{n} {100 * d / total:.0f}%" for n, d in top)
        # A facial NAME is not evidence of a facial performance: `pc_squint` is a no-op at 0.13 deg
        # from rest. The travel column is what separates them.
        print(f"{(label or '?')[:26]:26s} {secs:>5.1f} {len(travel):>10d} {total:>7.0f}°  {carry}")
    print(f"\n{len(rows)} clip(s) rotate a facially-named bone. Travel is what a clip MOVES: a clip "
          f"that holds\nan expression has near-zero travel, so a low number is not proof it does "
          f"nothing (specs/figures.md §8d).")


def show_check(lib) -> None:
    print("the two geometric checks, over every figure that has a face\n")
    for r, _ in sorted(figures(lib), key=lambda ra: (ra[0]["label"] or "").lower()):
        doc, blob = doc_of(r)
        names = morph_names(doc)
        if not X.facial(names):
            continue
        regions = X.check_regions(morph_centroids(doc, blob))
        directions = X.check_directions(morph_displacements(doc, blob))
        scheme = X.scheme_of(names)
        state = "FAIL" if (regions or directions) else ("OK" if scheme else "n/a")
        print(f"  {state:4s} {(r['label'] or '?')[:18]:18s} scheme={scheme or '-':4s} "
              f"facial={len(X.facial(names))}")
        for problem in regions + directions:
            print(f"        ! {problem}")


def main() -> None:
    lib = library()
    if "--clips" in FLAGS:
        show_clips(lib)
    elif "--check" in FLAGS:
        show_check(lib)
    else:
        show_figures(lib)


if __name__ == "__main__":
    main()
