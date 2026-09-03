"""External asset importer — bring files from disk into the Conjure library.

An extensible ingest pipeline. Each asset family is an `AssetImporter` that (a) claims file
extensions, (b) confirms a file by magic bytes / decodability (`sniff`), and (c) `extract`s the
catalog metadata into an `ImportResult`. `plan_import()` picks a handler (by forced kind, filename
heuristic, or extension) and runs it. The server's `/library/import` endpoint then content-addresses
the bytes and catalogs the row via `register_asset` — so a new asset type is *one handler + one
registry entry*, with no schema change: kind-specific fields ride the catalog's JSON `attributes` bag.

This module has NO dependency on the running server (only stdlib + Pillow, and trimesh lazily for
models), so it's unit-testable in isolation and reusable by a future NAS `scan()`
(docs/backlogs/library.md). The bottom half is the `conjure-import` CLI: a thin HTTP client of the
world server.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------- extraction model


@dataclass
class ImportResult:
    """What a handler extracts from a file: the catalog `kind`, the storage extension, and whatever
    metadata could be read locally. Everything beyond the core columns rides `attributes`."""
    kind: str
    ext: str
    label: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    transparent: bool = False
    attributes: dict = field(default_factory=dict)
    licence: Optional[str] = None
    attribution: Optional[str] = None
    creator: Optional[str] = None


def _ext(filename: str) -> str:
    """Normalized storage extension for a filename ('.JPEG' → '.jpg'), lower-cased."""
    ext = Path(filename).suffix.lower()
    return ".jpg" if ext == ".jpeg" else ext


def _image_dims(data: bytes) -> Optional[tuple[int, int, bool]]:
    """(width, height, has-real-alpha) for image bytes, or None if it isn't a decodable image."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            alpha = im.mode in ("RGBA", "LA") or "transparency" in im.info
            return im.size[0], im.size[1], bool(alpha)
    except Exception:  # noqa: BLE001 — any decode failure means "not an image I can import"
        return None


# Stereo packing hints from a filename stem: the common suffixes camera/rig software writes.
_SBS_TAGS = ("_sbs", "_lr", "-sbs", "-lr", " sbs", " lr")
_TB_TAGS = ("_tb", "_ou", "-tb", "-ou", " tb", " ou")   # top-bottom / over-under


def _stereo_from_name(filename: str) -> Optional[str]:
    stem = Path(filename).stem.lower()
    if any(stem.endswith(t) for t in _TB_TAGS):
        return "tb"
    if any(stem.endswith(t) for t in _SBS_TAGS):
        return "sbs"
    return None


# ---------------------------------------------------------------------------- handlers


class AssetImporter:
    """Base handler. Subclass, set `kind`/`extensions`, and override `extract` (and `sniff` if a magic
    check is warranted)."""
    kind: str = ""
    extensions: tuple[str, ...] = ()

    def sniff(self, filename: str, data: bytes) -> bool:
        return True

    def extract(self, filename: str, data: bytes, hints: dict) -> ImportResult:
        raise NotImplementedError


class ImageImporter(AssetImporter):
    """Flat 2D images. kind='image'; dims + alpha via Pillow."""
    kind = "image"
    extensions = (".png", ".jpg", ".jpeg", ".webp")

    def sniff(self, filename: str, data: bytes) -> bool:
        return _image_dims(data) is not None

    def extract(self, filename: str, data: bytes, hints: dict) -> ImportResult:
        dims = _image_dims(data)
        w, h, transparent = dims if dims else (None, None, False)
        return ImportResult(kind=self.kind, ext=_ext(filename), width=w, height=h, transparent=transparent,
                            label=hints.get("label"), licence=hints.get("licence"),
                            attribution=hints.get("attribution"), creator=hints.get("creator"))


class StereoImageImporter(ImageImporter):
    """A stereo pair packed into one image (side-by-side or top-bottom). Still an `image` on disk and in
    the vector/caption pipeline; the packing rides `attributes.stereo` so place_image renders per-eye."""

    def extract(self, filename: str, data: bytes, hints: dict) -> ImportResult:
        res = super().extract(filename, data, hints)
        layout = hints.get("stereo") or _stereo_from_name(filename) or "sbs"
        res.attributes = {**res.attributes, "stereo": layout}
        return res


