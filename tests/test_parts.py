"""The figure-parts vocabulary (conjure/parts.py) — which mesh is clothing, hair, a shoe, a body.

The rules are fitted to 96 distinct mesh-node names across twenty captured figures, and the tests are
mostly about the two decisions that are easy to get backwards: rule ORDER, and what `removable` means.
"""

import json

import pytest

from conjure import parts


@pytest.fixture
def vocab():
    return parts.load_vocabulary()


def _doc(*names):
    return {"nodes": [{"name": n, "mesh": 0} for n in names]}


def test_the_bundled_vocabulary_classifies_the_corpus_it_was_fitted_to(vocab):
    """Real names, from real captures. A prefix rule would be the obvious approach and does not work:
    `clothes_*` appears in only 8 of 20 captures and the rest say `Dress`, `Shorts`, `underwear`."""
    got = parts.classify(_doc(
        "clothes_Teacher_Shirt", "Dress", "Shorts", "underwear", "Kimono", "Strap_Top",
        "Canvas_shoes", "clothes_weddingdress_heels_L", "heels",
        "item_necklace", "Glasses", "item_weddingveil",
        "Hair", "Pigtail_braid_side", "Scalp_Female",
        "CC_Base_Body", "Body", "Oktoberfest_milf",
        "Eylashes", "CC_Base_Eye", "Custom_Teeth"), vocab)["parts"]
    for name in ("clothes_Teacher_Shirt", "Dress", "Shorts", "underwear", "Kimono", "Strap_Top"):
        assert got[name] == "clothing", name
    for name in ("Canvas_shoes", "clothes_weddingdress_heels_L", "heels"):
        assert got[name] == "shoes", name
    for name in ("item_necklace", "Glasses", "item_weddingveil"):
        assert got[name] == "accessory", name
    for name in ("Hair", "Pigtail_braid_side", "Scalp_Female"):
        assert got[name] == "hair", name
    for name in ("CC_Base_Body", "Body", "Oktoberfest_milf"):
        assert got[name] == "body", name
    for name in ("Eylashes", "CC_Base_Eye", "Custom_Teeth"):
        assert got[name] == "face", name


def test_BODY_IS_THE_LAST_RULE_because_its_names_contain_the_others(vocab):
    """The bug this catches shipped: `body` was first, and its name-matches are substrings of the more
    specific ones — so `model_britney_hair` classified as BODY and could never be taken off, and
    `model_britney_bride_eyelashes` with it."""
    assert [r["category"] for r in vocab["rules"]][-1] == "body"
    got = parts.classify(_doc("model_britney", "model_britney_hair",
                              "model_britney_eyelashes", "clothes_Teacher_Hair"), vocab)["parts"]
    assert got["model_britney"] == "body"
    assert got["model_britney_hair"] == "hair", "the more specific rule has to win"
    assert got["model_britney_eyelashes"] == "face"
    assert got["clothes_Teacher_Hair"] == "hair", "hair before clothing, despite the name"


def test_hair_is_removable_but_is_NOT_clothing(vocab):
    """Stripping a figure to check its integrity should not scalp it. `face` and `body` are not
    removable at all — taking the eyes out of a head is not undressing it."""
    found = parts.classify(_doc("Dress", "Hair", "Canvas_shoes", "item_necklace",
                                "Body", "CC_Base_Eye"), vocab)
    out = parts.removable(found["parts"], vocab)
    assert out == {"accessory": ["item_necklace"], "clothing": ["Dress"],
                   "hair": ["Hair"], "shoes": ["Canvas_shoes"]}
    assert "body" not in out and "face" not in out


def test_only_nodes_that_CARRY_A_MESH_are_parts(vocab):
    """A bone called `DEF_Skirt01` drives a garment and is not one. Hiding it would do nothing while
    implying it had."""
    doc = {"nodes": [{"name": "Dress", "mesh": 0}, {"name": "DEF_Skirt01"}, {"name": "Skirt_bone"}]}
    assert parts.classify(doc, vocab)["parts"] == {"Dress": "clothing"}


def test_what_the_vocabulary_does_not_know_is_REPORTED(vocab):
    """That list is the vocabulary's backlog and the only honest measure of its coverage. A classifier
    that silently called everything unknown "body" would read as complete while refusing to undress
    anyone. Across all twenty captured figures exactly one name is unclassified: `Beer`."""
    found = parts.classify(_doc("Dress", "Beer", "Zorblax"), vocab)
    assert found["unclassified"] == ["Beer", "Zorblax"]
    assert "Beer" not in found["parts"]


def test_a_user_vocabulary_SHADOWS_the_bundled_one_rather_than_merging(tmp_path):
    """Merging two rule lists gives an order nobody can predict from either file, and order is what
    decides a match. Copy the bundled file and edit it."""
    (tmp_path / "parts.json").write_text(json.dumps(
        {"revision": 99, "rules": [{"category": "clothing", "match": ["zorblax"]}],
         "removable": ["clothing"]}))
    mine = parts.load_vocabulary([tmp_path])
    assert mine["revision"] == 99
    assert parts.categorize("Zorblax_cape", mine) == "clothing"
    assert parts.categorize("Dress", mine) is None, "the bundled rules are not merged in"


def test_a_broken_vocabulary_does_not_stop_an_import(tmp_path, capsys):
    (tmp_path / "parts.json").write_text("{not json")
    got = parts.load_vocabulary([tmp_path])
    assert got["rules"] == [] and got["revision"] == 0
    assert "not readable JSON" in capsys.readouterr().out
