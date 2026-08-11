# Agent / Director Separation — plan

**Status:** PLAN (design agreed 2026-08-10; not yet built). Makes `Director` a **generic agent
runtime** and pushes everything experience-specific into the declarative **agent def**
(`agents/<name>/`). Removes inline LLM handover from the director (switching is the shell's job).
Introduces a second agent — **`outdoor`** (skybox-only) — as the test that the separation is real.

On branch `agent-refactor`, following the LLM-identity removal (`f6308b9`). Sibling to
[agent-server-plan.md](./agent-server-plan.md) (the later move of the director into a shared server);
this plan is about *what belongs in the runtime vs. the agent*, independent of where it runs.

---

## 1. Goal

`Director` should own only what's true for **every** agent: the LLM roster, the shared transcript,
the MCP client + tool loop, per-turn context injection. Everything that makes today's experience "the
builder" — its prompt, its ownership/identity framing, its toolset, its scope — belongs to the agent
def. A new agent should be **purely declarative** (a directory), needing no runtime code change.

The `builder` is not special-cased in code; it's just the first agent. `outdoor` proves it by being a
second, deliberately *narrower* agent (skybox / grounded-skybox worlds only).

---

## 2. What's builder-specific in the runtime today (to move out)

- **`director.py` `_system()` `identity_line`** — a paragraph about worlds/spaces/assets, ownership,
  and builder tools (`list_worlds`, `switch_world`, `update_asset`, `query_assets`). Runtime-injected,
  builder knowledge. → moves into `agents/builder/prompt.md`.
- **`director.py` module-level `DIRECTOR_PROMPT = load_agent("builder").prompt`** — a builder default
  baked into the runtime module. → decouple; the prompt always comes from the loaded agent def.
- **Inline LLM handover** — `route_turn`, `Route`, the `_HANDOVER`/`_SWITCH`/`_TAKEOVER`/`_ADDRESS`
  regexes, and the persistent/one-shot + bare-handover greeting logic in `handle()`. → removed
  (switching is the shell's deterministic job).

Already correct (no work): **image-generator selection is not in the director.** Each world tool
(`generate_image`, `generate_skybox_image`, …) exposes an optional `generator` param and the world
server's `select_generator` picks a default; the prompt guides when to override. Prompt + tooling, as
intended. `outdoor` inherits this for free.

---

## 3. Changes

### 3a. Remove inline handover from the director

`handle()` becomes "run the **active** LLM on this text; append user + assistant turns." No routing.

- Keep `_match_name` (the shell imports it from `director.py` for its own `_switch`).
- Update the stale `shell.py` docstring that says inline routing is "untouched."
- Drop one-shot addressing ("Gemini, make a cat") entirely — intended.
- Deterministic switching stays exactly as-is in `shell.py:_switch` (sets `director.active`).

### 3b. Identity/prompt → agent-owned; a general prompt-injection framework

- Move the `identity_line` text into `agents/builder/prompt.md`.
- Introduce a small, **extensible injection framework**: `Director._injections()` is a
  `{placeholder} → provider` registry (provider may be sync or async); `Director._system()` +
  `_fill_injection()` fill each placeholder **only if it appears in the prompt**. `_system()` carries
  **nothing** agent-specific — the agent's `prompt.md` owns all its text, including framing.
- Two placeholder forms so the framing lives in the prompt yet vanishes cleanly when there's no value:
  - `{name}` — bare substitution.
  - `{#name}…{name}…{/name}` — a **conditional section**, kept only when the value is non-blank,
    dropped entirely otherwise (no dangling header when the room is empty).
- Two injections to start, more to come:
  - **`{user}`** → `self.user` (the logged-in human — replaces the old identity line's interpolation).
  - **`{context}`** → `_fetch_context()` (live MCP resources, §5), now returning **raw data** — the
    builder frames it in prompt.md as `{#context}--- Live context --- {context}{/context}`. This
    replaces the old *unconditional append* to every agent's prompt: an agent that references neither
    `{context}` nor `{#context}` pays **no MCP fetch at all** (many agents won't care about surfaces).
- Result: a game agent frames identity however it likes and simply omits the context section; the
  builder keeps both. New injections (e.g. `{viewer}` head pose) slot in as one more registry row and
  get both placeholder forms for free.
- *Note:* world-ownership rules aren't purely builder-specific (`outdoor` also creates worlds), so
  builder and outdoor will share some of that text by copy for now. Factor a shared prompt include
  later if it earns its keep.

### 3c. Tool scoping — two enforcement layers

The agent def gains a per-server **tool allow-list**:

```jsonc
"mcp_servers": [
  { "server": "world",
    "access": "all",
    "tools": ["generate_skybox_image", "set_skybox",
              "generate_grounded_skybox_image", "set_grounded_skybox",
              "generate_image"] }      // opt-in only, no wildcard: omitted ⇒ NONE (default-deny).
                                       // builder enumerates the whole surface (a test keeps it in sync).
]
```

**Layer 1 — client-side omission (behavioral scope, ships first).**
`Director.connect` filters the MCP `list_tools()` result to the allow-list before building `ToolSpec`s
(`director.py:177`). The LLM is simply never offered other tools — and the provider APIs can't emit a
`tool_use` for an undeclared tool, so the *model* genuinely cannot escape its set. Plus two cheap
guards so omission fails **loud**, not open:
1. **Validate the allow-list against live tool names at connect** — a typo raises, rather than
   silently over/under-granting.
2. **Re-check the allow-list in `Director._execute_tool`** — catches programmatic / persona /
   agent-to-agent calls that still route through the Director (not just the offered list).

**Layer 2 — server-side hard gate (the real boundary; scaffolded now, enforced on trigger).**
Layer 1 restricts the *model*, not the *capability*: `mcp_server.py` still exposes every tool and
`server.py` still accepts every request, so any path that doesn't go through this Director's filtered
list (a direct HTTP client, a second agent, the future shared agent-server) is unscoped. The hard gate
closes that by making the world server itself refuse a disallowed tool — mirroring the existing
**owner-only-writes** middleware (`server.py` ~400–410, 403 on `X-Conjure-User` ≠ owner):

- The agent's `(allowed tools, access level)` travels to the world server as a **capability**, the
  same way `X-Conjure-Scope` already travels for ownership (`mcp_server.py:49`). Injected once at MCP
  launch (`director.py` sets `CONJURE_SCOPE`; add `CONJURE_TOOLS` / `CONJURE_ACCESS`).
- A small server-side middleware maps request → tool/capability and returns **403** if it's outside
  the agent's grant (and enforces `access: "read"` = no mutating endpoints — the same unenforced slice
  gets closed here).
- Independent of what the LLM was offered, so a Layer-1 filter bug or a non-LLM path can't bypass it.

**Why staged, not gate-first:** for the current threat model (friendly users, your own agents, "no
security" is the documented posture — spaces-and-users-plan) Layer 1 already achieves the *behavior*,
and cross-**user** writes are *already* gated server-side. The hard gate matters when the boundary
must hold against paths that bypass the model. So it's **scaffolded early** (the allow-list field,
the capability header plumbing) but **required** — a blocking prerequisite — the moment any of these
land:

- **agent-to-agent / persona invocation** (a tool call constructed outside one agent's LLM tool set);
- the **shared agent-server** ([agent-server-plan.md](./agent-server-plan.md)) — many agents behind
  one world server, where a silent misconfig would hand a "scoped" agent full control;
- an **untrusted / third-party agent** (not just your own);
- anyone wanting **`access: "read"` to actually mean read-only**.

Until then: document the Layer-1 scope plainly as **behavioral, not security**, so nothing builds a
trust boundary on it prematurely.

### 3d. Launch a non-builder agent

`Director.connect(agent=…)` already supports it, but `cli.py`/`voice.py` always pass `builder`. Add an
`--agent` flag to both (default `builder`) so we can run `--agent outdoor`. (No shell agent-switch yet
— the shell only *lists* agents today; switch-agent-in-shell is a follow-up.)

### 3e. The `outdoor` agent

`agents/outdoor/{agent.json, prompt.md}` — "a builder that can only make skybox / grounded-skybox
worlds." World isolation is free: its worlds land under `<user>/agents/outdoor/` via
`scope_for(user, "outdoor")`. `agent.json` carries the skybox tool allow-list (3c). `prompt.md` is a
focused, skybox-only framing (its own identity text, no surface-styling / object-building material).

---

## 4. Order of work (each step green before the next)

1. ✅ **Strip handover** from `director.py` (kept `_match_name`); fixed `shell.py` docstring; tests.
2. ✅ **Identity → prompt** + extensible injection framework (`{user}`, `{#context}…{/context}`);
   `_system()` agent-agnostic; module-level builder default decoupled.
3. ✅ **Tool scoping Layer 1**: `ServerRef.tools` allow-list + connect-time filter
   (`_scope_tools`, fails loud on a typo) + `_execute_tool` re-check; `CONJURE_TOOLS`/`CONJURE_ACCESS`
   env scaffolded for Layer 2.
   - *Also shipped alongside:* **assets hard-scoped to their agent** (`config.agent_of`; public never
     crosses agents) — a server-side data-layer wall, see co-location-plan §8a.
4. ✅ **`--agent` flag** on cli/voice (default `builder`) → `Director.connect(agent=…)`; also fixed the
   REPL/voice banners that still advertised the removed inline LLM switch.
5. ⏭ **`agents/outdoor/`** (def + prompt), scoped to skybox tools; smoke-test `--agent outdoor`.
6. ⏭ **Tool scoping Layer 2** (server-side hard gate) — *tracked; build when a §3c trigger lands.* The
   capability header from step 3 + a world-server middleware mirroring owner-only-writes; enforces
   both the tool allow-list and `access: "read"`.

Steps 1–5 are the near-term separation + test. Step 6 is the enforced boundary, deliberately deferred
but with its trigger conditions written down so it can't be silently forgotten.

---

## 5. Tradeoffs & risks

- **Layer-1 scoping is behavioral, not security** — restricts the model, not the capability; fails
  open to non-LLM paths until Layer 2. Mitigated by the two loud-fail guards + a clear doc note, and
  bounded by the fact that cross-user writes are already server-gated.
- **Dropping one-shot addressing** removes a (little-used) convenience; deterministic switching via the
  shell remains. Intended.
- **Prompt text duplication** (ownership rules) between builder and outdoor until a shared include —
  accepted for now.
- **`--agent` at launch only** (no live agent switching yet) — fine for testing; revisit with the
  shell.

---

## 6. Open questions

- **Shared prompt includes:** when to factor the common world-ownership framing out of individual
  agent prompts (vs. copy-for-now).
- **Capability granularity:** is a flat tool allow-list enough, or do we also want per-tool argument
  limits (e.g. outdoor may `generate_image` but only for skybox use)? Flat list first.
- **`access: "read"` semantics:** exact set of endpoints classed as mutating (settle when Layer 2 is
  built).
- **Shell agent-switch:** add `switch <agent>` to the shell (parallel to LLM switch) as the follow-up
  to `--agent`.
