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
from pathlib import Path
from typing import NamedTuple, Optional

from .config import VOID
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
        ref = "/".join(segs[2:])
        return Loc("space", user, name=_h().spaces.resolve(user, ref) or ref)
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
    # The Loc carries the resolved ID, not the reference — for worlds and spaces as well as sessions, so
    # that one `Loc` means one thing whoever typed what. A rename can then never move a resolved location
    # out from under the command holding it, and `loc_path` is stable where `display_path` is readable.
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
    ref = "/".join(segs[6:])
    wid = _h()._session_worlds(f"{user}/agents/{agent}", sid).resolve(ref)
    if wid is None:
        return f"no world {ref!r} in {sid}"
    return Loc("world", user, agent, sid, wid)

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

def label_of(loc: Loc) -> str:
    """The display name of whatever `loc` points at — its last path segment, in names."""
    if loc.kind == "world":
        try:
            return _h()._session_worlds(loc.scope, loc.sid).name_of(loc.name)
        except (OSError, ValueError, AttributeError):
            return loc.name
    if loc.kind == "space":
        try:
            return _h().spaces.name_of(loc.user, loc.name)
        except (OSError, ValueError, AttributeError):
            return loc.name
    if loc.kind == "session":
        return session_meta(loc.scope, loc.sid).get("title") or loc.sid
    if loc.kind == "asset":
        rec = (_h().library.get(loc.name) or {}) if _h().library else {}
        return (rec.get("label") or "").strip() or loc.name
    return {"root": "/", "user": loc.user, "agent": loc.agent}.get(loc.kind, loc.kind)


def display_path(loc: Loc) -> str:
    """The same location as `loc_path`, written in NAMES — what a person is shown and types back.

    `loc_path` is the canonical form and holds ids, so it survives a rename; this one is readable and
    does not. Both are returned by `/admin/{tree,show}` so a caller can remember the first and print the
    second (docs/backlogs/agents.md, phase 3)."""
    canon = loc_path(loc)
    # Assets keep their id in the path. Everything else has a name that resolves, so showing the name is
    # showing an address; an asset label is neither unique nor guaranteed, so a label-valued path would
    # print something you cannot type back. `disk`/`dir` show the label in their own name column instead.
    if loc.kind in ("world", "space", "session"):
        head, _, _ = canon.rpartition("/")
        canon = f"{head}/{label_of(loc)}"
    if loc.kind == "world":                                    # …/sessions/<id>/worlds/<name>
        parts = canon.split("/")
        parts[-3] = session_meta(loc.scope, loc.sid).get("title") or loc.sid
        canon = "/".join(parts)
    return canon


def node(label: str, kind: str, *cells: str, ref: str = "", active: bool = False) -> dict:
    """One listing row. `label` is what a person READS; `ref` is what they TYPE and what the server
    addresses the row by.

    They were one field until 2026-09-04, which is why the asset listing had to lead with an id: the
    row's display text was also its key, so `leaf_row` matched on it and the bulk delete passed it
    straight to `library.delete`. Anything renameable can keep them identical — a world's name IS how you
    address it — so `ref` is omitted unless it differs, and readers fall back to `label`.

    `cells` are the remaining columns, **already stringified but not yet laid out**: how wide they sit and
    whether they show at all is the renderer's business, and a terminal and a voice client answer that
    differently. They used to arrive as one pre-composed `detail` string, which is why no two listings
    ever lined up. Their meaning per listing is `COLUMNS`."""
    n: dict = {"label": label, "kind": kind}
    if ref and ref != label:
        n["ref"] = ref
    cells = tuple(c for c in cells)
    if any(cells):
        n["cells"] = list(cells)
    if active:
        n["active"] = True
    return n


def ref_of(row: dict) -> str:
    """What to address a row by — its `ref` where display and key differ, else its label."""
    return row.get("ref") or row.get("label", "")

# The header for each listing, `label` first. A kind absent here lists names only (users, categories,
# agents) and gets no header — a single column needs no explaining. `id` is appended by the renderer in
# long mode, and always for assets, where the id is genuinely the address rather than a curiosity.
COLUMNS = {
    "sessions": ["name", "worlds", "vis"],
    "worlds": ["name", "entities", "space"],
    "spaces": ["name", "surfaces", "geo", "vis"],
    "assets": ["name", "type", "vis"],
}


def columns_for(kind: str) -> list[str]:
    return list(COLUMNS.get(kind, []))


def _plural(n: int, noun: str, many: str = "") -> str:
    """`1 world` / `2 worlds`. The listing said "1 entities" for two years because the count and its noun
    were composed by f-string at each site."""
    return f"{n} {noun}" if n == 1 else f"{n} {many or noun + 's'}"


