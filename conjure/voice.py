"""Phase 2 — the voice loop (PipeCat).

Wires a real-time voice agent to the Conjure world:

    mic → Silero VAD → Whisper STT → shell → agent (conjure.shell/director) → Kokoro TTS → speaker

PipeCat here is only *ears and mouth*: STT, TTS, VAD, end-of-turn detection, and mute-while-speaking
echo mitigation. Spoken turns go through the deterministic `conjure.shell.Shell` (commands like
"conjure open shell" run there, never reaching an LLM) which forwards the rest to the active agent —
the shared `conjure.director.Director` (today's `builder`), the SAME agent the CLI drives. It owns the
LLM roster (Claude/Gemini/…, switchable mid-conversation), the attributed transcript, the world-editing
MCP tools, and the live room injected into its prompt. So spoken requests turn into world patches that
broadcast live to every connected headset, and adding/switching LLMs or agents needs no change here.

Audio runs on the host (decision #5's shared-room-device default) — no audio is piped through Quest.

Prerequisites (see docs/setup.md): `pip install -e ".[voice]"`, system libs (portaudio/espeak-ng),
and ANTHROPIC_API_KEY and/or GOOGLE_API_KEY in `.env`. Run `python -m conjure.doctor` to check.

Usage (two terminals):
    1) python -m conjure          # the world server (must be running)
    2) python -m conjure.voice    # this voice loop  (or the `conjure-voice` script)
"""

from __future__ import annotations

import asyncio
import sys
import urllib.request

from .config import DEFAULT_USER, Settings, get_settings
from .director import Director
from .shell import Shell

# PipeCat pipeline idle timeout (seconds). Prevents idle-timeout warnings after inactivity.
PIPELINE_IDLE_TIMEOUT_SECS = 3600  # 1 hour


def _world_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/world", timeout=2.0) as resp:
            resp.read(1)
        return True
    except Exception:
        return False


async def _run(settings: Settings, user: str = DEFAULT_USER) -> None:
    # Heavy imports are local so the package stays importable on a base (no-voice) install.
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import TTSSpeakFrame
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

    # Our "brain" as a PipeCat processor: it passes audio/text frames through untouched and, when a
    # user turn completes, runs the shared director and speaks its reply via TTSSpeakFrame (a
    # self-contained utterance the TTS service synthesizes immediately — the early acknowledgement
    # and the final confirmation each become one).
    class DirectorProcessor(FrameProcessor):
        def __init__(self, shell: Shell):
            super().__init__()
            self._shell = shell      # the shell (deterministic commands) wrapping the agent

        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)

        async def run_turn(self, text: str) -> None:
            async def on_text(reply: str, *, final: bool, speaker: str) -> None:
                await self.push_frame(TTSSpeakFrame(text=reply))
            try:
                await self._shell.feed(text, on_text=on_text)
            except Exception as exc:  # never let one bad turn tear down the pipeline
                print(f"director error: {exc}", file=sys.stderr)
                await self.push_frame(
                    TTSSpeakFrame(
                        text=(
                            "There was an error with the Director. "
                            "Please try again or switch to a different LLM."
                        )
                    )
                )

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

    stt = WhisperSTTService(settings=WhisperSTTService.Settings(model="base", language=Language.EN))
    tts = KokoroTTSService(settings=KokoroTTSService.Settings(voice="af_heart"))

    # The shared director owns the LLM roster and the world-editing MCP tools (it spawns
    # conjure.mcp_server over stdio itself). PipeCat no longer talks to any LLM.
    async with Director.connect(settings, user=user) as director:
        director_proc = DirectorProcessor(Shell(director, settings))

        # We keep the LLM context aggregator ONLY for its end-of-turn detection and mute-while-
        # speaking — not for messages (the director owns the transcript). Its `on_user_turn_stopped`
        # event hands us the full utterance, which we run through the director.
        aggregator = LLMContextAggregatorPair(
            LLMContext(messages=[]),
            user_params=LLMUserAggregatorParams(
                # End the user's turn after a short silence (instead of pipecat 1.3's default
                # Smart-Turn v3 model, which never completed the turn so nothing happened). Simple
                # + predictable for a command interface.
                user_turn_strategies=UserTurnStrategies(
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.8)],
                ),
                # Mute the mic while the bot speaks, so its own TTS leaking back through the room
                # speaker can't disrupt its turn (the "a red cu..." cut-off). Removes the echo
                # feedback loop without needing a headset.
                user_mute_strategies=[AlwaysUserMuteStrategy()],
            ),
        )

        @aggregator.user().event_handler("on_user_turn_stopped")
        async def _on_user_turn(aggr, strategy, message):
            text = (getattr(message, "content", "") or "").strip()
            if text:
                await director_proc.run_turn(text)

        pipeline = Pipeline(
            [
                transport.input(),       # mic
                vad,                     # VAD → emits user-speaking frames (1.3.x: a processor)
                stt,                     # speech → text
                aggregator.user(),       # detect end-of-turn + mute while speaking
                director_proc,           # brain anchor: speaks the director's replies as TTS frames
                tts,                     # reply text → speech
                transport.output(),      # speaker
                aggregator.assistant(),  # (context unused, kept so the pair is wired normally)
            ]
        )

        # Interruptions OFF + mute-while-speaking (above): on an open room mic the bot's own TTS
        # would otherwise leak back, get transcribed, and feed back as user input. Use earbuds for
        # clean room use today; proper room-speaker support (echo cancellation / push-to-talk) is
        # a roadmap audio-polish item.
        # Idle timeout: these are PipelineTask kwargs, NOT PipelineParams fields. PipelineParams silently
        # drops unknown kwargs, so the old idle_timeout_secs (and allow_interruptions) there did nothing —
        # the pipeline kept pipecat's 300s default and tore down after ~5 min idle. Set a long timeout AND
        # don't cancel on idle, so a quiet session is never killed.
        task = PipelineTask(
            pipeline,
            idle_timeout_secs=PIPELINE_IDLE_TIMEOUT_SECS,
            cancel_on_idle_timeout=False,
            cancel_runner_on_idle_timeout=False,
        )
        runner = PipelineRunner(handle_sigint=True)

        roster = ", ".join(director.roster) or "none"
        print(f"🎙️  Conjure voice is listening (active={director.active}; roster: {roster}). Speak "
              f"to build the world; say 'let me talk to <name>' to switch LLMs. Ctrl+C to stop.")
        await runner.run(task)


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="conjure.voice", description="Voice front-end for the Conjure director.")
    ap.add_argument("--user", default=DEFAULT_USER, help="logged-in user (owns spaces/worlds/assets)")
    args = ap.parse_args()

    settings = get_settings()

    if not (settings.anthropic_api_key or settings.google_api_key):
        print("No director LLM keys set. Add ANTHROPIC_API_KEY and/or GOOGLE_API_KEY to .env, then "
              "run `python -m conjure.doctor`.")
        return 1
    if not _world_reachable(settings.world_url):
        print(f"World server not reachable at {settings.world_url}.\n"
              f"Start it first in another terminal:  python -m conjure")
        return 1

    try:
        asyncio.run(_run(settings, args.user))
    except KeyboardInterrupt:
        print("\nStopped.")
    except ImportError as exc:
        print(f"Voice dependencies are missing ({exc}).\n"
              f"Install them with `./scripts/setup.sh` (see docs/setup.md), then re-run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