def read_glb_json(data: bytes) -> Optional[dict]:
    """The JSON chunk of a GLB, or None if it isn't one. Pure stdlib — a GLB is a 12-byte header then
    length-prefixed chunks, so the node tree, skins, animations and materials are all reachable without
    a glTF library (docs/backlogs/figures.md)."""
    if len(data) < 20 or data[:4] != b"glTF":
        return None
    try:
        length, _ = struct.unpack_from("<II", data, 12)     # first chunk: length, type
        return json.loads(data[20:20 + length])
    except Exception:  # noqa: BLE001 — a truncated/odd GLB just yields no metadata
        return None


def glb_bounds(doc: dict, blob: bytes = b"") -> Optional[tuple[list[float], list[float], bool]]:
    """`(bbox_min, bbox_max, rigged)` in metres, or None. `rigged` = the file contains a skin.

    Why this exists rather than trusting trimesh: **a skinned mesh's vertices are already in the skin's
    space**, so the node transform must NOT be applied to them. trimesh applies it anyway and reported
    Grace at 3.369 m against a true 1.757 m — and `_normalize` divides by that, so she placed at 53 %
    scale, a child-sized doll. The error is invisible on static props, which is why it survived this long.

    So: skinned primitives use their POSITION accessor min/max verbatim (glTF *requires* those on
    POSITION); unskinned ones get the node's world transform applied to the box corners.

    **But skin space is not always model space.** The vertices reach the world through the joints, so a
    scale on the ARMATURE — or baked into the inverse bind matrices — scales the figure even though the
    mesh node's transform is rightly ignored. Measured: `Steve` carries a `CharacterArmature` scaled
    ×100 and `Animated Woman` a ×100 inverse bind, so both were recorded at a couple of CENTIMETRES.
    The correction needs the BIN chunk for the bind matrices; without `blob` the joint scale alone is
    used, which is right for every file that keeps its bind at unit scale.
    """
    accessors, meshes, nodes = doc.get("accessors"), doc.get("meshes"), doc.get("nodes")
    if not accessors or not meshes or not nodes:
        return None
    rigged = bool(doc.get("skins"))
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3

    def mat_of(node: dict) -> list[float]:
        if "matrix" in node:                              # column-major 4x4
            return [float(x) for x in node["matrix"]]
        t = node.get("translation", [0.0, 0.0, 0.0])
        s = node.get("scale", [1.0, 1.0, 1.0])
        x, y, z, w = node.get("rotation", [0.0, 0.0, 0.0, 1.0])
        # quaternion → 3x3, then scale columns and set translation (column-major, like glTF)
        r = [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w),
             2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w),
             2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y)]
        return [r[0] * s[0], r[1] * s[0], r[2] * s[0], 0.0,
                r[3] * s[1], r[4] * s[1], r[5] * s[1], 0.0,
                r[6] * s[2], r[7] * s[2], r[8] * s[2], 0.0,
                float(t[0]), float(t[1]), float(t[2]), 1.0]

    def mul(a: list[float], b: list[float]) -> list[float]:
        return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4))
                for c in range(4) for r in range(4)]

    def read_floats(acc_index: int, count_per: int, limit: int, stride_hint: int = 1):
        """Yield tuples from a float accessor — enough of a glTF reader to skin a bounding box."""
        acc = accessors[acc_index]
        views = doc.get("bufferViews") or []
        if acc.get("componentType") != 5126 or "bufferView" not in acc or acc["bufferView"] >= len(views):
            return
        bv = views[acc["bufferView"]]
        base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        step = bv.get("byteStride") or count_per * 4
        for i in range(0, min(acc.get("count", 0), limit), stride_hint):
            off = base + i * step
            if off + count_per * 4 > len(blob):
                return
            yield i, struct.unpack_from("<" + "f" * count_per, blob, off)

    def read_ints(acc_index: int, limit: int, stride_hint: int = 1):
        """Yield VEC4 joint indices, whatever width the file stores them at."""
        acc = accessors[acc_index]
        views = doc.get("bufferViews") or []
        fmt = {5121: "B", 5123: "H", 5125: "I", 5126: "f"}.get(acc.get("componentType"))
        if not fmt or "bufferView" not in acc or acc["bufferView"] >= len(views):
            return
        width = {"B": 1, "H": 2, "I": 4, "f": 4}[fmt]
        bv = views[acc["bufferView"]]
        base = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        step = bv.get("byteStride") or width * 4
        for i in range(0, min(acc.get("count", 0), limit), stride_hint):
            off = base + i * step
            if off + width * 4 > len(blob):
                return
            yield i, struct.unpack_from("<" + fmt * 4, blob, off)

    def skinned_corners(prim: dict, skin: dict):
        """Vertex positions as the JOINTS actually place them, or None if the file will not say.

        The accessor's own min/max describe the mesh in BIND space, and for a good many rigs that is not
        where the figure ends up: `Steve` and one `Animated Woman` author every body part as a small
        cluster near the origin and let each joint carry it into place, so their accessor boxes read 1.3
        cm and 3.7 mm against true heights of 2.7 m and 1.8 m. No single scale factor recovers that —
        only skinning does. Sampled rather than exhaustive on dense meshes: a bounding box does not get
        meaningfully better after fifty thousand vertices.
        """
        attrs = prim.get("attributes") or {}
        pi, ji, wi = attrs.get("POSITION"), attrs.get("JOINTS_0"), attrs.get("WEIGHTS_0")
        if pi is None or ji is None or wi is None:
            return None
        count = accessors[pi].get("count", 0)
        stride = max(1, count // 50000)
        joints = skin.get("joints") or []
        mats = []
        ibm_i = skin.get("inverseBindMatrices")
        for k, j in enumerate(joints):
            world = joint_world.get(j)
            if world is None:
                mats.append(None)
                continue
            ibm = None
            if ibm_i is not None and 0 <= ibm_i < len(accessors):
                acc = accessors[ibm_i]
                views = doc.get("bufferViews") or []
                if "bufferView" in acc and acc["bufferView"] < len(views):
                    bv = views[acc["bufferView"]]
                    off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0) + 64 * k
                    if off + 64 <= len(blob):
                        ibm = list(struct.unpack_from("<16f", blob, off))
            mats.append(mul(world, ibm) if ibm else world)
        weights = dict(read_floats(wi, 4, count, stride))
        indices = dict(read_ints(ji, count, stride))
        norm = {5121: 255.0, 5123: 65535.0}.get(accessors[wi].get("componentType"), 1.0)
        out = []
        for i, p in read_floats(pi, 3, count, stride):
            w, jj = weights.get(i), indices.get(i)
            if not w or not jj:
                continue
            x = y = z = 0.0
            total = 0.0
            for k in range(4):
                wk = w[k] / norm
                m = mats[int(jj[k])] if 0 <= int(jj[k]) < len(mats) else None
                if wk <= 0 or m is None:
                    continue
                total += wk
                x += wk * (m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12])
                y += wk * (m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13])
                z += wk * (m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14])
            if total > 0:
                out.append([x, y, z])
        return out or None

    def skin_scale(skin_index: int) -> float:
        """How much the skin's joints scale their skin-space vertices on the way to the world."""
        skins = doc.get("skins") or []
        if not (0 <= skin_index < len(skins)):
            return 1.0
        skin = skins[skin_index]
        joints = skin.get("joints") or []
        scale = 1.0
        if joints:
            m = joint_world.get(joints[0])
            if m:
                scale *= math.sqrt(m[0] ** 2 + m[1] ** 2 + m[2] ** 2)
        acc_i = skin.get("inverseBindMatrices")
        if blob and acc_i is not None and 0 <= acc_i < len(accessors):
            acc = accessors[acc_i]
            views = doc.get("bufferViews") or []
            bv = views[acc["bufferView"]] if "bufferView" in acc and acc["bufferView"] < len(views) else None
            if bv is not None:
                off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
                if off + 64 <= len(blob):
                    m = struct.unpack_from("<16f", blob, off)
                    scale *= math.sqrt(m[0] ** 2 + m[1] ** 2 + m[2] ** 2)
        return scale or 1.0

    def walk(idx: int, parent: list[float], meshes_too: bool = True) -> None:
        node = nodes[idx]
        world = mul(parent, mat_of(node))
        joint_world[idx] = world
        mi = node.get("mesh") if meshes_too else None
        if mi is not None and 0 <= mi < len(meshes):
            skinned = "skin" in node
            for prim in meshes[mi].get("primitives", []):
                ai = prim.get("attributes", {}).get("POSITION")
                if ai is None or ai >= len(accessors):
                    continue
                acc = accessors[ai]
                amin, amax = acc.get("min"), acc.get("max")
                if not amin or not amax or len(amin) < 3:
                    continue
                if skinned:
                    skins = doc.get("skins") or []
                    si = node["skin"]
                    skinned_pts = (skinned_corners(prim, skins[si]) if blob and 0 <= si < len(skins)
                                   else None)
                    if skinned_pts is not None:
                        corners = skinned_pts               # where the joints actually put the vertices
                    else:                                   # no weights to read: skin space, scaled
                        k = skin_scale(si)
                        corners = [[v * k for v in amin], [v * k for v in amax]]
                else:
                    corners = [[amin[0] if i & 1 else amax[0],
                                amin[1] if i & 2 else amax[1],
                                amin[2] if i & 4 else amax[2]] for i in range(8)]
                    corners = [[world[0] * c[0] + world[4] * c[1] + world[8] * c[2] + world[12],
                                world[1] * c[0] + world[5] * c[1] + world[9] * c[2] + world[13],
                                world[2] * c[0] + world[6] * c[1] + world[10] * c[2] + world[14]]
                               for c in corners]
                for c in corners:
                    for k in range(3):
                        lo[k] = min(lo[k], float(c[k])); hi[k] = max(hi[k], float(c[k]))
        for child in node.get("children", []):
            walk(child, world, meshes_too)

    joint_world: dict[int, list[float]] = {}
    ident = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    scenes = doc.get("scenes") or []
    roots = scenes[doc.get("scene", 0)].get("nodes", []) if scenes else range(len(nodes))
    # Two passes: the joints have to be placed before a skinned mesh can be scaled by them, and a mesh
    # node is not necessarily visited after the armature it is bound to.
    for r in roots:
        walk(r, ident, meshes_too=False)
    for r in roots:
        walk(r, ident)
    if any(x == float("inf") for x in lo):
        return None
    return lo, hi, rigged


