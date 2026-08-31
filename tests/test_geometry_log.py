"""The geometry event log — the always-on, change-gated record behind the two field symptoms in
docs/backlogs/spaces-geometry.md (a surface dropping out and returning uncoloured; one room's floor sitting
a few inches high).

What matters here is not that lines get written, but that the file is still USEFUL days later: it rotates,
it prunes, one flush is one request, and a surface leaving the seed is named rather than being folded into
an aggregate count."""

import dataclasses
import json
from datetime import datetime, timedelta


def _lines(server, day=None):
    day = day or datetime.now().strftime("%Y-%m-%d")
    p = server.GEO_LOG_DIR / f"geometry-{day}.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def _surface(sid, semantic="wall", pos=(0.0, 1.2, 0.0)):
    return {"id": sid, "semantic": semantic, "position": list(pos), "rotation": [0.0, 0.0, 0.0],
            "extent": [3.0, 2.4], "holes": []}


def test_client_batch_lands_as_one_jsonl_line_per_event(srv, client):
    r = client.post("/geometry_log", json={"sid": "s_abc123", "events": [
        {"ev": "space.enter", "ct": 1000, "role": "owner", "ref": 59},
        {"ev": "churn.miss", "ct": 1001, "id": "real_wall_7", "why": "matcher", "gate": "perp",
         "val": 0.19, "tol": 0.15},
    ]})
    assert r.json()["ok"] is True
    rows = _lines(srv)
    assert [x["ev"] for x in rows] == ["space.enter", "churn.miss"]
    assert all(x["sid"] == "s_abc123" for x in rows), "every line carries the session that produced it"
    # The margin against the tolerance survives the round trip — it is the number that says what to set
    # --wall-perp-tol to, and rounding or stringifying it away would defeat the whole probe.
    assert rows[1]["val"] == 0.19 and rows[1]["tol"] == 0.15
    # The server stamps its own clock so headset lines and server lines sort together in one file, and
    # keeps the client's for intra-batch ordering.
    assert rows[0]["t"] < rows[1]["t"] or rows[0]["ct"] < rows[1]["ct"]


def test_a_settled_room_writes_nothing(srv, client):
    """The affordability argument for leaving this on: no change, no line."""
    surfaces = [_surface("real_wall_1"), _surface("real_floor_2", "floor", (0, 0, 0))]
    body = {"client_id": "hs_1", "surfaces": surfaces, "boundary": None, "replace": True}
    client.post("/space/capture", json=body)                  # first post: two adds
    before = len(_lines(srv))
    for _ in range(3):                                        # …then the same room, unchanged
        client.post("/space/capture", json=body)
    assert len(_lines(srv)) == before, "re-posting an unchanged room adds no geometry-log lines"


def test_a_pruned_surface_is_named_and_says_whether_it_had_styling(srv, client):
    """The server half of "drops out and returns uncoloured": a prune deletes the material with the
    geometry, so this line is the moment the colour is lost. `seed_ops=N` alone can't be chased."""
    both = [_surface("real_wall_1"), _surface("real_wall_2", pos=(4.0, 1.2, 0.0))]
    client.post("/space/capture", json={"client_id": "hs_1", "surfaces": both, "replace": True})
    client.post("/style_surface", json={"target": "real_wall_1", "color": "#ff0000"})
    # wall_1 absent from the confirmed set → pruned (the client owns the absence debounce).
    client.post("/space/capture", json={"client_id": "hs_1", "surfaces": [both[1]], "replace": True})

    prunes = [x for x in _lines(srv) if x["ev"] == "seed.prune"]
    assert [x["id"] for x in prunes] == ["real_wall_1"]
    assert prunes[0]["sem"] == "wall"
    assert prunes[0]["styled"] is True, "and records that a director-set colour went with it"
    assert prunes[0]["color"] == "#ff0000", "…naming the colour, so the loss is legible in the log"


