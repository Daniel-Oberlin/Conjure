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
- **Capability boundaries, not trust.** LLM-authored code and third-party modules get narrow, declared
  capabilities and no ambient authority (decision #7). Agent tool scoping realizes this today; the
  sandbox for generated code does not yet exist — §13 says exactly where the line sits.
- **Sync causes, never effects.** Broadcast the touch, not the ripple field it produced; the transport
  ("track X, play from shared-clock t₀"), not the audio analysis. Every client then derives the same
  state locally from a shared clock, a seed, and the config in the snapshot — consistency without
  per-frame sync (§6, [specs/dynamics.md §1](./specs/dynamics.md)).
- **Vendor-neutral baseline + extensions** (decision #11). Device features light up when present.
- **Network-decoupled components** so compute can move (Pi / Mac / home GPU box, decision #1).

## 2. System decomposition  🟢

Components and where each can run. "Host" = the machine running the Conjure server (Pi/Mac/Linux).

| # | Component | Runs on | Responsibility |
|---|---|---|---|
| 1 | **Voice / CLI clients** | host | Thin front-ends. Voice is a PipeCat pipeline (STT → WebSocket → TTS) plus a wake gate; the CLI is a terminal REPL. Neither holds state, keys, or command logic |
| 2 | **Agent server** | host | The long-lived host of the deterministic **shell** (switch agent/LLM/session, navigate the namespace — no LLM) above the active **agent** — an experience loaded from `agents/<name>/` ([specs/agents.md](./specs/agents.md)): an orchestrating LLM, MCP **client** of its scoped servers, with a **roster of named LLMs** (one active) sharing a single transcript (§7). One shared conversation for every client; follows the world server's live state |
| 3 | **World server** | host | Owns the world document; validates + applies patches; serves the WebXR app; MCP **server** of world-editing tools; broadcasts state |
| 4 | **Dynamic modules** | host (served) + client (run) | Live, animated, interactive effects the agent can **conjure** into a world — fireflies, a rippling Water Picture, object manipulation. Each is a folder + manifest whose entry script registers one A-Frame component; the server serves it and delivers its **config in the world snapshot**, so presence and state are shared for free. Built; specified in [specs/dynamics.md](./specs/dynamics.md) |
| 4b | **Behavior runtime** 🔴 | host **and** client | QuickJS-WASM sandbox for **LLM-authored** behaviors + geometry code (decision #7). Designed, not built — §9. Distinct from row 4: modules are curated, first-party, trusted code |
| 5 | **Asset pipeline** | host (+ remote model APIs) | Resolve / generate / convert / optimize / cache content |
| 6 | **Memory** | host | World store, asset store, vector index, connection graph, sessions, anchors |
| 7 | **MCP modules** | anywhere | Pluggable MCP **servers**: content sources, engines, capability extensions, input providers. *Not* row 4 — see the naming note in §11a |
| 8 | **Input layer** | host **and** client | Normalize + merge input devices into abstract actions/axes. The client half is built (`ConjurePointers`, §11b) |
| 9 | **Model services** | cloud / local / home box | STT, LLM, TTS, image-gen, 3D-gen behind a provider abstraction (decision #1); per-slot defaults/options in [providers.md](./providers.md) |
| 10 | **WebXR client** | Quest / any WebXR device | Render + interact; VR/AR/flat; applies patches; capability detection |
| 11 | **Audio engine** | client (+ host gen) | Extensible, plugin-based: spatialized playback, programmatic/procedural synthesis (Web Audio / AudioWorklet), generated/streamed sources (§7 spec) |

```
   voice · CLI  ─── WS ───▶  ┌──────────────┐
   (thin clients)            │ Agent server │  shell + agent + shared transcript
                             └──┬────────┬──┘
                    MCP (stdio) │        │ rides /ws as a passive listener
                         ┌──────▼──────┐ │      ┌─────────────┐   ┌────────────────┐
                         │ mcp_server  │ │      │   Modules   │   │ Model services │
                         │ + tool gate │ │      │ (NAS, IF,   │   │ (STT/LLM/TTS/  │
                         └──────┬──────┘ │      │  input, …)  │   │  gen) provider │
                        HTTP    │        │      └─────────────┘   │  abstraction   │
                         ┌──────▼────────▼───┐                    └────────────────┘
                         │   World server    │  the single source of truth
                         │ + validator + MCP │  (worlds, sessions, spaces, pointer)
                         └──┬─────────────┬──┘
                            │ state (WS)  │ assets (HTTPS)
   ┌────────────────────────┴─────────────┴─────────────────────────────────────────┐
   │  WebXR client (Quest / any device): A-Frame scene · QuickJS behaviors · input   │
   └────────────────────────── presence/high-rate channel ──────────────────────────┘
```

The two servers are deliberately separate and **order-independent**: the world server boots standalone
from disk and renders to headsets with no agent server present (walk your world with no AI), and the
agent server can load without the world server but only serves turns once connected.

## 3. Channels & protocols  🟢 / 🟡 transport choices

Six planes, deliberately separated by reliability and rate:

0. **Conversation channel — WebSocket** 🟢. One per-connection socket from each thin client to the
   agent server (`ws://…/ws?user=&client=`). Client → server: `{type:"turn", text}`, one line, verbatim.
   Server → client: this connection's `context` (data, not a formatted prompt) plus the shared
   conversation (`user_turn` / `assistant_delta` / `assistant_final` / `tool_call` / `notice` / `busy`)
   and a `turn_done` prompt gate. All command logic is server-side; the client never parses
   ([specs/agents.md §8](./specs/agents.md)).
1. **Control plane — MCP** 🟢. Director ↔ world server and ↔ modules. Tool calls (stdio / SSE /
   streamable-HTTP via PipeCat `MCPClient`). Low rate, reliable, request/response. Identity travels
   per-turn (`set_caller`) so a shared conversation attributes each turn to whoever spoke.
2. **State channel — WebSocket** 🟢. Server → all clients (and server ← validated edit results).
   Reliable, ordered. Carries **patches** with a monotonic `rev` (§5). Clients apply to their
   local A-Frame scene. Also delivers the initial world snapshot on join, and every snapshot carries the
   canonical live-state identifiers under `state` — so the headset and the agent server both reconcile
   from one broadcast.
3. **Presence / high-rate channel** 🟡. Per-user head/hand poses and vehicle kinematic pose.
   High rate, lossy-tolerant, unordered-ok. **Default: server relay** (uniform LAN/WAN, keeps the
   remote-bridge future open, decision #9). MAY use WebRTC datachannel / PeerJS P2P on LAN as an
   optimization. Never persisted into world state.
4. **Voice transport** 🟢. PipeCat WebRTC/WebSocket between the audio device (shared room mic or
   per-headset, decision #5) and the voice agent.
5. **Module event bus** 🟢. A `module_event` message on the *same* `/ws` as the state channel, relayed
   to the **other** clients only (the sender never receives its own). It carries a **cause**, not an
   effect — a water touch, not the resulting distortion field — so each client runs its own simulation
   from it. Cheap, lossy-tolerant, and deliberately not converged
   ([specs/dynamics.md §2, §6](./specs/dynamics.md)).

Plus **asset delivery** (HTTPS, content-addressed blobs from the world server / content store),
**module scripts + the shared clock** (`GET /dynamics/<module>/<file>` with an mtime cache-buster, and
`GET /time` for the Cristian-style clock sync every deterministic module derives its state from), and
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
                   "motionModel": "hot-air-balloon", "controlScheme": "balloon-default" },
    "water":     { "damping": 0.996, "src": "…" }       // a DYNAMIC MODULE: config-in-snapshot (§2 row 4)
  },
  "behaviors": [ /* BehaviorRef[] — see §9 */ ],
  "meta": { "license": "CC-BY", "attribution": "…", "source": "polypizza", "generated": false }
}
```

Invariants: fully serializable & restorable; every entity has a stable id; component set is
open/extensible; `parent` may target an anchor to keep persistent-AR reachable.

**The open component set is what makes dynamic modules free.** A module *is* an A-Frame component: the
server adds an entity carrying `components.<component> = <config>` and the client applies it with
`setAttribute`. Because that is an ordinary entity, a module is shared across clients, persisted, and
replayed on the existing entity/patch/snapshot path — there is no bespoke per-module loader, storage
model, or authority model. A procedural module persists by storing `(seed, config)`, so a reload
restores it exactly. ([specs/dynamics.md §1](./specs/dynamics.md).)

Two things stay deliberately **outside** that mechanism, and the boundary is what keeps it small:
**environment is world-level, not a module** — a world sets the backdrop (sky, fog, immersion) and
modules are the live performers on top of it, additive in passthrough and VR alike; and **occlusion is
global** — real-world depth is one pre-pass for the whole scene
([specs/occlusion.md](./specs/occlusion.md)), so no module ever samples depth or opts in.

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
- **Live effects: three sync tiers, declared per module** ([specs/dynamics.md §2](./specs/dynamics.md)).
  Determinism is a *capability a module opts into*, not a mandate, and the tier decides what crosses the
  wire at all:

  | Tier | What syncs | Cost | Shipped |
  |---|---|---|---|
  | **A** autonomous-procedural | `(clock, seed, config)` in the snapshot; **nothing at runtime** | ~free | `fireflies` |
  | **B** input-reactive | the input **event** (bus, §3 plane 5); each client sims locally from it | cheap | `water` |
  | **C** shared-authoritative | the **resting** state, committed on gesture end | expensive, self-rate-limiting | `grab` |

  The invariant underneath: *presence and simulation state are shared by everyone; presentation need
  not be pixel-identical.* That is not a loophole but how absoluteness is bought cheaply — foveation,
  billboard yaw and interpolation stay local, while state keyed off the shared clock stays common. Tier
  B is deliberately **not** converged (short-lived, cosmetic; convergence would cost more than it's
  worth), and tier C broadcasts nothing mid-gesture — peers see the object arrive at its resting pose on
  release, committed through the same owner-gated, validated, autosaved patch path as any other edit.
- **Vehicle motion:** the occupant with **motion authority** computes pose from the motion model
  and streams it on the high-rate channel; the server validates collisions/occupancy transitions
  and is authoritative for those. For tight loops with host-side input devices, the motion model
  may run server-side near the input (decision #6/#12, §11 input).
- **Co-location** (multi-user, decision #9): preferred path = Quest "Shared Spaces" shared origin
  (extension); neutral fallback = marker/QR or manual calibration. Presence + world render in that
  shared frame so objects land in the same physical spot.

## 7. The agent (director) + shell  🟡

The director is now the **`builder` agent** — an experience loaded from `agents/builder/`
([specs/agents.md](./specs/agents.md)): a prompt, the LLMs allowed to run it, the MCP servers it's
scoped to, the context it injects, the dynamic modules it may conjure. Above it sits a deterministic
**shell** (`conjure/shell.py`): the control plane for things that must be reliable — switch
agent/LLM/session, navigate and prune the namespace — parsed, never sent to an LLM. Both live in the
**agent server** (`conjure/agent_server.py`), one long-lived process holding one shell → one director →
one shared transcript, with voice and CLI as thin clients over the conversation channel (§3).

The shell forwards anything that isn't a command to the active agent, which runs the orchestration loop
(one turn):

1. **Perceive** — addressing gate (§ voice) / shell admits agent-directed speech → STT → the agent's
   LLM. The prompt carries: the **live room**, injected via the `room://current` context resource
   ([specs/agents.md §5.3](./specs/agents.md)) so the agent needn't re-query it; the placed scene
   (`world://current`) and its conjurable modules (`dynamics://available`); the agent's scoped MCP
   tools; and the session transcript. (`query_world` is still used where a prefetched snapshot would go
   stale.)
2. **Plan & act** — the agent calls MCP tools (world-editing and/or module tools). It reads
   state back when an edit is context-dependent.
3. **Apply** — tools mutate server world state through the validation gate → patches → broadcast.
4. **Narrate** — TTS confirms / narrates progress during slow asset/gen work ("fetching driftwood…").
5. **Record** — snapshot for undo; log the action + resulting diff (observability/provenance).

Design notes: keep tools **coarse and intent-level** (`place_asset("campfire", near=user)`) so the
agent reasons about goals, not transforms. The whole orchestration layer — the agent def, two-layer tool
scoping, the shell's command registry and namespace, sessions and their constructor, the agent server's
protocol and its follow loop, and the shared-session permission model — is specified in
**[specs/agents.md](./specs/agents.md)**, with the unbuilt parts (personas, per-agent world-space
composition, pinning while held) in **[backlogs/agents.md](./backlogs/agents.md)**.

### 7a. LLM roster — many named LLMs in one session  🟡

An agent is run by a **roster** of LLMs, not a single model (scoped to the agent's allowed set):

- **Roster** — a user-editable map of **casual name → provider/model config** (e.g. `"Gemini"` →
  Gemini, `"Chat"` → GPT), each behind the provider abstraction (decision #1). One is **active**
  at a time; the active stream is routed to it.
- **Switching** — `llm gemini` typed, or "talk to Gemini" spoken. This is **exclusively** the shell's
  job (a deterministic command — [specs/agents.md §6](./specs/agents.md)): the inline `route_turn`
  handover was removed from the agent, so no utterance is ever parsed for a switch. The active LLM is
  **shared** — a switch by anyone affects everyone — and the choice is remembered on the session, so it
  survives a restart or a switch-back.
- **Shared transcript** — a single user/assistant conversation log. It carries **no record of which
  LLM authored a reply**: every reply is a plain `assistant` turn, so a newly-active LLM inherits the
  whole history seamlessly and a switch of LLMs is invisible in the context. The system prompt names
  no LLM either — it is identical whichever LLM is active. World state and tool/edit history are
  shared too, so switching never drops context. (Earlier revisions tagged each turn with its LLM and
  prefixed other LLMs' lines `[Name]`; that identity-in-context machinery was removed.) *User* turns
  are attributed — each carries the human `speaker`, which the model sees as a label and which the
  world server enforces ownership against.
- Open design points: whether a switch should ever be surfaced *to the model* (today it is not);
  per-LLM system-prompt/persona.

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
- **Dynamic modules (built):** one generic tool each way —
  `conjure_module(module, config?, position?, on_surface?, billboard?, stretch?, name?)` and
  `dismiss_module(name=<entity id> | module=<kind>)`. No per-module tool and no discovery ritual: the
  agent's scoped catalog arrives in its prompt as the `dynamics://available` **resource**, one
  `name — description; params: k(default)…` line per module, so `conjure_module` stays generic while the
  vocabulary stays live. `POST /manipulate` commits a tier-C resting transform
  ([specs/dynamics.md §9](./specs/dynamics.md)).
- **Behavior & geometry (designed, §9):** `attach_behavior(entity, spec)`, `remove_behavior`,
  `generate_mesh(spec)` (runs in sandbox, decision #10)
- **Embodiment:** `spawn_vehicle(type, location)`, `set_avatar(spec)`, `occupy(entity, seat?)`,
  `exit()`, `set_motion_model`, `set_control_scheme`, `bind_input(scheme, mapping)`
- **Connections:** `create_portal(target_world)`
- **History:** `undo`, `redo`, `snapshot`, `revert_to(snapshot)`

Each tool returns the resulting `rev` and a summary so the director stays in sync.

## 9. Behavior & geometry runtime  🔴 not built / 🟢 boundary / 🟡 SDK surface

> **Read this against §2 row 4.** Live, animated, interactive behaviour ships today as **dynamic
> modules** — curated, first-party A-Frame components, delivered as config-in-snapshot and loaded
> straight into the page ([specs/dynamics.md](./specs/dynamics.md)). They are *trusted* code and get
> the full page: no sandbox, no capability declaration, no instruction cap. That is deliberate, and it
> is why the sandbox below is still unbuilt — nothing has yet needed to run code we didn't write.
>
> This section is the design for the case that changes it: **LLM- and user-generated** behaviour. The
> trust boundary is real even under "identity only, no security" — arbitrary JS or shader source can
> hang the render loop or crash a mobile GPU. The intent that shapes the module manifest *now* is that
> when generation lands, **real code stays in a curated registry and the generator emits only config +
> wiring** against a constrained surface; `config_schema` is the first slice of that boundary. See
> [backlogs/dynamics.md](./backlogs/dynamics.md) and decision #7.

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

## 11. MCP modules, input & capability extensions

### 11a. MCP modules  🟡

> **Two things are called "module".** An **MCP module** (this section) is a *server* an agent clients
> into — it extends the agent's **tool surface**. A **dynamic module** (§2 row 4,
> [specs/dynamics.md](./specs/dynamics.md)) is a *client-side A-Frame component* the agent conjures into
> a world — it extends the **scene**. They share no machinery: an MCP module is declared in
> `agents/servers.json` and referenced by an agent's `mcp_servers`; a dynamic module is a folder on the
> dynamics search path referenced by an agent's `dynamics`. Both are allow-listed per agent, and that is
> the whole of the resemblance.

MCP servers an agent also clients into — each agent declares which (via the registry,
[specs/agents.md §3](./specs/agents.md)).
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

**Built today — the client half, for XR controllers and hands** (`client/conjure-pointers.js`,
[specs/dynamics.md §6](./specs/dynamics.md)). It is the abstraction above proved out on the source that
exists now; host devices, drivers and hotplug remain designed.

- **One reader per frame.** `ConjurePointers` is the *only* consumer of `session.inputSources`. It
  publishes a normalized snapshot per pointer, cached on the XRFrame with a recency window, so N
  consumers cost one read. Before it, four places walked the frame themselves and hard-coded button
  indices.
- **Modules ask for actions, never buttons.** A binding table maps control → action and lives in config
  (`Settings.bindings`, injected as `window.CONJURE_BINDINGS`), so re-binding is a settings change, not
  an edit in every module. A control may be **hand-qualified** (`"left.stickY"`), so one hand can hold
  an object while the other shapes it. Defaults: `select`/`resize` → trigger, `grab` → grip,
  `reel`/`yaw` → right stick, `pitch`/`bank` → left stick.
- **Sharing is explicit.** Tick order isn't guaranteed and two modules can want the same control, so
  arbitration lives in the input layer: a **capture** (`claim`/`release`) holds a pointer for a whole
  gesture; a **reservation**, renewed every frame, says "I'd take the next press here". A reservation
  made this frame *or last* still counts, which is what makes it order-independent.
- **One definition of "in use".** `armed()` — the pointer is engaged, or was within a timeout. Both the
  visible beam and a module's highlight key off it, so presentation and focus agree by construction
  rather than by convention.

### 11c. Capability tiers  🟢
At session start the client builds a **capability descriptor** (immersive-vr/ar support,
shared-spaces, hand-tracking, depth/hit-test, gamepad/WebHID, …). Extensions activate per
capability with declared neutral fallbacks (decision #11). Both `immersive-vr` and `immersive-ar`
are baseline; `flat` covers non-XR browsers (desktop preview, decision #8).

## 12. Memory subsystem  🟡

- **World store** — world documents + version history (snapshots + inverse patches for undo/recall). A
  live **dynamic module** needs no store of its own: it is an entity carrying its component config, so
  it persists and reloads on this path like anything else (§4).
- **Asset store** — content-addressed blobs + descriptors (§10).
- **Vector index** — embeddings of world name/description/tags for semantic recall ("the beach world").
- **Connection graph** — portals between worlds.
- **Session store** — a **session** is an instance of an agent: its **shared transcript** (append-only
  `transcript.jsonl`, plain `user`/`assistant` turns with a human speaker, no per-LLM attribution, §7a),
  the worlds created in it, and its agent state (`state/`, a bag of named JSON docs behind the generic
  `state_*` tools). Named, owned, persisted, switchable; visibility lives here and a world inherits it.
  One global pointer records which session is live. See
  [specs/agents.md §7](./specs/agents.md).
- **Anchor registry** (forward-compat) — persistent real-world anchor id ↔ pose, keyed to a place.

Storage choices 🔴 (open, low-stakes): SQLite + a vector extension and a filesystem blob store is a
fine Pi-friendly default; swappable later.

## 13. Security & trust model  🟢 intent / 🔴 sandbox unbuilt

Two zones. **Trusted core:** world server, validator, memory, asset pipeline. **Untrusted:**
LLM-authored behavior/geometry code, and third-party MCP modules. The boundary between them:

- **Capability SDK only** — no ambient authority in either sandbox (host or browser).
- **Every effect is a validated patch intent** — even a sandbox escape can only produce
  budget/schema/permission-valid world edits.
- **Resource limits** on every sandbox run; **static lint gate** on generated code.
- **Module permission scoping** — least privilege; sensitive grants surfaced to the user.
- **Content & licensing** — moderation on generated assets; license/attribution captured per asset.
- **Transport** — TLS to every client (decision #3); modules authenticated.

**Where the line actually sits today.** Two of these are enforced and the rest are intent:

- **Enforced.** Agent **tool scoping** is real and two-layer: the director offers only the agent's
  allow-listed tools, and the MCP server — a separate process from the LLM — refuses a disallowed or
  (under `access: "read"`) mutating call before it reaches the world server. Conjuring is likewise
  gated: `/module` refuses a module outside the active agent's `dynamics` list, and every world-mutating
  route is owner-only ([specs/agents.md §4, §9.4](./specs/agents.md)).
- **Not enforced.** **Dynamic modules are trusted first-party code**, loaded into the page with every
  global available; `capabilities` and `claims` in the manifest are designed and unread, so a module
  cannot yet be told what it may touch or made to declare an exclusive resource. Nothing verifies GPU
  disposal on unload, nothing bounds the number of active modules, and there is no lint gate — because
  no path yet loads code we didn't write. The QuickJS sandbox (§9) is the answer for when one does.
- **The rest of the posture is identity-only:** usernames are trusted and unauthenticated. Deliberate
  for a friendly, co-located deployment; the consequences are enumerated in
  [backlogs/agents.md](./backlogs/agents.md).

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
· #8 desktop preview · #10 mesh-gen first mode · #12 vehicle motion (physics vs parametric). Settled
since, and load-bearing here: **#17** per-module *server* logic — deferred until a module that actually
needs it (an emitting one: a music transport, a rule engine, a shared-selection arbiter); `grab` does
not motivate it, because its server side is entirely generic and lives as a plain world-server
endpoint. **#18** whether the world server stays Python. See [decisions.md](./decisions.md);
sequencing in [roadmap.md](./roadmap.md).

**The missing middle tier of reactivity.** Three tiers exist in the design and only the outer two are
built: the **module loop** (frame rate — the module *is* the realtime agent, §2 row 4) and the **LLM**
(seconds, semantic — the choreographer, not the dancer: it sets mood, parameters and goals). Between
them belongs a lightweight declarative **rule engine** (`when beat.kick → emit flash`), which is what
would let ~80% of reactivity need neither custom code nor an LLM round-trip. Unbuilt
([backlogs/dynamics.md](./backlogs/dynamics.md)).

**First contracts to lock in Phase 0** (everything else builds on them): the **world document
schema** (§4), the **patch protocol** (§5), and the **state channel** (§3). Get a static A-Frame
scene onto the Quest over TLS, then drive it with hand-authored patches before adding the director.
