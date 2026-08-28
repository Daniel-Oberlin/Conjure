# Architecture (v1 design)

> Status: **real design, v1 draft.** This defines the concrete contracts and runtime shape that
> implement [vision.md](./vision.md) under the decisions in [decisions.md](./decisions.md). Schemas
> are illustrative (JSON-ish) and meant to be firmed up into typed definitions during Phase 0.
> Firmness is flagged per section: 🟢 firm · 🟡 shape-firm, details open · 🔴 sketch.
>
> **This is the only document that spans subsystems and marks built vs. unbuilt.** Where a subsystem
> now has a living spec in [`specs/`](./specs/), that spec is authoritative for behaviour and this one
> keeps only the cross-cutting shape and a link. A 🔴 here means *designed and absent* — checked
> against the code, not assumed.

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
- **Degrade to the next-broadest thing that is still true, never to a global default.** When a pointer
  names something that is gone — a deleted world, a purged session — the user's intent usually survives
  it: *put me back with the agent I was using, in the space I am standing in*. Falling back to a
  general-purpose default throws away facts that are still good.

  The ladder is the same at every level, and it has **three** rungs, not two: *the thing you were last
  in* → *any surviving sibling* → *build a new one*. So: world gone → the world you were in before it,
  else any other world in that session, else that agent's freshly-built opening; session gone → the
  session before it, else any other session **in the same agent**, else a new one there; the default
  agent only when nothing better is knowable ([specs/spaces.md §6.1](./specs/spaces.md)).

  The middle rung is the one that gets dropped. Written as two rungs, this principle was implemented as
  two — "no pointer" read as "never been here", so deleting the world you were last in minted a fresh
  one while its siblings sat untouched in the same session (and the session level had the identical
  defect, hidden because its fallback happened to name a session id that usually existed). The pointer
  is therefore an **MRU list** rather than a single id: rung 1 walks past however many of its entries
  have been deleted, which is what makes it a rung and not a coin flip
  ([specs/agents.md §7.6](./specs/agents.md)).
- **Every degradation is audible.** A fallback that fires silently is indistinguishable from a bug, and
  is how a cluster of world-entry defects survived unnoticed for months. Whenever the system hands you
  something other than what was asked for, it says so in one line — on the same channel that already
  announces an agent change nobody asked for (decision #20).
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
| 6 | **Memory** | host | World store, **space store**, asset store, vector index, connection graph, sessions. A **space** is the persistent record of a real physical environment — surfaces, boundary, geolocation, owner — shared across a user's worlds rather than copied into each ([specs/spaces.md](./specs/spaces.md)) |
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
    "public": true,                                         // visibility is a FLAG, never a path segment
    "space":  "daniel/space-1",                             // WHICH space this world is tied to, or "<void>"
    "sky":    { "type": "hdri", "asset": "sha256:…" },      // how this world presents the sky
    "passthrough": false,                                   // AR camera on/off — immersion axis 1
    "boundary": { … },                                      // active space's floor polygon — LIVE only
    "captureAuthority": "hs_a1b2",                          // headset allowed to report geometry — LIVE only
    "spacePresentation": {                                  // how this world presents the SPACE
      "active": true, "defaultSurfaceVisible": false,
      "surfaceStyles": { "real_wall_3": {…} },              // per-surface material DELTAS vs the space's base
      "edgesVisible": true, "annotations": false }
    // 🔴 not built: lighting presets, fog, per-world gravity, ambientAudio
  },
  "entities": [ /* Entity[] */ ],
  "connections": [ { "portal": "ent_door1", "target": "world_cabin" } ]   // 🔴 schema only — no consumer
}
```

**Entity** — transforms in **meters** (A-Frame rotation in degrees):

```jsonc
{
  "id": "ent_campfire_1",
  "parent": null,                       // entity id; placement vs the real world is meta.placement, below
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
  "meta": { "license": "CC-BY", "attribution": "…", "source": "polypizza", "generated": false,
            "placement": "grounded",                // grounded | free | on-surface | skybox
            "anchor": { /* plane-relative: ids + signed offsets + per-wall quaternion votes */ } }
}
```

`meta.placement` fixes **both** how an entity's position and its orientation are solved, as one
consistent choice — a grounded model is floor-seated and upright, a free one keeps its full quaternion.
`meta.anchor` is the plane-relative anchor each client re-solves against its own walls
([specs/spaces-geometry.md §5](./specs/spaces-geometry.md)).

Invariants: fully serializable & restorable; every entity has a stable id; component set is
open/extensible; nothing is stored in absolute real-world coordinates — every placed entity is
on-surface, grounded, free, or skybox.

**A real room surface is an ordinary entity**, tagged `meta.real`. There is no separate room-rendering
path: a captured wall carries a `surface` component (polygon, extent, holes) and a `material`, so it
flows through patches, broadcast and the director's material edits like anything else. `meta.real` is
the contract — restyle, hide, texture and mount onto it; never move or remove it
([specs/worlds-surfaces.md](./specs/worlds-surfaces.md)).

**The world document is composed, not stored, where the space is concerned.** Geometry lives once in the
space; a world stores only its per-surface material *deltas*. The live in-memory doc is always fully
composed so client, patch and director paths are unchanged; only persistence splits
([specs/spaces.md §4.1](./specs/spaces.md)).

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
    { "op": "env",    "set": { "environment.fog.density": 0.05 } }
  ],
  "inverse": [ … ]                   // server-computed inverse ops (see undo/redo below)
}
```

