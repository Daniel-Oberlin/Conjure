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


def test_save_load_roundtrips_the_doc(tmp_path):
    s = store()
    s.apply_patch([{"op": "add", "entity": {"id": "box", "components": {"geometry": {"primitive": "box"}}}}])
    s.apply_patch([{"op": "env", "set": {"room.edgesVisible": False}}])
    path = tmp_path / "world.json"
    s.save(path)
    loaded = WorldStore.load(path)
    assert loaded.doc["rev"] == s.doc["rev"]
    assert any(e["id"] == "box" for e in loaded.doc["entities"])
    assert loaded.doc["environment"]["room"]["edgesVisible"] is False


def test_save_is_atomic_no_partial_file_on_reopen(tmp_path):
    s = store()
    path = tmp_path / "world.json"
    s.save(path)
    s.apply_patch([{"op": "add", "entity": {"id": "e2", "components": {}}}])
    s.save(path)                                    # overwrite must fully replace, not append
    assert WorldStore.load(path).doc["rev"] == s.doc["rev"]
    assert not (tmp_path / "world.json.tmp").exists()  # temp cleaned up via rename


def _doc(name="W"):
    return {"id": "t", "name": name, "rev": 0, "environment": {}, "entities": []}


def test_repository_save_list_load_delete(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    assert repo.list("private/builder") == []
    s = WorldStore(_doc("Blade Runner"))
    s.apply_patch([{"op": "add", "entity": {"id": "rain", "components": {}}}])
    repo.save("private/builder", "bladerunner1", s)
    assert repo.list("private/builder") == ["bladerunner1"]
    assert repo.exists("private/builder", "bladerunner1")
    loaded = repo.load("private/builder", "bladerunner1")
    assert any(e["id"] == "rain" for e in loaded.doc["entities"])
    assert repo.delete("private/builder", "bladerunner1") is True
    assert repo.list("private/builder") == []


def test_repository_scope_isolation(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    repo.save("private/builder", "w", WorldStore(_doc()))
    repo.save("private/dungeonmaster", "w", WorldStore(_doc()))
    assert repo.list("private/builder") == ["w"]
    assert repo.list("private/dungeonmaster") == ["w"]          # same name, separate scopes
    repo.delete("private/builder", "w")
    assert repo.list("private/dungeonmaster") == ["w"]          # untouched


def test_repository_active_pointer_roundtrips_and_clears_on_delete(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    repo.save("private/builder", "home", WorldStore(_doc()))
    assert repo.get_active("private/builder") is None
    repo.set_active("private/builder", "home")
    assert repo.get_active("private/builder") == "home"
    repo.delete("private/builder", "home")
    assert repo.get_active("private/builder") is None          # pointer cleared with its target
    assert repo.list("private/builder") == []                 # _active.txt not listed as a world


def test_repository_recall_is_normalized(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    repo.save("private/builder", "Blade Runner 1", WorldStore(_doc("Blade Runner 1")))
    # case / spaces / underscores / hyphens are all interchangeable on recall
    for variant in ("blade runner 1", "BLADE_RUNNER_1", "blade-runner-1", "Blade-Runner 1"):
        assert repo.exists("private/builder", variant)
        assert repo.load("private/builder", variant) is not None
    assert repo.list("private/builder") == ["blade-runner-1"]   # one canonical slug, no dupes


def test_repository_supports_nested_world_paths(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    repo.save("private/dm", "Castle Quest/Dining Hall", WorldStore(_doc()))
    repo.save("private/dm", "castle quest/throne room", WorldStore(_doc()))
    repo.save("private/dm", "default", WorldStore(_doc()))
    assert repo.list("private/dm") == ["castle-quest/dining-hall", "castle-quest/throne-room", "default"]
    # nested recall is normalized per segment, just like flat names
    assert repo.exists("private/dm", "Castle_Quest / Dining-Hall")
    assert (tmp_path / "private" / "dm" / "castle-quest" / "dining-hall.json").exists()
    # a node can be both a leaf world and a parent of others
    repo.save("private/dm", "castle-quest", WorldStore(_doc()))
    assert "castle-quest" in repo.list("private/dm")
    repo.delete("private/dm", "castle-quest/throne-room")
    assert "castle-quest/dining-hall" in repo.list("private/dm")   # sibling untouched


def test_repository_neutralizes_punctuation_but_rejects_traversal(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    # stray punctuation in a flat name is stripped to a safe slug
    repo.save("private/builder", "My World!", WorldStore(_doc()))
    assert (tmp_path / "private" / "builder" / "my-world.json").exists()
    # explicit traversal segments / empties are rejected — a world can't escape its scope
    for bad in ("../evil", "castle/../secret", ".", "", "  ", "/"):
        with pytest.raises(ValueError):
            repo.save("private/builder", bad, WorldStore(_doc()))
    assert not (tmp_path / "evil.json").exists() and not (tmp_path / "private" / "secret.json").exists()
    for bad_scope in ("../../etc", "private/..", ""):
        with pytest.raises(ValueError):
            repo.list(bad_scope)


def _space(name="home"):
    return {"owner": "daniel", "name": name, "public": True, "geolocation": None,
            "surfaces": [{"id": "real_wall_0", "meta": {"real": True, "semantic": "wall"}}],
            "boundary": {"floorPolygon": [[0, 0], [3, 0], [3, 3]], "height": 2.6}}


def test_spacestore_save_list_load_delete(tmp_path):
    from conjure.world import SpaceStore
    s = SpaceStore(tmp_path)
    assert s.list("daniel") == []
    s.save("daniel", "Home", _space("Home"))
    assert s.list("daniel") == ["home"]                      # name slugified
    assert s.exists("daniel", "home")
    sp = s.load("daniel", "HOME")                            # recall is case/format-insensitive
    assert sp["boundary"]["height"] == 2.6 and len(sp["surfaces"]) == 1
    assert s.delete("daniel", "home") is True and s.list("daniel") == []


def test_spacestore_is_per_user(tmp_path):
    from conjure.world import SpaceStore
    s = SpaceStore(tmp_path)
    s.save("daniel", "home", _space())
    s.save("alice", "home", _space())                        # same name, different owner
    assert s.list("daniel") == ["home"] and s.list("alice") == ["home"]
    s.delete("daniel", "home")
    assert s.list("alice") == ["home"]                       # untouched


def test_spacestore_active_pointer_and_bad_user(tmp_path):
    from conjure.world import SpaceStore
    s = SpaceStore(tmp_path)
    s.save("daniel", "home", _space())
    assert s.get_active("daniel") is None
    s.set_active("daniel", "Home")
    assert s.get_active("daniel") == "home"                  # canonical slug
    s.delete("daniel", "home")
    assert s.get_active("daniel") is None                    # pointer cleared with its target
    for bad in ("../etc", "a/b", ".", ""):
        with pytest.raises(ValueError):
            s.list(bad)
