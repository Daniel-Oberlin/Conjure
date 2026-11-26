"""Re-assembling a Unity player build (`conjure/unity.py`).

The claims worth holding:

  1. the handedness conversion is exact and self-consistent — mirrored positions, reversed winding and
     mirrored normals agree, so faces point outward and nothing renders inside-out;
  2. Unity's padded half-float streams (a float16 normal stored as 4 components) come out 3-wide, with
     every value bit-identical;
  3. the material translation reads smoothness from where `UnityStandardInput.cginc` reads it, and the
     specular setup lands on F0 = the spec map;
  4. the written GLB is a GLB.

The fixture is synthetic: a real build is hundreds of MB and the structures that matter are a few
arrays and dicts. `CONJURE_UNITY_SAMPLE=<Data dir>` additionally runs the converter on a real build.
"""

import os

import pytest

np = pytest.importorskip("numpy")

from conjure.figures import split_glb                                                            # noqa: E402
from conjure.unity import (MaterialSource, TextureSink, _Glb, _Images, convert_triangles,        # noqa: E402
                           convert_trs, convert_vertices, material_from, matmul, srgb_to_linear,
                           trs_matrix)

C = [[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]


# ---------------------------------------------------------------- geometry


def test_mirrored_triangle_faces_the_way_its_normal_says():
    # Unity's RecalculateNormals is cross(b - a, c - a) on its clockwise front faces.
    a, b, c = np.array([0, 0, 0.0]), np.array([0, 1, 0.0]), np.array([1, 0, 0.0])
    n = np.cross(b - a, c - a)
    pos, nrm, _t, _uv = convert_vertices(np.stack([a, b, c]), np.stack([n, n, n]))
    (i, j, k), = convert_triangles([[0, 1, 2]])
    face = np.cross(pos[j] - pos[i], pos[k] - pos[i])            # glTF front face is counter-clockwise
    assert np.allclose(face / np.linalg.norm(face), nrm[0])
    assert list(convert_triangles([[0, 1, 2], [3, 4, 5]]).ravel()) == [0, 2, 1, 3, 5, 4]


def test_tangent_handedness_flips_with_the_mirror():
    _p, _n, tan, _uv = convert_vertices([[0, 0, 0]], [[0, 0, 1]], [[1, 0, 0, 1]])
    assert tan.tolist() == [[-1.0, 0.0, 0.0, -1.0]]


def test_uv_v_is_flipped():
    _p, _n, _t, (uv,) = convert_vertices([[0, 0, 0], [0, 0, 0]], uvs=[[[0.25, 0.0], [0.5, 0.75]]])
    assert uv.tolist() == [[0.25, 1.0], [0.5, 0.25]]


def test_padded_half_float_streams_are_sliced_and_exact():
    halves = np.array([[0.1, -0.7, 0.33, 0.0], [1e-3, 0.5, -0.999, 0.0]], dtype=np.float16)
    pos, nrm, _t, (uv,) = convert_vertices(np.zeros((2, 3)), halves.astype(np.float32),
                                           uvs=[halves.astype(np.float32)])
    assert nrm.shape == (2, 3) and uv.shape == (2, 2)
    expect = halves.astype(np.float32)[:, :3].copy()
    expect[:, 0] = -expect[:, 0]
    assert np.array_equal(nrm, expect)                          # bit-exact, not merely close
    assert nrm.dtype == np.float32


def test_node_transform_is_the_mirrored_matrix():
    t, r, s = (1.5, -2.0, 3.0), (0.1, 0.7, -0.2, 0.68), (1.0, 2.0, 0.5)
    norm = sum(x * x for x in r) ** 0.5
    r = tuple(x / norm for x in r)
    want = matmul(matmul(C, trs_matrix(t, r, s)), C)
    got = trs_matrix(*convert_trs(t, r, s))
    assert np.allclose(np.array(got), np.array(want))


# ---------------------------------------------------------------- materials


class Recorder(TextureSink):
    def __init__(self):
        self.calls = []

    def _rec(self, *args, **kw):
        self.calls.append((args, kw))
        return len(self.calls) - 1

    def base(self, key, *, keep_alpha):
        return self._rec("base", key, keep_alpha=keep_alpha)

    def normal(self, key):
        return self._rec("normal", key)

    def emissive(self, key):
        return self._rec("emissive", key)

    def occlusion(self, key):
        return self._rec("occlusion", key)

    def specular_color(self, key):
        return self._rec("spec", key)

    def metal_rough(self, **kw):
        return self._rec("mr", **kw)


ID = ((1.0, 1.0), (0.0, 0.0))


def _src(shader="Standard", **kw):
    return MaterialSource(name="m", shader=shader, **kw)


def test_specular_setup_puts_f0_on_the_spec_map():
    sink = Recorder()
    m, _ = material_from(_src("Standard (Specular setup)",
                              textures={"_MainTex": ("albedo", *ID), "_SpecGlossMap": ("sg", *ID)},
                              floats={"_GlossMapScale": 0.5}, keywords=frozenset({"_SPECGLOSSMAP"})), sink)
    spec = m["extensions"]["KHR_materials_specular"]
    assert spec["specularColorFactor"] == [25.0, 25.0, 25.0]     # 0.04 * 25 = 1: F0 is the texture
    assert m["pbrMetallicRoughness"]["metallicFactor"] == 0.0
    mr = [c for c in sink.calls if c[0] == ("mr",)][0][1]
    assert mr["gloss_key"] == "sg" and mr["gloss_scale"] == 0.5 and mr["metal_key"] is None


def test_smoothness_from_albedo_alpha_when_the_keyword_says_so():
    sink = Recorder()
    material_from(_src(textures={"_MainTex": ("albedo", *ID), "_MetallicGlossMap": ("mg", *ID)},
                       keywords=frozenset({"_METALLICGLOSSMAP", "_SMOOTHNESS_TEXTURE_ALBEDO_CHANNEL_A"})), sink)
    mr = [c for c in sink.calls if c[0] == ("mr",)][0][1]
    assert mr["gloss_key"] == "albedo" and mr["metal_key"] == "mg"
    base = [c for c in sink.calls if c[0][0] == "base"][0]
    assert base[1]["keep_alpha"] is True                         # the alpha is data, not opacity


def test_constant_smoothness_without_maps():
    m, _ = material_from(_src(floats={"_Glossiness": 0.8, "_Metallic": 0.25}), Recorder())
    pbr = m["pbrMetallicRoughness"]
    assert pbr["roughnessFactor"] == pytest.approx(0.2) and pbr["metallicFactor"] == 0.25


def test_alpha_modes():
    m, _ = material_from(_src(floats={"_Mode": 1, "_Cutoff": 0.3}, keywords=frozenset({"_ALPHATEST_ON"})), Recorder())
    assert (m["alphaMode"], m["alphaCutoff"]) == ("MASK", 0.3)
    m, _ = material_from(_src(floats={"_Mode": 3}), Recorder())
    assert m["alphaMode"] == "BLEND"
    m, _ = material_from(_src(), Recorder())
    assert "alphaMode" not in m


def test_emission_needs_the_keyword_and_a_colour():
    hot = {"_EmissionColor": (2.0, 1.0, 0.0, 1.0)}
    m, _ = material_from(_src(colors=hot), Recorder())
    assert "emissiveFactor" not in m                             # Standard without _EMISSION: off
    m, _ = material_from(_src(colors=hot, keywords=frozenset({"_EMISSION"})), Recorder())
    strength = m["extensions"]["KHR_materials_emissive_strength"]["emissiveStrength"]
    assert strength == pytest.approx(srgb_to_linear(2.0))
    assert m["emissiveFactor"][0] == pytest.approx(1.0)
    m, _ = material_from(_src(colors={"_EmissionColor": (0, 0, 0, 1)}, keywords=frozenset({"_EMISSION"})), Recorder())
    assert "emissiveFactor" not in m


def test_colours_are_linearised():
    m, _ = material_from(_src(colors={"_Color": (0.5, 0.5, 0.5, 0.5)}), Recorder())
    assert m["pbrMetallicRoughness"]["baseColorFactor"] == pytest.approx([0.2140, 0.2140, 0.2140, 0.5], abs=1e-4)


def test_main_tex_st_becomes_a_texture_transform_in_flipped_v():
    m, _ = material_from(_src(textures={"_MainTex": ("albedo", (2.0, 4.0), (0.1, 0.25))}), Recorder())
    tt = m["pbrMetallicRoughness"]["baseColorTexture"]["extensions"]["KHR_texture_transform"]
    assert tt["scale"] == [2.0, 4.0] and tt["offset"] == pytest.approx([0.1, 1.0 - 4.0 - 0.25])


def test_standard_derived_custom_shader_translates_as_standard():
    m, notes = material_from(_src("VROOM/StandardOffset", floats={"_Mode": 1}, keywords=frozenset({"_ALPHATEST_ON"})),
                             Recorder())
    assert m["alphaMode"] == "MASK" and any("translated as Standard" in n for n in notes)


def test_unknown_shader_glows_its_texture_not_a_flat_colour():
    sink = Recorder()
    m, notes = material_from(_src("VROOM/AlphaMask", textures={"_MainTex": ("lights", *ID)},
                                  colors={"_EmissionColor": (1, 1, 1, 1)}), sink)
    assert "emissiveTexture" in m and ("emissive", "lights") in [c[0] for c in sink.calls]
    assert any("not Standard" in n for n in notes)


# ---------------------------------------------------------------- textures and the container


def test_roughness_is_one_minus_scaled_gloss_alpha():
    from PIL import Image
    img = Image.new("RGBA", (2, 1))
    img.putdata([(10, 20, 30, 0), (10, 20, 30, 200)])
    glb = _Glb()
    sink = _Images(glb, lambda k: ("sg", img), max_texture=0, lossless=True)
    sink.metal_rough(metal_key=None, gloss_key="sg", gloss_channel="a", gloss_scale=0.5, metallic=0.0)
    doc, blob = split_glb(glb.finish())
    view = doc["bufferViews"][doc["images"][0]["bufferView"]]
    import io
    out = Image.open(io.BytesIO(blob[view["byteOffset"]:view["byteOffset"] + view["byteLength"]]))
    assert [out.getpixel((x, 0))[1] for x in range(2)] == [255, 155]   # G = 255 - a * 0.5
    assert [out.getpixel((x, 0))[2] for x in range(2)] == [0, 0]       # B = metallic


def test_written_glb_round_trips():
    glb = _Glb()
    pos = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
    acc = glb.accessor(pos, "VEC3", bounds=True)
    idx = glb.accessor(np.array([0, 2, 1], dtype=np.uint16).reshape(-1, 1), "SCALAR", target=34963)
    glb.doc["meshes"].append({"primitives": [{"attributes": {"POSITION": acc}, "indices": idx}]})
    glb.doc["nodes"].append({"mesh": 0})
    glb.doc["scenes"][0]["nodes"] = [0]
    doc, blob = split_glb(glb.finish())
    assert doc["accessors"][0]["max"] == [1.0, 1.0, 0.0]
    assert len(blob) >= doc["buffers"][0]["byteLength"]
    assert all(v["byteOffset"] % 4 == 0 for v in doc["bufferViews"])


# ---------------------------------------------------------------- a real build (opt-in)


@pytest.mark.skipif(not os.environ.get("CONJURE_UNITY_SAMPLE"), reason="set CONJURE_UNITY_SAMPLE=<Data dir>")
def test_real_build_converts(tmp_path):
    pytest.importorskip("UnityPy")
    from conjure.unity import rebuild_build
    lines = []
    written = rebuild_build(os.environ["CONJURE_UNITY_SAMPLE"], str(tmp_path),
                            under=os.environ.get("CONJURE_UNITY_UNDER", ""), max_texture=256, report=lines.append)
    assert written
    for path in written:
        doc, blob = split_glb(open(path, "rb").read())
        assert doc and doc["meshes"] and blob
