# Functional Spec (draft)

> Status: **living draft.** `❓ OPEN` marks decisions we haven't made yet — see
> [decisions.md](./decisions.md). This spec describes *what* Conjure does; the *how*
> lives in [architecture.md](./architecture.md).

## 1. System overview

Conjure has six logical subsystems:

1. **Voice agent** — PipeCat pipeline (STT → director LLM → TTS) with barge-in.
2. **Director** — the orchestrating LLM that turns intent into edits. It is an **MCP client of
   many servers** (PipeCat's `MCPClient` supports this): the world server plus any number of
   pluggable **modules** (§13).
3. **World server** — owns world state, serves the WebXR app, pushes live updates,
   exposes an **MCP server** of world-editing tools to the director.
4. **Asset pipeline** — finds, generates, converts, optimizes, and caches content.
5. **Memory** — persistent store of worlds, assets, connections, and session history.
6. **Modules** — pluggable MCP servers that extend Conjure: content sources (e.g. a NAS photo
   library), interactive engines (e.g. a Z-machine for interactive fiction), generators, and
   device-capability extensions. See §13.

The headset runs a **WebXR client** served by the world server and synced live over a
WebSocket. The client targets **any WebXR device**; Quest-specific features are extensions
layered on a vendor-neutral baseline (§7, decisions.md #11).

```
            ┌──────────── MCP ───────────┐
            │              │             │
 voice  ┌───────────┐  ┌───────┴──────┐  ┌─┴──────────┐  WebSocket  ┌──────────────┐
 ◀────▶ │  PipeCat  │  │ World Server │  │  Modules   │ ◀─────────▶ │ WebXR client │  any
        │ + Director│──│  + MCP + DB  │  │ (NAS, IF,  │   (state)   │ (VR / AR /   │  WebXR
        └───────────┘  └──────┬───────┘  │  gen, ...) │             │  flat)       │  device
                              │          └────────────┘             └──────────────┘
                              │ asset pipeline ▶ web / gen-model APIs / module content
```

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
- Versioned — every edit creates a recoverable snapshot.

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
- **Addressing** (multi-user, §12): the director must distinguish "someone is talking to
  Conjure" from "people in the room talking to each other." An addressing gate — **wake word
  ("Conjure, …") + push-to-talk**, with an intent-classifier backstop — decides what reaches the
  director; diarization/stream identity tells it *who* spoke. (decisions.md #5, #9)
- 🔶 Open-mic-VAD-as-input is ruled out by multi-user; wake-word + PTT is the lean. (#5)
- ✅ STT/LLM/TTS are **cloud-first behind a provider abstraction**, swappable for local or a
  self-hosted home endpoint. Pi stays viable as a thin orchestrator. (decisions.md #1)

## 4. World creation & editing (director capabilities)

Exposed as MCP tools the director calls. Indicative tool surface:

- `create_world(description)` / `load_world(id_or_query)` / `save_world()`
- `add_entity`, `update_entity`, `remove_entity`, `move/rotate/scale_entity`
- `set_environment` (sky, light, fog, time-of-day, ambient sound)
- `place_asset(query_or_ref, location)` — resolves via asset pipeline
- `generate_asset(kind, prompt)` — image / 3D / audio / texture
- `attach_behavior(entity, behavior_spec)` / `remove_behavior`
- `spawn_vehicle(type, location)` / `set_avatar(spec)` — create occupiable entities
- `occupy(entity, seat?)` / `exit()` — bind/unbind the user's embodiment (§7)
- `set_motion_model(entity, model, params)` / `set_control_scheme(entity, scheme)`
- `create_portal(target_world)` — link worlds
- `undo` / `redo` / `snapshot` / `revert_to(snapshot)`
- `query_world()` — let the director read current state before editing

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
  Poly Haven (HDRIs/textures/models).
- **Generative**: text/image → image (e.g. via the director calling image models),
  text/image → 3D (Meshy, Luma, Tripo, Hunyuan3D, Trellis, etc.), text → audio/SFX/music.
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
  (WebRTC/video/canvas) — keeps live-video-on-portals and remote-screen surfaces open (vision
  Future possibilities; architecture Forward-compatibility). Not a v1 feature.
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
    yoke flying a plane) the relevant motion model may run server-side near the input and sync pose
    to clients — ties to behavior placement (#6) and vehicle motion authority (§12, #12).
  - *Still to design (not a fork):* the host-input transport and the binding-config format.
- **Spatial audio**: positional sources + ambient beds for immersion.
- **Comfort & safety**: respect real-world **scale (meters)**, the guardian/play boundary, and
  motion-sickness comfort; don't spawn objects where the user will walk into a wall.
- **Vendor-neutral baseline + capability extensions** (decisions.md #11): the experience must
  work on any WebXR device. Device-specific features (Quest "Shared Spaces" co-location, certain
  hand-tracking specifics, passthrough particulars) are **extensions** that *enhance* but never
  *gate* core functionality. Each extension declares a neutral fallback.
- **WebXR requires a secure context (HTTPS).** Resolved as a 3-tier story: adb-reverse (dev) →
  Caddy + Let's Encrypt DNS-01 (multi-headset LAN) → Tailscale (remote). (decisions.md #3)
- **Co-location** (multi-user, §12): same-room headsets share a common physical origin. Preferred
  path is the Quest browser's native "Shared Spaces" (an *extension*); neutral fallback is
  marker/QR or manual calibration so co-location isn't Quest-locked. (decisions.md #9, #11)

## 8. Performance budget

The director builds within an explicit, enforced budget so worlds stay comfortable on Quest 3:
- target frame rate (72/90/120 Hz), max triangles, max draw calls, texture-memory ceiling.
- The pipeline downgrades/optimizes assets to fit; the director is told the budget and
  remaining headroom and must respect it.

## 9. Memory & persistence

- **World store**: serialized scene graphs + version history.
- **Asset store**: content-addressed, deduped, with license metadata.
- **Semantic recall**: find worlds by description ("the beach at sunset") — likely a vector
  index over world metadata/descriptions.
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
- **Co-location alignment.** Preferred path: Quest browser **Shared Spaces** (native colocated
  WebXR) for a common physical origin — but it's an *extension* (Quest-only, experimental, lost
  when the last participant leaves). Neutral fallback: marker/QR or manual calibration, so
  co-location degrades gracefully on non-Quest devices. (decisions.md #11)
- **Remote-ready transport.** World + presence go through a server relay (uniform LAN/WAN) so
  the remote voice bridge slots in over Tailscale/tunnel (§3 tier 3) without re-architecting.
  Presence MAY use P2P (PeerJS) on LAN, but world authority stays server-side.

## 13. Extensibility & modules

Conjure is, at heart, a **host for pluggable MCP modules**. The director is an MCP client of
many servers at once (PipeCat `MCPClient`); the world server is just the first. Adding a
capability = adding an MCP module — no core changes. Module taxonomy:

- **World-editing** — the core world server (§4). Always present.
- **Content-source modules** — surface assets/media the director can place. Local or remote.
  - *Example:* a **NAS photo library** module exposing `search_photos` / `get_photo`, including
    **stereoscopic (VR180/side-by-side) and 360° photos** placed as immersive media (§5).
  - *Example:* CC web asset libraries; generative-model wrappers.
- **Experience / engine modules** — external interactive systems the director *mediates* into
  the world.
  - *Example:* an **Infocom Z-machine** module (`new_game`, `send_command`, `get_state`). The
    user plays interactive fiction by voice; the director reads the IF state and **renders each
    room, object, and described scene as VR/AR content** — generating images/meshes and building
    the space as the story moves. Generalizes to any text/state-driven engine.
  - *Forward-compat example:* a **remote-session module** streaming a remote computer's screen
    onto a surface (and forwarding input). Relies on streaming/bidirectional module support
    (below) + live-media surfaces (§5). Not a v1 feature.
- **Capability / device-extension modules** — expose device-specific features (e.g. Quest
  Shared Spaces co-location) behind the capability-tier model (§7, decisions.md #11), each with
  a neutral fallback.

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
  an **input-forwarding path**, beyond request/response tool calls — keeping live feeds and
  remote sessions reachable. Design hook only, not a v1 requirement.

## 14. Out of scope (v1)

Deferred (not precluded): **truly remote multiplayer** (the remote voice bridge — §12 keeps the
transport ready for it), native app packaging, interactive mesh *sculpting* by hand (we
*generate* meshes, §5, but don't offer a modeling UI), photorealistic rendering. **Co-located,
same-room multi-user IS in scope** (§12).

## Appendix: open questions index

See [decisions.md](./decisions.md) for the full list.

- ✅ Resolved: cloud-vs-local compute (#1), world representation (#2), HTTPS-to-headset (#3),
  single-vs-multi-user (#9), capability tiers / Quest-as-extension (#11),
  **code sandboxing (#7 — capability API + unified JS in QuickJS-WASM)**.
- 🔶 Constrained: voice activation + audio capture (#5) — wake-word + PTT; shared-room device
  (diarization) or per-headset mics. Behavior placement (#6) — portable runtime; default split TBD.
- ❓ Still open: asset sources/providers (#4), desktop preview (#8),
  mesh-generation strategy (#10), vehicle motion: physics vs parametric (#12).
