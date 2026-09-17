"""A hand model you can WEAR: what import records, and what the endpoint refuses.

Synthetic hands, built to the shape the real ones actually have — which is the point of several of
these. The catalog's five hand files turned out to be two skeletons, with all 25 joints as SIBLINGS
under one node rather than as a chain, and the first version of this feature's gate required a chain.
It would have refused every hand we own.
"""
from __future__ import annotations

import json
import math

import pytest

from conjure import hands
from conjure.figures import write_glb


def _hand(side: str = "left", *, missing: tuple[str, ...] = (), flip_axis: str = "") -> dict:
    """A glTF whose 25 joints are siblings under one node, each rotated so its bone runs along −Z.

    The layout is the real one: `hand_L` with 25 children and no chain between them. Positions are a
    flat fan — good enough for chirality and for length, which is all anything here measures.
    """
    nodes = [{"name": "hand_" + side[0].upper(), "children": []}]
    across = 1 if side == "left" else -1          # thumb on +X for a left hand, −X for a right

    def place(name, x, z, y=0.0):
        # Each joint looks down −Z by default (identity), which is what the gate checks. `flip_axis`
        # names a joint authored along +Z instead — a rig from a different exporter.
        node = {"name": name, "translation": [x, y, z]}
        if name == flip_axis:
            node["rotation"] = [0.0, 1.0, 0.0, 0.0]          # 180° about Y: −Z becomes +Z
        nodes.append(node)
        nodes[0]["children"].append(len(nodes) - 1)

    place("wrist", 0.0, 0.0)
    # The thumb sits OUT OF THE PALM PLANE, and it has to: a flat hand has no chirality at all, and
    # the first version of this fixture was planar, so `hand_side` correctly reported that it could
    # not tell — which is the check working, on a fixture that was not a hand.
    for k, name in enumerate(("thumb-metacarpal", "thumb-phalanx-proximal",
                              "thumb-phalanx-distal", "thumb-tip")):
        place(name, across * (0.03 + 0.01 * k), -0.02 - 0.025 * k, y=-0.01)
    for f, x in (("index", 0.015), ("middle", 0.0), ("ring", -0.015), ("pinky", -0.03)):
        for k, part in enumerate(("metacarpal", "phalanx-proximal", "phalanx-intermediate",
                                  "phalanx-distal", "tip")):
            place(f"{f}-finger-{part}", across * x, -0.03 - 0.03 * k)
    # A skinned mesh, with bounds on the accessor. Import only looks at the skeleton once `glb_bounds`
    # has told it the file is rigged at all, so a joints-only GLB is catalogued as a prop and nothing
    # below ever runs — which is how the first version of this fixture came back with no attributes.
    nodes.append({"name": "model_hand", "mesh": 0, "skin": 0})
    doc = {"asset": {"version": "2.0"}, "scene": 0,
           "scenes": [{"nodes": [0, len(nodes) - 1]}], "nodes": nodes,
           "skins": [{"joints": list(range(1, len(nodes) - 1))}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "accessors": [{"min": [-0.05, -0.02, -0.19], "max": [0.05, 0.01, 0.0]}]}
    if missing:
        doc["nodes"] = [n for n in nodes if n.get("name") not in missing]
        doc["scenes"] = [{"nodes": [0, len(doc["nodes"]) - 1]}]
        doc["nodes"][0]["children"] = list(range(1, len(doc["nodes"]) - 1))
        doc["skins"] = [{"joints": list(range(1, len(doc["nodes"]) - 1))}]
    return doc


# ---------------------------------------------------------------- the vocabulary


def test_the_segments_split_into_bones_and_two_kinds_of_NOT_bones():
    """14 of 24. Measured on a Quest 3: the ratios spread 36% over all 24 and 5% over the 14, because
    `wrist -> metacarpal` is a frame offset and `distal -> tip` is a runtime-derived surface point.
    A scale estimate taken over all 24 would be biased by several per cent on every figure."""
    assert len(hands.JOINTS) == 25
    assert len(hands.SEGMENTS) == 24
    assert [hands.GROUPS.count(g) for g in ("wrist", "bone", "tip")] == [5, 14, 5]
    assert len(hands.BONES) == 14
    assert all(hands.GROUPS[i] == "bone" for i in hands.BONES)
    # every joint but the wrist is some segment's child, exactly once
    children = [s[1] for s in hands.SEGMENTS]
    assert len(set(children)) == 24
    assert set(children) | {"wrist"} == set(hands.JOINTS)


# ---------------------------------------------------------------- extraction


def test_a_webxr_named_hand_is_recognised_and_wearable():
    d = hands.describe(_hand("left"))
    assert d["hand_wearable"] is True
    assert d["hand_side"] == "left"
    assert len(d["hand_joints"]) == 25
    assert "hand_problems" not in d


def test_which_hand_it_is_is_MEASURED_never_read_from_the_name():
    """The filename is not evidence. Both catalog models say `VR_hand_L`/`_R` and the measurement is
    what the endpoint pairs on — a mirrored glove reads as broken tracking, not as a mistake."""
    assert hands.describe(_hand("left"))["hand_side"] == "left"
    assert hands.describe(_hand("right"))["hand_side"] == "right"


def test_a_model_that_is_not_a_hand_yields_NOTHING_rather_than_a_guess():
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}],
           "nodes": [{"name": "Armature", "children": []}], "skins": [{"joints": []}]}
    assert hands.describe(doc) == {}
    assert hands.hand_joints(doc) == {}


