"""Re-assemble a PlayCanvas published build into self-contained, textured GLBs.

PlayCanvas's converter splits an upload deliberately: geometry and skinning go into the GLB, while
materials and textures become separate entries in the project's asset registry. The GLB is never
self-contained *by design* — the engine reattaches materials at load time. So a build downloaded from
PlayCanvas hands you a model that renders as flat white in any ordinary glTF viewer, with every texture
sitting right there beside it and nothing saying which goes where.

This is the "which goes where". It is a TRANSCRIPTION, not a reconstruction: the binding is stated
outright in the build, so nothing here guesses.

    config.json  ->  assets by id (containers, renders, materials, textures)
    <scene>.json ->  entities, each with a `render` component holding
                       `asset`          -> a render asset -> (containerAsset, renderIndex)
                       `materialAssets` -> ONE PER PRIMITIVE, in order

That last line is the whole thing. A glTF mesh is split into primitives precisely because each one had a
different material, so the ordering survived the conversion and the list drops straight back on.

**The materials are on the SCENE, not the container.** A container ships whatever material its own
import produced, and the scene overrides it — Jane's hair container carries an untextured grey, and
reading it instead of the scene gives you grey hair with the real texture sitting unused on disk.

Verified against the engine shipped in the same build rather than from memory (`BLEND_NONE=3`,
`CULLFACE_NONE=0`, and the `glossPS` shader chunk for what `glossInvert` does), because every one of
those is a silent wrongness if guessed: the wrong blend mode is invisible until something is behind the
figure, and an inverted roughness map looks like a lighting problem.
"""

from __future__ import annotations

import io
import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .figures import split_glb, write_glb

# ---------------------------------------------------------------- the engine's own constants
#
# Read out of `playcanvas-stable.min.js` in the build being converted. Listed here because the
# translation is meaningless without them and "3" reads like "the third blend mode" rather than "none".

BLEND_SUBTRACTIVE, BLEND_ADDITIVE, BLEND_NORMAL, BLEND_NONE, BLEND_PREMULTIPLIED = 0, 1, 2, 3, 4
CULLFACE_NONE = 0

#: glTF wants roughness in G and metalness in B of one texture. PlayCanvas names a channel per map.
_CHANNEL = {"r": 0, "g": 1, "b": 2, "a": 3}

#: Settings that change how a material LOOKS and that this does not carry across, with the test for
#: "actually in use" and a word on what is lost. Warned about per material rather than dropped quietly.
#:
#: The list exists because the translation was written against ONE character, and a tool whose coverage
#: was shaped by its first input should say where that shows rather than let the next model come out
#: subtly wrong with a clean report. Several have a KHR extension waiting if a model needs them; none is
#: worth writing blind.
_UNCARRIED = (
    ("specular workflow", lambda d: d.get("specularMap") is not None and not d.get("useMetalness"),
     "specular/gloss, which glTF core does not have — KHR_materials_specular"),
    ("light map", lambda d: d.get("lightMap") is not None, "baked lighting, no glTF core equivalent"),
    ("environment map", lambda d: d.get("cubeMap") is not None or d.get("sphereMap") is not None,
     "a per-material environment, which glTF leaves to the viewer"),
    ("height map", lambda d: d.get("heightMap") is not None, "parallax, no glTF equivalent"),
    ("clear coat", lambda d: float(d.get("clearCoat") or 0) > 0, "KHR_materials_clearcoat"),
    # Gated on the `use*` flag, never on the value. PlayCanvas leaves sheen at a default WHITE with
    # `useSheen: false`, so testing the colour fires on every material in every build — 208 of them —
    # and a warning that always fires is one nobody reads. `useDynamicRefraction` is true exactly once
    # across all three builds, on Jane's eyes, which is the one this list earns its keep for.
    ("sheen", lambda d: bool(d.get("useSheen")), "KHR_materials_sheen"),
    ("refraction", lambda d: bool(d.get("useDynamicRefraction")) or float(d.get("refraction") or 0) > 0,
     "KHR_materials_transmission — the material will read as opaque"),
    ("iridescence", lambda d: bool(d.get("useIridescence")), "KHR_materials_iridescence"),
    ("alpha to coverage", lambda d: bool(d.get("alphaToCoverage")), "no glTF equivalent"),
    ("depth write off", lambda d: d.get("depthWrite") is False, "no glTF equivalent"),
    ("depth test off", lambda d: d.get("depthTest") is False, "no glTF equivalent"),
)

