# Decisions log

Consequential forks. Each is `OPEN` until we choose; then we record the choice and the
*why*. Ordered roughly by leverage (how much rework a late change would cost).

| # | Decision | Status | Choice |
|---|---|---|---|
| 1 | Cloud vs local compute (STT/LLM/TTS/gen) | ✅ RESOLVED | Cloud-first, provider-abstracted, host-flexible |
| 2 | World representation (declarative engine vs neutral JSON) | ✅ RESOLVED | Declarative — A-Frame (ECS-as-HTML over Three.js) |
| 3 | Serving WebXR to the headset over HTTPS | ✅ RESOLVED | 3-tier: adb reverse → Caddy/DNS-01 → Tailscale |
| 4 | Asset sources & generation providers for v1 | 🔶 PARTIAL | Fetch: Poly Pizza (GLB); image-gen: Gemini; 3D-gen TBD |
| 5 | Voice activation + audio capture topology | 🔶 CONSTRAINED by #9 | Presence-aware: solo → wake-word optional (open-mic ok); shared → wake-word + PTT |
| 6 | Behavior execution split (client vs server) | 🔶 EASED by #7 | Per-behavior placement (portable runtime); default split TBD |
| 7 | Sandboxing model for LLM-authored code | ✅ RESOLVED | Capability API + unified JS in QuickJS-WASM, both sides |
| 8 | Desktop/non-headset preview approach | OPEN | — |
| 9 | Single-user now vs multiplayer-ready architecture | ✅ RESOLVED | Co-located multi-headset; remote-bridge-ready |
| 10 | Mesh/geometry generation strategy | OPEN | — |
| 11 | Capability tiers: vendor-neutral baseline, device features as extensions | ✅ RESOLVED | Quest features are opt-in extensions w/ neutral fallbacks |
| 12 | Vehicle motion: full physics engine vs parametric/arcade models | OPEN | — |
| 13 | Image procurement decoupled from scene use; capability-aware generators | ✅ RESOLVED | Procure→reference by id; generators mediated by capability |
| 14 | Per-agent persistence scoping; asset store vs world/document store | 🔶 DESIGNED | private/public namespaces; scope = capability; worlds in a separate store |
| 15 | World/space identity: is the NAME the identity, or is there a permanent id? | ✅ RESOLVED | Permanent minted id + a mutable display name (no aliases) |
| 17 | Per-module SERVER logic — does `grab` motivate a "server module"? | ✅ RESOLVED | No. grab's server side is generic → a plain endpoint; server modules wait for an emitting module |
| 18 | World server: stay Python, or port to Node for one runtime? | 🔶 DIRECTION | Endorsed but incremental — extract shared JS math first; never a big-bang port |
| 19 | `full` environment-depth occlusion | ✅ RESOLVED | Shelved — three won't consume the Quest's depth format, and `hands` already covers the sharp case |

> Numbering note: §15 appears twice (an older "Users, spaces, and a user-first namespace" section
> predates the table row for world/space identity), and §16 has a section but no table row. External
> docs reference both, so nothing has been renumbered; new entries continue from 17. Worth reconciling
> when this file gets its own pass.

---

### 1. Cloud vs local compute — ✅ RESOLVED
**Choice:** Cloud-first for v1, but the design is **extensible to local compute and to a
self-hosted remote box** the user controls at home.

**Why:** Cloud APIs get us to a working system fastest and keep the Pi viable as a thin
orchestrator. But we don't want to be locked to cloud — the user wants the option to run
heavy models offline or on a powerful home machine.

**Implications (must hold from the start):**
- **Provider abstraction layer** — STT, LLM, TTS, image-gen, 3D-gen each sit behind an
  interface with swappable backends (cloud API ↔ local ↔ self-hosted endpoint). No service
  call hard-codes a vendor.
- **Compute placement is config-driven**, not baked into topology. Three reference targets:
  (a) Pi orchestrator + all-cloud models; (b) Mac host doing some models locally; (c) Pi/Mac
  orchestrator + a self-hosted GPU box at home serving model endpoints over the LAN.
- **Services are network-decoupled** — the world server and the model services can live on
  different hosts, talking over the network, so the "powerful machine I control" can be added
  later without re-architecting.

