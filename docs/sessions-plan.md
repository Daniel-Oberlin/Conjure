# Sessions, persistence & constructors

**Status:** DESIGN — iterating. No implementation yet. Companion to `persistence-model.md` (the store
layer this rides on) and `shared-session-plan.md` (the live shared-session runtime this generalizes).

The idea in one line: **a session is an *instance* of an agent (the class).** Today there is one
anonymous, implicit session per scope (the live transcript + the active world). This plan makes it
first-class: **named, owned, persisted, and switchable**, carrying its dialog, its worlds, and its
arbitrary state.

---

## 1. Agents and sessions

An **agent** is a fixed definition: a prompt (its behavior and personality), a toolset, and its setup
instructions. It lives in `agents/<name>/` and does not change as you talk to it. The builder and the
outdoor agent are two agents.

A **session** is one conversation with an agent, saved so you can leave it and come back. It holds three
things: the running dialog (the **transcript**), the **worlds** created during that conversation, and any
**state** the agent keeps (a map, an inventory, variables). A session belongs to a user and is created
by one agent — and that agent stays fixed for the life of the session.

Today there is exactly one session and it is implicit: the live transcript plus whichever world is
active. This plan makes sessions explicit — **named, owned, saved, and switchable.** You can have several
(one per conversation), list them, switch between them, and rename or delete them.

A useful way to hold it: the **agent is a reusable template; a session is one filled-in copy of it.**
Many sessions can come from the same agent. So:

- **What the agent provides:** the prompt, the toolset, and the setup steps run when a new session
  starts (the *constructor* — §6).
- **What a session holds:** its transcript, its worlds, and its state.

### Two consequences

- **Ownership and visibility live on the session.** A world no longer carries its own public/private
  flag — it inherits owner and visibility from the session that contains it. (Decision §8.2.)
- **The agent is fixed per session.** "Switch agent" means "switch to, or start, a session belonging to
  that agent" — you never swap the agent underneath a live conversation.

### A note on the word "session"

There is already a `_session.txt` file recording what is live right now: a **(scope, world)** pair. Here
**scope** is the `<user>/agents/<agent>` namespace a conversation runs in — e.g.
`daniel/agents/builder` — and **world** is the active world's name within it. Under this plan that
pointer changes meaning slightly: it records the **active session** instead, and the (scope, world) it
used to hold is simply read back from that session. Same facts, one level up.

---

## 2. What a session contains

```
session
  ├─ meta:        owner (user), agent, title, visibility (public/private), created,
  │                 active_world, llm (last used), summary?
  ├─ transcript:  the dialog context (append-only)
  ├─ worlds[]:    the worlds created during the session (nestable, as today); inherit owner+visibility
  │                 └─ active-world pointer
  └─ state/:      arbitrary agent state (the world-graph/map, inventory, variables …) — see §5
```

A world **belongs to exactly one session for now** (1:1 containment). Sharing a world across sessions
(a library world instantiated into a session) is explicitly deferred.

### Which LLM a session uses

An agent's `llms` list in `agent.json` is today an **allow-list** (which LLMs it may run on). We make it
**double as an ordered priority list**: at session start the active LLM is the **first entry that is
actually available** (its API key is configured), and `["*"]` falls back to the global roster order.
(Today, selection consults a single `default_llm` field + `settings.llm` + first-available; the list
*order* is preserved but not used for priority — this change makes the list the source of truth, and
`default_llm` collapses to "just put it first in the list".)

The session then **remembers the LLM last running** in `session.json` (`llm`): if the user switches LLM
mid-conversation, that choice sticks and is restored on resume. So the selection rule is:

> **stored session LLM (if still available) → else the agent's priority list → else global roster order.**

---

## 3. Disk layout

This is a **real directory change, not a virtual view** — but it *extends* the layout already on disk
rather than replacing it. Everything lives under the world store's root, which today is `.cache/worlds/`.

**Today**, worlds sit directly under the scope directory:

```
.cache/worlds/
    _session.txt                                   # the live (scope, world) pointer
    daniel/                                         # user
        agents/
            builder/                                # scope = daniel/agents/builder
                _active.txt                         # which world is active
                home.json  animal-house.json  …     # the worlds, side by side
```

