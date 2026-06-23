"""CLI REPL behaviour."""

from __future__ import annotations

from conjure.cli import _QUIT_WORDS


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
