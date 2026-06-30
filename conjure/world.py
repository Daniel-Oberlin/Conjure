"""WorldStore — the authoritative world document and patch applier (architecture.md §5, §6).

The canonical state is held as a plain dict (`doc`) for easy nested patching and
serialization; entities are validated against the schema on ingress. Every change goes
through `apply_patch`, which computes an inverse (for undo), bumps `rev`, and returns the
full patch to broadcast.
"""

from __future__ import annotations

import json
import re
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

    def _path(self, scope: str, name: str) -> Path:
        return self._scope_dir(scope) / f"{world_path(name)}.json"

    def list(self, scope: str) -> list[str]:
        """All worlds in the scope, as canonical hierarchical paths ('castle-quest/dining-hall'),
        recursively. The per-scope '_active.txt' pointer isn't a world, so it's never listed."""
        d = self._scope_dir(scope)
        if not d.is_dir():
            return []
        return sorted(p.relative_to(d).as_posix()[: -len(".json")] for p in d.rglob("*.json"))

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
        return self._path(scope, name).exists()

    def load(self, scope: str, name: str) -> "WorldStore":
        return WorldStore.load(self._path(scope, name))

    def save(self, scope: str, name: str, store: "WorldStore") -> None:
        store.save(self._path(scope, name))

    def delete(self, scope: str, name: str) -> bool:
        p = self._path(scope, name)
        if not p.exists():
            return False
        p.unlink()
        if self.get_active(scope) == world_path(name):
            (self._scope_dir(scope) / "_active.txt").unlink(missing_ok=True)
        scope_dir = self._scope_dir(scope)                 # prune now-empty parent folders in the tree
        d = p.parent
        while d != scope_dir and d.is_dir() and not any(d.iterdir()):
            d.rmdir()
            d = d.parent
        return True

    def get_active(self, scope: str) -> str | None:
        p = self._scope_dir(scope) / "_active.txt"
        return (p.read_text().strip() or None) if p.exists() else None

    def set_active(self, scope: str, name: str) -> None:
        d = self._scope_dir(scope)
        d.mkdir(parents=True, exist_ok=True)
        (d / "_active.txt").write_text(world_path(name))


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
