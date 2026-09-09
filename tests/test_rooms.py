"""Room identity resolution — `conjure/rooms.py`.

The thing under test is not "does it group surfaces" (the client does that, and `space-snap.test.js`
covers it) but "does a room keep being the same room". A wrong answer here does not degrade a render, it
migrates a name the user typed onto a different physical room, and there is no recovering it.
"""

from conjure.rooms import display_name, resolve_rooms


def _g(floor, ids, ceiling=None):
    return {"floor": floor, "ceiling": ceiling, "ids": ids}


def test_a_first_capture_mints_rooms_numbered_from_one():
    ids, rooms = resolve_rooms([_g("f_a", ["f_a", "w1", "w2"]), _g("f_b", ["f_b", "w3"])])
    assert ids == ["room_1", "room_2"]
    assert rooms["room_1"]["n"] == 1 and rooms["room_2"]["n"] == 2
    assert rooms["room_1"]["floor"] == "f_a"


def test_an_unchanged_recapture_re_mints_nothing():
    ids, rooms = resolve_rooms([_g("f_a", ["f_a", "w1"]), _g("f_b", ["f_b", "w2"])])
    labels = {s: r for r, g in zip(ids, [["f_a", "w1"], ["f_b", "w2"]]) for s in g}
    ids2, rooms2 = resolve_rooms([_g("f_a", ["f_a", "w1"]), _g("f_b", ["f_b", "w2"])], rooms, labels)
    assert ids2 == ids
    assert set(rooms2) == set(rooms)


def test_the_name_survives_the_floor_being_re_minted():
    """Tier 1 exists for exactly this. Keying identity on the defining floor alone — the obvious
    design — loses the name the moment that one surface churns, which is a routine event."""
    rooms = {"room_1": {"n": 1, "name": "kitchen", "floor": "f_a"}}
    labels = {"f_a": "room_1", "w1": "room_1", "w2": "room_1", "w3": "room_1"}
    # the floor comes back with a fresh id; its four room-mates do not
    ids, rooms2 = resolve_rooms([_g("f_NEW", ["f_NEW", "w1", "w2", "w3"])], rooms, labels)
    assert ids == ["room_1"], "overlap on the surviving members keeps the identity"
    assert rooms2["room_1"]["name"] == "kitchen"
    assert rooms2["room_1"]["floor"] == "f_NEW", "and the defining floor is refreshed"


def test_the_floor_id_recovers_identity_when_every_label_is_lost():
    """Tier 2. `main` preserves `meta.room` only because it never writes meta it does not know about —
    a property of today's code, not a guarantee. If the labels go, the names must not follow."""
    rooms = {"room_1": {"n": 1, "name": "kitchen", "floor": "f_a"}}
    ids, rooms2 = resolve_rooms([_g("f_a", ["f_a", "w1", "w2"])], rooms, prev_labels={})
    assert ids == ["room_1"]
    assert rooms2["room_1"]["name"] == "kitchen"


def test_overlap_outranks_the_floor_id_when_they_disagree():
    """The tiers are ordered, not alternatives: a floor that moved to the OTHER room must not drag the
    name with it."""
    rooms = {"room_1": {"n": 1, "name": "kitchen", "floor": "f_x"},
             "room_2": {"n": 2, "name": "bedroom", "floor": "f_y"}}
    labels = {"w1": "room_1", "w2": "room_1", "w3": "room_2", "w4": "room_2"}
    # the group made of room_1's members claims room_2's recorded floor id
    ids, _ = resolve_rooms([_g("f_y", ["w1", "w2"]), _g("f_z", ["w3", "w4"])], rooms, labels)
    assert ids == ["room_1", "room_2"], "membership decides, not the floor"


def test_the_strongest_overlap_wins_regardless_of_order():
    rooms = {"room_1": {"n": 1, "floor": "f_a"}, "room_2": {"n": 2, "floor": "f_b"}}
    labels = {**{f"a{i}": "room_1" for i in range(6)}, **{f"b{i}": "room_2" for i in range(6)}}
    # group 0 touches both rooms but is mostly room_2; group 1 is purely room_1
    ids, _ = resolve_rooms([_g("f_b", ["b0", "b1", "b2", "b3", "a0"]),
                            _g("f_a", ["a1", "a2", "a3"])], rooms, labels)
    assert ids == ["room_2", "room_1"]


def test_a_room_that_vanished_keeps_its_entry_and_its_name():
    """Rooms come back — a rescan, a door shut during capture. The name costs one small dict to keep
    and cannot be recovered if dropped."""
    rooms = {"room_1": {"n": 1, "name": "kitchen", "floor": "f_a"},
             "room_2": {"n": 2, "name": "bedroom", "floor": "f_b"}}
    labels = {"f_a": "room_1", "f_b": "room_2"}
    ids, rooms2 = resolve_rooms([_g("f_a", ["f_a"])], rooms, labels)
    assert ids == ["room_1"]
    assert rooms2["room_2"]["name"] == "bedroom", "the absent room is kept, not pruned"


def test_a_genuinely_new_room_mints_past_the_highest_number_ever_used():
    """Monotonic on purpose: the ordinal is what the user reads off a label and says back, so reissuing
    a deleted room's number onto a different physical room would make "room #2" mean two things."""
    rooms = {"room_1": {"n": 1, "floor": "f_a"}, "room_7": {"n": 7, "floor": "f_b"}}
    ids, rooms2 = resolve_rooms([_g("f_a", ["f_a"]), _g("f_new", ["f_new"])], rooms,
                                {"f_a": "room_1"})
    assert ids[0] == "room_1"
    assert ids[1] == "room_8", "one past the highest, not the first free slot"


def test_two_groups_never_collapse_onto_one_room():
    """Over-segmentation is safe — co-moving sub-rooms just refine to equal deltas — but two groups
    sharing one id would make membership ambiguous for everything downstream."""
    rooms = {"room_1": {"n": 1, "floor": "f_a"}}
    labels = {"w1": "room_1", "w2": "room_1", "w3": "room_1", "w4": "room_1"}
    ids, _ = resolve_rooms([_g("f_a", ["w1", "w2"]), _g("f_b", ["w3", "w4"])], rooms, labels)
    assert len(set(ids)) == 2, "the weaker claim mints rather than sharing"
    assert "room_1" in ids


def test_resolution_is_deterministic_under_a_tie():
    rooms = {"room_1": {"n": 1, "floor": "f_a"}, "room_2": {"n": 2, "floor": "f_b"}}
    labels = {"w1": "room_1", "w2": "room_2"}
    first = resolve_rooms([_g("f_x", ["w1", "w2"])], rooms, labels)[0]
    for _ in range(5):
        assert resolve_rooms([_g("f_x", ["w1", "w2"])], rooms, labels)[0] == first


def test_the_default_display_name_is_derived_and_never_stored():
    _, rooms = resolve_rooms([_g("f_a", ["f_a"])])
    assert "name" not in rooms["room_1"], "nothing to go stale after a rename"
    assert display_name(rooms, "room_1") == "room #1"
    rooms["room_1"]["name"] = "kitchen"
    assert display_name(rooms, "room_1") == "kitchen"


def test_the_input_record_is_not_mutated():
    rooms = {"room_1": {"n": 1, "name": "kitchen", "floor": "f_a"}}
    resolve_rooms([_g("f_NEW", ["f_NEW"])], rooms, {"f_a": "room_1"})
    assert rooms["room_1"]["floor"] == "f_a", "callers keep their copy"
