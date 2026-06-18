# Shell & agents — the orchestration layer 🟡 design

> Status: **design, not built.** This is the anchor we refine before writing code. It generalizes the
> current `Director` (a roster of LLMs + one MCP server + one prompt — see [architecture.md §7/§7a](./architecture.md))
> into two explicit layers: a deterministic **shell** and declarative **agents**. Nothing here ships
> until the schema and the load-bearing decisions (§9) are settled.

## 1. What an agent is — and why

An **agent** in Conjure is an **experience**: a distinct way of being in the headset, with its own
toolset and its own world. It's *less* a "do-my-tasks" AI agent and *more* a **mode of play** you step
into. Examples we're aiming at:

- **builder** — conversational world-building and editing; its purpose is to let you customize worlds
  for yourself. (This is today's `Director`, renamed.)
- **immersive DJ** — plays music and builds the room around it: album art, lyrics, fun facts, and an
  environment that suits the track.
- **dungeon master** — runs a role-playing game, generating immersive content and environment live as
  the story unfolds.
- **planetarium operator** — moves the sky view and shows immersive stellar visuals.

Each agent owns a **segregated world** (§3b) and a **scoped toolset**, and may host **personas** (§3a) —
characters who *participate* in the experience without operating it. Today one hardcoded thing — the
`Director` — fuses a prompt, the LLMs that may run it, and the world tools it can call, against a single
shared world, with no scoping. Generalizing it buys us:

1. **Distinct experiences, least privilege.** Each agent gets exactly the tools its experience needs — a
   planetarium can't rearrange furniture; an inspector can only read.
2. **Reliable control.** "Switch to the dungeon master", "reset", "save this world" must not hinge on an
   LLM parsing intent — they belong in a deterministic **shell**.

So three layers: a **shell** (deterministic control plane) sits above **agents** (experiences — the
LLM-driven work plane), which draw brains from **LLMs**, tools/context from **MCP servers**, and may
hand turns to **personas**.

```
┌───────────────────────────────────────────────────┐
│  SHELL   deterministic commands, no LLM             │  conjure:shell>
│          open/exit, switch agent/llm, reset,        │
│          save/load, list, status, …                 │
└───────────────┬───────────────────────────────────┘
                │ activates ONE experience (and its world)
┌───────────────▼───────────────────────────────────┐
│  AGENT = an EXPERIENCE                               │  conjure:claude.builder>
│   • its own segregated WORLD (kept for the session) │
│   • a scoped TOOLSET (MCP servers)                  │
│   • a prompt + allowed LLMs                          │
│   builder · DJ · dungeon master · planetarium · …   │
└───┬───────────────┬──────────────────────┬─────────┘
    │ runs on        │ calls / reads         │ invokes (as a tool)
┌───▼─────────┐  ┌───▼─────────────────┐  ┌──▼──────────────────┐
│ LLMs        │  │ MCP servers          │  │ PERSONAS            │
│ (roster)    │  │ tools · resources    │  │ prompt + llm, NO    │
└─────────────┘  └──────────────────────┘  │ tools — participate │
                                           └─────────────────────┘
```

The same shell/agents stack is **front-end-agnostic** — CLI (`cli.py`) and voice (`voice.py`) both
drive it, exactly as they both drive today's `Director`.

## 2. The shell  🟡

A deterministic command interpreter. **No LLM is consulted to recognize or execute a shell command** —
that's the whole point. It owns session control: switching agents/LLMs, entering/leaving modes, and any
operation that must be reliable (reset, save/load, realign room, god-mode confirmations).

**Entering & leaving.** `conjure open shell` (said or typed) drops you into shell mode →
`conjure:shell>`. `exit` leaves it and resumes the underlying active agent.

**Two ways a command is recognized** — this is the key UX decision (and the reason for the `conjure`
wake word):

- **Inline, while talking to an agent:** only input that leads with the **`conjure` wake prefix** is
  intercepted by the shell; everything else goes to the agent. This disambiguates control from content
  — "put a **shell** on the table" reaches the builder as content, while "**conjure** open shell" is a
  command. Reliability *and* no false captures.
- **In shell mode:** the prefix isn't needed — every line is a command. Natural language that matches no
  command is rejected (not sent to an LLM), so control mode never silently "does something."

**The active context & `exit`.** The session always has an underlying active `(agent, llm)` pair (the
"work context"). The shell is a **modal overlay** on top of it. Shell commands may *reassign* that pair
(`talk to curator`, `use gemini`). `exit` simply drops the overlay and resumes whatever the active pair
now is — hence "pops you back to wherever is appropriate" (the agent you switched to, not necessarily the
one you came from). If there's no active agent yet (shell opened before any agent was chosen), `exit`
prompts to pick one (or re-opens the default). A stack generalizes this later for sub-agents; v1 is
overlay + single active pointer.

**Extensible command registry.** Commands are `(pattern → handler)` entries in a registry — a tiny
deterministic dispatcher, *not* MCP tools. Adding a command is registering a handler. Seed set:

| Command (NL-ish grammar) | Effect |
|---|---|
| `open shell` / `exit` | enter / leave shell mode |
| `talk to <name>` / `use <llm>` / `switch to <agent>` | reassign the active agent and/or LLM |
| `agents` / `llms` / `servers` | list what's available (and in scope) |
| `whoami` / `status` | show the active pair + its scoped servers/tools |
| `reset` / `save <name>` / `load <name>` / `realign room` | **deterministic** world ops (today these would be LLM tool calls) |
| `help` | list commands |

**Migration.** Today's `route_turn` (the `"let me talk to X"` regex in `director.py`) *is* the
embryonic shell — it already intercepts deterministically before the LLM. The refactor lifts that logic
out of the agent and into the shell, where it generalizes. Routing stops being an agent responsibility;
the agent just runs turns.

**Grammar.** Keep it natural-language-friendly (good for voice) but **parsed, never modeled** — regex /
a small grammar, deterministic. Commands that take free-text args (`save <name>`) capture the tail
verbatim.

## 3. The agent  🟡

An agent is a **declarative JSON definition** of an experience: a prompt, the LLMs allowed to run it, the
MCP servers (toolset) it may use, the live context to inject, and any personas it hosts. It owns a
segregated world (§3b) at runtime. Sketch (schema firms up in §9):

```jsonc
{
  "name": "dungeon_master",
  "description": "Runs a role-playing game, building the world live as the story unfolds.",
  "prompt_file": "prompts/dungeon_master.md",    // or inline "prompt": "..."
  "llms": ["*"],                                 // allow-list, or "*" = any configured LLM
  "default_llm": "claude",                       // active brain when you switch to this agent
  "mcp_servers": [
    { "server": "world", "access": "all" },      // ref into the server registry; access: "read" | "all"
    { "server": "assets", "access": "all" }
  ],
  "context": ["room://current"],                 // MCP resources injected into the prompt each turn (§5)
  "personas": ["prompts/personas/goblin.json"]   // optional participants (§3a); may also be made at runtime
}
```

**Identity is a pair.** The session's active identity is `(llm, agent)`, surfaced in the prompt as
`conjure:claude.builder>`. One axis (LLM) can change while the other (agent) stays — swapping brains
mid-conversation, as the roster does today.

**Scoping LLMs.** `llms` is an allow-list referencing the configured roster, or `"*"` for any. A future
extension is capability-based selection (an agent declares it *needs* vision / strong tool-use and the
system picks a compatible LLM) — explicit list first.

**Scoping MCP servers.** `mcp_servers` references the server **registry** (§4) by name. **Default-deny:**
a server not listed is invisible. `"*"` grants any registered server — the deliberate "god" escape hatch.
Enforcement is by **tool-list construction**, not runtime checks: the LLM is only handed the tools from
its in-scope servers, so it *cannot* call anything else (nothing to call). `access: "read"` (vs `"all"`)
exposes only a server's read-only tools/resources — see the granularity decision in §9.

**Prompt.** Inline string or `prompt_file` (long prompts — ours is already a screenful — don't belong in
JSON). Template variables available to the prompt: `{name}` (agent), the active LLM name, the roster of
other agents/LLMs present (today's `roster_line`), and the injected context (§5).

### 3a. Personas — participants, not operators  🟡

A **persona** is an agent-lite: a prompt + an LLM assignment + read access to the shared context when
it's handed a turn — but **no tools**. It *participates* in an experience rather than running it: a
character in the dungeon master's game, a guest on the DJ's show.

**Invoked as a tool — no hardcoded turn loop.** The agent has an `invoke_persona(name, …)` tool. Calling
it runs that persona (its prompt + LLM + the injected context) and returns its **in-character speech
and/or intent** ("the goblin lunges at you") as the tool result. The agent then narrates it and realizes
any world effects with its *own* toolset. So:

1. The user asks a persona something (or the experience calls for it to act).
2. The agent calls `invoke_persona` to give that persona a turn; the persona replies through the tool.
3. The agent speaks the reply and acts on it with its world tools. **The persona never touches a tool.**

This keeps the runtime dumb: **who speaks when is prescribed by the agent's prompt** — turn-based ("go
around the table"), free-form, or user-driven — not by engine logic. And it's a clean privilege boundary:
personas are sandboxed to *speech + intent* (they're invoked, they don't invoke), so only the agent (the
trusted, scoped layer) mutates the world. Personas are therefore cheap to define and safe to proliferate.

**Definition.** A persona is JSON like an agent but smaller — `{ name, prompt | prompt_file, llm }` —
and may be **predeclared** by an agent (`personas: [...]`) or **created at runtime** (a DM inventing an
NPC mid-scene, then invoking it).

### 3b. Per-agent world spaces — segregated, switchable, session-persistent  🟡

Each agent owns a **world space** — think a subdirectory of its own where it builds and keeps worlds.
The builder's space, the DJ's space, and the dungeon master's space are all separate. **Exactly one
world, from any agent, is active at a time** (globally) — that's what the headset renders. Worlds are
**kept for the whole session** — switch away and back and nothing is lost. (Cross-session **persistence
comes later** — see [vision.md](./vision.md).)

Implications:

- **The world store becomes a tree.** Today there's a single `WorldStore` / one world doc. It becomes
  `agent → world space (a set of named worlds)`, with one **globally-active** world whose
  snapshots/patches broadcast to clients.
- **Two levels of switching.** *Switching agent* is a **shell command** (§2, deterministic) and activates
  that agent's current world. *Switching or creating a world within an agent's space* is the **agent's
  own job** — it has world-management tools (`new_world`, `switch_world`, `list_worlds`) and does it when
  it wants, often on user request ("start me a fresh dungeon", "back to the beach"). World-building lives
  with the experience; agent-switching stays in the reliable shell.
- **The real room is shared substrate, not per-world.** You're always standing in the *same physical
  room*, so the captured **geometry** (surfaces, boundary — see [room-model.md](./room-model.md)) is
  **shared session state**, captured once. Each world layers its own **content + environment + per-surface
  style/visibility** on top. The builder painting a wall green is a fact of the *builder's* world; step
  into the planetarium and the walls are bare again (or hidden). Model: **shared geometry base +
  per-world overrides**. (Decision in §9.)
- The **builder** is the agent whose *purpose* is to let you freely customize a world; other agents
  (DJ / DM / planetarium) **author the worlds in their space programmatically** as their experience plays
  out.

### 3c. World composition — shared geometry truth + per-world view  🟡

The split that makes "same room, different experiences" work: **capture owns the geometric *truth*; each
world owns a non-destructive *view* of it, plus its own content.** An agent never edits the captured room
— it says how the room is *presented* (styled, hidden, cropped, or replaced by a sky) and what's added on
top. The server keeps this *layered* internal model and **composes** the active world into the flat doc
it already broadcasts — so the **client contract is unchanged** (it still renders one world doc;
switching worlds is just a new snapshot). The layering is server-internal; the wire protocol
([architecture.md §4–5](./architecture.md)) stays flat.

**Two layers:**

- **Shared room base** — written *only* by room capture (the headset). Per surface: `id`, `transform`
  (position/rotation), `components.surface` (extent, holes, polygon), a **seeded default** material, and
  `meta` (real, semantic, friendly_id). Plus the boundary + capture flags. **One copy, shared by every
  world in every agent's space.**
- **Per-world layer (a *view* + content)** — written *only* by the agent:
  - **`room_view`** — broad presentation rules over the base, targeted by semantic / id / `all` (same
    grammar as today's surface tools): **hide** (remove the ceiling; or hide the *whole* room),
    **clip/transform geometry** (crop walls to 1 m, anchored at the floor). Non-destructive — the base
    stays 2.7 m tall; only the *render* changes.
  - **`surface_overrides`** — per-surface specifics that win over `room_view`: `surfaceId → { material?,
    visible?, … }` (this wall green; that one shown).
  - **`entities`** — the world's own **generated** content; an entity may carry a **`mount`** (below) to
    ride along with a base surface.
  - **`environment`** — sky, fog, **passthrough/immersion mode**, occlusion mode, `defaultSurfaceVisible`
    (the planetarium is full-VR with passthrough off; the builder is AR with it on). Hiding the room's
    *render* is independent of **occlusion/safety**: the boundary and optional occlusion geometry persist
    from the base even when nothing of the room is drawn.

**Composition (server-side), per active world:** for each base surface, apply `room_view` (hide / clip /
transform), then the per-surface `surface_overrides` (style/visibility win) → a rendered surface entity
*or nothing* (hidden); **resolve each mounted entity's pose against its base surface's *current*
transform** (below); add the world's free entities; apply its `environment`. The result is exactly
today's flat world doc — the client is none the wiser.

**Worked examples:**

- *Planetarium / skybox / outdoor view:* `room_view: hide all` + `environment: { passthrough: off, sky:
  <image> }`. The walls' geometry still exists (boundary + occlusion intact) but nothing of the room is
  drawn — you're inside the sky. Switch back to the builder and the room returns; the base never moved.
- *"Crop walls to 1 m and remove the ceiling":* `room_view: { clip_height: { walls: 1.0 }, hide:
  [ceiling] }`. Composition renders each wall clamped to 1 m at the floor and omits the ceiling; reality
  (and every other world) is untouched.

**Mounting — content that tracks the room.** A generated entity may declare `mount: { surface: <id>, at:
<on-surface position + offset/orientation> }`. Composition resolves its world pose from the base
surface's *current* transform, so when re-capture shifts or re-squares that surface, the mounted object
**rides along**. This makes `place_image(on_surface=…)` a *mount* — store the relationship, not a baked
absolute pose — and generalizes [room-model.md](./room-model.md)'s "mounting resolves against planes":
surfaces are the stable anchors, mounted content is expressed relative to them and re-resolved on every
recompose.

**Why this shape:**

- **Re-capture is free for every world.** Capture updates base geometry; because overrides are keyed by
  **stable surface id** (the id-stability work already shipped), each world re-composes onto the new
  geometry automatically — a green wall stays green after it's re-squared; a window's cutout updates
  everywhere at once.
- **Clean ownership, no conflicts.** Geometry ops (capture, squaring, corner-join, hole-cutting) only
  ever touch the base; style/visibility/content/env ops only ever touch the active world. Nothing to
  disambiguate — which is *also* why we could drop patch provenance (§6).
- **Switching is cheap and non-destructive.** Activate a world → recompose → one snapshot. Walls stay put
  (shared geometry); style, content, sky, and passthrough swap.

**Mechanics & edges:**

- **Defaults live in the base.** A fresh world shows sensible surfaces (door translucent, window glass —
  today's `_default_surface_material`) because the base carries the seeded default; a world diverges only
  where it sets an override.
- **Undo is per-world.** Each world keeps its own rev/history over its layer; the base isn't
  user-undoable (it reflects reality).
- **Orphan overrides stay dormant.** A surface that drops out on re-capture keeps its override (not
  deleted), so it re-applies if the surface returns — same spirit as stable friendly-ids.
- **Holes/cutouts are geometry → base** (a window opening is physical). A world that wants to "fill" a
  window places *content* over it; the opening itself is shared.

This reshapes the server's `WorldStore` (→ shared base + `agent → world space → worlds`) and the
patch-apply **routing** (capture → base, agent → active world), but **not** the wire protocol or the
client.

## 4. Config layers & the server registry  🟡

Three non-overlapping config layers, composed at load:

1. **LLM roster** (existing) — casual name → provider/model. **Secrets (API keys) live here / in env**,
   never in agent defs.
2. **MCP server registry** (new) — name → launch/connect config. Agents reference these by name:
   ```jsonc
   {
     "world":  { "transport": "stdio", "command": "python -m conjure.mcp_server",
                 "env": { "CONJURE_URL": "${world_url}" } },
     "assets": { "transport": "stdio", "command": "python -m conjure.assets_mcp" }   // future split
   }
   ```
   Server **processes are session-scoped and shared**; each agent gets a *filtered client view*, not its
   own process. This is also the nudge to **split today's monolithic world server** (world-edit /
   asset-search / room-query) so scoping is meaningful.
3. **Agent defs** (new) — `agents/*.json`, built-in (the builder) + user-defined; non-secret, shareable,
   version-controllable.

`"*"` resolution (any LLM / any server): decide whether it snapshots at load or dynamically includes
things added mid-session (privilege creep). Reserve `*` / `any` / `god` as names so no agent can *be* a
wildcard.

## 5. Context injection (resources) — the home for the room-context optimization  🟡

We noticed the builder narrating *"let me check the environment…"* before a `query_room` tool call, and
re-querying every turn. Root cause: **room state isn't in the prompt** ([architecture.md §7](./architecture.md)
says it *should* be — "a compact world summary" — it just isn't yet). The clean, general fix lives here:

MCP servers expose **resources** (read-only context) alongside tools. An agent's `context: [...]` lists
the resources to **prefetch and inject into the prompt each turn**. So the world server exposes
`room://current` (the real surfaces + boundary — *stable* within a session), and the builder declares it
in `context`. Result: no `query_room` round-trip, no narration, and it generalizes (a different agent
injects a different resource). `query_world` stays a *tool* for the **mutable** generated scene, where a
prefetched snapshot would go stale.

This turns the optimization from a one-off patch into a first-class agent capability. (The prompt should
*also* explicitly forbid narrating tool use — "never say you're checking the scene; just do it" — but the
durable fix is removing the need.)

## 6. State & attribution  🟡

- **Transcript ownership.** Switching *LLM* continues the conversation (same agent, new brain — as
  today). Switching *agent* starts that agent's own context. So **transcript belongs to the agent**;
  attribution extends from today's `[Name]` (LLM) to also cover **personas** that took a turn (their
  utterances are logged in-character). (See [§7a](./architecture.md) — the attributed shared transcript.)
- **No cross-agent patch provenance.** Because each agent edits only its **own** world space (§3b),
  patches never mix between agents — there's nothing to disambiguate, so we *don't* tag patches with
  agent identity. (The world store's existing `origin` field stays for its current uses.)

## 7. Routing & naming  🟡

Addressing now spans two axes (agent, LLM) and risks collisions:

- Grammar must cover: address an LLM (`use gemini`), an agent (`switch to inspector`), or both
  (`builder on gemini`). Persistent vs one-shot (today's `Route.persistent`) applies per axis.
- **Namespacing**: agent names vs LLM names vs server names vs command verbs must not clash ambiguously;
  define precedence (command > agent/llm name) and a disambiguation rule when a token is two things.
- All of this lives in the **shell** (§2), not the agent.

## 8. Security model  🟢 intent / 🟡 surface

- **Default-deny, explicit-allow** for both servers and LLMs; `"*"` is the deliberate, conspicuous
  override. (Aligns with [architecture.md §13](./architecture.md).)
- **Enforcement by exposure**: out-of-scope tools are never in the LLM's tool list → uncallable.
- **Secrets out of agent defs** so they're shareable; keys stay in env/roster.
- **Trust boundary**: loading an agent JSON grants whatever it lists — agent defs are *trusted config*.
  Note loudly; don't load agent defs from untrusted sources.
- The **shell** is the right place for permission confirmations / god-mode acknowledgements, precisely
  because it's deterministic.

## 9. Open decisions (refine these next)

The load-bearing, expensive-to-reverse ones first:

1. **Scoping granularity.** Server-level only, or structured allow-entries (`{server, access:
   read|all}`, optional `tools: [...]`)? Recommendation: ship `"all"` first but make the entry a
   *structure* now, so read-only / tool-level isn't a later schema break.
2. **Transcript ownership & attribution** (§6) — per-agent context with `(agent, llm)` tagging. Hard to
   retrofit.
3. **Inline-command model** (§2) — confirm the `conjure` wake-prefix-inline + prefix-free-in-shell-mode
   split (vs. shell-only, vs. always-on interception). This sets the whole input UX.
4. **`"*"` resolution** — load-time snapshot vs dynamic (§4).
5. **Server decomposition** — when/how to split the monolithic world server (§4) so scoping bites.
6. **World-space store** (§3b–3c) — the `WorldStore` becomes a shared **base** + `agent → world space
   (set of worlds)` with one globally-active world; the **layering** (shared geometry base + per-world
   style/visibility/content/env overrides, composed server-side into the flat broadcast doc) is designed
   in §3c. Remaining work is *implementation shape*, not concept: the store refactor, the patch-apply
   **routing** (capture → base, agent → active world), and recomposition on switch/re-capture.

Resolved earlier: **world layering** — shared geometry base + per-world *view* (style, visibility,
geometry presentation transforms, content, env), composed server-side so the client contract is unchanged
(§3c). **room presentation** — hiding the whole room (planetarium/skybox), hiding by semantic, and
geometric crops/transforms are non-destructive per-world `room_view` rules, *not* edits to the base
(§3c). **mounting** — generated content can mount to a base surface id and is re-resolved on every
recompose, so it tracks re-capture; `place_image(on_surface)` becomes a mount (§3c). **persona
orchestration** — no runtime turn loop; the agent invokes a persona via an `invoke_persona` tool, turn
discipline prescribed in its prompt (§3a). **patch provenance** — dropped; segregated world spaces make
it unnecessary (§6).

Deferrable (don't build yet, don't preclude): cross-session **persistence** of world spaces; agent-to-agent
delegation / sub-agents; capability-based LLM selection; concurrent multi-agent panels (v1 = one active
agent); hot-reload of defs; degraded-mode behavior when an allowed server won't start or an LLM has no key.

## 10. Build order (proving the abstraction)

1. **Server registry + shell skeleton**, no behavior change: lift `route_turn` into the shell, keep the
   single world server, keep current LLMs. Existing director/routing tests stay green.
2. **Define the current director as `builder.json`** (renaming `director → builder` is fine to defer; keep
   an alias). Route all input through shell → active agent. Identical behavior, now declarative.
3. **Resource context injection** (§5) — builder gets `room://current`; kill the "let me check" narration
   + round-trip.
4. **A second, trivial agent** (a read-only `inspector`). *This* is the real test — scoping enforcement,
   routing collisions, per-agent context. An abstraction with one instance always looks right; the second
   instance is where the design pressure shows up.

## 11. Mapping to current code

| Today | Becomes |
|---|---|
| `Director` (roster + 1 server + 1 prompt) | shell + agent runtime; `Director` → `builder` agent |
| `route_turn` (inline `"talk to X"`) | shell command registry (§2) |
| `DIRECTOR_PROMPT` (`director.py`) | `builder` agent's `prompt_file` |
| single `mcp_server` | server **registry** entry `world` (later: split servers) |
| single `WorldStore` (one world doc) | shared geometry **base** + `agent → world space → worlds`, one **globally-active** world, composed server-side (§3b–3c) |
| real surface = entity with `transform`+`surface`+`material` | geometry in the **base**; `material`/`visible`/crop/hide become a per-world **view** (`room_view` + `surface_overrides`) over it (§3c) |
| `place_image(on_surface)` bakes an absolute pose | a **mount** to a surface id, re-resolved each recompose so it tracks re-capture (§3c) |
| patch apply (one target) | **routed**: capture/geometry → base; agent view/content/env → active world (§3c) |
| — (no equivalent) | **personas** — agent-hosted participants, invoked via `invoke_persona` (§3a) |
| roster = available LLMs | global LLM pool; agent `llms` = allowed subset |
| `_system_for` (prompt + roster_line) | prompt template + injected `context` resources |
| `on_text`/`emit`/`on_tool` | unchanged; now per active agent |
