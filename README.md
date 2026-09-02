# Conjure

A voice-driven **holodeck**: describe a scene aloud and an LLM builds an interactive WebXR world
you experience on a Meta Quest 3. Speak, and a real-time agent fills the space around you —
placing objects, summoning real 3D models, generating and editing art, and wrapping you in
generated environments.

> **Design docs live in [`docs/`](./docs/).** Start with [vision](./docs/vision.md), then
> [vision](./docs/vision.md), [architecture](./docs/architecture.md), and the [roadmap](./docs/roadmap.md).

## What works today

You stand in a holodeck (black void, 1 m white grid) and talk. Conjure can:

- **Build** — primitives, and real CC-licensed **3D models** pulled from the web ([Poly Pizza]),
  auto-scaled to real-world size and placed on the floor; move / rotate / resize by voice.
- **Create art** — **AI-generated** paintings, posters, and photos hung as framed images;
  **edit them conversationally** in place ("make the dragon breathe fire") and **outpaint** them
  wider. Image *procurement* is decoupled from *placement* — it makes/fetches an image
  (Gemini or OpenAI, picked by capability — e.g. transparency → OpenAI) and then hangs it.
- **Set the scene** — generate a high-res 360° **skybox** that wraps the whole environment, or
  turn any in-world image into the surrounding sky. Then reach out and adjust it: grab the floor and
  drag to turn the sky or scale it around you, or to slide a whole outdoor world into place.
