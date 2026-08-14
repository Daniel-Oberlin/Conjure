# Agent Server — plan

**Status:** PLAN (design agreed 2026-08-09; not yet built). Moves the **director/agent + the shared
transcript** out of the ephemeral voice/CLI process into a **long-lived agent server**, and turns
voice and CLI into **thin HTTP/SSE clients**. Realizes a single shared conversation to match the
single shared world. No security — identity only (consistent with
[spaces-and-users-plan.md](./spaces-and-users-plan.md)).

Depends on / follows the LLM-identity removal (branch `agent-refactor`, commit `f6308b9`): the
transcript already carries no per-LLM identity, so the only attribution we (re)introduce here is the
**human speaker**, never the model.

The **state model** this transport binds to — the one shared reality `(space, world)` with
`agent = f(world)`, the single session pointer, reconciliation, the two floors, pinning-while-held, and
the multi-user permission tiers — lives in [shared-session-plan.md](./shared-session-plan.md). Read that
first: it decides *what the agent server follows*. Step 1 there (per-turn `speaker` + the conversational
floor) is Step 1 here, and is **done**.

---

## 1. Why

Today "everyone shares a world" is true, but **"everyone shares a conversation" is not.** The world is
a shared, long-lived, broadcast thing (the world server); the conversation is a per-process,
in-memory thing (the Director). Two people on two connections build the same world but hold two
completely separate, mutually-invisible conversations.

The goal of the project *right now* is **shared experience** — which implies a single shared
context/history. So the transcript should live where the world lives: in a long-lived server that all
connections talk to, with each user's turns attributed to **who spoke**.

### What's shared vs per-connection (target)

| | Today | Target |
|---|---|---|
| World state / active world | shared (world server) | shared (unchanged) |
| **Conversation transcript** | **per-process, in-memory** | **one shared transcript (agent server)** |
| **Active LLM** | per-process | **one shared active LLM** |
| Speaker of a turn | none (fixed `--user` per process) | **per-turn speaker id** |
| Head pose / presence | per-user (world server) | per-user (unchanged) |
| World & asset ownership | per-user scope (persistence) | per-user, **resolved per turn** |

Deliberately **not** keyed by session/room: one global shared session. We are not building MUD-style
isolated groups; the shared conversation *is* the feature. (If that ever changes, key the transcript
by session — but not now.)

---

## 2. Current architecture (for contrast)

The Director is created **inside** each front-end process and driven by in-process calls; the only
network tiers are downstream of it.

```
voice.py / cli.py  process
  mic/keyboard → Shell.feed(text) → Director.handle(text)     [in-process Python]
       Director (owns transcript, LLM roster, active LLM)
         │  MCP client
         ▼  stdio (spawned subprocess)
  mcp_server.py  (thin, stateless; SCOPE fixed at launch from env)
         │
         ▼  HTTP  (X-Conjure-User / X-Conjure-Scope headers)
  server.py  (world store; active world/scope globals; owner-only-writes gate)
         │
         ▼  WebSocket  /ws
  headsets (render clients)
```

Key facts (grounded):
- One Director per process, pinned to one `--user` (`voice.py:154`, `cli.py:299`; `--user` at
  `voice.py:227`, `cli.py:361`).
- Transcript is instance state, in-memory only, never persisted or shared (`director.py:135`,
  appended at `:298–299`).
- The world server holds **no** Director and imports none of the LLM stack.
- The MCP server's identity/scope is module-level, fixed at launch (`mcp_server.py:29,46,49`).

---

## 3. Proposed architecture

Lift the Director into its own **agent server** with an HTTP/SSE API. Voice/CLI become thin clients.
`mcp_server.py`, scoping, and the world server are **unchanged** — the Director was already an MCP
client; it just gets a new home and a front door.

```
voice client                cli client            (future: web / phone)
  audio in → VAD → STT          keyboard
  → POST /turn {speaker, text}  → POST /turn            [thin HTTP clients]
  ← SSE conversation stream     ← SSE stream            (send text+speaker, receive events)
  → TTS ← reply text
        │
        ▼  HTTP + SSE
  AGENT SERVER  (new)
    • one shared Director + one shared transcript
    • LLM roster + active LLM
    • turn arbitration (single floor: reject-while-busy)
    • broadcasts conversation events to all subscribed clients
         │  MCP client  (UNCHANGED seam)
         ▼  stdio
  mcp_server.py   (now: per-turn scope, not launch-fixed)
         │
         ▼  HTTP
  server.py  (world store — UNCHANGED)
         │
         ▼  WebSocket /ws
  headsets
```

### Two channels, mirroring the world server's snapshot+broadcast pattern

A shared conversation means every client is both a **speaker** and an **observer** of the whole
conversation (Alice's client should show/speak that Bob asked something and how the agent answered).
So the agent-server API has two halves, exactly analogous to the world server's `/ws` (snapshot on
join + patch broadcast):

