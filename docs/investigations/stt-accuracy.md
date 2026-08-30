# Speech-to-text accuracy

The campaign to work out **why transcription is only "decent"** — and, first, whether the fault is the
recognizer or the microphone.

Current pipeline: [`docs/specs/voice.md`](../specs/voice.md). Planned work:
[`docs/backlogs/voice.md`](../backlogs/voice.md).

**Status:** one fix shipped (`base` → `small.en`, measured). The mic-versus-model question is
**partially answered and still open** — the evidence so far points at the *model*, but the only corpus
that exists is synthetic and cannot represent a Bluetooth microphone.

---

## 1. Symptom

In the observer's words:

> I've always been slightly unsatisfied with the quality of STT. Sometimes it just gets words wrong,
> and I'm not sure if it is a limitation of the algorithm itself or if the software can't hear my voice
> clearly enough. I've experienced this with AirPods Pro (do they do noise reduction?) and with a
> Bluetooth speakerphone that has 4 microphones and noise reduction.

Conditions: it *works pretty decently already* — this is a quality campaign, not a bug hunt. Failures
are individual wrong words, not garbled output. Observed across two different Bluetooth input devices.

A standing constraint shapes every option: **the Mac's built-in microphone is not an acceptable
answer.** The system is used while walking around a room wearing a headset, so the input will be
Bluetooth, or eventually the Quest. Cloud STT is also ruled out (see §3).

## 2. Experiments and what each proved

### 2.1 Read the pipeline — the model was never chosen

`git log -L164,164:conjure/voice.py` returns exactly one commit: `fa08165`, the original Phase-2 voice
loop. The model string had never been revisited.

Three defects in one line, all of which held up:

- `base` is the **second-smallest** of eight Whisper models.
- It is the **multilingual** checkpoint. The pipeline passes `language=Language.EN`, which constrains
  decoding but does *not* select the English-only weights — a separate checkpoint.
- It is **below pipecat's own default** (`DISTIL_MEDIUM_EN`, `stt.py:257`). We were explicitly
  overriding the library downward.

**Proved:** the model was a day-one placeholder, not a decision. Any acoustic theory had to be tested
against a baseline that was already handicapped.

### 2.2 Bench: five models × two audio conditions

Corpus synthesized with macOS `say` (2 voices × 6 Conjure-vocabulary commands = 12 clips, 35.8 s,
120 reference words), so the experiment is repeatable without a microphone. Each clip run twice: clean
16 kHz, and a **narrowband** version simulating the Bluetooth HFP voice path (300–3400 Hz band,
decimated to 8 kHz, restored to 16 kHz). faster-whisper on CPU, M2 Max.

