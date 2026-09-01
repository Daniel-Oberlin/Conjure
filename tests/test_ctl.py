"""`conjure.ctl` — the direct, LLM-free world-server commands (split out of the CLI, which is now just
the conversational client)."""

from __future__ import annotations

import pytest

from conjure.ctl import build_parser, cmd_world


def test_bare_invocation_defaults_to_showing_the_world():
    # No subcommand is a useful default here (unlike the CLI, where it opens the REPL).
    assert getattr(build_parser().parse_args([]), "fn", None) is None   # main() falls back to cmd_world


def test_every_subcommand_binds_a_handler():
    p = build_parser()
    sub = next(a for a in p._actions if a.dest == "cmd")
    for name in sub.choices:
        args = {
            "add": ["add", "box"], "move": ["move", "e1", "0", "1", "2"], "remove": ["remove", "e1"],
            "asset": ["asset", "tree"], "image": ["image", "a dragon"], "skybox": ["skybox", "pines"],
            "grounded-skybox": ["grounded-skybox", "a meadow"], "texture": ["texture", "floor", "wood"],
            "style": ["style", "wall"], "edit": ["edit", "e1", "brighter"], "outpaint": ["outpaint", "e1"],
            "skybox-from": ["skybox-from", "e1"], "grab-mode": ["grab-mode", "skybox"],
        }.get(name, [name])
        assert callable(getattr(p.parse_args(args), "fn", None)), name


def test_parses_the_shapes_the_commands_rely_on():
    p = build_parser()
    assert p.parse_args(["add", "box", "--color", "red", "--pos", "0", "1", "-3"]).pos == [0.0, 1.0, -3.0]
    assert p.parse_args(["asset", "oak tree", "--size", "7"]).size == 7.0
    assert p.parse_args(["image", "a dragon", "--transparent"]).transparent is True
    assert p.parse_args(["retag-skyboxes", "--min-aspect", "1.9"]).min_aspect == 1.9
    assert p.parse_args(["annotate"]).state == "on"                    # optional positional defaults on
    assert p.parse_args(["edges", "off"]).state == "off"
    assert p.parse_args(["world"]).fn is cmd_world


def test_annotate_and_edges_reject_a_bad_state():
    for argv in (["annotate", "maybe"], ["edges", "sometimes"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)


def test_ctl_does_not_carry_the_conversational_flags():
    # `--user` did nothing here (no ctl command sent an identity), and the agent/LLM live on the CLI side.
    for argv in (["--user", "alice", "world"], ["--agent", "outdoor", "world"], ["say", "hello"]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(argv)
