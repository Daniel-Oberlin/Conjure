# Conjure

A voice-driven **holodeck**: describe a scene aloud and an LLM builds an interactive WebXR world
you experience on a Meta Quest 3. Speak, and a real-time agent fills the space around you —
placing objects, summoning real 3D models, generating and editing art, and wrapping you in
generated environments.

> **Design docs live in [`docs/`](./docs/).** Start with [vision](./docs/vision.md), then
> [spec](./docs/spec.md), [architecture](./docs/architecture.md), and the [roadmap](./docs/roadmap.md).

## What works today

You stand in a holodeck (black void, 1 m white grid) and talk. The director can:

- **Build** — primitives, and real CC-licensed **3D models** pulled from the web ([Poly Pizza]),
  auto-scaled to real-world size and placed on the floor; move / rotate / resize by voice.
- **Create art** — **AI-generated** paintings, posters, and photos (Google Gemini), hung as framed
  images — **edit them conversationally** in place ("make the dragon breathe fire") and
  **outpaint** them wider.
- **Set the scene** — generate a high-res 360° **skybox** that wraps the whole environment, or
  turn any in-world image into the surrounding sky.
- **Feel real-time** — a brief spoken acknowledgement ("on it") the instant you ask.

Everything is live: edits broadcast over a WebSocket to every connected headset. Models for
speech run locally; only the reasoning/generation models are cloud.

Implemented phases (see [roadmap](./docs/roadmap.md)): **0** world doc + patch protocol + A-Frame
client · **1** world-editing MCP tools · **2** voice loop · **3** assets · **4** image generation +
editing + skybox.

## How it fits together

```
 voice  ┌───────────┐  MCP   ┌──────────────┐  WebSocket  ┌──────────────┐
 ◀────▶ │  PipeCat  │ ◀────▶ │ World server │ ◀─────────▶ │ A-Frame      │  Quest 3
 (mic/  │ +director │        │ (FastAPI):   │  (patches)  │ WebXR client │  (or any
  spkr) │  (Claude) │        │ world + MCP  │             │              │   browser)
        └───────────┘        │ + assets/gen │
                             └──────┬───────┘
                                    │  Poly Pizza (models) · Gemini (images) · local Whisper/Kokoro
```

- **World server** (`conjure/`, Python/FastAPI) owns one declarative world document, applies
  **patches**, serves the WebXR app + cached assets, and exposes world-editing **MCP tools**.
- **Director** is an LLM (Claude) that drives those MCP tools by voice (PipeCat: local Whisper STT
  → Claude → local Kokoro TTS).
- Model roles (STT/TTS/LLM/image-gen) and asset sources sit behind **swappable registries**
  (`docs/providers.md`), so providers plug in without touching callers.

## Quickstart

**World only** (no voice — drive it with HTTP patches or the MCP server):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m conjure                       # serves http://localhost:8080  (open it in a browser)
python scripts/send_patch.py examples/patches/add_cube.json   # a cube appears live
```

**Full voice experience:**

```bash
./scripts/setup.sh                      # system deps + venv + voice extras + .env
#   then edit .env with your keys (see below), and:
python -m conjure.doctor                # confirm prerequisites are green
python -m conjure                       # terminal 1: world server
python -m conjure.voice                 # terminal 2: voice loop (use earbuds — see note)
```

Then speak: *"put an oak tree in front of me", "paint a sunset over mountains and hang it on the
wall", "wrap me in a misty forest", "make the painting nighttime".*

### Keys (in `.env` — git-ignored; see `.env.example`)

| Key | For | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | the voice director (Claude) | required for voice; billing |
| `POLY_PIZZA_API_KEY` | `place_asset` (3D models) | free, no billing |
| `GOOGLE_API_KEY` | `place_image` / `edit_image` / `set_skybox` | Gemini; billing |

Speech (Whisper STT, Kokoro TTS) runs **locally** — no keys. Full prerequisites + the doctor:
[docs/setup.md](./docs/setup.md). Provider options: [docs/providers.md](./docs/providers.md).

> **Audio note:** use **earbuds**. On an open room mic+speaker the director's own TTS feeds back
> into the mic; room-speaker support (push-to-talk / echo cancellation) is a tracked follow-up.

## CLI — drive it from the terminal (no voice)

Quiet, fast, discrete testing without the mic. With the world server running:

```bash
conjure-cli asset "oak tree" --size 7      # direct, deterministic tool commands
conjure-cli image "an oil painting of a red dragon"
conjure-cli skybox "a misty pine forest"
conjure-cli world                          # print the world

conjure-cli say "put a tree in front of me and hang a sunset painting"   # the director, by text
conjure-cli                                # no args → interactive director REPL
```

Quiet by default; add `-v` for tool calls and library logs. (`say`/REPL need `ANTHROPIC_API_KEY`.)

## On the Quest 3

- **USB (quickest):** `adb reverse tcp:8080 tcp:8080`, then open `http://localhost:8080` in the
  Quest browser and enter VR. Full step-by-step: [docs/testing-on-quest.md](./docs/testing-on-quest.md).
- **Wireless (HTTPS):** [docs/https-setup.md](./docs/https-setup.md) — cloudflared (fastest), Caddy
  + Let's Encrypt, or Tailscale.

## Layout

```
conjure/    world server (schema · world store · FastAPI app · MCP tools · voice loop ·
            CLI · assets pipeline · image-gen registry · config · doctor)
client/     A-Frame WebXR client + live patch applier
examples/   starter world + hand-authored example patches
scripts/    setup.sh, send_patch.py, mcp_smoke.py, mic_check.py, vad_check.py
docs/       vision · spec · architecture · decisions · providers · roadmap · setup · testing/https guides
```

[Poly Pizza]: https://poly.pizza
