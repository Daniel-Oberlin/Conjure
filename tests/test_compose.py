"""Composing a THING into one GLB, and the verifier that says whether it worked.

The verifier is the thing most worth testing, because it is what everything else is judged by: a
composer bug shows up as a `verify_thing` line, so a verifier that passes everything hides the lot.
Half the tests below therefore BREAK a good file on purpose and check the break is reported.
"""

from __future__ import annotations

import os

import pytest

from conjure.compose import (chain_matrix, compose_thing, joints_agree, mat_trs,
                             quat_from_euler, verify_thing)
from conjure.figures import split_glb, write_glb
from conjure.playcanvas import read_build, things
from tests.test_playcanvas import _build, _render


# ---------------------------------------------------------------- fixtures


def _glb(meshes, *, bones=(), skinned=()) -> bytes:
    """A GLB with real accessors, and optionally a skeleton.

    `meshes` is `[(name, [vertices per primitive])]`; `bones` is `[(name, parent or None, translation,
    rotation quaternion or None)]`; `skinned` names the mesh indices the skin drives. Small but not a
    stub — the composer copies buffer views by byte, so the offsets and lengths have to be real.
    """
    accessors, views, blob = [], [], bytearray()

    def add(count: int, kind: str, comp: int, width: int) -> int:
        nonlocal blob
        data = b"\x00" * (count * width)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)})
        blob += data
        accessors.append({"bufferView": len(views) - 1, "componentType": comp,
                          "count": count, "type": kind})
        return len(accessors) - 1

    prims = []
    for _name, counts in meshes:
        row = []
        for n in counts:
            attrs = {"POSITION": add(n, "VEC3", 5126, 12)}
            if skinned:
                attrs["JOINTS_0"] = add(n, "VEC4", 5123, 8)
                attrs["WEIGHTS_0"] = add(n, "VEC4", 5126, 16)
            row.append({"attributes": attrs})
        prims.append(row)

    nodes = [{"name": n, "mesh": i} for i, (n, _c) in enumerate(meshes)]
    doc = {"asset": {"version": "2.0"}, "meshes": [{"primitives": p} for p in prims],
           "accessors": accessors, "bufferViews": views, "nodes": nodes,
           "scenes": [{"nodes": list(range(len(meshes)))}], "scene": 0}
    if bones:
        first = len(nodes)
        for name, parent, translation, rotation in bones:
            node = {"name": name, "translation": list(translation)}
            if rotation:
                node["rotation"] = list(rotation)
            nodes.append(node)
            if parent is not None:
                nodes[first + parent].setdefault("children", []).append(len(nodes) - 1)
        joints = list(range(first, len(nodes)))
        doc["skins"] = [{"joints": joints,
                         "inverseBindMatrices": add(len(joints), "MAT4", 5126, 64)}]
        for i in skinned:
            nodes[i]["skin"] = 0
        doc["scenes"] = [{"nodes": [i for i in range(len(nodes)) if i < len(meshes) or
                                    all(i not in (n.get("children") or []) for n in nodes)]}]
    doc["buffers"] = [{"byteLength": len(blob)}]
    return write_glb(doc, bytes(blob))


#: The two containers a figure is merged from, as `things.json` would find them: identical skeletons by
#: name, different meshes. Modelled on office-babe, where body, clothes and underwear are three files.
BONES = [("hips", None, (0.0, 1.0, 0.0), None),
         ("hand.R", 0, (0.3, 0.4, 0.0), None)]


def _figure_build(tmp_path, *, donor_bones=None, extra=(), scale=None):
    """A build whose scene places one figure drawn from two containers, plus whatever `extra` says."""
    body = _glb([("Body", [30])], bones=BONES, skinned=[0])
    dress = _glb([("Dress", [20]), ("Slip", [10])], bones=donor_bones or BONES, skinned=[0, 1])
    tree = ("Girl", {"scale": list(scale)} if scale else {}, [
        ("hips", {"position": [0.0, 1.0, 0.0]}, [
            ("hand.R", {"position": [0.3, 0.4, 0.0]}, list(extra)),
        ]),
        ("Body", _render(10, 0, [30]), []),
        ("Dress", _render(11, 0, [31]), []),
        ("Slip", {**_render(11, 1, [31]), "enabled": False}, []),
    ])
    root = _build(tmp_path,
                  containers=[(10, "body.glb", "files/body.glb"), (11, "dress.glb", "files/dress.glb")],
                  renders=[(10000, "Body", 10, 0), (11000, "Dress", 11, 0), (11001, "Slip", 11, 1)],
                  materials=[(30, "skin", {}), (31, "cloth", {})],
                  glbs=[("files/body.glb", body), ("files/dress.glb", dress)],
                  scene_tree=tree)
    build = read_build(root)
    return build, things(build, rules={"exclude": [], "captures": {}})[0]


