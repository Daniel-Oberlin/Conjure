"""WorldStore — the authoritative world document and patch applier (architecture.md §5, §6).

The canonical state is held as a plain dict (`doc`) for easy nested patching and
serialization; entities are validated against the schema on ingress. Every change goes
through `apply_patch`, which computes an inverse (for undo), bumps `rev`, and returns the
full patch to broadcast.
"""

from __future__ import annotations

import json
import re
import shutil
import unicodedata
import uuid
from pathlib import Path
from typing import Any

from .schema import Entity, World

_SCOPE_PART = re.compile(r"[\w.-]+")


def fold_accents(s: str) -> str:
    """`Café` → `Cafe`. Decompose, then drop the combining marks.

    Lookup keys strip anything that isn't a-z0-9, so without this an accented letter would VANISH rather
    than fold — 'Café Noir' would key as 'caf noir' and typing 'Cafe Noir' would miss it. Since names may
    carry accents (see `clean_name`) and arrive by dictation, which is inconsistent about them, the two
    spellings have to converge.

    Latin-ish only: a script with no ASCII decomposition (CJK) folds to nothing, so such a name is exact-
    match only, and `slug` refuses it outright. That's a pre-existing limit of these keys, not a new one."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))


def slug(name: str) -> str:
    """Canonical key for a single world-name segment. Case-insensitive; spaces, underscores and hyphens
    are interchangeable; accents fold to their base letter; other punctuation is dropped — so 'Blade
    Runner', 'blade_runner' and 'BLADE-RUNNER' all resolve to the same segment. Raises if nothing usable
    remains."""
    s = re.sub(r"[\s_-]+", "-", fold_accents(name).strip().lower())
    s = re.sub(r"[^a-z0-9-]", "", s).strip("-")
    if not s:
        raise ValueError(f"bad world name segment {name!r}")
    return s


# Double quotes only. A name containing one can't be wrapped in the quotes a shell path needs, so it
# can't be typed back. The APOSTROPHE is deliberately not here: `shlex` handles "Bob's room" perfectly
# once the name is double-quoted, which it must be anyway the moment it has a space.
_QUOTES = "\"“”"

# What a display name may NOT contain — and therefore what a PATH SEGMENT may not, because a name is how
# you address the thing in the shell (`cd meadow`, `rename "my world" x`). A denylist, not an allowlist:
# the only characters that genuinely can't survive the round trip are the path separators and control
# characters. Everything else — accents, apostrophes, commas, parentheses — tokenises fine and is nobody's
# business to refuse, especially in a product where the names arrive by voice.
#
# This is safe because it is NOT the traversal gate: `_admin_resolve` rejects "."/".." outright and then
# checks every segment against an enumerated real set (users, agents, sessions, worlds), so a segment can
# never name something that doesn't exist. And since identity became an id, a name never reaches the
# filesystem at all — worlds are `wld_*.json`, sessions `session-N`, spaces `space-N` — so there's no
# encoding argument for ASCII either.
_NAME_BAD = re.compile(r"[/\\\x00-\x1f\x7f]")
# The path-segment charset server.py's `_ADMIN_PART` is built from — the same rule stated as a match, so
# the two can't drift into a name you can store but never type.
NAME_SEGMENT = r"[^/\\\x00-\x1f\x7f]+"


def clean_name(name: str, *, what: str = "name") -> str:
    """Normalise a user-supplied DISPLAY name before storing it: drop double quotes, collapse whitespace,
    trim, and refuse a path separator. Raises if nothing usable is left.

    The double quotes are the load-bearing part. A shell path is tokenised with `shlex`, which CONSUMES
    them, so a name carrying its own can never be typed back — `session rename "Session 1" "alien"` (under
    a parser that took the raw remainder) stored the title `"Session 1" "alien"`, and no natural form of
    itself matched it again. Cleaning on write kills the class at the source instead of teaching every
    lookup to forgive it.

    Dropping every quote beats stripping a surrounding pair: `"a" "b"` opens and closes with a quote
    without being quoted, so a strip-the-ends rule mangles it to `a" "b`. Removing them yields `a b` —
    exactly what `shlex` produces for the same input, so the stored name matches what a person typing it
    would get.

    An apostrophe is NOT a quote for this purpose, and neither is an accent. `shlex` parses
    `rename "Bob's room" x` and `rename "Café Noir" x` correctly, because a name with a space has to be
    double-quoted anyway. Refusing them bought nothing and cost a lot: the names here arrive by voice and
    from an LLM, neither of which will ration its punctuation to suit us."""
    s = " ".join("".join(c for c in (name or "") if c not in _QUOTES).split())
    if not s:
        raise ValueError(f"bad {what} {name!r}")
    if _NAME_BAD.search(s):
        bad = "".join(sorted({c for c in s if _NAME_BAD.match(c)}))
        raise ValueError(f"bad {what} {name!r}: {bad!r} can't be used in a name — it would break the path "
                         f"that addresses it")
    return s


WORLD_ID = re.compile(r"^wld_[0-9a-f]{10}$")


def new_world_id() -> str:
    """A world's permanent identity. Minted once and never changed, so the human name above it is free to
    change without stranding anything that stored a reference — the same split `SessionRepository` already
    uses (stable id + mutable title)."""
    return "wld_" + uuid.uuid4().hex[:10]


def is_world_id(ref: str) -> bool:
    return bool(WORLD_ID.match(ref or ""))

_MISSING = object()


def _find_entity(doc: dict, eid: str) -> dict | None:
    for e in doc["entities"]:
        if e["id"] == eid:
            return e
    return None


def _set_path(obj: dict, dotted: str, value: Any) -> Any:
    """Set a dotted path (creating intermediate dicts). Returns the prior value or _MISSING."""
    keys = dotted.split(".")
    cur = obj
    for k in keys[:-1]:
        nxt = cur.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[k] = nxt
        cur = nxt
    last = keys[-1]
    old = cur.get(last, _MISSING)
    cur[last] = value
    return old


def _get_path(obj: Any, dotted: str) -> Any:
    """Read a dotted path; None if any segment is missing."""
    cur = obj
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _del_path(obj: dict, dotted: str) -> bool:
    """Delete the key at a dotted path. Returns True if it existed."""
    keys = dotted.split(".")
    cur = obj
    for k in keys[:-1]:
        if not isinstance(cur, dict) or k not in cur:
            return False
        cur = cur[k]
    if isinstance(cur, dict) and keys[-1] in cur:
        del cur[keys[-1]]
        return True
    return False


class WorldStore:
    def __init__(self, doc: dict):
        self.doc = doc
        self.history: list[dict] = []  # applied patches (for undo/redo + observability)

    @classmethod
    def load(cls, path: str | Path) -> "WorldStore":
        raw = json.loads(Path(path).read_text())
        doc = World.model_validate(raw).model_dump()
        return cls(doc)

    def save(self, path: str | Path) -> None:
        """Atomically persist the current doc as JSON (durability for the active world). Written via a
        temp file + rename so a crash mid-write can't corrupt the saved world."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.doc))
        tmp.replace(path)

    def apply_patch(self, ops: list[dict], origin: str = "user") -> dict:
        """Validate + apply ops, compute inverse, bump rev. Returns the broadcastable patch.

        NOTE (Phase 0): budget/permission validation (architecture.md §5) is stubbed — see
        `_validate`. Inverse for `update`/`env` records prior values but does not yet model
        true key-deletion on undo for keys that were absent.
        """
        self._validate(ops, origin)
        inverse: list[dict] = []

        for op in ops:
            kind = op["op"]

            if kind == "add":
                ent = Entity.model_validate(op["entity"]).model_dump()
                existing = _find_entity(self.doc, ent["id"])
                if existing is not None:
                    self.doc["entities"].remove(existing)
                    inverse.insert(0, {"op": "add", "entity": existing})
                else:
                    inverse.insert(0, {"op": "remove", "id": ent["id"]})
                self.doc["entities"].append(ent)

            elif kind == "remove":
                ent = _find_entity(self.doc, op["id"])
                if ent is not None:
                    self.doc["entities"].remove(ent)
                    inverse.insert(0, {"op": "add", "entity": ent})

            elif kind == "update":
                ent = _find_entity(self.doc, op["id"])
                if ent is None:
                    raise ValueError(f"update: unknown entity {op['id']!r}")
                old: dict[str, Any] = {}
                for path, val in op["set"].items():
                    prior = _set_path(ent, path, val)
                    if prior is not _MISSING:
                        old[path] = prior
                inverse.insert(0, {"op": "update", "id": op["id"], "set": old})

            elif kind == "env":
                old = {}
                for path, val in op["set"].items():
                    prior = _set_path(self.doc["environment"], path, val)
                    if prior is not _MISSING:
                        old[path] = prior
                inverse.insert(0, {"op": "env", "set": old})

            else:
                raise ValueError(f"unknown op {kind!r}")

        self.doc["rev"] += 1
        patch = {"rev": self.doc["rev"], "origin": origin, "ops": ops, "inverse": inverse}
        self.history.append(patch)
        return patch

    def _validate(self, ops: list[dict], origin: str) -> None:
        """Placeholder for the §5 validation gate: schema, performance budget, permissions.

        Phase 0 only checks structural sanity; budget + capability checks land with the
        director and behavior runtime.
        """
        for op in ops:
            if "op" not in op:
                raise ValueError("patch op missing 'op'")


