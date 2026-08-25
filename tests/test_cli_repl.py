"""The full-screen REPL (`cli._Repl`) — status bar on top, conversation in the middle, prompt pinned
to the bottom.

These drive the real object rather than scraping a rendered screen: prompt_toolkit's renderer writes
only the cells that CHANGED and runs on the alternate screen buffer, so screen-scraping in-process is
unreliable (a terminal emulator replaying the stream sees pre- and post-exit output on one grid). The
one thing that genuinely needs a rendered terminal — that the layout puts the panes in the right place
— is covered by `test_layout_pins_the_status_bar_on_top_and_the_prompt_on_the_bottom`, which inspects
the built layout instead."""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest

from conjure import cli
from conjure.config import get_settings

_CTX = {"type": "context", "agent": "builder", "llm": "Claude", "user": "daniel", "in_shell": False,
        "stats": {"turns": 12, "cap": 40,
                  "chars": {"prompt": 10152, "room": 10589, "tools": 33492, "history": 568}}}


@pytest.fixture
def fake_ws(monkeypatch):
    """Stand in for the agent server; `sent` records submissions."""
    box = types.SimpleNamespace(events=[_CTX], sent=[], gap=0.02)

    class WS:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def __aiter__(self):
            for ev in box.events:
                await asyncio.sleep(box.gap)
                yield json.dumps(ev)
            await asyncio.Event().wait()

        async def send(self, raw):
            box.sent.append(json.loads(raw))

    monkeypatch.setitem(sys.modules, "websockets", types.SimpleNamespace(connect=lambda url: WS()))
    return box


class _Socket:
    """A connected socket for `_Conversation.send` to write to (it needs an awaitable `send`)."""

    def __init__(self, box):
        self._box = box

    async def send(self, raw):
        self._box.sent.append(json.loads(raw))


def _repl(user: str = "daniel", verbose: bool = False) -> cli._Repl:
    r = cli._Repl(get_settings(), verbose, user)
    r.conv.ctx.update({k: v for k, v in _CTX.items() if k != "type"})
    return r


def _text(frags) -> str:
    return "".join(t for _, t in frags)


def _key(k) -> str:
    """A binding key as its wire name ('pageup', '<scroll-up>') — the enum str()s as 'Keys.PageUp'."""
    return getattr(k, "value", k)


def _pane(r: cli._Repl) -> list:
    """The conversation pane as plain lines."""
    return [_text(line) for line in r.lines]


# --------------------------------------------------------------------------- the pane

async def test_events_become_attributed_lines_in_the_pane(fake_ws):
    r = _repl()
    for ev in ({"type": "user_turn", "speaker": "bob", "text": "hang a painting"},
               {"type": "assistant_delta", "text": "On it."},
               {"type": "assistant_final", "text": "Done."},
               {"type": "notice", "text": "Now talking to Gemini (builder)."}):
        await r.on_event(ev)
    assert _pane(r) == ["bob: hang a painting", "builder: On it.", "builder: Done.",
                        "Now talking to Gemini (builder)."]


async def test_control_events_add_no_line_but_still_repaint(fake_ws):
    r = _repl()
    await r.on_event({"type": "context", "agent": "builder"})
    await r.on_event({"type": "turn_done"})
    assert r.lines == []                       # nothing printed — but the status bar depends on both,
                                               # so `on_event` must not skip them (it calls repaint)


async def test_tool_traces_are_verbose_only(fake_ws):
    ev = {"type": "tool_call", "name": "place_asset", "args": {"query": "cat"}}
    quiet, loud = _repl(), _repl(verbose=True)
    await quiet.on_event(ev)
    await loud.on_event(ev)
    assert quiet.lines == []
    assert "place_asset" in _pane(loud)[0]


def test_pane_is_bounded_so_a_long_session_stays_cheap_to_repaint():
    r = _repl()
    for i in range(cli._SCROLLBACK + 50):
        r.add([("", f"line {i}")])
    assert len(r.lines) == cli._SCROLLBACK
    assert _pane(r)[0] == "line 50"            # oldest dropped, newest kept
    assert _pane(r)[-1] == f"line {cli._SCROLLBACK + 49}"


# --------------------------------------------------------------------------- submitting

