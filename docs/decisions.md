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
