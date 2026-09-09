#!/usr/bin/env python3
"""The pose eval harness — does the tool DESCRIPTION still steer a director correctly?

    python scripts/pose_eval.py --calls-only            # ~20 phrases x 3 rigs, no Blender, no judge
    python scripts/pose_eval.py                         # + geometry, renders and the visual judge
    python scripts/pose_eval.py --phrases arm-up,knee --rigs Saka --keep out/
    python scripts/pose_eval.py --llm Gemini --judge claude

Everything below `pose_figure` has tests. `pose_figure`'s own description does not, and it is now the
layer most likely to be wrong: the arms bug of 2026-09-03 was correct geometry under wrong English, and
every structural check passed while it shipped. **A change to a sentence of English is otherwise
unfalsifiable.** This is the thing that fails when one stops working.

What runs, per cell (one phrase x one rig):

1. A real director LLM is handed the REAL `inspect_figure` / `pose_figure` schemas — read live off the
   MCP server, never transcribed — and the phrase as a user turn. Its tool calls are recorded and
   answered, never executed: no world, no server, no headset.
2. The recorded pose is validated by `figures.clean_pose` (the endpoint's own validator) and applied to
   the rig's bind pose, so the reply the model reads — including which joints hit a limit — is the one
   the runtime would have sent.
3. `pose_corpus.check_call` scores the call, `check_geometry` scores where the joints landed.
4. Blender renders the pose and the rest pose from a shared camera, in clay, and the judge answers two
   multiple-choice questions about the pair.

The three layers fail differently and that is the point: a CALL failure is the tool description's
fault, a GEOMETRY failure with a clean call means the words and the frame disagree, and a JUDGE failure
against clean geometry means the numbers are right and the figure still does not look like a person.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conjure import pose_corpus                                          # noqa: E402
from conjure.config import get_settings                                  # noqa: E402
from conjure.figures import (apply_pose, best_humanoid, body_frame,      # noqa: E402
                             bone_directions, clean_pose, figure_description,
                             node_world_positions, resolve_pose, split_glb)
from conjure.importer import glb_bounds, vrm_humanoid                    # noqa: E402
from conjure.judge import build_judge                                    # noqa: E402
from conjure.llm import ToolSpec, build_roster                           # noqa: E402
from conjure.pose_corpus import (CAST, IMPOSSIBLE_BONE, IMPOSSIBLE_TWIST,  # noqa: E402
                                 MOVE_QUESTION, MOVES, PLAUSIBLE_BAD, PLAUSIBLE_OPTIONS,
                                 PLAUSIBLE_QUESTION, Phrase, Rig,
                                 check_call, check_geometry)  # noqa: F401 — Rig types `load`

BLENDER = os.environ.get("BLENDER", "/Applications/Blender.app/Contents/MacOS/Blender")
POSE_TEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pose_test.py")

#: The only tools the harness offers. `inspect_figure` is here because a director calls it first and the
#: bone list it returns is part of what steers the pose — measuring `pose_figure` without it would be
#: measuring a surface nobody uses.
TOOLS = ("inspect_figure", "pose_figure")

#: The figure's id in the harness's imaginary world. A director needs something to name, and the real
#: room description would bring a hundred irrelevant variables with it.
FIGURE_ID = "woman"

SYSTEM = ("You are the director of a Conjure session. Use the tools to carry out requests.\n"
          f"There is one thing in the world: a life-size human figure with the id {FIGURE_ID!r}.")


# ---------------------------------------------------------------- the rig under test


@dataclass
class Subject:
    """One loaded rig: its bind pose, its bone map, and what `inspect_figure` says about it."""

    rig: Rig
    doc: dict
    blob: bytes
    mapping: dict
    axes: dict
    height: float
    description: str

    def joints(self, pose: Optional[dict] = None):
        """`({bone: world position}, {bone: unit direction})` — at rest, or after `pose` is applied to a
        fresh copy of the bind pose. Always from the bind pose, never cumulatively: the axes are a
        property of the bind pose, and re-deriving them from an already-posed skeleton is how a second
        request goes wrong.

        Directions come along because an ABSOLUTE claim is about them: "the arm points up" is true or
        false regardless of where the arm started, which is the whole reason `aim` exists."""
        doc = copy.deepcopy(self.doc)
        if pose:
            apply_pose(doc, self.mapping, pose)
        by_name = {n.get("name"): i for i, n in enumerate(doc.get("nodes") or []) if n.get("name")}
        positions = node_world_positions(doc)
        return ({b: positions[by_name[n]] for b, n in self.mapping.items() if n in by_name},
                bone_directions(doc, self.mapping))


def load(rig: Rig) -> Optional[Subject]:
    """Read a rig the way the importer does — same discovery order, so the map under test is the map a
    headset would be sent. None (with a note) if the file is absent or has no usable map."""
    if not os.path.exists(rig.path):
        print(f"  ! {rig.name}: {rig.path} not found — skipped")
        return None
    raw = open(rig.path, "rb").read()
    doc, blob = split_glb(raw)
    if not doc:
        print(f"  ! {rig.name}: not a glTF file — skipped")
        return None
    stated = vrm_humanoid(doc)
    mapping = stated or (best_humanoid(doc, blob)[0] or {})
    if not mapping:
        print(f"  ! {rig.name}: no humanoid bone map — skipped")
        return None
    from conjure.figures import anatomical_axes
    axes = anatomical_axes(doc, mapping)
    # The importer's own bounds, which is where `inspect_figure`'s "1.70 m tall" comes from — so the
    # height the corpus scales its distances by is the height the director was told. Measuring the node
    # tree instead gave Trish 2.04 m, because her hair rig has bones half a metre above her head.
    bounds = glb_bounds(doc, blob)
    height = (bounds[1][1] - bounds[0][1]) if bounds else 1.7
    return Subject(rig, doc, blob, mapping, axes, height,
                   figure_description(label=rig.name, height_m=height, tris=None, bones=axes,
                                      has_map=True))


# ---------------------------------------------------------------- asking a director


async def tool_specs(settings) -> list[ToolSpec]:
    """The live schemas for TOOLS, read off the MCP server over stdio exactly as the director reads
    them. Transcribing them into this file would defeat the entire harness on the first edit."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    from conjure.agents import load_agent, load_server_registry
    from conjure.config import DEFAULT_USER
    from conjure.director import _stdio_params

    registry = load_server_registry()
    agentdef = load_agent("builder", registry=registry)
    ref = next(r for r in agentdef.servers if r.server in registry)
    params = _stdio_params(registry[ref.server], settings, "builder", DEFAULT_USER,
                           tools=list(TOOLS), access=ref.access)
    with open(os.devnull, "w") as errlog:
        async with stdio_client(params, errlog=errlog) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                live = (await session.list_tools()).tools
                specs = [ToolSpec(t.name, t.description or "", t.inputSchema)
                         for t in live if t.name in TOOLS]
    missing = set(TOOLS) - {s.name for s in specs}
    if missing:
        raise RuntimeError(f"the MCP server no longer offers {', '.join(sorted(missing))}")
    return specs