**Proposed** — two changes at once, since we're touching the tree anyway:

1. **Make `users/` the top level.** With worlds now several levels down, a top-level `worlds/` directory
   no longer names anything meaningful. The model is user-first (see `spaces-and-users-plan.md`), so the
   disk should be too: everything a user owns lives under `.cache/users/<user>/` — their spaces *and*
   their agents' sessions.
2. **Insert a `sessions/<id>/` level** and move each session's worlds beneath it.
3. **Lift the global pointer to `.cache/_session.txt`.** It names what's live across the *whole* server,
   so it belongs at the root, not buried inside a per-store subtree.

```
.cache/
    _session.txt                                   # the active SESSION id (global, one line)
    users/
        daniel/                                     # user — the top-level owner
            spaces/                                 # user-owned physical spaces (was .cache/spaces/daniel/)
                home.json  _active.txt
            agents/
                builder/                            # scope = daniel/agents/builder (UNCHANGED meaning)
                    sessions/
                        session-1/                  # one session (stable id; see below)
                            session.json            # meta: owner, agent, title, public, active_world, llm, …
                            transcript.jsonl        # the saved dialog
                            state/                  # agent state, copied from the agent's seed
                                map.json  inventory.json
                            worlds/
                                home.json  throne-room.json  …
```

The **scope** — the `<user>/agents/<agent>` prefix, and the security boundary (`config.agent_of`: a
`builder` never sees `outdoor`'s assets, and a session's worlds reference those assets, so they must stay
inside it) — keeps its meaning; it just sits under `users/` now. Worlds are still slug-normalized JSON
files exactly as today. This is a one-time on-disk migration (§7): move `.cache/worlds/<user>/…` →
`.cache/users/<user>/agents/…` (under a session) and `.cache/spaces/<user>/` → `.cache/users/<user>/spaces/`.

Implementation-wise this is a `SessionRepository` mirroring `WorldRepository` (same slug /
traversal-guard / atomic temp+rename machinery), with `WorldRepository` re-homed one segment deeper:
scope → **session** → worlds.

**How `WorldRepository` finds a scope's session (the facade, and the "5.5" hardening).** With a
`SessionRepository` attached, `worlds.<op>(scope, name)` transparently addresses the *session's* worlds —
so `scope` stays the pure capability token and the ~50 call sites are untouched (Option 1). Which session?
For the **live scope**, the server *tells* the repo via `set_live(scope, sid)` — set in ONE place
(`_switch_to` / boot), so live-session world addressing uses the server's explicit live session rather
than independently re-reading the `sessions/_active.txt` pointer (removing the second source of truth that
caused the step-4a "outgoing world leaked into the new session" bug). For **other scopes** (admin browse,
a switch target, a cross-user public world), it resolves that scope's own active-session pointer — the
right answer there. Cross-session/cross-scope *discovery* (`list_public`, `delete_user`) walks
`.../sessions/*/worlds` directly, since it isn't about a single active session. A fully session-explicit
API (threading `sid` through every call) is deferred until multi-session-per-scope operations grow to
need it — the current facade keeps `scope` pure with no churn, and `set_live` closes the drift hazard.

**Identity — both sessions and worlds get a stable id + a mutable name.** A session has a stable `id`
(immutable — so rename is free and its worlds/transcript never move) plus a mutable `title`. **Worlds get
the same treatment**, for the same reason and one more: the world-graph (§5.5) links worlds to each
other, and those edges must survive a rename. Conveniently the world doc **already has an `id` field** —
we promote it from decoration to the **reference identity** (map edges and the active-world pointer point
at world `id`, not the slug), while the slug/path stays the mutable, human-facing name for display and
organization. Rename = retitle only, for both. (The on-disk filename can stay the slug for readability,
with `id` inside the doc as the stable key — an id→file lookup is a cheap index.)

---

## 4. Persistence implementation — decision

**Files, not a database — with the transcript as append-only JSONL.** Rationale and boundaries:

- **Transcript → `transcript.jsonl`, one turn per line.** A dialog *grows*; append-only JSONL gives
  `O(1)` appends (no whole-blob rewrite per turn), crash-safety (a torn final line is dropped on load),
  tailability, and resume = stream-replay into the Director history + `llm._messages`. One line per
  turn: `{role, speaker, by, text, tool_calls?, ts}`.
