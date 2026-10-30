"""Mic-activation gate for the voice loop (conjure.voice._make_wake_gate).

This is the VOICE gate — "are you talking to me at all" — and it is a different word from the shell's
command escape on purpose. The gate consumes its word before anything downstream sees the line, so
sharing one would make spoken shell commands unreachable. See `test_the_two_gates_compose`."""

from __future__ import annotations

from conjure.voice import _make_wake_gate


def test_no_wake_word_passes_everything_through():
    gate = _make_wake_gate(None)
    assert gate("make a cat") == "make a cat"
    assert _make_wake_gate("")("hello") == "hello"       # blank wake word ⇒ passthrough


def test_wake_word_and_command_in_one_breath():
    gate = _make_wake_gate("computer")
    assert gate("computer make a cat") == "make a cat"    # strips the wake word, sends the command
    assert gate("Computer, place a tree") == "place a tree"   # case-insensitive + leading punctuation
    assert gate("um computer: paint the walls blue") == "paint the walls blue"  # wake word mid-utterance


def test_ignores_utterances_without_the_wake_word():
    gate = _make_wake_gate("computer")
    assert gate("make a cat") is None                    # no wake word → ignored
    assert gate("what's the weather") is None


def test_bare_wake_word_arms_the_next_utterance_then_re_waits():
    gate = _make_wake_gate("computer")
    assert gate("computer") is None                       # bare wake word → arm (nothing to run yet)
    assert gate("make a dragon") == "make a dragon"      # armed → next utterance runs in full
    assert gate("and a castle") is None                  # re-waits: not armed, no wake word → ignored
    assert gate("computer add a lake") == "add a lake"    # wake word again → runs


def test_word_boundary_avoids_false_matches():
    gate = _make_wake_gate("computer")
    assert gate("recomputed the scene") is None          # substring, not a whole word → no trigger


def test_the_two_gates_are_distinct_and_compose():
    """The mic gate opens the channel; the shell's wake word turns what's left into a command. Both have
    to survive one utterance, which is only possible if they are different words."""
    from conjure.shell import Shell
    gate, sh = _make_wake_gate("computer"), Shell(None)

    said = "computer conjure where am I"
    submitted = gate(said)
    assert submitted == "conjure where am I"           # the mic gate strips only ITS word
    assert sh.as_command(submitted, False) == "where am I"     # …leaving the shell's escape intact

    assert sh.as_command(gate("computer make a tree"), False) is None      # ordinary speech stays content


def test_a_gate_word_that_collides_with_the_shell_is_refused():
    """Sharing a word is silent breakage: "conjure where am I" would reach the shell as "where am I" —
    content — and you would have to say the word twice. Refuse rather than warn; a warning scrolls past
    and the symptom looks like a broken shell."""
    import pytest
    with pytest.raises(SystemExit, match="unreachable"):
        _make_wake_gate("conjure")
    with pytest.raises(SystemExit):                    # an alias of the shell word is just as fatal
        _make_wake_gate("coinjure")


def test_a_bespoke_gate_word_is_taken_literally():
    """`--wake-word banana` means banana — only the configured canonical expands to its aliases."""
    gate = _make_wake_gate("banana")
    assert gate("banana make a cat") == "make a cat"
    assert gate("computer make a cat") is None


def test_the_gate_word_may_be_given_as_a_comma_separated_list():
    """One invocation names the word AND its mis-hearings, so covering a new one needs no env var."""
    gate = _make_wake_gate("computer,computa,computah")
    assert gate("computer make a cat") == "make a cat"
    assert gate("computa make a cat") == "make a cat"
    assert gate("computah make a cat") == "make a cat"
    assert gate("hello there") is None


def test_a_shell_word_hidden_inside_a_list_is_still_refused():
    """The conflict check runs over the parsed set, not the raw string — otherwise a list would be the
    way to smuggle a collision past it."""
    import pytest
    with pytest.raises(SystemExit, match="unreachable"):
        _make_wake_gate("computer,conjure")