def test_submitting_echoes_the_line_and_sends_it_verbatim(fake_ws):
    r = _repl()
    r.conv.ws = _Socket(fake_ws)

    async def go():
        r.build()
        r.buffer.text = "conjure open shell"
        assert r._submit(r.buffer) is False    # False → prompt_toolkit clears the buffer
        await asyncio.sleep(0.05)              # let the send task run

    asyncio.run(go())
    assert fake_ws.sent == [{"type": "turn", "text": "conjure open shell"}]
    # Echoed attributed by NAME, the same way every other speaker reads — and the same way the server
    # replays this turn in the backlog, so a reconnect doesn't re-label your own history.
    assert _pane(r)[-1] == "daniel: conjure open shell"
    assert any("class:speaker.user" in style for style, _ in r.lines[-1])


def test_blank_input_is_not_submitted(fake_ws):
    r = _repl()

    async def go():
        r.build()
        for blank in ("", "   ", "\t"):
            r.buffer.text = blank
            assert r._submit(r.buffer) is False

    asyncio.run(go())
    assert fake_ws.sent == []
    assert r.lines == []


def test_quit_words_exit_in_agent_mode_but_are_forwarded_in_shell_mode(fake_ws):
    r = _repl()
    r.conv.ws = _Socket(fake_ws)

    async def go():
        app = r.build()
        exited = {"n": 0}
        app.exit = lambda *a, **k: exited.__setitem__("n", exited["n"] + 1)

        r.buffer.text = "exit"                                  # agent mode → quits the client
        r._submit(r.buffer)
        await asyncio.sleep(0.05)
        assert exited["n"] == 1 and fake_ws.sent == []

        r.conv.ctx["in_shell"] = True                           # shell mode → 'exit' is a SERVER command
        r.buffer.text = "exit"
        r._submit(r.buffer)
        await asyncio.sleep(0.05)
        assert exited["n"] == 1
        assert fake_ws.sent == [{"type": "turn", "text": "exit"}]

    asyncio.run(go())


def test_a_failed_send_is_reported_in_the_pane(fake_ws):
    r = _repl()
    assert r.conv.ws is None                                    # never connected

    async def go():
        r.build()
        r.buffer.text = "make a cat"
        r._submit(r.buffer)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert any("not reachable" in line for line in _pane(r))


# --------------------------------------------------------------------------- scrolling

def test_pane_sticks_to_the_live_tail_until_you_scroll_away():
    r = _repl()
    for i in range(100):
        r.add([("", f"line {i}")])
    assert r._follow and r._cursor().y == 99                    # pinned to the newest line

    r._scroll(-30)
    assert not r._follow and r._cursor().y == 69                # held where you left it
    r.add([("", "new line while scrolled back")])
    assert r._cursor().y == 69, "incoming output must not yank you back to the bottom"

    r._scroll(+1000)                                            # scrolling to the end re-follows
    assert r._follow and r._cursor().y == len(r.lines) - 1


def test_submitting_snaps_back_to_the_live_tail(fake_ws):
    r = _repl()
    r.conv.ws = _Socket(fake_ws)
    for i in range(50):
        r.add([("", f"line {i}")])
    r._scroll(-20)
    assert not r._follow

    async def go():
        r.build()
        r.buffer.text = "hello"
        r._submit(r.buffer)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    assert r._follow, "sending something should put you back where the reply will appear"


def test_scrolling_is_clamped_to_the_pane():
    r = _repl()
    for i in range(10):
        r.add([("", f"line {i}")])
    r._scroll(-9999)
    assert r._cursor().y == 0
    r._scroll(+9999)
    assert r._cursor().y == 9 and r._follow


def test_trimming_the_scrollback_keeps_the_scroll_position_pointing_at_the_same_text():
    r = _repl()
    for i in range(cli._SCROLLBACK):
        r.add([("", f"line {i}")])
    r._scroll(-100)
    at = _pane(r)[r._cursor().y]
    r.add([("", "one more, pushing the oldest line out")])
    assert _pane(r)[r._cursor().y] == at, "trimming must not slide the view onto different content"


# --------------------------------------------------------------------------- layout + status bar

def test_layout_pins_the_status_bar_on_top_and_the_prompt_on_the_bottom():
    from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl

    r = _repl()
    app = r.build()
    assert app.full_screen
    status, output, separator, prompt_row = app.layout.container.children

    assert isinstance(status.content, FormattedTextControl) and status.height == 1
    # The output pane must be the only child that grows, or the prompt rides up under a short
    # conversation instead of staying on the bottom line.
    assert output.height.weight == 1 and output.height.preferred == 0
    assert separator.height == 1
    assert isinstance(prompt_row.children[1].content, BufferControl)   # prompt label + input, one row


