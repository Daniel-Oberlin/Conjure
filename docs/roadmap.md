# Roadmap (phased)

A path that gets something working in the headset *early*, then deepens. Each phase ends
with something you can actually experience on the Quest.

## Phase 0 — Foundations & decisions ✅ DONE
- Settle the high-leverage decisions (decisions.md #1, #2, #3).
- Stand up the repo skeleton: Python backend, WebXR client, shared scene-graph schema.
- Solve **HTTPS-to-headset** so the Quest can load a page from the server. *Milestone:
  a static WebXR scene renders on the Quest 3.* — **achieved** (adb-reverse path,
  [testing-on-quest.md](./testing-on-quest.md)).

## Phase 1 — Live editing loop (no voice yet) ✅ DONE
- World server holds scene state; WebSocket pushes **patches** to the client.
- MCP server with a few tools (`add_entity`, `set_environment`, `move_entity`).
- Drive it from a text client / script. *Milestone: type a command, see a cube appear in
  the headset live.* — **achieved** (via `send_patch` and the MCP server, confirmed in-headset).

## Phase 2 — Voice ✅ DONE
- PipeCat pipeline (STT → director → TTS), director wired to the MCP server.
- Conversational editing. *Milestone: "add a red cube in front of me" works by voice* —
  **achieved** (local Whisper + Kokoro + cloud Claude; verified building/stacking shapes by voice).
- **Shared director + LLM roster ✅** (`conjure/director.py`, `conjure/llm.py`): voice and CLI drive
  one director; an LLM roster (Claude + Gemini) is switchable mid-conversation ("let me talk to
  Gemini") or addressable per-turn ("Gemini, …"), over an attributed transcript (arch §7a). New
  providers register in one place — no caller changes. (Verified end-to-end via CLI.)
- **Audio-polish follow-ups (open):** (1) room-speaker support without earbuds — acoustic echo
  cancellation or **push-to-talk** (ties to #5), since an open mic+speaker feeds the bot's TTS
  back to itself; today's loop is avoided with earbuds + a terse director prompt. (2) modernize
  `PipelineTask`/`PipelineRunner` → `PipelineWorker`/`WorkerRunner` (pipecat 1.3 deprecations).

## Phase 3 — Assets ✅ DONE (polish pending)
- Asset pipeline: **Poly Pizza** (decision #4) search → download → content-addressed cache →
  served at `/assets/<hash>.glb`; license + attribution captured per asset.
- `place_asset` MCP tool with progressive placeholder→model swap. *Milestone: "put a tree in
  front of me" pulls a real model into the world* — **achieved** (tree/chair/dog by voice,
  movable/rotatable/scalable).
- **Polish follow-ups (open):** (1) **auto-normalize asset scale + ground placement** (read the
  GLB bounding box, fit to a target size, sit on the floor) — models currently load at native
  scale and float; (2) tri-budget filtering when picking a result; (3) more CC sources as modules.

## Phase 4 — Generation 🔶 IMAGES DONE
- **Image generation** via a capability-aware generator registry in the provider abstraction
  (`conjure/llm.py`, decisions #1, #13). **Procurement is decoupled from scene use** (✅): MCP
  `generate_image`/etc. return an **image id**; `place_image`/`set_skybox` take an id. Generators —
  **Gemini** (default) + **OpenAI** (`gpt-image-1`; transparency) — declare `ImageCapabilities`; the
  world server mediates selection (best default per op, transparency→OpenAI, explicit override).
  *Milestone: "paint a dragon and hang it on the wall"* — **achieved**.
- **Skybox** via `set_skybox` — generate a 360° panorama (Gemini, 21:9) and wrap the scene in it
  (`<a-sky>`). *Working* (acceptable first pass; low-res/blocky + one seam, since a general image
  model isn't true seamless equirectangular).
- **Conversational image editing** via the generator `edit()` path (`edit_image` — "make the
  dragon breathe fire"); **outpainting** (`outpaint_image` — extend a picture wider;
  `skybox_from_image` — turn an in-world image into the 360° sky). *Working*.
- **Higher-res skyboxes** — `set_skybox`/`skybox_from_image` use **Nano Banana Pro @ 4K**
  (6336×2688), much sharper than the flash default. *Working*.
- **Open follow-ups:** (1) **true-equirectangular / seamless** skyboxes — a dedicated 360 model
  (Blockade Labs) as a registry plugin (the wrap-seam/pole distortion remains with general models);
  (2) editing the skybox itself; (3) non-square aspect ratios for `place_image`; (4) **text→3D
  generation** (Meshy/Tripo/…) as another `place_*` path; (5) generated audio.

## Phase 5 — Room model (AR / scene understanding) ⬅ NEXT
- Bring the **real room** out of the Quest into the world model via WebXR (`plane-detection`,
  `mesh-detection`, semantic labels, passthrough, anchors), as **first-class editable geometry**.
  Captured surfaces (`wall`/`floor`/`ceiling`/`table`/…) become **stylable entities** the director can
  **show/hide, recolor, and texture** (e.g. "make the ceiling a galaxy"), available for **display**
  (text labels) and **interaction** (mount images/objects, anchored). A full **immersion spectrum**
  from two axes (passthrough × surface-visibility): **virtual room** ↔ **AR** ↔ **mixed** ↔ **hide the
  room for the original unbounded VR**. The director is **room-aware** — new models land **inside the
  boundary** — and can **author its own room** fit to the real footprint ("turn my room into a
  cathedral"). **Progressive mesh refinement** runs in the **background on request**; the refined mesh
  is edited the **same way** (by semantic surface) as the coarse boundaries. Adds the
  **client→server reverse channel** (the WS is server→client only today). With multiple headsets,
  **one is the room authority** (others share it). Full design: **[room-model.md](./room-model.md)**.
  *Milestone: "hang that dragon on my real wall," "make my walls glass and the ceiling a galaxy,"
  "drop into full VR."*
- Realizes the VR+AR / passthrough / anchor-relative threads in spec §3 + vision; a textbook
  capability-tier extension (decision #11) — Quest gets room-aware while other devices fall back to
  the synthetic holodeck. Multi-headset room sharing depends on **co-location** (spec §12) and
  **persistent anchoring** (worlds fixed to a physical room across sessions) is designed-for here and
  built out with Phase 6 memory.

## Phase 6 — Memory & connections
- Persist worlds + versions; semantic recall ("the beach world"); portals between worlds.
  *Milestone: leave, come back tomorrow, reload it; walk through a door into another world.*

## Phase 7 — Dynamic behavior & mesh generation
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

## Embodiment & vehicles (after the behavior sandbox exists, ~Phase 7+)
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
