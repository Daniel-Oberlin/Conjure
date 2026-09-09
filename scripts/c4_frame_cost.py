#!/usr/bin/env python3
"""C4 of the figures device run — does posing a figure cost frames? Hands-free.

    python scripts/c4_frame_cost.py                 # the full A/B, ~3 minutes
    python scripts/c4_frame_cost.py --still 20 --walk 30

**The problem this solves.** The measurement needs two matched phases — the same figures unposed, then
posed, standing still and then walking the same path — and the phase boundaries have to be findable in
the log afterwards. You cannot type a marker while wearing a headset, and a phase boundary guessed from
wall-clock later is worth very little.

So this script drives it: it **speaks** each instruction through macOS `say` (audible in the headset if
the Mac is your audio out, and audible in the room regardless), writes an exact marker into
`temp/conjure.log` at every boundary via `/client_log`, and poses the figures itself between the phases.
You put the headset on, start it, and do what it tells you.

Afterwards it prints the log window to hand over. Nobody has to read a `PACE` line by hand.

**Prerequisites:** the server running with `--debug-jitter` (the `PACE` lines come from that probe), and
you in AR with the figures in view. It refuses to start if the probe is not live, because a run with no
`PACE` lines is worse than no run — it looks like a result.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = "http://localhost:8080"

#: The posed phase. One lowered pose, one arm pose, one deep pose — spread across the figures present so
#: the comparison is not about a single pose's cost. `stand` is the baseline for every figure.
POSED = ("kneel", "cheer", "crouch", "sit")


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(f"{URL}{path}", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {URL} — is the server running? ({exc})")


def get(path: str) -> dict:
    with urllib.request.urlopen(f"{URL}{path}", timeout=10) as r:
        return json.loads(r.read())


def mark(msg: str) -> None:
    """A marker line in the dev log, so the phase boundary is exact rather than reconstructed."""
    post("/client_log", {"tag": "c4", "msg": msg})
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}")


def speak(text: str) -> None:
    print(f"  >>> {text}")
    try:
        subprocess.run(["say", text], timeout=20)
    except Exception:                                    # noqa: BLE001 — no `say`, no problem
        pass


def figures() -> list[str]:
    return [e["id"] for e in get("/world").get("entities", [])
            if (e.get("meta") or {}).get("rigged")]


def pose_all(ids: list[str], names) -> None:
    for i, fid in enumerate(ids):
        name = names[i % len(names)] if isinstance(names, tuple) else names
        out = post("/figure", {"id": fid, "named": name})
        print(f"    {fid:<18} {name:<7} {'ok' if out.get('ok') else 'FAILED: ' + str(out.get('error'))}")


def phase(label: str, still: int, walk: int) -> None:
    mark(f"PHASE {label} still-start")
    speak(f"{label}. Stand still and look at the figures.")
    time.sleep(still)
    mark(f"PHASE {label} still-end walk-start")
    speak("Now walk your path. Same route both times.")
    time.sleep(walk)
    mark(f"PHASE {label} walk-end")
    speak("Stop.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--still", type=int, default=30, help="seconds standing still per phase")
    ap.add_argument("--walk", type=int, default=40, help="seconds walking per phase")
    ap.add_argument("--skip-check", action="store_true", help="run even if no PACE lines are seen")
    args = ap.parse_args()

    ids = figures()
    if not ids:
        return print("no rigged figures in the world — place some first") or 2
    print(f"figures: {', '.join(ids)}")

    # A run with no PACE lines produces a confident-looking nothing. Refuse it.
    if not args.skip_check:
        try:
            recent = subprocess.run(["tail", "-400", "temp/conjure.log"], capture_output=True,
                                    text=True, timeout=10).stdout
        except Exception:                                # noqa: BLE001
            recent = ""
        if "PACE" not in recent and "RATE" not in recent:
            return print("\nNo PACE/RATE lines in the recent log — the frame probe is not running.\n"
                         "Restart the server with `--debug-jitter` and enter AR, then re-run.\n"
                         "(--skip-check to override.)") or 2

    started = time.strftime("%Y-%m-%d %H:%M:%S")
    speak("Frame cost test. Two phases, about three minutes. Put the headset on and stand where you can "
          "see the figures.")
    time.sleep(6)

    print("\n--- baseline: every figure neutral ---")
    pose_all(ids, "stand")
    time.sleep(3)                                        # let the pose settle before measuring
    phase("BASELINE-unposed", args.still, args.walk)

    print("\n--- posed ---")
    pose_all(ids, POSED)
    time.sleep(3)
    phase("POSED", args.still, args.walk)

    ended = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"\nDone. Window to analyse: {started} → {ended}")
    print("\nHand this to Claude:\n"
          f'  "read C4 from the log, window {started} to {ended}"\n'
          "Or read it yourself with:\n"
          f"  awk '$2>=\"{started[11:]}\" && $2<=\"{ended[11:]}\"' temp/conjure.log "
          "| grep -E 'PACE|\\[c4\\]'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
