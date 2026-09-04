"""The namespace — the user tree as an addressable filesystem (docs/specs/agents.md §6.4).

The shell's `dir` / `show` / `delete` see storage as a path space, and this module is the whole of that
view: resolving a path to a location, listing one level of it, describing one entry, and purging one.
The world server keeps the three `/admin/*` routes and calls in here.

Paths mirror STORAGE and are agent-explicit. The one thing that isn't obvious from the outside: worlds
live PER SESSION — `WorldRepository(USERS_DIR, sessions=…)` routes every per-name op to the scope's
ACTIVE session's `worlds/` dir. So two sessions under one agent each own a separate set of worlds, and
a listing that hides the session level merges them invisibly.

    /                                                   users
    /<user>                                             agents/ · spaces/
    /<user>/spaces[/<name>]                             spaces are user-level, shared across agents
    /<user>/agents[/<agent>]                            sessions/ · assets/ · worlds→
    /<user>/agents/<a>/assets[/<id>]                    library rows scoped `<user>/agents/<a>` (virtual —
                                                        assets are SQLite rows, not files)
    /<user>/agents/<a>/sessions[/<sid>]                 worlds/ · state/
    /<user>/agents/<a>/sessions/<sid>/worlds[/<name>]   <name> may be nested (`castle/hall`)
    /<user>/agents/<a>/worlds                           SHORTCUT → the ACTIVE session's worlds; resolves to
                                                        the real path, so it can never go stale
    /<user>/assets[/<id>]                               legacy rows scoped to a bare user (no agent segment)

`dir` lists ONE level (the old recursive dump was unusable at any real size); `show` returns one entry
in depth. Deletes refuse whatever is ACTIVE — autosave would resurrect it and leave the in-memory store
inconsistent.
"""
from __future__ import annotations

import re
from typing import NamedTuple, Optional

from .world import NAME_SEGMENT

# Exactly what a display NAME may contain (`world.NAME_SEGMENT`) — one definition, so a name can never be
# stored that this then refuses to address. `clean_name` enforces the same rule on write. Everything but
# a path separator or a control character is allowed: this is defence-in-depth, not the traversal gate —
# "."/".." are rejected in `resolve` and every segment is checked against an enumerated real set below.
# Display names may contain spaces ("Living Room"), so a path segment allows them; `/` is still the
# separator.
SEGMENT = re.compile(NAME_SEGMENT)


# --------------------------------------------------------------------------- the host binding
#
# Every function here reads the world server's repositories (`worlds`, `sessions`, `spaces`, `library`)
# and its live pointers (`active_scope`, `active_sid`, `active_world`, …). Those are module globals over
# there, and both kinds MOVE: `_init_state` rebinds the repositories, and the live pointers change on
# every world or session switch. So this module holds a reference to the host MODULE and reads the
# attribute at call time — capturing values at import would pin a test to the previous run's
# repositories, and capturing the pointers would freeze them at whatever was live when we loaded.
#
# A module reference rather than an import both avoids the cycle (the host imports us) and leaves a seam:
# a test can bind a stub with the handful of attributes below.

_host = None


def bind(host) -> None:
    """Point the namespace at the module owning the live state — `conjure.server` in the running app."""
    global _host
    _host = host


def _h():
    if _host is None:
        raise RuntimeError("conjure.namespace is unbound — call bind(server) before serving a request")
    return _host


class Loc(NamedTuple):
    """A resolved path. `kind` names what it points at; the rest is filled in as far as the path goes."""
    kind: str                      # root|user|agents|agent|sessions|session|worlds|world|assets|asset|
                                   # spaces|space
    user: str = ""
    agent: str = ""
    sid: str = ""
    name: str = ""

    @property
    def scope(self) -> str:
        return f"{self.user}/agents/{self.agent}" if self.agent else ""

def _split(path: str) -> list[str]:
    return [s for s in (path or "").strip().strip("/").split("/") if s]

