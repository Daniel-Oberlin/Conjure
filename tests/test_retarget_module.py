"""`conjure.retarget` — rewriting a clip so it drives a rig it was not authored for.

The arithmetic is pinned in `test_retarget.py`, against the probe that discovered it. These are about
the part that ships: real GLB bytes in, a real clip GLB out, and the refusals.

Built on the mixamo/rigify pair in `test_server.py`, which exists for exactly this — one skeleton,
identical geometry, two spellings, two rig signatures. Binding by name resolves NOTHING across them,
which is the whole problem tier 2 solves.

Every case turns on one property: **a clip retargeted onto its own rig must come back unchanged.** It is
the only cheap test this has, and it caught two bugs that had looked like facts about rigs — posing the
two sides with different bone sets, and measuring a bone against its parent's REST rather than against
where the parent had actually moved to.
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_server import _MIXAMO, _RIGIFY, _glb_bytes, _skeleton_nodes   # noqa: E402

from conjure.figures import (best_humanoid, node_world_matrices,        # noqa: E402
                             split_glb)
from conjure.retarget import (Rig, _read_accessor, _sample, _tracks,   # noqa: E402
                              qconj, qmul, retarget_clip)


def _roll(nodes: list, degrees: float) -> list:
    """Roll every bone's REST and leave the geometry alone.

    Pre-multiplying each child's local by the inverse makes every world transform telescope back to
    what it was, so the rig is geometrically identical and differs only in what a local rotation MEANS.
    A clip's locals are then wrong on it by a conjugation — which is the thing a retarget has to undo,
    and the only control here whose right answer is known exactly.
    """
    a = math.radians(degrees) / 2
    x = (math.sin(a) * 0.5774, math.sin(a) * 0.5774, math.sin(a) * 0.5774, math.cos(a))
    xi = qconj(x)
    m = _mat3(xi)
    for nd in nodes:
        nd["rotation"] = list(qmul(tuple(nd.get("rotation") or (0, 0, 0, 1)), x))
    for nd in nodes:
        for c in nd.get("children") or []:
            ch = nodes[c]
            ch["rotation"] = list(qmul(xi, tuple(ch["rotation"])))
            t = ch.get("translation") or [0, 0, 0]
            ch["translation"] = [m[0] * t[0] + m[3] * t[1] + m[6] * t[2],
                                 m[1] * t[0] + m[4] * t[1] + m[7] * t[2],
                                 m[2] * t[0] + m[5] * t[1] + m[8] * t[2]]
    return nodes


#: How each real convention spells a finger joint, and the only place these strings are written down
#: in the tests — `figures.CONVENTIONS` is the thing under test, so a fixture that imported its table
#: would agree with itself no matter what either said. Taken off the files: `CC_Base_L_Mid1`,
#: `f_index.01.L`, `LeftHandPinky1`.
_FINGER_NAMES = {
    "mixamo": lambda s, f, n: f"{'Left' if s == 'left' else 'Right'}Hand"
                              f"{'Pinky' if f == 'Little' else f}{n}",
    "rigify-fk": lambda s, f, n: (f"thumb.0{n}.{'L' if s == 'left' else 'R'}" if f == "Thumb" else
                                  f"f_{('pinky' if f == 'Little' else f.lower())}.0"
                                  f"{n}.{'L' if s == 'left' else 'R'}"),
}

#: Where each finger sits on the palm, in the hand's own local frame: out along +x, spread along z.
#: The thumb leaves the palm PLANE, which is what makes it need a different reference axis from the
#: other four and is the whole point of the fixture.
_FINGER_LAYOUT = {"Index": (0.03, 0.0, 0.03), "Middle": (0.03, 0.0, 0.01),
                  "Ring": (0.03, 0.0, -0.01), "Little": (0.03, 0.0, -0.03),
                  "Thumb": (0.02, -0.02, 0.04)}


def _add_fingers(nodes: list, naming: dict, scheme: str) -> list:
    """Five three-joint fingers on each hand, spelled the way `scheme` really spells them."""
    spell = _FINGER_NAMES[scheme]
    by = {n["name"]: i for i, n in enumerate(nodes)}
    for side, slot in (("left", "l_hand"), ("right", "r_hand")):
        hand = by.get(naming[slot])
        if hand is None:
            continue
        flip = 1.0 if side == "left" else -1.0
        for finger, (dx, dy, dz) in _FINGER_LAYOUT.items():
            parent = hand
            for joint in range(1, 4):
                step = (dx * flip, dy, dz) if joint == 1 else (0.03 * flip, dy / 2, 0.0)
                nodes.append({"name": spell(side, finger, joint), "translation": list(step)})
                nodes[parent].setdefault("children", []).append(len(nodes) - 1)
                parent = len(nodes) - 1
    return nodes


def _lean(nodes: list, naming: dict, degrees: float) -> list:
    """Tip the SPINE's rest so the body leans, legs and all else untouched.

    Not a synthetic case: every rigged figure in the catalog leans, because `hips -> neck` is the chord
    of a curved spine and not an axis. Measured, Akari 0.1°, office-babe 2.9°, Alice 4.5°, Grace 6.7°,
    and the clip file Alice's `LayTableIdle` ships in, 10.7°. The gap between two of those was the whole
    of the tilt reported on device, because the law used to align the two rest body frames and so added
    the lean a second time on top of the one the absolute carry already reproduces.
    """
    i = next(k for k, n in enumerate(nodes) if n["name"] == naming["spine"])
    a = math.radians(degrees) / 2
    nodes[i]["rotation"] = list(qmul(tuple(nodes[i].get("rotation") or (0, 0, 0, 1)),
                                     (math.sin(a), 0.0, 0.0, math.cos(a))))
    return nodes


def _mat3(q):
    x, y, z, w = q
    return [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
            2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
            2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]


def figure(naming=None, *, roll: float = 0.0, lean: float = 0.0, fingers: str = "",
           nameless: bool = False, armature_scale: float = 1.0) -> bytes:
    """A rigged model: skeleton, one skinned mesh, a bind pose.

    `armature_scale` wraps the skeleton in a scaled node, which is how the captured rigs carry their
    units — Alice's armature is 0.01, baking centimetres into every local translation beneath it.
    """
    nodes, names = _skeleton_nodes(naming or _MIXAMO)
    if roll:
        _roll(nodes, roll)
    if lean:
        _lean(nodes, naming or _MIXAMO, lean)
    if fingers:
        _add_fingers(nodes, naming or _MIXAMO, fingers)
    if armature_scale != 1.0:
        for nd in nodes:
            if nd.get("translation"):
                nd["translation"] = [c / armature_scale for c in nd["translation"]]
    if nameless:
        for i, nd in enumerate(nodes):
            nd["name"] = f"b{i}"
    nodes.append({"name": "Body", "mesh": 0, "skin": 0})
    roots = [0, len(nodes) - 1]
    if armature_scale != 1.0:
        nodes.append({"name": "Armature", "scale": [armature_scale] * 3, "children": roots})
        roots = [len(nodes) - 1]
    doc = {"scenes": [{"nodes": roots}], "scene": 0, "nodes": nodes,
           "skins": [{"joints": list(range(len(names)))}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"min": [-0.6, -0.013, -0.17], "max": [0.6, 1.744, 0.23]}]}
    return _glb_bytes(doc)


def clip(naming=None, *, name="wave", roll: float = 0.0, lean: float = 0.0, fingers: str = "",
         drive=("l_arm", "spine", "l_fore"),
         extra=(), slide=(), slide_moves: bool = True, wrapper: str = "",
         slide_via_wrapper: bool = False) -> bytes:
    """A clip: the authoring rig's nodes, real motion, and no mesh — the shape the corpus ships."""
    table = naming or _MIXAMO
    nodes, _names = _skeleton_nodes(table)
    if roll:
        _roll(nodes, roll)
    if lean:
        _lean(nodes, table, lean)
    if fingers:
        _add_fingers(nodes, table, fingers)
    for bone in extra:                          # a bone no other rig has, e.g. a skirt chain
        nodes.append({"name": bone, "translation": [0.0, 0.1, 0.0], "rotation": [0, 0, 0, 1]})
        nodes[0].setdefault("children", []).append(len(nodes) - 1)
    root = 0
    if slide_via_wrapper:                       # the slide sits on an UNMAPPED node ABOVE the skeleton
        wrapper = wrapper or "Armature"
    if wrapper:                                 # an UNMAPPED node ABOVE the skeleton, e.g. an armature
        nodes.append({"name": wrapper, "translation": [0.0, 0.0, 0.0],
                      "rotation": [0, 0, 0, 1], "children": [0]})
        root = len(nodes) - 1
    by = {n["name"]: i for i, n in enumerate(nodes)}
    times = [0.0, 0.5, 1.0]

    blob, views, accessors = bytearray(), [], []

    def add(vals, kind, count, scalar=False) -> int:
        data = struct.pack("<" + "f" * len(vals), *vals)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob.extend(data)
        acc = {"bufferView": len(views) - 1, "componentType": 5126, "count": count, "type": kind}
        if scalar:
            acc["min"], acc["max"] = [min(vals)], [max(vals)]
        accessors.append(acc)
        return len(accessors) - 1

    t_acc = add(times, "SCALAR", len(times), scalar=True)
    channels, samplers = [], []
    for k, slot in enumerate(tuple(drive) + tuple(extra)):
        node_name = table.get(slot, slot)
        if node_name not in by:
            continue
        quats = []
        for i in range(len(times)):
            a = math.radians(18 + 14 * i + 9 * k) / 2
            quats += [0.0, math.sin(a) * 0.3, math.sin(a), math.cos(a)]
        samplers.append({"input": t_acc, "interpolation": "STEP",
                         "output": add(quats, "VEC4", len(times))})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": by[node_name], "path": "rotation"}})
    for slot in slide:                          # a bone the clip MOVES, not merely rotates
        node_name = wrapper if slide_via_wrapper else table.get(slot, slot)
        if node_name not in by:
            continue
        rest = nodes[by[node_name]].get("translation") or [0.0, 0.0, 0.0]
        xyz = []
        for i in range(len(times)):
            step = (0.04 * i) if slide_moves else 0.0
            xyz += [rest[0] + step, rest[1], rest[2]]
        samplers.append({"input": t_acc, "interpolation": "STEP",
                         "output": add(xyz, "VEC3", len(times))})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": by[node_name], "path": "translation"}})
    doc = {"scenes": [{"nodes": [root]}], "scene": 0, "nodes": nodes,
           "buffers": [{"byteLength": len(blob)}], "bufferViews": views, "accessors": accessors,
           "animations": [{"name": name, "channels": channels, "samplers": samplers}]}
    return _glb_bytes(doc, bytes(blob))


