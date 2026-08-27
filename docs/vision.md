# Vision

> **This document is intent, not description.** It says what Conjure is *for* and what it should
> eventually do — most of the detail below is not built. For what exists today see the living specs in
> [`specs/`](./specs/); for the concrete design and how much of it is real, see
> [architecture.md](./architecture.md), which flags every section 🟢 firm · 🟡 shape-firm · 🔴 sketch.
>
> The first half is the pitch. [Capabilities in detail](#capabilities-in-detail) is the same ambition at
> requirements resolution — it was `docs/spec.md` until 2026-08-26, folded in here because both halves
> describe intent and only differed in how much.

## The one-liner

Conjure is a personal holodeck: you *talk* to it, and it *builds* an interactive VR
world around you in real time. You describe a scene; it appears. You ask for changes;
it edits. You give the world rules and behaviors; it makes them happen. It remembers
every world you've made and can weave them together.

## What it feels like to use

1. You put on the Quest 3 and open a Conjure world (a WebXR page) in the headset.
2. You speak: *"Put me on a beach at sunset, with a campfire and gentle waves."*
3. The world assembles — progressively, narrating as it goes ("fetching driftwood,
   lighting the fire...") — placeholders first, refined as assets arrive.
4. You iterate by voice: *"Make the fire bigger. Add a hammock between those two palms.
   When I clap, launch fireworks."*
5. Later: *"Take me back to the mountain cabin I made last week,"* or *"Connect this
   beach to the cabin with a door."*

## Experiences (agents) & personas

Conjure isn't a single experience — it's a **host for many**, each a different way of being in the
headset, with its own toolset and its own world. We call an experience an **agent** — less a
"do-my-tasks" AI agent, more a **mode of play** you step into:

- **Builder** — the foundational agent: conversational world-building and editing (everything in *What
  it feels like to use*, above). Its job is to let you customize worlds for yourself.
- **Immersive DJ** — plays music and builds the room around it: album art, lyrics, fun facts, and an
  environment that suits the track.
- **Dungeon Master** — runs a tabletop-style role-playing game, generating immersive content and
  environment live as the story unfolds.
- **Planetarium operator** — moves the sky view, narrates the night, and shows immersive stellar visuals.

Within an experience there can be **personas** — characters who *participate* rather than operate. A
persona has its own voice (prompt) and brain (LLM) and sees the shared context when it's given a turn,
but it holds **no tools**: it speaks and intends, and the agent acts on its behalf with the tools at
hand. An NPC in the Dungeon Master's game is a persona; the DM gives it a turn, then realizes whatever it
does in the world.

Each agent gets its **own world**, kept separate from the others, and you can **switch between
experiences mid-session** — DJ, then a quick world-build, then back into the campaign — without losing
any of them. The deterministic plumbing that makes switching reliable, and the scoping that gives each
experience exactly the powers it needs, are specified in [specs/agents.md](./specs/agents.md).

## Who it's for

Primarily a **single creator/explorer** (you), experimenting with conversational world-
building. Multi-user/shared presence is a possible future, called out as a decision so
the architecture doesn't foreclose it.

## Goals

- **Voice-first creation.** Natural, conversational building and editing — not menus.
- **Real-time, in-headset feedback.** Changes appear live in the world you're standing in.
- **Persistent, connectable memory.** Worlds are saved, searchable by description, and
  linkable to each other (portals).
- **Dynamic behavior.** Worlds can have rules and reactive logic, authored by voice and
  run live ("when X, do Y").
- **Multimodal & resourceful.** Generate images/audio/3D via other models; **generate meshes**
  when nothing fits; find and reuse free existing content from the web; convert formats as needed.
- **Many AIs in one conversation.** Several LLMs available in a single session, each with a casual
  name you give it ("Gemini", "Chat"); ask to talk to a different one and it switches. The
  conversation remembers **who said what**, so the AIs can react to and build on each other.
- **Image up-res & outpainting.** Enhance images (super-resolution) and **paint beyond their
  edges** to turn an ordinary photo into an immersive skybox or wrap-around panorama.
- **Extensible audio engine.** A plugin-based audio engine — play audio files, **synthesize sound
  programmatically**, spatialize it — extensible with new audio sources and effects.
- **VR *and* AR.** Build immersive VR worlds or place content into your real room via passthrough.
- **Embodiment & vehicles.** Occupy an avatar, or climb into a vehicle — car, tank, plane, hot-air
  balloon — each with its own way of moving and being driven.
- **Bring your own controls.** Pluggable input devices — Quest controllers and hands, Bluetooth
  gamepads, and USB peripherals on the computer (flight yokes, rudder pedals, trackballs) — mapped
  to in-world actions.
- **Co-located multi-user.** Several people in one room, each in their own headset, share a world
  and see the same things in the same physical spots.
- **Extensible by MCP modules.** New powers plug in as modules — a NAS photo library, an
  interactive-fiction engine, web asset sources — without touching the core.
- **Cross-platform & modest hardware.** Server runs on Raspberry Pi, Mac, or Linux.
- **Works on any WebXR device.** Quest-specific features are extensions, never requirements.
- **Comfortable & safe.** Respects scale, comfort, and the play boundary.

## Non-goals (at least initially)

- Not a AAA game engine or a photorealistic renderer. WebXR-browser fidelity.
- Not a hand-modeling tool — we *generate, assemble, and place* content, we don't offer a
  sculpting UI. (We *generate* meshes — §5 — but offer no modeling UI.)
- Not *remote* multiplayer yet. **Co-located, same-room multi-user IS in scope** (§12); the remote
  voice bridge is a kept-open future and §12's transport stays ready for it.
- Not an app-store native app — it's WebXR delivered through the browser.

## Future possibilities (kept open, not excluded by the architecture)

Not v1 commitments — but the architecture is deliberately built so these stay reachable
(see *Forward-compatibility* in [architecture.md](./architecture.md)):

- **AR in your actual home.** Mount **portals on your real walls** and place objects in your real
  rooms **persistently** — still there next session, anchored to the physical space. (Rests on
  persistent real-world anchors as an AR extension, and anchor-relative placement in the world
  model.)
- **Live video on portals.** A portal — or any surface — showing a **live video feed** instead of
  a rendered world, via a surface that can bind to a streaming media source.
- **Remote sessions as modules.** A module that streams a **remote computer's screen** onto a
  surface in the world (and could forward your input back) — an experience module handing back a
  live stream + input channel.
- **Remote multiplayer.** The voice-bridge future from the multi-user design (off-site
  participants joining a shared world).

## Guiding principles

- **Declarative world state is the source of truth.** Everything — LLM, server, headset —
  agrees on one serializable scene representation.
- **The LLM proposes; the system constrains.** Performance budgets, content safety, and
  sandboxing are guardrails the LLM operates within, not afterthoughts.
- **Degrade gracefully.** Slow asset fetches show placeholders; a missing model falls back
  to a primitive; the experience never hard-stalls.
- **Everything is recoverable.** Voice editing is fuzzy, so undo/redo and world versioning
  are first-class.
- **A host for modules.** Conjure's power grows by adding MCP modules, not by growing a
  monolith. Agents client into scoped MCP servers (above a deterministic shell); the core stays small.
- **Vendor-neutral baseline, extensions on top.** It runs on any WebXR device; device-specific
  features light up when present and fall back gracefully when not.

---

# Capabilities in detail

> The same ambition at requirements resolution. `❓ OPEN` marks decisions not yet made — see
> [decisions.md](./decisions.md). **Nothing here is a claim about what exists.** Section numbers are
> stable and referenced from `decisions.md`, `providers.md` and `roadmap.md`.

## 1. System overview

Conjure has six logical subsystems:

1. **Voice agent** — PipeCat pipeline (STT → shell → agent → TTS) with barge-in.
2. **Shell + agents** — a deterministic **shell** (reliable control: switch agent/LLM, reset — no LLM)
   above the active **agent**: an experience (the `builder` is the first) that turns intent into edits.
   An agent is an orchestrating LLM and an **MCP client of its scoped servers** (the world server plus
   pluggable **modules**, §13), loaded from a declarative def (see [specs/agents.md](./specs/agents.md)).
3. **World server** — owns world state, serves the WebXR app, pushes live updates,
   exposes an **MCP server** of world-editing tools (and context resources) to agents.
4. **Asset pipeline** — finds, generates, converts, optimizes, and caches content.
5. **Memory** — persistent store of worlds, assets, connections, and session history.
6. **Modules** — pluggable MCP servers that extend Conjure: content sources (e.g. a NAS photo
   library), interactive engines (e.g. a Z-machine for interactive fiction), generators, and
   device-capability extensions. See §13.

The headset runs a **WebXR client** served by the world server and synced live over a
WebSocket. The client targets **any WebXR device**; Quest-specific features are extensions
layered on a vendor-neutral baseline (§7, decisions.md #11).

This is the intended *shape*. The decomposition as actually built — eleven components, which are real
and which are not — is [architecture.md §2](./architecture.md).

## 2. World model (the core contract)

The **scene graph** is a declarative, serializable description of a world. It is the
single source of truth shared by director, server, and client.

- **Entities** with transforms (position/rotation/scale, in meters), a hierarchy, and
  **components** (mesh/model ref, material, light, audio source, collider, behavior, etc.).
- **Environment**: skybox/HDRI, lighting, fog, time-of-day, ambient audio, gravity.
- **Behaviors**: reactive logic attached to entities or the world (see §6).
- **Metadata**: world id, name, description, tags, created/updated, performance budget,
  asset license records, portal connections.
- ✅ Representation: **A-Frame** declarative ECS — the scene graph *is* A-Frame entities +
  components, which is also the save format. (decisions.md #2)

Requirements:
- Fully serializable to disk and restorable byte-for-byte.
- Diffable — edits are expressed as patches (add/update/remove) for live sync & undo.
- **Goal, not yet true:** versioning — every edit leaves a recoverable snapshot. Patches carry a
  server-computed inverse today, but nothing consumes it; see [architecture.md §5](./architecture.md).

## 3. Voice & conversational interaction

- Real-time STT, low-latency TTS, **barge-in** (interrupt the agent mid-sentence).
- The agent **narrates progress** during slow operations ("fetching a tree...").
- Conversational memory within a session (pronoun/anaphora resolution: "make *it* bigger").
- **Configurable audio capture topology** (decisions.md #5):
  - *Shared room device* (e.g. a Bluetooth speakerphone) — the **expected default** for people
    in one room. One mixed stream with several voices ⇒ needs **speaker diarization** to tell
    who's who, and an addressing gate to pull out agent-directed speech.
  - *Per-headset mic* — also supported; gives a clean per-speaker stream tagged with identity
    (no diarization needed). The architecture must allow either or a mix.
- **Presence-aware activation** (decisions.md #5): the addressing requirement scales with who's
  in the room.
  - *Solo mode* — when the user indicates they're the only one present, the **wake word is
    optional**: open-mic / always-listening conversation is allowed (nothing to disambiguate).
  - *Shared mode* — with others present, **wake word ("Conjure, …") + push-to-talk** is required;
    open-mic is ruled out (room cross-talk). This is the **safe default** until the user declares
    solo (by voice or a toggle).
- **Addressing** (multi-user, §12): in shared mode the director must distinguish "someone is
  talking to Conjure" from "people talking to each other." The addressing gate (wake word + PTT,
  with an intent-classifier backstop) decides what reaches the director; diarization/stream
  identity tells it *who* spoke. (decisions.md #5, #9)
- ✅ STT/LLM/TTS are **cloud-first behind a provider abstraction**, swappable for local or a
  self-hosted home endpoint. Pi stays viable as a thin orchestrator. (decisions.md #1)
- **Multiple LLMs in one session (the agent's roster).** Several LLMs are available at once, each
  with a **user-given casual name** ("Gemini", "Chat", …). The user **switches** ("let me talk to
  Gemini") — a deterministic **shell** command ([specs/agents.md](./specs/agents.md) §6) — and the named LLM
  becomes the active brain running the agent. (Built on the provider abstraction, #1; the casual name
  doubles as an addressing target — see #5.)
  - **Attributed shared transcript.** One conversation log where every turn is tagged with its
    source. *(Built differently: **user** turns carry their human speaker, but assistant turns carry
    no LLM attribution — a switch of LLMs is deliberately invisible in the context. See
    [architecture.md §7a](./architecture.md).)*
  - Design notes: a **roster** maps names → provider/model configs (user-editable); one LLM is
    active at a time; the voice agent routes the active stream to it.

## 4. World creation & editing (director capabilities)

Exposed as MCP tools the director calls, kept **coarse and intent-level**
(`place_asset("campfire", near=user)`) so the agent reasons about goals, not transforms.

*The tool list that used to live here was a wish-list that drifted badly — it named 21 tools that
don't exist and missed 26 that do. The real surface is defined in exactly one place,*
`agents/builder/agent.json` *(a test asserts it equals every* `@mcp.tool` *in* `mcp_server.py`*), and
is described in* [specs/agents.md §3](./specs/agents.md)*. Groups are sketched in*
[architecture.md §8](./architecture.md).

Requirements:
- **Progressive construction**: place a placeholder immediately, swap in the real asset
  when ready. Never block the whole build on one slow fetch.
- **Edits are patches** applied to the live world and streamed to the headset.
- Director can **read state back** to make context-aware edits.

## 5. Asset pipeline

Find / generate / fetch / convert / optimize / cache content.

- **3D models**: glTF/GLB is the WebXR-native target. Convert OBJ/FBX/USD/STL via Blender
  (headless) / assimp / gltf-transform.
- **Optimization for Quest**: Draco/meshopt compression, texture downscaling, LOD,
  draw-call awareness. Enforce a **per-world performance budget** (§8).
- **Sources of free/CC content** (track license + attribution for *every* asset):
  Poly Pizza, Quaternius, Kenney, Sketchfab (CC filter), Objaverse, Smithsonian 3D,
  Poly Haven (HDRIs/textures/models). Only Poly Pizza is wired
  ([`specs/library.md §3`](./specs/library.md)).
- **Generative**: text/image → image (e.g. via the director calling image models),
  text/image → 3D (Meshy, Luma, Tripo, Hunyuan3D, Trellis, etc.), text → audio/SFX/music.
- **Image enhancement & extrapolation (plugin operations)** — post-process images via pluggable
  model operations behind the provider abstraction (#1) / as modules (§13):
  - *Up-res / super-resolution* — sharpen and enlarge a low-res image or texture.
  - *Outpainting / extrapolation* — paint **beyond an image's edges** to extend it; especially to
    turn an ordinary photo into an immersive **skybox**, **360°/equirectangular**, or **cylindrical
    panorama** that wraps the user. Pairs with the immersive media types below and seam-aware
    handling for wrap-around continuity.
- **Mesh generation (when nothing fetched/generated fits)** — produce geometry on demand.
  Three complementary modes (decisions.md #10):
  - *Procedural / parametric*: the director (or a behavior) emits code that builds geometry
    (e.g. Three.js `BufferGeometry`, or server-side Python via `trimesh`/`numpy` → glTF). Great
    for regular/parametric shapes; runs in the **sandbox** (§6, decisions.md #7).
  - *Dedicated text/image→3D agent or model*: a specially-prompted or fine-tuned generator for
    organic/complex meshes, behind the provider abstraction.
  - *Primitive composition*: assemble from A-Frame primitives as an always-available fallback.
- **Immersive media types** (first-class, not just flat textures): **stereoscopic photos/video**
  (side-by-side / VR180 — rendered per-eye for true depth), **360° / equirectangular** panoramas
  and video, and ordinary 2D images. Sourceable from modules (e.g. a NAS photo library, §13).
  *Forward-compat:* surfaces bind to a media **source**, which may later be a **live stream**
  (WebRTC/video/canvas) — keeps live-video-on-portals and remote-screen surfaces open. Not a v1
  feature.
- **Caching & dedup**: content-addressed asset store; reuse across worlds.
- ❓ OPEN: which sources/providers for v1, and self-host vs API for generation. (decisions.md #4)
- ❓ OPEN: mesh-generation strategy & where geometry code runs. (decisions.md #10)

## 6. Dynamic behavior & code modules

Worlds can have reactive logic ("when I clap, launch fireworks"; "the sun sets over 5 min").

- **Event model**: timers, proximity, gaze, controller/hand input, collisions, voice
  triggers, world lifecycle.
- **Behavior SDK**: one **JS/TS** capability API behaviors are written against — subscribe to
  events, read provided state, **emit world patches**, set timers, request assets, play audio.
  It is the *only* surface behaviors can touch. Mesh generators (§5) use the same SDK.
- **Sandbox** (decisions.md #7): behaviors run in **QuickJS-WASM** — the *same engine on the
  server and in the Quest browser*, so behaviors are **portable** and isolation is identical
  everywhere. The sandbox has **no ambient authority** (no filesystem/network/process/keys/DB on
  the server; no `window`/`document`/`fetch`/WebSocket/other-entity access in the browser) and
  runs under hard CPU/memory/timeout limits. Behaviors **declare the capabilities they need**;
  the host grants least privilege and surfaces sensitive ones.
- **Every effect is a validated intent.** Behaviors don't mutate the world directly — they emit
  patches the trusted world server re-validates (perf budget, schema, permissions) before
  applying. A breached sandbox can still only produce validated world-edits.
- **Placement** (decisions.md #6): because the runtime is portable, placement is a **per-behavior
  tag** — *client-side* for low-latency interaction (grab, gaze, immediate reactions),
  *server-side* for authoritative/shared/persistent logic and anything touching memory. Default
  split TBD; some SDK calls (memory/persistence) only resolve server-side.

*What ships today is a different, narrower thing — curated first-party **dynamic modules**
([specs/dynamics.md](./specs/dynamics.md)), trusted code with no sandbox. The QuickJS runtime above is
for **generated** behaviour and is unbuilt; the boundary is drawn in
[architecture.md §9](./architecture.md).*

## 7. XR interaction, modes & comfort

- **Session modes**: **VR** (`immersive-vr`) *and* **AR** (`immersive-ar`, passthrough) on
  request — a world can be placed into the user's real room. Plus a **flat** (non-immersive)
  fallback for any browser (§10 desktop preview).
  *Forward-compat:* the transform model allows **anchor-relative placement**, keeping open
  **persistent AR** — portals/objects fixed to real walls/rooms across sessions — as a later
  device extension (§13, decisions.md #11). Not a v1 feature.
- **Room-scale 6DoF**: the user physically **walks around and views things from different
  perspectives** (real positional tracking + parallax), not just teleport. Teleport/smooth
  locomotion is offered for travel beyond the physical room.
- **Embodiment by occupancy** (unifying model): the user always **occupies** an *occupiable
  entity* that binds their tracking origin + input + viewpoint and supplies a **motion model**
  and **control scheme**. The default is an **avatar** (motion model = room-scale walk +
  teleport). `occupy` / `exit` switches embodiment.
  - **Vehicles** are occupiable entities with richer motion models and controls: **car**
    (wheeled/Ackermann steering), **tank** (tracked + turret), **plane** (fixed-wing throttle /
    pitch / roll / yaw; or rotary), **hot-air balloon** (vertical buoyancy + wind drift, limited
    control), and others (boat, etc.). Motion models live in a **registry**; a *custom* vehicle's
    motion/control logic is just LLM-authored code in the **QuickJS sandbox** (§6, decisions.md
    #7) moving its entity through the validated-patch boundary.
  - **Seats / multi-occupant**: vehicles expose occupancy slots (driver + passengers); occupancy
    is shared world state in multi-user (§12) — one driver has motion authority, passengers ride.
  - **Control mapping**: control schemes bind the **abstract control axes** (throttle, steer,
    pitch/yaw/roll, brake, lift) — fed by the **input abstraction** (below) — so a vehicle can be
    flown with thumbsticks, a real yoke + pedals, or voice ("take off", "full speed"). Supports
    **diegetic** controls (grab a virtual yoke / wheel / throttle) and **abstract** controls;
    choice per vehicle.
  - **Vehicle comfort is first-class** (VR vection is the top sickness risk): cockpit/reference
    frame that moves with the user, optional vignette/tunneling, snap vs smooth turning, stable
    horizon. Comfort options must exist for every motion model.
  - ❓ OPEN: full physics engine vs parametric/arcade motion models (or hybrid). (decisions.md #12)
- **Input abstraction** (pluggable; spans client *and* host): Conjure consumes input as
  normalized **actions** (discrete) and **axes** (continuous) — including the vehicle control axes
  — never raw devices. Control schemes and behaviors bind to these abstractions.
  - **Sources in two locations, merged into one logical input state:**
    - *Client (Quest browser)* — WebXR controllers & hand-tracking; **Gamepad API** devices
      including **Bluetooth controllers paired to the Quest**; WebHID/WebUSB where available.
    - *Host (computer/server)* — **USB or Bluetooth peripherals on the Mac/Linux/Pi**: flight
      **yokes, rudder pedals, throttle quadrants, joysticks, trackballs**, gamepads — read via OS
      input (SDL/evdev/hidapi) and streamed into the session.
  - **Pluggable drivers/providers**: each device family has a driver, built-in or **module-provided
    (§13)**; a provider declares the devices/axes it offers (capability manifest). New peripherals
    plug in without core changes.
  - **Binding & profiles**: a mapping layer binds raw inputs → abstract actions/axes, with
    per-device profiles and per-control-scheme bindings (yoke → pitch/roll, pedals → rudder +
    brakes). User- and **voice-configurable** ("map the left pedal to the rudder").
  - **Hotplug & capability**: devices connect/disconnect mid-session; schemes adapt to what's
    present and degrade gracefully (capability-tier, #11). Gaze and voice remain always-available
    inputs.
  - **Latency/placement**: host-attached devices add a network hop; for tight control loops (a
    yoke flying a plane) the relevant motion model may run server-side near the input — ties to
    behavior placement (#6) and vehicle motion authority (§12, #12).
  - *The client half of this is built* — `ConjurePointers`, for XR controllers and hands
    ([architecture.md §11b](./architecture.md)). Host devices, drivers and hotplug are designed only.
- **Extensible audio engine** (plugin architecture): a first-class audio subsystem, not just
  attached sound files. Spatialized positional sources + ambient beds, driven by the director and
  by behaviors (`world.audio`, §6). Pluggable **audio sources/effects** (§13):
  - *File playback* — play/stream audio assets (positional or ambient).
  - *Programmatic / procedural audio* — synthesize sound at runtime (e.g. Web Audio oscillators /
    `AudioWorklet` in the browser; generators server-side), for synths, tones, generative
    soundscapes/music.
  - *Generated audio* — TTS, SFX/music from models (§5), and streamed sources.
  - New source and effect types plug in as modules without touching the core.
- **Comfort & safety**: respect real-world **scale (meters)**, the guardian/play boundary, and
  motion-sickness comfort; don't spawn objects where the user will walk into a wall.
- **Vendor-neutral baseline + capability extensions** (decisions.md #11): the experience must
  work on any WebXR device. Device-specific features (Quest "Shared Spaces" co-location, certain
  hand-tracking specifics, passthrough particulars) are **extensions** that *enhance* but never
  *gate* core functionality. Each extension declares a neutral fallback.
- **WebXR requires a secure context (HTTPS).** Resolved as a 3-tier story: adb-reverse (dev) →
  Caddy + Let's Encrypt DNS-01 (multi-headset LAN) → Tailscale (remote). (decisions.md #3)
- **Co-location** (multi-user, §12): same-room headsets share a common physical origin.
  *Resolved differently than planned:* there is **no** platform shared-anchor dependency — a guest
  registers its own detected planes against the space's stored constellation, and content is placed
  by plane-relative anchors ([specs/spaces-geometry.md](./specs/spaces-geometry.md)).

## 8. Performance budget

The goal is that the director builds within an explicit budget so worlds stay comfortable on Quest 3:
target frame rate (72/90/120 Hz), max triangles, max draw calls, texture-memory ceiling. The pipeline
downgrades/optimizes assets to fit; the director is told the budget and remaining headroom.

**Not enforced today.** The `budget` block exists in the world document and the validation gate that
would check it is a stub — see [architecture.md §5](./architecture.md).

## 9. Memory & persistence

- **World store**: serialized scene graphs + version history.
- **Space store**: the persistent record of a real physical environment, user-owned and shared across
  that user's worlds ([specs/spaces.md](./specs/spaces.md)).
- **Asset store**: content-addressed, deduped, with license metadata.
- **Semantic recall**: find worlds by description ("the beach at sunset") — a vector index over world
  metadata. *(The vector index exists but covers **assets**, not worlds.)*
- **Connection graph**: portals/links between worlds.
- **Session/conversation history**: for iterative editing and provenance ("why is this here").

## 10. Platform, deployment & ops

- **Server** runs on Raspberry Pi / Mac / Linux. Heavy compute (LLM, gen models) is remote
  if the host is a Pi; a Mac host can do more locally. (decisions.md #1)
- **Headset**: Meta Quest 3, content via the Quest browser (WebXR).
- **Desktop preview**: a flat/emulated 3D view in a normal browser for fast iteration
  *without* donning the headset — important for dev velocity. (decisions.md #8)
- **Config & secrets**: many service API keys; centralized config.
- **Observability**: log director actions and world-state diffs; ideally replay a build.

## 11. Safety, licensing & guardrails (cross-cutting)

- Sandboxed execution of LLM-authored code (§6).
- Content moderation for generated images/assets.
- License capture + attribution for every web/CC asset.
- Performance guardrails (§8) and VR comfort guardrails (§7).

*Where the line sits today — two of these enforced, the rest intent — is
[architecture.md §13](./architecture.md).*

## 12. Multi-user (co-located now, remote later)

Conjure supports **multiple people in the same room, each with their own headset**, sharing one
world. A future **remote voice bridge** extends this to off-site participants. (decisions.md #9)

- **Shared world state, server-authoritative.** One source of truth; world patches broadcast to
  all connected headsets over the reliable channel. No client owns the world.
- **Presence sync.** Per-user head/hand poses as transient state at high rate over a separate,
  lossy-tolerant channel; rendered as networked avatars. Never persisted into world state.
- **Occupancy & vehicle motion (§7).** Who occupies which seat is shared world state; vehicle
  kinematic pose flows on the high-rate channel (like presence). The **driver/occupant with
  motion authority** drives the model; passengers and other users receive the synced pose. Server
  remains authoritative for occupancy transitions and collisions.
- **Audio capture (§3).** Default is a **shared room device** (Bluetooth speakerphone): one
  mixed stream → diarization + addressing gate. Per-headset mics are equally supported (clean
  per-speaker streams). Topology is configurable; the architecture assumes neither.
- **Addressing the director (§3).** Wake word + PTT route only agent-directed speech to the
  director; diarization/stream identity tell it *who* spoke. Room cross-talk is ignored.
- **Co-location alignment.** *Resolved without a shared anchor* — see §7 and
  [specs/spaces.md](./specs/spaces.md). Content lands on the same *physical* wall for everyone while
  the coordinate numbers differ by centimetres.
- **Remote-ready transport.** World + presence go through a server relay (uniform LAN/WAN) so
  the remote voice bridge slots in over Tailscale/tunnel (§3 tier 3) without re-architecting.
  Presence MAY use P2P (PeerJS) on LAN, but world authority stays server-side.

## 13. Extensibility & modules

Conjure is, at heart, a **host for pluggable MCP modules**. The agent is an MCP client of
its scoped servers; the world server is just the first. Adding a capability = adding an MCP module —
no core changes. Module taxonomy:

- **World-editing** — the core world server (§4). Always present.
- **Content-source modules** — surface assets/media the director can place. Local or remote.
  - *Example:* a **NAS photo library** module exposing `search_photos` / `get_photo`, including
    **stereoscopic (VR180/side-by-side) and 360° photos** placed as immersive media (§5).
  - *Example:* CC web asset libraries; generative-model wrappers.
- **Processing modules** — transform existing content rather than source it: **image enhancement
  (up-res)** and **outpainting/extrapolation** (photo → skybox/panorama), format conversion, mesh
  optimization (§5).
- **Audio modules** — audio sources and effects for the extensible audio engine (§7): file
  players, **programmatic/procedural synths**, generated-audio wrappers, streamed sources.
- **Experience / engine modules** — external interactive systems the director *mediates* into
  the world.
  - *Example:* an **Infocom Z-machine** module (`new_game`, `send_command`, `get_state`). The
    user plays interactive fiction by voice; the director reads the IF state and **renders each
    room, object, and described scene as VR/AR content** — generating images/meshes and building
    the space as the story moves. Generalizes to any text/state-driven engine.
  - *Forward-compat example:* a **remote-session module** streaming a remote computer's screen
    onto a surface (and forwarding input).
- **Capability / device-extension modules** — expose device-specific features behind the
  capability-tier model (§7, decisions.md #11), each with a neutral fallback.

Requirements:
- **Discovery & hot-add**: modules can be registered without restarting a session where
  feasible; the director is told what tools each provides.
- **Trust & scoping**: modules are MCP servers with their own permissions; a content/engine
  module is *not* automatically granted world-write or code-execution rights. Ties into
  sandboxing (§6, decisions.md #7).
- **Neutral interfaces**: module tool schemas stay engine-agnostic where practical so the
  WebXR/world layer doesn't hard-depend on any one module.
- **Forward-compat — streaming & bidirectional**: the module contract should be able to carry
  **stream/handshake metadata** (e.g. a WebRTC offer / URL the client connects to directly) and
  an **input-forwarding path**, beyond request/response tool calls. Design hook only.

> **Two different things are called "module."** This section is the **MCP module** — a *server* that
> extends an agent's **tool surface**. A **dynamic module** ([specs/dynamics.md](./specs/dynamics.md))
> is a *client-side A-Frame component* an agent conjures into a world — it extends the **scene**. They
> share no machinery. See [architecture.md §11a](./architecture.md).

## Appendix: open questions index

See [decisions.md](./decisions.md) for the full list.

- ✅ Resolved: cloud-vs-local compute (#1), world representation (#2), HTTPS-to-headset (#3),
  single-vs-multi-user (#9), capability tiers / Quest-as-extension (#11),
  **code sandboxing (#7 — capability API + unified JS in QuickJS-WASM)**.
- 🔶 Constrained: voice activation + audio capture (#5) — wake-word + PTT; shared-room device
  (diarization) or per-headset mics. Behavior placement (#6) — portable runtime; default split TBD.
- ❓ Still open: asset sources/providers (#4), desktop preview (#8),
  mesh-generation strategy (#10), vehicle motion: physics vs parametric (#12).
