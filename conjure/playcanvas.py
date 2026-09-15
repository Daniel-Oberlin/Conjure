"""Re-assemble a PlayCanvas published build into self-contained, textured GLBs.

Spec: docs/specs/captures.md.

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
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from .figures import split_glb, write_glb

# ---------------------------------------------------------------- the engine's own constants
#
# Read out of `playcanvas-stable.min.js` in the build being converted. Listed here because the
# translation is meaningless without them and "3" reads like "the third blend mode" rather than "none".

BLEND_SUBTRACTIVE, BLEND_ADDITIVE, BLEND_NORMAL, BLEND_NONE, BLEND_PREMULTIPLIED = 0, 1, 2, 3, 4
BLEND_MULTIPLICATIVE, BLEND_ADDITIVEALPHA, BLEND_MULTIPLICATIVE2X, BLEND_SCREEN = 5, 6, 7, 8
CULLFACE_NONE = 0

#: Blend modes where BLACK contributes nothing — the layer only ever lightens. glTF has none of them,
#: and the choice of what to do instead is not free: rendered OPAQUE such a layer OCCLUDES whatever it
#: was meant to enhance. One model's corneas are pure black on `BLEND_SCREEN`, invisible by design, and
#: as opaque geometry they turned both her eyes into solid black discs.
LIGHTENING_BLENDS = (BLEND_ADDITIVE, BLEND_ADDITIVEALPHA, BLEND_SCREEN)

#: THREE ways a layer can be all environment and no content, and glTF expresses none of them: a blend
#: mode that only lightens (black is the no-op), one that modulates (white is), and a see-through
#: MIRROR whose colour is entirely a reflection. Each is made invisible when it carries no detail of
#: its own, because drawn opaque each one OCCLUDES the thing it was meant to enhance — and in all three
#: cases the thing behind it was an eye.
#:
#: And the mirror image: blend modes that MODULATE what is behind them, where WHITE is the no-op. Same
#: problem, opposite neutral colour — the stewardess's corneas are an untextured white on
#: `BLEND_MULTIPLICATIVE2X`, a highlight over an eyeball the body mesh draws underneath, and as opaque
#: geometry they became solid white discs. Her irises were there the whole time, behind them.
#:
#: Both lists are one idea: a blend glTF cannot express, on a layer carrying no detail of its own. What
#: differs is only which colour means "contribute nothing".
MODULATING_BLENDS = (BLEND_MULTIPLICATIVE, BLEND_MULTIPLICATIVE2X)

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
    # WHERE the claim came from: a scene is what runs, a template is the container's own default. Once a
    # build has scenes, a template-only binding is the signal for a mesh the site never draws — see
    # `dead_meshes`.
    source: str = "scene"


@dataclass
class Build:
    """A published PlayCanvas build: its asset registry, and what its scenes bind to what."""

    root: str
    assets: dict[int, dict]
    bindings: list[Binding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    origin: str = ""               # the URL it was captured from, when the path says (see `build_origin`)
    scened: bool = False           # at least one scene file was READ — see `dead_mesh_notes`
    # `(file name, entities)` per scene actually read. Kept because `things` needs the entity TREE and
    # not just the bindings it flattens to — which is the whole of § 2b: a container is a file and a
    # thing is a subtree, and flattening to (container, mesh) throws the subtree away.
    scenes: list = field(default_factory=list)
    templates: list = field(default_factory=list)   # `(name, entities)` per template — see `things`
    prefer: str = "scene"          # which claim wins where a scene and a template disagree
    # Every `(container, mesh)` a SCENE entity claims, regardless of whose MATERIALS won. Kept apart
    # from `Binding.source` because those answer different questions, and conflating them made
    # `dead_meshes` call Alice's `CC_Base_Eye` dead: the scene draws it, and the template's richer
    # materials merely won the tie-break.
    scene_claims: set = field(default_factory=set)

    def asset(self, aid) -> dict:
        return self.assets.get(int(aid)) if aid is not None else None

    def path(self, aid) -> Optional[str]:
        return self.locate(((self.asset(aid) or {}).get("file") or {}).get("url"))

    def locate(self, url: Optional[str]) -> Optional[str]:
        """Where a registry URL landed on disk, PERCENT-DECODED.

        A registry records `JAPANESEROOM%20BAKED.glb` and a browser saves
        `JAPANESEROOM BAKED.glb`, so joining the raw URL finds nothing and the
        container reads as missing while sitting right there. Any asset with a
        space or a non-ASCII character in its name hits this; on the capture that
        found it, the one casualty was the room.

        The raw form is tried as a fallback, because a file whose name genuinely
        contains a `%` is likelier than a capture tool that re-encodes.
        """
        if not url:
            return None
        decoded = urllib.parse.unquote(url)
        # `.png` last: decoding a Basis variant always produces a PNG, whatever the
        # registry calls the original. One texture here is named `.jpeg` and its
        # only surviving form is `Hands_diffuse_2K.basis`, so the decoded file and
        # the recorded name agree about everything except the extension.
        stem = os.path.splitext(decoded)[0] + ".png"
        for candidate in (decoded, url, stem):
            full = os.path.join(self.root, candidate)
            # Size, not existence. A failed download can leave a zero-byte file
            # behind, and an empty texture is not "present" in any sense that
            # helps — treated as there, it reads as a capture that succeeded and
            # then breaks the image decoder instead.
            if os.path.exists(full) and os.path.getsize(full) > 0:
                return full
        return os.path.join(self.root, decoded)

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


def _variant_urls(asset: dict) -> list[str]:
    return [v.get("url") for v in (((asset.get("file") or {}).get("variants")) or {}).values()
            if v.get("url")]


def missing_files(build: Build, *, kinds=("texture", "container")) -> list[tuple[str, str]]:
    """`[(name, url-or-"")]` for every asset the registry references and the capture does not hold.

    Reported as a LIST rather than one warning per use, because a texture shared by six materials went
    missing six times in the log — Akari's build references 105 textures and holds none of them, which
    was 200-odd identical lines burying the two findings that mattered.

    A texture present only as a compressed VARIANT is not missing and is excluded here; see
    `variant_only`, which is a different problem with a different answer.
    """
    out: list[tuple[str, str]] = []
    for aid, asset in sorted(build.assets.items()):
        if asset.get("type") not in kinds:
            continue
        url = (asset.get("file") or {}).get("url")
        if not url or os.path.exists(build.locate(url) or ""):
            continue
        if any(os.path.exists(build.locate(v) or "") for v in _variant_urls(asset)):
            continue
        out.append((asset.get("name") or str(aid), f"{build.origin}/{url}" if build.origin else url))
    return out


def variant_only(build: Build) -> list[tuple[str, str, str]]:
    """`[(name, the variant on disk, the url of the form we CAN read)]`.

    PlayCanvas transcodes textures to Basis Universal and the engine asks for that in preference, so a
    capture made by browsing holds `.basis` and never the PNG beside it — 91 of Akari's 105 textures
    declare one. Basis is a GPU-compressed format that Pillow cannot open and decoding it wants a
    transcoder this does not carry, so the honest report is "here is the file you actually need", with
    the original's URL, rather than a texture silently coming out untextured.
    """
    out: list[tuple[str, str, str]] = []
    for aid, asset in sorted(build.assets.items()):
        if asset.get("type") != "texture":
            continue
        url = (asset.get("file") or {}).get("url")
        if not url or os.path.exists(build.locate(url) or ""):
            continue
        on_disk = next((v for v in _variant_urls(asset)
                        if os.path.exists(build.locate(v) or "")), "")
        if on_disk:
            out.append((asset.get("name") or str(aid), os.path.basename(on_disk),
                        f"{build.origin}/{url}" if build.origin else url))
    return out


def how_dressed(build: Build, mats) -> tuple[int, int]:
    """`(primitives with a BASE COLOUR, filled map slots)` — how DRESSED a claim on a mesh is.

    The tie-break when several entities claim one mesh. First-wins was arbitrary and it picked wrong:
    three entities bind Jane's right hand, and the one the scene happens to list first is
    `handmodeltutorial`, wearing a placeholder with nothing but a sphere map. Her left hand has two
    claimants and got the real material by luck, so the pair came out mismatched — a textured left hand
    and a chrome right one — with nothing missing from the capture to explain it.

    A placeholder is a DEGENERATE version of the real material, so the richest claim is the authored
    one. Ties keep first-wins, which is what separates `ArmsVR` from `ArmsVRToolMode`: both fill two
    slots, and the plain one is listed first.

    **A base colour outranks a slot count, and Alice's hair is why.** Her two claims on `Side_Swept`
    fill six slots each and are not the same: one primitive of the losing set carries a SPECULAR map
    where the winning set carries a diffuse, so it renders as flat paint — pale locks at the front of a
    brown head. Slots alone score that a tie and pick by luck. What decides whether a primitive is a
    picture or a colour is whether it has a base colour at all, so that is counted first.

    Slots are counted as FILLED, not as distinct textures. A build reuses one image across several
    slots far more than you would guess — 33 of the 43 mapped materials in Jane's build point every map
    they have at a single texture, the hands among them, where `Hands_diffuse_2K.jpeg` is both the
    diffuse and the light map. Counting distinct textures scores all three claimants 1 and decides
    nothing.
    """
    base = slots = 0
    for material in mats:
        data = (build.asset(material) or {}).get("data") or {}
        base += 1 if data.get("diffuseMap") else 0
        slots += sum(1 for key, value in data.items() if key.endswith("Map") and value)
    return base, slots


def read_build(root: str, *, prefer: str = "scene") -> Build:
    """Load a build and resolve every entity → container → mesh → per-primitive material binding.

    **`prefer` is the one real judgment in this module, and it decides which of two twins survives.**

    A mesh can be claimed twice: by a SCENE entity and by the container's own TEMPLATE. Scenes are read
    first and `setdefault` keeps the first claim, so **the scene wins** — defensible, because a template
    is the default a container shipped with and a scene is what actually runs. It is still a choice, and
    it is the choice that makes the Japanese house's deck and Alice's `Scalp_Female` disappear rather
    than be painted.

    **`prefer` outranks richness, and only for a scene-vs-template disagreement.** Where two claims
    share a source — three scene entities binding Jane's right hand, one of them a placeholder with a
    single sphere map — the best-dressed still wins, which is what that tie-break was written for.
    Across a scene/template split it does not: of 1,340 meshes both claim, 952 are equally dressed and
    384 favour the scene either way, but 4 have a better-dressed TEMPLATE, and there "what actually
    runs" has to outrank "what has more maps" or this knob means nothing.

    `prefer="template"` reverses it, for looking at what a container shipped with — a bad default and a
    useful question. Note what it does NOT change: a mesh claimed by nobody at all stays unclaimed
    either way, which is the three body twins, so this knob cannot bring those back (see
    `adopt_unbound` for that, and `things` for why neither is needed once conversion walks scenes).
    """
    cfg = json.load(open(os.path.join(root, "config.json")))
    assets = {int(k): v for k, v in (cfg.get("assets") or {}).items()}
    build = Build(root=root, assets=assets)

    seen: dict[tuple[int, int], Binding] = {}
    clashes: dict[tuple[int, int], set] = {}

    def dressed(mats) -> tuple:
        return how_dressed(build, mats)

    def take(entities: dict, source: str = "scene") -> None:
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
            if source == "scene":
                build.scene_claims.add(key)
            bound = Binding(entity.get("name") or "?", key[0], key[1], mats, source=source)
            if key in seen and seen[key].source != source:
                # A scene-vs-TEMPLATE disagreement is settled by `prefer`, not by richness. Measured
                # over the captures: of 1,340 meshes both claim, 952 are equally dressed (order decides)
                # and 384 favour the scene either way — but 4 have a better-dressed TEMPLATE, and for
                # those "what actually runs" has to beat "what has more maps" or the knob means nothing.
                if source == prefer and seen[key].source != prefer:
                    clashes.setdefault(key, set()).add(seen[key].entity)
                    seen[key] = bound
                continue
            if key in seen and seen[key].materials != mats:
                # Two entities dressing the same mesh differently is a legitimate thing to do — a props
                # library reuses one button mesh in a dozen colours — and it has no single answer in a
                # file format that allows one material per primitive. The best-dressed wins, and the
                # clash is reported ONCE however many instances there are, because a props library
                # produces dozens of them and they would bury everything else.
                clashes.setdefault(key, set()).add(bound.entity)
                if dressed(mats) > dressed(seen[key].materials):
                    clashes[key].add(seen[key].entity)
                    clashes[key].discard(bound.entity)
                    seen[key] = bound
                continue
            seen.setdefault(key, bound)

    def read_scenes() -> bool:
        got = False
        for scene in cfg.get("scenes") or []:
            url = scene.get("url")
            if not url or not os.path.exists(os.path.join(root, url)):
                build.notes.append(
                    f"scene {scene.get('name')!r} is referenced but not on disk ({url}) — "
                    f"falling back to the templates, which carry the CONTAINER's own bindings and not "
                    f"the scene's. Materials may be silently wrong: one room came out with flat grey "
                    f"cushions and panelling this way, while the textures for both sat decoded on "
                    f"disk. Capture the scene.")
                continue
            loaded = json.load(open(os.path.join(root, url))).get("entities") or {}
            take(loaded)
            build.scenes.append((os.path.basename(url), loaded))
            got = True
        return got

    def read_templates() -> None:
        for asset in build.assets.values():
            if asset.get("type") == "template":
                entities = (asset.get("data") or {}).get("entities") or {}
                take(entities, source="template")
                build.templates.append((asset.get("name") or str(asset.get("id")), entities))

    # TEMPLATES are the same structure and a second place the binding lives. Read AFTER the scenes by
    # default, so a scene wins where both speak — it is what actually runs. See `prefer`.
    #
    # Not a fallback bolted on. A PlayCanvas template is a serialised entity hierarchy, which is how a
    # reusable thing is packaged, and a character is exactly that: the second capture to arrive had NO
    # scene file on disk and sixteen templates, one per skin-tone variant of the same model, each
    # binding its OWN container. So there is no ambiguity to resolve — reading them is what makes that
    # capture convertible at all, and it costs nothing where a scene is present because identical
    # bindings are deduplicated rather than reported as a clash.
    if prefer == "template":
        read_templates()
        read_a_scene = read_scenes()
    else:
        read_a_scene = read_scenes()
        read_templates()
    build.prefer = prefer
    for (container, index), others in sorted(clashes.items()):
        build.notes.append(
            f"{build.name(container)} mesh {index}: {len(others)} other entity binding(s) disagree "
            f"({', '.join(sorted(others)[:3])}...) — keeping {seen[(container, index)].entity!r}"
            if len(others) > 3 else
            f"{build.name(container)} mesh {index}: {', '.join(sorted(others))} bind different "
            f"materials — keeping {seen[(container, index)].entity!r}")
    build.bindings = sorted(seen.values(), key=lambda b: (b.container, b.mesh))
    build.scened = read_a_scene
    for note in dead_mesh_notes(build):
        build.notes.append(note)
    for note in dangling_notes(build):
        build.notes.append(note)
    return build


def container_meshes(build: Build, container: int) -> list[tuple[str, tuple[int, ...]]]:
    """`(node name, per-primitive vertex counts)` for each mesh of a container GLB, in mesh order.

    The container file, not the registry. A render asset is the registry's NAME for a mesh, and a build
    does not always have one: `office-babe.glb` holds nine meshes and the registry describes eight,
    starting at index 1. The missing one is her body.
    """
    path = build.path(container)
    if not path or not os.path.exists(path):
        return []
    try:
        doc, _bin = split_glb(open(path, "rb").read())
    except Exception:                                       # noqa: BLE001  (a bad file is not fatal here)
        return []
    if not doc:
        return []
    names: dict[int, str] = {}
    for node in doc.get("nodes") or []:
        if node.get("mesh") is not None:
            names.setdefault(int(node["mesh"]), node.get("name") or "")
    accessors = doc.get("accessors") or []
    out = []
    for i, mesh in enumerate(doc.get("meshes") or []):
        counts = []
        for prim in mesh.get("primitives") or []:
            pos = (prim.get("attributes") or {}).get("POSITION")
            n = int((accessors[pos] or {}).get("count") or 0) if isinstance(pos, int) and pos < len(accessors) else 0
            counts.append(n)
        # All or nothing. A partial count cannot align two primitive lists, and an empty tuple is the
        # honest way to say so — `adopt_unbound` then adopts the materials as they are, as it always did.
        out.append((names.get(i) or mesh.get("name") or "", tuple(counts) if all(counts) else ()))
    return out


def regroup_materials(donor: tuple[int, ...], mats: tuple, counts: tuple[int, ...]) -> Optional[tuple]:
    """Carry a donor's per-primitive materials onto the same mesh split into different primitives.

    Two copies of one mesh need not be split the same way, because a glTF primitive break IS a material
    break: the copy that was exported with materials splits where they change, and the copy that was
    exported without them does not. Office-babe's body is 5 primitives in her own container and 6 in
    `manager_fixing.glb`, which the scene actually renders — the difference is the mouth, one span there
    and two here, both wearing `Mouth`.

    So match by VERTEX COUNT and refuse anything else. Consume donor primitives until they sum to the
    recipient's, and take their material only if they agree on one; a span covering two materials has no
    answer, and a sum that never lands means these are not the same mesh and nothing should be adopted.
    """
    out, i = [], 0
    for want in counts:
        got, span = 0, []
        while i < len(donor) and got < want:
            got += donor[i]
            span.append(mats[i] if i < len(mats) else None)
            i += 1
        if got != want or len(set(span)) != 1:
            return None
        out.append(span[0])
    return tuple(out) if i == len(donor) else None


@dataclass
class Node:
    """One entity of a thing's subtree, exactly as the scene states it.

    The subtree is kept whole rather than flattened into per-piece transforms because it IS the
    skeleton: measured across every case in this corpus, 100% of a container's joints appear here as
    entities (181/181 office-babe across three containers, 222/222 bride, 85/85 Alice). Composing from
    the scene therefore needs no skeleton merge at all — there is one skeleton because the scene has
    one — and it sidesteps the trap that a container's own root transform is reproduced by the entity
    that instantiates it, so copying both applies it twice (bride, 100× out).
    """

    name: str
    parent: int                                 # index in `Thing.tree`; -1 for the thing's own root
    position: tuple
    rotation: tuple                             # euler DEGREES in PlayCanvas order — `compose.quat_from_euler`
    scale: tuple
    enabled: bool                               # this entity's OWN flag, uncomposed


@dataclass
class Piece:
    """One mesh a THING draws, plus everything about it the container file does not hold.

    A container is a file and a piece is a use of it: the same mesh appears twice in bride's heels, once
    per foot, and the difference between them lives entirely here.
    """

    container: int
    mesh: int                                   # `renderIndex` inside that container
    materials: tuple[Optional[int], ...]
    entity: str                                 # the scene entity that draws it
    parent: str                                 # what it hangs off — `DEF-hand.R` for Oktoberfest's beer
    path: tuple[str, ...]                       # entity names from the thing's root down to here
    enabled: bool                               # this entity AND every ancestor up to the thing root
    position: tuple = (0.0, 0.0, 0.0)           # composed down `path`, in the thing's own frame
    scale: tuple = (1.0, 1.0, 1.0)
    rotated: bool = False                       # a non-zero rotation appears in the chain — see `things`
    # Every LINK of that chain, `(name, position, rotation, scale)` from the thing's root down to this
    # entity inclusive, each transform the entity's OWN. `position`/`scale` above are a summary and are
    # exact only while the chain is unrotated; 538 of 948 pieces across the twenty captures are not, so
    # anything that has to place geometry rebuilds the chain from here instead of reading the summary.
    chain: tuple = ()
    node: int = -1                              # the entity that draws it, as an index into `Thing.tree`
    # Another piece draws this same mesh in this same place, better dressed — see `_shadow_variants`.
    # A VARIANT, not an instance, and not the scene's wardrobe switch either: emitted hidden.
    shadowed: bool = False


@dataclass
class Thing:
    """One addressable thing in a scene: a character, a room, a prop. The unit a container is NOT.

    A container is one artist's export; a thing is what the app places. The two disagree in both
    directions — office-babe draws from three containers, and `TOOLS LIBRARYblend5.glb` is split into
    fifteen separate props — which is the whole argument for reading scenes
    (`docs/plans/figures-and-library.md` § 2b).
    """

    name: str                                   # the entity name; becomes the asset label
    scene: str                                  # which scene file said so
    enabled: bool                               # placed in this scene, or sitting in its catalogue
    entities: int                               # subtree size, for reporting
    pieces: list[Piece] = field(default_factory=list)
    tree: list = field(default_factory=list)    # the whole entity subtree, parents before children
    # The thing entity's OWN transform. Its scale belongs to the thing — `Banana` is a 0.5 wrapper around
    # a 0.9166 mesh and a banana that skips it comes out twice life size — but its POSITION is where this
    # scene put it, which is not a property of the thing and does not travel with it.
    position: tuple = (0.0, 0.0, 0.0)
    rotation: tuple = (0.0, 0.0, 0.0)
    scale: tuple = (1.0, 1.0, 1.0)

    @property
    def containers(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for p in self.pieces:
            out[p.container] = out.get(p.container, 0) + 1
        return out

    @property
    def live(self) -> list[Piece]:
        return [p for p in self.pieces if p.enabled and not p.shadowed]

    @property
    def shadowed(self) -> list[Piece]:
        """Alternates: one mesh in one place claimed twice, the losing claim. See `_shadow_variants`."""
        return [p for p in self.pieces if p.shadowed]

    @property
    def optional(self) -> list[Piece]:
        """Drawn by an entity the scene has switched OFF — the site's own wardrobe switch.

        `underwear` is exactly this in office-babe, Oktoberfest and bride, and it is what the parts
        classifier has been reconstructing from mesh names. Emit it hidden rather than dropping it.
        """
        return [p for p in self.pieces if not p.enabled and not p.shadowed]


def thing_notes(build: Build, ts: Optional[list] = None, **kw) -> list[str]:
    """What the thing rule PROPOSED, per scene, so a person can see it and disagree.

    The rule is convention (see `things`), so this is the whole of its accountability: it prints the
    candidates with their subtree size and piece count and names no winner. The numbers are usually
    enough — a prop is 2 entities and 1 piece, while the app shell's `Gestures` is 380 entities and 1
    piece and is obviously not a thing anybody wants to place.
    """
    # `**kw` reaches `things` so a report describes what was actually CHOSEN — capture overrides and
    # exclusions included — rather than what the bare convention would have proposed.
    ts = things(build, **kw) if ts is None else ts
    if not ts:
        return []
    out = []
    for scene in sorted({t.scene for t in ts}):
        group = [t for t in ts if t.scene == scene]
        on = sum(1 for t in group if t.enabled)
        shown = ", ".join(f"{t.name}({t.entities}e/{len(t.pieces)}p)" for t in group[:6])
        out.append(f"{scene}: {len(group)} candidate thing(s), {on} enabled — {shown}"
                   f"{', …' if len(group) > 6 else ''}. Which entity is a THING is this app's "
                   f"convention and not the format; correct it per capture rather than trusting it")
    return out


def _vec(entity: dict, key: str, default: tuple) -> tuple:
    value = entity.get(key)
    if not isinstance(value, list) or len(value) != 3:
        return default
    try:
        return tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return default


THINGS_FILE = "things.json"


def load_thing_rules(paths: Optional[list] = None) -> dict:
    """`things.json` — which entity in a captured scene counts as a THING.

    DATA rather than code for the same reason `parts.json` is: the answer is a CONVENTION of the site
    being captured, not a property of glTF or PlayCanvas, so the next capture can arrive shaped a third
    way and correcting it should be an edit. A user file shadows the bundled one entirely — merging two
    would make the result depend on rule order across files nobody can see at once.
    """
    from . import config                                  # noqa: PLC0415  (config imports late)
    from pathlib import Path
    search = paths if paths is not None else [config.CONFIG_DIR / "captures",
                                              config.BUNDLED_CAPTURES_DIR]
    for base in search:
        candidate = Path(base) / THINGS_FILE
        if candidate.exists():
            try:
                return json.loads(candidate.read_text())
            except Exception:                             # noqa: BLE001 — a broken edit must not stop a build
                print(f"[conjure] thing rules at {candidate} are not readable JSON — ignoring them")
    return {"revision": 0, "exclude": [], "captures": {}}


def things(build: Build, *, roots: Optional[dict] = None, capture: str = "",
           rules: Optional[dict] = None) -> list[Thing]:
    """Every thing each of this build's scenes places, with the pieces it draws and from where.

    **Which entity is a thing is CONVENTION, not format.** A render component naming
    `(container, renderIndex, materialAssets)`, an entity carrying `enabled` / a parent / a transform,
    and therefore "a mesh no entity binds is not rendered" — all of that is the PlayCanvas data model
    and holds anywhere. This does not: the default rule here is *a direct child of a scene's Root whose
    subtree renders something*, which is true of the content scenes and **false of the app's own**.
    `2049393.json` has 1,016 entities whose Root children are `ToolModeStore`, `TRASH`, `DemoVRHoloes`
    and `SampleStore` — machinery groupings nested several levels deep, not things.

    So the rule PROPOSES and the caller can correct it: `roots` maps a scene's file name to the entity
    names to treat as things, and whatever is used is reported. The same discipline `parts/parts.json`
    uses for garment words and `adopt_unbound` uses when it prints INFERRED — a heuristic that fires
    where it can be inspected, rather than one buried in a conversion.

    `enabled` is composed down the chain, because an ancestor switched off takes its children with it.
    At THING level the flag means something else again — in the catalogue, not placed in this scene —
    which is the entire props library, so it is recorded on the `Thing` and never used to skip it.
    """
    # Scenes when there are any, TEMPLATES when there are not. Two of 58 builds declare a scene that is
    # not on disk, and a template IS a serialised entity hierarchy — same shape, same walk — so falling
    # back is what makes those builds readable at all rather than a special case for them.
    rules = load_thing_rules() if rules is None else rules
    # `roots=` is the explicit override a caller passes; the FILE is the durable one, keyed by capture
    # then scene. The file's entry wins where both speak, because it is the reviewed answer.
    per_capture = (rules.get("captures") or {}).get(capture or os.path.basename(build.root.rstrip("/")))
    excluded = set(rules.get("exclude") or ())
    out: list[Thing] = []
    # A scene wraps its things in a `Root`, so the THINGS are Root's children. A template IS one thing
    # already — `VR_hand_R` is 53 entities under a single root — so there the root is the thing itself.
    # Treating them alike returned a hand's fingers as three separate things.
    for name, entities, nested in ([(n, e, True) for n, e in build.scenes] or
                                   [(n, e, False) for n, e in build.templates]):
        if not entities:
            continue
        claimed = {c for e in entities.values() for c in (e.get("children") or [])}
        tops = [g for g in entities if g not in claimed]
        override = (per_capture or {}).get(name, (roots or {}).get(name))
        if override is not None:
            # An override NAMES the things, wherever they sit. susan's are `ModelParent` and
            # `EnvironmentVR`, two levels down inside `aula` — a filter over Root's children could only
            # ever have dropped her, which is what it did.
            # A list names them; a MAP also renames them, because the entity name is often the app's
            # internal one — susan's figure is `ModelParent`, which is no use as an asset label.
            wanted = dict(override) if isinstance(override, dict) else {n: n for n in override}
            candidates = [g for g, e in entities.items() if (e.get("name") or "?") in wanted]
        else:
            candidates = [g for top in tops for g in (entities[top].get("children") or [])] if nested \
                else tops
        for guid in candidates:
            if guid not in entities:
                continue
            # A template's root entity is called `RootNode` in six builds — the ASSET's name is the one
            # that means anything (`VR_hand_R`, `Dilda`), and it is what a label has to come from.
            label = entities[guid].get("name") or "?"
            if not nested and label in ("RootNode", "?"):
                label = name
            if override is None and label in excluded:
                continue                                  # the app's own machinery, site-wide
            thing = _walk(build, entities, guid, name)
            _shadow_variants(build, thing)
            thing.name = wanted.get(label, label) if override is not None else label
            if thing.pieces or override is not None:
                out.append(thing)
    return out


def _walk(build: Build, entities: dict, guid: str, scene: str) -> Thing:
    """Collect one thing's subtree: the entity TREE, and the pieces hanging off it.

    The tree is kept whole (see `Node`) because it is what the composer builds nodes from. The per-piece
    `chain`, `position` and `scale` are the same information summarised for reading and for reports.
    """
    top = entities[guid]
    thing = Thing(name=top.get("name") or "?", scene=scene,
                  enabled=top.get("enabled") is not False, entities=0)
    # Depth-first, and a parent is always appended before its children — so `Node.parent` always points
    # at an index that already exists and anything downstream can build the tree in one pass.
    stack = [(guid, (), (), True, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0), False, "", -1)]
    while stack:
        g, path, chain, on, pos, scl, rot, parent, up = stack.pop()
        if g not in entities:
            continue
        entity = entities[g]
        thing.entities += 1
        label = entity.get("name") or "?"
        here = path + (label,)
        # Compose down the chain. Scale multiplies and position accumulates through the parent's scale,
        # which is exact while every rotation is zero — and a non-zero one is FLAGGED rather than
        # silently mis-composed, because euler order is a decision this does not get to guess at.
        p = _vec(entity, "position", (0.0, 0.0, 0.0))
        s = _vec(entity, "scale", (1.0, 1.0, 1.0))
        r = _vec(entity, "rotation", (0.0, 0.0, 0.0))
        pos = tuple(pos[i] + p[i] * scl[i] for i in range(3))
        scl = tuple(scl[i] * s[i] for i in range(3))
        rot = rot or any(abs(v) > 1e-6 for v in r)
        links = chain + ((label, p, r, s),)
        if not path:
            thing.position, thing.rotation, thing.scale = p, r, s
        thing.tree.append(Node(name=label, parent=up, position=p, rotation=r, scale=s,
                               enabled=entity.get("enabled") is not False))
        mine = len(thing.tree) - 1
        # The THING's own flag is consumed by `Thing.enabled` and must NOT propagate: at that level it
        # means "in the catalogue, not placed in this scene", which is the entire props library — all
        # 15 tools and all 13 skin-tone variants are disabled Root children. Composing it would mark
        # every piece of every prop optional and import none of them. Below the root it means what it
        # says, and an ancestor switched off does take its children with it.
        if path:
            on = on and entity.get("enabled") is not False
        render = (entity.get("components") or {}).get("render")
        if render and render.get("type") == "asset" and render.get("asset") is not None:
            data = (build.asset(render["asset"]) or {}).get("data") or {}
            container, index = data.get("containerAsset"), data.get("renderIndex")
            if container is not None and index is not None:
                thing.pieces.append(Piece(
                    container=int(container), mesh=int(index),
                    materials=tuple(int(m) if m is not None else None
                                    for m in (render.get("materialAssets") or [])),
                    entity=label, parent=parent, path=here,
                    enabled=on and render.get("enabled") is not False,
                    position=pos, scale=scl, rotated=rot, chain=links, node=mine))
        for child in entity.get("children") or []:
            stack.append((child, here, links, on, pos, scl, rot, label, mine))
    thing.pieces.sort(key=lambda x: (x.container, x.mesh, x.entity))
    return thing


def _shadow_variants(build: Build, thing: Thing) -> None:
    """One mesh drawn twice in ONE place is a VARIANT; drawn twice in two places it is an instance.

    The distinction is the whole of it, and both halves are real in this corpus — exactly one case each,
    out of 948 pieces. Bride's heels are `clothes_weddingdress_heels_L` and `_R`: one mesh, two feet,
    two different chains, and flattening them loses a shoe. Alice's hair is `Side_Swept` and
    `Side_Swept2`: one mesh, one chain, both enabled, and two different sets of materials. That is a
    colour switch the site flips at runtime with a script the capture does not contain — and drawn
    together they z-fight, which is the pale locks at the front of her otherwise brown head.

    So: same mesh AND the same transform all the way up means one of them is an alternate. The
    best-dressed claim wins, on the same rule that settles it for a binding (`how_dressed` — a base
    colour outranks a slot count, which is what separates these two), and the loser is kept and marked
    rather than dropped. It is a wardrobe option the moment anything can switch it.
    """
    groups: dict[tuple, list[int]] = {}
    for i, piece in enumerate(thing.pieces):
        where = tuple((link[1], link[2], link[3]) for link in piece.chain)
        groups.setdefault((piece.container, piece.mesh, where), []).append(i)
    for members in groups.values():
        if len(members) < 2:
            continue
        best = max(members, key=lambda i: (how_dressed(build, thing.pieces[i].materials), -i))
        for i in members:
            if i != best:
                thing.pieces[i].shadowed = True


def dead_meshes(build: Build) -> list[Binding]:
    """Bindings that only a TEMPLATE claims, in a build whose scenes were read — probably not rendered.

    A template is the container's own default binding; a scene is what runs. `read_build` reads scenes
    first and templates after precisely so a scene wins, which means a surviving template binding is a
    mesh no scene entity asked for. **That is the signal for a mesh the site never draws**, and it is
    the only thing that identifies the whole family:

      · `JAPANESEROOM BAKED.glb` mesh 32 — a deck and railing, replaced in the scene by `WOODout.glb`
      · `aula_Aliceglb` mesh 14 `Scalp_Female` — wearing one of four scalp materials, the one with no
        maps, so it renders opaque WHITE. Her real scalp is a primitive of her hair mesh
      · office-babe's body twin, Oktoberfest's and bride's denser twins, bride's `clothes_sexyunderwear_*`

    **Scoped to containers the scene DOES use**, which is the difference between a signal and 725 rows
    of noise. A container no scene mentions at all is not full of dead meshes — it is a container this
    scene does not use, which is ordinary: the VR shell's controllers and the props library are bound
    only by templates in every capture. What is suspicious is a mesh whose SIBLINGS are scene-bound and
    which is not: the scene reached into that container, dressed the meshes it wanted, and left this one.

    Only meaningful once a scene has actually been read. Two of 58 builds have no scene file on disk,
    and there every binding is template-only and none of them is dead.

    Reported, never dropped. A dead mesh is a candidate for removal and the composition work is where
    that decision belongs (`docs/plans/figures-and-library.md` § 2b) — this tells you where to look.
    """
    if not build.scened:
        return []
    live = {c for c, _m in build.scene_claims}
    return [b for b in build.bindings
            if (b.container, b.mesh) not in build.scene_claims and b.container in live]


def dead_mesh_notes(build: Build) -> list[str]:
    out = []
    for container, group in sorted(by_container(Build(root=build.root, assets=build.assets,
                                                     bindings=dead_meshes(build))).items()):
        names = ", ".join(f"{b.mesh} [{b.entity}]" for b in group[:4])
        out.append(f"{build.name(container)}: {len(group)} mesh(es) bound ONLY by a template while this "
                   f"build's scenes were read — {names}{', …' if len(group) > 4 else ''}. A scene is "
                   f"what runs, so these are probably not drawn at all; converting them is how a dead "
                   f"twin or an empty material reaches the library looking like a bug")
    return out


def dangling(build: Build) -> dict[int, list[str]]:
    """Asset ids that something REFERENCES and the registry does not define — `{id: [who wants it]}`.

    Invisible to every other check, which is the point. `missing_files` walks the registry looking for
    files that are not on disk; an id the registry never had is not walked, so it reports nothing
    absent and the material converts flat. The Japanese house's deck material `WOODout` points at
    texture `194421251`, which is in no `config.json` — and *"no texture on the railing of the house"*
    cost a session and several re-downloads that could never have helped.

    This is the difference between **re-download this capture** and **the site ships it this way**, and
    nothing we produced could tell those apart.

    **Only what a BINDING depends on.** Scanning the whole registry finds 1,011 of these, almost all in
    the props library's materials — `SAUSAGE.diffuseMap`, `STICK.normalMap` — which nothing in the
    converted output binds, so they cost nobody anything. A reference is worth reporting when it is
    reachable from a mesh we actually emit.
    """
    want: dict[int, list[str]] = {}
    def need(aid, who: str) -> None:
        if aid is None:
            return
        try:
            key = int(aid)
        except (TypeError, ValueError):
            return
        if key not in build.assets:
            want.setdefault(key, []).append(who)

    for bind in build.bindings:
        where = f"{build.name(bind.container)} mesh {bind.mesh}"
        for mat in bind.materials:
            if mat is None:
                continue
            need(mat, f"{where} material")
            asset = build.asset(mat)
            if asset is None:
                continue
            for field_, value in (asset.get("data") or {}).items():
                if field_.endswith("Map"):
                    need(value, f"{where} material {asset.get('name') or mat!r}.{field_}")
    return want


def dangling_notes(build: Build) -> list[str]:
    want = dangling(build)
    if not want:
        return []
    shown = ", ".join(f"{k} ({want[k][0]})" for k in sorted(want)[:3])
    return [f"{len(want)} referenced asset id(s) are NOT IN THE REGISTRY at all — {shown}"
            f"{', …' if len(want) > 3 else ''}. Not a missing FILE: the id was never in config.json, so "
            f"re-capturing cannot help and `missing_files` reports nothing absent. Whatever points at "
            f"one converts flat"]


def adopt_unbound(build: Build) -> int:
    """Give an unbound mesh the material bound to an identically-named mesh elsewhere. Inferred.

    A character build routinely ships the same mesh twice: once inside the body container and once as a
    standalone container so it can be swapped. Jane's hair is both `jane_export.glb` mesh 3 and the whole
    of `hair.glb`, same 3,969 vertices, and the scene renders the standalone one — so the copy inside her
    body GLB is bound by nobody and comes out flat grey.

    Faithfully, that IS what the scene does. But a figure has to arrive as ONE file here, and a
    swappable-outfit vocabulary does not exist yet (specs/figures.md, § What is not built), so the
    useful answer and the literal answer differ. This is the useful one, kept behind a flag and reported
    as inferred every time, because a NAME matching is good evidence and not proof.

    Candidates come from the registry AND from the container files, because the registry can be silent.
    Office-babe's skin, hair and glasses came out matte black — the black of glTF's default material,
    metallic 1 — and her body was the reason: the scene renders it from `manager_fixing.glb`, so her own
    container's copy has no render asset, no name in the registry, and was invisible to a search that
    only ever looked at render assets. Her mesh is still called `Body` inside the file.
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

    # Only containers the build already binds something in. Enough to reach a stale sibling mesh, and it
    # keeps this from opening every GLB in a props library to look at meshes nothing refers to.
    geometry = {c: container_meshes(build, c) for c in sorted({b.container for b in build.bindings})}
    for container, meshes in geometry.items():
        for i, (name, _counts) in enumerate(meshes):
            renders.setdefault((container, i), name)
    for bind in build.bindings:                 # a bound mesh can be a donor under its in-file name too
        meshes = geometry.get(bind.container) or []
        if bind.mesh < len(meshes) and meshes[bind.mesh][0]:
            by_name.setdefault(meshes[bind.mesh][0], bind)

    adopted = 0
    for key, name in sorted(renders.items()):
        if key in bound or not name or name not in by_name:
            continue
        donor = by_name[name]
        mats, how = donor.materials, ""
        mine = geometry.get(key[0]) or []
        theirs = geometry.get(donor.container) or []
        if key[1] < len(mine) and donor.mesh < len(theirs):
            counts, donor_counts = mine[key[1]][1], theirs[donor.mesh][1]
            if counts and donor_counts and counts != donor_counts:
                mats = regroup_materials(donor_counts, donor.materials, counts)
                if mats is None:
                    build.notes.append(
                        f"{build.name(key[0])} mesh {key[1]} is bound by no entity and shares the name "
                        f"{name!r} with {build.name(donor.container)} mesh {donor.mesh}, but their "
                        f"primitives do not line up ({list(counts)} vs {list(donor_counts)}) — NOT "
                        f"adopting, they are not the same mesh")
                    continue
                how = (f", regrouped from {len(donor_counts)} primitive(s) to {len(counts)} by matching "
                       f"vertex counts")
        build.bindings.append(Binding(f"{donor.entity} (adopted by name {name!r})",
                                      key[0], key[1], mats, adopted=True))
        build.notes.append(f"{build.name(key[0])} mesh {key[1]} is bound by no entity; adopting the "
                           f"material from {build.name(donor.container)} mesh {donor.mesh}, which shares "
                           f"the name {name!r}{how} — INFERRED, look at it")
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

    def _open(self, path, name):
        """`load_image`, or None and a warning. One unreadable file must not take
        the whole run down with it — a capture with ten empty PNGs in it aborted
        every container, including the ones that were fine."""
        try:
            return load_image(path)
        except Exception as err:                      # noqa: BLE001 — any decoder failure
            self.warn(f"texture {name} could not be read ({type(err).__name__}) — that surface "
                      f"comes out untextured; re-capture the file")
            return None

    def plain(self, aid) -> Optional[int]:
        if self.build.asset(aid) is None:
            # A material naming a texture the registry does not contain. Not a
            # capture problem and not fixable by fetching: the asset was deleted
            # from the project and the material kept pointing at it, so the
            # surface is untextured in the source too.
            self.warn(f"a material points at texture asset {aid}, which is NOT IN THE REGISTRY — "
                      f"deleted from the project, so that surface has no texture at source either")
            return None
        path = self.build.path(aid)
        if not path or not os.path.exists(path):
            self.warn(f"texture {self.build.name(aid)} is missing from disk")
            return None
        img = self._open(path, self.build.name(aid))
        return self._add(("plain", int(aid)), img) if img is not None else None

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
        base = self._open(d_path, self.build.name(diffuse))
        mask = self._open(o_path, self.build.name(opacity))
        if base is None:
            return None
        if mask is None:
            return self._add(("plain", int(diffuse)), base)
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

        **The shininess scalar is BAKED IN when the map is a gloss map**, because the two models do not
        factor the same way. PlayCanvas computes `roughness = 1 - s*g`; glTF can only offer
        `roughnessFactor * roughnessTexture`, and `(1-s) * (1-g)` is a different surface — about 0.5
        too smooth across the whole range, which on skin is the difference between matte and wet. The
        product only agrees at `g = 1`. Baking `1 - s*g` per texel is exact, and the caller then sets
        `roughnessFactor` to 1.

        An INVERTED gloss map needs no baking: there `roughness = s*g`, which is a product already.
        """
        Image = _pil()
        gloss, metal = d.get("glossMap"), d.get("metalnessMap")
        if gloss is None and metal is None:
            return None
        key = ("mr", gloss, d.get("glossMapChannel"), metal, d.get("metalnessMapChannel"),
               bool(d.get("glossInvert")), round(float(d.get("shininess", 0) or 0), 3))
        if key in self._by_key:
            return self._by_key[key]
        size = None
        rough = metalness = None
        if gloss is not None and self.build.path(gloss) and os.path.exists(self.build.path(gloss)):
            src = self._open(self.build.path(gloss), self.build.name(gloss))
            if src is None:
                return None
            size = src.size
            rough = _channel(src, d.get("glossMapChannel"))
            if not d.get("glossInvert"):
                from PIL import ImageOps                        # noqa: PLC0415
                scalar = max(0.0, min(1.0, float(d.get("shininess", 0) or 0) / 100.0))
                if scalar != 1.0:                               # fold the scalar in: 1 - s*g, exactly
                    rough = rough.point(lambda v, k=scalar: int(round(v * k)))
                rough = ImageOps.invert(rough)                  # a gloss map is roughness upside down
        if metal is not None and self.build.path(metal) and os.path.exists(self.build.path(metal)):
            src = self._open(self.build.path(metal), self.build.name(metal))
            if src is None:
                return None if size is None else None
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

    # `diffuse` is a TINT, and PlayCanvas only applies it when asked. From the engine shipped in the
    # build being converted:
    #
    #     o = e.diffuseTint || (!e.diffuseMap && !e.diffuseVertexColor)
    #     t.diffuseTint = o ? 2 : 0            // 2 turns MAPCOLOR on
    #
    #     getAlbedo() { dAlbedo = vec3(1.0);
    #       #ifdef MAPCOLOR  dAlbedo *= material_diffuse.rgb;  #endif
    #       #ifdef MAPTEXTURE dAlbedo *= <the map>;            #endif }
    #
    # So with a map and `diffuseTint: false` the colour is DEAD DATA — the editor leaves whatever was
    # last set sitting in the field. glTF has no such switch: `baseColorFactor` always multiplies the
    # texture. Copying the field across regardless is how `AR_BlackGirl` came out rendering her skin
    # as pure black: `diffuse: [0, 0, 0]`, `diffuseTint: false`, a perfectly good 2K skin texture
    # underneath, multiplied to nothing. Her second material was the same bug 20% quieter — a stale
    # [0.8, 0.8, 0.8] dimming a texture the engine showed at full strength.
    diffuse = list(d.get("diffuse") or [1, 1, 1])[:3]
    opacity = float(d.get("opacity", 1) or 0)
    if not (d.get("diffuseTint") or not (d.get("diffuseMap") or d.get("diffuseVertexColor"))):
        diffuse = [1, 1, 1]
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
        if d.get("glossMap") is not None and not d.get("glossInvert"):
            # The scalar is already inside the texture (see `metallic_roughness`), so multiplying by it
            # again would apply it twice. This factor exists for the NO-TEXTURE case.
            pbr["roughnessFactor"] = 1.0

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
    # A third way a layer can be all environment and no content: a MIRROR. `useMetalness` with
    # `metalness: 1`, a gloss of 100, a skybox and no texture of its own is a reflection shell — the
    # wet film over an eye. glTF carries no per-material environment (see `_UNCARRIED`), so it renders
    # as a black mirror: the teacher's `Sclera` sits as its own primitive over the eye the body mesh
    # draws, and came out as a dark disc that reads as a closed eye.
    #
    # Gated on being SEE-THROUGH, which is the line between an overlay and an object. `Sclera` is 0.2,
    # `Cornea_v2` 0.2, `EyeMoisture` 0.5 — all films over something. `DIAMANT` is 0.963: also a
    # reflection material, also wrong without an environment, but there is nothing behind it, so a dark
    # gem is worse than no gem and it is left alone.
    mirror = (bool(d.get("useMetalness")) and float(d.get("metalness") or 0) >= 0.9
              and (d.get("useSkybox") or d.get("cubeMap") or d.get("sphereMap"))
              and d.get("diffuseMap") is None and opacity <= 0.6)
    # A refractive LENS: no maps at all, see-through, and its whole appearance is the environment bent
    # through it. glTF core cannot express that (`KHR_materials_transmission` can, at the cost of a
    # transmission pass this does not spend on a Quest), and the fallback is not neutral — a flat colour
    # at partial alpha is white PAINT over the thing it was meant to enhance. Bride's `Reflections-eyes`
    # is 48% white over her irises, and the brown came out washed to a pale tan. Five materials across
    # the twenty captures are this shape and four of them are named for an eye; the fifth is a pane of
    # glass, which is likewise better seen through than fogged.
    lens = (d.get("useDynamicRefraction") and blend in (BLEND_NORMAL, BLEND_PREMULTIPLIED)
            and opacity < 1 and not any(k.endswith("Map") and v for k, v in d.items()))
    lightening = blend in LIGHTENING_BLENDS
    modulating = blend in MODULATING_BLENDS
    if lens:
        out["alphaMode"] = "BLEND"
        pbr["baseColorFactor"] = [0.0, 0.0, 0.0, 0.0]
        warn.append(f"{name}: a refractive lens ({opacity:.2f} opacity, no maps of its own) — glTF core "
                    f"cannot bend what is behind it, so it is made invisible rather than drawn as a "
                    f"flat film over whatever it was meant to refract")
    elif mirror and not (lightening or modulating):
        out["alphaMode"] = "BLEND"
        pbr["baseColorFactor"] = [0.0, 0.0, 0.0, 0.0]
        warn.append(f"{name}: a see-through mirror ({opacity:.2f} opacity, metalness 1, reflecting a "
                    f"skybox glTF cannot carry) — it has no colour of its own, so it is made invisible "
                    f"rather than drawn as a black film over whatever it was meant to catch the light on")
    elif lightening or modulating:
        # The least-wrong translation: BLEND, so the alpha at least applies, and
        # fully transparent where the layer carries no detail of its own —
        # because that is a layer which contributes nothing, and saying so is
        # closer to the truth than drawing it.
        #
        # "No detail" means no texture AND the neutral colour for the family:
        # black where the mode only lightens, white where it modulates. Both
        # have cost a model its eyes — one pair went solid black, the other
        # solid white, and in each case the real eyeball was being drawn by the
        # body mesh directly behind the layer we had made opaque.
        out["alphaMode"] = "BLEND"
        neutral = (not any(diffuse)) if lightening else diffuse == [1, 1, 1]
        if "baseColorTexture" not in pbr and neutral:
            pbr["baseColorFactor"] = [0.0, 0.0, 0.0, 0.0]
            warn.append(f"{name}: blend mode {blend} "
                        f"{'only ever lightens' if lightening else 'modulates what is behind it'} and "
                        f"this material is a flat {'black' if lightening else 'white'} with no texture, "
                        f"so it contributes nothing — made invisible rather than drawn")
        else:
            warn.append(f"{name}: blend mode {blend} "
                        f"{'lightens' if lightening else 'modulates'} and glTF cannot express it — "
                        f"approximated as BLEND, which will look heavier than it should")
    elif d.get("alphaToCoverage") and alpha_real:
        # Alpha-to-coverage is a CUTOUT technique — it resolves a hard edge
        # through MSAA rather than blending — so glTF's MASK is the honest
        # equivalent even where the blend mode says otherwise. Getting this wrong
        # is not subtle: one build shares a single atlas between shorts, shirt and
        # hair with this mask selecting each garment's region, and translated as
        # BLEND the unselected regions came through as patches of the other
        # garments' colours instead of vanishing.
        out["alphaMode"] = "MASK"
        out["alphaCutoff"] = cutoff or 0.5
    elif blend in (BLEND_NORMAL, BLEND_PREMULTIPLIED) and (alpha_real or opacity < 1):
        out["alphaMode"] = "BLEND"
    elif cutoff > 0 and alpha_real:
        out["alphaMode"] = "MASK"
        out["alphaCutoff"] = cutoff
    elif cutoff > 0:
        warn.append(f"{name}: alphaTest {cutoff} on a texture with no alpha — left OPAQUE")
    if blend == BLEND_SUBTRACTIVE:
        # The multiplicative modes used to be warned about here and left opaque anyway. The warning was
        # right and nothing acted on it: the stewardess's corneas are `BLEND_MULTIPLICATIVE2X`, and
        # "left OPAQUE, which will look wrong" was printed while two white discs covered her irises.
        # They are handled above now, with the rest of the blends glTF cannot express.
        warn.append(f"{name}: blend mode {blend} subtracts from what is behind it and glTF cannot "
                    f"express that — left OPAQUE, which will look wrong")

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
        if lens and label == "refraction":
            continue                    # already reported above, and "will read as opaque" is now wrong
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

    # Materials the REGISTRY gives no texture at all. Worth separating from a
    # texture that failed to arrive: one is a capture to re-run, the other is how
    # the project was authored and no amount of downloading will change it.
    flat = [m["name"] for m in materials
            if "baseColorTexture" not in m.get("pbrMetallicRoughness", {})]
    if flat:
        notes.append(f"{len(flat)} material(s) carry no base-colour texture in the registry and come "
                     f"out flat-shaded ({', '.join(flat[:4])}"
                     f"{', ...' if len(flat) > 4 else ''}) — that is how the project is authored, "
                     f"not something missing from the capture")

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


BASIS_DECODER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "scripts", "basis_to_png.js")


def undecoded_basis(root: str) -> list[str]:
    """`.basis` files with no `.png` beside them — textures nothing downstream can read."""
    out = []
    for path, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not name.lower().endswith(".basis"):
                continue
            png = os.path.join(path, name[:-6] + ".png")
            # Size, not existence — the same rule `locate` uses. A failed download
            # leaves a zero-length PNG, and counting that as decoded left ten
            # textures empty with a good `.basis` sitting beside each one.
            if not (os.path.exists(png) and os.path.getsize(png) > 0):
                out.append(os.path.join(path, name))
    return out


def decode_basis(root: str, say: Callable[[str], None]) -> int:
    """Run the Basis pre-pass over `root`. Returns how many files it decoded.

    Called automatically, because leaving it to be remembered does not work: a
    fresh capture overwrites the decoded PNGs, and a rebuild then produces a
    figure textured only where a PNG happened to be served — outfit yes, skin no,
    room not at all. That looked like a capture problem and was not.

    It stays a separate script with its own output on disk. This only spares you
    from remembering to run it, and `--no-decode` opts out.
    """
    pending = undecoded_basis(root)
    if not pending:
        return 0
    node = shutil.which("node")
    if not node or not os.path.exists(BASIS_DECODER):
        say(f"    ! {len(pending)} Basis texture(s) are not decoded and node is "
            f"{'missing' if not node else 'available but the decoder is not'} — run "
            f"`node scripts/basis_to_png.js {root}` before rebuilding, or those textures are lost")
        return 0
    say(f"    decoding {len(pending)} Basis texture(s) first "
        f"(scripts/basis_to_png.js; --no-decode to skip)")
    result = subprocess.run([node, BASIS_DECODER, root], capture_output=True, text=True)
    if result.returncode != 0:
        say(f"    ! the Basis decoder failed: {(result.stderr or result.stdout).strip()[:200]}")
        return 0
    return len(pending) - len(undecoded_basis(root))


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
                  only: str = "", adopt: bool = False, decode: bool = True,
                  fetch_list: Optional[list] = None,
                  report: Optional[Callable[[str], None]] = None) -> list[str]:
    """Convert every container in every build under `root`. Returns the files written."""
    say = report or (lambda _s: None)
    if decode:
        decode_basis(root, say)
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
        compressed = variant_only(build)
        if compressed:
            say(f"    ! {len(compressed)} texture(s) remain ONLY as Basis-compressed variants "
                f"(e.g. {compressed[0][1]}) and will come out UNTEXTURED. Decode them with "
                f"`node scripts/basis_to_png.js {root}` — the uncompressed originals are also in "
                f"the fetch list if that fails")
            if fetch_list is not None:
                fetch_list.extend(u for _n, _v, u in compressed if build.origin)
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
