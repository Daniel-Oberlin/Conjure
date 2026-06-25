"""WorldStore — the authoritative world document and patch applier (architecture.md §5, §6).

The canonical state is held as a plain dict (`doc`) for easy nested patching and
serialization; entities are validated against the schema on ingress. Every change goes
through `apply_patch`, which computes an inverse (for undo), bumps `rev`, and returns the
full patch to broadcast.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schema import Entity, World

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
