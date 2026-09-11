"""Re-assembling a PlayCanvas build (`conjure/playcanvas.py`).

The claims worth holding, and every one of them is something the capture this was written against
actually does:

  1. the binding comes from the SCENE, per primitive, in order — not from the container;
  2. an opacity map naming a texture with no alpha is a no-op, and believing it punches holes;
  3. `glossInvert` decides whether the source is gloss or roughness, and it goes both ways in one build;
  4. the geometry comes out untouched, so a bone map derived from the original still applies.

The fixture is synthetic rather than a checked-in build: a real one is 229 MB, and every structure that
matters here is three dicts deep.
"""

import json
import os
import struct

import pytest

from conjure.figures import split_glb, write_glb
from conjure.playcanvas import (BLEND_NONE, BLEND_NORMAL, Build, adopt_unbound, build_origin,
                                by_container, find_builds, find_orphans, has_alpha, load_image,
                                material_from, missing_files, read_build, rebuild, report_orphans,
                                variant_only, _Textures)

PIL = pytest.importorskip("PIL.Image")


# ---------------------------------------------------------------- fixtures


def _glb(meshes=(1, 3)) -> bytes:
    """A GLB with the given primitive counts per mesh, and nothing else. Enough for the material pass,
    which reads `meshes[].primitives[]` and never looks at a vertex."""
    doc = {
        "asset": {"version": "2.0", "generator": "test"},
        "buffers": [{"byteLength": 8}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 8}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}} for _ in range(n)]}
                   for n in meshes],
        "nodes": [{"name": "root", "mesh": 0}],
        "scenes": [{"nodes": [0]}], "scene": 0,
    }
    return write_glb(doc, b"01234567")


