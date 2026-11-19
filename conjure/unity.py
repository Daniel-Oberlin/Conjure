"""Re-assemble a Unity player build into self-contained, textured GLBs.

Spec: docs/specs/captures.md §6 (Unity builds).

A Unity player build (an Android APK's `assets/bin/Data`, or a desktop `*_Data` folder) keeps its
scene, meshes, materials and textures as serialized engine objects — no glTF, no FBX, nothing an
ordinary viewer opens. Like `conjure.playcanvas`, this is a TRANSCRIPTION, not a reconstruction: the
scene states which mesh each renderer draws and which material sits on each submesh, so nothing here
guesses at bindings. What it does have to TRANSLATE is the material model (Unity's Standard shader
to glTF metallic-roughness), and every place that translation is approximate is reported.

**What is exact, and what is not.**

- Geometry is bit-exact. Positions are float32 in Unity and in glTF; normals, tangents and UVs are
  often float16 in Unity, which glTF cannot store, so they widen to float32 — every half-float value is
  exactly representable, so nothing is lost but bytes. Submeshes become primitives that share one set
  of vertex accessors, in the renderer's material order.
- Handedness: Unity is left-handed (+Z forward), glTF right-handed. X is negated in positions, normals,
  tangents and node transforms, tangent `w` flips with it, and triangle winding is reversed — all exact.
- UVs: Unity's V runs bottom-up, glTF's top-down, so `v -> 1 - v` (exact in float32 for [0,1]-ish
  values to within the format's own spacing; UnityPy decodes images upright to match).
- Textures are written exactly as the build decodes them. They were usually ASTC/ETC2 on device —
  already lossy — and the originals are not in the build. `--lossless` keeps them full size as PNG;
  the default is the same 1024 px budget `playcanvas.py` uses (see its `fit`).
- Materials are a TRANSLATION. Standard (metallic) maps exactly onto glTF PBR; Standard (Specular
  setup) maps onto `KHR_materials_specular` with metallic 0, which is close but not Unity's energy
  conservation; anything else falls back to colour + texture + emission, with a note.

Script-driven behaviour (spinning parts, pulsing lights) and audio are not in a GLB and are reported,
not invented.

UnityPy is an optional dependency (`pip install -e ".[unity]"`), imported only when a build is read.
"""

from __future__ import annotations

import io
import math
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .figures import write_glb

GENERATOR = "conjure.unity"

# Unity serializes material colours in gamma space and converts them when the project renders in
# linear; glTF factors are linear. Textures need no conversion — their bytes are sampled the same way.


def srgb_to_linear(c: float) -> float:
    c = max(0.0, c)
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


# ---------------------------------------------------------------- geometry: Unity -> glTF space


def convert_vertices(positions, normals=None, tangents=None, uvs=()):
    """Mirror vertex streams from Unity's left-handed space into glTF's right-handed one.

    Arrays are numpy float32 `(n, k)`; returns new float32 arrays. Every operation is a sign flip or
    `1 - v`, so the result is exact for the values the build stores.
    """
    import numpy as np                                          # noqa: PLC0415

    def streams(a, width: int):
        # Unity pads half-float streams to 4 components for alignment (a float16 normal is stored
        # xyz + pad), so a stream's stored width can exceed the attribute's. Shape by vertex count.
        a = np.asarray(a, dtype=np.float32).reshape(n, -1)
        if a.shape[1] < width:
            raise ValueError(f"vertex stream has {a.shape[1]} components, need {width}")
        return np.ascontiguousarray(a[:, :width], dtype=np.float32).copy()

    pos = np.asarray(positions, dtype=np.float32)
    n = pos.size // 3 if pos.ndim == 1 else pos.shape[0]
    pos = streams(pos, 3)
    pos[:, 0] = -pos[:, 0]
    nrm = None
    if normals is not None and len(normals):
        nrm = streams(normals, 3)
        nrm[:, 0] = -nrm[:, 0]
    tan = None
    if tangents is not None and len(tangents):
        tan = streams(tangents, 4)
        tan[:, 0] = -tan[:, 0]
        tan[:, 3] = -tan[:, 3]                                  # mirroring flips the bitangent's handedness
    out_uvs = []
    for uv in uvs:
        a = streams(uv, 2)
        a[:, 1] = np.float32(1.0) - a[:, 1]
        out_uvs.append(a)
    return pos, nrm, tan, out_uvs


def convert_triangles(triangles):
    """Reverse winding: (a, b, c) -> (a, c, b). Mirroring alone turns Unity's clockwise front faces
    into glTF's counter-clockwise ones *as seen*, but the mirrored camera sees them reversed again."""
    import numpy as np                                          # noqa: PLC0415
    t = np.asarray(triangles, dtype=np.uint32).reshape(-1, 3)
    return np.ascontiguousarray(t[:, [0, 2, 1]])