**Four ops exist** — `add`, `remove`, `update`, `env` (`world.py:194`); anything else raises
`unknown op`. The `occupy` / `exit` ops that embodiment (vision §7) implies are **not built** and are
not reserved anywhere.

- **Authority & ordering:** the **world server** is the only writer. It assigns `rev`, computes the
  `inverse`, applies, then broadcasts. Conflicts resolve by server order (last-writer-by-rev). 🟢
- **Validation gate** 🔴 **not built.** The design is: every patch, regardless of origin, checked
  schema-valid, within performance **budget** (§4), permitted for the origin's capabilities, with
  resolvable asset references; rejects dropped with a logged reason and the originator notified. The
  real `WorldStore._validate` (`world.py:239`) says *"Placeholder"* in its own docstring and checks
  only that each op carries an `"op"` key. Nothing enforces the budget anywhere. This is the single
  largest gap between this document and the code, and it is load-bearing for §13: "every effect is a
  validated patch intent" is the whole containment story for a future sandbox.
- **Undo/redo** 🔴 **machinery without a consumer.** The `inverse` genuinely is computed per patch and
  `WorldStore.history` accumulates in memory — but nothing reads either. There is no undo tool, no
  endpoint, no shell verb, and history is not persisted, so it does not survive a restart. Snapshots
  and `revert_to` do not exist. Treat the patch as the unit of live sync today, and of undo only in
  design.
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
- **Real geometry: local-first, and never broadcast.** The Quest's map of a multi-room space is
  **locally non-rigid** — measured up to ~9 cm of regional disagreement between sessions, which no single
  rigid transform reconciles. So the server holds the *semantic* model (ids, semantics, styling) plus a
  **seed** constellation, and **every client renders its own live capture**. Geometry ops update the seed
  and are deliberately not broadcast; only what clients consume goes out (env, boundary, on-surface
  re-anchors). `#world-root` is held at **identity** for a captured space
  ([specs/spaces-geometry.md](./specs/spaces-geometry.md)).