def test_pruning_an_UNSTYLED_surface_says_so(srv, client):
    """`styled` has to mean "a director edit is being destroyed". Every real surface is created with a
    per-semantic default material, so a plain truthiness check would report True for all of them — the
    field would be constant and carry no information at exactly the moment it is being read."""
    both = [_surface("real_wall_1"), _surface("real_wall_2", pos=(4.0, 1.2, 0.0))]
    client.post("/space/capture", json={"client_id": "hs_1", "surfaces": both, "replace": True})
    client.post("/space/capture", json={"client_id": "hs_1", "surfaces": [both[1]], "replace": True})
    prune = next(x for x in _lines(srv) if x["ev"] == "seed.prune")
    assert prune["styled"] is False, "a never-styled surface reports False, not the default material"


def test_adds_and_updates_are_named_too(srv, client):
    client.post("/space/capture", json={"client_id": "hs_1", "surfaces": [_surface("real_wall_1")],
                                        "replace": True})
    assert [x["id"] for x in _lines(srv) if x["ev"] == "seed.add"] == ["real_wall_1"]
    # A LARGE move (past the structural threshold) updates the seed and says why; sub-threshold drift
    # doesn't, which is what keeps a settled room silent.
    client.post("/space/capture", json={"client_id": "hs_1",
                                        "surfaces": [_surface("real_wall_1", pos=(0.0, 1.2, 2.0))],
                                        "replace": True})
    ups = [x for x in _lines(srv) if x["ev"] == "seed.update"]
    assert len(ups) == 1 and ups[0]["id"] == "real_wall_1" and ups[0]["why"] == "position"
    # `wrote` names the fields that actually changed — the log has to show that the gate and the payload
    # agree, since their disagreeing is what corrupted a seed.
    assert ups[0]["wrote"] == ["transform.position"]


def test_the_log_rotates_by_day_and_prunes_past_retention(srv, monkeypatch, tmp_path):
    """Days-to-weeks of history, and no unbounded growth. conjure.log is a single unrotated 8.8 MB file;
    this exists precisely so "compare Tuesday to Friday" stays possible."""
    srv.GEO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, geometry_log_days=21))
    old = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    recent = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    for day in (old, recent):
        (srv.GEO_LOG_DIR / f"geometry-{day}.jsonl").write_text('{"ev":"x"}\n')
    (srv.GEO_LOG_DIR / "keep-me.txt").write_text("not ours")

    srv._glog("churn.mint", {"sem": "wall"})

    today = datetime.now().strftime("%Y-%m-%d")
    assert (srv.GEO_LOG_DIR / f"geometry-{today}.jsonl").exists(), "today's events go in today's file"
    assert not (srv.GEO_LOG_DIR / f"geometry-{old}.jsonl").exists(), "40 days old, past 21-day retention"
    assert (srv.GEO_LOG_DIR / f"geometry-{recent}.jsonl").exists(), "3 days old, still inside the window"
    assert (srv.GEO_LOG_DIR / "keep-me.txt").exists(), "retention only ever touches its own files"


def test_retention_of_zero_keeps_everything(srv, monkeypatch):
    srv.GEO_LOG_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, geometry_log_days=0))
    old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    (srv.GEO_LOG_DIR / f"geometry-{old}.jsonl").write_text('{"ev":"x"}\n')
    srv._glog("churn.mint", {"sem": "wall"})
    assert (srv.GEO_LOG_DIR / f"geometry-{old}.jsonl").exists()


def test_logging_off_writes_nothing_at_all(srv, client, monkeypatch):
    monkeypatch.setattr(srv, "settings", dataclasses.replace(srv.settings, geometry_log=False))
    client.post("/geometry_log", json={"sid": "s_x", "events": [{"ev": "churn.mint"}]})
    client.post("/space/capture", json={"client_id": "hs_1", "surfaces": [_surface("real_wall_1")],
                                        "replace": True})
    assert _lines(srv) == []


def test_a_runaway_client_cannot_flood_the_day(srv, client):
    client.post("/geometry_log", json={"sid": "s_x",
                                       "events": [{"ev": "churn.mint", "i": i} for i in range(500)]})
    assert len(_lines(srv)) == 200, "one flush is capped; a wedged client can't fill the disk"


def test_an_unserialisable_event_never_breaks_a_capture(srv):
    """A diagnostic that can throw is worse than no diagnostic — it would take a capture down with it."""
    srv._glog("churn.mint", {"bad": {1, 2, 3}})               # a set is not JSON
    srv._glog("churn.mint", {"sem": "wall"})
    rows = _lines(srv)
    assert [x["ev"] for x in rows] == ["churn.mint"], "the bad line is dropped, the next one still lands"