- **Talk to more than one AI** — a roster of named LLMs (Claude + Gemini + "Chat"/OpenAI + Grok) shares
  one conversation; switch mid-stream with a shell command (`llm gemini`, or spoken "conjure talk to
  gemini") and the newly-active LLM picks up the whole conversation seamlessly. Switching is
  deterministic and never inferred from what you said, so "put a shell on the table" stays a request.
- **Feel real-time** — a brief spoken acknowledgement ("on it") the instant you ask.

Everything is live: edits broadcast over a WebSocket to every connected headset. Models for
speech run locally; only the reasoning/generation models are cloud.

Implemented phases (see [roadmap](./docs/roadmap.md)): **0** world doc + patch protocol + A-Frame
client · **1** world-editing MCP tools · **2** voice loop · **3** assets · **4** image generation +
editing + skybox.

**In progress — Phase 5: your real space (AR).** Bring the real world in as editable geometry: see your
space, restyle and texture its walls, mount content on real surfaces by semantic label, slide along a
passthrough↔virtual immersion spectrum (or hide it for full VR), and keep models inside the real bounds.

Shipped: space capture with stable surface ids, corner-joining and wall sealing, real door/window
cutouts, upright mounted art, per-world surface styling over one shared space record, geolocation +
surface-match space selection with an admission gate, presence avatars, and a local-first render that
keeps the capture off the frame budget. Not built: the progressive mesh tier and director-authored
replacement geometry.

Specs: [spaces](./docs/specs/spaces.md) (the record) ·
[spaces-geometry](./docs/specs/spaces-geometry.md) (where a surface is) ·
[worlds-surfaces](./docs/specs/worlds-surfaces.md) (how a world styles it).

## How it fits together

```
 voice  ┌───────────┐  WS   ┌──────────────┐  MCP  ┌──────────────┐  WebSocket  ┌──────────────┐
 ◀────▶ │  PipeCat  │ ◀───▶ │ Agent server │ ◀───▶ │ World server │ ◀─────────▶ │ A-Frame      │  Quest 3
 (mic/  │ ears+mouth│       │ shell+agent  │       │ (FastAPI):   │  (patches)  │ WebXR client │  (or any
  spkr) └───────────┘       │ + transcript │       │ world + MCP  │             │              │   browser)
        ┌───────────┐  WS   │              │       │ + assets/gen │             └──────────────┘
        │    CLI    │ ◀───▶ └──────┬───────┘       └──────┬───────┘
        └───────────┘              └──────────────────────┤ follows the live world/session over /ws
                                    Poly Pizza (models) · Gemini/OpenAI (images) · local Whisper/Kokoro
```

- **World server** (`conjure/server.py`, FastAPI) owns one declarative world document, applies
  **patches**, serves the WebXR app + cached assets, exposes world-editing **MCP tools**, and is the
  single source of truth for what's live (world · session · space). It runs standalone — you can walk
  your world with no AI in the loop.
- **Agent server** (`conjure/agent_server.py`) is the long-lived host of the **shell**
  (`conjure/shell.py` — a deterministic command plane: switch agent/LLM/session, walk the namespace, no
  LLM) and, below it, the active **agent** (`conjure/director.py`, loaded declaratively from
  `agents/<name>/`). It owns the one shared transcript and an **LLM roster** (`conjure/llm.py` —
  Claude/Gemini/OpenAI/Grok, switchable mid-conversation), and it *follows* the world server: when a
  headset walks into a room that belongs to another agent, it re-binds. New LLMs/agents register
  declaratively — nothing else changes. Spec: [docs/specs/agents.md](./docs/specs/agents.md).
- **Voice and CLI are thin clients** — one WebSocket each, sending raw lines and rendering events. No
  state, no keys, no parsing: all command logic is server-side, so the two can't drift.
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
[docs/running.md](./docs/running.md). Provider options: [docs/providers.md](./docs/providers.md).

> **Audio note:** use **earbuds**. On an open room mic+speaker the director's own TTS feeds back
> into the mic; room-speaker support (push-to-talk / echo cancellation) is a tracked follow-up.

## CLI — drive it from the terminal (no voice)

Quiet, fast, discrete testing without the mic. Two separate tools, because there are two separate
things to talk to:

| | Talks to | Puts an LLM in the loop |
|---|---|---|
| `conjure-cli` (`python -m conjure.cli`) | the **agent** server | yes — the director |
| `conjure-ctl` (`python -m conjure.ctl`) | the **world** server | no — plain REST calls |

```bash
python -m conjure                          # terminal 1: world server
conjure-agent                              # terminal 2: agent server (holds the shared director + transcript)

conjure-cli                                # interactive REPL — the usual way in
conjure-cli say "put a tree in front of me and hang a sunset painting"   # one-shot, then exit
conjure-cli --open-shell                   # skip the agent, open straight in the shell

conjure-ctl                                # print the world
conjure-ctl asset "oak tree" --size 7      # deterministic, no LLM, no API spend
conjure-ctl image "an oil painting of a red dragon"
conjure-ctl skybox "a misty pine forest"
conjure-ctl reindex                        # library maintenance

conjure-import ~/Photos/vr --recursive     # import files into the library (images, .glb/.vrm models, …)
conjure-import beach_SBS.jpg --stereo sbs  # import a side-by-side 3D photo (viewable in-headset per-eye)
```

`conjure-ctl` hits the same world-server endpoints the agent reaches through MCP (`ctl asset` →
`POST /place_asset`, exactly as `mcp_server.place_asset` does) — skipping the LLM is the point when
you're debugging placement math or reindexing.

The REPL is a full-screen client — a status bar pinned to the top, the conversation scrolling in the
middle, and the prompt pinned to the bottom under a separator, so incoming lines never disturb what
you're typing:

```
 builder·claude   14/40 turns   55.0k chars   prompt 10.2k (18%) · room 10.6k (19%) · tools 33.5k (61%) · hist 568 (1%)
 daniel: how many entities are in the world?
 builder: Based on the live context, there are 17 placed objects…
 ────────────────────────────────────────────────────────────────────────────────────────────────────
 conjure:daniel.builder.claude> and what colour is the floor▊
```

The status bar shows the active agent·LLM, transcript turns against the `--history-cap` trim size, and
the size of what the last turn actually sent the model, split into the agent's **prompt**, the live
`{context}` **room** injection, the **tool** schemas, and the **hist**ory. It's measured in characters:
each LLM in the roster tokenizes differently, so chars are the one figure that means the same thing
across all four. (Tool schemas are usually the biggest and least visible slice — worth knowing.) While
one of your turns is running an elapsed clock appears at the left. Narrow terminals shorten the bar
before dropping fields.

Persistent history (arrow keys) and full line editing. PgUp/PgDn scrolls the conversation by half a
screen and the pane sticks to the live tail until you scroll away; **End** returns to it, and while
you're scrolled back the status bar shows how much you've missed (`↓ 12 new · End`). That indicator
matters: in the alternate screen most terminals — and tmux — turn one mouse-wheel notch into a PgUp, so
it's easy to detach by accident, and a detached pane otherwise looks exactly like a frozen one.

The conversation is shared and attributed — other users by name, the agent by its agent name
(`builder: …`). Quiet by default; add `-v` for tool calls and library logs. (`conjure-cli` needs the
agent server running + `ANTHROPIC_API_KEY`.)

### The shell

`conjure open shell` drops into a deterministic command plane — parsed, never sent to an LLM (or launch
straight into it with `conjure-cli --open-shell`, which also drops the wake word from the one-shot form:
`conjure-cli --open-shell say "delete ~/spaces/old"`). Two shapes of command: a **noun** acts on
whatever is live, a **path** acts on anything addressable.

```
conjure:daniel.shell ~/agents/builder> where
user: daniel · agent: builder · LLM: Claude · session: Session 1 · world: daniel/animal-house

conjure:daniel.shell ~/agents/builder> dir
~/agents/builder
  sessions/
  assets/
  worlds     → sessions/session-1/worlds

conjure:daniel.shell ~/agents/builder> cd worlds
~/agents/builder/sessions/session-1/worlds

conjure:daniel.shell ~/agents/builder/sessions/session-1/worlds> show animal-house
  entities    24
  by kind     grid×5, image×8, model×6, other×4, plane×1
  space       daniel/space-1
```

Nouns — `agent`, `llm`, `session`, `world` — list when bare and switch when given a name
(`world meadow`, `llm gemini`), with `new`, `rename`, `clear` and `public`/`private` for the live one.
Paths — `dir`, `show`, `cd`, `delete`, `rename` — walk a namespace that mirrors storage, including the
part that isn't obvious: **worlds live per session**, so `…/sessions/<sid>/worlds/<name>` is the real
address and `…/agents/<agent>/worlds` is a shortcut to the active session's.

Every command declares whether it's **voice-safe**: voice gets the modal verbs ("where am I", "go to
the meadow", "new session") and is refused the ones that need a screen. Full table:
[docs/specs/agents.md §6](./docs/specs/agents.md).

The agent server picks its agent at launch (`conjure-agent --agent outdoor`); switch live with
`conjure agent <name>` — a server-side command, since it moves everyone in the shared session. It
routes through the world server, so every client (and the headset) follows the same pointer move.
Unfinished work and known problems: [docs/backlogs/agents.md](./docs/backlogs/agents.md).

## On the Quest 3

- **USB (quickest):** `adb reverse tcp:8080 tcp:8080`, then open `http://localhost:8080` in the
  Quest browser and enter VR. Full step-by-step: [docs/running.md §3](./docs/running.md).
- **Wireless (HTTPS):** [docs/running.md §4](./docs/running.md) — cloudflared (fastest), Caddy
  + Let's Encrypt, or Tailscale. Tip: `./scripts/tunnel.sh` runs cloudflared and lets you open a
  fixed `http://<mac-ip>:8080/tunnel` on the Quest that redirects to the current tunnel URL.

## Layout

```
conjure/    world server (schema · world store · FastAPI app · MCP tools + room resource) · shell ·
            agents (loader + server registry) · builder/LLM roster · voice loop ·
            cli (agent-server REPL) · ctl (direct world commands) ·
            assets pipeline · image-gen registry · config · doctor
agents/     bundled agent defs (builder/: agent.json + prompt.md) + servers.json (MCP registry);
            user-authored agents in ~/.config/conjure/agents/ shadow these (docs/specs/config.md)
client/     A-Frame WebXR client + live patch applier
examples/   starter world + hand-authored example patches
scripts/    setup.sh, tunnel.sh (cloudflared + /tunnel redirect), send_patch.py,
            send_room.py (synthetic room), mcp_smoke.py, mic_check.py, vad_check.py
tests/      pytest suite — fast/free/deterministic (`pip install -e ".[dev]" && pytest`); a
            pre-push hook runs it automatically. Live API canaries: `pytest -m live`
docs/       vision · spec · architecture · decisions · providers · roadmap · setup · testing/https guides
  specs/    per-area LIVING specs — what is built and how it behaves today (agents, dynamics,
            spaces, spaces-geometry, worlds-surfaces, occlusion)
  backlogs/ the matching per-area backlogs — unfinished work, future directions, known problems
  investigations/  debugging campaigns: what was measured, and what was tried and REJECTED
```

**Where your data lives.** Runtime state is stored in your user home, not the repo (docs/specs/config.md):
worlds, sessions, generated assets, and the asset catalog under `~/.local/share/conjure/` (precious —
back it up); settings + your own agent defs under `~/.config/conjure/`; disposable scratch under
`~/.cache/conjure/`. Override any location via `settings.json` or env, or set `CONJURE_HOME` to
consolidate all three under one directory. An existing in-project `.cache/` is migrated here on first run.

[Poly Pizza]: https://poly.pizza
