"""`conjure.describe` — the free layer of figure search text.

Pure function of a catalog row, so these are cheap and they are the reason this layer exists: it can be
trusted, where the vision layer beside it cannot.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conjure.describe import structured_text                      # noqa: E402

ALICE = {
    "rigged": True, "height_m": 1.7286, "morph_targets": 172,
    "humanoid_source": "convention:cc-base",
    "humanoid": {f"b{i}": f"n{i}" for i in range(52)},
    "parts": {"Rolled_sleeves_shirt": "clothing", "Skirt_Black": "clothing",
              "Underwear_Bottoms": "clothing", "Canvas_shoes": "shoes",
              "Pigtail_braid_side": "hair", "Side_Swept": "hair",
              "CC_Base_Body": "body", "CC_Base_Eye": "face"},
    "parts_hidden": ["Underwear_Bottoms", "Side_Swept"],
}


def test_a_figure_reads_as_a_sentence_a_person_would_search():
    out = structured_text("Alice", ALICE, clips=["LayTableIdle", "KneelAwait"], voiced=2)
    assert out.startswith("Alice. ")
    assert "1.73 m tall" in out
    assert "Rolled sleeves shirt" in out, "the mesh name is the most searchable text in the file"
    assert "Canvas shoes" in out and "Pigtail braid side" in out
    assert "2 animations including KneelAwait, LayTableIdle" in out
    assert "2 clips with recorded speech" in out
    assert "Character Creator rig, 52 mapped bones" in out


def test_a_HIDDEN_part_is_not_described_as_worn():
    """The lesson that made this layer worth writing. A vision pass on a render of EVERY mesh called
    Alice's hidden underwear 'a bright pink fanny pack worn diagonally across her front', and the losing
    half of a hair variant 'a striking white streak'. Both described in earnest. Text derived from the
    row must not repeat the mistake."""
    out = structured_text("Alice", ALICE)
    assert "Underwear Bottoms" not in out
    assert "Side Swept" not in out
    assert "2 alternate parts that can be swapped in" in out, "but they are still worth knowing about"


def test_body_and_face_meshes_are_not_clothing():
    out = structured_text("Alice", ALICE)
    assert "CC Base Body" not in out and "CC Base Eye" not in out


def test_a_figure_with_nothing_to_say_says_nothing():
    assert structured_text("Bare", {"rigged": True}) == ""
    assert structured_text("Crate", {"rigged": False, "height_m": 1.0}) == "", "a prop is not a figure"


def test_only_what_is_known_no_padding():
    """A figure with no clips says nothing about clips. Padding is what makes a search index lie."""
    out = structured_text("Grace", {"rigged": True, "height_m": 1.69,
                                    "humanoid_source": "convention:rigify-fk",
                                    "humanoid": {f"b{i}": "x" for i in range(51)}})
    assert out == "Grace. 1.69 m tall; Rigify rig, 51 mapped bones."
    for absent in ("animation", "morph", "wearing", "0 "):
        assert absent not in out


def test_an_unrecognised_convention_still_reports_the_bones():
    out = structured_text("X", {"rigged": True, "humanoid_source": "inferred",
                                "humanoid": {"hips": "h"}})
    assert "rig, 1 mapped bone" in out and "None" not in out
