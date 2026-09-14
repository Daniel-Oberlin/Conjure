"""Linking rules for an imported capture (conjure/capture_set.py).

Every rule here declines to guess where the evidence runs out, and the tests are mostly about that
line rather than about the happy path: a wrong link is worse than a missing one, because the missing
one is visible and the wrong one gets acted on.
"""

from conjure.capture_set import (audio_role, clip_kind, is_ambience, is_promo, is_slot_named, stem,
                                 voiced_clips, wants_props)

CLIPS = ["1_idle.glb", "5_idle.glb", "8_idle.glb", "9_idle.glb", "2_action.glb", "10_action.glb"]


def test_one_audio_file_can_voice_SEVERAL_clips_and_the_name_says_which():
    """Jane is 20-of-20 one-to-one and generalising from her was wrong: across twenty captures only
    200 of 898 audio files pair with a single clip. `1-5-8-9_idle` names four."""
    assert voiced_clips("1-5-8-9_idle.mp3", CLIPS) == \
        ["1_idle.glb", "5_idle.glb", "8_idle.glb", "9_idle.glb"]
    assert voiced_clips("2-10_action.mp3", CLIPS) == ["2_action.glb", "10_action.glb"]
    assert voiced_clips("1_idle.mp3", CLIPS) == ["1_idle.glb"]


def test_a_claim_about_a_clip_the_capture_does_not_HAVE_is_not_recorded():
    """The name is a claim. `3_idle` is a perfectly good claim and this capture cannot honour it."""
    assert voiced_clips("3_idle.mp3", CLIPS) == []
    assert voiced_clips("1-3-5_idle.mp3", CLIPS) == ["1_idle.glb", "5_idle.glb"], \
        "the half it can honour, and only that half"


def test_a_name_that_does_not_PARSE_is_left_unlinked_rather_than_guessed():
    """`HotelAction0`'s number indexes a script we do not have. Linking it to `10_idle` because both
    end in a digit would be inventing a fact, and the catalog would then assert it."""
    for name in ("HotelAction0.mp3", "AulaIdle0mp3", "WCAction3.mp3", "Washitsu outside.mp3"):
        assert voiced_clips(name, CLIPS) == []


def test_marketing_and_ui_sound_is_skipped_not_imported():
    """Twenty-four lines and a click arrive identically in every capture from this origin, which names
    those files after their own transcripts — that is what makes them recognisable."""
    assert is_promo("Bang me in any sex position in the full version.mp3")
    assert is_promo("Any girl you want. In your room right now. VR holes dot com.mp3")
    assert is_promo("Click - compressed.mp3")
    assert not is_promo("1_idle.mp3")
    assert not is_promo("Washitsu soundtrack.mp3")


def test_room_sound_is_ambience_and_the_rule_stays_narrow():
    """`Washitsu soundtrack` belongs to a room, not to a figure or a clip. `HotelAction0` is also
    attached to no clip and is NOT ambience — the rule matches what it recognises and no more."""
    assert is_ambience("Washitsu soundtrack.mp3")
    assert is_ambience("Moon Base Ambience.mp3")
    assert is_ambience("Ceiling Fan.mp3")
    assert not is_ambience("HotelAction0.mp3")
    assert not is_ambience("1_idle.mp3")


def test_audio_role_covers_every_file_and_unlinked_is_a_real_answer():
    """698 of 898 attach to nothing by name. They are still imported and still searchable; they just
    carry no assertion about what they go with."""
    assert audio_role("1-5-8-9_idle.mp3", CLIPS) == ("voice", CLIPS[:4])
    assert audio_role("Moon Base Ambience.mp3", CLIPS) == ("ambience", [])
    assert audio_role("Click - compressed.mp3", CLIPS) == ("skip", [])
    assert audio_role("HotelAction0.mp3", CLIPS) == ("unlinked", [])
    # Promo wins over everything: a marketing line mentioning a soundtrack is still marketing.
    assert audio_role("Buy the soundtrack in the full version.mp3", CLIPS)[0] == "skip"