def world_label(scope: str, sid: str, ref: str) -> str:
    """A session's active-world reference → the name a person recognises.

    Resolves an id OR a name, because the field holds both in the wild: `/session/new` used to write the
    world's NAME while `_ensure_session` and the id migration wrote its ID. Newly written values are ids;
    this coerces the rest at read time rather than rewriting anyone's disk."""
    if not ref:
        return "—"
    try:
        wdir = _h()._session_worlds(scope, sid)
        wid = wdir.resolve(ref)
        return wdir.name_of(wid) if wid else ref
    except (OSError, ValueError, AttributeError):
        return ref


def space_label(env_space: str, world_owner: str = "") -> str:
    """`environment.space` → the space's display name. The stored form is `<owner>/<space-id>` (ids, so a
    rename strands nothing — `SpaceStore.rename`), which is unreadable in a listing. VOID reads as what it
    means to a person rather than as its sentinel."""
    ref = (env_space or "").strip()
    if not ref:
        return "?"
    if ref == VOID:
        return "outdoor"
    owner, _, sid = ref.rpartition("/")
    if not owner:
        return ref
    try:
        name = _h().spaces.name_of(owner, sid)
    except (OSError, ValueError, AttributeError):
        name = sid
    return name if owner == world_owner or not world_owner else f"{owner}'s {name}"


def world_row(scope: str, sid: str, ref: str) -> dict:
    """`ref` is an id or a name; the row is always LED by the name."""
    wdir = _h()._session_worlds(scope, sid)
    wid = wdir.resolve(ref)
    name = wdir.name_of(wid) if wid else ref
    live = scope == _h().active_scope and sid == _h().active_sid and wid == _h().active_world
    try:
        doc = wdir.load(wid or ref).doc
    except (OSError, ValueError):
        doc = {}
    env = doc.get("environment") or {}
    n = len(doc.get("entities") or [])
    space = space_label(env.get("space"), scope.split("/", 1)[0])
    return node(name, "world", _plural(n, "entity", "entities"), space, ref=wid or "", active=live)

def session_meta(scope: str, sid: str) -> dict:
    try:
        return _h().sessions.load_meta(scope, sid) or {}
    except (OSError, ValueError):                              # no session.json yet — still a real session
        return {}

def session_row(scope: str, sid: str) -> dict:
    """Led by the TITLE, like a world row is led by its name — that's what you address it as, and titles
    are renameable and unique now. The id rides in `ref`: it is the stable handle when a title is in flux,
    and what a just-created session answers to before anyone names it, but it is not what you read a list
    for. `dir -l` and `disk` show it."""
    meta = session_meta(scope, sid)
    live = scope == _h().active_scope and sid == _h().active_sid
    nw = len(_h()._session_worlds(scope, sid).list())
    vis = "public" if meta.get("public", True) else "private"
    title = meta.get("title") or sid
    return node(title, "session", _plural(nw, "world"), vis, ref=sid, active=live)

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
    return node(name, "space", _plural(len(sp.get("surfaces") or []), "surface"), geo, vis,
                ref=sid, active=live)

def asset_rows(user: str, agent: str, limit: int = 200) -> list[dict]:
    """Assets whose scope is exactly `<user>/agents/<agent>` — the same hard boundary `agent_of()`
    enforces. `agent=""` selects the legacy rows scoped to a bare user."""
    want = f"{user}/agents/{agent}" if agent else user
    out = []
    for r in _h().library.by_user(user, limit=10_000):
        if (r.get("scope") or "") != want:
            continue
        vis = "public" if r.get("public", 1) else "private"
        # Led by the LABEL, like every other row. The id stays as `ref`, because it is genuinely the
        # address here: labels are auto-generated by the caption backfill and are neither unique nor
        # guaranteed present, so an ambiguous one is refused rather than guessed at.
        out.append(node((r.get("label") or "").strip() or "(unlabelled)", "asset",
                        r.get("kind") or "?", vis, ref=r["id"]))
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
        return next((r for r in asset_rows(loc.user, loc.agent) if ref_of(r) == loc.name), None)
    return None

def _stat(role: str, path, note: str = "", *, required: bool = True) -> dict:
    """One on-disk thing. Reports what is ACTUALLY there rather than what the catalog claims: row and
    blob drift apart (the live install carries 436 MB of files no row points at), so a path composed
    from a record and never checked is a path that lies."""
    p = Path(path)
    entry: dict = {"role": role, "path": str(p)}
    if note:
        entry["note"] = note
    try:
        st = p.stat()
        entry["size"] = st.st_size if p.is_file() else None
        entry["mtime"] = st.st_mtime
    except OSError:
        # Absent-and-expected is a fault worth naming; absent-and-not-yet-written is ordinary. A session
        # that nobody has spoken in HAS no transcript, and calling that "missing" sends you looking for a
        # bug. A blob a catalog row points at is the other case.
        entry["missing" if required else "absent"] = True
    return entry