def _angle(a, b) -> float:
    return math.degrees(2 * math.acos(max(-1.0, min(1.0, abs(qmul(b, qconj(a))[3])))))


def _locals(data: bytes) -> tuple[dict, list]:
    doc, blob = split_glb(data)
    return _tracks(doc, blob, doc["animations"][0])


def _body_up(fig_bytes: bytes, clip_bytes: bytes, t: float = 0.0) -> tuple:
    """Where `hips -> neck` POINTS once this clip has posed this skeleton — a unit vector in world.

    The physical question, and the one a person in a headset is answering when they say a figure is
    tilted back. It cannot be asked of rotations: `clip_diff.mjs` reports every angle relative to each
    figure's own `t=0`, so a constant lean is invisible to it, and the probe's body metric is an angle
    between two POSED body frames, which the term this is here to pin was constructed to null.
    """
    doc, blob = split_glb(fig_bytes)
    rig = Rig(doc, blob)
    c_doc, c_blob = split_glb(clip_bytes)
    tracks, _times = _tracks(c_doc, c_blob, c_doc["animations"][0])
    by = {n.get("name"): i for i, n in enumerate(doc["nodes"])}
    for name, track in tracks.items():          # the clip REPLACES a node's rotation, so assign it
        if name in by:
            doc["nodes"][by[name]]["rotation"] = list(_sample(track, t))
    world = node_world_matrices(doc)
    p = {b: world[i] for b in ("hips", "neck") if (i := rig.index(b)) is not None}
    d = tuple(p["neck"][12 + k] - p["hips"][12 + k] for k in range(3))
    n = math.sqrt(sum(c * c for c in d))
    return tuple(c / n for c in d)