| model | clean WER | narrowband WER | s/clip | ×realtime |
|---|---|---|---|---|
| `base` (multilingual) — *was shipping* | 8.3% | 8.3% | 0.47 | 6.3 |
| `base.en` | 3.3% | 5.0% | 0.47 / 1.30 | 6.4 |
| `small.en` — **now shipping** | **2.5%** | **2.5%** | 1.40 | 2.1 |
| `distil-medium.en` (*pipecat's default*) | 5.0% | 5.8% | 1.83 | 1.6 |
| `large-v3-turbo` | 1.7% | 0.8% | 3.35 | 0.9 |

Reproduce with **`python scripts/stt_bench.py`** (`--models` to pick a subset; clips are cached under
`temp/stt-bench/`). Kept because it is currently the only re-runnable measurement in this campaign —
the real-audio scorer does not exist yet.

**Re-run 2026-08-30 reproduced the table to within one word**, with one entry moving: `base` narrowband
came back **9.2%** rather than 8.3%. The clips are re-synthesized per fresh checkout, and one word is
0.8 points on a 120-word corpus — so this is the noise floor demonstrating itself, not a contradiction.
It is also the reason the script now prints that floor in its own header.

**Proved:**

- **`base` is clearly the worst** — 10 errors against 2–4 for everything else. This is the one gap in
  the table larger than the noise, and it is the fix that shipped.
- **English-only weights are free.** `base` → `base.en` is identical in size and speed (0.47 s/clip
  both) and cuts errors by 60%.
- **`large-v3-turbo` is unusable on CPU** at 0.9× realtime — transcription takes longer than the
  speech did.
- **`small.en` is uniquely stable under degradation.** Every other model either lost accuracy or lost
  speed on narrowband audio; `small.en` lost neither (2.5% / 2.5%, 1.40 s both).

### 2.3 The `base.en` timing split — temperature fallback is a confidence signal

`base.en` took 0.47 s on clean audio and **1.30 s on narrowband** — same audio, same length. That is
faster-whisper's temperature-fallback path: a segment that trips the compression-ratio or log-probability
threshold is re-decoded at a higher temperature.

**Proved:** a too-small model on a poor microphone degrades on *both* axes at once — slower and less
accurate together. It also means decode-time inflation is a usable low-confidence signal we are not
currently reading. `small.en` showed no such split, which is a second, independent reason to prefer it.

### 2.4 The GPU is idle — CTranslate2 has no Metal backend

Every model load printed:

```
[ctranslate2] the compute type inferred from the saved model is float16, but the target device or
backend do not support efficient float16 computation. The model weights have been automatically
converted to use the float32 compute type instead.
```

**Proved, not inferred:** models ship float16, CTranslate2 runs CPU-only on macOS, weights are
upconverted to float32, and the M2 Max GPU contributes nothing. This is why `small.en` costs 3× `base.en`
for 0.8 WER points, and it is the entire argument for the MLX backend in the backlog.

### 2.5 Dependency reconnaissance (no installs)

By `pip install --dry-run`:

- `mlx-whisper` → 5 packages, **no conflicts** with the current venv.
- `parakeet-mlx` → 14 packages (pulls librosa, scikit-learn, soundfile).
- Pipecat already ships `WhisperSTTServiceMLX` (`stt.py:389`) with `LARGE_V3_TURBO` in its enum.
- Pipecat already ships `SmallWebRTCTransport`, **`aiortc`-based and self-hosted** — the Quest audio
  path needs no cloud service. `aiortc` is not installed.

**Proved:** both the MLX route and the Quest transport are dependency-available and compatible with the
local-only constraint. Neither needs new protocol work.

## 3. Tried and rejected

Written for someone about to propose exactly these. Each says what would change the verdict.

### 3.1 `distil-medium.en` — rejected by measurement

The obvious "distilled = fast and accurate" pick, and **pipecat's own default**. Measured at **5.0% WER
and 1.83 s/clip**: half as accurate as `small.en` *and* 30% slower. Distillation is not automatically a
win, and the label promised more than the measurement delivered.

*Would reconsider if:* a real-audio corpus reversed the ordering — this was 120 synthetic words, and the
ranking of the middle of the table is the part least likely to survive contact with real speech.

### 3.2 `large-v3-turbo` on CPU — rejected on latency

1.7% WER, the best measured, at **0.9× realtime**. Not a candidate on the current backend.

*Would reconsider if:* it runs on the **GPU via MLX**. The rejection is of the *backend*, not the model —
the model remains the accuracy target and the MLX measurement is live work in the backlog.

### 3.3 Cloud streaming STT — rejected by constraint, not by evidence

Deepgram `nova-3` was the strongest robustness option investigated: trained on telephony and Bluetooth
audio (in-distribution for our worst case, where Whisper is out-of-distribution), native `keyterm`
boosting, and true streaming rather than utterance-segmented — which would dissolve the fragmentation
question entirely. Pipecat ships `DeepgramSTTService` already.

Rejected on the user's explicit constraint: *"I'm not interested in cloud based audio services."*
Consistent with the privacy posture around private sessions.

*Would reconsider if:* the constraint changes. Nothing in the measurements argues against it — this is a
values decision, and it should not be re-litigated on technical grounds.

### 3.4 Scene-graph-aware transcript repair — rejected by direction

Proposed: take Whisper's n-best candidates and fuzzy/phonetically match them against entities that
actually exist in the current world, so "bagel" resolves to "beagle" when there are three beagles and no
bagels. Attractive because it is entirely independent of audio quality.

Rejected: *"I don't want to do any fiddling with the language based on what we know about the scene
graph."*

*Note the boundary:* static `hotwords`/`initial_prompt` — a fixed list of application vocabulary given to
the decoder before it runs — is a **different thing** and remains open in the backlog. The rejection is
of runtime, scene-dependent rewriting of the transcript.

### 3.5 "Use the built-in microphone" — rejected as a *solution*, retained as an *instrument*

The Mac's built-in mic is 48 kHz, wideband and lightly processed; both Bluetooth devices are almost
certainly worse inputs for Whisper. But the system must work while walking around a room, so this cannot
be the answer.

**Retained** as the bench reference condition: the same utterances through the built-in mic and through
AirPods give the acoustic term directly, with the model held fixed.

## 4. Remaining theories

| # | Theory | Likelihood | How it gets tested |
|---|---|---|---|
| 4.1 | **Bluetooth voice-path degradation is a major term.** Both devices route the mic over HFP: band-limited, with device-side NR/AGC tuned for human listeners on a phone call, attacking exactly the fricative energy that separates short command words. The DSP is in the earbud and unreachable. | Medium — *reduced by §2.2* | Real-audio corpus with device condition tags; compare the same model across devices |
| 4.2 | **The remaining errors are model capacity**, and a turbo-class model on the GPU largely closes them. | Medium-high | MLX latency measurement, then the corpus |
| 4.3 | **Utterance fragmentation.** VAD closes a segment at 0.6 s of silence while the turn closes at 0.8 s, so a mid-sentence pause yields two Whisper calls on two context-free fragments. | Unknown | Falls out of the corpus for free: one WAV per VAD segment makes clips that end mid-sentence directly visible |
| 4.4 | **Double VAD.** faster-whisper's own `vad_filter=True` runs a *second* Silero pass over audio pipecat's Silero already accepted, at different parameters — plausibly trimming quiet onsets on a low-level mic. | Low-medium | A/B the flag over the corpus |
| 4.5 | **Missing decoder vocabulary.** Nothing tells Whisper that "conjure", "skybox" or "billboard" are likely words. `DEFAULT_WAKE_WORDS` — eight spellings of one word, amended by observation — is the downstream evidence that this hurts. | Medium | Keyword error rate over the corpus, with and without `hotwords` |

### On theory 4.1 — a partial self-correction

The campaign opened with the argument that *model size matters more as audio degrades*, and therefore
that a bigger model was the natural mitigation for a microphone we cannot change.

**The bench did not support this.** Band-limiting changed `base` not at all (8.3% → 8.3%) and `small.en`
not at all (2.5% → 2.5%). On this evidence bandwidth loss is **not** the dominant term, and model choice
is — which points effort at the recognizer rather than at buying microphones.

The honest limit of that conclusion: `say`-synthesized speech is clean and hyper-articulated, and the
filter only removes the *band*. It does not reproduce noise-suppression artifacts, AGC pumping,
packet-loss concealment, or a real room — which are plausibly the parts that actually hurt. So 4.1 is
**downgraded, not eliminated**, and only real audio can settle it.

### On sample size — why nothing above closes the question

120 reference words means **one word error moves WER by 0.83 points**. `base` = 10 errors, `base.en` = 4,
`small.en` = 3, `large-v3-turbo` = 2.

Consequences that must not be forgotten when quoting this table:

- The only difference reliably above the noise floor is **`base` versus everything else** — which the
  re-run above confirms from the other direction: the numbers that moved were the ones inside it.
- This corpus **cannot separate `small.en` from `large-v3-turbo`** (3 errors vs 2).
- `large-v3-turbo` scoring *better* on narrowband than clean (0.8% vs 1.7%) is a **one-word**
  difference. It is noise, not a finding, and it is not evidence that degradation helps.
- The **timings** do not suffer this problem and are the trustworthy half of the table.

Resolving differences of this size needs 1,000–2,000 reference words of real audio — the corpus work in
the backlog.

## 5. Fixes shipped

Each entry: symptom → cause → fix → knob → commit.

### 5.1 Whisper model `base` → `small.en`
- **Symptom:** individual words mis-transcribed often enough to be noticeable, across every input device.
- **Cause:** the model string was a day-one placeholder (`fa08165`, never revisited) naming the
  *multilingual* `base` — second-smallest of eight, and below pipecat's own default — while the pipeline
  passed `language=EN`, which constrains decoding without selecting the English-only weights.
- **Fix:** `model="small.en"` in `conjure/voice.py`, with the measurement and the
  do-not-"upgrade"-to-`distil-medium.en` warning recorded at the call site.
- **Effect:** WER 8.3% → 2.5% on the bench corpus (10 errors → 3); ~0.9 s more per utterance on CPU.
  Also removes the temperature-fallback behaviour that made small models slower *and* worse on degraded
  audio (§2.3).
- **Knob:** `conjure/voice.py` — the `WhisperSTTService.Settings(model=...)` line. Not yet
  config-driven; `CONJURE_STT` exists but `voice.py` does not read it (backlog).
- **Commit:** `b7be8de`

## Related

- [`docs/specs/voice.md`](../specs/voice.md) — the pipeline as it stands.
- [`docs/backlogs/voice.md`](../backlogs/voice.md) — the corpus framework, MLX, and the transport seam.
- [`docs/providers.md`](../providers.md) — the STT provider registry.
