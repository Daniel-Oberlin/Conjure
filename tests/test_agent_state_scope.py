"""Agent-wide state (docs/specs/agents.md §7.4): docs declared `scope: agent` live once per user per agent,
beside `sessions/`, so a new session doesn't start the agent over; and `timeline: true` docs keep dated
entries that close rather than vanish."""

import json
import types

import pytest

from conjure.agent_server import _maybe_seed, _state_view, _sync_transcript
from conjure.agents import load_agent
from conjure.director import Director, _today
from conjure.world import ScopedStateStore, SessionRepository, StateStore

from test_agent_server import FakeShell, _app
from test_director import FakeSession

SCOPE = "daniel/agents/companion"


def _agent(**state):
    return types.SimpleNamespace(name="companion", state=state)


def _meta(sessions, sid, **extra):
    sessions.save_meta(SCOPE, sid, {"id": sid, "owner": "daniel", "agent": "companion", "title": sid,
                                    "public": False, "active_world": "home", "llm": "",
                                    "greeted": True, "seeded": True, **extra})


# --------------------------------------------------------------------------- declaration (load time)

def _write(tmp_path, state):
    d = tmp_path / "companion"
    d.mkdir()
    (d / "agent.json").write_text(json.dumps({"prompt": "hi", "state": state}))
    return tmp_path


def test_scope_defaults_to_session_and_agent_docs_are_listed(tmp_path):
    a = load_agent("companion", agents_dir=_write(tmp_path, {"scene": {}, "profile": {"scope": "agent"}}))
    assert a.agent_docs() == {"profile"}


@pytest.mark.parametrize("spec, match", [
    ({"scope": "user"}, "session|agent"),                              # a typo must not fall back silently
    ({"migrate_from": "kinks"}, "migrate_from"),                       # only meaningful on an agent doc
    ({"scope": "agent", "migrate_from": ""}, "migrate_from"),
    ({"timeline": "yes"}, "timeline"),
])
def test_bad_state_declarations_are_fatal(tmp_path, spec, match):
    with pytest.raises(ValueError, match=match):
        load_agent("companion", agents_dir=_write(tmp_path, {"profile": spec}))


# --------------------------------------------------------------------------- routing

def test_scoped_store_routes_by_declaration(tmp_path):
    sess, agent = StateStore(tmp_path / "s"), StateStore(tmp_path / "a")
    v = ScopedStateStore(sess, agent, {"profile"})
    v.set("profile", "likes.rain", {"note": "rain"})
    v.set("scene", "where", "balcony")
    assert agent.read("profile") == {"likes": {"rain": {"note": "rain"}}}
    assert sess.read("scene") == {"where": "balcony"}
    assert v.list() == ["profile", "scene"]
    sess.write("profile", {"stray": True})                  # a same-named session doc is never consulted
    assert v.read("profile") == {"likes": {"rain": {"note": "rain"}}} and v.list() == ["profile", "scene"]
    assert v.delete("profile", "likes.rain") and agent.read("profile") == {"likes": {}}


def test_agent_doc_survives_a_new_session(tmp_path):
    sessions = SessionRepository(tmp_path)
    agent = _agent(profile={"scope": "agent", "seed_data": {"likes": {}}}, scene={})
    _meta(sessions, "session-1")
    _state_view(sessions_app(sessions), SCOPE, "session-1", None, agent).set("profile", "likes.rain", {"note": "x"})
    _meta(sessions, "session-2")
    v2 = _state_view(sessions_app(sessions), SCOPE, "session-2", None, agent)
    assert v2.get("profile", "likes.rain") == {"note": "x"}            # the point: memory outlives the session
    assert (tmp_path / "daniel/agents/companion/state/profile.json").exists()


def test_seed_once_and_never_clobber(tmp_path):
    sessions = SessionRepository(tmp_path)
    agent = _agent(profile={"scope": "agent", "seed_data": {"likes": {}, "notes": ""}})
    app = sessions_app(sessions)
    v = _state_view(app, SCOPE, "session-1", None, agent)
    assert v.read("profile") == {"likes": {}, "notes": ""}
    v.set("profile", "notes", "slow")
    assert _state_view(app, SCOPE, "session-1", None, agent).get("profile", "notes") == "slow"


def test_migrate_from_takes_the_latest_session_copy(tmp_path):
    import os, time
    sessions = SessionRepository(tmp_path)
    for sid, likes in (("session-1", {"old": {"note": "old"}}), ("session-2", {"new": {"note": "new"}})):
        _meta(sessions, sid)
        sessions.state(SCOPE, sid).write("kinks", {"likes": likes, "limits": {}})
    old = sessions.state(SCOPE, "session-1")._path("kinks")
    os.utime(old, (time.time() - 3600, time.time() - 3600))          # session-1's copy is older
    agent = _agent(profile={"scope": "agent", "migrate_from": "kinks", "seed_data": {"likes": {}}})
    v = _state_view(sessions_app(sessions), SCOPE, "session-1", None, agent)
    assert v.read("profile")["likes"] == {"new": {"note": "new"}}
    assert sessions.state(SCOPE, "session-2").read("kinks")["likes"]   # the source is left in place


def test_migrate_from_falls_back_to_seed(tmp_path):
    sessions = SessionRepository(tmp_path)
    agent = _agent(profile={"scope": "agent", "migrate_from": "kinks", "seed_data": {"likes": {}}})
    assert _state_view(sessions_app(sessions), SCOPE, "s", None, agent).read("profile") == {"likes": {}}