#: Every map PlayCanvas can put on a second UV set. glTF spells it `texCoord`, and carrying it is free.
_UV_KEYS = ("diffuse", "normal", "gloss", "metalness", "opacity", "emissive", "ao", "light")


@dataclass
class Binding:
    """One entity's claim on one mesh: which materials, in primitive order."""

    entity: str
    container: int
    mesh: int                      # `renderIndex` — the glTF mesh index inside the container
    materials: tuple[Optional[int], ...]
    adopted: bool = False          # inferred by name rather than stated by a scene — see `adopt_unbound`


@dataclass
class Build:
    """A published PlayCanvas build: its asset registry, and what its scenes bind to what."""

    root: str
    assets: dict[int, dict]
    bindings: list[Binding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    origin: str = ""               # the URL it was captured from, when the path says (see `build_origin`)

    def asset(self, aid) -> dict:
        return self.assets.get(int(aid)) if aid is not None else None

    def path(self, aid) -> Optional[str]:
        a = self.asset(aid)
        url = ((a or {}).get("file") or {}).get("url")
        return os.path.join(self.root, url) if url else None

    def name(self, aid) -> str:
        return ((self.asset(aid) or {}).get("name")) or f"asset {aid}"


# ---------------------------------------------------------------- reading the build


def find_builds(root: str) -> list[str]:
    """Every directory under `root` holding a published build, outermost first.

    A downloaded project routinely contains OTHER projects — the capture this was written against has
    three, one per character plus the room and a props library, nested under `api.<host>/release/<id>/`.
    Walking for them means one invocation converts the lot.
    """
    found = []
    for path, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "config.json" not in files:
            continue
        try:
            cfg = json.load(open(os.path.join(path, "config.json")))
        except (ValueError, OSError):
            continue
        if isinstance(cfg, dict) and "assets" in cfg and "scenes" in cfg:
            found.append(path)
    return found


@dataclass
class Orphan:
    """An asset tree with no registry above it — geometry captured, `config.json` not."""

    root: str                      # the directory that should hold config.json
    assets: int                    # how many files are under files/assets
    kinds: dict                    # extension -> count, so "all GLB" is visible at a glance
    origin: str = ""               # best-effort URL for the missing registry, or ""


def find_orphans(root: str) -> list[Orphan]:
    """Asset trees that look like a published build with the REGISTRY missing.

    Worth its own report because the failure is silent otherwise and the diagnosis is not obvious: the
    directory layout is right, the GLBs are valid, and every one of them is untextured — so "no
    PlayCanvas build here" sends you looking at the structure, which is fine. What is missing is
    `config.json` and the scene beside it, and without those there is nothing that says which material
    goes where, and usually no texture files either.

    The capture mirrors the URL it came from, so the address to fetch is derivable from the path: a
    segment with a dot in it is the host, and everything between it and `files` is the build. That turns
    a dead end into a shopping list.
    """
    out: list[Orphan] = []
    for path, dirs, _files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if os.path.basename(path) != "assets" or os.path.basename(os.path.dirname(path)) != "files":
            continue
        base = os.path.dirname(os.path.dirname(path))            # the dir that should hold config.json
        if os.path.exists(os.path.join(base, "config.json")):
            continue                                             # a proper build; not our business
        kinds: dict[str, int] = {}
        count = 0
        for _p, _d, fs in os.walk(path):
            for f in fs:
                if f.startswith("."):
                    continue
                count += 1
                kinds[os.path.splitext(f)[1].lstrip(".").lower() or "?"] = kinds.get(
                    os.path.splitext(f)[1].lstrip(".").lower() or "?", 0) + 1
        if not count:
            continue
        origin = build_origin(root, base)
        out.append(Orphan(root=base, assets=count, kinds=kinds,
                          origin=f"{origin}/config.json" if origin else ""))
    return out


def build_origin(capture_root: str, build_root: str) -> str:
    """The URL a build was captured from, or `""`.

    A capture mirrors the address it came from, so the path carries it: a segment with a dot in it is
    the host, and everything from there on is the build. That is what turns "this texture is missing"
    into something you can hand to `curl`.
    """
    parts = os.path.relpath(build_root, capture_root).split(os.sep)
    host = next((i for i, p in enumerate(parts) if "." in p and not p.startswith(".")), None)
    return ("https://" + "/".join(parts[host:])) if host is not None else ""


def missing_files(build: Build, *, kinds=("texture", "container")) -> list[tuple[str, str]]:
    """`[(name, url-or-"")]` for every asset the registry references and the capture does not hold.

    Reported as a LIST rather than one warning per use, because a texture shared by six materials went
    missing six times in the log — Akari's build references 105 textures and holds none of them, which
    was 200-odd identical lines burying the two findings that mattered.
    """
    out: list[tuple[str, str]] = []
    for aid, asset in sorted(build.assets.items()):
        if asset.get("type") not in kinds:
            continue
        url = (asset.get("file") or {}).get("url")
        if not url or os.path.exists(os.path.join(build.root, url)):
            continue
        out.append((asset.get("name") or str(aid), f"{build.origin}/{url}" if build.origin else url))
    return out


def read_build(root: str) -> Build:
    """Load a build and resolve every entity → container → mesh → per-primitive material binding."""
    cfg = json.load(open(os.path.join(root, "config.json")))
    assets = {int(k): v for k, v in (cfg.get("assets") or {}).items()}
    build = Build(root=root, assets=assets)

    seen: dict[tuple[int, int], Binding] = {}
    clashes: dict[tuple[int, int], set] = {}

    def take(entities: dict) -> None:
        for entity in (entities or {}).values():
            render = (entity.get("components") or {}).get("render")
            if not render or render.get("type") != "asset" or render.get("asset") is None:
                continue                      # a primitive box or a disabled slot: no container behind it
            data = (build.asset(render["asset"]) or {}).get("data") or {}
            container, index = data.get("containerAsset"), data.get("renderIndex")
            if container is None or index is None:
                continue
            mats = tuple(int(m) if m is not None else None
                         for m in (render.get("materialAssets") or []))
            key = (int(container), int(index))
            bound = Binding(entity.get("name") or "?", key[0], key[1], mats)
            if key in seen and seen[key].materials != mats:
                # Two entities dressing the same mesh differently is a legitimate thing to do — a props
                # library reuses one button mesh in a dozen colours — and it has no single answer in a
                # file format that allows one material per primitive. First wins, and the clash is
                # reported ONCE however many instances there are, because a props library produces
                # dozens of them and they would bury everything else.
                clashes.setdefault(key, set()).add(bound.entity)
                continue
            seen.setdefault(key, bound)

    for scene in cfg.get("scenes") or []:
        url = scene.get("url")
        if not url or not os.path.exists(os.path.join(root, url)):
            build.notes.append(f"scene {scene.get('name')!r} is referenced but not on disk ({url}) — "
                               f"falling back to the template assets, which carry the same bindings")
            continue
        take(json.load(open(os.path.join(root, url))).get("entities"))

    # TEMPLATES are the same structure and a second place the binding lives. Read AFTER the scenes, so
    # a scene wins where both speak — it is what actually runs.
    #
    # Not a fallback bolted on. A PlayCanvas template is a serialised entity hierarchy, which is how a
    # reusable thing is packaged, and a character is exactly that: the second capture to arrive had NO
    # scene file on disk and sixteen templates, one per skin-tone variant of the same model, each
    # binding its OWN container. So there is no ambiguity to resolve — reading them is what makes that
    # capture convertible at all, and it costs nothing where a scene is present because identical
    # bindings are deduplicated rather than reported as a clash.
    for asset in build.assets.values():
        if asset.get("type") == "template":
            take((asset.get("data") or {}).get("entities"))
    for (container, index), others in sorted(clashes.items()):
        build.notes.append(
            f"{build.name(container)} mesh {index}: {len(others)} other entity binding(s) disagree "
            f"({', '.join(sorted(others)[:3])}...) — keeping {seen[(container, index)].entity!r}"
            if len(others) > 3 else
            f"{build.name(container)} mesh {index}: {', '.join(sorted(others))} bind different "
            f"materials — keeping {seen[(container, index)].entity!r}")
    build.bindings = sorted(seen.values(), key=lambda b: (b.container, b.mesh))
    return build


def adopt_unbound(build: Build) -> int:
    """Give an unbound mesh the material bound to an identically-named render asset elsewhere. Inferred.

    A character build routinely ships the same mesh twice: once inside the body container and once as a
    standalone container so it can be swapped. Jane's hair is both `jane_export.glb` mesh 3 and the whole
    of `hair.glb`, same 3,969 vertices, and the scene renders the standalone one — so the copy inside her
    body GLB is bound by nobody and comes out flat grey.

    Faithfully, that IS what the scene does. But a figure has to arrive as ONE file here, and a
    swappable-outfit vocabulary does not exist yet (specs/figures.md, § What is not built), so the
    useful answer and the literal answer differ. This is the useful one, kept behind a flag and reported
    as inferred every time, because a render asset's NAME matching is good evidence and not proof.
    """
    renders = {}
    for aid, a in build.assets.items():
        if a.get("type") != "render":
            continue
        d = a.get("data") or {}
        if d.get("containerAsset") is not None and d.get("renderIndex") is not None:
            renders[(int(d["containerAsset"]), int(d["renderIndex"]))] = a.get("name") or ""
    bound = {(b.container, b.mesh) for b in build.bindings}
    by_name = {renders.get((b.container, b.mesh), ""): b for b in build.bindings}
    adopted = 0
    for key, name in sorted(renders.items()):
        if key in bound or not name or name not in by_name:
            continue
        donor = by_name[name]
        build.bindings.append(Binding(f"{donor.entity} (adopted by name {name!r})",
                                      key[0], key[1], donor.materials, adopted=True))
        build.notes.append(f"{build.name(key[0])} mesh {key[1]} is bound by no entity; adopting the "
                           f"material from {build.name(donor.container)} mesh {donor.mesh}, which shares "
                           f"the render name {name!r} — INFERRED, look at it")
        adopted += 1
    build.bindings.sort(key=lambda b: (b.container, b.mesh))
    return adopted


def by_container(build: Build) -> dict[int, list[Binding]]:
    out: dict[int, list[Binding]] = {}
    for b in build.bindings:
        out.setdefault(b.container, []).append(b)
    return out


# ---------------------------------------------------------------- images


def _pil():
    from PIL import Image                                       # noqa: PLC0415  (optional dependency)
    return Image


def load_image(path: str):
    """A texture as RGB or RGBA, with palettes expanded and transparency preserved.

    An indexed PNG carries its transparency in a `tRNS` chunk rather than a channel, and two of the maps
    in the capture this was written against are indexed — one of them the eyelashes, which are nothing
    but alpha. Converting via `RGBA` when `tRNS` is present is what keeps them from becoming solid
    rectangles across her face.
    """
    Image = _pil()
    img = Image.open(path)
    if img.mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
    elif img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if img.mode in ("LA", "PA") else "RGB")
    return img


