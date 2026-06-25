"""Captioner backends + selection (docs/asset-library-plan.md §12)."""

from __future__ import annotations

from conjure.captioner import Captioner, FakeCaptioner, build_captioner


async def test_fake_captioner_is_deterministic_and_marks_skyboxes():
    c = FakeCaptioner()
    a = await c.caption(b"\x89PNG-bytes")
    assert a == await c.caption(b"\x89PNG-bytes")          # deterministic
    assert (await c.caption(b"x", skybox=True)).startswith("skybox")  # skybox vs image flavor
    assert (await c.caption(b"x", skybox=False)).startswith("image")
    assert isinstance(c, Captioner)


class _S:
    def __init__(self, provider, key=None):
        self.caption_provider = provider
        self.caption_model = "gemini-2.5-flash"
        self.google_api_key = key


def test_build_captioner_honors_provider_and_needs_a_key():
    assert build_captioner(_S("none")) is None
    assert isinstance(build_captioner(_S("fake")), FakeCaptioner)
    assert build_captioner(_S("gemini", key=None)) is None        # gemini without a key → off
    g = build_captioner(_S("gemini", key="k"))
    assert g is not None and g.name == "gemini-2.5-flash"
