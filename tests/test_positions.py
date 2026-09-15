"""Reading what the captured site STATES about its own animation (conjure/positions.py).

The filename rule in `capture_set` is all the older builds give you and it fails completely on the
newer ones — the four captures it links nothing for are exactly the four that ship these configs. So
these tests are mostly about the shapes the config actually takes, because getting one wrong means a
silent figure rather than a crash.
"""

from __future__ import annotations

import json

from conjure.positions import (clip_audio, clip_speed, read_main, read_positions, resolves)


class _Build:
    """Just enough of a `Build`: an asset registry and a path for each file."""

    def __init__(self, tmp_path, files: dict, types: dict | None = None):
        self.assets = {}
        self._paths = {}
        for i, (name, body) in enumerate(files.items(), start=100):
            path = tmp_path / f"{name}.json"
            path.write_text(json.dumps(body))
            self.assets[i] = {"id": str(i), "name": name, "type": "json"}
            self._paths[i] = str(path)
        for j, (name, kind) in enumerate((types or {}).items(), start=900):
            self.assets[j] = {"id": str(j), "name": name, "type": kind}

    def path(self, aid):
        return self._paths.get(int(aid))


IDLE_ONLY = {                                     # susan's shape: an idle, a sound, nothing else
    "idle": {"animationName": "KneelAwait", "soundName": "AulaIdle4BJmp3", "speed": 1}
}

FULL = {                                          # ebony's shape
    "idle": {
        "animationName": "pc_leanOnBed_idle.glb", "soundName": "HotelIdle4.mp3", "speed": 0.5,
        "boneLayers": [{"layerName": "Eyelids", "states": [
            {"animationNames": ["pc_blink.glb"], "animationSpeed": 1, "timeMin": 5, "timeMax": 8,
             "weight": 0, "stateTransitionTime": 0},
            {"animationNames": ["pc_blink.glb"], "animationSpeed": 1, "timeMin": 0.37,
             "timeMax": 0.37, "weight": 1, "stateTransitionTime": 0}]}]
    },
    "action": {
        "animationName": ["pc_a1.glb", "pc_a2.glb", "pc_a3.glb"],
        "soundNameAction": "HotelAction3.mp3", "soundNameRough": "HotelRough3.mp3",
        "speedAction": 0.6, "speedRough": 1.2,
        "boneLayers": [{"layerName": "Head", "states": [
            {"animationNames": ["pc_headLeft.glb", "pc_headRight.glb"], "animationSpeed": 0.5,
             "timeMin": 3, "timeMax": 10, "weight": 1, "stateTransitionTime": 0.5}]}]
    }
}

MAIN = {"mainModelName": "MainModelHotelBlack", "dressObjects": ["Dress", "Shoe.L"],
        "boneLayers": [{"name": "Head", "enabledBones": ["DEF-spine.004", "DEF-spine.006"]},
                       {"name": "Eyelids", "enabledBones": ["DEF-lid.T.L"]}]}


def test_an_idle_only_position_is_read_without_inventing_an_action(tmp_path):
    """susan's six positions have no `action` at all, and a reader that assumed one found nothing."""
    build = _Build(tmp_path, {"position_1_config": IDLE_ONLY})
    [position] = read_positions(build, {})
    assert position.idle == "KneelAwait" and position.idle_sound == "AulaIdle4BJmp3"
    assert position.action == () and position.action_sound == ""
    assert position.idle_speed == 1.0 and position.layers == []


def test_an_action_names_SEVERAL_clips_and_an_idle_names_one(tmp_path):
    """`animationName` is a string for an idle and a LIST for an action — the site picks one of the
    three at random. Both shapes appear in one file, so both have to be read."""
    build = _Build(tmp_path, {"position_1_config": FULL})
    [position] = read_positions(build, {})
    assert position.idle == "pc_leanOnBed_idle.glb"
    assert position.action == ("pc_a1.glb", "pc_a2.glb", "pc_a3.glb")


def test_the_pairing_is_many_to_many_in_BOTH_directions(tmp_path):
    """One sound voices three action clips, and each action clip carries a normal AND a rough take.
    That is the site's shape rather than an accident, and `voiced_by` already expresses it."""
    build = _Build(tmp_path, {"position_1_config": FULL})
    pairs = clip_audio(read_positions(build, {}))
    assert pairs["pc_leanOnBed_idle.glb"] == ["HotelIdle4.mp3"]
    for clip in ("pc_a1.glb", "pc_a2.glb", "pc_a3.glb"):
        assert pairs[clip] == ["HotelAction3.mp3", "HotelRough3.mp3"]