def active_user() -> str:
    return _h().active_scope.split("/", 1)[0]

def all_users() -> list[str]:
    return sorted(set(_h().worlds.list_users()) | set(_h().spaces.list_users()) | set(_h().library.list_users()))

def agents(user: str) -> list[str]:
    """Agent names for a user — those with worlds/sessions on disk, plus any that only own assets."""
    names = {s.rsplit("/", 1)[-1] for s in _h().worlds.user_scopes(user)}
    for row in _h().library.by_user(user, limit=10_000):
        sc = row.get("scope") or ""
        if "/agents/" in sc and sc.split("/", 1)[0] == user:
            names.add(sc.rsplit("/", 1)[-1])
    return sorted(names)

def resolve(path: str):
    """`path` → a `Loc`, or an error string. The `worlds` shortcut at agent level resolves here, so
    everything downstream only ever sees real, unambiguous locations."""
    segs = _split(path)
    for seg in segs:
        if seg in (".", "..") or not SEGMENT.fullmatch(seg):
            return f"bad path segment {seg!r}"
    if not segs:
        return Loc("root")
    user = segs[0]
    if user not in all_users():
        return f"no such user {user!r}"
    if len(segs) == 1:
        return Loc("user", user)

    cat = segs[1]
    if cat == "spaces":
        if len(segs) == 2:
            return Loc("spaces", user)
        return Loc("space", user, name="/".join(segs[2:]))
    if cat == "assets":                                        # legacy: rows scoped to a bare user
        if len(segs) == 2:
            return Loc("assets", user)
        return Loc("asset", user, name=segs[2])
    if cat != "agents":
        return f"unknown category {cat!r} (agents|spaces)"
    if len(segs) == 2:
        return Loc("agents", user)

    agent = segs[2]
    if agent not in agents(user):
        return f"no agent {agent!r} for {user!r}"
    if len(segs) == 3:
        return Loc("agent", user, agent)

    scope = f"{user}/agents/{agent}"
    sub = segs[3]
    if sub == "assets":
        if len(segs) == 4:
            return Loc("assets", user, agent)
        return Loc("asset", user, agent, name=segs[4])
    if sub == "worlds":                                        # shortcut → the active session's worlds
        return resolve(f"/{scope}/sessions/{_h()._active_sid_for(scope)}/" + "/".join(segs[3:]))
    if sub != "sessions":
        return f"unknown category {sub!r} (worlds|sessions|assets)"
    if len(segs) == 4:
        return Loc("sessions", user, agent)

    # By id OR by display title, like every other addressable thing in a path (a world resolves either
    # way below, and so does a space). `dir` prints the title right there in the row, so refusing it here
    # made the listing a liar: `rename "Session 1" Home` came back "no session 'Session 1'".
    # `_resolve_sid` is the same resolver the /session/* endpoints use, so the shell and the API agree on
    # what a reference means. It stays dir-based — a session can hold worlds before anything writes its
    # `session.json`, and such a session is still a real, listable, deletable place.
    sid = _h()._resolve_sid(scope, segs[4])
    # The Loc carries the resolved ID, not the reference: `loc.sid` is used directly as a directory name
    # downstream. (The world branch stores the name instead and re-resolves per use — worlds are addressed
    # by name throughout, sessions by id.)
    if sid is None:
        return f"no session {segs[4]!r} for {scope}"
    if len(segs) == 5:
        return Loc("session", user, agent, sid)
    if segs[5] != "worlds":
        return f"unknown category {segs[5]!r} (worlds)"
    if len(segs) == 6:
        return Loc("worlds", user, agent, sid)
    # Verify it: without this, any trailing segments resolve to a `world` Loc and `cd`/`show` succeed on
    # a world that doesn't exist (worlds are flat now, so a name never spans segments).
    name = "/".join(segs[6:])
    if _h()._session_worlds(f"{user}/agents/{agent}", sid).resolve(name) is None:
        return f"no world {name!r} in {sid}"
    return Loc("world", user, agent, sid, name)

