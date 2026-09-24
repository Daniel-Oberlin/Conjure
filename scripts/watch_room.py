#!/usr/bin/env python3
"""Print which room each present user is standing in, once a second, with a timestamp.

For the device run's F2: you cannot type a command while wearing the headset, and taking it off ends the
thing being measured. So start this first, walk the house, come back and read it.

    python scripts/watch_room.py           # Ctrl-C to stop
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

BASE = "http://localhost:8080"


def _name(rooms: dict, rid: str) -> str:
    rec = rooms.get(rid) or {}
    return (rec.get("name") or "").strip() or f"room #{rec.get('n', '?')}"


def main() -> int:
    print("watching — walk between rooms, Ctrl-C when done\n")
    last = None
    while True:
        try:
            w = json.load(urllib.request.urlopen(f"{BASE}/room/whereami", timeout=5))
        except Exception as e:                       # server restart mid-walk should not end the watch
            print(f"  {time.strftime('%H:%M:%S')}  (unreachable: {type(e).__name__})")
            time.sleep(1)
            continue
        rooms, standing = w.get("rooms") or {}, w.get("standing") or {}
        now = ", ".join(f"{u} in {_name(rooms, r)}" for u, r in sorted(standing.items())) or "(nobody)"
        mark = "  ← CHANGED" if last is not None and now != last else ""
        print(f"  {time.strftime('%H:%M:%S')}  {now}{mark}")
        last = now
        time.sleep(1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nstopped")
