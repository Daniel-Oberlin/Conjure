#!/usr/bin/env python
"""Stage a (clip, figure) pair on disk for the JS harnesses, and print their bone specs.

    python -m scripts.clip_stage LayTableIdle alice grace akari

Everything that measures retargeting runs in node — because a model of the client is not the client
(`scripts/clip_diff.mjs`) — and everything that KNOWS which asset is which runs in python. This is the
seam: it resolves names against the library, writes the clip, each figure, and each RETARGETED clip
into a staging dir, and prints the `bone=node` spec each harness needs from the catalog's humanoid map
rather than from guessing at names.

A retarget is baked into the clip BYTES, so a question about how it was computed cannot be asked of the
result — it has to be re-run. That is why this stages the retargeted clip as a file rather than letting
the harnesses fetch one from the server: the comparison that removed `swing` was two runs of this,
against two builds, with everything else held identical.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conjure.library import AssetLibrary  # noqa: E402
from conjure.server import ASSET_CACHE, LIBRARY_DB  # noqa: E402

SCOPE = "daniel/agents/builder"


def attrs(rec: dict) -> dict:
    try:
        return json.loads(rec.get("attributes") or "{}")
    except (TypeError, ValueError):
        return {}


def spec_for(rec: dict) -> str:
    """The `bone=node` pairs `clip_diff.mjs` wants, from the catalog's own humanoid map."""
    hum = attrs(rec).get("humanoid") or {}
    want = {"hips": "hips", "neck": "neck", "head": "head",
            "lUpLeg": "leftUpperLeg", "rUpLeg": "rightUpperLeg",
            "lFoot": "leftFoot", "rFoot": "rightFoot"}
    have = {k: hum[v] for k, v in want.items() if hum.get(v)}
    return ",".join(f"{k}={v}" for k, v in have.items())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clip", help="clip label or asset id")
    ap.add_argument("figures", nargs="+", help="model labels or asset ids")
    ap.add_argument("--out", default="temp/stage")
    args = ap.parse_args()

    lib = AssetLibrary(LIBRARY_DB)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    def find(name: str, kind: str) -> dict:
        rec = lib.get(name)
        if rec is not None and rec.get("kind") == kind:
            return rec
        hits = lib.query(f"SELECT * FROM assets WHERE kind = '{kind}' AND label = '{name}'",
                         scope=SCOPE, limit=50) or []
        if not hits:
            hits = lib.query(f"SELECT * FROM assets WHERE kind = '{kind}' "
                             f"AND label LIKE '%{name}%'", scope=SCOPE, limit=50) or []
        if len(hits) != 1:
            raise SystemExit(f"{name!r} matches {len(hits)} {kind}s: "
                             + ", ".join(f"{h['id']} {h.get('label')!r}" for h in hits[:8]))
        return hits[0]

    clip = find(args.clip, "animation")
    clip_bytes = (ASSET_CACHE / clip["id"]).read_bytes()
    (out / "clip.glb").write_bytes(clip_bytes)
    print(f"clip   {clip['id']}  {clip.get('label')!r}  rig {attrs(clip).get('rig_sig')}")
    print(f"       {out / 'clip.glb'}")

    for name in args.figures:
        fig = find(name, "model")
        fig_bytes = (ASSET_CACHE / fig["id"]).read_bytes()
        slug = name.replace("/", "-")
        (out / f"{slug}.glb").write_bytes(fig_bytes)
        sig = attrs(fig).get("rig_sig")
        print(f"\nfigure {fig['id']}  {fig.get('label')!r}  rig {sig}")
        print(f"       {out / f'{slug}.glb'}")
        print(f"       {spec_for(fig)}")
        if sig == attrs(clip).get("rig_sig"):
            print("       plays NATIVELY — no retarget")
            continue
        from conjure.retarget import retarget_clip
        why: list[str] = []
        done = retarget_clip(clip_bytes, fig_bytes, why.append)
        if done is None:
            print(f"       cannot retarget: {'; '.join(why)}")
            continue
        (out / f"{slug}-clip.glb").write_bytes(done.data)
        print(f"       {out / f'{slug}-clip.glb'}  {done.bones} bones, "
              f"{done.dropped} dropped, {done.slid} slid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
