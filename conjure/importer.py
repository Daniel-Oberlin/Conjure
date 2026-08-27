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


class ModelImporter(AssetImporter):
    """glTF-binary (.glb) 3D models — e.g. exported from Blender. Validated by magic bytes; tris/bbox
    read best-effort via trimesh (mirrors conjure/assets.py) into `attributes` for later placement."""
    kind = "model"
    extensions = (".glb",)

    def sniff(self, filename: str, data: bytes) -> bool:
        return data[:4] == b"glTF"                      # binary-glTF magic

    def extract(self, filename: str, data: bytes, hints: dict) -> ImportResult:
        attributes: dict = {}
        try:
            import trimesh
            scene = trimesh.load(io.BytesIO(data), file_type="glb", force="scene")
            lo, hi = scene.bounds
            attributes = {"bbox_min": [float(x) for x in lo], "bbox_max": [float(x) for x in hi],
                          "tris": int(sum(len(g.faces) for g in scene.geometry.values()))}
        except Exception:  # noqa: BLE001 — bbox is a nicety; still catalog the model without it
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
