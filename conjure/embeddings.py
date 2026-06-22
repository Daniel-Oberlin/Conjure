"""Embeddings — a backend-swappable `Embedder` over a shared text-image space.

docs/asset-library-plan.md §4: the *model* is SigLIP (text + image land in one space, so a text query
can match image content). The *backend that runs it* is chosen by deployment behind this interface:

- **SigLipEmbedder** — local torch + transformers. The dev default; zero export friction, the matched
  processor ships with the model. Heavy (`torch`), so it's an **optional dependency group**
  (`conjure[embed]`), imported lazily — nothing here imports torch until an embedding is requested.
- **FakeEmbedder** — deterministic, dependency-free. Used by tests (keeps `torch` off the fast path)
  and as a stand-in.
- *(future)* ONNX / hosted backends implement the same interface (the lean/Pi and no-local-ML paths).

`build_embedder(settings)` returns the configured backend, or **None** when its deps are absent — the
server then simply skips vector write-through, so the catalog (FTS/exact) works with no ML installed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import struct
from typing import Optional, Protocol, runtime_checkable

_DEFAULT_MODEL = "google/siglip2-so400m-patch14-384"   # the quality SigLIP 2 checkpoint (decision §4)


@runtime_checkable
class Embedder(Protocol):
    name: str                                   # model id — recorded as `embed_model` for comparability
    def embed_text(self, text: str) -> list[float]: ...
    def embed_image(self, data: bytes) -> list[float]: ...


def _unit(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


class FakeEmbedder:
    """Deterministic, dependency-free embedder for tests/plumbing. Same input → same unit vector.
    Text and image share the space only nominally (it's a hash, not semantics) — enough to exercise
    storage, KNN, and write-through without `torch`."""

    def __init__(self, dim: int = 16):
        self.name = "fake"
        self.dim = dim

    def _vec(self, key: str) -> list[float]:
        out: list[float] = []
        i = 0
        while len(out) < self.dim:
            h = hashlib.sha256(f"{i}:{key}".encode()).digest()
            for j in range(0, len(h), 4):
                if len(out) >= self.dim:
                    break
                out.append(struct.unpack("<i", h[j:j + 4])[0] / 2**31)
            i += 1
        return _unit(out)

    def embed_text(self, text: str) -> list[float]:
        return self._vec("t:" + (text or ""))

    def embed_image(self, data: bytes) -> list[float]:
        return self._vec("i:" + hashlib.sha256(data or b"").hexdigest())


class SigLipEmbedder:
    """Local SigLIP via torch + transformers. Lazy: imports torch and loads the model only on first
    use, so importing this module (and booting the server) never pulls torch."""

    def __init__(self, model_id: str = _DEFAULT_MODEL):
        self.name = model_id
        self.dim: Optional[int] = None
        self._model = None
        self._proc = None
        self._torch = None

    def _ensure(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModel, AutoProcessor

        self._torch = torch
        self._model = AutoModel.from_pretrained(self.name)
        self._model.eval()
        self._proc = AutoProcessor.from_pretrained(self.name)

    def _finish(self, feat) -> list[float]:
        out = (feat[0] / feat[0].norm()).tolist()   # L2-normalize → cosine order == L2 order in vec0
        self.dim = len(out)
        return out

    def embed_text(self, text: str) -> list[float]:
        self._ensure()
        with self._torch.no_grad():                  # SigLIP wants fixed-length padding for text
            inp = self._proc(text=[text or ""], return_tensors="pt", padding="max_length")
            return self._finish(self._model.get_text_features(**inp))

    def embed_image(self, data: bytes) -> list[float]:
        self._ensure()
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(data)).convert("RGB")
        with self._torch.no_grad():
            inp = self._proc(images=[img], return_tensors="pt")
            return self._finish(self._model.get_image_features(**inp))


def _have(*mods: str) -> bool:
    return all(importlib.util.find_spec(m) is not None for m in mods)


def build_embedder(settings) -> Optional[Embedder]:
    """Pick the embedder backend from config, or None if its deps are missing (server degrades to
    FTS/exact-only — no ML required to run)."""
    backend = (getattr(settings, "embed_backend", "auto") or "auto").strip().lower()
    if backend in ("none", "off", ""):
        return None
    if backend == "fake":
        return FakeEmbedder()
    model = getattr(settings, "embed_model", _DEFAULT_MODEL) or _DEFAULT_MODEL
    if _have("torch", "transformers"):
        return SigLipEmbedder(model)
    if backend == "siglip":   # explicitly asked for, but deps absent — say so, don't crash
        print("[conjure] embed_backend=siglip but torch/transformers not installed — embeddings off "
              "(install the optional 'embed' dependency group)")
    return None
