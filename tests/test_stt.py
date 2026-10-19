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


# --------------------------------------------------------------------------- Grok: one utterance, once
# The sequences below are what xAI sent for one recorded sentence (2026-10-19): segments arrive `is_final`
# one chunk each, then `speech_final` repeats the whole utterance. Forwarding both doubled every turn.

from conjure.stt import Utterance  # noqa: E402

SENTENCE = "Let me check for you. You just have the one world right now default, and that's the one you're in."


def test_a_close_that_repeats_the_one_segment_adds_nothing():
    u = Utterance()
    assert u.segment(SENTENCE) == SENTENCE
    assert u.close(SENTENCE) == ""


def test_a_close_that_joins_several_segments_adds_nothing_even_re_punctuated():
    u = Utterance()
    for seg in ("Let me check for you. You just have the one world right.", "Right now, default.",
                "And that's the one you're in."):
        u.segment(seg)
    assert u.close("Let me check for you. You just have the one world right. Right now, default. "
                   "And that's the one you're in.") == ""


def test_each_close_resets_so_the_next_utterance_starts_clean():
    u = Utterance()
    u.segment("Let me check for you. You just have the one world right.")
    assert u.close("Let me check for you. You just have the one world right.") == ""
    u.segment("Right now, default.")
    u.segment("And that's the one you're in.")
    assert u.close("Right now, default. And that's the one you're in.") == ""


def test_a_close_with_no_segments_is_the_whole_utterance():
    assert Utterance().close("Whisper in my ear.") == "Whisper in my ear."


def test_a_close_that_extends_the_segments_adds_only_the_new_words():
    u = Utterance()
    u.segment("Whisper in my")
    assert u.close("Whisper in my ear, slowly.") == "ear, slowly."


def test_a_close_that_revises_sent_words_adds_nothing_rather_than_repeating_them():
    u = Utterance()
    u.segment("Whisper in my year.")
    assert u.close("Whisper in my ear.") == ""


def test_the_grok_service_submits_each_utterance_once():
    """The real subclass, driven by the event sequence xAI sent: one text frame, then an empty finalized
    one — the turn aggregator drops empty text, the turn strategy still learns STT is done."""
    import asyncio

    from pipecat.frames.frames import TranscriptionFrame

    svc = PROFILES["grok"].build(_settings(stt="grok", xai_api_key="k"))
    pushed = []

    async def capture(frame, *a, **k):
        pushed.append(frame)

    async def nothing(*a, **k):
        return None

    svc.push_frame = capture
    svc.stop_processing_metrics = nothing
    svc._trace_transcription = nothing

    async def run():
        await svc._handle_message({"type": "transcript.partial", "text": "Let me", "is_final": False})
        await svc._handle_message({"type": "transcript.partial", "text": SENTENCE, "is_final": True,
                                   "speech_final": False})
        await svc._handle_message({"type": "transcript.partial", "text": SENTENCE, "is_final": True,
                                   "speech_final": True})

    asyncio.run(run())
    finals = [f for f in pushed if type(f) is TranscriptionFrame]
    assert [f.text for f in finals] == [SENTENCE, ""]
    assert [f.finalized for f in finals] == [False, True]
