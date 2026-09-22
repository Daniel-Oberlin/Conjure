#!/usr/bin/env python3
"""Re-assemble a downloaded PlayCanvas build into self-contained, textured GLBs.

    python scripts/playcanvas_rebuild.py temp/vrh/jane --out temp/rebuilt
    python scripts/playcanvas_rebuild.py temp/vrh/jane --out temp/rebuilt --only jane
    python scripts/playcanvas_rebuild.py temp/vrh/jane --list        # what it would bind, and nothing else

PlayCanvas ships geometry and materials as SEPARATE assets and rejoins them at load time, so a build
downloaded from it renders flat white in any ordinary viewer while every texture sits beside it on disk.
The binding is stated outright in the build — `config.json` for the registry, the scene file for which
material goes on which primitive — so this transcribes rather than guesses. See `conjure/playcanvas.py`.

**`--max-texture` defaults to 1024 and that is a budget, not an optimisation.** The capture this was
written against uses 4096-square textures throughout: about 90 MB of VRAM each once mipmapped, nine of
them for one character who also costs 111k triangles. Ask for more only if you have measured that you
can afford it (docs/investigations/figures-frame-rate.md).
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure.compose import compose_build                                                        # noqa: E402
from conjure.playcanvas import (adopt_unbound, build_origin, by_container,                       # noqa: E402
                                find_builds, missing_files, read_build, rebuild_build,
                                report_orphans, thing_notes, things, variant_only)


def survey_things(root: str) -> int:
    """What each SCENE places, rather than what each FILE holds — the § 2b view."""
    for build_root in find_builds(root):
        build = read_build(build_root)
        capture = os.path.basename(root.rstrip("/"))
        found = things(build, capture=capture)
        if not found:
            continue
        print(f"\n{os.path.relpath(build_root, root) or '.'}")
        for note in thing_notes(build, found):
            print(f"    ! {note}")
        for thing in found:
            drawn = ", ".join(f"{build.name(c)}×{n}" for c, n in thing.containers.items())
            print(f"    {thing.name[:28]:30} {'' if thing.enabled else '(catalogued)':14} "
                  f"{thing.entities:5}e  {len(thing.live):3} live  {len(thing.optional):2} optional"
                  f"  <- {drawn}")
            if thing.optional:
                names = ", ".join(sorted({p.entity for p in thing.optional})[:4])
                print(f"        optional: {names} — the site's own switch, emit them HIDDEN")
            hung = [p for p in thing.live
                    if p.parent and p.parent != thing.name and p.parent not in {q.entity for q in thing.live}]
            for piece in hung[:4]:
                print(f"        {piece.entity} hangs off {piece.parent!r} — a merge must keep that")
            twice = {}
            for piece in thing.pieces:
                twice[(piece.container, piece.mesh)] = twice.get((piece.container, piece.mesh), 0) + 1
            for (cont, mesh), n in twice.items():
                if n > 1:
                    print(f"        {build.name(cont)} mesh {mesh} is drawn {n}× — instancing, "
                          f"and a merge that flattens nodes loses all but one")
    return 0


def survey(root: str) -> int:
    """What is here and what would be bound — the read-only half, for looking before converting."""
    builds = find_builds(root)
    orphans = report_orphans(root, print)
    if not builds:
        if not orphans:
            print(f"no PlayCanvas build under {root} (looking for a config.json with assets and "
                  f"scenes, or a files/assets tree)")
        return 2 if not orphans else 1
    for build_root in builds:
        build = read_build(build_root)
        build.origin = build_origin(root, build_root)
        adopt_unbound(build)
        groups = by_container(build)
        kinds: dict[str, int] = {}
        for a in build.assets.values():
            kinds[a.get("type")] = kinds.get(a.get("type"), 0) + 1
        print(f"\n{os.path.relpath(build_root, root) or '.'}")
        print(f"    {len(build.assets)} assets: "
              + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1])))
        for note in build.notes:
            print(f"    ! {note}")
        absent = missing_files(build)
        if absent:
            print(f"    ! {len(absent)} referenced file(s) are not in this capture — "
                  f"{'--fetch-list writes the URLs' if build.origin else 'no origin in the path'}")
        compressed = variant_only(build)
        if compressed:
            print(f"    ! {len(compressed)} texture(s) are here ONLY as Basis-compressed variants "
                  f"(e.g. {compressed[0][1]}) — needs a transcoder this does not carry")
        if not groups:
            print("    nothing bound — no entity in any scene uses a container")
        for container, binds in sorted(groups.items()):
            path = build.path(container)
            size = f"{os.path.getsize(path)/1e6:.1f} MB" if path and os.path.exists(path) else "MISSING"
            print(f"    {build.name(container):30} {size}")
            for bind in binds:
                print(f"        mesh {bind.mesh} [{bind.entity}]: "
                      + ", ".join(build.name(m) for m in bind.materials))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("build", help="a downloaded build, or a directory holding several")
    ap.add_argument("--out", default="", help="where to write the rebuilt GLBs")
    ap.add_argument("--list", action="store_true", help="report the bindings and write nothing")
    ap.add_argument("--things", action="store_true",
                    help="report what each SCENE places — the unit a container is not (plan § 2b)")
    ap.add_argument("--compose", action="store_true",
                    help="write one GLB per THING to --out, ALONGSIDE the per-container files rather "
                         "than instead of them, and check each one against the scene that described it")
    ap.add_argument("--as-shown", action="store_true",
                    help="with --compose, leave OUT what the scene does not draw — the wardrobe it has "
                         "switched off and the losing half of a variant. A viewing copy, not the asset: "
                         "a glb viewer draws every node it is given and ignores the hidden flag")
    ap.add_argument("--no-verify", action="store_true",
                    help="with --compose, write the files without checking them (you will not want this)")
    ap.add_argument("--only", default="", help="only containers whose name contains this")
    ap.add_argument("--max-texture", type=int, default=1024,
                    help="longest texture side, in pixels (default 1024 — read the docstring)")
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality for textures with no alpha")
    ap.add_argument("--fetch-list", default="",
                    help="write the URLs of referenced-but-absent files here, one per line — feed it "
                         "to `xargs -n1 curl -O` or wget -i")
    ap.add_argument("--no-decode", action="store_true",
                    help="do NOT decode Basis textures first (they come out untextured)")
    ap.add_argument("--adopt", action="store_true",
                    help="give a mesh no entity binds the material from an identically-named one "
                         "elsewhere in the build — INFERRED, and how Jane gets her hair back")
    args = ap.parse_args()

    if not os.path.isdir(args.build):
        print(f"{args.build} is not a directory")
        return 2
    if args.things:
        return survey_things(args.build)
    if args.list:
        return survey(args.build)
    if not args.out:
        print("--out is required (or use --list to look first)")
        return 2
    if args.compose:
        made, problems = compose_build(args.build, args.out, only=args.only, shown=args.as_shown,
                                       decode=not args.no_decode,
                                       max_texture=args.max_texture, quality=args.quality,
                                       verify=not args.no_verify, report=print)
        print(f"\n{len(made)} thing(s) written to {args.out}"
              + (f" — {problems} PROBLEM(S), listed above" if problems else
                 ("" if args.no_verify else " — the verifier is silent")))
        return 1 if problems or not made else 0
    urls: list[str] = []
    written = rebuild_build(args.build, args.out, max_texture=args.max_texture,
                            quality=args.quality, only=args.only, adopt=args.adopt,
                            decode=not args.no_decode, fetch_list=urls, report=print)
    if args.fetch_list:
        with open(args.fetch_list, "w") as fh:
            fh.write("\n".join(dict.fromkeys(urls)) + "\n")
        print(f"\n{len(set(urls))} URL(s) written to {args.fetch_list}")
    print(f"\n{len(written)} file(s) written to {args.out}" if written else "\nnothing written")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
