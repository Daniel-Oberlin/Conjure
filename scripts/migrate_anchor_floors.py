#!/usr/bin/env python3
"""Re-point each stored anchor at the floor UNDER its content.

`authorAnchor` used to choose its reference floor by iteration order, so in a multi-room space every
anchor in the dwelling bound to one arbitrary room's floor (docs/backlogs/spaces-geometry.md). The code is
fixed; anchors authored before the fix keep the floor they were written with, because `solveAnchor` reads
`anchor.floor.id` as stored.

Ordinary use repairs them one at a time — an anchor is re-authored whenever its entity is grabbed and
released — so this only brings that forward.

    python scripts/migrate_anchor_floors.py <user>/<space>            # dry run
    python scripts/migrate_anchor_floors.py <user>/<space> --apply

The footprint test lives in client/space-snap.js and there is no Python port (specs §12), so the
"which floor is under this point" decision is delegated to node. Stop the server first: it autosaves the
active world and would overwrite these edits.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path.home() / ".local/share/conjure/users"

_FLOOR_UNDER_JS = r"""
const THREE = require(process.argv[2] + "/node_modules/three");
const RS = require(process.argv[2] + "/client/space-snap.js");
const inp = JSON.parse(require("fs").readFileSync(process.argv[3], "utf8"));
const D2R = Math.PI / 180;
const RX = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);
// Stored surfaces carry the A-PLANE euler (normal = local +Z); `covers` wants the raw capture quaternion
// (normal = local +Y). Undo eulerYXZ's -90 X, exactly as the offline segmentation probe did.
const floors = inp.floors.map((f) => {
  const r = f.rotation || [0, 0, 0], p = f.position || [0, 0, 0];
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(r[0]*D2R, r[1]*D2R, r[2]*D2R, "YXZ"));
  return { id: f.id, semantic: "floor", extent: f.extent,
           _lp: new THREE.Vector3(p[0], p[1], p[2]), _lq: q.multiply(RX.clone().invert()) };
});
const out = inp.points.map((pt) => {
  const f = RS.floorUnder(THREE, floors, pt[0], pt[1]);
  return f ? f.id : null;
});
console.log(JSON.stringify(out));
"""


def floor_under(floors: list[dict], points: list[list[float]]) -> list[str | None]:
    if not points:
        return []
    tmp = ROOT / ".migrate-in.json"
    js = ROOT / ".migrate-floor.js"
    tmp.write_text(json.dumps({"floors": floors, "points": points}))
    js.write_text(_FLOOR_UNDER_JS)
    try:
        r = subprocess.run(["node", str(js), str(ROOT), str(tmp)],
                           capture_output=True, text=True, check=True)
        return json.loads(r.stdout)
    finally:
        tmp.unlink(missing_ok=True)
        js.unlink(missing_ok=True)


def main() -> int:
    if len(sys.argv) < 2 or "/" not in sys.argv[1]:
        print(__doc__)
        return 2
    owner, space = sys.argv[1].split("/", 1)
    apply = "--apply" in sys.argv

    sp = DATA / owner / "spaces" / f"{space}.json"
    if not sp.exists():
        print(f"no space at {sp}")
        return 1
    doc = json.loads(sp.read_text())
    floors = [{"id": s["id"],
               "position": (s.get("transform") or {}).get("position"),
               "rotation": (s.get("transform") or {}).get("rotation"),
               "extent": ((s.get("components") or {}).get("surface") or {}).get("extent"),
               "y": ((s.get("transform") or {}).get("position") or [0, 0, 0])[1]}
              for s in doc.get("surfaces", [])
              if ((s.get("meta") or {}).get("semantic")) == "floor"]
    if not floors:
        print("space has no floors")
        return 1
    fy = {f["id"]: f["y"] for f in floors}
    # An anchor naming a floor the space no longer has cannot be re-expressed; leave it for re-authoring.
    print(f"{space}: {len(floors)} floors — {', '.join(f['id'] for f in floors)}\n")

    ref = f"{owner}/{space}"
    changed_files = 0
    for wp in sorted((DATA / owner / "agents").rglob("worlds/*.json")):
        w = json.loads(wp.read_text())
        if (w.get("environment") or {}).get("space") not in (ref, space):
            continue
        ents = [e for e in w.get("entities", [])
                if ((e.get("meta") or {}).get("anchor") or {}).get("floor")]
        if not ents:
            continue
        pts = [[(e.get("transform") or {}).get("position", [0, 0, 0])[0],
                (e.get("transform") or {}).get("position", [0, 0, 0])[2]] for e in ents]
        want = floor_under(floors, pts)
        hits = 0
        for e, w_id in zip(ents, want):
            a = e["meta"]["anchor"]
            was = a["floor"].get("id")
            if not w_id or w_id == was or was not in fy:
                continue
            # Re-express the SAME height against the new floor — do not re-derive it from
            # transform.position. The transform is the last solved pose; the anchor was authored earlier,
            # and on this space the two disagree by ~0.5 m. Recomputing from the transform would leave the
            # floor constraint inconsistent with the wall constraints, which are untouched, and move the
            # content. The anchor's own notion of height is `offset + floor.y`; only the reference changes.
            new_off = a["floor"].get("offset", 0.0) + fy[was] - fy[w_id]
            print(f"  {wp.name}  {e['id'][:26]:28s} {was} → {w_id}"
                  f"   offset {a['floor'].get('offset', 0):.4f} → {new_off:.4f}")
            if apply:
                a["floor"] = {"id": w_id, "offset": new_off}
            hits += 1
        if hits and apply:
            wp.write_text(json.dumps(w))
            changed_files += 1
        if hits and not apply:
            changed_files += 1
    verb = "rewrote" if apply else "would rewrite"
    print(f"\n{verb} anchors in {changed_files} world doc(s)"
          + ("" if apply else "   — re-run with --apply"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
