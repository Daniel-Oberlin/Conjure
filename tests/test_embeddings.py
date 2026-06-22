"""Embedder backends + selection (docs/asset-library-plan.md §4, Phase 1)."""

from __future__ import annotations

import math

from conjure.embeddings import Embedder, FakeEmbedder, build_embedder


def test_fake_embedder_is_deterministic_and_unit_norm():
    e = FakeEmbedder(dim=16)
    a, b = e.embed_text("a red dragon"), e.embed_text("a red dragon")
    assert a == b and len(a) == 16                       # deterministic, right dim
    assert math.isclose(math.sqrt(sum(x * x for x in a)), 1.0, abs_tol=1e-6)  # normalized
    assert e.embed_text("a red dragon") != e.embed_text("a blue dragon")     # content-sensitive
    assert isinstance(e, Embedder)                       # satisfies the protocol


def test_text_and_image_paths_both_produce_vectors():
    e = FakeEmbedder(dim=8)
    assert len(e.embed_text("x")) == 8 and len(e.embed_image(b"\x89PNG...")) == 8


class _S:  # minimal settings stand-in
    def __init__(self, backend):
        self.embed_backend = backend
        self.embed_model = "google/siglip2-so400m-patch14-384"


def test_build_embedder_honors_backend_and_degrades_without_torch():
    assert build_embedder(_S("none")) is None
    assert isinstance(build_embedder(_S("fake")), FakeEmbedder)
    # "auto" with torch absent (this env) → None, so the server runs ML-free on FTS/exact.
    import importlib.util
    if importlib.util.find_spec("torch") is None:
        assert build_embedder(_S("auto")) is None
