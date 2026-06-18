"""The shell — a deterministic command plane above the agents (docs/agents.md §2).

Control that must be reliable — switching the active LLM, entering/leaving shell mode, inspecting
what's loaded — runs here, *parsed, never sent to an LLM*. The shell wraps the active agent (today's
`Director`): input it doesn't recognise as a command is forwarded to the agent unchanged, so existing
behaviour (incl. inline "let me talk to Gemini" routing) is untouched.

Entering: say/type `conjure open shell` → shell mode (prompt `conjure:shell>`); `exit` resumes the
agent. While *in* an agent, only input led by the `conjure` wake word is taken as a command — so
"put a shell on the table" still reaches the builder. The command set is a small registry, easy to grow.
"""
from __future__ import annotations

import re
from typing import Awaitable, Callable, Optional

from .agents import AGENTS_DIR
from .director import Director, _match_name

OnText = Callable[..., Awaitable[None]]
OnTool = Callable[..., Awaitable[None]]

# "conjure …" inline escape (STT rarely punctuates, so the comma is optional).
_WAKE = re.compile(r"^conjure\b[,:]?\s*(?P<rest>.*)$", re.I | re.S)
# Explicit LLM switch in the shell: "talk to / switch to / use <name>".
_SWITCH = re.compile(r"^(?:talk\s+to|switch\s+to|use|become|be)\s+(?P<name>[a-z0-9]+)$", re.I)


class Shell:
    def __init__(self, director: Director, settings=None):
        self._director = director
        self._settings = settings
        self.in_shell = False
        # (matcher, handler, help). First match wins; an LLM switch and the unknown-command fallback
        # are tried after, in _dispatch. Add a row to add a command.
        self._table = [
            (re.compile(r"^(?:open\s+)?shell$", re.I), self._open, "open shell — enter command mode"),
            (re.compile(r"^(?:exit|leave|close|done)$", re.I), self._exit, "exit — leave the shell, back to the agent"),
            (re.compile(r"^(?:help|\?|commands)$", re.I), self._help, "help — list commands"),
            (re.compile(r"^(?:whoami|status|where)$", re.I), self._status, "whoami — the active LLM + agent"),
            (re.compile(r"^(?:llms|models)$", re.I), self._llms, "llms — list available LLMs"),
            (re.compile(r"^agents$", re.I), self._agents, "agents — list available agents"),
        ]

    def prompt(self) -> str:
        """The prompt the front-end shows: `conjure:shell>` in shell mode, else `conjure:<agent>.<llm>>`
        (agent-primary — the experience is the constant; the LLM running it can vary)."""
        if self.in_shell:
            return "conjure:shell> "
        return f"conjure:{self._agent_name()}.{self._director.active.lower()}> "

    async def feed(self, text: str, *, on_text: Optional[OnText] = None,
                   on_tool: Optional[OnTool] = None) -> None:
        """Route one line: a recognised command runs here (deterministic); anything else goes to the
        active agent."""
        cmd = self._as_command(text)
        if cmd is None:
            await self._director.handle(text, on_text=on_text, on_tool=on_tool)
        else:
            await self._dispatch(cmd, on_text)

    def _as_command(self, raw: str) -> Optional[str]:
        s = raw.strip()
        if self.in_shell:
            return s                                          # shell mode: every line is a command
        m = _WAKE.match(s)
        if m:
            return m.group("rest").strip() or "open shell"   # bare "conjure" → open the shell
        return None                                           # agent mode, no wake word → not a command

    async def _dispatch(self, cmd: str, on_text) -> None:
        if not cmd:
            return
        for rx, handler, _ in self._table:
            if rx.match(cmd):
                await handler(on_text)
                return
        if await self._switch(cmd, on_text):
            return
        await self._say(on_text, f"Unknown command: {cmd!r}. Type 'help'.")

    # ----------------------------------------------------------------- commands
    async def _open(self, on_text):
        self.in_shell = True
        await self._say(on_text, "Shell. Type 'help' for commands, 'exit' to return.")

    async def _exit(self, on_text):
        if not self.in_shell:
            await self._say(on_text, "Not in the shell.")
            return
        self.in_shell = False
        await self._say(on_text, f"Back to {self._director.active} ({self._agent_name()}).")

    async def _help(self, on_text):
        lines = ["Commands:"] + [f"  {h}" for _, _, h in self._table]
        lines.append("  talk to <llm> / use <llm> — switch the active LLM")
        lines.append("(While talking to an agent, prefix a command with 'conjure', e.g. 'conjure open shell'.)")
        await self._say(on_text, "\n".join(lines))

    async def _status(self, on_text):
        d = self._director
        await self._say(on_text, f"{d.active}.{self._agent_name()} · {len(d.roster)} LLMs · "
                                 f"{len(d._tools)} tools · {'shell' if self.in_shell else 'agent'} mode")

    async def _llms(self, on_text):
        rows = [("* " if n == self._director.active else "  ") + n for n in self._director.roster]
        await self._say(on_text, "LLMs:\n" + "\n".join(rows))

    async def _agents(self, on_text):
        names = sorted(p.name for p in AGENTS_DIR.iterdir()
                       if (p / "agent.json").exists()) if AGENTS_DIR.exists() else []
        active = self._agent_name()
        await self._say(on_text, "Agents:\n" + "\n".join(("* " if n == active else "  ") + n for n in names))

    async def _switch(self, cmd: str, on_text) -> bool:
        m = _SWITCH.match(cmd)
        name = _match_name(m.group("name") if m else cmd, self._director.roster)
        if not name:
            return False
        self._director.active = name
        await self._say(on_text, f"Now talking to {name} ({self._agent_name()}).")
        return True

    # ----------------------------------------------------------------- helpers
    def _agent_name(self) -> str:
        return self._director.agent.name if self._director.agent else "agent"

    async def _say(self, on_text, text: str) -> None:
        if on_text:
            await on_text(text, final=True, speaker="shell")
