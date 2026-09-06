"""The named pose library — tier 2 of the pose vocabulary, and it is DATA.

Tier 1 is the axis vocabulary (`bend`, `spread`, `turn`, `aim`) and is closed by design. Tier 3 is
solving against the world ("hand flat on that table") and wants a solver, not a vocabulary. This is the
middle, and the only one that extends: a pose is a dict in the vocabulary that already exists, so adding
"kneel" is an entry rather than code.

**Because tier 1 is rig-independent, one authored pose works on every figure.** That is the first thing
the axis work buys rather than merely protects, and it is measured rather than assumed — every pose here
is checked against its own `signature` on all three rigs of the eval cast by `scripts/pose_library.py`.

**Every pose carries the assertions that define it.** A named pose nobody can check is a dict of numbers
someone once liked the look of, and the design this replaces had a vision model doing the checking —
which turned out to pass a figure with its head on backwards (docs/backlogs/figures.md). Geometry says
whether the knees are below the hips; it is the verifier in this feature that has held up. So the pose
library and the eval corpus are the same kind of object, and share the same predicate vocabulary.

**What is NOT here, and why:**

* Anything needing the world. "Sit on that chair" is tier 3 — `sit` here makes the shape of sitting and
  says in `needs` that something has to be under her.
* Anything needing the trunk to carry the body. `hips` reaches the whole figure on a VRM rig and only
  the legs on a Daz one (measured 2026-09-05), so a pose rooted there would mean two different things.
* "Lie down". `hips` is clamped to ±45° on every axis, and laying a figure horizontal is placement —
  the entity's own transform — not a pose.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Pose:
    """One named configuration: what it does, what it means, and how to tell whether it worked."""

    name: str
    about: str                       # one line, and the director reads it — write it for a reader
    bones: dict                      # exactly what `pose_figure` takes, in tier-1 terms
    signature: tuple = ()            # `pose_corpus` predicates that must hold once it is applied
    needs: str = ""                  # what the WORLD has to supply for this to make sense


#: Both knees down. The pose that motivated re-grounding, and the one that proved the design note wrong
#: twice over.
#:
#: First: a kneeling figure does not sink through the floor, she FLOATS 54 cm, because every joint hangs
#: off hips that rotation cannot move and a folded leg is shorter than a straight one.
#:
#: Second: the numbers this document authored for it — hip −75, knee 105 — are not a kneel. The hip is
#: clamped at −35 anyway, and what comes out is a figure with its thighs swung back and its shins in the
#: air. A kneel is geometrically simple once looked at: the thigh stays VERTICAL and the shin folds to
#: horizontal, which is 90 degrees at the knee and nothing at all at the hip. Rendered, glanced at,
#: corrected — the loop working exactly as it is supposed to, on the one pose everybody was sure of.
_KNEEL = {
    "leftUpperLeg": {"bend": 0}, "leftLowerLeg": {"bend": 90}, "leftFoot": {"bend": -35},
    "rightUpperLeg": {"bend": 0}, "rightLowerLeg": {"bend": 90}, "rightFoot": {"bend": -35},
    "spine": {"bend": 5},
}

POSES: tuple[Pose, ...] = (
    Pose("kneel", "down on both knees, sitting back on the heels",
         _KNEEL,
         # `points ... back` is the predicate that matters, and it is here because the WRONG kneel
         # satisfied everything else: knees below hips, feet moved back, both true of a figure with its
         # shins in the air. A shin that points backward and level is what makes it a kneel.
         signature=(("points", "leftLowerLeg", "back"), ("points", "rightLowerLeg", "back"),
                    ("below", "leftLowerLeg", "hips"), ("below", "rightLowerLeg", "hips"),
                    ("moved", "leftFoot", "back", 0.05), ("moved", "rightFoot", "back", 0.05))),
    Pose("kneel-one", "down on the right knee with the left foot planted in front — a proposal",
         {"rightUpperLeg": {"bend": -10}, "rightLowerLeg": {"bend": 95}, "rightFoot": {"bend": -35},
          "leftUpperLeg": {"bend": 75}, "leftLowerLeg": {"bend": 80}, "spine": {"bend": 5}},
         signature=(("points", "rightLowerLeg", "back"), ("below", "rightLowerLeg", "hips"),
                    ("moved", "rightFoot", "back", 0.05),
                    ("moved", "leftLowerLeg", "forward", 0.05))),
    Pose("crouch", "squatting on both feet, knees bent deep, torso forward for balance",
         {"leftUpperLeg": {"bend": 85}, "leftLowerLeg": {"bend": 105}, "leftFoot": {"bend": 25},
          "rightUpperLeg": {"bend": 85}, "rightLowerLeg": {"bend": 105}, "rightFoot": {"bend": 25},
          "spine": {"bend": 25}},
         signature=(("moved", "leftLowerLeg", "forward", 0.05), ("moved", "head", "forward", 0.05))),
    Pose("sit", "seated with the thighs forward and the shins down, as on a chair",
         {"leftUpperLeg": {"bend": 85}, "leftLowerLeg": {"bend": 85},
          "rightUpperLeg": {"bend": 85}, "rightLowerLeg": {"bend": 85}, "spine": {"bend": 5}},
         signature=(("points", "leftUpperLeg", "forward"), ("points", "rightUpperLeg", "forward"),
                    ("moved", "leftLowerLeg", "forward", 0.08),
                    ("moved", "rightLowerLeg", "forward", 0.08)),
         needs="something under her — a seat is tier 3, so place a chair and put her on it yourself"),
    Pose("t-pose", "arms straight out to the sides, the reference stance",
         {"leftUpperArm": {"aim": "out"}, "rightUpperArm": {"aim": "out"},
          "leftLowerArm": {}, "rightLowerArm": {}},
         signature=(("points", "leftUpperArm", "out"), ("points", "rightUpperArm", "out"))),
    Pose("cheer", "both arms straight up",
         {"leftUpperArm": {"aim": "up"}, "rightUpperArm": {"aim": "up"}},
         signature=(("points", "leftUpperArm", "up"), ("points", "rightUpperArm", "up"),
                    ("above", "leftHand", "head"), ("above", "rightHand", "head"))),
    Pose("reach-out", "both arms straight out in front, reaching",
         {"leftUpperArm": {"aim": "forward"}, "rightUpperArm": {"aim": "forward"}},
         signature=(("points", "leftUpperArm", "forward"), ("points", "rightUpperArm", "forward"))),
    Pose("hands-on-hips", "elbows out, hands at the waist",
         {"leftUpperArm": {"bend": -10, "spread": 25, "turn": 55},
          "leftLowerArm": {"bend": 95, "turn": -20},
          "rightUpperArm": {"bend": -10, "spread": 25, "turn": 55},
          "rightLowerArm": {"bend": 95, "turn": -20}},
         signature=(("nearer", "leftHand", "hips", 0.08), ("nearer", "rightHand", "hips", 0.08))),
    Pose("arms-crossed", "arms folded across the chest, left forearm over right",
         # What crosses the arms is TURN at the shoulder, not anything at the elbow. Two wrong answers
         # came first: bending the forearms forward gave a surrender pose (hands up beside the head),
         # and aiming them `in` was refused by the joint limits — correctly, since an elbow does not
         # abduct, and the clamp left the forearms pointing forward. Internal rotation of the upper arm
         # is what sweeps a bent forearm across the chest, which is also what a person does.
         # The two sides differ by 12 degrees of shoulder flexion so one forearm rests over the other
         # instead of intersecting it.
         # Negative spread tucks the elbows against the ribs, which is what lets the hands travel PAST
         # the midline instead of meeting at it — the difference between folded arms and clasped hands,
         # and 3 cm of hand travel either side of it.
         {"leftUpperArm": {"bend": 8, "turn": 75, "spread": -30}, "leftLowerArm": {"bend": 115},
          "rightUpperArm": {"bend": 20, "turn": 75, "spread": -30}, "rightLowerArm": {"bend": 115}},
         # Crossing the midline is what makes it crossed, so that is what is asserted. `nearer` alone
         # let the wrong pose through.
         signature=(("moved", "leftHand", "in", 0.25), ("moved", "rightHand", "in", 0.25),
                    ("points", "leftLowerArm", "in"), ("points", "rightLowerArm", "in"))),
    Pose("wave", "right arm up and bent, hand raised beside the head",
         {"rightUpperArm": {"aim": [0.6, 1.0, 0.2]}, "rightLowerArm": {"bend": 45},
          "head": {"turn": -10}},
         signature=(("above", "rightHand", "rightUpperArm"),)),
    Pose("point", "right arm straight out in front, pointing",
         {"rightUpperArm": {"aim": "forward"}, "rightLowerArm": {}},
         signature=(("points", "rightUpperArm", "forward"),)),
    Pose("bow", "bent forward from the waist, head lowered",
         {"spine": {"bend": 40}, "chest": {"bend": 15}, "neck": {"bend": 15}},
         signature=()),          # the trunk carries different bones per rig — see the module docstring
    Pose("stand", "back to a plain neutral stance", {}, signature=()),
)

BY_NAME: dict[str, Pose] = {p.name: p for p in POSES}


def resolve(name: str) -> Pose | None:
    """A pose by name, case- and separator-insensitively. "Hands On Hips" and "hands_on_hips" both land
    on `hands-on-hips`, because a director writing English should not have to guess our punctuation."""
    key = (name or "").strip().lower().replace(" ", "-").replace("_", "-")
    return BY_NAME.get(key)


def catalogue() -> str:
    """The library as the director sees it — name, what it does, and what it still needs from the world.

    Read from the data rather than written into a prompt, the way `dynamics://available` is, so adding a
    pose makes it discoverable with no second edit.
    """
    lines = []
    for p in POSES:
        line = f"{p.name} — {p.about}"
        if p.needs:
            line += f" (needs {p.needs})"
        lines.append(line)
    return "\n".join(lines)