# ---------------------------------------------------------------- fingers

def test_a_clip_that_drives_FINGERS_carries_them_across_rigs():
    """*"Neither Grace nor Akari's fingers move but Alice's does"*, reported on device.

    Correct, and it was the vocabulary rather than a defect: the humanoid named 22 bones, fingers were
    not among them, and all 82 of their channels were dropped. Alice kept hers only because she plays
    natively. Thirty more bones — five fingers, three joints, two hands — and they cross.
    """
    moving = clip(_MIXAMO, fingers="mixamo",
                  drive=("l_arm", "spine", "LeftHandIndex1", "LeftHandThumb2",
                         "RightHandPinky3"))   # `Pinky` in mixamo, `Little` in ours
    out = retarget_clip(moving, figure(_RIGIFY, fingers="rigify-fk"))
    assert out is not None
    after, _times = _locals(out.data)
    assert "f_index.01.L" in after, f"the index finger did not cross: {sorted(after)}"
    assert "thumb.02.L" in after, "the thumb did not cross"
    assert "f_pinky.03.R" in after, "the little finger did not cross"
    assert out.dropped == 0, f"nothing should have been dropped, {out.dropped} was"


def test_a_finger_is_squared_up_against_the_HAND_and_not_against_the_BODY():
    """The measurement that decided `_hand_refs`, as a property.

    A hand turns freely at the wrist, so no fixed BODY axis stays perpendicular to a finger — measured
    across the catalog, the best body axis still reads 0.966 against `Characters Shaun`'s index and
    0.996 against `Bride`'s thumb, either of which is a degenerate frame. Here the wrist is turned so
    the fingers point straight along body FORWARD, which is what the old rule would have used, and the
    frames must still come out orthonormal.
    """
    nodes, _names = _skeleton_nodes(_MIXAMO)
    _add_fingers(nodes, _MIXAMO, "mixamo")
    by = {n["name"]: i for i, n in enumerate(nodes)}
    a = math.radians(90) / 2                     # roll the hand a quarter turn about the arm
    nodes[by["LeftHand"]]["rotation"] = [math.sin(a), 0.0, 0.0, math.cos(a)]
    nodes.append({"name": "Body", "mesh": 0, "skin": 0})
    doc = {"scenes": [{"nodes": [0, len(nodes) - 1]}], "scene": 0, "nodes": nodes,
           "skins": [{"joints": list(range(len(nodes) - 1))}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"min": [-0.6, -0.013, -0.17], "max": [0.6, 1.744, 0.23]}]}
    rig = Rig(*split_glb(_glb_bytes(doc)))
    canon = rig.canonical()
    for bone in [b for b in canon if "Index" in b or "Thumb" in b]:
        x, y, z, w = canon[bone]
        assert abs(math.sqrt(x * x + y * y + z * z + w * w) - 1.0) < 1e-6, (
            f"{bone}'s canonical frame is not a rotation — the reference axis was parallel to it")


def test_a_rig_with_only_SOME_fingers_maps_only_those():
    """No convention promises a complete hand, so a partial one must not poison the rest of the map.

    Real cases: `Steve` has no finger bones at all, one Mixamo rig in the catalog carries a thumb and an
    index and nothing else, and `Characters Shaun`'s thumb stops at two joints. A slot that matches
    nothing is simply absent, the same way a missing toe has always been.
    """
    doc, blob = split_glb(figure(_MIXAMO, fingers="mixamo"))
    gone = {n.get("name") for n in doc["nodes"]
            if any(f in (n.get("name") or "") for f in ("Middle", "Ring", "Pinky"))}
    assert gone, "the fixture has to have those fingers before removing them proves anything"
    keep = [i for i, n in enumerate(doc["nodes"]) if n.get("name") not in gone]
    renumber = {old: new for new, old in enumerate(keep)}
    doc["nodes"] = [{**doc["nodes"][i],
                     "children": [renumber[c] for c in (doc["nodes"][i].get("children") or [])
                                  if c in renumber]} for i in keep]
    doc["skins"] = [{"joints": [renumber[j] for j in doc["skins"][0]["joints"] if j in renumber]}]
    doc["scenes"] = [{"nodes": [renumber[n] for n in doc["scenes"][0]["nodes"] if n in renumber]}]

    rig = Rig(doc, blob)
    assert "leftIndexProximal" in rig.mapping and "leftThumbDistal" in rig.mapping
    for absent in ("leftMiddleProximal", "leftRingDistal", "rightLittleProximal"):
        assert absent not in rig.mapping, f"{absent} is not in this rig and must not be mapped"
    assert "leftHand" in rig.mapping, "losing three fingers must not cost the hand"