#: VRM's humanoid bone vocabulary, keyed by the standard names the extension defines. Recording this
#: map is the whole point of the figures pipeline: every later capability — posing, retargeting an
#: animation, "raise her left arm" — needs a per-model translation from a semantic name to that model's
#: own node. VRM is the one source that states it outright (docs/backlogs/figures.md, discovery layer 1).
def vrm_humanoid(doc: dict) -> Optional[dict]:
    """`{semanticBone: nodeName}` from a VRM's humanoid map, or None if this isn't a VRM.

    Handles both VRM 1.0 (`VRMC_vrm`, `humanBones` as `{name: {node: i}}`) and VRM 0.x (`VRM`, a
    `humanBones` LIST of `{bone, node}`). Node *names* are stored rather than indices so the map stays
    meaningful if the file is ever re-exported with a different node order.
    """
    ext = (doc.get("extensions") or {})
    nodes = doc.get("nodes") or []

    def name_of(i):
        return nodes[i].get("name") if isinstance(i, int) and 0 <= i < len(nodes) else None

    raw = ((ext.get("VRMC_vrm") or {}).get("humanoid") or {}).get("humanBones")
    if isinstance(raw, dict):                                       # VRM 1.0
        out = {k: name_of(v.get("node")) for k, v in raw.items() if isinstance(v, dict)}
    elif isinstance((ext.get("VRM") or {}).get("humanoid"), dict):   # VRM 0.x
        legacy = (ext["VRM"]["humanoid"] or {}).get("humanBones") or []
        out = {b.get("bone"): name_of(b.get("node")) for b in legacy if isinstance(b, dict)}
    else:
        return None
    out = {k: v for k, v in out.items() if k and v}
    return out or None


