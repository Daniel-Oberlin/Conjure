"""Phase 2 — the voice loop (PipeCat), a thin client of the agent server (docs/specs/agents.md §10).

Wires a real-time voice front-end to the shared agent:

    mic → Silero VAD → STT → WebSocket → agent server → WebSocket → TTS → speaker

The ears and the mouth are two INDEPENDENT settings. `CONJURE_STT` (conjure/stt.py) is Whisper (local,
segmented) or Grok (xAI, streaming); `CONJURE_TTS` (conjure/tts.py) is Kokoro (local) or Grok (xAI,
renders speech tags such as [laugh] and <whisper>…</whisper>). Any pairing works. On connect this
client REGISTERS its TTS engine with the agent server — the tag vocabulary and the prompt guidance for
it — so the LLM is told what it may write and every other client is sent the reply with the tags
stripped. The engine is the only thing here that knows a tag.

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
import urllib.request
from typing import Callable, Optional

# fastapi is a core dependency (the world and agent servers), so the voice server's two routes import it
# at module level — and they MUST: this file uses `from __future__ import annotations`, so a `WebSocket`
# annotation is a string FastAPI resolves against the module's globals. Imported inside the builder it
# resolved to nothing, and FastAPI quietly treated the socket as a required QUERY parameter (every
# handshake answered 1008 "field required: websocket") — found by spying on the ASGI messages.
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .config import (DEFAULT_USER, VOICE_WAKE_WORDS, WAKE_WORDS, Settings, get_settings,
                     voice_wake_aliases, wake_word_conflict)
from .stt import HearingProfile, profile_for as hearing_for
from .stt_corpus import CORPUS_DIR, Corpus, purge as purge_corpus, summary as corpus_summary
from .transport import PROFILES as TRANSPORTS
from .tts import SpeechProfile, profile_for

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


def client_wire():
    """The two processors that tell a client what the pipeline is doing → `(WireMessages,
    SpeakingMessages)`. Module-level (pipecat imported lazily, like the rest of this file) so tests can
    drive them one at a time; `run_pipeline` places them."""
    from pipecat.frames.frames import (BotStartedSpeakingFrame, BotStoppedSpeakingFrame,
                                       OutputTransportMessageFrame, TranscriptionFrame)
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    # What the CLIENT is told about the pipeline's state, in two processors because the two facts are
    # born at opposite ends. `transcript` (what was heard — the on-headset diagnostic for mis-hearings) is
    # read right after the recognizer, on the way down.
    class WireMessages(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if (isinstance(frame, TranscriptionFrame) and direction == FrameDirection.DOWNSTREAM
                    and (frame.text or "").strip()):
                await self.push_frame(OutputTransportMessageFrame(
                    message={"type": "transcript", "text": frame.text.strip()}), FrameDirection.DOWNSTREAM)

    # `speaking` is read right BEFORE the output transport, the processor that pushes the Bot*Speaking
    # frames. It used to be read in WireMessages, after the recognizer, trusting the upstream copies to
    # climb past the TTS, the bridge and the user aggregator — and BotStarted never arrived there, so the
    # page never learned the bot was talking and never sent the barge-in `interrupt` (found on the first
    # headset run, 2026-10-20). Here the frame arrives from the very next processor, and the message goes
    # straight into the transport that serializes it. The page needs it: PTT-while-speaking IS barge-in.
    class SpeakingMessages(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            if isinstance(frame, (BotStartedSpeakingFrame, BotStoppedSpeakingFrame)) \
                    and direction == FrameDirection.UPSTREAM:
                on = isinstance(frame, BotStartedSpeakingFrame)
                await self.push_frame(OutputTransportMessageFrame(message={"type": "speaking", "on": on}),
                                      FrameDirection.DOWNSTREAM)

    return WireMessages, SpeakingMessages


async def run_pipeline(settings: Settings, *, user: str, transport, wake_word: Optional[str] = None,
                       corpus: Optional["Corpus"] = None, profile: Optional[SpeechProfile] = None,
                       hearing: Optional[HearingProfile] = None, handle_sigint: bool = True,
                       ready: Optional[asyncio.Event] = None, label: str = "") -> None:
    """ONE voice pipeline, on `transport`, acting as `user`. The host-mic client runs one of these; the
    voice server runs one per headset connection. Everything between the transport's two ends is the same
    chain: VAD → STT profile → turn aggregator → bridge (its own agent-server socket) → TTS profile."""
    profile = profile or profile_for(settings)   # the mouth: which engine, its rate, its tags (conjure/tts.py)
    hearing = hearing or hearing_for(settings, corpus=corpus is not None)   # the ears (conjure/stt.py)
    # Heavy imports are local so the package stays importable on a base (no-voice) install.
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.frames.frames import (InputTransportMessageFrame, InterruptionFrame,
                                       OutputTransportMessageFrame, TTSSpeakFrame)
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask
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

    import json

    import websockets

    from .agent_client import ws_url

    holder: dict = {"ws": None}          # the current agent-server socket (None while (re)connecting)
    stop = asyncio.Event()
    tag = f"[voice{(':' + label) if label else ''}]"

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
    #
    # It is ALSO where the wire's control messages meet the pipeline (conjure/transport.py `Wire`): a
    # client's `interrupt` cancels the reply at both ends — an InterruptionFrame downstream flushes the
    # TTS and the transport's buffered audio; the agent server's reserved `interrupt` cancels the turn.
    class VoiceBridge(FrameProcessor):
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, InputTransportMessageFrame) and isinstance(frame.message, dict):
                if frame.message.get("type") == "interrupt":
                    await self.interrupt()
                return                                     # consumed: the wire's messages stop here
            await self.push_frame(frame, direction)

        async def speak(self, text: str) -> None:
            await self.push_frame(TTSSpeakFrame(text=text))

        async def tell(self, message: dict) -> None:
            """A control message to the client (speaking / transcript / notice). A local transport has
            no one to tell and drops it; the websocket serializer sends it as JSON."""
            await self.push_frame(OutputTransportMessageFrame(message=message))

        async def submit(self, text: str) -> None:
            ws = holder["ws"]
            if ws is None:
                await self.speak("The agent server isn't connected yet.")
                return
            try:
                await ws.send(json.dumps({"type": "turn", "text": text}))
            except Exception as exc:  # noqa: BLE001 — a send failure shouldn't tear down the pipeline
                print(f"voice send error: {exc}", file=sys.stderr)

        async def interrupt(self) -> None:
            await self.push_frame(InterruptionFrame())       # downstream: TTS + output flush what's queued
            ws = holder["ws"]
            if ws is not None:
                try:
                    await ws.send(json.dumps({"type": "interrupt"}))
                except Exception as exc:  # noqa: BLE001
                    print(f"voice send error: {exc}", file=sys.stderr)

    # In pipecat 1.3.x, VAD is a pipeline PROCESSOR, not a transport param (passing it to the
    # transport is silently ignored). The VADProcessor emits the speaking frames that both STT
    # (to segment utterances) and the turn aggregator consume. min_volume=0 because a desk mic
    # peaks low (~0.25 full-scale); Silero's confidence alone discriminates speech well.
    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(confidence=0.6, start_secs=0.2, stop_secs=0.6, min_volume=0.0),
        )
    )

    # The recognizer, built by its profile; a Whisper profile taps itself for `--stt-corpus` (the
    # exact WAV it decoded, gated on the session being public). A streaming profile has no WAV to
    # record, which `hearing_for(corpus=True)` already refused at the door.
    stt = hearing.build(settings, corpus=corpus, may_record=gate_capture)

    tts = profile.build(settings)

    bridge = VoiceBridge()
    WireMessages, SpeakingMessages = client_wire()
    wire = WireMessages()
    speaking = SpeakingMessages()

    # Listener: turn agent-server events into speech. Reconnects with backoff. `backlog=False` so a fresh
    # connection doesn't get the whole transcript spoken at us — just the current context (which voice
    # ignores; it has no prompt). We speak the shared conversation's replies (assistant text + notices).
    async def listen() -> None:
        url = ws_url(settings.agent_url, user, backlog=False, client="voice")
        while not stop.is_set():
            try:
                async with websockets.connect(url) as ws:
                    holder["ws"] = ws
                    # Say what we can render BEFORE the first turn, and again on every reconnect (the
                    # server forgets a connection's registration with the connection). The server
                    # injects the engine's tag guidance into the system prompt and keeps the tags in
                    # replies bound for us; a plain engine registers no tags and hears none.
                    await ws.send(json.dumps({"type": "register", "speech": profile.registration()}))
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
                        # context / user_turn / tool_call / turn_done / interrupted → not spoken
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
            print(f"{tag} (idle — say '{wake_word}' to talk)")

    pipeline = Pipeline(
        [
            transport.input(),       # mic (host device, or a browser's worklet over the wire)
            vad,                     # VAD → emits user-speaking frames (1.3.x: a processor)
            stt,                     # speech → text
            wire,                    # tells the client what was heard / when the bot speaks
            aggregator.user(),       # detect end-of-turn + mute while speaking
            bridge,                  # sends turns to the agent server; speaks its replies as TTS frames
            tts,                     # reply text → speech
            speaking,                # tells the client when the bot starts/stops talking (barge-in)
            transport.output(),      # speaker (host device, or the browser's playback worklet)
            aggregator.assistant(),  # (context unused, kept so the pair is wired normally)
        ]
    )

    # Interruptions OFF + mute-while-speaking (above): on an open room mic the bot's own TTS would leak
    # back, get transcribed, and feed back. Use earbuds today; a browser client with echo cancellation can
    # barge in through the wire's `interrupt` instead (C3b).
    task = PipelineTask(
        pipeline,
        params=PipelineParams(audio_in_sample_rate=hearing.sample_rate, audio_out_sample_rate=profile.sample_rate),
        idle_timeout_secs=PIPELINE_IDLE_TIMEOUT_SECS,
        cancel_on_idle_timeout=False,
        cancel_runner_on_idle_timeout=False,
    )
    runner = PipelineRunner(handle_sigint=handle_sigint)

    listen_task = asyncio.create_task(listen())
    if ready is not None:
        ready.set()
    try:
        await runner.run(task)
    finally:
        stop.set()
        listen_task.cancel()


async def _run(settings: Settings, user: str = DEFAULT_USER, wake_word: Optional[str] = None,
               corpus: Optional["Corpus"] = None, profile: Optional[SpeechProfile] = None,
               hearing: Optional[HearingProfile] = None, serve_port: Optional[int] = None,
               mic: bool = True) -> None:
    """The process: the host-mic pipeline (unless `mic=False`), plus — with `serve_port` — the `/audio`
    listener that runs one more pipeline per browser connection (`conjure/transport.py`)."""
    profile = profile or profile_for(settings)
    hearing = hearing or hearing_for(settings, corpus=corpus is not None)
    tasks = []
    if mic:
        local = TRANSPORTS["local"].build(settings, in_rate=hearing.sample_rate, out_rate=profile.sample_rate)
        print(f"🎙️  Conjure voice is listening (agent server {settings.agent_url}; hearing through "
              f"{hearing.description}; speaking through {profile.description}). Speak to build the world; "
              f"say 'conjure open shell' then 'use <name>' / 'agent <name>' to switch. Ctrl+C to stop.")
        tasks.append(asyncio.create_task(run_pipeline(settings, user=user, transport=local, wake_word=wake_word,
                                                      corpus=corpus, profile=profile, hearing=hearing)))
    if serve_port:
        tasks.append(asyncio.create_task(serve(settings, port=serve_port, wake_word=wake_word, corpus=corpus,
                                               profile=profile, hearing=hearing)))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    for t in done:
        t.result()


def build_voice_app(settings: Settings, *, wake_word: Optional[str] = None, corpus: Optional["Corpus"] = None,
                    profile: Optional[SpeechProfile] = None, hearing: Optional[HearingProfile] = None,
                    pipeline=None):
    """The voice SERVER's app: `/audio?user=<name>` accepts a browser's socket (the headset page, relayed
    by the world server — docs/specs/voice.md §11) and runs one pipeline on it, as that user, until it
    closes. A headset is therefore one more voice client of the agent server, with its own registration,
    its own rendering of every reply and its own corpus gate — nothing there changes.

    `pipeline` is `run_pipeline` unless a test injects a stand-in (the real one loads the recognizer)."""
    from .transport import make_wire_serializer

    profile = profile or profile_for(settings)
    hearing = hearing or hearing_for(settings, corpus=corpus is not None)
    run = pipeline or run_pipeline
    app = FastAPI()
    live: dict[str, int] = {}
    app.state.live = live

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "connections": sum(live.values()), "stt": hearing.name, "tts": profile.name}

    @app.websocket("/audio")
    async def audio(websocket: WebSocket) -> None:
        await websocket.accept()
        user = websocket.query_params.get("user") or DEFAULT_USER
        serializer = make_wire_serializer()
        transport = TRANSPORTS["websocket"].build(settings, in_rate=hearing.sample_rate,
                                                  out_rate=profile.sample_rate, websocket=websocket,
                                                  serializer=serializer)
        # The headset's clips carry WHERE they came from as the corpus condition tag — the thing the
        # corpus exists to compare — instead of the host's PortAudio device, which heard none of it.
        link_corpus = None
        if corpus is not None:
            from .stt_corpus import Corpus as _Corpus
            link_corpus = _Corpus(corpus.root, device=f"browser:{user}")
        live[user] = live.get(user, 0) + 1
        print(f"[voice:{user}] browser connected ({sum(live.values())} live)")
        pipeline_task = None
        try:
            await websocket.send_json({"type": "ready", "out_rate": profile.sample_rate})
            ready = asyncio.Event()
            pipeline_task = asyncio.create_task(run(
                settings, user=user, transport=transport, wake_word=wake_word, corpus=link_corpus,
                profile=profile, hearing=hearing, handle_sigint=False, ready=ready, label=user))

            @transport.event_handler("on_client_disconnected")
            async def _gone(_t, _ws):
                if pipeline_task and not pipeline_task.done():
                    pipeline_task.cancel()

            await pipeline_task
        except (asyncio.CancelledError, WebSocketDisconnect):
            pass
        except Exception as exc:  # noqa: BLE001 — one headset's pipeline must not take the server down
            print(f"[voice:{user}] pipeline ended: {exc}", file=sys.stderr)
        finally:
            if pipeline_task and not pipeline_task.done():
                pipeline_task.cancel()
            live[user] -= 1
            if live[user] <= 0:
                live.pop(user, None)
            print(f"[voice:{user}] browser disconnected ({sum(live.values())} live)")

    return app


async def serve(settings: Settings, *, port: int, wake_word: Optional[str] = None,
                corpus: Optional["Corpus"] = None, profile: Optional[SpeechProfile] = None,
                hearing: Optional[HearingProfile] = None, host: str = "0.0.0.0") -> None:
    """Run `build_voice_app` under uvicorn, in this process's loop beside the host-mic pipeline."""
    import uvicorn

    app = build_voice_app(settings, wake_word=wake_word, corpus=corpus, profile=profile, hearing=hearing)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    print(f"🔊 Voice server listening on ws://{host}:{port}/audio (relayed by the world server at /audio)")
    await uvicorn.Server(config).serve()


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
    from urllib.parse import urlparse

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
    ap.add_argument("--serve", action="store_true",
                    help="also accept headset/browser audio on /audio (one pipeline per connection; the "
                         "world server relays the page's /audio here — CONJURE_VOICE_URL). The host mic "
                         "keeps running alongside unless --no-mic")
    ap.add_argument("--no-mic", action="store_true",
                    help="with --serve: don't open the host microphone/speaker at all — a headless voice "
                         "server for headsets only")
    # Not --capture-*: "capture" already means SPACE capture throughout this codebase, so a voice flag
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

    # Both engines are checked at the door — an unknown name, a missing key, or a corpus the recognizer
    # cannot tap is a one-line fix, and a pipeline that starts and then fails on the first utterance
    # hides which line.
    try:
        profile = profile_for(settings)
        hearing = hearing_for(settings, corpus=args.stt_corpus)
    except ValueError as exc:
        print(f"{exc}")
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
    serve_port = (urlparse(settings.voice_url).port or 8771) if args.serve else None
    if args.no_mic and not serve_port:
        print("--no-mic only makes sense with --serve (nothing would be listening).")
        return 1
    try:
        asyncio.run(_run(settings, args.user, args.wake_word, corpus, profile, hearing, serve_port,
                         mic=not args.no_mic))
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
