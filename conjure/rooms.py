"""Room identity — matching a fresh segmentation onto the rooms a space already has.

The client segments the *seed* into rooms geometrically (`SpaceSnap.segmentRooms`); this decides which
of the space's **existing** rooms each of those groups *is*. That is the whole job, and it is pure set
arithmetic — no geometry crosses into Python (docs/specs/spaces-geometry.md §12).

It matters more than it looks. Membership (`meta.room`) is derived and cheap to recompute, but a room's
**name** is user data that cannot be recovered. Resolve identity wrongly and the user's "kitchen" silently
migrates to a different room — the inset-churn failure of §6.1 one level up, and with worse consequences.

So identity resolves in three tiers, most robust first:

1. **surface-set overlap** against the previous membership. A room is ~20 surfaces, so this survives any
   one of them churning — including its floor.
2. **the defining floor's id**, recorded on the room. The fallback for when the per-surface labels are
   lost wholesale, which is not hypothetical: `main` preserves `meta.room` only because it never writes
   meta it does not know about, and that is a property of today's code rather than a guarantee.
3. **mint** a new room. Ids are monotonic and never reused.

Keying *only* on the floor id was rejected as the primary: a floor that churns would take the room's name
with it. Keying on segmentation order was rejected outright — that is exactly the churn mechanism.
"""

from __future__ import annotations

import re

_ROOM_ID = re.compile(r"^room_(\d+)$")


def _members(prev_labels: dict[str, str], room_id: str) -> set[str]:
    return {sid for sid, rid in (prev_labels or {}).items() if rid == room_id}


def _next_n(rooms: dict[str, dict]) -> int:
    """One past the highest ordinal ever used. Monotonic, so a deleted room's number is never reissued
    onto a different physical room — the number is what the user reads and says back."""
    best = 0
    for rid, rec in (rooms or {}).items():
        n = rec.get("n")
        if not isinstance(n, int):
            m = _ROOM_ID.match(rid)
            n = int(m.group(1)) if m else 0
        best = max(best, n or 0)
    return best + 1


def resolve_rooms(
    incoming: list[dict],
    stored_rooms: dict[str, dict] | None = None,
    prev_labels: dict[str, str] | None = None,
) -> tuple[list[str | None], dict[str, dict]]:
    """Map each incoming segmentation group onto a persistent room id.

    `incoming`      — `[{"floor": <surface id>, "ceiling": <id|None>, "ids": [<surface id>, …]}, …]`
    `stored_rooms`  — the space's `rooms` record: `{"room_1": {"n": 1, "name"?: str, "floor": <id>}, …}`
    `prev_labels`   — the previous membership, `{<surface id>: <room id>}` (i.e. every `meta.room`)

    Returns `(ids, rooms)` — one room id per incoming group, positionally, and the updated record.

    A room with no incoming group **keeps its entry**, name and all. Rooms come back (a rescan, a door
    closed during capture), names are precious, and an empty room costs one small dict.
    """
    rooms: dict[str, dict] = {rid: dict(rec) for rid, rec in (stored_rooms or {}).items()}
    prev_labels = prev_labels or {}
    out: list[str | None] = [None] * len(incoming)

    # Tier 1 — overlap. Score every (group, room) pair, then take them greedily best-first so the
    # strongest correspondence wins regardless of order. Ties break on room id and group index, so the
    # result is deterministic: an arbitrary-but-stable answer is the whole requirement here.
    scores: list[tuple[int, str, int]] = []
    for gi, g in enumerate(incoming):
        ids = set(g.get("ids") or [])
        if not ids:
            continue
        for rid in rooms:
            overlap = len(ids & _members(prev_labels, rid))
            if overlap:
                scores.append((overlap, rid, gi))
    scores.sort(key=lambda t: (-t[0], t[1], t[2]))
    taken_rooms: set[str] = set()
    for _overlap, rid, gi in scores:
        if out[gi] is None and rid not in taken_rooms:
            out[gi] = rid
            taken_rooms.add(rid)

    # Tier 2 — the recorded defining floor. Reached when the labels are gone wholesale rather than when
    # one surface churned, so it is a recovery path, not a routine one.
    for gi, g in enumerate(incoming):
        if out[gi] is not None:
            continue
        for rid, rec in rooms.items():
            if rid not in taken_rooms and rec.get("floor") and rec["floor"] == g.get("floor"):
                out[gi] = rid
                taken_rooms.add(rid)
                break

    # Tier 3 — mint.
    for gi, g in enumerate(incoming):
        if out[gi] is not None:
            continue
        n = _next_n(rooms)
        rid = f"room_{n}"
        rooms[rid] = {"n": n}
        out[gi] = rid
        taken_rooms.add(rid)

    # Refresh the defining floor on every matched room; the floor CAN change id without the room
    # changing identity, which is the entire reason tier 1 outranks tier 2.
    for gi, g in enumerate(incoming):
        rid = out[gi]
        if rid and g.get("floor"):
            rooms[rid]["floor"] = g["floor"]

    return out, rooms


def display_name(rooms: dict[str, dict], room_id: str) -> str:
    """What the user reads and says back. The default is **derived, never stored**, so a rename cannot
    leave a stale "room #2" behind anywhere — there was never a copy of it to go stale."""
    rec = (rooms or {}).get(room_id) or {}
    name = (rec.get("name") or "").strip()
    if name:
        return name
    n = rec.get("n")
    if not isinstance(n, int):
        m = _ROOM_ID.match(room_id or "")
        n = int(m.group(1)) if m else None
    return f"room #{n}" if n else room_id
