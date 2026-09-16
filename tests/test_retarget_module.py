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

from conjure.figures import split_glb                                   # noqa: E402
from conjure.retarget import Rig, _sample, _tracks, qconj, qmul, retarget_clip   # noqa: E402


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


def _mat3(q):
    x, y, z, w = q
    return [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
            2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
            2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]


def figure(naming=None, *, roll: float = 0.0, nameless: bool = False) -> bytes:
    """A rigged model: skeleton, one skinned mesh, a bind pose."""
    nodes, names = _skeleton_nodes(naming or _MIXAMO)
    if roll:
        _roll(nodes, roll)
    if nameless:
        for i, nd in enumerate(nodes):
            nd["name"] = f"b{i}"
    nodes.append({"name": "Body", "mesh": 0, "skin": 0})
    doc = {"scenes": [{"nodes": [0, len(nodes) - 1]}], "scene": 0, "nodes": nodes,
           "skins": [{"joints": list(range(len(names)))}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"min": [-0.6, -0.013, -0.17], "max": [0.6, 1.744, 0.23]}]}
    return _glb_bytes(doc)


def clip(naming=None, *, name="wave", roll: float = 0.0, drive=("l_arm", "spine", "l_fore"),
         extra=(), slide=(), slide_moves: bool = True, wrapper: str = "") -> bytes:
    """A clip: the authoring rig's nodes, real motion, and no mesh — the shape the corpus ships."""
    table = naming or _MIXAMO
    nodes, _names = _skeleton_nodes(table)
    if roll:
        _roll(nodes, roll)
    for bone in extra:                          # a bone no other rig has, e.g. a skirt chain
        nodes.append({"name": bone, "translation": [0.0, 0.1, 0.0], "rotation": [0, 0, 0, 1]})
        nodes[0].setdefault("children", []).append(len(nodes) - 1)
    root = 0
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
        node_name = table.get(slot, slot)
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
