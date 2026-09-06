"""The pose eval harness — its corpus, its scoring, and the judge seam under it.

Tier 1: no keys, no network, no Blender, no model files. The harness itself needs all four, which is
exactly why the parts that decide whether a cell PASSED are pure functions living in `pose_corpus` —
a scoring bug would otherwise be indistinguishable from the tool description being wrong, which is the
one confusion this feature cannot afford.

docs/backlogs/figures.md — *the eval harness: verify the utterance, not the axis*.
"""

import pytest

from test_figures import _skeleton

from conjure.figures import (CORE_BONES, POSE_AXES, anatomical_axes, apply_pose, body_frame,
                             bone_directions, figure_description, infer_humanoid,
                             node_world_positions)
from conjure.judge import (FakeJudge, GeminiJudge, Verdict, build_judge, parse_choice, prompt_for)
from conjure.pose_corpus import (CAST, CORPUS, IMPOSSIBLE_BONE, IMPOSSIBLE_TWIST, MOVES,
                                 PLAUSIBLE_BAD, PLAUSIBLE_OK, PLAUSIBLE_OPTIONS, Phrase,
                                 bones_used, by_id, check_call, check_geometry)


# ---------------------------------------------------------------- the corpus is well-formed
#
# A corpus is data, and data rots quietly: a phrase whose expectations name a bone no rig has scores a
# free pass forever, and nobody notices because the cell goes green.


def test_every_phrase_is_uniquely_named_and_says_why_it_exists():
    assert len({p.id for p in CORPUS}) == len(CORPUS)
    for p in CORPUS:
        assert p.say and p.subject and p.why, p.id
        # "" is legal and means the judge is not asked which way it moved — see MOVES' note on why a
        # phrase stays silent. Anything else must be an option the judge is actually offered.
        assert p.moves in ({""} | set(MOVES)), f"{p.id}: {p.moves!r} is not a movement on offer"


def test_expectations_only_name_bones_a_humanoid_map_can_have():
    """Every bone in every expectation is one the discovery pipeline actually produces."""
    for p in CORPUS:
        unknown = bones_used(p) - set(CORE_BONES)
        assert not unknown, f"{p.id} expects {unknown}, which no bone map contains"


def test_sign_expectations_name_real_axes():
    for p in CORPUS:
        for bone, axis, sign in p.signs:
            assert axis in POSE_AXES, f"{p.id}: {axis!r} is not a rotation"
            assert sign in (-1, 1), f"{p.id}: sign must be ±1"


def test_the_three_device_failures_are_in_the_corpus():
    """The phrases that failed in the headset on 2026-09-03 — the reason this harness exists."""
    assert {p.id for p in by_id("arm-up", "arm-down", "legs-apart")} == {"arm-up", "arm-down",
                                                                        "legs-apart"}


def test_by_id_refuses_a_typo_rather_than_running_fewer_phrases():
    with pytest.raises(KeyError):
        by_id("arm-up", "arm-upp")


def test_the_cast_disagrees_about_rig_convention():
    """Three rigs and at least two provenances: a phrase that only works on one lineage is the failure
    this corpus is shaped to catch, and one rig cannot catch it."""
    assert len(CAST) >= 3
    assert len({r.path.rsplit("/", 1)[0] for r in CAST}) >= 2


# ---------------------------------------------------------------- scoring the call


def _phrase(**kw) -> Phrase:
    base = dict(id="t", say="do it", subject="arm", moves="up", why="a test")
    return Phrase(**{**base, **kw})


def test_a_turn_that_never_posed_anything_fails_loudly():
    assert check_call(_phrase(touches=("head",)), {}) == ["no pose was called at all"]


def test_the_named_bone_must_be_touched():
    fails = check_call(_phrase(touches=("rightUpperArm",)), {"leftUpperArm": {"aim": "up"}})
    assert fails == ["never touched rightUpperArm"]


def test_a_bone_that_should_have_been_left_alone_is_a_failure():
    """Aiming the forearm as well as the upper arm folds the elbow — a raised arm becomes a chicken
    wing, and the geometry check alone would not always notice."""
    fails = check_call(_phrase(touches=("rightUpperArm",), leaves=("rightLowerArm",)),
                       {"rightUpperArm": {"aim": "up"}, "rightLowerArm": {"aim": "up"}})
    assert fails == ["also moved rightLowerArm, which should have been left alone"]