def _one(thing_tree, tmp_path, **kw):
    """A build with a single container and the given scene subtree."""
    root = _build(tmp_path, scene_tree=thing_tree, **kw)
    build = read_build(root)
    return build, things(build, rules={"exclude": [], "captures": {}})[0]


def _parse(data):
    doc, _blob = split_glb(data)
    return doc


def _named(doc):
    return {n.get("name"): i for i, n in enumerate(doc["nodes"])}


def _parents(doc):
    return {c: i for i, n in enumerate(doc["nodes"]) for c in (n.get("children") or [])}


# ---------------------------------------------------------------- the arithmetic underneath


def test_playcanvas_euler_is_zyx_not_xyz():
    """PlayCanvas's `Quat.setFromEulerAngles` is three.js's ZYX order.

    Pinned with a case where the orders DISAGREE — a single-axis rotation cannot tell them apart, and a
    test that used one would pass against every convention there is.
    """
    q = quat_from_euler((90.0, 90.0, 0.0))
    assert q == pytest.approx([0.5, 0.5, -0.5, 0.5], abs=1e-9)
    assert quat_from_euler((0.0, 0.0, 0.0)) == pytest.approx([0, 0, 0, 1])


def test_a_chain_composes_its_rotations_and_drops_only_the_things_own_position():
    """The thing's scale travels with it and its position does not — `Banana` is a 0.5 wrapper."""
    chain = (("Banana", (5.0, 0.0, 0.0), (0.0, 0.0, 0.0), (0.5, 0.5, 0.5)),
             ("BANANA", (0.0, 2.0, 0.0), (-90.0, 0.0, 0.0), (0.9, 0.9, 0.9)))
    m = chain_matrix(chain)
    assert m[3] == pytest.approx(0.0), "the scene's placement of the thing is not part of the thing"
    assert m[7] == pytest.approx(1.0), "0.5 × 2.0 — the wrapper's scale still applies to the child"
    # -90° about X takes the child's +Y axis to -Z, and the 0.45 is 0.5 × 0.9 rather than 0.9 alone.
    assert m[9] == pytest.approx(-0.45) and abs(m[5]) < 1e-9


# ---------------------------------------------------------------- what a composed file contains


def test_a_thing_is_composed_from_its_scene_subtree_and_verifies(tmp_path):
    build, thing = _figure_build(tmp_path)
    data, notes = compose_thing(build, thing)
    assert verify_thing(build, thing, data) == [], notes
    doc = _parse(data)
    assert doc["extras"]["conjure"]["thing"] == "Girl"
    assert len(doc["skins"]) == 2, "one skin per container, but pointing at ONE set of bones"
    joints = {tuple(s["joints"]) for s in doc["skins"]}
    assert len(joints) == 1, "the skeletons welded by name rather than stacking"


def test_pieces_from_two_containers_land_in_one_file(tmp_path):
    build, thing = _figure_build(tmp_path)
    data, _notes = compose_thing(build, thing)
    doc = _parse(data)
    drawn = {n["name"] for n in doc["nodes"] if n.get("mesh") is not None}
    assert drawn == {"Body", "Dress", "Slip"}
    assert len(doc["meshes"]) == 3
    # The vertex counts come from the source containers, so a mis-copied accessor cannot pass here.
    counts = sorted(doc["accessors"][m["primitives"][0]["attributes"]["POSITION"]]["count"]
                    for m in doc["meshes"])
    assert counts == [10, 20, 30]


def test_a_disabled_piece_is_present_hidden_rather_than_dropped(tmp_path):
    """`enabled: false` on a piece is the site's own wardrobe switch — emit it, flagged."""
    build, thing = _figure_build(tmp_path)
    data, _notes = compose_thing(build, thing)
    doc = _parse(data)
    assert doc["extras"]["conjure"]["hidden"] == ["Slip"]
    slip = doc["nodes"][_named(doc)["Slip"]]
    assert slip["extras"]["conjure"]["optional"] is True
    assert slip.get("mesh") is not None, "hidden means hideable, not absent"


