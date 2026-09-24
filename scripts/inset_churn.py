#!/usr/bin/env python3
"""Read the day's INSET churn out of the geometry event log, and settle two open questions.

    python scripts/inset_churn.py              # today
    python scripts/inset_churn.py 2026-09-25   # a given day

Insets — doors, windows, wall art — are 69% of all recorded surface churn, and until 2026-09-24 not one
event of it was validly diagnosed: `explainNoMatch` answered in `matchWall`'s terms (facing, perpendicular
offset, along-line overlap) for surfaces that are matched by `matchInset` (host wall + along-slot). This
script reads the replacement gates, and exists to answer the two questions that could not be separated
before (`docs/investigations/surface-churn.md`):

  Q1  Why do doors churn — because their WALL churned, or because the two faces of one doorway compete?
      A doorway is captured as two planes, one per room. Either mechanism produces the same alternation in
      the old log. Now: `nohost` means the host wall went missing (cascade); `outbid` means a rival
      candidate took the slot (competition). They are different repairs.

  Q2  Is one face's EXTENT unstable? One side of a doorway can segment the architrave rather than the door
      — about twice as wide (1.36 m vs 0.71 m, observed in space-1). A width that flips between captures
      drags the along-slot with it, which would cause churn with no competition involved at all. `rw`/`cw`
      (ref width, candidate width) make that visible.

A settled space emits nothing at all, so an empty report is also what "no churn today" looks like — check
the STALE CLIENT warning below before concluding the fix works.
"""

from __future__ import annotations

import collections
import datetime as _dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "temp"

INSET_SEMS = {"door", "window", "wall art"}
INSET_GATES = {"nokey", "nohost", "norecon", "along", "outbid", "taken"}
WALL_GATES = {"dyaw", "perp", "gap", "claimed"}

GATE_MEANS = {
    "nokey": "no host wall or anchor — matchInset could never key it",
    "nohost": "its host wall is not in this capture  ← CASCADE",
    "norecon": "wall present, slot could not be reconstructed (corners missing)",
    "along": "a candidate exists but its slot is past the 0.4 m tolerance",
    "outbid": "reachable, but a nearer slot took the candidate  ← COMPETITION",
    "taken": "(mint side) the slot it wanted was already held  ← COMPETITION",
    "device": "nothing of that semantic detected on that wall — genuinely new/absent",
}