def has_alpha(img) -> bool:
    """Whether the image carries transparency that is actually USED.

    A material naming an opacity map is not evidence of one: five of Jane's materials point their
    opacity at a texture with no alpha channel at all, and taking that at face value would emit
    `alphaMode: MASK` and punch holes through her.
    """
    if "A" not in img.getbands():
        return False
    lo, hi = img.getchannel("A").getextrema()
    return lo < 255


def fit(img, max_side: int):
    """Downscale so neither side exceeds `max_side`, keeping powers of two where they started.

    Not an optimisation — a budget. The capture's textures are 4096 square, which is about 90 MB of VRAM
    each once mipmapped; nine of them for one figure is most of a headset's budget spent on one
    character who also costs 111k triangles. See docs/investigations/figures-frame-rate.md.
    """
    Image = _pil()
    w, h = img.size
    if max(w, h) <= max_side:
        return img
    scale = max_side / float(max(w, h))
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def encode(img, *, quality: int = 90) -> tuple[bytes, str]:
    """PNG when there is alpha to keep, JPEG when there is not — the latter is several times smaller."""
    buf = io.BytesIO()
    if has_alpha(img):
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png"
    img.convert("RGB").save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue(), "image/jpeg"


def _channel(img, letter: str):
    """One channel as an L image, tolerating a request for alpha on an image that has none."""
    Image = _pil()
    letter = (letter or "r").lower()[:1]
    if letter == "a" and "A" not in img.getbands():
        return Image.new("L", img.size, 255)
    return img.getchannel({"r": "R", "g": "G", "b": "B", "a": "A"}.get(letter, "R"))