def test_a_mesh_no_entity_draws_is_not_in_the_file(tmp_path):
    """The dead twin. `office-babe.glb` holds a second body copy no scene entity binds."""
    glb = _glb([("Kept", [12]), ("DeadTwin", [999])])
    build, thing = _one(("Prop", {}, [("Kept", _render(10, 0, [30]), [])]), tmp_path,
                        renders=[(10000, "Kept", 10, 0)], glbs=[("files/body.glb", glb)])
    data, _notes = compose_thing(build, thing)
    doc = _parse(data)
    counts = [doc["accessors"][p["attributes"]["POSITION"]]["count"]
              for m in doc["meshes"] for p in m["primitives"]]
    assert counts == [12], "the twin is in the container and not in the scene, so not in the output"


def test_one_mesh_drawn_twice_stays_two_nodes_and_one_mesh(tmp_path):
    """Bride's heels: one mesh, two feet. Flattening the nodes loses a shoe."""
    glb = _glb([("Heel", [8])])
    build, thing = _one(("Bride", {}, [("heel_L", {**_render(10, 0, [30]), "position": [-0.1, 0, 0]}, []),
                                       ("heel_R", {**_render(10, 0, [30]), "position": [0.1, 0, 0]}, [])]),
                        tmp_path, renders=[(10000, "Heel", 10, 0)], glbs=[("files/body.glb", glb)])
    data, _notes = compose_thing(build, thing)
    assert verify_thing(build, thing, data) == []
    doc = _parse(data)
    drawn = [n for n in doc["nodes"] if n.get("mesh") is not None]
    assert len(drawn) == 2 and len({n["mesh"] for n in drawn}) == 1
    assert len(doc["meshes"]) == 1, "instanced, not duplicated"


def test_a_piece_parented_to_a_bone_keeps_that_bone(tmp_path):
    """Oktoberfest's beer hangs off `DEF-hand.R`, and a merge that reparents it drops it on the floor."""
    beer = ("Beer", {**_render(11, 0, [31]), "position": [0.0, 0.05, 0.0]}, [])
    build, thing = _figure_build(tmp_path, extra=[beer])
    # `Beer` reuses the dress container's mesh 0 purely to keep the fixture small; what is under test
    # is where the node ends up, not which file it came from.
    data, _notes = compose_thing(build, thing)
    assert verify_thing(build, thing, data) == []
    doc = _parse(data)
    assert doc["nodes"][_parents(doc)[_named(doc)["Beer"]]]["name"] == "hand.R"


def test_the_thing_root_carries_its_scale_and_not_its_position(tmp_path):
    build, thing = _one(("Banana", {"position": [5.0, 0.0, 0.0], "scale": [0.5, 0.5, 0.5]},
                         [("BANANA", {**_render(10, 0, [30]), "scale": [0.9, 0.9, 0.9]}, [])]),
                        tmp_path, renders=[(10000, "BANANA", 10, 0)],
                        glbs=[("files/body.glb", _glb([("BANANA", [6])]))])
    data, _notes = compose_thing(build, thing)
    assert verify_thing(build, thing, data) == []
    doc = _parse(data)
    root = doc["nodes"][doc["scenes"][0]["nodes"][0]]
    assert root["scale"] == [0.5, 0.5, 0.5]
    assert root["translation"] == [0.0, 0.0, 0.0]
    assert doc["nodes"][_named(doc)["BANANA"]]["scale"] == [0.9, 0.9, 0.9]


# ---------------------------------------------------------------- the skeleton


def test_a_donor_bound_against_a_different_rest_pose_is_reported(tmp_path):
    """Bride's `model_britney_bride.glb` is a different rig wearing the same bone names."""
    moved = [("hips", None, (0.0, 1.0, 0.0), None), ("hand.R", 0, (0.3, 9.9, 0.0), None)]
    build, thing = _figure_build(tmp_path, donor_bones=moved)
    data, notes = compose_thing(build, thing)
    assert any("DIFFERENT rest pose" in n and "hand.R" in n for n in notes), notes
    assert any("not where" in problem for problem in verify_thing(build, thing, data))


