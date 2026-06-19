# Architecture (v1 design)

> Status: **real design, v1 draft.** This defines the concrete contracts and runtime shape that
> implement [spec.md](./spec.md) under the decisions in [decisions.md](./decisions.md). Schemas
> are illustrative (JSON-ish) and meant to be firmed up into typed definitions during Phase 0.
> Firmness is flagged per section: 🟢 firm · 🟡 shape-firm, details open · 🔴 sketch.

## 1. Principles that drive the structure

- **One declarative world document is the source of truth** (A-Frame ECS, decision #2). Agents, the
  server, and every headset agree on it.
- **All change is a validated patch.** Nothing — not an agent, not behaviors — mutates the
  world directly; everything emits patches the trusted server validates and broadcasts.
- **Capability boundaries, not trust.** LLM-authored code and modules get narrow, declared
  capabilities and no ambient authority (decision #7).
- **Vendor-neutral baseline + extensions** (decision #11). Device features light up when present.
- **Network-decoupled components** so compute can move (Pi / Mac / home GPU box, decision #1).

## 2. System decomposition  🟢

Components and where each can run. "Host" = the machine running the Conjure server (Pi/Mac/Linux).

| # | Component | Runs on | Responsibility |
|---|---|---|---|
| 1 | **Voice agent** | host | PipeCat pipeline: STT → shell → agent → TTS, barge-in, addressing gate |
| 2 | **Shell + agents** | host | A deterministic **shell** (control: switch agent/LLM, reset — no LLM) above the active **agent** — an experience loaded from `agents/<name>/` ([agents.md](./agents.md)): an orchestrating LLM, MCP **client** of its scoped servers, with a **roster of named LLMs** (one active) sharing an attributed transcript (§7). The `builder` is the first agent (today's director) |
| 3 | **World server** | host | Owns the world document; validates + applies patches; serves the WebXR app; MCP **server** of world-editing tools; broadcasts state |
| 4 | **Behavior runtime** | host **and** client | QuickJS-WASM sandbox executing behaviors + geometry code (decision #7) |
| 5 | **Asset pipeline** | host (+ remote model APIs) | Resolve / generate / convert / optimize / cache content |
| 6 | **Memory** | host | World store, asset store, vector index, connection graph, sessions, anchors |
| 7 | **Modules** | anywhere | Pluggable MCP servers: content sources, engines, capability extensions, input providers |
| 8 | **Input layer** | host **and** client | Normalize + merge input devices into abstract actions/axes |
| 9 | **Model services** | cloud / local / home box | STT, LLM, TTS, image-gen, 3D-gen behind a provider abstraction (decision #1); per-slot defaults/options in [providers.md](./providers.md) |
| 10 | **WebXR client** | Quest / any WebXR device | Render + interact; VR/AR/flat; applies patches; capability detection |
| 11 | **Audio engine** | client (+ host gen) | Extensible, plugin-based: spatialized playback, programmatic/procedural synthesis (Web Audio / AudioWorklet), generated/streamed sources (§7 spec) |

```
                         ┌──────────── MCP (control plane) ─────────────┐
                         │                    │                         │
   ┌───────────┐    ┌────┴─────────┐    ┌──────┴──────┐          ┌───────┴────────┐
   │ Front-ends │   │ World server │    │   Modules   │          │ Model services │
   │ Shell+agent│───│ + validator  │    │ (NAS, IF,   │          │ (STT/LLM/TTS/  │
   │  (PipeCat) │   │ + MCP server │    │  input, …)  │          │  gen) provider │
   └─────┬─────┘    └──┬────────┬──┘    └─────────────┘          │  abstraction   │
         │ voice       │ state  │ assets                         └────────────────┘
         │ (WebRTC)    │ (WS)   │ (HTTPS)
   ┌─────┴───────────────────────────────────┴─────────────────────────────────────┐
   │  WebXR client (Quest / any device): A-Frame scene · QuickJS behaviors · input  │
   └────────────────────────── presence/high-rate channel ──────────────────────────┘
```

## 3. Channels & protocols  🟢 / 🟡 transport choices

Four planes, deliberately separated by reliability and rate:

1. **Control plane — MCP** 🟢. Director ↔ world server and ↔ modules. Tool calls (stdio / SSE /
   streamable-HTTP via PipeCat `MCPClient`). Low rate, reliable, request/response.
2. **State channel — WebSocket** 🟢. Server → all clients (and server ← validated edit results).
   Reliable, ordered. Carries **patches** with a monotonic `rev` (§5). Clients apply to their
   local A-Frame scene. Also delivers initial world snapshot on join.
3. **Presence / high-rate channel** 🟡. Per-user head/hand poses and vehicle kinematic pose.
   High rate, lossy-tolerant, unordered-ok. **Default: server relay** (uniform LAN/WAN, keeps the
   remote-bridge future open, decision #9). MAY use WebRTC datachannel / PeerJS P2P on LAN as an
   optimization. Never persisted into world state.
4. **Voice transport** 🟢. PipeCat WebRTC/WebSocket between the audio device (shared room mic or
   per-headset, decision #5) and the voice agent.

Plus **asset delivery** (HTTPS, content-addressed blobs from the world server / content store) and
**module streams** (future: out-of-band WebRTC/URL negotiated via the module manifest, §11).

Suggested host web stack 🟡: async Python (FastAPI/Starlette + uvicorn) for HTTP + WebSocket;
Caddy in front for TLS (decision #3).

## 4. The world document (scene-graph schema)  🟡

The serializable source of truth. Maps onto A-Frame entities/components; portable JSON so the
server, memory, and client share one representation.

```jsonc
{
  "id": "world_beach_sunset",
  "name": "Beach at sunset",
  "description": "A quiet beach, campfire, gentle waves",   // indexed for semantic recall
  "tags": ["beach", "sunset"],
  "rev": 42,                                                // bumped on every applied patch
  "created": "2026-06-04T…", "updated": "2026-06-04T…",
  "budget": { "maxTris": 500000, "maxDrawCalls": 200, "texMemMB": 256, "targetHz": 90 },
  "environment": {
    "sky":     { "type": "hdri", "asset": "sha256:…" },
    "lighting":{ "preset": "sunset", "directional": {…} },
    "fog":     { "color": "#f80", "density": 0.02 },
    "gravity": [0, -9.81, 0],
    "ambientAudio": "sha256:…"
  },
  "anchors": [                                              // forward-compat: persistent AR (#11)
    { "id": "anchor_wall1", "kind": "persistent-real-world", "fallback": "world-origin" }
  ],
  "entities": [ /* Entity[] */ ],
  "connections": [ { "portal": "ent_door1", "target": "world_cabin" } ]
}
```

**Entity** — transforms in **meters** (A-Frame rotation in degrees):

```jsonc
{
  "id": "ent_campfire_1",
  "parent": null,                       // entity id, or an anchor id (anchor-relative placement)
  "transform": { "position": [2,0,-3], "rotation": [0,45,0], "scale": [1,1,1] },
  "components": {
    "model":     { "asset": "sha256:…", "placeholder": "primitive:cone" },   // progressive build
    "material":  { "src": "sha256:…" },                 // src may later be a live stream (fwd-compat §5)
    "light":     { "type": "point", "intensity": 2.0, "color": "#fa3" },
    "sound":     { "asset": "sha256:…", "positional": true },
    "collider":  { "shape": "box" },
    "occupiable":{ "seats": [{ "id":"pilot", "transform":{…} }],
                   "motionModel": "hot-air-balloon", "controlScheme": "balloon-default" }
  },
  "behaviors": [ /* BehaviorRef[] — see §9 */ ],
  "meta": { "license": "CC-BY", "attribution": "…", "source": "polypizza", "generated": false }
}
```

Invariants: fully serializable & restorable; every entity has a stable id; component set is
open/extensible; `parent` may target an anchor to keep persistent-AR reachable.

## 5. Patch protocol  🟡

Every change is a patch — the unit of live sync **and** undo/redo.

```jsonc
{
  "rev": 43,                         // = previous rev + 1; clients reject out-of-order
  "origin": "director" | "behavior:beh_x" | "module:nas" | "user:u1",
  "ops": [
    { "op": "add",    "entity": { … } },
    { "op": "update", "id": "ent_campfire_1", "set": { "components.light.intensity": 3.0 } },
    { "op": "remove", "id": "ent_x" },
    { "op": "env",    "set": { "environment.fog.density": 0.05 } },
    { "op": "occupy", "user": "u1", "entity": "ent_balloon", "seat": "pilot" },
    { "op": "exit",   "user": "u1" }
  ],
  "inverse": [ … ]                   // server-computed inverse ops for undo (or snapshot ref)
}
```

- **Authority & ordering:** the **world server** is the only writer. It assigns `rev`, computes the
  `inverse`, applies, then broadcasts. Conflicts resolve by server order (last-writer-by-rev).
- **Validation gate** (every patch, regardless of origin): schema-valid, within performance
  **budget** (§ world doc), permitted for the origin's capabilities, references resolvable assets.
  Rejected patches are dropped with a reason logged; the originator is notified.
- **Undo/redo:** apply the stored `inverse` (or revert to a snapshot). Operates on server state,
  re-broadcast as a normal patch.
- **Progressive construction:** `place_asset` emits an `add` with a `placeholder` immediately, then
  a follow-up `update` swapping in the resolved asset when the pipeline finishes.

## 6. State synchronization & authority  🟡

- **World state:** server-authoritative. Client applies patches to its A-Frame scene; it never
  authors world state directly — it sends **intents** (from local behaviors / input) that the
  server validates into patches.
- **Presence:** relayed, not authoritative — poses are advisory and ephemeral.
- **Vehicle motion:** the occupant with **motion authority** computes pose from the motion model
  and streams it on the high-rate channel; the server validates collisions/occupancy transitions
  and is authoritative for those. For tight loops with host-side input devices, the motion model
  may run server-side near the input (decision #6/#12, §11 input).
- **Co-location** (multi-user, decision #9): preferred path = Quest "Shared Spaces" shared origin
  (extension); neutral fallback = marker/QR or manual calibration. Presence + world render in that
  shared frame so objects land in the same physical spot.

## 7. The agent (director) + shell  🟡

The director is now the **`builder` agent** — an experience loaded from `agents/builder/`
([agents.md](./agents.md)): a prompt, the LLMs allowed to run it, the MCP servers it's scoped to, and
the context it injects. Above it sits a deterministic **shell** (`conjure/shell.py`): the control plane
for things that must be reliable — switch agent/LLM, reset, status — parsed, never sent to an LLM. The
shell forwards anything that isn't a command to the active agent, which runs the orchestration loop
(one turn):

1. **Perceive** — addressing gate (§ voice) / shell admits agent-directed speech → STT → the agent's
   LLM. The prompt carries: the **live room**, injected via the `room://current` context resource
   (agents.md §5) so the agent needn't re-query it; the performance budget + headroom; the agent's
   scoped MCP tools; and session/conversation memory. (`query_world` is still used for the mutable
   generated scene.)
2. **Plan & act** — the agent calls MCP tools (world-editing and/or module tools). It reads
   state back when an edit is context-dependent.
3. **Apply** — tools mutate server world state through the validation gate → patches → broadcast.
4. **Narrate** — TTS confirms / narrates progress during slow asset/gen work ("fetching driftwood…").
5. **Record** — snapshot for undo; log the action + resulting diff (observability/provenance).

Design notes: keep tools **coarse and intent-level** (`place_asset("campfire", near=user)`) so the
agent reasons about goals, not transforms. The shell + agent abstraction (scoped toolsets, personas,
per-agent world spaces, and a second agent beyond the builder) is designed in **[agents.md](./agents.md)**.

### 7a. LLM roster — many named LLMs in one session  🟡

An agent is run by a **roster** of LLMs, not a single model (scoped to the agent's allowed set):

- **Roster** — a user-editable map of **casual name → provider/model config** (e.g. `"Gemini"` →
  Gemini, `"Chat"` → GPT), each behind the provider abstraction (decision #1). One is **active**
  at a time; the active stream is routed to it.
- **Switching** — "let me talk to Gemini" (or addressing a name directly) makes that LLM active. This
  is the **shell**'s job (a deterministic command — agents.md §2); the inline phrase is still also
  handled inside the agent today (migration deferred). The casual name doubles as an addressing target
  alongside the wake word (decision #5).
- **Attributed shared transcript** — a single conversation log where every turn carries a
  `speaker` (`user` | LLM name). All LLMs are prompted with this shared, attributed history, so a
  newly-active LLM can **reference/comment on another LLM's contributions**. World state and
  tool/edit history are shared too — switching never drops context.
- Open design points: how much of a non-active LLM's internal reasoning is shared (we share the
  visible transcript + edits, not hidden chain-of-thought); per-LLM system-prompt/persona.

## 8. MCP tool surface (world server)  🟡

Grouped; signatures indicative. These are the director's action vocabulary (spec §4).

- **Lifecycle:** `create_world(description)`, `load_world(id_or_query)`, `save_world()`,
  `query_world(filter?)`
- **Entities:** `add_entity`, `update_entity`, `remove_entity`, `move/rotate/scale_entity`
- **Environment:** `set_environment(sky|light|fog|time_of_day|ambient)`
- **Assets:** `place_asset(query_or_ref, location)`, `generate_asset(kind, prompt)`
- **Images — procurement decoupled from scene use (decision #13):** *procure* (return an opaque
  image id) `generate_image`, `generate_skybox_image`, `edit_image(image_id)`,
  `outpaint_image(image_id)`, `skybox_from_image(image_id)`, `list_image_generators`; *use in scene*
  `place_image(image_id)`, `set_skybox(image_id)`; *one-shot in-scene edits* (entity-keyed, procure
  +apply server-side) `edit_scene_image`, `widen_scene_image`, `skybox_from_scene_image`. Generators
  declare `ImageCapabilities`; the world server mediates which one runs (best default per op, an
  optional explicit `generator`, transparency→OpenAI), backed by an in-memory image store over the
  content-addressed cache. Built in `conjure/llm.py` (the provider abstraction) + `conjure/server.py`.
- **Behavior & geometry:** `attach_behavior(entity, spec)`, `remove_behavior`,
  `generate_mesh(spec)` (runs in sandbox, decision #10)
- **Embodiment:** `spawn_vehicle(type, location)`, `set_avatar(spec)`, `occupy(entity, seat?)`,
  `exit()`, `set_motion_model`, `set_control_scheme`, `bind_input(scheme, mapping)`
- **Connections:** `create_portal(target_world)`
- **History:** `undo`, `redo`, `snapshot`, `revert_to(snapshot)`

Each tool returns the resulting `rev` and a summary so the director stays in sync.

## 9. Behavior & geometry runtime  🟢 boundary / 🟡 SDK surface

**Engine:** QuickJS-WASM on both sides (decision #7) — `quickjs-emscripten` in the browser; QuickJS
embedded in the Python host (binding, or `quickjs.wasm` under `wasmtime-py` for double isolation).
Behaviors are **portable**; placement is a per-behavior tag (decision #6).

**BehaviorRef** (stored on an entity or the world):

```jsonc
{ "id": "beh_clap_fireworks", "placement": "client" | "server",
  "capabilities": ["timer", "audio", "spawn"],          // declared; host grants least privilege
  "source": "…inline JS…" | "ref:sha256:…" }
```

**Behavior SDK** — the *only* surface injected into the sandbox (no `window`, `fetch`, fs, net,
keys). Effects are emitted as **patch intents**, validated server-side before applying:

```js
export default (world) => {
  world.on('clap', (e) => {                 // events: timers, proximity, gaze, input actions,
    world.spawn({ model: 'firework', at: e.position });   // collisions, voice triggers, lifecycle
    world.audio.play('firework-pop', { at: e.position });
  });
  world.every(5000, () => world.update('sun', { 'rotation.x': '+=2' }));
  world.input.onAxis('throttle', (v) => world.vehicle.setThrust(v));   // input abstraction (§11b)
};
```

SDK groups: `world.on/every/after` (events/timers) · `world.get/query` (read provided state) ·
`world.spawn/update/remove` (emit patch intents) · `world.audio` · `world.assets.request` ·
`world.input` (abstract actions/axes) · `world.occupancy` / `world.vehicle` · `world.log`.

**Limits & gating:** per-run instruction cap (QuickJS interrupt), memory ceiling, wall-clock
timeout; director-generated code passes a lint/review gate before first run. **Geometry generators**
use the same sandbox and return geometry via the SDK with no I/O (decision #10).

## 10. Asset pipeline  🟡

Flow: **resolve → (fetch | generate) → convert → optimize → cache → describe → place**.

- **Resolve:** cache hit by content hash? else pick a source (CC library / module / generator).
- **Convert:** anything → **glTF/GLB** (Blender headless / assimp / gltf-transform).
- **Process (pluggable ops):** image **up-res / super-resolution** and **outpainting/extrapolation**
  (photo → seamless skybox / 360 / cylindrical panorama), behind the provider abstraction or as
  processing modules (spec §5, §13). These produce derived assets with their own descriptor.
- **Optimize to budget:** Draco/meshopt, texture downscale, LOD; must fit the world's perf budget.
- **Cache:** content-addressed blob store; dedup across worlds.

**Asset descriptor** (in memory; referenced by hash from entities):

```jsonc
{ "hash": "sha256:…", "kind": "model|image|stereo-image|360-image|audio|hdri|stream",
  "format": "glb", "media": { "stereo": "sbs|vr180|null", "projection": "equirect|null" },
  "license": { "id": "CC-BY", "attribution": "…", "url": "…" }, "source": "polypizza|gen:meshy|module:nas",
  "optimized": { "draco": true, "lod": [0,1,2], "texMB": 4 }, "tris": 1200 }
```

Media types are first-class (stereo/360, spec §5); `kind:"stream"` is the forward-compat hook for
live video / remote-screen surfaces (§12).

## 11. Modules, input & capability extensions

### 11a. Modules  🟡
MCP servers an agent also clients into — each agent declares which (via the registry, agents.md §4).
**Module manifest** (Conjure metadata atop MCP):

```jsonc
{ "name": "nas-photos", "kind": "content-source|experience|capability|input-provider",
  "tools": [ … MCP tool schemas … ],
  "permissions": ["read"],                 // NOT world-write/code-exec unless explicitly granted
  "streams": false,                        // future: { "kind":"webrtc", "input": true }
}
```

Trust: a module's permissions are scoped (decision #7); content/engine modules can't write world
state or run code unless granted. Example future *experience* module: a remote-desktop streamer
(streams: webrtc + input) rendering onto a `kind:"stream"` surface.

### 11b. Input architecture  🟡
Abstract **actions** (discrete) + **axes** (continuous); control schemes and behaviors bind to
these, never raw devices.

- **Sources, merged into one logical state:** *client* (WebXR controllers/hands, Gamepad API incl.
  BT controllers paired to Quest, WebHID/WebUSB) and *host* (USB/BT yokes, pedals, throttles,
  joysticks, trackballs via SDL/evdev/hidapi, streamed in).
- **Drivers** are pluggable (built-in or input-provider modules); each declares the devices/axes it
  offers.
- **Binding schema** (voice-configurable):

```jsonc
{ "scheme": "plane-yoke",
  "axes":    { "pitch": "yoke/axisY", "roll": "yoke/axisX", "yaw": "pedals/rudder", "throttle": "throttle/axisZ" },
  "actions": { "brake": "pedals/toeButton", "fire": "controller/trigger" } }
```

- **Hotplug + capability:** schemes adapt to present devices; gaze/voice always available.
- **Latency/placement:** host devices add a hop → tight loops may run the motion model host-side.

### 11c. Capability tiers  🟢
At session start the client builds a **capability descriptor** (immersive-vr/ar support,
shared-spaces, hand-tracking, depth/hit-test, gamepad/WebHID, …). Extensions activate per
capability with declared neutral fallbacks (decision #11). Both `immersive-vr` and `immersive-ar`
are baseline; `flat` covers non-XR browsers (desktop preview, decision #8).

## 12. Memory subsystem  🟡

- **World store** — world documents + version history (snapshots + inverse patches for undo/recall).
- **Asset store** — content-addressed blobs + descriptors (§10).
- **Vector index** — embeddings of world name/description/tags for semantic recall ("the beach world").
- **Connection graph** — portals between worlds.
- **Session store** — the **attributed transcript** (each turn tagged with speaker: `user` or an
  LLM roster name, §7a) + edit provenance ("why is this here"). Shared across all roster LLMs.
- **Anchor registry** (forward-compat) — persistent real-world anchor id ↔ pose, keyed to a place.

Storage choices 🔴 (open, low-stakes): SQLite + a vector extension and a filesystem blob store is a
fine Pi-friendly default; swappable later.

## 13. Security & trust model  🟢

Two zones. **Trusted core:** world server, validator, memory, asset pipeline. **Untrusted:**
LLM-authored behavior/geometry code, and modules. The boundary between them:

- **Capability SDK only** — no ambient authority in either sandbox (host or browser).
- **Every effect is a validated patch intent** — even a sandbox escape can only produce
  budget/schema/permission-valid world edits.
- **Resource limits** on every sandbox run; **static lint gate** on generated code.
- **Module permission scoping** — least privilege; sensitive grants surfaced to the user.
- **Content & licensing** — moderation on generated assets; license/attribution captured per asset.
- **Transport** — TLS to every client (decision #3); modules authenticated.

## 14. Deployment topologies (decision #1)  🟢

Network-decoupled components let compute move without re-architecting:

- **A — Pi + all-cloud:** Pi runs voice agent + world server + memory; all model services are cloud.
- **B — Mac host:** same, with some model services local on the Mac.
- **C — Pi/Mac + home GPU box:** orchestrator stays light; a self-hosted machine on the LAN serves
  model endpoints behind the same provider abstraction.

The provider abstraction (one interface per model role) makes A/B/C the same code.

## 15. Forward-compatibility (must not be precluded)  🟢

- **Source-agnostic media surfaces** — materials bind to a source that may be a live stream
  (`kind:"stream"`), enabling live video on portals + remote-screen surfaces.
- **Anchor-relative placement** — entities may parent to a persistent real-world anchor, enabling
  persistent AR in the home as a capability extension.
- **Streaming & bidirectional modules** — the module manifest can carry stream/handshake metadata
  and an input-forwarding path, enabling remote-desktop and other live feeds.

## 16. Open decisions & build order

Open forks (none blocking the architecture): #4 asset sources/providers · #6 default behavior split
· #8 desktop preview · #10 mesh-gen first mode · #12 vehicle motion (physics vs parametric). See
[decisions.md](./decisions.md); sequencing in [roadmap.md](./roadmap.md).

**First contracts to lock in Phase 0** (everything else builds on them): the **world document
schema** (§4), the **patch protocol** (§5), and the **state channel** (§3). Get a static A-Frame
scene onto the Quest over TLS, then drive it with hand-authored patches before adding the director.
