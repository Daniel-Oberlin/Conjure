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