def test_the_authored_speed_is_kept_and_a_default_one_is_not(tmp_path):
    """0.5, 0.6 and 1.2 are authored in this corpus, so a clip played at its own rate is played wrong.
    Storing 1.0 would say nothing, and would make "nobody said" indistinguishable from "they said 1"."""
    build = _Build(tmp_path, {"position_1_config": FULL, "position_2_config": IDLE_ONLY})
    speeds, clash = clip_speed(read_positions(build, {}))
    assert speeds["pc_leanOnBed_idle.glb"] == 0.5
    assert speeds["pc_a1.glb"] == 0.6
    assert "KneelAwait" not in speeds, "1.0 is the default and is not worth recording"
    assert clash == []


def test_two_positions_disagreeing_about_a_RATE_are_reported_not_resolved(tmp_path):
    """There is no basis for choosing, and first-wins would be silent about having chosen."""
    other = {"idle": {"animationName": "pc_leanOnBed_idle.glb", "soundName": "x.mp3", "speed": 0.9}}
    build = _Build(tmp_path, {"position_1_config": FULL, "position_2_config": other})
    speeds, clash = clip_speed(read_positions(build, {}))
    assert clash == ["pc_leanOnBed_idle.glb"]
    assert "pc_leanOnBed_idle.glb" not in speeds


def test_a_blink_is_a_LAYER_STATE_with_its_own_timer_not_a_clip_you_play(tmp_path):
    """Which is the answer to "why is `Blink.glb` never played at placement". It is not a clip anybody
    plays: it is a bone-masked state that fires every 5-8 seconds over whatever is running."""
    build = _Build(tmp_path, {"position_1_config": FULL})
    main = read_main(_Build(tmp_path, {"main_config": MAIN}))
    [position] = read_positions(build, main["bone_masks"])
    eyelids = [layer for layer in position.layers if layer.name == "Eyelids"]
    assert len(eyelids) == 2, "one Layer per STATE — each fires on its own timer"
    assert eyelids[0].clips == ("pc_blink.glb",)
    assert eyelids[0].every == (5.0, 8.0) and eyelids[0].weight == 0.0
    assert eyelids[0].bones == ("DEF-lid.T.L",), "the mask comes from main_config"
    head = [layer for layer in position.layers if layer.name == "Head"][0]
    assert head.clips == ("pc_headLeft.glb", "pc_headRight.glb") and head.blend == 0.5
    assert head.bones == ("DEF-spine.004", "DEF-spine.006")


def test_main_config_states_what_this_pipeline_otherwise_infers(tmp_path):
    """`mainModelName` is what `captures/things.json` overrides by hand, and `dressObjects` is what
    `parts.py` classifies from mesh names. Here the author simply says."""
    main = read_main(_Build(tmp_path, {"main_config": MAIN}))
    assert main["mainModelName"] == "MainModelHotelBlack"
    assert main["dressObjects"] == ["Dress", "Shoe.L"]
    assert set(main["bone_masks"]) == {"Head", "Eyelids"}


def test_a_build_with_no_configs_yields_nothing_rather_than_failing(tmp_path):
    """Sixteen of the twenty captures have none, and they are the ones the filename rule works for."""
    build = _Build(tmp_path, {})
    assert read_positions(build, {}) == [] and read_main(build) == {}
    assert clip_audio([]) == {} and clip_speed([]) == ({}, [])


def test_a_name_matching_no_asset_is_COUNTED_never_guessed_at(tmp_path):
    """The evidence for trusting the mapping is that it resolves — 98 of 98 on nancy, 100 of 100 on
    ebony. A build where it drops is saying the config describes a version no longer on disk, and that
    has to show up as a number rather than as a quietly missing link."""
    build = _Build(tmp_path, {"position_1_config": IDLE_ONLY},
                   types={"KneelAwait": "animation"})       # the SOUND is absent from the registry
    positions = read_positions(build, {})
    assert resolves(build, positions) == (1, 1, 0, 1)
    # And a name whose asset is the wrong TYPE does not count either.
    wrong = _Build(tmp_path, {"position_1_config": IDLE_ONLY},
                   types={"KneelAwait": "audio", "AulaIdle4BJmp3": "audio"})
    assert resolves(wrong, read_positions(wrong, {})) == (0, 1, 1, 1)


def test_a_broken_config_is_skipped_rather_than_taking_the_import_down(tmp_path):
    (tmp_path / "position_9_config.json").write_text("{ not json")
    build = _Build(tmp_path, {"position_1_config": IDLE_ONLY})
    build.assets[500] = {"id": "500", "name": "position_9_config", "type": "json"}
    build._paths[500] = str(tmp_path / "position_9_config.json")
    assert [p.name for p in read_positions(build, {})] == ["position_1_config"]
