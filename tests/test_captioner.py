"""Captioner backends + selection (docs/asset-library-plan.md §12)."""

from __future__ import annotations

from conjure.captioner import (LOCAL_CAPTION_MODEL, Captioner, FakeCaptioner,
                               LocalCaptioner, build_captioner, clean_caption)


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


# ---------------------------------------------------------------- the local captioner

def test_the_local_captioner_needs_no_key_and_ignores_a_hosted_default_model():
    """`caption_model` ships holding a Gemini id. Switching only `caption_provider` must not ask a
    local runtime to load "gemini-2.5-flash" — the stale default is stepped over, not obeyed."""
    c = build_captioner(_S("local"))
    assert isinstance(c, LocalCaptioner) and c.name == LOCAL_CAPTION_MODEL


def test_an_explicit_local_model_is_honored():
    s = _S("local")
    s.caption_model = "some-org/some-vlm"
    assert build_captioner(s).name == "some-org/some-vlm"


def test_a_caption_is_trimmed_to_the_phrase_that_gets_indexed():
    """A small local model does not reliably obey "no preamble". Every one of these came back as a
    label that would be indexed verbatim, and the quotes and full stops are what search sees."""
    assert clean_caption('  "a misty pine forest at dawn."  ') == "a misty pine forest at dawn"
    assert clean_caption("Here is a short phrase: a red bicycle") == "a red bicycle"
    assert clean_caption("a cat\nand more rambling") == "a cat"
    assert clean_caption("") == ""


def test_a_colon_inside_the_description_survives():
    """The preamble strip is guarded by length, because a caption can legitimately contain a colon."""
    long_left = "a diner sign reading open all night: neon lettering at dusk"
    assert clean_caption(long_left) == long_left


def test_local_captioning_is_off_rather_than_a_crash_when_the_deps_are_absent(monkeypatch, capsys):
    """A fresh install without the `vision` group must degrade like every other backend here — None
    and a message naming the group — not an ImportError raised from inside `AutoProcessor` on the
    first image someone imports."""
    import conjure.captioner as c

    monkeypatch.setattr(c, "_have", lambda *mods: False)
    assert build_captioner(_S("local")) is None
    assert "vision" in capsys.readouterr().out


def test_the_deps_it_checks_for_include_torchvision(monkeypatch):
    """torchvision is the easy one to forget: `embed` does not need it, but a VLM's AutoProcessor
    pulls in a video processor that imports it even for stills."""
    import conjure.captioner as c

    asked = []
    monkeypatch.setattr(c, "_have", lambda *mods: asked.extend(mods) or True)
    build_captioner(_S("local"))
    assert {"torch", "transformers", "torchvision"} <= set(asked)
