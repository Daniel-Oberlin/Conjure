# Agents, the shell, and the shared session — the spec

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/agents.md`](../backlogs/agents.md); rejected alternatives and
the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

This is the orchestration layer: the deterministic **shell**, the declarative **agents** it switches
between, the **sessions** they run in, and the **agent server** that hosts one shared conversation for
every front-end. It is the counterpart to [`docs/specs/dynamics.md`](./dynamics.md) for modules.

---

## 1. The shape

Three layers and two servers.

```
  CLI (cli.py)            voice (voice.py)          — thin clients, no state, no keys
        └───────────────┬────────────────┘
                        │  one WebSocket per connection:  ws://…/ws?user=&client=&shell=
        ┌───────────────▼───────────────────────────────────────────┐
        │  AGENT SERVER  (agent_server.py, :8770)                    │
        │    one Shell  →  one Director  →  one shared transcript    │
        │    per-connection: user, shell mode, cwd                   │
        └───────┬──────────────────────────────────┬────────────────┘
                │ MCP over stdio (one subprocess    │ rides the world server's /ws
                │  per live agent, respawned on     │  as a passive listener → follows
                │  every agent switch)              │  the live (scope, session, agent)
        ┌───────▼──────────┐                        │
        │  mcp_server.py   │  hard capability gate  │
        └───────┬──────────┘                        │
                │ HTTP  (X-Conjure-User / -Scope)   │
        ┌───────▼───────────────────────────────────▼────────────────┐
        │  WORLD SERVER  (server.py, :8080) — the single source of    │
        │  truth: worlds, sessions, spaces, the live session pointer  │
        └───────┬─────────────────────────────────────────────────────┘
                │ WebSocket /ws
            headsets
```

- An **agent** is an experience: a prompt, the LLMs allowed to run it, the MCP tools it may call, the
  context it injects, the dynamic modules it may conjure. Declarative — a directory, no runtime code.
- A **session** is one instance of an agent: its transcript, its worlds, its state. Named, owned,
  persisted, switchable.
- The **shell** is a deterministic command plane above both. No LLM is consulted to recognise or run a
  shell command.

**The world server is the single authority.** It persists what is live and answers "what's live". The
agent server, the headsets, and the clients are peripherals that reconcile to it. `agent = f(world)` —
a world lives in a scope `<user>/agents/<agent>`, so the agent falls out of the world and is never
stored separately.

**The world server runs standalone.** It boots from disk, restores the session pointer, and renders to
headsets with no agent server present — you can walk your world with no AI in the loop. That
independence is why the two are separate processes.

---

## 2. Scope — the capability namespace

```
scope = "<user>/agents/<agent>"          config.scope_for(user, agent)
agent = scope.rsplit("/agents/", 1)[1]   config.agent_of(scope)
```

Scope is **runtime-injected, never an LLM argument**. It is the hard boundary for assets: an agent only
ever reaches assets whose scope has the same agent segment, regardless of public/private — a `builder`
never sees `outdoor`'s assets ([decisions.md §14](../decisions.md)). `DEFAULT_SCOPE` is
`<DEFAULT_USER>/agents/builder`.

**Why injected and not a parameter** — this is the security crux, not a style choice. If any tool took
`scope=…`, the model could pass another agent's scope and read it, and so could a prompt injection
hidden in some asset's label or notes. Instead the trusted runtime holds the scope and hands the store a
*scoped handle* with the visibility predicate already baked in, so an agent **physically cannot widen its
own view**. Reads are its own scope ∪ granted public; writes are its own scope only.

Scope is a property of the **catalog entry** — the searchable row and its embedding — not of the bytes.
Content-addressed bytes still dedupe globally on disk, while visibility, search and curation are
per-scope, so two agents never see each other's catalog even for byte-identical content. In SQL the
predicate is `scope = ? OR (public = 1 AND scope GLOB '*/agents/<agent>')` (`library.py:495`).

### 2.1 Visibility is a flag, not a path segment

`public: bool` lives on the item. Publishing is a metadata flip that relocates nothing and breaks no
reference, content-addressed assets keep their hash, and access is a predicate rather than path
arithmetic. Path-encoded visibility was considered and rejected for exactly those reasons. The same flag
and the same read/write rule (`owner == caller OR public` / `owner == caller`) apply to sessions,
worlds, assets and spaces alike — see [`specs/spaces.md §5`](./spaces.md).

### 2.2 Two stores, one scope

A world and an asset are different *kinds* of thing, so they live in different stores that share the
scoping layer:

| | **Asset store** (`library.py`) | **World / document store** (`world.py`) |
|---|---|---|
| Identity | **content hash** — immutable | **permanent id** (`wld_…`); the name is display text |
| Mutability | write-once blobs | edited over time (patches, snapshots) |
| Versioning | new bytes ⇒ new asset | inverse log per patch (§ undo, backlog) |
| Query | similarity / semantic + intent | by-id lookup, list, metadata filter |
| Embeddings | central — the whole point | not applicable |
| Role | **leaf content**, referenced | a **document that references** many assets |

So a world is a named, mutable document that *points at* assets by id; it does not live in the
content-addressed media catalog. It is simpler than the asset catalog — no embeddings, no similarity
search — precisely because it is a document rather than searchable media. A third **state** store
(§7.4) rides the same scheme, per session.

**Public assets are referenced in place**, never copied into the caller's scope. That is safe because
assets are content-addressed: the bytes behind a hash are immutable by construction, so "changing" a
public asset means producing a *new* hash and a reference cannot be silently mutated underneath you. The
two genuine risks are the entry being unpublished and its curation drifting (label, notes, tags) — the
guard against both is copy-to-private, which is not built (see
[`backlogs/agents.md`](../backlogs/agents.md)).

---

## 3. The agent definition

Each agent is a self-contained directory. **The directory name is the agent's identity** — `agent.json`
need not repeat it (a `name` field, if present, is validated to match).

```
agents/                          # bundled (config.BUNDLED_AGENTS_DIR)
  servers.json                   # the shared MCP server registry
  builder/
    agent.json
    prompt.md
  outdoor/
    agent.json
    prompt.md
  scratch/
    agent.json
    prompt.md
    state/quest.json             # seed data (§7.4)
    schema/quest.schema.json     # JSON Schema
```

User agents live in `~/.config/conjure/agents/<name>/` and **shadow** a bundled agent of the same name.
Resolution (`agents.resolve_agent_dir`, mirroring `dynamics.resolve_module_dir`):

```
env CONJURE_AGENTS_PATH  →  settings["agents_path"]  →  [<config_dir>/agents, BUNDLED_AGENTS_DIR]
```

user-first, first match wins. `list_agents()` annotates each name `bundled` or `user`.

### `agent.json`

```jsonc
{
  "description": "One line — shown by the shell's `agent` listing.",
  "prompt_file": "prompt.md",                  // relative to this dir; or inline "prompt": "…"
  "llms": ["*"],                               // allow-list AND priority order (§5.2)
  "default_llm": "claude",                     // fallback when no `llms` entry is available
  "mcp_servers": [
    { "server": "world", "access": "all",      // "all" | "read"
      "tools": ["query_world", "place_asset"] }  // opt-in only, NO wildcard; omitted ⇒ none
  ],
  "context": ["room://current"],               // MCP resources injected each turn (§5.3)
  "dynamics": ["fireflies", "water", "grab"],  // required allow-list (specs/dynamics.md §9)
  "world":   { "outdoor": false, "on_create": [ … ], "on_exit": [] },  // §7.5
  "session": { "public": true, "greeting": "…", "first_world": { … } },  // §7.5
  "state":   { "quest": { "seed": …, "schema": …, "inject": "{quest}" } }  // §7.4
}
```

| Field | Required | Validated at load | Notes |
|---|---|---|---|
| `prompt` / `prompt_file` | yes (one) | non-empty after resolution | `prompt_file` is read relative to the agent dir |
| `description` | no (`""`) | — | one line |
| `llms` | no (`["*"]`) | — | allow-list; also the **priority order** for picking the active LLM |
| `default_llm` | no | — | consulted only when no `llms` entry is in the roster |
| `mcp_servers` | no (`[]`) | every `server` name exists in the registry (when a registry is passed) | v1 launches **exactly one** |
| `mcp_servers[].access` | no (`"all"`) | — | `"read"` refuses mutating tools at the MCP gate |
| `mcp_servers[].tools` | no (`[]`) | each name exists on the live server, at connect | opt-in only; omitted ⇒ **none** |
| `context` | no (`[]`) | — | MCP resource URIs; fetched only if `{context}` appears in the prompt |
| `dynamics` | no (`[]`) | every module resolves on the dynamics search path | a dangling name **fails the load** |
| `world.outdoor` | no (`false`) | — | this agent's worlds are **room-less**: they never adopt the live space (§7.5) |
| `session.public` | no (`true`) | — | visibility a NEW session is born with, on **every** mint path (§7.5) |
| `world` / `session` / `state` | no (`{}`) | `state[].seed`/`schema` files parsed if present (failures skipped) | read by the world server and the agent server, not the loader |
| `personas` | no (`[]`) | — | parsed into `AgentDef.personas` and **read by nothing** |

A malformed def raises: unknown MCP server, unknown dynamic module, missing/empty prompt, a `name` that
disagrees with the directory.

### The MCP server registry

`agents/servers.json` — name → how to launch it. Agents reference entries by name.

```jsonc
{ "world": { "command": "python", "args": ["-m", "conjure.mcp_server"],
             "env": { "CONJURE_URL": "${world_url}" } } }
