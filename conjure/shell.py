"""The shell — a deterministic command plane above the agents (docs/agents.md §2).

Control that must be reliable — switching the active LLM, entering/leaving shell mode, inspecting
what's loaded — runs here, *parsed, never sent to an LLM*. The shell wraps the active agent (today's
`Director`): input it doesn't recognise as a command is forwarded to the agent unchanged. Switching
the active LLM lives *only* here (deterministic) — the agent no longer parses handovers out of an
utterance.

Entering: say/type `conjure open shell` → shell mode (prompt `conjure:shell>`); `exit` resumes the
agent. While *in* an agent, only input led by the `conjure` wake word is taken as a command — so
"put a shell on the table" still reaches the builder. The command set is a small registry, easy to grow.
"""
from __future__ import annotations

import re
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Awaitable, Callable, Optional

from .agents import AGENTS_DIR
from .config import DEFAULT_USER, Settings, scope_for
from .director import Director

OnText = Callable[..., Awaitable[None]]
OnTool = Callable[..., Awaitable[None]]

# "conjure …" inline escape (STT rarely punctuates, so the comma is optional).
_WAKE = re.compile(r"^conjure\b[,:]?\s*(?P<rest>.*)$", re.I | re.S)
# Explicit LLM switch in the shell: "talk to / switch to / use <name>".
_SWITCH = re.compile(r"^(?:talk\s+to|switch\s+to|use|become|be)\s+(?P<name>[a-z0-9]+)$", re.I)


def _match_name(token: str, roster) -> Optional[str]:
    """Case-insensitive lookup of a spoken/typed word against the roster's casual LLM names
    ('gemini' → 'Gemini'); None if it matches none. The gate that keeps a switch deterministic."""
    for name in roster:
        if name.lower() == token.lower():
            return name
    return None


