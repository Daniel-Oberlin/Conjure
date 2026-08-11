"""CLI REPL behaviour."""

from __future__ import annotations

from conjure.cli import _QUIT_WORDS, build_parser
from conjure.config import get_settings


def test_agent_flag_defaults_to_builder_and_is_captured():
    p = build_parser()
    assert p.parse_args(["say", "make a cat"]).agent == "builder"      # default
    assert p.parse_args(["--agent", "outdoor", "repl"]).agent == "outdoor"


def test_debug_registration_flag_defaults_off_and_reads_env(monkeypatch):
    monkeypatch.delenv("CONJURE_DEBUG_REGISTRATION", raising=False)
    assert get_settings().debug_registration is False              # opt-in, off by default
    monkeypatch.setenv("CONJURE_DEBUG_REGISTRATION", "1")
    assert get_settings().debug_registration is True
    monkeypatch.setenv("CONJURE_DEBUG_REGISTRATION", "off")
    assert get_settings().debug_registration is False              # unset/bogus values stay off


def _is_quit(line: str) -> bool:
    """Mirror the REPL's check (cmd_repl): whole-line, case-insensitive, trimmed."""
    return line.strip().lower() in _QUIT_WORDS


def test_repl_quits_on_bare_quit_exit_and_synonyms():
    for w in ("exit", "quit", "EXIT", "Quit", "q", ":q", ":quit", "bye", "goodbye", "  exit  "):
        assert _is_quit(w), w


def test_repl_passes_instructions_through_not_mistaken_for_quit():
    # whole-line match only — these are real instructions, not a quit
    for w in ("exit the room", "quit the game and start over", "make a cat", "exits", "q1", ""):
        assert not _is_quit(w), w
