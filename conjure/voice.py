"""Phase 2 — the voice loop (PipeCat).

Wires a real-time voice agent to the Conjure world:

    mic → Silero VAD → Whisper STT → Claude (director, w/ MCP tools) → Kokoro TTS → speaker

The director LLM is given the world-editing MCP tools (via PipeCat's MCPClient connecting to
`conjure.mcp_server` over stdio), so spoken requests turn into world patches that broadcast live
to every connected headset. Audio runs on the host (decision #5's shared-room-device default) —
no audio is piped through the Quest.

Prerequisites (see docs/setup.md): `pip install -e ".[voice]"`, system libs (portaudio/espeak-ng),
and ANTHROPIC_API_KEY in `.env`. Run `python -m conjure.doctor` to check.

Usage (two terminals):
    1) python -m conjure          # the world server (must be running)
    2) python -m conjure.voice    # this voice loop  (or the `conjure-voice` script)
"""

from __future__ import annotations

import asyncio
import os
import sys
import urllib.request

from .config import Settings, get_settings

# Spoken context for the director. Matches the world's coordinate convention (user faces -z,
# eye height ~1.6 m), so placements land in front of the user.
SYSTEM_PROMPT = (
    "You are Conjure, the director of a voice-controlled VR holodeck. When the user describes or "
    "requests a scene or a change, USE THE TOOLS to build and edit the world — add, move, update, "
    "or remove objects, and set the environment. "
    "For real-world objects (a tree, a chair, a car, an animal), use place_asset with a short "
    "search query; use add_entity only for basic primitive shapes (cube, sphere, cone, ...). "
    "CRITICAL: do NOT think out loud, explain your reasoning, or recite coordinates, sizes, or "
    "measurements. Do the work silently via tool calls, then reply with AT MOST one short "
    "confirmation sentence (e.g. 'Added a gray sphere above the cubes.'). Never repeat or restate "
    "what the user said. If no action is needed, reply with a single word like 'Okay.' "
    "Call query_world first when an edit depends on what's already there. "
    "Positions are [x, y, z] in meters: the user faces -z, so place things a few meters in front "
    "(negative z) around y=1 unless asked otherwise. For place_asset, always pass size_m as the "
    "object's real-world size in meters (tree ~7, chair ~0.9, mug ~0.1) so the scene is to-scale; "
    "those objects auto-sit on the floor (y=0) — only raise y to set something on a surface."
)


def _world_reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/world", timeout=2.0) as resp:
            resp.read(1)
        return True
    except Exception:
        return False


async def _run(settings: Settings) -> None:
    # Heavy imports are local so the package stays importable on a base (no-voice) install.
    from mcp import StdioServerParameters
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
    from pipecat.processors.audio.vad_processor import VADProcessor
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.services.anthropic.llm import AnthropicLLMService
    from pipecat.turns.user_mute.always_user_mute_strategy import AlwaysUserMuteStrategy
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.services.mcp_service import MCPClient
    from pipecat.services.whisper.stt import WhisperSTTService
    from pipecat.transcriptions.language import Language
    from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

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
    llm = AnthropicLLMService(
        api_key=settings.anthropic_api_key,
        settings=AnthropicLLMService.Settings(model=settings.llm_model),
    )

    # The director's tools come from our world-editing MCP server, spawned over stdio. It POSTs
    # patches to the world server, so CONJURE_URL must point there.
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "conjure.mcp_server"],
        env={**os.environ, "CONJURE_URL": settings.world_url},
    )

    async with MCPClient(server_params=server_params) as mcp:
        tools = await mcp.register_tools(llm)  # discovers + registers world tools with the LLM
        context = LLMContext(
            messages=[{"role": "system", "content": SYSTEM_PROMPT}],
            tools=tools,
        )
        # End the user's turn after a short silence (instead of pipecat 1.3's default Smart-Turn
        # v3 model, which otherwise decides when the director runs — and was never completing
        # the turn, so nothing happened). Simple + predictable for a command interface.
        aggregator = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.8)],
                ),
                # Mute the mic while the bot speaks, so its own TTS leaking back through the room
                # speaker can't disrupt its turn (the "a red cu..." cut-off). Removes the echo
                # feedback loop without needing a headset.
                user_mute_strategies=[AlwaysUserMuteStrategy()],
            ),
        )

        pipeline = Pipeline(
            [
                transport.input(),       # mic
                vad,                     # VAD → emits user-speaking frames (1.3.x: a processor)
                stt,                     # speech → text
                aggregator.user(),       # add user turn to context
                llm,                     # director: may emit tool calls (world edits)
                tts,                     # reply text → speech
                transport.output(),      # speaker
                aggregator.assistant(),  # add assistant turn to context
            ]
        )

        # Interruptions OFF + mute-while-speaking (above): on an open room mic the bot's own TTS
        # would otherwise leak back, get transcribed, and feed back as user input. Use earbuds for
        # clean room use today; proper room-speaker support (echo cancellation / push-to-talk) is
        # a roadmap audio-polish item.
        task = PipelineTask(pipeline, params=PipelineParams(allow_interruptions=False))
        runner = PipelineRunner(handle_sigint=True)

        print(f"🎙️  Conjure voice is listening (director={settings.llm_model}). Speak to build the "
              f"world. Ctrl+C to stop.")
        await runner.run(task)


def main() -> int:
    settings = get_settings()

    if settings.llm == "claude" and not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set. Add it to .env, then run `python -m conjure.doctor`.")
        return 1
    if not _world_reachable(settings.world_url):
        print(f"World server not reachable at {settings.world_url}.\n"
              f"Start it first in another terminal:  python -m conjure")
        return 1

    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        print("\nStopped.")
    except ImportError as exc:
        print(f"Voice dependencies are missing ({exc}).\n"
              f"Install them with `./scripts/setup.sh` (see docs/setup.md), then re-run.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