class WorldDir:
    """**Id-addressed** world documents in ONE flat directory — ``<dir>/<id>.json`` — plus a per-dir
    ``_active.txt`` holding the live world's id.

    Identity is a minted ``wld_…`` id that never changes; the human ``name`` lives in the doc and is free
    to change. A rename therefore moves no files and strands no references: session records, active
    pointers, a space's `last_world`, another user's `environment.space` and schema-free agent state all
    hold the id. This is the split `SessionRepository` already uses ("the session id is a stable, safe
    segment; the mutable human title lives in the meta doc, so a rename is a metadata edit that moves
    nothing") — worlds and spaces just weren't consistent with it.

    Worlds are **flat**. Hierarchical names (``castle/dining-hall``) are retired: sessions are the
    grouping now, and a subdirectory that might-or-might-not itself be a world had no good answer.

    Names are unique within a directory (compared slug-wise, so 'Blade Runner' and 'blade-runner' clash).
    That keeps name→id resolution total, which is what lets a person or an agent keep saying "the meadow".

    This is the layer both `WorldRepository` (rooted per capability scope) and `SessionRepository` (rooted
    per session's ``worlds/``) reuse — the only difference is which directory (docs/sessions-plan.md §3).
    """

    def __init__(self, dir: str | Path):
        self.dir = Path(dir)

    def _path(self, wid: str) -> Path:
        if not is_world_id(wid):
            raise ValueError(f"not a world id: {wid!r}")
        return self.dir / f"{wid}.json"

    # -- identity ------------------------------------------------------------------------------
    def ids(self) -> list[str]:
        if not self.dir.is_dir():
            return []
        return sorted(p.stem for p in self.dir.glob("wld_*.json"))

    def name_of(self, wid: str) -> str:
        """The display name, falling back to the id for a doc that somehow has none."""
        try:
            return (json.loads(self._path(wid).read_text()).get("name") or "").strip() or wid
        except (OSError, ValueError):
            return wid

    def entries(self) -> list[dict]:
        """`[{id, name}]`, name-sorted — what a person or an agent should be shown. Reads each doc; fine
        at this scale, and the one place to add an index if a session ever holds hundreds of worlds."""
        return sorted(({"id": i, "name": self.name_of(i)} for i in self.ids()),
                      key=lambda e: e["name"].lower())

    def list(self) -> list[str]:
        """Display names (the human-facing listing). `ids()` is the addressing one."""
        return [e["name"] for e in self.entries()]

    def resolve(self, ref: str) -> str | None:
        """An id or a name → the id, or None. Ids win over names, so an id is never shadowed."""
        ref = (ref or "").strip()
        if not ref:
            return None
        if is_world_id(ref) and self._path(ref).exists():
            return ref
        try:
            want = slug(ref)
        except ValueError:
            return None
        for e in self.entries():
            try:
                if slug(e["name"]) == want:
                    return e["id"]
            except ValueError:
                continue
        return None

    def name_taken(self, name: str, *, other_than: str = "") -> bool:
        try:
            want = slug(name)
        except ValueError:
            return False
        return any(e["id"] != other_than and _slug_or_none(e["name"]) == want for e in self.entries())

    # -- CRUD ----------------------------------------------------------------------------------
    def create(self, name: str, store: "WorldStore") -> str:
        """Mint an id for a NEW world. `save` upserts; this refuses to touch an existing one."""
        name = clean_name(name, what="world name")
        if self.name_taken(name):
            raise ValueError(f"a world called {name!r} already exists here")
        return self.save(name, store)

    def rename(self, ref: str, new_name: str) -> str:
        """Retitle in place. No file moves and no references break — that's the whole point of the id."""
        wid = self.resolve(ref)
        if wid is None:
            raise ValueError(f"no world {ref!r}")
        new_name = clean_name(new_name, what="world name")
        slug(new_name)                                     # …and reject one with nothing sluggable in it
        if self.name_taken(new_name, other_than=wid):
            raise ValueError(f"a world called {new_name!r} already exists here")
        store = self.load(wid)
        store.doc["name"] = new_name
        self.save(wid, store)
        return wid

    def exists(self, ref: str) -> bool:
        return self.resolve(ref) is not None

    def load(self, ref: str) -> "WorldStore":
        wid = self.resolve(ref)
        if wid is None:
            raise ValueError(f"no world {ref!r}")
        return WorldStore.load(self._path(wid))

    def save(self, ref: str, store: "WorldStore") -> str:
        """Upsert by REFERENCE — an id, or a name. A name that resolves to nothing is a new world, so an
        id is minted and the name recorded; that keeps `save(scope, "home", …)` meaning what it always
        did while making identity explicit underneath. Returns the id."""
        wid = self.resolve(ref)
        by_name = not is_world_id(ref)
        if wid is None:
            if by_name:
                slug(ref)                                      # reject an unusable name
                wid = new_world_id()
            else:
                wid = ref                                      # caller minted it (new_world) — honour it
        if by_name:
            # Saving BY NAME asserts the name: `save(scope, "default", doc)` means "this is the world
            # called default", whatever the incoming doc happens to say. Without this, a doc built from a
            # seed template (name "Holodeck") would silently rename the world it was written into.
            store.doc["name"] = ref
        store.doc["id"] = wid
        if not str(store.doc.get("name") or "").strip():
            store.doc["name"] = wid
        store.save(self._path(wid))
        return wid

    def delete(self, ref: str) -> bool:
        wid = self.resolve(ref)
        if wid is None:
            return False
        self._path(wid).unlink(missing_ok=True)
        if self.get_active() == wid:
            (self.dir / "_active.txt").unlink(missing_ok=True)
        return True

    # -- the live pointer ----------------------------------------------------------------------
    def get_active(self) -> str | None:
        p = self.dir / "_active.txt"
        return (p.read_text().strip() or None) if p.exists() else None

    def set_active(self, ref: str) -> None:
        wid = self.resolve(ref)
        if wid is None:
            raise ValueError(f"no world {ref!r}")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "_active.txt").write_text(wid)


