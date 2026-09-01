"""`POST /world_frame` — the persisted half of `grab`'s skybox and void-world modes
(docs/specs/dynamics.md §8b).

What matters here is that these are DELTAS on a derived frame, not entity transforms. The client rewrites
both the skybox pose and a void world's `#world-root` parking from the derived frame on every capture, so
what gets stored has to be something the client composes on top — which is why this lands in
`environment` via an `env` op rather than going through /manipulate.

The validation tests are the load-bearing ones. A non-finite value reaching a quaternion or a position
blanks that branch of the scene graph and STAYS blanked, and unlike a bad gesture a bad persisted value
comes back on every reload.
"""


def _env(srv):
    return srv.store.doc.get("environment", {})


def test_a_sky_commit_lands_in_environment_not_on_an_entity(srv, client):
    before = len(srv.store.doc["entities"])
    r = client.post("/world_frame", json={"sky": {"yaw": 30.0, "scale": 2.5}})
    assert r.json()["ok"] is True
    assert _env(srv)["frame"]["skyYaw"] == 30.0
    assert _env(srv)["frame"]["skyScale"] == 2.5
    assert len(srv.store.doc["entities"]) == before, "a frame delta must not create or touch an entity"


def test_a_sky_commit_preserves_the_panorama_already_set(srv, client):
    client.post("/patch", json={"ops": [{"op": "env", "set": {"sky": {"src": "/img/pines.png"}}}]})
    client.post("/world_frame", json={"sky": {"yaw": 15.0}})
    assert _env(srv)["sky"]["src"] == "/img/pines.png"
    assert _env(srv)["frame"]["skyYaw"] == 15.0


def test_a_sky_delta_never_writes_under_environment_sky(srv, client):
    """Regression for the field failure of 2026-09-01, and the reason `skyYaw` lives under `frame`.

    The delta first lived at `environment.sky.yaw`. Dotted paths kept the stored `src` intact, but the
    BROADCAST patch still carried `{"sky": {"yaw": …, "scale": …}}` — and the client's `applyEnv` reads an
    `env.sky` object as a complete description of the sky, so no `src` meant no panorama and it tore the
    grounded dome down on every release. The sky vanished on release and came back on reload.

    `applyEnv` was right; sharing the key was the mistake. So the invariant is structural: a sky delta
    touches no path under `sky`, and therefore no reader of `sky` ever sees a fragment of one."""
    client.post("/patch", json={"ops": [{"op": "env", "set": {
        "sky": {"src": "/img/meadow.png", "grounded": True, "height": 1.6, "radius": 30.0}}}]})
    r = client.post("/world_frame", json={"sky": {"yaw": 20.0, "scale": 1.5}}).json()
    assert r["ok"] is True
    assert all(not k.startswith("sky") for k in r["set"]), r["set"]
    # The dome's own description is untouched, field for field.
    assert _env(srv)["sky"] == {"src": "/img/meadow.png", "grounded": True, "height": 1.6, "radius": 30.0}


def test_a_reset_never_writes_under_environment_sky_either(srv, client):
    client.post("/patch", json={"ops": [{"op": "env", "set": {"sky": {"src": "/img/pines.png"}}}]})
    r = client.post("/world_frame", json={"reset": "all"}).json()
    assert all(not k.startswith("sky") for k in r["set"]), r["set"]
    assert _env(srv)["sky"] == {"src": "/img/pines.png"}


def test_a_void_frame_commit_stores_yaw_and_a_horizontal_offset(srv, client):
    r = client.post("/world_frame", json={"frame": {"yaw": -12.5, "offset": [0.4, -1.25]}})
    assert r.json()["ok"] is True
    assert _env(srv)["frame"] == {"yaw": -12.5, "offset": [0.4, -1.25]}


def test_sky_and_frame_commit_together(srv, client):
    client.post("/world_frame", json={"sky": {"yaw": 5.0}, "frame": {"yaw": 6.0}})
    assert _env(srv)["frame"]["skyYaw"] == 5.0
    assert _env(srv)["frame"]["yaw"] == 6.0


def test_a_partial_commit_leaves_the_other_field_alone(srv, client):
    client.post("/world_frame", json={"sky": {"yaw": 20.0, "scale": 3.0}})
    client.post("/world_frame", json={"sky": {"scale": 4.0}})
    assert _env(srv)["frame"]["skyYaw"] == 20.0, "a scale-only commit must not reset the yaw"
    assert _env(srv)["frame"]["skyScale"] == 4.0


# ---- reset: the ONLY recovery path, by design -----------------------------------------------------
# Two decisions remove the alternatives. There is deliberately no minimum engage radius, so one twitch
# near the centre can apply a large yaw — and a symmetric panorama gives no way to tell yaw 0 from yaw 180
# by eye. And the void offset is deliberately unbounded, so the world can be dragged far enough that the
# floor point you would need to grab to drag it back is out of reach.

def test_reset_sky_returns_the_derived_frame_defaults(srv, client):
    client.post("/world_frame", json={"sky": {"yaw": 90.0, "scale": 8.0}})
    r = client.post("/world_frame", json={"reset": "sky"})
    assert r.json()["ok"] is True
    assert _env(srv)["frame"]["skyYaw"] == 0.0
    assert _env(srv)["frame"]["skyScale"] == 1.0