def test_bones_the_containers_agree_on_are_taken_from_them_not_from_a_saved_pose(tmp_path):
    """Alice's two eye bones are 90° out in the scene and nowhere else — the scene holds a POSE there.

    The fix has to be narrow: where the containers disagree with EACH OTHER the scene keeps its value,
    because letting a one-off donor rewrite the skeleton would break every other piece bound to it.
    """
    turned = [("hips", None, (0.0, 1.0, 0.0), None),
              ("hand.R", 0, (0.3, 0.4, 0.0), (0.7071, 0.0, 0.0, 0.7071))]
    body = _glb([("Body", [30])], bones=turned, skinned=[0])
    dress = _glb([("Dress", [20])], bones=turned, skinned=[0])
    tree = ("Girl", {}, [("hips", {"position": [0.0, 1.0, 0.0]},
                          [("hand.R", {"position": [0.3, 0.4, 0.0]}, [])]),
                         ("Body", _render(10, 0, [30]), []),
                         ("Dress", _render(11, 0, [31]), [])])
    root = _build(tmp_path,
                  containers=[(10, "body.glb", "files/body.glb"), (11, "dress.glb", "files/dress.glb")],
                  renders=[(10000, "Body", 10, 0), (11000, "Dress", 11, 0)],
                  glbs=[("files/body.glb", body), ("files/dress.glb", dress)], scene_tree=tree)
    build = read_build(root)
    thing = things(build, rules={"exclude": [], "captures": {}})[0]
    data, notes = compose_thing(build, thing)
    assert any("as the container binds them" in n for n in notes), notes
    doc = _parse(data)
    assert "matrix" in doc["nodes"][_named(doc)["hand.R"]], "rebound bones carry the container's matrix"
    assert verify_thing(build, thing, data) == [], "and the file then agrees with both containers"


def test_the_rest_pose_test_survives_the_scene_scaling_the_whole_figure(tmp_path):
    """Oktoberfest is placed at 1.25, and a world-space comparison called all 175 of her bones broken."""
    build, thing = _figure_build(tmp_path, scale=(1.25, 1.25, 1.25))
    assert thing.scale == (1.25, 1.25, 1.25)
    data, notes = compose_thing(build, thing)
    assert not [n for n in notes if "rest pose" in n], notes
    assert verify_thing(build, thing, data) == []


def test_joints_agree_reads_local_matrices_so_depth_does_not_matter():
    class _S:
        doc = {"nodes": [{"name": "a", "translation": [0, 1, 0], "children": [1]},
                         {"name": "b", "translation": [0, 2, 0]}],
               "skins": [{"joints": [0, 1]}]}
        blob = b""
    rest = {"a": mat_trs((0, 1, 0), (0, 0, 0, 1), (1, 1, 1)),
            "b": mat_trs((0, 2, 0), (0, 0, 0, 1), (1, 1, 1))}
    assert joints_agree(_S(), rest) == []
    rest["b"] = mat_trs((0, 5, 0), (0, 0, 0, 1), (1, 1, 1))
    assert joints_agree(_S(), rest) == ["b"]


def test_one_mesh_drawn_twice_in_the_SAME_place_is_a_variant_not_an_instance(tmp_path):
    """Alice's hair: `Side_Swept` and `Side_Swept2`, one mesh, one transform, two material sets.

    Both are enabled and the site flips between them with a script the capture does not contain, so
    drawn together they z-fight — which is the pale locks at the front of her otherwise brown head.
    The distinction from bride's heels is the transform: same place is a variant, two places is an
    instance.
    """
    glb = _glb([("Hair", [8])])
    build, thing = _one(("Alice", {}, [("Side_Swept", _render(10, 0, [30]), []),
                                       ("Side_Swept2", _render(10, 0, [31]), [])]),
                        tmp_path, renders=[(10000, "Hair", 10, 0)], glbs=[("files/body.glb", glb)],
                        materials=[(30, "pale", {"specularMap": 40}),
                                   (31, "brown", {"diffuseMap": 40})],
                        textures=[(40, "hair.png", "files/hair.png")])
    assert [p.entity for p in thing.shadowed] == ["Side_Swept"], "the one with no base colour loses"
    assert [p.entity for p in thing.live] == ["Side_Swept2"]
    data, notes = compose_thing(build, thing)
    assert verify_thing(build, thing, data) == [], notes
    doc = _parse(data)
    assert doc["extras"]["conjure"]["hidden"] == ["Side_Swept"]
    assert doc["nodes"][_named(doc)["Side_Swept"]]["extras"]["conjure"]["variant"] is True
    assert doc["nodes"][_named(doc)["Side_Swept"]].get("mesh") is not None, "kept, not dropped"


