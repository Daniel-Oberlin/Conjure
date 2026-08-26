# Agents, the shell, and the shared session — backlog

Unfinished work, future directions, and known problems for the orchestration layer. The current state is
[`docs/specs/agents.md`](../specs/agents.md); the reasoning behind rejected alternatives is
[`docs/decisions.md`](../decisions.md).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems

Each says how strong it is. **Certain** = the code plainly does this, with a line to check.
**Unproven trigger** = the mechanism is certain but nobody has shown the condition that fires it arises.

- **A failed agent-follow silently empties the transcript.** *Certain.* In `_reconcile_state`
  (`agent_server.py:437`), a `shell._open_agent` failure broadcasts a notice and **returns** — before
  `app.state.live = state` and `_sync_transcript`. `Shell._open_agent`'s own handler has already
  reopened the *previous* agent, which means a **fresh Director with an empty transcript**. Because
  `loaded_session` is unchanged, nothing refills it: the conversation is gone until the session next
  changes. The fix is to re-sync on the restore path too.
- **A client's `agent <name>` switches into the HOST's scope, not the speaker's.**
  *Certain* — `_make_agent_switch_hook` uses `scope_for(app.state.user, agent_name)`
  (`agent_server.py:499`), and the in-process path uses `self._user` (`shell._activate_world`). Every
  other identity-scoped verb uses `Shell._scope()`, i.e. the **speaker**. So a guest who is permitted to
  drive the session lands everyone in *daniel's* outdoor scope — a scope the guest doesn't own and
  therefore can't edit. Decide which is right (probably the speaker, matching the session verbs) and
  make the two paths agree.
- **`voice.py --agent` is a dead flag.** *Certain* — `_run(settings, user, wake_word, agent)` accepts it
  and never reads it (`voice.py:74`). Voice is a thin client now; the agent server owns which agent is
  open. Either drop the flag or make it assert `/scope/activate` on connect. The CLI already dropped it.
- **`world.on_exit` is declared and read by nothing.** *Certain* — `agents/builder/agent.json` carries
  `"on_exit": []` and only `on_create` is ever looked up (`server._new_world_store`). Either implement it
  or remove the field so it isn't mistaken for a hook that runs.
- **`admin/tree` (`dir`) is not scoped to the caller.** *Certain* — `/admin/delete` is caller-gated
  (§6e) but the read listing isn't, so anyone can browse every user's namespace. Noted as a follow-up
  when the gate was added; a full auth model is out of the "identity only, no security" posture, but the
  asymmetry is worth closing on its own.
- **Vestigial visibility surfaces.** `/worlds/visibility` now sets the *session's* flag and is kept only
  because `set_world_visibility` calls it; the world doc's `environment.public` is written by
  `_new_world_store` and read by nothing. The admin tree still shows worlds without their (session-level)
  visibility. All three want a cleanup pass.

## Never built — designed but absent

**Personas.** `AgentDef.personas` is parsed and read by nothing; there is no `invoke_persona` tool. The
design: a persona is an agent-lite — a prompt + an LLM + read access to the injected context when it's
handed a turn — with **no tools**. It *participates* in an experience rather than running it.