def convert_trs(position, rotation, scale):
    """A Unity local transform (Vector3, Quaternion xyzw, Vector3) as glTF TRS."""
    px, py, pz = position
    qx, qy, qz, qw = rotation
    return [-px, py, pz], [qx, -qy, -qz, qw], list(scale)


def trs_matrix(t, r, s):
    """4x4 row-major matrix of a TRS (any handedness; it is just the composition)."""
    x, y, z, w = r
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz, wx, wy, wz = x * y, x * z, y * z, w * x, w * y, w * z
    rot = [[1 - 2 * (yy + zz), 2 * (xy - wz), 2 * (xz + wy)],
           [2 * (xy + wz), 1 - 2 * (xx + zz), 2 * (yz - wx)],
           [2 * (xz - wy), 2 * (yz + wx), 1 - 2 * (xx + yy)]]
    m = [[rot[i][j] * s[j] for j in range(3)] + [t[i]] for i in range(3)]
    return m + [[0.0, 0.0, 0.0, 1.0]]


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


# ---------------------------------------------------------------- the material translation


@dataclass
class MaterialSource:
    """A Unity material reduced to what the translation reads. `textures` maps a slot (`_MainTex`) to
    `(texture key, (scale u, v), (offset u, v))`; keys are opaque to the translation."""
    name: str
    shader: str
    textures: dict = field(default_factory=dict)
    floats: dict = field(default_factory=dict)
    colors: dict = field(default_factory=dict)              # slot -> (r, g, b, a), gamma space
    keywords: frozenset = frozenset()
    render_queue: int = -1


class TextureSink:
    """Where the translation sends textures. The real one encodes images into the GLB; tests record."""

    def base(self, key, *, keep_alpha: bool) -> int: raise NotImplementedError
    def normal(self, key) -> int: raise NotImplementedError
    def emissive(self, key) -> int: raise NotImplementedError
    def occlusion(self, key) -> int: raise NotImplementedError
    def specular_color(self, key) -> int: raise NotImplementedError
    def metal_rough(self, *, metal_key, gloss_key, gloss_channel: str, gloss_scale: float,
                    metallic: float) -> int: raise NotImplementedError


def _tex_info(index: int, st) -> dict:
    """A glTF textureInfo, with `KHR_texture_transform` when Unity's `_MainTex_ST` is not identity.

    Unity samples `uv * scale + offset` in its bottom-up V; in glTF's flipped V that is the same scale
    with offset `(ox, 1 - sy - oy)`.
    """
    info = {"index": index}
    if st:
        (sx, sy), (ox, oy) = st
        if (sx, sy, ox, oy) != (1.0, 1.0, 0.0, 0.0):
            info["extensions"] = {"KHR_texture_transform": {"offset": [ox, 1.0 - sy - oy], "scale": [sx, sy]}}
    return info


