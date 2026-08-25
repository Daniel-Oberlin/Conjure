"""The shell — a deterministic command plane above the agents (docs/agents.md §2).

Control that must be reliable — switching the active LLM, entering/leaving shell mode, inspecting
what's loaded — runs here, *parsed, never sent to an LLM*. The shell wraps the active agent (today's
`Director`): input it doesn't recognise as a command is forwarded to the agent unchanged. Switching
the active LLM lives *only* here (deterministic) — the agent no longer parses handovers out of an
utterance.

Entering: say/type `conjure open shell` → shell mode (prompt `conjure:<user>.shell>`); `exit` resumes the
agent. While *in* an agent, only input led by the `conjure` wake word is taken as a command — so
"put a shell on the table" still reaches the builder.

**Two audiences, one registry.** Voice is live in the simulation with no screen; the CLI has a terminal.
Rather than two command sets that would drift, every row carries a `voice` flag: voice-safe commands are
the modal/navigational ones whose output is speakable ("where am I", "go to the meadow", "new session"),
while the namespace commands — listings, paths, deletion — are CLI-only and refuse politely by voice.

**Two shapes of command.** A *noun* command acts on the thing that is LIVE (`world meadow`, `session new`),
and reads the same spoken or typed. A *path* command acts on anything addressable (`dir`, `show`, `cd`,
`delete`, `rename`, `public`/`private`), over a namespace that mirrors storage:

    /<user>/spaces/<name>
    /<user>/agents/<agent>/assets/<id>
    /<user>/agents/<agent>/sessions/<sid>/worlds/<name>   (name or id — a world's identity is its id)
    /<user>/agents/<agent>/worlds            → shortcut for the ACTIVE session's worlds
"""
from __future__ import annotations

import re
import shlex
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Awaitable, Callable, Optional

from .agents import agent_names, list_agents
from .config import DEFAULT_USER, Settings, scope_for
from .director import Director

OnText = Callable[..., Awaitable[None]]
OnTool = Callable[..., Awaitable[None]]

# "conjure …" inline escape (STT rarely punctuates, so the comma is optional).
_WAKE = re.compile(r"^conjure\b[,:]?\s*(?P<rest>.*)$", re.I | re.S)
# Spoken aliases for `llm <name>`. Voice only: in text these would claim every LLM name as a reserved
# word, so the canonical typed form is the noun command, consistent with `agent`/`world`/`session`.
_SPOKEN_LLM = re.compile(r"^(?:talk\s+to|switch\s+to|use|become|be)\s+(?P<name>[a-z0-9]+)$", re.I)
# The two shell-MODE toggles (open/leave). Recognised server-side (the client never knows these phrases).
_OPEN_SHELL = re.compile(r"^(?:open\s+)?shell$", re.I)
_LEAVE_SHELL = re.compile(r"^(?:exit|leave|close|done)$", re.I)


# --------------------------------------------------------------------------- paths
#
# Pure, so the resolution rules are testable without a server: `~` is your own home, everything else
# resolves against the working directory the connection carries.

def home_of(user: str) -> str:
    return f"/{user}"


def default_cwd(user: str, agent: str) -> str:
    """Where a connection starts: its own scope, so a bare `dir` shows something worth seeing."""
    return f"/{user}/agents/{agent}" if agent else home_of(user)


def resolve_path(cwd: str, arg: str, user: str) -> str:
    """`arg` (absolute, `~`-relative or cwd-relative) → a normalized absolute path."""
    arg = (arg or "").strip()
    if not arg:
        base = cwd
    elif arg == "~" or arg.startswith("~/"):
        base = home_of(user) + arg[1:]
    elif arg.startswith("/"):
        base = arg
    else:
        base = f"{cwd.rstrip('/')}/{arg}"
    out: list[str] = []
    for seg in base.split("/"):
        if not seg or seg == ".":
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/" + "/".join(out)


def unquote_arg(arg: str) -> str:
    """A path argument, quote-aware. Display names contain spaces now, so both `cd "a b/c"` and the
    unquoted `cd a b/c` have to mean the same thing — quoting is optional, not required."""
    arg = (arg or "").strip()
    if arg[:1] in ("'", '"'):
        try:
            return (shlex.split(arg) or [""])[0]
        except ValueError:
            return arg.strip("'\"")
    return arg


def loc_name(path: str) -> str:
    """The last segment of a path — the entry's own name."""
    return (path or "").rstrip("/").rsplit("/", 1)[-1]