# ---------------------------------------------------------------- the material translation


class _Textures:
    """Images, samplers and textures for one output GLB, de-duplicated by how they were derived."""

    def __init__(self, build: Build, max_texture: int, quality: int):
        self.build, self.max, self.quality = build, max_texture, quality
        self.images: list[dict] = []
        self.textures: list[dict] = []
        self.blobs: list[bytes] = []
        self._by_key: dict[tuple, int] = {}
        self.warnings: list[str] = []

    def warn(self, message: str) -> None:
        """Once per distinct message. A texture shared by six materials was reported six times."""
        if message not in self.warnings:
            self.warnings.append(message)

    def _add(self, key: tuple, img) -> int:
        if key in self._by_key:
            return self._by_key[key]
        raw, mime = encode(fit(img, self.max), quality=self.quality)
        self.blobs.append(raw)
        self.images.append({"mimeType": mime, "name": key[0] if isinstance(key[0], str) else None})
        self.textures.append({"source": len(self.images) - 1})
        self._by_key[key] = len(self.textures) - 1
        return self._by_key[key]

    def plain(self, aid) -> Optional[int]:
        path = self.build.path(aid)
        if not path or not os.path.exists(path):
            self.warn(f"texture {self.build.name(aid)} is missing from disk")
            return None
        return self._add(("plain", int(aid)), load_image(path))

    def base_colour(self, diffuse, opacity, opacity_channel: str) -> Optional[int]:
        """The base colour, with opacity composited into its alpha when it does not already live there.

        glTF has exactly one place for transparency — the alpha of the base colour texture — while
        PlayCanvas will happily read it from any channel of any other image. Where they agree (the
        common case, and every case in the capture) this costs nothing and returns the plain image.
        """
        if opacity is None or (diffuse == opacity and (opacity_channel or "a").lower() == "a"):
            return self.plain(diffuse) if diffuse is not None else None
        d_path, o_path = self.build.path(diffuse), self.build.path(opacity)
        if not d_path or not o_path or not os.path.exists(d_path) or not os.path.exists(o_path):
            return self.plain(diffuse) if diffuse is not None else None
        base, mask = load_image(d_path), load_image(o_path)
        alpha = _channel(mask, opacity_channel)
        if alpha.size != base.size:
            alpha = alpha.resize(base.size)
        merged = base.convert("RGBA")
        merged.putalpha(alpha)
        return self._add(("opacity", int(diffuse), int(opacity), (opacity_channel or "a").lower()),
                         merged)

    def metallic_roughness(self, d: dict) -> Optional[int]:
        """G = roughness, B = metalness, assembled from whatever channels PlayCanvas was reading.

        `glossInvert` decides whether the source is gloss or roughness, and it is not a guess: the
        engine's `glossPS` chunk multiplies the scalar by the map and THEN applies `1.0 - x` under
        `MAPINVERT`, so an inverted gloss map is a roughness map and goes straight through.
        """
        Image = _pil()
        gloss, metal = d.get("glossMap"), d.get("metalnessMap")
        if gloss is None and metal is None:
            return None
        key = ("mr", gloss, d.get("glossMapChannel"), metal, d.get("metalnessMapChannel"),
               bool(d.get("glossInvert")))
        if key in self._by_key:
            return self._by_key[key]
        size = None
        rough = metalness = None
        if gloss is not None and self.build.path(gloss) and os.path.exists(self.build.path(gloss)):
            src = load_image(self.build.path(gloss))
            size = src.size
            rough = _channel(src, d.get("glossMapChannel"))
            if not d.get("glossInvert"):
                from PIL import ImageOps                        # noqa: PLC0415
                rough = ImageOps.invert(rough)                  # a gloss map is roughness upside down
        if metal is not None and self.build.path(metal) and os.path.exists(self.build.path(metal)):
            src = load_image(self.build.path(metal))
            size = size or src.size
            metalness = _channel(src, d.get("metalnessMapChannel"))
        if size is None:
            return None
        white = Image.new("L", size, 255)
        merged = Image.merge("RGB", (Image.new("L", size, 0),      # R is unused by glTF
                                     (rough if rough is not None else white).resize(size),
                                     (metalness if metalness is not None else white).resize(size)))
        return self._add(key, merged)


