"""Wake-word gate for the voice loop (conjure.voice._make_wake_gate)."""

from __future__ import annotations

from conjure.voice import _make_wake_gate


def test_no_wake_word_passes_everything_through():
    gate = _make_wake_gate(None)
    assert gate("make a cat") == "make a cat"
    assert _make_wake_gate("")("hello") == "hello"       # blank wake word ⇒ passthrough


def test_wake_word_and_command_in_one_breath():
    gate = _make_wake_gate("conjure")
    assert gate("conjure make a cat") == "make a cat"    # strips the wake word, sends the command
    assert gate("Conjure, place a tree") == "place a tree"   # case-insensitive + leading punctuation
    assert gate("um conjure: paint the walls blue") == "paint the walls blue"  # wake word mid-utterance


def test_ignores_utterances_without_the_wake_word():
    gate = _make_wake_gate("conjure")
    assert gate("make a cat") is None                    # no wake word → ignored
    assert gate("what's the weather") is None


def test_bare_wake_word_arms_the_next_utterance_then_re_waits():
    gate = _make_wake_gate("conjure")
    assert gate("conjure") is None                       # bare wake word → arm (nothing to run yet)
    assert gate("make a dragon") == "make a dragon"      # armed → next utterance runs in full
    assert gate("and a castle") is None                  # re-waits: not armed, no wake word → ignored
    assert gate("conjure add a lake") == "add a lake"    # wake word again → runs


def test_word_boundary_avoids_false_matches():
    gate = _make_wake_gate("conjure")
    assert gate("reconjuret the scene") is None          # substring, not a whole word → no trigger