@dataclass
class Attempt:
    """What one director turn did: the accumulated pose, and everything it was told along the way."""

    pose: dict = field(default_factory=dict)
    calls: list = field(default_factory=list)             # (tool, args, reply)
    reply: str = ""

    @property
    def inspected(self) -> bool:
        return any(t == "inspect_figure" for t, _, _ in self.calls)


async def ask(llm, specs: list[ToolSpec], subject: Subject, phrase: Phrase) -> Attempt:
    """Run one real director turn against `phrase`, answering its tool calls from the rig file.

    Nothing is executed. `pose_figure` is answered the way the endpoint would answer it — the same
    validator, the same merge-per-bone semantics, and the same joint-limit report — because a director
    that is told "your elbow request hit a limit" behaves differently from one that is told "Moved
    leftLowerArm", and which of those it hears is part of what is being measured.
    """
    out = Attempt()

    async def execute(name: str, args: dict) -> str:
        reply = _answer(name, args, subject, out)
        out.calls.append((name, args, reply))
        return reply

    async def emit(text: str, *, final: bool = False) -> None:
        if final:
            out.reply = text

    await llm.run_turn(system=SYSTEM, history=[], user_text=phrase.say, tools=specs,
                       execute_tool=execute, emit=emit)
    return out


def _answer(name: str, args: dict, subject: Subject, out: Attempt) -> str:
    """The reply the real tool would have given, from a file instead of a world."""
    if name == "inspect_figure":
        return subject.description
    if name != "pose_figure":
        return f"error: tool {name!r} is not available to this agent"
    if args.get("clear"):
        out.pose.clear()
        return "Back to a neutral stance."
    pose = args.get("pose")
    if not isinstance(pose, dict) or not pose:
        return "Give me a pose like {\"leftUpperArm\": {\"aim\": \"up\"}}, or clear=true to reset."
    clean, problem = clean_pose(pose, subject.axes)
    if problem:
        return f"Couldn't pose that: {problem}."
    out.pose.update(clean)
    for bone, rot in list(out.pose.items()):
        if not rot:
            del out.pose[bone]                     # a cleared bone leaves no trace, as on the server
    limited: list = []
    resolve_pose(subject.axes, clean, limited)
    moved = f"Moved {', '.join(sorted(clean))}."
    return moved + (" Joint limits applied: " + "; ".join(limited) + "." if limited else "")