def material_from(d: dict, name: str, tex: _Textures) -> dict:
    """One PlayCanvas material as a glTF one. Faithful rather than tasteful — see the module docstring.

    Faithful means transcribing settings that do nothing, and there are two in the capture: an eye
    material with `opacity: 0.1` under a blend mode that ignores opacity, and a skin material with a
    normal map at `bumpMapFactor: 0`. Both survive as glTF that renders identically. "Fixing" them here
    would be this tool deciding it knows better than the author, silently, in a file nobody re-reads.
    """
    pbr: dict = {}
    warn: list[str] = []

    def ref(index: int, slot: str, **extra) -> dict:
        """A glTF texture reference, carrying the second UV set if PlayCanvas was reading one."""
        out = {"index": index, **extra}
        uv = int(d.get(f"{slot}MapUv", 0) or 0)
        if uv:
            out["texCoord"] = uv
        return out

    diffuse = list(d.get("diffuse") or [1, 1, 1])[:3]
    opacity = float(d.get("opacity", 1) or 0)
    if diffuse != [1, 1, 1] or opacity != 1:
        pbr["baseColorFactor"] = [float(c) for c in diffuse] + [opacity]

    o_map, o_ch = d.get("opacityMap"), (d.get("opacityMapChannel") or "a")
    idx = tex.base_colour(d.get("diffuseMap"), o_map, o_ch)
    if idx is not None:
        pbr["baseColorTexture"] = ref(idx, "diffuse")

    # PlayCanvas multiplies the scalar by the map, then inverts under `glossInvert` — so an inverted
    # gloss IS roughness, and the factor rides along with it either way.
    gloss = float(d.get("shininess", 0) or 0) / 100.0
    pbr["roughnessFactor"] = round(gloss if d.get("glossInvert") else 1.0 - gloss, 6)
    pbr["metallicFactor"] = float(d.get("metalness", 0) or 0) if d.get("useMetalness") else 0.0
    mr = tex.metallic_roughness(d)
    if mr is not None:
        pbr["metallicRoughnessTexture"] = ref(mr, "gloss" if d.get("glossMap") else "metalness")

    out: dict = {"name": name, "pbrMetallicRoughness": pbr}

    if d.get("normalMap") is not None:
        idx = tex.plain(d["normalMap"])
        if idx is not None:
            scale = float(d.get("bumpMapFactor", 1))
            out["normalTexture"] = ref(idx, "normal", **({"scale": scale} if scale != 1 else {}))
    if d.get("aoMap") is not None:
        idx = tex.plain(d["aoMap"])
        if idx is not None:
            strength = float(d.get("aoIntensity", 1))
            out["occlusionTexture"] = ref(idx, "ao",
                                          **({"strength": strength} if strength != 1 else {}))
    if d.get("emissiveMap") is not None:
        idx = tex.plain(d["emissiveMap"])
        if idx is not None:
            out["emissiveTexture"] = ref(idx, "emissive")
    # PlayCanvas multiplies the emissive colour by an intensity; glTF has only the colour, so fold it in
    # rather than lose it — an unlit sign at intensity 0.5 comes out twice as bright otherwise.
    intensity = float(d.get("emissiveIntensity", 1))
    emissive = [float(c) * intensity for c in (d.get("emissive") or [0, 0, 0])[:3]]
    if any(emissive):
        out["emissiveFactor"] = [min(1.0, c) for c in emissive]

    # Alpha. `BLEND_NONE` with a cutoff is a MASK; a cutoff whose alpha does not exist is nothing at
    # all, which is why this asks the IMAGE rather than trusting the material.
    blend = int(d.get("blendType", BLEND_NONE))
    cutoff = float(d.get("alphaTest", 0) or 0)
    alpha_real = "baseColorTexture" in pbr and tex.has_alpha(pbr["baseColorTexture"]["index"])
    if blend in (BLEND_NORMAL, BLEND_PREMULTIPLIED) and (alpha_real or opacity < 1):
        out["alphaMode"] = "BLEND"
    elif cutoff > 0 and alpha_real:
        out["alphaMode"] = "MASK"
        out["alphaCutoff"] = cutoff
    elif cutoff > 0:
        warn.append(f"{name}: alphaTest {cutoff} on a texture with no alpha — left OPAQUE")
    if blend in (BLEND_ADDITIVE, BLEND_SUBTRACTIVE):
        warn.append(f"{name}: blend mode {blend} has no glTF equivalent — left OPAQUE")

    if int(d.get("cull", 1)) == CULLFACE_NONE:
        out["doubleSided"] = True
    moved = sorted({k[:-len(suffix)] for k, v in d.items() for suffix in ("MapTiling", "MapOffset",
                                                                          "MapRotation")
                    if k.endswith(suffix) and v
                    and list(v if isinstance(v, list) else [v]) not in ([1, 1], [0, 0], [0])})
    if moved:
        warn.append(f"{name}: {', '.join(moved)} map(s) are tiled, offset or rotated — not carried "
                    f"(needs KHR_texture_transform)")
    for label, used, lost in _UNCARRIED:
        if used(d):
            warn.append(f"{name}: {label} is set and not carried — {lost}")
    for message in warn:
        tex.warn(message)
    return out