class ModelImporter(AssetImporter):
    """glTF-binary 3D models — `.glb` from Blender, or `.vrm`, which IS a GLB with extra extension
    blocks. Validated by magic bytes, and a `.vrm` is stored as `.glb` so the client's `gltf-model`
    loads it with no special case.

    Bounds come from the GLB's own JSON chunk (`glb_bounds`), NOT trimesh, because trimesh mis-sizes
    every rigged model — see that function. trimesh is still used for the triangle count, which it gets
    right. Rigged models additionally record what a figure needs: joint counts, clips, morph targets,
    and — when the file states it — the humanoid bone map."""
    kind = "model"
    extensions = (".glb", ".vrm")

    def sniff(self, filename: str, data: bytes) -> bool:
        return data[:4] == b"glTF"                      # binary-glTF magic (a .vrm is one too)

    def extract(self, filename: str, data: bytes, hints: dict) -> ImportResult:
        attributes: dict = {}
        doc = read_glb_json(data)
        if doc:
            from .figures import split_glb                        # BIN chunk: bind matrices, weights
            blob = split_glb(data)[1] if doc.get("skins") else b""
            bounds = glb_bounds(doc, blob)
            if bounds:
                lo, hi, rigged = bounds
                attributes.update({"bbox_min": lo, "bbox_max": hi})
                if rigged:
                    # A rigged model is a FIGURE: authored at life size and placed without normalization
                    # (docs/backlogs/figures.md). Height is the Y extent — glTF is Y-up.
                    attributes.update({
                        "rigged": True,
                        "height_m": round(hi[1] - lo[1], 4),
                        "joints": [len(s.get("joints", [])) for s in doc.get("skins", [])],
                        "clips": [a.get("name") for a in doc.get("animations", [])],
                        "morph_targets": sum(len(p.get("targets", []))
                                             for m in doc.get("meshes", [])
                                             for p in m.get("primitives", [])),
                    })
                    humanoid = vrm_humanoid(doc)
                    if not humanoid:
                        # No stated map, so work down the discovery layers: a known naming convention
                        # first (free and exact, and it survives a bind pose that defeats geometry),
                        # then skeleton SHAPE. Whatever comes back is pruned and must validate — a map
                        # that is plausible but wrong is worse than none, because posing and retargeting
                        # both inherit it silently.
                        try:
                            from .figures import best_humanoid
                            guess, source = best_humanoid(doc, blob)
                            if guess:
                                attributes["humanoid"] = guess
                                attributes["humanoid_source"] = source
                        except Exception as exc:  # noqa: BLE001 — never fail an import over this
                            # Reported, not swallowed: a silent guard here hid a wrong argument and made
                            # inference look like it simply found nothing on every non-VRM model.
                            print(f"[conjure] humanoid discovery failed for {filename}: {exc}")
                    if humanoid:
                        # Discovery layer 1, for free. `humanoid_source` is recorded because a stated
                        # map and an inferred one warrant different trust, and it is what lets a later
                        # bug be attributed rather than guessed at.
                        attributes["humanoid"] = humanoid
                        attributes["humanoid_source"] = "vrm"
                    if attributes.get("humanoid"):
                        # The anatomical frame: which way to rotate each bone so that "bend 45" means
                        # the same motion on every rig. Derived from the bind pose, so it is a property
                        # of the FILE and belongs here beside the map rather than being re-measured by
                        # every consumer (docs/backlogs/figures.md, the axis problem).
                        try:
                            from .figures import anatomical_axes
                            axes = anatomical_axes(doc, attributes["humanoid"])
                            if axes:
                                attributes["humanoid_axes"] = axes
                        except Exception as exc:  # noqa: BLE001 — a map without axes still places
                            print(f"[conjure] anatomical axes failed for {filename}: {exc}")
                    used = doc.get("extensionsUsed") or []
                    if "VRMC_springBone" in used or "VRM" in (doc.get("extensions") or {}):
                        attributes["spring_bones"] = "VRMC_springBone" in used
        # Stamped on EVERY model, not only the ones that turn out to be figures. The stamp records which
        # build looked at this file, and "we looked and it is a prop" is exactly as much worth recording
        # as a bone map — three rigged characters sat in the catalog as props because an earlier ingest
        # path never looked at all (docs/backlogs/figures.md).
        from .figures import FRAME_REV
        attributes["frame_rev"] = FRAME_REV
        try:
            import trimesh
            scene = trimesh.load(io.BytesIO(data), file_type="glb", force="scene")
            attributes["tris"] = int(sum(len(g.faces) for g in scene.geometry.values()))
            if "bbox_min" not in attributes:             # unrigged fallback if the JSON walk found nothing
                lo, hi = scene.bounds
                attributes["bbox_min"] = [float(x) for x in lo]
                attributes["bbox_max"] = [float(x) for x in hi]
        except Exception:  # noqa: BLE001 — tri count is a nicety; still catalog the model without it
            pass
        return ImportResult(kind=self.kind, ext=".glb", label=hints.get("label") or Path(filename).stem,
                            attributes=attributes, licence=hints.get("licence"),
                            attribution=hints.get("attribution"), creator=hints.get("creator"))