def test_a_base_colour_outranks_a_slot_count_when_two_claims_tie():
    """Alice's two hair claims fill six slots each; only one has a base colour on every primitive.

    Slots alone score that a tie and pick by luck, which is how the pale set won. It also fixes a bug
    of the same shape in `read_build`: ebony's `handmodeltutorial` binds a sphere-map placeholder over
    both hand primitives and used to beat the real `ArmsVR`/`FingernailsVR` on a 2-2 tie.
    """
    from conjure.playcanvas import how_dressed

    class _B:
        @staticmethod
        def asset(mid):
            return {1: {"data": {"specularMap": 9, "opacityMap": 9}},
                    2: {"data": {"diffuseMap": 9, "opacityMap": 9}}}[mid]
    assert how_dressed(_B(), (1,)) == (0, 2) and how_dressed(_B(), (2,)) == (1, 2)
    assert how_dressed(_B(), (2,)) > how_dressed(_B(), (1,))


def test_a_refractive_lens_with_no_maps_is_made_invisible_not_painted_on(tmp_path):
    """Bride's `Reflections-eyes` is 48% white over her irises, and the brown washed out to pale tan."""
    from conjure.playcanvas import material_from, _Textures
    build, _thing = _figure_build(tmp_path)
    tex = _Textures(build, 1024, 90)
    lens = material_from({"useDynamicRefraction": True, "refraction": 1, "blendType": 2,
                          "opacity": 0.477273, "diffuse": [1, 1, 1]}, "Reflections-eyes", tex)
    assert lens["pbrMetallicRoughness"]["baseColorFactor"] == [0.0, 0.0, 0.0, 0.0]
    assert lens["alphaMode"] == "BLEND"
    assert any("refractive lens" in w for w in tex.warnings)
    assert not any("will read as opaque" in w for w in tex.warnings), "the generic warning is now wrong"


def test_a_refraction_that_CARRIES_a_texture_is_left_alone(tmp_path):
    """The rule is about a layer with no detail of its own. Tinted, textured glass is an object."""
    from conjure.playcanvas import material_from, _Textures
    build, _thing = _figure_build(tmp_path)
    tex = _Textures(build, 1024, 90)
    glass = material_from({"useDynamicRefraction": True, "refraction": 1, "blendType": 2,
                           "opacity": 0.5, "diffuse": [1, 1, 1], "diffuseMap": 999},
                          "stained-glass", tex)
    assert glass["pbrMetallicRoughness"]["baseColorFactor"] != [0.0, 0.0, 0.0, 0.0]
    assert not any("refractive lens" in w for w in tex.warnings)


def test_a_degenerate_scene_claim_loses_to_the_containers_own_materials(tmp_path):
    """Alice's eyes: the scene binds `Eye_R, Cornea_R, Eye_R, Cornea_R` over four primitives where the
    container's template binds `Eye_R, Cornea_R, Eye_L, Cornea_L`, and reaches for a MAPLESS duplicate
    while it is at it — three assets are named `aula_Std_Cornea_R` and it takes an empty one. Her
    corneas are where the blood vessels in the whites are drawn, so the empty pair renders as nothing.

    A claim that repeats one material where another names two has lost information rather than
    expressed a preference, and only then does the template outrank `prefer="scene"`.
    """
    glb = _glb([("Eyes", [4, 4, 4, 4])])
    root = _build(tmp_path, scene_tree=("Alice", {}, [("CC_Base_Eye", _render(10, 0, [30, 32, 30, 32]), [])]),
                  renders=[(10000, "Eyes", 10, 0)], glbs=[("files/body.glb", glb)],
                  materials=[(30, "Eye_R", {"diffuseMap": 40}), (31, "Eye_L", {"diffuseMap": 40}),
                             (32, "Cornea_R", {}), (33, "Cornea_L", {"diffuseMap": 40})],
                  textures=[(40, "eye.png", "files/eye.png")],
                  templates=[(50, "alice", [("CC_Base_Eye", 10000, [30, 32, 31, 33])])])
    build = read_build(root)
    thing = things(build, rules={"exclude": [], "captures": {}})[0]
    piece = thing.pieces[0]
    assert piece.redressed is True
    assert piece.materials == (30, 32, 31, 33), "the left eye stops wearing the right eye's material"
    data, notes = compose_thing(build, thing)
    assert any("degenerate copy" in n for n in notes), notes
    assert verify_thing(build, thing, data) == []