def test_a_wrong_sign_is_caught_even_though_the_bone_is_right():
    fails = check_call(_phrase(touches=("head",), signs=(("head", "bend", -1),)),
                       {"head": {"bend": 20}})
    assert fails == ["head bend=20, wanted negative"]
    assert check_call(_phrase(touches=("head",), signs=(("head", "bend", -1),)),
                      {"head": {"bend": -20}}) == []


def test_a_zero_rotation_is_not_a_sign():
    """`{"bend": 0}` is a call that did nothing while looking like a call that did something."""
    fails = check_call(_phrase(touches=("head",), signs=(("head", "turn", 1),)), {"head": {"turn": 0}})
    assert fails == ["head got no turn"]


def test_an_aim_is_exempt_from_a_sign_expectation():
    """`aim` names a destination, so its equivalent rotation is not the director's to get wrong —
    scoring it on sign would punish the exact form the tool description recommends."""
    assert check_call(_phrase(touches=("leftUpperArm",), signs=(("leftUpperArm", "bend", -1),)),
                      {"leftUpperArm": {"aim": "up"}}) == []


# ---------------------------------------------------------------- scoring the geometry
#
# On a synthetic T-posed skeleton, so the expected answers are known by construction rather than by
# rendering something and squinting at it.


@pytest.fixture
def rig():
    doc, _ = _skeleton()
    mapping = infer_humanoid(doc)
    positions = node_world_positions(doc)
    by_name = {n["name"]: i for i, n in enumerate(doc["nodes"])}
    at_rest = {b: positions[by_name[n]] for b, n in mapping.items()}
    ys = [p[1] for p in positions.values()]
    return doc, mapping, at_rest, body_frame(doc, mapping), max(ys) - min(ys)


def _after(doc, mapping, pose):
    """Joint positions AND bone directions after posing — a relative claim needs the first, an absolute
    one ("the arm points up") needs the second."""
    import copy
    posed = copy.deepcopy(doc)
    apply_pose(posed, mapping, pose)
    positions = node_world_positions(posed)
    by_name = {n["name"]: i for i, n in enumerate(posed["nodes"])}
    return ({b: positions[by_name[n]] for b, n in mapping.items()}, bone_directions(posed, mapping))


def test_a_raised_arm_satisfies_the_raised_arm_phrase(rig):
    doc, mapping, at_rest, frame, height = rig
    phrase = by_id("arm-up")[0]
    after, dirs = _after(doc, mapping, {"rightUpperArm": {"aim": "up"}})
    assert check_geometry(phrase, at_rest, after, frame, height, dirs) == []


def test_the_same_phrase_fails_when_the_arm_goes_the_other_way(rig):
    """The measured bug of 2026-09-03, reproduced: correct bone, wrong direction. This is the assertion
    that would have failed in an afternoon with no headset."""
    doc, mapping, at_rest, frame, height = rig
    phrase = by_id("arm-up")[0]
    after, dirs = _after(doc, mapping, {"rightUpperArm": {"aim": "down"}})
    fails = check_geometry(phrase, at_rest, after, frame, height, dirs)
    assert len(fails) == 2                                  # it points down, and the wrist is not above
    assert "rightUpperArm points down" in fails[0]


def test_spreading_the_legs_is_measured_as_the_feet_parting(rig):
    doc, mapping, at_rest, frame, height = rig
    phrase = by_id("legs-apart")[0]
    after, dirs = _after(doc, mapping, {"leftUpperLeg": {"spread": 25},
                                        "rightUpperLeg": {"spread": 25}})
    assert check_geometry(phrase, at_rest, after, frame, height, dirs) == []


def test_negating_one_side_of_a_spread_swings_both_legs_the_same_way(rig):
    """The sign error the tool description warns about twice, caught by the feet not parting."""
    doc, mapping, at_rest, frame, height = rig
    phrase = by_id("legs-apart")[0]
    after, dirs = _after(doc, mapping, {"leftUpperLeg": {"spread": 25},
                                        "rightUpperLeg": {"spread": -25}})
    fails = check_geometry(phrase, at_rest, after, frame, height, dirs)
    assert fails and "leftFoot and rightFoot" in fails[0]