def test_status_bar_reports_turns_and_the_context_breakdown():
    r = _repl()
    bar = "".join(t for _, t in r._status_fragments())
    assert "builder·claude" in bar
    assert "12/40 turns" in bar
    assert "54.8k chars" in bar                                  # 10152+10589+33492+568
    assert "tools 33.5k (61%)" in bar                            # the biggest slice, and the least visible


def test_status_bar_shows_the_working_clock_only_while_a_turn_is_ours_and_running(fake_ws):
    r = _repl()
    assert "working" not in "".join(t for _, t in r._status_fragments())
    r.conv.ws = _Socket(fake_ws)
    asyncio.run(r.conv.send("make a cat"))
    assert "working" in "".join(t for _, t in r._status_fragments())


def test_status_bar_flags_that_you_have_scrolled_off_the_live_tail():
    r = _repl()
    for i in range(50):
        r.add([("", f"line {i}")])
    assert "scrolled" not in "".join(t for _, t in r._status_fragments())
    r._scroll(-10)
    assert "scrolled" in "".join(t for _, t in r._status_fragments())


# --------------------------------------------------------------------------- the one-shot

async def test_say_prints_only_this_turn_skipping_the_replayed_backlog(fake_ws, capsys):
    fake_ws.events = [
        {"type": "user_turn", "speaker": "daniel", "text": "old question", "backlog": True},
        {"type": "assistant_final", "text": "old answer", "backlog": True},
        _CTX,
        {"type": "user_turn", "speaker": "daniel", "text": "make a cat"},   # our turn begins
        {"type": "tool_call", "name": "place_asset", "args": {"query": "cat"}},
        {"type": "assistant_final", "text": "Placed a cat."},
        {"type": "turn_done"},
        {"type": "assistant_final", "text": "AFTER THE END"},
    ]
    await asyncio.wait_for(cli._say(get_settings(), False, "daniel", "make a cat"), timeout=10)
    out = capsys.readouterr().out
    assert fake_ws.sent == [{"type": "turn", "text": "make a cat"}]
    assert out == "builder: Placed a cat.\n"            # backlog, our own echo, the trace and the
                                                        # post-turn_done line are all suppressed


async def test_say_reports_a_command_reply_and_stops(fake_ws, capsys):
    fake_ws.events = [_CTX, {"type": "notice", "text": "Now talking to Gemini (builder)."}]
    await asyncio.wait_for(cli._say(get_settings(), False, "daniel", "conjure use gemini"), timeout=10)
    assert capsys.readouterr().out == "Now talking to Gemini (builder).\n"


# --------------------------------------------------------------------------- getting back to the tail
#
# Scrolling away and never coming back is indistinguishable from the app freezing: new lines land below
# the viewport and nothing moves. The usual way in is an accidental wheel notch — in the alternate
# screen most terminals (and tmux) send PgUp for one — so detaching has to be visible and reversible.

def test_scrolling_away_counts_what_you_are_missing():
    r = _repl()
    for i in range(60):
        r.add([("", f"line {i}")])
    assert r.unseen == 0                                   # attached → nothing missed by definition

    r._scroll(-10)
    assert r.unseen == 0                                   # detached, but nothing new has arrived yet
    for i in range(7):
        r.add([("", f"new {i}")])
    assert r.unseen == 7


def test_the_status_bar_names_the_key_that_gets_you_back():
    r = _repl()
    for i in range(60):
        r.add([("", f"line {i}")])
    r._scroll(-10)
    assert "↑ scrolled · End" in "".join(t for _, t in r._status_fragments())
    for i in range(3):
        r.add([("", f"new {i}")])
    bar = "".join(t for _, t in r._status_fragments())
    assert "↓ 3 new · End" in bar                          # a count, so a stalled pane can't read as frozen


def test_follow_tail_reattaches_and_clears_the_count():
    r = _repl()
    for i in range(60):
        r.add([("", f"line {i}")])
    r._scroll(-20)
    r.add([("", "arrived while away")])
    assert not r._follow and r.unseen == 1

    r.follow_tail()
    assert r._follow and r.unseen == 0
    assert r._cursor().y == len(r.lines) - 1
    assert "End" not in "".join(t for _, t in r._status_fragments())


