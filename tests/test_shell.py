"""The shell — the deterministic command plane above the agent (conjure.shell).

Commands are parsed, never sent to an LLM. The key behaviours: in agent mode only `conjure`-prefixed
input is a command (so agent content like "put a shell on the table" passes through); in shell mode
every line is a command and non-commands are rejected (never forwarded to the LLM)."""
import pytest

from conjure.shell import Shell


class FakeDirector:
    """Just what the shell reads/forwards to: a roster, an active LLM, an agent, and async handle()."""

    def __init__(self):
        self.roster = {"Claude": object(), "Gemini": object()}
        self.active = "Claude"
        self.agent = type("A", (), {"name": "builder"})()
        self._tools = [object(), object()]
        self.user = "daniel"
        self.handled = []

    async def handle(self, text, *, on_text=None, on_tool=None):
        self.handled.append(text)
        if on_text:
            await on_text(f"did «{text}»", final=True, speaker=self.active)


def _shell():
    d = FakeDirector()
    out = []

    async def on_text(text, *, final, speaker):
        out.append((speaker, text))

    return Shell(d), d, out, on_text


# --------------------------------------------------------------------------- agent mode (default)

async def test_plain_text_in_agent_mode_is_forwarded():
    sh, d, out, on_text = _shell()
    await sh.feed("put an oak tree in front of me", on_text=on_text)
    assert d.handled == ["put an oak tree in front of me"]   # went to the agent, not the shell


async def test_agent_content_mentioning_shell_is_not_a_command():
    # The whole reason for the wake word: "shell" as content must reach the agent.
    sh, d, out, on_text = _shell()
    await sh.feed("put a shell on the table", on_text=on_text)
    assert d.handled == ["put a shell on the table"] and sh.in_shell is False


async def test_conjure_open_shell_enters_shell_mode():
    sh, d, out, on_text = _shell()
    await sh.feed("conjure open shell", on_text=on_text)
    assert sh.in_shell is True and d.handled == []           # a command, not forwarded
    assert any("Shell" in t for _, t in out)


async def test_bare_conjure_opens_the_shell():
    sh, d, out, on_text = _shell()
    await sh.feed("conjure", on_text=on_text)
    assert sh.in_shell is True


async def test_conjure_use_switches_llm_inline_without_entering_shell():
    sh, d, out, on_text = _shell()
    await sh.feed("conjure use Gemini", on_text=on_text)
    assert d.active == "Gemini" and sh.in_shell is False and d.handled == []


# --------------------------------------------------------------------------- shell mode

async def test_in_shell_mode_every_line_is_a_command_not_forwarded():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("make a tree", on_text=on_text)             # not a command
    assert d.handled == []                                    # NOT sent to the LLM
    assert any("Unknown command" in t for _, t in out)


async def test_exit_leaves_shell_mode():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("exit", on_text=on_text)
    assert sh.in_shell is False and any("Back to" in t for _, t in out)


async def test_switch_llm_in_shell_mode():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("talk to Gemini", on_text=on_text)
    assert d.active == "Gemini" and any("Gemini" in t for _, t in out)
    out.clear()
    await sh.feed("gemini", on_text=on_text)                  # bare known name also switches
    assert d.active == "Gemini"


async def test_help_and_llms_list_without_touching_the_agent():
    sh, d, out, on_text = _shell()
    sh.in_shell = True
    await sh.feed("help", on_text=on_text)
    await sh.feed("llms", on_text=on_text)
    assert d.handled == []
    text = "\n".join(t for _, t in out)
    assert "Commands:" in text and "Claude" in text and "Gemini" in text


# --------------------------------------------------------------------------- prompt

def test_prompt_reflects_mode():
    sh, d, out, on_text = _shell()
    assert sh.prompt() == "conjure:daniel.builder.claude> "   # user · agent-primary; the LLM can vary
    sh.in_shell = True
    assert sh.prompt() == "conjure:shell> "