def test_a_hand_MISSING_a_joint_is_refused_and_told_which():
    """Discovery is deliberately not built: these files arrived pre-labelled, and a hand named any
    other way gets no map and a reason (docs/plans/hands.md phase 2)."""
    d = hands.describe(_hand("left", missing=("pinky-finger-tip",)))
    assert d == {}, "24 of 25 is not a hand — a partial map would drive a torn skeleton"


def test_a_bone_authored_along_the_WRONG_AXIS_is_refused_and_named():
    """The whole feature rests on a transform copy being EXACT, and that holds only because these
    files are authored in the WebXR joint frame. A rig that is not gets no silent approximation."""
    d = hands.describe(_hand("left", flip_axis="index-finger-phalanx-proximal"))
    assert d["hand_wearable"] is False
    assert any("local −Z" in p for p in d["hand_problems"])
    assert any("index-finger-phalanx-intermediate" in p for p in d["hand_problems"])


def test_the_WRIST_is_exempt_from_the_axis_gate_because_five_bones_leave_one_frame():
    """Measured on the real models: ≥0.986 down every finger and 0.706–0.975 at the wrist, which is
    exactly why the WebXR spec leaves that joint loose. A flat threshold there refuses every hand."""
    doc = _hand("left")
    problems = hands.check(doc, hands.hand_joints(doc))
    assert problems == []
    wrist_rooted = [s for i, s in enumerate(hands.SEGMENTS) if hands.GROUPS[i] == "wrist"]
    assert len(wrist_rooted) == 5 and all(s[0] == "wrist" for s in wrist_rooted)


def test_the_joints_are_SIBLINGS_and_that_is_fine():
    """The real files are 25 children of one node with a Blender `_end` tail each. The gate this
    feature was planned with required 'each finger a real parent→child chain' and would have refused
    every hand in the catalog; the chain lives in the skin's joint list, not the node tree."""
    doc = _hand("left")
    assert len(doc["nodes"][0]["children"]) == 25
    assert hands.describe(doc)["hand_wearable"] is True


def test_bind_lengths_come_back_in_SEGMENTS_order_and_in_metres():
    doc = _hand("left")
    lengths = hands.bind_lengths(doc, hands.hand_joints(doc))
    assert len(lengths) == 24
    assert all(v is not None and 0.001 < v < 0.5 for v in lengths)


# ---------------------------------------------------------------- the endpoint


def _place_hand(srv, client, side="left"):
    """Import a hand and place it, so the entity carries what `/figure/hand` reads."""
    from test_server import _import_id
    asset = _import_id(client, f"VR_hand_{side[0].upper()}.glb", write_glb(_hand(side)))
    attrs = json.loads(srv.library.get(asset)["attributes"])
    assert attrs.get("hand_wearable") is True, attrs.get("hand_problems")
    client.post("/place_cached_asset", json={"id": asset, "name": "h1"})
    return "h1"


def test_wearing_is_durable_world_state_on_the_entity(srv, client):
    eid = _place_hand(srv, client)
    out = client.post("/figure/hand", json={"id": eid, "hand": "auto"}).json()
    assert out["ok"] and out["worn"] and out["hand"] == "left" and out["joints"] == 25
    ent = next(e for e in srv.store.doc["entities"] if e["id"] == eid)
    rig = ent["components"]["hand-rig"]
    assert rig["hand"] == "left"
    assert len(json.loads(rig["joints"])) == 25


def test_taking_it_off_clears_the_side_and_keeps_the_entity(srv, client):
    """Wearing OCCUPIES a model, it does not consume one — so there is always somewhere to put it
    down (docs/decisions.md §30)."""
    eid = _place_hand(srv, client)
    client.post("/figure/hand", json={"id": eid, "hand": "auto"})
    out = client.post("/figure/hand", json={"id": eid, "hand": "off"}).json()
    assert out["ok"] and out["worn"] is False
    ent = next(e for e in srv.store.doc["entities"] if e["id"] == eid)
    assert ent["components"]["hand-rig"]["hand"] == ""
    assert ent["id"] == eid, "the entity survives being taken off"


def test_the_WRONG_SIDE_is_refused_rather_than_honoured(srv, client):
    """A left model on a right hand is a mirrored glove. Worn, it reads as broken tracking rather
    than as a mistake anyone made — so it is refused, and the refusal says which model to place."""
    eid = _place_hand(srv, client, "left")
    out = client.post("/figure/hand", json={"id": eid, "hand": "right"}).json()
    assert out["ok"] is False
    assert "left hand" in out["error"] and "right" in out["error"]