def test_a_scene_claim_that_merely_has_fewer_MAPS_still_wins(tmp_path):
    """ebony's scene names the VR hand materials and the template the AR ones — both two distinct, and
    this is a VR app. Richness alone must not overturn the scene, which is why `prefer` exists."""
    glb = _glb([("Hand", [4, 4])])
    root = _build(tmp_path, scene_tree=("Hand", {}, [("model_hand", _render(10, 0, [30, 32]), [])]),
                  renders=[(10000, "Hand", 10, 0)], glbs=[("files/body.glb", glb)],
                  materials=[(30, "ArmsVR", {"diffuseMap": 40}), (31, "ArmsAR", {"diffuseMap": 40, "aoMap": 40}),
                             (32, "NailsVR", {"diffuseMap": 40}), (33, "NailsAR", {"diffuseMap": 40, "aoMap": 40})],
                  textures=[(40, "h.png", "files/h.png")],
                  templates=[(50, "hand", [("model_hand", 10000, [31, 33])])])
    build = read_build(root)
    thing = things(build, rules={"exclude": [], "captures": {}})[0]
    assert thing.pieces[0].materials == (30, 32) and thing.pieces[0].redressed is False


def test_a_viewing_copy_leaves_out_what_the_scene_does_not_draw(tmp_path):
    """A glb viewer draws every node it is given and ignores `extras`, so the asset looks wrong in one.

    Alice's hidden pale hair and bride's switched-off underwear are both plainly visible in a viewer
    and read as bugs that were already fixed. `shown=True` writes a copy for LOOKING at; the asset
    keeps them, because a part the runtime can show again has to be in the file to be shown.
    """
    build, thing = _figure_build(tmp_path)
    full, _n = compose_thing(build, thing)
    view, _n = compose_thing(build, thing, shown=True)
    assert {n["name"] for n in _parse(full)["nodes"] if n.get("mesh") is not None} == \
        {"Body", "Dress", "Slip"}
    assert {n["name"] for n in _parse(view)["nodes"] if n.get("mesh") is not None} == {"Body", "Dress"}
    assert _parse(view)["extras"]["conjure"]["shown"] is True
    assert verify_thing(build, thing, view) == [], "and it is judged by what it claims to be"
    assert verify_thing(build, thing, full) == []


def test_a_viewing_copy_is_still_held_to_everything_else(tmp_path):
    """`shown` excuses an absent wardrobe piece and nothing else — a LIVE piece missing is still wrong."""
    build, thing = _figure_build(tmp_path)
    view, _n = compose_thing(build, thing, shown=True)

    def drop(doc):
        i = _named(doc)["Dress"]
        doc["nodes"][i].pop("mesh")
        doc["nodes"][i].pop("extras")
    assert any("is not in the file" in p for p in verify_thing(build, thing, _tamper(view, drop)))


# ---------------------------------------------------------------- the verifier earns its keep


def _tamper(data, change):
    doc, blob = split_glb(data)
    change(doc)
    return write_glb(doc, blob)


def test_the_verifier_catches_a_piece_that_went_missing(tmp_path):
    build, thing = _figure_build(tmp_path)
    data, _n = compose_thing(build, thing)

    def drop(doc):
        i = _named(doc)["Dress"]
        doc["nodes"][i].pop("mesh")
        doc["nodes"][i].pop("extras")
    assert any("is not in the file" in p for p in verify_thing(build, thing, _tamper(data, drop)))


def test_the_verifier_catches_geometry_the_thing_never_asked_for(tmp_path):
    build, thing = _figure_build(tmp_path)
    data, _n = compose_thing(build, thing)

    def smuggle(doc):
        doc["nodes"].append({"name": "Twin", "mesh": 0})
        doc["nodes"][doc["scenes"][0]["nodes"][0]]["children"].append(len(doc["nodes"]) - 1)
    assert any("claims no piece" in p for p in verify_thing(build, thing, _tamper(data, smuggle)))


def test_the_verifier_catches_a_moved_piece(tmp_path):
    build, thing = _figure_build(tmp_path, extra=[("Beer", _render(11, 0, [31]), [])])
    data, _n = compose_thing(build, thing)

    def shove(doc):
        doc["nodes"][_named(doc)["Beer"]]["translation"] = [9.0, 9.0, 9.0]
    assert any("not where the scene puts it" in p
               for p in verify_thing(build, thing, _tamper(data, shove)))


