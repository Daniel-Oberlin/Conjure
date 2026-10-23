# Voice — the ears-and-mouth front-end

**Living spec.** Describes what is built and how it behaves today. Unfinished work and shelved
directions live in [`docs/backlogs/voice.md`](../backlogs/voice.md); the accuracy campaign and what it
ruled out live in [`docs/investigations/stt-accuracy.md`](../investigations/stt-accuracy.md).

---

## 1. What this layer is, and what it deliberately is not

`conjure/voice.py` is a **thin client**. It owns a microphone, a speaker, and the machinery to decide
when you have finished a sentence. It owns no conversation.

It owns one key, when either end is a cloud engine: `CONJURE_STT=grok` or `CONJURE_TTS=grok` needs
`XAI_API_KEY` here, because this process is what streams audio to the engine. That is the one exception
to "keys live with the agent server", and a narrow one — the key buys audio, never a conversation
(§4, §5).

A completed spoken turn goes over one WebSocket to the **agent server**, which owns the shell, the
shared transcript, the LLM roster, the Director, and the world-editing tools
([`agents.md`](./agents.md) §10). Replies stream back as events and are spoken. No LLM, no keys, no
command routing lives here.

The load-bearing consequence: **voice and the CLI share one conversation.** Speaking a sentence and
typing it are the same act arriving by different doors. Nothing in this file needs to know which agent
is loaded, which LLM is active, or what a world is.

Agent selection is therefore a **server-side** concern — the connection resumes your last-used agent.
There is deliberately **no `--agent` flag**: this client cannot honour one, so it could only lie. Switch
agents by voice (`conjure open shell`, then `agent <name>`).

## 2. The pipeline

Audio runs on the **host**, not through the Quest ([`decisions.md`](../decisions.md) #5 — the
shared-room-device default).

```
transport.input()      mic — LocalAudioTransport, 16 kHz in
  → vad                Silero VAD; emits the speaking frames everything downstream keys off
  → stt                the engine `CONJURE_STT` names (§4) — Whisper (segmented) or Grok (streaming)
  → aggregator.user()  end-of-turn detection + mute-while-speaking
  → bridge             VoiceBridge — submits turns to the agent server, speaks its replies
  → tts                the engine `CONJURE_TTS` names (§5) — Kokoro or Grok, 24 kHz out
  → transport.output() speaker
  → aggregator.assistant()
```

**VAD is a pipeline processor, not a transport parameter.** In pipecat 1.3.x, passing a VAD analyzer to
the transport is *silently ignored*. This has bitten us; the contract test in `tests/test_contracts.py`
exists to catch that class of drift on a dependency bump.

## 3. Voice activity detection and segmentation

```python
VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.6, min_volume=0.0)
```

`min_volume=0.0` because a desk mic peaks low — around 0.25 full-scale. Silero's confidence
discriminates speech well enough on its own, and a volume floor tuned for one mic silently deafens
another.

Segmentation is inherited from pipecat's `SegmentedSTTService`, and its shape matters:

- Audio is buffered continuously; while you are **not** speaking the buffer is trimmed to a **1-second
  pre-roll**, so the run-up to VAD's decision is not lost. First words are not clipped.
- On VAD *stop* the buffer is closed into a WAV and transcribed **whole**. This is utterance-based
  recognition, not streaming: nothing is transcribed until you stop talking.
- The WAV is then discarded.

**Two different silence thresholds are in play**, and they are not the same number:

| Threshold | Value | What it ends |
|---|---|---|
| `VADParams.stop_secs` | 0.6 s | the **audio segment** — closes the buffer, runs Whisper |
| `SpeechTimeoutUserTurnStopStrategy` | 0.8 s | the **turn** — what gets submitted to the agent server |

So a mid-sentence pause between 0.6 s and 0.8 s produces *two* Whisper calls on two fragments, which the
turn aggregator then concatenates. Whisper decodes each fragment without the other's context. Whether
this measurably hurts is **untested** — see the investigation.

## 4. Speech-to-text

**The engine is `CONJURE_STT`**, resolved by `conjure/stt.py` into a **hearing profile** — the mirror
of the speech profiles in §5, and **independent of `CONJURE_TTS`**: Whisper with Grok speech, Grok
hearing with Kokoro, either engine for both.

