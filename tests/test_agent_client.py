"""Thin-client helpers (shared-session Step C1) — the pure SSE parsing / prompt / rendering the CLI and
voice share. No network (post_turn/stream_events are the only I/O, exercised live)."""

from conjure.agent_client import (apply_context, parse_sse_line, prompt_from_context, render_event)


def test_parse_sse_line_reads_data_and_ignores_the_rest():
    assert parse_sse_line('data: {"type": "notice", "text": "hi"}') == {"type": "notice", "text": "hi"}
    assert parse_sse_line(": keepalive") is None            # SSE comment (heartbeat)
    assert parse_sse_line("") is None                        # blank
    assert parse_sse_line("event: notice") is None           # non-data field
    assert parse_sse_line("data: not json") is None          # malformed → skipped, not fatal


def test_prompt_reflects_context_and_shell_mode():
    assert prompt_from_context({"user": "alice", "agent": "builder", "llm": "Claude"}) \
        == "conjure:alice.builder.claude> "
    assert prompt_from_context({"in_shell": True}) == "conjure:shell> "
    assert prompt_from_context({}) == "conjure:you.agent.?> "   # graceful before the first context event


def test_apply_context_folds_only_known_keys():
    ctx = {"agent": "builder", "llm": "Claude", "user": "alice", "in_shell": False}
    apply_context(ctx, {"type": "context", "agent": "outdoor", "llm": "Gemini", "in_shell": True, "x": 1})
    assert ctx == {"agent": "outdoor", "llm": "Gemini", "user": "alice", "in_shell": True}  # no stray "x"


def test_render_event_formats_each_type():
    # our own turn isn't echoed; another speaker's is (one shared conversation)
    assert render_event({"type": "user_turn", "speaker": "alice", "text": "hi"}, me="alice", verbose=False) is None
    assert render_event({"type": "user_turn", "speaker": "bob", "text": "hi"}, me="alice", verbose=False) == "bob: hi"
    assert render_event({"type": "assistant_delta", "text": "on it"}, me="alice", verbose=False) == "on it"
    assert render_event({"type": "assistant_final", "text": "done"}, me="alice", verbose=False) == "done"
    assert render_event({"type": "notice", "text": "Now on Gemini"}, me="alice", verbose=False) == "Now on Gemini"
    assert render_event({"type": "busy"}, me="alice", verbose=False).startswith("[busy")
    assert render_event({"type": "context", "agent": "builder"}, me="alice", verbose=False) is None
    assert render_event({"type": "turn_done", "speaker": "alice"}, me="alice", verbose=False) is None  # control


def test_render_tool_call_is_verbose_only():
    ev = {"type": "tool_call", "name": "place_asset", "args": {"query": "tree"}}
    assert render_event(ev, me="alice", verbose=False) is None
    assert render_event(ev, me="alice", verbose=True) == '  · place_asset({"query": "tree"})'