# ---------------------------------------------------------------- rendering and judging


def render(subject: Subject, pose: dict, outdir: str, size: int,
           frame: Optional[str] = None) -> Optional[dict[str, str]]:
    """Front and side, in clay. `{view: path}`, or None if Blender is unavailable or fell over.

    Shells out to `scripts/pose_test.py` rather than reimplementing: that script already writes the
    pose into the GLB the way the runtime does and renders the result, and a second renderer here would
    be a second thing that could disagree with the headset.
    """
    if not os.path.exists(BLENDER):
        return None
    os.makedirs(outdir, exist_ok=True)
    cmd = [BLENDER, "--background", "--python", POSE_TEST, "--",
           subject.rig.path, outdir, json.dumps(pose), str(size), "--clay"]
    if frame:
        cmd += ["--frame", frame]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    shots = {"front": os.path.join(outdir, "posed.png"),
             "quarter": os.path.join(outdir, "posed_q.png"),
             "side": os.path.join(outdir, "posed_side.png")}
    if not all(os.path.exists(p) for p in shots.values()):
        print(f"    ! render failed: {(proc.stderr or proc.stdout)[-300:].strip()}")
        return None
    return shots


async def calibrate(judge, subject: Subject, workdir: str, size: int) -> bool:
    """Ask the judge one question whose answer is known: is a figure with its head on backwards a pose a
    person could hold? Returns whether it said no.

    **An instrument is not trusted until it has been pointed at the defect it exists to find.** This one
    was not, and when finally asked (2026-09-05) both candidate models answered "yes" — so the anatomy
    verdict is reported but does not fail a cell until this passes. Costs one render and one call.
    """
    bone = subject.mapping.get(IMPOSSIBLE_BONE)
    if not bone:
        return False
    shots = render(subject, {bone: IMPOSSIBLE_TWIST}, os.path.join(workdir, "_calibration"), size)
    if not shots:
        return False
    verdict = await judge.choose(
        [("the posed figure, from the front", open(shots["front"], "rb").read()),
         ("the same pose seen from her front-left", open(shots["quarter"], "rb").read()),
         ("the same pose from her left side", open(shots["side"], "rb").read())],
        PLAUSIBLE_QUESTION, list(PLAUSIBLE_OPTIONS))
    return verdict.choice == PLAUSIBLE_BAD


async def judge_cell(judge, phrase: Phrase, rest: dict[str, str], posed: dict[str, str],
                     gates: bool) -> dict:
    """Up to two questions about the renders. Both are multiple choice; neither is free-form.

    The plausibility question is asked of EVERY cell and is the judge's real contribution — geometry can
    already say where a joint landed and cannot say whether that is a shoulder or a dislocation. The
    movement question is asked only where the corpus claims a single word is true of the motion on every
    rig (`Phrase.moves`); elsewhere it is silent rather than noisy.

    All three posed views go to the plausibility question. Two are not enough: an arm aimed straight
    forward points at the front camera, foreshortens into what looks like a folded elbow, and was called
    anatomically impossible on exactly that render (2026-09-05) while the side view showed it perfect.
    """
    def png(path: str) -> bytes:
        return open(path, "rb").read()

    fails: list = []
    advisory: list = []
    move = None
    if phrase.moves:
        options = list(MOVES.values())
        want = list(MOVES).index(phrase.moves)
        # Two views each, and MEASURED to be the right number rather than assumed. Handing this
        # question the three-quarter as well — which is what fixed the anatomy question's
        # foreshortening — took judge failures from 2 to 7 across the same 60 cells and broke a `knee`
        # cell that had passed on every rig. More evidence is not always a better answer: six images of
        # two poses is a harder comparison than four, and this question is a comparison.
        move = await judge.choose(
            [("the figure at rest, from the front", png(rest["front"])),
             ("the same figure at rest, from her left side", png(rest["side"])),
             ("the figure after the pose, from the front", png(posed["front"])),
             ("the same pose, from her left side", png(posed["side"]))],
            MOVE_QUESTION.format(subject=phrase.subject), options)
        if not move.answered:
            (fails if gates else advisory).append(
                f"judge would not answer the movement question ({move.raw[:60]!r})")
        elif move.choice != want:
            (fails if gates else advisory).append(
                f"judge saw {options[move.choice]!r}, wanted {options[want]!r}")
    body = await judge.choose(
        [("the posed figure, from the front", png(posed["front"])),
         ("the same pose seen from her front-left", png(posed["quarter"])),
         ("the same pose from her left side", png(posed["side"]))],
        PLAUSIBLE_QUESTION, list(PLAUSIBLE_OPTIONS))
    # Only the explicit "no" is a failure. "Cannot tell from these views" is an ABSTENTION, and scoring
    # it as a defect reports a problem with the pose when the problem was with the question.
    if body.choice == PLAUSIBLE_BAD:
        note = f"judge on anatomy: {PLAUSIBLE_OPTIONS[body.choice]}"
        (fails if gates else advisory).append(note)
    return {"move": move.label if move else "", "anatomy": body.label, "fails": fails,
            "advisory": advisory}