def test_end_and_the_scroll_keys_are_bound():
    r = _repl()
    app = r.build()
    bound = {tuple(_key(k) for k in b.keys) for b in app.key_bindings.bindings}
    assert ("end",) in bound                               # the key the status bar advertises
    assert ("<scroll-up>",) in bound and ("<scroll-down>",) in bound
    assert ("pageup",) in bound and ("pagedown",) in bound


def test_paging_moves_half_a_screen_not_a_whole_one():
    # One wheel notch often arrives as PgUp; a full page per notch throws the conversation far away.
    r = _repl()
    for i in range(200):
        r.add([("", f"line {i}")])
    app = r.build()
    rows = app.output.get_size().rows
    before = r._cursor().y
    for binding in app.key_bindings.bindings:
        if tuple(_key(k) for k in binding.keys) == ("pageup",):
            binding.call(types.SimpleNamespace(app=app, current_buffer=r.buffer))
            break
    moved = before - r._cursor().y
    assert 0 < moved <= max(1, (rows - 4) // 2)


# --------------------------------------------------------------------------- multi-line messages
#
# Regression: agent replies are markdown and routinely contain embedded newlines. Storing one such
# reply as a single `lines` entry made `len(self.lines)` undercount the content that
# FormattedTextControl actually renders, so the cursor `_cursor()` reports pointed into the MIDDLE of
# the pane. The view stuck there while new output piled up below the fold — the pane silently fell
# further and further behind and never caught up.

async def test_a_multiline_reply_becomes_one_pane_line_per_rendered_line(fake_ws):
    r = _repl()
    await r.on_event({"type": "assistant_final",
                      "text": "Here are your worlds:\n- **home**\n- **session-1**\n\nSwitch to either?"})
    assert _pane(r) == ["builder: Here are your worlds:", "- **home**", "- **session-1**", "",
                        "Switch to either?"]


async def test_pane_line_count_matches_what_gets_rendered(fake_ws):
    """The invariant the bug broke: one `lines` entry per rendered line, so the cursor index is truthful."""
    r = _repl()
    for i in range(20):
        await r.on_event({"type": "assistant_final", "text": f"msg {i}\nbullet a\nbullet b"})
    assert len(r.lines) == 60                                   # 20 replies × 3 rendered lines

    # `_output_fragments` is what the control renders; it must contain exactly one newline per line gap.
    newlines = sum(t.count("\n") for _, t in r._output_fragments())
    assert newlines == len(r.lines) - 1
    assert r._cursor().y == len(r.lines) - 1                    # …so following the tail really is the tail


async def test_following_stays_on_the_newest_line_through_multiline_traffic(fake_ws):
    r = _repl()
    for i in range(40):
        await r.on_event({"type": "assistant_final", "text": f"MSG-{i}\n- a\n- b\n\nEND-{i}"})
        assert r._follow
        assert _pane(r)[r._cursor().y] == f"END-{i}", "the cursor must land on the newest rendered line"


def test_your_own_echo_matches_how_the_backlog_will_replay_it(fake_ws):
    """Your line should read the same before and after a reconnect. It used to be echoed with the shell
    prompt (`conjure:daniel.builder.claude> …`) while the server's replay used `daniel: …`, so your own
    history changed shape underneath you."""
    r = _repl()
    r.conv.ws = _Socket(fake_ws)

    async def go():
        r.build()
        r.buffer.text = "put an oak tree in front of me"
        r._submit(r.buffer)
        await asyncio.sleep(0.05)

    asyncio.run(go())
    live_echo = _pane(r)[-1]

    # what the server sends back for the same turn on reconnect (agent_server `_turn_to_event`)
    replayed = cli._fragments({"type": "user_turn", "speaker": "daniel",
                               "text": "put an oak tree in front of me", "backlog": True},
                              verbose=False, ctx=r.conv.ctx)
    assert live_echo == _text(replayed) == "daniel: put an oak tree in front of me"

    # …and the same line spoken into OUR OWN voice client, arriving live from the other connection, reads
    # identically — it's unmarked, so nothing mistakes it for this client's echo and drops it.
    spoken = cli._fragments({"type": "user_turn", "speaker": "daniel",
                             "text": "put an oak tree in front of me"},
                            verbose=False, ctx=r.conv.ctx)
    assert _text(spoken) == "daniel: put an oak tree in front of me"
