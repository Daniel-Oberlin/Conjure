"""The pose evaluation corpus — what a person says, and what the figure must then be doing.

The layer this measures has no other test. `validate()` checks a bone map, `tests/test_figures.py`
checks the axes, and both would have **passed clean** on the arms bug of 2026-09-03: the geometry was
right and the English above it was wrong. A tool description is otherwise unfalsifiable — nothing fails
when a sentence stops steering an LLM correctly — so this file is the falsifier.

Run it with `scripts/pose_eval.py`. This module holds only the corpus and the scoring, no I/O and no
API calls, so it is unit-testable and so editing a phrase never means reading a runner.

**Three layers of check, cheapest first**, and each catches something the one above cannot:

1. `touches` / `leaves` / `signs` — the CALL. Which bones the director chose and which way it pushed
   them. Needs no render and no model file: this is the pass to run after editing a tool description.
2. `geometry` — where the joints ACTUALLY end up once the pose is applied to a real rig. Catches a call
   that names the right bone with a defensible-looking number and still puts the hand behind the back.
3. the judge — `subject`/`moves`, asked of a picture. Catches only what an assertion cannot name: a
   raised arm that is really a dislocated shoulder.

**Expectations are per-phrase, not per-rig.** That is the claim the whole vocabulary rests on — the same
sentence should do the same thing to Grace, Trish and Saka, whose rest poses differ by 45 degrees at the
shoulder. A phrase that needs a different expectation per rig is evidence against the design, not a
corpus entry to be special-cased.

Distances are fractions of the figure's own height, never metres: the cast is life-size but not
identical, and "the wrist rose 30 cm" means different things on a 1.55 m rig and a 1.80 m one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------- the cast
#
# Three rigs, chosen to disagree. Grace and Trish are Daz Genesis 8 through the Rigify conversion and
# rest in an A-pose; Saka is VRoid and states her own 54-bone map. If a phrase works on all three, the
# vocabulary is doing its job; if it works on two, the wording is riding a rest pose.
#
# The files live under `temp/` and are NOT in the repository (they are licensed content, and large).
# A missing rig is skipped with a note rather than failing the run.


@dataclass(frozen=True)
class Rig:
    name: str
    path: str
    note: str


CAST: tuple[Rig, ...] = (
    Rig("Grace", "temp/tmp-models/grace_nude5.glb", "Daz Genesis 8 → Rigify, 21 bones, A-pose"),
    Rig("Trish", "temp/tmp-models/trish_nude.glb", "Daz Genesis 8 → Rigify, 21 bones, A-pose"),
    Rig("Saka", "temp/3d-model-examples/Saka.vrm", "VRoid, states 54 bones — the control"),
)


# ---------------------------------------------------------------- the judge's fixed vocabulary
#
# One option set for every movement question, so the judge is answering the same question about every
# limb and no phrase can smuggle in a leading choice. "Barely moved at all" is the option that catches
# the failure this harness exists for: a call that was syntactically fine and did nothing.

MOVES: dict[str, str] = {
    "up": "up, raised above where it started",
    "down": "down, lowered below where it started",
    "forward": "forward, out in front of the body",
    "back": "backward, behind the body",
    "out": "out to the side, away from the body",
    "across": "across the front of the body",
    "still": "barely moved at all",
}

MOVE_QUESTION = ("Compared with the first images, which way has the figure's {subject} moved?")

#: A phrase with `moves=""` is not asked the movement question at all, and that is a claim about the
#: PHRASE, not a way of quieting a judge that disagreed. Three structural reasons for silence, all
#: measured on 2026-09-05: a trunk bone does not translate, so "which way did the head move" has no true
#: answer; an absolute request means something different on a rig that already rests in the target pose
#: (Saka's T-posed arms are already "out"); and some poses move two ways at once, which a seven-word
#: vocabulary cannot say. The plausibility question is asked of every cell regardless — it is the one
#: that asks something geometry cannot.

#: Asked of every posed render, with no per-phrase wording. This is the question geometry cannot ask.
PLAUSIBLE_QUESTION = ("Ignoring what the pose is meant to be: does this look like a position a human "
                      "body could actually hold?")
PLAUSIBLE_OPTIONS = (
    "yes — the joints all bend in directions a person's joints bend",
    "no — a limb is bent or twisted in a way a human body cannot do",
    "cannot tell from these two views",
)
PLAUSIBLE_OK = 0        #: the only answer that is a pass
PLAUSIBLE_BAD = 1       #: the only answer that is a FAILURE — "cannot tell" is an abstention

#: A pose no body can hold, for asking the judge a question with a known answer BEFORE trusting its
#: answers to the real ones. Raw euler on the bone the map calls `head`, in Blender's bone space where
#: Y runs along the bone — so this is a 170-degree twist of the skull on the neck, on any rig, and it
#: renders as a figure facing forward with the back of its head toward the camera.
#:
#: This exists because the plausibility question FAILED it (2026-09-05, both Gemini 2.5 Flash and Claude
#: Sonnet 4.6 answered "yes, a person could hold this"), which is only knowable if something asks. An
#: instrument that cannot detect the defect it is pointed at does not become trustworthy by being run
#: on more cells.
IMPOSSIBLE_TWIST = [0.0, 170.0, 0.0]
IMPOSSIBLE_BONE = "head"


# ---------------------------------------------------------------- the corpus


@dataclass(frozen=True)
class Phrase:
    """One thing a person says, and everything that must be true afterwards.

    `signs` is `(bone, axis, sign)` — the axis vocabulary is unit-tested, so what is worth asserting
    here is that the director reached for the right one and pushed it the right way. It is the only
    check available for the head and spine, whose joints barely move when they rotate.
    """

    id: str
    say: str
    touches: tuple[str, ...] = ()            # bones the call MUST set
    leaves: tuple[str, ...] = ()             # bones it must NOT set (interference, wrong side)
    signs: tuple[tuple[str, str, int], ...] = ()
    geometry: tuple[tuple, ...] = ()
    subject: str = ""                        # plain words, for the judge's question
    moves: str = ""                          # the key in MOVES the judge should pick
    why: str = ""                            # why this phrase is in the corpus at all


#: A limb aimed by its top bone only — the rule the tool description states twice and the one most
#: likely to be lost in an edit. "Raise her arm" is ONE call on the upper arm; aiming the forearm too
#: folds the elbow, which is how a raised arm becomes a chicken wing.
_ARM_ONLY_R = ("rightLowerArm", "rightHand")
_ARM_ONLY_L = ("leftLowerArm", "leftHand")

CORPUS: tuple[Phrase, ...] = (
    # --- the three that failed on device 2026-09-03. Everything else exists to keep company with them.
    Phrase("arm-up", "raise her right arm up",
           touches=("rightUpperArm",), leaves=_ARM_ONLY_R + ("leftUpperArm",),
           geometry=(("points", "rightUpperArm", "up"),
                     ("above", "rightHand", "rightUpperArm")),
           subject="right arm", moves="up",
           why="the measured bug: the director picked the wrong sign and the arm went down"),
    Phrase("arm-down", "point her right arm straight down",
           touches=("rightUpperArm",), leaves=_ARM_ONLY_R,
           geometry=(("points", "rightUpperArm", "down"),
                     ("below", "rightHand", "rightUpperArm")),
           subject="right arm", moves="",
           why="the mirror of arm-up. No movement question: an A-posed arm rests close to down and a T-posed one swings 90 degrees, so no single word is true of both"),
    Phrase("legs-apart", "spread her legs apart",
           touches=("leftUpperLeg", "rightUpperLeg"),
           geometry=(("apart", "leftFoot", "rightFoot", 0.06),),
           subject="legs", moves="out",
           why="failed on device: the same sign on both sides is a symmetric pose, and it was negated"),

    # --- aim, the six directions, on both sides
    Phrase("arm-forward", "have her hold her left arm straight out in front of her",
           touches=("leftUpperArm",), leaves=_ARM_ONLY_L,
           geometry=(("points", "leftUpperArm", "forward"),),
           subject="left arm", moves="forward",
           why="aim forward, and the left side, where a mirrored sign error hides"),
    Phrase("arms-out", "have her hold both arms straight out to the sides",
           touches=("leftUpperArm", "rightUpperArm"),
           leaves=("leftLowerArm", "rightLowerArm"),
           geometry=(("points", "leftUpperArm", "out"), ("points", "rightUpperArm", "out")),
           subject="arms", moves="",
           why="out is side-aware, so the SAME word on both sides must part them. Saka already rests in a T-pose, which is why this is scored on where the arms END UP and not asked of the judge at all"),
    Phrase("hands-up", "have her put both hands up in the air",
           touches=("leftUpperArm", "rightUpperArm"),
           geometry=(("points", "leftUpperArm", "up"), ("points", "rightUpperArm", "up"),
                     ("above", "leftHand", "leftUpperArm"), ("above", "rightHand", "rightUpperArm")),
           subject="arms", moves="up",
           why="the same as arm-up said a completely different way, and about hands rather than arms"),
    Phrase("arm-back", "swing her right arm back behind her",
           touches=("rightUpperArm",), leaves=_ARM_ONLY_R,
           geometry=(("moved", "rightHand", "back", 0.05),),
           subject="right arm", moves="",
           why="the direction a shoulder allows least — a joint limit should engage, not fail, so this asks only that the arm went BACKWARDS. Too small a move for a judge to call reliably"),
    Phrase("leg-forward", "have her lift her left knee up in front of her",
           touches=("leftUpperLeg",),
           geometry=(("points", "leftUpperLeg", "forward"),
                     ("moved", "leftFoot", "forward", 0.10), ("moved", "leftFoot", "up", 0.05)),
           subject="left leg", moves="",
           why="a hip flexes forward; the rig where this came out backwards is the reason aim exists. A lifted knee goes up AND forward, which the movement vocabulary cannot say in one word"),
    Phrase("leg-back", "have her swing her right leg back behind her",
           touches=("rightUpperLeg",),
           geometry=(("moved", "rightFoot", "back", 0.05),),
           subject="right leg", moves="back",
           why="the opposite of leg-forward, which a single wrong sign would pass both of"),
    Phrase("point-ahead", "have her point straight ahead with her right arm",
           touches=("rightUpperArm",),
           geometry=(("points", "rightUpperArm", "forward"),),
           subject="right arm", moves="forward",
           why="'point' is not in the vocabulary; it has to be read as an aim"),

    # --- bend, on the hinges, where aim is the WRONG answer
    Phrase("elbow", "bend her left elbow",
           touches=("leftLowerArm",), leaves=("leftUpperArm",),
           signs=(("leftLowerArm", "bend", +1),),
           geometry=(("nearer", "leftHand", "leftUpperArm", 0.05),),
           subject="left forearm", moves="forward",
           why="the shoulder must stay put: an adjustment is a bend, not an aim"),
    Phrase("knee", "have her bend her right knee",
           touches=("rightLowerLeg",), leaves=("rightUpperLeg",),
           signs=(("rightLowerLeg", "bend", +1),),
           geometry=(("nearer", "rightFoot", "rightUpperLeg", 0.05),
                     ("moved", "rightFoot", "up", 0.03)),
           subject="right lower leg", moves="back",
           why="a knee folds the heel BACKWARD — the one hinge whose positive bend is not forward"),
    Phrase("both-elbows", "have her bend both elbows",
           touches=("leftLowerArm", "rightLowerArm"),
           signs=(("leftLowerArm", "bend", +1), ("rightLowerArm", "bend", +1)),
           geometry=(("nearer", "leftHand", "leftUpperArm", 0.05),
                     ("nearer", "rightHand", "rightUpperArm", 0.05)),
           subject="forearms", moves="forward",
           why="bend is not mirrored the way spread is; the same sign must work on both sides"),

    # --- the trunk, where there is no aim and the sign is the whole claim
    Phrase("look-left", "have her look to her own left",
           touches=("head",), signs=(("head", "turn", +1),),
           subject="head", moves="",
           why="a head twist is invisible to geometry AND to a camera that only sees translation — the sign IS the test"),
    Phrase("look-right", "have her turn her head to her right",
           touches=("head",), signs=(("head", "turn", -1),),
           subject="head", moves="",
           why="its mirror; one wrong sign would otherwise pass look-left and fail nothing"),
    Phrase("look-up", "have her tilt her head back to look up at the ceiling",
           touches=("head",), signs=(("head", "bend", -1),),
           subject="head", moves="",
           why="bend is forward-positive, so looking up is NEGATIVE — the sign most often guessed"),
    Phrase("look-down", "have her look down at the floor",
           touches=("head",), signs=(("head", "bend", +1),),
           subject="head", moves="",
           why="the easy half of the pair, and worthless without look-up beside it"),
    Phrase("bow", "have her bow forward from the waist",
           touches=("spine",), signs=(("spine", "bend", +1),),
           subject="upper body", moves="",
           why="the spine has no aim, and a bow is the phrase most likely to reach for one. Scored on "
               "the CALL only: Trish's spine bones are siblings under one parent rather than a chain, "
               "so bending the mapped spine deforms her waist without tipping her shoulders — a rig "
               "defect (docs/backlogs/figures.md) that a wrong red cell here would only obscure"),

    # --- interference and side discrimination
    Phrase("left-only", "raise only her left arm, leave the right one alone",
           touches=("leftUpperArm",), leaves=("rightUpperArm",),
           geometry=(("points", "leftUpperArm", "up"),),
           subject="left arm", moves="up",
           why="an explicit instruction not to touch the other side; bones unmentioned stay put"),
    Phrase("wave", "have her wave hello",
           touches=("rightUpperArm",),
           geometry=(("above", "rightHand", "rightUpperArm"),),
           subject="right arm", moves="up",
           why="a whole gesture rather than a joint — the first request that is not a literal axis"),
)


# ---------------------------------------------------------------- scoring the call
#
# `pose_figure` takes `{bone: {axis: value}}`, so what a director chose is readable without any model.


def _requested(pose: dict, bone: str) -> Optional[dict]:
    r = (pose or {}).get(bone)
    return r if isinstance(r, dict) else None


def check_call(phrase: Phrase, pose: dict) -> list[str]:
    """Everything wrong with the CALL, in plain words. Empty means it passed.

    An `aim` satisfies a `signs` expectation vacuously: aiming names a destination and the sign of the
    equivalent rotation is not the director's to get wrong. Only an explicit bend/spread/turn is judged
    on its sign — otherwise this check would punish the very form the description recommends.
    """
    fails: list[str] = []
    if not isinstance(pose, dict) or not pose:
        return ["no pose was called at all"]
    for bone in phrase.touches:
        if _requested(pose, bone) is None:
            fails.append(f"never touched {bone}")
    for bone in phrase.leaves:
        if _requested(pose, bone) is not None:
            fails.append(f"also moved {bone}, which should have been left alone")
    for bone, axis, sign in phrase.signs:
        req = _requested(pose, bone)
        if req is None or req.get("aim") is not None:
            continue                       # missing is already reported by `touches`; an aim is exempt
        value = req.get(axis)
        if not isinstance(value, (int, float)) or value == 0:
            fails.append(f"{bone} got no {axis}")
        elif (value > 0) != (sign > 0):
            fails.append(f"{bone} {axis}={value:g}, wanted {'positive' if sign > 0 else 'negative'}")
    return fails


# ---------------------------------------------------------------- scoring the geometry
#
# Predicates over joint positions, read in the BODY's own frame rather than the world's — a rig that
# faces -Z and one that faces +Z must score the same, and that is the whole reason `body_frame` exists.
# `out` is side-aware, exactly as it is in the aim vocabulary: it means away from the midline, so a
# symmetric expectation is written once and not once per side.


#: Each predicate and how many of its arguments are BONE names — the rest is a direction or a distance.
#: One table, read by both the checker and `bones_used`, because a predicate the corpus can write and
#: the checker cannot read is a cell that passes by never being looked at.
PREDICATES = {"points": 1, "moved": 1, "above": 2, "below": 2, "apart": 2, "nearer": 2}

#: How far off a requested direction a limb may land and still count — the cosine, so 0.8 is about 37
#: degrees. Loose enough for a director that aims [0, 1, 0.4] instead of straight up, tight enough that a
#: limb pointing anywhere else fails. One number for every `points` predicate: a per-phrase tolerance
#: would be a knob to turn until the corpus went green, which is the opposite of what it is for.
AIMED = 0.8


def _components(frame: dict, bone: str, v) -> dict[str, float]:
    """A world-space displacement, resolved into the body's own up/forward/out."""
    up, forward, left = frame["up"], frame["forward"], frame["left"]
    outward = left if bone.startswith("left") else [-c for c in left]
    dot = lambda a, b: sum(x * y for x, y in zip(a, b))  # noqa: E731 — local, and reads better inline
    return {"up": dot(v, up), "down": -dot(v, up), "forward": dot(v, forward), "back": -dot(v, forward),
            "out": dot(v, outward), "in": -dot(v, outward)}