```

`${world_url}` is substituted from settings at launch. A bare `python`/`python3` is mapped to the
running interpreter, so the subprocess inherits the venv.

### The shipped agents

- **`builder`** — the full-access world-building agent. It enumerates the **entire** world tool surface
  (a test asserts the list equals every `@mcp.tool` in `mcp_server.py`, minus the control tool
  `set_caller`, so a new tool can't go silently un-granted). Context: `room://current`,
  `world://current`, `dynamics://available`. Dynamics: `fireflies`, `water`, `grab`.
- **`outdoor`** — skybox-only: twelve tools, no `context` at all (so it pays **zero** per-turn context
  cost — the live contrast with builder), no dynamics. Declares `world.outdoor` — its worlds are
  room-less and never adopt the live space, which is a property of *the agent* rather than of whichever
  request happened to create a world. Its `session.first_world.on_create` runs a
  generative constructor (§7.5). It holds the **read** half of the library (`search_library`,
  `query_assets`) but none of the mutating half — every sky it generates is catalogued, so without
  reads it would write to a store it can never read, and the agent wall (§2) means nothing else could
  read it either.
- **`scratch`** — the agent-state demo: one tool (`query_world`), a seeded + schema-validated `quest`
  doc injected as `{quest}`.

---

## 4. Tool scoping — two enforcement layers

Tool access is **opt-in only**: an agent gets exactly the tools it names, and omitting `tools` grants
**none**. `"*"` is not accepted for tools.

**Layer 1 — client-side omission (`director.py`).** `Director.connect` filters the live
`list_tools()` result to the allow-list (`_scope_tools`) before building `ToolSpec`s, so the LLM is
never *offered* an out-of-scope tool — and no provider API can emit a `tool_use` for a tool it wasn't
given. Two guards keep it failing loud rather than open:

1. the allow-list is validated against the live tool names at connect — a typo **raises**;
2. `Director._execute_tool` re-checks every call against `_allowed_tools`, catching a programmatic path
   that didn't come from the offered list.

**Layer 2 — hard gate at the MCP server (`mcp_server._GatedMCP`).** A `FastMCP` subclass whose
`call_tool` refuses a disallowed tool — or, under `access: "read"`, a mutating one — **before** any
world-server call, returning an error result. It reads the capability env injected at launch:

```
CONJURE_SCOPE = <user>/agents/<agent>
CONJURE_TOOLS = comma-separated allow-list      # unset = no restriction; "" = none
CONJURE_ACCESS = all | read
```

This is a *separate process from the LLM*, so it holds regardless of what the model was offered — a
Layer-1 filter bug or a non-LLM path can't bypass it. `_READONLY_TOOLS` is an explicit set
(`query_world`, `query_room`, `view_relative`, `list_worlds`, `list_image_generators`,
`search_library`, `query_assets`); **everything else counts as mutating**, so a newly added tool is
denied to a read-only agent until it is classified.

The gate lives at the MCP layer, not in `server.py`, because tool identity only exists there: most
mutating tools funnel through one generic `/patch` endpoint, so a path→tool mapping doesn't exist. The
one exemption is `set_caller` (§8), a control tool the director calls and no agent lists.

---

## 5. The agent runtime (`Director`)

The Director is **agent-agnostic**. It owns what is true for every agent — the roster, the transcript,
the MCP client and tool loop, per-turn context injection — and nothing about any particular one.

```python
async with Director.connect(settings, agent="builder", user="daniel") as director:
    await director.handle("put a tree in front of me", speaker="daniel", on_text=…, on_tool=…)
```

`connect` loads the def, launches its **one** MCP server over stdio, builds the scoped roster, filters
the tools, and yields a ready Director. It raises if the agent allows no available LLM, or if the def
references anything other than exactly one registered server.

### 5.1 One turn

1. `handle` takes the single **floor** — a turn submitted while another is in flight raises `Busy`
   (reject, never queue or interleave). The check-and-set has no `await` between it and the flag.
2. `_set_caller(speaker)` tells the MCP server who this turn acts as (§8).
3. `_system()` assembles the system prompt from the agent's `prompt.md` plus its injections (§5.3).
4. The utterance is **labelled with its speaker** (`"daniel: put a tree…"`) so the model never sees an
   unattributed human message, matching how history is labelled. It is stored **raw**; the label is
   re-derived from `Turn.by` on replay, so history never double-labels.
5. The active LLM runs the turn with the scoped tools. `on_text(text, final=, speaker=)` fires per LLM
   round-trip (an ack, pre-tool narration, then the final reply); `on_tool(name, args)` fires before
   each call.
6. Two turns are appended: `Turn("user", text, by=speaker)` and `Turn("assistant", final)`. The
   assistant turn carries **no LLM attribution**, so switching LLMs is invisible in the context.

`greet(instruction)` is the same loop with **no tools** and **no user turn** — one assistant turn for a
session's generated opening line (§7.5).

### 5.2 The roster and the active LLM

`scoped_roster` filters the global roster to the agent's `llms` (or everything for `["*"]`).
`_pick_active` then chooses the starting LLM in this order:

> **first available entry of the agent's `llms` (its own order) → `default_llm` → `settings.llm` →
> whatever is first in the roster.**

`llms` therefore doubles as a **priority list**, and `default_llm` collapses to "put it first". A
session's *remembered* LLM overrides all of this on load (§7.3).

Only one LLM is active at a time, and it is **shared** — a switch by anyone affects everyone. Switching
is the shell's job (`llm <name>`), never inferred from an utterance: the old `route_turn` inline
handover has been removed from the director entirely.

### 5.3 Prompt injection

The agent's `prompt.md` owns **all** its text, including the framing around any injected value. The
runtime only fills placeholders it has a provider for, and **only when the placeholder appears** — an
agent that references neither `{context}` nor `{#context}` pays no MCP resource fetch at all.

Two forms (`director._fill_injection`):

- `{name}` — bare substitution.
- `{#name}…{name}…{/name}` — a **conditional section**: the inner block is kept only when the value is
  non-blank and dropped entirely otherwise, so a header vanishes with its value (no dangling
  `--- Live context ---` when the room is empty).

Only the exact registered names are touched, so JSON or SQL braces elsewhere in a prompt survive.

The registry (`Director._injections`) is a list of `(name, provider)` rows; a provider may be sync or
async. Today:

| Placeholder | Value |
|---|---|
| `{user}` | the **speaker of this turn** (`_speaker`), not a fixed launch identity |
| `{context}` | the agent's `context` MCP resources, concatenated (`_fetch_context`) |
| per-doc, e.g. `{quest}` | the live session's state doc as JSON (§7.4) |

A failed or missing context resource is skipped, never fatal. The world server exposes three:

| Resource | Contents |
|---|---|
| `room://current` | the live real-room summary — the same formatter `query_room` uses |
| `world://current` | placed objects (excluding scaffold and real surfaces) + the environment line |
| `dynamics://available` | the **active agent's** conjurable module catalog (specs/dynamics.md §9) |

`query_world` stays a *tool* for anything a prefetched snapshot would make stale. It dumps the
**placed** scene: real room surfaces collapse to one counted line that names what it withheld and
points at `room://current`, because a per-surface listing was most of the dump and carried strictly
less than the summary — an identical-looking line per surface reads as complete, and a reader that
wants a colour concludes none is stored.

### 5.3a Per-LLM sections — the `{#llm}` switch

Some prompt text is worth spending on one model and wasteful on the others: a guardrail Grok needs
([backlog](../backlogs/agents.md)) buys nothing on Claude but costs its tokens on every turn. A `{#llm}`
block says so, in the prompt, next to the paragraph it qualifies:

```
{#llm}
{=grok}
- A tool result that says it succeeded means it succeeded. Never call it again to check.
{=gemini,chat}
- Never emit narration and a tool call in the same message.
{=*}
{/llm}
```

Deliberately a **sibling of the conditional section** above rather than a second system. Branches are
markers, not nested tags — a paired form (`{#llm:gemini,grok}…{/llm:gemini,grok}`) makes you type the
list twice and get it wrong once.

| Rule | |
|---|---|
| names | roster casual names (`Claude`, `Gemini`, `Chat`, `Grok`), comma-separated, case-insensitive. Vendor aliases are **not** accepted — one spelling per thing, and it matches what you type at the shell |
| `{=*}` | the **remainder**, not a positional branch: a named branch always wins, so `{=*}` may sit anywhere. Absent ⇒ unmatched LLMs get nothing |
| empty branch | nothing between two markers is legal and emits nothing — the empty case needs no syntax of its own |
| markers own their line | consumed whole, trailing newline included, so a dropped branch cannot weld two bullets together or leave a blank gap |
| granularity | LLM-level only. Model-level (`claude-opus-5` vs `claude-sonnet-4-6`) is not expressible; the grammar does not foreclose it |

**Everything is validated at load** (`agents.validate_llm_sections`, from `load_agent`) and every fault
is fatal, because a prompt has no runtime that complains — nobody reads the bytes actually sent to the
model, so a branch that can never fire would sit there indefinitely. Fatal: a name not in the `ROSTER`
(`{=cluade}`); a name the agent's own `llms` doesn't allow (dead text, same class as a dangling
`dynamics` reference); text before the first branch; one name in two branches of a block; a marker that
isn't part of a well-formed block — markers are line-anchored, so a mis-placed one would otherwise reach
the model as literal text.

Two things the implementation gets right and would be easy to get wrong:

- **Resolution runs before injection.** An injection inside a dropped branch then costs nothing at all —
  `_system` skips any placeholder no longer present, so a `{context}` in a branch this LLM doesn't get
  means no MCP resource fetch, and `context_stats` stays an accurate account of what was sent.