def test_a_STATED_vrm_map_beats_inference_and_brings_its_fingers():
    """A file that names its own bones is better evidence than any guess we can make about it.

    Left to the caller, this split one figure in two: the importer stored Saka's 54-bone VRM map as
    `humanoid` and then asked `rig_signature` for a fingerprint, which ran discovery again and
    fingerprinted a 21-bone INFERRED map — so her row held a map and a signature describing different
    skeletons. The retarget had it worse, because `Rig` has bytes and no caller: it drove her with 21
    bones while her file names 54, fingers included.
    """
    doc, blob = split_glb(figure(_MIXAMO, fingers="mixamo"))
    stated = {"hips": "Hips", "spine": "Spine", "chest": "Spine1", "neck": "Neck", "head": "Head",
              "leftUpperArm": "LeftArm", "leftLowerArm": "LeftForeArm", "leftHand": "LeftHand",
              "rightUpperArm": "RightArm", "rightLowerArm": "RightForeArm", "rightHand": "RightHand",
              "leftUpperLeg": "LeftUpLeg", "leftLowerLeg": "LeftLeg", "leftFoot": "LeftFoot",
              "rightUpperLeg": "RightUpLeg", "rightLowerLeg": "RightLeg", "rightFoot": "RightFoot",
              "leftIndexProximal": "LeftHandIndex1", "leftIndexIntermediate": "LeftHandIndex2"}
    by = {n.get("name"): i for i, n in enumerate(doc["nodes"])}
    doc["extensions"] = {"VRMC_vrm": {"humanoid": {"humanBones": {
        k: {"node": by[v]} for k, v in stated.items() if v in by}}}}

    mapping, source, _follows = best_humanoid(doc, blob)
    assert source == "vrm", f"the file states its own map and discovery took {source!r} instead"
    assert mapping["leftIndexIntermediate"] == "LeftHandIndex2"
    # And the retarget sees the same thing, which is the half that has no caller to do it for it.
    assert Rig(doc, blob).mapping["leftIndexProximal"] == "LeftHandIndex1"


# ---------------------------------------------------------------- the rest body frame

def test_a_rig_that_RESTS_LEANING_does_not_lean_the_target_a_second_time():
    """Reported on device: *"Akari is tilted back compared to Grace and Alice"*, playing Alice's clip.

    Every captured rig rests leaning a little, because `hips -> neck` is the chord of a curved spine —
    Akari 0.1°, Alice 4.5°, Grace 6.7°, and the clip file itself 10.7°. The law used to carry a
    `swing = Bt · Bs⁻¹` that aligned the two rest body frames, on the reasoning that a figure should
    perform in HER frame. But those rigs lean because their spine BONES lean, and an absolute carry
    reproduces that already; `swing` added it again. Measured end to end, against Alice playing it
    natively at 94.1° from vertical: Grace 97.3°, Akari 105.8°, office-babe 100.2°, Saka 99.5° — an
    11.7° spread. Without the term: 93.5°, 95.6°, 92.7°, 93.8°, a spread of 2.9°.

    So: pose a clip authored on a LEANING rig onto an UPRIGHT one and the two bodies must end up
    pointing the same way. The old law failed this by the difference of the two leans.
    """
    src_lean, dst_lean = 12.0, 0.0
    moving = clip(_MIXAMO, lean=src_lean)
    out = retarget_clip(moving, figure(_RIGIFY, lean=dst_lean))
    assert out is not None

    for t in (0.0, 0.5, 1.0):
        want = _body_up(figure(_MIXAMO, lean=src_lean), moving, t)   # the clip on its OWN rig
        got = _body_up(figure(_RIGIFY, lean=dst_lean), out.data, t)
        off = math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b for a, b in zip(want, got))))))
        assert off < 2.0, (f"t={t}: the retargeted body points {off:.1f}° away from the source's. "
                           f"The two rests differ by {src_lean - dst_lean:.0f}°, which is what an "
                           f"alignment term would add here.")


def test_the_two_rigs_really_do_REST_DIFFERENTLY_or_the_lean_case_proves_nothing():
    """The companion every control needs: a fixture that does not actually differ passes anything."""
    upright = Rig(*split_glb(figure(_RIGIFY)))
    leaning = Rig(*split_glb(figure(_MIXAMO, lean=12.0)))
    off = _angle(upright.body_frame(), leaning.body_frame())
    assert off > 6.0, f"the leaning fixture only leans {off:.1f}°"


# ---------------------------------------------------------------- the identity property

def test_a_clip_retargeted_onto_its_OWN_rig_comes_back_unchanged():
    """The only cheap test this has, and it has earned its keep twice."""
    out = retarget_clip(clip(), figure())
    assert out is not None and out.bones >= 3
    before, _t = _locals(clip())
    after, times = _locals(out.data)
    for node, track in after.items():
        assert node in before, node
        for t in times:
            assert _angle(_sample(before[node], t), _sample(track, t)) < 0.05, node


