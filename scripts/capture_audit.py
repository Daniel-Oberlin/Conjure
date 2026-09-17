#!/usr/bin/env python3
"""Is a downloaded capture COMPLETE? Per build, what is missing, and did a re-download lose anything?

    python scripts/capture_audit.py temp/vrh --each             # every capture
    python scripts/capture_audit.py temp/vrh/arabic             # one
    python scripts/capture_audit.py temp/vrh/arabic --against temp/vrh.old
    python scripts/capture_audit.py temp/vrh --files            # name the missing files

A capture is a mirror of a published build, and the build's `config.json` is a complete manifest: every
asset that has bytes declares a `file.url`, a size and a hash. So "is this capture complete" is a
question with an exact answer, and this asks it — which is worth having because the alternative is
discovering a gap much later, as a figure with no clip sound or a prop that renders white.

**`--against` is the one that matters after a re-download.** The grabber writes a fresh tree, so a
re-download REPLACES rather than tops up: a run that captures less than the last one silently loses
files. Measured on one: arabic's own character build went from 35 of 35 to 9 of 35. Point this at the
previous copy before you throw it away.

A whole BUILD can go missing, not just files in one, and that is reported separately as `BUILD GONE`.
It has to be, because builds are matched by asset-id set and a build that is simply not here has no
fingerprint to compare — so every file in it would go unreported. moon-girl's re-download dropped its
entire moon-base release, 62 assets and the two environment things composed from them, while gaining
12 files elsewhere; the fix is to copy that one directory across rather than to restore the capture.

Builds are matched by their ASSET-ID SET rather than by directory name, because the same build appears
under many captures — the shared props library is in fifteen of them — and the useful comparison is
between two copies of one build, wherever they sit.

**A declared VARIANT counts as present.** The engine asks for the compressed form, so a browsed capture
holds `Agnes_2_Diffuse.basis` and never the `.png` the registry names. Counting that as missing is how
an earlier version of this reported nancy as having lost her skin textures when her download was
complete and the only thing that had not run was the Basis pre-pass.

Only assets that a converter actually consumes are counted; see `USED`. Scripts, fonts and stylesheets
are page furniture, and an asset with no `file` at all (a material, a render asset, an animation state
graph) is inline in the registry and has nothing to download.
"""

from __future__ import annotations

import argparse
import collections
import os
import pathlib
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure.playcanvas import find_builds, read_build                            # noqa: E402

#: Asset types the pipeline reads. `animation` and `audio` are in because a figure's clips and their
#: voices are the whole point of a capture; `json` is in because the newer builds state their
#: clip-to-sound pairing, playback speeds and bone layers in `position_N_config.json`.
USED = ("container", "texture", "audio", "animation", "json", "cubemap", "template")


def survey(root: str) -> dict:
    """`{build fingerprint: (label, {asset id: (name, type, present)})}` for one capture."""
    out = {}
    for build_root in find_builds(root):
        try:
            build = read_build(build_root)
        except Exception as exc:                                    # noqa: BLE001
            print(f"    ! {build_root}: {exc}")
            continue
        assets = {}
        for aid, asset in build.assets.items():
            if asset.get("type") not in USED:
                continue
            if not ((asset.get("file") or {}).get("url") or ""):
                continue                                            # inline in the registry
            # A DECLARED VARIANT SATISFIES THE ASSET. The engine asks for the compressed form, so a
            # browsed capture holds `Agnes_2_Diffuse.basis` and never the `.png` the registry names —
            # and counting that as missing is how this tool reported nancy as having lost her skin
            # when her download was complete and only the Basis pre-pass had not run.
            here = False
            for candidate in [(asset.get("file") or {}).get("url")] + [
                    v.get("url") for v in ((asset.get("file") or {}).get("variants") or {}).values()]:
                if not candidate:
                    continue
                full = os.path.join(build.root, candidate)
                if os.path.exists(full) and os.path.getsize(full) > 0:
                    here = True
                    break
            if not here:                                        # also accept the decoded `.png`
                path = build.path(aid)
                here = bool(path and os.path.exists(path) and os.path.getsize(path) > 0)
            assets[int(aid)] = (asset.get("name") or str(aid), asset.get("type"), here)
        if assets:
            label = os.path.basename(build_root.rstrip("/")) or os.path.basename(root.rstrip("/"))
            out[frozenset(assets)] = (label, assets)
    return out


def captures_under(path: str, each: bool) -> list[str]:
    """One capture, or each child treated as one. Told rather than guessed.

    Guessing got it wrong: a capture's builds may sit at the root (`arabic/config.json` plus three
    nested releases) or only in subdirectories (`nancy/scenes/…` and `nancy/start/…`), and no rule
    distinguishes the second from a directory OF captures without also splitting nancy in two.
    """
    if not each:
        return [path]
    return [os.path.join(path, name) for name in sorted(os.listdir(path))
            if os.path.isdir(os.path.join(path, name)) and find_builds(os.path.join(path, name))]


#: The sibling repo that turns a capture into a runnable app, and therefore the only thing that can
#: say whether one boots. Resolved relative to this checkout, since the two live side by side.
UNPACK = pathlib.Path(__file__).resolve().parent.parent.parent / "playcanvas-unpack"