# `_Textures.has_alpha` is defined out of line so `material_from` can ask it without reaching into
# internals: whether the image behind a texture index actually carries transparency.
def _textures_has_alpha(self, index: int) -> bool:
    return self.images[self.textures[index]["source"]]["mimeType"] == "image/png"


_Textures.has_alpha = _textures_has_alpha


# ---------------------------------------------------------------- assembly


def rebuild(glb: bytes, binds: list[Binding], build: Build, *,
            max_texture: int = 1024, quality: int = 90) -> tuple[bytes, list[str]]:
    """`(glb, notes)` — the container with its materials and textures written back into it.

    Additive: every primitive already carries an empty `material` slot, and the images become new buffer
    views on the end of the existing binary chunk. Nothing that was in the file moves, so the geometry,
    the skin and the bone names come out bit-identical and a map derived from the original still applies.
    """
    doc, blob = split_glb(glb)
    if not doc:
        return glb, ["not a binary glTF"]
    notes: list[str] = []
    tex = _Textures(build, max_texture, quality)

    materials: list[dict] = []
    by_pc: dict[int, int] = {}
    for bind in binds:
        mesh = (doc.get("meshes") or [])[bind.mesh] if bind.mesh < len(doc.get("meshes") or []) else None
        if mesh is None:
            notes.append(f"{bind.entity!r} binds mesh {bind.mesh}, which the file does not have")
            continue
        prims = mesh.get("primitives") or []
        if len(bind.materials) != len(prims):
            notes.append(f"{bind.entity!r}: {len(bind.materials)} material(s) for {len(prims)} "
                         f"primitive(s) on mesh {bind.mesh} — pairing as far as they go")
        for i, prim in enumerate(prims):
            pc = bind.materials[i] if i < len(bind.materials) else None
            if pc is None:
                continue
            if pc not in by_pc:
                # `is None` and not falsiness: a material whose settings are all defaults has an EMPTY
                # data bag, and dropping it would leave the primitive untextured for looking correct.
                data = (build.asset(pc) or {}).get("data")
                if data is None:
                    notes.append(f"material {pc} is bound but not in the registry")
                    continue
                materials.append(material_from(data, build.name(pc), tex))
                by_pc[pc] = len(materials) - 1
            prim["material"] = by_pc[pc]

    unbound = [i for i, m in enumerate(doc.get("meshes") or [])
               for p in (m.get("primitives") or []) if "material" not in p]
    if unbound:
        # Careful about what this claims. The scene does not leave these untextured — it does not
        # render them AT ALL, so coming out grey is the one outcome the source never produces. Saying
        # otherwise reads as reassurance, and on Jane that grey is her hair.
        notes.append(f"mesh(es) {sorted(set(unbound))} are bound by no entity and come out UNTEXTURED. "
                     f"The scene does not render them at all, so this is not what it does either — "
                     f"`--adopt` takes the material from an identically-named render asset elsewhere "
                     f"in the build, which is usually the same mesh shipped twice")
    if not materials:
        return glb, notes + ["nothing to bind"]

    # The images go on the end of the existing chunk, 4-byte aligned as buffer views require.
    buffer_views = doc.setdefault("bufferViews", [])
    out = bytearray(blob)
    for raw, image in zip(tex.blobs, tex.images):
        out += b"\x00" * (-len(out) % 4)
        buffer_views.append({"buffer": 0, "byteOffset": len(out), "byteLength": len(raw)})
        out += raw
        image["bufferView"] = len(buffer_views) - 1
        if image.get("name") is None:
            image.pop("name")
    doc["buffers"] = [{"byteLength": len(out)}]
    doc["images"] = tex.images
    doc["samplers"] = [{"wrapS": 10497, "wrapT": 10497}]         # REPEAT/REPEAT, as PlayCanvas defaults
    doc["textures"] = [{**t, "sampler": 0} for t in tex.textures]
    doc["materials"] = materials
    notes.extend(tex.warnings)
    return write_glb(doc, bytes(out)), notes


