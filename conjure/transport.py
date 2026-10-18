"""The audio device — where the voice pipeline's sound comes from and goes to.

The third profile registry beside the ears (conjure/stt.py) and the mouth (conjure/tts.py). A transport
is pipecat's object with `.input()` and `.output()`; everything between them — VAD, STT, the turn
aggregator, the bridge to the agent server, TTS — is unchanged when it is swapped. Two real ones:

- **local** — the host's microphone and speaker (PortAudio), as the voice client has always run.
- **websocket** — a browser on the other end of a socket: the Quest headset's page captures the mic in
  an AudioWorklet and plays the reply through Web Audio (client/voice-link.js), and the voice server
  runs ONE pipeline per connection (`conjure.voice --serve`). Raw PCM both ways, so a second client (a
  desktop browser, a probe script) can speak it from the description in `Wire` below.

A WebRTC transport (pipecat's `SmallWebRTCTransport`) is the third row when a remote participant needs
it; it is UDP, so it cannot reach a headset over `adb reverse` or a cloudflared tunnel, which is why the
socket came first (temp/plans/quest-audio-streaming-2026-10-18.md §2.1).

## Wire (the `websocket` profile)

    client → server   text    {"type":"hello", "user":"daniel", "in_rate":48000}   # first, once
                      binary  Int16 LE mono PCM at in_rate — any frame size (20 ms suggested)
                      text    {"type":"interrupt"}                                 # barge-in (C3b)
    server → client   text    {"type":"ready", "out_rate":24000}                   # the pipeline is up
                      binary  Int16 LE mono PCM at out_rate, paced to real time by pipecat
                      text    {"type":"speaking", "on": true|false}                # TTS started/stopped
                      text    {"type":"transcript", "text": "…"}                   # what STT heard
                      text    {"type":"notice", "text": "…"}

The browser sends at its AudioContext's native rate (48 kHz on the Quest) and the SERVER resamples to
the pipeline's rate: pipecat's input transport does not (it takes the frame's rate as the pipeline's),
so the resample lives in the serializer, on pipecat's streaming SOXR resampler. Decimating in the
worklet is the thing to do when bandwidth matters (a tunnel), and nothing to do before.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings


@dataclass(frozen=True)
class TransportProfile:
    """One audio device as the voice pipeline sees it. `build` is the only thing that imports pipecat.
    `serves` says whether this transport ACCEPTS connections — one pipeline each — or is the one device
    the process was started on."""
    name: str
    description: str
    serves: bool
    build: Callable[..., object]       # (settings, *, in_rate, out_rate, **ctx) → a pipecat BaseTransport


# --------------------------------------------------------------------------- local (host mic + speaker)

def _build_local(settings: "Settings", *, in_rate: int, out_rate: int, **_):
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    return LocalAudioTransport(LocalAudioTransportParams(
        audio_in_enabled=True, audio_in_sample_rate=in_rate,      # the recognizer's rate
        audio_out_enabled=True, audio_out_sample_rate=out_rate,   # the engine's native rate
    ))


# --------------------------------------------------------------------------- websocket (a browser)

def make_wire_serializer(*, in_rate_default: int = 48000):
    """The `Wire` above as a pipecat `FrameSerializer`. Built per connection (it holds the resampler's
    stream state and the client's declared rate). Pure enough to unit-test with fake frames."""
    from pipecat.audio.utils import create_stream_resampler
    from pipecat.frames.frames import (InputAudioRawFrame, InputTransportMessageFrame, OutputAudioRawFrame,
                                       OutputTransportMessageFrame)
    from pipecat.serializers.base_serializer import FrameSerializer

    class WireSerializer(FrameSerializer):
        def __init__(self):
            super().__init__()
            self.in_rate = in_rate_default          # what the client SAYS it sends, from its hello
            self.pipeline_rate = 16000              # set from the StartFrame
            self.out_rate = 24000
            self.hello: Optional[dict] = None
            # HQ, not pipecat's VHQ default: measured 21 ms of latency against 37 ms, on a signal that
            # is going to a recognizer, not an ear (QQ is 0 ms and aliases; LQ 6 ms).
            self._resampler = create_stream_resampler(quality="HQ")

        async def setup(self, frame):
            self.pipeline_rate = frame.audio_in_sample_rate
            self.out_rate = frame.audio_out_sample_rate

        async def serialize(self, frame):
            if isinstance(frame, OutputAudioRawFrame):
                return frame.audio                              # Int16 mono at out_rate, as produced
            if isinstance(frame, OutputTransportMessageFrame):
                if self.should_ignore_frame(frame):
                    return None
                return json.dumps(frame.message)
            return None                                         # control frames stay inside the pipeline

        async def deserialize(self, data):
            if isinstance(data, (bytes, bytearray)):
                audio = bytes(data)
                if self.in_rate != self.pipeline_rate:
                    audio = await self._resampler.resample(audio, self.in_rate, self.pipeline_rate)
                if not audio:
                    return None
                return InputAudioRawFrame(audio=audio, sample_rate=self.pipeline_rate, num_channels=1)
            try:
                msg = json.loads(data)
            except (TypeError, ValueError):
                return None
            if not isinstance(msg, dict):
                return None
            if msg.get("type") == "hello":
                self.hello = msg
                rate = msg.get("in_rate")
                if isinstance(rate, (int, float)) and 8000 <= rate <= 192000:
                    self.in_rate = int(rate)
                return None                                     # consumed here; the server reads `.hello`
            return InputTransportMessageFrame(message=msg)      # interrupt, ptt, … → the pipeline

    return WireSerializer()


def _build_websocket(settings: "Settings", *, in_rate: int, out_rate: int, websocket=None, serializer=None, **_):
    from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

    if websocket is None:
        raise ValueError("the websocket transport is built per connection — pass websocket=")
    return FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
        audio_in_enabled=True, audio_in_sample_rate=in_rate,
        audio_out_enabled=True, audio_out_sample_rate=out_rate,
        serializer=serializer or make_wire_serializer(),
    ))


PROFILES: dict[str, TransportProfile] = {
    "local": TransportProfile(name="local", description="host microphone and speaker", serves=False,
                              build=_build_local),
    "websocket": TransportProfile(name="websocket", description="a browser over /audio (raw PCM)",
                                  serves=True, build=_build_websocket),
}