def test_new_session_seeding_skips_agent_docs(tmp_path):
    sessions = SessionRepository(tmp_path)
    _meta(sessions, "session-1", seeded=False)
    shell = FakeShell()
    shell.director.agent = _agent(profile={"scope": "agent", "seed_data": {"likes": {}}},
                                  scene={"seed_data": {"where": "home"}})
    app = _app(shell, [], sessions=sessions)
    app.state.live = {"scope": SCOPE, "session": "session-1", "agent": "companion"}
    _maybe_seed(app)
    assert sessions.state(SCOPE, "session-1").list() == ["scene"]     # no stray per-session profile


def test_sync_binds_the_scoped_view(tmp_path):
    sessions = SessionRepository(tmp_path)
    _meta(sessions, "session-1")
    shell = FakeShell()
    bound = []
    shell.director.agent = _agent(profile={"scope": "agent", "seed_data": {"likes": {}}})
    shell.director.bind_state = bound.append
    app = _app(shell, [], sessions=sessions)
    app.state.live = {"scope": SCOPE, "session": "session-1", "agent": "companion"}
    _sync_transcript(app)
    assert isinstance(bound[0], ScopedStateStore) and bound[0].read("profile") == {"likes": {}}


def test_a_visitor_gets_its_own_agents_wide_state_not_the_hosts(tmp_path):
    sessions = SessionRepository(tmp_path)
    sessions.agent_state(SCOPE).write("profile", {"secret": True})     # the host's memory
    visitor = types.SimpleNamespace(name="builder", state={"profile": {"scope": "agent", "seed_data": {}}})
    v = _state_view(sessions_app(sessions), SCOPE, "session-1", "builder", visitor)
    assert v.read("profile") == {}
    assert v.agent.dir == tmp_path / "daniel/agents/builder/state"


def sessions_app(sessions):
    return types.SimpleNamespace(state=types.SimpleNamespace(sessions=sessions))


# --------------------------------------------------------------------------- timeline docs

def _director(tmp_path, **spec):
    d = Director(settings=None, session=FakeSession(), roster={"Claude": object()}, active="Claude",
                 tools=[], allowed_tools=set(), state_defs={"profile": {"timeline": True, **spec}})
    d.bind_state(StateStore(tmp_path))
    return d


async def _call(d, name, **args):
    return await d._execute_tool(name, {"doc": "profile", **args}, None, "t")


async def test_new_entries_get_since_and_rewrites_keep_it(tmp_path):
    d = _director(tmp_path)
    assert await _call(d, "state_set", path="likes.rain", value={"note": "rain"}) == "ok"
    assert d._state.get("profile", "likes.rain") == {"note": "rain", "since": _today()}
    d._state.set("profile", "likes.rain.since", "2026-10-01")
    await _call(d, "state_set", path="likes.rain", value={"note": "warm rain"})
    assert d._state.get("profile", "likes.rain") == {"note": "warm rain", "since": "2026-10-01"}
    await _call(d, "state_set", path="likes.sky", value={"note": "x", "since": "2026-01-01"})
    assert d._state.get("profile", "likes.sky.since") == "2026-01-01"  # the model's own date wins
    await _call(d, "state_set", path="notes", value="slow")
    assert d._state.get("profile", "notes") == "slow"                  # scalars are not stamped


async def test_delete_closes_an_entry_and_forget_erases_it(tmp_path):
    d = _director(tmp_path)
    await _call(d, "state_set", path="likes.rain", value={"note": "rain"})
    assert (await _call(d, "state_delete", path="likes.rain")).startswith("closed")
    assert d._state.get("profile", "likes.rain")["until"] == _today()
    assert (await _call(d, "state_delete", path="likes.rain")).startswith("already closed")
    assert await _call(d, "state_delete", path="likes.rain", forget=True) == "ok"
    assert d._state.get("profile", "likes.rain") is None


async def test_timeline_refuses_whole_doc_and_scalar_deletes_without_forget(tmp_path):
    d = _director(tmp_path)
    await _call(d, "state_set", path="notes", value="slow")
    assert (await _call(d, "state_delete")).startswith("error")
    assert (await _call(d, "state_delete", path="notes")).startswith("error")
    assert await _call(d, "state_delete", path="nope") == "not found"
    assert await _call(d, "state_delete", path="notes", forget=True) == "ok"
    assert d._state.read("profile") == {}


async def test_closing_respects_the_schema(tmp_path):
    schema = {"type": "object", "properties": {"likes": {"type": "object", "additionalProperties": {
        "type": "object", "properties": {"note": {"type": "string"}, "since": {"type": "string"}},
        "additionalProperties": False}}}}
    d = _director(tmp_path, schema_data=schema)
    await _call(d, "state_set", path="likes.rain", value={"note": "rain"})
    assert (await _call(d, "state_delete", path="likes.rain")).startswith("error")   # `until` not allowed
    assert "until" not in d._state.get("profile", "likes.rain")


async def test_a_doc_without_timeline_deletes_as_before(tmp_path):
    d = Director(settings=None, session=FakeSession(), roster={"Claude": object()}, active="Claude",
                 tools=[], allowed_tools=set(), state_defs={"profile": {}})
    d.bind_state(StateStore(tmp_path))
    await _call(d, "state_set", path="likes.rain", value={"note": "rain"})
    assert "since" not in d._state.get("profile", "likes.rain")
    assert await _call(d, "state_delete", path="likes.rain") == "ok"
