"""Thin-client helpers (shared-session Step D) — the pure prompt/render/context pieces the CLI and voice
share. No network (the WebSocket itself is opened by the caller)."""

from conjure.agent_client import apply_context, prompt_from_context, render_event, ws_url


def test_ws_url_builds_the_per_connection_socket_url():
    assert ws_url("http://localhost:8770", "guest") == "ws://localhost:8770/ws?user=guest"
    assert ws_url("https://host:9/", "alice") == "wss://host:9/ws?user=alice"


def test_prompt_reflects_context_data_and_shell_mode():
    assert prompt_from_context({"user": "alice", "agent": "builder", "llm": "Claude"}) \
        == "conjure:alice.builder.claude> "
    assert prompt_from_context({"in_shell": True}) == "conjure:shell> "
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
    assert render_event({"type": "assistant_delta", "text": "on it"}, me="alice", verbose=False) == "on it"
    assert render_event({"type": "assistant_final", "text": "done"}, me="alice", verbose=False) == "done"
    assert render_event({"type": "notice", "text": "Now on Gemini"}, me="alice", verbose=False) == "Now on Gemini"
    assert render_event({"type": "busy"}, me="alice", verbose=False).startswith("[busy")
    assert render_event({"type": "context", "agent": "builder"}, me="alice", verbose=False) is None
    assert render_event({"type": "turn_done"}, me="alice", verbose=False) is None   # control


def test_render_tool_call_is_verbose_only():
    ev = {"type": "tool_call", "name": "place_asset", "args": {"query": "tree"}}
    assert render_event(ev, me="alice", verbose=False) is None
    assert render_event(ev, me="alice", verbose=True) == '  · place_asset({"query": "tree"})'