# The registry. Add a handler here and it's importable everywhere — no other change.
_IMAGE = ImageImporter()
_STEREO = StereoImageImporter()
_MODEL = ModelImporter()
_HANDLERS: tuple[AssetImporter, ...] = (_IMAGE, _MODEL)
_BY_KIND = {"image": _IMAGE, "stereo": _STEREO, "model": _MODEL}
_BY_EXT = {ext: h for h in _HANDLERS for ext in h.extensions}


def importable_extensions() -> set[str]:
    """Every extension a handler claims (drives the CLI's directory filter)."""
    return set(_BY_EXT) | {".jpeg"}


def plan_import(filename: str, data: bytes, hints: dict) -> Optional[ImportResult]:
    """Pick a handler and extract, or None if nothing recognizes the file. Selection order: an explicit
    `hints['kind']`, then a stereo filename/hint (an image is still an image on disk), then extension."""
    forced = hints.get("kind")
    if forced == "stereo" or hints.get("stereo") or _stereo_from_name(filename):
        handler: Optional[AssetImporter] = _STEREO
    elif forced in _BY_KIND:
        handler = _BY_KIND[forced]
    else:
        handler = _BY_EXT.get(_ext(filename))
    if handler is None or not handler.sniff(filename, data):
        return None
    return handler.extract(filename, data, hints)


