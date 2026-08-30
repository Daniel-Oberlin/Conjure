"""Phase 2 — the voice loop (PipeCat), a thin client of the agent server (docs/specs/agents.md §10).

Wires a real-time voice front-end to the shared agent:

    mic → Silero VAD → Whisper STT → WebSocket → agent server → WebSocket → Kokoro TTS → speaker

PipeCat here is only *ears and mouth*: STT, TTS, VAD, end-of-turn detection, and mute-while-speaking
echo mitigation. A completed spoken turn is sent to the **agent server** over one WebSocket (as the
`--user`), which owns the shell (command routing), the shared Director/transcript, the LLM roster, and
the world-editing MCP tools. The server's replies stream back as events and are spoken as TTS — so
voice shares one conversation with the CLI (and any other client). No LLM/Director/keys live here.

Audio runs on the host (decision #5's shared-room-device default) — no audio is piped through Quest.

Prerequisites (see docs/running.md): `pip install -e ".[voice]"`, system libs (portaudio/espeak-ng).
Keys live with the agent server, not here.

Usage (three terminals):
    1) python -m conjure                # the world server
    2) python -m conjure.agent_server   # the agent server (holds the director + keys)
    3) python -m conjure.voice          # this voice loop  (or the `conjure-voice` script)
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import urllib.request
from typing import Callable, Optional

from .config import (DEFAULT_USER, VOICE_WAKE_WORDS, WAKE_WORDS, Settings, get_settings,
                     voice_wake_aliases, wake_word_conflict)
from .stt_corpus import CORPUS_DIR, Corpus, purge as purge_corpus, summary as corpus_summary

# PipeCat pipeline idle timeout (seconds). Prevents idle-timeout warnings after inactivity.
PIPELINE_IDLE_TIMEOUT_SECS = 3600  # 1 hour


def _make_wake_gate(wake_word: Optional[str]) -> Callable[[str], Optional[str]]:
    """Wake-word gate for the voice loop. Returns fn(utterance) -> command to run, or None to ignore.

    With no wake word set, every utterance passes through (today's behavior). With one (e.g. 'conjure'):
    - 'conjure make a cat'  -> 'make a cat'  (wake word + command in one breath; then it re-waits)
    - 'conjure' alone       -> arms; the NEXT utterance runs in full, then it re-waits
    - anything else while unarmed -> ignored
    Matching is case-insensitive on word boundaries; text after the wake word (stripped of leading
    punctuation) is the command."""
    if not wake_word or not wake_word.strip():
        return lambda text: text
    # Match the configured aliases too, not just the literal word — the gate is defeated by an STT
    # mis-hearing just as the shell's escape is, and a gate that silently ignores you is harder to
    # diagnose than one that mis-fires. `--wake-word banana` still means banana alone.
    words = voice_wake_aliases(wake_word)
    # The gate CONSUMES its word before anything downstream sees the line, so sharing one with the shell
    # makes shell commands unreachable by voice: "conjure where am I" would arrive as "where am I", which
    # is content. Refuse rather than warn — the failure is silent and would look like a broken shell.
    clash = wake_word_conflict(WAKE_WORDS, words)
    if clash:
        raise SystemExit(
            f"--wake-word {', '.join(clash)!r} is also the shell's wake word, which would make spoken "
            f"shell commands unreachable (the mic gate strips it first). Pick a different word "
            f"(e.g. --wake-word computer), or change CONJURE_WAKE_WORDS.")
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in words) + r")\b", re.IGNORECASE)
    state = {"armed": False}

    def gate(text: str) -> Optional[str]:
        m = pattern.search(text)
        if m:
            after = text[m.end():].lstrip(" ,.:;!?-—").strip()
            state["armed"] = not after            # bare wake word → arm for the next utterance
            return after or None
        if state["armed"]:
            state["armed"] = False
            return text.strip() or None
        return None

    return gate


def _agent_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=2.0) as resp:
            resp.read(1)
        return True
    except Exception:
        return False


async def _run(settings: Settings, user: str = DEFAULT_USER, wake_word: Optional[str] = None,
               corpus: Optional["Corpus"] = None) -> None:
    # Heavy imports are local so the package stays importable on a base (no-voice) install.
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import TranscriptionFrame, TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.processors.frame_processor import FrameProcessor
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.turns.user_mute.always_user_mute_strategy import AlwaysUserMuteStrategy
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.services.whisper.stt import WhisperSTTService
    from pipecat.transcriptions.language import Language
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

    import json

    import websockets

    from .agent_client import ws_url

    holder: dict = {"ws": None}          # the current agent-server socket (None while (re)connecting)
    stop = asyncio.Event()

    # Corpus capture is gated on the LIVE session being public (docs/backlogs/voice.md). `None` means we
    # have not seen a `context` event yet, and unknown must not record: the whole point of a governed
    # recorder is that it is never on when you assumed it wasn't. Every flip is announced, because a
    # recorder that silently stops is as bad as one that silently starts.
    session: dict = {"public": None, "said": None}

    def gate_capture() -> bool:
        ok = session["public"] is True
        if ok != session["said"]:
            session["said"] = ok
            print("[stt-corpus] recording" if ok else
                  "[stt-corpus] paused — session is private" if session["public"] is False else
                  "[stt-corpus] paused — waiting for the session state")
        return ok

    # The voice client is DUMB (docs/specs/agents.md §10): it sends completed user turns to the agent server
    # over a WebSocket and speaks the server's replies. No Director/shell/LLM here — pipecat is just ears +
    # mouth. `bridge` passes frames through and provides speak()/submit(); a listener task turns server
    # events into speech. The early-ack and the final each arrive as their own event → their own spoken
    # utterance, so the streaming cadence is preserved.
    class VoiceBridge(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

        async def speak(self, text: str) -> None:
            await self.push_frame(TTSSpeakFrame(text=text))

        async def submit(self, text: str) -> None:
            ws = holder["ws"]
            if ws is None:
                await self.speak("The agent server isn't connected yet.")
                return
            try:
                await ws.send(json.dumps({"type": "turn", "text": text}))
            except Exception as exc:  # noqa: BLE001 — a send failure shouldn't tear down the pipeline
                print(f"voice send error: {exc}", file=sys.stderr)

    transport = LocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=24000,  # Kokoro's native rate
        )
    )

    # In pipecat 1.3.x, VAD is a pipeline PROCESSOR, not a transport param (passing it to the
    # transport is silently ignored). The VADProcessor emits the speaking frames that both STT
    # (to segment utterances) and the turn aggregator consume. min_volume=0 because a desk mic
    # peaks low (~0.25 full-scale); Silero's confidence alone discriminates speech well.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.6, min_volume=0.0),
        )
    )

    # `small.en`, not `base`. Measured on an M2 Max over a 12-clip synthetic corpus (see
    # docs/investigations/stt-accuracy.md): `base` is the MULTILINGUAL model, and passing language=EN
    # only constrains decoding — it does not buy the English-only weights. WER 8.3% → 2.5%, and
    # `small.en` was also the only model whose decode time did not blow up on band-limited audio
    # (faster-whisper re-decodes at a higher temperature when a segment trips its confidence
    # thresholds, so a small model on a bad mic gets slower and worse together). Costs ~0.9s more per
    # utterance on CPU. Do not "upgrade" to distil-medium.en — measured worse AND slower.
    stt_model = "small.en"
    stt_settings = WhisperSTTService.Settings(model=stt_model, language=Language.EN)

    if corpus is None:
        stt = WhisperSTTService(settings=stt_settings)
    else:
        # Tap `run_stt`: it receives the exact WAV the recognizer saw and yields the text it produced,
        # so the pair is captured at the one point where both exist. Nothing here may raise into the
        # pipeline — a diagnostic that can break the thing it is diagnosing is worse than none.
        class _CorpusWhisperSTTService(WhisperSTTService):
            async def run_stt(self, audio: bytes):
                t0 = time.perf_counter()
                said = ""
                async for frame in super().run_stt(audio):
                    if isinstance(frame, TranscriptionFrame):
                        said = frame.text or ""
                    yield frame
                if not said.strip():
                    return                       # silence / a dropped no-speech segment: nothing to label
                if not gate_capture():
                    return
                try:
                    await asyncio.to_thread(
                        corpus.record, audio, hypothesis=said.strip(), model=stt_model,
                        decode_s=round(time.perf_counter() - t0, 3),
                        sample_rate=self.sample_rate)
                except Exception as exc:  # noqa: BLE001 — never cost the user a turn over a dropped clip
                    print(f"[stt-corpus] could not record clip: {exc}", file=sys.stderr)

        stt = _CorpusWhisperSTTService(settings=stt_settings)

    tts = KokoroTTSService(settings=KokoroTTSService.Settings(voice="af_heart"))

    bridge = VoiceBridge()

    # Listener: turn agent-server events into speech. Reconnects with backoff. `backlog=False` so a fresh
    # connection doesn't get the whole transcript spoken at us — just the current context (which voice
    # ignores; it has no prompt). We speak the shared conversation's replies (assistant text + notices).
    async def listen() -> None:
        url = ws_url(settings.agent_url, user, backlog=False, client="voice")
        while not stop.is_set():
            try:
                async with websockets.connect(url) as ws:
                    holder["ws"] = ws
                    async for raw in ws:
                        ev = json.loads(raw)
                        t = ev.get("type")
                        if t in ("assistant_delta", "assistant_final", "notice"):
                            txt = ev.get("text")
                            if txt and txt.strip():
                                await bridge.speak(txt)
                        elif t == "busy":
                            await bridge.speak("One moment — I'm still working on the last request.")
                        elif t == "context" and "public" in ev:
                            session["public"] = bool(ev["public"])   # gates corpus capture; never spoken
                        # context / user_turn / tool_call / turn_done → not spoken
            except Exception:  # noqa: BLE001 — agent server down/restarting: back off and reconnect
                holder["ws"] = None
                session["public"] = None         # lost the server ⇒ lost the privacy state ⇒ stop recording
                if stop.is_set():
                    return
                await asyncio.sleep(1.0)

    # The LLM context aggregator is kept ONLY for end-of-turn detection + mute-while-speaking — not for
    # messages (the agent server owns the transcript). `on_user_turn_stopped` hands us the full utterance,
    # which we send to the agent server.
    aggregator = LLMContextAggregatorPair(
        LLMContext(messages=[]),
        user_params=LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.8)],
            ),
            user_mute_strategies=[AlwaysUserMuteStrategy()],
        ),
    )

    gate = _make_wake_gate(wake_word)   # mic-activation gate: only speech after the wake word is submitted
    @aggregator.user().event_handler("on_user_turn_stopped")
    async def _on_user_turn(aggr, strategy, message):
        text = (getattr(message, "content", "") or "").strip()
        if not text:
            return
        cmd = gate(text)
        if cmd:
            await bridge.submit(cmd)        # the server routes it (agent utterance vs command)
        elif wake_word:
            print(f"[conjure] (idle — say '{wake_word}' to talk)")

    pipeline = Pipeline(
        [
            transport.input(),       # mic
            vad,                     # VAD → emits user-speaking frames (1.3.x: a processor)
            stt,                     # speech → text
            aggregator.user(),       # detect end-of-turn + mute while speaking
            bridge,                  # sends turns to the agent server; speaks its replies as TTS frames
            tts,                     # reply text → speech
            transport.output(),      # speaker
            aggregator.assistant(),  # (context unused, kept so the pair is wired normally)
        ]
    )

    # Interruptions OFF + mute-while-speaking (above): on an open room mic the bot's own TTS would leak
    # back, get transcribed, and feed back. Use earbuds today; room-speaker support (echo cancellation /
    # barge-in via the WS `interrupt` message) is a follow-up.
    task = PipelineTask(
        pipeline,
        idle_timeout_secs=PIPELINE_IDLE_TIMEOUT_SECS,
        cancel_on_idle_timeout=False,
        cancel_runner_on_idle_timeout=False,
    )
    runner = PipelineRunner(handle_sigint=True)

    listen_task = asyncio.create_task(listen())
    print(f"🎙️  Conjure voice is listening (agent server {settings.agent_url}). Speak to build the world; "
          f"say 'conjure open shell' then 'use <name>' / 'agent <name>' to switch. Ctrl+C to stop.")
    try:
        await runner.run(task)
    finally:
        stop.set()
        listen_task.cancel()


def _purge_cli(*, include_reviewed: bool) -> int:
    """`--stt-corpus-purge`. Unreviewed pairs go without ceremony; reviewed ones need a typed yes."""
    before = corpus_summary()
    if not before["clips"]:
        print(f"No clips in {before['root']}.")
        return 0
    print(f"{before['clips']} clip(s) in {before['root']} — {before['reviewed']} reviewed, "
          f"{before['clips'] - before['reviewed']} not.")
    if include_reviewed and before["reviewed"]:
        # The labels are the hours; the audio is the part that cannot be re-created at all. Losing a
        # reviewed pair loses both, so this one is not a silent flag.
        try:
            if input(f"Delete ALL {before['clips']} clips including {before['reviewed']} reviewed "
                     f"(their labels go too)? Type 'yes': ").strip().lower() != "yes":
                print("Nothing deleted.")
                return 1
        except (EOFError, KeyboardInterrupt):
            print("\nNothing deleted.")
            return 1
    rep = purge_corpus(include_reviewed=include_reviewed)
    print(f"Removed {rep.removed}, kept {rep.kept}.")
    if rep.orphans:
        print(f"Also dropped {len(rep.orphans)} label(s) whose audio was already missing "
              f"(a transcript with no clip cannot be measured).")
    return 0


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="conjure.voice", description="Voice front-end for the Conjure director.")
    # No --agent: voice is a thin client and the agent server owns which agent is open, so the flag
    # could only ever have lied. Switch agents by voice instead ("conjure open shell", then "agent <name>").
    ap.add_argument("--user", default=DEFAULT_USER, help="logged-in user (owns spaces/worlds/assets)")
    ap.add_argument("--wake-word", default=None, metavar="WORDS",
                    help=f"mic gate: only send phrases that start with this word; then it waits for it "
                         f"again. Off when omitted. Accepts a comma-separated list so an STT mis-hearing "
                         f"can ride along (--wake-word computer,computa). Must NOT be the shell's wake "
                         f"word ({WAKE_WORDS[0]!r}) — the gate strips its own word first, so sharing one "
                         f"would make spoken shell commands unreachable")
    # Not --capture-*: "capture" already means ROOM capture throughout this codebase, so a voice flag
    # borrowing the word would read as something to do with Room Setup.
    ap.add_argument("--stt-corpus", action="store_true",
                    help=f"record each utterance to {CORPUS_DIR.relative_to(CORPUS_DIR.parent.parent)}/ "
                         f"for offline STT evaluation. Off by default; pauses while a session is "
                         f"private. See docs/backlogs/voice.md")
    ap.add_argument("--stt-corpus-purge", action="store_true",
                    help="delete UNREVIEWED clips and their labels, then exit — the default purge, "
                         "since unreviewed clips are what fill the disk and are freely re-captured")
    ap.add_argument("--all", action="store_true",
                    help="with --stt-corpus-purge: delete reviewed clips too. Asks first — a reviewed "
                         "clip and its label are hours of work and cannot be regenerated")
    args = ap.parse_args()

    if args.stt_corpus_purge:
        return _purge_cli(include_reviewed=args.all)

    settings = get_settings()

    # Voice is a thin client of the agent server now — it holds no LLM keys itself (the agent server does).
    if not _agent_reachable(settings.agent_url):
        print(f"Agent server not reachable at {settings.agent_url}.\n"
              f"Start it first in another terminal:  python -m conjure.agent_server\n"
              f"(which itself needs the world server:  python -m conjure)")
        return 1

    if args.wake_word:
        print(f"🔒 Wake word active: say '{args.wake_word}' before a command.")

    corpus = None
    if args.stt_corpus:
        corpus = Corpus()
        # Say it out loud at startup. A recorder you forgot you enabled is the failure mode that
        # matters, and naming the device here is also how you notice the mic is not the one you think.
        print(f"⏺  STT corpus recording to {corpus.root} (input: {corpus.device}). "
              f"Paused while a session is private. Purge: --stt-corpus-purge")
    try:
        asyncio.run(_run(settings, args.user, args.wake_word, corpus))
    except KeyboardInterrupt:
        print("\nStopped.")
    except ImportError as exc:
        print(f"Voice dependencies are missing ({exc}).\n"
              f"Install them with `./scripts/setup.sh` (see docs/running.md), then re-run.")
        return 1
    if corpus is not None and corpus.count:
        print(f"⏺  Recorded {corpus.count} clip(s). Label them by editing the `truth` column of "
              f"{corpus.truth} and marking `reviewed` y.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