def material_from(src: MaterialSource, sink: TextureSink) -> tuple[dict, list[str]]:
    """Translate one Unity material to a glTF material. Returns `(material, notes)`.

    Follows `UnityStandardInput.cginc` for where smoothness comes from — albedo alpha when
    `_SMOOTHNESS_TEXTURE_ALBEDO_CHANNEL_A`, else the gloss map's alpha, each times `_GlossMapScale`;
    `_Glossiness` when there is no map — because a wrong guess here reads as a lighting problem, not a
    material one. glTF roughness is perceptual, as Unity's `1 - smoothness` is, so that part is exact.
    """
    notes: list[str] = []
    kw, f, c, t = src.keywords, src.floats, src.colors, src.textures
    shader = src.shader or "?"
    # Custom shaders built on Standard (e.g. "VROOM/StandardOffset", Standard plus a depth offset for
    # decals) carry Standard's properties and keywords, so they translate the same way.
    leaf = shader.rsplit("/", 1)[-1]
    standard = leaf.startswith("Standard")
    specular_setup = leaf == "Standard (Specular setup)"
    if standard and "/" in shader:                               # built-in Standard has no path
        notes.append(f"{src.name}: '{shader}' translated as Standard — anything it adds (depth offset, "
                     f"masks) is not in glTF")
    mat: dict = {"name": src.name}
    pbr: dict = {}
    ext: dict = {}
    st = t["_MainTex"][1:] if t.get("_MainTex") else None    # Standard applies _MainTex_ST to every map

    # alpha mode
    mode = int(f.get("_Mode", 0)) if standard else 0
    if "_ALPHATEST_ON" in kw or mode == 1:
        mat["alphaMode"] = "MASK"
        mat["alphaCutoff"] = float(f.get("_Cutoff", 0.5))
    elif ({"_ALPHABLEND_ON", "_ALPHAPREMULTIPLY_ON"} & kw) or mode in (2, 3) or "Transparent" in shader:
        mat["alphaMode"] = "BLEND"
    if "_ALPHAPREMULTIPLY_ON" in kw:
        notes.append(f"{src.name}: premultiplied transparency rendered as ordinary blending")
    if f.get("_Cull", 2) == 0:
        mat["doubleSided"] = True

    # base colour
    col = c.get("_Color", (1.0, 1.0, 1.0, 1.0))
    factor = [srgb_to_linear(col[0]), srgb_to_linear(col[1]), srgb_to_linear(col[2]), float(col[3])]
    if factor != [1.0, 1.0, 1.0, 1.0]:
        pbr["baseColorFactor"] = factor
    albedo_alpha_gloss = "_SMOOTHNESS_TEXTURE_ALBEDO_CHANNEL_A" in kw
    main = t.get("_MainTex")
    if main:
        keep_alpha = "alphaMode" in mat or albedo_alpha_gloss
        pbr["baseColorTexture"] = _tex_info(sink.base(main[0], keep_alpha=keep_alpha), st)

    gloss_scale = float(f.get("_GlossMapScale", 1.0))
    if standard:
        if specular_setup:
            spec = t.get("_SpecGlossMap") if "_SPECGLOSSMAP" in kw or "_SpecGlossMap" in t else None
            pbr["metallicFactor"] = 0.0
            s_ext: dict = {}
            if spec:
                # F0 = 0.04 * specularColorFactor * texture for the default ior 1.5, so 25 makes F0 the
                # texture itself — Unity's specular setup stores F0 directly.
                s_ext["specularColorTexture"] = _tex_info(sink.specular_color(spec[0]), st)
                s_ext["specularColorFactor"] = [25.0, 25.0, 25.0]
            else:
                sc = c.get("_SpecColor", (0.2, 0.2, 0.2, 1.0))
                s_ext["specularColorFactor"] = [25.0 * srgb_to_linear(x) for x in sc[:3]]
            ext["KHR_materials_specular"] = s_ext
            gloss_key = main[0] if albedo_alpha_gloss and main else (spec[0] if spec else None)
            metal_key = None
            metallic = 0.0
        else:
            mg = t.get("_MetallicGlossMap") if "_METALLICGLOSSMAP" in kw or "_MetallicGlossMap" in t else None
            metal_key = mg[0] if mg else None
            metallic = 1.0 if mg else float(f.get("_Metallic", 0.0))
            gloss_key = main[0] if albedo_alpha_gloss and main else (mg[0] if mg else None)
            pbr["metallicFactor"] = 1.0 if mg else metallic
        if gloss_key is not None or metal_key is not None:
            pbr["metallicRoughnessTexture"] = _tex_info(sink.metal_rough(
                metal_key=metal_key, gloss_key=gloss_key, gloss_channel="a",
                gloss_scale=gloss_scale if gloss_key is not None else 0.0,
                metallic=metallic), st)
            pbr["roughnessFactor"] = 1.0
            if gloss_key is None:                                # metal map without gloss: constant smoothness
                pbr["roughnessFactor"] = 1.0 - float(f.get("_Glossiness", 0.5))
        else:
            pbr["roughnessFactor"] = 1.0 - float(f.get("_Glossiness", 0.5))
    else:
        # Legacy / custom shaders: no PBR inputs to read. Blinn-Phong shininess -> a rough equivalent.
        pbr["metallicFactor"] = 0.0
        if "_Shininess" in f:
            shin = max(1e-3, float(f["_Shininess"])) * 128.0
            pbr["roughnessFactor"] = round(math.sqrt(2.0 / (shin + 2.0)), 4)
        else:
            pbr["roughnessFactor"] = 1.0
        notes.append(f"{src.name}: shader '{shader}' is not Standard — colour, texture and emission only")

    # normal map
    bump = t.get("_BumpMap")
    if bump and (not standard or "_NORMALMAP" in kw):
        info = {"index": sink.normal(bump[0])}
        scale = float(f.get("_BumpScale", 1.0))
        if scale != 1.0:
            info["scale"] = scale
        mat["normalTexture"] = info | {k: v for k, v in _tex_info(0, st).items() if k == "extensions"}

    # occlusion (Unity reads G; glTF reads R — the sink moves it)
    occ = t.get("_OcclusionMap")
    if occ:
        info = {"index": sink.occlusion(occ[0])}
        strength = float(f.get("_OcclusionStrength", 1.0))
        if strength != 1.0:
            info["strength"] = strength
        mat["occlusionTexture"] = info

    # emission: Standard needs the keyword; a black colour means off either way
    ec = c.get("_EmissionColor")
    emit_on = ("_EMISSION" in kw) if standard else ec is not None
    if emit_on and ec is not None:
        lin = [srgb_to_linear(x) for x in ec[:3]]
        peak = max(lin)
        if peak > 0.0:
            if peak > 1.0:
                ext["KHR_materials_emissive_strength"] = {"emissiveStrength": peak}
                lin = [x / peak for x in lin]
            mat["emissiveFactor"] = lin
            em = t.get("_EmissionMap")
            if em:
                mat["emissiveTexture"] = _tex_info(sink.emissive(em[0]), st)
            elif not standard and main:
                # A non-Standard shader with an emission colour and no emission map (light strips,
                # self-illuminated decals): glowing the colour alone would light the whole surface
                # flat; the base texture is the nearer reading of what glows.
                mat["emissiveTexture"] = _tex_info(sink.emissive(main[0]), st)
                notes.append(f"{src.name}: emission taken from the base texture (shader has no emission map)")

    for slot in ("_DetailAlbedoMap", "_DetailNormalMap", "_ParallaxMap", "_AlphaTex"):
        if t.get(slot):
            notes.append(f"{src.name}: {slot} has no glTF equivalent and was dropped")

    mat["pbrMetallicRoughness"] = pbr
    if ext:
        mat["extensions"] = ext
    return mat, notes


