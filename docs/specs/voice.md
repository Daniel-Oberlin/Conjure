# Voice — the ears-and-mouth front-end

**Living spec.** Describes what is built and how it behaves today. Unfinished work and shelved
directions live in [`docs/backlogs/voice.md`](../backlogs/voice.md); the accuracy campaign and what it
ruled out live in [`docs/investigations/stt-accuracy.md`](../investigations/stt-accuracy.md).

---

## 1. What this layer is, and what it deliberately is not

`conjure/voice.py` is a **thin client**. It owns a microphone, a speaker, and the machinery to decide
when you have finished a sentence. It owns no conversation.

A completed spoken turn goes over one WebSocket to the **agent server**, which owns the shell, the
shared transcript, the LLM roster, the Director, and the world-editing tools
([`agents.md`](./agents.md) §10). Replies stream back as events and are spoken. No LLM, no keys, no
command routing lives here.

The load-bearing consequence: **voice and the CLI share one conversation.** Speaking a sentence and
typing it are the same act arriving by different doors. Nothing in this file needs to know which agent
is loaded, which LLM is active, or what a world is.

Agent selection is therefore a **server-side** concern — the connection resumes your last-used agent.
(`--agent` is accepted on the command line and currently does nothing; see the backlog.)

## 2. The pipeline

Audio runs on the **host**, not through the Quest ([`decisions.md`](../decisions.md) #5 — the
shared-room-device default).

```
transport.input()      mic — LocalAudioTransport, 16 kHz in
  → vad                Silero VAD; emits the speaking frames everything downstream keys off
  → stt                Whisper (faster-whisper), utterance-segmented
  → aggregator.user()  end-of-turn detection + mute-while-speaking
  → bridge             VoiceBridge — submits turns to the agent server, speaks its replies
  → tts                Kokoro, 24 kHz out (its native rate)
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

The model is **hardcoded**. `Settings.stt` / `CONJURE_STT` exists in
[`config.md`](./config.md) and is read by `conjure/doctor.py`, but `voice.py` does not consult it —
selecting a different STT backend today means editing this line.

## 5. Text-to-speech

Kokoro, voice `af_heart`, 24 kHz. Each server event becomes its own `TTSSpeakFrame`, so the early
acknowledgement and the final reply arrive as separate spoken utterances and the streaming cadence of
the conversation survives into speech.

## 6. Echo, interruption, and why barge-in is off

Interruptions are **off**, and `AlwaysUserMuteStrategy` mutes the mic while the bot speaks. On an open
room microphone the TTS output would otherwise be transcribed as user speech and fed back as a turn.

The accepted cost: **you cannot interrupt.** The assumption is earbuds. Room-speaker support needs
acoustic echo cancellation and a barge-in path, and is not built.

## 7. The mic-activation gate

`_make_wake_gate(word)` decides whether you are addressing Conjure at all. With no word configured
every utterance passes through.

- `computer make a cat` → submits `make a cat` — word and command in one breath, then it re-waits.
- `computer` alone → **arms**; the next utterance submits in full, then it re-waits.
- anything else while unarmed → ignored.

Matching is case-insensitive on word boundaries, so `recomputed` does not trigger. Configured aliases
match too, because a gate defeated by a mis-hearing fails *silently* and that is harder to diagnose than
one that mis-fires ([`config.md`](./config.md) — `DEFAULT_WAKE_WORDS`).

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
- **Reconnect** on any failure with a 1-second backoff, indefinitely. While disconnected, a submitted
  turn is answered by speech: *"The agent server isn't connected yet."*
- **Spoken events:** `assistant_delta`, `assistant_final`, `notice`. `busy` speaks a canned "one moment"
  line. `context`, `user_turn`, `tool_call` and `turn_done` are not spoken.
- **Idle timeout** is one hour, and does not cancel the pipeline — silence is not a failure.

## 9. Testing

The wake gate is pure and fully unit-tested (`tests/test_voice.py`), including that the two gates
compose and that a colliding word is refused. The real-time pipeline is not unit-tested; the
**Tier-2 contract test** asserts the pipecat classes we construct still have the shapes we call
([`testing.md`](../testing.md)). Accuracy itself is not covered by any automated test today — that gap
is what the capture-and-evaluate work in the backlog exists to close.

## Related

- [`docs/backlogs/voice.md`](../backlogs/voice.md) — the model/transport work not yet done.
- [`docs/investigations/stt-accuracy.md`](../investigations/stt-accuracy.md) — measurements, and the
  options already ruled out.
- [`docs/specs/agents.md`](./agents.md) §10 — the agent-server protocol this client speaks.
- [`docs/providers.md`](../providers.md) — the STT/TTS provider registry and future options.
