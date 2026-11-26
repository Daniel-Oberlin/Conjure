# Conjure today, as a host for a relationship/companion agent (the `erotic` agent)

Scope note for the report writer: the sources are local files, not web pages. Line numbers are for
**`main` at daf5941** (2026-11-24, "the agent serves everyone present; other agents can visit"). The
checked-out branch `feat/unity-builds` (ef5a1d6) branched before that commit, so its working tree has no
`serves`/`visiting_agents` code and slightly different line numbers. `[main:conjure/x.py:N]` means
`git show main:conjure/x.py`, line N. The user agent lives outside the repo at
`~/.config/conjure/agents/erotic/` (its own git repo, last commit 80ca24a). `~/.config/conjure/private-notes.md`
is headed "LLMs disregard", so it was not used.

## 0. The companion agent as it exists

### Takeaway
`erotic` is a declarative agent: a persona prompt, a 16-tool room-dressing subset of the world tools, one
schema-validated memory doc (`kinks`) injected into every turn, a literal greeting, and private-by-default
sessions. It has no body, no figure tools, no time awareness and no way to act unless spoken to.

### Cited Findings
- `agent.json`: `llms: ["Grok","Claude","Chat","Gemini"]` (also the priority order, so Grok is tried first); tools: `set_immersion, show_edges, style_surface, texture_surface, set_environment, place_asset, place_cached_asset, search_library, move_entity, remove_entity, generate_image, place_image, list_worlds, new_world, switch_world, set_world_visibility`; context `space://current, world://current, dynamics://available`; `dynamics: ["grab"]`; `visiting_agents: ["builder"]`; `world.on_create` turns edges off; `session.public: false`; a literal `session.greeting` string; `state.kinks` with seed, schema and `inject: "{kinks}"` — [~/.config/conjure/agents/erotic/agent.json](file:///Users/daniel/.config/conjure/agents/erotic/agent.json)
- The persona prompt sets: a woman lover, "warm, playful, filthy when it's earned"; spoken style (short turns, contractions, no lists, no asterisk stage directions); escalate gradually; second person, present tense; never mention its own mechanics; role-play rules (stay in character, give him something to push against, take out-of-fiction redirections without comment); the room as foreplay (style surfaces, lighting, place assets, hang images); "build while you talk" — [erotic/prompt.md](file:///Users/daniel/.config/conjure/agents/erotic/prompt.md)
- The prompt uses `{user}`, `{#speech}…{speech}…{/speech}` (its own palette of quiet Grok tags: `<soft>`, `<whisper>`, `<slow>`, `[breath]`, `[sigh]`, `[giggle]`, `[pause]`…), a `{#llm}{=grok}…{=*}{/llm}` block with three Grok-only tool guardrails, `{kinks}`, and `{#context}…{context}{/context}` — [erotic/prompt.md](file:///Users/daniel/.config/conjure/agents/erotic/prompt.md)
- Memory instructions in the prompt: write to `kinks` "the moment you learn something" through `state_set(doc='kinks', path='likes.<slug>', value={note, heat 1–5, learned})`; raise or lower `heat`; `state_delete` entries that don't land; hard no's go in `limits.<slug>` and "are never offered again"; `notes` holds pacing, pet names and openings; never list the doc or say it remembered something; work in one or two high-heat threads per session; keep some novelty — [erotic/prompt.md](file:///Users/daniel/.config/conjure/agents/erotic/prompt.md)
- Schema: `likes` and `limits` are objects keyed by `^[a-z0-9][a-z0-9-]*$` slugs; a like is `{note (required), heat int 1–5, learned}`; a limit is `{note}`; `notes` is a string; `additionalProperties: false` — [erotic/schema/kinks.schema.json](file:///Users/daniel/.config/conjure/agents/erotic/schema/kinks.schema.json). Seed: `{ "likes": {}, "limits": {}, "notes": "" }` — [erotic/state/kinks.json](file:///Users/daniel/.config/conjure/agents/erotic/state/kinks.json)
- For proper building (figures, rearranging, a new sky) the prompt tells the user to say "agent builder visit" and "agent erotic" to come back — [erotic/prompt.md](file:///Users/daniel/.config/conjure/agents/erotic/prompt.md)
- Usage on disk: the live data root has no `erotic` sessions now. Backups (`users.bak-2026-11-03`) hold one session, `"first session"`, private, `llm: "Chat"`, a 114-line transcript, and a `kinks` doc with 2 likes, 0 limits and empty notes (contents not read) — `~/.local/share/conjure/users.bak-2026-11-03/daniel/agents/erotic/sessions/session-1/`

### Inferences
- `dynamics: ["grab"]` is declared, but the agent has no `conjure_module` tool, so it can't conjure grab itself (the module only places through that tool — [main:docs/specs/dynamics.md:637-680](docs/specs/dynamics.md)). The field appears to have no effect for this agent.
- The agent has none of the figure tools (`inspect_figure`, `pose_figure`, `set_expression`, `dress_figure`, `play_clip`, `list_clips`, `wear_hand`) and no `view_relative` or `add_entity`. So it can't give itself a body or place things relative to the user's head.

### Gaps
- No usage telemetry beyond the one backed-up session; how often the agent actually writes `kinks` in practice is unmeasured.

## 1. Memory: state docs, transcripts, scope, cross-session behaviour

### Takeaway
Memory is (a) the full verbatim transcript on disk, of which only the last 40 turns (user/assistant text
only) reach the model, and (b) schema-validated JSON state docs that can be injected into every turn. Both
are **per session**, so `session new` starts the companion with a blank `kinks` doc. There is no
summarization, no retrieval over past sessions, no timestamps, and no memory shared across sessions.
Every one of these is written up as backlog, not built.

### Cited Findings
- Six director-hosted tools: `state_get(doc, path?)`, `state_set(doc, path, value)`, `state_merge(doc, value)`, `state_delete(doc, path?)`, `state_list()`, `state_schema(doc)`. They run in-process, not over MCP, and are offered only when the agent declares `state` — [main:docs/specs/agents.md §7.4](docs/specs/agents.md); [main:conjure/director.py:156](conjure/director.py) (`_STATE_TOOL_SPECS`), [:200-210](conjure/director.py), [:396](conjure/director.py) (`_state_tool`)
- `seed` is copied into the session's `state/` once, at construction (`_maybe_seed`, gated on `seeded: false`, never overwrites an existing doc). `schema` is JSON Schema checked on every write; a violation rejects the write (skipped if `jsonschema` isn't installed). `inject` puts the live doc into the prompt as JSON on every turn — [main:conjure/agent_server.py:378-403](conjure/agent_server.py); [main:conjure/director.py:425-443](conjure/director.py); [main:conjure/director.py:313-334](conjure/director.py)
- `StateStore` is "a per-session bag of named JSON documents", stored at `<data>/users/<user>/agents/<agent>/sessions/<sid>/state/<doc>.json` with atomic writes — [main:conjure/world.py:876-916](conjure/world.py); [main:conjure/world.py:785-790](conjure/world.py)
- The backlog states the companion problem directly: "State is per-session, and some agents need per-agent… Start a second session with the same agent and it seeds blank again… Surfaced by a user agent keeping a `kinks` doc — a list built up over months is meaningless if a new conversation resets it." Proposed fix: a `"scope": "agent" | "session"` field per doc, with agent-scoped docs at `<user>/agents/<agent>/state/` (likely per user per agent). Not built — [main:docs/backlogs/agents.md:229-252](docs/backlogs/agents.md)
- It mostly doesn't show today because `agent <name>` resumes the scope's most recent session (an MRU list in `_active.txt`) rather than starting a new one — [main:docs/backlogs/agents.md:236-239](docs/backlogs/agents.md); [docs/specs/agents.md §7.6](docs/specs/agents.md)
- Transcript: append-only `sessions/<sid>/transcript.jsonl`, one `{role, by, text}` per line. **No timestamps**, and tool calls and results are not stored — [main:conjure/agent_server.py:323-330](conjure/agent_server.py) (`_entry`); [main:conjure/llm.py:29-35](conjure/llm.py) (`Turn` = speaker, text, by); [main:conjure/world.py:792-822](conjure/world.py)
- Only the user utterance and the **final** assistant text are added to the transcript each turn. Intermediate narration and tool traffic are dropped from memory — [main:conjure/director.py:588-589](conjure/director.py)
- Replay: the whole saved transcript is loaded into the Director once per session change (`_sync_transcript`), but the model sees only `_recent_history()`, the last `history_cap` turns: default 40 (about 20 exchanges); `CONJURE_HISTORY_CAP`; 0 = unlimited. The cap exists because "a bloated history was observed to make the director skip tool calls" — [main:conjure/agent_server.py:333-356](conjure/agent_server.py); [main:conjure/director.py:247-254](conjure/director.py); [main:conjure/config.py:463,688](conjure/config.py)
- Older turns are dropped outright. "Rolling summarization" (keep a recent tail verbatim, summarize the older part into a running summary stored at `session.json.summary`, pin key turns) is designed but unbuilt. So is a per-model token budget — [main:docs/backlogs/agents.md:195-212](docs/backlogs/agents.md); [:1108-1138](docs/backlogs/agents.md)
- `clear` wipes the session's transcript (memory and JSONL) but keeps worlds and state docs. `reset agent <name>` wipes every session, transcript, state doc and world for that agent — [docs/specs/agents.md §6.3, §7.3](docs/specs/agents.md); [main:conjure/world.py:801-805](conjure/world.py)
- A state doc is addressable and deletable from the shell at `/<user>/agents/<agent>/sessions/<sid>/state/<doc>` ("the agent forgets it") — [docs/specs/agents.md §6.4](docs/specs/agents.md)
- No undo for state edits. Seed drift has no migration — [main:docs/backlogs/agents.md:254-268](docs/backlogs/agents.md)
- Persisted per session: `session.json` (`id, owner, agent, title, public, active_world, llm, greeted, seeded`) plus worlds. The remembered LLM is restored on resume — [docs/specs/agents.md §7.1, §7.3](docs/specs/agents.md)
- Embeddings and semantic search exist only for the **asset library** (`library.py`), not for dialog or worlds — [docs/specs/agents.md §2.2](docs/specs/agents.md)
- Roadmap Phase 6, "Memory & connections", covers worlds and semantic recall of worlds, not personal memory — [main:docs/roadmap.md:97-99](docs/roadmap.md)

### Inferences
- In effect the companion has a short-term window of about 20 exchanges plus one structured "user profile" doc that lasts only as long as one session. Compared with apps that offer long-term memory (summaries, retrieval, a memory UI), Conjure has the structured-profile half, built with validation and scoping, but not the episodic half (what happened in past sessions).
- Tool calls aren't recorded and dropped turns aren't summarized, so the agent can't remember what it built or did earlier unless the world itself still shows it (`world://current`).

### Gaps
- No measurement of how much prompt the `kinks` injection costs as it grows. `context_stats` reports characters, and nothing caps a growing doc.

## 2. Persona and prompt templating

### Takeaway
The prompt system is solid. `prompt.md` owns all the text; the runtime fills `{user}`, `{context}`,
`{speech}` and state placeholders (only when they appear), handles `{#name}…{/name}` conditional sections,
and resolves `{#llm}{=name}` per-LLM branches on each turn, with checks at load time. The greeting is a
literal string or one LLM-generated line. Personas as separate characters are designed but not built.

### Cited Findings
- Injections: `{user}` = this turn's speaker; `{context}` = the agent's MCP resources joined together; `{speech}` = speech-tag guidance registered by connected voice clients; one placeholder per state doc. A provider runs only if its placeholder is present — [main:conjure/director.py:313-357](conjure/director.py); [docs/specs/agents.md §5.3](docs/specs/agents.md)
- `{#name}…{/name}` keeps its block only when the value isn't blank. Only registered names are replaced, so other braces survive — [main:conjure/director.py:68-92](conjure/director.py)
- `{#llm}` branches (`{=grok}`, `{=gemini,chat}`, `{=*}` for the rest) are resolved **per turn**, before injection. Load fails on an unknown LLM name, a name not in the agent's `llms`, or malformed markers. Granularity is per LLM, not per model — [docs/specs/agents.md §5.3a](docs/specs/agents.md); `agents.validate_llm_sections`
- LLM roster: Claude, Gemini, Chat (OpenAI), Grok (xAI), Local (Ollama/LM Studio/mlx_lm). One LLM is active at a time and it is shared; switching is a shell command (`llm <name>`; by voice "talk to gemini"); the choice is remembered per session — [main:conjure/llm.py:743-753](conjure/llm.py); [docs/specs/agents.md §5.2, §6.2](docs/specs/agents.md)
- Greeting: `session.greeting` is a literal string, appended verbatim, or `{"generate": "<instruction>"}`, which runs one `Director.greet` turn with no tools. It fires **once per session**, only if the session is fresh and empty; `greeted` then flips to true permanently — [main:conjure/agent_server.py:406-439](conjure/agent_server.py); [main:conjure/director.py:485-507](conjure/director.py)
- There is no on-resume greeting or recap. `on_resume` ("Previously, you were in…") is a backlog item — [main:docs/backlogs/agents.md:222-224](docs/backlogs/agents.md)
- `personas` is parsed into `AgentDef.personas` and "read by nothing". The design is an `invoke_persona(name, …)` tool: tool-less, speech-and-intent-only characters inside an experience — [docs/specs/agents.md §3 table](docs/specs/agents.md); [main:docs/backlogs/agents.md:83-97](docs/backlogs/agents.md); [main:docs/vision.md:30-50](docs/vision.md)
- Each user utterance reaches the model labelled `"<speaker>: text"`. Assistant turns carry no LLM attribution, so an LLM switch doesn't show in context — [main:conjure/director.py:575-576, 588-589](conjure/director.py)
- Loop guards: the same tool with the same args is refused after 2 calls per turn (`REPEAT_LIMIT`), and a turn is capped at `MAX_TOOL_HOPS = 24` — [docs/specs/agents.md §5.6](docs/specs/agents.md); [main:conjure/director.py:51, 552-560](conjure/director.py)
- Known LLM behaviour problems are logged in the backlog: the director says "Done" without calling the tool, Grok narrates one tool and calls another, replies drift toward a repeated phrase as a session runs, Grok carries tool arguments over between turns — [main:docs/backlogs/agents.md:863-1084](docs/backlogs/agents.md)

### Inferences
- "Replies converge on a repeated phrase as a session runs" ([backlogs/agents.md:1030](docs/backlogs/agents.md)) is the same repetition problem companion apps fight. The erotic prompt's novelty rule addresses it only through prompting.
- There's no user-facing persona editor or character card. A persona is a hand-edited `prompt.md` plus `agent.json`.

### Gaps
- No mechanism found for a per-user persona variant (e.g. a different name or voice for the companion per user) beyond writing a separate agent directory.

## 3. Time awareness

### Takeaway
The agent knows nothing about time: no current time, no time since the last session, no session history.
The backlog proposes coarse relative markers for a "journaling companion" as opt-in.

### Cited Findings
- The injection registry holds exactly `user`, `context`, the client slots (`speech`) and state docs. No time or date provider exists — [main:conjure/director.py:313-334](conjure/director.py)
- Transcript entries have no `ts`. The backlog suggests storing it ("nearly free") and, as an opt-in, showing the model **coarse/relative** markers ("later that day", a gap only when it's large). "A time-aware agent (a game with day/night, a journaling companion) turns it on." Not built — [main:docs/backlogs/agents.md:214-220](docs/backlogs/agents.md)
- `session.json` has no created or last-active timestamps. Rung 2 of arrival uses file mtime only to pick a world or session — [docs/specs/agents.md §7.1, §7.6](docs/specs/agents.md)
- A shared `ConjureClock` exists only for client modules (synchronized animation), not for the LLM — [main:docs/specs/dynamics.md §6](docs/specs/dynamics.md)

### Inferences
- The companion can't say "it's been a week" or "good morning", and can't tell a resumed session from a continuous one. The only hint would be the content of the last 40 turns.

### Gaps
- None. Absence confirmed by reading the injection registry.

## 4. Proactivity: unprompted speech, timers, acting between turns

### Takeaway
The agent is purely reactive. An LLM turn runs for a user utterance, or for the one-time greeting of a new
session. There are no timers, scheduled turns, idle check-ins, or event-driven turns (touch, proximity,
gaze, module events).

### Cited Findings
- `Director.handle` (one user utterance) and `Director.greet` (once per session) are the only two LLM entry points. A single "floor" refuses a concurrent turn (`Busy`) rather than queueing it — [main:conjure/director.py:465-507](conjure/director.py)
- The agent server runs an LLM turn from `_handle_turn` (a client `turn` message) and `_maybe_greet` (during reconcile). Reconciliation with the world server re-binds, seeds and greets but never calls the LLM for anything else — [main:conjure/agent_server.py:457-530, 550-585](conjure/agent_server.py)
- World-server notices are relayed to clients as server-written `notice` events, not LLM speech — [docs/specs/agents.md §8.1](docs/specs/agents.md)
- Module events (`module_event`, e.g. a water touch) are relayed only between headset peers. The event-bus `out` channel ("a downsampled/summarized feed to the LLM… 'user dragged the globe for 10 s'") and an agent-callable `module_event` tool are listed as missing — [main:conjure/server.py:6638-6640](conjure/server.py); [main:docs/backlogs/dynamics.md:333-345](docs/backlogs/dynamics.md)
- The vision's event model ("timers, proximity, gaze, controller/hand input, collisions, voice") and a behavior SDK that "set[s] timers" are Phase 7, unbuilt — [main:docs/vision.md:281-298](docs/vision.md); [main:docs/roadmap.md](docs/roadmap.md) Phase 7

### Inferences
- Companion apps' proactive messages (check-ins, "thinking of you", reacting to silence) have no hook here. The shortest path in this architecture would be a server-side trigger that calls a `greet`-like Director entry under `floor_lock`; `_maybe_greet` already shows the pattern.

### Gaps
- None. Absence confirmed in code.

## 5. Voice: STT, TTS, speech tags, latency, barge-in

### Takeaway
There is a full voice loop: Silero VAD, then Whisper small.en (local) or Grok streaming STT; turns go to
the agent server; replies come back through Kokoro (local, voice `af_heart`) or Grok TTS (voices eve, ara,
rex, sal, leo) with 26 expressive speech tags. It runs on the host or through the Quest (an
AudioWorklet-over-WebSocket relay) in push-to-talk or open-mic modes. Barge-in works on the headset by
button press or by talking over the reply. Measured speech-end to first audio is about 5.9 s, and there is
no token-level streaming. Nothing detects emotion in the user's voice.

### Cited Findings
- Pipeline: mic → Silero VAD → STT (`CONJURE_STT` = `whisper` small.en local, segmented; or `grok`, `grok-voice-transcribe-2.0`, streaming) → turn aggregator → VoiceBridge (WebSocket to the agent server) → TTS (`CONJURE_TTS` = `kokoro` `af_heart` local; or `grok`, xAI HTTP TTS, voice `CONJURE_XAI_TTS_VOICE` default `eve`) → speaker — [main:docs/specs/voice.md §2, §4, §5](docs/specs/voice.md); [main:conjure/tts.py:64-67, 114-123](conjure/tts.py)
- Speech tags: Grok renders inline tags (`[laugh]`, `[pause]`, `[sigh]`, `[giggle]`, `[breath]`…) and wrapping ones (`<whisper>`, `<slow>`, `<emphasis>`, `<singing>`…). The voice client registers its vocabulary and guidance on connect; the server puts it into `{speech}`; each connection gets a rendering with its own engine's tags kept and the rest stripped (CLI sees plain text). The transcript stores the tags — [main:docs/specs/voice.md §5.1](docs/specs/voice.md); [docs/specs/agents.md §5.3, §10](docs/specs/agents.md)
- A speech stage rewrites LLM prose for voice: strips asterisks, names emoji in words, reads `#5` as "number 5" — [docs/specs/agents.md §10 "The speech stage"](docs/specs/agents.md); `conjure/speech.py`
- Turn segmentation: VAD `stop_secs 0.6`; push-to-talk turn end at 0.8 s; open mic uses the Smart Turn v3 model, with a 2 s fallback for unfinished pauses — [main:docs/specs/voice.md §3](docs/specs/voice.md)
- Measured latency (host spike, 2026-10-18): speech end → transcript 2.4 s; transcript → first reply audio 3.5 s; **speech end → first reply audio 5.9 s** — [main:docs/specs/voice.md §11.1](docs/specs/voice.md)
- No token streaming. Each LLM round trip produces whole text (`on_text` fires per round trip: an ack, pre-tool narration, the final reply), and each event becomes its own `TTSSpeakFrame`. `llm.py` has no streaming calls — [docs/specs/agents.md §5.1](docs/specs/agents.md); [main:docs/specs/voice.md §5](docs/specs/voice.md); grep of [main:conjure/llm.py](conjure/llm.py)
- Barge-in: on the host it is **off** (mute-while-speaking; earbuds assumed). On the headset, pressing push-to-talk during a reply sends `{type:"interrupt"}`, and in `open` mode talking over the reply interrupts it (more than −45 dBFS in 12 of 20 frames). The server cancels that connection's in-flight turn, broadcasts "[user interrupted]" and fires `turn_done` — [main:docs/specs/voice.md §6, §11](docs/specs/voice.md); [main:conjure/agent_server.py:526, 799-803](conjure/agent_server.py). (The module docstring at [main:conjure/agent_server.py:23](conjure/agent_server.py) still says barge-in is "shelved", which is stale.)
- Interruption isn't reflected in memory. A barge-in after the reply was generated leaves the whole reply in the transcript as if heard, and one mid-turn drops both the utterance and the partial reply. "Remember an interrupted reply as heard" is designed, not built — [main:docs/backlogs/voice.md:311-330](docs/backlogs/voice.md)
- Headset audio: Quest page `client/voice-link.js` → world server `/audio` relay → voice server `conjure.voice --serve`, one pipeline per connection. Activation is `ptt` (default) or `open` or `off`; browser AEC is on. Multiple headsets mean N pipelines sharing one floor — [main:docs/specs/voice.md §11](docs/specs/voice.md)
- Optional mic wake word (`--wake-word`, no default), separate from the shell's "conjure" wake word — [docs/specs/agents.md §10 Voice](docs/specs/agents.md)
- Provider errors are read aloud verbatim (one took about 20 s) — open item — [main:docs/backlogs/voice.md:362-368](docs/backlogs/voice.md)
- One TTS voice per voice-server process, set by environment, not per agent. Nothing in `agent.json` selects a voice — [main:docs/specs/voice.md §5](docs/specs/voice.md) ("Nothing in an agent prompt names an engine")

### Inferences
- The erotic agent's whisper/breath/giggle palette works only when a Grok TTS client is connected. With Kokoro, the `{#speech}` section drops out and the voice is plain.
- About 6 s from end of speech to first sound is far slower than voice-first companion apps; with no token streaming, a long reply waits for the whole generation.
- Only text crosses from the voice layer to the agent, so tone, prosody and emotion in the user's voice are lost.

### Gaps
- No latency measurement on the headset path or with Grok STT/TTS. The 5.9 s figure is Whisper/Kokoro on the host.

## 6. Embodiment: avatar, figures, poses, expressions, clips, lip sync, gaze, touch

### Takeaway
There is a strong figure system (rigged humanoids posed by name, retargeted animation clips, many with
recorded voice tracks, outfits, a semantic face/viseme vocabulary, hands driven by hand tracking), but it
is world content the builder agent manipulates. It is **not** the companion's body. The erotic agent has
none of these tools. No live lip sync from TTS, no automatic blink (except during clips), no eye contact
yet (planned), and no touch or proximity reaction reaching the LLM.

### Cited Findings
- Figure tools on the world MCP server: `inspect_figure` (height, posable bones, removable parts, morphs), `dress_figure` (hide/show `clothing`, `shoes`, `accessory`, `hair`, or `only_body`), `play_clip` / `stop_clip` / `list_clips` (captured animations, retargeted across rigs, "a clip brings its own SOUND", e.g. 20 of Barbie's 21 voiced), `list_poses`, `pose_figure` (named poses kneel/sit/crouch/wave/bow… plus per-bone `aim`/`bend`/`spread`/`turn`), `wear_hand`, `set_expression` — [main:conjure/mcp_server.py:611-1010](conjure/mcp_server.py)
- Faces: a semantic vocabulary (`smile`, `blink`, `look_left`, five visemes `aa ee ih oh ou`) resolved per expression rig (VRM, CC). "Two of 38 rigged figures can use it" — [main:conjure/expressions.py:56-145](conjure/expressions.py); [main:docs/backlogs/figures.md:130-136](docs/backlogs/figures.md); [main:docs/specs/figures.md §8d](docs/specs/figures.md)
- "**Nothing drives visemes from speech.** The five mouth shapes exist and the voice path exists; nothing connects them." Recorded lip sync exists only inside captured clips (`1_idle_speaking` puts 74.9% of its motion into `DEF-jaw_master`) — [main:docs/backlogs/figures.md:155, 240-241](docs/backlogs/figures.md)
- "Nothing blinks on its own." A jittered idle blink timer is designed but deliberately not built yet — [main:docs/backlogs/figures.md:190-207](docs/backlogs/figures.md)
- Eye contact ("aiming the eyes at the wearer") is planned in `plans/gaze-and-blink.md`: eye bones exist on every rig family, a client component would aim them at the camera after the animation mixer, clamped to about 30–35°, with head-follow and occasional looking away as open questions — [main:docs/backlogs/figures.md:209-238](docs/backlogs/figures.md)
- Not built: tier-3 world-aware posing ("sit on that chair" makes the shape of sitting but doesn't sit on the chair), FBX/Mixamo import, spring bones/MToon, facial retarget across rig naming families — [main:docs/specs/figures.md §12](docs/specs/figures.md)
- Worn hands (`hand-rig`): a hand model driven by the wearer's 25-joint hand tracking. "While worn it **is** the wearer's hand" — [main:docs/specs/figures.md §8f](docs/specs/figures.md)
- `grab` (tier-C module) lets the **user** move, rotate and resize placed objects with controllers or hands (grip, reel, sticks). "Owner-only." A headset's own writes keep the owner-only rule even when a session serves everyone — [dynamics/grab/module.json](dynamics/grab/module.json); [main:docs/specs/dynamics.md §8, §10](docs/specs/dynamics.md); decision §35 in [main:docs/decisions.md](docs/decisions.md)
- `ConjureContact.inBox(...)` tells a client module which hand bones are inside a volume. It publishes no events and puts nothing on the wire; the LLM never sees it — [main:docs/specs/dynamics.md §6 ConjureContact](docs/specs/dynamics.md)
- Other users appear to each other as a "sphere-on-box avatar" through presence — [main:client/conjure-client.js:1191, 1589](client/conjure-client.js)
- Vision items not built: "Embodiment & vehicles" (occupancy, seats, the avatar as motion model) — [main:docs/roadmap.md](docs/roadmap.md) "Embodiment & vehicles"

### Inferences
- To embody the companion today: use `agent builder visit`, place a rigged figure, pose, dress and animate it, and hand back. The erotic agent then sees the figure only as a line in `world://current` and can't move, pose or animate it. It speaks through TTS from the user's head position or near-ear, not from the figure; only a clip's own voice track is positioned on the figure ([main:conjure/mcp_server.py:709-760](conjure/mcp_server.py)).
- Everything needed to animate a companion body is in place (named poses, clips, visemes, eye bones, gaze data), but nothing ties it to the conversation: no TTS-to-viseme link, no automatic idle, no gaze tracking.

### Gaps
- Couldn't confirm from code whether any figure in the current library is suitable or intended for the companion. The figure corpus names (Barbie, Saka, Alice, Grace…) come from the backlog, not from a companion design.

## 7. Perception: what the agent can sense about the user

### Takeaway
The world server tracks each headset user's head pose and which room/floor they stand in. The agent gets
only "the user is standing in <room>" (single-user case) through `space://current`, plus head-relative
placement through the `view_relative` tool, which the erotic agent lacks. Hands, gaze target, body
proximity to objects or figures, and voice emotion are not exposed to the LLM.

### Cited Findings
- `server.gaze[user]` = `{origin, forward, right, up}` (plus an optional plane-relative head `anchor`) from each headset's `presence` messages. `server.standing[user]` = the floor surface the user is over (AR clients only) — [main:conjure/server.py:448-453, 6641-6660](conjure/server.py)
- `space://current` / `query_space` includes "the user is standing in <room> — <size>" only when exactly one user is standing somewhere — [main:conjure/mcp_server.py:321-340](conjure/mcp_server.py)
- `view_relative(direction, distance)` resolves "in front of me / to my left" from the live head pose; the erotic agent doesn't list it. A prefetched `viewer://current` resource (position + yaw on each turn) and a `near="me"` argument are backlog — [main:conjure/mcp_server.py:1179](conjure/mcp_server.py); [main:docs/backlogs/agents.md:839-847](docs/backlogs/agents.md)
- Hand tracking is used for input (synthesized pinch/grasp/poke gestures) and worn hands. Nothing is summarized to the LLM — [main:docs/specs/input.md](docs/specs/input.md); [main:docs/backlogs/dynamics.md:333-345](docs/backlogs/dynamics.md)
- Only text crosses from voice to agent (`{type:"turn", text}`); no prosody or emotion features — [docs/specs/agents.md §8.1](docs/specs/agents.md); [main:docs/specs/voice.md §1](docs/specs/voice.md)
- No camera or vision of the user. The only vision models are for asset captioning and judging (`captioner.py`, `judge.py`) — local commit 60a9732 ("a local judge and a local captioner"); [conjure/captioner.py](conjure/captioner.py)

### Inferences
- In-headset "presence" cues a companion could use (you're close, you looked away, you reached toward me) are captured server-side as pose data but never reach the LLM or trigger anything.

### Gaps
- Not checked: whether the headset client sends hand-joint data to the server at all (presence carries the head pose; no hand data seen in the `presence` handler).

## 8. Multi-user and multi-agent: sessions, guests, serves, visits

### Takeaway
There is one shared live session for everyone connected (one transcript, one floor, one active LLM).
Sessions belong to a user, are public or private, and other users can visit public ones. Since 2026-11-24
the world's agent "serves everyone present" by default, and an agent like erotic can let `builder` visit
its world to build, with a separate transcript per visit. A private erotic session keeps its dialog away
from non-owners.

### Cited Findings
- One shared reality: one global live session pointer `_session.txt`; `agent = visitor or agent_of(world.scope)`; one Director, one shared transcript, one floor for every CLI, voice and headset client. Shell mode, user and cwd are per connection — [docs/specs/agents.md §1, §8, §9](docs/specs/agents.md)
- Privacy: visibility lives on the session. A private session's dialog isn't broadcast to non-permitted clients (`_conv_broadcast` filters on `_permitted`); headsets are demoted to passthrough (`evicted`); CLI/voice clients are forced into shell mode. Erotic sessions are born `public: false` — [main:conjure/agent_server.py:159-198, 256-275](conjure/agent_server.py); [docs/specs/agents.md §9.4](docs/specs/agents.md); [erotic/agent.json](file:///Users/daniel/.config/conjure/agents/erotic/agent.json)
- Each turn is labelled with its speaker. `{user}` is this turn's speaker. `set_caller(user, scope, actor)` passes the speaker and the actor (whose rights writes use) to the MCP server — [main:conjure/director.py:509-522](conjure/director.py); [main:conjure/agent_server.py:305-320](conjure/agent_server.py) (`_actor_for`)
- `serves` (decision §35): default `"everyone"`, so the world's agent makes changes for anyone present in a public session; `"owner"` restores the strict rule; set with the `serve` verb / `POST /session/serves` (owner only). Reset, capture, realign and the world frame stay with the owner — [main:docs/decisions.md §35](docs/decisions.md); git show daf5941 (specs/agents.md §9.4)
- Visits: `agent <name> visit` brings another of the **same user's** agents into the live world if the host lists it in `visiting_agents`. The visitor's transcript and state live at `sessions/<id>/visits/<agent>/`, so a second visit resumes the first. Not persisted: a restart or any world move ends it. A visit doesn't seed, greet or change the remembered LLM. Assets the visitor uses are granted to the host's scope. "Back with the host, the host agent learns what changed by reading the world" — [main:conjure/world.py:759-783](conjure/world.py); [main:conjure/agent_server.py:298-303, 333-356](conjure/agent_server.py); daf5941 specs/agents.md §8.4a
- Cross-user visiting: `session <user> <name>` enters another user's **public** session in your active agent, as a guest — [docs/specs/agents.md §7.2](docs/specs/agents.md)
- The rejected alternative for specialist agents was delegation (`delegate(agent, request)`): "a round trip and a lossy hand-off" — [main:docs/decisions.md §35](docs/decisions.md)
- Unbuilt: per-participant access tiers, pinning while a space is held, human co-editing with consent, multi-MCP-server agents, personas — [main:docs/backlogs/agents.md:81-191](docs/backlogs/agents.md)

### Inferences
- The companion is built as a single shared conversation. In a multi-user public session, everyone present would talk to the same companion with one memory doc, which suits a holodeck but not a one-to-one relationship. The private default is what keeps it one-to-one.
- After a builder visit, the erotic agent's transcript has no record of what was said with builder, only the resulting world state.

### Gaps
- No per-user memory inside a shared session: `kinks` belongs to the session, and the schema assumes one "him".

## 9. Backlog and spec items already relevant to a companion

### Takeaway
The repo has already named most of the companion gaps: per-agent state scope (called out because of the
`kinks` doc), rolling summarization, timestamps and relative time for "a journaling companion",
`on_resume` recaps, interrupted-reply memory, eye contact and blink, visemes from speech, `viewer://current`,
an event feed to the LLM, personas. None is built.

### Cited Findings
- Per-agent (cross-session) state scope — [main:docs/backlogs/agents.md:229-252](docs/backlogs/agents.md)
- Rolling summarization / `session.json.summary` / pinned turns — [main:docs/backlogs/agents.md:195-212](docs/backlogs/agents.md)
- Timestamps with coarse relative markers, citing "a journaling companion" — [main:docs/backlogs/agents.md:214-220](docs/backlogs/agents.md)
- `on_resume` "Previously…" turn — [main:docs/backlogs/agents.md:222-224](docs/backlogs/agents.md)
- Interrupted reply remembered as heard — [main:docs/backlogs/voice.md:311-330](docs/backlogs/voice.md)
- Eye contact / gaze-and-blink plan; idle blink timer; visemes not driven from speech — [main:docs/backlogs/figures.md:190-241](docs/backlogs/figures.md)
- `viewer://current` context resource — [main:docs/backlogs/agents.md:839-847](docs/backlogs/agents.md)
- Event bus `out` feed to the LLM and an agent `module_event` tool — [main:docs/backlogs/dynamics.md:333-345](docs/backlogs/dynamics.md)
- Personas via `invoke_persona` — [main:docs/backlogs/agents.md:83-97](docs/backlogs/agents.md)
- Record/replay of a session (dialog + world event log) — [main:docs/backlogs/agents.md:849-855](docs/backlogs/agents.md)
- Before a third headset: a shared Whisper model and a TTS speech cache — [main:docs/backlogs/voice.md:335-336](docs/backlogs/voice.md)

### Inferences
- A gap analysis can mostly map competitor features onto existing backlog entries. The truly new items would be: proactive/scheduled turns, user-facing memory review and editing (beyond shell `show`/`delete` on state paths), per-agent TTS voice selection, token-streamed TTS for latency, emotion from voice, companion embodiment (the agent driving its own figure), and per-user memory in shared sessions.

### Gaps
- No product-level companion spec or plan exists in `docs/plans` or `docs/specs`. The erotic agent lives only in the user's config directory and appears in repo docs only as the motivating case in the backlog and decision §35.
