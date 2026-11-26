# Plan — agent jobs: turns an agent runs with nobody present

**Status:** all phases open · **Opened:** 2026-11-25 · **Blocked on:** nothing · **First consumer:**
[`companion.md`](./companion.md) (memory consolidation, reflection, "while you were away")

**This file is temporary.** It holds one sequence across agent definitions, the agent server, the world
server's write gate and visits, and the MCP server registry, and is deleted when the last phase has
settled: finished work to [`specs/agents.md`](../specs/agents.md), deferred work to
[`backlogs/agents.md`](../backlogs/agents.md), forks taken to [`decisions.md`](../decisions.md).

**Dissolves to:** `specs/agents.md` — a new **§7.9 (jobs)**, additions to **§3** (the `jobs` field),
**§8.4a** (detached visits), **§9.4** (the system-turn caller), **§12** (routes) ·
`agents/servers.json` + its spec row for the email and web servers · `decisions.md` for §10.

Line references are to **`main` at 2bf2e1e**.

---

## 0. Why

Every LLM turn today starts with a person: a user utterance (`Director.handle`, `director.py:465`) or
the one-time greeting (`Director.greet`, `:485`, called from `_maybe_greet`, `agent_server.py:406`).
Several things want a turn **without** one:

- the companion's memory consolidation and reflection after a session (companion phase 4);
- the companion changing the room while he's away, carried out by builder (companion phase 7);
- the companion's in-session "inner thoughts" (companion phase 7) — same shape, a presence trigger;
- later, for any agent: outdoor matching the sky to real weather each morning, builder tidying a
  library overnight, a game agent advancing its world between sessions, a morning email to the owner.

Built once, generally: **a job is a declared, budgeted, logged turn an agent runs on its own prompt,
optionally handing a brief to another agent to carry out.** Nothing about it is companion-specific.

---

## 1. What this is built on

- **The greeting path** (`agent_server.py:406-439`) is a system-initiated turn: tool-less `greet`,
  under `app.state.floor_lock`, assistant turn only, persisted and broadcast. A job generalises it:
  tools allowed (scoped), not broadcast unless asked, and not necessarily on the live Director.
- **Visits** (`specs/agents.md` §8.4a): an agent listed in the host's `visiting_agents` acts in the
  host's world as its own scope, has its own transcript at `sessions/<id>/visits/<agent>/`, and
  imports the assets it uses. Today the visitor is the **global** `active_visitor`
  (`server.py:480`), which also changes what every connected client sees.
- **The write gate** (`server.py:803-830`): a write needs the owner as speaker (or an agent turn in a
  session that serves everyone) **and** an actor that is the live scope or the active visitor's.
- **Two identities per turn**: `set_caller(user, scope, actor)` — speaker and actor travel as
  headers to the world server.
- **MCP servers** are declared per agent from `agents/servers.json`; one more server is one more entry.
- **The world server holds one live world** (`active_scope`). A job that edits a world needs that
  world live — see §5.

---

## 2. Phase 1 — the job declaration and runner (no world edits yet)

**Declaration** in `agent.json`:

```json
"jobs": {
  "consolidate": {
    "on": { "session_idle_minutes": 10 },
    "instruction": "…",
    "tools": ["state_set", "state_merge", "episode_write"],
    "output": "state",
    "budget": { "tool_calls": 20 }
  }
}
```

| field | meaning |
|---|---|
| `on` | the trigger (§3); one per job |
| `instruction` | the user-side text of the turn. Runs on the agent's **own system prompt**, so it's in character and gets the usual injections (state docs, `{time}`, `{episodes}`, `{context}`) |
| `tools` | an allow-list **subset** of the agent's tools for this run (and the director-hosted `state_*` tools). Empty = tool-less |
| `delegate_to` | optional; phase 3 |
| `output` | where the result goes: `state` (the tools already wrote it), `episode`, `queue` (shown or spoken next session), `none` |
| `budget` | `tool_calls`, `generations`, `usd` (best-effort from provider usage), `seconds` |
| `model` | optional override; **default is the agent's own model** (§10.1) |

**The runner** lives in the agent server. For each due job it builds a **detached Director** — the
job agent's definition, its own MCP session, `set_caller(owner, scope, actor)` — so the live Director,
its transcript and its floor are never touched. Its turn transcript is written to
`<owner>/agents/<agent>/jobs/<job>/<run-id>.jsonl`, not the session transcript.

**Model.** The job runs on the LLM the agent's session was **last running on** (the session meta
already remembers it), falling back to the roster's first. `model` overrides.

