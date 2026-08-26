"""Unit tests for the world document + patch protocol (the foundation everything rides on)."""

import json

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
    s.apply_patch([{"op": "env", "set": {"spacePresentation.edgesVisible": False}}])
    path = tmp_path / "world.json"
    s.save(path)
    loaded = WorldStore.load(path)
    assert loaded.doc["rev"] == s.doc["rev"]
    assert any(e["id"] == "box" for e in loaded.doc["entities"])
    assert loaded.doc["environment"]["spacePresentation"]["edgesVisible"] is False


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


def test_repository_list_public_discovers_public_sessions(tmp_path):
    # Discovery is by SESSION visibility now (§8.2): a public session's worlds are listed; a private
    # session's are not. Back the repo with a SessionRepository — the way the server constructs it.
    from conjure.world import WorldRepository, SessionRepository
    se = SessionRepository(tmp_path)
    repo = WorldRepository(tmp_path, sessions=se)
    se.save_meta("daniel/agents/builder", "session-1", {"public": True})
    repo.save("daniel/agents/builder", "default", WorldStore(_doc()))                 # → daniel session-1 (public)
    se.save_meta("friend/agents/builder", "session-1", {"public": True})
    repo.save("friend/agents/builder", "test-world", WorldStore(_doc()))              # → friend session-1 (public)
    se.save_meta("friend/agents/builder", "session-2", {"public": False})             # friend's PRIVATE session
    se.worlds("friend/agents/builder", "session-2").save("secret", WorldStore(_doc()))
    avail = repo.list_public(exclude_scope="friend/agents/builder")                   # friend looks outward
    assert {(w["owner"], w["name"]) for w in avail} == {("daniel", "default")}        # only daniel's public
    seen = repo.list_public()                                                         # global view
    assert ("friend", "test-world") in {(w["owner"], w["name"]) for w in seen}
    assert ("friend", "secret") not in {(w["owner"], w["name"]) for w in seen}        # private session excluded


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
    wid = repo.resolve("private/builder", "home")
    assert repo.get_active("private/builder") == wid       # the pointer holds the ID, not the name
    repo.delete("private/builder", "home")
    assert repo.get_active("private/builder") is None          # pointer cleared with its target
    assert repo.list("private/builder") == []                 # _active.txt not listed as a world


def test_repository_session_pointer_roundtrips_globally(tmp_path):
    # The single global session pointer (scope, world) — what's live across the whole server, distinct
    # from the per-scope _active.txt. It carries the scope (hence the agent, derived) and the world name.
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    assert repo.get_session() is None                          # unset on a fresh cache
    repo.set_session("daniel/agents/outdoor", "beach")
    assert repo.get_session() == ("daniel/agents/outdoor", "beach")
    repo.set_session("daniel/agents/builder", "wld_00000000ab")
    assert repo.get_session() == ("daniel/agents/builder", "wld_00000000ab")   # an ID, stored verbatim
    assert repo.list_users() == []                             # _session.txt is a root file, not a user


