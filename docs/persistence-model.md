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
<visibility>/<agent>/<category>/<name>

  private/builder/worlds/bladerunner1
  private/builder/assets/<hash>.glb
  public/builder/assets/<hash>.glb        ← documented use case; not built yet
  private/dungeonmaster/worlds/...         ← future agent; never sees builder's private content
```

- **visibility** — `private` | `public`. The real axis (everything is "scoped"; the question is who
  can see it). Private = only this agent; public = world-readable, this-agent-writable.
- **agent** — `builder`, `dungeonmaster`, … the owning agent.
- **category** — `assets` | `worlds` | `state`. **Routes to the typed store** (§4–5).
- **name** — content hash (assets) or a chosen name (worlds).

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
`WorldStore`**: today that loads a single `sample_world.json` and is ephemeral; world persistence adds
named **save / load / list / version** under a scope. It's *simpler* than the asset catalog (no
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

## 6. Status — built vs. deferred

- **Built:** the asset catalog (`conjure/library.py`) — currently **unscoped** (single-agent).
- **Now (cheap, when Phase 2 touches the schema):** add a `scope` field to the asset catalog so
  entries carry visibility/owner — the data seam, ahead of enforcement.
- **Deferred until the second agent (e.g. the RPG dungeonmaster) actually lands:**
  - scope **enforcement** — scoped handles + capability injection by the runtime (no scope in LLM
    tools), per §2;
  - the **world store** (named save/load/list/version on `WorldStore`);
  - **public** visibility + reference-in-place + copy-to-private;
  - any `state` store.

Building the enforcement and the world store before there's a second agent or a save-world request
would be speculative; the model is documented so the seams (a `scope` field, capability-bound handles,
category dispatch) are in place when those features arrive.
