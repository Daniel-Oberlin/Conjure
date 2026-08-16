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
from pathlib import Path
from typing import Any

from .schema import Entity, World

_SCOPE_PART = re.compile(r"[\w.-]+")


def slug(name: str) -> str:
    """Canonical key for a single world-name segment. Case-insensitive; spaces, underscores and hyphens
    are interchangeable; other punctuation is dropped — so 'Blade Runner', 'blade_runner' and
    'BLADE-RUNNER' all resolve to the same segment. Raises if nothing usable remains."""
    s = re.sub(r"[\s_-]+", "-", (name or "").strip().lower())
    s = re.sub(r"[^a-z0-9-]", "", s).strip("-")
    if not s:
        raise ValueError(f"bad world name segment {name!r}")
    return s


def world_path(name: str) -> str:
    """Canonical, **hierarchical** key for a world: '/'-separated segments, each slugified — so an agent
    can organize worlds in a tree ('castle-quest/dining-hall'). Each segment normalizes independently
    (case/spaces/underscores/hyphens), and traversal ('..') or empty segments are rejected, so a world
    can never escape its scope dir. Returns a posix relative path (no leading slash, no extension)."""
    segs = [slug(s) for s in (name or "").split("/") if s.strip()]
    if not segs:
        raise ValueError(f"bad world name {name!r}")
    return "/".join(segs)

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
    """Named, hierarchical world documents inside ONE directory: ``<dir>/<name>.json`` (a name may nest,
    e.g. ``castle-quest/dining-hall``, each segment slug-normalized). A per-dir ``_active.txt`` records
    the live world.

    This is the **name-addressed** layer that both `WorldRepository` (which roots one per capability
    scope, ``<root>/<scope>/``) and `SessionRepository` (which roots one per session's ``worlds/``) reuse
    — the only difference between them is *which directory* the worlds live in (docs/sessions-plan.md §3,
    Option 1). Keeping this separate is what lets `scope` stay the pure capability namespace while worlds
    move under a session.
    """

    def __init__(self, dir: str | Path):
        self.dir = Path(dir)

    def _path(self, name: str) -> Path:
        return self.dir / f"{world_path(name)}.json"

    def list(self) -> list[str]:
        """Every world as a canonical hierarchical path, recursively. ``_active.txt`` is a .txt, so it's
        never matched and never listed."""
        if not self.dir.is_dir():
            return []
        return sorted(p.relative_to(self.dir).as_posix()[: -len(".json")] for p in self.dir.rglob("*.json"))

    def exists(self, name: str) -> bool:
        return self._path(name).exists()

    def load(self, name: str) -> "WorldStore":
        return WorldStore.load(self._path(name))

    def save(self, name: str, store: "WorldStore") -> None:
        store.save(self._path(name))

    def delete(self, name: str) -> bool:
        p = self._path(name)
        if not p.exists():
            return False
        p.unlink()
        if self.get_active() == world_path(name):
            (self.dir / "_active.txt").unlink(missing_ok=True)
        d = p.parent                                       # prune now-empty parent folders in the tree
        while d != self.dir and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
        return True

    def get_active(self) -> str | None:
        p = self.dir / "_active.txt"
        return (p.read_text().strip() or None) if p.exists() else None

    def set_active(self, name: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "_active.txt").write_text(world_path(name))