# ── the seed is written only in the fields that changed (docs/specs/spaces-geometry.md §8) ─────
# A structural gate that writes the WHOLE record imports whatever frame the capture was in. Observed
# 2026-08-31: a relocalization put the space ~93 mm low, a door appeared on two walls, and the seed
# absorbed the offset into those walls and a ceiling while its other 55 surfaces kept the old frame —
# leaving the reference the floating-room detector, guest registration and recovery all measure against
# internally inconsistent.

def _post(client, surfaces):
    return client.post("/space/capture", json={"client_id": "hs_1", "surfaces": surfaces, "replace": True})


def _stored(client, sid):
    return next(e for e in client.get("/world").json()["entities"] if e["id"] == sid)


def test_an_opening_change_does_not_rewrite_the_surfaces_position(srv, client):
    """The exact bug. A door appearing on a wall is a real change to `holes` and nothing else."""
    wall = _surface("real_wall_1", pos=(0.0, 1.2, 0.0))
    _post(client, [wall])
    before = _stored(client, "real_wall_1")["transform"]["position"]

    moved_and_holed = dict(wall, position=[0.0, 1.29, 0.0],          # 90 mm of live displacement…
                           holes=[{"x": 0.0, "y": 0.0, "w": 0.9, "h": 2.0}])   # …carried by an opening change
    _post(client, [moved_and_holed])

    after = _stored(client, "real_wall_1")
    assert after["components"]["surface"]["holes"], "the opening is recorded"
    assert after["transform"]["position"] == before, "and the displacement is NOT imported with it"
    ev = next(x for x in _lines(srv) if x["ev"] == "seed.update")
    assert ev["why"] == "holes" and ev["wrote"] == ["components.surface.holes"]


def test_a_rotation_does_not_rewrite_the_surfaces_position(srv, client):
    """The other half of what was observed: `real_ceiling_13` was written `why: rotated`, and its stored
    height moved 22 mm with it — enough on its own to flip a room's coherence test either way."""
    ceil = _surface("real_ceiling_1", "ceiling", pos=(0.0, 2.68, 0.0))
    _post(client, [ceil])
    _post(client, [dict(ceil, position=[0.0, 2.59, 0.0], rotation=[180.0, 0.0, 0.0])])
    after = _stored(client, "real_ceiling_1")
    assert after["transform"]["rotation"] == [180.0, 0.0, 0.0]
    assert after["transform"]["position"] == [0.0, 2.68, 0.0], "height held; only the rotation changed"


def test_a_genuine_move_still_writes_the_pose(srv, client):
    """The gate's original purpose is intact — real furniture moving is exactly what it must let through."""
    s0 = _surface("real_table_1", "table", pos=(0.0, 0.5, 0.0))
    _post(client, [s0])
    _post(client, [dict(s0, position=[2.0, 0.5, 0.0])])
    assert _stored(client, "real_table_1")["transform"]["position"] == [2.0, 0.5, 0.0]


def test_a_resize_carries_the_centre_with_it(srv, client):
    """Size and centre are ONE measurement (§9.1's matched-pair rule). Storing a new extent against an old
    centre would be worse than writing neither — the rectangle would describe a shape never captured."""
    s0 = _surface("real_wall_1", pos=(0.0, 1.2, 0.0))
    _post(client, [s0])
    _post(client, [dict(s0, position=[0.0, 1.6, 0.0], extent=[4.2, 3.2])])
    after = _stored(client, "real_wall_1")
    assert after["components"]["surface"]["extent"] == [4.2, 3.2]
    assert after["transform"]["position"] == [0.0, 1.6, 0.0], "centre comes with the size"


def test_drift_below_the_threshold_still_writes_nothing(srv, client):
    s0 = _surface("real_wall_1", pos=(0.0, 1.2, 0.0))
    _post(client, [s0])
    n = len(_lines(srv))
    _post(client, [dict(s0, position=[0.0, 1.29, 0.0])])       # 90 mm — drift, not a relocation
    assert len(_lines(srv)) == n, "a settled room still says nothing"
    assert _stored(client, "real_wall_1")["transform"]["position"] == [0.0, 1.2, 0.0]
