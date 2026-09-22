"""`conjure.expressions` — driving a face across the two expression rigs that exist.

There are exactly two in the corpus and they share no vocabulary: Saka's author states the emotion
(`Fcl_ALL_Joy`), Alice's names muscles (`Mouth_Smile_L`). So `smile` is a semantic request resolved per
scheme, in the same way `leftUpperArm` is a semantic bone.

The vocabularies below are REAL — copied from the two figures — because a table tested against names
invented for the test proves only that the test and the table agree.

What makes this more than an assertion is `check_regions`. A morph target carries position deltas, so
"this one is a brow" is testable: measured on both rigs, the brow, eye and mouth bands do not overlap
at all (Saka brow 1.4228..1.4257, eye 1.3973..1.4104, mouth 1.3387..1.3440). Anatomy, not convention.
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_server import _glb_bytes                                      # noqa: E402

from conjure import expressions as X                                     # noqa: E402
from conjure.figures import (morph_centroids, morph_displacements,       # noqa: E402
                             morph_names, split_glb)
from conjure.importer import _face_attrs                                 # noqa: E402

# Verbatim from the two figures in the library, trimmed to the entries the tables reach for.
SAKA = ["Fcl_ALL_Neutral", "Fcl_ALL_Angry", "Fcl_ALL_Fun", "Fcl_ALL_Joy", "Fcl_ALL_Sorrow",
        "Fcl_ALL_Surprised", "Fcl_BRW_Angry", "Fcl_BRW_Surprised", "Fcl_EYE_Close",
        "Fcl_EYE_Close_R", "Fcl_EYE_Close_L", "Fcl_EYE_Joy", "Fcl_MTH_Large", "Fcl_MTH_A",
        "Fcl_MTH_I", "Fcl_MTH_U", "Fcl_MTH_E", "Fcl_MTH_O"]

ALICE = ["Brow_Raise_Inner_L", "Brow_Raise_Inner_R", "Brow_Raise_Outer_L", "Brow_Raise_Outer_R",
         "Brow_Drop_L", "Brow_Drop_R", "Eye_Blink_L", "Eye_Blink_R", "Eye_Squint_L", "Eye_Squint_R",
         "Eye_L_Look_L", "Eye_R_Look_L", "Eye_L_Look_R", "Eye_R_Look_R", "Eye_L_Look_Up",
         "Eye_R_Look_Up", "Eye_L_Look_Down", "Eye_R_Look_Down", "Mouth_Smile_L", "Mouth_Smile_R",
         "Mouth_Pucker", "Mouth_Funnel", "Mouth_Shrug_Upper", "Jaw_Open", "Tongue_Out"]

#: Bianca and Blondie, and the reason `facial()` exists. 22 targets, none of them a face.
TONGUE_ONLY = ["Tongue_up", "Tongue_Out", "T05_Tongue_Roll", "Vagina_Open", "Anus_Open"]

#: Geeky, Nancy, Akari — the commonest case by far. Wardrobe and skin tone wearing the word "morph".
WARDROBE = ["Body_Asian", "Body_Alabaster", "Shirt_Blue", "Shorts_Black", "Hair_Asian", "Pussy2"]


# ------------------------------------------------------------------- schemes

def test_each_rig_is_recognised_from_its_VOCABULARY_alone():
    """Most of these figures arrive through a capture and carry no statement of origin, so the names
    are all there is."""
    assert X.scheme_of(SAKA) == "vrm"
    assert X.scheme_of(ALICE) == "cc"


def test_a_pile_of_wardrobe_morphs_is_not_an_expression_rig():
    assert X.scheme_of(WARDROBE) == ""
    assert X.scheme_of(TONGUE_ONLY) == ""
    assert X.scheme_of([]) == ""


def test_one_stray_eyelid_correction_does_not_make_a_FACE():
    """Moon Girl carries `closed_eyes_correction` among thirteen wardrobe shapes. Calling that an
    expression rig would offer `smile` on a figure whose mouth cannot move, which is a worse answer
    than admitting she has no face."""
    assert X.scheme_of(WARDROBE + ["closed_eyes_correction"]) == ""


def test_facial_separates_a_face_from_everything_else_a_morph_can_be():
    """22 of 38 rigged figures carry morph targets and two carry a face. Counting targets says a
    figure is expressive when it can only change its shorts."""
    assert X.facial(WARDROBE) == []
    assert X.facial(TONGUE_ONLY) == []
    assert len(X.facial(SAKA)) == len(SAKA)
    assert "Tongue_Out" not in X.facial(ALICE)
    assert "Jaw_Open" in X.facial(ALICE)


# ------------------------------------------------------------------ resolving

def test_the_same_request_reaches_a_different_target_on_each_rig():
    """The whole point of a semantic name."""
    vrm, _ = X.resolve(SAKA, {"smile": 1})
    cc, _ = X.resolve(ALICE, {"smile": 1})
    assert vrm == {"Fcl_ALL_Fun": 1.0}
    assert cc == {"Mouth_Smile_L": 1.0, "Mouth_Smile_R": 1.0}


def test_a_weight_scales_the_WHOLE_entry_not_just_its_first_target():
    """A half smile is half of every muscle in it, or it is a grimace."""
    got, _ = X.resolve(ALICE, {"joy": 0.5})
    assert got == {"Mouth_Smile_L": 0.5, "Mouth_Smile_R": 0.5,
                   "Eye_Squint_L": 0.2, "Eye_Squint_R": 0.2}


def test_two_expressions_touching_one_target_ADD_rather_than_overwrite():
    """A face is additive. `surprised` opens the jaw halfway and `aa` opens it 0.7; letting whichever
    came last in a dict win would make the result depend on key order."""
    got, _ = X.resolve(ALICE, {"surprised": 1, "aa": 1})
    assert got["Jaw_Open"] == 1.0            # 0.5 + 0.7, clamped — a jaw does not open further
    assert got["Brow_Raise_Inner_L"] == 1.0


def test_a_raw_target_name_wins_over_the_table():
    """A caller that has inspected a figure can drive anything it carries — including the tongue-only
    rigs, which have no scheme at all and are perfectly drivable by name."""
    got, skipped = X.resolve(TONGUE_ONLY, {"Tongue_Out": 1})
    assert got == {"Tongue_Out": 1.0}
    assert skipped == []


def test_what_a_figure_CANNOT_do_is_reported_not_approximated():
    """VRM aims the eyes with BONES, so that rig has no eye-direction morphs. Quietly returning
    nothing would read as the tool being broken; quietly substituting a head turn would be a lie."""
    got, skipped = X.resolve(SAKA, {"look_left": 1})
    assert got == {}
    assert skipped == ["look_left"]


def test_a_known_expression_a_PARTICULAR_figure_lacks_is_also_reported():
    """Different from an unknown name: the scheme knows `smile`, this figure has no mouth shapes."""
    got, skipped = X.resolve(TONGUE_ONLY + ["Brow_Drop_L", "Eye_Blink_L", "Mouth_Smile_L"],
                             {"look_up": 1})
    assert got == {}
    assert skipped == ["look_up"]


def test_an_unknown_name_is_skipped_rather_than_guessed_at():
    got, skipped = X.resolve(ALICE, {"smirk": 1, "smile": 1})
    assert got == {"Mouth_Smile_L": 1.0, "Mouth_Smile_R": 1.0}
    assert skipped == ["smirk"]


def test_weights_are_clamped_into_range():
    assert X.resolve(ALICE, {"smile": 5})[0]["Mouth_Smile_L"] == 1.0
    assert X.resolve(ALICE, {"smile": -3})[0]["Mouth_Smile_L"] == 0.0
    assert X.resolve(ALICE, {"smile": "loads"})[1] == ["smile"]


def test_both_eyes_move_together_because_one_eye_turning_is_a_LAZY_EYE():
    got, _ = X.resolve(ALICE, {"look_left": 1})
    assert got == {"Eye_L_Look_L": 1.0, "Eye_R_Look_L": 1.0}


def test_every_expression_in_the_vocabulary_resolves_on_at_least_one_rig():
    """A name a director can be told about and no figure can perform is a name that should not be in
    the list."""
    for name in X.EXPRESSIONS + X.VISEMES:
        landed = (X.resolve(SAKA, {name: 1})[0] or X.resolve(ALICE, {name: 1})[0]
                  or name == "neutral")
        assert landed, f"{name} resolves on neither rig"


def test_every_table_entry_names_a_target_a_REAL_figure_carries():
    """The tables are written against two specific vocabularies. A typo in one — `Fcl_MTH_Larg` — would
    silently resolve to nothing and read as "this figure cannot", forever."""
    for scheme, vocab in (("vrm", SAKA), ("cc", ALICE)):
        for expression, entry in X.SCHEMES[scheme].items():
            for target in entry:
                assert target in vocab, f"{scheme}/{expression} names {target}, which no figure has"


# -------------------------------------------------------- the geometric check

def test_anatomy_is_what_the_region_check_tests():
    """brow above eye above mouth, on any face there has ever been."""
    assert X.check_regions({"Fcl_BRW_Angry": 1.43, "Fcl_EYE_Close": 1.40, "Fcl_MTH_A": 1.34}) == []
    assert X.check_regions({"Brow_Drop_L": 1.60, "Eye_Blink_L": 1.58, "Jaw_Open": 1.52}) == []


def test_a_target_classified_onto_the_wrong_feature_is_CAUGHT():
    """The failure a name-only table hides: something called a brow that acts on the mouth."""
    problems = X.check_regions({"Fcl_BRW_Angry": 1.33, "Fcl_EYE_Close": 1.40, "Fcl_MTH_A": 1.34})
    assert problems and "brow" in problems[0]


def test_bands_that_merely_TOUCH_are_a_finding():
    """Measured, the bands do not overlap at all on either rig, so anything looser would pass a table
    that has genuinely gone wrong."""
    assert X.check_regions({"Brow_Drop_L": 1.50, "Jaw_Open": 1.50})


def test_a_recognised_RIG_that_measures_as_empty_is_a_FAILURE_not_a_pass():
    """Alice's entire face measured as empty once — a sparse-only accessor read as zero vertices — and
    this returned `[]`, which every caller read as "geometry agrees". A green result that cannot go
    red is worse than no check, because it is trusted."""
    assert X.check_regions({name: None for name in ALICE})      # cc rig, nothing measurable
    assert X.check_regions({"Jaw_Open": 1.52, "Brow_Drop_L": None, "Eye_Blink_L": None})


def test_a_figure_making_NO_ordering_claim_is_not_applicable_rather_than_broken():
    """Moon Girl carries `closed_eyes_correction` among thirteen wardrobe morphs. There is no ordering
    to be wrong about, and reporting it as a failure buries the real ones — three of the five figures
    with any facial shape at all are in exactly this state."""
    assert X.check_regions({}) == []
    assert X.check_regions({"Body_Alabaster": 1.0}) == []
    assert X.check_regions({"closed_eyes_correction": 1.58, "Body_Asian": 1.0}) == []


def test_a_target_whose_geometry_contradicts_its_NAME_is_caught():
    """A check the region test cannot make. A rig with `Brow_Raise` and `Brow_Drop` modelled the wrong
    way round passes the regions completely — both are brows, both in the brow band — and puts an
    angry face on every request for a surprised one."""
    assert X.check_directions({"Brow_Drop_L": -0.002, "Mouth_Smile_L": +0.007}) == []
    problems = X.check_directions({"Brow_Drop_L": +0.002})
    assert problems and "inverted" in problems[0]


def test_a_WHOLE_FACE_composite_is_not_held_to_a_direction():
    """The first version asserted `Fcl_ALL_Joy` rises, because joy is a smile. Measured, it FALLS: it
    is `Fcl_EYE_Joy` (-0.00584 — happy eyes close, and an eyelid closes downward) plus `Fcl_MTH_Joy`
    (-0.00246 — the mouth opens) against `Fcl_BRW_Joy` (+0.00073). A composite's net vertical motion
    is the sum of parts pulling opposite ways and asserts nothing.

    The same mistake as putting `face` into `ROLE_ORDER`, and both checks now exclude composites."""
    assert X.check_directions({"Fcl_ALL_Joy": -0.00376}) == []
    assert X.check_directions({"Fcl_ALL_Surprised": -0.00329}) == []


def test_a_mostly_SIDEWAYS_shape_says_nothing_about_up_and_down():
    """`Eye_L_Look_L` moves horizontally; a near-zero vertical mean is not evidence of anything."""
    assert X.check_directions({"Eye_L_Look_Up": 0.000001}) == []


# --------------------------------------------------------------- reading a GLB

def _morph_glb(*, sparse: bool) -> bytes:
    """A mesh with three morph targets at three heights, stored dense or sparse.

    Sparse is the case that matters: it is how a morph target is NORMALLY stored, because a target
    moves a few hundred vertices of a mesh with sixty thousand.
    """
    # Four vertices at four heights; each target moves exactly one of them.
    positions = [(0.0, 1.60, 0.0), (0.0, 1.58, 0.0), (0.0, 1.52, 0.0), (0.0, 0.10, 0.0)]
    blob = b"".join(struct.pack("<fff", *p) for p in positions)
    accessors = [{"componentType": 5126, "type": "VEC3", "count": 4, "bufferView": 0}]
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": len(blob)}]
    targets = []

    for i in range(3):
        if sparse:
            # No bufferView at all: the base is implicitly all zeros and ONE index is overridden.
            idx_off = len(blob)
            blob += struct.pack("<H", i)
            blob += b"\x00" * (-len(blob) % 4)
            val_off = len(blob)
            blob += struct.pack("<fff", 0.0, 0.01, 0.0)
            views.append({"buffer": 0, "byteOffset": idx_off, "byteLength": 2})
            views.append({"buffer": 0, "byteOffset": val_off, "byteLength": 12})
            accessors.append({"componentType": 5126, "type": "VEC3", "count": 4,
                              "sparse": {"count": 1,
                                         "indices": {"bufferView": len(views) - 2, "byteOffset": 0,
                                                     "componentType": 5123},
                                         "values": {"bufferView": len(views) - 1, "byteOffset": 0}}})
        else:
            off = len(blob)
            for j in range(4):
                blob += struct.pack("<fff", 0.0, 0.01 if j == i else 0.0, 0.0)
            views.append({"buffer": 0, "byteOffset": off, "byteLength": 48})
            accessors.append({"componentType": 5126, "type": "VEC3", "count": 4,
                              "bufferView": len(views) - 1})
        targets.append({"POSITION": len(accessors) - 1})

    doc = {"asset": {"version": "2.0"},
           "meshes": [{"name": "Face",
                       "extras": {"targetNames": ["Brow_Drop_L", "Eye_Blink_L", "Jaw_Open"]},
                       "primitives": [{"attributes": {"POSITION": 0}, "targets": targets}]}],
           "accessors": accessors, "bufferViews": views,
           "buffers": [{"byteLength": len(blob)}]}
    return _glb_bytes(doc, blob)


def test_morph_names_reads_the_only_place_glTF_can_put_one():
    doc, _ = split_glb(_morph_glb(sparse=False))
    assert morph_names(doc) == ["Brow_Drop_L", "Eye_Blink_L", "Jaw_Open"]


def test_a_SPARSE_morph_target_is_read_and_not_reported_as_moving_nothing():
    """The bug this pins. `_read_vec3` returned on `"bufferView" not in acc`, which for a sparse-only
    accessor yields nothing — so every target read as moving no vertices, `morph_centroids` came back
    empty, and `check_regions` passed vacuously. Alice's whole face was invisible that way."""
    for sparse in (False, True):
        doc, blob = split_glb(_morph_glb(sparse=sparse))
        got = morph_centroids(doc, blob)
        assert set(got) == {"Brow_Drop_L", "Eye_Blink_L", "Jaw_Open"}, f"sparse={sparse}"
        assert round(got["Brow_Drop_L"], 2) == 1.60, f"sparse={sparse}"
        assert round(got["Eye_Blink_L"], 2) == 1.58, f"sparse={sparse}"
        assert round(got["Jaw_Open"], 2) == 1.52, f"sparse={sparse}"


