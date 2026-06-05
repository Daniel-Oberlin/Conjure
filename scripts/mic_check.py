#!/usr/bin/env python3
"""Mic diagnostic: list input devices and measure the input level for ~3 seconds.

Run this in the SAME terminal you run the voice loop from (mic permission is tied to the
terminal app). If the peak level stays near zero while you speak, the mic isn't reaching
Python — usually a macOS microphone-permission issue or the wrong default input device.

    python scripts/mic_check.py
"""

import array
import sys
import time


def main() -> int:
    try:
        import pyaudio
    except Exception as exc:  # noqa: BLE001
        print(f"pyaudio not available ({exc}). Install voice extras: pip install -e '.[voice]'")
        return 1

    pa = pyaudio.PyAudio()
    try:
        default_in = None
        try:
            default_in = pa.get_default_input_device_info()
        except Exception:  # noqa: BLE001
            pass

        print("Input devices:")
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                mark = " <-- default" if default_in and info["index"] == default_in["index"] else ""
                print(f"  [{info['index']}] {info['name']} (in_ch={info['maxInputChannels']}){mark}")

        if not default_in:
            print("\nNo default input device found.")
            return 1

        print("\nListening for 3 seconds — SPEAK NOW...")
        rate = 16000
        stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                         frames_per_buffer=1024)
        peak = 0
        t0 = time.time()
        while time.time() - t0 < 3.0:
            data = stream.read(1024, exception_on_overflow=False)
            samples = array.array("h", data)
            if samples:
                peak = max(peak, max(abs(s) for s in samples))
        stream.stop_stream()
        stream.close()

        print(f"\nPeak input level: {peak} / 32767")
        if peak < 200:
            print("→ Essentially silence. The mic isn't reaching Python. Likely causes:")
            print("   • Terminal app lacks mic permission: System Settings ▸ Privacy & Security ▸")
            print("     Microphone ▸ enable your terminal (Terminal/iTerm), then quit & reopen it.")
            print("   • Wrong default input device (check the list above / Sound settings).")
        else:
            print("→ Mic is capturing audio fine. If voice still doesn't respond, the issue is")
            print("  downstream (VAD/STT/turn-detection), not the microphone.")
        return 0
    finally:
        pa.terminate()


if __name__ == "__main__":
    sys.exit(main())
