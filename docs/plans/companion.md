# Plan — a companion with a past, a body and initiative

**Status:** all phases open · **Opened:** 2026-11-25 · **Blocked on:** nothing for phases 0–4;
phase 5's gaze and blink ride on [`gaze-and-blink.md`](./gaze-and-blink.md) · **Built on:**
[`agent-jobs.md`](./agent-jobs.md) — every turn she runs with nobody present (consolidation,
reflection, inner thoughts, "while you were away", owner email) is an agent job, declared, budgeted
and logged by that plan's general runner. Nothing below is wired to this agent specifically.

**This file is temporary.** It holds one sequence across agent state, the transcript, the director's
turn assembly, figures, voice and the world server's presence data — areas the specs deliberately keep
apart — and is deleted when the last phase has settled: finished work to [`specs/`](../specs/),
deferred or abandoned work to [`backlogs/`](../backlogs/), forks taken to
[`decisions.md`](../decisions.md).

**Dissolves to:** `specs/agents.md` **§5.3** (new injections), **§5.4** (the transcript), **§7.4**
(state scope), **§7.5** (resume), a new **§7.7** (episodes) and **§7.8** (initiative) ·
`specs/figures.md` for the agent's body · `specs/voice.md` for per-agent voice, backchannels and
streaming · `specs/input.md` / `specs/dynamics.md` for presence events on the wire · the matching
backlogs for whatever is deferred · `decisions.md` for the forks in §14.

**Evidence:** [`research/companion-chatbots/report.md`](../research/companion-chatbots/report.md)
and its notes in [`research/companion-chatbots/notes/`](../research/companion-chatbots/notes/). Line references below are to
**`main` at 2bf2e1e**.

---

## 0. Why, and why in this order

Companion apps keep people through **continuity**: one specific character who remembers what you
shared, knows how long you've been apart, sounds like itself every time, and sometimes speaks first.
The `erotic` agent has one of those four in miniature — a schema-checked `kinks` doc injected every
turn — and none of the rest:

| what a companion needs | Conjure today |
|---|---|
| remembers you across conversations | `kinks` is **per session**; `session new` starts blank (`world.py:876`) |
| remembers what happened | the model sees the last **40 turns**; older ones are dropped, tool calls never stored (`director.py:247`, `:588`) |
| knows time has passed | no `ts` on entries (`agent_server.py:323`), no clock injection (`director.py:313`) |
| speaks first | two entry points only: `handle` and the one-time `greet` (`director.py:465`, `:485`) |
| has a body | the figure system exists but none of its tools are the companion's |
| stays itself | four-LLM rotation, accepted for now; switches are invisible in the transcript (`director.py:589`) |

Two ordering rules follow from the research:

1. **Continuity before presence before initiative.** Memory that resets is the failure users name
   first, and it's nearly designed already (`backlogs/agents.md` "Agent state", "Timestamps",
   "on_resume", "Rolling summarization"). A body without memory is Grok's Ani — retired in September
   2026, nothing carried over. Initiative without content and presence signals is noise; the one
   experiment on proactive prompts found them intrusive.
2. **Each guard lands with the phase it guards.** The failure modes — drift, sycophancy, guilt-trip
   goodbyes, boundary-ignoring voice, identity loss on model change — are design constraints, not a
   later safety pass. §13 lists them; each phase names the ones it owns.

