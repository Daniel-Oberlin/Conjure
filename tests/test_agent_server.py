"""Agent server (shared-session Step C1) — the HTTP/SSE host of the shared Shell/Director.

The turn logic (`_run_turn`) and the fan-out (`Hub`) are exercised directly with a fake shell — no
network, no MCP subprocess, no LLM. The endpoints are covered with a TestClient over an injected fake
shell (build_app's `shell=` bypasses the real Shell.session/lifespan)."""

import types

from conjure.agent_server import Hub, _backlog_events, _context_event, _run_turn, build_app
from conjure.config import get_settings
from conjure.llm import Turn


# --------------------------------------------------------------------------- fakes

class FakeDir:
    def __init__(self):
        self.agent = types.SimpleNamespace(name="builder")
        self.active = "Claude"
        self.transcript: list[Turn] = []


class FakeShell:
    """Just what the agent server reads/drives: _as_command routing, feed, and a director with a
    transcript. feed mimics the real split — an utterance emits ack+tool+final and records the transcript;
    a command emits a single reply."""

    def __init__(self):
        self.director = FakeDir()
        self._user = "daniel"
        self.in_shell = False
        self.fed: list[tuple] = []

    def _as_command(self, text):
        s = text.strip()
        if self.in_shell:
            return s
        if s.lower().startswith("conjure"):
            return s[len("conjure"):].strip(" ,:") or "open shell"
        return None

    async def feed(self, text, *, speaker=None, on_text=None, on_tool=None):
        self.fed.append((speaker, text))
        if self._as_command(text) is None:                       # an utterance
            if on_text:
                await on_text("on it", final=False, speaker=self.director.active)
            if on_tool:
                await on_tool("place_asset", {"query": "tree"})
            if on_text:
                await on_text("done — a tree", final=True, speaker=self.director.active)
            self.director.transcript.append(Turn("user", text.strip(), by=speaker or self._user))
            self.director.transcript.append(Turn("assistant", "done — a tree"))
        else:                                                    # a deterministic command
            if on_text:
                await on_text("Now talking to Gemini (builder).", final=True, speaker=self.director.active)


def _app_like(shell):
    return types.SimpleNamespace(state=types.SimpleNamespace(shell=shell, hub=Hub(), turn_active=True))


def _drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# --------------------------------------------------------------------------- Hub

async def test_hub_fans_out_to_all_and_unsubscribe_stops_delivery():
    hub = Hub()
    a, b = hub.subscribe(), hub.subscribe()
    await hub.publish({"type": "x"})
    assert a.get_nowait() == {"type": "x"} and b.get_nowait() == {"type": "x"}
    hub.unsubscribe(a)
    await hub.publish({"type": "y"})
    assert b.get_nowait() == {"type": "y"} and a.empty()      # unsubscribed no longer receives


# --------------------------------------------------------------------------- _run_turn

async def test_run_turn_utterance_emits_full_event_sequence_and_clears_floor():
    shell = FakeShell()
    app = _app_like(shell)
    q = app.state.hub.subscribe()
    await _run_turn(app, "alice", "put a tree here")
    events = _drain(q)
    assert [e["type"] for e in events] == \
        ["user_turn", "assistant_delta", "tool_call", "assistant_final", "turn_done"]
    assert events[0] == {"type": "user_turn", "speaker": "alice", "text": "put a tree here"}
    assert events[1]["text"] == "on it" and events[1]["llm"] == "Claude"       # delta tagged with the LLM
    assert events[2] == {"type": "tool_call", "name": "place_asset", "args": {"query": "tree"}}
    assert events[3] == {"type": "assistant_final", "text": "done — a tree", "llm": "Claude"}
    assert events[4] == {"type": "turn_done", "speaker": "alice"}   # unambiguous end-of-turn (floor free)
    assert shell.fed == [("alice", "put a tree here")]        # speaker threaded through to feed
    assert app.state.turn_active is False                     # floor released in finally


async def test_run_turn_command_emits_notice_and_context_but_no_user_turn():
    shell = FakeShell()
    app = _app_like(shell)
    q = app.state.hub.subscribe()
    await _run_turn(app, "alice", "conjure use gemini")
    events = _drain(q)
    assert [e["type"] for e in events] == ["notice", "context", "turn_done"]  # reply, refreshed prompt, end
    assert events[0]["text"].startswith("Now talking to Gemini")
    assert events[1] == _context_event(shell)                 # agent/llm/user snapshot
    assert app.state.turn_active is False


async def test_run_turn_error_is_reported_and_never_strands_the_floor():
    class BoomShell(FakeShell):
        async def feed(self, *a, **k):
            raise RuntimeError("boom")

    app = _app_like(BoomShell())
    q = app.state.hub.subscribe()
    await _run_turn(app, "alice", "put a tree")               # an utterance → user_turn, then feed blows up
    events = _drain(q)
    assert events[0]["type"] == "user_turn"
    assert any(e["type"] == "notice" and "boom" in e["text"] for e in events)
    assert events[-1]["type"] == "turn_done"                  # end-of-turn fires even on error
    assert app.state.turn_active is False                     # floor released despite the exception


# --------------------------------------------------------------------------- endpoints

def _client(shell=None):
    from fastapi.testclient import TestClient
    return TestClient(build_app(get_settings(), shell=shell or FakeShell()))


def test_post_turn_accepted_when_idle():
    with _client() as client:
        r = client.post("/turn", json={"speaker": "alice", "text": "put a tree"})
        assert r.status_code == 200 and r.json()["accepted"] is True


def test_post_turn_rejected_with_busy_while_a_turn_is_in_flight():
    with _client() as client:
        client.app.state.turn_active = True                   # simulate an in-flight turn (single floor)
        r = client.post("/turn", json={"speaker": "bob", "text": "hi"})
        assert r.status_code == 409 and r.json()["busy"] is True


def test_stream_backlog_is_context_then_the_transcript():
    # The snapshot a late joiner gets before the live feed. Tested directly (the live SSE loop is a thin
    # wrapper around this + the Hub, both covered above; the socket itself is smoke-tested by hand).
    shell = FakeShell()
    shell.director.transcript = [Turn("user", "hi", by="alice"), Turn("assistant", "hello")]
    events = _backlog_events(shell)
    assert events[0]["type"] == "context" and events[0]["agent"] == "builder" and events[0]["llm"] == "Claude"
    assert events[1] == {"type": "user_turn", "speaker": "alice", "text": "hi"}
    assert events[2] == {"type": "assistant_final", "text": "hello"}