1. **Submit a turn** — `POST /turn { speaker, text }` (fire-and-forget; the reply comes over the
   stream, not the POST response).
2. **Subscribe to the conversation** — `GET /stream` (SSE) or a session WebSocket: on connect, the
   backlog/snapshot of the transcript; then a live event feed:
   - `user_turn { speaker, text }` — someone spoke
   - `assistant_delta { text }` — streamed reply chunks (preserves the "on it" → work cadence)
   - `tool_call { name, args }` — a world edit is happening (for UI/telemetry)
   - `assistant_final { text }` — turn complete
   - `busy { rejected_speaker }` — a turn was rejected because the floor was taken

This keeps the real-time feel (decision D3) and makes every client a live participant in one
conversation rather than a private tunnel.

---

## 4. Design decisions

### D1 — Separate agent server, **not** colocated in the world server ✅ (recommended)

**Decision:** the Director/transcript/LLM live in a *new, dedicated* process, alongside (not inside)
the world server.

**Why:** because we keep MCP + scoping (D2), the director→world call goes through the MCP seam
regardless — so colocating in the world server would **not** collapse that hop; it would only drag
the LLM SDKs and the "a hung LLM turn can stall world serving / websocket fan-out" risk into the world
process for little benefit. A separate server keeps the world server lean and failure-isolated, and is
the **minimal, seam-preserving** change (the Director is already an MCP client).

**Tradeoff:** one more process to run/deploy, and a new client→agent-server network hop. But that hop
is exactly where SSE gives us streaming, so it is needed anyway. World-vs-transcript ordering is
slightly looser than a single process would give, but with reject-while-busy (D4) only one turn runs
at a time and tool calls serialize through the one Director, so ordering is fine in practice.

*(Rejected alternative — Director inside the world server: fewer processes, but negates D2's benefit
and couples LLM failure to world serving.)*

### D2 — Keep MCP and the scoping/enforcement ✅

**Decision:** the agent server remains an MCP **client**; the world edit surface stays behind
`mcp_server.py` with server-side owner-gating.

**Why:** MCP is the clean, provider-neutral, per-agent-scoped seam the multi-agent vision leans on
(agents.md). Keeping it means the world server needs **zero** changes and D1 is a small lift.

**Tradeoff:** a small serialization/transport cost per tool call vs. direct in-process world calls.
Acceptable, and it buys process isolation + the scoping model.

**Required change:** scope/identity must move from **launch-fixed** to **per-turn**. Today
`mcp_server.py` reads `CONJURE_SCOPE` once at import (`:29`) and sends fixed `X-Conjure-User/Scope`
headers (`:49`). With a shared Director serving many speakers, the owning user varies per turn, so the
active speaker's scope must be threaded into each tool call's headers (whoever speaks owns what they
create). See §5.

### D3 — Streaming transport: SSE for the reply stream ✅ (WebSocket acceptable)

**Decision:** `POST /turn` to submit; **SSE** `GET /stream` for the conversation event feed.

**Why:** SSE is the simplest thing that preserves the early-ack-then-work cadence and supports
broadcast to multiple observers; the client→server direction is a plain POST, so full duplex isn't
required for the reject-while-busy model. A session **WebSocket** is a fine alternative and would make
future **barge-in/interrupt** and richer presence easier — if we expect those soon, start with WS.

**Tradeoff:** SSE is one-way (fine now, but interrupts/cancel need a separate `POST /cancel`); WS is
more moving parts but duplex. Start SSE, keep the door open to WS.

### D4 — Turn arbitration: single floor, **reject-while-busy** ✅

**Decision:** the Director handles one turn at a time; a turn submitted while another is in flight is
rejected with a `busy` event (the client can surface a subtle "one sec…" and the user retries).

**Why:** simplest correct policy, and concurrent speech is an edge case among people **co-located in
one room** (they naturally take turns). Avoids interleaving-two-turns-into-one-transcript hazards.

**Tradeoff:** no queueing/barge-in yet. Easy to evolve later (queue turns, or interrupt-and-replace)
once the single-floor version is proven. Note the reject must be **atomic** (a lock/flag around the
turn) so two near-simultaneous POSTs can't both start.

### D5 — Identity only, no security; `--user` stays on the clients ✅

**Decision:** each client keeps its `--user` flag and sends it as the **speaker** on every `/turn`.
The agent server **trusts** it (no auth tokens), consistent with the prototype's existing posture.

**Why:** we're among friendly users; heavyweight auth is out of scope now (matches
spaces-and-users-plan.md "no security — identity only").

**Consequence:** `self.user` on the Director is no longer a single scalar — it becomes the **active
speaker for the current turn**, and user turns are tagged with that speaker (the human-attribution we
deferred). Ownership/scope for world+asset writes resolves from the active speaker per turn (D2).

**Tradeoff:** anyone can claim any username. Accepted for now; revisit if the deployment opens up.