def test_bending_an_elbow_closes_the_hand_onto_the_shoulder(rig):
    doc, mapping, at_rest, frame, height = rig
    phrase = by_id("elbow")[0]
    after, dirs = _after(doc, mapping, {"leftLowerArm": {"bend": 90}})
    assert check_geometry(phrase, at_rest, after, frame, height, dirs) == []


def test_a_predicate_about_a_bone_this_rig_lacks_is_skipped_not_failed(rig):
    """Saka maps 54 bones; the Daz conversions map 21. A corpus limited to the intersection would test
    less on every rig in order to test the same on all of them."""
    _, _, at_rest, frame, height = rig
    assert "upperChest" not in at_rest                       # VRM states one; the Daz rigs have none
    phrase = _phrase(geometry=(("moved", "upperChest", "up", 0.9),))
    assert check_geometry(phrase, at_rest, dict(at_rest), frame, height) == []


def test_an_unknown_predicate_is_an_error_not_a_pass(rig):
    """A typo in an expectation must never read as 'nothing to check'."""
    _, _, at_rest, frame, height = rig
    with pytest.raises(ValueError):
        check_geometry(_phrase(geometry=(("hovers", "leftHand", "up", 0.1),)),
                       at_rest, dict(at_rest), frame, height)


def test_distances_are_fractions_of_height_not_metres(rig):
    """A 1.55 m rig and a 1.80 m one must score the same phrase the same way, so the same displacement
    scaled by height passes both — and unscaled would not."""
    doc, mapping, at_rest, frame, height = rig
    phrase = by_id("elbow")[0]                       # a `nearer`, which is the predicate that scales
    after, dirs = _after(doc, mapping, {"leftLowerArm": {"bend": 90}})
    tall = {b: tuple(c * 2 for c in p) for b, p in at_rest.items()}
    tall_after = {b: tuple(c * 2 for c in p) for b, p in after.items()}
    assert check_geometry(phrase, at_rest, after, frame, height, dirs) == []
    assert check_geometry(phrase, tall, tall_after, frame, height * 2, dirs) == []


# ---------------------------------------------------------------- the judge seam


@pytest.mark.parametrize("reply,want", [
    ("b", 1), ("(c)", 2), ("C", 2), ("c) forward", 2), ("a.", 0),
    ("The answer is (d).", 3),
    ("up, raised above where it started", 0),          # wrote the option out instead of lettering it
    ("z", -1), ("", -1), ("I can't tell from this image", -1),
])
def test_a_letter_is_read_out_of_whatever_the_model_actually_said(reply, want):
    options = ["up, raised above where it started", "forward", "backward", "down"]
    assert parse_choice(reply, options) == want


def test_a_letter_outside_the_offered_range_is_no_answer_at_all():
    """Never the nearest option: a judge that answered (f) to four choices was not judging."""
    assert parse_choice("(f)", ["a", "b", "c", "d"]) == -1


def test_an_unanswered_verdict_is_not_a_pass():
    assert not Verdict(-1, "", "dunno").answered
    assert Verdict(0, "up", "(a)").answered


def test_the_prompt_introduces_every_image_in_order():
    """A paired comparison is meaningless unless the model is told which image is which."""
    text = prompt_for([("at rest, from the front", b"1"), ("posed, from the front", b"2")],
                      "which way did the arm move?", ["up", "down"])
    assert "Image 1: at rest, from the front" in text
    assert "Image 2: posed, from the front" in text
    assert "(a) up" in text and "(b) down" in text


def test_too_many_options_is_refused_rather_than_silently_truncated():
    with pytest.raises(ValueError):
        prompt_for([("x", b"")], "?", [str(i) for i in range(30)])


async def test_a_scripted_fake_judge_answers_in_order():
    judge = FakeJudge(["b", 0])
    first = await judge.choose([("x", b"1")], "q1", ["up", "down"])
    second = await judge.choose([("x", b"1")], "q2", ["up", "down"])
    assert (first.choice, second.choice) == (1, 0)
    assert [q for q, _ in judge.asked] == ["q1", "q2"]


async def test_an_unscripted_fake_judge_is_deterministic():
    a, b = FakeJudge(), FakeJudge()
    same = [await j.choose([("x", b"pixels")], "which way?", list(MOVES.values())) for j in (a, b)]
    assert same[0].choice == same[1].choice >= 0


class _Settings:
    def __init__(self, **kw):
        self.judge_provider = kw.get("provider", "gemini")
        self.judge_model = kw.get("model", "")
        self.google_api_key = kw.get("google", None)
        self.anthropic_api_key = kw.get("anthropic", None)


