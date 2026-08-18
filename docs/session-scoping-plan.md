# Session-scoped worlds + human-driven visiting — plan

**Status:** BUILT (2026-08-18). The agent's world tools are session-local; cross-user visiting is a
human act via the shell `session switch <user>/<agent>/<sid>`, gated public. Discovery of other users'
public sessions is surfaced by the shell `sessions` command (not `dir` — chosen for cohesion with the
existing session listing). See `worlds.list_public_sessions`, `/sessions` `available`,
`/session/switch` public-gate, and `Shell._session_target`.

Tighten the boundary the storage already implies: **an agent only knows the worlds in its own
session**, and **cross-user visiting is a human act at the shell, at the session level** — not a
world-level capability handed to the LLM. This removes the class of bug where a user's own worlds
under a *different agent* surface as "another user's" worlds the agent can neither own nor reach.

## Motivation — the bug this fixes

A user (`daniel/agents/builder`) asked "which worlds do I own." The director reported it owned only
`animal-house` and that `forest`, `futuristic-city`, `meadow`, … "belong to a **different daniel**
user." There is only one daniel. Those worlds are real files under the *same* user's **other agents**
(`daniel/agents/outdoor`, `daniel/agents/scratch`), created earlier under those personas.

Root cause is a key mismatch in the world listing:

- Worlds are **partitioned by full scope** (`<user>/agents/<agent>` + active session):
  `worlds.list(scope)` = only the caller's active session (e.g. `world.py:283`).
- But the "available / other users' public worlds" list is built by
  `worlds.list_public(exclude_scope=req.scope)` (`world.py:288-320`), which walks **every**
  `*/agents/*/sessions/*`, excludes **only the exact caller-scope string** (`world.py:303`), and labels
  each by the **user prefix only** — `owner = scope.split("/",1)[0]` (`world.py:305`).

So same-user/other-agent worlds are (a) excluded from "yours" and (b) re-listed under *"Other users'
public worlds"* still tagged `daniel`. The director (`mcp_server.py` `list_worlds`, prints the
"Other users' public worlds" block) reads that literally and confabulates a second daniel. The
partition key (full scope) is finer than the display key (user) — that mismatch is the whole bug.

Secondary symptoms, same cause: `switch to forest` fails (cross-user branch in `worlds_switch`,
`server.py:1663`, is gated on `owner != caller`; `owner="daniel"` == caller, so it falls through to
`worlds.exists(builder, "forest")` → False), and the user's earlier `delete_world` calls silently
no-op'd (the tool only targets the caller's own scope, `mcp_server.py` `delete_world`).

## Principle

**Never partition by one key and label by another.** Whatever a listing shows, label each item by the
key that actually gates access to it. Corollary for this design: the agent's world vocabulary is
exactly its session's worlds — nothing cross-agent or cross-user is ever handed to the LLM, so it has
nothing to mislabel.

## Target model

The session is the unit an agent inhabits. Worlds live inside sessions. Therefore:

- **In-session world-switching is the agent's job** — switch among the worlds in *its* session.
- **Cross-session / cross-user movement is a human act at the shell**, at the session level.
- **Visiting preserves identity.** Entering another user's public session admits you as a **guest**
  (you, not them): the `/ws` gate is `joined = (user == owner) or public` and edits are owner-only
  (`server.py:3466-3478`). `active_scope` records *whose session is live*, never *who you are*.
- **One shared reality.** There is a single global live session (`_session.txt`, the `active_*`
  globals). Visiting repoints that one pointer — everyone present travels together. Appropriate that a
  human, not the LLM mid-turn, pulls that lever.

### Who sees / does what

| A world is… | Today | Target |
|---|---|---|
| in the agent's current session | "yours" ✓ | "yours" — the agent's whole world vocabulary |
| the same user's **other session** (same agent) | leaks as "other user" ✗ | reached by human session-switch |
| the same user's **other agent** | leaks as "other user" ✗ (the bug) | reached by human `agent <name>` |
| a **different user's** public world | shown mixed into the agent's list ✗ | reached by human **visit** (session-level) |

## Changes

### A. Agent world tools become session-local
Drop cross-boundary reach from what the LLM is handed:
- `list_worlds` (`mcp_server.py`) → returns/prints only the caller's current-session worlds + which is
  active. Remove the "Other users' public worlds" block.