# ---------------------------------------------------------------- the run


@dataclass
class Cell:
    rig: str
    phrase: str
    pose: dict
    call_fails: list
    geom_fails: list
    judge_fails: list
    advisory: list
    inspected: bool
    skipped: str = ""

    @property
    def ok(self) -> bool:
        return not (self.call_fails or self.geom_fails or self.judge_fails) and not self.skipped


async def run(args) -> int:
    settings = get_settings()
    phrases = pose_corpus.by_id(*[p for p in (args.phrases or "").split(",") if p])
    rigs = [r for r in CAST if not args.rigs or r.name.lower() in args.rigs.lower().split(",")]

    roster, active = build_roster(settings)
    name = args.llm or active
    if name not in roster:
        print(f"No director LLM {name!r} — have: {', '.join(roster) or 'none (no API keys set)'}")
        return 2
    llm = roster[name]
    judge = None if (args.calls_only or args.no_judge) else build_judge(settings, args.judge)
    specs = await tool_specs(settings)
    pose_spec = next(s for s in specs if s.name == "pose_figure")

    print(f"\npose eval — {len(phrases)} phrase(s) x {len(rigs)} rig(s), director {name}, "
          f"judge {getattr(judge, 'name', 'none')}")
    print(f"  tool description: {len(pose_spec.description)} chars, live off the MCP server")

    subjects = [s for s in (load(r) for r in rigs) if s]
    if not subjects:
        print("No rigs available — nothing to measure.")
        return 2

    workdir = args.keep or tempfile.mkdtemp(prefix="pose-eval-")
    os.makedirs(workdir, exist_ok=True)
    cells: list[Cell] = []
    started = time.time()

    # Before the battery: does the judge notice a figure with its head on backwards? Reported either
    # way, because it is the single most useful line in the run — an instrument that cannot detect the
    # blatant case cannot be believed about the subtle one.
    if judge and not args.no_render:
        caught = await calibrate(judge, subjects[0], workdir, args.size)
        print(f"  judge calibration: {'PASSED' if caught else 'FAILED'} — asked whether a figure with "
              f"its head on backwards is a pose a person could hold, it said "
              f"{'no (correct)' if caught else 'YES'}")
    if judge and args.judge_gates:
        print("  --judge-gates: judge disagreements will FAIL cells (they are advisory by default, "
              "because across four runs they did not reproduce)")

    for subject in subjects:
        print(f"\n{subject.rig.name} — {subject.rig.note}, {subject.height:.2f} m")
        at_rest, _ = subject.joints()
        frame = body_frame(subject.doc, subject.mapping)
        for phrase in phrases:
            cell = await one(args, llm, specs, judge, subject, phrase, at_rest, frame, workdir)
            cells.append(cell)
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return report(cells, phrases, subjects, time.time() - started, args)


