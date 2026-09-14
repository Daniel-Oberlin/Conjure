"""`conjure.ctl` — the direct, LLM-free world-server commands (split out of the CLI, which is now just
the conversational client)."""

from __future__ import annotations

import pytest

from conjure.ctl import build_parser, cmd_world, parse_bones


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
            "clips": ["clips", "jane"], "clip": ["clip", "jane", "1_idle"],
            "pose": ["pose", "jane", "kneel"], "dress": ["dress", "jane"],
        }.get(name, [name])
        assert callable(getattr(p.parse_args(args), "fn", None)), name


def test_parses_the_shapes_the_commands_rely_on():
    p = build_parser()
    assert p.parse_args(["add", "box", "--color", "red", "--pos", "0", "1", "-3"]).pos == [0.0, 1.0, -3.0]
    assert p.parse_args(["asset", "oak tree", "--size", "7"]).size == 7.0
    assert p.parse_args(["image", "a dragon", "--transparent"]).transparent is True
    assert p.parse_args(["retag-skyboxes", "--min-aspect", "1.9"]).min_aspect == 1.9
    assert p.parse_args(["annotate"]).state == "on"                    # optional positional defaults on
    # `clip <id>` with no clip named is the same request as `--stop`, so the positional is optional and
    # the handler routes an empty one to the stop path — the endpoint answers it with the STOP shape,
    # which has no `clip` key to print.
    assert p.parse_args(["clip", "jane"]).clip == ""
    assert p.parse_args(["clip", "jane", "3_action", "--once"]).once is True
    # `--bone` and `--hide` accumulate: a figure is posed and undressed a piece at a time.
    a = p.parse_args(["pose", "jane", "kneel", "--bone", "leftUpperArm:bend=45",
                      "--bone", "rightUpperArm:aim=up"])
    assert a.named == "kneel" and a.bone == ["leftUpperArm:bend=45", "rightUpperArm:aim=up"]
    assert p.parse_args(["pose", "jane", "--clear"]).clear is True
    a = p.parse_args(["dress", "jane", "--hide", "clothing", "--hide", "shoes", "--show", "hair"])
    assert a.hide == ["clothing", "shoes"] and a.show == ["hair"] and a.only_body is False
    assert p.parse_args(["dress", "jane", "--only-body"]).only_body is True
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


def test_a_bone_spec_splits_an_AIM_from_a_number_of_degrees():
    """`aim` takes a DIRECTION — a word, or a three-vector — while bend/spread/turn take degrees. Send
    a string where the server wants a number and the validation error comes back from three layers
    down naming neither the bone nor the flag, so the split happens where the input is still in hand."""
    assert parse_bones(["leftUpperArm:bend=45"]) == ({"leftUpperArm": {"bend": 45.0}}, "")
    assert parse_bones(["rightUpperArm:aim=up"]) == ({"rightUpperArm": {"aim": "up"}}, "")
    assert parse_bones(["a:aim=0,1,1"]) == ({"a": {"aim": [0.0, 1.0, 1.0]}}, ""), "commas or spaces"
    assert parse_bones(["a:aim=0 1 1"]) == ({"a": {"aim": [0.0, 1.0, 1.0]}}, "")


def test_bone_specs_accumulate_onto_one_bone():
    """Two axes of the same joint are one entry, not two — which is also how the endpoint merges."""
    pose, err = parse_bones(["leftUpperArm:bend=45", "leftUpperArm:turn=-10"])
    assert pose == {"leftUpperArm": {"bend": 45.0, "turn": -10.0}} and not err


@pytest.mark.parametrize("spec, wanted", [
    ("nonsense", "NAME:AXIS=VALUE"),
    ("a:bend=up", "not a number of degrees"),
    ("a:aim=x,y,z", "not a direction or a three-vector"),
    ("a:bend=", "NAME:AXIS=VALUE"),
])
def test_a_malformed_bone_spec_is_refused_by_name(spec, wanted):
    """Named back with the offending spec in it. A pose is typed by hand and a typo in one of four
    flags is otherwise a hunt."""
    pose, err = parse_bones([spec])
    assert pose == {} and wanted in err