def loc_path(loc: Loc) -> str:
    """The canonical path for a `Loc` — what a shortcut resolves to, and what `cd` should remember."""
    p = {"root": "/", "user": f"/{loc.user}", "agents": f"/{loc.user}/agents",
         "spaces": f"/{loc.user}/spaces", "space": f"/{loc.user}/spaces/{loc.name}",
         "agent": f"/{loc.user}/agents/{loc.agent}"}.get(loc.kind)
    if p:
        return p
    if loc.kind in ("assets", "asset"):
        base = f"/{loc.user}/agents/{loc.agent}/assets" if loc.agent else f"/{loc.user}/assets"
        return f"{base}/{loc.name}" if loc.kind == "asset" else base
    base = f"/{loc.user}/agents/{loc.agent}/sessions"
    if loc.kind == "sessions":
        return base
    if loc.kind == "session":
        return f"{base}/{loc.sid}"
    return f"{base}/{loc.sid}/worlds" + (f"/{loc.name}" if loc.kind == "world" else "")

def node(label: str, kind: str, detail: str = "", *, active: bool = False) -> dict:
    n: dict = {"label": label, "kind": kind}
    if detail:
        n["detail"] = detail
    if active:
        n["active"] = True
    return n

def world_row(scope: str, sid: str, name: str) -> dict:
    wdir = _h()._session_worlds(scope, sid)
    wid = wdir.resolve(name)
    live = scope == _h().active_scope and sid == _h().active_sid and wid == _h().active_world
    try:
        doc = wdir.load(name).doc
    except (OSError, ValueError):
        doc = {}
    env = doc.get("environment") or {}
    n = len(doc.get("entities") or [])
    return node(name, "world", f"{n} entities · space={env.get('space') or '?'}", active=live)

def session_meta(scope: str, sid: str) -> dict:
    try:
        return _h().sessions.load_meta(scope, sid) or {}
    except (OSError, ValueError):                              # no session.json yet — still a real session
        return {}

def session_row(scope: str, sid: str) -> dict:
    """Led by the TITLE, like a world row is led by its name — that's what you address it as, and titles
    are renameable and unique now. The id stays in the description: unlike a world's `wld_…` it's short
    and meaningful (`session-1`), it's the stable handle when a title is in flux, and it's what a
    just-created session answers to before anyone names it."""
    meta = session_meta(scope, sid)
    live = scope == _h().active_scope and sid == _h().active_sid
    nw = len(_h()._session_worlds(scope, sid).list())
    vis = "public" if meta.get("public", True) else "private"
    title = meta.get("title") or sid
    desc = f"{nw} worlds · {vis}" if title == sid else f"{sid} · {nw} worlds · {vis}"
    return node(title, "session", desc, active=live)

def _last_world_label(sp: dict) -> str:
    """A space's back-reference is a world ID; show the name a person would recognise."""
    ls, lw = sp.get("last_scope"), sp.get("last_world")
    if not ls or not lw:
        return "—"
    try:
        return f"{ls} / {_h().worlds.name_of(ls, lw)}"
    except (OSError, ValueError):
        return f"{ls} / {lw}"

def space_row(user: str, ref: str) -> dict:
    sid = _h().spaces.resolve(user, ref) or ref
    try:
        sp = _h().spaces.load(user, sid)
    except (OSError, ValueError):
        sp = {}
    name = (sp.get("name") or "").strip() or sid          # label by NAME; the id shows in `show`
    live = user == _h().active_space_owner and sid == _h().active_space
    geo = "geo✓" if sp.get("geolocation") else "geo✗"
    vis = "public" if sp.get("public", True) else "private"
    return node(name, "space", f"{len(sp.get('surfaces') or [])} surfaces · {geo} · {vis}", active=live)