def test_the_rewritten_clip_targets_the_TARGET_RIGS_node_names():
    """Which is the whole reason this is done on the server. What comes back is an ordinary clip, so
    the client's existing bind-by-name path resolves it and no new code path exists to go wrong."""
    out = retarget_clip(clip(_MIXAMO), figure(_RIGIFY))
    doc, _blob = split_glb(out.data)
    names = {n["name"] for n in doc["nodes"]}
    assert "upper_arm.fk.L" in names, "spelled the way the FIGURE spells it"
    assert "LeftArm" not in names, "and not the way the clip did"
    assert not doc.get("meshes") and not doc.get("skins"), "a clip carries motion and nothing else"


def test_binding_by_NAME_across_these_two_rigs_resolves_nothing():
    """The fixture's reason for existing: identical geometry, two spellings. Without this the tests
    above would pass on a pair of rigs that never needed retargeting."""
    src, _ = _locals(clip(_MIXAMO))
    doc, blob = split_glb(figure(_RIGIFY))
    have = {n.get("name") for n in doc["nodes"]}
    assert not (set(src) & have), "no channel would bind, so a naive copy plays nothing at all"
    assert Rig(*split_glb(clip(_MIXAMO))).mapping, "yet both sides have a humanoid map"


def test_a_rig_whose_REST_IS_ROLLED_is_recovered_exactly():
    """The control whose right answer is known. The geometry is identical and only the spelling of a
    local rotation differs, so a correct retarget reproduces the source's pose to arithmetic noise."""
    out = retarget_clip(clip(), figure(roll=90.0))
    assert out is not None
    rolled_ref = retarget_clip(clip(), figure())          # same clip, unrolled rig
    a, times = _locals(out.data)
    b, _ = _locals(rolled_ref.data)
    # The two rigs are geometrically one, so the two retargets must put the body in the same place —
    # which they can only do by undoing the roll, since the locals themselves must differ.
    differ = any(_angle(_sample(a[n], 0.5), _sample(b[n], 0.5)) > 1.0 for n in a if n in b)
    assert differ, "the locals must differ, or the fixture is not testing anything"


# ---------------------------------------------------------------- what it refuses, and what it says

def test_a_figure_with_no_HUMANOID_MAP_is_refused_rather_than_approximated():
    """A clip bound through a map we could not recover is a figure folded into a knot, and "no" is a
    better answer than that. Tamaki is the real case: rigged, and no map at all."""
    said = []
    assert retarget_clip(clip(), figure(nameless=True), said.append) is None
    assert any("humanoid map" in m for m in said), said


def test_a_file_with_no_ANIMATION_is_refused():
    said = []
    assert retarget_clip(figure(), figure(), said.append) is None
    assert any("no animation" in m for m in said), said


def test_channels_with_no_bone_to_receive_them_are_DROPPED_AND_COUNTED():
    """The humanoid names 22 bones and a captured clip drives 222. The other 200 — skirt, breast and
    secondary chains — have no receiver on any rig, and that is a ceiling on tier 2 rather than a
    defect in it. Counted, so a caller can say so instead of a user discovering it on device."""
    out = retarget_clip(clip(extra=("DEF_Skirt01", "DEF_Skirt02")), figure(_RIGIFY))
    assert out.dropped >= 2
    assert any("no rig has anything to receive them" in n for n in out.notes), out.notes


def test_a_bone_the_clip_SLIDES_carries_its_translation_too():
    """Rotation alone is not the whole of a pose.

    The client's own `retarget()` has always kept moving position tracks, re-basing each onto the
    model's rest so the clip's authored ADDRESS is discarded and its movement is not — and a rewrite
    that emitted rotations only was quietly dropping all of them. Measured on a real clip: the native
    path kept 204 tracks of 312, of which 101 were re-based positions, while the rewrite kept 21 of 21
    and carried no translation at all.
    """
    sliding = clip(drive=("l_arm",), slide=("hips",))
    out = retarget_clip(sliding, figure(_RIGIFY))
    assert out.slid == 1, "the sliding bone is carried"
    doc, _blob = split_glb(out.data)
    paths = {c["target"]["path"] for c in doc["animations"][0]["channels"]}
    assert paths == {"rotation", "translation"}


def test_a_bone_that_merely_RESTATES_its_rest_every_frame_is_not_carried():
    """Most translation channels do not move. Measured on `LayTableIdle`: 104 of them, of which FOUR
    move at all. Carrying the rest would make a retargeted clip the size of the skeleton instead of the
    size of the motion, and would nudge every still bone by whatever the two rigs round differently."""
    still = clip(drive=("l_arm",), slide=("hips",), slide_moves=False)
    out = retarget_clip(still, figure(_RIGIFY))
    assert out.slid == 0
    doc, _blob = split_glb(out.data)
    assert {c["target"]["path"] for c in doc["animations"][0]["channels"]} == {"rotation"}