**Budget enforcement** is in the runner, not the prompt: the tool executor counts calls and
generations and refuses past the cap ("budget spent — finish now"), and a wall-clock timeout cancels.

**Run log.** Every run appends one line to `<owner>/agents/<agent>/jobs/log.jsonl`:
`{run, job, trigger, started, ended, model, tool_calls, generations, usd, outcome, summary}`.
Shell: `jobs` lists declared jobs and next due; `jobs log [job]` shows recent runs; `job <name> run`
runs one now; `jobs off` / `jobs on` pauses all of an agent's jobs.

**Tests:** declaration validates at load (unknown tools in `tools` rejected, as `mcp_servers` tools
are); detached Director doesn't touch the live transcript or floor; budget refusals; log line per run;
model selection.

---

## 3. Phase 2 — triggers

| trigger | fires | notes |
|---|---|---|
| `session_end` | the last client leaves a session | debounced (reconnects within 2 min don't count) |
| `session_idle_minutes: N` | no turn for N minutes, clients may still be connected | for tool-less / state-only jobs |
| `after_absence_hours: N` | N hours after `session_end`, if nobody came back | for world-editing jobs (§5) |
| `schedule: "0 7 * * *"` | cron, owner's timezone | a missed run (machine asleep) runs once at wake, never catches up a backlog |
| `event: [...]` | presence events (companion phase 6) | in-session; gated by the etiquette rules below |

**Etiquette for any in-session job that speaks** (`output: "say"`): never while VAD hears a user; a
minimum gap since the agent last spoke; exponential back-off after each unanswered one; suppressed by
a deterministic "give me a minute" phrase; **never** triggered by someone leaving or saying goodbye.
These are runner rules, not prompt rules.

The scheduler is an asyncio task in the agent server (`agent_server.py:730` already starts long-lived
tasks); job state (`last_run`, `next_due`) persists beside the run log so a restart doesn't re-fire.

**Tests:** each trigger with a fake clock; debounce; missed-schedule-once; no event trigger on leave.

---

## 4. Phase 3 — delegation: one agent decides, another acts

A job with `delegate_to` is two steps, both run by the runner. The deciding agent **needs no
delegate tool** — the runner orchestrates.

1. **Decide** — the declaring agent's turn, usually tool-less, returns a **brief** matching a small
   schema: `{"do": "…", "why": "…", "keep": [...], "done_when": "…"}` or `null` (nothing to do — a
   normal answer). Validated; one retry on a malformed brief, then the run ends as `no_brief`.
2. **Act** — the delegate agent runs one turn with the brief as its instruction, plus the job's
   limits appended verbatim, under its own prompt and the job's `delegate_tools` allow-list (default:
   the delegate's tools minus the deny list in §6). Its final reply is a one-line report.
3. **Record** — brief + report go to the declaring agent's `output` (for the companion: `self.recent`
   and an episode), so the declaring agent owns the result in its own voice.

```json
"away": {
  "on": { "after_absence_hours": 2 },
  "instruction": "He's away. Given {self}, {relationship} and tonight's episode, would you change something in this room before he's back? Describe it as you'd want it, or answer null.",
  "tools": [],
  "delegate_to": "builder",
  "delegate_tools": "default",
  "world": "last",
  "output": "episode",
  "budget": { "tool_calls": 12, "generations": 2 }
}
```

**Who may be a delegate:** only an agent in the declaring agent's `visiting_agents` — the same
whitelist that already governs visits, so there's one list, not two.

**Tests:** null brief ends cleanly; malformed brief retry; delegate outside `visiting_agents`
refused; report recorded under the declaring agent.

---

## 5. Phase 4 — jobs that edit a world

Two pieces of server work, then a rule.

1. **Detached visits.** Today a visit is the global `active_visitor`. A job's delegate must act in the
   host's world **without** changing `/state` for anyone. The visitor travels with the request
   instead: the job's MCP session sends `X-Conjure-Visit: <host-scope>` and the gate treats that actor
   as a visitor of that host for this request only (still checked against the host's
   `visiting_agents`). `_import_for_visit` takes the host from the request instead of the global.
2. **The system-turn caller.** No human is present, so the speaker is the **owner** and a new header
   `X-Conjure-Job: <run-id>` marks it. The gate accepts it only when the target world is owned by that
   owner, is **private**, and the run id is live in the agent server (a short-lived token the agent
   server registers with the world server at run start). Owner-speaker paths (`/reset`,
   `/space/capture`, `/space/realign`, `/world_frame`) stay refused for jobs regardless.
