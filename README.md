# Conjure

A voice-driven **holodeck**: describe a scene aloud and an LLM builds an interactive WebXR
world you experience on a Meta Quest 3 — with persistent memory, dynamic behavior, and the
ability to pull in generated or pre-existing content.

> **Design docs live in [`docs/`](./docs/).** Start with [docs/vision.md](./docs/vision.md),
> then [docs/spec.md](./docs/spec.md) and [docs/architecture.md](./docs/architecture.md).

## Status — Phase 0 scaffold

This repo currently implements the bare **state loop** from the architecture: a Python world
server that holds a declarative world document, a WebSocket **state channel**, and an A-Frame
client that renders the world and applies **patches** live. The director, voice agent, MCP
modules, and behavior sandbox are not wired up yet.

What works today: serve a scene, open it in a browser **or** the Quest, and mutate it live by
POSTing hand-authored patches.

## Quickstart (desktop)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                       # base: world server + MCP (no voice)
python -m conjure                      # serves on http://localhost:8080
```

Open <http://localhost:8080> in a browser — you'll see a green ground plane and a pillar.
In another terminal, drive it live:

```bash
python scripts/send_patch.py examples/patches/add_cube.json        # a tomato cube appears
python scripts/send_patch.py examples/patches/recolor_pillar.json  # pillar turns blue & rises
```

`GET /world` returns the current world document.

## Drive it with the MCP server (Phase 1)

The world-editing **MCP server** exposes the director's tools (`query_world`, `add_entity`,
`move_entity`, `update_entity`, `remove_entity`, `set_environment`). It translates tool calls
into patches and POSTs them to the running world server, so edits broadcast live to every
headset. This is the surface PipeCat's `MCPClient` will connect to in Phase 2.

With the world server running, exercise it over stdio (as an MCP client would):

```bash
python scripts/mcp_smoke.py
```

To wire it into an MCP client / agent, run it as a stdio server: `python -m conjure.mcp_server`
(or the `conjure-mcp` console script), with `CONJURE_URL` pointing at the world server.

## Voice (Phase 2)

Talk to the world. The loop is **mic → Whisper (STT) → Claude director w/ MCP tools → Kokoro (TTS)
→ speaker**, all local except the cloud director, behind a provider abstraction. Audio runs on the
host — nothing is piped through the Quest.

Setup (one script + one key):

```bash
./scripts/setup.sh                     # system deps + venv + voice extras + .env
# add ANTHROPIC_API_KEY to .env, then:
python -m conjure.doctor               # confirm prerequisites
```

Run it (two terminals):

```bash
python -m conjure                      # 1) world server (also serving the Quest)
python -m conjure.voice                # 2) voice loop  (or the `conjure-voice` script)
```

Then speak, e.g. *"add a red cube in front of me"* — the director calls the world tools and the
change appears live in the headset. Prerequisites & the doctor: [docs/setup.md](./docs/setup.md);
provider options: [docs/providers.md](./docs/providers.md).

## Run it on the Quest 3 (Phase-0 path: `adb reverse`)

WebXR needs a secure context, but the browser treats `localhost` as secure — so we forward the
Quest's `localhost` to this machine over USB (decision #3, tier 1; no TLS needed yet):

```bash
adb reverse tcp:8080 tcp:8080
```

Then open <http://localhost:8080> in the **Quest browser** and enter VR. Multi-headset /
LAN serving moves to Caddy + TLS later (decision #3, tiers 2–3).

**Full step-by-step (adb install, Developer Mode, USB authorize, live-edit test):**
see [docs/testing-on-quest.md](./docs/testing-on-quest.md).

## Layout

```
conjure/        Python server: schema (world + patch), world store, FastAPI app
client/         A-Frame WebXR client + patch applier
examples/       sample_world.json and hand-authored patches
scripts/        send_patch.py helper
docs/           vision, spec, architecture, roadmap, decisions
```