def report_orphans(root: str, say: Callable[[str], None]) -> int:
    """Say what a registry-less asset tree is missing, and where to get it. Returns how many there are."""
    orphans = find_orphans(root)
    for o in orphans:
        kinds = ", ".join(f"{n} {k}" for k, n in sorted(o.kinds.items(), key=lambda kv: -kv[1]))
        say(f"\n{os.path.relpath(o.root, root) or '.'} — {o.assets} asset file(s) ({kinds}) and NO "
            f"config.json")
        say("    The layout is right and the GLBs are fine; what is missing is the registry, so nothing "
            "says which material goes where.")
        if o.origin:
            say(f"    Fetch:  {o.origin}")
            say("            ...then the scene file it names under `scenes[].url`, and the texture "
                "assets it lists.")
        else:
            say("    This is the app's own build; its config.json sits at the root of wherever the "
                "page was served from.")
    return len(orphans)


def rebuild_build(root: str, out_dir: str, *, max_texture: int = 1024, quality: int = 90,
                  only: str = "", adopt: bool = False, fetch_list: Optional[list] = None,
                  report: Optional[Callable[[str], None]] = None) -> list[str]:
    """Convert every container in every build under `root`. Returns the files written."""
    say = report or (lambda _s: None)
    report_orphans(root, say)
    written: list[str] = []
    for build_root in find_builds(root):
        build = read_build(build_root)
        build.origin = build_origin(root, build_root)
        if adopt:
            adopt_unbound(build)
        groups = by_container(build)
        if not groups:
            continue
        say(f"\n{os.path.relpath(build_root, root) or '.'} — {len(build.assets)} assets, "
            f"{len(groups)} container(s) bound")
        for note in build.notes:
            say(f"    ! {note}")
        absent = missing_files(build)
        if absent:
            say(f"    ! {len(absent)} referenced file(s) are not in this capture "
                f"({', '.join(n for n, _ in absent[:3])}{', ...' if len(absent) > 3 else ''})"
                + (" — use --fetch-list to write the URLs" if build.origin else ""))
            if fetch_list is not None:
                fetch_list.extend(u for _n, u in absent if build.origin)
        for container, binds in sorted(groups.items()):
            name = build.name(container)
            path = build.path(container)
            if only and only.lower() not in name.lower():
                continue
            if not path or not os.path.exists(path):
                say(f"    ! {name}: the container GLB is not on disk")
                continue
            data, notes = rebuild(open(path, "rb").read(), binds, build,
                                  max_texture=max_texture, quality=quality)
            stem = os.path.splitext(os.path.basename(path))[0]
            os.makedirs(out_dir, exist_ok=True)
            dest = os.path.join(out_dir, f"{stem}.glb")
            open(dest, "wb").write(data)
            written.append(dest)
            grew = len(data) / max(1, os.path.getsize(path))
            say(f"    {name:28} -> {os.path.relpath(dest)}  "
                f"{os.path.getsize(path)/1e6:.1f} MB -> {len(data)/1e6:.1f} MB ({grew:.1f}x)")
            for bind in binds:
                say(f"        mesh {bind.mesh} [{bind.entity}]:{' ~' if bind.adopted else ''} "
                    + ", ".join(build.name(m) for m in bind.materials))
            for note in notes:
                say(f"        ! {note}")
    return written
