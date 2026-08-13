# Shared Session — reconciliation, presence & permissions (plan)

**Status:** PLAN (design agreed 2026-08-13; not yet built). Defines the **one shared reality** the whole
stack reconciles to — who owns which state, how the world server, agent server, headsets and (dumb)
voice/CLI clients stay in sync, and how permissions resolve when multiple users of different identities
are present at once.

This is the **state model** the [agent-server-plan.md](./agent-server-plan.md) transport sits on top of.
Agent-server Step 1 (per-turn `speaker` + the single conversational floor) is **done**; this plan is the
prerequisite for Step 2 (standing up the agent server) because it decides *what the agent server binds to
and follows*.

Builds on and extends:
- [new-space-flow.md](./new-space-flow.md) **D1–D8** — the AR admission gate, the "active space is set by
  the first AR user," and **D6** (a space is locked while occupied). The **spatial floor** below is D6
  generalized from "joiners refused" to "shared-pointer moves constrained" (**pinning**, P7).
- [co-location-plan.md](./co-location-plan.md) **§4** — edit-rights follow ownership (the `_owner_only_writes`
  guard). That is **G3** below.
- [spaces-and-users-plan.md](./spaces-and-users-plan.md) — scope = `<user>/agents/<agent>`, spaces owned +
  geolocated, no security (identity only).

---

## 1. Principles (the invariants everything else follows from)

- **P1 — One shared reality.** There is exactly one active tuple `(space, world)`, and `agent = f(world)`
  (a world lives in a scope `<user>/agents/<agent>`, so the agent falls out of the world). No per-user
  worlds, no forking — the shared experience *is* the feature (agent-server-plan D6).
- **P2 — The world server is the single source of truth.** It persists the active tuple and answers
  "what's live." Every other component (agent server, headsets, voice/CLI) is a **peripheral that
  reconciles to it** — never a second authority.
- **P3 — Clients are dumb peripherals.** After the agent-server refactor, voice and CLI hold **no state**:
  they submit turns (tagged with a `speaker`) and render what they're told. All durable/session state lives
  in the two servers.
- **P4 — State outlives clients.** Headsets come and go. When they're all gone the tuple stays put between
  the world + agent servers, so voice/CLI keep working against it. A headset re-joining the *same* space
  changes nothing.
- **P5 — Physical presence dominates; screens follow the room.** A real headset in a real room is the
  strongest anchor. A remote (screen-only) client can never break a co-located headset's reality (see P7).
- **P6 — Two floors, both first-come, both released on exit.**
  - *Conversational floor* (agent-server D4): one turn at a time; a turn submitted mid-turn is rejected
    (`Busy`). **Done in Step 1.**
  - *Spatial floor* (new-space-flow D6): one **held room** at a time; a headset in another room is refused
    until the holder releases.
- **P7 — Pinning while held.** While a space is held, the shared pointer is *pinned* to it: moves are legal
  only to worlds **compatible** with that space — another world **in the same space**, or a **VOID/skybox**
  world (no surfaces to match). A different real space's world is refused. **Unheld** (no headset), voice/CLI
  move the pointer freely. This is the concrete form of P5 and the extension of D6.
- **P8 — Presence ≠ voice.** Headset presence (a *view* membership, gated by G1) and conversational
  membership (a *turn* membership, gated per-speaker by G2/G3) are **separate**. Headsets render but never
  submit turns; only voice/CLI converse. To both see *and* direct a world, a user needs a headset **and** a
  voice/CLI.
- **P9 — Permission is evaluated at the identity and moment of each action.** For a headset it's
  *continuous presence*; for voice/CLI it's *per-turn* against the **speaker** of that utterance (exactly the
  `speaker` field added in Step 1, enforced via the per-turn scope of Step 3). Permissions are not a bolt-on
  — they're the per-turn speaker doing its job.
