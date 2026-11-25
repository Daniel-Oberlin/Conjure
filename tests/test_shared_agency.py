"""An agent that serves everyone present, and agents that visit another agent's world
(docs/specs/agents.md §8.4, §8.5, §9.4; decisions.md §35)."""

from conftest import ASSET_RECORD, FakeAssetResolver

OWNER = "daniel/agents/builder"          # the fixture's live world scope
BOX = {"ops": [{"op": "add", "entity": {"id": "b", "components": {}}}]}


def _turn(speaker, actor):
    """The headers an agent turn sends: who asked, and whose rights its writes use."""
    return {"X-Conjure-User": speaker, "X-Conjure-Speaker": speaker, "X-Conjure-Scope": actor}


def _session(srv, **meta):
    srv.sessions.save_meta(srv.active_scope, srv.active_sid,
                           {"id": srv.active_sid, "owner": "daniel", "agent": "builder", **meta})


# ---- serving everyone ------------------------------------------------------------------------------

def test_a_guests_request_is_carried_out_when_the_session_serves_everyone(srv, client):
    _session(srv)                                                  # default: serves everyone
    r = client.post("/patch", json=BOX, headers=_turn("bob", OWNER))
    assert r.status_code == 200


def test_serving_the_owner_refuses_a_guests_request(srv, client):
    _session(srv, serves="owner")
    r = client.post("/patch", json=BOX, headers=_turn("bob", OWNER))
    assert r.status_code == 403 and "only the owner" in r.json()["error"]
    assert client.post("/patch", json=BOX, headers=_turn("daniel", OWNER)).status_code == 200


def test_the_agent_defaults_whom_it_serves(srv, client, monkeypatch):
    _session(srv)                                                  # nothing recorded on the session
    monkeypatch.setattr(srv, "_agent_session_serves", lambda scope: "owner")
    assert client.post("/patch", json=BOX, headers=_turn("bob", OWNER)).status_code == 403


def test_resetting_and_realigning_stay_the_owners_whatever_it_serves(srv, client):
    _session(srv)
    for path in ("/reset", "/space/realign", "/world_frame"):
        r = client.post(path, json={}, headers=_turn("bob", OWNER))
        assert r.status_code == 403, path
    assert client.post("/space/realign", json={}, headers=_turn("daniel", OWNER)).status_code == 200


def test_a_headsets_own_write_keeps_the_owner_rule(srv, client):
    """Serving everyone is the AGENT acting for them; a request with no actor is not an agent turn."""
    _session(srv)
    assert client.post("/patch", json=BOX, headers={"X-Conjure-User": "bob"}).status_code == 403


def test_a_turn_acting_as_the_guest_is_refused(srv, client):
    """The agent server hands the guest's own scope as actor when the session serves only the owner;
    the gate must refuse it even if `serves` were somehow everyone."""
    _session(srv)
    assert client.post("/patch", json=BOX, headers=_turn("bob", "bob/agents/builder")).status_code == 403


def test_only_the_owner_changes_whom_the_session_serves(srv, client):
    _session(srv)
    r = client.post("/session/serves", json={"serves": "owner"}, headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] is False
    r = client.post("/session/serves", json={"serves": "owner"}, headers={"X-Conjure-User": "daniel"}).json()
    assert r["ok"] is True and srv._active_serves() == "owner"
    assert client.get("/agent/serves").json()["serves"] == "owner"
    assert srv._live_state()["serves"] == "owner"


# ---- the full-scope check -------------------------------------------------------------------------

def test_another_agent_of_the_same_user_cannot_edit_without_visiting(srv, client):
    """The user-only gate this replaces let any of your agents edit any of your worlds."""
    r = client.post("/patch", json=BOX, headers=_turn("daniel", "daniel/agents/outdoor"))
    assert r.status_code == 403 and "isn't visiting" in r.json()["error"]


# ---- visiting -------------------------------------------------------------------------------------

def _lets_in(srv, monkeypatch, *names):
    monkeypatch.setattr(srv, "_agent_visiting_agents", lambda scope: list(names))


def test_a_visit_needs_the_host_to_list_the_visitor(srv, client, monkeypatch):
    _lets_in(srv, monkeypatch)
    r = client.post("/agent/visit", json={"agent": "outdoor"}).json()
    assert r["ok"] is False and "visiting_agents" in r["error"]
    assert srv.active_visitor is None


def test_only_the_owner_brings_a_visitor_in(srv, client, monkeypatch):
    _lets_in(srv, monkeypatch, "outdoor")
    r = client.post("/agent/visit", json={"agent": "outdoor"}, headers={"X-Conjure-User": "bob"}).json()
    assert r["ok"] is False and srv.active_visitor is None