# ---------------------------------------------------------------- the GLB being written


class _Glb:
    """Buffer views, accessors, images and the rest of one output document."""

    def __init__(self):
        self.doc: dict = {"asset": {"version": "2.0", "generator": GENERATOR},
                          "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [],
                          "materials": [], "accessors": [], "bufferViews": [], "buffers": [],
                          "images": [], "textures": [], "samplers": []}
        self.blob = bytearray()
        self.used: set[str] = set()

    def view(self, data: bytes, target: Optional[int] = None) -> int:
        self.blob += b"\x00" * (-len(self.blob) % 4)
        bv = {"buffer": 0, "byteOffset": len(self.blob), "byteLength": len(data)}
        if target:
            bv["target"] = target
        self.blob += data
        self.doc["bufferViews"].append(bv)
        return len(self.doc["bufferViews"]) - 1

    def accessor(self, array, kind: str, *, bounds: bool = False, target: int = 34962) -> int:
        import numpy as np                                      # noqa: PLC0415
        comp = {np.dtype(np.float32): 5126, np.dtype(np.uint32): 5125,
                np.dtype(np.uint16): 5123, np.dtype(np.uint8): 5121}[array.dtype]
        acc = {"bufferView": self.view(np.ascontiguousarray(array).tobytes(), target),
               "componentType": comp, "count": int(array.shape[0]), "type": kind}
        if bounds:
            acc["min"] = [float(x) for x in array.min(axis=0)]
            acc["max"] = [float(x) for x in array.max(axis=0)]
        self.doc["accessors"].append(acc)
        return len(self.doc["accessors"]) - 1

    def image(self, data: bytes, mime: str, name: str) -> int:
        self.doc["images"].append({"bufferView": self.view(data), "mimeType": mime, "name": name})
        if not self.doc["samplers"]:
            self.doc["samplers"].append({"magFilter": 9729, "minFilter": 9987, "wrapS": 10497, "wrapT": 10497})
        self.doc["textures"].append({"source": len(self.doc["images"]) - 1, "sampler": 0})
        return len(self.doc["textures"]) - 1

    def finish(self) -> bytes:
        doc = {k: v for k, v in self.doc.items() if v != []}
        doc["buffers"] = [{"byteLength": len(self.blob)}]
        used = set(self.used)
        for m in doc.get("materials", []):
            used.update((m.get("extensions") or {}).keys())
            for v in list(m.values()) + list((m.get("pbrMetallicRoughness") or {}).values()) \
                    + list(((m.get("extensions") or {}).get("KHR_materials_specular") or {}).values()):
                if isinstance(v, dict) and "extensions" in v:
                    used.update(v["extensions"].keys())
        if used:
            doc["extensionsUsed"] = sorted(used)
        return write_glb(doc, bytes(self.blob))


