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

from conjure.playcanvas import (adopt_unbound, build_origin, by_container,     # noqa: E402
                                find_builds, missing_files, read_build, rebuild_build,
                                report_orphans, variant_only)


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
    ap.add_argument("--only", default="", help="only containers whose name contains this")
    ap.add_argument("--max-texture", type=int, default=1024,
                    help="longest texture side, in pixels (default 1024 — read the docstring)")
    ap.add_argument("--quality", type=int, default=90, help="JPEG quality for textures with no alpha")
    ap.add_argument("--fetch-list", default="",
                    help="write the URLs of referenced-but-absent files here, one per line — feed it "
                         "to `xargs -n1 curl -O` or wget -i")
    ap.add_argument("--adopt", action="store_true",
                    help="give a mesh no entity binds the material from an identically-named one "
                         "elsewhere in the build — INFERRED, and how Jane gets her hair back")
    args = ap.parse_args()

    if not os.path.isdir(args.build):
        print(f"{args.build} is not a directory")
        return 2
    if args.list:
        return survey(args.build)
    if not args.out:
        print("--out is required (or use --list to look first)")
        return 2
    urls: list[str] = []
    written = rebuild_build(args.build, args.out, max_texture=args.max_texture,
                            quality=args.quality, only=args.only, adopt=args.adopt,
                            fetch_list=urls, report=print)
    if args.fetch_list:
        with open(args.fetch_list, "w") as fh:
            fh.write("\n".join(dict.fromkeys(urls)) + "\n")
        print(f"\n{len(set(urls))} URL(s) written to {args.fetch_list}")
    print(f"\n{len(written)} file(s) written to {args.out}" if written else "\nnothing written")
    return 0 if written else 1


if __name__ == "__main__":
    sys.exit(main())
