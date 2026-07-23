"""JS/Python parity for the plane-relative anchor solver (docs/local-first-geometry.md §13.1).

The server's conjure/plane_anchor.py is a 1:1 port of client/plane-anchor.js. Both are pinned to the SAME
golden vectors (tests/js/fixtures/plane-anchor-golden.json) — this file checks the Python side against them,
tests/js/plane-anchor.test.js checks the JS side. If an intentional algorithm change regenerates the
fixture, BOTH suites must still pass, which is what keeps the two implementations from silently drifting.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from conjure.plane_anchor import author_anchor, solve_anchor

FIXTURE = Path(__file__).resolve().parent / "js" / "fixtures" / "plane-anchor-golden.json"


def _quat_angle(a, b) -> float:
    """Angle (radians) between two unit quaternions — matches THREE.Quaternion.angleTo: 2·acos(|dot|)."""
    dot = sum(x * y for x, y in zip(a, b))
    return 2.0 * math.acos(min(1.0, abs(dot)))


def _normalize(q):
    n = math.sqrt(sum(c * c for c in q))
    return [c / n for c in q] if n else q


def _load_cases():
    gold = json.loads(FIXTURE.read_text())
    return gold["cases"]


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["name"])
def test_golden_vectors_match_js(case):
    """Every golden case reproduces the recorded output within the cross-language tolerance."""
    got = solve_anchor(case["anchor"], case["planes"])
    exp = case["expect"]
    assert got["ok"] == exp["ok"], f"{case['name']}: ok mismatch ({got['stat']})"
    if not exp["ok"]:
        return
    # position: 1e-6 m (same bar as the JS suite)
    dp = math.dist(got["position"], exp["position"])
    assert dp < 1e-6, f"{case['name']}: position off by {dp} ({got['position']})"
    # orientation: quaternion ANGLE error scales as √(component error), so 1e-5 rad (~6e-4°) is a tight-but-
    # realistic cross-language bar — far below anything visible.
    da = _quat_angle(_normalize(got["quaternion"]), _normalize(exp["quaternion"]))
    assert da < 1e-5, f"{case['name']}: quaternion off by {da} rad ({got['quaternion']})"


def test_author_then_solve_round_trips():
    """Author an anchor from a pose against a room, solve it back against the SAME room → recover the pose.
    Exercises author_anchor (not covered by the solve-only golden vectors) and confirms the round trip."""
    planes = [
        {"id": "floor", "kind": "floor", "normal": [0, 1, 0], "point": [0, 0, 0]},
        {"id": "wall_xp", "kind": "wall", "normal": [1, 0, 0], "point": [2, 1.2, 0]},
        {"id": "wall_xn", "kind": "wall", "normal": [-1, 0, 0], "point": [-2, 1.2, 0]},
        {"id": "wall_zp", "kind": "wall", "normal": [0, 0, 1], "point": [0, 1.2, 3]},
        {"id": "wall_zn", "kind": "wall", "normal": [0, 0, -1], "point": [0, 1.2, -3]},
    ]
    # a free pose with a real tilt so orientation is non-trivial
    half = math.radians(20) / 2
    entity = {"position": [0.4, 1.3, -0.9],
              "quaternion": [math.sin(half), 0.0, 0.0, math.cos(half)], "mode": "free"}
    anchor = author_anchor(entity, planes)
    got = solve_anchor(anchor, planes)
    assert got["ok"], got["stat"]
    assert math.dist(got["position"], entity["position"]) < 1e-9
    assert _quat_angle(_normalize(got["quaternion"]), entity["quaternion"]) < 1e-9


def test_server_authors_anchor_that_round_trips(monkeypatch):
    """Step 7c(A): the server builds seed planes from its real surfaces and authors a content anchor that,
    solved back against those same seed planes, recovers the placed pose. Exercises server._seed_planes +
    server._content_anchor end to end (the wiring that persists meta.anchor on placed models)."""
    from conjure import server
    from conjure.world import WorldStore

    def wall(wid, pos, ry):   # a-plane euler [rx, ry, rz]; _plane_basis reads ry → outward normal
        return {"id": wid, "meta": {"real": True, "semantic": "wall"},
                "transform": {"position": pos, "rotation": [0.0, ry, 0.0]}}

    doc = {"id": "t", "name": "T", "rev": 0, "environment": {}, "entities": [
        {"id": "real_floor_0", "meta": {"real": True, "semantic": "floor"},
         "transform": {"position": [0, 0, 0], "rotation": [-90, 0, 0]}},
        wall("real_wall_1", [2, 1.2, 0], 90),      # normal +X
        wall("real_wall_2", [-2, 1.2, 0], -90),    # normal -X
        wall("real_wall_3", [0, 1.2, 3], 0),       # normal +Z
        wall("real_wall_4", [0, 1.2, -3], 180),    # normal -Z
    ]}
    monkeypatch.setattr(server, "store", WorldStore(doc))

    planes = server._seed_planes()
    assert sum(1 for p in planes if p["kind"] == "wall") == 4
    assert sum(1 for p in planes if p["kind"] == "floor") == 1

    transform = {"position": [0.3, 0.0, -0.8], "scale": [1, 1, 1]}
    anchor = server._content_anchor(transform, "grounded")
    assert anchor is not None and anchor["floor"] and anchor["walls"]

    sol = solve_anchor(anchor, planes)
    assert sol["ok"], sol["stat"]
    assert math.dist(sol["position"], transform["position"]) < 1e-6, sol["position"]


def test_server_content_anchor_none_without_walls(monkeypatch):
    """Too few seed walls ⇒ no anchor authored (caller leaves the entity on its raw F_ref pose)."""
    from conjure import server
    from conjure.world import WorldStore
    doc = {"id": "t", "name": "T", "rev": 0, "environment": {}, "entities": [
        {"id": "real_floor_0", "meta": {"real": True, "semantic": "floor"},
         "transform": {"position": [0, 0, 0], "rotation": [-90, 0, 0]}}]}
    monkeypatch.setattr(server, "store", WorldStore(doc))
    assert server._content_anchor({"position": [0, 0, 0]}, "grounded") is None


def test_degenerate_parallel_walls_declines():
    """Two parallel walls (no XZ span) can't fix the lateral position → ok:False, not a bogus pose."""
    planes = [
        {"id": "floor", "kind": "floor", "normal": [0, 1, 0], "point": [0, 0, 0]},
        {"id": "wall_xp", "kind": "wall", "normal": [1, 0, 0], "point": [2, 1.2, 0]},
        {"id": "wall_xn", "kind": "wall", "normal": [-1, 0, 0], "point": [-2, 1.2, 0]},
    ]
    anchor = {"mode": "grounded", "floor": {"id": "floor", "offset": 0},
              "walls": [{"id": "wall_xp", "offset": -2, "rel": [0, 0, 0, 1]},
                        {"id": "wall_xn", "offset": -2, "rel": [0, 0, 0, 1]}]}
    got = solve_anchor(anchor, planes)
    assert got["ok"] is False
    assert got["position"] is None