# ---------------------------------------------------------------------------- CLI (thin HTTP client)


def _collect(paths: list[str], recursive: bool) -> list[Path]:
    """Expand the given paths to a flat list of importable files (extension-filtered so a directory
    sweep doesn't upload junk; the server still sniffs each)."""
    exts = importable_extensions()
    out: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            out.extend(f for f in sorted(it) if f.is_file() and f.suffix.lower() in exts)
        elif p.is_file():
            out.append(p)
        else:
            print(f"skip (not found): {raw}", file=sys.stderr)
    return out


def main() -> int:
    import httpx

    from .config import DEFAULT_USER, get_settings, scope_for

    ap = argparse.ArgumentParser(prog="conjure-import",
                                 description="Import asset files (images, stereo pairs, .glb models) into the library.")
    ap.add_argument("paths", nargs="+", help="files and/or directories to import")
    ap.add_argument("-r", "--recursive", action="store_true", help="walk directories recursively")
    ap.add_argument("--kind", choices=["auto", "image", "stereo", "model"], default="auto",
                    help="force a handler (default: auto — sniff by extension/name)")
    ap.add_argument("--stereo", choices=["sbs", "tb"], help="treat image inputs as this stereo layout")
    ap.add_argument("--license", dest="licence", help="license string to record on every asset")
    ap.add_argument("--attribution", help="attribution to record")
    ap.add_argument("--creator", help="creator to record")
    ap.add_argument("--user", default=DEFAULT_USER, help="owning user (asset scope)")
    ap.add_argument("--agent", default="builder", help="owning agent (asset scope)")
    ap.add_argument("--dry-run", action="store_true", help="report what would import; write nothing")
    ap.add_argument("-v", "--verbose", action="store_true", help="print full JSON results")
    a = ap.parse_args()

    s = get_settings()
    try:
        httpx.get(f"{s.world_url}/world", timeout=3.0)
    except Exception:
        print(f"World server not reachable at {s.world_url}. Start it: python -m conjure", file=sys.stderr)
        return 1

    files = _collect(a.paths, a.recursive)
    if not files:
        print("nothing importable found", file=sys.stderr)
        return 1

    hints = {k: v for k, v in {"kind": None if a.kind == "auto" else a.kind, "stereo": a.stereo,
                               "licence": a.licence, "attribution": a.attribution,
                               "creator": a.creator}.items() if v}
    items = [{"filename": p.name, "data_b64": base64.b64encode(p.read_bytes()).decode(), "hints": hints}
             for p in files]
    print(f"{'(dry-run) ' if a.dry_run else ''}importing {len(items)} file(s)…", file=sys.stderr)

    headers = {"X-Conjure-User": a.user, "X-Conjure-Scope": scope_for(a.user, a.agent)}
    try:
        r = httpx.post(f"{s.world_url}/library/import", json={"items": items, "dry_run": a.dry_run},
                       headers=headers, timeout=300.0)
        r.raise_for_status()
        out = r.json()
    except httpx.HTTPError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if a.verbose:
        import json
        print(json.dumps(out, indent=2))
    else:
        for res in out.get("results", []):
            if not res.get("ok"):
                print(f"  ✗ {res['filename']}: {res.get('error', 'failed')}")
            elif res.get("dry_run"):
                st = res.get("attributes", {}).get("stereo")
                print(f"  · {res['filename']} → {res['kind']}{' [stereo ' + st + ']' if st else ''}")
            else:
                st = res.get("attributes", {}).get("stereo")
                print(f"  ✓ {res['filename']} → {res['id']}{' [stereo ' + st + ']' if st else ''}")
    print(f"{out.get('imported', 0)} imported, {out.get('failed', 0)} failed"
          + (" (dry-run)" if a.dry_run else ""), file=sys.stderr)
    return 0 if out.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