Invoked **as a tool**, not by a runtime turn loop: the agent calls `invoke_persona(name, …)`, which runs
that persona and returns its in-character speech and/or intent as the tool result; the agent then
narrates it and realizes any world effects with its *own* toolset. That keeps the runtime dumb — **who
speaks when is prescribed by the agent's prompt**, turn-based or free-form or user-driven, not by engine
logic — and it is a clean privilege boundary: personas are sandboxed to speech + intent (they're
invoked, they don't invoke), so only the trusted, scoped agent mutates the world. Definition:
`{name, prompt | prompt_file, llm}`, predeclared by an agent or created at runtime (a DM inventing an
NPC mid-scene).

This is also the trigger that makes the Layer-2 tool gate *load-bearing* rather than defence-in-depth: a
persona path constructs tool calls outside one agent's offered set.

**Per-agent world spaces as a composed tree.** Today each agent's worlds are simply segregated by scope
and session, and the world doc is flat. The designed model is a **shared geometry base + per-world
view**, composed server-side into the flat doc that already broadcasts, so the client contract is
unchanged:

- **Shared room base** — written *only* by room capture: per surface `id`, `transform`,
  `components.surface`, a seeded default material, `meta`; plus the boundary and capture flags. One
  copy, shared by every world in every agent's space.
- **`room_view`** — broad presentation rules over the base, targeted by semantic / id / `all`: **hide**
  (the ceiling; or the whole room), **clip/transform geometry** (crop walls to 1 m, anchored at the
  floor). Non-destructive — the base stays 2.7 m tall; only the render changes.
- **`surface_overrides`** — per-surface specifics that win over `room_view` (`surfaceId → {material?,
  visible?, …}`).
- **`environment`** — sky, fog, passthrough/immersion mode, occlusion mode, `defaultSurfaceVisible`.
  Hiding the room's *render* is independent of occlusion and safety: boundary and occlusion geometry
  persist from the base even when nothing of the room is drawn.
- **Mounting** — a generated entity declares `mount: {surface: <id>, at: …}` and composition resolves its
  world pose from that surface's *current* transform, so re-capture carries it along. This generalizes
  "mounting resolves against planes": surfaces are the stable anchors, mounted content is expressed
  relative to them and re-resolved on every recompose. `place_image(on_surface=…)` becomes a mount.

Why this shape: **re-capture is free for every world** (overrides are keyed by stable surface id, which
already ships), **ownership is clean** (geometry ops only touch the base, style/content/env ops only
touch the active world — which is also why patch provenance could be dropped), and **switching is cheap
and non-destructive** (activate → recompose → one snapshot; walls stay put, everything else swaps).

Remaining work is implementation shape, not concept: the store refactor, patch-apply **routing**
(capture → base, agent → active world), and recomposition on switch/re-capture. Mechanics already
settled: defaults live in the base; undo is per-world (the base isn't user-undoable); orphan overrides
stay dormant so they re-apply if a surface returns; holes and cutouts are geometry and therefore base.

Worked examples: *planetarium* = `room_view: hide all` + `environment: {passthrough: off, sky: <image>}`
— the walls' geometry still exists but nothing of the room is drawn. *"Crop walls to 1 m, remove the
ceiling"* = `room_view: {clip_height: {walls: 1.0}, hide: [ceiling]}` — reality, and every other world,
untouched.

**Multi-server agents.** `Director.connect` raises unless the def resolves to **exactly one** registered
MCP server. The registry, `ServerRef`, and the launch path are all plural-shaped already; what's missing
is opening N stdio clients and merging their tool lists (with a name-collision rule).

**`"*"` as a server reference.** Accepted by the loader and by `agent_names` validation, but
`Director.connect` filters `agentdef.servers` to names *in* the registry, so a lone `"*"` resolves to
zero servers and raises. Decide whether `*` snapshots at load or dynamically includes servers added
mid-session (privilege creep), and reserve `*` / `any` / `god` as agent names so no agent can *be* a
wildcard.

## The shared-session model — the unbuilt half

Steps A–C of the shared-session plan shipped (one pointer, the enriched broadcast + `/state`, the agent
server and its follower). D–G did not.

**Pinning while held (P7).** While a space is occupied, the shared pointer should be *pinned* to it:
moves are legal only to a world **in the same space** or a **VOID/skybox** world (no surfaces to match).
A different real space's world is refused with a clear reason. Unheld, voice/CLI move freely. Today
`/worlds/switch` and `/scope/activate` are entirely unconstrained by occupancy, so a remote CLI user can
break a co-located headset's surface-match out from under them — *"remote can't blind local"* is stated
as a principle and not enforced. Tests: held + same-space ok; held + VOID ok; held + other-space refused;
unheld free.

The escape hatch already falls out of the design and works: with a **VOID** world live, headsets in
different rooms and a voice user can all co-inhabit it (every headset passes surface-match trivially,
nobody holds a space). *Outdoor agents are inherently multi-room; surface agents are inherently
single-room.*

**Three-tier access on every move (P10).** The design computes each present participant's tier —
**Editor** (owns the live world: see + edit + converse), **Viewer** (may occupy, not the owner: see +
converse), **Locked-out** (may not occupy: headset → passthrough, voice/CLI → rejected turns) — on every
pointer move, and emits per-participant access state. Today the *effects* exist piecemeal (the `/ws`
join gate, `_regate_clients`, `_apply_bumps`, `_owner_only_writes`) but there is no tier as a first-class
value, and nothing tells a client which tier it is in.

**Context-reflecting prompts (Step G).** The `context` event already carries `world`, `space`, `owner`
and they are folded into the client's ctx (`apply_context`) — but neither `prompt_from_context` nor
`status_segments` renders any of them, and there is no locked-out form. The designed prompt:

```
conjure:carol@living/cozy-cabin · builder · claude>     # editor/viewer
conjure:carol ⊘ bedroom (no access)>                    # locked-out
```

A background listener already prints out-of-band context notices, so the eventual-consistency half is
there; correctness comes from the server gate regardless, per P9.

**Privacy fallback precedence (Step F).** When a matched space's `last_world` is inaccessible to the
establisher (private to a third user), the deterministic precedence should be: (i) the establisher's
**own** last world in this space, else (ii) the **most-recent world tied to this space that they may
occupy**, else (iii) **mint** a fresh default tied to the space. *"Pick a random world" is rejected as
unprincipled.* Today only the degenerate MVP exists — recalled-if-reachable, else mint (or refuse for a
private space with nothing to join). (i)/(ii) need a space to remember an **ordered history of worlds
shown here** rather than a single `last_world` pointer — the one genuinely open design knob.

**Co-edit / consent.** Edit-ownership is owner-only. A future consent model could let a guest co-edit
another's world. Deliberately out of scope.

## Context and the transcript

**Rolling summarization.** Full replay is right for now, but a long session will exceed the active
model's context window. `history_cap` is a blunt turn count that drops the oldest turns from the model's
view outright. The generic, model-agnostic fix:

- the prompt and injections are always rebuilt fresh and never trimmed;
- keep a recent tail verbatim (last K turns, or last T tokens);
- when prompt + injections + transcript exceed a budget — a fraction of the **active model's** window,
  so the budget is per-model — summarize the older head into a compact running summary and feed that in
  place of those turns, re-summarizing incrementally as the tail ages;
- the on-disk `transcript.jsonl` stays complete. Trimming affects only the *context view*; the full
  transcript remains the source of truth and the compacted context is a derived projection of it — the
  same "truth on disk, derived view in memory" pattern as the world doc vs its deltas.

Two things keep it safe rather than lossy: **durable facts live in the state store, not the dialog**, so
the map, inventory and variables survive summarization untouched; and the summary prompt can be
agent-tunable, with the option to **pin** key turns (the greeting, pivotal decisions) so they're never
summarized away. `session.json.summary` is the storage hook. A **per-model token budget** for the
trim is separately shelved in [`backlog.md`](../backlog.md).

**Timestamps.** Transcript entries carry `{role, by, text}` and **no `ts`** — the persisted entry is
built by `agent_server._entry`, and the plan's `ts` was deferred. Storing it is nearly free and useful
regardless (resume, summarization, an `on_resume` recap). Whether the *model* sees them is a separate,
opt-in question: raw per-turn timestamps are token-wasteful and read unnaturally, so an agent that opts
in should get **coarse/relative** markers ("later that day", an explicit gap only when it's large)
rather than precise clocks. Lean default; a time-aware agent (a game with day/night, a journaling
companion) turns it on.

**`on_resume`.** An optional step emitting *"Previously, you were in the throne room…"* as a visible or
spoken turn when a saved session is re-opened — a nicety separate from how much transcript goes back
into the context. Blocks nothing.

**Backlog on join.** A connecting client is replayed the **whole** transcript. At some length that wants
a last-N bound, which affects what a late joiner sees.

## Agent state

**Undo for state edits.** `state_set` deliberately mirrors the world patch op shape
(`{op:"set", path:…, value:…}`) so it can inherit `world.py`'s inverse machinery: `_set_path` returns the
prior value at a path, so the inverse writes it back — "undo that" would walk back a game move exactly as
it walks back a world edit. Not wired: `StateStore.set` computes no inverse and there is no state
history. Near-free whenever undo grouping lands.

**Free-form docs are unvalidated by design.** A doc with no declared `schema` accepts anything (scratch
space). That's intended, but there is no way for an agent to *discover* which of its docs are validated
short of calling `state_schema` per doc.

**Seed drift.** A seed is copied once, at construction (`seeded: false → true`). If the agent's seed
changes afterwards, existing sessions keep the old copy — correct for the class/instance split, but
there is no migration or "reseed" path when an authored map genuinely gains a room.

## Constructor

**A richer step vocabulary.** `_build_generative_ops` handles exactly three tool names —
`generate_skybox_image`, `generate_grounded_skybox_image`, `set_skybox`. Everything else is silently
ignored (deliberately forward-compatible, but it means a constructor step can be a no-op with no
warning). The design target is that **any** of the agent's own tools can be a scripted step; that needs
the constructor to run against a real tool surface rather than a hand-written switch.

**Progress during construction.** A generative first world broadcasts one *"Setting up your new world…"*
notice and then goes quiet for tens of seconds. Per-step progress to agent clients was deferred.

**Named but deferred:** default visibility for a new session; an auto-title scheme (`"Adventure #3"` vs
LLM-derived from the first turn); an **LLM pin** (determinism for a game); entry rules (single/multi
player, guests).

## Persistence

**Undo/redo rides inverses we already compute.** `apply_patch` records an inverse for every op
(`world.py`), so undo is a cursor over that history plus a tool — not a new subsystem. The real work is
the two things around it:

- **Action grouping** — one director turn is one undoable unit, not N patches. Without this, "undo that"
  walks back a fragment of a turn.
- **Origin filtering** — never undo an automatic room re-capture, a re-anchor, or an embedding
  write-through. Patches already carry an `origin` (`"room"`, etc.), so the filter has something to key
  on.

MVP shape: session-level, in-memory, voice-accessible. This is also what unblocks state undo (see
*Agent state* above) and surface-styling undo (see
[`backlogs/worlds-surfaces.md`](./worlds-surfaces.md)) — one mechanism, three consumers.

**Durable versioning is a different shape.** Cross-restart history, named checkpoints and branching are
*snapshots*, not an inverse log, and are a separate later feature. Worth not conflating with undo: the
inverse log is cheap because it already exists; snapshots are a storage design.

**Copy-to-private is not built.** Public assets are referenced in place, which is safe against silent
mutation because the bytes are content-addressed ([`specs/agents.md §2.2`](../specs/agents.md)). It is
*not* safe against the entry being unpublished, or its curation drifting. Copy-to-private is the opt-in
guard: copy the (already immutable) bytes' catalog entry into your own scope to pin a stable,
self-curated version. Nothing implements it.

**Cross-machine federation.** The `public` share works on one machine because bytes are
content-addressed and global on that disk. Sharing across machines needs a transport, not a predicate.

## Sharing and identity

**A world belongs to exactly one session.** Sharing a world across sessions — a library world
instantiated into a session — is explicitly deferred.

**Sessions are keyed per `(user, agent)`.** Re-keying them by user, with the agent as just the current
driver, is a cleaner long-term model and a larger, separable change. Deliberately deferred, not decided.

**Promoting the world id to the reference identity.** Worlds have permanent `wld_…` ids and are
addressed by them internally, but the world-graph edges of a state doc would need them consistently.
Deferred until the graph lands.

**Capability-based LLM selection.** `llms` is an explicit allow-list plus priority order. Letting an
agent declare it *needs* vision or strong tool-use, and having the system pick a compatible LLM, is the
natural extension.

**Agent-to-agent delegation / sub-agents**, **concurrent multi-agent panels** (v1 is one active agent),
**hot-reload of defs**, and **degraded-mode behavior** when an allowed server won't start or an LLM has
no key are all named and unbuilt. The shell's modal overlay would generalize to a **stack** for
sub-agents; v1 is overlay + a single active pointer.

## Shell

**World ops as commands.** `reset` / `save` / `load` were named as shell verbs and never added; today
they are agent tools or nothing.

**Shared prompt includes.** The world-ownership framing is duplicated by copy between `builder` and
`outdoor` prompts. Factor a shared include when it earns its keep.

**`exit` with no active agent.** The design says `exit` from a shell opened before any agent was chosen
should prompt to pick one. In practice there is always an agent (the server opens one at boot), so the
branch has never been needed.

**Per-tool argument limits.** A flat tool allow-list is the current granularity. Whether an agent should
be able to call `generate_image` *only for skybox use* — argument-level scoping — is open; flat list
first.

## Server decomposition

Splitting the monolithic world MCP server (world-edit / asset-search / room-query) is what would make
per-server scoping meaningful — today every agent references the one `world` server and scoping bites
only at the tool level. The registry already supports it (`{"assets": {…}}` was sketched); it needs the
multi-server launch above, and a decision on where the seam falls.

Server **processes** are intended to be session-scoped and shared, with each agent getting a *filtered
client view* rather than its own process. Today each agent switch spawns and tears down its own MCP
subprocess.

## Security posture

The standing posture is **identity only, no security**: usernames are trusted, anyone can claim any
name, and a missing `X-Conjure-User` header is treated as the owner. That is deliberate and documented
for a friendly, co-located deployment. Consequences worth naming before it changes:

- A direct raw HTTP client to the world server bypasses the MCP capability gate entirely — it is out of
  scope by the same posture, and a `server.py`-level check only becomes meaningful if the trust model
  gains auth.
- **Trust boundary for agent defs:** loading an agent JSON grants whatever it lists. Agent defs are
  *trusted config* — don't load them from untrusted sources.
- **Secrets stay out of agent defs** (keys live in env / the roster), which is what makes a def
  shareable and version-controllable.

## `viewer://current`

Relative placement — "a few metres in front of me", "to my left", "behind me" — works today through the
`view_relative` **tool**, which resolves against the live head pose that presence reports
(`server.gaze`, keyed by `X-Conjure-User`, preferring the plane-relative head anchor over the raw
presence pose). What doesn't exist is the *prefetched* form: a `viewer://current` context resource
(position + yaw, injected each turn) so the agent can resolve "in front of me" without a round-trip, the
same way `room://current` removed the `query_room` hop. A `near="me"` tool argument is the other half of
the same idea.

## Record / replay

Because the transcript is append-only and world change is patch-sourced, a session should be replayable.
Nothing records the world event log alongside the dialog today, so this is unrealized. See the matching
item in [`backlogs/dynamics.md`](./dynamics.md) — tiers A and B are procedural and event-sourced, so the
module half is replayable from `(seed, clock, event log)` for near-free.