def check_geometry(phrase: Phrase, before: dict, after: dict, frame: dict, height: float,
                   dirs: Optional[dict] = None) -> list[str]:
    """Everything the posed skeleton disagrees with. `before`/`after` are `{bone: (x, y, z)}` in world
    space; `dirs` is `{bone: unit direction}` AFTER posing, which is what an absolute `points` claim is
    made about. Distances in the corpus are fractions of `height` and are resolved against it here.

    **`points` for an aim, `moved` for an adjustment.** That distinction is not bookkeeping: Saka rests
    in a T-pose, so "hold both arms out to the sides" correctly moves her arms not at all, and a corpus
    that demanded displacement scored a right answer as a failure. An absolute request has to be scored
    on where the limb ENDED UP, which is the same reason `aim` exists at all.

    A predicate naming a bone this rig does not have is skipped, not failed — Saka has toes and the Daz
    conversions do not, and a corpus that could only use the intersection would test less on every rig
    to test the same on all of them.
    """
    return check_predicates(phrase.geometry, before, after, frame, height, dirs, phrase.id)


def check_predicates(predicates, before: dict, after: dict, frame: dict, height: float,
                     dirs: Optional[dict] = None, label: str = "") -> list[str]:
    """The predicate evaluator, split out from `check_geometry` so a named pose can be scored by the
    same vocabulary that scores an utterance.

    That is not tidiness. A named pose is only worth having if something can say whether a rig actually
    performed it, and the honest way to say so is the way the corpus already does: assert where the
    joints landed. It makes the pose library and the eval corpus the same kind of object — poses ship
    with the assertions that define them.
    """
    fails: list[str] = []
    dirs = dirs or {}
    sub = lambda a, b: [x - y for x, y in zip(a, b)]     # noqa: E731
    dist = lambda a, b: math.dist(a, b)                  # noqa: E731
    for pred in predicates:
        kind, args = pred[0], pred[1:]
        if kind not in PREDICATES:
            # Checked BEFORE the missing-bone skip below, or a typo would read as "nothing to check"
            # and the cell would go green for the rest of its life.
            raise ValueError(f"unknown geometry predicate {kind!r} in {label or 'a signature'}")
        names = args[:PREDICATES[kind]]
        if any(n not in before or n not in after for n in names):
            continue                                     # this rig cannot answer; not a failure
        if kind == "points":
            bone, way = args[0], args[1]
            if bone not in dirs:
                continue
            got = _components(frame, bone, dirs[bone])[way]
            if got < AIMED:
                landed = max(_components(frame, bone, dirs[bone]).items(), key=lambda kv: kv[1])[0]
                fails.append(f"{bone} points {landed} (cos {got:+.2f} to {way}), wanted {way}")
        elif kind == "moved":
            bone, way, want = args
            got = _components(frame, bone, sub(after[bone], before[bone]))[way]
            if got < want * height:
                fails.append(f"{bone} moved {got / height:+.2f}h {way}, wanted at least {want:.2f}h")
        elif kind in ("above", "below"):
            a, b = args
            gap = _components(frame, a, sub(after[a], after[b]))["up"]
            if (gap <= 0) if kind == "above" else (gap >= 0):
                fails.append(f"{a} is {abs(gap) / height:.2f}h "
                             f"{'below' if gap <= 0 else 'above'} {b}, wanted {kind}")
        elif kind == "apart":
            a, b, want = args
            grew = (dist(after[a], after[b]) - dist(before[a], before[b])) / height
            if grew < want:
                fails.append(f"{a} and {b} separated by {grew:+.2f}h, wanted at least {want:.2f}h")
        elif kind == "nearer":
            a, b, want = args
            closed = (dist(before[a], before[b]) - dist(after[a], after[b])) / height
            if closed < want:
                fails.append(f"{a} closed on {b} by {closed:+.2f}h, wanted at least {want:.2f}h")
    return fails


def bones_used(phrase: Phrase) -> set[str]:
    """Every bone the phrase's expectations name — what a rig must have for the phrase to be scorable."""
    used = set(phrase.touches) | set(phrase.leaves) | {b for b, _, _ in phrase.signs}
    for pred in phrase.geometry:
        used |= set(pred[1:1 + PREDICATES[pred[0]]])
    return used


def by_id(*ids: str) -> tuple[Phrase, ...]:
    """The named phrases, in corpus order. Raises on a typo rather than silently running fewer."""
    known = {p.id: p for p in CORPUS}
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise KeyError(f"no such phrase(s): {', '.join(unknown)}")
    return tuple(p for p in CORPUS if not ids or p.id in ids)