- **Co-location** (multi-user, decision #9): **no platform shared-anchor** — there is no Quest "Shared
  Spaces" dependency and no shared render frame. A guest registers its own detected planes against the
  space's stored constellation; registration's useful output is **id correspondence**, not a coordinate
  transform. Free content, skybox and avatars are placed by **plane-relative anchors** re-solved against
  each client's own walls. So content lands on the same *physical* wall for everyone while the coordinate
  numbers differ by centimetres — physical consistency is preserved, coordinate agreement is neither
  achieved nor needed.

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

## 8. MCP tool surface (world server)  🟢 built · 🔴 designed

The director's action vocabulary (vision §4). **The authoritative list is not here** — it is
`agents/builder/agent.json`, which a test pins equal to every `@mcp.tool` in `mcp_server.py` minus the
control tool `set_caller`, so a new tool cannot go silently un-granted ([specs/agents.md
§3](./specs/agents.md)). What follows is the shape, not the roster; check the file for names.

**Built — 45 tools**, in these groups:

- **World & session navigation:** `query_world`, `query_room`, `view_relative`, `list_worlds`,
  `new_world`, `switch_world`, `delete_world`, `reset_world`, `set_world_visibility`,
  `set_space_visibility`
- **Entities:** `add_entity`, `update_entity`, `move_entity`, `remove_entity`, `set_environment`
- **Real surfaces** (a captured room is ordinary entities, §4): `show_surface`, `texture_surface`,
  `style_surface`, `show_edges`, `style_edges`, `show_annotations`, `style_annotations`,
  `set_immersion`, `realign_room`
- **Library:** `place_asset`, `place_cached_asset`, `search_library`, `query_assets`, `update_asset`,
  `delete_asset`
- **Images — procurement decoupled from scene use (decision #13):** *procure* (return an opaque
  image id) `generate_image`, `generate_skybox_image`, `generate_grounded_skybox_image`,
  `edit_image(image_id)`, `outpaint_image(image_id)`, `skybox_from_image(image_id)`,
  `list_image_generators`; *use in scene*
  `place_image(image_id)`, `set_skybox(image_id)`, `set_grounded_skybox(image_id)` (projects the
  ground at the viewer's feet so they stand *in* the scene rather than under it);
  *one-shot in-scene edits* (entity-keyed, procure
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
Each tool returns the resulting `rev` and a summary so the director stays in sync.

**Designed, not built** 🔴 — no stub, no name reserved, nothing to call:

| Group | Tools | Blocked on |
|---|---|---|
| Behavior & geometry | `attach_behavior`, `remove_behavior`, `generate_mesh` | the sandbox (§9, decision #10) |
| Embodiment | `spawn_vehicle`, `set_avatar`, `occupy`, `exit`, `set_motion_model`, `set_control_scheme`, `bind_input` | motion models + the `occupy`/`exit` patch ops (§5) |
| Connections | `create_portal` | `connections` is a schema field with no consumer (§4) |
| History | `undo`, `redo`, `snapshot`, `revert_to` | nothing reads the computed `inverse` (§5) |

> Note the naming that did *not* survive contact. There is no `create_world` / `load_world` /
> `save_world`: worlds are `new_world` / `switch_world` and **autosave** — an explicit save verb never
> made sense once the live doc became the source of truth. There is no `generate_asset` either;
> procurement split by medium (decision #13). Earlier revisions of this section listed the wished-for
> names as though they existed.

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

- **Resolve** 🟢 — cache hit by content hash, else fetch. **Poly Pizza is the one wired source**
  (`AssetResolver`, `conjure/assets.py:48` — "searches Poly Pizza, downloads the best low-poly GLB");
  the rest of vision §5's list is unwired, and generators are a separate path (images, §8).
- **Convert** 🔴 **not built.** Nothing converts anything. `ModelImporter` accepts **`.glb` only**,
  confirmed by the `glTF` magic bytes, and rejects everything else — so OBJ/FBX/USD/STL never enter the
  pipeline at all. Blender, assimp and gltf-transform are design names with no call site; the one
  "Blender" string in the tree is a docstring aside meaning *a `.glb` you may have exported from it*.
  `trimesh` is loaded lazily in two places (`importer.py:131`, `assets.py:37`) purely to **read**
  bbox/tris for the catalog — never to export.
- **Process (pluggable ops)** 🟡 — **outpainting/extrapolation is built** (`outpaint_image`,
  `widen_scene_image`, `skybox_from_image` — photo → seamless skybox / 360 / panorama), as are
  arbitrary prompt edits (`edit_image`). **Up-res / super-resolution is not built** — no upscaling path
  exists. Derived images get their own descriptor and content hash.
- **Optimize to budget** 🔴 **not built** — Draco/meshopt compression, texture downscale and LOD are
  all absent, and there is no budget to fit to (§5, the validation gate). Assets are cached and served
  exactly as fetched or generated, which is why "download the best **low-poly** GLB" is doing the work
  a whole optimize stage was meant to.
- **Cache** 🟢 — content-addressed blob store; dedup across worlds.

**Asset descriptor** (in memory; referenced by hash from entities):

```jsonc
{ "hash": "sha256:…", "kind": "model|image|stereo-image|360-image|audio|hdri|stream",
  "format": "glb", "media": { "stereo": "sbs|vr180|null", "projection": "equirect|null" },
  "license": { "id": "CC-BY", "attribution": "…", "url": "…" }, "source": "polypizza|gen:meshy|module:nas",
  "optimized": { "draco": true, "lod": [0,1,2], "texMB": 4 }, "tris": 1200 }
```

Media types are first-class (stereo/360, vision §5); `kind:"stream"` is the forward-compat hook for
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

**What exists** 🟢 — `agents/servers.json`, and it is only a launch table:

```jsonc
{ "world": { "command": "python", "args": ["-m", "conjure.mcp_server"],
             "env": { "CONJURE_URL": "${world_url}" } } }
```

**One entry, and v1 launches exactly one server per agent** (`Director.connect` raises otherwise). All
the scoping that exists is on the *agent* side — `access` plus an opt-in `tools` allow-list, enforced
twice (§13) — not declared by the module.

**Module manifest** 🔴 **designed, unread.** None of these fields is parsed by `load_server_registry`:

```jsonc
{ "name": "nas-photos", "kind": "content-source|experience|capability|input-provider",
  "tools": [ … MCP tool schemas … ],
  "permissions": ["read"],                 // NOT world-write/code-exec unless explicitly granted
  "streams": false,                        // future: { "kind":"webrtc", "input": true }
}
```

Intended trust: a module's permissions are scoped (decision #7); content/engine modules can't write
world state or run code unless granted. Example future *experience* module: a remote-desktop streamer
(streams: webrtc + input) rendering onto a `kind:"stream"` surface. Until a second module exists there
is nothing for the manifest to discriminate, which is why it stays unbuilt.

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
- **Space store** — one record per real physical environment: surfaces, boundary, geolocation, owner,
  visibility, and a return-visit pointer to the last world used in it. **User-owned and
  agent-agnostic** — the room belongs to the person who captured it, not to an agent — and shared across
  all their worlds, so re-styling a wall in one world never touches the geometry or another world. Also
  the server's own solver geometry: pose-relative queries ("the wall I'm looking at") run against the
  seed. See [specs/spaces.md](./specs/spaces.md).
- **Asset store** — content-addressed blobs + descriptors (§10).
- **Vector index** — **over assets, not worlds.** `library.py` loads the `sqlite-vec` extension and
  creates `assets_vec` lazily at the live embedder's dimension, beside an `assets_fts` FTS5 table; a
  search blends both. There is **no** world-level embedding, so semantic world recall ("the beach
  world", vision §9) does not exist — `list_worlds` matches names loosely and that is all.
- **Connection graph** 🔴 — `connections` is a field in the world document with **zero consumers**:
  nothing writes it, reads it, or renders a portal. Schema only.
- **Session store** — a **session** is an instance of an agent: its **shared transcript** (append-only
  `transcript.jsonl`, plain `user`/`assistant` turns with a human speaker, no per-LLM attribution, §7a),
  the worlds created in it, and its agent state (`state/`, a bag of named JSON docs behind the generic
  `state_*` tools). Named, owned, persisted, switchable; visibility lives here and a world inherits it.
  One global pointer records which session is live. See
  [specs/agents.md §7](./specs/agents.md).
- **Anchor registry** (forward-compat, §15) — a persistent WebXR anchor handle per space, for reloading
  a world fixed to the same physical place across sessions. Not needed for placement or co-location,
  both of which are plane-relative today.

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
- **Cross-session persistent anchors** — a WebXR persistent-anchor handle, stored and restored, would
  let a world reload fixed to the same physical place. Note what this is *not* for: co-location needs no
  shared anchor, since a guest registers against the space's geometry instead
  ([specs/spaces-geometry.md](./specs/spaces-geometry.md)). Placement *within* a session is already
  solved differently — by **plane-relative anchors** re-solved against each client's own walls — so this
  is a persistence feature, not a placement one.
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