def test_a_model_that_is_not_a_hand_is_refused_with_a_REASON(srv, client):
    from test_figures import _named_skeleton                            # a plain figure
    from test_server import _import_id
    doc, _ = _named_skeleton("dot-side", arms_down=False)
    asset = _import_id(client, "not_a_hand.glb", write_glb(doc))
    client.post("/place_cached_asset", json={"id": asset, "name": "f1"})
    out = client.post("/figure/hand", json={"id": "f1", "hand": "auto"}).json()
    assert out["ok"] is False and "not a wearable hand" in out["error"]


def test_an_unknown_entity_says_so(srv, client):
    out = client.post("/figure/hand", json={"id": "nope", "hand": "auto"}).json()
    assert out["ok"] is False and "no entity" in out["error"]


def test_inspect_describes_a_hand_as_a_HAND_and_not_as_a_broken_figure():
    """It is not a figure: nothing about it is posable, no clip plays on it, and listing the bones it
    does not have would invite exactly the calls that cannot work. Same lesson as the clothing and the
    face — a tool that describes a figure has to describe the whole figure — read the other way."""
    from conjure.figures import figure_description
    said = figure_description(label="VR_hand_L", hand_side="left")
    assert "left HAND you can wear" in said
    assert "wear_hand" in said
    assert "Not worn" in said
    assert "not posable" in said
    assert "No humanoid bone map" not in said, "that reads as a defect, and it is not one"

    worn = figure_description(label="VR_hand_L", hand_side="left", worn="left")
    assert "Currently worn on the left hand" in worn


def test_the_library_and_the_world_can_both_be_ASKED_what_is_wearable(srv, client):
    """`wear <entity>` is unanswerable without this. A hand has to be placed before it can be worn,
    nothing in the CLI placed a library model, and "what do I pass?" is not a question a feature
    should leave to `world` and a squint."""
    from test_server import _import_id
    left = _import_id(client, "VR_hand_L.glb", write_glb(_hand("left")))
    right = _import_id(client, "VR_hand_R.glb", write_glb(_hand("right")))

    out = client.get("/figure/hands").json()
    assert out["ok"]
    assert sorted(h["side"] for h in out["library"]) == ["left", "right"]
    assert {h["id"] for h in out["library"]} == {left, right}
    assert out["placed"] == [], "nothing is placed yet, and that is the distinction that matters"

    client.post("/place_cached_asset", json={"id": left, "name": "hand_left"})
    out = client.get("/figure/hands").json()
    assert [h["id"] for h in out["placed"]] == ["hand_left"]
    assert out["placed"][0]["side"] == "left" and out["placed"][0]["worn"] == ""

    client.post("/figure/hand", json={"id": "hand_left", "hand": "auto"})
    assert client.get("/figure/hands").json()["placed"][0]["worn"] == "left"


def test_a_figure_that_is_not_a_hand_is_not_LISTED_as_wearable(srv, client):
    from test_figures import _named_skeleton
    from test_server import _import_id
    doc, _ = _named_skeleton("dot-side", arms_down=False)
    _import_id(client, "not_a_hand.glb", write_glb(doc))
    assert client.get("/figure/hands").json()["library"] == []


def test_the_tip_offset_is_carried_to_the_component_and_defaults_to_zero(srv, client):
    """Zero is the only defensible default: everything else about this component is exact by
    construction — every joint lands on the pose the runtime reports — and a non-zero default would
    quietly make that untrue."""
    eid = _place_hand(srv, client)
    client.post("/figure/hand", json={"id": eid, "hand": "auto"})
    ent = next(e for e in srv.store.doc["entities"] if e["id"] == eid)
    assert ent["components"]["hand-rig"]["tipOut"] == "0"

    out = client.post("/figure/hand", json={"id": eid, "hand": "auto", "tip_out": 5}).json()
    assert out["ok"] and out["tip_out_mm"] == 5
    ent = next(e for e in srv.store.doc["entities"] if e["id"] == eid)
    assert ent["components"]["hand-rig"]["tipOut"] == "5"


def test_the_tip_offset_can_be_dialled_on_an_ALREADY_WORN_hand(srv, client):
    """Live, and that is the point: the component reads `tipOut` per frame and A-Frame replaces
    `this.data` on the patch, with no `update` handler to re-run `init` — so the skeleton, the bind
    pose and the captured rest all survive. Dialling a number in must not cost a reload."""
    eid = _place_hand(srv, client)
    client.post("/figure/hand", json={"id": eid, "hand": "auto"})
    for mm in (5, 9, 0, -2):
        out = client.post("/figure/hand", json={"id": eid, "hand": "auto", "tip_out": mm}).json()
        assert out["ok"] and out["worn"] is True
        ent = next(e for e in srv.store.doc["entities"] if e["id"] == eid)
        rig = ent["components"]["hand-rig"]
        assert rig["tipOut"] == f"{mm:g}", mm
        # and the rest of the component is untouched, or re-dialling would re-wear it
        assert rig["hand"] == "left" and len(json.loads(rig["joints"])) == 25