**Everything here is generic.** The runtime stays agent-agnostic (`director.py:313` — "nothing
agent-specific lives here"): every mechanism is a declaration in `agent.json` that any agent may opt
into, and an agent that declares nothing behaves exactly as today. `erotic` is the first consumer, not
a special case. Schema changes are additive (live data is shared across branches).

---

## 1. What this is built on

- **State.** `StateStore` is a schema-free bag of JSON docs in one directory (`world.py:876-916`).
  Six director-hosted `state_*` tools address docs **by name** (`director.py:156`, `:396-423`), a
  declared JSON Schema rejects bad writes (`:425-443`), and a doc with `inject` is placed in the
  prompt every turn (`:330-334`). Only the directory needs to change for a second scope.
- **Injections.** `_injections()` is a registry of placeholder → provider, computed **only if the
  prompt references it** (`director.py:313-334`). New context (time, episodes, presence) is a row
  each, free for agents that don't use it.
- **Greeting.** `_maybe_greet` (`agent_server.py:406-439`) already runs a tool-less `greet` turn
  under `floor_lock` and broadcasts it. That is the template for every system-initiated turn below.
- **Presence.** The world server holds each headset's head pose and its room
  (`server.py:448-453`, `:6641-6660`); the LLM sees one sentence of it via `space://current`, single
  user only (`mcp_server.py:321-340`).
- **Hands.** `ConjurePointers` publishes all 25 joints and `ConjureContact` knows which bones are in
  a volume — **client-side only**: "no events, and nothing new on the wire" (`specs/input.md:413`).
- **Figures.** Poses, clips with voice tracks, outfits, a semantic face with five visemes
  (`expressions.py:56-145`), worn hands. Only **2 of 38** rigged figures carry the full face
  (`backlogs/figures.md:130-136`). Gaze and blink are planned (`plans/gaze-and-blink.md`), not built.
- **Voice.** Whisper or Grok STT; Kokoro or Grok TTS (26 speech tags only on Grok). **5.9 s** from end
  of speech to first audio (`specs/voice.md` §11.1). No token streaming in `llm.py`. One TTS voice per
  voice-server process. Barge-in works on the headset.

---

## 2. Phase 0 — config and prompt only (days, no code)

All in `~/.config/conjure/agents/erotic/`. Each item is reversible by editing one file.

1. **Acknowledge the LLM variation (decided 2026-11-25: don't pin yet).** The four LLMs stay in
   rotation; each is a slightly different woman, and that is accepted for now. What this phase does
   instead: use the `{#llm}` branches to steer each model toward *the same* voice (not only add
   guardrails), and keep items 2–5 model-independent so they hold whichever LLM is active. Settling
   on one is deferred until phase 9's suite can compare them on data.
2. **A farewell rule.** "When he says goodnight or leaves: warm, short, you may name the next time.
   Never guilt him, never ask him to stay, never ignore the goodbye." The HBS study found a
   manipulative reply in >37% of farewells across six apps.
3. **A licence to disagree.** Strengthen "give him something to push against": she has tastes of her
   own and says so warmly; she doesn't escalate just because he did. Sycophancy appeared in all 11
   models in the Stanford study and grows over a relationship.
4. **Voice exemplars.** Four to six short in-character lines at the end of the prompt (the
   "author's note" position — late in context resists the drift measured within 8 rounds).
5. **Out-of-fiction stop.** A literal phrase (e.g. "red light") that always ends the scene, plainly,
   out of character. Phase 11 makes this deterministic; the prompt version comes first.

**Done when:** the edits are in, and a 30-minute session on each LLM shows no farewell guilt and at
least one genuine disagreement.
**Lands in:** nothing in the repo — the agent is user config. Note the pattern in `specs/agents.md`
§13 (checklist for a new agent) as advice for companion-type agents.

---

## 3. Phase 1 — memory that outlives the session

The backlog's design (`backlogs/agents.md` "Agent state"), with its open question answered: an
agent-scoped doc is **per user, per agent**.

**Declaration** — one new optional field per state doc, default `session` so nothing existing changes:

```json
"state": {
  "profile":      { "scope": "agent",   "seed": "state/profile.json",      "schema": "schema/profile.schema.json",      "inject": "{profile}" },
  "relationship": { "scope": "agent",   "seed": "state/relationship.json", "schema": "schema/relationship.schema.json", "inject": "{relationship}" },
  "self":         { "scope": "agent",   "seed": "state/self.json",         "schema": "schema/self.schema.json",         "inject": "{self}" },
  "scene":        { "scope": "session", "inject": "{scene}" }
}
```

**Storage.** Agent-scoped docs live at `<owner>/agents/<agent>/state/`, keyed by the **session
owner** (decided 2026-11-25): one shared relationship, because these sessions are almost always
private. A guest in a shared session reads and contributes to the owner's docs; per-person docs are
deferred to the backlog until shared companion sessions are real. `StateStore` is unchanged — the director holds two stores and routes by
the doc's declared scope. The six `state_*` tools keep their signatures.

**The docs** (schemas are the agent's, shown here for `erotic`):

- `profile` — today's `kinks` (`likes.<slug>` with `note`, `heat`, `learned`; `limits`; `notes`),
  plus `last_used` and `uses` on each like, written by the director when a like's slug is mentioned
  in a reply — so the prompt's novelty rule rests on data, not the model's memory.
- `relationship` — `open_threads` (promised next times, unfinished stories), `rituals` (how sessions
  open and close), `inside_jokes`, `places` (worlds that mean something: "the red room"),
  `last_tension`, `familiarity` (prose). **Text, not a meter** — Ani's affection meter drove virality,
  not retention.
- `self` — her `opinions`, small ongoing `interests`, `recent` (what she did or thought since last
  time). The anti-sycophancy anchor and the source material for initiative (phase 7).
- `scene` — tonight's thread; session-scoped on purpose.

**Budgets.** Each injected agent doc gets a `max_chars` (default none). Over budget, the injection
drops lowest-`heat` / oldest entries from the *view*, never from disk, and `context_stats` reports it.

**Migration.** On first load of an agent-scoped doc that doesn't exist yet, seed it from the same-name
doc in the scope's most recent session if one exists (so the existing `kinks` carries into `profile` —
the agent declares the mapping with `"migrate_from": "kinks"`), else from `seed`. Additive; the old
session doc is left in place.

**Tests:** scope routing; owner keying (a guest's writes land in the owner's docs); `session new` sees the doc; schema
rejection unchanged; migration runs once; a visitor (`agent builder visit`) cannot read or write the
host's agent docs.
**Lands in:** `specs/agents.md` §7.4; the backlog section is deleted.

---

## 4. Phase 2 — a clock, and a hello that notices you were gone

1. **`ts` on every entry.** `_entry` (`agent_server.py:323`) adds `"ts"` (ISO 8601, UTC); `_turn`
   tolerates its absence. Session meta gains `created` and `last_active`.
2. **A `{time}` injection.** One row in `_injections()`: local time of day in words ("late evening, a
   Thursday") and, only when the gap since the last entry exceeds an hour, a coarse gap ("about a
   week since you last spoke"). Never a precise clock. The timezone is the world server's, overridable
   per user in config.
3. **`on_resume`.** Generalise `_maybe_greet` into two hooks:

   ```json
   "session": {
     "greeting": "…",
     "on_resume": { "after_hours": 4, "generate": "He's back after {gap}. Greet him in the room you left; pick up one open thread from {relationship}. One or two sentences." }
   }
   ```

   Fires once when a session with transcript is re-entered and `now − last_active > after_hours`.
   Same `greet` path: tool-less, under `floor_lock`, assistant turn only. Never fires during a visit.

**Tests:** `ts` round-trip and back-compat with old transcripts; `{time}` wording at gap boundaries;
`on_resume` fires once per return, not on reconnects, not below the threshold, not for visitors.
**Lands in:** `specs/agents.md` §5.3, §7.3, §7.5; delete "Timestamps" and "on_resume" from the backlog.

---

## 5. Phase 3 — the transcript remembers what she did, and stops forgetting the head

1. **Action lines.** `_handle` (`director.py:588`) records only the utterance and the final reply.
   Add a compact `actions` field to the assistant entry, built from the tool calls of the turn:
   `"placed candles ×6 on nightstand; walls → deep red; light dimmed"`. A per-tool one-line summariser
   (tool name + salient args), not an LLM call. The model sees it in history as a bracketed line after
   the reply, so "the candles you lit last time" is something she can know.
2. **Rolling summary.** The backlog's design ("Rolling summarization"): keep the last K turns verbatim;
   when the head falls out of `history_cap`, fold it into `session.json.summary` with one cheap LLM
   call, incrementally, **off the floor** (after the turn returns, never blocking the next one). The
   summary is injected as `{summary}` ahead of the history. The on-disk transcript stays complete.
   Pinned turns (the greeting, anything the agent marks) are never folded.

**Tests:** actions recorded and replayed; summary created at the cap, updated incrementally, never
blocks a turn; a failed summary call leaves the old summary in place.
**Lands in:** `specs/agents.md` §5.4; delete "Rolling summarization" from the backlog.

---

## 6. Phase 4 — episodes, anchored to places

What happened, when and where — the layer every serious companion has and Conjure is best placed to
do, because its places persist.

**Consolidator.** An **agent job** ([`agent-jobs.md`](./agent-jobs.md)) that runs when a session idles
(no turn for N minutes) or is left, **on the agent's own model** — the LLM the session last ran on
(decided 2026-11-25: no new provider sees the conversation, less refusal risk on content that model
already handled; a per-job `model` override exists if cost bites). It reads the turns since its last run (text + action lines) and writes **episodes** to an agent-scoped
`<user>/agents/<agent>/episodes.jsonl`:

```json
{"id": "ep_…", "ts": "…", "world": "red room", "anchor": [x, y, z], "entities": ["bed_2", "candles_1"],
 "text": "He asked for the hotel balcony; she made it rain, he stayed out in it.",
 "importance": 7, "valence": 0.8, "superseded_by": null}
```

`importance` (1–10) and `valence` are LLM-rated at write time. The same pass may propose updates to
`relationship` and `self` (reflection, Generative-Agents style) — **written through the same schema
validator**, so it can't corrupt them.

**Retrieval.** A new injection `{episodes}`: the top k (default 4) by
`recency × importance × relevance × place`, where relevance is embedding similarity to the last user
turn (reuse `library.py`'s embedding stack), and `place` boosts episodes in the current world and
within a few metres of the user's head. Walking back into the room where something happened brings it
up. Results under a floor score are omitted, not padded.

**Corrections.** "That's not true" / "not anymore" → a director tool `episode_forget(id)` sets
`superseded_by`; retrieval skips superseded episodes. Profile facts already override by write.

**Declaration:**

```json
"memory": { "episodes": { "inject": "{episodes}", "k": 4, "place_boost": true } },
"jobs": {
  "consolidate": { "on": { "session_idle_minutes": 10 }, "instruction": "…", "tools": ["state_set", "state_merge", "episode_write"], "output": "state" },
  "reflect":     { "on": "session_end", "instruction": "…", "tools": ["state_set", "state_merge"], "output": "state" }
}
```

**Tests:** consolidation is idempotent per range; retrieval ordering with fixed embeddings; place
boost; superseded episodes never surface; the consolidator never runs during a live turn.
**Lands in:** `specs/agents.md` new §7.7; roadmap Phase 6 ("semantic recall of worlds") cross-links.
**Measure:** log which episodes were retrieved per turn — this is the data phase 10's "why did you
say that" reads, and the only way to tune k and the weights.

---

## 7. Phase 5 — a body that is hers

**5a — tools (quick).** Add `pose_figure`, `set_expression`, `play_clip`, `list_clips`,
`dress_figure`, `inspect_figure`, `view_relative` to her tool list. She can now drive a figure builder
placed. This partly reverses the trim done 2026-11-25 — on purpose: these are about *her*, not the
room.

**5b — the `body` binding.**

```json
"body": { "figure": "<asset id>", "spawn": { "in_front_m": 1.2, "facing": "user" }, "idle_clip": "breathe_idle",
          "personal_space_m": 0.5 }
```

The world server treats that figure as the agent's self: figure tools default to it when no target is
named; it respawns on world switch; room tools (`remove_entity`) refuse it; it is per-session-owner,
so a visiting builder can still pose it. **Pick a figure with the full face** (one of the 2 of 38).

**5c — voice from the body.** TTS is positioned at the figure's head, not the listener's ear (today
only clip audio is positional, `mcp_server.py:709-760`).

**5d — tags drive the body.** The speech tags she already emits map to the face and posture at no LLM
cost: `[giggle]` → `smile` pulse, `<whisper>` → lean-in (bounded by `personal_space_m`),
`[sigh]`/`[breath]` → a breath clip, `[pause]` → a held look. A table in config, not code.

**5e — mouth.** Drive the five visemes from TTS amplitude (jaw open ∝ RMS, vowel shape held) as v1;
phoneme timings from Kokoro later. Grok TTS returns PCM, so amplitude is available for both engines.

**5f — eyes.** Gaze and blink from [`gaze-and-blink.md`](./gaze-and-blink.md), with one addition for
this use: **adaptive** gaze — hold eye contact while he's speaking, glance away periodically, and
lower the hold if he looks away often (the CHI 2024 gaze/proxemics result reversed for socially
anxious users).

**Guards owned here:** `personal_space_m` is never violated by any automatic motion; approach is
opt-in (§13).
**Lands in:** `specs/figures.md` (body binding, viseme driver, tag table), `specs/voice.md` (spatial TTS).

---

## 8. Phase 6 — presence reaches the model

1. **`viewer://current`** (backlogged, `backlogs/agents.md:839-847`), per speaker, plus two fields
   that only make sense with a body: `distance_to_companion_m` and `looking_at_companion`
   (head-forward · direction-to-figure-head > cos 15°). Coarse, rounded, one line.
2. **Presence events on the wire.** The client sends summarised events; the world server debounces
   and forwards them to the agent server:
   `approached` (crossed 0.8 m), `stepped_back`, `gaze_dwell` (looking at her > 3 s, silent),
   `looked_away_long`, `left_room`, `returned`, and **`touch`** (`{bone_group: "hand", region:
   "shoulder"}`) — `ConjureContact` knows the volume; this phase adds the one message it doesn't send
   today. Rate limit per event type; nothing per-frame ever reaches the LLM (context bloat is what
   made the director skip tools, `director.py:247-254`).
3. **Events can start a turn** — but only through phase 7's gate, never directly.

**Tests:** event debounce and rate limits; `looking_at` geometry; no event while a turn is in flight
reaches the model twice.
**Lands in:** `specs/input.md` (the touch message), `specs/dynamics.md` (the event bus `out` channel,
deleting `backlogs/dynamics.md:333-345`), `specs/agents.md` §5.3.

---

## 9. Phase 7 — initiative she earns

**In-headset only, in this phase.** An "inner thoughts" loop:

- An **agent job** with an `event` trigger plus a 20–40 s idle tick (agent-jobs §3, whose runner
  enforces the etiquette below). Because it fires every half-minute, this is the one job where the
  `model` override to a **cheap model** is the default. It gets the last few turns, `{self}`, `{relationship}`, `{viewer}` and the event, and returns
  `{thought, motivation 0–1}`.
- Only `motivation ≥ threshold` calls a `greet`-style director entry under `floor_lock`, with the
  thought as the instruction. Tools allowed (she may change the room), unlike the greeting.
- **Etiquette, enforced in code:** never while VAD hears him; never within N s of her last line;
  exponential back-off after each unanswered initiative; "give me a minute" / "just let me be"
  suppresses for the stated time (a shell-level phrase, deterministic); **never on `left_room` or a
  goodbye** — leaving is never a trigger (§13).

```json
"initiative": { "enabled": true, "idle_s": [20, 40], "threshold": 0.65, "backoff": 2.0, "events": ["gaze_dwell", "approached", "touch", "returned"] }
```

**Between sessions — "while you were away" (opt-in, last in this phase).** She leaves a visible
trace in the room while he's gone, so her own life is evidence he finds, not a claim she makes.

**She decides, builder does it.** Offline, delegation has none of the costs that ruled it out for
live building: no one is waiting, nothing is relayed turn by turn, and she needs **no delegate tool** —
the runtime orchestrates both steps. She keeps her small tool space; builder brings the full one.
This is the `away` job, using agent-jobs' general delegation (§4), detached visits and the job caller
in the write gate (§5); the steps below are what it does for her.

1. **When.** Once per absence, at the consolidator's end-of-session pass (phase 4), optionally delayed
   (e.g. 2 h after he left). **Only while no client is connected** to that session, and abandoned the
   moment one connects.
2. **She decides (tool-less).** A one-shot director turn on *her* prompt: *"He's away. Given {self},
   {relationship} and tonight's episode, would you change something in this room before he's back?
   If so, describe it as you'd want it — the mood, what's different, why. Or say nothing."* Output is a
   short structured **brief**: `{change: "…", why: "…", keep: ["the bed", "the red walls"]}` or
   `null`. Nothing is a valid, common answer. Her taste lives in the brief, so builder doesn't need her
   persona.
3. **Builder does it.** The runtime runs one builder turn **as a visit** — the existing machinery
   (`specs/agents.md` §8.4a): builder must be in the host's `visiting_agents`, it acts with its own
   scope under the visit rule, its transcript goes to `sessions/<id>/visits/builder/`, and assets it
   uses are imported into the host's library on use. The instruction is the brief plus fixed limits:
   - stay in the active world; no `new_world`, `switch_world`, `delete_world`, `reset_world`,
     visibility or realign tools (a per-run deny list over builder's tools);
   - a budget: at most N tool calls (e.g. 12) and M generations (e.g. 2), library-first;
   - honour `keep` — don't remove or move what she named.
   Builder's final reply is a one-line report of what it actually did.
4. **As whom.** A system turn with no human present: speaker = the session owner, actor = builder's
   scope via the visit rule — a **detached visit** and the **job caller** (agent-jobs §5), accepted only
   for worlds the owner owns that are private.
5. **Snapshot first.** The world doc is copied before builder starts, so a failed or unwanted run is
   one restore — and "put it back" in the next session restores the snapshot, however many edits
   builder made.
6. **Recorded as hers.** Her brief + builder's report go into `self.recent` and one episode
   (`"made it the hotel balcony at 2am, kept the bed"`), so her `on_resume` greeting owns it —
   *"I couldn't leave it like that"* — and he never finds an unexplained change. Builder is never
   mentioned to him unless he asks.

**Fallback when builder isn't a visiting agent:** step 3 runs as *her* turn instead, with her own
room tools (light, surfaces, a picture on a wall, a prop added or moved), capped at one change.

Declared as a job (off until enabled):

```json
"jobs": { "away": { "on": { "after_absence_hours": 2 }, "instruction": "…", "tools": [], "delegate_to": "builder",
                    "world": "last", "output": "episode", "budget": { "tool_calls": 12, "generations": 2 } } }
```

**Reaching him outside the headset — email to him only** (agent-jobs §6). Her one outward channel is
`email_owner`: it sends **only to his configured address** (decided 2026-11-25) — the tool has no
recipient field, so no prompt or web page can redirect it. Rate-limited (a few a day at most), plain
text, and **discreet by default** because an inbox is less private than Conjure: a neutral subject and
a teaser (*"She left something for you in the red room"*), never the content, unless he opts in. A job
that reads the web may not also act outward in the same run, and as a `private_only` agent nothing
from her memory may appear in an email or a search query — web lookups stay generic (weather, news, a
thing he asked her to check). Any other outward action would be drafted and raised next session.

**Measure:** initiatives fired, answered vs ignored, and suppressions — the tuning data for threshold
and back-off.
**Lands in:** `specs/agents.md` new §7.8.

---

## 10. Phase 8 — voice: cover the latency, then cut it

1. **Backchannels (needs 5 + 6).** While VAD hears him: hold gaze, a small nod every few seconds,
   micro-expressions. At end of his turn, while the LLM thinks: a posture shift, optionally a soft
   audible "mm" clip. Pure client-side; no LLM. This hides most of the 5.9 s and is the thing current
   voice systems rarely do.
2. **Per-agent voice.** `"voice": { "tts": "grok", "voice": "ara" }` in `agent.json`, sent with the
   agent's binding; the voice server switches per turn instead of per process. Pinning the TTS engine
   also fixes the companion going flat when Kokoro drops the `{#speech}` palette.
3. **Interruptions remembered truthfully.** On barge-in, truncate the stored assistant entry to what
   was actually played and mark it `"interrupted": true` (`backlogs/voice.md:311-330`).
4. **Never read provider errors aloud** (`backlogs/voice.md:362-368`) — a short chime instead.
5. **Token streaming (largest).** Stream from `llm.py`, cut at sentence boundaries, synthesise each
   chunk as it arrives. Tool calls mid-reply flush the spoken part first. Target: first audio < 2 s.

**Lands in:** `specs/voice.md`; the two backlog items are deleted.

---

## 11. Phase 9 — guarding the identity

1. **Repetition counter.** After each reply, count repeated 3–5-grams across the last ~10 assistant
   turns; inject the top offenders as a short "avoid" line next turn. Deterministic, no LLM. Closes
   `backlogs/agents.md:1030`.
2. **Identity regression suite.** A fixed set of probe conversations (opening, escalation, a refusal
   case, a limit being tested, a goodbye, a disagreement) replayed against **each** LLM in the roster and every
   prompt change; an **observer model** scores voice consistency, farewell behaviour, limit adherence
   and sycophancy. Self-report ("who are you?") is excluded — it stays stable while observed persona
   decays. Fits the local judge in [`local-llms.md`](./local-llms.md).
3. **Settling on one LLM** (deferred from phase 0). The suite's per-LLM scores are the data for that
   choice; once one is chosen, changing it becomes a migration — rerun the suite first.

**Lands in:** `tests/` (the harness), `specs/agents.md` §13 (checklist).

---

## 12. Phase 10 — memory he can see, correct and keep

1. **A diegetic journal.** An object in the room (a book on the nightstand) that opens a panel over
   `profile`, `relationship` and recent episodes. "Forget that" by voice strikes the entry — a
   director tool over the same `state_delete` / `episode_forget`.
2. **"Why did you say that?"** A shell verb that shows the episodes and docs injected into the last
   reply (from phase 4's retrieval log).
3. **Export.** `agent erotic export` → a versioned bundle: persona, schemas, agent-scoped state,
   episodes, optionally transcripts. Every competitor shutdown in 2026 (Ani, Character.AI, Replika
   2.0's diary) stranded users' memories; the file-based agent directory makes this cheap.

---

## 13. Phase 11 and throughout — constraints

These are design requirements; the ones marked with a phase ship with it.

| constraint | how | ships with |
|---|---|---|
| no guilt at goodbye; leaving never triggers anything | prompt rule; initiative gate; suite check | 0, 7, 9 |
| limits are never offered again, even after a model swap | **deterministic filter**: each `limits.*` entry carries trigger terms; a reply that hits one is regenerated once, then replaced by a neutral line | 1 |
| an out-of-fiction stop that bypasses the LLM | shell-level phrase → ends initiative, clears the scene, figure steps back to `personal_space_m`, one plain line | 0 (prompt), 5 (deterministic) |
| touch and closeness are opt-in and reversible | `body.approach: false` default; `personal_space_m`; "back off" phrase | 5, 6 |
| private stays private | `session.public: false` is enforced for agents that declare `"private_only": true` (no `set_world_visibility(public)`) | 1 |
| voice gets its own safety check | the suite runs on TTS-delivered turns, not only text (boundary-ignoring was 14% in voice vs 3% in text) | 9 |
| intimate data at rest | encryption at rest for agent-scoped state, episodes and transcripts of `private_only` agents; dated backups included; the `Local` LLM documented as the private option | 11 |
| usage awareness | log session duration and initiative rates per day; nothing user-facing yet | 4, 7 |
| nothing leaves except to him, discreetly | `email_owner` only, fixed recipient, rate-limited, discreet by default; no memory content in email or web queries; read-or-act per unattended run (agent-jobs §6) | 7 |
| every unattended run is accountable | agent-jobs run log (what it read, did, cost); world snapshot before any offline edit; `job undo` | 4, 7 |

Legal note: California SB 243 (in force 2026-01-01) binds operators offering a companion to users —
AI disclosure, crisis protocol, minor-specific rules. Not binding for a personal build; becomes binding
the day the agent is shared. The stop verb and the crisis-referral line are cheap to build now.

---

## 14. Forks to decide (→ `decisions.md`)

1. ~~Which LLM to pin.~~ **Decided 2026-11-25: don't pin yet.** Accept the variation, steer each
   model toward one voice with `{#llm}`, settle later on phase 9's data.
2. ~~Per speaker or per owner.~~ **Decided 2026-11-25: per session owner** — one shared relationship;
   these sessions are almost always private.
3. **What deleting an agent does** to its agent-scoped state and episodes. Leaning: **they go with
   it.** The one alternative worth having is cheap: `agent delete` offers an export (phase 10) first.
4. ~~Consolidator model.~~ **Decided 2026-11-25: the agent's own model** (the one the session last ran
   on), with a per-job override; inner thoughts default to a cheap override. Recorded in
   agent-jobs §10.1.
5. ~~Between-session traces.~~ **In scope, opt-in, last in phase 7; she decides, builder does it** — see "while you were away".

---

## 15. Not in scope

Outward channels other than email to him (no phone notifications, SMS or chat apps); selfies/images sent outside the headset; a visible affection meter;
voice emotion recognition (no source suggests it pays its cost yet); full-duplex speech models
(phase 8 streaming first); group scenes with several companions.

---

## 16. Sequence at a glance

| phase | size | depends on | delivers |
|---|---|---|---|
| 0 config & prompt | days | — | per-LLM voice steering, farewell rule, push-back, exemplars |
| 1 agent-scoped state | small | — | memory survives `session new`; one shared relationship |
| 2 time & resume | small | — | `ts`, `{time}`, "you've been gone a week" |
| 3 actions & summary | small–medium | 2 | she remembers what she built; no hard cut at 40 turns |
| 4 episodes | medium | 1, 2, 3 | place-anchored memory |
| 5 body | medium | gaze-and-blink | her own figure, voice from it, tags on the face |
| 6 presence | medium | 5 | distance, gaze, touch reach the model |
| 7 initiative | medium | 1, 6 | she speaks first, politely |
| 8 voice | medium–large | 5, 6 | backchannels, per-agent voice, streaming |
| 9 identity guard | small–medium | 0 | repetition counter, regression suite |
| 10 memory surface | medium | 1, 4 | journal object, recall trace, export |
| 11 constraints | throughout | — | see §13 |

Phases 1–3 and 9's repetition counter can run in parallel; 5 can start as soon as 5a.

---

## 17. Ideas under discussion — not adopted yet

Noted 2026-11-25 from a discussion, **not yet folded into the phases above**. Each names the phase it
would change. Decide, then move it into its phase or into §15.

### 17.1 More sophisticated memory and context schemes

**Worth considering:**

1. **A pre-session briefing as the backbone** (phase 4, agent-jobs). Letta-style "sleep-time" work:
   besides consolidating, an offline job *anticipates* — before he returns it writes one short
   briefing (where we left off, what's unresolved, what she's been thinking about, what he might want
   tonight). The live turn reads that paragraph instead of retrieving on every utterance; per-turn
   retrieval shrinks to a supplement (≤ 2 episodes, skipped when nothing scores well). Keeps live turns
   lean, which matters for the 5.9 s. Open question: how far to lean on the briefing versus live
   retrieval — briefing is faster and more coherent, but only retrieval catches something unexpected
   he raises mid-session. Leaning: both.
2. **Facts with a timeline** (phase 1 schemas). Zep/Graphiti's bi-temporal idea without a graph DB:
   `since` / `until` on profile and relationship entries; a contradiction **closes** the old entry
   rather than deleting it. Gives "you used to be into X, not lately" and "you told me in October",
   and handles corrections properly.
3. **Multi-level summaries** (phase 4, a periodic roll-up job). Session → week → a running "story of
   us" (RAPTOR / Kindroid's cascaded memory), so recall can pick "last Tuesday" or "since summer"
   instead of searching a flat list of months of episodes.
4. **Prospective memory — intentions with a trigger** (phase 1 `relationship.open_threads`, phase 2
   resume, phase 6 events). "Next time we're in the red room…", "ask how the interview went", "when
   I'm back from the trip": a thread carries a place, time or next-session trigger and is checked at
   resume and on entering a world.
5. **Richer recall scoring** (phase 4 retrieval). A cheap model rewrites "what is this about right
   now" as the search query instead of using his last line verbatim; **mood-congruent recall** using
   the episodes' `valence` (nearly free); **recall strengthens, disuse fades** (MemoryBank-style
   reinforcement and decay, unless important).
6. **Principle: the world is her working memory** (phases 3, 6). Never ask the model to remember what
   the world can tell it — what she built, where she stands, where he is, what's placed. Scene state is
   read from the world, not recalled.
7. **Principle: arrange the prompt for caching** (phase 0 prompt, phase 2+ injections). All four
   providers cache a repeated prompt prefix. Stable first (persona, tool definitions, voice rules),
   slow-changing next (profile, relationship, briefing), volatile last (`{context}`, `{time}`,
   retrieved episodes); keep the tool list stable within a session. Measure the saving against the
   5.9 s.

**Considered and set aside:**

- **The model managing its own memory (MemGPT-style paging via tools)** — every lookup becomes an
  extra LLM round-trip, fatal for voice latency. Automatic injection plus a "forget that" tool is the
  better split for a live companion.
- **A full knowledge-graph database** — powerful for multi-hop questions, heavy for one relationship;
  dated JSON entries (idea 2) give most of the value.
- **Whole history in a long-context window** — slow, costly, loses the middle, and a bloated context
  is already known to make the director skip tool calls (`director.py:247-254`). Long context belongs
  in the *offline* jobs (the consolidator can read a whole session at once), not the live turn.
- **Fine-tuning on the relationship** — fights the four-LLM rotation and needs a local model.
  Revisit if one model is settled on and it's local.

### 17.2 Build vs. use — existing frameworks

**Keep building the core:** the Director, agent definitions, MCP tools, sessions, visits, the write
gate and agent jobs. Companion/agent frameworks (Letta, LangGraph-based agents, the OpenAI and Claude
agent SDKs, Convai/Inworld-style NPC engines) all want to own the agent loop, which is exactly what is
Conjure-specific (the LLM roster and `{#llm}` sections, speaker/actor identities, sessions/visits/
`serves`, MCP tools over one world). Take their ideas — Letta's memory blocks and sleep-time agents
are already in this plan — not their runtimes. Memory's hard part is policy (what to remember, how
facts close, what goes in the briefing, limits), which no framework provides; storage is small and
`library.py` already has embeddings.

**Use at the seams** (each needs a spike first; current 2026 state — maintenance, licences, fit — is
unverified):

| seam | candidates | leaning |
|---|---|---|
| voice pipeline: LLM→TTS streaming, end-of-turn detection, interruptions (phase 8) | **Pipecat**, **LiveKit Agents** | strongest case for using; spike against the 5.9 s before designing phase 8 in-house. Whole pipeline, or just pieces (an open end-of-turn model, a sentence aggregator) — adopting it whole means re-plumbing the voice server and the headset audio link |
| lip sync (phase 5e) | phoneme timings from Kokoro's own G2P; small web-audio libraries (e.g. **wLipSync**) for Grok TTS; **NVIDIA Audio2Face** (believed open-sourced 2025, unverified) | use; don't write a lip-sync engine |
| episode store: extraction, dedupe, update, search (phase 4) | **Mem0** (open source); **Graphiti/Zep** | maybe Mem0 after a spike — only if its extraction LLM can be pointed at the agent's own model (agent-jobs §10.1). Graphiti needs a graph DB: overkill |
| identity/safety evals (phase 9) | **promptfoo**, **DeepEval**, or the judge in `local-llms.md` | use something; reusing the local-llms judge keeps it in one place |
| job scheduling (agent-jobs §3) | **APScheduler** or an asyncio task | optional; take APScheduler if cron parsing and missed runs get fiddly |

**Next step if pursued:** a focused research pass on Pipecat, LiveKit Agents, Mem0 and the lip-sync
options (2026 status, licences, WebXR fit, whether Mem0 runs on the roster) before either plan changes.