| `CONJURE_STT` | Engine | Shape | `--stt-corpus` | Needs |
|---|---|---|---|---|
| `whisper` (default) | faster-whisper `small.en`, local | segmented — decodes each utterance whole | yes | the `[voice]` extra |
| `grok` | xAI Grok STT over `wss://api.x.ai/v1/stt`, 16 kHz PCM, `grok-voice-transcribe-2.0` | streaming — transcripts form while you speak | **refused** | `XAI_API_KEY` |

An unknown name, Grok without its key, or `--stt-corpus` with a streaming engine is refused **at
startup** with the fix in the message. `conjure-doctor` reports the same check.

**Streaming changes what "segmentation" means.** With Grok, audio goes to the engine continuously and
the engine's own endpointing (server default 400 ms of silence) marks `speech_final`; interim results
arrive about every half second. Our VAD still runs — the turn aggregator keys off *its* speaking frames,
and that is what decides when a turn is submitted (§3's 0.8 s) — but no WAV is ever assembled, so
the corpus tap (§9) has nothing to record. The §3 description below is the **Whisper** shape.

**Grok says each utterance twice, and we pass it on once.** xAI sends every segment as a final
transcript, then at `speech_final` sends the whole utterance again — the segments joined, sometimes
re-punctuated (measured 2026-10-19). pipecat's `XAISTTService` forwards both, and the turn aggregator
concatenates everything in a turn, so every spoken line reached the agent doubled. `GrokSTT` in
`conjure/stt.py` forwards the segments as they arrive and, at the close, only words not yet sent —
normally none. It then sends an empty `finalized` frame, which adds no text but still lets the turn end
without waiting out the STT safety timeout. A close that *revises* a word already sent is dropped:
the word is already downstream. The override is pipecat's private `_push_final_transcript`, pinned in
`tests/test_contracts.py`.

Note also that `AlwaysUserMuteStrategy` mutes *transcripts* while the bot speaks, not the microphone:
with a streaming engine the bot's own speech is still streamed and billed. Earbuds remain the assumption
(§6).

### 4.1 Whisper

```python
WhisperSTTService(settings=WhisperSTTService.Settings(model="small.en", language=Language.EN))
```

**`small.en`, chosen by measurement**, not by default. The full numbers are in the investigation; the
two facts that matter here:

- The previous value, `base`, is the **multilingual** model. `language=Language.EN` constrains decoding
  but does not select the English-only weights — those are a different checkpoint. Switching cost
  nothing and cut WER from 8.3% to 2.5% on the bench corpus.
- `small.en` was also the only model tested whose decode time did **not** inflate on band-limited
  audio. faster-whisper re-decodes a segment at a higher temperature when it trips its
  confidence thresholds, so a too-small model on a poor microphone gets *slower and less accurate
  together*.

Backend is **faster-whisper** (CTranslate2). On Apple Silicon this runs on the **CPU in float32**:
CTranslate2 has no Metal backend, and the models ship as float16, so every load prints a conversion
warning and the GPU stays idle. That is the current, known state — not a target.

Whisper's own `no_speech_prob` filter (pipecat default 0.4) drops segments the model judges to be
non-speech, which is what suppresses the classic hallucinated "Thank you." on silence.

The model name is a constant (`stt.WHISPER_MODEL`), not a setting: it was chosen by measurement and
the alternatives were ruled out by the same measurement. A different Whisper backend (MLX, for the
turbo model) is a profile row in `conjure/stt.py`, not an edit to the pipeline.

## 5. Text-to-speech

Each server event becomes its own `TTSSpeakFrame`, so the early acknowledgement and the final reply
arrive as separate spoken utterances and the streaming cadence of the conversation survives into speech.

**The engine is `CONJURE_TTS`**, resolved by `conjure/tts.py` into a **speech profile** — how to build
the pipecat service, its sample rate, and what it can render:

| `CONJURE_TTS` | Engine | Speech tags | Needs |
|---|---|---|---|
| `kokoro` (default) | Kokoro, voice `af_heart`, local | none | the `[voice]` extra |
| `grok` | xAI Grok TTS over `POST https://api.x.ai/v1/tts`, PCM; voice `CONJURE_XAI_TTS_VOICE` (`eve`; also `ara`, `rex`, `sal`, `leo`) | 26, below | `XAI_API_KEY` |

An unknown name, or Grok without its key, is refused **at startup** with the fix in the message — a
pipeline that starts and then fails on the first reply hides which line was wrong. `conjure-doctor`
reports the same check.

### 5.1 Speech tags — the listener's vocabulary, registered with the server

Grok renders **speech tags** written into the text: inline tags at a point (`[laugh]`, `[pause]`,
`[sigh]`, `[giggle]`, `[breath]`, …) and wrapping tags around a span (`<whisper>…</whisper>`,
`<slow>…</slow>`, `<emphasis>…</emphasis>`, `<singing>…</singing>`, …). The full vocabulary is the
`GROK_INLINE` / `GROK_WRAPPING` tables in `conjure/tts.py`, copied from xAI's docs. Kokoro would read
the brackets aloud. So a tag is a property of the **listener**, and that is where it is declared:

1. **The profile knows its tags and the guidance for them.** `SpeechProfile.prompt` is rendered from the
   same tables the tag names come from, so the vocabulary the model is told about and the vocabulary
   the server strips cannot drift apart. The guidance is xAI's own — sparingly, with punctuation, around
   whole phrases — plus the rule that matters in a shared conversation: the reply must still read as a
   complete sentence with every tag removed, because text clients see exactly that.
2. **The voice client registers it on connect** — and on every reconnect, since the server forgets a
   connection's registration with the connection:
   ```
   client → server   {type:"register", speech:{engine:"grok", tags:[…26 names…], prompt:"…"}}
   ```
   Kokoro registers too, with no tags and an empty prompt. It is a statement of capability, not a
   request: the server decides what to do with it.
3. **The agent server composes the live registrations into the Director's `speech` slot**
   ([`agents.md`](./agents.md) §5.3), which every bundled agent frames as `{#speech}…{speech}…{/speech}`.
   Two Grok clients contribute one copy; a Kokoro client contributes nothing; with no tag-rendering
   listener connected the section drops out of the system prompt entirely, and the model is never told
   about tags it has no one to speak to. An agent switch rebuilds the Director, and the follower
   re-applies the composition after every re-bind.
4. **Replies are rendered per connection** (`_render_for`, the same fan-out seam as the speech stage in
   [`agents.md`](./agents.md) §10): a voice client keeps the tags its own engine registered and loses
   any other; the CLI, and a voice client on a tagless engine, lose them all (`speech.strip_tags`). The
   stripped set is every tag any client has registered **since the server started**, not just the ones
   live now — a Grok client that disconnects leaves tagged replies in the transcript and the model keeps
   the habit for a turn or two, and the CLI must go on reading clean sentences. Only *named* tags are
   ever touched: `[asset 3f2a]` in the builder's vocabulary is not a speech tag.

The transcript stores what the model wrote, tags and all — a rendering, not an edit, like the rest of
the speech stage. Nothing in an agent prompt names an engine: change `CONJURE_TTS` and the vocabulary,
the guidance and the rendering all follow from the one registration.

## 6. Echo, interruption, and why barge-in is off

Interruptions are **off**, and `AlwaysUserMuteStrategy` mutes the mic while the bot speaks. On an open
room microphone the TTS output would otherwise be transcribed as user speech and fed back as a turn.

The accepted cost on the host: **you cannot interrupt.** The assumption is earbuds. Room-speaker support
needs acoustic echo cancellation and a barge-in path. A headset is different: its page has the browser's
echo canceller and a button, and push-to-talk while the bot speaks *is* the interruption (§11).

## 7. The mic-activation gate

`_make_wake_gate(word)` decides whether you are addressing Conjure at all. With no word configured
every utterance passes through.

- `computer make a cat` → submits `make a cat` — word and command in one breath, then it re-waits.
- `computer` alone → **arms**; the next utterance submits in full, then it re-waits.
- anything else while unarmed → ignored.

Matching is case-insensitive on word boundaries, so `recomputed` does not trigger. Configured aliases
match too, because a gate defeated by a mis-hearing fails *silently* and that is harder to diagnose than
one that mis-fires ([`config.md`](./config.md) — `DEFAULT_VOICE_WAKE_WORDS`).

**This is a different word from the shell's, and that is enforced, not advised.** The gate consumes its
own word before anything downstream sees the line. Sharing one with the shell would make spoken shell
commands unreachable — `conjure where am I` would arrive at the shell as `where am I`, which is
content — so `wake_word_conflict` raises `SystemExit` at startup rather than warning. The two gates
compose: the mic gate opens the channel, the shell's escape decides whether what remains is a command.

## 8. Connection behaviour

- **Preflight.** `main()` probes `GET /health` and refuses to start with instructions if the agent
  server is not up, rather than failing later inside the pipeline.
- **Backlog suppressed.** The socket is opened with `backlog=False` — a voice client cannot *speak* the
  history at you on reconnect.
- **Client kind `voice`.** The server uses this to serve the modal/navigational command subset; a spoken
  directory listing helps nobody.
- **`register` first.** Immediately after connecting (every time — a reconnect is a new connection to
  the server) the client sends its speech profile's registration (§5.1), before any turn.
- **Reconnect** on any failure with a 1-second backoff, indefinitely. While disconnected, a submitted
  turn is answered by speech: *"The agent server isn't connected yet."*
- **Spoken events:** `assistant_delta`, `assistant_final`, `notice`. `busy` speaks a canned "one moment"
  line. `context`, `user_turn`, `tool_call` and `turn_done` are not spoken.
- **Idle timeout** is one hour, and does not cancel the pipeline — silence is not a failure.

## 9. The STT corpus (`--stt-corpus`)

Off by default. When on, every recognized utterance is written to `temp/stt-corpus/` (gitignored) so
models can be compared offline on **real** audio — the synthetic bench that chose `small.en` cannot
represent a Bluetooth microphone, and is too small to separate the top candidates
([the investigation](../investigations/stt-accuracy.md)).

```
temp/stt-corpus/
  clips/20260830-142305-118.wav   audio        — machine
  manifest.jsonl                  capture log  — machine, append-only
  truth.tsv                       the labels   — HUMAN, hand-edited
```

**The tap is `run_stt`**, subclassed in the Whisper profile (`conjure/stt.py`): it receives the exact WAV the recognizer saw and
yields the text it produced, so the pair is captured at the one point where both exist. Recording runs
off-thread and swallows its own errors — a diagnostic that can break the thing it diagnoses is worse
than none.

**Format follows the writer.** JSONL for the manifest (machine-written: escapes commas and quotes,
gains fields without rewriting old rows); TSV for truth (hand-edited: speech has no tabs, so it
survives `vim` as well as a spreadsheet). Scoring results live in neither — they are derived and
regenerable, and accumulating them would mutate the record that is meant to hold still.

**`truth` is prefilled with the hypothesis**, so labelling is scan-and-fix. The `reviewed` column is
load-bearing, not bookkeeping: with two columns, `truth == hypothesis` means either *verified* or
*never looked at*, and an unreviewed prefill scores its own source model at 0% WER — so abandoning a
labelling pass halfway would make the measurement **invert** rather than degrade. Anything unrecognised
in that column counts as not reviewed.

**One condition tag, auto-captured.** The PortAudio input device name, read once at startup — an open
stream stays on the device it opened with, so per-clip re-reads would disagree with the audio they
describe. Utterance length is derived at scoring time from `duration_s`. Environment is deliberately
not recorded (see the backlog for the confound this accepts).

### Privacy gate

Capture requires the live session to be **public**, and the client learns this from the `public` field
on the `context` event. Session privacy is otherwise enforced by *omission* — `_conv_broadcast` skips
non-permitted clients — which the **owner never observes**, and the owner is exactly who needs to know
here.

Unknown state does not record. Before the first `context` event, and after a dropped connection, the
gate is closed: a governed recorder must never be running when you assumed it wasn't. Every transition
prints, since a recorder that stops silently is as bad as one that starts silently.

### Purge

`--stt-corpus-purge` deletes **unreviewed clips and their labels together**, then exits. A clip and its
label are one unit: a transcript with no audio cannot be run against a model, so labels never outlive
their clips, and a label whose clip is already gone is reported as a defect rather than kept.

The asymmetry is labelled-versus-unlabelled, not audio-versus-labels. Unreviewed clips are cheap,
plentiful, and what fills the disk. `--all` additionally removes reviewed pairs and prompts for a typed
confirmation first — that is the operation that destroys work.

## 10. Testing

The wake gate is pure and fully unit-tested (`tests/test_voice.py`), including that the two gates
compose and that a colliding word is refused. The corpus is likewise pure file behaviour and tested
without pipecat or a microphone (`tests/test_stt_corpus.py`) — including that purge takes pairs, that
an unknown `reviewed` marker reads as *not* reviewed, and that tabs in a hypothesis cannot break the
TSV.

The speech and hearing profiles are pure and tested without pipecat (`tests/test_tts.py`,
`tests/test_stt.py`): the engines, the startup refusals, that the ears and the mouth resolve
independently in every pairing, that the corpus is refused for a streaming recognizer, and that Grok's
TTS registration carries every tag its prompt mentions. `tests/test_stt.py` also replays the event
sequences xAI actually sent (§4) through `Utterance` and through the real `GrokSTT`, asserting each
utterance reaches the aggregator once, followed by an empty `finalized` frame. The stripper is
tested in `tests/test_speech.py`; the registration protocol, the per-connection rendering and the
re-bind survival in `tests/test_agent_server.py`; the `{speech}` injection in `tests/test_director.py`.

The real-time pipeline is not unit-tested. The **Tier-2 contract test** asserts the pipecat classes we
construct still have the shapes we call ([`testing.md`](../testing.md)) — including that
`WhisperSTTService.run_stt` is still an async generator taking `audio`, since `--stt-corpus` taps it
and drift there would stop recording *silently*, and that `XAIHttpTTSService` / `XAISTTService` still
take `api_key` and `settings=` — and that `XAISTTService._push_final_transcript`, the private hook
`GrokSTT` overrides, still takes the arguments it passes.

Accuracy itself is still covered by no automated test; that is what a labelled corpus exists to make
possible.

## 11. The headset link — audio to and from the Quest

The investigation that chose this shape is `temp/plans/quest-audio-streaming-2026-10-18.md`; decision
#33 records the choice. What is built:

```
 Quest page (client/voice-link.js)                 world server            voice server
 mic → AudioWorklet → Int16 PCM ──┐                                       conjure.voice --serve
 Web Audio ← AudioWorklet ← PCM ←─┼── /audio?user= ──▶ relay ──▶ /audio ──▶ one pipeline per
 ptt / speaking / transcript      │   (wss, same origin)   (voice_url)      connection, as that user
```

**A headset is one more voice client.** `voice-link.js` captures the microphone in an AudioWorklet,
sends Int16 PCM over a WebSocket to the world server's `/audio` relay, and plays the reply's PCM through a
second worklet. The relay (world server, `audio_relay`) pumps bytes to the voice server named by
`voice_url` — stateless, so the world server stays ML-free and off the audio path except as a pipe —
and the voice server runs **one `run_pipeline` per connection**, acting as the page's `?user=`. That
pipeline is the §2 chain on a different transport (`conjure/transport.py`, the third profile registry):
its bridge opens its own agent-server socket, registers its own TTS engine (§5.1), hears its own
rendering of every reply, and gates its own corpus capture. Nothing on the agent server changed.

**Why a WebSocket and not WebRTC.** ICE is UDP; a headset over `adb reverse` or a cloudflared tunnel
cannot reach it. Raw PCM on the socket the page already has works on every serving tier and costs
~0.4 Mb/s each way on a LAN. WebRTC is the third transport profile when a remote participant needs it.

**The wire** is documented in `conjure/transport.py`: `hello {user, in_rate}` first, then binary
frames at the browser's own rate (48 kHz on the Quest); the server resamples to the pipeline's rate in
the serializer (pipecat's input transport does not; SOXR `HQ`, measured 21 ms of latency against VHQ's
37). Back come `ready {out_rate}`, binary PCM paced to real time, and `speaking {on}`, `transcript
{text}` (what STT heard — the on-headset diagnostic for every mis-hearing), `notice {text}`.

**Permission and the gesture.** An AudioContext created outside a user gesture starts suspended, and
`getUserMedia` wants one too, so `voice-link` takes both on the ENTER AR tap (a capture-phase click
listener, before `enterVR`). That is enough **only once the site already holds the permission.** On a
site the headset hasn't granted, the prompt opens on the same tap that starts the session and waits
behind it, unseen: the request neither resolves nor fails, and the link never opens (first headset run,
2026-10-20). A cloudflared quick tunnel is a new site every run, so it asks every run; `adb reverse` (a
stable `localhost`) asks once. **So while the permission is still `prompt`, the ENTER tap is held:**
`voice-link` reads the state through the Permissions API, and a click on anything that starts a session
(`[data-xr-enter]` — conjure-client's ENTER AR carries it — or A-Frame's enter buttons) is stopped before
it reaches the button. The prompt then opens on the 2D page, a hint beside the button says "Allow the
microphone, then tap ENTER AR again", and the next tap — permission granted — enters with the link
already open. Granted, denied, or no Permissions API: the tap passes as before. The page logs the state
(`microphone permission: prompt`), the hold, and — for the unknown-state case — a request unanswered
after 5 s. Capture asks for
`echoCancellation: true, noiseSuppression: false, autoGainControl: false` — the backlog's "NR off,
AEC on": Chromium's canceller uses audio *this browser renders* as its reference, which is why the
reply is played by the page and not positionally.

**Activation is the input layer's.** `talk` is an action in `CONJURE_BINDINGS` (default `stickPress`)
and `CONJURE_VOICE_ACTIVATION` picks how the mic reaches the server:

| `voice_activation` | Behaviour |
|---|---|
| `ptt` (default — decision #5's shared mode) | the worklet captures continuously (AEC stays converged) but frames are **sent** only while `talk` is held, then 350 ms more of the mic, then **digital silence until 1 s after release**. The silence is load-bearing: both recognizers end a sentence on silence they *hear* (xAI after ~400 ms, our VAD after 0.6 s), and audio that just stops isn't silence — with only the 350 ms tail, a sentence released on its last word was held until the next press, then arrived glued to it or drew a `busy` (measured 2026-10-20/22). Zeros rather than the mic, so nothing said after letting go is sent. Nothing is streamed while nobody is pressing — which also makes the streaming-STT billing note in §4 moot |
| `open` | no stick needed: the mic streams for the whole AR session (nothing out of AR — streaming STT bills by the second). The **echo gate** sends digital silence while this page's reply is playing and for 300 ms (`ECHO_MS`) after: the server's mute-while-speaking ends when it has *sent* the reply, but the headset is still playing it (priming buffer, network, output latency), and that tail came back as a turn. The page's playback worklet reports `on`/`off`, so the gate follows what is actually audible and does not depend on AEC quality. `talk` stays as the barge-in button — held, it also lifts the gate so you are heard over the reply. Solo mode: everything said is a turn unless the voice server runs with a wake word (§7) |
| `off` | no microphone; the component is inert |

**The capture reports its own health, and repairs a dead one.** Each press logs its length and the peak
level of the mic audio actually sent (`press: 26 frames over 516 ms, peak -12 dBFS`); the mic track's
`mute`/`unmute`/`ended` and the AudioContext's state changes are logged; entering AR logs the context and
track state. A press that carried **nothing but digital zeros**, an ended track, or a track that isn't live
on entering AR triggers a fresh `getUserMedia` on the same constraints (permission already granted: no
prompt, no gesture) and re-attaches it to the capture worklet. Under push-to-talk an ended track is only
re-acquired in AR: out of AR the Quest ends the track, and ends a re-acquired one again every ~15 s, so
there the page waits for entering AR or the next press (whose first moments are lost to the re-acquire). Motivation: after leaving and re-entering AR
on the same page, the page kept sending frames while the voice server's VAD never fired once — almost
certainly a capture delivering silence (2026-10-22). Verified in headless Chrome by disabling the track:
the silent press is reported, the mic re-acquired, and the next press carries sound. Not yet confirmed to
be the Quest's failure mode — the press levels are what will say.

**Barge-in (C3b) is a gesture, not an acoustic guess.** Pressing `talk` while the bot is speaking flushes
the playback ring buffer and sends `{type:"interrupt"}`; the bridge pushes an `InterruptionFrame`
(TTS and transport flush what is queued) and forwards the reserved `interrupt` to the agent server,
which cancels that connection's in-flight turn, broadcasts *[user interrupted]* and still fires one
`turn_done`. Deliberate interruption needs no echo-cancellation quality at all, and works the same in
`open` mode. Interrupting by *voice alone* (no press) waits on spike 1's measurement: the echo gate
silences the mic while the reply plays, so today a spoken interruption is simply not heard.

**Reply audio already in flight is dropped too.** Frames the server sent before the interrupt reached it
land after the page's flush — too few to fill the 120 ms priming buffer, so they used to wait there and
play just before the *new* reply. After a barge-in the page discards all reply audio until that press's
`transcript` arrives (it precedes the new reply's audio on the same socket), or 2 s after release if no
transcript comes; it logs `barge-in: dropped N late frames of the interrupted reply`.

**The page has to know the bot is speaking**, because that is what makes a press a barge-in rather than
a new turn. Two signals, either sufficient: the server's `{type:"speaking", on}`, and reply audio received
in the last 600 ms (the server reports *stopped* for ~0.7 s between two TTS sentences). `speaking` is sent
by a processor placed **immediately before the output transport** — the processor that pushes the
`Bot*SpeakingFrame`s. It used to be read after the recognizer, from the upstream copies, and those never
climbed past the TTS / bridge / aggregator: the page never got `on: true`, never sent `interrupt`, and the
words spoken over the reply were dropped by mute-while-speaking (first headset run, 2026-10-20; reproduced
and verified with a scripted stand-in: interrupt → zero further reply frames, the new line answered).
`tests/test_voice.py` pins both the messages and the placement.

**Corpus from the headset.** With `--stt-corpus`, a headset's clips are tagged `browser:<user>` as
their condition — the thing the corpus exists to compare — in place of the host's PortAudio device,
which heard none of it.

**Multi-headset.** N connections, N pipelines, N agent-server connections, one floor; every reply through
every headset, near-ear. Two costs to pay before the third headset: a shared Whisper model across
pipelines, and a speech cache so a reply is synthesised once (plan §2.3).

### 11.1 What the spikes measured (2026-10-18)

Spike 2/3 ran on the host with `scripts/audio_probe.py` standing in for the browser (a corpus clip,
resampled to 48 kHz, streamed in 20 ms frames through the world server's relay to a headless voice
server, `conjure.voice --serve --no-mic`):

| Stage | Measured |
|---|---|
| connect → `ready` | 0.83 s (pipeline + Whisper start) |
| speech end → `transcript` | 2.4 s — VAD stop 0.6 + turn 0.8 + Whisper ≈ 0.9 |
| `transcript` → first reply audio | 3.5 s — the LLM turn and the first TTS utterance |
| **speech end → first reply audio** | **5.9 s**, of which the transport's share is tens of milliseconds |
| heard | *"What worlds do I have?"* — the clip's label, verbatim |
| reply (4.9 s, transcribed back through Grok STT) | *"Let me check for you. You just have the one world right now default, and that's the one you're in."* |

Spike 1 — mic capture inside `immersive-ar` and how much AEC3 cancels on the Quest's open-air speakers
— needs the headset: open `/static/audio-spike.html` on it, tap **1**, then **2**, **3**, **4**; every
result line lands in `temp/conjure.log` under `audio-spike`. Until it has run, VAD barge-in stays off.
`open` no longer waits on it (the echo gate doesn't rely on AEC); `ptt` stays the default because
decision #5 makes shared mode the safe default, and `open` is how a solo user declares otherwise. Spike 3's render-loop check (`--debug-jitter` with the link up) and spike 4's
push-to-talk on a real controller are likewise waiting on a headset session.

## Related

- [`docs/backlogs/voice.md`](../backlogs/voice.md) — the model/transport work not yet done.
- [`docs/investigations/stt-accuracy.md`](../investigations/stt-accuracy.md) — measurements, and the
  options already ruled out.
- [`docs/specs/agents.md`](./agents.md) §10 — the agent-server protocol this client speaks.
- [`docs/providers.md`](../providers.md) — the STT/TTS provider registry and future options.
