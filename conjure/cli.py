"""Conjure CLI — the conversational client: talk to the agent server from the terminal, no mic.

    python -m conjure.cli                                    # interactive REPL (the usual way in)
    python -m conjure.cli say "put an oak tree in front of me"   # one-shot, then exit

Start the servers first — the world server (`python -m conjure`) and the agent server
(`python -m conjure.agent_server`), which holds the director, the LLM keys, and the shared transcript.
For deterministic world edits with no LLM in the loop, use `python -m conjure.ctl` instead.

The client is **dumb**: it opens one WebSocket, sends each line verbatim as `{type:"turn", text}`, and
renders what comes back. It parses nothing — the wake word, shell mode, and every command live
server-side in the shell (`conjure.shell`), so the CLI and voice can't drift apart. The one exception is
the quit words below, which end the *program* and never reach the server.

The conversation is shared: another user typing on their own CLI, or speaking on voice, shows up here
attributed to them, and the agent's replies are attributed to the agent by name.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

from .agent_client import (apply_context, prompt_from_context, render_event, render_parts,
                           status_from_context, ws_url)
from .config import CACHE_ROOT, DEFAULT_USER, Settings, get_settings

# Whole-line inputs that end the CLIENT (case-insensitive). Exact match only, so "exit the room" is
# still passed through. These quit the program in AGENT mode; in shell mode "exit" is a server command
# (leave shell), so it's forwarded — the only client-side special-case; ALL shell logic is server-side.
_QUIT_WORDS = {":q", ":quit", "q", "quit", "exit", "bye", "goodbye"}

# How long to wait for the first connection before telling the user the server looks down. The listener
# keeps retrying underneath, so this is a message, not a failure.
_CONNECT_GRACE = 2.0


def _agent_unreachable_msg(s: Settings, err: str) -> str:
    return (f"Agent server not reachable at {s.agent_url} ({err}).\n"
            f"Start it first:  python -m conjure.agent_server")


def _history_path(user: str) -> Path:
    """Where this user's REPL history lives. In the disposable cache root: losing it costs you arrow-key
    recall and nothing else, which doesn't earn a place in the precious data tree."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", user) or "anon"
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    return CACHE_ROOT / f"repl-history-{safe}"


# --------------------------------------------------------------------------- the connection
#
# One WebSocket to the agent server, shared by both front-ends below: the REPL drives it with a live
# prompt, `say` drives it for exactly one turn. Everything client-side that isn't rendering lives here.

class _Conversation:
    """One session with the agent server. Owns the socket (reconnecting when the server restarts), folds
    `context` events into `ctx` so the caller can render a live prompt, and counts our in-flight turns so
    a front-end can show that something is running.

    Deliberately has no idea what any line *means* — `send` ships text, `listen` hands every event to the
    caller. Interpretation is the server's job."""

    def __init__(self, s: Settings, user: str, *, backlog: bool = True):
        self._s, self._user, self._backlog = s, user, backlog
        self.ctx: dict = {"agent": "agent", "llm": "", "user": user, "in_shell": False}
        self.ws = None                                  # current socket (None while (re)connecting)
        self.connected = asyncio.Event()
        self._stop = asyncio.Event()
        # In-flight turns of OURS. A count, not a flag: `turn_done` is sent per-connection to the
        # submitter (agent_server `_on_turn`), so two quick submissions owe us two of them — a bare
        # boolean would clear on the first and under-report.
        self._inflight = 0
        self._since = 0.0                               # monotonic start of the oldest in-flight turn

    @property
    def working(self) -> Optional[float]:
        """Seconds the oldest in-flight turn has been running, or None if we're idle."""
        return (time.monotonic() - self._since) if self._inflight else None

    def stop(self) -> None:
        self._stop.set()

    async def listen(self, on_event, *, reconnect: bool = True) -> None:
        """Read events until stopped, dispatching each to `on_event(ev)`. Reconnects with a short backoff
        when the socket drops (the agent server restarting shouldn't kill the REPL); `reconnect=False`
        for a one-shot, where a drop means we're done."""
        import websockets

        url = ws_url(self._s.agent_url, self._user, backlog=self._backlog)
        first = True
        while not self._stop.is_set():
            try:
                async with websockets.connect(url) as ws:
                    self.ws = ws
                    self.connected.set()
                    if not first:
                        await on_event({"type": "notice", "text": "[reconnected]"})
                    first = False
                    async for raw in ws:
                        ev = json.loads(raw)
                        kind = ev.get("type")
                        if kind == "context":
                            apply_context(self.ctx, ev)
                        elif kind == "turn_done":
                            self._inflight = max(0, self._inflight - 1)
                        await on_event(ev)
                        if self._stop.is_set():
                            return
            except Exception:  # noqa: BLE001 — server down / restarting / socket dropped
                self.ws, self._inflight = None, 0
                self.connected.clear()
                if self._stop.is_set() or not reconnect:
                    return
                await asyncio.sleep(1.0)

    async def send(self, text: str) -> Optional[str]:
        """Submit one line verbatim. Returns None on success, or a message to show the user."""
        ws = self.ws
        if ws is None:
            return _agent_unreachable_msg(self._s, "not connected")
        if not self._inflight:
            self._since = time.monotonic()
        self._inflight += 1
        try:
            await ws.send(json.dumps({"type": "turn", "text": text}))
        except Exception as exc:  # noqa: BLE001
            self._inflight = max(0, self._inflight - 1)
            return _agent_unreachable_msg(self._s, str(exc))
        return None


