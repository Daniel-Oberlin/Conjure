"""Conjure preflight check ("doctor") — verifies Phase-2 prerequisites and tells you how to fix gaps.

Run:  python -m conjure.doctor   (or the `conjure-doctor` console script)

Safe on a base install: it reports what the voice extras need *without* importing them, so a fresh
clone gets actionable guidance instead of a stack trace. Exit code is non-zero if a prerequisite
that's *required for the selected stack* is missing.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.request

from .config import get_settings

OK, WARN, FAIL = "✓", "⚠", "✗"


def _have(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def main() -> int:
    s = get_settings()
    rows: list[tuple[str, str, str]] = []  # (status, label, fix-hint)
    hard_fail = False

    # 1. Voice Python deps
    if _have("pipecat"):
        rows.append((OK, "pipecat-ai installed", ""))
    else:
        rows.append((FAIL, "pipecat-ai not installed", 'pip install -e ".[voice]"'))
        hard_fail = True

    # 2. Local audio (portaudio, surfaced via pyaudio)
    if _have("pyaudio"):
        try:
            import pyaudio  # noqa: F401
            rows.append((OK, "local audio (pyaudio + portaudio)", ""))
        except Exception:
            rows.append((FAIL, "pyaudio present but portaudio missing",
                         "brew install portaudio  (Linux: apt-get install portaudio19-dev)"))
            hard_fail = True
    else:
        rows.append((WARN, "pyaudio not installed (host mic/speaker)", 'comes with the [voice] extra'))

    # 3. STT backend
    if s.stt == "whisper":
        if _have("mlx_whisper") or _have("faster_whisper"):
            rows.append((OK, "Whisper backend present (model downloads on first run)", ""))
        else:
            rows.append((WARN, "Whisper backend not found", "comes with pipecat-ai[whisper]"))
    else:
        rows.append((WARN, f"STT='{s.stt}' (non-local) — ensure its key/deps are set", ""))

    # 4. TTS backend
    if s.tts == "kokoro":
        present = _have("kokoro_onnx")
        rows.append((OK if present else WARN,
                     "Kokoro TTS (model downloads on first run)",
                     "" if present else "comes with pipecat-ai[kokoro]"))
    elif s.tts == "piper":
        rows.append((WARN, "Piper TTS — run a local Piper server + voice model", "see docs/setup.md"))
    else:
        rows.append((WARN, f"TTS='{s.tts}' (non-local) — ensure its key/deps are set", ""))

    # 5. Director LLM credentials
    if s.llm == "claude":
        if s.anthropic_api_key:
            rows.append((OK, "ANTHROPIC_API_KEY set", ""))
        else:
            rows.append((FAIL, "ANTHROPIC_API_KEY missing",
                         "add it to .env  (get one at https://console.anthropic.com)"))
            hard_fail = True
    else:
        rows.append((WARN, f"LLM='{s.llm}' — ensure its key/deps are set", ""))

    # 6. World server reachability (informational only)
    try:
        with urllib.request.urlopen(f"{s.world_url}/world", timeout=1.5) as resp:
            resp.read(1)
        rows.append((OK, f"world server reachable at {s.world_url}", ""))
    except Exception:
        rows.append((WARN, f"world server not reachable at {s.world_url}", "start it: python -m conjure"))

    print(f"\nConjure preflight  (stack: STT={s.stt}, TTS={s.tts}, LLM={s.llm})\n")
    for status, label, hint in rows:
        print(f"  {status} {label}")
        if hint:
            print(f"      → {hint}")
    print()
    if hard_fail:
        print("Some REQUIRED prerequisites are missing (✗). Fix them, then re-run.\n")
        return 1
    print("All required prerequisites look good.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