def _slug_or_none(name: str):
    try:
        return slug(name)
    except ValueError:
        return None


class WorldRepository:
    """Named world documents on disk under a scope: ``<root>/<scope>/<name>.json``.

    Scope is a **trusted, runtime-injected namespace** (``private/builder``) — never an LLM argument —
    and is validated component-by-component so it can't escape the root. World ``name`` is user-chosen
    (voice), so it's validated to a safe charset (no path separators / traversal). A tiny per-scope
    ``_active.txt`` pointer records which world is live, so a restart resumes where you were.

    **Session facade (docs/sessions-plan.md §3, Option 1).** When constructed with a `SessionRepository`
    (``sessions=``), every per-name op is transparently routed to the scope's **active session's**
    ``worlds/`` dir instead of the bare scope dir — so worlds are stored per-session while the public API
    and ``scope`` (the capability namespace) stay exactly as before, and no call site changes. Without
    ``sessions`` (the default), it behaves as the flat pre-session store (used by unit tests).
    """

    def __init__(self, root: str | Path, *, sessions: "SessionRepository | None" = None):
        self.root = Path(root)
        self._sessions = sessions
        self._live: tuple[str, str] | None = None   # the server-declared live (scope, sid) — the ONE source
                                                     # for the live scope's world addressing (§3, "5.5")

    def set_live(self, scope: str, sid: str) -> None:
        """Declare the globally-live session (docs/sessions-plan.md §3). Per-name ops on `scope` then
        address `sid` explicitly — set by the server in ONE place (`_switch_to`/boot) — instead of the
        facade independently re-reading the active-session pointer (which could lag the server and, before
        this, leaked the outgoing world into a new session on a switch)."""
        self._live = (scope, sid)

    def _scope_dir(self, scope: str) -> Path:
        parts = (scope or "").split("/")
        if not parts or any(p in ("", ".", "..") or not _SCOPE_PART.fullmatch(p) for p in parts):
            raise ValueError(f"bad scope {scope!r}")
        return self.root.joinpath(*parts)

    def _dir(self, scope: str) -> "WorldDir":
        """The name-addressed `WorldDir` this scope's per-name ops act on. With a `SessionRepository`
        attached: the **live scope** uses the server's declared live session (`set_live`); any **other**
        scope resolves ITS active-session pointer (default ``session-1``). Without a `SessionRepository`,
        the bare scope dir (flat, pre-session)."""
        if self._sessions is not None:
            if self._live is not None and self._live[0] == scope:
                sid = self._live[1]
            else:
                sid = self._sessions.get_active(scope) or MIGRATED_SID
            return self._sessions.worlds(scope, sid)
        return WorldDir(self._scope_dir(scope))

    def list(self, scope: str) -> list[str]:
        """Display names of the scope's worlds. `entries` gives `{id, name}` pairs — prefer it anywhere
        the result is stored or handed to an agent, since only the id survives a rename."""
        return self._dir(scope).list()

    def entries(self, scope: str) -> list[dict]:
        return self._dir(scope).entries()

    def ids(self, scope: str) -> list[str]:
        return self._dir(scope).ids()

    def resolve(self, scope: str, ref: str) -> str | None:
        """An id or a name → the world's id, or None."""
        return self._dir(scope).resolve(ref)

    def name_of(self, scope: str, wid: str) -> str:
        return self._dir(scope).name_of(wid)

    def create(self, scope: str, name: str, store: "WorldStore") -> str:
        return self._dir(scope).create(name, store)

    def id_of(self, scope: str, ref: str) -> str | None:
        return self._dir(scope).resolve(ref)

    def rename(self, scope: str, ref: str, new_name: str) -> str:
        return self._dir(scope).rename(ref, new_name)

    def list_public(self, *, exclude_scope: str | None = None) -> list[dict]:
        """Every PUBLIC world across *all* scopes — the cross-user 'worlds available to me' discovery
        (co-location-plan §3). Returns `{scope, owner, name, public}` per world whose doc is public
        (default true when the flag is absent). A filesystem walk that reads each doc — fine at small
        scale; a derived world-index replaces it when discovery needs to scale (backlog). Visibility is the
        **session's** now (§8.2): we enumerate `<root>/*/agents/*/sessions/<id>` and, for each session
        whose `session.json` is public, list its worlds. Returns `{scope, owner, name, public}` per world
        of a public session (`session` id included so a caller can navigate to it)."""
        out: list[dict] = []
        if not self.root.is_dir():
            return []
        for agent_dir in sorted(self.root.glob("*/agents/*")):
            if not agent_dir.is_dir():
                continue
            scope = agent_dir.relative_to(self.root).as_posix()
            if scope == exclude_scope:
                continue
            owner = scope.split("/", 1)[0]
            for sess_dir in sorted((agent_dir / "sessions").glob("*")):
                if not sess_dir.is_dir():
                    continue
                try:
                    meta = json.loads((sess_dir / "session.json").read_text())
                except (OSError, ValueError):
                    meta = {}                                    # no/unreadable meta → treat as public (default)
                if not meta.get("public", True):                 # explicitly private session → skip its worlds
                    continue
                for e in WorldDir(sess_dir / "worlds").entries():
                    out.append({"scope": scope, "owner": owner, "id": e["id"], "name": e["name"],
                                "session": sess_dir.name, "public": True})
        return out

    def list_public_sessions(self, *, agent: str | None = None,
                             exclude_user: str | None = None) -> list[dict]:
        """Every PUBLIC session belonging to *other* users, in a given AGENT — the discovery a human
        browses to visit someone else's live world (session-scoping-plan §B). One entry per public
        session: `{scope, owner, agent, session, title, active_world}`. Scoped to the caller's active
        `agent` (same lens as your own list — to see another agent's sessions you switch agents) and
        excludes the caller's WHOLE user (`exclude_user`), so your own other agents/sessions never appear
        here as a stranger's. A filesystem walk; a derived index replaces it if discovery needs to scale."""
        out: list[dict] = []
        if not self.root.is_dir():
            return []
        pattern = f"*/agents/{agent}" if agent else "*/agents/*"
        for agent_dir in sorted(self.root.glob(pattern)):
            if not agent_dir.is_dir():
                continue
            scope = agent_dir.relative_to(self.root).as_posix()
            owner = scope.split("/", 1)[0]
            if owner == exclude_user:                            # your own scopes are reached by navigation,
                continue                                         # never surfaced here as "someone else's"
            agent_seg = scope.split("/agents/", 1)[1] if "/agents/" in scope else scope
            for sess_dir in sorted((agent_dir / "sessions").glob("*")):
                if not sess_dir.is_dir():
                    continue
                try:
                    meta = json.loads((sess_dir / "session.json").read_text())
                except (OSError, ValueError):
                    meta = {}                                    # no/unreadable meta → treat as public (default)
                if not meta.get("public", True):
                    continue
                out.append({"scope": scope, "owner": owner, "agent": agent_seg, "session": sess_dir.name,
                            "title": meta.get("title", sess_dir.name), "active_world": meta.get("active_world")})
        return out

    def users_in_agent(self, agent: str) -> list[str]:
        """Every user that has any scope under `agent` (`<user>/agents/<agent>`) — the candidate set for
        resolving a spoken owner when visiting a session in the caller's active agent."""
        if not self.root.is_dir():
            return []
        return sorted({d.relative_to(self.root).parts[0]
                       for d in self.root.glob(f"*/agents/{agent}") if d.is_dir()})

    def exists(self, scope: str, name: str) -> bool:
        return self._dir(scope).exists(name)

    def load(self, scope: str, name: str) -> "WorldStore":
        return self._dir(scope).load(name)

    def save(self, scope: str, ref: str, store: "WorldStore") -> str:
        return self._dir(scope).save(ref, store)

    def delete(self, scope: str, name: str) -> bool:
        return self._dir(scope).delete(name)

    def get_active(self, scope: str) -> str | None:
        return self._dir(scope).get_active()

    def set_active(self, scope: str, name: str) -> None:
        self._dir(scope).set_active(name)

    def get_last_agent(self, user: str) -> str | None:
        """The agent this `user` last used. **Legacy / migration read-through only** — superseded by the
        global session pointer (`get_session`), from which the live agent is now derived
        (shared-session-plan §2). Read on boot solely to reconstruct the pointer from a pre-session cache."""
        p = self._scope_dir(user) / "_last_agent.txt"
        return (p.read_text().strip() or None) if p.exists() else None

    def set_last_agent(self, user: str, agent: str) -> None:
        """Legacy writer, retained only to simulate a pre-session cache (migration tests). The runtime no
        longer writes this — the live agent is derived from `get_session` (shared-session-plan §2)."""
        d = self._scope_dir(user)
        d.mkdir(parents=True, exist_ok=True)
        (d / "_last_agent.txt").write_text(agent)

    def get_session(self) -> tuple[str, str] | None:
        """The single global **session pointer** — the `(scope, world)` that is live across the whole
        server (shared-session-plan §2). This is the one fact boot restores; `agent = agent_of(scope)` is
        derived from it, so `_last_agent.txt` is no longer the source of truth. Distinct from the per-scope
        `get_active` (which world to resume *for a given agent's scope*). The active SPACE isn't stored here
        — it's derived from the live world's `environment.space`. Returns None when unset (fresh cache)."""
        p = self.root / "_session.txt"
        if not p.exists():
            return None
        scope, _, name = p.read_text().strip().partition("\t")
        return (scope, name) if scope and name else None

    def set_session(self, scope: str, wid: str) -> None:
        """The global live pointer stores the world's ID, so a rename never strands the boot path."""
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_session.txt").write_text(f"{scope}\t{wid}")

    # -- admin (shell dir/delete; docs/agents.md §2) ---------------------------------------------
    def list_users(self) -> list[str]:
        """Every user with worlds on disk — the immediate subdirs of the root (`<root>/<user>/…`)."""
        return sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.is_dir() else []

    def user_scopes(self, user: str) -> list[str]:
        """All `<user>/agents/<agent>` world-scopes for a user (worlds are addressed agent-abstracted
        as `/<user>/worlds/<name>`, but live under a per-agent scope)."""
        base = self._scope_dir(user) / "agents"                # validates `user`
        return [f"{user}/agents/{p.name}" for p in sorted(base.iterdir()) if p.is_dir()] if base.is_dir() else []

    def delete_user(self, user: str) -> int:
        """Remove ALL of a user's worlds — the whole ``<root>/<user>/agents`` subtree (NOT the user dir,
        which under the shared ``users/`` root also holds their spaces). Returns the world count (files
        under each session's ``worlds/``, not counting session meta)."""
        d = self._scope_dir(user) / "agents"                   # validates `user` → no traversal
        if not d.is_dir():
            return 0
        n = sum(1 for wdir in d.glob("*/sessions/*/worlds") for _ in wdir.rglob("*.json"))
        shutil.rmtree(d)
        return n