### D6 — One shared session, not keyed ✅

**Decision:** a single global transcript + single active LLM for the whole server. Not keyed by
user, world, or room. The transcript **spans world switches** (same people, continuous conversation).

**Why:** shared experience is the explicit goal; per-session keying is complexity we don't want.

**Tradeoff:** no isolated groups. If multiple independent parties ever need separate conversations,
introduce a session key then — deliberately deferred.

---

## 5. Required code changes

Keep the Director a **plain, testable object** owned by the agent server (not entangled with the web
framework), so the existing fast unit tests (`FakeLLM`, `FakeSession`, direct `handle()`) keep
working.

1. **Director: per-turn speaker.**
   - `handle(text)` → `handle(text, *, speaker)`; record `Turn("user", text)` **plus** the speaker
     (add a `speaker`/`by` field to the *user* turn, distinct from the removed LLM identity).
   - `self.user` (single scalar, `director.py:134`) becomes the **current turn's** speaker; the
     system prompt's logged-in-user line (`_system`) resolves from it per turn.
   - Serialize turns behind a lock/flag for D4 (reject-while-busy).

2. **New agent server** (e.g. `conjure/agent_server.py`, a small FastAPI app):
   - Holds one Director (via `Director.connect(...)`), one transcript.
   - `POST /turn {speaker, text}` → runs `director.handle(text, speaker=speaker)`; `on_text`/`on_tool`
     callbacks fan out as SSE events (§3) instead of TTS/print callbacks.
   - `GET /stream` (SSE) → snapshot + live conversation events.
   - `POST /switch` (or reuse shell semantics) for LLM/agent switch — still deterministic.

3. **`mcp_server.py`: per-turn scope.** Replace module-level `SCOPE`/`_USER`/`_HEADERS` (`:29,46,49`)
   with a per-call scope derived from the active speaker (e.g. a contextvar the Director sets before
   each turn, or scope passed through the tool-execution path). The world server's owner-gate is
   unchanged.

4. **Voice client** (`voice.py`): drop the in-process Director; keep the audio pipeline (VAD/STT/TTS).
   On end-of-utterance → `POST /turn {speaker: --user, text}`; subscribe to `/stream` and speak
   `assistant_delta`/`assistant_final` via TTS. `--user` stays.

5. **CLI client** (`cli.py`): drop the in-process Director; `POST /turn`; render `/stream` events.
   `--user` stays. The `Shell` deterministic commands either move to the agent server or stay
   client-side calling `POST /switch` etc.

6. **World server** (`server.py`): **no change** (that's the point of D1+D2).

### Migration path (incremental, each step shippable)

- **Step 0 (done):** LLM identity removed from context/prompt (`agent-refactor`).
- **Step 1 (done):** per-turn `speaker` on `Director.handle(text, *, speaker=)` → tags the *user* turn
  (`Turn.by`, dropped by `_messages` so the model never sees it) and resolves `{user}` per turn; the
  single floor is a `Busy`-raising flag (reject-while-busy, D4), not a queue. Clients pass their own
  `--user` as speaker (`shell.feed`). In-process only — no transport change yet.
- **Step 2:** stand up the agent server wrapping the existing Director; add `POST /turn` + `GET
  /stream`. Prove it with the CLI as an HTTP client (easiest to debug).
- **Step 3 (done):** `mcp_server.py` per-turn scope — the director sends `set_caller(user, scope)` at each
  turn; the MCP server threads that speaker into request headers + body scope (see shared-session-plan §10).
- **Step 4:** convert the voice client to the HTTP/SSE model (hardest — audio + streaming timing).
- **Step 5:** delete the in-process director paths from voice/cli once both clients are HTTP.

---

## 6. Tradeoffs & risks (summary)

- **Streaming is the main lift** — getting SSE timing right so voice still feels instant (early ack).
- **New per-turn scope path** in `mcp_server.py` — must be correct or writes attribute to the wrong
  owner. Cover with tests.
- **One more process** to run and supervise (mitigated: world server stays lean; failures isolate).
- **Reject-while-busy** is coarse — fine for co-located users; revisit for queue/barge-in later.
- **No security** — trusted usernames only; a deliberate, documented non-goal for now.
- **Testability** — preserved *iff* the Director stays a plain object the server holds, not
  framework-entangled. Enforce this in review.

---

## 7. Open questions

- **Shell commands' home:** do deterministic commands (switch LLM/agent, status) move server-side
  (shared for all clients) or stay per-client? Leaning server-side, since the active LLM is now shared
  (D6) — a switch by anyone affects everyone.
- **SSE vs WS final call:** ship SSE first; adopt WS if/when barge-in or richer presence lands (D3).
- **Transcript persistence:** in-memory only (like today) or persisted so the session survives an
  agent-server restart? Not required for the goal; decide when convenient.
- **Backlog on join:** how much transcript history to replay to a newly-connecting client (all vs.
  last N) — affects late joiners' context.
