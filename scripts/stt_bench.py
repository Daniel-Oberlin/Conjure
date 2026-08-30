#!/usr/bin/env python3
"""Compare Whisper model sizes on synthesized speech, clean and band-limited.

Answers "which model, and what does it cost" without needing a microphone: utterances come from
macOS `say`, so the run is repeatable on any Mac. This is the bench that chose `small.en` over `base`
(docs/investigations/stt-accuracy.md §2.2).

    python scripts/stt_bench.py                 # the models that produced the recorded table
    python scripts/stt_bench.py --models base,small.en

**Read the limits before quoting a number.** TTS speech is cleaner and more articulated than yours,
and `--narrowband` only removes the frequency *band* the Bluetooth voice path removes — not its noise
suppression, AGC pumping or packet-loss concealment, which are plausibly what actually hurt. The
default corpus is ~120 reference words, so **one word error moves WER by ~0.8 points** and differences
under about two points are noise. The timings do not suffer that problem and are the trustworthy half.

For a verdict on real audio, use the corpus recorded by `python -m conjure.voice --stt-corpus`
(docs/specs/voice.md §9) — this script cannot represent a Bluetooth microphone at all.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "temp" / "stt-bench"        # temp/ is gitignored; clips are cached between runs

# Conjure's own command vocabulary — the words the recognizer has weak priors for are the point.
LINES = [
    "Conjure, make a beagle next to the table.",
    "Turn on surface edges and show the annotations.",
    "Put the skybox back to the meadow and make the billboard face me.",
    "Switch to the builder agent and open the shell.",
    "Make the table dark pink and move it two meters to the left.",
    "Where am I? List the worlds in this space.",
]
VOICES = ["Samantha", "Fred"]            # two keeps a CPU run to a few minutes

# Short name → faster-whisper model id. The recorded table used exactly these.
MODELS = {
    "base": "base",                                  # multilingual — the old default, kept as the baseline
    "base.en": "base.en",
    "small.en": "small.en",                          # what ships
    "distil-medium.en": "Systran/faster-distil-whisper-medium.en",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
}


def _words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def wer(ref: str, hyp: str) -> tuple[int, int]:
    """(edit distance, reference length), so totals pool correctly across clips."""
    r, h = _words(ref), _words(hyp)
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i]
        for j, hw in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw)))
        prev = cur
    return prev[len(h)], len(r)


def synth(text: str, voice: str, path: Path) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["say", "-v", voice, "-o", str(path), "--data-format=LEI16@16000", text],
                       capture_output=True)
    return r.returncode == 0 and path.exists()


def read_wav(path: Path):
    import numpy as np
    with wave.open(str(path), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return a.astype(np.float32) / 32768.0


def narrowband(x):
    """The Bluetooth HFP voice path, approximately: 300-3400 Hz, decimated to 8 kHz and back.

    The upper band is gone for good after the round trip — that is the point. What this does NOT
    simulate is the earbud's own noise suppression and AGC, so treat it as a floor on the damage."""
    from scipy.signal import butter, resample_poly, sosfilt
    y = sosfilt(butter(6, [300 / 8000, 3400 / 8000], btype="band", output="sos"), x)
    return resample_poly(resample_poly(y, 1, 2), 2, 1).astype("float32")


def main() -> int:
    ap = argparse.ArgumentParser(description="Whisper model comparison on synthesized speech.")
    ap.add_argument("--models", default=",".join(MODELS),
                    help=f"comma-separated subset of: {', '.join(MODELS)}")
    ap.add_argument("--voices", default=",".join(VOICES), help="macOS `say` voices")
    ap.add_argument("--clean-only", action="store_true", help="skip the narrowband condition")
    args = ap.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper is missing — install the voice extras (./scripts/setup.sh).")
        return 1

    clips = []
    for i, text in enumerate(LINES):
        for voice in args.voices.split(","):
            p = OUT / "clips" / f"{i}_{voice}.wav"
            if p.exists() or synth(text, voice.strip(), p):
                clips.append((text, read_wav(p)))
    if not clips:
        print("`say` produced no audio — is this macOS?", file=sys.stderr)
        return 1
    secs = sum(len(a) for _, a in clips) / 16000.0
    ref_words = sum(len(_words(t)) for t, _ in clips)
    print(f"corpus: {len(clips)} clips, {secs:.1f}s, {ref_words} reference words "
          f"(one word error ≈ {100.0 / ref_words:.1f} WER points)\n")

    conditions = [("clean", lambda a: a)] + ([] if args.clean_only else [("narrowband", narrowband)])
    rows = []
    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        if name not in MODELS:
            print(f"unknown model {name!r} — choose from {', '.join(MODELS)}", file=sys.stderr)
            return 2
        try:
            model = WhisperModel(MODELS[name], device="cpu", compute_type="default")
        except Exception as exc:  # noqa: BLE001 — a download or backend failure shouldn't end the run
            print(f"{name:>18}  unavailable ({type(exc).__name__}: {exc})")
            continue
        row = {"model": name}
        for cond, fn in conditions:
            errs = words = 0
            t0 = time.perf_counter()
            for ref, audio in clips:
                segs, _ = model.transcribe(fn(audio), language="en")
                e, w = wer(ref, " ".join(s.text for s in segs))
                errs, words = errs + e, words + w
            dt = time.perf_counter() - t0
            row[cond] = 100.0 * errs / words
            row[f"{cond}_s"] = dt / len(clips)
            print(f"{name:>18} {cond:>11}  WER {row[cond]:5.1f}%  {dt/len(clips):5.2f}s/clip  "
                  f"{secs/dt:5.1f}x realtime")
        rows.append(row)
        del model

    print(f"\n--- CPU, faster-whisper (note: no Metal backend, so weights run upconverted to fp32) ---")
    head = f"{'model':>18} " + " ".join(f"{c:>11}" for c, _ in conditions) + f"{'s/clip':>9}"
    print(head)
    for r in rows:
        cells = " ".join(f"{r.get(c, float('nan')):10.1f}%" for c, _ in conditions)
        print(f"{r['model']:>18} {cells} {r.get(f'{conditions[0][0]}_s', 0):8.2f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
