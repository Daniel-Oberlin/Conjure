#!/usr/bin/env python3
"""Find — and optionally repair — content left pointing at surfaces the space no longer has.

Between 2026-09-29 and 2026-10-08 a broken settle gate let ids minted on a bad frame into the space
record (docs/specs/spaces-geometry.md §4.0c). They could never be matched again, and the churn that
eventually pruned them took genuine styled surfaces with it. The gate is fixed; this is the mess it left.

Three findings, and only one of them is safely repairable by a script:

  stranded anchors   an anchor names walls (or a floor) that are gone. `solve_anchor` needs TWO walls,
                     so an anchor down to one is not degraded, it is DEAD — the content lands wherever
                     one plane and a floor imply, which is how a figure ends up in another room.
                     REPAIRABLE: re-author from the content's current pose against the walls that exist,
                     which is exactly what a grab-and-release would do (`plane_anchor.author_anchor`).

  stranded surfaces  an image pinned with `meta.on_surface` to a wall-art surface that is gone. NOT
                     repairable here: the right host is a visual judgement about which picture belongs on
                     which wall, and guessing would hang it somewhere plausible and wrong.

  lost styling       colours destroyed with a pruned surface, recovered from the geometry log. NOT
                     repairable: the colour is known, the surface it belonged to no longer exists under
                     any id, and matching it to whatever replaced it needs the room in front of you.
                     Reported so it can be re-applied deliberately.

    python scripts/repair_stranded.py <user>/<space>              # report only
    python scripts/repair_stranded.py <user>/<space> --reanchor    # + re-author the dead anchors

Stop the server first: it autosaves the active world and would overwrite these edits. Every file written
is backed up alongside first.
"""

from __future__ import annotations

import glob
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = pathlib.Path.home() / ".local/share/conjure/users"
sys.path.insert(0, str(ROOT))

from conjure.plane_anchor import Quat, Vec3, author_anchor  # noqa: E402


def _planes(space: dict) -> list:
    """The space's real surfaces in the form `author_anchor` reads: `{id, kind, normal, point}`, walls and
    floors only, since those are the two kinds an anchor is written against.

    The normal is the entity's local **+Z** carried through its stored rotation — an `a-plane` faces +Z, and
    the client derives it the same way (`SpaceSnap.surfaceToRef`). Read as YXZ and not XYZ: that is the
    order the client renders and stores in, and reading it back as XYZ corrupts any tilted surface.
    """
    out = []
    for e in space.get("surfaces", []):
        m = e.get("meta") or {}
        sem = m.get("semantic")
        if sem not in ("wall", "floor"):
            continue
        t = e.get("transform") or {}
        pos = t.get("position")
        if not pos:
            continue
        q = Quat(*_quat(t.get("rotation") or [0, 0, 0]))
        n = _rot(q, Vec3(0.0, 0.0, 1.0))
        out.append({"id": e["id"], "kind": sem, "normal": [n.x, n.y, n.z], "point": list(pos)})
    return out


def _rot(q: "Quat", v: "Vec3") -> "Vec3":
    """`q * v * q⁻¹` — the one piece of quaternion arithmetic this script needs that the port does not
    already expose."""
    qv = Quat(v.x, v.y, v.z, 0.0)
    r = q.multiply(qv).multiply(q.clone().invert())
    return Vec3(r.x, r.y, r.z)


