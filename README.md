# Conjure

A voice-driven **holodeck**: describe a scene aloud and an LLM builds an interactive WebXR world
you experience on a Meta Quest 3. Speak, and a real-time agent fills the space around you —
placing objects, summoning real 3D models, generating and editing art, and wrapping you in
generated environments.

> **Design docs live in [`docs/`](./docs/).** Start with [vision](./docs/vision.md), then
> [spec](./docs/spec.md), [architecture](./docs/architecture.md), and the [roadmap](./docs/roadmap.md).

## What works today

You stand in a holodeck (black void, 1 m white grid) and talk. Conjure can:

- **Build** — primitives, and real CC-licensed **3D models** pulled from the web ([Poly Pizza]),
  auto-scaled to real-world size and placed on the floor; move / rotate / resize by voice.
- **Create art** — **AI-generated** paintings, posters, and photos hung as framed images;
  **edit them conversationally** in place ("make the dragon breathe fire") and **outpaint** them
  wider. Image *procurement* is decoupled from *placement* — it makes/fetches an image
  (Gemini or OpenAI, picked by capability — e.g. transparency → OpenAI) and then hangs it.
- **Set the scene** — generate a high-res 360° **skybox** that wraps the whole environment, or
  turn any in-world image into the surrounding sky.
- **Talk to more than one AI** — a roster of named LLMs (Claude + Gemini + "Chat"/OpenAI) shares one
  conversation; switch mid-stream ("let me talk to Gemini") or hand off a single turn ("Gemini, make
  a picture of a cat"), and a newly-active LLM picks up the whole conversation seamlessly.
- **Feel real-time** — a brief spoken acknowledgement ("on it") the instant you ask.

Everything is live: edits broadcast over a WebSocket to every connected headset. Models for
speech run locally; only the reasoning/generation models are cloud.

Implemented phases (see [roadmap](./docs/roadmap.md)): **0** world doc + patch protocol + A-Frame
client · **1** world-editing MCP tools · **2** voice loop · **3** assets · **4** image generation +
editing + skybox.

**In progress — Phase 5: room model (AR).** Bring your real room into the world as editable geometry:
see your room, restyle/texture its walls, mount content on real surfaces by semantic label, slide along
a passthrough↔virtual immersion spectrum (or hide the room for full VR), keep models inside the real
bounds, and refine the room mesh progressively. Shipped so far: room capture + stable surface ids, wall
squaring + corner-joining, real door/window cutouts, and upright mounted art. Design:
[docs/room-model.md](./docs/room-model.md).

## How it fits together

```
 voice  ┌───────────┐  MCP   ┌──────────────┐  WebSocket  ┌──────────────┐
 ◀────▶ │  PipeCat  │ ◀────▶ │ World server │ ◀─────────▶ │ A-Frame      │  Quest 3
 (mic/  │  + shell  │        │ (FastAPI):   │  (patches)  │ WebXR client │  (or any
  spkr) │  + agent  │        │ world + MCP  │             │              │   browser)
        └───────────┘        │ + assets/gen │
                             └──────┬───────┘
                                    │  Poly Pizza (models) · Gemini/OpenAI (images) · local Whisper/Kokoro
```

- **World server** (`conjure/`, Python/FastAPI) owns one declarative world document, applies
  **patches**, serves the WebXR app + cached assets, and exposes world-editing **MCP tools**.
- **Shell + agent** — the **shell** (`conjure/shell.py`) is a deterministic command plane (switch
  agent/LLM, status — no LLM); below it the **builder agent** (`conjure/director.py`, loaded
  declaratively from `agents/builder/`) is the brain for both voice and CLI: it owns the shared
  user/assistant transcript, an **LLM roster** (`conjure/llm.py` — Claude/Gemini/OpenAI, switchable mid-conversation),
  the MCP tools, and the live room injected into its prompt. PipeCat is just ears+mouth (Whisper STT →
  shell → agent → Kokoro TTS); the CLI feeds it typed text. New LLMs/agents register declaratively —
  nothing else changes.
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
conjure-agent                           # terminal 2: agent server (holds the director + keys)
python -m conjure.voice                 # terminal 3: voice loop (use earbuds — see note)
```

Voice and CLI are thin clients of the **agent server** — start it first. They share one conversation:
speak on voice and type on `conjure-cli` and it's the same director, same transcript.

Then speak: *"put an oak tree in front of me", "paint a sunset over mountains and hang it on the
wall", "wrap me in a misty forest", "make the painting nighttime".*

### Keys (in `.env` — git-ignored; see `.env.example`)

| Key | For | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | director "Claude" | billing |
| `GOOGLE_API_KEY` | director "Gemini" + the default image generator | Gemini; billing |
| `OPENAI_API_KEY` | director "Chat" + the OpenAI image generator (text/typography, transparency) | billing |
| `XAI_API_KEY` | director "Grok" + the Grok image generator (generate-only) | billing |
| `POLY_PIZZA_API_KEY` | `place_asset` (3D models) | free, no billing |

At least one of `ANTHROPIC_API_KEY` / `GOOGLE_API_KEY` / `OPENAI_API_KEY` / `XAI_API_KEY` is required
for the director (set more to grow the roster; `GOOGLE`/`OPENAI`/`XAI` also enable image generators).
Speech (Whisper STT,
Kokoro TTS) runs **locally** — no keys. Full prerequisites + the doctor:
[docs/setup.md](./docs/setup.md). Provider options: [docs/providers.md](./docs/providers.md).

> **Audio note:** use **earbuds**. On an open room mic+speaker the director's own TTS feeds back
> into the mic; room-speaker support (push-to-talk / echo cancellation) is a tracked follow-up.

## CLI — drive it from the terminal (no voice)

Quiet, fast, discrete testing without the mic. The direct tool commands hit the world server; the
director paths (`say` / interactive REPL) are now **thin clients of the agent server** — start it first:

```bash
python -m conjure                          # terminal 1: world server
conjure-agent                              # terminal 2: agent server (holds the shared director + transcript)

conjure-cli asset "oak tree" --size 7      # direct, deterministic tool commands (→ world server)
conjure-cli image "an oil painting of a red dragon"
conjure-cli skybox "a misty pine forest"
conjure-cli world                          # print the world

conjure-cli say "put a tree in front of me and hang a sunset painting"   # the director (→ agent server)
conjure-cli                                # no args → interactive director REPL

conjure-import ~/Photos/vr --recursive     # import files into the library (images, .glb models, …)
conjure-import beach_SBS.jpg --stereo sbs  # import a side-by-side 3D photo (viewable in-headset per-eye)
```

Quiet by default; add `-v` for tool calls and library logs. (`say`/REPL need the agent server running +
`ANTHROPIC_API_KEY`.) The agent server picks its agent at launch (`conjure-agent --agent outdoor`); switch
live from the REPL with `conjure agent <name>`. *(Voice still hosts its own director in-process — it moves
onto the agent server in a later step.)*

## On the Quest 3

- **USB (quickest):** `adb reverse tcp:8080 tcp:8080`, then open `http://localhost:8080` in the
  Quest browser and enter VR. Full step-by-step: [docs/testing-on-quest.md](./docs/testing-on-quest.md).
- **Wireless (HTTPS):** [docs/https-setup.md](./docs/https-setup.md) — cloudflared (fastest), Caddy
  + Let's Encrypt, or Tailscale. Tip: `./scripts/tunnel.sh` runs cloudflared and lets you open a
  fixed `http://<mac-ip>:8080/tunnel` on the Quest that redirects to the current tunnel URL.

## Layout

```
conjure/    world server (schema · world store · FastAPI app · MCP tools + room resource) · shell ·
            agents (loader + server registry) · builder/LLM roster · voice loop · CLI ·
            assets pipeline · image-gen registry · config · doctor
agents/     bundled agent defs (builder/: agent.json + prompt.md) + servers.json (MCP registry);
            user-authored agents in ~/.config/conjure/agents/ shadow these (docs/user-home-plan.md)
client/     A-Frame WebXR client + live patch applier
examples/   starter world + hand-authored example patches
scripts/    setup.sh, tunnel.sh (cloudflared + /tunnel redirect), send_patch.py,
            send_room.py (synthetic room), mcp_smoke.py, mic_check.py, vad_check.py
tests/      pytest suite — fast/free/deterministic (`pip install -e ".[dev]" && pytest`); a
            pre-push hook runs it automatically. Live API canaries: `pytest -m live`
docs/       vision · spec · architecture · agents · room-model · decisions · providers · roadmap · setup · testing/https guides
```

**Where your data lives.** Runtime state is stored in your user home, not the repo (docs/user-home-plan.md):
worlds, sessions, generated assets, and the asset catalog under `~/.local/share/conjure/` (precious —
back it up); settings + your own agent defs under `~/.config/conjure/`; disposable scratch under
`~/.cache/conjure/`. Override any location via `settings.json` or env, or set `CONJURE_HOME` to
consolidate all three under one directory. An existing in-project `.cache/` is migrated here on first run.

[Poly Pizza]: https://poly.pizza