def display_path(path: str, user: str) -> str:
    """`/daniel/agents/builder` → `~/agents/builder` for the prompt, when it's your own home."""
    home = home_of(user)
    if path == home:
        return "~"
    return "~" + path[len(home):] if path.startswith(home + "/") else path


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
        self._acting = self._user        # WHO the current command acts as (the speaker); set per `_dispatch`
        self._permitted = True           # is the speaker permitted in the LIVE session? gates shared-effect
                                         # verbs (switch/new/agent) so a bumped guest can't drive (§6d)
        self._errlog = None
        self._voice = False              # is this dispatch coming from a voice client? (set per-dispatch)
        self._stack: Optional[AsyncExitStack] = None
        self.in_shell = False
        self._pending_delete: Optional[str] = None            # armed by `delete`, fired by a `y` confirmation
        # Host override for `agent <name>`: when set, the switch is delegated instead of running the
        # in-process `_open_agent` teardown. The agent server sets this so a client switch routes through
        # the world server (assert scope → its /ws follower re-binds the Director in the OWNING task) —
        # required because a command runs in the connection's receive-loop task, and a cross-task MCP
        # `aclose()` raises "exit a cancel scope in a different task". `async (agent_name, on_text) -> None`.
        self._agent_switch_hook = None
        # `clear` wipes the live session's chat history. The agent server sets this to clear BOTH the
        # Director's in-memory transcript (what the LLM sees) and the persisted JSONL; a hand-built shell
        # (tests) leaves it None and clears in-memory only. `async (on_text) -> None`.
        self._clear_transcript_hook = None
        self._cwd = ""                   # this dispatch's working directory (per-connection; see _dispatch)
        # (matcher, handler, help, voice). First match wins; the unknown-command fallback is tried after,
        # in _dispatch. The handler is called as handler(on_text, match). Add a row to add a command.
        #
        # `voice=True` means: safe to invoke by voice AND its output is worth hearing. Everything else is
        # CLI-only — a spoken directory listing helps nobody, and deletion by voice is a bad idea.
        self._table = [
            # -- mode + orientation
            (_OPEN_SHELL, self._open, "open shell — enter command mode", True),
            (_LEAVE_SHELL, self._exit, "exit — leave the shell, back to the agent", True),
            (re.compile(r"^(?:help|\?|commands)(?:\s+(?P<topic>\S+))?$", re.I), self._help,
             "help [command] — list commands, or explain one", True),
            (re.compile(r"^(?:where|status)$", re.I), self._where,
             "where — user, agent, LLM, session, world and space in one line", True),
            (re.compile(r"^tools$", re.I), self._tools, "tools — what the active agent can call", False),

            # -- nouns: bare = list (current marked *), <name> = switch, verbs act on the LIVE one
            (re.compile(r"^agents?$", re.I), self._agents, "agent — list agents", True),
            (re.compile(r"^agent\s+(?P<name>[\w./-]+)$", re.I), self._switch_agent,
             "agent <name> — switch agent (relaunches its tools; its own sessions and worlds)", True),
            (re.compile(r"^(?:llms?|models?)$", re.I), self._llms, "llm — list LLMs", True),
            (re.compile(r"^llm\s+(?P<name>[\w.-]+)$", re.I), self._switch_llm,
             "llm <name> — switch the active LLM (spoken: 'talk to gemini')", True),
            (re.compile(r"^sessions$", re.I), self._sessions, "sessions — list this agent's sessions", True),
            (re.compile(r"^session(?:\s+(?P<rest>\S.*))?$", re.I), self._session,
             "session [new [title] | rename <title>] · session <name> · session <user> <name> — "
             "list / create / switch / visit (quote names with spaces)", True),
            (re.compile(r"^worlds$", re.I), self._worlds, "worlds — list this session's worlds", True),
            (re.compile(r"^world(?:\s+(?P<rest>\S.*))?$", re.I), self._world,
             "world [new] <name> — list, switch to, or create a world", True),
            (re.compile(r"^spaces?$", re.I), self._spaces, "spaces — list your captured spaces", False),
            (re.compile(r"^users?$", re.I), self._users, "users — everyone with a namespace here", False),
            (re.compile(r"^clear$", re.I), self._clear,
             "clear — wipe this session's chat history (keeps worlds and assets)", True),

            # -- paths: act on anything addressable
            (re.compile(r"^(?:dir|ls)(?:\s+(?P<path>\S.*))?$", re.I), self._dir,
             "dir [path] — list one level of the namespace", False),
            (re.compile(r"^(?:show|info)(?:\s+(?P<path>\S.*))?$", re.I), self._show,
             "show [path] — one entry in detail", False),
            (re.compile(r"^cd(?:\s+(?P<path>\S.*))?$", re.I), self._cd,
             "cd [path] — change the working directory (bare: back to your agent)", False),
            (re.compile(r"^(?P<vis>public|private)(?:\s+(?P<path>\S.*))?$", re.I), self._visibility,
             "public | private [path] — visibility of the live session, or of a path", True),
            (re.compile(r"^rename\s+(?P<rest>\S.*)$", re.I), self._rename,
             "rename <path> <new name> — retitle a world, space or session; relabel an asset "
             "(quote a path containing spaces)", False),
            (re.compile(r"^(?:delete|rm)\s+(?P<path>\S.*)$", re.I), self._delete,
             "delete <path> — remove a world, session, space, asset or user (asks to confirm)", False),
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

    async def _open_agent(self, agent: str, *, activate_world: bool = True) -> None:
        """Switch the active agent, tearing down the current one's MCP server and launching the new one.
        Order matters: the MCP client uses anyio task groups whose cancel scopes must unwind **LIFO in
        the same task**, so we CLOSE the current connection *before* opening the next (opening on top and
        closing underneath raises "exit cancel scope that isn't the current task's current"). If the new
        agent fails to start we restore the previous one so the shell isn't stranded. New agent = its own
        fresh transcript.

        `activate_world=True` (a user-driven switch) also asserts a world in the new scope on the world
        server. The agent server sets it **False** when *following* a world-server-driven agent change
        (shared-session C2): the world is already live there, so re-asserting would be a redundant loop."""
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
        if activate_world:
            await self._activate_world(agent)                 # make a world in the new agent's scope live

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

    async def feed(self, text: str, *, speaker: Optional[str] = None, on_text: Optional[OnText] = None,
                   on_tool: Optional[OnTool] = None) -> None:
        """Route one line: a recognised command runs here (deterministic); anything else goes to the
        active agent, attributed to `speaker` (WHO said it). `speaker` is per-call so one shell can serve
        many speakers (the agent server, shared-session-plan §6); it defaults to the shell's own `_user`
        for a single-user front-end."""
        cmd = self._as_command(text)
        if cmd is None:
            await self._director.handle(text, speaker=speaker or self._user, on_text=on_text, on_tool=on_tool)
        else:
            await self._dispatch(cmd, on_text)

    def as_command(self, raw: str, in_shell: bool) -> Optional[str]:
        """Route a line given the caller's shell MODE — the command string if it's a command, else None.
        In shell mode every line is a command; in agent mode only a `conjure`-led line is (bare `conjure`
        = open shell). **Mode is a parameter**, not instance state, so one shell serves many connections
        each with their own mode (the agent server passes each connection's `in_shell`)."""
        s = raw.strip()
        if in_shell:
            return s                                          # shell mode: every line is a command
        m = _WAKE.match(s)
        if m:
            return m.group("rest").strip() or "open shell"   # bare "conjure" → open the shell
        return None                                           # agent mode, no wake word → not a command

    def _as_command(self, raw: str) -> Optional[str]:
        return self.as_command(raw, self.in_shell)            # in-process (voice) path: instance mode

    @staticmethod
    def is_open_shell(cmd: str) -> bool:
        """Does this command string turn shell mode ON? (Recognised server-side; clients never know it.)"""
        return bool(_OPEN_SHELL.match(cmd))

    @staticmethod
    def is_leave_shell(cmd: str) -> bool:
        """Does this command string turn shell mode OFF (back to the agent)?"""
        return bool(_LEAVE_SHELL.match(cmd))

    async def _dispatch(self, cmd: str, on_text, *, speaker: Optional[str] = None,
                        permitted: bool = True, cwd: str = "", voice: bool = False) -> None:
        if not cmd:
            return
        # WHO typed this command (the connection's user), so identity-scoped verbs act as the speaker, not
        # the shared shell's host user (else a guest could manage the host's sessions). Read synchronously
        # by `_scope()`/`_require_permitted` at each handler's start (before any await), so concurrent
        # dispatches don't race. `permitted` = is the speaker allowed in the live session (§6d).
        # `cwd`/`voice` are likewise per-connection: one shell serves many clients, each with its own
        # working directory and its own idea of whether a directory listing is any use.
        self._acting = speaker or self._user
        self._permitted = permitted
        self._voice = voice
        self._cwd = cwd or default_cwd(self._acting, self._agent_name())
        if self._pending_delete is not None:                  # a delete is armed — this line is the y/n answer
            await self._confirm_delete(cmd, on_text)
            return
        if voice:                                             # spoken aliases for `llm <name>`
            sm = _SPOKEN_LLM.match(cmd)
            if sm and await self._switch_llm(on_text, sm):
                return
        for rx, handler, _, is_voice in self._table:
            m = rx.match(cmd)
            if m:
                if voice and not is_voice:
                    await self._say(on_text, f"'{cmd.split()[0]}' is a terminal command — "
                                             f"run it from the CLI.")
                    return
                await handler(on_text, m)
                return
        await self._say(on_text, f"Unknown command: {cmd!r}. Type 'help'.")

    @property
    def cwd(self) -> str:
        """The working directory after the last dispatch — the caller persists it per connection."""
        return self._cwd

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
        topic = (m.group("topic") or "").strip().lower() if m and m.groupdict().get("topic") else ""
        if topic:                                             # `help <command>` — the row whose help starts with it
            hit = next((h for _, _, h, _ in self._table if h.split()[0].lower() == topic), None)
            await self._say(on_text, hit or f"No command {topic!r}. Type 'help'.")
            return
        rows = [(h, v) for _, _, h, v in self._table]
        if self._voice:                                       # spoken: only what's worth hearing
            lines = ["Commands:"] + [f"  {h}" for h, v in rows if v]
        else:
            lines = ["Commands  (· = also available by voice):"] + \
                    [f"  {'·' if v else ' '} {h}" for h, v in rows]
        lines.append("(While talking to an agent, prefix a command with 'conjure', e.g. 'conjure open shell'.)")
        await self._say(on_text, "\n".join(lines))

    async def _where(self, on_text, m=None):
        """One line locating you: who, which agent and LLM, which session, world and space. The most
        useful thing to be able to ask by voice — there's no status bar in a headset."""
        d = self._director
        sess = await self._session_api("GET", "/sessions", scope=self._scope())
        if not sess.get("ok"):
            session_str = "unknown"
        elif sess.get("active"):
            active = sess["active"]
            title = next((s.get("title") for s in sess.get("sessions", []) if s.get("id") == active), active)
            session_str = f"{title} ({active})"
        else:
            session_str = "none"
        w = await self._session_api("POST", "/worlds/list", scope=self._scope())
        cur = (w.get("current") or {}) if w.get("ok") else {}
        world = f"{cur.get('owner', '?')}/{cur.get('name', '?')}" if cur else "?"
        await self._say(on_text,
                        f"user: {self._acting} · agent: {self._agent_name()} · LLM: {d.active} · "
                        f"session: {session_str} · world: {world} · "
                        f"{'shell' if self.in_shell else 'agent'} mode "
                        f"({len(d.roster)} LLMs, {len(d._tools)} tools)")

    async def _tools(self, on_text, m=None):
        """What the active agent can actually call. The one thing you want when it won't do something —
        and, at 45 schemas, usually the largest slice of its context."""
        names = sorted(t.name for t in (self._director._tools or []))
        if not names:
            await self._say(on_text, "No tools loaded.")
            return
        await self._say(on_text, f"Tools ({len(names)}) for {self._agent_name()}:\n" +
                        "\n".join("  " + n for n in names))

    async def _clear(self, on_text, m=None):
        if not self._permitted:
            await self._say(on_text, "You're a guest here — only someone in the session can clear its history.")
            return
        await self._do_clear(on_text)

    async def _do_clear(self, on_text):
        # Wipe the live session's chat history (permission gated by the caller).
        if self._clear_transcript_hook is not None:   # hosted (agent server): clears in-memory + persisted
            await self._clear_transcript_hook(on_text)
            return
        if self._director is not None:                 # hand-built shell / tests: in-memory only
            self._director.transcript = []
        await self._say(on_text, "Chat history cleared.")

    async def _llms(self, on_text, m=None):
        rows = [("* " if n == self._director.active else "  ") + n for n in self._director.roster]
        await self._say(on_text, "LLMs:\n" + "\n".join(rows))

    async def _switch_llm(self, on_text, m) -> bool:
        """`llm <name>` (typed) / "talk to <name>" (spoken). Returns True when it matched a roster name,
        so the spoken form can fall through to the agent when it didn't."""
        name = _match_name(m.group("name"), self._director.roster)
        if not name:
            if not self._voice:                               # typed: a wrong name is a mistake, say so
                avail = ", ".join(self._director.roster) or "none"
                await self._say(on_text, f"No LLM {m.group('name')!r}. Available: {avail}.")
                return True
            return False
        if not self._permitted:                               # the active LLM is SHARED — a shared-effect
            await self._say(on_text, "This session is private — you can't change the LLM here.")   # verb (§6d)
            return True
        self._director.active = name
        await self._say(on_text, f"Now talking to {name} ({self._agent_name()}).")
        return True

    def _agent_names(self) -> list[str]:
        """Available agent names across the search path (user defs shadow bundled — user-home-plan §5)."""
        return agent_names()

    async def _agents(self, on_text, m=None):
        active = self._agent_name()
        rows = [("* " if n == active else "  ") + n + ("" if src == "bundled" else "  (user)")
                for n, src in list_agents()]
        await self._say(on_text, "Agents:\n" + "\n".join(rows))

    async def _switch_agent(self, on_text, m):
        if not self._permitted:                               # switching the shared agent is a shared-effect
            await self._say(on_text, "This session is private — you can't switch the agent here.")   # verb (§6d)
            return
        name = m.group("name").strip()
        match = next((a for a in self._agent_names() if a.lower() == name.lower()), None)
        if not match:
            avail = ", ".join(self._agent_names()) or "none"
            await self._say(on_text, f"No agent {name!r}. Available: {avail}.")
            return
        if match == self._agent_name():
            await self._say(on_text, f"Already on {match}.")
            return
        if self._agent_switch_hook is not None:               # hosted (agent server): delegate — route via
            await self._agent_switch_hook(match, on_text)     # the world server so every client follows and
            return                                            # the host re-binds the Director in its own task
        if self._stack is None:                               # hand-built Shell(director) — no lifecycle
            await self._say(on_text, "Agent switching isn't available in this session.")
            return
        try:
            await self._open_agent(match)                     # relaunches its MCP server; keeps the current agent on failure
        except Exception as exc:                              # bad def, no LLM key for it, server won't start
            await self._say(on_text, f"Couldn't switch to {match}: {exc}")
            return
        await self._say(on_text, f"Switched to agent {match} ({self._director.active}).")

    # -- sessions: list/new/switch/rename/delete, driven through the world server (the source of truth for
    # the live (scope, session)). A session switch doesn't re-bind the Director — same agent/tools; the
    # agent server's /ws follower just swaps the transcript (step 2) — so these can run in the connection
    # task and POST directly, no cross-task hook (unlike agent switching, docs/sessions-plan.md §3).
    def _scope(self) -> str:
        return scope_for(self._acting, self._agent_name())    # the SPEAKER's scope (set per-dispatch), not host

    async def _session_api(self, method: str, path: str, **kw) -> dict:
        url = getattr(self._settings, "world_url", None) if self._settings else None
        if not url:
            return {"ok": False, "error": "no world server configured"}
        try:
            import httpx
            # Identify the caller so the server can gate a cross-user session VISIT (public-only). Same
            # header the MCP client / headset send; harmless on the same-scope calls.
            headers = {"X-Conjure-User": self._acting}
            # `session new` can run a generative constructor (skybox-from-description) server-side, which
            # takes tens of seconds — allow for it (the client's long-turn heartbeat covers the wait).
            async with httpx.AsyncClient(timeout=180.0) as client:
                resp = await (client.get(f"{url}{path}", params=kw, headers=headers) if method == "GET"
                              else client.post(f"{url}{path}", json=kw, headers=headers))
                try:
                    return resp.json()
                except ValueError:                        # non-JSON (e.g. a plain-text 500) → a useful line
                    return {"ok": False, "error": f"server error {resp.status_code}: {resp.text[:200].strip()}"}
        except Exception as exc:  # noqa: BLE001 — network / server down
            return {"ok": False, "error": f"session request failed: {exc}"}

    @staticmethod
    def _mark(is_live: bool, is_last: bool) -> str:
        """Row marker: `@` = the one live session (you're here) — wins over `*` = your last-used in this
        agent (resume target). Two space if neither."""
        return "@ " if is_live else ("* " if is_last else "  ")

    async def _sessions(self, on_text, m=None):
        scope = self._scope()
        data = await self._session_api("GET", "/sessions", scope=scope)
        if not data.get("ok"):
            await self._say(on_text, data.get("error", "error"))
            return
        live = data.get("live") or {}
        rows = []
        for s in data.get("sessions", []):
            is_live = live.get("scope") == scope and live.get("session") == s["id"]
            extra = f", {s['llm']}" if s.get("llm") else ""
            vis = "" if s.get("public", True) else ", private"
            rows.append(self._mark(is_live, bool(s.get("active")))
                        + f"{s.get('title')} ({s['id']}) — world {s.get('active_world')}{extra}{vis}")
        text = "Sessions:\n" + ("\n".join(rows) or "  (none)")
        # Other users' public sessions you can VISIT (session-scoping-plan §B), scoped to THIS agent — a
        # human act, so it lives here in the shell, not in the agent's world tools. Visit by name.
        avail = data.get("available", [])
        if avail:
            pub = []
            for a in avail:
                is_live = live.get("scope") == a["scope"] and live.get("session") == a["session"]
                pub.append(self._mark(is_live, False)
                           + f"{a['owner']}  \"{a.get('title')}\"  — world {a.get('active_world')}")
            text += ("\n\nOther users' public sessions (visit: session <user> <name>):\n" + "\n".join(pub))
        text += "\n\n@ = live session (you're here) · * = your last-used in this agent"
        await self._say(on_text, text)

    async def _session(self, on_text, m):
        rest = (m.group("rest") or "").strip()
        if not rest:                                          # bare `session` → list
            await self._sessions(on_text)
            return
        try:
            tokens = shlex.split(rest)                        # quote-aware, so names with spaces survive
        except ValueError:                                    # unbalanced quotes → naive split
            tokens = rest.split()
        verb = tokens[0].lower() if tokens else ""
        scope = self._scope()
        # switch/visit/bare `session <…>` are everything that isn't a management verb. `delete`, `clear`
        # and visibility are no longer session sub-verbs: they're `delete <path>`, `clear` and
        # `public|private [path]`, so there's one way to do each of them across every noun.
        is_switch = verb not in ("new", "rename")
        # Shared-effect verbs move the GLOBAL live pointer (everyone follows) — allowed only for a speaker
        # permitted in the live session (§6d), so a bumped guest can't yank everyone out of a private one.
        if (verb == "new" or is_switch) and not self._permitted:
            await self._say(on_text, "This session is private — ask its owner to make it public "
                                     "before switching or creating sessions here.")
            return
        if verb == "new":
            data = await self._session_api("POST", "/session/new", scope=scope, title=" ".join(tokens[1:]) or None)
            msg = f"Created and switched to {data.get('title')} ({data.get('session')})."
        elif verb == "rename":
            title = " ".join(tokens[1:])
            if not title:
                await self._say(on_text, "Usage: session rename <new title>")
                return
            data = await self._session_api("POST", "/session/rename", scope=scope, title=title)
            msg = f"Renamed to {title}."
        else:                                                 # switch/visit: `session [switch] <name>` OR
            args = tokens[1:] if verb == "switch" else tokens #                `session [switch] <user> <name>`
            if len(args) == 1:                                # your own session (by name, in your agent)
                data = await self._session_api("POST", "/session/switch", scope=scope, session=args[0])
                whose = ""
            elif len(args) == 2:                              # VISIT <user>'s session, in your active agent
                data = await self._session_api("POST", "/session/switch",
                                               scope=scope, owner=args[0], session=args[1])
                whose = f" ({data.get('owner', args[0])}'s)"
            else:
                await self._say(on_text, "Usage: session <name>  |  session <user> <name>  "
                                         "(quote a name with spaces)")
                return
            msg = f"Switched to session {data.get('session')}{whose}."
        await self._say(on_text, msg if data.get("ok") else data.get("error", "error"))

    # -- dir / delete: a filesystem-like view + purge of the namespace (docs/agents.md §2). Both go
    # through the world server's /admin endpoints, so they act on its live state (not raw files).
    def _path(self, m, default: str = "") -> str:
        """The path argument of a command, resolved against this connection's cwd."""
        raw = (m.group("path") or "") if (m and m.groupdict().get("path")) else ""
        return resolve_path(self._cwd, unquote_arg(raw) or default, self._acting)

    async def _dir(self, on_text, m):
        path = self._path(m)
        data = await self._admin("tree", path)
        if not data.get("ok"):
            await self._say(on_text, data.get("error", "error"))
            return
        await self._say(on_text, self._render_listing(data))

    async def _show(self, on_text, m):
        data = await self._admin("show", self._path(m))
        if not data.get("ok"):
            await self._say(on_text, data.get("error", "error"))
            return
        width = max((len(k) for k, _ in data.get("fields", [])), default=0)
        rows = "\n".join(f"  {k:<{width}}  {v}" for k, v in data.get("fields", []))
        await self._say(on_text, f"{display_path(data['path'], self._acting)}\n{rows}")

    async def _cd(self, on_text, m):
        """Bare `cd` returns to your own agent scope — the useful default, not the root."""
        target = self._path(m, default=default_cwd(self._acting, self._agent_name()))
        data = await self._admin("tree", target)
        if not data.get("ok"):
            await self._say(on_text, data.get("error", "error"))
            return
        # Adopt the path the SERVER resolved, not the one typed: `…/worlds` is a shortcut for the active
        # session's worlds, and remembering the shortcut would silently point elsewhere after a switch.
        self._cwd = data.get("path") or target
        await self._say(on_text, display_path(self._cwd, self._acting))

    async def _visibility(self, on_text, m):
        """`public`/`private` — bare acts on the live session (the common case, and voice-safe); with a
        path, on that session, space or asset."""
        public = m.group("vis").lower() == "public"
        raw = (m.group("path") or "").strip()
        if not self._permitted:
            await self._say(on_text, "This session is private — you can't change visibility here.")
            return
        if not raw:
            data = await self._session_api("POST", "/session/visibility", scope=self._scope(), public=public)
            await self._say(on_text, f"Session is now {'public' if public else 'private'}."
                            if data.get("ok") else data.get("error", "error"))
            return
        path = resolve_path(self._cwd, unquote_arg(raw), self._acting)
        data = await self._admin("show", path)
        if not data.get("ok"):
            await self._say(on_text, data.get("error", "error"))
            return
        kind, fields = data.get("kind"), dict(data.get("fields", []))
        if kind == "space":
            out = await self._session_api("POST", "/space/visibility", name=fields.get("space"), public=public)
        elif kind == "session":
            out = await self._session_api("POST", "/session/visibility", scope=fields.get("scope"),
                                          session=fields.get("session"), public=public)
        elif kind == "asset":
            out = await self._session_api("POST", "/update_asset", id=fields.get("asset"),
                                          scope=fields.get("scope"), public=public)
        else:
            await self._say(on_text, f"A {kind} has no visibility of its own — "
                                     f"a world inherits its session's.")
            return
        await self._say(on_text, f"{display_path(path, self._acting)} is now "
                                 f"{'public' if public else 'private'}."
                        if out.get("ok") else out.get("error", "error"))

    async def _rename(self, on_text, m):
        """Retitle anything with a display name: a world, a space, a session, or an asset's label.

        Worlds and spaces are safe to rename because their identity is a permanent id, not their name —
        so a rename moves no file and strands nothing, not the active pointers, not a space's
        `last_world`, not another user's `environment.space`, and not whatever a schema-free agent state
        doc stashed. (That's what the shelved alias scheme was for; ids removed the need.)"""
        # Names routinely contain spaces now, so the PATH may too — take it quote-aware rather than
        # assuming the first whitespace ends it.
        try:
            tokens = shlex.split(m.group("rest"))
        except ValueError:
            tokens = m.group("rest").split()
        if len(tokens) < 2:
            await self._say(on_text, 'Usage: rename <path> <new name>   (quote a path with spaces)')
            return
        path = resolve_path(self._cwd, tokens[0], self._acting)   # shlex already unquoted it
        new = " ".join(tokens[1:]).strip()
        data = await self._admin("show", path)
        if not data.get("ok"):
            await self._say(on_text, data.get("error", "error"))
            return
        kind, fields = data.get("kind"), dict(data.get("fields", []))
        if kind == "session":
            out = await self._session_api("POST", "/session/rename", scope=fields.get("scope"),
                                          session=fields.get("session"), title=new)
        elif kind == "asset":
            out = await self._session_api("POST", "/update_asset", id=fields.get("asset"),
                                          scope=fields.get("scope"), label=new)
        elif kind == "world":
            out = await self._session_api("POST", "/worlds/rename", scope=fields.get("scope"),
                                          session=fields.get("session"),
                                          name=fields.get("id") or loc_name(path), new_name=new)
        elif kind == "space":
            out = await self._session_api("POST", "/space/rename", owner=fields.get("owner"),
                                          name=fields.get("space"), new_name=new)
        else:
            await self._say(on_text, f"Can't rename a {kind} — worlds, spaces, sessions and assets have "
                                     f"names; the rest are containers.")
            return
        await self._say(on_text, f"Renamed to {new}." if out.get("ok") else out.get("error", "error"))

    async def _delete(self, on_text, m):
        if not self._permitted:                               # destructive — refuse for a bumped guest (§6d)
            await self._say(on_text, "This session is private — you can't delete anything here.")
            return
        path = self._path(m)
        preview = await self._admin("tree", path)             # resolve + show what's about to go
        if not preview.get("ok"):
            await self._say(on_text, preview.get("error", "error"))
            return
        # Confirm against the path the SERVER resolved, so a `worlds` shortcut shows the real session
        # it points at — you should see exactly what you're agreeing to remove.
        path = preview.get("path") or path
        self._pending_delete = path
        await self._say(on_text, f"Delete {display_path(path, self._acting)} "
                                 f"({self._summarize(preview)})?  "
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

    # -- nouns backed by the world server -------------------------------------------------------
    async def _worlds(self, on_text, m=None):
        await self._dir_at(on_text, f"/{self._acting}/agents/{self._agent_name()}/worlds")

    async def _world(self, on_text, m):
        """`world` = list · `world <name>` = switch · `world new <name>` = create and switch."""
        rest = (m.group("rest") or "").strip() if m and m.groupdict().get("rest") else ""
        if not rest:
            await self._worlds(on_text)
            return
        try:
            tokens = shlex.split(rest)
        except ValueError:
            tokens = rest.split()
        if not self._permitted:                               # switching/creating moves everyone (§6d)
            await self._say(on_text, "This session is private — you can't change worlds here.")
            return
        if tokens[0].lower() == "new":
            name = " ".join(tokens[1:])
            if not name:
                await self._say(on_text, "Usage: world new <name>")
                return
            data = await self._session_api("POST", "/worlds/new", scope=self._scope(), name=name)
            msg = f"Created and switched to world {name}."
        else:
            name = " ".join(tokens)
            data = await self._session_api("POST", "/worlds/switch", scope=self._scope(), name=name)
            msg = f"Switched to world {name}."
        await self._say(on_text, msg if data.get("ok") else data.get("error", "error"))

    async def _spaces(self, on_text, m=None):
        await self._dir_at(on_text, f"/{self._acting}/spaces")

    async def _users(self, on_text, m=None):
        await self._dir_at(on_text, "/")

    async def _dir_at(self, on_text, path: str) -> None:
        data = await self._admin("tree", path)
        await self._say(on_text, self._render_listing(data) if data.get("ok")
                        else data.get("error", "error"))

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
                resp = await client.post(f"{url}/admin/{action}", json={"path": path},
                                         headers={"X-Conjure-User": self._acting})   # WHO is browsing/deleting
                return resp.json()
        except Exception as exc:                              # network / server down / bad JSON
            return {"ok": False, "error": f"admin request failed: {exc}"}

    def _render_listing(self, data: dict) -> str:
        """One line per entry, columns aligned. Deliberately NOT a tree: the recursive form dumped every
        world, space and asset of every user at the root, which is unreadable at any real size."""
        rows = data.get("children") or []
        if self._voice:                                   # a path read aloud is noise; the rows are the answer
            return "\n".join(f"{c['label']}" + (f" — {c['detail']}" if c.get("detail") else "")
                              for c in rows) or "(empty)"
        head = display_path(data.get("path", "/"), self._acting)
        if data.get("path") != data.get("requested", data.get("path")):
            head += f"   (→ {data['path']})"
        if not rows:
            return f"{head}\n  (empty)"
        # A trailing '/' marks the things you can `cd` into, so the shape of the namespace is visible.
        def label(c):
            return c["label"] + ("/" if c.get("kind") in ("category", "user", "agent") else "")
        width = max(len(label(c)) for c in rows)
        out = [head]
        for c in rows:
            mark = "*" if c.get("active") else " "
            detail = f"  {c['detail']}" if c.get("detail") else ""
            out.append(f" {mark}{label(c):<{width}}{detail}".rstrip())
        return "\n".join(out)

    def _summarize(self, data: dict) -> str:
        """What a `delete` is about to take — shown before the confirmation. Counts the children for a
        container; for a single entry uses the server's own row, since a session's children are just
        `worlds/` and `state/` and counting those would report "nothing" for a real deletion."""
        counts: dict = {}
        for c in data.get("children") or []:
            k = c.get("kind")
            if k and k not in ("note", "category", "shortcut"):
                counts[k] = counts.get(k, 0) + 1
        me = data.get("self") or {}
        if me and (not counts or counts == {me.get("kind"): 1}):
            detail = f" — {me['detail']}" if me.get("detail") else ""
            return f"{me.get('kind', 'entry')} {me.get('label', '')}{detail}".strip()
        if counts:
            return ", ".join(f"{v} {k}{'' if v == 1 else 's'}" for k, v in sorted(counts.items()))
        return f"the whole {data.get('kind', 'entry')}"

    async def _say(self, on_text, text: str) -> None:
        if on_text:
            await on_text(text, final=True, speaker="shell")