3. **Only while nobody is connected.** The world server holds one live world. A world-editing job
   runs only when no client is connected to the server; it activates the target world (`world:
   "last"` = the world the session was last in), runs, and restores the previous active scope. A client
   connecting mid-run cancels the job at its next tool call, restores the snapshot (below) and the
   previous world, and the run is logged `cancelled`. Editing a non-live world headlessly is deferred
   (§10.3).
4. **Snapshot first.** The world doc is copied before the act step; a failed, cancelled or unwanted
   run is one restore. The snapshot id is in the run log, and `job undo <run>` restores it — which is
   what the companion's "put it back" calls.

**Tests:** detached visit leaves `/state` unchanged; gate refuses the job caller on a public world,
on another owner's world, after the run ends, on owner-speaker paths; connect-mid-run cancels and
restores; undo restores.

---

## 6. Phase 5 — reaching outside Conjure: web and email

Agents already declare MCP servers; a job can use any tool its agent has. Two new servers ship in
`agents/servers.json`, and three rules make unattended use safe.

**Servers.**
- `web` — `web_search(query)`, `web_fetch(url)`. Read-only.
- `email` — `email_owner(subject, body)`. **No recipient parameter**: the address comes from the
  owner's config (`CONJURE_OWNER_EMAIL` in `.env`, or user config), filled in by the server, so no
  prompt or fetched page can redirect it. Plain text only.

**Rule 1 — read *or* act, not both, in one unattended run.** A job whose allowed tools include an
external **reader** (`web_*`) may not also hold an external **actor** (any tool that sends something
out of Conjure). Enforced at load. Readers write their findings to state or the queue as data; a later
run, or the owner, acts on them. A fetched page can therefore never steer an action in the same run.
`email_owner` is the one exception (rule 2), because its recipient is fixed and it is the owner.

**Rule 2 — outward actions go only to the owner, automatically; anything else is drafted.**
- `email_owner` sends automatically: one fixed recipient, rate-limited (per job and per day, e.g.
  3/day), plain text, and **links from fetched content shown as bare URLs labelled as from the web**,
  so a page can't phish the owner through a message that looks like the agent's.
- Any other outward tool, if one is ever added, writes a **draft** to the queue; the agent raises it
  next session and it is sent only on the owner's yes.

**Rule 3 — private agents keep their private data in.** For an agent with `"private_only": true`
(companion plan, §13):
- **no state, episode or transcript content in an outward action** — `web_search` queries are
  generic (weather, news, a shipping status the owner named), never built from memory; the runner
  rejects a query or email that contains a state-doc string longer than a few words;
- **discreet email by default** — a neutral subject and a teaser ("She left something for you in the
  red room") rather than content; full content is a per-agent opt-in
  (`"email": { "discreet": false }`), because an inbox is less private than Conjure (lock-screen
  previews, provider retention).

**Tests:** rule 1 rejects reader+actor at load; `email_owner` ignores any recipient-shaped input and
rate-limits; bare-URL labelling; private-agent leak check; discreet default.

---

## 7. Phase 6 — the companion's jobs (first consumer)

Declared in `~/.config/conjure/agents/erotic/agent.json`, owned by [`companion.md`](./companion.md):

| job | trigger | tools | delegate | output |
|---|---|---|---|---|
| `consolidate` | `session_idle_minutes: 10` | `state_*`, `episode_write` | — | state, episodes |
| `reflect` | `session_end` | `state_*` | — | `relationship`, `self` |
| `away` | `after_absence_hours: 2` | none | `builder` | episode + `self.recent` |
| `thoughts` | `event: [gaze_dwell, approached, touch, returned]` + idle 20–40 s | her room tools | — | `say` |

`away` is off until enabled; `thoughts` arrives with companion phase 6.

---

## 7a. Phase 7 — a chat channel to the owner's phone (idea, 2026-11-26)

Email is one-way and slow. The next outward channel is a **two-way chat with the owner's phone**: the
agent messages him, and **his reply is a turn** — run detached (phase 1), so she can talk and write
state but not build. Same shape as `email_owner`: one fixed peer, the owner, and nobody else.

**Transport — open protocols only, end-to-end encrypted.**

| | protocol | phone apps | bot side | why / why not |
|---|---|---|---|---|
| **Delta Chat** (first) | email + Autocrypt/OpenPGP E2EE | iOS, Android | `deltachat-rpc-client` (Python), a normal account | rides the owner-email whitelist we already have; E2EE; no server to run — a mailbox for her (chatmail or own domain) |
| **Matrix** (later) | open, federated, E2EE | Element, FluffyChat | `matrix-nio` / matrix-rust-sdk (bot must do E2EE) | richest (voice notes, images, rooms, devices) and self-hostable (Synapse, Conduit); one more service to run — the upgrade if it becomes a hub for several agents |
| XMPP | open, OMEMO | Conversations (Android), Monal (iOS) | `slixmpp` | works; weaker apps |
| ntfy | open HTTP push, self-hostable | iOS, Android | one POST | one-way doorbell only ("come in"); fine as a fallback ping |

Ruled out: **Telegram** (bot chats are never E2EE — the server reads everything), **Signal**
(`signal-cli` is unofficial, needs its own number, against ToS), **WhatsApp / iMessage / Discord**
(closed; explicit content is a liability).

**Server.** `chat` in `agents/servers.json` — `message_owner(text, audio?)`. No recipient parameter;
the peer comes from owner config (e.g. `CONJURE_OWNER_CHAT` in `.env`). Inbound messages from **any
other sender are dropped** unread (logged, never shown to the model). Audio is her TTS output sent as
a voice note.

**Inbound turns.** A reply from the phone resumes the agent's most recent session (or opens a
detached one) as an `on_message` trigger (phase 2). It is a user turn with no world: tools limited to
`say` → chat, `state_*`, and queueing items for the next headset session. Rule 1 still holds — a run
that read the web doesn't also message out.