- **Meta → `session.json`**, atomic temp+rename (as `WorldStore.save`).
- **Worlds → stay JSON files**, unchanged except the extra path segment. The **world server keeps
  owning world reads/writes** — do not move worlds into a DB.
- **State → a per-session `state/` dir** of JSON docs (the "third generic state store" already
  anticipated in `persistence-model.md` §4).

**Why files, for now.** Everyone already speaks files (the world server reads world JSON directly);
atomic-write + slug + traversal-guard already exist; it is git-diffable and hand-inspectable; and the
**agent server is the single writer** of transcript+state (one Shell→Director), so there is no
concurrency pain. This honors the standing doctrine (`persistence-model.md` §7): *files are the source
of truth; indexes are derived when scale demands.*

**Where SQLite earns its place later.** The moment you want *queries* — "find my cyberpunk sessions,"
graph traversal over a large world-map, full-text over transcripts — add SQLite as a **derived, rebuildable
index**, never the source of truth. Worlds stay inspectable; the world server stays unchanged.

**The two-server seam.** Agent server owns `transcript.jsonl` + `state/`; world server owns
`worlds/*.json`; they share the session directory on disk. Writer boundaries must stay disjoint so
nothing double-writes.

### Timestamps and the agent's sense of time

Each transcript line already carries a `ts`. **Always store it** — it's nearly free on disk and useful
regardless (resume, summarization, an `on_resume` "you were last here 3 days ago" recap). The real
question is whether the *model* sees timestamps in its context:

- **Injecting them is optional and off by default.** Raw per-turn timestamps are token-wasteful and read
  unnaturally.
- **When an agent opts in** (an `agent.json` flag), prefer **coarse / relative** markers — "later that
  day", and an explicit gap only when it's large ("3 days later") — over precise clocks, so the agent
  gains a sense of elapsed time without paying for a timestamp on every line.

Lean default, with a time-aware agent (a game with day/night, a journaling companion) able to turn it on.

### Fitting the transcript into the context window

Full replay is right for now, but a long session will eventually exceed the active model's context
window. The generic fix — model-agnostic, so it works for any LLM in the roster — is **rolling
summarization**:

- **The prompt and injected context are always rebuilt fresh** (system prompt, `{user}`, `{map}`, …) and
  are never trimmed.
- **Keep the most recent turns verbatim** — a recent tail (last K turns, or last T tokens).
- **When prompt + injections + transcript exceed a budget** — a fraction of the *active model's* context
  window, so the budget is **per-model** (ties into LLM selection, §2) — **summarize the older head** into
  a compact running summary and feed that in place of those turns. As the tail ages, re-summarize
  incrementally (previous summary + newly-aged turns → new summary).
- **The on-disk `transcript.jsonl` stays complete.** Trimming affects only the *context view* fed to the
  model, never what's stored — the full transcript remains the source of truth, and the compacted context
  is a **derived projection** of it (the same "truth on disk, derived view in memory" pattern as the
  world doc vs. its deltas, §5.6). This is essentially what Claude Code's own `/compact` does.

Two things keep it safe rather than lossy: **durable facts live in the state store, not the dialog** — the
map, inventory, and variables (§5) survive summarization untouched, so the agent never relies on the
transcript to "remember" them — and the summary prompt can be agent-tunable, with the option to **pin**
key turns (the greeting, pivotal decisions) so they're never summarized away. `session.json.summary` is
the storage hook.

---

## 5. Agent state: generic tools, agent-owned schema

The crux (from design discussion): **do not encode a domain schema into the MCP tool definitions.** Two
separate schemas:

1. **The MCP tool contract** — generic, agent-agnostic CRUD over a namespaced document store.
2. **The data schema** — what shape the stored docs have. Agent-owned *data*, not part of any tool
   definition — it travels with the agent exactly like `prompt.md`.

### 5.1 Generic tools (agent-agnostic)

Named documents in the session's `state/`, addressed by dotted JSON path:

- `state_get(doc, path?)` · `state_set(doc, path, value)` · `state_merge(doc, value)` ·
  `state_delete(doc, path?)` · `state_list()` · `state_schema(doc)` (introspection).