class _Images(TextureSink):
    """The real sink: decodes Unity textures (via a lookup), derives channel-packed maps, encodes."""

    def __init__(self, glb: _Glb, lookup: Callable, *, max_texture: int, lossless: bool, quality: int = 90):
        self.glb, self.lookup = glb, lookup
        self.max_texture, self.lossless, self.quality = max_texture, lossless, quality
        self.cache: dict = {}
        self.decoded: dict = {}

    def _img(self, key):
        if key not in self.decoded:
            name, img = self.lookup(key)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGBA")
            self.decoded[key] = (name, img)
        return self.decoded[key]

    def _put(self, cache_key, img, name: str) -> int:
        if cache_key in self.cache:
            return self.cache[cache_key]
        from .playcanvas import encode, fit                      # noqa: PLC0415  (shared texture budget)
        if not self.lossless and self.max_texture:
            img = fit(img, self.max_texture)
        if self.lossless:
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=False, compress_level=6)
            data, mime = buf.getvalue(), "image/png"
        else:
            data, mime = encode(img, quality=self.quality)
        idx = self.glb.image(data, mime, name)
        self.cache[cache_key] = idx
        return idx

    def base(self, key, *, keep_alpha: bool) -> int:
        name, img = self._img(key)
        if not keep_alpha and img.mode == "RGBA" and not self.lossless:
            img = img.convert("RGB")
        return self._put(("base", key, keep_alpha or self.lossless), img, name)

    def normal(self, key) -> int:
        name, img = self._img(key)
        r = img.getchannel("R")
        if img.mode == "RGBA" and r.getextrema()[0] == r.getextrema()[1] and \
                img.getchannel("A").getextrema()[0] != img.getchannel("A").getextrema()[1]:
            img = _unpack_dxt5nm(img)                            # x in A, y in G (Unity's "DXT5nm" encoding)
        return self._put(("normal", key), img.convert("RGB"), name)

    def emissive(self, key) -> int:
        name, img = self._img(key)
        return self._put(("emissive", key), img.convert("RGB"), name)

    def specular_color(self, key) -> int:
        name, img = self._img(key)
        return self._put(("spec", key), img.convert("RGB"), name + " (specular)")

    def occlusion(self, key) -> int:
        from PIL import Image                                    # noqa: PLC0415
        name, img = self._img(key)
        g = img.getchannel("G")
        return self._put(("occ", key), Image.merge("RGB", (g, g, g)), name + " (occlusion)")

    def metal_rough(self, *, metal_key, gloss_key, gloss_channel, gloss_scale, metallic) -> int:
        from PIL import Image                                    # noqa: PLC0415
        ck = ("mr", metal_key, gloss_key, gloss_channel, gloss_scale, metallic)
        if ck in self.cache:
            return self.cache[ck]
        ref = self._img(metal_key if metal_key is not None else gloss_key)
        size = ref[1].size
        if metal_key is not None:
            metal = self._img(metal_key)[1].getchannel("R")
        else:
            metal = Image.new("L", size, round(255 * metallic))
        if gloss_key is not None:
            gimg = self._img(gloss_key)[1]
            src = gimg.getchannel("A") if "A" in gimg.getbands() else Image.new("L", gimg.size, 255)
            if src.size != size:
                src = src.resize(size, Image.BILINEAR)
            rough = src.point(lambda a: max(0, min(255, round(255 - a * gloss_scale))))
        else:
            rough = Image.new("L", size, 255)
        if metal.size != size:
            metal = metal.resize(size, Image.BILINEAR)
        out = Image.merge("RGB", (Image.new("L", size, 255), rough, metal))
        idx = self._put(ck, out, f"{ref[0]} (metal-rough)")
        return idx


def _unpack_dxt5nm(img):
    from PIL import Image                                        # noqa: PLC0415
    x = img.getchannel("A")
    y = img.getchannel("G")
    xs, ys = x.tobytes(), y.tobytes()
    zs = []
    for a, g in zip(xs, ys):
        nx, ny = a / 127.5 - 1.0, g / 127.5 - 1.0
        zs.append(round((math.sqrt(max(0.0, 1.0 - nx * nx - ny * ny)) * 0.5 + 0.5) * 255))
    z = Image.frombytes("L", img.size, bytes(zs))
    return Image.merge("RGB", (x, y, z))


# ---------------------------------------------------------------- reading the build


def _unitypy():
    try:
        import UnityPy                                           # noqa: PLC0415
    except ImportError as exc:                                   # pragma: no cover
        raise SystemExit('UnityPy is not installed — pip install -e ".[unity]"') from exc
    return UnityPy


def _key(obj) -> tuple:
    return (obj.assets_file.name if obj.assets_file else "", obj.path_id)


@dataclass
class Thing:
    """One output GLB: a GameObject and everything under it."""
    name: str
    transform: object                                            # the UnityPy Transform object reader
    path: str