def _session_seg(seg: str) -> str:
    """Validate a session **id** as one safe path segment — kept **verbatim** (not slugified, so a
    stable id never shifts under us) and rejecting traversal/empties. The mutable human *title* lives in
    the meta doc, not the path, so a rename never moves anything on disk (docs/sessions-plan.md §3)."""
    if not seg or seg in (".", "..") or not _SCOPE_PART.fullmatch(seg):
        raise ValueError(f"bad session id {seg!r}")
    return seg


class SessionRepository:
    """Sessions on disk under a scope: ``<root>/<user>/agents/<agent>/sessions/<id>/`` (docs/
    sessions-plan.md §3). A *session* is an instance of an agent — its meta (``session.json``), its
    append-only transcript (``transcript.jsonl``), its agent state (``state/``), and the worlds created
    within it (``worlds/``).

    The **scope** (``<user>/agents/<agent>``) is the same trusted, runtime-injected capability namespace
    as `WorldRepository` — never an LLM argument — validated segment-by-segment so a session can't escape
    the root. The session **id** is a stable, safe segment; the mutable human **title** lives in the meta
    doc, so a rename is a metadata edit that moves nothing. A per-scope ``sessions/_active.txt`` records
    the live session for that scope.

    **Step 1 scope (docs/sessions-plan.md §9):** the container — meta CRUD, the per-scope active pointer,
    and the **path helpers** (`worlds_dir`/`state_dir`/`transcript_path`) that later steps' transcript,
    state, and world sub-stores build on. Those I/O layers wire in on steps 2, 4, 5; nothing imports this
    class yet, so it changes no runtime behavior.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _scope_dir(self, scope: str) -> Path:
        parts = (scope or "").split("/")
        if not parts or any(p in ("", ".", "..") or not _SCOPE_PART.fullmatch(p) for p in parts):
            raise ValueError(f"bad scope {scope!r}")
        return self.root.joinpath(*parts)

    def _sessions_dir(self, scope: str) -> Path:
        return self._scope_dir(scope) / "sessions"

    # -- addressing (the seam later steps build on) ----------------------------------------------
    def dir(self, scope: str, sid: str) -> Path:
        """The session's directory; its ``worlds/``, ``state/`` and transcript live beneath it."""
        return self._sessions_dir(scope) / _session_seg(sid)

    def meta_path(self, scope: str, sid: str) -> Path:
        return self.dir(scope, sid) / "session.json"

    def transcript_path(self, scope: str, sid: str) -> Path:
        return self.dir(scope, sid) / "transcript.jsonl"

    def state_dir(self, scope: str, sid: str) -> Path:
        return self.dir(scope, sid) / "state"

    def worlds_dir(self, scope: str, sid: str) -> Path:
        return self.dir(scope, sid) / "worlds"

    def worlds(self, scope: str, sid: str) -> "WorldDir":
        """The session's worlds as a name-addressed `WorldDir` rooted at ``.../sessions/<id>/worlds/``
        (docs/sessions-plan.md §3, Option 1) — the same per-name API as a scope's worlds, one level
        deeper. This is how the runtime reaches the live session's worlds without threading a scope
        through every call: resolve the active session once, then address worlds by name."""
        return WorldDir(self.worlds_dir(scope, sid))

    def state(self, scope: str, sid: str) -> "StateStore":
        """The session's agent-state as a `StateStore` rooted at ``.../sessions/<id>/state/`` (docs/
        sessions-plan.md §5) — the storage behind the agent's generic ``state_*`` tools."""
        return StateStore(self.state_dir(scope, sid))

    # -- transcript (append-only JSONL; docs/sessions-plan.md §4) --------------------------------
    def append_transcript(self, scope: str, sid: str, entry: dict) -> None:
        """Append one turn as a JSON line — cheap O(1) growth, no whole-file rewrite. Entries are plain
        dicts (``{role, by, text, …}``); the agent server converts to/from its `Turn`, so this layer stays
        free of any conversation type."""
        p = self.transcript_path(scope, sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def clear_transcript(self, scope: str, sid: str) -> None:
        """Wipe a session's saved dialog (chat-history reset). Keeps the world, assets, and state docs —
        only the conversation JSONL is removed, so a fresh `read_transcript` returns []. Used by the shell
        `clear` command to reset a bloated conversation that's degrading the model."""
        self.transcript_path(scope, sid).unlink(missing_ok=True)

    def read_transcript(self, scope: str, sid: str) -> list[dict]:
        """The saved dialog as a list of entry dicts (empty if none). A torn final line (crash mid-append)
        is skipped rather than fatal — the append-only format degrades to "lose the last turn"."""
        p = self.transcript_path(scope, sid)
        if not p.exists():
            return []
        out: list[dict] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue                                   # tolerate a torn final line
        return out

    # -- meta CRUD -------------------------------------------------------------------------------
    def list(self, scope: str) -> list[str]:
        """The session ids under a scope (immediate subdirs of ``sessions/``); the ``_active.txt``
        pointer is not a session, so it's never listed."""
        d = self._sessions_dir(scope)
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def exists(self, scope: str, sid: str) -> bool:
        return self.meta_path(scope, sid).exists()

    def load_meta(self, scope: str, sid: str) -> dict:
        return json.loads(self.meta_path(scope, sid).read_text())

    def save_meta(self, scope: str, sid: str, meta: dict) -> None:
        """Create-or-update the session's ``session.json`` (atomic temp+rename). Creating the file also
        materializes the session directory."""
        p = self.meta_path(scope, sid)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(meta))
        tmp.replace(p)

    def delete(self, scope: str, sid: str) -> bool:
        """Remove the whole session directory — its transcript, state, and worlds included (worlds belong
        to the session; docs/sessions-plan.md §8.10). Clears the active pointer if it named this one."""
        d = self.dir(scope, sid)
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        if self.get_active(scope) == sid:
            (self._sessions_dir(scope) / "_active.txt").unlink(missing_ok=True)
        return True

    # -- per-scope active pointer ----------------------------------------------------------------
    def get_active(self, scope: str) -> str | None:
        p = self._sessions_dir(scope) / "_active.txt"
        return (p.read_text().strip() or None) if p.exists() else None

    def set_active(self, scope: str, sid: str) -> None:
        d = self._sessions_dir(scope)
        d.mkdir(parents=True, exist_ok=True)
        (d / "_active.txt").write_text(_session_seg(sid))


