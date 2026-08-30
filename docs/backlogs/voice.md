# Voice — backlog

Unfinished work and future directions for the voice front-end. What runs today is
[`docs/specs/voice.md`](../specs/voice.md); what has already been measured and ruled out is
[`docs/investigations/stt-accuracy.md`](../investigations/stt-accuracy.md).

**Read the investigation before proposing an STT change.** Several obvious-looking upgrades are already
eliminated by measurement.

---

## The problem this backlog is organised around

Transcription is *decent* and not better than decent. Words come back wrong often enough to notice, and
until recently we could not say whether that was the recognizer or the microphone.

The forcing constraint is **portability**. The MacBook's built-in microphone is the best input in the
house and it is not a usable answer: the target is a headset worn while walking around a room, so the
microphone will be a Bluetooth earbud, a Bluetooth speakerphone, or eventually the Quest itself.
Anything that only works while sitting at the laptop solves the wrong problem.

## Target use cases

Ordered by how much they constrain the design, not by when they arrive.

| # | Case | Input | Status |
|---|---|---|---|
| 1 | **Bluetooth earbuds** (AirPods Pro) while moving around the room | HFP voice path — band-limited, denoised in the earbud, unreachable DSP | today's primary |
| 2 | **Bluetooth speakerphone**, multi-mic, hands-free at desk or table | HFP voice path; device-side beamforming + AGC | today, secondary |
| 3 | **Built-in Mac microphone** | 48 kHz wideband, minimally processed | dev and **bench reference** only |
| 4 | **Quest audio streaming** — capture in the headset browser, stream to the host | WiFi, Opus, wideband; browser DSP is *reachable* | wanted, not built |
| 5 | **2.4 GHz USB wireless microphone** (lav-style receiver) | USB audio class, wideband, light processing | untried; would satisfy portability without Bluetooth |

All five must keep working. Case 4 is the one worth designing toward rather than merely allowing,
because it is the only one where the input is likely to get *better* rather than worse — see below.

### Why case 4 is an upgrade, not another compromise

Quest capture never touches the Bluetooth voice path. It is wideband audio streamed digitally over
WiFi, and — unlike cases 1 and 2, where the noise suppression lives inside the earbud and cannot be
switched off — the browser's processing is reachable through capture constraints
(`echoCancellation`, `noiseSuppression`, `autoGainControl`). We can hand the recognizer raw wideband
audio and let the model do the denoising it was trained to do.

**The tension to resolve before building it:** if input moves to the Quest, output likely follows, and
Quest speakers are open-air. That requires real echo cancellation — the same class of processing we
would be switching off. Current thinking is *NR off, AEC on*, since AEC is far more surgical than
broadband noise suppression, but this is undecided and belongs in [`decisions.md`](../decisions.md)
when it is settled.

---

## Landed

**`base` → `small.en`.** Measured, not guessed: WER 8.3% → 2.5% on the bench corpus, at a cost of
roughly 0.9 s per utterance on CPU. The old value was the *multilingual* `base` while the pipeline was
passing `language=EN`, which constrains decoding without buying the English-only weights. Details and
the full table are in the investigation.

**`--agent` removed.** The flag was parsed, passed into `_run`, and never read, while `--help`
advertised it as selecting the agent. Voice is a thin client and the agent server owns which agent is
open, so the flag could not have been honoured without a `/scope/activate` on connect — and agent
switching already works by voice. Dropped rather than wired; the spec now records the absence and why.

---

## Next: the capture-and-evaluate framework

This is the highest-value remaining work, because **every subsequent decision is blocked on it.** The
synthetic bench corpus cannot separate `small.en` from `large-v3-turbo` — the difference is smaller
than its sampling noise — and it cannot represent a Bluetooth microphone at all.

### What a corpus needs

Three things per clip. The audio is the easy part; the other two are what make it an asset.

1. **Audio.** `SegmentedSTTService` already assembles a complete WAV per utterance and discards it.
   That is the tap point. One WAV per VAD segment.
2. **Truth** — what was actually said. Nobody can produce this but the user.
3. **Condition tags** — device, environment, utterance type. *Painful to retrofit*, so they must be
   captured at record time, not reconstructed later.

A sidecar record per clip carries the hypothesis, the decode time, the model, and the tags. A directory
of WAVs plus a JSONL manifest; nothing more elaborate.

### Making labelling affordable

Transcribing hundreds of commands by hand is the chore that kills this kind of effort. Two mitigations:

- **Bootstrap the labels** — run the largest model offline with no latency pressure and correct its
  output. Correcting beats typing. **Caveat that must be honoured:** a clip whose draft was accepted
  without listening scores *that model* unfairly well. Bootstrapped labels are fine for ranking other
  models and not fine for grading the model that wrote them.
- **Label only disagreements** — run several models over every clip and hand-check only where they
  differ, typically 10–20% of clips. Where they all agree the answer is nearly always right.

### How much is needed

| Goal | Size |
|---|---|
| Confirm a large gap (e.g. `base` vs `small.en` — 10 errors vs 3) | ~50 clips |
| Separate `small.en` from `large-v3-turbo` (under one WER point) | **1,000–2,000 reference words**, ~150–300 short commands |

The second is a few sessions of ordinary use with capture enabled. It is collected passively; there is
no sit-down-and-record step.

