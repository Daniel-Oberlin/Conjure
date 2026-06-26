# Persistence & scoping model

**Status:** DESIGN (documented now; built incrementally — see §6). Spans more than the asset library —
it covers worlds, agent scoping, and how the stores compose. See also `asset-library-plan.md`
(the asset store) and `decisions.md` #7 (the capability/sandbox model this scoping rides on).

The idea: **one persistence service, scoped per agent, hosting several *typed* stores.** Each agent
(the builder, a future role-playing "dungeonmaster", …) operates inside its own namespace and never
sees another agent's private content. Assets, worlds, and arbitrary agent state are *different* kinds
of data with *different* stores — but they share one scoping layer.

## 1. Namespace

```
<visibility>/<agent>/<category>/<name…>

  private/builder/worlds/bladerunner1
  private/dungeonmaster/worlds/castle-quest/dining-hall   ← worlds may nest (a hierarchical name)
  private/builder/assets/<hash>.glb
  public/builder/assets/<hash>.glb        ← documented use case; not built yet
  private/dungeonmaster/worlds/...         ← future agent; never sees builder's private content
```

- **visibility** — `private` | `public`. The real axis (everything is "scoped"; the question is who
  can see it). Private = only this agent; public = world-readable, this-agent-writable.
- **agent** — `builder`, `dungeonmaster`, … the owning agent.
- **category** — `assets` | `worlds` | `state`. **Routes to the typed store** (§4–5).
- **name** — content hash (assets), or a chosen name (worlds). A world name may be a **hierarchical
  path** (`castle-quest/dining-hall`) so an agent can organize its worlds in a tree. Each segment is
  slug-normalized independently (case/spaces/underscores/hyphens interchangeable) and traversal
  (`..`) is rejected, so a world is always confined to its scope. The trust boundary is still the
  `<visibility>/<agent>` prefix — that's runtime-injected and never an LLM argument; the world *path*
  below it is user/agent-chosen and freely structured.

## 2. Scope is a capability, not a parameter (the security crux)

What makes this *securely* scoped: the scope is **bound by the trusted runtime per agent and injected
server-side — never a tool argument the LLM fills in.** If a tool took `scope=…`, the model (or a
prompt-injection hidden in some asset's text/notes) could pass another agent's scope and read it.

- The MCP / agent runtime holds the agent's scope (from how the agent was launched / its capability
  grant — this is the same capability model as `decisions.md` #7).
- It hands the store a **scoped handle** (`store.for_scope("private/builder")`) with the visibility
  predicate baked in, so the agent *physically cannot* widen its own view.
- **LLM-visible tools have no scope parameter.** Scope is ambient and invisible to the model.
- **Reads** = the agent's own private scope ∪ any granted public scopes. **Writes** = its own private
  scope. (An agent creates into private; it can read public; it cannot write another agent's space.)

On the asset store specifically: scope is a property of the **catalog entry** (the searchable row +
embedding), not the bytes. Content-addressed bytes still dedupe globally on disk; visibility, search,
and curation are per-scope, so agents never see each other's catalog even for identical content. (If
we ever want zero cross-talk at the byte level too, bytes can be partitioned per scope — not needed
now.)

## 3. Public sharing semantics — reference in place

**Decision:** when an agent *uses* a **public** asset, it is **referenced in place** (the world points
at it where it lives) — *not* copied into the agent's private scope.

This is safe because assets are **content-addressed**: the bytes behind a given hash are immutable by
construction — "changing" a public asset really means producing a *new* hash. So a public reference
**cannot be silently mutated** underneath you. The only real risks of referencing are:
- the public entry being **unpublished / removed** (the target disappears), or
- its **metadata/curation drifting** (label/notes/tags change).

**Copy-to-private** is the opt-in guard against exactly those: copy the (already-immutable) bytes'
catalog entry into your private scope to own a stable, self-curated copy. So "reference now, copy if
you need to pin it" is a clean default.

## 4. Two stores, one scope: assets vs. worlds

A **world** and an **asset** are different *kinds* of thing, so they live in different stores (same
reasoning as "segregate by domain" in `asset-library-plan.md` §5):

| | **Asset store** (built) | **World / document store** (new) |
|---|---|---|
| Identity | **content hash** (immutable) | **name/path** — `worlds/bladerunner1` (mutable) |
| Mutability | write-once blobs | edited over time (patches, snapshots) |
| Versioning | new bytes ⇒ new asset | versioned / branchable doc history |
| Query | **similarity / semantic + intent** | by-name lookup, list, metadata filter |
| Embeddings | central (the whole point) | optional ("find my cyberpunk worlds"); not core |
| Role | **leaf content**, referenced | a **document that references** many assets |

So `bladerunner1` is a **named, mutable, versioned document** that *points at* assets by id — it does
**not** live in the content-addressed media catalog. Its natural home is an **evolution of the existing
`WorldStore`**: it now loads a single saved world and autosaves it (single-world durability — §6); the
scoped world store adds named **save / load / list / switch** under a scope. It's *simpler* than the asset catalog (no
embeddings, no similarity search) precisely because worlds are named documents, not searchable media.