class StateStore:
    """A per-session bag of named JSON documents in one directory (docs/sessions-plan.md §5) — the storage
    behind the agent's generic ``state_*`` tools. Plain JSON docs, dotted-path CRUD, atomic per-doc writes.
    The agent's data **schema** is declared/owned elsewhere (`agent.json`); this layer is schema-free — it
    doesn't know a `map` from an `inventory`, which is the whole point (no domain baked into storage)."""

    def __init__(self, dir: str | Path):
        self.dir = Path(dir)

    def _path(self, doc: str) -> Path:
        return self.dir / f"{slug(doc)}.json"                # doc name → safe filename (no traversal)

    def list(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.json")) if self.dir.is_dir() else []

    def read(self, doc: str) -> Any:
        p = self._path(doc)
        return json.loads(p.read_text()) if p.exists() else {}

    def write(self, doc: str, value: Any) -> None:
        p = self._path(doc)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")               # atomic (temp + rename)
        tmp.write_text(json.dumps(value))
        tmp.replace(p)

    def get(self, doc: str, path: str | None = None) -> Any:
        d = self.read(doc)
        return d if not path else _get_path(d, path)

    def set(self, doc: str, path: str, value: Any, *, validate=None) -> Any:
        d = self.read(doc)
        if not isinstance(d, dict):
            d = {}
        _set_path(d, path, value)
        if validate:                                         # agent-owned check (schema-free store): the
            validate(d)                                      # caller passes it; raising → no write
        self.write(doc, d)
        return d

    def merge(self, doc: str, value: dict, *, validate=None) -> Any:
        d = self.read(doc)
        if isinstance(d, dict) and isinstance(value, dict):
            d.update(value)
        else:
            d = value
        if validate:
            validate(d)
        self.write(doc, d)
        return d

    def delete(self, doc: str, path: str | None = None) -> bool:
        if not path:                                         # drop the whole doc
            p = self._path(doc)
            existed = p.exists()
            p.unlink(missing_ok=True)
            return existed
        d = self.read(doc)
        ok = _del_path(d, path) if isinstance(d, dict) else False
        if ok:
            self.write(doc, d)
        return ok


class SpaceStore:
    """Named, **USER-owned** physical spaces on disk: ``<root>/<user>/spaces/<name>.json`` (docs/
    spaces-and-users-plan.md §5; sessions-plan.md §3). A *space* is the captured real geometry —
    `surfaces` (geometry + default materials) + `boundary` + meta (`owner`, `public`, `geolocation`) —
    shared across all of a user's worlds, *not* a full WorldStore doc and *not* per-agent. The owner's
    headset is its capture authority. Stored as a plain JSON dict; a per-user ``_active.txt`` records the
    live space. Under the user-first tree the root is ``.cache/users`` (shared with agents), so spaces
    live in a ``spaces/`` subdir beside ``agents/`` rather than at the user root."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _user_dir(self, user: str) -> Path:
        if not user or user in (".", "..") or not _SCOPE_PART.fullmatch(user):
            raise ValueError(f"bad user {user!r}")
        return self.root / user / "spaces"

    def _path(self, user: str, name: str) -> Path:
        return self._user_dir(user) / f"{slug(name)}.json"

    def list(self, user: str) -> list[str]:
        d = self._user_dir(user)
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def exists(self, user: str, name: str) -> bool:
        return self._path(user, name).exists()

    def load(self, user: str, name: str) -> dict:
        return json.loads(self._path(user, name).read_text())

    def save(self, user: str, name: str, space: dict) -> None:
        p = self._path(user, name)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")           # atomic write (temp + rename)
        tmp.write_text(json.dumps(space))
        tmp.replace(p)

    def delete(self, user: str, name: str) -> bool:
        p = self._path(user, name)
        if not p.exists():
            return False
        p.unlink()
        if self.get_active(user) == slug(name):
            (self._user_dir(user) / "_active.txt").unlink(missing_ok=True)
        return True

    def get_active(self, user: str) -> str | None:
        p = self._user_dir(user) / "_active.txt"
        return (p.read_text().strip() or None) if p.exists() else None

    def set_active(self, user: str, name: str) -> None:
        d = self._user_dir(user)
        d.mkdir(parents=True, exist_ok=True)
        (d / "_active.txt").write_text(slug(name))

    # -- admin (shell dir/delete) ----------------------------------------------------------------
    def name_of(self, user: str, sid: str) -> str:
        """A space's display name. The FILE key (`space-1`) is its permanent id — auto-minted, never a
        user's choice — so nothing is re-keyed here; a space simply gains a name it never had. Falls back
        to the id, which is what every space shows until somebody names it."""
        try:
            return (self.load(user, sid).get("name") or "").strip() or sid
        except (OSError, ValueError):
            return sid

    def entries(self, user: str) -> list[dict]:
        return [{"id": i, "name": self.name_of(user, i)} for i in self.list(user)]

    def resolve(self, user: str, ref: str) -> str | None:
        """An id (`space-1`) or a display name → the id. Ids win, so one can't be shadowed by a name."""
        ref = (ref or "").strip()
        if not ref:
            return None
        if self.exists(user, ref):
            return slug(ref)
        want = _slug_or_none(ref)
        return next((e["id"] for e in self.entries(user) if _slug_or_none(e["name"]) == want), None) \
            if want else None

    def rename(self, user: str, ref: str, new_name: str) -> str:
        """Retitle in place. `environment.space` in every world — including OTHER users' worlds, which we
        may not rewrite — points at the id, so this strands nothing."""
        sid = self.resolve(user, ref)
        if sid is None:
            raise ValueError(f"no space {ref!r}")
        new_name = clean_name(new_name, what="space name")
        want = slug(new_name)
        if any(e["id"] != sid and _slug_or_none(e["name"]) == want for e in self.entries(user)):
            raise ValueError(f"a space called {new_name!r} already exists")
        sp = self.load(user, sid)
        sp["name"] = new_name
        self.save(user, sid, sp)
        return sid

    def list_users(self) -> list[str]:
        """Users with a presence under the root (its immediate subdirs). Under the shared ``users/`` root
        this includes users who have agents but no spaces yet — harmless for the callers, which union it
        and then `list` each user's (possibly empty) spaces."""
        return sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.is_dir() else []

    def delete_user(self, user: str) -> int:
        """Remove ALL of a user's spaces — only their ``<root>/<user>/spaces`` subtree (NOT the user dir,
        which under the shared ``users/`` root also holds their agents). Returns the space count."""
        d = self._user_dir(user)                               # validates → no traversal; = <user>/spaces
        if not d.is_dir():
            return 0
        n = len(list(d.glob("*.json")))
        shutil.rmtree(d)
        return n


MIGRATED_SID = "session-1"


def migrate_worlds_to_ids(users_root: str | Path) -> int:
    """One-time: re-key every world from its NAME to a minted `wld_…` id (2026-08-25).

    Worlds used to be addressed by a slugged filename, which made the name their identity — so renaming
    one stranded every reference to it (session records, active pointers, a space's `last_world`, another
    user's `environment.space`, and schema-free agent state we can't even inspect). After this the file is
    `<id>.json`, the name lives in the doc, and a rename touches nothing else.

    Rewrites, in each session: the world files, the `worlds/_active.txt` pointer, and `session.json`'s
    `active_world`. Also rewrites each space's `last_world` back-reference. Idempotent — a directory whose
    worlds are already id-keyed is skipped, so it's safe to run on every boot. Returns how many worlds
    moved."""
    root = Path(users_root)
    if not root.is_dir():
        return 0
    moved, renamed = 0, {}                       # renamed: (worlds_dir, old_key) → new id
    for wdir in sorted(root.glob("*/agents/*/sessions/*/worlds")):
        if not wdir.is_dir():
            continue
        mapping: dict[str, str] = {}
        for old in sorted(wdir.rglob("*.json")):
            key = old.relative_to(wdir).as_posix()[: -len(".json")]
            if is_world_id(key):
                continue                         # already migrated
            try:
                doc = json.loads(old.read_text())
            except (OSError, ValueError):
                continue
            wid = new_world_id()
            # The old KEY is the name people know it by — not the doc's `name`, which every world on disk
            # copied verbatim from the seed template ("Holodeck") and nothing ever updated.
            doc["id"], doc["name"] = wid, key.replace("/", "-")     # hierarchy is retired; flatten the name
            (wdir / f"{wid}.json").write_text(json.dumps(doc))
            old.unlink()
            mapping[key] = wid
            moved += 1
        if not mapping:
            continue
        for d in sorted(wdir.rglob("*"), reverse=True):             # prune the old nested dirs
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        ptr = wdir / "_active.txt"
        if ptr.exists():
            ptr.write_text(mapping.get(ptr.read_text().strip(), "") or "")
        meta_p = wdir.parent / "session.json"
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text())
            except (OSError, ValueError):
                meta = None
            aw = (meta or {}).get("active_world")
            if meta is not None and aw and not is_world_id(aw):
                # A dangling `active_world` predates this migration (it names a world that isn't in the
                # session at all — seen in real data). Re-point it at the WorldDir pointer, which IS
                # right, rather than carrying a broken reference across.
                meta["active_world"] = mapping.get(aw) or (ptr.read_text().strip() if ptr.exists() else "")
                meta_p.write_text(json.dumps(meta))
        renamed.update({(str(wdir), k): v for k, v in mapping.items()})
    # A space points back at the world last live in it; that reference is an id now too.
    for sp_p in sorted(root.glob("*/spaces/*.json")):
        try:
            sp = json.loads(sp_p.read_text())
        except (OSError, ValueError):
            continue
        lw, ls = sp.get("last_world"), sp.get("last_scope")
        if not lw or is_world_id(lw) or not ls:
            continue
        hit = next((v for (d, k), v in renamed.items() if k == lw and f"/{ls}/" in d.replace("\\", "/")), None)
        if hit:
            sp["last_world"] = hit
            sp_p.write_text(json.dumps(sp))
    return moved