class Build:
    """A Unity player build, read once. `root` is the data folder (`.../assets/bin/Data`)."""

    def __init__(self, root: str):
        self.root = root
        self.env = _unitypy().load(root)
        self.transforms: dict = {}
        self.parent: dict = {}
        self.go_transform: dict = {}
        for obj in self.env.objects:
            if obj.type.name in ("Transform", "RectTransform"):
                tr = obj.read()
                k = _key(obj)
                self.transforms[k] = (obj, tr)
                if tr.m_Father and tr.m_Father.m_PathID:
                    self.parent[k] = (k[0], tr.m_Father.m_PathID)
                self.go_transform[(k[0], tr.m_GameObject.m_PathID)] = k

    def game_object(self, tkey):
        obj, tr = self.transforms[tkey]
        return tr.m_GameObject.read()

    def name(self, tkey) -> str:
        try:
            return self.game_object(tkey).m_Name
        except Exception:                                        # noqa: BLE001
            return "?"

    def children(self, tkey) -> list:
        _obj, tr = self.transforms[tkey]
        return [(tkey[0], c.m_PathID) for c in tr.m_Children if c.m_PathID]

    def roots(self) -> list:
        return [k for k in self.transforms if k not in self.parent]

    def path(self, tkey) -> str:
        parts = []
        k = tkey
        while k is not None:
            parts.append(self.name(k))
            k = self.parent.get(k)
        return "/".join(reversed(parts))

    def components(self, tkey) -> list:
        """`[(type name, object)]` for the GameObject's components; unreadable ones are skipped."""
        out = []
        go = self.game_object(tkey)
        for pair in go.m_Component:
            ptr = getattr(pair, "component", pair)
            try:
                reader = ptr.deref()
                out.append((reader.type.name, reader.read()))
            except Exception:                                    # noqa: BLE001  (script types without a typetree)
                continue
        return out

    def renders(self, tkey) -> bool:
        if any(t in ("MeshFilter",) for t, _ in self.components(tkey)):
            return True
        return any(self.renders(c) for c in self.children(tkey))

    def find(self, path_or_name: str) -> Optional[tuple]:
        for k in self.transforms:
            if self.path(k) == path_or_name or self.name(k) == path_or_name:
                return k
        return None

    def things(self, under: str = "") -> list[Thing]:
        """What gets a GLB of its own: the children of `under` (by name or path), or — without it —
        every scene root that renders anything."""
        if under:
            k = self.find(under)
            if k is None:
                raise SystemExit(f"no GameObject named or at {under!r}")
            keys = self.children(k)
        else:
            keys = self.roots()
        return [Thing(self.name(k), k, self.path(k)) for k in keys if self.renders(k)]

    def world_matrix(self, tkey):
        m = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
        chain = []
        k = tkey
        while k is not None:
            chain.append(k)
            k = self.parent.get(k)
        for k in reversed(chain):
            _o, tr = self.transforms[k]
            p, r, s = tr.m_LocalPosition, tr.m_LocalRotation, tr.m_LocalScale
            m = matmul(m, trs_matrix((p.x, p.y, p.z), (r.x, r.y, r.z, r.w), (s.x, s.y, s.z)))
        return m


# ---------------------------------------------------------------- one thing -> one GLB


def _source_of(material, readers: dict) -> MaterialSource:
    """Read a Unity Material. Texture references are resolved to `(file, path_id)` keys, and the
    object reader for each is recorded in `readers` so the sink can decode it later."""
    sp = material.m_SavedProperties
    shader = "?"
    try:
        shader = material.m_Shader.read().m_ParsedForm.m_Name
    except Exception:                                            # noqa: BLE001
        pass
    textures = {}
    for slot, env in sp.m_TexEnvs:
        if env.m_Texture and env.m_Texture.m_PathID:
            try:
                reader = env.m_Texture.deref()
            except Exception:                                    # noqa: BLE001  (texture not in this build)
                continue
            key = _key(reader)
            readers[key] = reader
            textures[slot] = (key, (env.m_Scale.x, env.m_Scale.y), (env.m_Offset.x, env.m_Offset.y))
    floats = {k: float(v) for k, v in sp.m_Floats}
    colors = {k: (v.r, v.g, v.b, v.a) for k, v in sp.m_Colors}
    kws = set(getattr(material, "m_ValidKeywords", None) or [])
    legacy = getattr(material, "m_ShaderKeywords", None)
    if isinstance(legacy, str):
        kws.update(legacy.split())
    return MaterialSource(material.m_Name, shader, textures, floats, colors, frozenset(kws),
                          int(getattr(material, "m_CustomRenderQueue", -1)))