def test_server_head_from_anchor_recovers_gaze(monkeypatch):
    """§7b: the server solves a streamed plane-relative head anchor against the seed to recover the head's
    origin + look direction (non-rigid-consistent), the shape view_relative consumes. Author an anchor for a
    known head pose against the seed walls, then _head_from_anchor must recover that origin + forward."""
    from conjure import server
    from conjure.world import WorldStore
    from conjure.plane_anchor import author_anchor as author

    def wall(wid, pos, ry):
        return {"id": wid, "meta": {"real": True, "semantic": "wall"},
                "transform": {"position": pos, "rotation": [0.0, ry, 0.0]}}
    doc = {"id": "t", "name": "T", "rev": 0, "environment": {}, "entities": [
        {"id": "real_floor_0", "meta": {"real": True, "semantic": "floor"},
         "transform": {"position": [0, 0, 0], "rotation": [-90, 0, 0]}},
        wall("real_wall_1", [2, 1.2, 0], 90), wall("real_wall_2", [-2, 1.2, 0], -90),
        wall("real_wall_3", [0, 1.2, 3], 0), wall("real_wall_4", [0, 1.2, -3], 180)]}
    monkeypatch.setattr(server, "store", WorldStore(doc))
    planes = server._seed_planes()

    # a head at (0.5, 1.6, -0.4) looking toward -Z (identity orientation → forward = -Z)
    head = {"position": [0.5, 1.6, -0.4], "quaternion": [0.0, 0.0, 0.0, 1.0], "mode": "free"}
    anchor = author(head, planes)
    got = server._head_from_anchor(anchor)
    assert got is not None
    assert math.dist(got["origin"], head["position"]) < 1e-6, got["origin"]
    assert math.dist(got["forward"], [0.0, 0.0, -1.0]) < 1e-6, got["forward"]


def test_server_head_from_anchor_none_paths(monkeypatch):
    """No anchor, or a seed without walls, → None (view_relative falls back to the presence pose)."""
    from conjure import server
    from conjure.world import WorldStore
    monkeypatch.setattr(server, "store", WorldStore(
        {"id": "t", "name": "T", "rev": 0, "environment": {}, "entities": []}))
    assert server._head_from_anchor(None) is None
    assert server._head_from_anchor({"mode": "free", "floor": None, "walls": []}) is None


def test_surface_offset_round_trips():
    """§7c-B2: _surface_offset returns the on-surface content's pose in its HOST's local frame (host⁻¹·image).
    Re-applying it to the host (image = host · offset) — exactly what the client does with the stored offset —
    must reconstruct the original image pose. This is what lets the client ride without the host seed pose."""
    from conjure import server
    spos, srot = [1.0, 1.2, 0.5], [0.0, 90.0, 0.0]        # host (F_ref)
    ipos, irot = [1.02, 1.5, 0.6], [3.0, 92.0, -1.0]      # image (2 cm off, slightly rotated)
    off = server._surface_offset(spos, srot, ipos, irot)
    qh = server._euler_yxz_quat(srot)
    p_img2 = [spos[i] + server._quat_rot(qh, off["p"])[i] for i in range(3)]   # host_pos + rot(qh, p_off)
    q_img2 = server._quat_mul(qh, off["q"])                                    # qh · q_off
    qi = server._euler_yxz_quat(irot)
    assert math.dist(p_img2, ipos) < 1e-4, p_img2
    assert abs(sum(q_img2[i] * qi[i] for i in range(4))) > 1 - 1e-6, "orientation reconstructs (up to sign)"