- **Resolution runs per turn, not at load.** `active` changes under `llm <name>` and that switch is
  shared (§5.2), so a prompt resolved at construction would keep serving whichever LLM the session
  started on — a bug that appears only after a mid-session switch.

### 5.4 The transcript and the model's view

The transcript is a flat list of `Turn(speaker, text, by=)` — plain user/assistant, no LLM identity.
Two distinct things ride on it:

- **What is stored and replayed to clients** — the whole thing, persisted to
  `transcript.jsonl` (§7.3) and replayed as backlog to any connecting client.
- **What the model sees** — `_recent_history()`, the tail capped to `settings.history_cap`
  **turns** (default 40; `--history-cap`, `0` = unlimited). Older turns are dropped from the model's
  view only. The cap exists because context bloat degrades tool-calling well before the window fills —
  a bloated history was observed to make the director skip tool calls.

`context_stats()` reports `{turns, cap, chars:{prompt, room, tools, history}}`, measured at the moment
a turn is assembled (the only point where all four exist together). Characters, not tokens: every
provider tokenises differently, so a char count is the one figure that means the same thing across the
roster. `tools` is usually the largest and least visible slice. This is what the CLI's status bar shows.

### 5.5 Director-hosted tools

Some tools are dispatched **in-process** rather than over MCP (`_local_tools` + a short-circuit in
`_execute_tool`). Today that is exactly the generic agent-state store (§7.4): offered only when the
agent declares a `state` block, and added to `_allowed_tools` so the Layer-1 re-check passes.

### 5.6 Bounding the turn

A turn is a loop — run the tools the model asked for, feed the results back, ask again — and **nothing
in any vendor's protocol makes it terminate**. A model can answer every tool result with the same call
forever; each hop is an API call and a patch broadcast to every connected client. Two independent
limits, deliberately at different levels:

- **The repeat guard** (`Director`, `REPEAT_LIMIT = 2`) is per turn and keys on the tool name *and* its
  arguments. The first two identical calls run; from the third the Director **does not execute** and
  returns an error-shaped result naming the loop and telling the model to reply. Two is the largest
  count that is still obviously legitimate — read, edit, read again — and the argument key means "style
  surface 12, then 13" is progress, not repetition. The refusal is logged `REPEAT`.
- **The hop cap** (`llm.MAX_TOOL_HOPS = 24`) bounds the round trips in every provider loop, for a model
  that loops while *varying* its arguments enough to slip past the guard. On the cap the turn ends with
  `HOP_LIMIT_NOTICE` emitted `final=True` — so it is spoken, and lands in the transcript where the next
  turn's history shows the model its own failure instead of an unexplained gap.

The order matters: the guard cuts the common pathology early and **without executing anything**, so the
world stops changing the moment the loop is detected; the cap is the backstop that guarantees the turn
ends at all. Neither shortens real work — the cap is far above any real build turn, and the guard only
ever refuses a call whose exact result the model already has.

> Why not simply end the turn on a repeat? Because the user is waiting for a reply. Handing back a
> result lets the model read its way out and answer; cutting it off produces silence.

---

## 6. The shell

A deterministic command interpreter. **No LLM is consulted to recognise or execute a shell command.**
Input it doesn't recognise as a command is forwarded to the active agent unchanged.

### 6.1 Two ways a command is recognised

- **Inline, while talking to an agent** — only a line led by the **wake word** is intercepted
  (`^(?:conjure|…)\b[,:]?\s*(rest)`; the comma is optional because STT rarely punctuates). So "put a
  **shell** on the table" reaches the builder as content, while "**conjure** open shell" is a command. A
  bare wake word means `open shell`.

  **The wake word is a configured list, not one string.** "Conjure" is not in most STT vocabularies, so
  Whisper returns something phonetically near — "coinjure" is the one observed. That failure is silent
  in the worst way: the line doesn't match, so a *command* is forwarded to the agent as *content*, and
  the agent obligingly tries to build you a coinjure. The list resolves env `CONJURE_WAKE_WORDS` >
  settings.json `wake_words` > `config.DEFAULT_WAKE_WORDS`, so a newly-observed mis-hearing is a config
  line rather than a code change; entry `[0]` is the canonical one a user is told to say.

  **Every alias must be a non-word.** A real word would swallow ordinary speech that merely starts with
  it — silent in the other direction, and worse than the problem it solves. Amend by observation.

  The **voice client's mic gate** is a *different word*, with **no default at all** — see §Voice. The two
  gates compose in one breath and must not collide.
- **In shell mode** — every line is a command; the prefix isn't needed. A line matching no command is
  rejected (`Unknown command: … Type 'help'.`), never sent to an LLM, so control mode never silently
  "does something".

**Mode is a parameter, not instance state.** `Shell.as_command(text, in_shell)` takes it from the
caller, so one shell serves many connections each with their own mode. `is_open_shell` / `is_leave_shell`
recognise the two toggles (`open shell` / `shell`, and `exit|leave|close|done`) — the phrases live
server-side; the client never knows them.

### 6.2 Two audiences, one registry

Voice is live in the simulation with no screen; the CLI has a terminal. Rather than two command sets
that would drift, every row of `Shell._table` carries a `voice` flag. A CLI-only command invoked by
voice refuses politely: *"'dir' is a terminal command — run it from the CLI."* The client declares
itself with `?client=voice|cli`; the agent server passes `voice=(conn.kind == "voice")` into dispatch.

The line the flag draws is **the path**, not danger or destructiveness. A command whose value is a path
— `dir`, `show`, `cd`, `rename`, `delete` — is unspeakable to say and unusable to hear, so it stays
typed. Everything addressed by a whole name is speakable, including the listings (`spaces`, `users`) and
including a destructive one, which is confirmed instead of forbidden.

Voice also gets spoken aliases for `llm <name>` — `talk to gemini`, `switch to claude`, `use grok`.
These are **voice-only**: in text they would claim every LLM name as a reserved word, so the canonical
typed form is the noun command.

**The `voice` flag also selects the OUTPUT.** Which commands exist is one registry; how their answers
read is not. Every listing the shell prints is a column of rows with `*` or `@` marking the current one,
which is instant on a screen and useless aloud — the marker is a character a TTS engine mispronounces
or, since the voice speech stage strips asterisks, never sees at all. A voice listener got a list of
names and no idea which one they were in.

So a listing is **data**, and rendering is per-audience (`spoken_list`, `_join_spoken`):

| Command | Terminal | Voice |
|---|---|---|
| `agent` · `llm` · `sessions` · `worlds` | column, `*`/`@` marker, ids, detail | *"3 agents: builder, outdoor and scratch. You're in builder."* |
| `spaces` | column with surface counts, geo and visibility | *"2 spaces: living room and studio. You're standing in living room."* |
| `users` | column, `*` on the server's active user | *"2 users: daniel and guest. You're daniel."* |
| `where` | `·`-separated status line with roster and tool counts | *"You're daniel, in the builder agent with Grok. Session Home, world animal-house. Agent mode."* |
| `help` | every row, `·` marking the voice-safe ones | the verbs only — usage lines are `<name>`/`|`/`·` syntax noise, and `help <command>` still gives detail |
| `dir`-style listing | path header, aligned columns | names only, current one named in words |

**"In" is not right for every noun**, so `spoken_list` takes the phrasing from its caller: you are
*standing in* a space and you *are* a user. `users` also departs from the marker deliberately — the `*`
tracks whichever user the world server last acted for, and spoken back as *"You're …"* that would be a
lie to a guest, who is exactly the listener who needs it right. So the terminal keeps the marker and the
spoken form names the **speaker**; each is true for its own audience.

The marker becomes a sentence: that is the one thing it carried, and the only thing a listener would
otherwise lose. Ids (`session-1`, `wld_…`), paths, alignment and the legend explaining the markers are
all dropped — they are handles for typing, not for hearing. `/session/switch` returns the session
**title** alongside its id for the same reason; reporting "switched to session-1" was poor typed and
worse spoken.

Single-line results ("Created and switched to world home.") already read the same either way and are
left alone — the split is where structure exists, not everywhere.

### 6.3 Two shapes of command

A **noun** command acts on whatever is LIVE and reads the same spoken or typed. A **path** command acts
on anything addressable. Nouns for the live thing, paths for any thing.

| Noun command | Effect | Voice |
|---|---|:--:|
| `open shell` / `exit` | enter / leave shell mode | ✓ |
| `help [command]` | list commands, or explain one | ✓ |
| `where` (`status`) | user · agent · LLM · session · world · mode, in one line | ✓ |
| `tools` | what the active agent can call | — |
| `agent` · `agent <name>` | list · switch (relaunches its MCP server; its own sessions and worlds) | ✓ |
| `llm` · `llm <name>` | list · switch the shared active LLM | ✓ |
| `sessions` · `session …` | list · `new [title]` · `rename <title>` · `<name>` · `<user> <name>` | ✓ |
| `worlds` · `world [new] <name>` | list · switch · create and switch | ✓ |
| `clear` | wipe this session's chat history (keeps worlds and assets) | ✓ |
| `reset agent <name> [assets]` | wipe an agent back to **never-used** — every session, its transcript, state and worlds — so the next arrival is a genuine first run. Assets are kept unless asked for | ✓ (confirmed) |
| `yes` | confirm the reset just offered | ✓ |
| `spaces` · `users` | list your captured spaces · everyone with a namespace here | ✓ |

