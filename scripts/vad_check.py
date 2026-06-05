#!/usr/bin/env python3
"""Feed live mic audio straight into pipecat's Silero VAD and print its confidence.

Bypasses the whole pipeline to answer one question: does Silero detect speech in YOUR mic
audio at all? Run in the same terminal as the voice app, then speak for ~5 seconds:

    python scripts/vad_check.py
"""

import array
import sys
import time


def main() -> int:
    try:
        import numpy as np
        import pyaudio
        from pipecat.audio.vad.silero import SileroVADAnalyzer
        from pipecat.audio.vad.vad_analyzer import VADParams
    except Exception as exc:  # noqa: BLE001
        print(f"missing deps ({exc}); run inside the venv with voice extras installed")
        return 1

    rate = 16000
    vad = SileroVADAnalyzer(sample_rate=rate, params=VADParams(confidence=0.3, min_volume=0.0))
    vad.set_sample_rate(rate)
    n = vad.num_frames_required()
    print(f"sample_rate={vad.sample_rate}, num_frames_required={n} samples "
          f"(~{n / rate * 1000:.0f} ms per window)")

    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                     frames_per_buffer=n)
    print("Speak now for ~5 seconds...\n")

    t0 = time.time()
    max_conf = 0.0
    speaking = 0
    total = 0
    while time.time() - t0 < 5.0:
        data = stream.read(n, exception_on_overflow=False)
        conf = float(np.ravel(vad.voice_confidence(data))[0])
        peak = max((abs(s) for s in array.array("h", data)), default=0)
        total += 1
        max_conf = max(max_conf, conf)
        if conf >= 0.5:
            speaking += 1
        if total % 5 == 0:
            print(f"  conf={conf:.2f}  peak={peak}")
    stream.stop_stream()
    stream.close()
    pa.terminate()

    print(f"\nmax confidence = {max_conf:.2f}; windows >= 0.5: {speaking}/{total}")
    if max_conf >= 0.5:
        print("→ Silero DOES detect your speech directly. The break is in how the pipeline feeds")
        print("  the VAD (transport/integration), not Silero or your audio.")
    else:
        print("→ Silero does NOT detect speech even fed directly — points at audio scale/format/rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
