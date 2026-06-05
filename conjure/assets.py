"""Asset pipeline — resolve a text query to a cached, locally-served glTF model.

Phase 3 (decision #4): Poly Pizza — free, low-poly **GLB**, CC-licensed, keyword search. A model
is downloaded once, cached content-addressed, and served by the world server at
``/assets/<hash>.glb`` so the headset loads it over the same connection. License + attribution are
captured per asset (architecture.md §10 asset descriptor; spec §11 licensing).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

SEARCH_URL = "https://api.poly.pizza/v1.1/search/{query}"


@dataclass
class AssetRecord:
    hash: str
    title: str
    attribution: str
    licence: str
    creator: str
    tris: int
    source_url: str
    bbox_min: list[float] | None = None  # axis-aligned bounds (model units) for auto-scaling
    bbox_max: list[float] | None = None


def _bounding_box(glb_path: Path) -> tuple[list[float], list[float]] | None:
    """Axis-aligned bounding box of a GLB (node transforms applied), or None if unavailable."""
    try:
        import trimesh

        mesh = trimesh.load(glb_path, force="scene")
        bounds = getattr(mesh, "bounds", None)
        if bounds is None:
            return None
        return [float(v) for v in bounds[0]], [float(v) for v in bounds[1]]
    except Exception:
        return None


class AssetResolver:
    """Searches Poly Pizza, downloads the best low-poly GLB, and caches it content-addressed."""

    def __init__(self, api_key: str, cache_dir: Path):
        self._key = api_key
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self._dir / "index.json"
        self._index: dict[str, str] = (
            json.loads(self._index_path.read_text()) if self._index_path.exists() else {}
        )

    def path_for(self, asset_hash: str) -> Path:
        return self._dir / f"{asset_hash}.glb"

    def _meta_for(self, asset_hash: str) -> Path:
        return self._dir / f"{asset_hash}.json"

    async def resolve(self, query: str) -> AssetRecord | None:
        """Return a cached AssetRecord for the best match, or None if nothing was found."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                SEARCH_URL.format(query=query),
                headers={"x-auth-token": self._key},
                params={"Limit": 8},
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return None
            item = results[0]  # Poly Pizza returns by relevance
            download_url = item["Download"]

            # Cache hit: same source URL already downloaded.
            cached_hash = self._index.get(download_url)
            if cached_hash and self.path_for(cached_hash).exists() and self._meta_for(cached_hash).exists():
                return AssetRecord(**json.loads(self._meta_for(cached_hash).read_text()))

            blob = (await client.get(download_url, timeout=60.0)).content

        asset_hash = hashlib.sha256(blob).hexdigest()[:16]
        self.path_for(asset_hash).write_bytes(blob)
        bbox = _bounding_box(self.path_for(asset_hash))
        record = AssetRecord(
            hash=asset_hash,
            title=item.get("Title") or query,
            attribution=item.get("Attribution") or "",
            licence=item.get("Licence") or "",
            creator=(item.get("Creator") or {}).get("Username", ""),
            tris=int(item.get("Tri Count") or 0),
            source_url=download_url,
            bbox_min=bbox[0] if bbox else None,
            bbox_max=bbox[1] if bbox else None,
        )
        self._meta_for(asset_hash).write_text(json.dumps(asdict(record)))
        self._index[download_url] = asset_hash
        self._index_path.write_text(json.dumps(self._index))
        return record
