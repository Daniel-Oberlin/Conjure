# Roadmap (phased)

A path that gets something working in the headset *early*, then deepens. Each phase ends
with something you can actually experience on the Quest.

## Phase 0 — Foundations & decisions
- Settle the high-leverage decisions (decisions.md #1, #2, #3).
- Stand up the repo skeleton: Python backend, WebXR client, shared scene-graph schema.
- Solve **HTTPS-to-headset** so the Quest can load a page from the server. *Milestone:
  a static WebXR scene renders on the Quest 3.*

## Phase 1 — Live editing loop (no voice yet)
- World server holds scene state; WebSocket pushes **patches** to the client.
- MCP server with a few tools (`add_entity`, `set_environment`, `move_entity`).
- Drive it from a text client / script. *Milestone: type a command, see a cube appear in
  the headset live.*

## Phase 2 — Voice
- PipeCat pipeline (STT → director LLM → TTS), director wired to the MCP server.
- Conversational editing with barge-in and progress narration. *Milestone: "add a red
  cube on the table" works by voice.*

## Phase 3 — Assets
- Asset pipeline: resolve from CC sources, convert to glTF, optimize to budget, cache.
- `place_asset` + progressive placeholder→real swap. *Milestone: "put a tree here" pulls a
  real model into the world.*

## Phase 4 — Generation
- Generative image/3D/audio via model APIs, incorporated as assets. *Milestone: "make a
  painting of a dragon and hang it on the wall."*

## Phase 5 — Memory & connections
- Persist worlds + versions; semantic recall ("the beach world"); portals between worlds.
  *Milestone: leave, come back tomorrow, reload it; walk through a door into another world.*

## Phase 6 — Dynamic behavior & mesh generation
- Event model + sandboxed behavior SDK; director authors behaviors. *Milestone: "when I
  clap, launch fireworks."*
- Mesh generation rides on the same sandbox: procedural/parametric geometry first, dedicated
  generator later (decisions.md #10). *Milestone: "make a spiral staircase here."*

## Multi-user (threads through, not a single phase)
- **Phase 1:** make world state server-authoritative & multi-client from the start (broadcast
  patches) — cheap if done now, a rewrite if retrofitted.
- **Phase 2:** addressing gate (wake word + PTT) + audio capture (shared room device with
  diarization, or per-headset mics) so the director only acts on agent-directed speech.
- **Dedicated milestone:** co-location (Quest "Shared Spaces" extension + neutral fallback) +
  presence avatars — *two people in one room see the same campfire in the same physical spot.*
- **Future:** remote voice bridge over Tailscale relay (decisions.md #3 tier 3, #9).

## Modules (extensibility — slot in once the MCP-client-of-many plumbing exists, ~Phase 2+)
- **Module host plumbing:** director connects to N MCP servers; per-module trust/permission
  scoping. *Foundational — unlocks everything below.*
- **NAS photo module:** `search_photos`/`get_photo`, incl. stereoscopic & 360° media placed in
  the world. *Milestone: "find my Iceland stereo photos and hang them on this wall."*
- **Interactive-fiction module:** Z-machine engine; director renders IF state as VR/AR scenes.
  *Milestone: play Zork by voice, with rooms built around you as you explore.*

## AR mode (slots in with the capability layer)
- `immersive-ar` passthrough alongside VR; place content into the real room. *Milestone:
  "put a fish tank on my actual coffee table."*

## Embodiment & vehicles (after the behavior sandbox exists, ~Phase 6+)
- Occupancy model (`occupy`/`exit`, seats) with the avatar as the default motion model.
- Parametric motion-model registry: car, tank, plane, hot-air balloon — with comfort options
  (cockpit reference frame, vignette). Custom models ride the QuickJS sandbox (decisions.md #12,
  #7). *Milestone: "spawn a hot-air balloon, let me climb in, and drift over the valley."*
- **Input abstraction** (lands with vehicles): normalize client + host devices into actions/axes,
  pluggable drivers, voice-configurable bindings. *Milestone: fly the plane with a USB yoke +
  rudder pedals plugged into the Mac.*
- Multi-user occupancy: driver has motion authority; passengers ride along (spec §12).

## Later / optional
- Desktop preview polish, richer comfort/locomotion, performance autotuning, more modules.

> Phases can overlap, but #0's HTTPS-to-headset and the scene-graph schema are blocking
> prerequisites for everything visual. The vendor-neutral baseline + capability layer
> (decisions.md #11) should exist before Quest-specific extensions are added.