# --------------------------------------------------------------------------- the voice server (/audio)

def test_audio_socket_runs_one_pipeline_per_connection_as_that_user():
    """`/audio?user=` is accepted, answered `ready` with the TTS rate, counted while open, and the
    pipeline it started is cancelled when the browser goes. The pipeline is a stand-in here — the real
    one loads the recognizer — but the transport it is handed is the real websocket one."""
    import asyncio
    import time

    from fastapi.testclient import TestClient

    from conjure.config import get_settings
    from conjure.voice import build_voice_app

    seen: list = []

    async def fake_pipeline(settings, *, user, transport, ready=None, label="", activation=None, **kw):
        seen.append((user, type(transport).__name__, label, activation))
        ready.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            seen.append(("cancelled", user))
            raise

    app = build_voice_app(get_settings(), pipeline=fake_pipeline)
    with TestClient(app) as c:
        with c.websocket_connect("/audio?user=alice") as ws:
            assert ws.receive_json() == {"type": "ready", "out_rate": 24000}
            assert c.get("/health").json()["connections"] == 1
            assert seen[0] == ("alice", "FastAPIWebsocketTransport", "alice", "ptt")   # a page that didn't say
        with c.websocket_connect("/audio?activation=open&user=alice") as ws:
            ws.receive_json()
            for _ in range(100):
                if len(seen) > 2:
                    break
                time.sleep(0.02)
            assert seen[-1] == ("alice", "FastAPIWebsocketTransport", "alice", "open")
        for _ in range(100):                                   # the disconnect lands in the finally
            if ("cancelled", "alice") in seen:
                break
            time.sleep(0.02)
        assert ("cancelled", "alice") in seen
        assert c.get("/health").json()["connections"] == 0


# --------------------------------------------------------------------------- what the client is told
# The page sends the barge-in `interrupt` only while it believes the bot is speaking, so `speaking` must
# reach it. It didn't: the message was read after the recognizer, from Bot*SpeakingFrames that never
# climbed that far, and on the first headset run a held talk button over a reply did nothing (2026-10-20).

async def _drive(processor, frames, direction):
    from pipecat.processors.frame_processor import FrameDirection
    from pipecat.tests.utils import run_test

    return await run_test(processor, frames_to_send=frames,
                          frames_to_send_direction=getattr(FrameDirection, direction))


def _messages(frames):
    from pipecat.frames.frames import OutputTransportMessageFrame

    return [f.message for f in frames if isinstance(f, OutputTransportMessageFrame)]


async def test_the_bot_starting_and_stopping_reaches_the_client_as_speaking_messages():
    from pipecat.frames.frames import BotStartedSpeakingFrame, BotStoppedSpeakingFrame

    from conjure.voice import client_wire

    _, Speaking = client_wire()
    down, up = await _drive(Speaking(), [BotStartedSpeakingFrame(), BotStoppedSpeakingFrame()], "UPSTREAM")
    assert _messages(down) == [{"type": "speaking", "on": True}, {"type": "speaking", "on": False}]
    assert [type(f).__name__ for f in up] == ["BotStartedSpeakingFrame", "BotStoppedSpeakingFrame"]


async def test_only_the_upstream_copy_counts_so_the_client_hears_each_change_once():
    """The output transport pushes each Bot*SpeakingFrame BOTH ways; the downstream copy never passes this
    processor in the real pipeline, but if it did it must not double the message."""
    from pipecat.frames.frames import BotStartedSpeakingFrame

    from conjure.voice import client_wire

    _, Speaking = client_wire()
    down, _ = await _drive(Speaking(), [BotStartedSpeakingFrame()], "DOWNSTREAM")
    assert _messages(down) == []


