"""The audio transport registry (conjure/transport.py) and the raw-PCM wire serializer the websocket
profile speaks — docs/specs/voice.md §11. The serializer needs pipecat's frame classes, so those tests
skip on a base install; the registry's shape does not."""

import json
import struct

import pytest

from conjure.transport import PROFILES


def test_the_two_transports_and_which_one_serves():
    assert set(PROFILES) == {"local", "websocket"}
    assert PROFILES["local"].serves is False
    assert PROFILES["websocket"].serves is True


def test_the_websocket_transport_is_built_per_connection():
    with pytest.raises(ValueError, match="websocket="):
        PROFILES["websocket"].build(None, in_rate=16000, out_rate=24000)


# --------------------------------------------------------------------------- the wire

def _pcm(n, rate, hz=440.0, amp=8000):
    import math
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * hz * i / rate)) for i in range(n)))


async def _serializer(in_rate=48000, pipeline_rate=16000, out_rate=24000):
    pytest.importorskip("pipecat")
    from pipecat.frames.frames import StartFrame

    from conjure.transport import make_wire_serializer

    s = make_wire_serializer(in_rate_default=in_rate)
    await s.setup(StartFrame(audio_in_sample_rate=pipeline_rate, audio_out_sample_rate=out_rate))
    return s


async def test_hello_sets_the_clients_rate_and_is_consumed():
    s = await _serializer(in_rate=16000)
    assert await s.deserialize(json.dumps({"type": "hello", "user": "alice", "in_rate": 48000})) is None
    assert s.in_rate == 48000 and s.hello["user"] == "alice"
    assert await s.deserialize(json.dumps({"type": "hello", "in_rate": 5})) is None   # nonsense rate ignored
    assert s.in_rate == 48000


async def test_browser_audio_is_resampled_to_the_pipelines_rate():
    """The browser sends at its AudioContext's rate (48 kHz on the Quest) and pipecat's input transport
    takes a frame's rate as the pipeline's, so the serializer is where 48 → 16 happens."""
    from pipecat.frames.frames import InputAudioRawFrame

    s = await _serializer(in_rate=48000, pipeline_rate=16000)
    total = 0
    for _ in range(10):                                   # 10 × 20 ms at 48 kHz
        f = await s.deserialize(_pcm(960, 48000))
        if f is not None:
            assert isinstance(f, InputAudioRawFrame) and f.sample_rate == 16000 and f.num_channels == 1
            total += f.num_frames
    assert 2700 <= total <= 3200                          # ≈ 200 ms at 16 kHz, less ~21 ms resampler latency


async def test_audio_at_the_pipelines_rate_passes_straight_through():
    s = await _serializer(in_rate=16000, pipeline_rate=16000)
    pcm = _pcm(320, 16000)
    f = await s.deserialize(pcm)
    assert f.audio == pcm and f.sample_rate == 16000


async def test_control_messages_become_transport_messages_and_junk_is_dropped():
    from pipecat.frames.frames import InputTransportMessageFrame

    s = await _serializer()
    f = await s.deserialize(json.dumps({"type": "interrupt"}))
    assert isinstance(f, InputTransportMessageFrame) and f.message == {"type": "interrupt"}
    assert await s.deserialize("not json") is None
    assert await s.deserialize("[1,2]") is None


async def test_reply_audio_goes_out_raw_and_messages_as_json():
    from pipecat.frames.frames import OutputAudioRawFrame, OutputTransportMessageFrame, TTSStartedFrame

    s = await _serializer()
    pcm = _pcm(480, 24000)
    assert await s.serialize(OutputAudioRawFrame(audio=pcm, sample_rate=24000, num_channels=1)) == pcm
    out = await s.serialize(OutputTransportMessageFrame(message={"type": "speaking", "on": True}))
    assert json.loads(out) == {"type": "speaking", "on": True}
    assert await s.serialize(TTSStartedFrame()) is None            # control frames stay inside the pipeline