def test_the_verifier_catches_a_piece_torn_off_its_bone(tmp_path):
    build, thing = _figure_build(tmp_path, extra=[("Beer", _render(11, 0, [31]), [])])
    data, _n = compose_thing(build, thing)

    def reparent(doc):
        beer, hand = _named(doc)["Beer"], _named(doc)["hand.R"]
        doc["nodes"][hand]["children"].remove(beer)
        doc["nodes"][doc["scenes"][0]["nodes"][0]].setdefault("children", []).append(beer)
    assert any("hangs off" in p for p in verify_thing(build, thing, _tamper(data, reparent)))


def test_the_verifier_catches_flattened_instancing(tmp_path):
    glb = _glb([("Heel", [8])])
    build, thing = _one(("Bride", {}, [("heel_L", _render(10, 0, [30]), []),
                                       ("heel_R", _render(10, 0, [30]), [])]),
                        tmp_path, renders=[(10000, "Heel", 10, 0)], glbs=[("files/body.glb", glb)])
    data, _n = compose_thing(build, thing)

    def flatten(doc):
        i = _named(doc)["heel_R"]
        doc["nodes"][i].pop("mesh")
        doc["nodes"][i].pop("extras")
    assert any("instancing was flattened" in p or "is not in the file" in p
               for p in verify_thing(build, thing, _tamper(data, flatten)))


def test_the_verifier_catches_a_material_the_scene_did_not_bind(tmp_path):
    build, thing = _figure_build(tmp_path)
    data, _n = compose_thing(build, thing)

    def rename(doc):
        doc["materials"][0]["name"] = "something else"
    assert any("wears" in p for p in verify_thing(build, thing, _tamper(data, rename)))


def test_the_verifier_catches_geometry_copied_from_the_wrong_mesh(tmp_path):
    """A swapped accessor is invisible in a viewer and obvious against the source's vertex counts."""
    build, thing = _figure_build(tmp_path)
    data, _n = compose_thing(build, thing)

    def swap(doc):
        a = doc["nodes"][_named(doc)["Dress"]]["mesh"]
        b = doc["nodes"][_named(doc)["Slip"]]["mesh"]
        doc["nodes"][_named(doc)["Dress"]]["mesh"] = b
        doc["nodes"][_named(doc)["Slip"]]["mesh"] = a
    assert any("vertex counts" in p for p in verify_thing(build, thing, _tamper(data, swap)))


def test_the_verifier_catches_two_skeletons_wearing_one_set_of_names(tmp_path):
    build, thing = _figure_build(tmp_path)
    data, _n = compose_thing(build, thing)

    def stack(doc):
        hand = _named(doc)["hand.R"]
        doc["nodes"].append(dict(doc["nodes"][hand]))
        doc["skins"][1]["joints"] = [doc["skins"][1]["joints"][0], len(doc["nodes"]) - 1]
    assert any("did not weld, they stacked" in p for p in verify_thing(build, thing, _tamper(data, stack)))


def test_the_verifier_rejects_something_that_is_not_a_glb(tmp_path):
    build, thing = _figure_build(tmp_path)
    assert verify_thing(build, thing, b"not a glb at all") == ["not a binary glTF"]


# ---------------------------------------------------------------- the awkward inputs


def test_a_container_that_is_not_on_disk_is_reported_rather_than_crashing(tmp_path):
    """Bride's `underwear.glb` never downloaded, and one missing file must not lose the other twelve."""
    build, thing = _figure_build(tmp_path)
    os.remove(os.path.join(build.root, "files", "dress.glb"))
    data, notes = compose_thing(build, thing)
    assert data is not None and any("not in this capture" in n for n in notes)
    assert any("is not in the file" in p for p in verify_thing(build, thing, data))


def test_a_thing_whose_every_container_is_gone_composes_to_nothing(tmp_path):
    build, thing = _figure_build(tmp_path)
    for name in ("body.glb", "dress.glb"):
        os.remove(os.path.join(build.root, "files", name))
    data, notes = compose_thing(build, thing)
    assert data is None and notes


