"""Dynamic-module definitions — the declarative layer behind conjurable, animated effects.

A *dynamic module* is a live, shared A-Frame component the world delivers as config-in-snapshot (see
docs/dynamic-content-plan.md, docs/dynamic-module-spec.md): the world server adds an entity carrying
the module's component, so every headset renders the same effect (deterministic from the shared clock),
and it persists on the existing entity/patch/snapshot path — no bespoke loader.

This module is the **first-class, extensible** structure for those effects, a direct mirror of
`conjure.agents` (docs/dynamic-modules-refactor-plan.md): each module is a self-contained directory —
`dynamics/<name>/module.json` plus its client script(s) and any assets. The directory name *is* the
module's identity. User modules resolve on a search path (user shadows bundled), exactly like agents.
This module just **loads and validates** those defs; the runtime wiring (serving the JS, placing an
instance, scoping to an agent) lives in server.py and director.py.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import config

MANIFEST = "module.json"        # the per-module manifest filename (mirrors agent.json)

# Anchor kinds — where a module is meant to live (drives placement + the director catalog copy).
ANCHORS = ("free", "surface", "volume", "ambient")


def _search_path(dynamics_path: Optional[list[Path]] = None) -> list[Path]:
    """The dynamic-module search path — the explicit arg, else the resolved `config.DYNAMICS_PATH`
    (read live so tests can monkeypatch it). User dirs first, bundled last (docs/user-home-plan.md §5)."""
    return dynamics_path if dynamics_path is not None else config.DYNAMICS_PATH


def resolve_module_dir(name: str, dynamics_path: Optional[list[Path]] = None) -> Path:
    """First `<dir>/<name>/module.json` in the search path — so a user module shadows a bundled one of
    the same name. Raises FileNotFoundError if the name is nowhere on the path."""
    for base in _search_path(dynamics_path):
        if (base / name / MANIFEST).exists():
            return base / name
    tried = [str(p) for p in _search_path(dynamics_path)]
    raise FileNotFoundError(f"dynamic module {name!r} not found in search path: {tried}")


def list_modules(dynamics_path: Optional[list[Path]] = None) -> list[tuple[str, str]]:
    """Sorted unique `(name, source)` across the search path, first-match-wins (so a shadowing user
    module hides the bundled one). `source` is 'bundled' for the bundled dir, else 'user'."""
    seen: dict[str, str] = {}
    for base in _search_path(dynamics_path):
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir()):
            if p.name not in seen and (p / MANIFEST).exists():
                seen[p.name] = "bundled" if base == config.BUNDLED_DYNAMICS_DIR else "user"
    return sorted(seen.items())


def module_names(dynamics_path: Optional[list[Path]] = None) -> list[str]:
    """Just the names from `list_modules` (the common case)."""
    return [n for n, _ in list_modules(dynamics_path)]


@dataclass
class DynamicModuleDef:
    """A loaded module manifest — the contract between a module folder and the runtime.

    `dir` is the resolved module directory (where the client scripts + assets live); the world server
    serves `GET /dynamics/<name>/<file>` from it. `entry` is always normalized to a list of script
    filenames (a manifest may give a string or a list). `config_schema` is the LLM-facing parameter
    surface — `{param: {type, default, desc}}` — rendered into the director's `dynamics://available`
    catalog and validated/passed through to the component's A-Frame schema client-side.
    """
    name: str
    dir: Path
    component: str                                        # the A-Frame component the entry registers
    entry: list[str] = field(default_factory=list)       # client script(s) to load, in order
    tier: str = "A"                                       # A|B|C (informational; docs/dynamic-content-plan.md)
    anchor: str = "free"                                  # free | surface | volume | ambient
    singleton: bool = False                               # one live instance reused across conjures
    face_user: bool = False                               # free-standing flat content faces the viewer at creation
    default_pos: list[float] = field(default_factory=lambda: [0.0, 1.3, -1.5])
    description: str = ""                                 # one line → the director catalog
    config_schema: dict = field(default_factory=dict)    # {param: {type, default, desc}} the LLM may set

    def catalog_line(self) -> str:
        """One director-catalog row: `name — description; params: k(default)…` (decision 1). Params come
        from `config_schema`; a param with no default is shown bare. Empty schema → no params clause."""
        params = []
        for key, spec in (self.config_schema or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            default = spec.get("default")
            params.append(f"{key}({default})" if default is not None else key)
        head = f"{self.name} — {self.description}".rstrip(" —") if self.description else self.name
        return f"{head}; params: {', '.join(params)}" if params else head


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise FileNotFoundError(f"not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"invalid JSON in {path}: {e}") from e


def load_module(name: str, *, dynamics_dir: Optional[Path] = None,
                dynamics_path: Optional[list[Path]] = None) -> DynamicModuleDef:
    """Load and validate one dynamic-module definition.

    Resolution mirrors `agents.load_agent`: an explicit `dynamics_dir` pins a single directory
    (`<dynamics_dir>/<name>`; used by tests); otherwise the name is looked up on the search path
    (`dynamics_path`, else `config.DYNAMICS_PATH`), so a user module shadows a bundled one.

    The directory name is the module's identity, so `module.json` needn't repeat it (a `name` field, if
    present, is validated to match). Raises ValueError on a malformed def, FileNotFoundError on a missing
    manifest.
    """
    module_dir = (dynamics_dir / name) if dynamics_dir is not None else resolve_module_dir(name, dynamics_path)
    data = _read_json(module_dir / MANIFEST)
    if data.get("name", name) != name:
        raise ValueError(f"dynamic module {name!r}: 'name' field is {data.get('name')!r}, expected {name!r}")

    component = data.get("component")
    if not component or not str(component).strip():
        raise ValueError(f"dynamic module {name!r}: needs a non-empty 'component'")

    entry_raw = data.get("entry")
    entry = [entry_raw] if isinstance(entry_raw, str) else list(entry_raw or [])
    if not entry:
        raise ValueError(f"dynamic module {name!r}: needs an 'entry' script (string or list)")
    for f in entry:                                      # each entry must actually exist beside the manifest
        if not (module_dir / f).exists():
            raise ValueError(f"dynamic module {name!r}: entry script {f!r} not found in {module_dir}")

    anchor = data.get("anchor", "free")
    if anchor not in ANCHORS:
        raise ValueError(f"dynamic module {name!r}: unknown anchor {anchor!r} (use one of {ANCHORS})")

    default_pos = list(data.get("default_pos") or [0.0, 1.3, -1.5])

    return DynamicModuleDef(
        name=name, dir=module_dir, component=str(component), entry=entry,
        tier=str(data.get("tier", "A")), anchor=anchor,
        singleton=bool(data.get("singleton", False)), face_user=bool(data.get("face_user", False)),
        default_pos=default_pos, description=data.get("description", ""),
        config_schema=dict(data.get("config_schema") or {}),
    )


def load_modules(names: list[str], *, dynamics_path: Optional[list[Path]] = None,
                 ) -> dict[str, DynamicModuleDef]:
    """Load an explicit set of modules by name (an agent's `dynamics` list) → {name: def}, order
    preserved. Every name is REQUIRED: a missing one raises FileNotFoundError (agents fail to load with
    a dangling module reference — decision "Required" in the plan)."""
    return {name: load_module(name, dynamics_path=dynamics_path) for name in names}


def load_all(dynamics_path: Optional[list[Path]] = None) -> dict[str, DynamicModuleDef]:
    """Every module discovered on the search path → {name: def} (first-match-wins). The world server's
    full registry: it serves + places ALL discovered modules, then scopes per active agent."""
    return {name: load_module(name, dynamics_path=dynamics_path) for name in module_names(dynamics_path)}
