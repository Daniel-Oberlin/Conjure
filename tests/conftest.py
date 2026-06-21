"""Shared test fixtures. Tier 1: fast, free, deterministic — no network, no keys, no LLM."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from conjure.assets import AssetRecord
from conjure.library import AssetLibrary
from conjure.llm import ImageCapabilities, ImageResult


def _png(color="red", size=(4, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def _png_rgba(color=(255, 0, 0, 0), size=(4, 4)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


TINY_PNG = _png()
WIDE_PNG = _png("blue", (8, 4))  # 2:1 — lets place_image's aspect handling be checked
ALPHA_PNG = _png_rgba()          # has a real alpha channel (fully transparent)
# Passes the GLB magic check; trimesh won't parse it (bbox → None), which is fine for these tests.
FAKE_GLB = b"glTF" + bytes(40)


class FakeImageGenerator:
    """Stand-in for a Gemini-like generator (all ops, free aspect) — returns a tiny PNG, no API."""

    name = "Gemini"
    model = "fake-gemini"
    capabilities = ImageCapabilities(
        operations=frozenset({"generate", "edit", "outpaint", "skybox"}),
        edit_mode="prompt", max_resolution=4096, aspect="free", fixed_sizes=(), transparency=False,
    )

    async def generate(self, prompt, *, aspect_ratio=None, image_size=None, model=None,
                       transparent=False) -> ImageResult:
        return ImageResult(data=TINY_PNG, mime_type="image/png", provider=self.name, model=model or self.model)

    async def edit(self, prompt, image, *, aspect_ratio=None, image_size=None, model=None,
                   transparent=False, mask=None) -> ImageResult:
        # Different bytes (and shape) so callers can detect the change; WIDE so outpaint resizes.
        return ImageResult(data=WIDE_PNG, mime_type="image/png", provider=self.name, model=model or self.model)


class FakeOpenAIImageGenerator:
    """Stand-in for OpenAI: generate + edit only, transparency, fixed sizes — for mediation tests."""

    name = "Chat"
    model = "fake-gpt-image"
    capabilities = ImageCapabilities(
        operations=frozenset({"generate", "edit"}),
        edit_mode="mask", max_resolution=1536, aspect="fixed",
        fixed_sizes=("1024x1024", "1536x1024", "1024x1536"), transparency=True,
    )

    async def generate(self, prompt, *, aspect_ratio=None, image_size=None, model=None,
                       transparent=False) -> ImageResult:
        data = ALPHA_PNG if transparent else TINY_PNG  # mirror real gpt-image-1 transparency
        return ImageResult(data=data, mime_type="image/png", provider=self.name, model=self.model)

    async def edit(self, prompt, image, *, aspect_ratio=None, image_size=None, model=None,
                   transparent=False, mask=None) -> ImageResult:
        data = ALPHA_PNG if transparent else TINY_PNG
        return ImageResult(data=data, mime_type="image/png", provider=self.name, model=self.model)


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
    monkeypatch.setattr(server, "image_generators", {"Gemini": FakeImageGenerator()})
    monkeypatch.setattr(server, "IMAGES", {})  # clean image store per test
    monkeypatch.setattr(server, "resolver", None)
    monkeypatch.setattr(server, "library", AssetLibrary(tmp_path / "library.db"))  # isolated catalog
    # (friendly ids are derived from the surface id now — no counter to reset)
    server.clients.clear()
    return server


@pytest.fixture
def client(srv):
    from fastapi.testclient import TestClient

    return TestClient(srv.app)