def test_reset_sky_keeps_the_panorama(srv, client):
    # "Put the sky back" means undo MY adjustment, not throw away the image.
    client.post("/patch", json={"ops": [{"op": "env", "set": {"sky": {"src": "/img/meadow.png"}}}]})
    client.post("/world_frame", json={"sky": {"yaw": 90.0}})
    client.post("/world_frame", json={"reset": "sky"})
    assert _env(srv)["sky"]["src"] == "/img/meadow.png"
    assert _env(srv)["frame"]["skyYaw"] == 0.0


def test_reset_frame_leaves_the_sky_adjustment_alone(srv, client):
    client.post("/world_frame", json={"sky": {"yaw": 45.0}, "frame": {"yaw": 30.0, "offset": [9.0, 9.0]}})
    client.post("/world_frame", json={"reset": "frame"})
    assert _env(srv)["frame"]["yaw"] == 0.0
    assert _env(srv)["frame"]["offset"] == [0.0, 0.0]
    assert _env(srv)["frame"]["skyYaw"] == 45.0, "resetting the world must not straighten the sky too"


def test_reset_all_clears_both(srv, client):
    client.post("/world_frame", json={"sky": {"yaw": 45.0, "scale": 3.0}, "frame": {"yaw": 30.0}})
    client.post("/world_frame", json={"reset": "all"})
    assert _env(srv)["frame"]["skyYaw"] == 0.0 and _env(srv)["frame"]["skyScale"] == 1.0
    assert _env(srv)["frame"]["yaw"] == 0.0 and _env(srv)["frame"]["offset"] == [0.0, 0.0]


def test_reset_recovers_a_world_dragged_out_of_reach(srv, client):
    # The unbounded-offset case the reset exists for: 40 m away, every grabbable floor point included.
    client.post("/world_frame", json={"frame": {"yaw": 170.0, "offset": [40.0, -37.5]}})
    client.post("/world_frame", json={"reset": "frame"})
    assert _env(srv)["frame"] == {"yaw": 0.0, "offset": [0.0, 0.0]}


def test_an_unknown_reset_target_is_refused_rather_than_ignored(srv, client):
    r = client.post("/world_frame", json={"reset": "everything"})
    assert r.json()["ok"] is False
    assert "everything" in r.json()["error"]


# ---- validation ----------------------------------------------------------------------------------

def test_a_zero_or_negative_scale_is_refused(srv, client):
    # Zero collapses the sky sphere to a point; negative mirrors it inside out.
    for bad in (0, -1.5):
        r = client.post("/world_frame", json={"sky": {"scale": bad}})
        assert r.json()["ok"] is False, bad
        assert "scale" in r.json()["error"]
    assert _env(srv).get("frame", {}).get("skyScale") is None


def test_non_finite_values_are_refused_on_every_field(srv, client):
    """Sent as RAW bodies, because `json.dumps` refuses inf/nan — but `json.loads` accepts the bare
    `Infinity`/`NaN` literals, which is exactly how one could reach us. This is the test that matters most
    here: a non-finite value in a quaternion or a position blanks that branch of the scene graph and stays
    blanked, and a persisted one comes back on every reload."""
    raw = ('{"sky": {"yaw": Infinity}}',
           '{"sky": {"scale": NaN}}',
           '{"frame": {"yaw": -Infinity}}',
           '{"frame": {"offset": [1.0, Infinity]}}')
    for body in raw:
        r = client.post("/world_frame", content=body,
                        headers={"Content-Type": "application/json"})
        assert r.json()["ok"] is False, body
        assert "finite" in r.json()["error"], body
    assert _env(srv).get("frame", {}).get("skyYaw") is None
    assert _env(srv).get("frame", {}).get("yaw") is None


def test_a_non_numeric_value_is_refused(srv, client):
    r = client.post("/world_frame", json={"sky": {"yaw": "sideways"}})
    assert r.json()["ok"] is False
    assert "yaw" in r.json()["error"]


def test_a_malformed_offset_is_refused(srv, client):
    for bad in ([1.0], [1.0, 2.0, 3.0], "0,0", {"x": 1}):
        r = client.post("/world_frame", json={"frame": {"offset": bad}})
        assert r.json()["ok"] is False, bad
        assert "offset" in r.json()["error"]


def test_an_empty_request_changes_nothing(srv, client):
    r = client.post("/world_frame", json={})
    assert r.json()["ok"] is False
    assert "nothing to change" in r.json()["error"]


def test_ergonomic_bounds_are_not_enforced_here(srv, client):
    """Deliberate: the client clamps effective metres, because that is where the gesture that produces the
    value and the readout that reports it both live. Duplicating the numbers server-side would give two
    homes that can disagree. The server's job is structural safety only."""
    r = client.post("/world_frame", json={"sky": {"scale": 5000.0}})
    assert r.json()["ok"] is True
    assert _env(srv)["frame"]["skyScale"] == 5000.0


def test_world_frame_is_owner_gated(srv, client):
    # Same rule as every world write, and the same rule /manipulate follows.
    assert "/world_frame" in srv._OWNER_ONLY_PATHS
    r = client.post("/world_frame", json={"sky": {"yaw": 10.0}},
                    headers={"X-Conjure-User": "someone-else"})
    assert r.status_code == 403
    assert _env(srv).get("frame", {}).get("skyYaw") is None