def migrate_cache_to_users(cache: str | Path) -> bool:
    """One-time, idempotent relocation to the user-first, session tree (docs/sessions-plan.md §3, §7).

    Moves the pre-session layout::

        <cache>/worlds/<user>/agents/<agent>/<world>.json   (+ per-scope _active.txt)
        <cache>/spaces/<user>/<space>.json
        <cache>/worlds/_session.txt   =  <scope>\\t<world>

    to::

        <cache>/users/<user>/agents/<agent>/sessions/session-1/worlds/<world>.json
        <cache>/users/<user>/agents/<agent>/sessions/session-1/session.json
        <cache>/users/<user>/agents/<agent>/sessions/_active.txt   = session-1
        <cache>/users/<user>/spaces/<space>.json
        <cache>/_session.txt          =  <scope>\\t session-1     (the active SESSION)

    Each scope's existing worlds become the ``worlds/`` of a single ``session-1``; that scope's old
    ``_active.txt`` becomes the session's ``active_world`` in ``session.json``. Acts only when an old
    ``worlds/``/``spaces/`` dir exists and ``users/`` does not, so re-running is a no-op. Returns True iff
    it moved anything. (Legacy ``_last_agent.txt`` is dropped — the live agent now derives from the active
    session.)"""
    cache = Path(cache)
    users = cache / "users"
    old_worlds = cache / "worlds"
    old_spaces = cache / "spaces"
    if users.exists() or not (old_worlds.is_dir() or old_spaces.is_dir()):
        return False

    # -- worlds → users/<user>/agents/<agent>/sessions/session-1/worlds/ -------------------------
    if old_worlds.is_dir():
        for user_dir in sorted(p for p in old_worlds.iterdir() if p.is_dir()):
            user = user_dir.name
            agents_dir = user_dir / "agents"
            if not agents_dir.is_dir():
                continue
            for agent_dir in sorted(p for p in agents_dir.iterdir() if p.is_dir()):
                agent = agent_dir.name
                active_txt = agent_dir / "_active.txt"          # read the old pointer before moving files
                active_world = (active_txt.read_text().strip() or None) if active_txt.exists() else None
                dest_sess = users / user / "agents" / agent / "sessions" / MIGRATED_SID
                dest_worlds = dest_sess / "worlds"
                dest_worlds.mkdir(parents=True, exist_ok=True)
                names: list[str] = []
                for wp in sorted(agent_dir.rglob("*.json")):    # sorted() materializes → safe to move mid-loop
                    rel = wp.relative_to(agent_dir)
                    (dest_worlds / rel).parent.mkdir(parents=True, exist_ok=True)
                    wp.replace(dest_worlds / rel)
                    names.append(rel.as_posix()[: -len(".json")])
                aw = active_world or (names[0] if names else "default")
                (dest_worlds / "_active.txt").write_text(aw)                   # WorldDir active pointer
                meta = {"id": MIGRATED_SID, "owner": user, "agent": agent, "title": "Session 1",
                        "public": True, "active_world": aw, "llm": ""}
                tmp = dest_sess / "session.json.tmp"
                tmp.write_text(json.dumps(meta))
                tmp.replace(dest_sess / "session.json")
                (dest_sess.parent / "_active.txt").write_text(MIGRATED_SID)   # sessions/_active.txt (which session)

    # -- global pointer: worlds/_session.txt (scope\tworld) → cache/_session.txt (scope\tsid) ----
    old_ptr = old_worlds / "_session.txt"
    if old_ptr.exists():
        scope, _, _world = old_ptr.read_text().strip().partition("\t")
        if scope:
            (cache / "_session.txt").write_text(f"{scope}\t{MIGRATED_SID}")

    # -- spaces → users/<user>/spaces/ ----------------------------------------------------------
    if old_spaces.is_dir():
        for user_dir in sorted(p for p in old_spaces.iterdir() if p.is_dir()):
            dest = users / user_dir.name / "spaces"
            dest.mkdir(parents=True, exist_ok=True)
            for f in sorted(user_dir.iterdir()):
                if f.is_file():
                    f.replace(dest / f.name)

    shutil.rmtree(old_worlds, ignore_errors=True)               # remove the emptied old trees
    shutil.rmtree(old_spaces, ignore_errors=True)
    return True