def test_a_measured_face_passes_both_of_its_own_checks():
    doc, blob = split_glb(_morph_glb(sparse=True))
    assert X.check_regions(morph_centroids(doc, blob)) == []
    # Every target in the fixture moves the mesh UP, so the two whose names say otherwise — a jaw
    # opening and a brow dropping — are both caught, and the eye blink (which claims no direction)
    # is not.
    problems = X.check_directions(morph_displacements(doc, blob))
    assert len(problems) == 2
    assert {p.split()[0] for p in problems} == {"Jaw_Open", "Brow_Drop_L"}


def test_the_catalog_counts_DISTINCT_targets_not_rows():
    """`morph_targets` used to sum `len(primitive.targets)` over every primitive of every mesh, so a
    vocabulary shared by four primitives counted four times: Saka read **399** for 57 real targets and
    Alice **172** for 34. "Who can smile" was already a query and it was querying a meaningless number.
    """
    doc, _ = split_glb(_morph_glb(sparse=False))
    # The same vocabulary on a second primitive, which is exactly what inflated the old count.
    mesh = doc["meshes"][0]
    mesh["primitives"].append(json.loads(json.dumps(mesh["primitives"][0])))

    attrs = _face_attrs(doc)
    assert attrs["morph_targets"] == 3
    assert attrs["facial_morphs"] == 3
    assert attrs["expression_scheme"] == "cc"
    assert attrs["morph_names"] == ["Brow_Drop_L", "Eye_Blink_L", "Jaw_Open"]


def test_a_model_with_no_morphs_records_a_zero_and_no_vocabulary():
    """Not an empty list of names: 76 of 98 models are in this state and a key per model costs more
    than it says."""
    assert _face_attrs({"meshes": []}) == {"morph_targets": 0}