def asset_rows(user: str, agent: str, limit: int = 200) -> list[dict]:
    """Assets whose scope is exactly `<user>/agents/<agent>` — the same hard boundary `agent_of()`
    enforces. `agent=""` selects the legacy rows scoped to a bare user."""
    want = f"{user}/agents/{agent}" if agent else user
    out = []
    for r in _h().library.by_user(user, limit=10_000):
        if (r.get("scope") or "") != want:
            continue
        vis = "public" if r.get("public", 1) else "private"
        label = f" · {r['label']}" if r.get("label") else ""
        out.append(node(r["id"], "asset", f"{r.get('kind', '?')} · {vis}{label}"))
        if len(out) >= limit:
            out.append(node(f"… (more than {limit})", "note"))
            break
    return out

def children(loc: Loc) -> list[dict]:
    """One level below `loc` — never recursive."""
    if loc.kind == "root":
        return [node(u, "user", active=(u == active_user())) for u in all_users()]
    if loc.kind == "user":
        return [node("agents", "category"), node("spaces", "category")] + \
               ([node("assets", "category", "legacy (no agent)")] if asset_rows(loc.user, "") else [])
    if loc.kind == "agents":
        return [node(a, "agent", active=(f"{loc.user}/agents/{a}" == _h().active_scope))
                for a in agents(loc.user)]
    if loc.kind == "agent":
        sid = _h()._active_sid_for(loc.scope)
        return [node("sessions", "category"), node("assets", "category"),
                node("worlds", "shortcut", f"→ sessions/{sid}/worlds" if sid else "→ (no active session)")]
    if loc.kind == "sessions":
        return [session_row(loc.scope, s) for s in _h().sessions.list(loc.scope)]
    if loc.kind == "session":
        return [node("worlds", "category"), node("state", "category")]
    if loc.kind == "worlds":
        return [world_row(loc.scope, loc.sid, n) for n in _h()._session_worlds(loc.scope, loc.sid).list()]
    if loc.kind == "spaces":
        return [space_row(loc.user, n) for n in _h().spaces.list(loc.user)]
    if loc.kind == "assets":
        return asset_rows(loc.user, loc.agent)
    return []                                                  # a leaf: world/space/asset/user item

def leaf_row(loc: Loc) -> Optional[dict]:
    """The one-line row for a leaf, so `dir <leaf>` shows the item rather than nothing."""
    if loc.kind == "world":
        return world_row(loc.scope, loc.sid, loc.name)
    if loc.kind == "session":
        return session_row(loc.scope, loc.sid)
    if loc.kind == "space":
        return space_row(loc.user, loc.name)
    if loc.kind == "asset":
        return next((r for r in asset_rows(loc.user, loc.agent) if r["label"] == loc.name), None)
    return None