def test_a_clip_name_that_calls_out_a_FIXTURE_is_recorded_as_a_hint():
    """93 of 206 names do, which is why rig compatibility is not sufficiency: `pc_leanOnSink_headLeft`
    plays on any figure of the right rig and is wrong on one standing in a field.

    A substring and not a word match, because these names run together and a boundary rule finds none
    of them."""
    assert wants_props("pc_leanOnSink_headLeft.glb") == ["sink"]
    assert wants_props("LayTableIdle") == ["table"]
    assert wants_props("WhiteboardIdleFIXFIXU") == ["whiteboard"]
    assert wants_props("1_idle.glb") == []


def test_slot_names_are_told_apart_from_real_ones():
    """`1_idle` exists in 16 captures in 14 distinct versions, 10 to 43 seconds long — a label within
    a set and nothing across one. This marks what a naming pass should look at; four captures carry
    no slot-numbered clips at all."""
    assert is_slot_named("1_idle.glb")
    assert is_slot_named("10-3_action.glb")
    assert not is_slot_named("pc_leanOnSink_headLeft.glb")
    assert not is_slot_named("KneelAwait")


def test_clip_kind_reads_the_one_semantic_a_slot_name_carries():
    assert clip_kind("1_idle.glb") == "idle"
    assert clip_kind("10_action.glb") == "action"
    assert clip_kind("2_rough.glb") == "rough"
    assert clip_kind("KneelAwait") is None


def test_stem_drops_an_extension_and_a_path_but_not_a_dotted_name():
    assert stem("files/assets/1/1/3_idle.mp3") == "3_idle"
    assert stem("aula_Std_Eye_R") == "aula_Std_Eye_R"
    assert stem("Plant_Dracaena-trifasciata_A.png") == "Plant_Dracaena-trifasciata_A"


def test_identity_above_the_bytes_is_per_CAPTURE(tmp_path):
    """A content-addressed catalog has no other way to say "the same logical asset": re-converting gives
    it a new id every time the converter improves. So identity is `(kind, label)` **within one
    capture** — and the capture tag is the whole safety argument.

    Two captures can ship different `computer_desk` bytes and each keeps its own row, because a row
    tagged only `jane` is never a candidate while importing `akari`. Without that, re-importing one
    capture would retire another's props."""
    import runpy

    from conjure.library import AssetLibrary
    mod = runpy.run_path("scripts/import_capture.py", run_name="not_main")
    same_thing = mod["same_thing"]
    S = "daniel/agents/builder"
    lib = AssetLibrary(tmp_path / "library.db")
    lib.upsert("desk-akari.glb", kind="model", label="computer_desk", scope=S,
               source="cache://a", tags="akari")
    lib.upsert("desk-jane.glb", kind="model", label="computer_desk", scope=S,
               source="cache://b", tags="jane")
    lib.upsert("shared.glb", kind="model", label="magnet", scope=S, source="cache://c",
               tags="akari, jane")
    lib.upsert("clip.glb", kind="animation", label="computer_desk", scope=S, source="cache://d",
               tags="akari")

    assert [r["id"] for r in same_thing(lib, S, "akari", "model", "computer_desk")] \
        == ["desk-akari.glb"], "jane's desk is not akari's to retire"
    assert [r["id"] for r in same_thing(lib, S, "jane", "model", "computer_desk")] \
        == ["desk-jane.glb"]
    assert [r["id"] for r in same_thing(lib, S, "akari", "model", "magnet")] == ["shared.glb"], \
        "a prop shared by both IS akari's to replace when akari is re-imported"
    assert same_thing(lib, S, "akari", "model", "nothing-called-this") == []
    assert same_thing(lib, S, "akari", None, "computer_desk") == [], "no kind, no claim"


def test_a_tombstone_is_never_a_candidate_a_second_time(tmp_path):
    """Retiring a tombstone means nothing, and `by_user` already hides them — so the rule inherits that
    for free rather than restating it."""
    import runpy

    from conjure.library import AssetLibrary
    mod = runpy.run_path("scripts/import_capture.py", run_name="not_main")
    S = "daniel/agents/builder"
    lib = AssetLibrary(tmp_path / "library.db")
    for i in ("old.glb", "new.glb"):
        lib.upsert(i, kind="model", label="jane", scope=S, source=f"cache://{i}", tags="jane")
    lib.supersede("old.glb", "new.glb")
    assert [r["id"] for r in mod["same_thing"](lib, S, "jane", "model", "jane")] == ["new.glb"]