def test_a_listed_visitor_works_in_the_world_and_it_stays_put(srv, client, monkeypatch):
    _lets_in(srv, monkeypatch, "outdoor")
    world = srv.active_world
    r = client.post("/agent/visit", json={"agent": "outdoor"}).json()
    assert r["ok"] is True and r["host"] == "builder"
    assert srv.active_visitor == "outdoor" and srv.active_world == world and srv.active_scope == OWNER
    state = srv._live_state()
    assert state["agent"] == "outdoor" and state["host_agent"] == "builder" and state["visitor"] == "outdoor"
    assert client.post("/patch", json=BOX, headers=_turn("daniel", "daniel/agents/outdoor")).status_code == 200


def test_switching_back_to_the_host_ends_the_visit_without_moving(srv, client, monkeypatch):
    _lets_in(srv, monkeypatch, "outdoor")
    world = srv.active_world
    client.post("/agent/visit", json={"agent": "outdoor"})
    r = client.post("/scope/activate", json={"scope": OWNER}).json()
    assert r["ok"] and r.get("visit_ended") is True
    assert srv.active_visitor is None and srv.active_world == world
    assert client.post("/patch", json=BOX, headers=_turn("daniel", "daniel/agents/outdoor")).status_code == 403


def test_any_world_move_ends_a_visit(srv, client, monkeypatch):
    _lets_in(srv, monkeypatch, "outdoor")
    client.post("/agent/visit", json={"agent": "outdoor"})
    client.post("/worlds/new", json={"name": "elsewhere", "scope": OWNER})
    assert srv.active_visitor is None


# ---- assets while visiting ------------------------------------------------------------------------

def _catalog(srv, asset_id, scope, label):
    srv.library.upsert(asset_id, kind="model", scope=scope, source=f"cache://{asset_id}", filename=asset_id,
                       label=label, query=label, public=0)


def test_a_visitor_searches_its_own_library_and_its_hosts(srv, client, monkeypatch):
    _lets_in(srv, monkeypatch, "outdoor")
    _catalog(srv, "aaaa.glb", "daniel/agents/outdoor", "red lamp")
    _catalog(srv, "bbbb.glb", OWNER, "blue lamp")
    client.post("/agent/visit", json={"agent": "outdoor"})
    r = client.post("/library/search", json={"query": "lamp", "scope": "daniel/agents/outdoor"}).json()
    assert {c["id"] for c in r["candidates"]} == {"aaaa.glb", "bbbb.glb"}
    # …and only while visiting
    client.post("/scope/activate", json={"scope": OWNER})
    r = client.post("/library/search", json={"query": "lamp", "scope": "daniel/agents/outdoor"}).json()
    assert {c["id"] for c in r["candidates"]} == {"aaaa.glb"}


def test_an_asset_a_visitor_places_is_imported_into_the_host(srv, client, monkeypatch, tmp_path):
    _lets_in(srv, monkeypatch, "outdoor")
    mid = f"{ASSET_RECORD.hash}.glb"
    _catalog(srv, mid, "daniel/agents/outdoor", "oak tree")
    (tmp_path / mid).write_bytes(b"glTF" + bytes(8))
    client.post("/agent/visit", json={"agent": "outdoor"})
    r = client.post("/place_cached_asset", json={"id": mid},
                    headers=_turn("daniel", "daniel/agents/outdoor")).json()
    assert r["ok"] is True
    assert OWNER in srv.library.owners(mid)                       # the host owns it now…
    assert srv.library.get(mid)["scope"] == "daniel/agents/outdoor"   # …and the visitor still does
    hits = client.post("/library/search", json={"query": "oak tree", "scope": OWNER}).json()["candidates"]
    assert mid in {c["id"] for c in hits}                          # the host agent can find it


def test_outside_a_visit_another_agents_asset_stays_walled_off(srv, client, tmp_path):
    mid = f"{ASSET_RECORD.hash}.glb"
    _catalog(srv, mid, "daniel/agents/outdoor", "oak tree")
    (tmp_path / mid).write_bytes(b"glTF" + bytes(8))
    r = client.post("/place_cached_asset", json={"id": mid}, headers=_turn("daniel", OWNER)).json()
    assert r["ok"] is False and OWNER not in srv.library.owners(mid)


def test_an_asset_made_during_a_guests_turn_belongs_to_the_world(srv, client):
    _session(srv)
    srv.resolver = FakeAssetResolver(record=ASSET_RECORD)
    r = client.post("/place_asset", json={"query": "oak tree"}, headers=_turn("bob", OWNER)).json()
    assert r["ok"] is True
    assert srv.library.get(f"{ASSET_RECORD.hash}.glb")["scope"] == OWNER