**Phase-2 voice default (first concrete provider picks):** **local Whisper (STT) + local
Kokoro/Piper (TTS) + cloud Claude (director)** — local speech keeps audio on the Mac at near-zero
cost; cloud Claude gives reliable tool-calling; one API key to start. Fully swappable. The full
catalog of per-slot defaults and alternatives lives in [providers.md](./providers.md).

### 2. World representation — ✅ RESOLVED
**Choice:** Declarative scene graph via **A-Frame** (entity-component system expressed as
HTML-like custom elements over Three.js).

**Why:** Easiest representation for an LLM to generate and mutate; doubles as the serializable
save format; maps cleanly to entity/component edits and patches. Three.js power is still
available underneath when we need it.

**Implications:** The A-Frame scene graph *is* the world model (spec §2). Patches are
add/update/remove of entities and component attributes. Keep an eye on not leaking A-Frame
specifics into the MCP tool/behavior contracts where avoidable, so a future engine swap stays
possible — but we are not paying portability tax for it in v1.

### 3. HTTPS to the headset — ✅ RESOLVED
**Choice:** A 3-tier progression, graduated by project stage (not an either/or):

1. **Dev / single headset — `adb reverse`.** USB-connect the Quest, `adb reverse tcp:8080
   tcp:8080`, browse `http://localhost:8080` on the Quest. `localhost` is a secure context, so
   **WebXR runs with no TLS.** Best over USB (reverse over wifi is flaky). → Phase-0 unblock.
2. **Multi-headset LAN (real target) — Caddy + Let's Encrypt DNS-01.** A real hostname points
   at the server's LAN IP via a public A record; Caddy gets a real cert via the DNS-01
   challenge (no inbound ports). Real CA ⇒ **every Quest trusts it with zero per-device setup.**
   (Fallback: Caddy local-CA self-signed + install root cert on each headset — avoids needing a
   domain but is per-device fiddly.)
3. **Remote multiplayer (future) — Tailscale.** Server + remote headsets on a tailnet;
   `tailscale cert` gives valid HTTPS over the private mesh. Aligns with the remote voice
   bridge. Validate Quest's Tailscale app support when we reach it.

**Why:** Each tier removes friction at the cost of setup we don't need yet. Tier 1 gets pixels
in the headset today; Tier 2 is the standing multi-user LAN story; Tier 3 is the remote path.

### 4. Asset sources & generation providers — 🔶 PARTIAL
**Fetch source (Phase 3) — ✅ Poly Pizza.** Free API, keyword search, **low-poly GLB** (Quest-
friendly), CC-licensed, no end-user login. Needs a free developer token (`POLY_PIZZA_API_KEY`).
Other CC sources (Quaternius/Kenney CC0 packs, Sketchfab-CC, Objaverse, Poly Haven for HDRIs) can
be added later as additional content-source modules.

