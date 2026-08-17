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
    assert repo.get_active("private/builder") == "home"
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
    repo.set_session("daniel/agents/builder", "Castle Quest/Dining Hall")
    assert repo.get_session() == ("daniel/agents/builder", "castle-quest/dining-hall")  # world normalized
    assert repo.list_users() == []                             # _session.txt is a root file, not a user


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


# -- SessionRepository (docs/sessions-plan.md §3) ------------------------------------------------

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

def test_worlddir_roundtrip_active_nested_and_prune(tmp_path):
    from conjure.world import WorldDir
    wd = WorldDir(tmp_path / "worlds")
    assert wd.list() == [] and wd.get_active() is None
    wd.save("home", WorldStore(_doc("Home")))
    wd.save("Castle Quest/Throne Room", WorldStore(_doc()))
    assert wd.list() == ["castle-quest/throne-room", "home"]          # slugged + nested, recursive
    assert wd.exists("HOME") and wd.load("home").doc["name"] == "Home"
    wd.set_active("home")
    assert wd.get_active() == "home"
    assert wd.delete("home") is True and wd.get_active() is None      # pointer cleared with its target
    assert (tmp_path / "worlds" / "castle-quest" / "throne-room.json").exists()
    wd.delete("castle-quest/throne-room")
    assert not (tmp_path / "worlds" / "castle-quest").exists()        # now-empty parent pruned


def test_worldrepo_set_live_drives_the_live_scope_but_not_others(tmp_path):
    # The facade addresses the LIVE scope via the server-declared live session (set_live), and any OTHER
    # scope via its own active-session pointer (docs/sessions-plan.md §3, "5.5").
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
    wd.save("home", WorldStore(_doc("Home")))
    assert wd.list() == ["home"]
    assert (tmp_path / "daniel" / "agents" / "builder" / "sessions" / "session-1"
            / "worlds" / "home.json").exists()
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


# -- StateStore (the generic agent-state store; docs/sessions-plan.md §5) -------------------------

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


# -- migration to the user-first session tree (docs/sessions-plan.md §7) --------------------------

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
    assert repo.worlds("daniel/agents/builder", "session-1").list() == ["castle-quest/throne", "home"]


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
