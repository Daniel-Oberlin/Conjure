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