def fields(loc: Loc) -> list[list]:
    """Ordered `[key, value]` pairs describing one entry (shell `show`)."""
    if loc.kind == "world":
        try:
            doc = _h()._session_worlds(loc.scope, loc.sid).load(loc.name).doc
        except (OSError, ValueError) as exc:
            return [["error", str(exc)]]
        env = doc.get("environment") or {}
        ents = doc.get("entities") or []
        kinds: dict = {}
        for e in ents:
            c = e.get("components") or {}
            k = ("model" if "gltf-model" in c else "image" if (c.get("material") or {}).get("src")
                 else "grid" if "grid" in c else (c.get("geometry") or {}).get("primitive") or "other")
            kinds[k] = kinds.get(k, 0) + 1
        meta = session_meta(loc.scope, loc.sid)
        wid = _h()._session_worlds(loc.scope, loc.sid).resolve(loc.name)
        return [["world", loc.name], ["id", wid or "?"], ["session", loc.sid], ["scope", loc.scope],
                ["entities", str(len(ents))],
                ["by kind", ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "—"],
                ["space", env.get("space") or "?"], ["sky", (env.get("sky") or {}).get("color") or "—"],
                ["rev", str(doc.get("rev", "?"))],
                ["visibility", "public" if meta.get("public", True) else "private (session)"],
                ["active", "yes" if (loc.scope == _h().active_scope and loc.sid == _h().active_sid
                                     and wid == _h().active_world) else "no"]]
    if loc.kind == "session":
        meta = session_meta(loc.scope, loc.sid)
        wl = _h()._session_worlds(loc.scope, loc.sid).list()
        return [["session", loc.sid], ["title", meta.get("title") or loc.sid], ["scope", loc.scope],
                ["turns", str(len(_h().sessions.read_transcript(loc.scope, loc.sid)))],
                ["llm", meta.get("llm") or "—"], ["active world", meta.get("active_world") or "—"],
                ["worlds", f"{len(wl)}: " + (", ".join(wl) if wl else "—")],
                ["state docs", ", ".join(_h().sessions.state(loc.scope, loc.sid).list()) or "—"],
                ["visibility", "public" if meta.get("public", True) else "private"],
                ["active", "yes" if (loc.scope == _h().active_scope and loc.sid == _h().active_sid) else "no"]]
    if loc.kind == "space":
        sid = _h().spaces.resolve(loc.user, loc.name) or loc.name
        try:
            sp = _h().spaces.load(loc.user, sid)
        except (OSError, ValueError) as exc:
            return [["error", str(exc)]]
        return [["space", (sp.get("name") or "").strip() or sid], ["id", sid], ["owner", loc.user],
                ["surfaces", str(len(sp.get("surfaces") or []))],
                ["geolocation", "yes" if sp.get("geolocation") else "no"],
                ["boundary", "yes" if sp.get("boundary") else "no"],
                ["visibility", "public" if sp.get("public", True) else "private"],
                ["last world", _last_world_label(sp)],
                ["active", "yes" if (loc.user == _h().active_space_owner and sid == _h().active_space) else "no"]]
    if loc.kind == "asset":
        r = _h().library.get(loc.name)
        if not r:
            return [["error", f"no asset {loc.name!r}"]]
        return [["asset", r["id"]], ["kind", r.get("kind") or "?"], ["label", r.get("label") or "—"],
                ["query", r.get("query") or "—"], ["scope", r.get("scope") or "—"],
                ["visibility", "public" if r.get("public", 1) else "private"],
                ["tags", r.get("tags") or "—"], ["file", r.get("filename") or "—"],
                ["last used", str(r.get("last_used") or "—")]]
    if loc.kind == "user":
        ags = agents(loc.user)
        nsess = sum(len(_h().sessions.list(f"{loc.user}/agents/{a}")) for a in ags)
        nw = sum(len(_h()._session_worlds(f"{loc.user}/agents/{a}", s).list())
                 for a in ags for s in _h().sessions.list(f"{loc.user}/agents/{a}"))
        return [["user", loc.user], ["agents", ", ".join(ags) or "—"], ["sessions", str(nsess)],
                ["worlds", str(nw)], ["spaces", str(len(_h().spaces.list(loc.user)))],
                ["assets", str(_h().library.count_by_user(loc.user))],
                ["active", "yes" if loc.user == active_user() else "no"]]
    if loc.kind == "agent":
        sids = _h().sessions.list(loc.scope)
        return [["agent", loc.agent], ["user", loc.user], ["scope", loc.scope],
                ["sessions", f"{len(sids)}: " + (", ".join(sids) if sids else "—")],
                ["active session", _h()._active_sid_for(loc.scope) or "—"],
                ["assets", str(len(asset_rows(loc.user, loc.agent, limit=10_000)))],
                ["active", "yes" if loc.scope == _h().active_scope else "no"]]
    return [["path", loc_path(loc)], ["kind", loc.kind]]