- `worlds_list` (`server.py:1622-1633`) → stop folding in `worlds.list_public(...)` for the agent path
  (or split it behind a flag the agent path doesn't set).
- `switch_world` (`mcp_server.py`) / `worlds_switch` (`server.py:1663`) → remove the `owner` param from
  the **agent-facing** tool; keep switching among the session's own worlds. (Keep in-session switch.)
- `delete_world` stays session-scoped (already is) — now consistent, because the agent never sees
  worlds it can't delete.

Result: the "another daniel" confabulation becomes **structurally impossible** — the LLM is never
handed a world outside its session.

### B. Extend the existing shell session-switch with an optional cross-user path
**No new verb.** Keep the current shell session-switch syntax; enrich it with optional path info that
names another user's public session. The shell is already the cross-cutting human layer —
`dir /alice/worlds` browses across users and `delete <path>` spans scopes (`shell.py:68-85`) — so a
cross-user *switch* target is a natural extension of what's already there.

- **Same-scope (unchanged):** `session switch <name-or-id>` → the caller's own scope, exactly as today
  (`shell.py` `_session`, posts to `/session/switch` with `scope=self._scope()`).
- **Cross-user (new, optional):** `session switch <path>` where `<path>` is a `dir`-style rooted path
  to another user's public session (e.g. `/alice/…`, mirroring the `dir /alice/worlds` convention).
  The shell resolves the path to a target `(scope, sid)` and posts it; the server switches only if that
  target session is **public** (a `_active_public`-style check on the *target*, not the caller) and
  otherwise refuses with the "ask its owner to make it public" info message. On success it calls the
  existing `_switch_to(scope, world)` (`server.py:1161-1185`), which repoints the one global pointer and
  lands everyone in that session's active world. **No identity change** — the caller stays themselves,
  admitted as a guest (can inhabit, owner-only writes still refuse edits).
- **Server:** `/session/switch` (`SessionRef`, `server.py:1264`) grows an optional cross-user target
  (the `scope`/`sid` of the chosen public session). The endpoint already accepts a `scope`; add the
  public-gate on cross-scope targets so it can't be used to jump into a private session.
- **Discovery:** list **public sessions of *other* users** so the human has paths to copy. This is
  `list_public` with the exclusion widened from `exclude_scope` to **`exclude_user`** (drop all
  `<caller-user>/agents/*`) so the list is only genuinely other users, labeled by user. `list_public`
  already returns `{scope, owner, name, session}` (`world.py:318`) — enough to render a copy-pasteable
  path per row. Surface it through the existing browse path (e.g. `dir` showing public sessions), not a
  bespoke command.

### C. Discovery excludes your own user
Whichever surface lists "visitable" sessions must exclude the caller's **entire user prefix**, not
just the exact scope. This is the one-line fix that stops your own other agents from ever masquerading
as strangers, and it makes "label by user" correct again (every row is a different user).

### D. Framing
State the boundary in the `list_worlds` tool description and director prompt: *"You only know the
worlds in your current session. Other sessions and agents are separate; other users' worlds are
visited by a person at the shell."* Even with the data fixed, this stops the LLM from narrating around
a gap.

## Decisions (resolved 2026-08-18)

1. **Session per-`(user, agent)` — keep for now.** Storage stays under
   `<user>/agents/<agent>/sessions/…`; a user can have several public sessions (one per persona), so a
   cross-user session-switch targets a **specific** public `(user, agent, session)` triple (discovery
   lists them flat, labeled by user + session title / path). Re-keying sessions by user (agent = just
   the current driver) is a cleaner long-term model but a larger, separable change — deliberately
   deferred, not decided here. ✅
2. **"Everyone travels" — yes.** A cross-user session-switch moves the single shared reality for all
   connected clients (no per-connection session state, by design — `shared-session-plan.md` P1). This
   is intended (co-located-household holodeck). Per-user independent visiting is out of scope. ✅
3. **No world-level cross-user switch — retire it entirely.** Remove the `owner` path from
   `worlds_switch` / the `switch_world` MCP tool; do **not** add a shell world-switch sugar either. The
   only cross-user movement is the shell session-switch (§B). World-switching is exclusively the
   agent's *in-session* job. ✅
4. **No new `visit` verb.** Cross-user movement reuses the existing `session switch` syntax with an
   optional path (§B), not a bespoke command. ✅

## Invariant (to prevent recurrence)
Any listing's displayed owner **must be the exact key that gates access to it.** If two rows can't be
told apart by their label (two "daniel: forest"), the label is wrong — widen the filter or the label
until the visible key is unambiguous.

## Non-goals / preserve
- **Headset co-location is unaffected** — space admission goes through spaces / `/space/select` /
  presence, not the director world-listing. Cross-user *visiting* stays available (relocated to the
  shell, honest).
- **Admin cleanup stays** — the shell `delete /<user>/worlds/<name>` path spans a user's agent scopes
  (`server.py` `_admin_delete_worlds`) for bulk removal (e.g. the stranded `outdoor`/`scratch` worlds).
- **No per-connection sessions** — this plan does not introduce per-user independent worlds.

## Test sketch
- `list_worlds` under `daniel/agents/builder` returns only its session's worlds; no "other users"
  block; never lists `outdoor`/`scratch` worlds.
- The agent-facing `switch_world` has no `owner` reach; switching to a non-session world errors cleanly.
- Shell cross-user switch: `session switch <path-to-another-users-public-session>` repoints the live
  session, admits the caller as guest (can inhabit, owner-only writes still refuse edits); a **private**
  target refuses with the "ask its owner to make it public" message; a same-scope `session switch <name>`
  behaves exactly as today.
- Discovery excludes the caller's own user entirely (no same-user rows).

## Rollout / risk
Mostly *removal* + one small shell verb. Low risk. The one thing to verify during implementation:
nothing outside the director depends on `worlds_list` returning `available` (grep call sites). Migrate
nothing on disk — this only changes what is *surfaced*; existing stray worlds are cleaned up
operationally (human `agent <name>` + delete, or the admin delete path).