def test_an_UNMAPPED_ANCESTOR_that_rotates_is_part_of_the_pose_it_cannot_be_carried_into():
    """The bug that survived three rounds of measurement, because the probe made it too.

    A bone the humanoid does not name can still be a mapped bone's ANCESTOR, and then it is part of
    that bone's world orientation whether or not we can carry it onward. Alice's `CC_Base_BoneRoot`
    rotates 36.3° over `LayTableIdle` while `CC_Base_Hip` under it rotates 38.6° the other way — they
    very nearly cancel, and what you see is her SLIDING, not turning.

    Reading the hips' world from the mapped subset alone reported the full 38.6° as real, and the
    retargeted figure swung bodily about her own axis at the cadence of a motion that does not rotate
    her at all. Reported from a headset as "Alice translates, Grace rotates" — which is the whole bug in
    four words.
    """
    # `hips` turns, and the unmapped armature above it turns the other way by the same amount.
    straight = clip(drive=("hips",))
    cancelled = clip(drive=("hips",), wrapper="Armature")
    cd, cb = split_glb(cancelled)
    anim = cd["animations"][0]
    by = {n.get("name"): i for i, n in enumerate(cd["nodes"])}
    hips_node = by[_MIXAMO["hips"]]
    hips_ch = next(c for c in anim["channels"] if c["target"]["node"] == hips_node)
    # Give the wrapper the INVERSE of the hips' own track, so the two compose to nothing in world.
    inv = _invert_sampler(cd, cb, anim["samplers"][hips_ch["sampler"]])
    anim["samplers"].append(inv[0])
    anim["channels"].append({"sampler": len(anim["samplers"]) - 1,
                             "target": {"node": by["Armature"], "path": "rotation"}})
    cancelled = _glb_bytes(cd, inv[1])

    fig = figure(_RIGIFY)
    turned = _locals(retarget_clip(straight, fig).data)[0]
    still = _locals(retarget_clip(cancelled, fig).data)[0]
    tgt = _RIGIFY["hips"]
    swing = max(_angle(_sample(turned[tgt], 0.0), _sample(turned[tgt], t)) for t in (0.5, 1.0))
    none_ = max(_angle(_sample(still[tgt], 0.0), _sample(still[tgt], t)) for t in (0.5, 1.0))
    assert swing > 8.0, "the fixture has to actually turn her, or this proves nothing"
    assert none_ < swing / 3, (
        f"an ancestor turning the other way must cancel it: {none_:.1f}° against {swing:.1f}°")


def _invert_sampler(doc, blob, sampler):
    """A copy of `sampler` with every quaternion conjugated, appended to the buffer."""
    import struct as _s
    from conjure.retarget import _read_accessor
    quats = _read_accessor(doc, blob, sampler["output"])
    flat = [c for q in quats for c in qconj(q)]
    data = _s.pack("<" + "f" * len(flat), *flat)
    blob = bytes(blob) + data
    doc["bufferViews"].append({"buffer": 0, "byteOffset": len(blob) - len(data),
                               "byteLength": len(data)})
    doc["accessors"].append({"bufferView": len(doc["bufferViews"]) - 1, "componentType": 5126,
                             "count": len(quats), "type": "VEC4"})
    doc["buffers"][0]["byteLength"] = len(blob)
    return ({"input": sampler["input"], "interpolation": "STEP",
             "output": len(doc["accessors"]) - 1}, blob)


def test_a_carried_translation_converts_the_armatures_UNITS_not_only_its_axes():
    """The bug that came straight after the last one, and looked nothing like it.

    A translation lives in its parent's coordinates, and those differ in UNITS as well as direction:
    Alice's armature bakes centimetres — a parent world scale of 0.01 — where Grace's is metres at 1.0.
    Turning the delta without rescaling it made a 0.64 cm hip sway into 0.61 m and slid the figure
    across the room, in a rhythm quite unlike the swing it had just stopped doing.

    Scaling by HEIGHT looks like the same correction and is not: both figures are about 1.7 m in world,
    so the ratio was 0.96 and it corrected nothing. The unit difference is in the armature, not the body.
    """
    sliding = clip(drive=("l_arm",), slide=("hips",))
    metres = retarget_clip(sliding, figure(_RIGIFY))
    centimetres = retarget_clip(sliding, figure(_RIGIFY, armature_scale=0.01))

    def span(out):
        doc, blob = split_glb(out.data)
        anim = doc["animations"][0]
        for ch in anim["channels"]:
            if ch["target"]["path"] == "translation":
                v = _read_accessor(doc, blob, anim["samplers"][ch["sampler"]]["output"])
                return max(math.dist(p, v[0]) for p in v)
        return 0.0

    a, b = span(metres), span(centimetres)
    assert a > 1e-6 and b > 1e-6, "both must carry the slide at all"
    # The same WORLD movement on a rig whose locals are 100x smaller is 100x larger in those locals.
    assert 80 < b / a < 120, f"expected ~100x in local units, got {b / a:.1f}x"


def test_a_slide_on_an_UNMAPPED_ANCESTOR_reaches_the_target_through_the_hips():
    """The translation half of the ancestor problem, and it took a third form of the same complaint.

    Alice's `CC_Base_BoneRoot` slides 4 cm and is the hips' PARENT, so on her own rig the hips travel
    4 cm while the body barely turns. The humanoid does not name that bone, so a per-bone carry had
    nowhere to put it — the target has no equivalent root — and the retargeted figure held still.

    The hips' WORLD displacement is measured on the source with every translation applied, and folded
    into the target's hips. Measured after: the retargeted hips tracks the native one to within a
    millimetre over the clip, against nothing at all before.
    """
    above = clip(drive=("l_arm",), slide=("hips",), slide_via_wrapper=True)
    out = retarget_clip(above, figure(_RIGIFY))
    assert out.slid == 1, "an ancestor's slide still has to reach the figure"
    doc, blob = split_glb(out.data)
    anim = doc["animations"][0]
    by = {n.get("name"): i for i, n in enumerate(doc["nodes"])}
    moved = [c for c in anim["channels"] if c["target"]["path"] == "translation"]
    assert len(moved) == 1
    assert doc["nodes"][moved[0]["target"]["node"]]["name"] == _RIGIFY["hips"], \
        "carried by the one bone that can carry it"
    v = _read_accessor(doc, blob, anim["samplers"][moved[0]["sampler"]]["output"])
    assert max(math.dist(p, v[0]) for p in v) > 1e-4


