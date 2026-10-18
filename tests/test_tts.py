"""The TTS profile registry (conjure/tts.py) — which engine the voice client speaks through, and what
each engine's registration tells the agent server. Pure: no pipecat, no network."""

import dataclasses

import pytest

from conjure.config import get_settings
from conjure.tts import GROK_INLINE, GROK_TAGS, GROK_WRAPPING, PROFILES, profile_for


def _settings(**over):
    return dataclasses.replace(get_settings(), **over)


def test_the_two_engines_are_registered_and_kokoro_is_tagless():
    assert set(PROFILES) == {"kokoro", "grok"}
    k = PROFILES["kokoro"]
    assert k.tags.names == () and k.prompt == "" and k.needs_key is None
    assert k.registration() == {"engine": "kokoro", "tags": [], "prompt": ""}


def test_grok_registers_every_tag_it_prompts_for():
    """The prompt is rendered FROM the tag tables, so what the model is told and what the server strips
    are one vocabulary — this is the test that keeps them from drifting apart."""
    g = PROFILES["grok"]
    reg = g.registration()
    assert reg["engine"] == "grok" and reg["prompt"] == g.prompt
    assert set(reg["tags"]) == set(GROK_TAGS.names) and len(reg["tags"]) == len(set(reg["tags"]))
    for t in GROK_TAGS.inline:
        assert f"[{t}]" in g.prompt
    for t in GROK_TAGS.wrapping:
        assert f"<{t}>…</{t}>" in g.prompt
    assert GROK_TAGS.inline == tuple(t for _, ts in GROK_INLINE for t in ts)
    assert GROK_TAGS.wrapping == tuple(t for _, ts in GROK_WRAPPING for t in ts)


def test_grok_prompt_asks_for_restraint_and_a_readable_sentence():
    """The two rules that matter in a shared conversation: sparingly, and still a complete sentence once
    the tags are gone — because text clients see exactly that."""
    p = PROFILES["grok"].prompt
    assert "Sparingly" in p and "every tag removed" in p


def test_profile_for_honours_the_tts_setting():
    assert profile_for(_settings(tts="kokoro")).name == "kokoro"
    assert profile_for(_settings(tts="Grok", xai_api_key="k")).name == "grok"      # case-insensitive


def test_an_unknown_engine_is_refused_with_the_choices():
    with pytest.raises(ValueError, match="grok, kokoro"):
        profile_for(_settings(tts="elevenlabs"))


def test_grok_without_its_key_is_refused_at_the_door_naming_the_variable():
    with pytest.raises(ValueError, match="XAI_API_KEY"):
        profile_for(_settings(tts="grok", xai_api_key=None))


def test_the_grok_voice_is_a_setting():
    s = _settings(tts="grok", xai_api_key="k", xai_tts_voice="leo")
    assert s.xai_tts_voice == "leo"
    assert PROFILES["grok"].sample_rate == 24000 == PROFILES["kokoro"].sample_rate