Added to the server registry **once** and allow-listed per agent via the existing
`mcp_servers[].tools` array. If `state_set` reuses the world patch op shape
(`{op:"set", path:"nodes.throne-room.visited", value:true}`) it inherits the existing dotted-path +
**inverse** machinery (`world.py` `_set_path`), so state edits become **undoable for free** and
consistent with how worlds are patched.

### 5.2 Agent-owned schema + seed — declared in `agent.json`, referenced as files

Follows the `prompt_file` precedent (declare, reference a file — don't inline big blobs):

```
agents/dungeonmaster/
  agent.json
  prompt.md
  state/   map.json  inventory.json          # seed data
  schema/  map.schema.json                     # JSON Schema (optional per doc)
```

```jsonc
// agent.json — a new "state" block declaring the store's shape
"state": {
  "map":       { "seed": "state/map.json", "schema": "schema/map.schema.json", "inject": "{map}" },
  "inventory": { "seed": "state/inventory.json", "inject": "{inventory}" }
}
```

Each declared doc carries up to three optional things: a **seed** file, a **schema** file, an **inject**
placeholder.

### 5.3 One declaration, three consumers

- **Prompt injection (discovery).** `inject:"{map}"` wires the live doc into the prompt via the *same*
  `director._injections` path as `{user}`/context. The agent learns the shape by seeing the data. Best
  for small, always-relevant state. Opt-in per doc to control context bloat.
- **Introspection tool (on-demand).** `state_schema(doc)` returns the declared JSON Schema so *large*
  state can be looked up when needed instead of always injected.
- **Server-side validation (safety).** On write, the store validates against `schema` and rejects/explains
  on mismatch, so a hallucinating LLM can't corrupt a structured doc. No schema ⇒ free-form scratch.

### 5.4 Seed is copied per-instance (class/instance split)

At construction, declared **seed files are copied into the session's `state/`** — a fresh mutable copy
per instance. The agent dir's `state/map.json` stays pristine (class template); the session mutates its
own copy (instance). For a Zork-style game: the authored room graph is the class-level seed; the
playthrough's progress (visited, doors unlocked) is instance-level mutation of the copy. Seeding is thus
one of the **constructor's** jobs (§6).

### 5.5 The world-graph (Zork) rides this, with no domain tools

- **Nodes = worlds** — already nestable/named/listable; each place is a world. No new primitive.
- **Edges + progress = a `map` doc in session `state/`** — adjacency with directions/labels, plus
  per-playthrough flags. *Structure* may be authored at the agent (class) level (the seed); *progress*
  is per-session (instance). Read/written through the **generic** `state_*` tools; surfaced to the model
  via `{map}` injection. Nothing Zork-specific reaches the tool layer. (Edges reference worlds by their
  **stable id** — §3 Identity — so renaming a room never breaks its exits.)

### 5.6 How undo/redo works (the inverse machinery)

Every change to a world already flows through `world.py`'s `apply_patch`, which — for each op — computes
and stores the **inverse op** needed to undo it, then bumps the doc's `rev`. It never diffs or snapshots;
the inverse is captured *at apply time* from the real prior state:

- **`add` an entity** → inverse is **`remove`** that id (or, if it *replaced* an existing entity, an
  `add` restoring the old one).
- **`remove` an entity** → inverse is **`add`** back the full entity that was removed.
- **`update` / `env` with dotted paths** → each `set` writes via `_set_path`, which **returns the prior
  value** at that path; the inverse writes those prior values back. (Dotted paths like
  `components.material.color` let one op reach deep into nested dicts, creating missing intermediates on
  the way down.)

So **undo = apply the stored inverse; redo = re-apply the original.** The remaining work (per
`persistence-model.md` §6) is **grouping** — one director turn = one undoable unit, not N patches — and
**origin filtering** — never undo an automatic room-recapture or embedding write-through.

**Deltas are in-memory; the document is the source of truth.** The inverses live only in
`WorldStore.history` (memory). On disk, `save()` writes the **whole document**, never the delta log, and
`load()` starts with an empty history (`world.py`). There is **no reconstruct-from-deltas**: the full doc
is authoritative and autosaved whenever `rev` advances, and undo/redo is therefore **session-scoped and
in-memory** — durable cross-restart history / named checkpoints are a separate, later feature
(`persistence-model.md` §6). The transcript (§4) works the same way: the full `transcript.jsonl` is the
truth, and anything derived from it is an ephemeral view.

Because `state_set` reuses the *same* op shape and `_set_path`, state edits get the same inverse for
free: a `state_set` on `map.nodes.throne-room.visited` records the prior value, so "undo that" walks back
a game move exactly as it walks back a world edit.

---

## 6. The constructor — session setup

A **constructor** is the ordered list of setup steps an agent runs once when a new session of it is
created. It is declared in `agent.json` (data, not code) and is the same idea as today's world setup,
widened from a single world to a whole session.

### Constructor steps are scripted tool calls

Today's built constructor is a tiny macro vocabulary (`_WORLD_COMMANDS`: `show_edges`,
`show_annotations`, `set_sky_color`) that maps a `cmd` to an env patch. That was a shortcut. The design
target (and your original framing — "a list of 0 or more MCP calls") is broader: **a constructor step is
a scripted invocation of one of the agent's own tools**, with fixed arguments, run at construction —
*without* the LLM in the loop (deterministic, not model-decided):