**`reset agent` is the one confirmed command.** Typed, it runs at once — typing it is already a
deliberate act, and a prompt in a terminal is friction for its own sake. **Spoken, the first utterance
only arms it**: the shell names what will be destroyed and waits for `yes`. Three properties make the
confirmation worth having rather than ceremonial — the agent name is checked against the search path
*before* arming, since a garbled name is the likeliest way this goes wrong and the server would
otherwise only object after the confirmation; the armed reset carries **who** armed it, so another
speaker's `yes` is refused; and it **expires** (60s), so a `yes` meant for something else cannot reach
back and fire it. The first `yes` consumes it, so an echo is inert.

| Path command | Effect | Voice |
|---|---|:--:|
| `dir [-l] [path]` (`ls`) | list **one level** of the namespace; `-l` also shows ids | — |
| `show [path]` (`info`) | one entry in detail | — |
| `disk [path]` (`file`) | where it lives on disk — real files, sizes, an asset's catalog row | — |
| `cd [path]` | change the working directory (bare: back to your agent) | — |
| `public` / `private [path]` | visibility of the live session, or of a path | ✓ |
| `rename <path> <new>` | retitle a world, space or session; relabel an asset | — |
| `delete <path> [!]` (`rm`) | remove a world, session, space, asset or user — **immediate** for an explicit path; a pattern lists what it matched and needs `!` | — |
| `gc [!]` | asset files nothing references; lists them, `!` deletes them | — |
| `set [key [value]] [--save]` | list settings, read one, or change it for this run | — |
| `unset <key>` | drop a run-time change | — |

First match wins; the unknown-command fallback runs after. Adding a command is adding a row.

**Patterns.** A `*`, `?` or `[…]` in the LAST segment expands against display names, folded the same way
every other lookup folds (§6.5) — so `dir "kitchen*"` and `dir "Kitchen *"` agree. No `**`: worlds are
flat and the tree is four levels deep, so a recursive form could only ever mean "everything". Matching is
server-side (`namespace.match`), which is where the entries are. `cd` takes a pattern only when it
matches exactly one thing; `rename` refuses one, since N things cannot share a new name.

### 6.4 The namespace

Paths mirror storage, one level per real containment:

```
/<user>/spaces/<name>
/<user>/agents/<agent>/assets/<id>                     library rows (virtual — SQLite, not files)
/<user>/agents/<agent>/sessions/<sid>/worlds/<name>
/<user>/agents/<agent>/sessions/<sid>/state/<doc>      the agent's own memory (§7.4)
/<user>/agents/<agent>/worlds                          shortcut → the ACTIVE session's worlds
```

**Worlds live per session** — `WorldRepository` routes every per-name op to the scope's live session's
`worlds/` dir, so two sessions under one agent own separate sets of worlds. The path says so; hiding the
session level merges them into one indistinguishable list.

Paths are absolute, `~`-relative (your own home), or relative to the connection's `cwd`, which starts at
your own scope (`default_cwd`) so a bare `dir` shows something worth seeing. Resolution is pure
(`shell.resolve_path`) and quote-aware (`unquote_arg`), because display names contain spaces.

A shortcut **resolves on use**: `cd worlds` adopts the path the *server* resolved
(`…/sessions/session-1/worlds`), so it can't silently point elsewhere after a session switch. The same
holds for `delete` — it reports what the server resolved.

`dir` lists **one level**. The old recursive form dumped every user's worlds, spaces and assets at the
root, unreadable at any real size. A trailing `/` marks what you can `cd` into; `*` marks what's live.

**A row is a `label` and a list of `cells`, laid out by the renderer**, under a `columns` header naming
what they mean. The server used to compose one `detail` string per row, which is why no two listings
lined up. `label` is what you READ; `ref` is what you TYPE, present only when the two differ. That split
matters more than it looks: while they were one field, the asset listing had to lead with an id, because
`leaf_row` matched on the row's display text and the bulk delete passed it to `library.delete`.

The **id column is hidden** — `dir -l` shows it, and `disk` shows it with the real filenames. The
exception is assets, where the id genuinely IS the address. Names are clipped at 44 columns: asset labels
are prompt-derived and run to 611 characters in practice, so an uncapped column destroys the table rather
than wrapping.

**`state/` is addressable**, not just listed. It was a child of every session from the start and refused
by the resolver the whole time, so `dir` named somewhere you could not go. `show` describes a doc's top
level rather than dumping it — the schema belongs to the agent (§7.4), so the only honest summary is what
each entry *is*. Deleting one is allowed and reported as what it is: the agent forgets it.

**Both forms of every path are returned.** `path` holds ids and is canonical; `display` holds names and is
what you are shown. The shell remembers the first and prints the second, so renaming the session you are
standing in cannot strand your working directory — and cannot silently re-point it at a LATER session of
the same name, which a name-valued cwd would do without failing. No protocol field was needed: `cwd`
never round-trips, it is per-connection server state that is only ever sent out for the prompt.

A cwd can still name something **deleted**, so a path command whose cwd no longer resolves walks up to
the nearest surviving ancestor and says so — the same self-healing `_mru_first` does for the world
pointer. It checks the cwd before blaming it, so a genuine typo is still reported as a typo.

All of these hit the world server's `/admin/{tree,show,delete}`, so they act on its live state rather
than raw files. `_admin_resolve` rejects `.`/`..` outright and then checks every segment against an
enumerated real set (users, agents, sessions, worlds), so a segment can never name something that
doesn't exist.

### 6.5 Names

A display name is also how you *address* the thing, so `world.clean_name` holds both ends to one rule
(`world.NAME_SEGMENT`, which `server._ADMIN_PART` is built from — they can't drift):

- **Double quotes are dropped.** `shlex` consumes them when tokenising a path, so a name carrying its
  own could never be typed back. `rename x '"a" "b"'` stores `a b`, which is what typing it yields
  anyway. Removing them beats stripping a surrounding pair, since `"a" "b"` opens and closes with a
  quote without being quoted.
- **Apostrophes and accents are kept.** `rename "Bob's room" x` and `rename "Café Noir" x` both tokenise
  correctly, because a name with a space has to be double-quoted regardless. These names arrive by voice
  and from an LLM; rationing their punctuation buys nothing.
- Whitespace is collapsed and trimmed.
- Only a **path separator or control character** is refused, naming the offender. The charset is
  defence-in-depth, not the traversal gate. And since identity became an id, a name never reaches the
  filesystem (`wld_*.json`, `session-N`, `space-N`), so there's no encoding argument for ASCII either.
- Names are **unique** within their container, compared the way lookup compares them: `_loose()` folds
  case, accents, and treats spaces/underscores/hyphens as equal, then drops other punctuation — the same
  key `world.slug` has always used. So `Home` and `home` collide, and the error says which one it hit
  rather than reporting the ambiguous match as "not found".

**Rename is safe.** Identity is a permanent id (`wld_…` for a world, `session-N`, `space-N`) and the
name is display text — so a rename moves no file and strands nothing: not the active pointers, not
`session.json`'s `active_world`, not a space's `last_world`, not another user's `environment.space`, not
whatever a schema-free state doc stashed. See [decisions.md §15](../decisions.md).

### 6.6 `delete` acts on the one line

No confirmation for an explicit path. `delete <path>` resolves the target, removes it, and reports
**after**: the path the *server* resolved and what was in it (`Shell._summarize` off the pre-delete
tree). A wrong target is visible immediately rather than agreed to in advance.

**A pattern is the exception, and `!` is its confirmation.** The argument above rests on `delete` taking
an explicit path — you cannot land on it by accident. A pattern voids exactly that: `delete "s*"` looks
precise and may match nine things. So a pattern lists what it matched, deletes nothing, and acts only on
`delete <pattern> !`. The guard lands on the new ambiguity and nowhere else, so an explicit path keeps
the one-shot form that dropping the y/n prompt bought.

**An ambiguous NAME is the opposite case and gets the opposite answer.** A pattern is intentionally
plural, so confirming the set is right; a bare name is a request for one thing, so acting on three would
answer a question nobody asked. It refuses and lists the candidates with their ids. This is routine, not
a corner: asset labels are auto-generated and not unique, and the duplicates cluster on the short,
typeable ones.

The y/n prompt was dropped 2026-08-25 because it needed a **second line back on the same connection**,
which made `delete` the one command with no one-shot form — `cli say "conjure delete …"` stopped at the
question and exited before it could answer. The safety it bought was thin: `delete` is typed, refuses by
voice, and takes an explicit path, so there is no way to land on it by accident.

Guards that do hold, server-side: you can't delete the **active** world, session or space (autosave
would resurrect it), and `/admin/delete` is caller-gated — `X-Conjure-User` may only purge its **own**
namespace. A missing caller header is treated as trusted-local, mirroring `_owner_only_writes`.

---

## 7. Sessions

> **A session is an instance of an agent (the class).** The agent is a reusable template — its prompt,
> its toolset, its setup steps. A session is one filled-in copy: its transcript, its worlds, its state.

A session belongs to a user and is created by one agent, and **that agent is fixed for its life**.
"Switch agent" means "switch to, or start, a session belonging to that agent" — you never swap the agent
underneath a live conversation.

### 7.1 Disk layout

```
<data root>/                                  # ~/.local/share/conjure by default (config.DATA_DIR)
  _session.txt                                # the global live pointer: "<scope>\t<sid>"
  users/
    daniel/
      spaces/
        space-1.json  _active.txt
      agents/
        builder/                              # scope = daniel/agents/builder
          sessions/
            _active.txt                       # this scope's active session
            session-1/
              session.json                    # meta
              transcript.jsonl                # the saved dialog, one turn per line
              state/                          # agent state docs
                quest.json
              worlds/
                _active.txt
                wld_a1b2c3d4e5.json
```

