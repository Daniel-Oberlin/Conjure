#!/usr/bin/env python3
"""Read the day's per-room registration residuals out of the geometry event log.

    python scripts/room_residuals.py              # today
    python scripts/room_residuals.py 2026-09-12   # a given day

Answers the question step 2 of the rooms plan exists to ask: how far apart are the rooms, really?
Registration solves ONE rigid transform for a space the Quest maps non-rigidly, so every room but the one
the fit favours carries a residual — §1 measured ~9 cm as a whole-space aggregate, and nobody has seen it
split by room.

Also prints the `level.anomaly` lines beside it, because that is the cross-check: the floating-room
detector and this fit's `dy` estimate the same displacement through entirely separate code paths
(docs/investigations/rooms-device-run-2026-09.md §G5).
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "temp"


def _rows(day: str, ev: str) -> list[dict]:
    p = LOG_DIR / f"geometry-{day}.jsonl"
    if not p.exists():
        print(f"no log for {day} at {p}", file=sys.stderr)
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("ev") == ev:
            out.append(e)
    return out


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else _dt.date.today().isoformat()

    res = _rows(day, "room.residual")
    print(f"=== room.residual — {day} ({len(res)} events) ===")
    if not res:
        print("  none. A settled space emits nothing, so this is also what 'no change since the last")
        print("  reading' looks like — check the log has room.* lines at all before concluding.")
    else:
        print("  %-12s %4s %5s %9s %9s %8s %8s %8s  %s"
              % ("room", "n", "walls", "before", "after", "dx", "dz", "dy", "would_apply"))
        for e in res:
            dy = "-" if e.get("dy") is None else "%.3f" % e["dy"]
            print("  %-12s %4s %5s %9.3f %9.3f %8.3f %8.3f %8s  %s"
                  % (e.get("room", "?"), e.get("n", "?"), e.get("walls", "?"),
                     e.get("before", 0.0), e.get("after", 0.0),
                     e.get("dx", 0.0), e.get("dz", 0.0), dy, e.get("would_apply")))
        worst = max(res, key=lambda e: e.get("before", 0.0))
        print(f"\n  worst room: {worst.get('room')} at {worst.get('before', 0):.3f} m")
        print("  → under ~0.02 m, the effect is real but not worth correcting (drop step 3).")
        print("  → near ~0.09 m, it is eating most of matchWall's 0.15 m identity budget (do step 3).")

    empty = _rows(day, "room.empty")
    if empty:
        print(f"\n=== room.empty — segmentation saw no floor ({len(empty)}) ===")
        for e in empty:
            print(f"  {e.get('n')} surfaces, segmentRooms={e.get('has')}, "
                  f"{e.get('noPose')} without a pose, {e.get('noExtent')} without extent")
            print(f"  semantics: {json.dumps(e.get('sem'))}")

    err = _rows(day, "room.error")
    if err:
        print(f"\n=== room.error — a rooms step THREW ({len(err)} distinct) ===")
        for e in err:
            print(f"  {e.get('where')}: {e.get('msg')}")
        print("  Logged once per distinct failure. Segmentation and the residual probe are additive, so")
        print("  the capture still landed — but anything above means section A of the device run failed.")

    lv = _rows(day, "level.anomaly")
    print(f"\n=== level.anomaly — the independent estimate ({len(lv)} events) ===")
    if not lv:
        print("  none — the floating-room fault was not active during this run.")
        print("  Then every dy above should also be small. If one is not, they disagree; see §G5.")
    else:
        for e in lv:
            print("  " + json.dumps({k: v for k, v in e.items() if k not in ("ev", "ct")}))
        print("\n  §G5: does a room named here match the room with the large dy above, at a similar size?")
        print("  They share no code path, so agreement validates both and disagreement names the room")
        print("  to look at.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
