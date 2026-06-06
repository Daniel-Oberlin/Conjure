# Setup & prerequisites

How Conjure's prerequisites are captured, what installs automatically, and what you do by hand.
Provider/model choices live in [providers.md](./providers.md); this is the install/onboarding guide.

## TL;DR

```bash
# from the repo root
./scripts/setup.sh                 # system deps + venv + voice extras + .env + doctor
# then edit .env: ANTHROPIC_API_KEY (voice) + POLY_PIZZA_API_KEY (models) + GOOGLE_API_KEY (images)
python -m conjure.doctor           # until all required checks pass
```

Phase 0/1 only (no voice) needs no system deps and no key:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                   # base: world server + MCP, no voice
```

## What's automatic vs. manual

| Prerequisite | How it's handled |
|---|---|
| **Python packages** | Automatic via `pyproject.toml`. Base: `pip install -e .` Voice: `pip install -e ".[voice]"` (an [optional-dependency group](https://packaging.python.org/en/latest/specifications/dependency-specifiers/)). |
| **Model weights** (Whisper, Kokoro) | Automatic — downloaded from Hugging Face on first run. |
| **System libraries** (portaudio, espeak-ng) | Not pip-installable → `scripts/setup.sh` installs them (brew/apt), or do it by hand (below). |
| **API keys** | You provide. Documented in `.env.example`; validated by the doctor. |
| **`adb`** (Quest testing) | `brew install android-platform-tools` — see [testing-on-quest.md](./testing-on-quest.md). |

The single source of truth for each: **`pyproject.toml`** (Python deps), **`.env.example`** (keys/config),
this doc + **`scripts/setup.sh`** (system libs), and **`conjure/doctor.py`** (the runtime checklist).

## Manual system-dependency install (if not using setup.sh)

- **macOS:** `brew install portaudio espeak-ng`
- **Debian/Ubuntu/Raspberry Pi:** `sudo apt-get install portaudio19-dev espeak-ng`

`portaudio` is needed for the host microphone/speaker (local audio transport). `espeak-ng` is used
by the local TTS for phonemization.

## API keys

Speech runs **locally** (Whisper STT, Kokoro TTS) — no keys. Keys are needed per feature
(all in `.env`, git-ignored; see `.env.example`):

| Key | Unlocks | Notes |
|---|---|---|
| **`ANTHROPIC_API_KEY`** | the voice/CLI **director** (Claude) | required for voice; billing — <https://console.anthropic.com> (separate from a Claude.ai subscription) |
| **`POLY_PIZZA_API_KEY`** | **`place_asset`** (real 3D models) | free, no billing — <https://poly.pizza/docs/api/v1.1> |
| **`GOOGLE_API_KEY`** | **`place_image` / `edit_image` / `set_skybox`** (Gemini) | billing — <https://aistudio.google.com> |

You can run with just the keys for the features you use (e.g. world + voice needs only Anthropic;
add Poly Pizza for models, Google for generated art). Switching to cloud speech or adding roster
LLMs needs their keys too — see `.env.example`.

> Note: the doctor checks the voice stack + `ANTHROPIC_API_KEY`; the asset/image keys surface as a
> clear error from `place_asset`/`place_image` if missing.

## The doctor (preflight check)

`python -m conjure.doctor` verifies the prerequisites for your selected stack and prints a
checklist with fixes, e.g.:

```
Conjure preflight  (stack: STT=whisper, TTS=kokoro, LLM=claude)

  ✓ pipecat-ai installed
  ✗ pyaudio present but portaudio missing
      → brew install portaudio  (Linux: apt-get install portaudio19-dev)
  ✓ Whisper backend present (model downloads on first run)
  ⚠ Kokoro TTS (model downloads on first run)
      → comes with pipecat-ai[kokoro]
  ✗ ANTHROPIC_API_KEY missing
      → add it to .env  (get one at https://console.anthropic.com)
  ⚠ world server not reachable at http://localhost:8080
      → start it: python -m conjure
```

It exits non-zero while any **required** check (✗) is unmet, so it's safe to use in scripts.

## Notes

- **Piper TTS** (alt to Kokoro) needs a separately-run Piper HTTP server + a downloaded voice
  model — Kokoro is more self-contained, which is why it's the default.
- **Heavy install:** the `[voice]` extra pulls large deps (e.g. torch). First install + first model
  download take a while; subsequent runs are fast.
- **Raspberry Pi:** prefer Piper for TTS and a small/streaming STT (Moonshine) — see providers.md.