`session.json`:

```jsonc
{ "id": "session-1", "owner": "daniel", "agent": "builder", "title": "Session 1",
  "public": true, "active_world": "wld_…", "llm": "Claude",
  "greeted": false, "seeded": false }        // one-shot construction flags (§7.5)
```

**Files, not a database.** Transcript as append-only JSONL gives O(1) appends (no whole-blob rewrite per
turn), crash-safety (a torn final line is dropped on load), and tailability. Meta is atomic
temp+rename. The agent server is the single writer of `transcript.jsonl` + `state/`; the world server
owns `worlds/*.json`. The writer boundaries are disjoint, so nothing double-writes.

**The session facade.** `WorldRepository(root, sessions=…)` routes every per-name op through the
scope's session, so `scope` stays the pure capability token and no call site changed. Which session:
for the **live scope**, the world server *tells* it via `set_live(scope, sid)` — set in exactly one
place (`_switch_to` / `_boot_world`), so live-session world addressing uses the server's explicit live
session rather than independently re-reading the active-session pointer. For **other** scopes it
resolves that scope's own active pointer. Cross-scope discovery walks `.../sessions/*/worlds`
directly, since it isn't about a single active session.

### 7.2 Session verbs

All go through the world server, which owns the live `(scope, session)`.

| Endpoint | Shell verb | Notes |
|---|---|---|
| `GET /sessions?scope=` | `sessions` | this scope's sessions + `available` (other users' public ones) + the global `live` |
| `POST /session/new` | `session new [title]` | runs the constructor (§7.5), then switches |
| `POST /session/switch` | `session <name>` / `session <user> <name>` | own, or a cross-user **visit** |
| `POST /session/rename` | `rename <path> <title>` | retitle only; the id is stable |
| `POST /session/delete` | `delete <path>` | refuses the live session |
| `POST /session/visibility` | `public` / `private` | visibility lives on the session; a world inherits it |

A session reference resolves **exact id → exact title → unique loose match** (`_resolve_sid`), so voice
can say a title in any case or spacing. An **ambiguous** loose match is rejected, not guessed. Titles
are checked for collision on create and rename with the same `_loose` key, because that is what makes a
title ambiguous in practice — two sessions both called "Home" used to make `_resolve_sid` return None
and report "no session 'Home'", i.e. *doesn't-exist* when it meant *matches-two*.

**Visiting.** `session <user> <name>` switches into another user's session **in the caller's active
agent** — allowed only if that session is public, and you land there as yourself (a guest: you can
inhabit it, owner-only writes still refuse edits). Discovery (`available`) is scoped to the caller's
active agent — the same lens as their own list — and excludes the caller's **whole user**, so your own
other agents never masquerade as a stranger's.

> **Never partition by one key and label by another.** Listing worlds partitioned by full scope but
> labelled by user prefix is what made a user's own `outdoor` worlds appear as belonging to "a different
> daniel". Whatever a listing shows, label each item by the key that actually gates access to it.

**The agent's world vocabulary is exactly its session's worlds.** `list_worlds` returns only the
caller's current-session worlds; `switch_world` has no cross-user reach. Cross-session and cross-user
movement is a human act at the shell, at the *session* level. The confabulation is therefore
structurally impossible — the LLM is never handed a world it can't own or reach. `list_worlds` does name
the true live world when it belongs to someone else, so a guest agent knows it's inhabiting a shared
world it can't change.

Two markers in the shell's listings: **`@`** = the one live session (you're here), shown wherever it
appears; **`*`** = your last-used session in this agent (the resume target). `@` wins if both.

### 7.3 Transcript persistence

The agent server keys the transcript on the live `(scope, session)`:

- `_sync_transcript` loads a session's saved dialog into the Director **once per session change**
  (tracked by `app.state.loaded_session`), so a restart or an agent switch resumes the conversation. It
  also restores the session's remembered `llm` when that name is still in the roster — a remembered
  choice beats the agent's priority list.
- `_persist_new_turns(before)` appends whatever the Director added this turn.
- `_persist_llm` writes the active LLM back to the meta after a `use`/`llm` switch, so it sticks across
  a restart or a switch-back.
- `clear` wipes **both** the in-memory transcript (what the LLM sees) and the persisted JSONL, without
  touching worlds, assets or the session itself.

Full replay: the whole saved transcript goes back into the Director, and the model's view is bounded by
`history_cap` rather than by trimming what's stored.

### 7.4 Agent state

Two separate schemas, deliberately: the **MCP tool contract** is generic and agent-agnostic; the **data
schema** is agent-owned data that travels with the agent like `prompt.md`.

Six director-hosted tools over named JSON docs, addressed by dotted path:

```
state_get(doc, path?)   state_set(doc, path, value)   state_merge(doc, value)
state_delete(doc, path?)   state_list()   state_schema(doc)
```

They are offered **only** when the agent declares a `state` block, and dispatched in-process against the
live session's `StateStore` (`sessions.state(scope, sid)`) — never over MCP.

Each declared doc carries up to three optional things:

```jsonc
"state": { "quest": { "seed": "state/quest.json", "schema": "schema/quest.schema.json",
                      "inject": "{quest}" } }
```

- **`seed`** — copied into the session's `state/` at construction (§7.5), a fresh mutable copy per
  instance. The agent dir's copy stays pristine (class template); the session mutates its own
  (instance).
- **`schema`** — a JSON Schema validated on every write; a violation **rejects** the write with an
  explanation, so a hallucinating LLM can't corrupt a structured doc. Best-effort: skipped if
  `jsonschema` isn't installed. No schema ⇒ free-form scratch.
- **`inject`** — wires the live doc into the prompt as JSON through the *same* `_injections` path as
  `{user}`. Opt-in per doc, to control context bloat; large state is looked up with `state_schema` /
  `state_get` instead.

Both `seed` and `schema` are resolved to parsed JSON **at agent load** (`seed_data` / `schema_data`), so
construction needs no file I/O at runtime.

### 7.5 The constructor

The ordered setup an agent runs when a new session is created — declared in `agent.json` as data, not
code. Three hooks, chained:

| Hook | Declared in | Runs |
|---|---|---|
| `world.on_create` | `world` | **every** world created in the scope |
| `session.first_world.on_create` | `session` | **only** the session's first world, after the above |
| `session.greeting` / `session.state` | `session` | **once**, at session mint |

A step is either a **sync** step — a name in `_WORLD_COMMANDS` (`show_edges`, `show_annotations`,
`set_sky_color`), compiled straight to an env patch — or a **generative** step, run for real. Both are
written the same way, as a scripted tool call with fixed args:

```jsonc
"first_world": { "name": "home", "on_create": [
  { "tool": "generate_skybox_image", "args": { "description": "a calm dawn meadow" }, "as": "sky" },
  { "tool": "set_skybox",            "args": { "image_id": "${sky.image_id}" } }
]}
```

**Steps thread data explicitly.** A step binds its result under `"as": <name>`; a later step references
it with `${name.field}`, resolved recursively through dicts and lists. A whole-value `${…}` keeps the
referenced value's type; an embedded one substitutes as text. There is **no hidden "last image"** — an
unresolved reference is an error.

**Every path that mints a session runs the opening.** `_build_first_world` is shared by `/session/new`,
an **agent switch** into a scope with no session, and a **session switch** into a session with no world.
It used to have one caller, so an agent switch minted a bare `default` from `world.on_create` alone and a
session switch hard-coded the name `home` — an agent's intended opening was reachable only by typing
`session new`, which is not how anyone arrives. Boot is the deliberate exception: it must come up
regardless and runs before any space is known, so it mints a plain placeholder (`UNSET`,
[`specs/spaces.md §4.3`](./spaces.md)) rather than calling an image model at startup.

**Fail-hard — and therefore abort is a no-op.** Generative steps run **first**, into patch ops, before
anything is created or switched (`_build_generative_ops`). **Nothing is written until every fallible step
has succeeded**, so the first failing step leaves no session directory, no world file and no moved
pointer — there is nothing to roll back, and the answer to "where does the user end up" is *exactly where
they were*, with the error surfaced. That beats silently starting a session missing its intended skybox.
Not the shell: dropping someone into command mode is a bigger disruption than the failure warrants, and
in a headset the shell means nothing.

**One limit.** The greeting is generated by the *agent server*, on its next reconcile, after the world
server has already committed — so a greeting failure cannot abort anything, and `greeted` flips to
`true` regardless so it never retries. Atomicity covers the world-server half (generative steps, world
build, state seed), not the greeting; a missing opening line is not an inconsistent session.

