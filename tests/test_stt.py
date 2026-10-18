"""The STT profile registry (conjure/stt.py) — which engine the voice client listens through. Pure:
no pipecat, no network. The ears and the mouth are independent settings, and that is tested here."""

import dataclasses

import pytest

from conjure.config import get_settings
from conjure.stt import PROFILES, WHISPER_MODEL, profile_for
from conjure.tts import profile_for as speech_for


def _settings(**over):
    return dataclasses.replace(get_settings(), **over)


def test_the_two_recognizers_and_their_shapes():
    assert set(PROFILES) == {"whisper", "grok"}
    w, g = PROFILES["whisper"], PROFILES["grok"]
    assert w.corpus_tap and not w.streaming and w.needs_key is None and WHISPER_MODEL in w.description
    assert g.streaming and not g.corpus_tap and g.needs_key == "xai_api_key"
    assert w.sample_rate == g.sample_rate == 16000


def test_profile_for_honours_the_stt_setting():
    assert profile_for(_settings(stt="whisper")).name == "whisper"
    assert profile_for(_settings(stt="Grok", xai_api_key="k")).name == "grok"


def test_an_unknown_recognizer_is_refused_with_the_choices():
    with pytest.raises(ValueError, match="grok, whisper"):
        profile_for(_settings(stt="deepgram"))


def test_grok_without_its_key_is_refused_naming_the_variable():
    with pytest.raises(ValueError, match="XAI_API_KEY"):
        profile_for(_settings(stt="grok", xai_api_key=None))


def test_the_corpus_tap_is_refused_for_a_streaming_recognizer():
    """A governed recorder must never be running when you assumed it wasn't — nor NOT running when you
    assumed it was. A streaming engine decodes no WAV, so `--stt-corpus` is refused at the door."""
    with pytest.raises(ValueError, match="--stt-corpus.*whisper"):
        profile_for(_settings(stt="grok", xai_api_key="k"), corpus=True)
    assert profile_for(_settings(stt="whisper"), corpus=True).name == "whisper"


def test_ears_and_mouth_are_independent_settings():
    """Every pairing resolves: Whisper with Grok speech, Grok hearing with Kokoro, either for both."""
    for stt, tts in [("whisper", "kokoro"), ("whisper", "grok"), ("grok", "kokoro"), ("grok", "grok")]:
        s = _settings(stt=stt, tts=tts, xai_api_key="k")
        assert (profile_for(s).name, speech_for(s).name) == (stt, tts)
    s = _settings(stt="grok", tts="kokoro", xai_api_key=None)        # the key is checked per engine
    assert speech_for(s).name == "kokoro"
    with pytest.raises(ValueError, match="CONJURE_STT=grok"):
        profile_for(s)