- **P10 — Never disconnect; demote.** When a move makes the tuple inaccessible to a present participant, that
  participant becomes a **locked-out spectator** (headset → passthrough; voice/CLI → "no access, pick
  another"), never dropped. Access is a live function of `identity × current tuple`.

---

## 2. The source of truth: one session pointer

Today "what's live" is smeared across three on-disk facts: `worlds/<scope>/_active.txt` (active world per
scope), `spaces/<user>/_active.txt` (active space), and `worlds/<user>/_last_agent.txt` (last agent). Under
P1 + `agent = f(world)`, collapse them to a **single global session pointer**:

> **`(active_space, active_world)`** — where `active_world` carries its scope, so
> **`agent = agent_of(active_world.scope)`** is derived, not stored.

`_last_agent.txt` becomes derivable and is **retired** (the agent is a function of the world, and the world
is the persisted fact). The world server restores this one pointer on boot; everything reconciles to it.

### The movers (only three things change the pointer)

1. **Boot-restore** — the world server, on startup, restores the persisted pointer (provisional; §5).
2. **Headset establish/relocalize** — the *first* headset selecting a space may move the pointer
   (space → world → agent) via `/space/select` → `_switch_to(sp.last_scope, sp.last_world)`. Physical
   authority (P5), constrained by the spatial floor + pinning (P6/P7).
3. **Explicit voice/CLI switch** — a user switching world/agent. Logical authority, constrained by pinning
   while a room is held (P7) and by the gates (§3).

Every mover must satisfy: **a mover may only move the pointer to a tuple it is itself permitted to occupy**
(§3). Illegal states never become active.

### Reconciliation: one broadcast, two consumers

The world server already broadcasts a snapshot on every `_switch_to` (`_broadcast(_snapshot_msg())`), but
today it carries only `{world, owner}` and **only headsets subscribe**. Enrich it to the canonical
**"what's live"** and give it two consumers:

```
world server: pointer change → broadcast { space, world, scope, agent, owner }
                                   │
                 ┌─────────────────┴──────────────────┐
             headset                              agent server
        renders the world                    binds its brain (Director) to the agent
```

- The agent server **subscribes** (not a one-shot GET) so it *follows* the pointer — including
  headset-driven changes it didn't initiate. **Decided (Step C):** it rides the world server's existing
  `/ws` as a passive listener and reads `state` from each snapshot (reuses Step B; no new world-server
  surface — verify a listener isn't counted as a render client / space-holder). On an `agent` change it
  **re-binds** its Director (the existing `_open_agent` close-old-then-open-new dance).
- Add a plain **`GET /state`** → `{space, world, scope, agent, owner}` as the reconciliation snapshot for a
  fresh subscriber / any client that just needs to ask.

---

## 3. Gates and the three access tiers

A participant's relationship to the current tuple is one of **three tiers**, recomputed for *everyone
present* on *every* pointer move (P10):

| Tier | Condition | Can |
|---|---|---|
| **Editor** | owns the active world | see + edit + converse |
| **Viewer** | may occupy it (public world / public space) but not the owner | see + converse, **not** edit |
| **Locked-out** | may not occupy it (private, not theirs) | nothing — headset → passthrough, voice/CLI → rejected turns |

The tier is computed from three gates — the predicate **"may actor A occupy / act on tuple T?"**:

- **G1 — surface-match** (AR headsets only; **VOID-exempt**). Does A's *physical room* match T's space?
  A headset can only ever move the pointer to *the world for the room it is physically in* — which is what
  makes physical presence honest. Worlds with **no surfaces (VOID/skybox)** are exempt: any headset passes.
- **G2 — privacy** (everyone). Is T's space/world public, or owned by A? (new-space-flow D8; a user may not
  enter another user's private space/world.)
- **G3 — edit-ownership** (everyone; co-location §4, `_owner_only_writes`). Only the **active world's owner**
  may mutate it. Navigation (`/worlds/new`, `/worlds/switch`) is *not* gated — anyone may switch and everyone
  follows — but you land in your **own** scope, so you only get edit rights on worlds you create/own.

The gates live entirely in the **(space, world)** layer the world server owns. **The agent server never
makes a permission decision** — it is told the new agent and re-binds. G1 governs the *headset's view*;
G2/G3 govern *whose turn is allowed* (per-speaker, P9).

---

## 4. Boot & lifecycle

- **Order-independent, lazy binding.** No handshake. Discovery is static URLs, one per downstream hop:
  clients → `agent_url` → agent server → (MCP) → world server → `world_url` → headsets (`/ws`).
  - **World server boots standalone** from disk (restores the session pointer) and **renders to headsets
    with no agent server present** — you can walk your world with no AI. That independence is why we keep
    them separate; don't spend it.
  - **Agent server** loads locally (agent defs + roster need no network), then subscribes to the world
    server; retries with backoff until it's up. It can *load* without the world server; it can only *serve
    turns* once connected. Until bound, `POST /turn` → `503 not-ready`.
  - **Clients** connect to `agent_url`; retry if it's down.
- **Restart matrix** (all consistent because the world server is the single writer):
  - *World server alone* → re-restores the pointer from disk; the agent server's live Director already
    matches (it followed the last change), headsets' `/ws` reconnects and re-snapshots.
  - *Agent server alone* → re-subscribes, reads `/state`, re-binds to the current agent. **Transcript is
    lost** (in-memory; §7) — acceptable for now.
  - *Client alone* → re-subscribes, prompt/room reflect current context.
- **Provisional vs anchored.** The boot-restored pointer is a **provisional** guess for the window before
  any headset establishes a space (so voice/CLI/desktop and the renderer have something coherent). The first
  headset to establish/match a space **anchors** it (new-space-flow D1); the spatial truth then supersedes
  the temporal guess.

---

## 5. Switching (world & agent)

- **Agent switch is a world switch.** Because `agent = f(world)`, "switch to the outdoor agent" means "make
  a world in the outdoor scope live." The client/agent server asserts it via `/scope/activate`; the world
  server resumes that scope's active world or mints its default; the enriched broadcast tells the agent
  server to re-bind and all clients to update.
- **Constrained by pinning (P7).** While a room is held, only same-space or VOID moves are legal. A move to a
  different real space's world is refused with a clear reason (headset holds the room). Unheld, moves are
  free.
- **Explicit intent vs resume.** A client launched with an explicit `--agent X` **asserts** (moves the
  pointer to X, subject to gates/pinning). Launched bare, it **resumes** — aligns read-only to whatever the
  world server reports; no assert, so it never stomps a headset-driven state.

---

## 6. Multi-user & permissions — the worked cases

Scenario used throughout (the pressure test): **Alice** (headset in the **living room**; space `living`
public, last world `cozy-cabin` builder/public), **Bob** (headset in the **bedroom**; space `bedroom`
private, last world `bob-lab` builder/private), **Carol** (voice + CLI, a third identity, no space).

1. **Boot, no headset.** Pointer restored to `(living, cozy-cabin)`, agent builder. Carol connects →
   **Viewer** (public world, not owner).
2. **Alice joins in the living room.** Space unclaimed → she establishes and **holds** `living` → **Editor**
   (owner). Pointer unchanged.
3. **Bob joins in the bedroom (contention).** Space is **occupied** by Alice → Bob's capture doesn't match
   `living` → **REFUSED**, `H_B` stays in **passthrough**. *Two headsets in two rooms cannot both be live* —
   one shared world tied to one space can surface-match only one physical room. The spatial floor (P6) is the
   anti-thrash arbiter; Bob cannot steal the reality while Alice holds it.
4. **Alice leaves.** Hold releases; space goes provisional. Carol still on `cozy-cabin`.
5. **Bob re-selects in the bedroom.** Now unclaimed → matched → Bob **holds** `bedroom`/`bob-lab` (his own) →
   **Editor**. Pointer moves. **Re-evaluate everyone:** Carol → `bob-lab` is private to Bob → **Locked-out**;
   her prompt flips to `carol ⊘ bedroom (no access)` and her turns are rejected ("no access — try `worlds`").
   She is *not* disconnected (P10).
6. **Remote-can't-blind-local (P7).** Back at step 2–3: Carol says "take us to Bob's beach world." That move
   would break Alice's surface-match, so while Alice holds `living` it is **refused** — Carol may only move
   to another `living` world or a VOID/skybox world. Unheld, she'd be free.
7. **Nobody-can-talk (P8).** If Bob has only a headset (no voice/CLI), after step 5 there is **no admitted
   speaker**: Bob sees `bob-lab` but headsets don't submit turns; the agent server idles until someone with
   access speaks. Benign, and it names the presence≠voice split.
8. **Cross-room escape hatch (VOID).** If the active world is a **skybox/VOID** world, Alice-in-living,
   Bob-in-bedroom and Carol-on-voice can **all** co-inhabit it at once (every headset passes G1 trivially;
   nobody holds a space). The instant someone establishes a *surface* world, the single-room floor snaps
   back. *Outdoor agents are inherently multi-room; surface (builder) agents are inherently single-room* —
   this falls straight out of "a world binds to a space only if it has surfaces."

---

## 7. Edge-case catalog (surface everything)

**Boot / relocalization**
- *First headset matches the booted space* → no change.
- *First headset matches a different space* → relocalize: pointer → that space's world → its agent; agent
  server re-binds; clients update.
- *First headset no-match ("somewhere new")* → mint a geo-stamped `space-N` + default world owned by the
  connecting user (new-space-flow D2/D7).
- *Matched space's `last_world` is inaccessible to the establisher* (e.g. it's private to a third user) →
  **precedence, deterministic**: (i) the establisher's **own** last world in this space, else (ii) the
  **most-recent world tied to this space that the establisher may occupy** (public / theirs), else (iii)
  **mint** a fresh default tied to the space. *Reject "pick a random world" — unprincipled.* MVP is the
  degenerate form "recalled world if accessible, else mint"; enrich to (i)/(ii) by having a space remember a
  short **ordered history of worlds shown here** (§9 open knob).

**Contention / floors**
- *Two headsets, same room* → the second passes the admission gate as co-located (matches the held space) →
  both Editors/Viewers per ownership; no move.
- *Two headsets, different rooms* → spatial floor: one held, the other passthrough until release.
- *Held-room theft attempt* → refused; no mint, no switch.
- *Voice/CLI switch while a room is held* → pinned to same-space or VOID moves only (P7).
- *Voice/CLI switch while unheld* → free.

**Access transitions (P10)**
- *Move into a private world* → non-permitted bystanders → Locked-out (voice → rejected turns; headset →
  passthrough).
- *Move that changes the agent* → agent server re-binds; **transcript resets** (different brain/tools/
  conversation); clients update prompt.
- *Move within the same agent* (world switch) → **transcript kept** (agent-server D6).
- *Guest (Viewer) asks the agent to edit* → the tool call 403s (G3); the agent relays "this world belongs to
  <owner>; want to spin up your own?" and can `/worlds/new` in the guest's scope (they become owner → Editor).
- *Locked-out voice user types a stale command* → the per-turn gate rejects it safely (a no-op/error, never a
  corruption). Correctness comes from the **server gate**, not client synchrony.

**Process restarts** — see §4 restart matrix. Transcript is in-memory in the agent server (survives client
churn, lost on agent-server restart; persistence deferred, §9).

**Identity**
- *Two voice users of different identities* → the conversational floor (P6) serializes their turns; each turn
  is gated by **its own speaker** (P9). Interleave is impossible (one floor).
- *Missing user header (direct dev CLI)* → treated as owner/DEFAULT (interim convenience; co-location §4).

---

## 8. Client behaviour (dumb, context-reflecting)

- **Voice/CLI hold no state** (P3). They subscribe to the agent server's stream, submit `POST /turn
  {speaker, text}`, and render `assistant_delta`/`assistant_final`/`busy` + **context-change** events.
- **The prompt reflects live context and access tier**, updated whenever a context event arrives — not just
  lazily on the next line:
  ```
  conjure:carol@living/cozy-cabin · builder · claude>     # editor/viewer
  conjure:carol ⊘ bedroom (no access)>                    # locked-out
  ```
  Encodes `user @ space/world · agent · llm`, with a distinct locked-out form.
- **CLI is synchronous** (blocking REPL). Don't make it interruptible: a background listener prints
  out-of-band context notices ("↪ a headset arrived — now in *bedroom* (builder); you have no access") and
  refreshes the prompt; the *next* line reflects the new state, and any stale command is safely gated
  server-side. **Eventually-consistent per prompt**, correctness from the gate (P9).

---

## 9. Open questions / knobs

- **Space world-history richness** (the one genuinely open design knob) — a single `last_world` pointer vs an
  **ordered history of worlds shown in a space** (drives the fallback precedence in §7). Start minimal
  ("last-if-accessible-else-mint"), enrich later.
- **Transcript persistence** — in-memory (today's behaviour, lost on agent-server restart) vs persisted so a
  session survives a restart. Not required for the goal; if persisted, it belongs in the world server (the
  persistence tier). Reset-on-agent-change stays regardless.
- **Backlog on join** — how much transcript to replay to a late-joining client (all vs last N).
- **Co-edit / consent** — G3 is owner-only today; a future consent model could let a guest co-edit another's
  world. Deliberately out of scope (co-location §4 notes this as a later tightening).

---

## 10. Execution plan (incremental, each step shippable & testable)

Ordered so the browser/headset-independent pieces land first and each step is unit-testable in-process.

- **✅ Step 1 (done) — per-turn `speaker` + conversational floor.** `Director.handle(text, *, speaker=)`,
  `Turn.by`, `Busy`. (agent-server-plan Step 1.)
- **✅ Step A (done) — collapse to one session pointer.** World server persists a single global
  `_session.txt` = `(scope, world)` (`WorldRepository.get_session`/`set_session`); `_boot_world` restores it
  (migration read-through from `_last_agent` + per-scope `_active.txt` when absent, then writes it forward);
  `_switch_to` updates it; `_activate_scope` no longer records a per-user last-agent; `/agent/last` derives
  the live agent from `agent_of(active_scope)`. The active **space** stays derived from the world's
  `environment.space` (not stored in the pointer). `_last_agent.txt` retired to a migration-only fact.
  Tested: pointer round-trip (+ normalization), boot-from-pointer, pre-session migration, `/scope/activate`
  moves the pointer + live agent.
- **✅ Step B (done) — enrich the broadcast + `GET /state`.** `_live_state()` returns the canonical
  identifiers `{scope, agent, world, owner, space}` (space = `<owner>/<name>` or VOID); every `/ws` snapshot
  now carries them under `state` (backward-compatible — `world` doc + top-level `owner` unchanged for the
  renderer); new `GET /state` returns them flat (subsumes `/agent/last`). Tested: `/state` for the default,
  `/state` after a scope activation (agent derived, space reflected), snapshot carries `state` beside the
  doc.
- **Step C — the agent server (new host of `Shell.session`).** Move the `Shell.session` host out of
  cli.py/voice.py into `conjure/agent_server.py` (a small FastAPI app holding one Shell → one Director →
  one shared transcript); clients become thin HTTP/SSE. `Director`/`Shell` stay **plain objects** (unit
  tests unchanged); only their host moves. **Decisions (2026-08-13):** follow the world server by riding
  its existing `/ws` as a passive listener (reuse Step B's `state`; verify a listener isn't counted as a
  render client / space-holder); convert **CLI first, voice later**; route both utterances *and*
  deterministic commands through **`POST /turn`** (keep `Shell.feed`'s wake-word routing; command output
  emits as a `notice` SSE event). Substeps:
  - **✅ C1 (done) — stand up the server + convert the CLI** (agent-server-plan Step 2).
    `conjure/agent_server.py`: `POST /turn {speaker, text}` (fire-and-forget behind a single turn floor;
    409/`busy`) + `GET /stream` (SSE: backlog snapshot, then `user_turn`/`assistant_delta`/`tool_call`/
    `assistant_final`/`notice`/`busy`/`context`); `build_app(shell=…)` injects a shell for tests.
    `conjure/agent_client.py`: pure `parse_sse_line`/`prompt_from_context`/`render_event`/`apply_context` +
    `post_turn`/`stream_events`. CLI (`_repl_client`/`_say_client`) is now a thin client — background SSE
    listener, prompt from `context`; `say` skips the backlog via its own turn marker. `Shell.feed(…,
    speaker=)` per call; `agent_url` config; `conjure-agent` entrypoint. Verified live end-to-end (world +
    agent servers, a command turn). Voice still in-process (no regression). Suite 364 py + 97 js.
  - **✅ C2 (done) — follow the pointer + re-bind** (shared-session Step C proper). The agent server rides
    the world `/ws` as a passive listener (`_follow_world_state`), reconciling each snapshot's `state`
    (`_reconcile_state`): on an **agent** change it re-binds the Director via `_open_agent(…,
    activate_world=False)` (fresh Director = fresh transcript); a same-agent world/space change keeps the
    transcript; either way it emits an enriched `context` (`{agent, llm, user, scope, world, space,
    owner}`) so clients refresh. **Structured-concurrency fix:** the follow loop runs in the *same task*
    that owns `Shell.session` (`_shell_and_follow`) — a cross-task `_open_agent` `aclose()` raised an anyio
    cancel-scope error; turns still run in their own tasks (they only *call* the session) and are
    serialized against a re-bind by `floor_lock`. Verified live: forced builder↔outdoor transitions
    re-bind the Director (43↔10 tools). Suite 367 py + 97 js.
  - **C3 — convert voice** to POST /turn + SSE→TTS (agent-server-plan Step 4; hardest — audio + timing).
  - **C4 — delete the in-process director paths** from cli/voice (agent-server-plan Step 5).
  - **✅ Adjacent (done) — `mcp_server.py` per-turn scope** (agent-server-plan Step 3). Identity was fixed
    at MCP launch, so with a shared agent server every turn acted as the launch user — a real permission
    bypass (a `--user guest` client edited daniel's world, and `list_worlds` showed daniel's worlds as
    guest's). Now the director sends `set_caller(user, scope)` at the start of each turn (a control tool,
    never in an agent's allow-list, exempt from the gate); the MCP server threads that speaker into BOTH
    the request headers (`_headers()`, owner gate) AND the body scope (`_scope()`, "your worlds" / asset
    ownership). `_post_patch` now carries identity too and raises a clean owner-only message on a 403 (the
    patch path previously sent no header → bypassed the gate). Verified live: `--user guest` correctly sees
    daniel's world as read-only and is refused edits.
- **Step D — pinning while held (P7).** World server: while a space is occupied, constrain pointer moves
  (`/worlds/switch`, `/scope/activate`) to same-space or VOID; refuse cross-space moves with a clear reason.
  Tests: held + same-space ok; held + VOID ok; held + other-space refused; unheld free.
- **Step E — three-tier access on every move (P10).** On each pointer move, recompute each present
  participant's tier; emit per-participant access state; voice/CLI turns gated per-speaker (folds into the
  per-turn scope of agent-server Step 3); headset lock-out = existing passthrough. Tests: private-move locks
  out a bystander; guest edit 403 + agent fallback; stale command safely rejected.
- **Step F — privacy fallback precedence (§7).** Implement establisher-accessible world resolution (own-last
  → most-recent-accessible → mint), starting with the degenerate MVP. Tests cover each branch.
- **Step G — context-reflecting clients.** Voice/CLI subscribe to context events; prompt reflects
  `user@space/world·agent·llm` + locked-out form; CLI background listener + eventual-consistency. Tests:
  prompt string for each tier; stale-command path.

Steps A–B are pure world-server + unit-testable; C is the agent-server lift; D–F are the permission model; G
is the client polish. Each is independently shippable.
