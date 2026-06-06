"""Shared test fixtures. Tier 1: fast, free, deterministic — no network, no keys, no LLM."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from conjure.assets import AssetRecord
from conjure.imagegen import ImageResult


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, "PNG")
    return buf.getvalue()


TINY_PNG = _png()
# Passes the GLB magic check; trimesh won't parse it (bbox → None), which is fine for these tests.
FAKE_GLB = b"glTF" + bytes(40)


class FakeImageGenerator:
    """Stand-in for an image generator — returns a fixed tiny PNG, no API call."""

    name = "fake"
    model = "fake-model"

    async def generate(self, prompt, *, aspect_ratio=None, image_size=None, model=None) -> ImageResult:
        return ImageResult(data=TINY_PNG, mime_type="image/png", provider="fake", model=model or self.model)

    async def edit(self, prompt, image, *, aspect_ratio=None, image_size=None, model=None) -> ImageResult:
        # Different bytes so callers can detect the change.
        return ImageResult(data=TINY_PNG + b"edited", mime_type="image/png", provider="fake", model=model or self.model)


class FakeAssetResolver:
    """Stand-in for AssetResolver: returns a fixed record, or raises, with no network/disk."""

    def __init__(self, record: AssetRecord | None = None, error: Exception | None = None):
        self._record = record
        self._error = error

    async def resolve(self, query: str):
        if self._error is not None:
            raise self._error
        return self._record


# A model record with a known bounding box so normalization math is checkable.
# bbox 2 (x) × 4 (y) × 2 (z), min_y = -2.
ASSET_RECORD = AssetRecord(
    hash="abc123def456",
    title="Oak Tree",
    attribution="\"Oak Tree\" by Tester",
    licence="CC-BY 3.0",
    creator="Tester",
    tris=1000,
    source_url="https://example/tree.glb",
    bbox_min=[-1.0, -2.0, -1.0],
    bbox_max=[1.0, 2.0, 1.0],
)


@pytest.fixture
def srv(tmp_path, monkeypatch):
    """The world-server module with a clean world, temp asset cache, and a fake image generator.
    Tests set `srv.resolver` per scenario."""
    import conjure.server as server
    from conjure.world import WorldStore

    monkeypatch.setattr(server, "ASSET_CACHE", tmp_path)
    monkeypatch.setattr(
        server, "store",
        WorldStore({"id": "test", "name": "Test", "rev": 0, "environment": {"sky": {"color": "#000"}}, "entities": []}),
    )
    monkeypatch.setattr(server, "image_gen", FakeImageGenerator())
    monkeypatch.setattr(server, "resolver", None)
    server.clients.clear()
    return server


@pytest.fixture
def client(srv):
    from fastapi.testclient import TestClient

    return TestClient(srv.app)