An unknown tool name is ignored (forward-compatible: a future step type won't hard-fail an old server).
Because image generation takes tens of seconds, the server broadcasts a *"Setting up your new world…"*
notice first, and **both** the shell's `session new` and its agent switch allow 180 s.

Order at mint: title cleaned and uniqueness-checked → generative steps → session meta written
(`greeted: false`, `seeded: false`, `public` from the agent's `session.public`) → first world built from
`world.on_create` ⊕ `first_world.on_create` ⊕ the generative ops → switch. Then, on the agent server's
next reconcile:

1. **`_maybe_seed`** — copies the declared seeds into the session's `StateStore`, never clobbering a doc
   that exists, and flips `seeded: true`. Runs before the greeting so a generated greeting can reference
   seeded state.
2. **`_maybe_greet`** — only for a fresh, un-greeted, **empty** session. A `"greeting": "…"` string is
   appended verbatim; a `{"generate": "<instruction>"}` runs one `Director.greet` turn under the floor
   lock. The result is appended to the transcript, persisted, and broadcast. `greeted` flips to `true`
   even when the greeting is empty or failed, so reconnects and re-syncs never repeat it.

**Both hooks gate on the flag being exactly `False`**, so an *absent* flag means "a legacy session — do
not retro-greet it". That makes writing the flags an obligation of **every** path that mints a session,
not just `/session/new`: `_ensure_session` (`server.py:283`) marks one fresh too, because it is what an
**agent switch** and the boot fallback mint through. Omitting them there meant a session born of an agent
switch skipped its constructor permanently — no state seeded, no greeting, with no error anywhere.

**A session minted implicitly still does not run `session.first_world`.** `_first_world_spec` has exactly
one caller (`/session/new`), so an agent switch into a scope with no session mints a world called
`default` from `world.on_create` alone — not the declared first-world name, and none of its generative
steps. Switch to `outdoor` for the first time and you get a bare `default` instead of `home` with its
sky. Tracked in [`backlogs/agents.md`](../backlogs/agents.md).

The `world.on_exit` block is declared and read by nothing.

### 7.6 Arriving: the three-rung ladder

Every arrival — an agent switch, a session switch, boot — has to answer *which world* and *which
session*. Both answers come off the same ladder ([architecture.md §1](../architecture.md)):

| Rung | World (`_activate_scope`) | Session (`_ensure_session`) | Announced |
|---|---|---|:--:|
| 1 | the newest world in the history that still exists | the newest session in the history that still exists | — |
| 2 | any surviving world in that session, newest by mtime | any surviving session in that agent, newest by mtime | ✓ |
| 3 | build the agent's opening (§7.5) | create one, which then runs §7.5 | ✓ if you'd been here |

**The pointer is an MRU list.** `_active.txt` — at both levels — holds up to ten ids, newest first, and
reading it means *the newest entry that still exists*. That single change supplies rung 1 and both of
its awkward cases for free: `delete` drops the id wherever it sits in the history rather than unlinking
the file, so the entry before it becomes current; and reading skips any number of dead entries, so
"the one before it is gone too" is not a special case. It is also self-healing against ids removed
out-of-band — a purge, a manual `rm` — and a legacy single-line file is already a valid one-entry list.

**Rung 2 is a floor, not a preference.** It catches what the history cannot: a world created but never
switched to was never activated, so it never entered the MRU, and without this rung an exhausted history
reads as *"this scope is empty"* — the mint-past-your-data bug in its other form. mtime is a weak signal
(a restore or an rsync can flatten it), which is tolerable precisely because this is a rare floor beneath
an explicit history, and because landing on it is said out loud.

**Rung 1 is silent; 2 and 3 are not.** Resuming the previous world is the pointer doing its job, and
narrating it would make every ordinary delete-and-return feel like a fault. Rungs 2 and 3 hand you
something other than what you left, so they say which and why. Rung 3 is silent in one case: a scope with
no sessions at all has never been used, so building its opening is a **first run**, not a loss.

> Before this, both resolvers had two rungs and `delete` unlinked the pointer — so deleting the world you
> were last in left no pointer, "no pointer" meant "never been here", and the arrival minted a new world
> past its surviving siblings. The session level had the identical two lines and only appeared to work
> because its fallback was the constant `MIGRATED_SID` (`"session-1"`), which usually named a session that
> existed. `MIGRATED_SID` is now only the *name* given to a brand-new session, never a resolution step.

---

## 8. The agent server

One long-lived process holding one `Shell` → one `Director` → one shared transcript. Voice and CLI are
**dumb clients**: they hold no state, parse nothing, and hold no keys.

### 8.1 The WebSocket protocol

```
ws://<agent_url>/ws?user=<name>&client=cli|voice&backlog=0|1&shell=0|1

client → server   {type:"turn", text}                    # one line: an utterance OR a command
server → client   {type:"context", …}                    # this connection's state — DATA, not a prompt
                  {type:"user_turn"|"assistant_delta"|"assistant_final"|"tool_call", …}
                  {type:"notice"|"busy"|"turn_done", …}
```

- `client` selects the command set (§6.2). `backlog=0` suppresses the transcript replay — a voice client
  can't *speak* the history, so it wants only the current context. `shell=1` opens the connection
  already in shell mode (`cli --open-shell`).
- **`turn_done` always fires**, in a `finally`, and is the client's prompt gate.
- `busy` is the single floor rejecting a concurrent turn. It is **always paired with a `turn_done`** —
  the gate has to re-arm, or a refusal wedges the prompt just as surely as the wait it replaced.
- **The receive loop reads ahead.** A submitted line runs as a task and the loop goes straight back to
  `receive_json`; a line that arrives mid-flight is answered `busy` immediately. Serialization is
  unchanged — still one command at a time — but the socket is always being read. Awaiting the handler
  inline instead is the obvious shape and it makes a slow command look like a **crash**: a generative
  session constructor can run for its full 180s, during which nothing is read, so the client shows a
  dead terminal and then flushes the entire backlog at once (observed 2026-08-28). `busy` could not
  fire for a same-connection follow-up, because the follow-up was never read.
- **World-server notices are relayed into the conversation.** The world server narrates its slow or
  surprising moments (*"Setting up your new world…"*, *"You're in a world with no room — staying put"*)
  on its own socket, which only world clients — the headset — are on. `_follow_world_state` forwards
  any `notice` it sees to the conversation hub, so the CLI or voice client that *asked* is the one told.
  Without it the reassurance was addressed to whoever was wearing the headset, which is not necessarily
  anyone.

**Shell mode is per-connection** (`Conn.in_shell`), so one client entering command mode never drags the
others in. So are `user` and `cwd`. Everything else — Director, transcript, active LLM, floor — is
shared.

Making shell mode *connection state* rather than a synthetic `conjure open shell` turn is what keeps
`--open-shell` clean: nothing lands in the shared transcript, the first `context` event is already
right, and a reconnect after a restart comes back in the mode the client was launched in.

### 8.2 Routing one line

`_handle_turn` asks the shell `as_command(text, conn.in_shell)`:

- **`None` (an utterance)** → take the floor (else send `busy`), broadcast `user_turn`, run
  `director.handle` under `floor_lock`, fan out `assistant_delta` / `tool_call` / `assistant_final`,
  persist the new turns, refresh every context.
- **a mode toggle** → flip `conn.in_shell`, notice, refresh *this* connection's context.
- **any other command** → `shell._dispatch(cmd, speaker=conn.user, permitted=…, cwd=conn.cwd,
  voice=…)`, output back as a `notice` **to this client only**, then refresh everyone's context
  (a shared LLM/agent switch changes every prompt). `conn.cwd` is read back from the shell after
  dispatch, so `cd` is per-connection.

The `mine: True` marker on the submitter's copy of a broadcast lets that client suppress the server's
echo of a line it already printed, **without** the stream going asymmetric — every client still sees
every event, so the live stream and a replayed backlog have the same shape. It has to be
per-**connection**, not per-user: one person is routinely on a CLI and the voice client at once, and a
client that filtered on the speaker's *name* threw away the other client's turns as if it had typed
them itself.

### 8.3 Following the world server

`_follow_world_state` rides the world server's `/ws` as a **passive listener** (it never sends `hold`,
so it is not counted as a render client or a space holder). Every snapshot carries `state`; each one is
reconciled:

```python
_reconcile_state(app, state):
    if state.agent != current agent:
        async with floor_lock:                       # serialize against in-flight turns
            shell._open_agent(new_agent, activate_world=False)   # following, not driving — no loop
            if nobody here asked for it: broadcast "[now in the <agent> agent — <world> · <space>]"
    app.state.live = state
    _sync_transcript(); _maybe_seed(); await _maybe_greet(); await _apply_bumps()
    await _broadcast_context()
```

**Structured concurrency.** The follow loop runs in the *same task* that owns `Shell.session`
(`_shell_and_follow`), because the Director's MCP session must be entered, exited and re-bound all in
one task — a cross-task `aclose()` raises an anyio cancel-scope error. Turns run in the connection
tasks but only *call* the session, which is safe; only re-binds enter and exit it, serialized against
turns by `floor_lock`.

**Announcing an unasked change.** The most common cause of a surprise agent change is co-location: an
AR client votes its capture against the geo candidates, the world server matches a space, and joins that
space's last-active world in whatever scope owns it. Your room can hand you a different agent. That is
intended, but it used to happen in silence — you kept talking and something else answered. `state`
carries no reason, so the notice names the destination (world and space are the evidence that makes a
room match recognisable as one).

`app.state.expect_agent` is the hook's claim on the echo of a switch **this** server asked for, so it
isn't announced on top of the client's own narration. It is consumed only when the switch it named
actually lands — snapshots arrive for all sorts of reasons, and clearing on an unrelated one would drop
the claim before the real change showed up. `_switch` also clears it in a `finally`, because an
already-active scope answers `unchanged` and broadcasts nothing, and a stale claim would muffle a
genuine unasked switch to the same agent later.

### 8.4 Switching agents from a client

`agent <name>` must **not** re-bind the Director from a connection task (a cross-task MCP teardown).
Instead the hook asserts the target scope on the world server (`POST /scope/activate`); the world
server's broadcast makes *this* server's follower re-bind in the owning task, and every other client
follow. A client agent-switch is just another pointer move through the single source of truth.

The hook then waits (~10 s, polling) for the re-bind to land before returning, so the client's next
context reflects the new agent instead of lagging. It holds no floor while waiting, so the follower can
take it.

A new agent gets a **fresh Director**, hence a fresh transcript, which `_sync_transcript` then refills
from the new session's saved dialog.

`Shell._open_agent` is **close-old-then-open-new**: the MCP client's anyio cancel scopes must unwind
LIFO in the same task, so opening on top and closing underneath raises. If the new agent fails to start,
the previous one is reopened so the shell isn't stranded. The current LLM is carried across when the new
agent's roster allows it.

### 8.5 Per-turn identity

Identity used to be fixed at MCP launch, so with a shared agent server every turn acted as the launch
user — a real permission bypass (`--user guest` could edit daniel's world, and `list_worlds` showed
daniel's worlds as guest's).

Now the director calls `set_caller(user, scope)` at the start of each turn. The MCP server threads that
speaker into **both** the request headers (`X-Conjure-User` / `X-Conjure-Scope`, for the owner gate) and
the request **body** scope (for "your worlds" and asset ownership). `_post_patch` carries identity too
and raises a clean owner-only message on a 403 — the patch path previously sent no header and so bypassed
the gate entirely. Turns are serialized by the single floor, so one process-global caller is safe
against interleaving. `set_caller` is exempt from the capability gate and is in no agent's allow-list.

---

## 9. The one shared reality

> There is exactly one active `(space, world)`, and `agent = agent_of(world.scope)`. No per-user worlds,
> no forking — the shared experience *is* the feature.

### 9.1 One session pointer

`<data root>/_session.txt` holds `"<scope>\t<sid>"` — the live **session**, globally. Everything else is
derived: the agent from `agent_of(scope)`, the active world from that session's `worlds/_active.txt`,
the space from the world's `environment.space`. The old per-user `_last_agent.txt` is retired to a
migration-only fact; `GET /agent/last` now just answers `agent_of(active_scope)` and its `user`
parameter is vestigial.

Three things move the pointer:

1. **Boot-restore** — `_boot_world` reads the pointer, makes that session live (`worlds.set_live`), and
   loads its active world. A **provisional** guess for the window before any headset establishes a
   space, so voice/CLI/desktop and the renderer have something coherent.
2. **Headset establish / relocalize** — `/space/select` (§9.3). Physical authority; the first headset to
   match a space **anchors** it, and spatial truth supersedes the temporal guess.
3. **Explicit switch** — a `world`/`session`/`agent` verb, or an agent-side `switch_world`/`new_world`.

### 9.2 Reconciliation: one broadcast, two consumers

`_live_state()` is the canonical "what's live" — identifiers only, no world doc:

```jsonc
{ "scope": "daniel/agents/builder", "agent": "builder", "session": "session-1",
  "world": "animal-house", "world_id": "wld_…", "owner": "daniel",
  "public": true, "space": "daniel/space-1" }        // space = "<void>" for an outdoor world
```

It is served flat at `GET /state` and mirrored into **every** `/ws` snapshot under `state`, beside the
world doc and top-level `owner` the renderer already used. So one broadcast feeds both consumers: the
headset renders the world, the agent server binds its brain to `agent`.

`world` is the display **name**; `world_id` is the permanent identity a client should key state on, so a
rename doesn't read as a world switch.

### 9.3 Space selection and the admission gate

Two stages. `POST /geolocation` is **read-only** discovery: the AR client reports coarse location and
gets back every geo-near candidate space across all users, each with its surface constellation, to
disambiguate by registration. `POST /space/select` commits, and what happens depends on whether the
active space is already **claimed** (`_occupied()` — any AR headset holding it):

- **Unclaimed** — this AR user *establishes* the space (first-in claims it):
  - **matched** → join that space's `last_world` if it exists and is reachable; else mint a world in it
    (subject to `_may_create_world_in`); else refuse (a private space with nothing to join).
  - **no match** → "somewhere new": mint a fresh geo-stamped `space-N` + default world owned by the
    connecting user. Born *with* its location — no separate stamp path.
- **Claimed** — the admission gate: an AR joiner must match the **active** space. Matched → admitted
  (co-location join, no world change). Anything else → refused; nothing minted, nothing switched, the
  client stays in passthrough.

A client commits **once per claim epoch** (idempotent by `cid`) so GPS jitter can't re-open its choice;
a *different*, co-located client still gets candidates and must vote. On admit/establish the client
declares `hold` over `/ws` and becomes a `_space_holder`; the space unclaims when the last holder leaves
or disconnects. Voice/CLI/desktop never reach `/space/select` (no AR session), so the gate governs AR
headsets only.

### 9.4 Permissions

Three gates, all owned by the world server. **The agent server never makes a permission decision** — it
is told the new state and re-gates its own clients from it.

| Gate | Applies to | Enforced at |
|---|---|---|
| **surface-match** | AR headsets only; VOID-exempt | `/space/select` — a headset can only move the pointer to the world for the room it is physically in |
| **privacy** | everyone | the live **session**'s `public` flag: the `/ws` join gate, `_regate_clients`, and the agent server's `_permitted` |
| **edit-ownership** | everyone | `_owner_only_writes` — only the active world's owner may mutate it |

`_OWNER_ONLY_PATHS` gates the mutating routes: `/reset`, `/patch`, `/space/capture`,
`/space/realign`, `/texture_surface`,
`/style_surface`, `/place_asset`, `/place_cached_asset`, `/place_image`, `/set_skybox`,
`/set_grounded_skybox`, `/edit_image`, `/outpaint_image`, `/skybox_from_image`, `/module`,
`/module/dismiss`, `/manipulate`. A missing `X-Conjure-User` header is treated as the owner (interim
convenience for the direct dev CLI).

Membership is an **exact string match** on the request path, so renaming a route without updating the
set silently *ungates* it rather than 404ing — a route-inventory test would not notice. A test pins
membership for exactly that reason (`test_every_geometry_and_scene_write_is_gated`), and also asserts
every gated path is a real route, so a typo can't gate nothing.

**Navigation is deliberately not gated.** Anyone may create or switch worlds and everyone comes along —
but a created or switched-into world is in the caller's **own** scope, so the caller becomes its owner
and only then can edit it. That lets a guest spin up and build their own worlds with everyone present,
while another user's curated world stays protected.

**Visibility lives on the session; a world inherits it.** Per-world `public` is retired.
`/worlds/visibility` is kept only as the surface `set_world_visibility` still calls — it sets the target
scope's active *session* public or private, and, when the live session goes public, publishes its
world's private assets so visitors can load the whole scene.

**Never disconnect; demote.** When a move makes the live session inaccessible to someone present, they
are demoted, not dropped:

- **Headsets** — `_regate_clients` moves every non-owner out of `clients` into `_blocked` (kept, so a
  later go-public can re-admit) and sends `evicted`, so the render client blanks to passthrough.
  `_readmit_clients` is the inverse: back into `clients` plus a fresh snapshot, no page reload. A
  refused-at-join guest gets the **same** `evicted` signal, so entry and eviction behave identically.
  Bumping a headset also drops it from `_space_holders`, so the sweep ends with `_unclaim()` — a no-op
  while the owner is still standing there, but otherwise it frees the space *and* reopens selection.
  Without that the space stayed committed for the claim epoch: the evicted headset's `cid` was already
  in `_selected_cids`, so its re-vote returned `selected: False` and it sat in passthrough until the
  wearer physically left AR. Eviction has to be recoverable by looking around, not by taking the headset
  off ([specs/spaces.md §6.1](./spaces.md)).
- **CLI/voice** — `_apply_bumps` forces non-permitted clients into shell mode with a notice, and a
  private session's *dialog* is never broadcast to them (`_conv_broadcast` filters on `_permitted`).
  `Conn.bumped` distinguishes our bump from a shell the user chose, so going public only undoes what we
  did — and a connection already in a shell of its own is never claimed as a bump.

**Shared-effect verbs are refused for a non-permitted speaker** (§6d of the sessions design): `session
switch`/`new`, `agent <name>`, `llm <name>`, `world`, `clear`, `public`/`private`, and `delete`. Any
**permitted** participant may drive the shared session — control isn't a scarce token, so there is
nothing to hand off and no owner/drivers/handoff apparatus. Accepted consequence: a permitted guest can
switch everyone into their own session. Session **management** verbs act on the *speaker's own scope*
(`Shell._scope()` uses `_acting`), so a guest can only touch their own sessions.

---

## 10. Front-ends

Both clients are thin: one WebSocket, send each line verbatim, render what comes back. **All** command
logic — the wake word, the mode phrases, routing, dispatch — is server-side.

### CLI (`cli.py`)

```bash
conjure-cli                                     # full-screen REPL — the usual way in
conjure-cli say "put a tree in front of me"     # one-shot, then exit
conjure-cli --open-shell                        # open straight into shell mode
conjure-cli --open-shell say "delete ~/spaces/old"    # one-shot command, no wake word needed
conjure-cli --user alice                        # who you connect as
conjure-cli -v                                  # show tool calls and library logs
```

A full-screen app: a status bar pinned to the top, the conversation scrolling in the middle, the prompt
pinned to the bottom. It owns the screen because the status bar has to stay put while output flows past
it; the trade is that the pane does its own scrolling (PgUp/PgDn, End returns to the live tail). While
you're scrolled back the status bar shows how much you've missed (`↓ 12 new · End`) — load-bearing,
because in the alternate screen most terminals turn one wheel notch into a PgUp, and a detached pane
otherwise looks exactly like a frozen one.

**The client formats its own prompt from context DATA**, never a server-authored string — a voice client
renders none. `prompt_from_context` gives `conjure:daniel.builder.claude>` or, in shell mode,
`conjure:daniel.shell ~/agents/builder>` — the cwd shown absolute, with `~` for your own home, so it
never lies about where you are. `status_from_context` renders the working
clock, agent·llm, turn count, and the context breakdown, degrading in three stages (full → compact →
dropping fields from the least-important end) rather than wrapping.

The **one** client-side special case is the quit words (`:q`, `quit`, `exit`, `bye`, …), which end the
*program* in agent mode. In shell mode `exit` is a server command and is forwarded.

### Voice (`voice.py`)

```
mic → Silero VAD → Whisper STT → WebSocket → agent server → WebSocket → Kokoro TTS → speaker
```

PipeCat is only **ears and mouth**: STT, TTS, VAD, end-of-turn detection, and mute-while-speaking echo
mitigation. No LLM, no Director, no keys. A completed spoken turn is sent as `{type:"turn", text}`; the
server's `assistant_delta` / `assistant_final` / `notice` events are spoken, preserving the streaming
cadence. `backlog=0`, because a fresh connection shouldn't get the whole transcript spoken at it.

`--wake-word` is a separate, purely voice-input concern: a mic-activation gate ahead of the socket.
Bare wake word arms for the next utterance; wake word plus text submits the remainder immediately. It
**ships no word at all**: it is opt-in, and which word suits a room is the user's call, not ours. So
unlike the shell's escape — which must work out of the box and therefore carries its mis-hearings — the
gate is off until you name one:

```bash
conjure-voice --wake-word computer            # one word
conjure-voice --wake-word computer,computa    # …and its mis-hearings, in one go
```

The flag takes a **comma-separated list** so covering a new mis-hearing needs no env var. (It used to
take one word and match the literal string `"computer,computa"` if given a list — silently wrong.) A
standing default can still be set via `CONJURE_VOICE_WAKE_WORDS` > settings `voice_wake_words`; naming a
single word that is already in that list expands to the whole of it, and anything else is literal.

**The two gates must be distinct, and it is enforced.** They do different jobs and compose in one
breath: the mic gate decides whether you are addressing Conjure at all, the shell's wake word decides
whether what you said is a command.

```
"computer conjure where am I"  →  mic strips "computer"  →  "conjure where am I"  →  shell: `where am I`
"computer make a tree"         →  mic strips "computer"  →  "make a tree"         →  content, to the LLM
```

Because the gate **consumes** its word before anything downstream sees the line, sharing one makes
spoken shell commands unreachable — `"conjure where am I"` would arrive at the shell as `"where am I"`,
which is content, and you would have to say the word twice. `_make_wake_gate` therefore **refuses to
start** on an overlap (`wake_word_conflict`) rather than warning: the failure is silent, and its symptom
looks like a broken shell rather than a misconfigured gate.

#### The speech stage — what a voice client hears

An LLM writes for a screen. Asterisks (`**bold**`, `*shrugs*`, bullets) and emoji are free there and are
noise or silence through a TTS engine. `conjure/speech.py` is the one place that turns written text into
**speakable** text:

| Stage | Does |
|---|---|
| `asterisks` | removes every `*`, then collapses the runs of spaces and leading indents that leaves — newlines stay, they pace the speech |
| `emoji` | replaces each emoji with its Unicode name plus the word "emoji" (🍦 → *soft ice cream emoji*), so the character isn't silently dropped along with whatever it meant |

A **run** of adjacent emoji codepoints becomes one phrase named for the first — a joined family or a
skin-toned thumb is one picture to a reader, and naming every component would bury the sentence. The
emoji test is explicit pictographic ranges rather than the `So` Unicode category, which would also catch
©, ® and ™.

Two properties make this safe to extend, and both are why it lives at fan-out (`_conv_broadcast`) rather
than in the director:

- **Per-connection, not per-turn.** The same reply reaches a CLI written and a voice client spoken.
- **A rendering, not an edit.** Nothing is persisted or replayed through it — the transcript stores what
  the LLM wrote, so switching clients mid-conversation never rewrites history.

Only LLM prose is rewritten (`assistant_delta`, `assistant_final`). A `user_turn` is the speaker's own
words echoed back and a `notice` is server-authored; rewriting either would make a client hear something
different from what it said. Adding a case is adding a function to `_STAGES`.

---

## 11. Boot and lifecycle

**Order-independent, lazy binding.** No handshake. Discovery is static URLs, one per downstream hop:
clients → `agent_url` → agent server → (MCP) → world server → `world_url` → headsets.

- The **world server** boots standalone: settings template, home migration, catalog, one-time on-disk
  relocations, then `_boot_world`.
- The **agent server** opens its `Shell.session` (spawning the agent's MCP server) and then subscribes
  to the world server, reconnecting with backoff. The lifespan waits for the shell to be ready and
  re-raises a startup failure (no keys, bad agent def).
- **Clients** connect to `agent_url` and retry.

**Which agent opens.** `Shell.session(agent=None)` resumes the live one via `GET /agent/last`. That call
**waits** for the world server rather than guessing past it (`LAST_AGENT_WAIT`, 5 s, polled at 0.25 s),
because the two race at startup — the world server runs migrations before it binds — and the answer
decides which agent's MCP server gets spawned. It prints one line while waiting and one if it gives up.

**Explicit intent vs resume.** An explicit `--agent X` is an instruction, so it is asserted at the world
server (`activate_world=True`). A **resumed** agent never is: at boot the world server already restored
the live scope from the session pointer, so asserting what we just read back at it is either a no-op
(`/scope/activate` answers `unchanged`) or — when `_last_agent` had to fall back — a guess overwriting
that truth and losing the session you were in. Staying quiet costs nothing; the follow loop adopts the
live agent on the first snapshot.

**Restart matrix** (all consistent, because the world server is the single writer):

| Restarted | What happens |
|---|---|
| World server alone | re-restores the pointer from disk; the agent server's follower reconnects and re-reads `state`; headsets' `/ws` reconnects and re-snapshots |
| Agent server alone | re-opens the shell, re-subscribes, re-binds to the current agent; **the transcript is reloaded from `transcript.jsonl`** |
| A client alone | re-subscribes; context, prompt and backlog reflect the current state |

**Autosave.** A background task writes the live composed doc whenever its `rev` advances (~1 s
debounce). `_save_active` **splits** it: real-surface geometry + boundary → the active **space** (in the
space owner's scope, so a world built in someone else's space writes its walls back to them); placed
objects, display prefs and per-surface style overrides → the **world** doc. It also records
`last_scope`/`last_world` on the space, which is what `/space/select` resumes on a return visit. A
room-less world has no space to split out and is saved whole — as `<void>` if it is deliberately
outdoor, and with the ref **omitted** if it is merely undecided ([`specs/spaces.md §4.3`](./spaces.md)).

**Autosave never resurrects a deleted session.** `_save_active` returns early when the live session's
*directory* is gone — purged by `reset agent`, or by a session delete — because writing the live world
back would re-create exactly what was just removed. Keyed on the directory rather than on `session.json`:
a session can legitimately hold worlds before any meta is written, and treating that as deleted would
silently disable autosave for it. This is what lets `reset` be a plain purge followed by a re-entry,
with no ordering dance.

---

## 12. Surface reference

### World server — orchestration routes

| Endpoint | Purpose | Owner-gated |
|---|---|---|
| `GET /state` | the canonical live-state identifiers | no |
| `GET /agent/last` | the live agent (subsumed by `/state`) | no |
| `POST /scope/activate` | make a world in a scope live (agent switch) | no |
| `POST /agent/reset` | wipe an agent back to never-used; re-enters it if it was live | no |
| `GET /sessions?scope=` | a scope's sessions + visitable + live | no |
| `POST /session/{new,switch,rename,delete,visibility}` | session verbs | no |
| `POST /worlds/{list,new,switch,delete,rename,visibility}` | world verbs (session-local) | no |
| `POST /space/{select,rename,visibility}` · `POST /geolocation` | space selection + admin | no |
| `POST /admin/{tree,show,delete}` | the shell's namespace view + purge | `delete` is caller-gated |
| `ws /ws?user=` | render clients **and** the agent server's follower | — |

### Agent server

| Endpoint | Purpose |
|---|---|
| `GET /health` | `{ok, turn_active, connections}` |
| `ws /ws?user=&client=&backlog=&shell=` | the one client connection |

### Environment injected into an MCP server

| Variable | Meaning |
|---|---|
| `CONJURE_URL` | the world server base URL |
| `CONJURE_SCOPE` | `<user>/agents/<agent>` — launch identity and catalog scope |
| `CONJURE_TOOLS` | the tool allow-list (unset = unrestricted; `""` = none) |
| `CONJURE_ACCESS` | `all` \| `read` |

---

## 13. Checklist for a new agent

1. `agents/<name>/agent.json` — `description`, `prompt_file`, `llms`, one `mcp_servers` entry with an
   explicit `tools` list, and `context`/`dynamics` if it wants them.
2. `agents/<name>/prompt.md` — **all** of the agent's text, including the framing around any injection
   it references (`{user}`, `{#context}…{/context}`, a state doc's `{…}`) and any per-LLM `{#llm}`
   section (§5.3a) — whose branch names must be in the roster *and* in this agent's own `llms`.
3. Optional: a `session` block (greeting, `first_world`), a `world.on_create` chain, a `state` block with
   seed and schema files.
4. Confirm it loads (`load_agent("<name>", registry=load_server_registry())` fails loud on a typo'd
   server, tool, or dynamic module).
5. `agent <name>` in the shell — it should relaunch the MCP server, land you in the new scope's world,
   and start (or resume) that scope's session.
