# Dynamic modules → first-class, extensible (mirror the agents pattern) — plan

**Status:** ALIGNED (2026-08-21), building. Make dynamic modules discoverable/extensible exactly like
agents: each module is a folder under a top-level `dynamics/` dir; user modules resolve on a search
path (user shadows bundled); agents declare which modules they require; the director discovers its
scoped modules via an injected catalog. Replaces the flat `client/water.js` + `client/dynamic-modules.js`
files and the hardcoded server `DYNAMIC_MODULES` dict.

## Aligned decisions
1. **Director discovery = injected catalog.** Auto-inject a `dynamics://available` context resource each
   turn (like `room://current`), built from the agent's resolved modules' `description` + `config_schema`.
   `conjure_module` stays one generic tool, scoped server-side. (No dynamic tool schema; no discovery ritual.)
2. **User modules live in the CONFIG dir** — `~/.config/conjure/dynamics/` — a true mirror of user agents
   (`config_dir/agents`).
3. **Client loads only the active agent's scoped modules** (`<script>` per module, re-injected on agent switch).

## Layout (mirror `agents/`)
```
dynamics/                         # bundled, sibling to agents/  (BUNDLED_DYNAMICS_DIR = ROOT/dynamics)
  fireflies/
    module.json
    fireflies.js
  water/
    module.json
    water.js
    assets/…                      # per-module assets travel with the module
```
User modules: `~/.config/conjure/dynamics/<name>/…` (shadow bundled by name).

## `module.json` (mirrors `agent.json`) — the manifest, replaces the hardcoded `DYNAMIC_MODULES`
```jsonc
{
  "component": "water",            // the A-Frame component the entry registers
  "entry": "water.js",             // client script(s) to load (string or list)
  "tier": "A|B|C",
  "anchor": "free",                // free | surface | volume | ambient
  "singleton": false,
  "face_user": true,               // free-standing flat content faces the viewer at creation
  "default_pos": [0, 1.4, -1.2],
  "description": "…",              // one line → the director catalog
  "config_schema": {               // params the LLM may set: {type, default, desc}
    "damping": { "type": "number", "default": 0.996, "desc": "→1 = long-lived ripples" }
  }
}
```

## Resolution — `resolve_dynamics_path()` mirroring `resolve_agents_path()`
`env CONJURE_DYNAMICS_PATH > settings["dynamics_path"] > [<config_dir>/dynamics, BUNDLED_DYNAMICS_DIR]`,
user-first. A loader scans the path, reads each `module.json` → a `DynamicModuleDef` registry (like the
agents loader). Config additions mirror agents: `BUNDLED_DYNAMICS_DIR`, `resolve_dynamics_path`,
`DYNAMICS_PATH`, `settings.json` key `dynamics_path`.

## Agent scoping — `agent.json` gains `"dynamics": [...]`
```jsonc
"dynamics": ["water", "fireflies"]
```
- **Required:** the agent FAILS to load if any listed module isn't found on the search path.
- **Scoping:** modules not listed are unavailable to that agent (like the MCP tool allowlist).

## Director discovery (decision 1, detail)
- On agent load, resolve `agent.dynamics` against the path (fail if missing).
- Auto-inject `dynamics://available` into the director's context (mechanism = existing context injection):
  each scoped module rendered as `name — description; params: k(default)…` from its `config_schema`.
- `conjure_module(module, config, …)` is **scoped server-side**: `/module` validates the requested module
  against the ACTIVE agent's `dynamics` (the world server can read the active agent def) and rejects
  out-of-scope names. Soft (catalog) + hard (endpoint) scoping together.

## Server / client split
- **World server**: loads ALL discovered modules → serves their JS (`GET /dynamics/<name>/<file>`,
  mtime-versioned) and runs `/module` (placement uses the manifest: component/anchor/default_pos/
  face_user). Registry is discovered, not hardcoded.
- **Agent server**: loads the AGENT's subset → injects the catalog + enforces required-present.
- Both use the shared `dynamics` loader.

## Client loading (decision 3)
`index.html` no longer hardcodes module `<script>`s. The server injects `<script src="/dynamics/<name>/
<entry>?v=<mtime>">` for the **active agent's** modules, re-injected on agent switch (so a switch swaps the
available components). "Not scoped → not available" holds client-side too.

## Migration
- `client/dynamic-modules.js` (fireflies) → `dynamics/fireflies/{module.json, fireflies.js}`.
- `client/water.js` → `dynamics/water/{module.json, water.js}`.
- `builder/agent.json` gains `"dynamics": ["fireflies", "water"]`.
- Server `DYNAMIC_MODULES` dict → the loader; `/module`, version-stamping, and `index.html` injection
  updated. `ConjureBus` (in conjure-client.js) stays global (shared bus infra, not a module).

## The spec/contract doc — `docs/dynamic-module-spec.md` (living, written as part of this work)
The authoritative contract for implementing a module:
- Folder + `module.json` field reference.
- **Client component contract:** lifecycle `init / update / tick / remove`; **provided** — `this.data`
  (from `config_schema`), `window.ConjureClock`, `window.ConjureBus` (`emitShared`/`on`), the renderer,
  `this.el`, the anchor/placement + `face_user`/composable `billboard`; **required** — register the
  component, fully dispose on `remove` (GPU/textures/RAF/subscriptions).
- Placement & anchors; the shared-event bus; how the director discovers + conjures.
- Worked examples: fireflies (tier-A minimal), water (tier-B interactive).

## Build sequence (each step coherent/committable)
1. **config.py**: `BUNDLED_DYNAMICS_DIR`, `resolve_dynamics_path`, `DYNAMICS_PATH`, `settings.json` key.
2. **`dynamics` loader** (`DynamicModuleDef` + path scan reading `module.json`) — unit-tested pure.
3. **Move modules** to `dynamics/fireflies/`, `dynamics/water/` with manifests; delete the flat client files.
4. **World server**: discovered registry; `/module` via manifest; `GET /dynamics/<name>/<file>`;
   per-active-agent `<script>` injection into `index.html`; scope `/module` to the active agent.
5. **Agent server / director**: load agent `dynamics` (fail if missing); inject `dynamics://available`.
6. **`builder/agent.json`**: add `dynamics`.
7. **`docs/dynamic-module-spec.md`**: the contract.
8. Tests: loader, resolution/shadowing, agent required-missing failure, `/module` scoping, injection.

## Non-goals (for now)
- Optional (non-required) modules in `agent.dynamics` — all listed are required.
- LLM/user-GENERATED module code (the plan's later phase) — this is about the extensible *loading*
  structure, not generation.