**Rules carried over from §6:**
- **only the owner**, automatically; anything else drafted;
- **private agents stay discreet on the lock screen** — E2EE protects the wire, not the notification
  preview, so the *first* message of an exchange is a teaser unless he opts in; replies inside a
  conversation he opened can carry content (per-agent `"chat": { "discreet": "first" | "always" | false }`);
- **rate limit + quiet hours** — at most N unprompted messages a day, none in his quiet hours,
  back-off when ignored (the initiative etiquette from phase 2).

**Tests:** non-owner inbound dropped and never reaches the model; no recipient input honoured;
discreet first message; rate limit and quiet hours; inbound turn can't call build tools.

---

## 8. Sequence

| phase | size | depends on | delivers |
|---|---|---|---|
| 1 declaration + runner + log | medium | — | detached Director, budgets, run log, shell verbs |
| 2 triggers | small–medium | 1 | session end/idle/absence, cron, events, etiquette |
| 3 delegation | small | 1 | decide → brief → act → record |
| 4 world-editing jobs | medium | 1, 3 | detached visits, job caller in the gate, idle-only runs, snapshots, undo |
| 5 web + email | small–medium | 1 | two MCP servers, rules 1–3 |
| 6 companion jobs | small | 1–4, companion 1–4 | consolidation, reflection, "while you were away" |
| 7 owner chat | medium | 1, 2, 5 | `message_owner` over Delta Chat, inbound replies as detached turns |

---

## 9. Not in scope

Jobs for other users' agents; a job editing a world while someone is in it; outward channels other
than owner email and owner chat (SMS, closed chat apps, posting anywhere, messaging anyone but the owner); jobs creating or deleting worlds; jobs changing
their own declaration.

---

## 10. Forks (→ `decisions.md`)

1. **Job model — decided 2026-11-25: the agent's own model** (the one its session last ran on).
   Reasons: no new provider sees the conversation; less refusal risk on content the model already
   handled; the same judgement that was present rates what mattered. Costs accepted: price on long
   transcripts, and some models' weaker structured output (schema check + one retry). Per-job `model`
   override for when either bites.
2. **Owner email — decided 2026-11-25: sent automatically, only to the owner's configured address**;
   the model never names a recipient.
3. **Owner chat transport — proposed 2026-11-26: Delta Chat first, Matrix later** (§7a); open,
   E2EE protocols only. Open: discreet-first vs always for private agents.
4. **Headless world edits** (a job editing a world that isn't live, while someone is in another) —
   deferred; switch-and-restore while idle covers the first consumers.
4. **Under discussion, not adopted** (noted in [`companion.md`](./companion.md) §17): a **pre-session
   briefing** job that anticipates the next session; a periodic **summary roll-up** job; APScheduler
   versus a plain asyncio task for §3.
5. **Where queued items surface** — spoken by the agent next session, shown in the shell, or both.
   Leaning: both; the agent raises at most one per session.