def _relocate(src: Path, dst: Path) -> bool:
    """Move `src`→`dst`, merging into an existing destination directory (so a pre-created empty `dst`,
    e.g. from an import-time mkdir, doesn't block the move). Atomic rename where possible, falling back
    to a cross-device copy. Returns True iff `src` existed. Idempotent: a missing `src` is a no-op, so a
    re-run after an interrupted migration simply resumes."""
    if not src.exists():
        return False
    if src.is_dir() and dst.exists():
        for child in list(src.iterdir()):
            _relocate(child, dst / child.name)
        try:
            src.rmdir()                                          # drop the now-empty source dir
        except OSError:
            pass
        return True
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.replace(dst)                                         # atomic within one filesystem
    except OSError:
        shutil.move(str(src), str(dst))                          # cross-device fallback
    return True


# The precious items that move into the DATA root (docs/user-home-plan.md §3.1). `backups/` is
# intentionally absent — user-owned, never touched. `worlds/`/`spaces/` are the pre-user legacy trees,
# carried along if still present. The disposable `tunnel_url` (ephemeral dev-tooling scratch written by
# scripts/tunnel.sh) moves into the CACHE root, not the DATA tree — so nothing lands in the in-project
# .cache anymore.
_HOME_DATA_ITEMS = ("users", "_session.txt", "assets", "library.db", "library.db-shm",
                    "library.db-wal", "worlds", "spaces")