def files(loc: Loc) -> list[dict]:
    """Every real thing `loc` is made of on disk (shell `disk`).

    Deliberately a LIST, because "the file" is a lie for three of the six kinds. A session is a
    directory of four things. An asset is a `library.db` row plus a blob — and, for a model downloaded
    from poly.pizza, plus a `.json` sidecar carrying the licence and attribution, which a file-only
    answer would silently drop. Only a world and a space are single documents."""
    h = _h()
    if loc.kind == "world":
        p = h._session_worlds(loc.scope, loc.sid).path_of(loc.name)
        return [_stat("doc", p)] if p else []
    if loc.kind == "session":
        return [_stat("dir", h.sessions.dir(loc.scope, loc.sid)),
                _stat("meta", h.sessions.meta_path(loc.scope, loc.sid)),
                _stat("transcript", h.sessions.transcript_path(loc.scope, loc.sid), required=False),
                _stat("state", h.sessions.state_dir(loc.scope, loc.sid), required=False)]
    if loc.kind == "space":
        p = h.spaces.path_of(loc.user, loc.name)
        return [_stat("doc", p)] if p else []
    if loc.kind == "asset":
        rec = h.library.get(loc.name) if h.library else None
        out = []
        fn = (rec or {}).get("filename") or loc.name
        if fn:
            out.append(_stat("blob", Path(h.ASSET_CACHE) / fn))
            side = Path(h.ASSET_CACHE) / (Path(fn).stem + ".json")
            if side.exists():                                  # poly.pizza writes licence + attribution here
                out.append(_stat("sidecar", side))
        out.append(_stat("row", h.library.path, f"assets.id = {loc.name!r}") if h.library
                   else {"role": "row", "path": "?", "missing": True})
        src = (rec or {}).get("source")
        if src:
            out.append({"role": "source", "path": src})
        return out
    if loc.kind in ("worlds",):
        return [_stat("dir", h.sessions.worlds_dir(loc.scope, loc.sid))]
    if loc.kind in ("sessions",):
        return [_stat("dir", h.sessions.dir(loc.scope, "").parent)]
    if loc.kind == "spaces":
        return [_stat("dir", h.spaces.dir_of(loc.user))]
    if loc.kind in ("assets",):
        return [_stat("dir", h.ASSET_CACHE, "shared by every user — assets are content-addressed")]
    if loc.kind == "agent":
        return [_stat("dir", Path(h.USERS_DIR) / loc.user / "agents" / loc.agent)]
    if loc.kind in ("user", "agents"):
        base = Path(h.USERS_DIR) / loc.user
        return [_stat("dir", base / "agents" if loc.kind == "agents" else base)]
    return [_stat("dir", h.USERS_DIR)]


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
        return [["world", label_of(loc)], ["id", wid or "?"], ["session", loc.sid], ["scope", loc.scope],
                ["entities", str(len(ents))],
                ["by kind", ", ".join(f"{k}×{v}" for k, v in sorted(kinds.items())) or "—"],
                ["space", space_label(env.get("space"), loc.user)
                    + (f"  ({env['space']})" if env.get("space") and env["space"] != VOID else "")],
                ["sky", (env.get("sky") or {}).get("color") or "—"],
                ["rev", str(doc.get("rev", "?"))],
                ["visibility", "public" if meta.get("public", True) else "private (session)"],
                ["active", "yes" if (loc.scope == _h().active_scope and loc.sid == _h().active_sid
                                     and wid == _h().active_world) else "no"]]
    if loc.kind == "session":
        meta = session_meta(loc.scope, loc.sid)
        wl = _h()._session_worlds(loc.scope, loc.sid).list()
        return [["session", loc.sid], ["title", meta.get("title") or loc.sid], ["scope", loc.scope],
                ["turns", str(len(_h().sessions.read_transcript(loc.scope, loc.sid)))],
                ["llm", meta.get("llm") or "—"],
                ["active world", world_label(loc.scope, loc.sid, meta.get("active_world"))],
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
            _h().library.delete(ref_of(r))
        where = f"{loc.user}/agents/{loc.agent}" if loc.agent else loc.user
        return {"ok": True, "deleted": f"{len(rows)} assets in {where}"}

    return {"ok": False, "error": f"can't delete a {loc.kind} — name a world, session, space or asset"}
