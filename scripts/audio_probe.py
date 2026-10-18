#!/usr/bin/env python
"""A browser stand-in for the voice server's /audio wire (conjure/transport.py) — spike 2/3 of
temp/plans/quest-audio-streaming-2026-10-18.md, runnable without a headset.

Connects to /audio?user=<name> (the world server's relay by default, or the voice server directly),
sends `hello`, streams a WAV as Int16 PCM in real time at the WAV's own rate (so a 48 kHz file exercises
the server-side resample exactly as the Quest would), then listens: every text message is printed with a
timestamp, every binary frame is appended to an output WAV. Prints the one number spike 3 wants —
seconds from the end of the sent speech to the first reply audio — and what the server heard.

    python scripts/audio_probe.py temp/stt-corpus/clips/20260901-082216-163.wav
    python scripts/audio_probe.py clip.wav --url ws://localhost:8771/audio --user daniel --rate 48000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave
from pathlib import Path


def _read_wav(path: Path, rate: int | None) -> tuple[bytes, int]:
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2, "mono 16-bit WAV only"
        pcm, src = w.readframes(w.getnframes()), w.getframerate()
    if rate and rate != src:                     # resample locally so the wire carries the asked-for rate
        import numpy as np
        import soxr
        x = np.frombuffer(pcm, dtype=np.int16)
        pcm = soxr.resample(x, src, rate).astype(np.int16).tobytes()
        src = rate
    return pcm, src


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("wav", type=Path)
    ap.add_argument("--url", default="ws://localhost:8080/audio", help="the relay (default) or the voice server")
    ap.add_argument("--user", default="daniel")
    ap.add_argument("--rate", type=int, default=None, help="send at this rate (resampled locally), e.g. 48000")
    ap.add_argument("--wait", type=float, default=25.0, help="seconds to listen after the clip")
    ap.add_argument("--out", type=Path, default=Path("temp/audio-probe-reply.wav"))
    ap.add_argument("--frame-ms", type=int, default=20)
    args = ap.parse_args()

    import websockets

    pcm, rate = _read_wav(args.wav, args.rate)
    url = f"{args.url}?user={args.user}"
    t0 = time.perf_counter()
    stamp = lambda: f"{time.perf_counter() - t0:7.3f}s"  # noqa: E731
    reply = bytearray()
    out_rate = {"v": 24000}
    first_audio = {"t": None}
    sent_end = {"t": None}

    async with websockets.connect(url, max_size=None) as ws:
        print(f"{stamp()} connected {url}")
        await ws.send(json.dumps({"type": "hello", "user": args.user, "in_rate": rate}))

        async def listen():
            async for msg in ws:
                if isinstance(msg, bytes):
                    if first_audio["t"] is None:
                        first_audio["t"] = time.perf_counter()
                        print(f"{stamp()} first reply audio ({len(msg)} bytes)")
                    reply.extend(msg)
                else:
                    ev = json.loads(msg)
                    if ev.get("type") == "ready":
                        out_rate["v"] = int(ev.get("out_rate") or 24000)
                    print(f"{stamp()} {ev}")

        lt = asyncio.create_task(listen())
        frame = int(rate * args.frame_ms / 1000) * 2
        print(f"{stamp()} streaming {len(pcm) / rate / 2:.2f}s of speech at {rate} Hz in {args.frame_ms} ms frames")
        for i in range(0, len(pcm), frame):
            await ws.send(pcm[i:i + frame])
            await asyncio.sleep(args.frame_ms / 1000)
        # A second of silence so the VAD closes the segment the way a real mic would.
        for _ in range(int(1000 / args.frame_ms)):
            await ws.send(b"\x00" * frame)
            await asyncio.sleep(args.frame_ms / 1000)
        sent_end["t"] = time.perf_counter()
        print(f"{stamp()} speech ended (silence sent); listening {args.wait:.0f}s")
        try:
            await asyncio.wait_for(lt, timeout=args.wait)
        except asyncio.TimeoutError:
            lt.cancel()

    if reply:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(args.out), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(out_rate["v"]); w.writeframes(bytes(reply))
        print(f"reply: {len(reply) / out_rate['v'] / 2:.2f}s of audio at {out_rate['v']} Hz → {args.out}")
    if first_audio["t"] and sent_end["t"]:
        print(f"speech-end → first reply audio: {first_audio['t'] - sent_end['t']:.2f}s")
    elif not reply:
        print("no reply audio received")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