_HOME_CACHE_ITEMS: tuple[str, ...] = ("tunnel_url",)


def migrate_project_cache_to_home(project_cache: str | Path, data_dir: str | Path,
                                  cache_dir: str | Path) -> bool:
    """One-time relocation of the in-project ``.cache`` into the user home (docs/user-home-plan.md §6).

    Moves the precious tree (``users/``, ``_session.txt``, ``assets/``, ``library.db`` + WAL sidecars,
    and any legacy ``worlds/``/``spaces/``) into ``data_dir``, and the disposable ``tunnel_url`` into
    ``cache_dir``. **Skips ``backups/``** — user-owned, never touched. A move (never a copy or delete),
    so content is preserved and content-addressed assets stay valid.

    Idempotent via a ``<project_cache>/MOVED.txt`` breadcrumb: once written, re-runs are a no-op; an
    interrupted run (breadcrumb not yet written) resumes, since each item-move skips a missing source.
    Returns True iff it moved anything."""
    project_cache, data_dir, cache_dir = Path(project_cache), Path(data_dir), Path(cache_dir)
    breadcrumb = project_cache / "MOVED.txt"
    if not project_cache.is_dir() or breadcrumb.exists():
        return False
    # Guard against migrating the home onto itself (e.g. a test that points .cache AT the data root).
    if project_cache.resolve() in (data_dir.resolve(), cache_dir.resolve()):
        return False
    moved = False
    for name in _HOME_DATA_ITEMS:
        moved |= _relocate(project_cache / name, data_dir / name)
    for name in _HOME_CACHE_ITEMS:
        moved |= _relocate(project_cache / name, cache_dir / name)
    if moved:
        breadcrumb.write_text(f"Contents moved to the user home (docs/user-home-plan.md §6):\n"
                              f"  data  → {data_dir}\n  cache → {cache_dir}\n")
    return moved