def _rows(day: str) -> list[dict]:
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
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else _dt.date.today().isoformat()
    rows = _rows(day)
    churn = [e for e in rows if str(e.get("ev", "")).startswith("churn.")]
    events = [e for e in churn if e.get("ev") in ("churn.mint", "churn.miss")]
    insets = [e for e in events if e.get("sem") in INSET_SEMS]
    others = [e for e in events if e.get("sem") not in INSET_SEMS]

    print(f"=== inset churn — {day} ===")
    print(f"  {len(churn)} churn events, {len(events)} with a verdict; "
          f"{len(insets)} inset, {len(others)} wall/horizontal")

    # --- STALE CLIENT ---------------------------------------------------------------------------
    # The headset caches the client aggressively. An inset reporting a WALL gate means the running
    # client predates the dispatch fix, and every inset verdict below is the old, invalid kind.
    stale = [e for e in insets if e.get("gate") in WALL_GATES]
    if stale:
        print()
        print("  *** STALE CLIENT — STOP ***")
        print(f"  {len(stale)} inset events report a WALL gate ({sorted({e.get('gate') for e in stale})}).")
        print("  Insets are matched by matchInset and cannot fail a matchWall gate, so the headset is")
        print("  running a cached client from before the dispatch fix. Nothing below is trustworthy.")
        print("  Force-reload in the headset (or restart the browser) and capture again.")
        print()
    elif insets and not any(e.get("gate") in INSET_GATES or e.get("why") == "device" for e in insets):
        print("\n  (no recognised inset gate — check the client is current)")

    if not insets:
        print("\n  No inset churn today. That is the goal state, but it is also what an unexercised")
        print("  session looks like — walk between rooms past the doorways before concluding.")
    else:
        # --- Q1: cascade or competition? --------------------------------------------------------
        by_gate = collections.Counter(e.get("gate") or f"({e.get('why')})" for e in insets)
        print("\n  --- gates ---")
        for g, n in by_gate.most_common():
            print("    %-10s %3d   %s" % (g, n, GATE_MEANS.get(g, "")))

        cascade = by_gate.get("nohost", 0)
        compete = by_gate.get("outbid", 0) + by_gate.get("taken", 0)
        print("\n  --- Q1: why do insets churn? ---")
        print(f"    cascade (nohost):        {cascade}")
        print(f"    competition (outbid/taken): {compete}")
        if cascade or compete:
            if cascade > 2 * max(compete, 1):
                print("    → CASCADE dominates. Insets are dying because their walls churn; fix the wall")
                print("      assignment first and most of this disappears. Check which walls:")
                walls = collections.Counter(e.get("near") for e in insets if e.get("gate") == "nohost")
                for w, n in walls.most_common(5):
                    print(f"        {w}: {n}")
            elif compete > 2 * max(cascade, 1):
                print("    → COMPETITION dominates. The two faces of a doorway are fighting over one")
                print("      observation; recognise the pair (antiparallel + through-wall offset +")
                print("      overlapping spans, NOT centroid or extent) rather than touching the walls.")
            else:
                print("    → MIXED, or too few events. Both mechanisms are live; get more data before")
                print("      choosing, since the two lead to different fixes.")

        # --- Q2: is an extent flipping? ---------------------------------------------------------
        print("\n  --- Q2: is a face's extent unstable? ---")
        widths = [e for e in insets if e.get("rw") is not None and e.get("cw") is not None]
        if not widths:
            print("    no rw/cw pairs recorded (they appear only when a candidate was found).")
        else:
            big = [e for e in widths if max(e["rw"], e["cw"]) / max(min(e["rw"], e["cw"]), 1e-6) >= 1.4]
            print(f"    {len(widths)} events carry both widths; {len(big)} disagree by ≥1.4x")
            for e in big[:8]:
                print("      %-22s ref %.2f m vs candidate %.2f m  (%.2fx)  gate=%s"
                      % (e.get("id") or e.get("near"), e["rw"], e["cw"],
                         max(e["rw"], e["cw"]) / min(e["rw"], e["cw"]), e.get("gate")))
            if big:
                print("    → An architrave is being segmented as the door on one side. Whether it FLIPS is")
                print("      the question: see the per-id width history below.")
        # per-id width history — a width that CHANGES between captures is the flip
        hist: dict[str, list[float]] = collections.defaultdict(list)
        for e in insets:
            k = e.get("id") or e.get("near")
            if k and e.get("cw") is not None:
                hist[k].append(e["cw"])
        flips = {k: v for k, v in hist.items() if len(set(round(x, 2) for x in v)) > 1}
        if flips:
            print("\n    WIDTHS THAT CHANGED BETWEEN CAPTURES (the flip — Q2 is YES for these):")
            for k, v in list(flips.items())[:8]:
                print(f"      {k}: {[round(x, 2) for x in v]}")
        elif hist:
            print("\n    Every candidate width was stable across captures → Q2 is NO; the extent is not")
            print("    flipping, so competition (Q1) is the mechanism to fix.")

    # --- context: the other two matchers ----------------------------------------------------------
    if others:
        print("\n=== wall / horizontal churn, for context ===")
        for g, n in collections.Counter(e.get("gate") or f"({e.get('why')})" for e in others).most_common():
            print("    %-10s %3d" % (g, n))
        print("    (`claimed` here is matchWall's: every tolerance passed and another plane took the ref)")

    pruned = [e for e in churn if e.get("ev") == "churn.prune"]
    if pruned:
        styled = [e for e in pruned if e.get("color") and e.get("color") != "#888"]
        print(f"\n=== cost: {len(pruned)} surfaces pruned, {len(styled)} carrying a non-default colour ===")
        for e in styled:
            print("    %-24s %-10s %s%s" % (e.get("id"), e.get("sem"), e.get("color"),
                                            "  (anchored — server keeps it)" if e.get("anchored") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
