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
pip install -e .
python -m conjure                      # serves on http://localhost:8080
```

Open <http://localhost:8080> in a browser — you'll see a green ground plane and a pillar.
In another terminal, drive it live:

```bash
python scripts/send_patch.py examples/patches/add_cube.json        # a tomato cube appears
python scripts/send_patch.py examples/patches/recolor_pillar.json  # pillar turns blue & rises
```

`GET /world` returns the current world document.

## Run it on the Quest 3 (Phase-0 path: `adb reverse`)

WebXR needs a secure context, but the browser treats `localhost` as secure — so we forward the
Quest's `localhost` to this machine over USB (decision #3, tier 1; no TLS needed yet):

```bash
adb reverse tcp:8080 tcp:8080
```

Then open <http://localhost:8080> in the **Quest browser** and enter VR. Multi-headset /
LAN serving moves to Caddy + TLS later (decision #3, tiers 2–3).

## Layout

```
conjure/        Python server: schema (world + patch), world store, FastAPI app
client/         A-Frame WebXR client + patch applier
examples/       sample_world.json and hand-authored patches
scripts/        send_patch.py helper
docs/           vision, spec, architecture, roadmap, decisions
```
