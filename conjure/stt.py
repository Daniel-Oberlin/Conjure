"""The ears — which speech-to-text engine the voice client listens through.

`Settings.stt` (`CONJURE_STT`) picks a **hearing profile** from `PROFILES`, the mirror of the TTS
profiles in conjure/tts.py: how to build the pipecat service (a heavy, lazy import), the input sample
rate, what it needs to run, and whether the `--stt-corpus` tap can see what it heard. The two settings
are independent — Whisper with Grok speech, Grok hearing with Kokoro speech, either engine for both.

Two shapes of recognizer sit behind the same seam, and the difference matters downstream:

- **Segmented** (Whisper): our VAD closes an utterance into a WAV and the model decodes it whole. Nothing
  is recognised until you stop talking; the corpus tap works because the exact WAV exists at one point.
- **Streaming** (Grok): audio goes to the engine continuously over a WebSocket and transcripts come back
  as they form, with the engine's own endpointing marking the end of an utterance. Our VAD still runs —
  the turn aggregator keys off its speaking frames, and that is what decides when a turn is submitted —
  but no WAV is ever assembled, so the corpus tap has nothing to record and is refused up front.

Keys: as for TTS, a cloud recognizer's key lives with the voice client, because the voice client is what
streams audio to it (docs/specs/voice.md §1).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings
    from .stt_corpus import Corpus

MayRecord = Callable[[], bool]


@dataclass(frozen=True)
class HearingProfile:
    """One STT engine as the voice client sees it. `build` is the only thing that imports pipecat; it
    takes the corpus and its gate so a segmented engine can tap itself (`corpus_tap`)."""
    name: str
    description: str
    sample_rate: int                                 # what the microphone is opened at
    build: Callable[..., object]                     # (settings, *, corpus=None, may_record=None) → STTService
    streaming: bool = False                          # transcripts form while you speak (vs. after)
    corpus_tap: bool = False                         # can `--stt-corpus` record the audio it decoded?
    needs_key: Optional[str] = None                  # Settings field holding the engine's key, if any


# --------------------------------------------------------------------------- Whisper (local, segmented)

# `small.en`, not `base`. Measured on an M2 Max over a 12-clip synthetic corpus (see
# docs/investigations/stt-accuracy.md): `base` is the MULTILINGUAL model, and passing language=EN only
# constrains decoding — it does not buy the English-only weights. WER 8.3% → 2.5%, and `small.en` was
# also the only model whose decode time did not blow up on band-limited audio (faster-whisper re-decodes
# at a higher temperature when a segment trips its confidence thresholds, so a small model on a bad mic
# gets slower and worse together). Costs ~0.9s more per utterance on CPU. Do not "upgrade" to
# distil-medium.en — measured worse AND slower.
WHISPER_MODEL = "small.en"


def _build_whisper(settings: "Settings", *, corpus: Optional["Corpus"] = None,
                   may_record: Optional[MayRecord] = None):
    import asyncio
    import sys
    import time

    from pipecat.frames.frames import TranscriptionFrame
    from pipecat.services.whisper.stt import WhisperSTTService
    from pipecat.transcriptions.language import Language

    st = WhisperSTTService.Settings(model=WHISPER_MODEL, language=Language.EN)
    if corpus is None:
        return WhisperSTTService(settings=st)

    allowed = may_record or (lambda: True)

    # Tap `run_stt`: it receives the exact WAV the recognizer saw and yields the text it produced, so the
    # pair is captured at the one point where both exist. Nothing here may raise into the pipeline — a
    # diagnostic that can break the thing it is diagnosing is worse than none.
    class _CorpusWhisperSTTService(WhisperSTTService):
        async def run_stt(self, audio: bytes):
            t0 = time.perf_counter()
            said = ""
            async for frame in super().run_stt(audio):
                if isinstance(frame, TranscriptionFrame):
                    said = frame.text or ""
                yield frame
            if not said.strip():
                return                       # silence / a dropped no-speech segment: nothing to label
            if not allowed():
                return
            try:
                await asyncio.to_thread(
                    corpus.record, audio, hypothesis=said.strip(), model=WHISPER_MODEL,
                    decode_s=round(time.perf_counter() - t0, 3),
                    sample_rate=self.sample_rate)
            except Exception as exc:  # noqa: BLE001 — never cost the user a turn over a dropped clip
                print(f"[stt-corpus] could not record clip: {exc}", file=sys.stderr)

    return _CorpusWhisperSTTService(settings=st)


# --------------------------------------------------------------------------- Grok (xAI, streaming)

def _build_grok(settings: "Settings", *, corpus: Optional["Corpus"] = None,
                may_record: Optional[MayRecord] = None):
    from pipecat.services.xai.stt import XAISTTService
    from pipecat.transcriptions.language import Language

    # pipecat's service streams 16 kHz PCM to wss://api.x.ai/v1/stt and leaves the model, endpointing
    # (server default 400 ms) and interim results at their defaults. `interim_results` stay on: the turn
    # aggregator uses them to notice speech resuming, and they cost nothing extra.
    return XAISTTService(api_key=settings.xai_api_key or "", sample_rate=16000,
                         settings=XAISTTService.Settings(language=Language.EN))


PROFILES: dict[str, HearingProfile] = {
    "whisper": HearingProfile(
        name="whisper", description=f"Whisper {WHISPER_MODEL} — local, segmented", sample_rate=16000,
        build=_build_whisper, corpus_tap=True),
    "grok": HearingProfile(
        name="grok", description="xAI Grok STT — cloud, streaming", sample_rate=16000,
        build=_build_grok, streaming=True, needs_key="xai_api_key"),
}


def profile_for(settings: "Settings", *, corpus: bool = False) -> HearingProfile:
    """The profile `Settings.stt` names, checked for what it needs. `corpus=True` says the caller wants
    `--stt-corpus`, which only a profile with a tap can honour — refused here, at the door, rather than
    recording nothing in silence (the one failure a governed recorder must not have)."""
    name = (settings.stt or "").strip().lower()
    prof = PROFILES.get(name)
    if prof is None:
        raise ValueError(f"CONJURE_STT={settings.stt!r} is not a speech recognizer; choose one of "
                         f"{', '.join(sorted(PROFILES))}")
    if prof.needs_key and not getattr(settings, prof.needs_key, None):
        from .config import env_var

        raise ValueError(f"CONJURE_STT={name} needs {env_var(prof.needs_key)} (set it in .env)")
    if corpus and not prof.corpus_tap:
        raise ValueError(f"--stt-corpus records the WAV the recognizer decoded, and CONJURE_STT={name} "
                         f"streams audio instead of decoding WAVs — nothing to record. Use "
                         f"CONJURE_STT=whisper to collect clips.")
    return prof