async def test_a_transcript_is_told_once_on_its_way_down_and_never_for_empty_text():
    from pipecat.frames.frames import TranscriptionFrame

    from conjure.voice import client_wire

    Wire, _ = client_wire()
    heard = TranscriptionFrame("Make the ceilings white.", "u", "t")
    closing = TranscriptionFrame("", "u", "t", finalized=True)          # GrokSTT's "nothing new" close
    down, _ = await _drive(Wire(), [heard, closing], "DOWNSTREAM")
    assert _messages(down) == [{"type": "transcript", "text": "Make the ceilings white."}]
    _, up = await _drive(Wire(), [heard], "UPSTREAM")
    assert _messages(up) == []                                          # no message for an upstream copy


def test_the_speaking_processor_sits_right_before_the_output_transport():
    """Placement IS the fix — next to the transport that pushes the frames and serializes the message."""
    import inspect

    from conjure import voice

    src = inspect.getsource(voice.run_pipeline)
    order = [line.split("#")[0].strip().rstrip(",") for line in src.split("Pipeline(", 1)[1].split("]", 1)[0]
             .splitlines() if line.split("#")[0].strip()]
    assert order[order.index("transport.output()") - 1] == "speaking"


# --------------------------------------------------------------------------- the end of a turn
# "Make the doors… solid black" (2026-10-30): an open mic's silence timer cut the sentence at the pause,
# the first half was answered as a question, and the second was refused `busy` and lost.

def test_push_to_talk_ends_a_turn_on_silence_and_an_open_mic_asks_the_turn_model():
    from pipecat.turns.user_stop import (SpeechTimeoutUserTurnStopStrategy,
                                         TurnAnalyzerUserTurnStopStrategy)

    from conjure.voice import stop_strategies

    assert [type(s) for s in stop_strategies("ptt")] == [SpeechTimeoutUserTurnStopStrategy]
    assert [type(s) for s in stop_strategies("open")] == [TurnAnalyzerUserTurnStopStrategy]


def test_the_second_half_of_a_sentence_joins_a_request_that_has_not_acted():
    from conjure.voice import TurnFloor

    f = TurnFloor()
    assert f.submit("Make the doors") == [("turn", "Make the doors")]
    assert f.on_event({"type": "assistant_delta", "text": "On it!"}) == []
    assert f.submit("solid black.") == [("interrupt",)]
    assert f.on_event({"type": "interrupted"}) == []
    assert f.on_event({"type": "turn_done"}) == [("turn", "Make the doors solid black.")]
    assert f.on_event({"type": "turn_done"}) == [] and f.inflight is None


def test_speech_after_the_request_acted_waits_and_goes_on_its_own():
    from conjure.voice import TurnFloor

    f = TurnFloor()
    f.submit("make the doors black")
    f.on_event({"type": "tool_call", "name": "patch"})
    assert f.submit("and the windows") == []                       # held, not joined: the doors are done
    assert f.submit("too") == []
    assert f.on_event({"type": "turn_done"}) == [("turn", "and the windows too")]


def test_speech_after_a_barge_in_is_a_new_request_not_a_continuation():
    from conjure.voice import TurnFloor

    f = TurnFloor()
    f.submit("tell me a long story")
    f.interrupted()
    assert f.submit("make the walls red") == []
    assert f.on_event({"type": "turn_done"}) == [("turn", "make the walls red")]


def test_refused_speech_is_offered_again_when_the_other_clients_turn_ends():
    from conjure.voice import TurnFloor

    f = TurnFloor()
    f.submit("make the doors black")
    assert f.on_event({"type": "busy"}) == []
    assert f.on_event({"type": "turn_done"}) == []                 # the refusal's own turn_done
    assert f.inflight is None
    assert f.on_event({"type": "context"}) == [("turn", "make the doors black")]


def test_a_dropped_agent_socket_keeps_held_speech_and_frees_the_floor():
    from conjure.voice import TurnFloor

    f = TurnFloor()
    f.submit("make the doors")
    f.submit("black")                                              # joining
    f.reset()
    assert f.inflight is None
    assert f.submit("please") == [("turn", "make the doors black please")]