def test_repository_recall_is_normalized(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    repo.save("private/builder", "Blade Runner 1", WorldStore(_doc("Blade Runner 1")))
    # case / spaces / underscores / hyphens are all interchangeable on recall
    for variant in ("blade runner 1", "BLADE_RUNNER_1", "blade-runner-1", "Blade-Runner 1"):
        assert repo.exists("private/builder", variant)
        assert repo.load("private/builder", variant) is not None
    # The NAME is kept as the person typed it; only *matching* is slug-insensitive.
    assert repo.list("private/builder") == ["Blade Runner 1"]


def test_worlds_are_flat_and_stored_under_their_id(tmp_path):
    # Hierarchical world names are retired: sessions are the grouping now, and "is this subdirectory also
    # a world?" never had a good answer. Files are `<id>.json` in one flat directory.
    from conjure.world import WorldRepository, is_world_id
    repo = WorldRepository(tmp_path)
    wid = repo.save("private/dm", "Dining Hall", WorldStore(_doc()))
    assert is_world_id(wid)
    assert (tmp_path / "private" / "dm" / f"{wid}.json").exists()
    assert not (tmp_path / "private" / "dm" / "dining-hall.json").exists()
    assert repo.list("private/dm") == ["Dining Hall"]
    assert repo.entries("private/dm") == [{"id": wid, "name": "Dining Hall"}]


def test_repository_neutralizes_punctuation_but_rejects_traversal(tmp_path):
    from conjure.world import WorldRepository
    repo = WorldRepository(tmp_path)
    # A name is never a path now (the file is `<id>.json`), so traversal can't escape by construction —
    # but a name that slugs to nothing is still rejected rather than silently accepted.
    wid = repo.save("private/builder", "My World!", WorldStore(_doc()))
    assert (tmp_path / "private" / "builder" / f"{wid}.json").exists()
    assert repo.list("private/builder") == ["My World!"]
    for bad in (".", "", "  ", "/", "!!!"):
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


# -- SessionRepository (docs/specs/agents.md §7.1) ------------------------------------------------

def _meta(title="Session 1", agent="builder"):
    return {"id": "session-1", "owner": "daniel", "agent": agent, "title": title,
            "public": True, "active_world": "home", "llm": ""}


def test_sessionrepo_meta_save_list_load_delete(tmp_path):
    from conjure.world import SessionRepository
    repo = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    assert repo.list(scope) == []
    assert not repo.exists(scope, "session-1")
    repo.save_meta(scope, "session-1", _meta())
    assert repo.list(scope) == ["session-1"]
    assert repo.exists(scope, "session-1")
    assert repo.load_meta(scope, "session-1")["title"] == "Session 1"
    repo.save_meta(scope, "session-1", _meta(title="Renamed"))          # retitle = metadata edit
    assert repo.load_meta(scope, "session-1")["title"] == "Renamed"
    assert repo.delete(scope, "session-1") is True
    assert repo.list(scope) == [] and repo.delete(scope, "session-1") is False


def test_sessionrepo_delete_takes_the_whole_tree(tmp_path):
    # Worlds/state/transcript belong to the session (§8.10) — deleting it removes them too.
    from conjure.world import SessionRepository
    repo = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    repo.save_meta(scope, "session-1", _meta())
    repo.worlds_dir(scope, "session-1").mkdir(parents=True)
    (repo.worlds_dir(scope, "session-1") / "home.json").write_text("{}")
    repo.state_dir(scope, "session-1").mkdir(parents=True)
    (repo.state_dir(scope, "session-1") / "map.json").write_text("{}")
    repo.transcript_path(scope, "session-1").write_text("{}\n")
    assert repo.delete(scope, "session-1") is True
    assert not repo.dir(scope, "session-1").exists()


def test_sessionrepo_active_pointer_roundtrips_and_clears_on_delete(tmp_path):
    from conjure.world import SessionRepository
    repo = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    repo.save_meta(scope, "session-1", _meta())
    assert repo.get_active(scope) is None
    repo.set_active(scope, "session-1")
    assert repo.get_active(scope) == "session-1"
    repo.delete(scope, "session-1")
    assert repo.get_active(scope) is None                                # cleared with its target
    assert repo.list(scope) == []                                        # _active.txt not a session


def test_sessionrepo_scope_isolation(tmp_path):
    from conjure.world import SessionRepository
    repo = SessionRepository(tmp_path)
    repo.save_meta("daniel/agents/builder", "session-1", _meta())
    repo.save_meta("daniel/agents/outdoor", "session-1", _meta(agent="outdoor"))  # same id, other scope
    assert repo.list("daniel/agents/builder") == ["session-1"]
    assert repo.list("daniel/agents/outdoor") == ["session-1"]
    repo.delete("daniel/agents/builder", "session-1")
    assert repo.list("daniel/agents/outdoor") == ["session-1"]                     # untouched


def test_sessionrepo_paths_and_rejects_traversal(tmp_path):
    from conjure.world import SessionRepository
    repo = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    base = tmp_path / "daniel" / "agents" / "builder" / "sessions" / "session-1"
    assert repo.meta_path(scope, "session-1") == base / "session.json"
    assert repo.transcript_path(scope, "session-1") == base / "transcript.jsonl"
    assert repo.worlds_dir(scope, "session-1") == base / "worlds"
    assert repo.state_dir(scope, "session-1") == base / "state"
    for bad_id in ("../evil", "a/b", ".", "", "  "):
        with pytest.raises(ValueError):
            repo.dir(scope, bad_id)
    for bad_scope in ("../../etc", "daniel/..", ""):
        with pytest.raises(ValueError):
            repo.list(bad_scope)


# -- WorldDir (the name-addressed layer WorldRepository + SessionRepository both reuse) -----------

def test_worlddir_roundtrip_and_active_pointer(tmp_path):
    from conjure.world import WorldDir
    wd = WorldDir(tmp_path / "worlds")
    assert wd.list() == [] and wd.get_active() is None
    home = wd.save("home", WorldStore(_doc("Home")))
    throne = wd.save("Throne Room", WorldStore(_doc()))
    assert wd.list() == ["home", "Throne Room"]                       # name-sorted, verbatim
    assert wd.exists("HOME") and wd.load("home").doc["name"] == "home"
    wd.set_active("home")
    assert wd.get_active() == home                                    # the pointer holds the ID
    assert wd.delete("home") is True and wd.get_active() is None      # pointer cleared with its target
    assert (tmp_path / "worlds" / f"{throne}.json").exists()          # flat, id-keyed


def test_worldrepo_set_live_drives_the_live_scope_but_not_others(tmp_path):
    # The facade addresses the LIVE scope via the server-declared live session (set_live), and any OTHER
    # scope via its own active-session pointer (docs/specs/agents.md §7.1).
    from conjure.world import WorldRepository, SessionRepository
    se = SessionRepository(tmp_path)
    repo = WorldRepository(tmp_path, sessions=se)
    scope = "daniel/agents/builder"
    se.worlds(scope, "session-1").save("s1world", WorldStore(_doc("S1")))
    se.worlds(scope, "session-2").save("s2world", WorldStore(_doc("S2")))
    se.set_active(scope, "session-1")                                # the pointer says session-1
    assert repo.list(scope) == ["s1world"]                           # no set_live → falls back to the pointer
    repo.set_live(scope, "session-2")                                # server declares session-2 live
    assert repo.list(scope) == ["s2world"]                           # live scope now uses the explicit sid,
    assert se.get_active(scope) == "session-1"                       #   even though the pointer still says s1
    # a DIFFERENT scope is unaffected by set_live — it resolves its own pointer
    other = "friend/agents/builder"
    se.worlds(other, "session-1").save("fworld", WorldStore(_doc("F")))
    assert repo.list(other) == ["fworld"]


def test_sessionrepo_worlds_is_a_worlddir_under_the_session(tmp_path):
    from conjure.world import SessionRepository, WorldDir
    repo = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    repo.save_meta(scope, "session-1", _meta())
    wd = repo.worlds(scope, "session-1")
    assert isinstance(wd, WorldDir)
    wid = wd.save("home", WorldStore(_doc("Home")))
    assert wd.list() == ["home"]
    assert (tmp_path / "daniel" / "agents" / "builder" / "sessions" / "session-1"
            / "worlds" / f"{wid}.json").exists()
    repo.delete(scope, "session-1")                                   # deleting the session takes worlds
    assert repo.worlds(scope, "session-1").list() == []


def test_sessionrepo_transcript_append_read_roundtrip_and_tolerates_torn_line(tmp_path):
    from conjure.world import SessionRepository
    repo = SessionRepository(tmp_path)
    scope = "daniel/agents/builder"
    assert repo.read_transcript(scope, "session-1") == []             # none yet
    repo.append_transcript(scope, "session-1", {"role": "user", "by": "daniel", "text": "hi"})
    repo.append_transcript(scope, "session-1", {"role": "assistant", "by": "", "text": "hello"})
    got = repo.read_transcript(scope, "session-1")
    assert [(e["role"], e["by"], e["text"]) for e in got] == [
        ("user", "daniel", "hi"), ("assistant", "", "hello")]
    with repo.transcript_path(scope, "session-1").open("a") as f:     # a crash mid-append → torn last line
        f.write('{"role": "user", "by": "dan')
    assert len(repo.read_transcript(scope, "session-1")) == 2         # torn line skipped, not fatal


# -- StateStore (the generic agent-state store; docs/specs/agents.md §7.4) -------------------------

def test_statestore_crud_and_dotted_paths(tmp_path):
    from conjure.world import StateStore
    st = StateStore(tmp_path)
    assert st.list() == [] and st.read("map") == {}                  # empty doc reads as {}
    st.set("map", "nodes.throne", {"visited": False})                # dotted set creates nesting
    assert st.get("map", "nodes.throne.visited") is False
    st.set("map", "nodes.throne.visited", True)
    assert st.get("map", "nodes.throne.visited") is True
    st.merge("map", {"start": "home"})
    assert st.get("map", "start") == "home" and st.get("map", "nodes.throne.visited") is True
    assert st.list() == ["map"]
    assert st.delete("map", "nodes.throne") is True                  # delete a path
    assert st.get("map", "nodes.throne") is None and st.get("map", "start") == "home"
    assert st.delete("map") is True and st.list() == []              # delete the whole doc
    assert st.delete("map") is False


def test_sessionrepo_state_is_a_statestore_under_the_session(tmp_path):
    from conjure.world import SessionRepository, StateStore
    repo = SessionRepository(tmp_path)
    st = repo.state("daniel/agents/dm", "session-1")
    assert isinstance(st, StateStore)
    st.set("inventory", "lamp", True)
    assert (tmp_path / "daniel" / "agents" / "dm" / "sessions" / "session-1"
            / "state" / "inventory.json").exists()


# -- migration to the user-first session tree (docs/specs/agents.md §7.1) --------------------------

def test_migrate_cache_to_users(tmp_path):
    from conjure.world import migrate_cache_to_users, SessionRepository
    cache = tmp_path
    w = cache / "worlds" / "daniel" / "agents" / "builder"
    (w / "castle-quest").mkdir(parents=True)
    (w / "home.json").write_text(json.dumps(_doc("Home")))
    (w / "castle-quest" / "throne.json").write_text(json.dumps(_doc("Throne")))
    (w / "_active.txt").write_text("home")
    (cache / "worlds" / "_session.txt").write_text("daniel/agents/builder\thome")
    sp = cache / "spaces" / "daniel"; sp.mkdir(parents=True)
    (sp / "home.json").write_text("{}"); (sp / "_active.txt").write_text("home")

    assert migrate_cache_to_users(cache) is True

    base = cache / "users" / "daniel" / "agents" / "builder" / "sessions" / "session-1"
    assert (base / "worlds" / "home.json").exists()
    assert (base / "worlds" / "castle-quest" / "throne.json").exists()      # nesting preserved
    meta = json.loads((base / "session.json").read_text())
    assert (meta["active_world"], meta["agent"], meta["owner"]) == ("home", "builder", "daniel")
    assert (base.parent / "_active.txt").read_text() == "session-1"
    assert (base / "worlds" / "_active.txt").read_text() == "home"           # WorldDir active pointer
    assert (cache / "_session.txt").read_text() == "daniel/agents/builder\tsession-1"
    assert (cache / "users" / "daniel" / "spaces" / "home.json").exists()
    assert not (cache / "worlds").exists() and not (cache / "spaces").exists()   # old trees gone
    repo = SessionRepository(cache / "users")                                # reachable via the repo
    assert repo.list("daniel/agents/builder") == ["session-1"]
    # Still NAME-keyed at this point: re-keying to permanent ids is the separate pass that runs next at
    # boot. Chain it, as `_init_state` does, and the same worlds come back addressed by id.
    from conjure.world import migrate_worlds_to_ids
    assert migrate_worlds_to_ids(cache / "users") == 2
    wd = repo.worlds("daniel/agents/builder", "session-1")
    assert sorted(wd.list()) == ["castle-quest-throne", "home"]     # hierarchy flattened into the name
    assert all(e["id"].startswith("wld_") for e in wd.entries())
    assert wd.get_active() == wd.resolve("home")                    # pointer re-pointed at the id
    assert json.loads((base / "session.json").read_text())["active_world"] == wd.resolve("home")
    assert migrate_worlds_to_ids(cache / "users") == 0              # idempotent — safe on every boot


def test_migrate_env_room_to_space_presentation(tmp_path):
    """`environment.room` → `environment.spacePresentation`, carrying the per-world style overrides.

    The rename is lossy if it misses a world: `surfaceStyles` is the only place a world's own colours
    live (the space holds the base material), so a dropped key silently reverts a styled room to grey.
    """
    from conjure.world import migrate_env_room_to_space_presentation
    wd = tmp_path / "daniel/agents/builder/sessions/session-1/worlds"
    wd.mkdir(parents=True)
    styled = {"id": "wld_a", "name": "green", "environment": {
        "space": "daniel/space-1", "sky": {"color": "#001"},
        "room": {"active": True, "edgesVisible": False,
                 "boundary": {"height": 2.6},          # live-only strays that got persisted anyway
                 "authorityClientId": "hs_dead",
                 "surfaceStyles": {"real_wall_3": {"color": "darkgreen"}}}}}
    (wd / "wld_a.json").write_text(json.dumps(styled))
    (wd / "wld_b.json").write_text(json.dumps({"id": "wld_b", "environment": {"sky": {}}}))   # no key

    assert migrate_env_room_to_space_presentation(tmp_path) == 1        # only the one carrying it
    env = json.loads((wd / "wld_a.json").read_text())["environment"]
    assert "room" not in env
    assert env["spacePresentation"]["surfaceStyles"] == {"real_wall_3": {"color": "darkgreen"}}
    assert env["spacePresentation"]["edgesVisible"] is False
    assert env["space"] == "daniel/space-1" and env["sky"] == {"color": "#001"}   # siblings untouched
    assert list(env) == ["space", "sky", "spacePresentation"]           # keeps its slot: readable diffs
    # Neither was ever presentation, and neither is persisted by design — a stale copy would contradict
    # the new homes (environment.boundary / environment.captureAuthority), so it's dropped, not moved.
    assert "boundary" not in env["spacePresentation"]
    assert "authorityClientId" not in env["spacePresentation"]

    assert migrate_env_room_to_space_presentation(tmp_path) == 0        # idempotent — safe every boot


def test_migrate_drops_stale_members_from_an_already_renamed_world(tmp_path):
    """The two dropped members can outlive the rename — a world saved between the two changes carries
    `spacePresentation.authorityClientId` with no `room` key to trigger the pass."""
    from conjure.world import migrate_env_room_to_space_presentation
    wd = tmp_path / "daniel/agents/builder/sessions/session-1/worlds"
    wd.mkdir(parents=True)
    (wd / "wld_c.json").write_text(json.dumps({"id": "wld_c", "environment": {
        "spacePresentation": {"active": True, "authorityClientId": "hs_dead",
                              "boundary": {"height": 2.6}}}}))

    assert migrate_env_room_to_space_presentation(tmp_path) == 1
    pres = json.loads((wd / "wld_c.json").read_text())["environment"]["spacePresentation"]
    assert pres == {"active": True}
    assert migrate_env_room_to_space_presentation(tmp_path) == 0


def test_migrate_active_world_falls_back_when_no_pointer(tmp_path):
    from conjure.world import migrate_cache_to_users
    w = tmp_path / "worlds" / "daniel" / "agents" / "outdoor"
    w.mkdir(parents=True)
    (w / "meadow.json").write_text(json.dumps(_doc("Meadow")))            # no _active.txt
    assert migrate_cache_to_users(tmp_path) is True
    meta = json.loads((tmp_path / "users" / "daniel" / "agents" / "outdoor"
                       / "sessions" / "session-1" / "session.json").read_text())
    assert meta["active_world"] == "meadow"                                # fell back to the only world


def test_migrate_is_idempotent_and_noop_on_fresh(tmp_path):
    from conjure.world import migrate_cache_to_users
    assert migrate_cache_to_users(tmp_path) is False                       # nothing to migrate
    w = tmp_path / "worlds" / "daniel" / "agents" / "builder"; w.mkdir(parents=True)
    (w / "home.json").write_text(json.dumps(_doc()))
    assert migrate_cache_to_users(tmp_path) is True
    assert migrate_cache_to_users(tmp_path) is False                       # users/ exists now → no-op


# ── user-home migration (.cache → resolved home; docs/user-home-plan.md §6) ──────────────────────
def _fake_cache(cache):
    """A realistic in-project .cache: precious data, the disposable tunnel_url, and user backups/."""
    (cache / "users" / "daniel" / "agents" / "builder").mkdir(parents=True)
    (cache / "users" / "daniel" / "agents" / "builder" / "marker.txt").write_text("world")
    (cache / "assets").mkdir()
    (cache / "assets" / "abc123.png").write_bytes(b"\x89PNG")
    (cache / "_session.txt").write_text("daniel/agents/builder\tsession-1")
    (cache / "library.db").write_text("catalog")
    (cache / "library.db-wal").write_text("wal")
    (cache / "tunnel_url").write_text("https://x.trycloudflare.com")
    (cache / "backups").mkdir()
    (cache / "backups" / "keep.txt").write_text("mine")


def test_migrate_project_cache_moves_data_and_tunnel_keeps_backups(tmp_path):
    from conjure.world import migrate_project_cache_to_home
    cache = tmp_path / ".cache"; cache.mkdir()
    data = tmp_path / "home" / "data"; cacheroot = tmp_path / "home" / "cache"
    _fake_cache(cache)

    assert migrate_project_cache_to_home(cache, data, cacheroot) is True
    # precious data relocated into the DATA root
    assert (data / "users" / "daniel" / "agents" / "builder" / "marker.txt").read_text() == "world"
    assert (data / "assets" / "abc123.png").read_bytes() == b"\x89PNG"
    assert (data / "_session.txt").read_text().endswith("session-1")
    assert (data / "library.db").read_text() == "catalog"
    assert (data / "library.db-wal").read_text() == "wal"
    # the moved items are GONE from .cache
    assert not (cache / "users").exists() and not (cache / "assets").exists()
    # the disposable tunnel_url moves into the CACHE root (not the DATA tree) and leaves .cache
    assert (cacheroot / "tunnel_url").read_text() == "https://x.trycloudflare.com"
    assert not (cache / "tunnel_url").exists()
    # backups/ is UNTOUCHED (user-owned)
    assert (cache / "backups" / "keep.txt").read_text() == "mine"
    # breadcrumb written
    assert (cache / "MOVED.txt").exists()


def test_migrate_project_cache_is_idempotent(tmp_path):
    from conjure.world import migrate_project_cache_to_home
    cache = tmp_path / ".cache"; cache.mkdir()
    data = tmp_path / "data"; cacheroot = tmp_path / "cache"
    _fake_cache(cache)
    assert migrate_project_cache_to_home(cache, data, cacheroot) is True
    assert migrate_project_cache_to_home(cache, data, cacheroot) is False   # breadcrumb → no-op
    # a fresh .cache created afterwards is still skipped while the breadcrumb stands
    (cache / "users").mkdir(exist_ok=True)
    assert migrate_project_cache_to_home(cache, data, cacheroot) is False


def test_migrate_project_cache_merges_into_precreated_dest(tmp_path):
    # ASSET_CACHE.mkdir may pre-create <data>/assets before migration runs — the move must MERGE.
    from conjure.world import migrate_project_cache_to_home
    cache = tmp_path / ".cache"; cache.mkdir()
    data = tmp_path / "data"; cacheroot = tmp_path / "cache"
    _fake_cache(cache)
    (data / "assets").mkdir(parents=True)                                   # pre-existing empty dest
    assert migrate_project_cache_to_home(cache, data, cacheroot) is True
    assert (data / "assets" / "abc123.png").read_bytes() == b"\x89PNG"


def test_migrate_project_cache_noop_when_absent(tmp_path):
    from conjure.world import migrate_project_cache_to_home
    assert migrate_project_cache_to_home(tmp_path / "nope", tmp_path / "d", tmp_path / "c") is False


def test_migrate_project_cache_refuses_self(tmp_path):
    # If .cache IS the data root (misconfig / test), don't eat itself.
    from conjure.world import migrate_project_cache_to_home
    cache = tmp_path / ".cache"; cache.mkdir()
    _fake_cache(cache)
    assert migrate_project_cache_to_home(cache, cache, tmp_path / "c") is False
    assert (cache / "users").exists()                                       # untouched


def test_clear_transcript_removes_saved_dialog(tmp_path):
    from conjure.world import SessionRepository
    repo = SessionRepository(tmp_path)
    scope, sid = "daniel/agents/builder", "s1"
    repo.append_transcript(scope, sid, {"role": "user", "by": "daniel", "text": "hi"})
    repo.append_transcript(scope, sid, {"role": "assistant", "by": "", "text": "hello"})
    assert len(repo.read_transcript(scope, sid)) == 2
    repo.clear_transcript(scope, sid)
    assert repo.read_transcript(scope, sid) == []      # wiped
    repo.clear_transcript(scope, sid)                  # idempotent — missing file is fine


# --------------------------------------------------------------------------- display-name hygiene

def test_clean_name_drops_quotes_so_a_stored_name_can_always_be_typed_back():
    from conjure.world import clean_name
    # The bug: a parser that took the raw remainder stored the quotes, and no natural form of the name
    # matched it again. Dropping every quote — rather than stripping a surrounding pair — is what makes
    # the two-token case work: `"a" "b"` opens and closes with a quote without being quoted, so an
    # ends-only rule would mangle it to `a" "b`.
    assert clean_name('"Session 1" "alien"') == "Session 1 alien"
    assert clean_name('"alien"') == "alien"
    assert clean_name("“smart quotes”") == "smart quotes"
    # …but an apostrophe STAYS: shlex parses `rename "Bob's room" x` fine, because a name with a space
    # has to be double-quoted anyway. Stripping it bought nothing.
    assert clean_name("Bob's room") == "Bob's room"
    # …and the result is exactly what the shell's own tokeniser produces for the same input
    import shlex
    assert clean_name('"Session 1" "alien"') == " ".join(shlex.split('"Session 1" "alien"'))
    # whitespace is collapsed and trimmed
    assert clean_name("  a   b  ") == "a b"
    # nothing usable left → refuse rather than store something unaddressable
    for bad in ('""', "   ", "", None):
        try:
            clean_name(bad)
            assert False, f"expected {bad!r} to be refused"
        except ValueError:
            pass


def test_clean_name_refuses_only_what_a_path_genuinely_cannot_carry():
    """A denylist, not an allowlist — only the path separators and control characters.

    An earlier pass restricted names to ASCII letters/digits/`. _ -`, reasoning that `_admin_resolve`
    would refuse anything else. It would have, but that was OUR allowlist, chosen conservatively, not a
    real constraint: shlex tokenises `"Café Noir"` and `"Bob's room"` correctly, the charset is
    defence-in-depth behind per-segment existence checks, and since identity became an id a name never
    reaches the filesystem. Refusing them cost real usability — these names arrive by voice and from an
    LLM — and bought nothing.
    """
    from conjure.world import clean_name
    for bad, offender in [("a/b", "/"), ("a\\b", "\\"), ("bell\x07", "\x07")]:
        try:
            clean_name(bad)
            assert False, f"expected {bad!r} to be refused"
        except ValueError as exc:
            # the message reprs the offender, so a control character reads as '\x07' rather than vanishing
            assert repr(offender) in str(exc), f"{exc} should name the offending {offender!r}"
    # …and everything else passes through untouched, punctuation and accents included
    for ok in ("meadow", "Kitchen Table", "my-world.v2", "test_7", "Session 1",
               "Café Noir", "Bob's Diner", "Session (old)", "50% off", "sun & moon"):
        assert clean_name(ok) == ok


def test_every_name_clean_name_accepts_is_a_legal_path_segment():
    """The invariant the two definitions exist to keep: write and address can't drift apart.

    A name is how you address the thing in the shell, so a name that `clean_name` blesses but
    `_admin_resolve` rejects is unreachable by path — which is exactly how a session ended up titled
    something no one could type. Both now read `world.NAME_CHARS`; this asserts they agree.
    """
    from conjure.server import _ADMIN_PART
    from conjure.world import clean_name
    candidates = ["meadow", "Kitchen Table", '"alien"', "  a   b  ", "my-world.v2", "Bob's room",
                  "test_7", "Session 1", "a.b-c_d e", "“smart”", "Café Noir", "Session (old)",
                  "50% off", "sun & moon"]
    accepted = 0
    for c in candidates:
        try:
            cleaned = clean_name(c)
        except ValueError:
            continue                                    # refused on write → never stored → not our problem
        accepted += 1
        assert _ADMIN_PART.fullmatch(cleaned), f"{c!r} → {cleaned!r} is storable but not addressable"
    assert accepted >= 8, "the sample should mostly be accepted, or it proves nothing"


def test_world_and_space_names_are_cleaned_on_write(tmp_path):
    from conjure.world import SpaceStore, WorldDir
    wd = WorldDir(tmp_path / "worlds")
    wid = wd.create('"alien"', WorldStore({"id": "w", "name": "x", "rev": 0,
                                           "environment": {}, "entities": []}))
    assert wd.name_of(wid) == "alien"                    # stored clean, not '"alien"'
    wd.rename(wid, '  "the   moon"  ')
    assert wd.name_of(wid) == "the moon"

    sp = SpaceStore(tmp_path / "spaces")
    sp.save("alice", "space-1", {"owner": "alice", "name": "", "surfaces": [], "boundary": {}})
    sp.rename("alice", "space-1", '"living room"')
    assert sp.name_of("alice", "space-1") == "living room"


def test_an_accented_name_is_found_by_its_unaccented_spelling():
    """Allowing accents without folding them would be half a change.

    The lookup keys strip anything that isn't a-z0-9, so an accented letter would VANISH rather than
    fold: 'Café Noir' keyed as 'caf noir', and typing 'Cafe Noir' missed it. Since these names arrive by
    dictation — which is inconsistent about accents — the two spellings have to converge.
    """
    from conjure.server import _loose
    from conjure.world import fold_accents, slug
    assert fold_accents("Café") == "Cafe"
    for typed in ("Cafe Noir", "cafe-noir", "CAFE_NOIR"):
        assert _loose("Café Noir") == _loose(typed), typed
        assert slug("Café Noir") == slug(typed), typed
    # the accent survives in what's STORED — only the match key folds
    from conjure.world import clean_name
    assert clean_name("Café Noir") == "Café Noir"
