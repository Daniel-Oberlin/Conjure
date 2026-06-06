"""Unit tests for the world document + patch protocol (the foundation everything rides on)."""

import pytest

from conjure.world import WorldStore


def store() -> WorldStore:
    return WorldStore({"id": "t", "name": "T", "rev": 0, "environment": {"sky": {"color": "#000"}}, "entities": []})


def test_add_bumps_rev_and_inverse_removes():
    s = store()
    patch = s.apply_patch([{"op": "add", "entity": {"id": "box", "components": {"geometry": {"primitive": "box"}}}}])
    assert patch["rev"] == 1 and s.doc["rev"] == 1
    assert any(e["id"] == "box" for e in s.doc["entities"])
    assert patch["inverse"] == [{"op": "remove", "id": "box"}]


def test_update_dotted_paths_and_inverse_restores():
    s = store()
    s.apply_patch([{"op": "add", "entity": {
        "id": "e", "transform": {"position": [0.0, 0.0, 0.0]}, "components": {"material": {"color": "red"}}}}])
    patch = s.apply_patch([{"op": "update", "id": "e", "set": {
        "components.material.color": "blue", "transform.position": [1, 2, 3]}}])
    e = next(x for x in s.doc["entities"] if x["id"] == "e")
    assert e["components"]["material"]["color"] == "blue"
    assert e["transform"]["position"] == [1, 2, 3]
    inv = patch["inverse"][0]
    assert inv["op"] == "update" and inv["set"]["components.material.color"] == "red"
    assert inv["set"]["transform.position"] == (0.0, 0.0, 0.0)


def test_remove_inverse_readds_full_entity():
    s = store()
    s.apply_patch([{"op": "add", "entity": {"id": "e", "components": {"material": {"color": "green"}}}}])
    patch = s.apply_patch([{"op": "remove", "id": "e"}])
    assert not any(x["id"] == "e" for x in s.doc["entities"])
    inv = patch["inverse"][0]
    assert inv["op"] == "add" and inv["entity"]["id"] == "e"


def test_env_op_replaces_and_inverse_restores_old_sky():
    s = store()
    patch = s.apply_patch([{"op": "env", "set": {"sky": {"src": "/a.png"}}}])
    assert s.doc["environment"]["sky"] == {"src": "/a.png"}
    assert patch["inverse"][0]["set"]["sky"] == {"color": "#000"}


def test_dotted_set_creates_missing_nested_dicts():
    s = store()
    s.apply_patch([{"op": "add", "entity": {"id": "e", "components": {}}}])
    s.apply_patch([{"op": "update", "id": "e", "set": {"components.light.intensity": 2.0}}])
    e = next(x for x in s.doc["entities"] if x["id"] == "e")
    assert e["components"]["light"]["intensity"] == 2.0


def test_add_existing_id_replaces():
    s = store()
    s.apply_patch([{"op": "add", "entity": {"id": "e", "components": {"material": {"color": "red"}}}}])
    s.apply_patch([{"op": "add", "entity": {"id": "e", "components": {"material": {"color": "blue"}}}}])
    matches = [x for x in s.doc["entities"] if x["id"] == "e"]
    assert len(matches) == 1 and matches[0]["components"]["material"]["color"] == "blue"


def test_update_unknown_entity_raises():
    s = store()
    with pytest.raises(ValueError):
        s.apply_patch([{"op": "update", "id": "nope", "set": {"transform.position": [1, 1, 1]}}])