def _lost_styling(limit_days: int = 30) -> dict:
    """Colours that went with a pruned surface, read back out of the geometry log. The log is the only
    record: the space document keeps no history, and the entity it belonged to is gone."""
    out = {}
    for f in sorted(glob.glob(str(ROOT / "temp" / "geometry-*.jsonl")))[-limit_days:]:
        day = pathlib.Path(f).stem.replace("geometry-", "")
        for line in open(f):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("ev") == "seed.prune" and d.get("styled"):
                out[d["id"]] = (day, d.get("color"))
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    reanchor = "--reanchor" in sys.argv
    if not args:
        print(__doc__)
        return 2
    user, _, name = args[0].partition("/")
    spath = DATA / user / "spaces" / f"{name}.json"
    if not spath.exists():
        print(f"no space at {spath}")
        return 1
    space = json.loads(spath.read_text())
    present = {e["id"] for e in space.get("surfaces", [])}
    planes = _planes(space)
    print(f"{args[0]}: {len(present)} surfaces, {len(planes)} of them walls/floors\n")

    # ---- stranded anchors and on_surface pins, across every world that references this space ----
    dead, degraded, pinned, fixed = [], [], [], 0
    for wf in sorted(glob.glob(str(DATA / user / "agents/*/sessions/*/worlds/*.json"))):
        w = json.loads(pathlib.Path(wf).read_text())
        changed = False
        for e in w.get("entities", []):
            m = e.get("meta") or {}
            if m.get("real"):
                continue
            host = m.get("on_surface")
            if host and host not in present:
                pinned.append((e["id"], host, wf))
            a = m.get("anchor")
            if not a:
                continue
            walls = [x.get("id") for x in (a.get("walls") or [])]
            live = [x for x in walls if x in present]
            fl = (a.get("floor") or {}).get("id")
            if len(live) == len(walls) and (not fl or fl in present):
                continue
            row = (e["id"], [x for x in walls if x not in present], fl if fl and fl not in present else None, wf)
            (dead if len(live) < 2 else degraded).append(row)
            if not reanchor:
                continue
            t = e.get("transform") or {}
            pos, rot = t.get("position"), t.get("rotation") or [0, 0, 0]
            if not pos:
                continue
            # The same call a grab-and-release makes. `mode` is kept as stored — a grounded figure must
            # not silently become free-floating because its walls churned.
            try:
                fresh = author_anchor({"position": pos, "quaternion": _quat(rot),
                                       "mode": (m.get("placement") or a.get("mode") or "grounded")}, planes)
            except Exception as exc:  # noqa: BLE001 — a bad anchor must not stop the rest being repaired
                print(f"  ! {e['id']}: could not re-author ({exc})")
                continue
            if not (fresh.get("walls") and len(fresh["walls"]) >= 2):
                print(f"  ! {e['id']}: the space has too few walls near it to re-author safely")
                continue
            m["anchor"] = fresh
            changed = True
            fixed += 1
        if changed and reanchor:
            p = pathlib.Path(wf)
            bak = p.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            bak.write_text(p.read_text())
            p.write_text(json.dumps(w, indent=1))

    def _show(rows, label):
        print(f"{label}: {len(rows)}")
        for eid, missing, floor, wf in rows:
            where = pathlib.Path(wf).stem
            bits = f"walls gone {missing}" + (f", floor gone {floor}" if floor else "")
            print(f"  {eid[:26]:28} {bits}   [{where}]")

    _show(dead, "DEAD anchors (under two walls left — the content cannot be placed)")
    print()
    _show(degraded, "degraded anchors (still solvable on the walls that remain)")
    print()
    print(f"images pinned to a missing surface: {len(pinned)}   (not repairable here)")
    for eid, host, wf in pinned:
        print(f"  {eid[:26]:28} on_surface {host}   [{pathlib.Path(wf).stem}]")

    lost = _lost_styling()
    print(f"\nstyling destroyed with a pruned surface: {len(lost)}   (re-apply by hand)")
    for sid, (day, col) in sorted(lost.items()):
        print(f"  {sid:20} {str(col):10} lost {day}")

    print()
    if reanchor:
        print(f"re-authored {fixed} anchor(s); each world backed up alongside before writing.")
    elif dead or degraded:
        print(f"{len(dead) + len(degraded)} anchor(s) repairable — re-run with --reanchor")
    return 0


def _quat(rot_deg):
    """A-Frame euler degrees (YXZ) → quaternion [x, y, z, w]. YXZ and not XYZ: the client renders and
    stores in that order, and reading it back as XYZ corrupts any multi-axis rotation."""
    import math
    rx, ry, rz = (math.radians(v) for v in rot_deg)
    cx, sx = math.cos(rx / 2), math.sin(rx / 2)
    cy, sy = math.cos(ry / 2), math.sin(ry / 2)
    cz, sz = math.cos(rz / 2), math.sin(rz / 2)
    # YXZ: q = qy * qx * qz
    qy = (0.0, sy, 0.0, cy)
    qx = (sx, 0.0, 0.0, cx)
    qz = (0.0, 0.0, sz, cz)
    def mul(a, b):
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return (aw * bx + ax * bw + ay * bz - az * by,
                aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw,
                aw * bw - ax * bx - ay * by - az * bz)
    return list(mul(mul(qy, qx), qz))


if __name__ == "__main__":
    raise SystemExit(main())
