"""Tier 2 — library contract checks. No API calls, ~free. Catches SDK API drift (e.g. the
PipeCat 1.x moves that bit us) at *test* time instead of at runtime. Skips if a dep isn't installed."""

import inspect

import pytest


def test_pipecat_surface_we_depend_on():
    pytest.importorskip("pipecat")
    from pipecat.audio.vad.silero import SileroVADAnalyzer  # noqa: F401
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.pipeline.pipeline import Pipeline  # noqa: F401
    from pipecat.pipeline.task import PipelineParams, PipelineTask  # noqa: F401
    from pipecat.processors.aggregators.llm_context import LLMContext  # noqa: F401
    from pipecat.processors.aggregators.llm_response_universal import (  # noqa: F401
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.processors.audio.vad_processor import VADProcessor  # noqa: F401
    from pipecat.processors.frame_processor import FrameProcessor, FrameDirection  # noqa: F401
    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.services.whisper.stt import WhisperSTTService
    from pipecat.turns.user_mute.always_user_mute_strategy import AlwaysUserMuteStrategy  # noqa: F401
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy  # noqa: F401

    assert hasattr(WhisperSTTService, "Settings")
    assert hasattr(KokoroTTSService, "Settings")
    # voice.py's DirectorProcessor subclasses FrameProcessor: overrides process_frame(self, frame,
    # direction) and calls push_frame; it speaks the director's reply via TTSSpeakFrame(text=...).
    assert {"frame", "direction"} <= set(inspect.signature(FrameProcessor.process_frame).parameters)
    assert hasattr(FrameProcessor, "push_frame")
    assert "text" in {f.name for f in __import__("dataclasses").fields(TTSSpeakFrame)}


def test_genai_image_config_surface():
    pytest.importorskip("google.genai")
    from google.genai import types

    fields = types.ImageConfig.model_fields
    assert "aspect_ratio" in fields and "image_size" in fields
    assert "response_modalities" in types.GenerateContentConfig.model_fields


def test_mcp_client_surface():
    from mcp import ClientSession, StdioServerParameters  # noqa: F401
    from mcp.client.stdio import stdio_client

    assert "errlog" in inspect.signature(stdio_client).parameters
