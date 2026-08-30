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
  (`agent_server.py:446`), a `shell._open_agent` failure broadcasts a notice and **returns** — before
  `app.state.live = state` and `_sync_transcript`. `Shell._open_agent`'s own handler has already
  reopened the *previous* agent, which means a **fresh Director with an empty transcript**. Because
  `loaded_session` is unchanged, nothing refills it: the conversation is gone until the session next
  changes. The fix is to re-sync on the restore path too.
- **A client's `agent <name>` switches into the HOST's scope, not the speaker's.**
  *Certain* — `_make_agent_switch_hook` uses `scope_for(app.state.user, agent_name)`
  (`agent_server.py:508`), and the in-process path uses `self._user` (`shell._activate_world`). Every
  other identity-scoped verb uses `Shell._scope()`, i.e. the **speaker**. So a guest who is permitted to
  drive the session lands everyone in *daniel's* outdoor scope — a scope the guest doesn't own and
  therefore can't edit. Decide which is right (probably the speaker, matching the session verbs) and
  make the two paths agree.
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
trim is separately shelved below (harvested from the old flat backlog).

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

**State is per-session, and some agents need per-agent.** A state doc is a fresh mutable copy per
session (§7.4's class/instance split), so what an agent learns lives inside **one conversation**. Start a
second session with the same agent and it seeds blank again; everything learned stays behind in the
first, still on disk, no longer consulted.

That lifetime is right for the demo it was built for — a quest's progress *should* reset with a new
game — and it stays invisible in practice, because `agent <name>` resumes the scope's last-used session
rather than minting one. It bites the first time someone types `session new`, which is exactly when they
least expect the agent to have forgotten them.

It is wrong for any agent whose whole point is accumulating a picture of the **user** rather than the
playthrough: preferences, likes and limits, how they want to be addressed. Surfaced by a user agent
keeping a `kinks` doc — a list built up over months is meaningless if a new conversation resets it.

The shape this wants is a **second scope for state, beside the per-session one**: docs declared
per-agent live at `<user>/agents/<agent>/state/` and are shared by every session that agent owns, while
per-session docs stay where they are. The agent's own declaration says which it wants — the natural
spelling is a `"scope": "agent" | "session"` field on the doc, defaulting to `session` so nothing
existing changes. Both resolve through the same `StateStore`; only the directory differs, and the
injection and the six `state_*` tools are unchanged because they address a doc by name, not by path.

Open: whether a per-agent doc should be per-**user**-per-agent (almost certainly yes — it's the user's
preferences, and scope already carries the user) and what happens to an agent-scoped doc when the agent
itself is deleted.

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

---

## Harvested from the old flat `docs/backlog.md` (2026-08-26)

*Items filed against this subsystem before the per-area backlogs existed. Status lines
and dates are as originally written; none has been re-verified against today's code.*

## Director reports success after an explicit tool failure

**Status:** open · noticed 2026-08-28 · **model behaviour** · same family as the couch below, but
sharper: there the tool was never called, here it was called and plainly failed.

**Observed.** An image request was refused by the provider's content policy — the tool result began
`Couldn't generate image:` and named the refusal. Six seconds later the director told the user the
image existed and offered to hang it. It then made three more attempts (one naming a generator that
does not exist, one hitting a capability our mediation had *just* explained in the previous result),
each failing, each reported as a success, and finally narrated the picture as though it were in the
room.

So the model is not reading tool results as outcomes at all in this mode — it is answering the *user's
request* and treating the tool round trip as decoration. That also explains the couch below, where the
tool was skipped entirely: same posture, different point of failure.

**Contained on our side (2026-08-28):** the provider dump was 400 characters of raw SDK JSON, which
buries the one actionable word and reads like a transient error. `_reason()` now reduces a content
refusal to one sentence that names the cause and says explicitly that rewording the same subject will
be refused again — removing the invitation to retry. That does nothing about the false report itself.

**The fix is a prompt guardrail**, and it is the same sentence the couch item asks for: *never state
that something was done unless a tool call this turn returned success; if a result begins "Couldn't",
say what failed.* It belongs in the shared prompt material rather than in one agent's file, which is
the open question — we have no mechanism for prompt text every agent inherits. Worth considering
alongside it: a **result-shape convention** the guardrail can name, since `Couldn't …` / `error:` /
`No …` are currently three different failure shapes a model has to recognise.

**Related capability note, not a bug:** the only transparency-capable generator in the roster is
OpenAI, and OpenAI's content policy is the strictest of the three. Any agent whose subject matter
OpenAI refuses therefore cannot use transparency at all — the other generators have no alpha channel.
`select_generator` cannot know this in advance (policy is enforced at generation time, per prompt), so
the combination fails at the provider rather than at mediation.

## Director claims a surface restyle is done without calling the tool ("the couch")

**Status:** open · noticed 2026-06-26 · **CONFIRMED hallucination** (repro'd both ways)

**Confirmation (clean session, same couch):** "surface 41 green" → director called `show_surface(real_couch_41)`
then `style_surface(real_couch_41, green)` → `Styled 1 surface(s)` → couch turned green. Same surface,
same world — works when the tool is actually called. So the failing turn was purely the director
emitting "Done" without calling `style_surface`. Likely contributing factor: the failing turn was in a
DEGRADED-tracking session with the every-2s re-ingest flood (noisy context); the successful one was a
clean restart with no flood. So the prompt guardrail is the fix; reducing context noise may also help.

**Symptom:** "Make the couch green" → director replies "Done — the couch is now green!" but nothing
changes. Reported as couch-specific and reproducible; other surfaces (tables, walls) restyle fine.

**Evidence (decisive):** the saved world `new-room` (rev 273) shows the 4 tables `color=blue
visible=True` (styled) but `real_couch_41` still `color=#888 visible=None` — **never touched**. The log
for that turn has **no `style_surface` tool call and no `material.color` patch** — just the final
"Done". So the director hallucinated completion without calling the tool.

**Not a surface bug:** matching (`target="couch"` → semantic match), material defaults (couch = normal
opaque panel; only doors/windows are special), and recapture (updates in place, preserves style) treat
the couch *identically* to the tables that worked. If `style_surface(target="couch")` had run it would
have worked. No couch-specific code path exists — this is LLM behavior (assert-done-without-acting),
same class as the re-query papercut.

**Possible trigger (unconfirmed):** an unstyled surface shows in the director's room summary as
`visible=False` (styled ones flip to `visible=True`), so the model may treat the couch as "not active"
and skip to a confirmation.

**Proposed fix:** (1) prompt guardrail — never report a change as done unless a tool was actually
called this turn; (2) clarify that every real surface, **including furniture (couch/shelf/table)**, is
a valid `style_surface` target. Both are soft (prompt-level).

**To confirm on repro (the user will retry in a fresh world):** watch the log on "make the couch X" —
- **no `style_surface` call** → confirmed hallucination → the prompt guardrail is the fix;
- **`style_surface(target="couch")` fires but the couch still doesn't change** → flips to a CLIENT
  rendering bug (couch `material.color` not applied), a different investigation.

**Side note:** the same log shows the room re-ingesting all ~45 surfaces every ~2s continuously — heavy
and noisy (recapture never touches `material.color`, so not the couch cause); may be amplified by the
shared-room layer in the multi-world code. Worth watching.

## Grok narrates one tool and calls another, then loops on it

**Status:** contained 2026-08-28 · noticed 2026-08-28 (a user agent running Grok) · **model behaviour, not ours**

**Symptom:** "Annotations and Surface Edges" → the director said *"show_annotations with on is true"* and
called `show_edges({"on": true})`. Then said and did exactly the same thing again. Forty-plus times, one
patch broadcast per hop, until the server was killed by hand.

Two distinct model failures in one turn, worth separating:

1. **The spoken text names a different tool than the call.** It had already called
   `show_annotations({"on": true})` correctly one hop earlier, so it *can*; it then narrated that call
   while emitting a different one. This is raw tool-name confusion, and it is what made the log look
   like `show_annotations` was the thing looping when it never ran again.
2. **It never converged.** Every `Surface edges on.` result was answered with the identical call.

**What was fixed:** the *containment*, not the cause — the turn is now bounded twice over
([specs/agents.md §5.6](../specs/agents.md)): the repeat guard refuses the third identical call without
executing it, and `MAX_TOOL_HOPS` ends the turn audibly. A stuck model can no longer take the server
with it, and the client stops seeing patch churn.

**Phrasing tightened (2026-08-28):** four tool results were verbless headlines — `"Surface edges on."`,
`"Surface annotations on."`, `"Immersion set to ar."`, `"Reset to an empty holodeck…"` — which is
imperative-shaped English. A result a model can read as *an instruction to do the thing* is one it can
satisfy by calling the tool again. They now report state (`"Surface edges are now on."`), matching the
`is now` pattern the visibility tools already used. **This is a suspected contributor, not a proven
cause** — the log shows one hop (06:36:39) where both tools ran and both succeeded, and it kept looping
anyway, which points at plain pattern lock-in rather than any misreading. The phrasing costs nothing
either way; the guards are what actually contain it.

**What is still open:** why Grok does this at all. Same family as the "couch" hallucination and the
re-query papercut above — assert-one-thing-do-another — and the same soft fix probably applies (a prompt
guardrail: *a tool result that says it succeeded means it succeeded; do not call it again*). Worth
checking whether it correlates with the two failed `generate_image` calls immediately before it, since a
model that has just been refused twice may be in a retry mood. Grok is the only model observed doing it;
the guard is provider-neutral regardless.

## A tool turn can speak twice — the ack and the reply say the same thing

**Status:** shelved 2026-08-28 · **CONFIRMED** (mechanism traced end to end; fix deferred, four options below)

**Symptom:** on a turn that runs a tool, the user hears two complete replies a few seconds apart, saying
substantially the same thing in slightly different words.

**Mechanism — ours, not the model's.** A turn emits text at *every* hop. Hop 1 produces text alongside
the tool call and goes out `final=False`; hop 2 produces the closing text `final=True`. `Director.emit`
forwards both to `on_text`; `agent_server.py` broadcasts them as `assistant_delta` and
`assistant_final`; `voice.py` speaks **both** — there is no filter between them.

That is by design: the first is meant to be an **acknowledgement** ("On it") so the user isn't left in
silence while a slow tool runs. The design assumes the model writes a short ack there. Some models
instead write a complete, past-tense answer *before* doing the work, and then a second complete answer
after — so the "ack" and the "reply" are the same sentence twice.

**Evidence (34 turns, 2026-08-27→28):**

| | doubled |
|---|---|
| turns that ran a tool (17) | 4 |
| turns that ran no tool (17) | **0** |

No counterexamples: the extra utterance appears only where there is a tool call for it to precede, and
only sometimes within those, because the model only sometimes emits text alongside the call. The two log
lines are written by *different* code paths (the non-final branch of `emit`, and `_handle` after
`run_turn` returns), so the log distinguishes two events rather than one seen twice.

**The ack was not buying anything in the observed turns.** Measured: request → first text 18s and 6s;
first text → tool call **0s** in both. The model had already finished thinking, so the ack covered no
latency at all, and the tool it preceded (`style_surface`) takes milliseconds. An ack only earns its
place ahead of a genuinely slow tool — image and skybox generation run ~8s.

**Two side-effects, independent of whether the duplication is audible:**
- The pre-tool text **asserts completion before the tool has run**. Same family as the couch
  hallucination above; harmless when the call then succeeds, but the user has already been told it
  worked if it doesn't.
- Only the final reply enters the transcript. The user hears two utterances; the record keeps one, so
  the transcript under-reports what was said.

**Options, roughly in order of confidence:**
1. **Gate the ack on the tool's expected duration** — speak pre-tool text only ahead of known-slow tools
   (image/skybox generation). Puts the ack where its value actually is; needs no model cooperation.
2. **Near-duplicate suppression in the speech stage.** `speech.py` already exists as the per-connection
   "what the model wrote vs. what should be said" filter. Needs a similarity threshold — a judgement call.
3. **Length gate** — an ack is short, an answer is not. Crude, one line, provider-independent.
4. **Prompt guardrail** — don't describe a change as done before calling the tool. Also addresses the
   assert-before-acting hazard, but it is soft and lives in each agent's own prompt.

**Not yet established:** whether this is what a user reporting "it repeats itself" is actually reacting
to — the phrase lock-in below is present in the same transcripts and fits the same description. And the
trace stops at `bridge.speak()`; TTS was not observed rendering both.

## Replies converge on a repeated phrase as a session runs

**Status:** open · noticed 2026-08-28 · **model behaviour**

Late in a long session, successive replies increasingly end on the same clause, cosmetically reworded
each time — four consecutive replies across two turns closed on the same beat. The history the model
sees is a trimmed tail; once that tail is saturated with a pattern, the most probable continuation is
another instance of it, which then saturates it further.

Self-reinforcing, so it worsens with session length, and it is the most likely sustainer of the tool
loop above (which continued even after a hop where every requested tool ran and succeeded). Nothing in
our code causes it. Levers are prompt-side, or context-side: the trim currently keeps the most recent
turns, which is exactly the window most contaminated once lock-in starts. Worth considering whether the
trim should preserve diversity rather than pure recency.

## Sticky tool arguments across turns (Grok)

**Status:** open · noticed 2026-08-28

Asked for a transparent-background image "using Gemini", the director correctly passed
`transparent: true, generator: "Gemini"` — a combination our mediation refuses, since Gemini has no
alpha. The user then asked again **without mentioning transparency**, and the model sent
`transparent: true` a second time, so it failed identically. It had carried the argument from the
previous turn rather than deriving it from what was asked.

The refusal text now names both ways out by name (*"Use Chat for transparency. Or drop transparent to
keep Gemini."*) instead of the old *"omit the generator or pick one that can"*, which the model never
acted on — it dropped the request entirely and changed the subject. Whether a more actionable error is
enough to break the stickiness is unverified; if not, the next lever is a prompt line telling the
director to re-derive image arguments from the current utterance.

## Director re-queries for ids it already has in context

**Status:** open · noticed 2026-06-25 during live director testing

**Symptom:** the director re-runs `query_assets`/`search_library` for data it retrieved a turn or two
earlier and still has in context. Live: it listed the 3 transparent images *with ids*, then on "place
them left to right" announced "let me look those up properly first!" and ran the identical query again
to get ids it already had. Cheap and correct (fast local SQL, right result) — a papercut, not a defect.

**Cause:** the reuse nudge exists in the prompt ("REUSE ids you already retrieved; don't re-run
query_assets for something you just listed") but doesn't hold reliably. Two reasons: (1) it's one
clause buried in a single ~600-word run-on paragraph, so it gets diluted; (2) the model defaults to
"verify before acting" — describing felt low-stakes, *placing* felt like a commit, so it re-confirmed.
Suppressing a cheap idempotent re-lookup is inherently soft for a prompt nudge.

**Options:** (a) leave it — cheap and correct; (b) hoist the reuse rule into a prominent standalone
line — low risk, diminishing returns (the nudge already exists once); (c) **restructure the whole
builder prompt** from one wall-of-text paragraph into scannable sections / a "Rules" block — the real
fix, since right now every behavioral rule competes inside one paragraph. (c) is behavioral (can't be
unit-tested) and risks nudging other behaviors, so it needs a live test pass.

**Lean:** (c) is the high-leverage move if these "nudge didn't stick" papercuts keep recurring;
otherwise (a) is defensible.

## Voice barge-in (shared-session C3b, shelved)

**What:** interrupt the director mid-turn by talking over it — VAD detects the user speaking while TTS
is playing → cancel the in-flight turn and take the new utterance.

**Why shelved:** voice landed as a thin WebSocket client (C3a) with mute-while-speaking; barge-in is a
UX polish, not required for the shared-conversation goal.

**Proposed fix:** the WebSocket protocol already reserves a `{type:"interrupt"}` client→server message.
Wiring it: (1) the agent server must run each turn as a **cancellable task** (not awaited inline in the
connection's receive loop) so the loop stays responsive mid-turn; on `interrupt`, cancel that task, emit
`interrupted`+`turn_done`, release the floor. (2) voice: turn OFF mute-while-speaking (or gate it), and on
VAD-during-TTS send `{type:"interrupt"}` + stop local TTS. Needs echo handling (earbuds today) so the
bot's own voice doesn't self-interrupt.

**Open decision:** does an interrupt discard the partial turn's world edits, or keep them? (Probably keep
what already applied; just stop further tool calls.)

## Per-model token budget for history trim (shelved)

**What:** replace the director's fixed **turn-count** history cap (`settings.history_cap`, default 40 —
bounds the LLM's view of the transcript in `Director._recent_history`) with a **token budget derived
from each LLM's context window**, so the trim adapts across models (Claude, Gemini, grok, gpt-*).

**Why shelved:** the 40-turn cap already fixed the real problem (a bloated session made the director
skip tool calls). Turn-count is predictable and enough for now; the token version is a refinement.

**Key insight (don't lose this):** the binding constraint was **quality, not the window** — the model
had plenty of context left and still degraded. So the trim length is a **reliability** knob, NOT a
"fill the window" one; a bigger window is not a reason to keep more history. Size to the degradation
knee, clamp by the window only on small models:
`trim_budget = min(quality_budget, context_window × safety_frac − reserved)`.

**Proposed fix:**
- Add a static `context_window` (public, known metadata) to each roster adapter; conservative default
  (~128k) for unknowns. (Provider APIs don't expose it reliably → curated map.)
- `reserved` = system prompt (incl. the injected room+world context) + tool schemas + current turn +
  response headroom (`max_tokens`); roughly constant since the injected context is bounded.
- `_recent_history` keeps the most-recent turns whose cumulative token estimate ≤ available; `chars/4`
  is plenty for trimming (no exact tokenizer needed).
- The `quality_budget` stays the PRIMARY limit and is tuned **empirically** (watch the `[bcast]`/`[tool]`
  trace as history grows — the window doesn't tell you the degradation knee). Optional per-model overrides.

**Safe here specifically:** `world://current` + `room://current` are re-injected fresh every turn, so
trimming old chat loses only conversational continuity, never scene knowledge — aggressive trimming is
safe, and summarizing dropped turns is optional (narrative continuity only).

**Open decision:** pure trim vs. rolling summarization of dropped turns; and whether the `quality_budget`
is one shared cap or per-model (tuned by observation).