### What it buys

1. **Picks the model** on real audio instead of synthesized speech.
2. **Settles mic-versus-model** — with condition tags, WER becomes a table (models down, devices
   across). Reading down a column gives the value of a bigger model; reading across a row gives the cost
   of a worse microphone. No other artifact answers this.
3. **Diagnoses fragmentation for free** — one WAV per VAD segment means clips that end mid-sentence are
   directly visible. The `stop_secs` 0.6 / turn-timeout 0.8 gap (spec §3) needs no separate
   instrumentation.
4. **Becomes a regression suite** — the durable payoff. Every later change is scored against the same
   audio. Without it, every tuning decision after this one is recency bias.

### Metrics: WER is the diagnostic, not the headline

WER weights every word equally, so it scores a dropped "the" like a mis-heard "beagle". Two metrics fit
this system better:

- **Keyword error rate** over the vocabulary that actually matters — the wake word, object nouns, agent
  names, space names.
- **Command success rate** — did the utterance produce the right action? This is what correlates with
  the experience of using the system. A transcript can be 90% accurate and fail the command, or sloppy
  and work fine, because the Director is an LLM and absorbs a lot of slop. It is also *cheaper to
  label*: one keystroke versus a transcription.

Track command success as the headline; consult WER when it drops.

### Capture must be governed

This records the user's voice, continuously, at home — including everything said to private agents.
Non-negotiable properties, because an ungoverned local recording is not obviously better than the cloud
STT that was rejected on privacy grounds:

- **Off by default**, behind an explicit flag. Not leavable-on by accident.
- **Written outside the repo and gitignored** — the same care as `docs/private-notes.md`.
- **Disabled for private sessions** by default.
- A retention story and a **one-command purge**.

---

## MLX: the only route to turbo-class quality

`large-v3-turbo` is the accuracy target and measured **0.9× realtime on CPU** — transcription taking
longer than the speech did. Unusable. The cause is visible in every model-load log line: CTranslate2 has
no Metal backend, models ship float16, and the weights are upconverted to float32 on CPU cores while the
GPU idles.

MLX runs the same model on the GPU in its native precision. Pipecat already ships
`WhisperSTTServiceMLX` (`pipecat/services/whisper/stt.py`) with `LARGE_V3_TURBO` in its model enum, and
`mlx-whisper` resolves to five packages with no conflicts in the current venv.

**Open question, and it is a latency question, not an accuracy one:** does turbo-on-GPU fit the
interactive budget? That measurement does not need a corpus and could be done now. Prefer fp16 turbo
over the Q4 quantization — quantization loss lands hardest on exactly the degraded audio these use cases
are made of.

Doing this would also make [`providers.md`](../providers.md)'s existing "MLX / faster-whisper" claim
true; today it describes an intention the code never picked up.

## The transport seam

`voice.py` hardcodes `LocalAudioTransport` and `WhisperSTTService`. Neither is swappable, and
`Settings.stt` / `CONJURE_STT` is declared in [`config.md`](../specs/config.md) and read by
`doctor.py` but **never consulted by `voice.py`** — a seam declared in one file and ignored in another.

Two seams are wanted:

- **Transport** — local microphone today, Quest audio later, same pipeline downstream. This is the one
  that is expensive to retrofit and cheap to allow for now.
- **STT backend** — honour `CONJURE_STT`, so faster-whisper vs MLX is a config line rather than an edit.

Cheaper than it first appeared: pipecat already ships `SmallWebRTCTransport`, an **`aiortc`-based,
self-hosted** WebRTC peer. The Quest browser is one end, the host is the other, and no cloud service is
in the path — which is what makes it compatible with the local-only constraint. `aiortc` is not
currently installed.

If the seam is built, design it against **two real implementations**. A seam designed around one real
client and one imagined client is usually wrong for the imagined one.

## Smaller open items

- **Decoder vocabulary.** `faster_whisper.transcribe` accepts `initial_prompt` and `hotwords`; pipecat's
  call site passes neither. A **static** list of the application's own vocabulary — "conjure",
  "skybox", "billboard", "annotations" — would bias the decoder toward words it has weak priors for.
  `DEFAULT_WAKE_WORDS` (eight spellings of one word, amended by observation) is the hand-maintained
  downstream patch for exactly this. Requires subclassing, since the parameter is not exposed.
  *Scope note:* this is a fixed decoder prior, distinct from the runtime scene-graph repair rejected in
  the investigation.
- **Double VAD.** `faster_whisper.transcribe` defaults to `vad_filter=True`, so audio already accepted
  by pipecat's Silero is trimmed by a *second* Silero pass at different parameters. Interacts badly with
  a quiet microphone. Unmeasured.
- **Input device is unpinned.** `input_device_index=None` follows the system default, and virtual
  devices (NoMachine, Teams) are present on the dev machine. If one became the default input, quality
  would degrade silently with no message anywhere. At minimum, name the chosen device at startup.
- **Barge-in** — blocked on echo cancellation; see spec §6 and the Quest tension above.

## Related

- [`docs/investigations/stt-accuracy.md`](../investigations/stt-accuracy.md) — **read first.**
- [`docs/providers.md`](../providers.md) — STT/TTS options per slot.
- [`docs/specs/config.md`](../specs/config.md) — where `CONJURE_STT` and the wake-word lists live.