A third **generic `state` store** (agent memory / settings — a scoped KV/doc store) can appear later
under the same scheme if an agent needs arbitrary persistence.

## 5. Composition — the persistence service

```
persistence service ── scope / visibility enforcement (shared, capability-bound)
   ├── /assets/  → AssetLibrary        content-addressed media + embeddings + search
   ├── /worlds/  → WorldStore++         named, mutable, versioned docs → reference assets
   └── /state/   → KV/doc store         agent memory/settings (if/when needed)
```

This is the **"shared toolkit + domain stores"** shape (asset-library-plan.md §5) with **scope as
additional shared machinery**: the namespace + access control is written once and applies to
*everything* an agent persists; the typed stores differ in schema and access pattern. A world in
`private/builder/worlds/bladerunner1` references assets in `private/builder/assets/…` — a cross-store
reference *within one scope*. The `<category>` segment is the dispatch key.

So: the asset store does **not** become the general persistence store. The general persistence need
(worlds, agent state) is met by **separate stores** that share the scoping layer and the service.

## 6. World lifecycle — durability, constructor, undo

How a world comes into being, gets set up per agent, persists, and is walked back. Decisions settled
2026-06-25.

**Durability (autosave on change).** The active world is written to disk whenever its `rev` advances.
A background poll debounces naturally — a multi-patch turn or a room-capture flurry coalesces into one
write — and touches no `apply_patch` call site. On boot the saved world loads (corrupt/missing → the
sample). There is **no** rebuild-from-cache: a lost world doc is lost, so it's backed up like any file.

**Constructor / destructor = a macro of existing server commands.** A per-agent setup is an ordered
list of the *same operations the director already calls* (`show_edges`, `style_surface`, `set_skybox`,
…), declared in `agent.json` — not per-agent code, not a static state blob (a command can encode
dynamic setup a blob can't). The builder's constructor sets edges visible; the future dungeonmaster's
sets them off — same mechanism.

```jsonc
// agents/<agent>/agent.json
"world": { "on_create": [ { "cmd": "show_edges", "args": { "on": true } } ], "on_exit": [] }
```

**It runs at creation only, and bakes into the doc.** Because display state (`environment.room.edgesVisible`,
`room.annotations`, sky, …) is *in the world document*, **load = restore** is sufficient — the
constructor is **not** re-run on load, so it never clobbers a world's later customizations. The
destructor (`on_exit`) is for persistence/cleanup on switch-away; often empty since autosave handles
saving.

**Default world = blank base + constructor.** First instantiation of an agent (no worlds yet) creates
`default`, runs `on_create`, makes it active. The constructor *is* the per-agent starting definition;
no separate seed file is required (an agent may still ship a richer seed doc if it wants pre-placed
content).

**The real room is a shared live layer, not per-world.** Captured surfaces (`meta.real` entities) are
the same physical room regardless of the active world, so they're a live layer merged into whatever
world is active — *not* snapshotted into each world. "Edges on/off" is then a pure per-world display
preference over that shared layer. **This was decided but NOT yet built** — the initial multi-world
store persists the room *inside* each world doc, which caused a recurring class of "live state frozen
per-world" bugs (stale/sparse rooms, re-capture churn, the authority lockout). The full design — shared
geometry + per-world style overlay, with the durable/session split — is in **`shared-room-layer.md`**.

**Undo/redo rides the inverses we already compute.** `apply_patch` already records an inverse for
every op (`world.py`); undo is a cursor over that history plus a tool, not a new subsystem. The real
work is **action grouping** (one director turn = one undoable unit, not N patches) and **origin
filtering** (never undo an automatic room-recapture or embedding write-through). MVP: session-level,
in-memory, voice-accessible ("undo that"). **Durable cross-restart history and named checkpoints /
branching are a separate, later feature** (a different shape: snapshots, not an inverse log).

## 7. Status — built vs. deferred

- **Built:** the asset catalog (`conjure/library.py`), now **scoped** (a `scope` field per entry;
  single-agent today). The **scoped, hierarchical world store** — `WorldRepository` (named/nestable
  worlds at `.cache/worlds/<scope>/<name>.json`, normalized recall, per-scope active pointer),
  autosave-on-change, boot-into-last-active-or-`default`, the `list/new/switch/delete` endpoints +
  MCP tools, and the `agent.json` `on_create` constructor run at world creation. Scope is carried in
  the request body (server-side default for now; capability-injected when the second agent lands).
- **Next:** session **undo/redo** (rides the existing inverses; blocks nothing) — independent, any time.
- **Deferred until the second agent (e.g. the RPG dungeonmaster) actually lands:**
  - scope **enforcement** — scoped handles + capability injection by the runtime (no scope in LLM
    tools), per §2;
  - **public** visibility + reference-in-place + copy-to-private;
  - any `state` store;
  - durable world **versioning / checkpoints / branching**.

Build order is incremental: single-world durability → scoped multi-world + constructor (both done) →
undo/redo whenever → durable versioning later. Each step's primitive is reused by the next (the saved
active world *is* what the repository wraps with naming + scope).