async def one(args, llm, specs, judge, subject: Subject, phrase: Phrase, at_rest, frame,
              workdir: str) -> Cell:
    """One cell, layer by layer, stopping at the first layer that has nothing left to say."""
    try:
        attempt = await ask(llm, specs, subject, phrase)
    except Exception as exc:                       # noqa: BLE001 — one bad cell must not end the run
        print(f"  {phrase.id:<12} ERROR {type(exc).__name__}: {exc}")
        return Cell(subject.rig.name, phrase.id, {}, [f"{type(exc).__name__}: {exc}"], [], [], [], False)

    call_fails = check_call(phrase, attempt.pose)
    geom_fails: list = []
    judge_fails: list = []
    advisory: list = []
    if attempt.pose and not args.calls_only:
        after, dirs = subject.joints(attempt.pose)
        geom_fails = check_geometry(phrase, at_rest, after, frame, subject.height, dirs)
    if attempt.pose and not args.no_render:
        cellwork = os.path.join(workdir, subject.rig.name, phrase.id)
        posed = render(subject, attempt.pose, os.path.join(cellwork, "posed"), args.size)
        # The reference is rendered PER CELL, through the camera the posed shot chose. One shared rest
        # render would be cheaper by half and would compare two differently-zoomed pictures, which is
        # the one thing a "compared to the first image" question cannot survive.
        rest = render(subject, {}, os.path.join(cellwork, "rest"), args.size,
                      frame=os.path.join(cellwork, "posed", "frame.json")) if posed else None
        if judge and posed and rest:
            out = await judge_cell(judge, phrase, rest, posed, args.judge_gates)
            judge_fails, advisory = out["fails"], out["advisory"]

    cell = Cell(subject.rig.name, phrase.id, attempt.pose, call_fails, geom_fails, judge_fails,
                advisory, attempt.inspected)
    mark = "ok " if cell.ok else "FAIL"
    print(f"  {mark} {phrase.id:<12} {json.dumps(cell.pose, separators=(',', ':'))[:88]}")
    for layer, fails in (("call", call_fails), ("geometry", geom_fails), ("judge", judge_fails)):
        for f in fails:
            print(f"       {layer}: {f}")
    for note in advisory:
        print(f"       (advisory) {note}")
    return cell


def report(cells: list[Cell], phrases, subjects, elapsed: float, args) -> int:
    """A table by phrase, because a phrase that fails on one rig and passes on two is a DIFFERENT
    finding from one that fails on all three: the first says the wording rides a rest pose, the second
    says the wording is wrong."""
    rigs = [s.rig.name for s in subjects]
    width = max(len(p.id) for p in phrases)
    print("\n" + "=" * (width + 4 + 9 * len(rigs)))
    print(f"{'phrase':<{width}}  " + "  ".join(f"{r[:8]:<8}" for r in rigs))
    print("-" * (width + 4 + 9 * len(rigs)))
    for phrase in phrases:
        row = []
        for rig in rigs:
            cell = next((c for c in cells if c.rig == rig and c.phrase == phrase.id), None)
            if cell is None:
                row.append("-")
            elif cell.ok:
                row.append("ok")
            else:
                row.append(",".join(k for k, v in (("call", cell.call_fails),
                                                   ("geom", cell.geom_fails),
                                                   ("judge", cell.judge_fails)) if v))
        print(f"{phrase.id:<{width}}  " + "  ".join(f"{c:<8}" for c in row))
    passed = sum(1 for c in cells if c.ok)
    layers = {k: sum(1 for c in cells if getattr(c, k)) for k in
              ("call_fails", "geom_fails", "judge_fails", "advisory")}
    print("-" * (width + 4 + 9 * len(rigs)))
    print(f"{passed}/{len(cells)} cells clean in {elapsed:.0f}s — "
          f"{layers['call_fails']} call, {layers['geom_fails']} geometry, "
          f"{layers['judge_fails']} judge")
    if layers["advisory"]:
        print(f"{layers['advisory']} cell(s) the judge disagreed with, not counted — see --judge-gates")
    inspected = sum(1 for c in cells if c.inspected)
    print(f"{inspected}/{len(cells)} turns called inspect_figure first")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump([c.__dict__ for c in cells], fh, indent=1)
        print(f"wrote {args.json}")
    return 0 if passed == len(cells) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--phrases", default="", help="comma-separated phrase ids (default: all)")
    ap.add_argument("--rigs", default="", help="comma-separated rig names (default: the whole cast)")
    ap.add_argument("--llm", default="", help="director to steer, e.g. Claude / Gemini (default: active)")
    ap.add_argument("--judge", default="", help="judge backend: gemini | claude | fake | none")
    ap.add_argument("--calls-only", action="store_true",
                    help="score the tool call and stop — reads the rigs, but no Blender and no judge")
    ap.add_argument("--no-render", action="store_true", help="geometry but no pictures")
    ap.add_argument("--no-judge", action="store_true", help="render but ask nothing")
    ap.add_argument("--judge-gates", action="store_true",
                    help="let the judge FAIL cells. Off by default: its verdicts did not reproduce "
                         "across runs, and it passes a figure with its head on backwards")
    ap.add_argument("--size", type=int, default=640, help="render resolution (default 640)")
    ap.add_argument("--keep", default="", help="keep renders in this directory")
    ap.add_argument("--json", default="", help="write the per-cell results here")
    args = ap.parse_args()
    if args.calls_only:
        args.no_render = args.no_judge = True
    return asyncio.run(run(args))


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(main())