def delete(loc: Loc) -> dict:
    au = active_user()
    if loc.kind == "user":
        if loc.user == au:
            return {"ok": False, "error": f"{loc.user!r} is the active user — switch away first"}
        nw, ns, na = _h().worlds.delete_user(loc.user), _h().spaces.delete_user(loc.user), \
            _h().library.delete_by_user(loc.user)
        return {"ok": True, "deleted": f"user {loc.user!r}: {nw} worlds, {ns} spaces, {na} assets"}

    if loc.kind == "world":
        if loc.scope == _h().active_scope and loc.sid == _h().active_sid \
                and _h()._session_worlds(loc.scope, loc.sid).resolve(loc.name) == _h().active_world:
            return {"ok": False, "error": "can't delete the active world — switch away first"}
        ok = _h()._session_worlds(loc.scope, loc.sid).delete(loc.name)
        return {"ok": ok, "deleted": f"world {loc.name!r}"} if ok else \
            {"ok": False, "error": f"no world {loc.name!r}"}
    if loc.kind == "worlds":
        names = _h()._session_worlds(loc.scope, loc.sid).list()
        if loc.scope == _h().active_scope and loc.sid == _h().active_sid and _h().active_world in names:
            return {"ok": False, "error": "the active world is here — switch away first"}
        for n in names:
            _h()._session_worlds(loc.scope, loc.sid).delete(n)
        return {"ok": True, "deleted": f"{len(names)} worlds in {loc.sid}"}

    if loc.kind == "session":
        if loc.scope == _h().active_scope and loc.sid == _h().active_sid:
            return {"ok": False, "error": "can't delete the live session — switch away first"}
        ok = _h().sessions.delete(loc.scope, loc.sid)
        return {"ok": ok, "deleted": f"session {loc.sid!r}"} if ok else \
            {"ok": False, "error": f"no session {loc.sid!r}"}
    if loc.kind == "sessions":
        sids = _h().sessions.list(loc.scope)
        if loc.scope == _h().active_scope and _h().active_sid in sids:
            return {"ok": False, "error": "the live session is here — switch away first"}
        for s in sids:
            _h().sessions.delete(loc.scope, s)
        return {"ok": True, "deleted": f"{len(sids)} sessions in {loc.agent}"}

    if loc.kind == "space":
        sid = _h().spaces.resolve(loc.user, loc.name)
        if sid is None:
            return {"ok": False, "error": f"no space {loc.name!r} for {loc.user!r}"}
        if loc.user == _h().active_space_owner and not _h()._no_space() and sid == _h().active_space:
            return {"ok": False, "error": "can't delete the active space — switch away first"}
        _h().spaces.delete(loc.user, sid)
        return {"ok": True, "deleted": f"space {loc.name!r}"}
    if loc.kind == "spaces":
        if loc.user == _h().active_space_owner and not _h()._no_space() \
                and _h().active_space in _h().spaces.list(loc.user):
            return {"ok": False, "error": "the active space is here — switch away first"}
        return {"ok": True, "deleted": f"{_h().spaces.delete_user(loc.user)} spaces for {loc.user!r}"}

    if loc.kind == "asset":
        rec = _h().library.get(loc.name)
        sc = (rec or {}).get("scope") or ""
        if rec is None or not (sc == loc.user or sc.startswith(f"{loc.user}/")):
            return {"ok": False, "error": f"no asset {loc.name!r} for {loc.user!r}"}
        ok, err = _h().library.delete(loc.name)
        return {"ok": ok, "deleted": f"asset {loc.name!r}"} if ok else {"ok": False, "error": err}
    if loc.kind == "assets":
        rows = asset_rows(loc.user, loc.agent, limit=10_000)
        for r in rows:
            _h().library.delete(r["label"])
        where = f"{loc.user}/agents/{loc.agent}" if loc.agent else loc.user
        return {"ok": True, "deleted": f"{len(rows)} assets in {where}"}

    return {"ok": False, "error": f"can't delete a {loc.kind} — name a world, session, space or asset"}