# --------------------------------------------------------------- the face
#
# 519 of 543 captured clips rotate a facial bone — not the handful with facial names, the ordinary body
# performances. `1_idle` spends 9,031 degrees on a face with the eyelids as its top two movers. All of
# it was dropped here, because the carried set is built from the humanoid map and the map has 51 bones,
# none facial: a retargeted figure moved her body with a dead face.
#
# Facial bones are carried by their LOCAL DELTA rather than through the law, and that is forced: the
# law squares a bone up against a frame derived from where it POINTS, and a facial bone frequently
# points nowhere. Every `DEF-jaw` in the corpus is a leaf, and so is every one of Eve Maccaro's lids.

import math                                                            # noqa: E402

from conjure.retarget import (FACE_REST_TOLERANCE, _facial_bones,      # noqa: E402
                              _quat_gap, retarget_clip)


def _about(axis, degrees):
    half = math.radians(degrees) / 2
    s = math.sin(half)
    return [axis[0] * s, axis[1] * s, axis[2] * s, math.cos(half)]


def _with_face(naming, faces: dict, extra=()):
    """A figure GLB carrying facial bones at stated LOCAL rests. `faces` maps name -> rest quaternion."""
    nodes, names = _skeleton_nodes(naming)
    head = names.index(naming["head"])
    kids = nodes[head].setdefault("children", [])
    for bone, rest in faces.items():
        kids.append(len(nodes))
        nodes.append({"name": bone, "translation": [0, 0, 0], "rotation": list(rest)})
    for bone in extra:
        kids.append(len(nodes))
        nodes.append({"name": bone, "translation": [0, 0, 0]})
    nodes.append({"name": "Body", "mesh": 0, "skin": 0})
    doc = {"scenes": [{"nodes": [0, len(nodes) - 1]}], "scene": 0, "nodes": nodes,
           "skins": [{"joints": list(range(len(names)))}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"min": [-0.6, -0.013, -0.17], "max": [0.6, 1.744, 0.23]}]}
    return _glb_bytes(doc)


def _face_clip(naming, faces: dict, swing_degrees=26.0):
    """A clip on `naming` whose facial bones rest at `faces` and rotate `swing_degrees` about X."""
    nodes, names = _skeleton_nodes(naming)
    face_index = {}
    for bone, rest in faces.items():
        face_index[bone] = len(nodes)
        nodes.append({"name": bone, "translation": [0, 0, 0], "rotation": list(rest)})
    # Frame 0 at rest, frame 1 swung — so the delta is exactly `swing_degrees` from this rig's rest.
    swung = {b: _mul(_about((1, 0, 0), swing_degrees), faces[b]) for b in faces}
    blob = struct.pack("<2f", 0.0, 1.0)
    samplers, channels = [], []
    views = [{"buffer": 0, "byteOffset": 0, "byteLength": 8}]
    accessors = [{"bufferView": 0, "componentType": 5126, "count": 2, "type": "SCALAR",
                  "min": [0.0], "max": [1.0]}]

    def add_rotation(node_index, first, second):
        offset = len(blob[:])
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": 32})
        accessors.append({"bufferView": len(views) - 1, "componentType": 5126, "count": 2,
                          "type": "VEC4"})
        samplers.append({"input": 0, "output": len(accessors) - 1, "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1,
                         "target": {"node": node_index, "path": "rotation"}})
        return struct.pack("<8f", *first, *second)

    # The BODY, at identity, because a retarget refuses a clip that drives no humanoid bone at all —
    # and a clip that drives only a face is not the case this feature is for. Every captured clip that
    # carries a face carries a body performance with it.
    for i in range(len(names)):
        blob += add_rotation(i, (0, 0, 0, 1), (0, 0, 0, 1))
    for bone, idx in face_index.items():
        blob += add_rotation(idx, faces[bone], swung[bone])
    doc = {"scenes": [{"nodes": [0]}], "scene": 0, "nodes": nodes,
           "buffers": [{"byteLength": len(blob)}], "bufferViews": views, "accessors": accessors,
           "animations": [{"name": "1_idle", "samplers": samplers, "channels": channels}]}
    return _glb_bytes(doc, blob)



def _mul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return [aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz]


AGREE = {"DEF-lid.T.L": _about((0, 1, 0), 10), "DEF-jaw": _about((0, 1, 0), 5)}


def _carried(clip_bytes, figure_bytes):
    """Which bones the output actually drives."""
    out = retarget_clip(clip_bytes, figure_bytes)
    assert out is not None, "the retarget refused outright"
    doc, _blob = split_glb(out.data)
    nodes = doc.get("nodes") or []
    driven = set()
    for anim in doc.get("animations") or []:
        for ch in anim.get("channels") or []:
            i = (ch.get("target") or {}).get("node")
            if isinstance(i, int) and i < len(nodes):
                driven.add(nodes[i].get("name"))
    return out, driven


def test_a_facial_bone_both_rigs_name_and_rest_alike_is_CARRIED():
    clip = _face_clip(_MIXAMO, AGREE)
    figure = _with_face(_RIGIFY, AGREE)
    out, driven = _carried(clip, figure)
    assert "DEF-lid.T.L" in driven, "the eyelid must survive the retarget"
    assert "DEF-jaw" in driven, "a LEAF bone must survive — the law cannot square one up, the delta can"
    assert any("facial" in n for n in out.notes)