```jsonc
{ "tool": "show_edges", "args": { "on": true } }
```

This unifies the vocabulary (the builder already *has* `show_edges`, `set_skybox`,
`generate_skybox_image`, … as tools) and, crucially, unlocks **generative setup**. "Create a skybox from
a description" is achievable because it's just two scripted tool calls:

```jsonc
{ "tool": "generate_skybox_image", "args": { "description": "a calm dawn meadow, soft light" }, "as": "sky" },
{ "tool": "set_skybox",           "args": { "image_id": "${sky.image_id}" } }
```

**Steps thread data explicitly.** A step may bind its result under `"as": "<name>"`, and a later step
references it with `${name.field}` in its args (resolved recursively through dicts/lists; a whole-value
`${…}` keeps the referenced value's type). There is **no hidden "last image"** — an unresolved reference
is an error (fail-hard). This generalizes to any generate→use chain, and keeps each step a real tool call.

The catch: generative steps are **slow and can fail** (see the known grounded-skybox timeout). So
construction is an **async** operation with progress — and **if any nondeterministic step fails, the whole
constructor fails**: session creation **aborts and rolls back** (no half-built session left on disk),
surfacing the error rather than silently starting a session missing its intended skybox or greeting. A
bounded retry may precede the failure, but the terminal behavior is failure, not fail-soft. Deterministic
steps (env patches like `show_edges`) don't have this failure mode. (Decision §8.13.)

### Three hooks, and how they chain

There are three setup layers; creating the first world **chains** them in order:

| Hook | Declared in | Runs |
|---|---|---|
| `world.on_create` | `agent.json` → `world` | **every** world created in the session |
| `session.first_world.on_create` | `agent.json` → `session` | **only** the session's first world |
| session steps (greeting, state seed) | `agent.json` → `session` | **once**, at session mint |

```jsonc
"world": {
  "on_create": [ { "tool": "show_edges", "args": { "on": true } } ],   // EVERY world
  "on_exit": []
},
"session": {
  "first_world": {
    "name": "home",                                                    // default "home", overridable
    "on_create": [                                                     // ONLY the first world
      { "tool": "generate_skybox_image", "args": { "description": "a calm dawn meadow" }, "as": "sky" },
      { "tool": "set_skybox",            "args": { "image_id": "${sky.image_id}" } }
    ]
  },
  "greeting": "Welcome. Where shall we begin?",                        // literal — or { "generate": "…" }
  "state": { … }                                                       // declared state docs to seed (§5.2)
}
```

**Session construction, in order:**

1. **Create the first world** (name from `first_world.name`, **default `"home"`**). Its setup is the
   **chain** `world.on_create` **⊕** `first_world.on_create` — generic per-world steps first, then the
   first-world-only steps. Every *later* world in the session runs only `world.on_create`. So the
   first-world constructor is exactly "set up the first world only," composed on top of the per-world one.
2. **Seed state** — copy the declared `state` seeds into the session's `state/` as fresh mutable copies
   (§5.2). Done before the greeting so a *generated* greeting can reference seeded state.
3. **Greeting** — append the opening assistant turn to the transcript (so it persists, replays on resume,
   and shows in a latecomer's backlog). Two forms:
   - **literal** — `"greeting": "Welcome…"` — appended verbatim (deterministic, free).
   - **generated** — `"greeting": { "generate": "<instruction>" }` — run **one** turn on the session's
     selected LLM with the agent prompt + that instruction + the freshly-seeded state/world context, and
     append the result. Warmer and varied; costs one turn and is nondeterministic (so a game wanting
     determinism uses the literal form). **This is in the plan, not deferred.**

Named now, deferred: default visibility, auto-title scheme (`"Adventure #3"` vs LLM-derived from the
first turn), persona/voice (the unused `personas` field), LLM pin (determinism for a game), entry rules
(single/multi-player, guests), and the lifecycle siblings `on_resume` (e.g. inject a "previously…"
recap) / `on_exit` (persist/cleanup — already a stub on the world block).

---

## 7. Migration from the implicit session

- **Relocate on disk** (one-time, idempotent, like `_migrate_world_dirs`): `.cache/worlds/<user>/agents/…`
  → `.cache/users/<user>/agents/…` (under a session), `.cache/spaces/<user>/` →
  `.cache/users/<user>/spaces/`, and `.cache/worlds/_session.txt` → `.cache/_session.txt`.
- `_session.txt` `(scope, world)` → **active session id**; the `(scope, world)` it used to hold is read
  back from the session.
- The current worlds under a scope become the `worlds/` of a session `session-1` (stable id) whose
  active-world is the old `_active.txt`; boot reconstructs it if absent (same read-through pattern as
  `_boot_world`).
- Each world doc's existing `id` becomes its **reference identity** (§3 Identity); a missing/blank one is
  minted on first load.
- The live agent now comes from the **active session** (which stores its agent), replacing the old
  "derive the agent from the active world's scope" (`agent_of(active_scope)`).
- `session.json.llm` starts empty; first run seeds it from the agent's priority list (§2).

---

## 8. Decisions

**Resolved** (this round):

1. **Session ↔ agent binding** — one session = one agent. ✅
2. **Per-world `public`** — retired; worlds inherit visibility from the session. `list_public` becomes
   "public **sessions**," not public worlds (touches co-location discovery). ✅
3. **Multi-user switching** — the live session stays *shared*, but switching re-runs the privacy gate;
   non-permitted clients are **bumped to shell mode** (no session), not disconnected. ✅
4. **Identity** — sessions **and** worlds get a stable `id` + mutable name (§3 Identity). ✅
5. **Declared vs. free-form state docs** — allow both (declared = validated/injected; undeclared =
   free-form scratch); MVP focuses on declared. ✅
6. **Schema format** — **JSON Schema**. ✅
7. **Validation strictness** — reject-on-invalid for declared-with-schema docs. ✅
8. **Undo scope** — reuse the world patch inverses so state edits join the "undo that" flow (§5.6). ✅
10. **Delete/rename guards** — delete a session ⇒ deletes its contained worlds; can't delete the
    *active* session without switching away; ownership-gated. ✅

Newly settled from this round's comments:

11. **Directory relocation** — user-first tree `.cache/users/<user>/…`; global pointer at
    `.cache/_session.txt` (§3, §7). ✅
12. **LLM selection** — the agent's `llms` list doubles as a priority list; the session remembers the
    last-used LLM (§2). ✅
13. **Constructor = scripted tool calls**, incl. **generative** steps (skybox-from-description), plus an
    optional **first-world-only** constructor chained after `world.on_create`; **generated greeting** is
    in-plan (§6). Any **nondeterministic (generative) step that fails aborts construction** — fail-hard +
    rollback (optionally after a bounded retry), never fail-soft. ✅

**Deferred (decided, not day-one):**

9. **Resume UX — what happens when you re-open a saved session?** Three sub-questions:
   - **(a) How much of the transcript goes back into the model's context?** → **Full replay now; add
     generic rolling summarization when it exceeds the model's budget** (§4 "Fitting the transcript into
     the context window"). The on-disk transcript always stays complete; only the context view compacts. ✅
   - **(b) Do we show the user a recap on return?** → **Deferred.** An optional `on_resume` step that
     emits "Previously, you were in the throne room…" as a visible/spoken turn — a nicety *separate* from
     (a), added later; blocks nothing.
   - **(c) Does the model get a sense of elapsed time between sessions?** Covered by §4 Timestamps —
     opt-in, coarse, off by default.

---

## 9. Build order (proposed, incremental)

1. **Disk relocation + `SessionRepository`** (§3, §7): the user-first tree, the `sessions/<id>/` layer,
   the global pointer move — a one-time idempotent migration. ✅ **Done** (`SessionRepository` + `WorldDir`
   + `migrate_cache_to_users` + the `WorldRepository` session facade; boot/switch resolve the live session
   via `.cache/_session.txt`). *Deferred within this step:* promoting the world doc `id` to the reference
   identity (§3 Identity) — worlds are still addressed by slug; fine until the world-graph (§5.5) lands.
2. **Transcript persistence** (§4): append on turn-done; **full replay** on load; wire the agent server's
   single writer. ✅ **Done** (`SessionRepository.append/read_transcript`; world `/state` carries the live
   `session`; the agent server appends each turn and replays the saved dialog on session bind/change).
   *Deferred:* storing `ts` (lands with the timestamps feature) and rolling summarization (§4 "Fitting the
   transcript…") — both non-blocking.
3. **Session shell verbs:** list / switch / rename / delete / new (§8.10 guards), incl. `session.json.llm`
   restore + the priority-list selection (§2). ✅ **Done** — 3a: world-server session endpoints
   (`/sessions`, `/session/{new,switch,rename,delete}`) + the switch flow; 3b: shell verbs (`sessions`,
   `session …`); 3c: the `llms` priority list (`_pick_active`) + per-session last-used LLM persist/restore.
   *Deferred:* a new session starts with a blank `home` world — the richer constructor is step 4.
4. **Constructor** (§6): the `world`⊕`first_world` chain, greeting, and generative steps. ✅ **Done** —
   **4a** first-world naming + the on_create chain (`cmd`/`tool` steps → world ops); **4b** the greeting
   (`Director.greet` + the agent server's `_maybe_greet`, `greeted` flag); **4c** the async generative
   step (`_build_generative_ops`: skybox-from-description → build-then-commit, **fail-hard** abort with
   nothing to roll back, "setting up…" notice, 180s client timeout). *Deferred:* state seed-copy folds
   into step 5 (coupled to the `state_*` store); richer progress-to-agent-clients during construction.
5. **Generic `state_*` tools** + `agent.json` `state` block + injection (§5). ✅ **Done** — a schema-free
   `StateStore`; **director-hosted** state tools (decision A — a reusable `_local_tools` seam, dispatched
   in-process, never over MCP); `{…}` injection via `_injections`; seed-copy at construction
   (`_maybe_seed`); JSON-Schema validation on write (reject-on-invalid). *Deferred:* undo for state (§5.6)
   — rides the world inverse machinery whenever, near-free since `state_set` mirrors the op shape.
5.5 **Live-session hardening** (§3): the server tells `WorldRepository` the live `(scope, sid)` via
   `set_live`, so live-scope world addressing uses one explicit source instead of re-reading the active
   pointer — done before step 6 so its per-session gates reason about one unambiguous live session.

6. **Visibility/ownership move to session** (§8.2) + multi-user gate re-key (§8.3). ✅ **Done** —
   **6a** session visibility model (`session.json.public`, `/session/visibility`, `session public|private`);
   **6b** re-keyed the join gate, asset inheritance, and `list_public` onto the session (per-world `public`
   retired/vestigial); **6c** bump-out — the world server re-gates connected headset clients
   (`_regate_clients`) and the agent server shells non-owner CLI/voice clients + withholds a private
   session's dialog (`_permitted`/`_conv_broadcast`/`_apply_bumps`). *Deferred cleanups:* delete the
   vestigial world `environment.public`; admin-tree shows per-world visibility (now session-level).

Each step's primitive feeds the next; nothing here blocks the current runtime until step 6 changes
gating.