def boot_report(path: str, each: bool = False) -> None:
    """Can these captures BOOT — asked by shelling out to `pcunpack check`.

    Everything above answers one question: did the download finish. It reads `config.json`, which is a
    complete manifest of what a build LOADS, and reports every asset that did not arrive.

    It cannot answer the other question. An app needs `index.html`, the engine, and five `__*.js`
    bootstrap files — none of which are assets, so none of which appear in any manifest, so none of
    which this script can see missing. Measured across twenty captures: **six had `config.json` and
    every texture and no app code at all, and not one reported a problem.** Meanwhile `index.html`
    landed on disk 143 times by accident, misfiled under texture names, because a single-page app
    answers a 404 with its own shell.

    "The download finished" and "this can run" are different questions, and only one was being asked.

    Shelled out to rather than reimplemented, deliberately. The list of required files is a property of
    the thing that CONSUMES a capture, and `playcanvas-unpack` is where it is already measured, tested
    and kept current — `docs/capture-format.md` there is the contract. A second copy of the list here
    would be a second thing to be wrong, and the failure mode of a stale copy is the one this whole
    check exists to remove: a stage that reports success it has not earned.
    """
    if not (UNPACK / "pcunpack").is_dir():
        print(f"\nboot check skipped — no {UNPACK} beside this checkout.")
        print("  Without it, 'complete' above means every ASSET arrived and nothing about whether the")
        print("  app can start: the engine and the five __*.js files are in no manifest.")
        return
    # ABSOLUTE, and the captures rather than their parent. It runs with `cwd=UNPACK`, so a relative
    # path resolves against the wrong directory — and `--each` means the children are the captures, so
    # handing over the parent asks `check` about a directory that holds no build of its own.
    root = pathlib.Path(path).resolve()
    targets = ([str(d) for d in sorted(root.iterdir()) if d.is_dir()] if each else [str(root)])
    if not targets:
        return
    print("\ncan it boot?  (pcunpack check — the assets above are a different question)")
    try:
        done = subprocess.run([sys.executable, "-m", "pcunpack", "check", "--brief", *targets],
                              cwd=UNPACK, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:        # noqa: BLE001 — never the main report
        print(f"  could not run it: {exc}")
        return
    for line in (done.stdout or "").splitlines():
        print(f"  {line}")
    if done.returncode not in (0, 1):
        # 1 is "something cannot run", which is a finding rather than a failure of the tool.
        for line in (done.stderr or "").splitlines()[:3]:
            print(f"  ! {line}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("path", help="a capture, or a directory of them")
    ap.add_argument("--against", default="",
                    help="a directory of EARLIER copies — reports what this download LOST")
    ap.add_argument("--files", action="store_true", help="name every missing file, not just count it")
    ap.add_argument("--each", action="store_true",
                    help="treat each child directory as its own capture (use for temp/vrh)")
    ap.add_argument("--no-boot", dest="boot", action="store_false",
                    help="skip the boot check (see `boot_report`)")
    args = ap.parse_args()

    if not os.path.isdir(args.path):
        print(f"{args.path} is not a directory")
        return 2
    theirs, theirs_by_capture = {}, {}
    if args.against:
        for other in captures_under(args.against, True):
            seen = survey(other)
            theirs.update(seen)
            # Kept PER CAPTURE as well as flat, because a build can go missing WHOLESALE and the flat
            # map cannot see that: matching is by asset-id set, so a build that is simply not here has
            # no fingerprint to compare and every file in it goes unreported. That is not hypothetical
            # — moon-girl's re-download dropped its entire moon-base release, 62 assets, and this tool
            # said "nothing was lost".
            theirs_by_capture[os.path.basename(other.rstrip("/"))] = seen

    worst = 0
    for capture in captures_under(args.path, args.each):
        name = os.path.basename(capture.rstrip("/"))
        builds = survey(capture)
        if not builds:
            print(f"\n{name}: no published build here")
            continue
        missing = sum(1 for _fp, (_l, a) in builds.items() for _n, _t, ok in a.values() if not ok)
        total = sum(len(a) for _fp, (_l, a) in builds.items())
        print(f"\n{name}: {total - missing} of {total} present"
              + (f", {missing} MISSING" if missing else " — complete"))
        for fp, (label, assets) in sorted(theirs_by_capture.get(name, {}).items(),
                                          key=lambda kv: -len(kv[1][1])):
            if fp in builds:
                continue
            had = sum(1 for _n, _t, ok in assets.values() if ok)
            print(f"   {label[:24]:26} BUILD GONE — the earlier copy had it, {had} of {len(assets)} "
                  f"present. Copy the directory across.")
            worst += had
        for fp, (label, assets) in sorted(builds.items(), key=lambda kv: -len(kv[1][1])):
            gaps = collections.Counter(t for _n, t, ok in assets.values() if not ok)
            lost = []
            if fp in theirs:
                _l, before = theirs[fp]
                lost = [assets[aid][0] for aid in assets
                        if not assets[aid][2] and before.get(aid, ("", "", False))[2]]
            flag = f"   LOST {len(lost)}" if lost else ""
            print(f"   {label[:24]:26} {len(assets) - sum(gaps.values()):4}/{len(assets):4}"
                  f"  {dict(gaps) or '-'}{flag}")
            worst += len(lost)
            if args.files and gaps:
                for aid, (nm, kind, ok) in sorted(assets.items(), key=lambda kv: kv[1][1]):
                    if not ok:
                        print(f"        missing {kind:10} {nm}")
            for nm in lost[:8]:
                print(f"        lost    {nm}")
            if len(lost) > 8:
                print(f"        … and {len(lost) - 8} more")
    if theirs:
        print(f"\n{worst} file(s) the earlier copy had and this one does not"
              if worst else "\nnothing was lost against the earlier copy")
    if args.boot:
        boot_report(args.path, each=args.each)
    return 1 if worst else 0


if __name__ == "__main__":
    sys.exit(main())