# --------------------------------------------------------------------------- rendering
#
# `render_parts` decides WHAT to show (shared with voice); the styling below is the terminal's business.

def _style():
    from prompt_toolkit.styles import Style

    return Style.from_dict({
        "speaker.user":  "bold ansicyan",       # another person in the shared conversation
        "speaker.agent": "bold ansigreen",      # the agent, attributed by name like anyone else
        "body.interim":  "ansibrightblack",     # an intermediate message mid-turn (an ack, pre-tool narration)
        "notice":        "ansiyellow",          # the shell / this client talking, not a participant
        "tool":          "ansibrightblack",     # -v tool trace
        "statusbar":     "reverse",             # the pinned top bar — reverse video tracks any terminal theme
        "separator":     "ansibrightblack",     # the rule above the prompt
        "prompt":        "bold",
        "scrollmark":    "reverse",             # shown in the status bar when scrolled off the live tail
    })


def _fragments(ev: dict, *, me: str, verbose: bool, ctx: dict):
    """Style one event for the terminal, or None to print nothing."""
    from prompt_toolkit.formatted_text import FormattedText

    parts = render_parts(ev, me=me, verbose=verbose, agent=ctx.get("agent"))
    if parts is None:
        return None
    speaker, text = parts
    if speaker is None:                                 # not a participant: shell notice, tool trace, busy
        cls = "class:tool" if ev.get("type") == "tool_call" else "class:notice"
        return FormattedText([(cls, text)])
    kind = ev.get("type")
    is_agent = kind in ("assistant_delta", "assistant_final")
    # An `assistant_delta` is a whole intermediate message, not a token chunk (llm.py emits once per LLM
    # round-trip) — dim it so a multi-round turn reads as progress and the real answer stands out.
    body = "class:body.interim" if kind == "assistant_delta" else ""
    return FormattedText([("class:speaker.agent" if is_agent else "class:speaker.user", f"{speaker}: "),
                          (body, text)])


# --------------------------------------------------------------------------- REPL

_BANNER = ("Conjure REPL — thin client of the agent server (start it: 'python -m conjure.agent_server').",
           "Type an instruction. 'conjure open shell' for deterministic commands (LLM/agent, sessions, "
           "status). PgUp/PgDn scrolls, End returns to the live tail. 'exit'/^C/^D leaves.")

_SCROLLBACK = 2000        # lines of conversation kept in the pane; the whole list is re-read every repaint,
                          # so this bounds repaint cost. The full transcript lives on the server regardless.


