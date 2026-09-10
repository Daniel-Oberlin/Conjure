"""The named pose library — tier 2 of the pose vocabulary, and it is DATA.

Tier 1 is the axis vocabulary (`bend`, `spread`, `turn`, `aim`) and is closed by design. Tier 3 is
solving against the world ("hand flat on that table") and wants a solver, not a vocabulary. This is the
middle, and the only one that extends: a pose is a dict in the vocabulary that already exists, so adding
"kneel" is an entry rather than code.

**Because tier 1 is rig-independent, one authored pose works on every figure.** That is the first thing
the axis work buys rather than merely protects, and it is measured rather than assumed — every pose here
is checked against its own `signature` on all three rigs of the eval cast by `scripts/pose_library.py`.

**Every pose carries the assertions that define it.** A named pose nobody can check is a dict of numbers
someone once liked the look of. `signature` holds `pose_corpus` predicates, so the pose library and the
eval corpus are the same kind of object over one evaluator, and "is this a kneel" is arithmetic.

**Geometry gates; a vision model looks.** `scripts/pose_library.py --identify` renders each pose and
asks a model WHICH of these poses it is — recognition, never "is this pose any good", which is a
judgement and demonstrably fails (it passes a figure with its head on backwards). Recognition catches
what a signature structurally cannot: **a signature only asserts the bones a pose sets, so it is blind
to the bones a pose forgets.** Both defects below were found that way and neither was visible to any
assertion.

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
    clears: bool = False             # return every OTHER bone to rest first (see `stand`)


#: Arms hanging at the sides — and every pose that is ABOUT THE LEGS has to say so.
#:
#: A pose only sets the bones it names; everything else stays at the rig's bind pose. On the two A-posed
#: Daz conversions that is invisible, and on Saka it means a kneeling figure with her arms straight out
#: like a scarecrow, because a VRoid rig rests in a T-pose. "One authored pose works on every figure"
#: quietly stops being true, and no signature catches it — a signature only asserts the bones the pose
#: sets. The visual check is what found it (2026-09-05).
#:
#: `aim` is the right tool precisely because it is ABSOLUTE: "point the arm down" lands the same way
#: from a T-pose and an A-pose, which is the whole reason it exists.
#:
#: **`down` is not straight down, and the 8° is measured.** Aimed along the body's own axis, an arm ends
#: up INSIDE the torso — reported from the headset 2026-09-09. A shoulder sits almost exactly at the
#: torso's edge, so the overlap is the arm's own radius, which joint positions cannot see. Measured with
#: `figures.body_profile` + `limb_radius` across the cast:
#:
#:     rig      torso half-width   shoulder out   arm radius   overlap   tangent angle
#:     Saka           6.5 cm           8.0 cm       2.2 cm      0.6 cm       0.8°
#:     Grace         15.9 cm          15.2 cm       3.0 cm      3.7 cm       4.2°
#:     Trish         15.9 cm          14.8 cm       2.9 cm      4.0 cm       4.2°
#:
#: 4.2° is where the arm is exactly tangent to the body on the worst rig. 8° carries a margin over that
#: and is also what a person does — an arm hangs abducted, not plumb. The `clears` predicate on every
#: pose that uses this is what stops it regressing.
_ARMS_DOWN = {
    "leftUpperArm": {"aim": [0.14, -1, 0]}, "rightUpperArm": {"aim": [0.14, -1, 0]},
    "leftLowerArm": {}, "rightLowerArm": {},
}

#: Asserted by every pose whose arms hang: the wrist must sit outside the torso by the forearm's own
#: radius. Needs a mesh measurement, so it is skipped rather than failed on a file that cannot answer.
#: The ELBOW is checked as well as the wrist, because the wrist alone misses where a body flares. On
#: Saka the wrist clears by 6 mm while the hips below it are wider — reported from the headset as arms
#: entering her hips "a little". Two samples along the limb, not one.
_ARMS_CLEAR = (("clears", "leftHand", "leftLowerArm"), ("clears", "rightHand", "rightLowerArm"),
               ("clears", "leftLowerArm", "leftLowerArm"),
               ("clears", "rightLowerArm", "rightLowerArm"))

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
    "spine": {"bend": 5}, **_ARMS_DOWN,
}

#: The kneel this project designed, kept because it is the best negative control there is: it passed the
#: first signature written for it AND looks obviously wrong, which is exactly the failure a geometric
#: assertion cannot be trusted to catch on its own. `scripts/pose_library.py --identify` renders it and
#: requires the judge to answer "none of these" before believing anything it says about a real pose.
WRONG_KNEEL = {
    "leftUpperLeg": {"bend": -75}, "leftLowerLeg": {"bend": 105}, "leftFoot": {"bend": -20},
    "rightUpperLeg": {"bend": -75}, "rightLowerLeg": {"bend": 105}, "rightFoot": {"bend": -20},
    "spine": {"bend": 5},
}

#: Offered alongside the pose names so the judge can decline rather than pick the nearest — which is the
#: whole reason it catches a wrong pose instead of rounding it to the intended one.
NONE_OF_THESE = "none of these"

POSES: tuple[Pose, ...] = (
    Pose("kneel", "down on both knees, sitting back on the heels",
         _KNEEL,
         # `points ... back` is the predicate that matters, and it is here because the WRONG kneel
         # satisfied everything else: knees below hips, feet moved back, both true of a figure with its
         # shins in the air. A shin that points backward and level is what makes it a kneel.
         signature=(("points", "leftLowerLeg", "back"), ("points", "rightLowerLeg", "back"),
                    ("below", "leftLowerLeg", "hips"), ("below", "rightLowerLeg", "hips"),
                    ("moved", "leftFoot", "back", 0.05), ("moved", "rightFoot", "back", 0.05),
                    ("points", "leftUpperArm", "down"), ("points", "rightUpperArm", "down"))
                   + _ARMS_CLEAR),
    Pose("kneel-one", "down on the right knee with the left foot planted in front — a proposal",
         {"rightUpperLeg": {"bend": -10}, "rightLowerLeg": {"bend": 95}, "rightFoot": {"bend": -35},
          "leftUpperLeg": {"bend": 75}, "leftLowerLeg": {"bend": 80}, "spine": {"bend": 5},
          **_ARMS_DOWN},
         signature=(("points", "rightLowerLeg", "back"), ("below", "rightLowerLeg", "hips"),
                    ("moved", "rightFoot", "back", 0.05),
                    ("moved", "leftLowerLeg", "forward", 0.05))),
    Pose("crouch", "squatting on both feet, knees bent deep, torso forward for balance",
         {"leftUpperLeg": {"bend": 85}, "leftLowerLeg": {"bend": 105}, "leftFoot": {"bend": 25},
          "rightUpperLeg": {"bend": 85}, "rightLowerLeg": {"bend": 105}, "rightFoot": {"bend": 25},
          "spine": {"bend": 25}, **_ARMS_DOWN},
         signature=(("moved", "leftLowerLeg", "forward", 0.05), ("moved", "head", "forward", 0.05))),
    Pose("sit", "seated with the thighs forward and the shins down, as on a chair",
         {"leftUpperLeg": {"bend": 85}, "leftLowerLeg": {"bend": 85},
          "rightUpperLeg": {"bend": 85}, "rightLowerLeg": {"bend": 85}, "spine": {"bend": 5},
          **_ARMS_DOWN},
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
    # A ONE-ARMED pose still has to say what the other arm does. Same defect as the leg poses had, found
    # the same way: `wave` and `point` were misread on all three rigs at once, which is the signature of
    # a bad pose rather than a noisy judge. On a T-posed rig the idle arm stayed straight out, so a wave
    # read as a cheer and a point read as a T-pose — both fair descriptions of what was rendered.
    Pose("wave", "right arm up and bent, hand raised beside the head, left arm down",
         {"rightUpperArm": {"aim": [0.6, 1.0, 0.2]}, "rightLowerArm": {"bend": 45},
          "leftUpperArm": {"aim": "down"}, "leftLowerArm": {}, "head": {"turn": -10}},
         signature=(("above", "rightHand", "rightUpperArm"),
                    ("points", "leftUpperArm", "down"))),
    Pose("point", "right arm straight out in front, pointing, left arm down",
         {"rightUpperArm": {"aim": "forward"}, "rightLowerArm": {},
          "leftUpperArm": {"aim": "down"}, "leftLowerArm": {}},
         signature=(("points", "rightUpperArm", "forward"),
                    ("points", "leftUpperArm", "down"))),
    Pose("bow", "bent forward from the waist, head lowered",
         {"spine": {"bend": 40}, "chest": {"bend": 15}, "neck": {"bend": 15}, **_ARMS_DOWN},
         # Nothing is asserted here, and both halves of that are deliberate. The trunk carries different
         # bones on different rigs (see the module docstring). And the ARMS cannot be asserted either:
         # `aim` is absolute with respect to the BIND pose, not to wherever the bone's ancestors have
         # since been rotated — so arms aimed `down` under a spine bent 40 degrees come out 40 degrees
         # off vertical. That is correct behaviour and correct anatomy (arms hang from a bowed torso and
         # swing with it), and it is a real limit of `aim` worth knowing: aiming a limb while also
         # rotating what it hangs from compounds the two.
         signature=()),
    # `stand` is a POSE, not a reset: arms at the sides and legs straight, which is what standing looks
    # like on any rig. Returning to the FILE's bind pose is `clear=true`, and on a VRoid rig that is a
    # T-pose — a perfectly good reference stance and not what anyone means by "have her stand".
    # `clears` is what makes this a stance rather than an adjustment. A named pose merges per BONE, so
    # "stand" after "kneel" would otherwise put her arms down and leave her kneeling. It is the one pose
    # whose meaning includes everything it does NOT mention.
    Pose("stand", "a plain neutral stance, arms at the sides",
         dict(_ARMS_DOWN), clears=True,
         signature=(("points", "leftUpperArm", "down"), ("points", "rightUpperArm", "down"))
                   + _ARMS_CLEAR),
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