def _mesh_streams(mesh):
    """Decode a Unity Mesh with UnityPy's handler: `(positions, normals, tangents, [uv0, uv1...],
    colors, [triangles per submesh], topology notes)`."""
    import numpy as np                                           # noqa: PLC0415
    from UnityPy.helpers.MeshHelper import MeshHandler          # noqa: PLC0415
    h = MeshHandler(mesh)
    h.process()
    uvs = []
    for i in range(8):
        uv = getattr(h, f"m_UV{i}", None)
        if not uv:
            break
        uvs.append(np.asarray(uv, dtype=np.float32))
    colors = np.asarray(h.m_Colors, dtype=np.float32) if h.m_Colors else None
    tris = [np.asarray(t, dtype=np.uint32).reshape(-1, 3) if len(t) else np.zeros((0, 3), np.uint32)
            for t in h.get_triangles()]
    return (np.asarray(h.m_Vertices, dtype=np.float32), np.asarray(h.m_Normals, dtype=np.float32) if h.m_Normals else None,
            np.asarray(h.m_Tangents, dtype=np.float32) if h.m_Tangents else None, uvs, colors, tris)


def export_thing(build: Build, thing: Thing, *, max_texture: int = 1024, lossless: bool = False,
                 quality: int = 90) -> tuple[bytes, list[str], dict]:
    """One GLB for `thing`. Returns `(glb bytes, notes, stats)`."""
    import numpy as np                                           # noqa: PLC0415
    glb = _Glb()
    notes: list[str] = []
    stats = {"triangles": 0, "vertices": 0, "meshes": 0, "materials": 0, "textures": 0, "skipped": []}

    readers: dict = {}

    def lookup(key):
        tex = readers[key].read()
        return tex.m_Name, tex.image

    images = _Images(glb, lookup, max_texture=max_texture, lossless=lossless, quality=quality)
    mat_index: dict = {}
    mesh_attrs: dict = {}

    def material_index(ptr) -> Optional[int]:
        if not ptr or not ptr.m_PathID:
            return None
        try:
            reader = ptr.deref()
        except Exception:                                        # noqa: BLE001
            return None
        k = _key(reader)
        if k not in mat_index:
            m, n = material_from(_source_of(reader.read(), readers), images)
            notes.extend(n)
            glb.doc["materials"].append(m)
            mat_index[k] = len(glb.doc["materials"]) - 1
        return mat_index[k]

    def node_for(tkey, *, root: bool) -> int:
        _obj, tr = build.transforms[tkey]
        go = build.game_object(tkey)
        node: dict = {"name": go.m_Name}
        if root:
            # The thing keeps its world rotation and scale (what the app shows) but sits at the origin.
            w = build.world_matrix(tkey)
            c = [[-1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
            w = matmul(matmul(c, w), c)
            w[0][3] = w[1][3] = w[2][3] = 0.0
            ident = [[1.0 if i == j else 0.0 for j in range(4)] for i in range(4)]
            if any(abs(w[i][j] - ident[i][j]) > 1e-9 for i in range(4) for j in range(4)):
                node["matrix"] = [w[r][col] for col in range(4) for r in range(4)]
        else:
            p, r, s = tr.m_LocalPosition, tr.m_LocalRotation, tr.m_LocalScale
            t, q, sc = convert_trs((p.x, p.y, p.z), (r.x, r.y, r.z, r.w), (s.x, s.y, s.z))
            if t != [0.0, 0.0, 0.0] and t != [-0.0, 0.0, 0.0]:
                node["translation"] = t
            if q != [0.0, -0.0, -0.0, 1.0] and q != [0.0, 0.0, 0.0, 1.0]:
                node["rotation"] = q
            if sc != [1.0, 1.0, 1.0]:
                node["scale"] = sc
        if not go.m_IsActive:
            notes.append(f"{build.path(tkey)}: inactive in the scene (the app enables it at runtime?) — exported anyway")
        comps = build.components(tkey)
        mf = next((o for t, o in comps if t == "MeshFilter"), None)
        mr = next((o for t, o in comps if t == "MeshRenderer"), None)
        for t, _o in comps:
            if t in ("SkinnedMeshRenderer",):
                stats["skipped"].append(f"{build.path(tkey)}: skinned mesh (not exported)")
            if t == "AudioSource" and getattr(_o, "m_audioClip", None) and _o.m_audioClip.m_PathID:
                try:
                    stats.setdefault("audio", []).append(f"{build.path(tkey)}: {_o.m_audioClip.read().m_Name}")
                except Exception:                               # noqa: BLE001
                    pass
            if t == "MonoBehaviour":
                try:
                    stats.setdefault("scripts", set()).add(_o.m_Script.read().m_ClassName)
                except Exception:                               # noqa: BLE001
                    pass
        if mf is not None and mf.m_Mesh and mf.m_Mesh.m_PathID:
            if mr is not None and not mr.m_Enabled:
                notes.append(f"{build.path(tkey)}: renderer disabled in the scene — exported anyway")
            mesh = mf.m_Mesh.read()
            mkey = (mf.m_Mesh.m_FileID, mf.m_Mesh.m_PathID, mesh.m_Name)
            if mkey not in mesh_attrs:
                pos, nrm, tan, uvs, colors, tris = _mesh_streams(mesh)
                pos, nrm, tan, uvs = convert_vertices(pos, nrm, tan, uvs)
                attrs = {"POSITION": glb.accessor(pos, "VEC3", bounds=True)}
                if nrm is not None and len(nrm) == len(pos):
                    attrs["NORMAL"] = glb.accessor(nrm, "VEC3")
                if tan is not None and len(tan) == len(pos):
                    attrs["TANGENT"] = glb.accessor(tan, "VEC4")
                for i, uv in enumerate(uvs):
                    if len(uv) == len(pos):
                        attrs[f"TEXCOORD_{i}"] = glb.accessor(uv, "VEC2")
                if colors is not None and len(colors) == len(pos):
                    attrs["COLOR_0"] = glb.accessor(np.clip(colors, 0, 1).astype(np.float32), "VEC4")
                prims = []
                n = len(pos)
                for tri in tris:
                    if not len(tri):
                        prims.append(None)
                        continue
                    idx = convert_triangles(tri)
                    idx = idx.astype(np.uint16) if n <= 65535 else idx
                    prims.append(glb.accessor(idx.reshape(-1, 1).ravel().reshape(-1, 1), "SCALAR", target=34963))
                    stats["triangles"] += len(tri)
                stats["vertices"] += n
                mesh_attrs[mkey] = (attrs, prims, mesh.m_Name)
            attrs, prims, mname = mesh_attrs[mkey]
            mats = list(mr.m_Materials) if mr is not None else []
            primitives = []
            for i, acc in enumerate(prims):
                if acc is None:
                    continue
                prim = {"attributes": attrs, "indices": acc}
                mi = material_index(mats[i]) if i < len(mats) else None
                if mi is not None:
                    prim["material"] = mi
                elif i >= len(mats):
                    notes.append(f"{build.path(tkey)}: submesh {i} has no material")
                primitives.append(prim)
            if len(mats) > len(prims):
                notes.append(f"{build.path(tkey)}: {len(mats) - len(prims)} extra material(s) (Unity multi-pass) dropped")
            if primitives:
                glb.doc["meshes"].append({"name": mname, "primitives": primitives})
                node["mesh"] = len(glb.doc["meshes"]) - 1
                stats["meshes"] += 1
        kids = [node_for(c, root=False) for c in build.children(tkey)]
        glb.doc["nodes"].append(node)
        me = len(glb.doc["nodes"]) - 1
        if kids:
            node["children"] = kids
        return me

    glb.doc["scenes"][0]["nodes"] = [node_for(thing.transform, root=True)]
    glb.doc["scenes"][0]["name"] = thing.name
    stats["materials"] = len(glb.doc["materials"])
    stats["textures"] = len(glb.doc["textures"])
    if "scripts" in stats:
        stats["scripts"] = sorted(stats["scripts"])
    return glb.finish(), notes, stats


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "unnamed"


def rebuild_build(root: str, out_dir: str, *, under: str = "", only: str = "", max_texture: int = 1024,
                  lossless: bool = False, quality: int = 90,
                  report: Optional[Callable[[str], None]] = None) -> list[str]:
    """Convert every thing in the build at `root`. Returns the files written."""
    say = report or (lambda _s: None)
    build = Build(root)
    found = build.things(under)
    say(f"{root} — {len(found)} thing(s){' under ' + under if under else ''}")
    written = []
    os.makedirs(out_dir, exist_ok=True)
    for thing in found:
        if only and only.lower() not in thing.name.lower():
            continue
        data, notes, stats = export_thing(build, thing, max_texture=max_texture, lossless=lossless, quality=quality)
        dest = os.path.join(out_dir, safe_name(thing.name) + ".glb")
        if dest in written:
            dest = os.path.join(out_dir, f"{safe_name(thing.name)}~{thing.transform[1]}.glb")
        with open(dest, "wb") as fh:
            fh.write(data)
        written.append(dest)
        say(f"    {thing.name:28} -> {os.path.relpath(dest)}  {len(data)/1e6:.1f} MB  "
            f"{stats['triangles']:,} tris, {stats['vertices']:,} verts, {stats['meshes']} meshes, "
            f"{stats['materials']} materials, {stats['textures']} textures")
        for line in stats.get("skipped", []):
            say(f"        - {line}")
        if stats.get("scripts"):
            say(f"        - behaviour scripts not exported: {', '.join(stats['scripts'])}")
        for line in stats.get("audio", []):
            say(f"        - audio not exported: {line}")
        for note in dict.fromkeys(notes):
            say(f"        ! {note}")
    return written
