"""Thin-client helpers (shared-session Step D) — the pure prompt/render/context pieces the CLI and voice
share. No network (the WebSocket itself is opened by the caller)."""

from conjure.agent_client import (apply_context, human_count, prompt_from_context, render_event,
                                  render_parts, status_from_context, ws_url)

_STATS = {"turns": 12, "cap": 40,
          "chars": {"prompt": 10152, "room": 10589, "tools": 33492, "history": 568}}   # total 54_801
_CTX = {"agent": "builder", "llm": "Claude", "stats": _STATS}


def test_human_count_keeps_the_bar_a_stable_width():
    assert [human_count(n) for n in (0, 812, 1000, 2345, 49_700, 123_456, 1_234_567)] \
        == ["0", "812", "1.0k", "2.3k", "49.7k", "123k", "1.2M"]


def test_status_reports_turns_total_and_a_char_breakdown_with_percentages():
    bar = status_from_context(_CTX)
    assert bar == ("builder·claude   12/40 turns   54.8k chars   "
                   "prompt 10.2k (19%) · room 10.6k (19%) · tools 33.5k (61%) · hist 568 (1%)")


def test_status_omits_slices_that_are_zero():
    # `room` is 0 until a turn actually assembles the {context} injection (and some agents never do) —
    # a permanent "room 0 (0%)" would be noise.
    ctx = {"agent": "outdoor", "llm": "Claude",
           "stats": {"turns": 4, "cap": 40, "chars": {"prompt": 3434, "room": 0, "tools": 5367,
                                                      "history": 1478}}}
    bar = status_from_context(ctx)
    assert "room" not in bar
    assert "prompt 3.4k (33%) · tools 5.4k (52%) · hist 1.5k (14%)" in bar


def test_status_degrades_by_shortening_before_it_drops_anything():
    full = status_from_context(_CTX)
    compact = status_from_context(_CTX, width=len(full) - 1)
    # The breakdown survives a narrow terminal by losing its char counts, not by disappearing.
    assert compact != full and len(compact) <= len(full) - 1
    for label in ("prompt", "room", "tools", "hist"):
        assert f"{label} " in compact
    assert "10.2k" not in compact and "19%" in compact


def test_status_drops_least_important_fields_first_and_never_wraps():
    for width in (200, 120, 100, 80, 60, 40, 20, 10, 3, 0):
        bar = status_from_context(_CTX, width=width)
        assert len(bar) <= width, f"width {width} overflowed: {bar!r}"
        if width >= 20:
            assert "builder·claude" in bar, f"identity should outlive the numbers at width {width}"


def test_status_shows_the_working_clock_first_when_a_turn_is_running():
    assert status_from_context(_CTX, working=12.7).startswith("working 12s")
    assert "working" not in status_from_context(_CTX)


def test_status_survives_a_server_that_sends_no_stats():
    assert status_from_context({"agent": "builder", "llm": "Claude"}) == "builder·claude"
    assert status_from_context({}) == "agent·?"


def test_ws_url_builds_the_per_connection_socket_url():
    assert ws_url("http://localhost:8770", "guest") == "ws://localhost:8770/ws?user=guest&client=cli"
    assert ws_url("https://host:9/", "alice") == "wss://host:9/ws?user=alice&client=cli"
    # `client` picks the command set: a spoken directory listing helps nobody, so voice gets a subset.
    assert ws_url("http://h", "v", backlog=False, client="voice") == "ws://h/ws?user=v&client=voice&backlog=0"


def test_prompt_reflects_context_data_and_shell_mode():
    assert prompt_from_context({"user": "alice", "agent": "builder", "llm": "Claude"}) \
        == "conjure:alice.builder.claude> "
    assert prompt_from_context({"user": "alice", "in_shell": True}) == "conjure:alice.shell> "
    assert prompt_from_context({"in_shell": True}) == "conjure:you.shell> "   # no user yet → placeholder
    assert prompt_from_context({}) == "conjure:you.agent.?> "     # graceful before the first context event


def test_apply_context_folds_only_known_keys():
    ctx = {"agent": "builder", "llm": "Claude", "user": "alice", "in_shell": False}
    apply_context(ctx, {"type": "context", "agent": "outdoor", "llm": "Gemini", "in_shell": True,
                        "world": "meadow", "x": 1})
    assert ctx == {"agent": "outdoor", "llm": "Gemini", "user": "alice", "in_shell": True, "world": "meadow"}
    assert "x" not in ctx                                          # no stray keys


def test_render_event_formats_each_type():
    # our own LIVE turn isn't echoed; another speaker's is (one shared conversation)
    assert render_event({"type": "user_turn", "speaker": "alice", "text": "hi"}, me="alice", verbose=False) is None
    assert render_event({"type": "user_turn", "speaker": "bob", "text": "hi"}, me="alice", verbose=False) == "bob: hi"
    # but a BACKLOG turn IS shown even when it's ours — reviewing history we weren't here to type it
    assert render_event({"type": "user_turn", "speaker": "alice", "text": "hi", "backlog": True},
                        me="alice", verbose=False) == "alice: hi"
    assert render_event({"type": "busy"}, me="alice", verbose=False).startswith("[busy")
    assert render_event({"type": "context", "agent": "builder"}, me="alice", verbose=False) is None
    assert render_event({"type": "turn_done"}, me="alice", verbose=False) is None   # control


def test_agent_replies_are_attributed_by_agent_name_like_any_other_speaker():
    for kind in ("assistant_delta", "assistant_final"):
        assert render_event({"type": kind, "text": "on it"}, me="alice", verbose=False,
                            agent="builder") == "builder: on it"
    # no agent known yet (before the first context event) → a neutral label, never a bare line
    assert render_event({"type": "assistant_final", "text": "done"}, me="alice", verbose=False) == "agent: done"
    assert render_event({"type": "assistant_final", "text": "done"}, me="alice", verbose=False,
                        agent="") == "agent: done"
    # an empty reply prints nothing rather than a lone "builder:"
    assert render_event({"type": "assistant_final", "text": ""}, me="alice", verbose=False,
                        agent="builder") is None


def test_notices_and_tool_traces_are_not_attributed_to_the_agent():
    # A notice is the SHELL (the deterministic plane) talking — labelling it 'builder:' would claim the
    # agent said something it didn't.
    assert render_parts({"type": "notice", "text": "Now on Gemini"}, me="alice", verbose=False,
                        agent="builder") == (None, "Now on Gemini")
    assert render_event({"type": "notice", "text": "Now on Gemini"}, me="alice", verbose=False,
                        agent="builder") == "Now on Gemini"
    assert render_parts({"type": "busy"}, me="alice", verbose=False, agent="builder")[0] is None


def test_render_parts_splits_speaker_from_text_so_a_front_end_can_style_them():
    assert render_parts({"type": "user_turn", "speaker": "bob", "text": "hi"},
                        me="alice", verbose=False) == ("bob", "hi")
    assert render_parts({"type": "assistant_final", "text": "done"},
                        me="alice", verbose=False, agent="builder") == ("builder", "done")
    assert render_parts({"type": "user_turn", "speaker": "alice", "text": "hi"},
                        me="alice", verbose=False) is None


def test_render_tool_call_is_verbose_only():
    ev = {"type": "tool_call", "name": "place_asset", "args": {"query": "tree"}}
    assert render_event(ev, me="alice", verbose=False) is None
    assert render_event(ev, me="alice", verbose=True) == '  · place_asset({"query": "tree"})'
    assert render_parts(ev, me="alice", verbose=True)[0] is None       # a trace, not a speaker