class _Repl:
    """The full-screen client: a status bar pinned to the top, the conversation scrolling in the middle,
    and the prompt pinned to the bottom under a separator.

    This owns the screen (rather than printing into the terminal's scrollback) because the status bar has
    to stay put while output flows past it. The trade is that the terminal's own scrollback no longer
    applies to the conversation, so the pane does its own scrolling — PgUp/PgDn, and it sticks to the
    live tail until you scroll away from it."""

    def __init__(self, s: Settings, verbose: bool, user: str):
        self._s, self._verbose, self._user = s, verbose, user
        self.conv = _Conversation(s, user)
        self.lines: list = []                 # rendered conversation, one entry per printed line
        self._follow = True                   # stuck to the live tail? (False once you scroll up)
        self._view = 0                        # line to keep on screen while not following
        self._detached_at = 0                 # len(lines) when we left the tail → how much you've missed
        self._app = None

    # -- output ------------------------------------------------------------
    def add(self, frags) -> None:
        """Append one message, split into RENDERED lines.

        The split is load-bearing, not tidiness: agent replies are markdown and routinely carry embedded
        newlines (bullet lists, paragraphs). `FormattedTextControl` splits on them internally, so if a
        five-line reply were stored as one entry, `len(self.lines)` would undercount the real content and
        the cursor `_cursor()` reports — the thing that scrolls the pane — would point somewhere in the
        middle. The view then sticks there while new output piles up below the fold, which reads as the
        pane silently falling behind and never catching up."""
        from prompt_toolkit.formatted_text.utils import split_lines

        for line in split_lines(list(frags)):
            self.lines.append(list(line))
        if len(self.lines) > _SCROLLBACK:
            drop = len(self.lines) - _SCROLLBACK
            del self.lines[:drop]
            self._view = max(0, self._view - drop)
            self._detached_at = max(0, self._detached_at - drop)
        self.repaint()

    @property
    def unseen(self) -> int:
        """Lines that have arrived since you scrolled away from the tail."""
        return 0 if self._follow else max(0, len(self.lines) - self._detached_at)

    def follow_tail(self) -> None:
        self._follow = True
        self.repaint()

    def notice(self, text: str) -> None:
        self.add([("class:notice", text)])

    def repaint(self) -> None:
        if self._app is not None and self._app.is_running:
            self._app.invalidate()

    # -- the three panes ---------------------------------------------------
    def _output_fragments(self):
        out: list = []
        for i, line in enumerate(self.lines):
            if i:
                out.append(("", "\n"))
            out.extend(line)
        return out

    def _cursor(self):
        """Where the output Window should scroll to. FormattedTextControl has no real cursor, but a
        reported position is what the Window keeps visible — so pointing at the last line pins us to the
        tail, and pointing at `_view` holds position while scrolled back."""
        from prompt_toolkit.data_structures import Point

        last = max(0, len(self.lines) - 1)
        return Point(x=0, y=last if self._follow else min(self._view, last))

    def _status_fragments(self):
        width = self._app.output.get_size().columns if self._app is not None else None
        # Being scrolled away from the tail looks exactly like the app having frozen — new lines land
        # below the viewport and nothing moves. Say so loudly, count what's been missed, and name the key
        # that fixes it, because the usual way into this state is an accidental one-notch wheel scroll.
        if self._follow:
            mark = []
        else:
            n = self.unseen
            mark = [("class:scrollmark", f" ↓ {n} new · End " if n else " ↑ scrolled · End ")]
        reserve = sum(len(t) for _, t in mark) + 1
        text = status_from_context(self.conv.ctx, working=self.conv.working,
                                   width=(width - reserve) if width else None)
        pad = max(0, (width or len(text) + 1) - len(text) - reserve)
        return [("class:statusbar", " " + text + " " * pad)] + mark

    # -- input -------------------------------------------------------------
    def _submit(self, buf) -> bool:
        text = buf.text.strip()
        if not text:
            return False
        if not self.conv.ctx.get("in_shell") and text.lower() in _QUIT_WORDS:
            self._app.exit()
            return False
        # Echo it attributed by name, exactly as every other speaker appears — and exactly as the server
        # will replay this same turn in the backlog after a reconnect. (We echo locally because
        # `render_parts` suppresses our own LIVE `user_turn`, and because a shell command gets no
        # broadcast at all — only a notice back to us.)
        self.add([("class:speaker.user", f"{self._user}: "), ("", text)])
        self._follow = True                                    # submitting jumps back to the live tail
        asyncio.create_task(self._send(text))
        return False                                           # False → prompt_toolkit clears the buffer

    async def _send(self, text: str) -> None:
        err = await self.conv.send(text)
        if err:
            self.notice(err)
        self.repaint()

    def _scroll(self, delta: int) -> None:
        last = max(0, len(self.lines) - 1)
        start = last if self._follow else self._view
        target = max(0, min(last, start + delta))
        if self._follow and target < last:                     # leaving the tail — start counting misses
            self._detached_at = len(self.lines)
        self._follow = target >= last                          # scrolling back to the end re-attaches
        self._view = target
        self.repaint()

    # -- assembly ----------------------------------------------------------
    def build(self):
        from prompt_toolkit.application import Application
        from prompt_toolkit.buffer import Buffer
        from prompt_toolkit.filters import Condition
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys
        from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
        from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
        from prompt_toolkit.layout.dimension import Dimension

        # A single-line Buffer gives the whole line editor for free — emacs/vi keys, and Up/Down mapped to
        # history rather than cursor movement (prompt_toolkit's `auto_up`/`auto_down`).
        self.buffer = Buffer(history=FileHistory(str(_history_path(self._user))),
                             accept_handler=self._submit, multiline=False)

        keys = KeyBindings()

        @keys.add("c-c")
        def _(event):
            event.app.exit()

        # ^D leaves only on an EMPTY line; with text in the buffer the default binding takes it as
        # delete-char, which is what every other line editor does.
        @keys.add("c-d", filter=Condition(lambda: not self.buffer.text))
        def _(event):
            event.app.exit()

        def _page(event) -> int:
            # Half a page, not a whole one: in the alternate screen most terminals (and tmux) turn a
            # mouse-wheel notch into PgUp/PgDn, and a full page per notch makes an accidental brush of
            # the wheel throw the conversation far out of view.
            return max(1, (event.app.output.get_size().rows - 4) // 2)

        @keys.add("pageup")
        def _(event):
            self._scroll(-_page(event))

        @keys.add("pagedown")
        def _(event):
            self._scroll(_page(event))

        # Terminals that send real scroll sequences instead of paging keys.
        @keys.add(Keys.ScrollUp)
        def _(event):
            self._scroll(-3)

        @keys.add(Keys.ScrollDown)
        def _(event):
            self._scroll(3)

        # Back to the live tail. `end` is the discoverable one (and the status bar names it); the buffer
        # is a single line, so losing `end` as end-of-line costs nothing you can't do with `right`.
        @keys.add("end")
        @keys.add("escape", ">")
        def _(event):
            self.follow_tail()

        prompt_window = Window(FormattedTextControl(
            lambda: [("class:prompt", prompt_from_context(self.conv.ctx))]),
            dont_extend_width=True, height=1)

        layout = Layout(HSplit([
            Window(FormattedTextControl(self._status_fragments), height=1, style="class:statusbar"),
            # `Dimension(weight=1)` (preferred 0, unbounded max) is what pins the prompt to the bottom:
            # a FormattedTextControl otherwise prefers exactly its content height, so a short conversation
            # would let the separator and prompt ride up under it with dead space below.
            Window(FormattedTextControl(self._output_fragments, get_cursor_position=self._cursor,
                                        show_cursor=False),
                   wrap_lines=True, height=Dimension(weight=1)),
            Window(height=1, char="─", style="class:separator"),
            VSplit([prompt_window, Window(BufferControl(self.buffer))], height=1),
        ]), focused_element=self.buffer)

        # `refresh_interval` only has to drive the status bar's elapsed-seconds clock; everything else
        # repaints on demand via `repaint()`.
        self._app = Application(layout=layout, key_bindings=keys, style=_style(),
                                full_screen=True, refresh_interval=1.0)
        return self._app

    # -- run ---------------------------------------------------------------
    async def on_event(self, ev: dict) -> None:
        """One event from the server → a line in the pane, or just a repaint. `context`/`turn_done`
        render to nothing but still move the status bar (turn counts, context size, the working clock),
        so they repaint rather than falling through."""
        frags = _fragments(ev, me=self._user, verbose=self._verbose, ctx=self.conv.ctx)
        if frags is not None:
            self.add(frags)
        else:
            self.repaint()

    async def run(self) -> None:
        on_event = self.on_event
        for line in _BANNER:
            self.add([("class:notice", line)])
        listener = asyncio.create_task(self.conv.listen(on_event))
        app = self.build()
        try:
            await asyncio.wait_for(self.conv.connected.wait(), timeout=_CONNECT_GRACE)
        except asyncio.TimeoutError:
            self.notice(_agent_unreachable_msg(self._s, "no response"))   # the listener keeps retrying
        try:
            await app.run_async()
        finally:
            self.conv.stop()
            listener.cancel()


async def _repl(s: Settings, verbose: bool, user: str) -> None:
    await _Repl(s, verbose, user).run()


# --------------------------------------------------------------------------- one-shot

async def _say(s: Settings, verbose: bool, user: str, text: str) -> None:
    """Connect, submit `text`, print only THIS turn's output, exit. Skips the replayed backlog by waiting
    for our own turn to begin — an utterance echoes our `user_turn`; a command replies with a `notice`.
    Plain text, not styled: `say` is the scriptable path, and its output is usually piped."""
    conv = _Conversation(s, user)
    state = {"sent": False, "started": False}

    def emit(ev: dict) -> None:
        out = render_event(ev, me=user, verbose=verbose, agent=conv.ctx.get("agent"))
        if out is not None:
            print(out)

    async def on_event(ev: dict) -> None:
        t = ev.get("type")
        if t == "context" and not state["sent"]:            # subscribed → submit once
            state["sent"] = True
            err = await conv.send(text)
            if err:
                print(err)
                conv.stop()
            return
        if not state["started"]:                            # skip replayed backlog until our turn begins
            if t == "user_turn" and ev.get("speaker") == user and ev.get("text") == text:
                state["started"] = True
            elif t in ("notice", "busy"):                   # command reply / rejection → done
                emit(ev)
                conv.stop()
            elif t == "turn_done":                          # a turn with no textual output → just stop
                conv.stop()
            return
        emit(ev)
        if t == "turn_done":                                # the definitive end (covers tool-only turns)
            conv.stop()

    await conv.listen(on_event, reconnect=False)
    if not state["sent"]:
        print(_agent_unreachable_msg(s, "no response"))


# --------------------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="conjure-cli",
        description="Talk to the Conjure agent server from the terminal.",
        epilog="For direct, LLM-free world edits, see `python -m conjure.ctl`.")
    p.add_argument("-v", "--verbose", action="store_true", help="show tool calls and library logs")
    p.add_argument("--user", default=DEFAULT_USER, help="who you connect as (owns spaces/worlds/assets)")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("say", help="run one text instruction through the agent, then exit")
    a.set_defaults(fn=_say)
    a.add_argument("text", nargs="+")

    sub.add_parser("repl", help="interactive REPL (the default with no subcommand)").set_defaults(fn=_repl)
    return p


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    for name in ("httpx", "anthropic", "mcp", "google_genai", "websockets"):
        logging.getLogger(name).setLevel(logging.INFO if args.verbose else logging.WARNING)

    settings = get_settings()
    fn = getattr(args, "fn", None) or _repl                 # no subcommand → the REPL
    # The REPL prints no banner here: it takes over the screen, so its greeting is the first lines of the
    # conversation pane instead (`_BANNER`).
    coro = (_say(settings, args.verbose, args.user, " ".join(args.text).strip()) if fn is _say
            else _repl(settings, args.verbose, args.user))

    try:
        asyncio.run(coro)
    except KeyboardInterrupt:                               # ^C outside the prompt (e.g. during connect)
        print()
        return 130
    except RuntimeError as exc:                             # e.g. no LLM keys for the director
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