def _png(path, mode, *, transparent=False, size=(8, 8)):
    """A PNG of the given kind. `P` + `transparent` is the awkward one on purpose — an indexed image
    whose alpha lives in a `tRNS` chunk, which is what Jane's eyelashes are."""
    Image = PIL
    if mode == "P":
        img = Image.new("P", size, 1)
        img.putpalette([0, 0, 0] + [200, 100, 50] * 255)
        if transparent:
            # Half the pixels on the transparent index, because alpha that no pixel USES is not
            # transparency — which is the distinction `has_alpha` exists to make.
            for x in range(size[0] // 2):
                for y in range(size[1]):
                    img.putpixel((x, y), 0)
            img.save(path, transparency=0)
        else:
            img.save(path)
        return str(path)
    if mode == "RGBA":
        img = Image.new("RGBA", size, (200, 100, 50, 0 if transparent else 255))
    else:
        img = Image.new("RGB", size, (200, 100, 50))
    img.save(path)
    return str(path)


def _build(tmp_path, *, materials=None, entities=None, renders=None, containers=None, textures=(),
           templates=(), scene=True):
    """Write a minimal published build to disk and return its directory.

    `templates` is `[(id, name, [(entity, render, [materials])])]`; `scene=False` leaves the scene
    REFERENCED but absent, which is the shape the second capture arrived in."""
    assets = {}
    for aid, name, url in containers or [(10, "body.glb", "files/body.glb")]:
        assets[str(aid)] = {"id": str(aid), "type": "container", "name": name,
                            "file": {"filename": name, "url": url}}
    for aid, name, url in textures:
        assets[str(aid)] = {"id": str(aid), "type": "texture", "name": name,
                            "file": {"filename": name, "url": url}, "data": {}}
    for aid, name, container, index in renders or [(20, "body", 10, 0), (21, "hair", 10, 1)]:
        assets[str(aid)] = {"id": str(aid), "type": "render", "name": name,
                            "data": {"containerAsset": container, "renderIndex": index}}
    for aid, name, data in materials or [(30, "skin", {}), (31, "hair", {})]:
        assets[str(aid)] = {"id": str(aid), "type": "material", "name": name, "data": data}

    def hierarchy(specs):
        return {f"guid-{i}": {"name": n, "components": {
            "render": {"type": "asset", "asset": r, "materialAssets": m}}}
            for i, (n, r, m) in enumerate(specs)}

    ents = hierarchy(entities if entities is not None else [("body", 20, [30]),
                                                            ("hair", 21, [31, 31, 31])])
    for aid, name, specs in templates:
        assets[str(aid)] = {"id": str(aid), "type": "template", "name": name,
                            "data": {"entities": hierarchy(specs)}}
    os.makedirs(tmp_path / "files", exist_ok=True)
    (tmp_path / "files" / "body.glb").write_bytes(_glb())
    (tmp_path / "config.json").write_text(json.dumps(
        {"assets": assets, "scenes": [{"name": "main", "url": "scene.json"}]}))
    if scene:
        (tmp_path / "scene.json").write_text(json.dumps({"entities": ents}))
    return str(tmp_path)


# ---------------------------------------------------------------- reading the build


def test_a_build_is_found_by_its_config_and_scenes(tmp_path):
    root = _build(tmp_path / "outer")
    _build(tmp_path / "outer" / "release" / "abc")           # a build nested inside a build: real
    (tmp_path / "outer" / "notabuild").mkdir()
    (tmp_path / "outer" / "notabuild" / "config.json").write_text('{"unrelated": 1}')
    found = find_builds(str(tmp_path))
    assert sorted(found) == sorted([root, os.path.join(root, "release", "abc")])


def test_the_binding_is_read_off_the_scene_per_primitive_in_order(tmp_path):
    build = read_build(_build(tmp_path))
    binds = {b.mesh: b for b in build.bindings}
    assert binds[0].materials == (30,) and binds[0].entity == "body"
    assert binds[1].materials == (31, 31, 31), "one material per primitive, in order"


def test_a_render_component_with_no_container_behind_it_is_skipped(tmp_path):
    root = _build(tmp_path)
    scene = json.loads((tmp_path / "scene.json").read_text())
    scene["entities"]["box"] = {"name": "Box", "components": {
        "render": {"type": "box", "asset": None, "materialAssets": [30]}}}
    (tmp_path / "scene.json").write_text(json.dumps(scene))
    assert len(read_build(root).bindings) == 2


def test_two_entities_dressing_one_mesh_differently_is_reported_once(tmp_path):
    """A props library reuses one button mesh in a dozen colours. First wins, and the clash is reported
    ONCE however many instances there are — reporting per instance buried everything else."""
    root = _build(tmp_path)
    scene = json.loads((tmp_path / "scene.json").read_text())
    for i in range(5):
        scene["entities"][f"dup-{i}"] = {"name": f"other-{i}", "components": {
            "render": {"type": "asset", "asset": 20, "materialAssets": [31]}}}
    (tmp_path / "scene.json").write_text(json.dumps(scene))
    build = read_build(root)
    assert {b.mesh: b.materials for b in build.bindings}[0] == (30,), "the first binding wins"
    assert len([n for n in build.notes if "bind different" in n or "disagree" in n]) == 1


def test_an_unbound_mesh_can_adopt_a_material_by_render_name(tmp_path):
    """Jane's hair is inside her body GLB *and* a standalone container, and the scene renders the
    standalone one — so the copy in her body file is bound by nobody and comes out flat grey."""
    root = _build(tmp_path,
                  containers=[(10, "body.glb", "files/body.glb"), (11, "hair.glb", "files/body.glb")],
                  renders=[(20, "body", 10, 0), (21, "hairmesh", 10, 1), (22, "hairmesh", 11, 0)],
                  entities=[("body", 20, [30]), ("hair", 22, [31])])
    build = read_build(root)
    assert not [b for b in build.bindings if b.container == 10 and b.mesh == 1]
    assert adopt_unbound(build) == 1
    got = [b for b in build.bindings if b.container == 10 and b.mesh == 1][0]
    assert got.materials == (31,) and got.adopted, "flagged, because a name match is evidence not proof"
    assert any("INFERRED" in n for n in build.notes)


def test_a_template_supplies_the_binding_when_the_scene_is_absent(tmp_path):
    """A PlayCanvas template is a serialised entity hierarchy — the same structure as a scene, and a
    second place the binding lives. Akari's capture had NO scene file on disk and sixteen templates,
    one per skin-tone variant, each binding its own container; reading them is what made it
    convertible at all."""
    root = _build(tmp_path, scene=False, entities=[],
                  templates=[(40, "JapaneseVER2", [("body", 20, [30]), ("hair", 21, [31, 31, 31])])])
    build = read_build(root)
    assert {b.mesh: b.materials for b in build.bindings} == {0: (30,), 1: (31, 31, 31)}
    assert any("falling back to the template" in n for n in build.notes)


def test_a_scene_wins_over_a_template_where_both_speak(tmp_path):
    """Both are read, scenes first, because the scene is what actually runs. Jane's build has both, and
    her templates name a different material for one mesh than her scene does."""
    root = _build(tmp_path, entities=[("body", 20, [30])],
                  templates=[(40, "variant", [("body-template", 20, [31])])])
    build = read_build(root)
    got = [b for b in build.bindings if b.mesh == 0][0]
    assert got.materials == (30,) and got.entity == "body"


def test_a_template_can_bind_a_mesh_the_scene_leaves_alone(tmp_path):
    """Which is how Jane's in-file hair copy gets its material from the build rather than from the
    `--adopt` guess: her scene ignores that mesh and a template binds it."""
    root = _build(tmp_path, entities=[("body", 20, [30])],
                  templates=[(40, "variant", [("hair", 21, [31, 31, 31])])])
    build = read_build(root)
    assert {b.mesh: b.materials for b in build.bindings} == {0: (30,), 1: (31, 31, 31)}
    assert adopt_unbound(build) == 0, "nothing left to guess at"


# ---------------------------------------------------------------- what the capture does not hold


def test_a_percent_encoded_url_finds_the_file_a_browser_actually_saved(tmp_path):
    """A registry records `JAPANESEROOM%20BAKED.glb`; a browser saves
    `JAPANESEROOM BAKED.glb`. Joining the raw URL finds nothing and the container reads as MISSING
    while sitting right there — which is how an entire room went unnoticed in a capture that had it.
    Any asset with a space or a non-ASCII character in its name hits this."""
    os.makedirs(tmp_path / "files", exist_ok=True)
    (tmp_path / "files" / "JAPANESEROOM BAKED.glb").write_bytes(_glb())
    build = Build(root=str(tmp_path), assets={7: {
        "id": "7", "type": "container", "name": "room",
        "file": {"filename": "JAPANESEROOM BAKED.glb",
                 "url": "files/JAPANESEROOM%20BAKED.glb"}}})
    assert os.path.exists(build.path(7))
    assert missing_files(build) == [], "and it is not reported as absent"


def test_a_decoded_basis_is_found_even_when_the_original_was_a_jpeg(tmp_path):
    """Decoding a Basis variant always produces a PNG, whatever the registry calls the original. One
    texture in the capture is named `.jpeg` and its only surviving form is `Hands_diffuse_2K.basis`, so
    the decoded file agrees with the recorded name about everything but the extension — and without
    this it reported as missing right after being successfully decoded."""
    os.makedirs(tmp_path / "files", exist_ok=True)
    (tmp_path / "files" / "hands.png").write_bytes(b"decoded")
    build = Build(root=str(tmp_path), assets={9: {
        "id": "9", "type": "texture", "name": "hands",
        "file": {"filename": "hands.jpeg", "url": "files/hands.jpeg",
                 "variants": {"basis": {"url": "files/hands.basis"}}}}})
    assert build.path(9).endswith("hands.png")
    assert missing_files(build) == [] and variant_only(build) == []


def test_a_name_that_genuinely_contains_a_percent_still_resolves(tmp_path):
    os.makedirs(tmp_path / "files", exist_ok=True)
    (tmp_path / "files" / "100%25.png").write_bytes(b"x")
    build = Build(root=str(tmp_path), assets={8: {
        "id": "8", "type": "texture", "name": "odd",
        "file": {"filename": "100%25.png", "url": "files/100%25.png"}}})
    assert os.path.exists(build.path(8)), "the raw form is tried as a fallback"


def test_the_origin_is_derived_from_the_capture_path(tmp_path):
    base = tmp_path / "api.example.com" / "release" / "abc123"
    base.mkdir(parents=True)
    assert build_origin(str(tmp_path), str(base)) == "https://api.example.com/release/abc123"
    assert build_origin(str(tmp_path), str(tmp_path / "local")) == "", "no host in the path, no guess"


def test_referenced_files_that_are_absent_are_listed_with_their_urls(tmp_path):
    """Akari's build references 105 textures and holds none of them. Reported per USE that was 200-odd
    identical lines burying the two findings that mattered, so it is a list now."""
    root = _build(tmp_path, textures=[(100, "skin.png", "files/assets/1/1/skin.png")])
    build = read_build(root)
    build.origin = "https://api.example.com/release/abc"
    absent = dict(missing_files(build))
    assert absent["skin.png"] == "https://api.example.com/release/abc/files/assets/1/1/skin.png"


def _with_variant(tmp_path, *, write_png, write_basis):
    os.makedirs(tmp_path / "files", exist_ok=True)
    if write_png:
        _png(tmp_path / "files" / "skin.png", "RGB")
    if write_basis:
        (tmp_path / "files" / "skin.basis").write_bytes(b"not really basis")
    asset = {"id": "100", "type": "texture", "name": "skin.png",
             "file": {"filename": "skin.png", "url": "files/skin.png",
                      "variants": {"basis": {"filename": "skin.basis", "url": "files/skin.basis"}}}}
    b = Build(root=str(tmp_path), assets={100: asset})
    b.origin = "https://api.example.com/release/abc"
    return b


def test_a_texture_present_only_as_a_basis_variant_is_not_called_missing(tmp_path):
    """PlayCanvas transcodes textures to Basis Universal and the engine asks for THAT in preference, so
    a capture made by browsing holds `.basis` and never the PNG beside it — 91 of Akari's 105 textures
    declare one. Counting those as absent overstated one capture's gap by eight files."""
    b = _with_variant(tmp_path, write_png=False, write_basis=True)
    assert missing_files(b) == []
    name, on_disk, url = variant_only(b)[0]
    assert (name, on_disk) == ("skin.png", "skin.basis")
    assert url.endswith("/files/skin.png"), "the form we can actually read is what to go and get"


def test_a_texture_absent_in_every_form_is_missing(tmp_path):
    b = _with_variant(tmp_path, write_png=False, write_basis=False)
    assert [n for n, _ in missing_files(b)] == ["skin.png"] and variant_only(b) == []


def test_a_texture_present_as_named_is_neither(tmp_path):
    b = _with_variant(tmp_path, write_png=True, write_basis=True)
    assert missing_files(b) == [] and variant_only(b) == []


def test_a_texture_missing_from_six_materials_is_reported_once(tmp_path):
    build, ids = _tex(tmp_path, gone=("RGB", False))
    os.remove(tmp_path / "files" / "gone.png")
    tex = _Textures(build, 1024, 90)
    for i in range(6):
        material_from({"diffuseMap": ids["gone"]}, f"m{i}", tex)
    assert len([w for w in tex.warnings if "missing from disk" in w]) == 1


# ---------------------------------------------------------------- a capture with no registry
#
# The second capture to arrive had the right directory layout, valid GLBs, and no `config.json`
# anywhere — 74 files, every one untextured. "No PlayCanvas build here" sends you looking at the
# structure, which is fine; what is missing is the registry. Since the capture mirrors the URL it came
# from, the address to fetch is derivable from the path.


def _orphan_tree(tmp_path, rel, n=3):
    d = tmp_path / rel / "files" / "assets" / "220331523" / "1"
    d.mkdir(parents=True)
    for i in range(n):
        (d / f"model{i}.glb").write_bytes(_glb())
    return tmp_path / rel


def test_an_asset_tree_with_no_registry_is_reported_with_where_to_get_it(tmp_path):
    _orphan_tree(tmp_path, "api.example.com/release/abc123")
    orphans = find_orphans(str(tmp_path))
    assert len(orphans) == 1
    o = orphans[0]
    assert o.assets == 3 and o.kinds == {"glb": 3}
    # A path segment with a dot in it is the host; everything between it and `files` is the build.
    assert o.origin == "https://api.example.com/release/abc123/config.json"


def test_a_tree_with_no_host_in_its_path_says_so_rather_than_guessing(tmp_path):
    _orphan_tree(tmp_path, "somewhere")
    assert find_orphans(str(tmp_path))[0].origin == ""


def test_a_proper_build_is_not_reported_as_missing_its_registry(tmp_path):
    _build(tmp_path)                                   # writes config.json AND files/
    assert find_orphans(str(tmp_path)) == []


def test_an_empty_asset_tree_is_not_reported(tmp_path):
    (tmp_path / "files" / "assets").mkdir(parents=True)
    assert find_orphans(str(tmp_path)) == []


def test_the_orphan_report_names_the_url_and_the_scene_it_leads_to(tmp_path):
    _orphan_tree(tmp_path, "api.example.com/release/abc123")
    lines = []
    assert report_orphans(str(tmp_path), lines.append) == 1
    text = "\n".join(lines)
    assert "NO config.json" in text and "https://api.example.com/release/abc123/config.json" in text
    assert "scenes[].url" in text, "config.json alone is not enough; it names the scene"


# ---------------------------------------------------------------- images


def test_a_palette_png_keeps_its_transparency(tmp_path):
    """An indexed PNG carries alpha in a `tRNS` chunk, not a channel. Jane's eyelashes are one, and
    they are nothing but alpha — read as RGB they become a solid rectangle across her face."""
    opaque = load_image(_png(tmp_path / "flat.png", "P"))
    assert opaque.mode == "RGB" and not has_alpha(opaque)
    cut = load_image(_png(tmp_path / "cut.png", "P", transparent=True))
    assert cut.mode == "RGBA" and has_alpha(cut)


def test_alpha_that_is_present_but_never_used_does_not_count(tmp_path):
    """A fully opaque alpha channel is not transparency, and treating it as such costs a PNG where a
    JPEG would do — several times the bytes, for nothing."""
    assert not has_alpha(load_image(_png(tmp_path / "solid.png", "RGBA")))
    assert has_alpha(load_image(_png(tmp_path / "holes.png", "RGBA", transparent=True)))


# ---------------------------------------------------------------- the material translation


def _tex(tmp_path, **files):
    """`(build, {name: asset id})` for a bag of textures written to disk. `files` is `name=(mode, alpha)`."""
    assets = {}
    os.makedirs(tmp_path / "files", exist_ok=True)
    for i, (name, (mode, transparent)) in enumerate(files.items()):
        aid = 100 + i
        _png(tmp_path / "files" / f"{name}.png", mode, transparent=transparent)
        assets[aid] = {"id": str(aid), "type": "texture", "name": f"{name}.png",
                       "file": {"filename": f"{name}.png", "url": f"files/{name}.png"}}
    return Build(root=str(tmp_path), assets=assets), {n: 100 + i for i, n in enumerate(files)}


def test_an_opacity_map_with_no_alpha_leaves_the_material_opaque(tmp_path):
    """The one that punches holes in a figure if believed. Five of Jane's eight materials name an
    opacity map pointing at a texture with no alpha channel at all."""
    build, ids = _tex(tmp_path, diffuse=("RGB", False))
    tex = _Textures(build, 1024, 90)
    m = material_from({"diffuseMap": ids["diffuse"], "opacityMap": ids["diffuse"],
                       "opacityMapChannel": "a", "alphaTest": 0.4, "blendType": BLEND_NONE},
                      "body", tex)
    assert m.get("alphaMode", "OPAQUE") == "OPAQUE"
    assert any("no alpha" in w for w in tex.warnings), "and it says so rather than going quiet"


def test_a_real_cutout_becomes_a_mask_with_its_cutoff(tmp_path):
    build, ids = _tex(tmp_path, lashes=("RGBA", True))
    tex = _Textures(build, 1024, 90)
    m = material_from({"diffuseMap": ids["lashes"], "opacityMap": ids["lashes"],
                       "opacityMapChannel": "a", "alphaTest": 0.399, "blendType": BLEND_NONE},
                      "eyelashes", tex)
    assert m["alphaMode"] == "MASK" and m["alphaCutoff"] == pytest.approx(0.399)


def test_alpha_to_coverage_is_a_cutout_whatever_the_blend_mode_says(tmp_path):
    """Alpha-to-coverage resolves a hard edge through MSAA — it is a CUTOUT, so MASK is the honest
    translation even when the blend mode says premultiplied. One build shares a single atlas between
    shorts, shirt and hair with this mask selecting each garment's region; read as BLEND, the regions
    that should vanish came through as patches of the other garments' colours."""
    build, ids = _tex(tmp_path, atlas=("RGB", False), mask=("RGBA", True))
    tex = _Textures(build, 1024, 90)
    m = material_from({"diffuseMap": ids["atlas"], "opacityMap": ids["mask"],
                       "opacityMapChannel": "r", "alphaToCoverage": True,
                       "alphaTest": 0, "blendType": 4}, "Agnes2", tex)
    assert m["alphaMode"] == "MASK"
    assert m["alphaCutoff"] == pytest.approx(0.5), "a default cutoff, since alphaTest is 0"
    assert not any("alpha to coverage" in w for w in tex.warnings), "carried, so no longer warned about"


def test_blend_normal_becomes_blend(tmp_path):
    build, ids = _tex(tmp_path, glass=("RGBA", True))
    m = material_from({"diffuseMap": ids["glass"], "opacityMap": ids["glass"],
                       "opacityMapChannel": "a", "blendType": BLEND_NORMAL},
                      "glass", _Textures(build, 1024, 90))
    assert m["alphaMode"] == "BLEND"


def test_gloss_invert_decides_whether_the_number_is_roughness(tmp_path):
    """Not a guess: the engine's `glossPS` chunk multiplies scalar by map and THEN applies `1.0 - x`
    under `MAPINVERT`, so an inverted gloss IS roughness. One build uses it both ways — Jane's lips
    (invert, shininess 0) and her mouth (no invert, shininess 90) are both wet, from opposite settings."""
    build, _ = _tex(tmp_path)
    tex = _Textures(build, 1024, 90)
    glossy = material_from({"shininess": 0, "glossInvert": True}, "lips", tex)
    assert glossy["pbrMetallicRoughness"]["roughnessFactor"] == pytest.approx(0.0)
    also = material_from({"shininess": 90, "glossInvert": False}, "mouth", tex)
    assert also["pbrMetallicRoughness"]["roughnessFactor"] == pytest.approx(0.1)
    matte = material_from({"shininess": 100, "glossInvert": True}, "skin", tex)
    assert matte["pbrMetallicRoughness"]["roughnessFactor"] == pytest.approx(1.0)


def test_a_gloss_map_lands_in_the_green_channel(tmp_path):
    """glTF reads roughness from G and metalness from B of one texture, whatever channel PlayCanvas
    was pointing at."""
    build, ids = _tex(tmp_path, orm=("RGB", False))
    tex = _Textures(build, 1024, 90)
    m = material_from({"glossMap": ids["orm"], "glossMapChannel": "g", "glossInvert": True,
                       "metalnessMap": ids["orm"], "metalnessMapChannel": "b", "useMetalness": True,
                       "metalness": 1}, "clothes", tex)
    assert "metallicRoughnessTexture" in m["pbrMetallicRoughness"]
    assert m["pbrMetallicRoughness"]["metallicFactor"] == pytest.approx(1.0)


def test_emissive_intensity_is_folded_into_the_colour(tmp_path):
    """glTF has the emissive colour and no multiplier, so an unlit sign authored at intensity 0.5 comes
    out twice as bright unless the two are combined."""
    build, _ = _tex(tmp_path)
    m = material_from({"emissive": [0.8, 0.4, 0.2], "emissiveIntensity": 0.5}, "sign",
                      _Textures(build, 1024, 90))
    assert m["emissiveFactor"] == pytest.approx([0.4, 0.2, 0.1])


def test_ao_intensity_becomes_occlusion_strength(tmp_path):
    build, ids = _tex(tmp_path, ao=("RGB", False))
    m = material_from({"aoMap": ids["ao"], "aoIntensity": 0.25}, "wall", _Textures(build, 1024, 90))
    assert m["occlusionTexture"]["strength"] == pytest.approx(0.25)


def test_a_second_uv_set_is_carried_as_texcoord(tmp_path):
    """PlayCanvas can read any map off a second UV set; glTF spells it `texCoord`, and carrying it is
    free. Dropping it puts a lightmap on the wrong coordinates, which reads as scrambled texture."""
    build, ids = _tex(tmp_path, d=("RGB", False), n=("RGB", False))
    m = material_from({"diffuseMap": ids["d"], "diffuseMapUv": 1,
                       "normalMap": ids["n"], "normalMapUv": 0}, "wall", _Textures(build, 1024, 90))
    assert m["pbrMetallicRoughness"]["baseColorTexture"]["texCoord"] == 1
    assert "texCoord" not in m["normalTexture"], "UV 0 is the default and says so by omission"


def test_a_feature_switched_off_does_not_warn(tmp_path):
    """The cry-wolf lesson, and it is measured. PlayCanvas leaves `sheen` at a default WHITE with
    `useSheen: false`, so a warning keyed on the colour fires for all 208 materials across the three
    builds this was written against — and a warning that always fires is one nobody reads. Gate on the
    `use*` flag. `useDynamicRefraction` is true exactly ONCE in those 208, on Jane's eyes, which is the
    case the whole list exists to surface."""
    build, _ = _tex(tmp_path)
    quiet = _Textures(build, 1024, 90)
    material_from({"sheen": [1, 1, 1], "useSheen": False, "useIridescence": False}, "skin", quiet)
    assert not quiet.warnings

    loud = _Textures(build, 1024, 90)
    material_from({"sheen": [1, 1, 1], "useSheen": True, "useDynamicRefraction": True}, "eyes", loud)
    assert any("sheen" in w for w in loud.warnings)
    assert any("refraction" in w and "opaque" in w for w in loud.warnings)


def test_a_tiled_or_rotated_map_is_reported_rather_than_dropped(tmp_path):
    build, ids = _tex(tmp_path, d=("RGB", False))
    tex = _Textures(build, 1024, 90)
    material_from({"diffuseMap": ids["d"], "diffuseMapTiling": [0.1, 0.47],
                   "normalMapRotation": 180}, "bar", tex)
    assert any("KHR_texture_transform" in w for w in tex.warnings)
    assert any("diffuse" in w and "normal" in w for w in tex.warnings), "listed together, once"


def test_culling_off_means_double_sided(tmp_path):
    build, _ = _tex(tmp_path)
    tex = _Textures(build, 1024, 90)
    assert material_from({"cull": 0}, "a", tex).get("doubleSided") is True
    assert material_from({"cull": 1}, "b", tex).get("doubleSided") is None


def test_a_setting_that_does_nothing_is_still_transcribed(tmp_path):
    """Faithful rather than tasteful. Jane's skin names a normal map at `bumpMapFactor: 0`; keeping it
    renders identically, and 'fixing' it would be this tool overruling the author in a file nobody
    re-reads."""
    build, ids = _tex(tmp_path, n=("RGB", False))
    m = material_from({"normalMap": ids["n"], "bumpMapFactor": 0}, "skin", _Textures(build, 1024, 90))
    assert m["normalTexture"]["scale"] == 0


# ---------------------------------------------------------------- assembly


def test_rebuild_writes_materials_onto_the_right_primitives(tmp_path):
    root = _build(tmp_path)
    build = read_build(root)
    out, notes = rebuild(_glb(), build.bindings, build)
    doc, _ = split_glb(out)
    assert [p.get("material") for p in doc["meshes"][0]["primitives"]] == [0]
    assert [p.get("material") for p in doc["meshes"][1]["primitives"]] == [1, 1, 1]
    assert [m["name"] for m in doc["materials"]] == ["skin", "hair"]


def test_the_geometry_comes_out_untouched(tmp_path):
    """Additive by construction: the images are appended as new buffer views and nothing that was in
    the file moves. It is what lets a bone map derived from the original still apply."""
    root = _build(tmp_path)
    before, blob_before = split_glb(_glb())
    out, _ = rebuild(_glb(), read_build(root).bindings, read_build(root))
    after, blob_after = split_glb(out)
    assert after["nodes"] == before["nodes"] and after["scenes"] == before["scenes"]
    assert blob_after[:len(blob_before)] == blob_before
    assert after["accessors"] if "accessors" in before else True
    assert after["bufferViews"][0] == before["bufferViews"][0]


def test_the_written_glb_is_spec_shaped(tmp_path):
    """Chunks 4-aligned, the header length honest, and `buffers[0].byteLength` within 3 bytes of the
    BIN chunk — the padding rule that a hand-rolled writer gets wrong once and nobody notices until a
    strict loader refuses the file."""
    root = _build(tmp_path)
    build = read_build(root)
    data, _ = rebuild(_glb(), build.bindings, build)
    assert data[:4] == b"glTF"
    assert struct.unpack("<I", data[8:12])[0] == len(data)
    off, chunks = 12, []
    while off < len(data) - 8:
        ln, kind = struct.unpack("<II", data[off:off + 8])
        chunks.append((kind, ln))
        off += 8 + ln
    assert all(ln % 4 == 0 for _, ln in chunks)
    doc, blob = split_glb(data)
    assert 0 <= len(blob) - doc["buffers"][0]["byteLength"] < 4
    assert all(bv.get("byteOffset", 0) + bv["byteLength"] <= doc["buffers"][0]["byteLength"]
               for bv in doc["bufferViews"])


def test_an_unbound_mesh_is_reported_for_what_it_actually_is(tmp_path):
    """It comes out GREY, and the report has to say so. The scene does not render such a mesh at all,
    so untextured is the one outcome the source never produces — an earlier wording claimed the
    opposite, which reads as reassurance. On Jane that grey mesh is her hair."""
    root = _build(tmp_path, renders=[(20, "body", 10, 0), (21, "hair", 10, 1)],
                  entities=[("body", 20, [30])])                    # nothing binds mesh 1
    build = read_build(root)
    _out, notes = rebuild(_glb(), build.bindings, build)
    note = next(n for n in notes if "UNTEXTURED" in n)
    assert "[1]" in note and "--adopt" in note
    assert "what the scene does" not in note


def test_a_mesh_the_file_does_not_have_is_reported_not_raised(tmp_path):
    root = _build(tmp_path)
    build = read_build(root)
    _out, notes = rebuild(_glb(meshes=(1,)), build.bindings, build)      # only one mesh in the file
    assert any("does not have" in n for n in notes)


def test_a_container_nothing_binds_produces_nothing(tmp_path):
    build = Build(root=str(tmp_path), assets={})
    out, notes = rebuild(_glb(), [], build)
    assert out == _glb() and "nothing to bind" in notes


def test_by_container_groups_what_rebuild_needs(tmp_path):
    build = read_build(_build(tmp_path))
    groups = by_container(build)
    assert set(groups) == {10} and len(groups[10]) == 2
