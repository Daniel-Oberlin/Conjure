"""CLI REPL behaviour — the conversational client (`conjure.cli`). The direct world-server commands
live in `conjure.ctl` and are covered by test_ctl.py."""

from __future__ import annotations

import asyncio
import json

import pytest

from conjure.cli import _QUIT_WORDS, _Conversation, _history_path, build_parser
from conjure.config import get_settings


def test_parser_exposes_say_and_repl_and_defaults_to_the_repl():
    p = build_parser()
    assert p.parse_args(["say", "make", "a", "cat"]).text == ["make", "a", "cat"]
    assert getattr(p.parse_args([]), "fn", None) is None       # no subcommand → main() picks the REPL
    assert p.parse_args(["--user", "alice", "repl"]).user == "alice"


def test_parser_no_longer_takes_a_dead_agent_flag():
    # The agent server owns agent selection; switching is a shared-effect verb, so it's a server command
    # (`conjure agent <name>`), not a launch flag that silently moved everyone.
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--agent", "outdoor", "repl"])


def test_debug_registration_flag_defaults_off_and_reads_env(monkeypatch):
    monkeypatch.delenv("CONJURE_DEBUG_REGISTRATION", raising=False)
    assert get_settings().debug_registration is False              # opt-in, off by default
    monkeypatch.setenv("CONJURE_DEBUG_REGISTRATION", "1")
    assert get_settings().debug_registration is True
    monkeypatch.setenv("CONJURE_DEBUG_REGISTRATION", "off")
    assert get_settings().debug_registration is False              # unset/bogus values stay off


def _is_quit(line: str) -> bool:
    """Mirror the REPL's check: whole-line, case-insensitive, trimmed."""
    return line.strip().lower() in _QUIT_WORDS


def test_repl_quits_on_bare_quit_exit_and_synonyms():
    for w in ("exit", "quit", "EXIT", "Quit", "q", ":q", ":quit", "bye", "goodbye", "  exit  "):
        assert _is_quit(w), w


def test_repl_passes_instructions_through_not_mistaken_for_quit():
    # whole-line match only — these are real instructions, not a quit
    for w in ("exit the room", "quit the game and start over", "make a cat", "exits", "q1", ""):
        assert not _is_quit(w), w


def test_history_file_is_per_user_and_name_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("conjure.cli.CACHE_ROOT", tmp_path)
    assert _history_path("alice").name == "repl-history-alice"
    assert _history_path("bob").name == "repl-history-bob"
    # a user string can't escape the cache dir or smuggle a separator into the filename
    assert _history_path("../../etc/passwd").parent == tmp_path
    assert "/" not in _history_path("../../etc/passwd").name


# --------------------------------------------------------------------------- _Conversation

class _FakeWS:
    """Records what the client sends. The client is dumb, so 'what it sent' is the whole contract."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _conv(user: str = "alice") -> _Conversation:
    return _Conversation(get_settings(), user)


def test_send_ships_the_line_verbatim_without_parsing_it():
    conv = _conv()
    conv.ws = ws = _FakeWS()
    # A quit-looking word, a wake-word command and a plain utterance all go out unchanged: the client
    # never interprets (shell mode, the wake word and dispatch are all server-side).
    for line in ("conjure open shell", "exit", "put a tree in front of me"):
        assert asyncio.run(conv.send(line)) is None
    assert ws.sent == [{"type": "turn", "text": line} for line in
                       ("conjure open shell", "exit", "put a tree in front of me")]


def test_send_without_a_socket_reports_instead_of_raising():
    conv = _conv()
    assert conv.ws is None
    err = asyncio.run(conv.send("hello"))
    assert err and "not reachable" in err
    assert conv.working is None                       # a failed send must not leave us stuck "working"


def test_working_counts_turns_so_two_submissions_need_two_turn_dones():
    conv = _conv()
    conv.ws = _FakeWS()
    assert conv.working is None
    asyncio.run(conv.send("one"))
    asyncio.run(conv.send("two"))
    assert conv.working is not None
    conv._inflight -= 1                               # first turn_done
    assert conv.working is not None, "a bare flag would have cleared here and under-reported"
    conv._inflight -= 1                               # second turn_done
    assert conv.working is None
