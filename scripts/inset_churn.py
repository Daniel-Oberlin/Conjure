#!/usr/bin/env python3
"""Read the day's surface churn out of the geometry event log, and say whether the fixes are working.

(Still named for insets, which is where it started and where most churn lives. It now also reports the
wall assignment and the server-side prune guards, so it is the whole picture for one day.)

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

Three fixes landed 2026-09-24, and each leaves its own trace here:

  churn.pair_held         a door face's absence was excused because its twin was visible — the doorway
                          guard doing its job. Every one of these is a surface that would have been pruned.
  churn.assign_contested  two planes wanted one reference and the cheaper took it. Not an error; it is the
                          audit trail for a wrong match, which emits nothing else.
  seed.prune_refused      the server declined a destructive post (unvoted capturer, or >50% of a space).
  seed.merge_forced       the unvoted guard stood down after _MERGE_PATIENCE captures without a vote.

A settled space emits nothing at all, so an empty report is also what "no churn today" looks like — check
the CLIENT BUILD line before concluding anything.
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

    print(f"=== surface churn — {day} ===")
    print(f"  {len(churn)} churn events, {len(events)} with a verdict; "
          f"{len(insets)} inset, {len(others)} wall/horizontal")

    # --- STALE CLIENT ---------------------------------------------------------------------------
    # Checked two ways, because the second one only works if churn happened. The FIRST is definitive and
    # always available: every space.enter carries `build`, the newest client-script mtime the browser
    # actually loaded, and that can be compared against the files on disk. Absence of a symptom is not
    # evidence of a current client — a clean session and a stale one both log nothing.
    builds = {e["build"] for e in rows if e.get("ev") == "space.enter" and e.get("build")}
    disk = 0
    cdir = ROOT / "client"
    if cdir.is_dir():
        disk = max((int(f.stat().st_mtime) for f in cdir.glob("*.js")), default=0)
    print()
    if not builds:
        # No stamp. That does NOT mean stale: every log written before the stamp itself shipped lacks it.
        # Fall back to the one comparison that is always valid — when the session ran, against when the
        # client files were last changed. If the files are newer, the session did not test today's code,
        # whatever else was true of it.
        entered = [e for e in rows if e.get("ev") == "space.enter" and e.get("t")]
        if not entered:
            print("  CLIENT BUILD: unknown (no space.enter today — the headset never entered AR).")
        else:
            first = min(e["t"] for e in entered)
            try:
                ran = _dt.datetime.fromisoformat(first)
            except ValueError:
                ran = None
            print("  CLIENT BUILD: not stamped — this log predates the build stamp, which is NOT by itself")
            print("  evidence of a stale client.")
            if ran and disk:
                newest = _dt.datetime.fromtimestamp(disk)
                if newest > ran:
                    print(f"  But the session ran at {ran:%H:%M:%S} and client/ was last changed at "
                          f"{newest:%H:%M:%S},")
                    print("  so it did not exercise the current code. Re-run after a deploy to get a stamped log.")
                else:
                    print(f"  The session ran at {ran:%H:%M:%S}, after the last change to client/ "
                          f"({newest:%H:%M:%S}),")
                    print("  so it did run code current at the time — assuming the deploy ritual was followed.")
    elif disk and max(builds) < disk:
        old = _dt.datetime.fromtimestamp(max(builds)).strftime("%H:%M:%S")
        new = _dt.datetime.fromtimestamp(disk).strftime("%H:%M:%S")
        print(f"  *** STALE CLIENT — the headset ran a build from {old}, disk has {new} ***")
        print("  Everything below describes code that is no longer what you are testing.")
        print("  Restart the server → restart the Quest browser → reload the page.")
    else:
        when = _dt.datetime.fromtimestamp(max(builds)).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  client build: {when} — current with the files on disk. Verdicts below are trustworthy.")
    if len(builds) > 1:
        print(f"  (note: {len(builds)} different builds entered today — events may be mixed)")

    # The second check: an inset reporting a WALL gate. Only possible from a client predating the dispatch
    # fix, since the two vocabularies are disjoint. Redundant when the build stamp is present, and the
    # only signal when it is not.
    stale = [e for e in insets if e.get("gate") in WALL_GATES]
    if stale:
        print()
        print("  *** STALE CLIENT — STOP ***")
        print(f"  {len(stale)} inset events report a WALL gate ({sorted({e.get('gate') for e in stale})}).")
        print("  Insets are matched by matchInset and cannot fail a matchWall gate, so the headset is")
        print("  running a cached client from before the dispatch fix. Nothing below is trustworthy.")
        print("  A plain reload does NOT clear it — the Quest serves cached /static JS and even no-store")
        print("  HTML across reloads. The ritual that works (docs/running.md Part 3):")
        print("      restart the server  →  restart the Quest browser  →  reload the page")
        print("  Then capture again.")
        print()
    elif insets and not any(e.get("gate") in INSET_GATES or e.get("why") == "device" for e in insets):
        print("\n  (no recognised inset gate — check the client is current)")

    # --- what the fixes actually did ---------------------------------------------------------------
    held = [e for e in churn if e.get("ev") == "churn.pair_held"]
    contested = [e for e in churn if e.get("ev") == "churn.assign_contested"]
    refused = [e for e in rows if e.get("ev") == "seed.prune_refused"]
    forced = [e for e in rows if e.get("ev") == "seed.merge_forced"]
    if held or contested or refused or forced:
        print("\n=== what the guards did ===")
    if held:
        pairs = collections.Counter((e.get("id"), e.get("twin")) for e in held)
        print(f"  doorway guard: {len(held)} face(s) spared — each would have been pruned before")
        for (a, b), n in pairs.most_common(6):
            print(f"      {a} held while {b} was visible" + (f" (x{n})" if n > 1 else ""))
    if refused:
        print(f"  prune refused: {len(refused)} destructive post(s) declined")
        for e in refused:
            print(f"      {e.get('why')} — {e.get('kept')} of {e.get('have')} surfaces kept")
    if forced:
        print(f"  *** the unvoted guard STOOD DOWN {len(forced)} time(s) after "
              f"{forced[0].get('after')} captures without a vote.")
        print("      Expected for a CLI or desktop capture source. From a headset it means selection")
        print("      never completed, which is worth understanding — that is the 2026-09-09 window.")
    if contested:
        print(f"  assignment: {len(contested)} plane(s) did not get their first-choice reference")
        for e in contested[:6]:
            print(f"      {e.get('sem')}: wanted {e.get('wanted')}, got {e.get('got') or 'a new id'}")
        print("      Not an error — two planes wanted one reference and the cheaper took it. Worth a look")
        print("      only if the same pair contests every capture, which would mean the geometry is")
        print("      genuinely ambiguous rather than the order being unlucky.")

    if not insets:
        print("\n  No inset churn today. That is the goal state — but it is also what an unexercised")
        print("  session looks like, and what a session on stale code looks like. Read it together with")
        print("  the CLIENT BUILD line above, then walk between rooms past the doorways before concluding.")
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