def test_the_carried_rotation_is_the_SOURCE_DELTA_on_the_target_rest():
    """Not the source's absolute local. Two faces of the same build rest differently because they ARE
    different faces; pasting one rest onto the other is a shape transplant, not a performance."""
    swing = 26.0
    clip = _face_clip(_MIXAMO, {"DEF-lid.T.L": _about((0, 1, 0), 10)}, swing_degrees=swing)
    # The target rests the SAME bone 25 degrees away — inside tolerance, so it carries.
    figure = _with_face(_RIGIFY, {"DEF-lid.T.L": _about((0, 1, 0), 25)})
    out = retarget_clip(clip, figure)
    doc, blob = split_glb(out.data)
    nodes = doc["nodes"]
    i = next(k for k, n in enumerate(nodes) if n.get("name") == "DEF-lid.T.L")
    anim = doc["animations"][0]
    track = next(ch for ch in anim["channels"] if ch["target"]["node"] == i)
    values = list(_read_accessor(doc, blob, anim["samplers"][track["sampler"]]["output"]))
    if values and not isinstance(values[0], (tuple, list)):
        values = [tuple(values[j:j + 4]) for j in range(0, len(values), 4)]
    target_rest = _about((0, 1, 0), 25)
    # Frame 0 lands ON the target's rest, and frame 1 exactly `swing` away from it.
    assert _quat_gap(values[0], target_rest) < 0.01
    assert abs(_quat_gap(values[-1], target_rest) - swing) < 0.01


def test_a_facial_bone_the_two_rigs_rest_DIFFERENTLY_is_refused_and_named():
    """Eve Maccaro is this case and it is why the gate exists: her facial bones carry the IDENTICAL
    Rigify names and rest inverted — median 142.6 degrees against the clip. Carried by name alone a
    blink swings her lid the wrong way while every count says it worked."""
    clip = _face_clip(_MIXAMO, {"DEF-lid.T.L": _about((0, 1, 0), 10)})
    figure = _with_face(_RIGIFY, {"DEF-lid.T.L": _about((0, 1, 0), 170)})
    out, driven = _carried(clip, figure)
    assert "DEF-lid.T.L" not in driven
    assert any("REFUSED" in n for n in out.notes), "a refusal that says nothing is a silent wrong answer"


def test_the_gate_is_on_the_LOCAL_rest_so_a_flipped_ARMATURE_does_not_refuse_everything():
    """Measured in WORLD, `1_idle`'s head rests 180 degrees from Barbie's, so every facial bone reads
    ~175 degrees apart and every one is refused — on rigs that agree to within 14. A local delta is
    indifferent to every ancestor above the bone, so the local rest is the only frame neither the
    armature nor the bone-map's choice of vertebra can corrupt."""
    clip = _face_clip(_MIXAMO, AGREE)
    figure_doc, _ = split_glb(_with_face(_RIGIFY, AGREE))
    # Flip the whole armature: every world basis moves 180 degrees, every LOCAL rest is untouched.
    figure_doc["nodes"][0]["rotation"] = _about((0, 1, 0), 180)
    flipped = _glb_bytes(figure_doc)
    _out, driven = _carried(clip, flipped)
    assert "DEF-lid.T.L" in driven, "a rotated armature is bookkeeping, not a disagreement about a face"


def test_a_facial_bone_the_TARGET_does_not_have_is_simply_absent():
    clip = _face_clip(_MIXAMO, AGREE)
    figure = _with_face(_RIGIFY, {"DEF-jaw": _about((0, 1, 0), 5)})
    _out, driven = _carried(clip, figure)
    assert "DEF-jaw" in driven
    assert "DEF-lid.T.L" not in driven


def test_a_NON_facial_unmapped_bone_is_still_dropped():
    """The skirt and breast chains. Carrying every unmapped name that happens to match would put a
    source figure's secondary motion onto a target whose cloth is shaped differently."""
    clip = _face_clip(_MIXAMO, {**AGREE, "DEF-skirt.L": _about((0, 1, 0), 10)})
    figure = _with_face(_RIGIFY, {**AGREE, "DEF-skirt.L": _about((0, 1, 0), 10)})
    _out, driven = _carried(clip, figure)
    assert "DEF-lid.T.L" in driven
    assert "DEF-skirt.L" not in driven


def test_the_refusal_quotes_the_median_of_what_it_REFUSED():
    """It quoted the median of every measured bone, which explained a refusal with a number below the
    threshold that caused it — "refused because they rest 11 degrees apart", where 11 is what carries."""
    faces = {"DEF-lid.T.L": _about((0, 1, 0), 10), "DEF-lid.T.R": _about((0, 1, 0), 10),
             "DEF-jaw": _about((0, 1, 0), 5)}
    far = {"DEF-lid.T.L": _about((0, 1, 0), 10), "DEF-lid.T.R": _about((0, 1, 0), 10),
           "DEF-jaw": _about((0, 1, 0), 175)}
    clip, figure = _face_clip(_MIXAMO, faces), _with_face(_RIGIFY, far)
    _carry, refuse, median = _facial_bones(Rig(*split_glb(clip)), Rig(*split_glb(figure)))
    assert refuse == ["DEF-jaw"]
    assert median > FACE_REST_TOLERANCE, "the number quoted must be one that justifies the refusal"