def test_morph_targets_and_their_sparse_accessors_survive_the_copy(tmp_path):
    """27 of the corpus's 1,293 containers morph, and a sparse accessor hides two extra buffer views."""
    glb = _glb([("Face", [4])])
    doc, blob = split_glb(glb)
    doc["bufferViews"].append({"buffer": 0, "byteOffset": 0, "byteLength": 4})
    doc["accessors"].append({"componentType": 5125, "count": 1, "type": "SCALAR",
                             "sparse": {"count": 1,
                                        "indices": {"bufferView": len(doc["bufferViews"]) - 1,
                                                    "byteOffset": 0, "componentType": 5125},
                                        "values": {"bufferView": len(doc["bufferViews"]) - 1,
                                                   "byteOffset": 0}}})
    doc["meshes"][0]["primitives"][0]["targets"] = [{"POSITION": len(doc["accessors"]) - 1}]
    doc["meshes"][0]["weights"] = [0.0]
    doc["meshes"][0]["extras"] = {"targetNames": ["smile"]}
    build, thing = _one(("Head", {}, [("Face", _render(10, 0, [30]), [])]), tmp_path,
                        renders=[(10000, "Face", 10, 0)], glbs=[("files/body.glb", write_glb(doc, blob))])
    data, _notes = compose_thing(build, thing)
    out = _parse(data)
    assert out["meshes"][0]["extras"]["targetNames"] == ["smile"]
    target = out["meshes"][0]["primitives"][0]["targets"][0]["POSITION"]
    assert "sparse" in out["accessors"][target]
    for part in ("indices", "values"):
        view = out["accessors"][target]["sparse"][part]["bufferView"]
        assert view < len(out["bufferViews"]), "the sparse sub-views were remapped, not left dangling"
    assert verify_thing(build, thing, data) == []


def test_a_file_name_is_made_safe_without_colliding(tmp_path):
    from conjure.compose import _safe
    assert _safe("JAPANESEROOM BAKED") == "JAPANESEROOM BAKED"
    assert _safe("a/b:c") == "a_b_c"
    assert _safe("...") == "thing"


def test_a_figure_welded_to_its_room_is_reported(tmp_path):
    """teacher's `SchoolCorridor` is 394 entities holding BOTH the corridor and the teacher, so
    converting it as one thing means she is not placeable at all — no clips, no rig, no figure.

    susan is the same shape and was found by hand, weeks apart. The convention cannot decide this, but
    it can SEE it, and one line of output is the difference between a person finding it and a figure
    quietly disappearing at the next import.
    """
    from conjure.playcanvas import thing_notes
    room = _glb([(f"wall{i}", [6]) for i in range(4)])
    figure = _glb([("Body", [30])], bones=BONES, skinned=[0])
    tree = ("SchoolCorridor", {}, [
        ("EnvironmentVR", {}, [(f"wall{i}", _render(10, i, [30]), []) for i in range(4)]),
        ("MainModelTeacher", {}, [("Body", _render(11, 0, [30]), [])]),
    ])
    root = _build(tmp_path,
                  containers=[(10, "room.glb", "files/room.glb"), (11, "teacher.glb", "files/teacher.glb")],
                  renders=[(10000 + i, f"wall{i}", 10, i) for i in range(4)] + [(11000, "Body", 11, 0)],
                  glbs=[("files/room.glb", room), ("files/teacher.glb", figure)], scene_tree=tree)
    build = read_build(root)
    bare = {"exclude": [], "captures": {}}
    assert any("welded to its environment" in n for n in thing_notes(build, rules=bare))
    # Split, and the note goes away — the report is about the CHOICE, not about the geometry.
    split = {"exclude": [], "captures": {"cap": {"scene.json": {"MainModelTeacher": "Teacher",
                                                                "EnvironmentVR": "corridor"}}}}
    notes = thing_notes(build, capture="cap", rules=split)
    assert not any("welded" in n for n in notes), notes
    assert sorted(t.name for t in things(build, capture="cap", rules=split)) == ["Teacher", "corridor"]


def test_the_capture_name_is_not_in_the_bytes(tmp_path):
    """A composed file is CONTENT; which capture it was pulled from is catalog metadata.

    Putting it in `extras` defeated the content addressing the whole catalog rests on: the props build
    is the same PlayCanvas release in fifteen captures, so `Banana` should be one row tagged with all
    fifteen — and instead it was fifteen rows with fifteen ids, fifteen copies of the bytes, and a
    director with fifteen bananas to choose between. Everything else in the mark is stable across
    captures (the scene file id, the container asset ids), which is why only this one had to go.
    """
    build, thing = _figure_build(tmp_path)
    first, _n = compose_thing(build, thing)
    second, _n = compose_thing(build, thing)
    assert first == second, "composing is deterministic"
    mark = _parse(first)["extras"]["conjure"]
    assert "capture" not in mark
    assert set(mark) == {"thing", "scene", "pieces", "hidden", "rebound", "containers", "shown"}