class WorldRepository:
    """Named world documents on disk under a scope: ``<root>/<scope>/<name>.json``.

    Scope is a **trusted, runtime-injected namespace** (``private/builder``) — never an LLM argument —
    and is validated component-by-component so it can't escape the root. World ``name`` is user-chosen
    (voice), so it's validated to a safe charset (no path separators / traversal). A tiny per-scope
    ``_active.txt`` pointer records which world is live, so a restart resumes where you were.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _scope_dir(self, scope: str) -> Path:
        parts = (scope or "").split("/")
        if not parts or any(p in ("", ".", "..") or not _SCOPE_PART.fullmatch(p) for p in parts):
            raise ValueError(f"bad scope {scope!r}")
        return self.root.joinpath(*parts)

    def _dir(self, scope: str) -> "WorldDir":
        """The scope's world directory as a `WorldDir` — the name-addressed layer this scope wraps."""
        return WorldDir(self._scope_dir(scope))

    def list(self, scope: str) -> list[str]:
        """All worlds in the scope, as canonical hierarchical paths ('castle-quest/dining-hall'),
        recursively. The per-scope '_active.txt' pointer isn't a world, so it's never listed."""
        return self._dir(scope).list()

    def list_public(self, *, exclude_scope: str | None = None) -> list[dict]:
        """Every PUBLIC world across *all* scopes — the cross-user 'worlds available to me' discovery
        (co-location-plan §3). Returns `{scope, owner, name, public}` per world whose doc is public
        (default true when the flag is absent). A filesystem walk that reads each doc — fine at small
        scale; a derived world-index replaces it when discovery needs to scale (backlog). Scopes are
        `<user>/agents/<agent>`, so we enumerate `<root>/*/agents/*` dirs."""
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
            for p in sorted(agent_dir.rglob("*.json")):
                name = p.relative_to(agent_dir).as_posix()[: -len(".json")]
                try:
                    doc = json.loads(p.read_text())
                except (OSError, ValueError):
                    continue
                if (doc.get("environment") or {}).get("public", True):
                    out.append({"scope": scope, "owner": owner, "name": name, "public": True})
        return out

    def exists(self, scope: str, name: str) -> bool:
        return self._dir(scope).exists(name)

    def load(self, scope: str, name: str) -> "WorldStore":
        return self._dir(scope).load(name)

    def save(self, scope: str, name: str, store: "WorldStore") -> None:
        self._dir(scope).save(name, store)

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

    def set_session(self, scope: str, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "_session.txt").write_text(f"{scope}\t{world_path(name)}")

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
        """Remove ALL of a user's worlds (the whole `<root>/<user>` subtree). Returns the world count."""
        d = self._scope_dir(user)                              # validates → no traversal
        if not d.is_dir():
            return 0
        n = sum(1 for _ in d.rglob("*.json"))
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


class SpaceStore:
    """Named, **USER-owned** physical spaces on disk: ``<root>/<user>/<name>.json`` (docs/
    spaces-and-users-plan.md §5). A *space* is the captured real geometry — `surfaces` (geometry +
    default materials) + `boundary` + meta (`owner`, `public`, `geolocation`) — shared across all of a
    user's worlds, *not* a full WorldStore doc and *not* per-agent. The owner's headset is its capture
    authority. Stored as a plain JSON dict; a per-user ``_active.txt`` records the live space."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _user_dir(self, user: str) -> Path:
        if not user or user in (".", "..") or not _SCOPE_PART.fullmatch(user):
            raise ValueError(f"bad user {user!r}")
        return self.root / user

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
    def list_users(self) -> list[str]:
        """Every user with spaces on disk (the immediate subdirs of the root)."""
        return sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.is_dir() else []

    def delete_user(self, user: str) -> int:
        """Remove ALL of a user's spaces (the whole `<root>/<user>` subtree). Returns the space count."""
        d = self._user_dir(user)                               # validates → no traversal
        if not d.is_dir():
            return 0
        n = len(list(d.glob("*.json")))
        shutil.rmtree(d)
        return n


MIGRATED_SID = "session-1"


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
                meta = {"id": MIGRATED_SID, "owner": user, "agent": agent, "title": "Session 1",
                        "public": True, "active_world": active_world or (names[0] if names else "default"),
                        "llm": ""}
                tmp = dest_sess / "session.json.tmp"
                tmp.write_text(json.dumps(meta))
                tmp.replace(dest_sess / "session.json")
                (dest_sess.parent / "_active.txt").write_text(MIGRATED_SID)   # sessions/_active.txt

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