class Shell:
    def __init__(self, director: Optional[Director] = None, settings=None):
        self._director = director
        self._settings = settings
        # Director lifecycle (agent switching): the shell owns it via a per-director AsyncExitStack, so
        # `agent <name>` can tear down the current agent's MCP server and launch the next. Set by
        # `Shell.session`; None for a hand-built Shell(director) (e.g. tests — no switching).
        self._user = getattr(director, "user", DEFAULT_USER)
        self._errlog = None
        self._stack: Optional[AsyncExitStack] = None
        self.in_shell = False
        self._pending_delete: Optional[str] = None            # armed by `delete`, fired by a `y` confirmation
        # (matcher, handler, help). First match wins; an LLM switch and the unknown-command fallback
        # are tried after, in _dispatch. The handler is called as handler(on_text, match). Add a row
        # to add a command.
        self._table = [
            (re.compile(r"^(?:open\s+)?shell$", re.I), self._open, "open shell — enter command mode"),
            (re.compile(r"^(?:exit|leave|close|done)$", re.I), self._exit, "exit — leave the shell, back to the agent"),
            (re.compile(r"^(?:help|\?|commands)$", re.I), self._help, "help — list commands"),
            (re.compile(r"^(?:whoami|status|where)$", re.I), self._status, "whoami — the active LLM + agent"),
            (re.compile(r"^(?:llms|models)$", re.I), self._llms, "llms — list available LLMs"),
            (re.compile(r"^agents$", re.I), self._agents, "agents — list available agents"),
            (re.compile(r"^agent\s+(?P<name>[\w./-]+)$", re.I), self._switch_agent,
             "agent <name> — switch to another agent (relaunches its tools; starts its own context)"),
            (re.compile(r"^(?:dir|ls)(?:\s+(?P<path>\S.*))?$", re.I), self._dir,
             "dir [path] — list users/spaces/worlds/assets (e.g. dir /alice/worlds)"),
            (re.compile(r"^(?:delete|rm)\s+(?P<path>\S.*)$", re.I), self._delete,
             "delete <path> — remove a user/space/world/asset (asks to confirm)"),
        ]

    @classmethod
    @asynccontextmanager
    async def session(cls, settings: Settings, *, agent: Optional[str] = None, user: str = DEFAULT_USER,
                      errlog=None):
        """Own an agent's lifecycle for a front-end. Opens `agent` (spawning its MCP server) and yields
        a Shell driving it; `agent <name>` can then switch agents in place. Closes the active agent's
        MCP server on exit. `agent=None` resumes the user's last-used agent (server-persisted). Front-ends
        use this instead of `Director.connect` directly."""
        shell = cls(None, settings)
        shell._user = user
        shell._errlog = errlog
        await shell._open_agent(agent or await shell._last_agent())
        try:
            yield shell
        finally:
            if shell._stack is not None:
                await shell._stack.aclose()

    async def _last_agent(self) -> str:
        """The user's last-used agent (server-persisted), so a launch without --agent resumes it. Falls
        back to `builder` when there's no record or the server is unreachable."""
        url = getattr(self._settings, "world_url", None) if self._settings else None
        if not url:
            return "builder"
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(f"{url}/agent/last", params={"user": self._user})
                return r.json().get("agent") or "builder"
        except Exception:
            return "builder"

    @property
    def director(self) -> Director:
        return self._director

    async def _open_agent(self, agent: str) -> None:
        """Switch the active agent, tearing down the current one's MCP server and launching the new one.
        Order matters: the MCP client uses anyio task groups whose cancel scopes must unwind **LIFO in
        the same task**, so we CLOSE the current connection *before* opening the next (opening on top and
        closing underneath raises "exit cancel scope that isn't the current task's current"). If the new
        agent fails to start we restore the previous one so the shell isn't stranded. New agent = its own
        fresh transcript."""
        prev_agent = self._agent_name() if self._director else None
        keep_active = self._director.active if self._director else None

        async def _open(name: str) -> None:
            stack = AsyncExitStack()
            director = await stack.enter_async_context(
                Director.connect(self._settings, agent=name, user=self._user, errlog=self._errlog))
            if keep_active in director.roster:
                director.active = keep_active                 # keep talking to the same LLM if allowed
            self._stack, self._director = stack, director

        if self._stack is not None:                           # close current FIRST (LIFO-safe teardown)
            await self._stack.aclose()
            self._stack, self._director = None, None
        try:
            await _open(agent)
        except Exception:
            if prev_agent is not None:                        # restore so the shell keeps working
                await _open(prev_agent)
            raise
        await self._activate_world(agent)                     # make a world in the new agent's scope live

    async def _activate_world(self, agent: str) -> None:
        """Ask the world server to make a world in the new agent's scope live (resume its last-active
        world, or create its default) — so switching agents doesn't leave the previous agent's world
        active. Best-effort: a failure (old server without the route, network) doesn't break the switch."""
        url = getattr(self._settings, "world_url", None) if self._settings else None
        if not url:
            return
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(f"{url}/scope/activate",
                                  json={"scope": scope_for(self._user, agent)})
        except Exception:
            pass

    def prompt(self) -> str:
        """The prompt the front-end shows: `conjure:shell>` in shell mode, else
        `conjure:<user>.<agent>.<llm>>` (who you're logged in as · agent-primary — the experience is the
        constant, the LLM running it can vary)."""
        if self.in_shell:
            return "conjure:shell> "
        return f"conjure:{self._director.user}.{self._agent_name()}.{self._director.active.lower()}> "

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
        if self._pending_delete is not None:                  # a delete is armed — this line is the y/n answer
            await self._confirm_delete(cmd, on_text)
            return
        for rx, handler, _ in self._table:
            m = rx.match(cmd)
            if m:
                await handler(on_text, m)
                return
        if await self._switch(cmd, on_text):
            return
        await self._say(on_text, f"Unknown command: {cmd!r}. Type 'help'.")

    # ----------------------------------------------------------------- commands
    async def _open(self, on_text, m=None):
        self.in_shell = True
        await self._say(on_text, "Shell. Type 'help' for commands, 'exit' to return.")

    async def _exit(self, on_text, m=None):
        if not self.in_shell:
            await self._say(on_text, "Not in the shell.")
            return
        self.in_shell = False
        await self._say(on_text, f"Back to {self._director.active} ({self._agent_name()}).")

    async def _help(self, on_text, m=None):
        lines = ["Commands:"] + [f"  {h}" for _, _, h in self._table]
        lines.append("  talk to <llm> / use <llm> — switch the active LLM")
        lines.append("(While talking to an agent, prefix a command with 'conjure', e.g. 'conjure open shell'.)")
        await self._say(on_text, "\n".join(lines))

    async def _status(self, on_text, m=None):
        d = self._director
        await self._say(on_text, f"{d.active}.{self._agent_name()} · {len(d.roster)} LLMs · "
                                 f"{len(d._tools)} tools · {'shell' if self.in_shell else 'agent'} mode")

    async def _llms(self, on_text, m=None):
        rows = [("* " if n == self._director.active else "  ") + n for n in self._director.roster]
        await self._say(on_text, "LLMs:\n" + "\n".join(rows))

    def _agent_names(self) -> list[str]:
        """Available agents — every `agents/<name>/agent.json` on disk."""
        if not AGENTS_DIR.exists():
            return []
        return sorted(p.name for p in AGENTS_DIR.iterdir() if (p / "agent.json").exists())

    async def _agents(self, on_text, m=None):
        active = self._agent_name()
        rows = [("* " if n == active else "  ") + n for n in self._agent_names()]
        await self._say(on_text, "Agents:\n" + "\n".join(rows))

    async def _switch_agent(self, on_text, m):
        name = m.group("name").strip()
        if self._stack is None:                               # hand-built Shell(director) — no lifecycle
            await self._say(on_text, "Agent switching isn't available in this session.")
            return
        match = next((a for a in self._agent_names() if a.lower() == name.lower()), None)
        if not match:
            avail = ", ".join(self._agent_names()) or "none"
            await self._say(on_text, f"No agent {name!r}. Available: {avail}.")
            return
        if match == self._agent_name():
            await self._say(on_text, f"Already on {match}.")
            return
        try:
            await self._open_agent(match)                     # relaunches its MCP server; keeps the current agent on failure
        except Exception as exc:                              # bad def, no LLM key for it, server won't start
            await self._say(on_text, f"Couldn't switch to {match}: {exc}")
            return
        await self._say(on_text, f"Switched to agent {match} ({self._director.active}).")

    # -- dir / delete: a filesystem-like view + purge of the namespace (docs/agents.md §2). Both go
    # through the world server's /admin endpoints, so they act on its live state (not raw files).
    async def _dir(self, on_text, m):
        path = (m.group("path") or "/").strip() if m else "/"
        data = await self._admin("tree", path)
        if not data.get("ok"):
            await self._say(on_text, data.get("error", "error"))
            return
        await self._say(on_text, self._render_tree(data["node"]))

    async def _delete(self, on_text, m):
        path = m.group("path").strip()
        preview = await self._admin("tree", path)             # resolve + show what's about to go
        if not preview.get("ok"):
            await self._say(on_text, preview.get("error", "error"))
            return
        self._pending_delete = path
        await self._say(on_text, f"Delete {path} ({self._summarize(preview['node'])})?  "
                                 f"Type 'y' to confirm, anything else cancels.")

    async def _confirm_delete(self, cmd: str, on_text) -> None:
        path, self._pending_delete = self._pending_delete, None
        if cmd.strip().lower() not in ("y", "yes", "confirm"):
            await self._say(on_text, "Cancelled.")
            return
        data = await self._admin("delete", path)
        if data.get("ok"):
            await self._say(on_text, f"Deleted {data.get('deleted', path)}.")
        else:
            await self._say(on_text, f"Not deleted: {data.get('error', 'error')}")

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

    async def _admin(self, action: str, path: str) -> dict:
        """POST to the world server's /admin/{tree,delete}. Returns the JSON, or an error dict."""
        url = getattr(self._settings, "world_url", None) if self._settings else None
        if not url:
            return {"ok": False, "error": "no world server configured"}
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(f"{url}/admin/{action}", json={"path": path})
                return resp.json()
        except Exception as exc:                              # network / server down / bad JSON
            return {"ok": False, "error": f"admin request failed: {exc}"}

    def _render_tree(self, node: dict, depth: int = 0) -> str:
        pad = "  " * depth
        kind = node.get("kind", "")
        tag = f" [{kind}]" if kind not in ("root", "note", "category") else ""
        line = f"{pad}{node.get('label', '')}{tag}"
        if node.get("detail"):
            line += f"  — {node['detail']}"
        out = [line]
        for child in node.get("children") or []:
            out.append(self._render_tree(child, depth + 1))
        return "\n".join(out)

    def _summarize(self, node: dict) -> str:
        counts: dict = {}

        def walk(n: dict) -> None:
            k = n.get("kind")
            if k in ("world", "space", "asset", "user"):
                counts[k] = counts.get(k, 0) + 1
            for c in n.get("children") or []:
                walk(c)

        walk(node)
        if not counts:
            return "nothing"
        return ", ".join(f"{v} {k}{'' if v == 1 else 's'}" for k, v in sorted(counts.items()))

    async def _say(self, on_text, text: str) -> None:
        if on_text:
            await on_text(text, final=True, speaker="shell")