**Image generation (Phase 4) — ✅ Google Gemini "Nano Banana"** (`gemini-2.5-flash-image`) +
**OpenAI** (`gpt-image-1`), behind a capability-aware generator registry in the provider abstraction
(`conjure/llm.py`) so FLUX/local SD plug in. Procurement is decoupled from scene use and selection is
mediated by capability (decision #13). Gemini chosen as default for best-in-class editing + outpainting
(sets up the vision's skybox/panorama work). Needs billing. **Still open:** text/image→3D generation.

### 5. Voice activation + audio capture topology — 🔶 CONSTRAINED by #9
**Presence-aware activation:** the activation model scales with who's present.
- *Solo mode* — when the user indicates they're alone (by voice or a toggle), the **wake word is
  optional**; open-mic / always-listening is allowed since there's nothing to disambiguate.
- *Shared mode* — with others present, **wake word ("Conjure, ...") + push-to-talk** (controller
  button / pinch) is required, with an LLM intent-classifier backstop. Open-mic VAD is ruled out
  here by multi-user cross-talk (decision #9). **Safe default is shared** until the user declares solo.

**Capture topology (corrected — don't assume per-headset mics):**
- *Shared room device* (Bluetooth speakerphone) is the **expected default** for co-located use:
  one mixed stream with several voices ⇒ needs **speaker diarization** + the addressing gate.
- *Per-headset mic* is also supported and gives clean, identity-tagged per-speaker streams.
- The architecture treats capture as **configurable** and must not assume either. (Earlier draft
  wrongly assumed per-headset mics "for free" — superseded.)

Final activation form still OPEN, but the space is narrow. See spec §3 and §12.

### 6. Behavior execution split — 🔶 EASED by #7
What runs client-side (low-latency interaction) vs server-side (authoritative/persistent). The
#7 choice makes behaviors **portable** (same JS runtime both sides), so placement becomes a
per-behavior tag rather than a fork in the codebase. Still OPEN: the *default* split and which
SDK calls are available where (e.g. memory/persistence calls only resolve server-side).

### 7. Sandboxing for LLM-authored code — ✅ RESOLVED
**Choice:** A **capability-based sandbox** running a **single JS/TS behavior language inside
QuickJS-WASM on both the server and the Quest browser.**

**Why:** One Behavior SDK written once; behaviors are portable between client and server (serves
#6); identical isolation semantics everywhere; QuickJS-as-WASM has no native build and runs the
same on Quest/Pi/Mac/Linux (the Pi constraint rules out most heavier options). Geometry
generation (#10) rides the same sandbox.

**The boundary is capability-based, not just engine isolation:**
- **No ambient authority.** The sandbox gets *no* filesystem, raw network, process spawn, API
  keys, or DB on the server; *no* `window`/`document`/`fetch`/WebSocket/other-entity access in
  the browser. It receives only the injected **Behavior SDK** (subscribe to events; read provided
  state; emit world patches; timers; request asset; play audio).
- **Every effect is an *intent*, re-validated by trusted code.** Behaviors emit patches; the
  trusted world server validates them against the same guardrails the director obeys (perf
  budget, schema, permissions) before applying. Even a sandbox breach can only produce validated
  world-edits. Defense in depth.
- **Hard limits + least privilege.** Per-run CPU/instruction cap (QuickJS interrupt handler),
  memory ceiling, wall-clock timeout. Behaviors *declare* needed capabilities; host grants the
  minimum and surfaces sensitive ones to the user. Same trust model scopes modules.
- **Static gate.** Director-generated code passes a lint/review gate before first run.

**Runtimes:** browser = `quickjs-emscripten`; server = QuickJS embedded in the Python world
server (a `quickjs` Python binding, or `quickjs.wasm` under `wasmtime-py` for double isolation),
run as a separate sandboxed unit reachable only via the narrow capability RPC.

**Still to design (implementation, not a fork):** the exact Behavior SDK surface, the capability
manifest format, and the patch-validation rules.

### 8. Desktop preview
A flat or WebXR-emulated browser view for iterating without the headset. Cheap to keep in
mind early, painful to retrofit. Likely "just load the same WebXR page; it falls back to a
flat 3D view when no XR device is present."

### 9. Single-user vs multiplayer-ready — ✅ RESOLVED
**Choice:** Build for **co-located multi-user from the start** — multiple people in the same
physical room, each with their own headset, sharing one world. Architecture must also be
**ready for a future remote voice bridge** (truly remote participants). The director LLM must
distinguish **"someone is addressing Conjure" vs. "people in the room talking to each other."**

**Why:** The user's core use case is shared, in-room creation. Retrofitting multi-user onto a
single-user state/authority model is a rewrite; the addressing requirement also reshapes the
voice design, so it can't be deferred.

**Implications (baked into spec §12):**
- **Server-authoritative shared world state**, multiple connected clients; world patches
  broadcast to all headsets (reliable, persistent channel).
- **Presence sync** — each user's head/hand poses + voice streamed as transient per-user state
  at high rate (separate, lossy-ok channel; networked avatars). Distinct from world patches.
- **Co-location alignment** — use the **Quest browser's native colocated WebXR** ("Shared
  Spaces", Horizon OS browser v39+, built on Shared Spatial Anchors): headsets in a room share
  a common origin automatically. ⚠️ Quest-only, experimental, and the shared space is lost when
  the last participant leaves — track as a dependency/risk; have a manual-recalibration
  fallback.
- **Addressing gate** — an "addressed-to-Conjure" gate (wake word / PTT, see #5) decides what
  reaches the director; diarization or per-stream identity tells it *who* spoke. Audio may come
  from a shared room device or per-headset mics (#5) — the gate works for both.
- **Remote-ready transport** — prefer a server relay for world + presence (uniform for LAN and
  WAN) over pure peer-to-peer, so the future remote voice bridge slots in over Tailscale/tunnel
  (#3 tier 3) without re-architecting. The Meta colocation sample uses PeerJS P2P — fine for
  presence on LAN, but world authority stays server-side.

---

### 10. Mesh / geometry generation strategy — ❓ OPEN
**Need:** generate meshes when no fetched or text-to-3D asset fits.

**Options (likely all three, tiered):**
- *Procedural / parametric via code* — director or behavior emits geometry-building code
  (Three.js `BufferGeometry` client-side, or Python `trimesh`/`numpy` → glTF server-side). Best
  for regular/parametric shapes. **Runs in the sandbox (#7)** — geometry code is LLM-authored,
  so this is coupled to the sandboxing decision.
- *Dedicated generator agent/model* — a specially-prompted or fine-tuned text/image→3D model
  for organic/complex meshes, behind the provider abstraction (#1).
- *Primitive composition* — always-available fallback from A-Frame primitives.

**Execution boundary settled (#7):** geometry code runs in the same QuickJS-WASM capability
sandbox as behaviors, emitting geometry via the SDK with no I/O. **Still open:** which generation
mode to build first and the quality bar.

### 11. Capability tiers: vendor-neutral baseline, device features as extensions — ✅ RESOLVED
**Choice (user directive):** Core functionality must run on **any WebXR device**. Device-specific
capabilities (Quest "Shared Spaces" co-location, certain hand-tracking/passthrough specifics) are
**opt-in extensions** that enhance but never *gate* the baseline; each extension declares a
**neutral fallback**.

**Why:** Avoid lock-in to one vendor's experimental features; keep the experience usable as
hardware/standards evolve. Co-location, for instance, prefers Shared Spaces but degrades to
marker/QR or manual calibration.

**Implications:**
- A **capability-detection layer** in the client: query what the device/session supports, light
  up extensions when present, fall back otherwise.
- Both **`immersive-vr` and `immersive-ar`** session modes are baseline; a **flat** mode covers
  non-XR browsers (desktop preview, #8).
- Module/tool schemas stay engine-agnostic where practical so the world layer never hard-depends
  on a device feature (see spec §13 Extensibility).

### 12. Vehicle motion: full physics vs parametric/arcade models — ❓ OPEN
**Context:** Embodiment-by-occupancy (spec §7) lets the user occupy vehicles with distinct motion:
car, tank, plane, hot-air balloon, boat. Each needs a motion model + control scheme.

**Options:**
- *Parametric / arcade models* — hand-tuned kinematics per vehicle type (steering curves, lift,
  buoyancy, wind drift). Predictable, cheap, comfort-friendly, easy for the director to spawn.
- *Full physics engine* (browser: Rapier / Ammo / Cannon) — rigid bodies, collisions, terrain
  contact. Richer/emergent, heavier, harder to keep comfortable and within the perf budget.
- *Hybrid* — physics for contact/collision, parametric overlays for flight/buoyancy "feel".

**Coupled sub-questions:** built-in motion-model registry vs LLM-authored models in the QuickJS
sandbox (#7); **diegetic vs abstract controls** (grab-the-yoke vs thumbstick); motion authority &
sync in multi-user (spec §12). Lean: start with **parametric models + a registry**, add physics
where it earns its keep; custom models ride the sandbox. Decide at the vehicles phase.

### 13. Image procurement decoupled from scene use; capability-aware generators — ✅ RESOLVED
**Choice (user directive):** Getting an image and using it in the scene are **separate concerns**.
MCP **procurement** tools (`generate_image`, `generate_skybox_image`, `edit_image`, `outpaint_image`,
`skybox_from_image`) produce/transform an image and return an opaque **image id**; **scene** tools
(`place_image`, `set_skybox`) take an id. Image generation joins the **provider abstraction**
(`conjure/llm.py`): each generator declares `ImageCapabilities`; the world server **mediates**
selection — the LLM can `list_image_generators`, name one explicitly (clear error if incapable), or
omit it for a hard-coded **best default per op** (Gemini for all; **transparency → OpenAI**).

**Why:** Future image **sources** (web, filesystem) and **uses** (texturing an object) plug in without
each re-implementing "how do I get an image." Capabilities differ materially (Gemini: prompt-edit,
free aspect, 4K, outpaint/skybox; OpenAI: mask edit, fixed ≤1536, **transparency**), so exposing them
lets the director and the mediator choose well.

**Implications / nuances:**
- **Hybrid edit surface:** keep one-shot, entity-keyed in-scene editors (`edit_scene_image`,
  `widen_scene_image`, `skybox_from_scene_image`) for the common voice case — they procure+apply
  server-side in a single director round-trip; the id-based procurement tools enable the general flow.
- An in-memory **image store** (id → bytes/dims/provenance) over the content-addressed cache; entities
  carry `meta.image_id`. Names/roles for the roster + generators live in **one place** (`ROSTER`).

---

### 14. Per-agent persistence scoping; asset store vs world/document store — 🔶 DESIGNED
**Choice:** A single **persistence service, scoped per agent**, hosting several **typed** stores.
Agents work inside `private/<agent>/…` and never see each other's private content; `public/<agent>/…`
is world-readable (documented, not built). **Worlds are a separate document store**, not the asset
catalog. Full design in **`docs/specs/agents.md`**; the asset store is `asset-library-plan.md`.

**Why:**
- **Scope = capability, not a parameter.** The runtime binds each agent's scope and injects it
  server-side; LLM-visible tools have no scope arg, so a prompt-injection can't name another scope.
  Rides on the capability model (#7).
- **Different data, different stores.** Assets are content-addressed, immutable, semantically
  searchable media (embeddings central). Worlds are *named, mutable, versioned documents* that
  *reference* assets — an evolution of `WorldStore` (no embeddings). Cramming worlds into the media
  catalog is the same category error as the cache/NAS split.
- **Public = reference in place** (copy-to-private to pin). Content-addressing makes bytes immutable,
  so a public reference can't be silently mutated; copy-to-private guards only against unpublish /
  metadata drift.

**Implications (seams now; enforcement later):**
- Add a `scope` field to the asset catalog when Phase 2 touches the schema (cheap data seam).
- Defer enforcement (scoped handles + capability injection), the world store (named save/load/version),
  public visibility, and any `state` KV store until the **second agent** actually lands — building them
  before that is speculative.

### 15. Users, spaces, and a user-first namespace — 🔶 DESIGNED
**Choice:** Introduce primitive **users** (a username, no auth) and first-class **spaces** (a physical
environment — captured surfaces + boundary — owned by a user, with an approximate geolocation; the
owner's headset is the capture authority). Rework the namespace to **user-first** —
`/<user>/[agents/<agent>/]<category>/<name>` — and make **visibility a per-item `public` flag, not a
path segment**. Worlds associate with a space (or `<void>`). Default public; visibility inherits the
active world. A second user may **join a public world** and co-locate. Full plan in
**`docs/specs/spaces.md`**.

**Why:**
- **A "space" is the shared room layer made first-class** (resolves the per-world-room duplication and
  the class of "live state frozen in the durable world doc" bugs — stale rooms, re-capture churn, the
  authority lockout). Authority = space owner falls out cleanly.
- **Visibility as a flag, not a path segment:** content-addressed assets can't path-encode it;
  publishing must not relocate an item or break references; access is a clean predicate
  (`owner == requester OR public`). (HIGH-in-path was considered and rejected.)
- **Co-location needs no platform anchor:** both headsets register their own planes onto the *same*
  space geometry (the §register vote), so content co-locates without Quest "Shared Spaces." The real
  cost is matcher robustness for partial/extra planes.
- **Geolocation** (confirmed available in the Quest browser, ~hundreds of feet) is a coarse prefilter
  for "which space am I in"; the geometry-registration vote is the fine discriminator.

**Implications:** supersedes #14's agent-first namespace; migration moves existing `private/builder`
data to user `daniel` and extracts the embedded room surfaces into `daniel`'s first space. Phased
build (5 phases) in the plan; Phase 1 (users + namespace + migration) is foundational and user-invisible.

### 16. Capture solve off the render thread; gated + sliced render — ✅ RESOLVED
**Choice:** Keep the ~0.5 Hz room capture from ever landing heavy work in a single render frame. Move
`RoomSnap.register` into a **Web Worker** (the render applies on its reply); split the apply-gate into
**pose vs shape** so tracking drift re-lays only cheap transforms; gate the per-surface **styling** to run
only on change; and **time-slice** the mesh re-triangulation across frames under a per-frame budget
(`--geo-slice-ms`). Full mechanism in **`docs/specs/spaces-geometry.md` §14**.

**Why:**
- The capture ran ~22 ms synchronously in one `tick`, blowing the 90 Hz / 11.1 ms budget **every ~2 s**. The
  compositor reprojected the dropped frame; under head translation that's the ~1 cm **walking jitter**
  (`docs/private-notes.md`). Probes (`--debug-jitter`) proved our transforms never moved across the flick —
  it was purely a dropped frame, so the fix is frame-budget, not geometry.
- Same principle as the worker throughout: **cap per-frame cost, absorb load as latency, never a dropped
  frame** — which also buys headroom as scenes grow (the solve/rebuild cost decouples from frame rate).

**Implications:**
- New client files `room-worker.js` + vendored `three.module.min.js`; `?v=`-cache-busted like page scripts.
  Synchronous fallback if the worker can't start (`window.CONJURE_WORKER=false` forces it) — no hard
  dependency on worker support.
- New knobs: `--geo-slice-ms` (slice budget; `<=0` = off) and `--debug-jitter` (probes without the heavy
  registration diagnostics). Probes are flag-gated and kept in-tree for the next perf pass.
- **Scaling is partial** (§14.3): the solve axis and the geometry-rebuild axis are decoupled from frame rate,
  but **element creation** (first lay) and **`matchRef`** still grow with room size on the main thread — the
  next levers if large rooms hitch. Element creation would need its own slicing; `matchRef` would migrate
  behind the same worker message boundary.


### 15. World & space identity — ✅ RESOLVED
**Choice:** A world's identity is a permanent minted `wld_…` **id**; its **name** is display text a
person can change freely. A space's id is its existing auto-minted key (`space-1`) and it gains a name it
never had. Renaming is a metadata edit; there is **no alias/redirect table**.

**Why:** the name was the identity — it was the filename, the active pointer, the session's
`active_world`, a space's `last_world`, and `environment.space` in other users' worlds. Renaming
therefore stranded references, including two we can't reach at all: schema-free agent state (`StateStore`
"doesn't know a `map` from an `inventory`, which is the whole point") and `environment.space` inside
**another user's** world, which we may not rewrite.

The first design was a move+alias scheme: keep names as identity, patch the breakage with a redirect
table. It was rejected because every question it raised — chain collapsing, name reuse, alias expiry,
what `delete` on an alias means — existed *only because aliases existed*, the table grows forever, and it
still couldn't fix a client-side bug (`conjure-client.js` keyed the room-capture frame on the world
*name*, so a rename read as a world switch and reset the frame). An id removes the cause instead of
patching the symptom, and is correct on the second rename as well as the first.

Decisive evidence: **sessions already worked this way** — `SessionRepository`: "the session id is a
stable, safe segment; the mutable human title lives in the meta doc, so a rename is a metadata edit that
moves nothing." Worlds and spaces were simply inconsistent with it. World docs also already carried `id`
and `name` fields — every world on disk said `id='world_holodeck', name='Holodeck'`, copied from the seed
template, never updated, and read by nothing.

**Implications:**
- Files are `<id>.json`, flat. **Hierarchical world names are retired** — sessions are the grouping now,
  and "is this subdirectory also a world?" had no good answer. Nesting was unused in real data.
- Display names are **unique within a session** (slug-compared), which keeps name→id resolution total, so
  a person or an agent can keep saying "the meadow". Uniqueness ≠ immutability; the id is what's stable.
- Everything that stores a reference stores the **id**; everything a human reads shows the **name**.
  Shell paths address by name (nothing persists a shell path). `/state` reports both.
- The agent is handed `{id, name}` pairs and told to store the id; tools accept either. We can't *force*
  a schema-free state doc to hold ids — but a lazy agent is no worse off than before, and a correct one
  is now bulletproof.
- **Accepted trade:** a stale *human* reference now fails loudly (`switch_world("old-name")` → "no world")
  instead of silently resolving. That's the right side of the trade: ids protect the machine references
  that corrupt state quietly; a person's stale name is recoverable.
- One-time migration `migrate_worlds_to_ids` (idempotent, runs at boot) re-keys worlds and rewrites the
  active pointers, `session.json`'s `active_world`, and each space's `last_world`. It also heals
  `active_world` values that were already dangling in real data.


### 17. Per-module server logic — does `grab` motivate a "server module"? — ✅ RESOLVED
**Choice:** No. `grab`'s server side is 100% generic — authorize (owner), apply, recompute
`meta.surface_offset`, persist, broadcast — so it lives as a plain world-server endpoint (`POST
/manipulate`), not module code. Per-module server logic stays a deliberate future track, to be designed
*by* the module that actually needs it.

**Why:** the reciprocal idea (a module having a server half) is real and in the plan's DNA — the music
transport, the rule engine, semantic-cue emitters. But nothing in grab is grab-specific on the server, so
building the framework around it would mean generalizing from a case that exercises none of the hard
parts. The genuine driver is an **autonomous/emitting** module (music, rules, a shared-selection
arbiter), which needs bidirectional bus access and a server-side tick — neither of which grab wants.
Retrofitting that onto grab later is worse than designing it with its real first client.

Alignments already made, so the eventual design starts from them:
- **The world server stays the single authority and store.** A server module is *behavior operating on
  the world store through a constrained capability object* — the server mirror of the client capability
  (`apply_patch`, `broadcast`, `clock`, `store` reads, `on_event`/`emit` on the ws bus, an optional
  `tick`) — **not** a parallel database.
- **Runtime-agnostic contract.** `input → ops/events`, so the same manifest (`module.json` gains an
  optional `server` entry plus `runtime`) can be hosted by Python now and JS later.
- **Two channels, by purpose.** Authoritative tier-C commits go over **HTTP** (owner-gated, persisted,
  request/response — what grab uses). Reactive/autonomous behavior goes over the **ws bus**, which today
  only relays to peers; a server module would make it bidirectional.

**Also deliberately not server-side: snapping and clamping.** They are live-feedback concerns — you must
see it snap *as you drag*. A server-commit snap would hop after release.

### 18. World server: stay Python, or port to Node? — 🔶 DIRECTION
**Choice:** The direction is endorsed but strictly **incremental**, and the first step does not involve
Node at all. Never a big-bang port.

**Why the prize is real:** one runtime kills the server↔client geometry-math duplication.
`_face_room` / `_plane_basis` / `_fit_extent` / `_surface_offset` / quaternion+YXZ-euler all shadow JS in
`room-snap.js` / `world-model.js` / `plane-anchor.js`, and that duplication is the source of a whole class
of parity bugs — YXZ order, quat→euler, the boundary frame-flip, normals-outward. It would also give
module authors one language for both halves.

**Why not a big-bang port:** the ~4000-line world server holds co-location registration, the apply-gate,
and spaces/sessions/admission — subtle, working, hard-won code. Porting it wholesale is high risk for low
marginal return.

**Two constraints that shape any plan:**
- **"In-process + Node" is a contradiction.** CPython cannot host Node in-process. The choices are (a) an
  *embedded JS engine* (py_mini_racer / quickjs / pythonmonkey) — in-process, runs **pure** JS (three.js
  math is pure JS ✓) but has **no Node APIs** (fs/net/timers); or (b) a *real Node sidecar* — full
  ecosystem and autonomous behavior, but a separate process. Pure-compute server modules (grab-shaped:
  inputs → ops) fit (a); autonomous/networked ones (music) need (b).
- **Python anchors must relocate first:** SigLIP embeddings (torch/transformers) and `trimesh` (GLB
  bounds), both in-process with the asset library. Image/caption SDKs have JS equivalents; embeddings do
  not, cleanly — decide their home (agent server? a Python sidecar?) before porting anything.

**The path:** (1) extract the shared geometry/placement math into one **pure-JS module** the client uses
and the server consumes — this is where the recurring pain actually is, and it de-risks everything;
(2) relocate the Python anchors; (3) introduce Node **at the module seam** (server modules in JS) and
prove the symmetry there; (4) port the core wholesale only once it is mostly plumbing and the anchors are
gone. The math-dedup prize is captured by step 1 alone.

### 19. `full` environment-depth occlusion — ✅ RESOLVED (shelved)
**Choice:** Ship `off` / `hands` / `hands-solid`. Shelve `full` (occluding all real geometry via the Quest
depth sensor).

**Why:** three findings from on-device investigation, then a value judgement.
- **The Quest does provide the data.** Requesting WebXR depth-sensing yields `depthUsage=gpu-optimized`,
  `depthDataFormat=unsigned-short`. It is genuinely possible on the hardware.
- **three's built-in occlusion does not consume it.** `renderer.xr.hasDepthSensing()` stayed `false` —
  three's depth mesh expects a `luminance-alpha` `sampler2DArray`, and the Quest's `unsigned-short`
  per-view delivery is not accepted, so three never builds the texture or renders the mesh. We cannot ride
  three's built-in path here.
- **Ordering would break it anyway.** three renders its depth mesh *before* A-Frame's scene render, which
  clears depth (`autoClearDepth = true`) — so even with a valid texture it would be wiped without further
  intervention. (The hand mesh sidesteps this by living in the scene graph.)

Given that, `full` means writing device-specific WebGL ourselves, and: (1) it cannot be unit-tested —
every pass needs a headset round trip; (2) environment depth is low-res and roughly one frame laggy, so
edges are inherently blocky and shimmering, unlike the sharp hand mesh; (3) hands are already covered
sharply by `hands`, so full's marginal value is only *moving real things* — people, pets, held objects.

**Cheaper alternative recorded:** render the `mesh-detection` captured room mesh (walls + furniture) as
depth-only occluders — sharp, stable, no depth sensor, but static only. Pairs well with `hands`. A better
next step than `full` if static-furniture occlusion is the goal. See
[`docs/backlogs/occlusion.md`](./backlogs/occlusion.md).

### 20. Co-location may change the live agent — ✅ RESOLVED (kept, made audible)
**Choice:** A space match keeps its authority to move the live scope across the agent boundary. It now
announces itself instead of happening in silence.

**The behaviour.** A world belongs to a session, a session to an agent, and a space remembers the last
world opened in it (`last_scope`/`last_world`). So when an AR client votes its capture and matches a
room, `/space/select` joins that room's last world — and `_switch_to` writes the global session pointer,
which the agent server follows by re-binding its Director. Put the headset on in a room whose space was
last used by `builder`, and you are now talking to `builder`, whatever you were talking to before.

**Observed:** mid-session in the `outdoor` agent, a restart plus a headset reload landed in `builder`
and `animal-house` twelve seconds after page load, with nothing said about it. Two rejected fixes:
refuse to cross the agent boundary on a match, and skip selection entirely while the active world is
VOID. Both break the thing the design is for — *your room, your world* — to avoid a surprise that is
really a reporting failure. The room genuinely is the more authoritative signal about where a person is;
it just has to say so.

**So:** `_reconcile_state` broadcasts `[now in the <agent> agent — <world> · <space>]` on any agent
change it did not itself initiate (a client's own `agent <name>` already narrates, and sets
`expect_agent` to claim the echo). `notice` is spoken by the voice client and shown in the CLI, so the
switch is audible on the same channel the person is already using. Naming the world and the space is
what makes a room match recognisable *as* a room match, since the state carries no reason field.

**Related, and separate:** this is also what exposed the dynamic-module loading bug — the page had been
served under `outdoor`, which declares no `dynamics`, so it carried no module scripts and rendered
`animal-house`'s `grab`/`water` entities as inert attributes. Module `<script>`s are no longer scoped to
the active agent; see [`docs/specs/dynamics.md`](./specs/dynamics.md) §9.