def test_no_key_means_no_judge_rather_than_a_crash():
    """The harness still runs its geometry pass and says nobody looked at the pictures."""
    assert build_judge(_Settings(provider="gemini", google=None)) is None
    assert build_judge(_Settings(provider="claude", anthropic=None)) is None


def test_the_backend_supplies_its_own_default_model():
    """`judge_model` empty means 'whatever this provider's default is', so switching provider is one
    setting and not two."""
    judge = build_judge(_Settings(provider="gemini", google="k"))
    assert isinstance(judge, GeminiJudge) and judge.name == "gemini-2.5-flash"
    assert build_judge(_Settings(provider="claude", anthropic="k")).name.startswith("claude")


def test_judging_can_be_turned_off_and_a_typo_does_not_pass_silently():
    assert build_judge(_Settings(provider="none")) is None
    assert build_judge(_Settings(provider="wat", google="k")) is None
    assert isinstance(build_judge(_Settings(provider="fake")), FakeJudge)


def test_an_explicit_override_beats_config():
    assert isinstance(build_judge(_Settings(provider="gemini", google="k"), "fake"), FakeJudge)


def test_the_plausibility_question_has_an_out_that_is_not_a_failure():
    """"Cannot tell" must be offered, or a two-way choice makes the judge guess — and it must not score
    as a defect, or the harness reports a problem with the pose that was a problem with the question."""
    assert len(PLAUSIBLE_OPTIONS) == 3
    assert PLAUSIBLE_OPTIONS[PLAUSIBLE_OK].startswith("yes")
    assert PLAUSIBLE_OPTIONS[PLAUSIBLE_BAD].startswith("no")
    assert PLAUSIBLE_OK != PLAUSIBLE_BAD and len({PLAUSIBLE_OK, PLAUSIBLE_BAD}) < len(PLAUSIBLE_OPTIONS)


def test_a_phrase_that_cannot_name_one_direction_says_so():
    """Silence is a claim about the phrase, not a way of quieting a judge that disagreed — a trunk bone
    does not translate, and an absolute request means something different on a rig already resting in
    the target pose."""
    silent = {p.id for p in CORPUS if not p.moves}
    assert {"look-up", "look-down", "bow", "arms-out"} <= silent
    assert {"arm-up", "knee", "legs-apart"} & silent == set()


# ---------------------------------------------------------------- the wording under test


def test_the_harness_and_the_tool_read_the_same_description():
    """`inspect_figure` and the harness both call this. A harness with its own phrasing would pass
    while the real surface failed."""
    text = figure_description(label="Grace", height_m=1.7, tris=99, bones=("head", "leftUpperArm"),
                              has_map=True)
    assert "1.70 m tall" in text and "Posable bones (2)" in text
    assert "aim (up, down, forward, back, out, in)" in text


def test_a_figure_with_no_frame_is_told_apart_from_one_with_no_skeleton():
    """"Cannot be posed" would send the caller looking for a missing skeleton that is right there."""
    assert "place it again" in figure_description(label="x", has_map=True)
    assert "No humanoid bone map" in figure_description(label="x", has_map=False)


def test_apply_pose_moves_the_joint_the_axes_say_it_should(rig):
    """The harness's geometry pass rests on this: a pose written into node rotations is the same pose
    the client applies, so joint positions read off the result are what a headset would show."""
    doc, mapping, at_rest, _, _ = rig
    after, _dirs = _after(doc, mapping, {"rightUpperArm": {"aim": "up"}})
    assert after["rightHand"][1] > at_rest["rightHand"][1] + 0.2
    assert at_rest == {b: p for b, p in at_rest.items()}     # the bind pose was not mutated
    assert anatomical_axes(doc, mapping)["rightUpperArm"]["rest"]


def test_the_calibration_pose_is_one_no_body_can_hold():
    """The harness asks the judge a question with a known answer before believing its answers to the
    real ones — and this is that question. A twist about the bone's own length is rig-independent
    (Blender runs Y along every bone), and 170 degrees of it puts a skull on backwards."""
    assert IMPOSSIBLE_BONE in CORE_BONES
    assert IMPOSSIBLE_TWIST[1] > 150 and not IMPOSSIBLE_TWIST[0] and not IMPOSSIBLE_TWIST[2]
