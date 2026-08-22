"""Dynamic-module definitions loader (conjure.dynamics) — the first-class, extensible structure behind
conjurable effects, a mirror of conjure.agents. Pure file loading + validation; no network, no A-Frame,
no world server. (docs/dynamic-modules-refactor-plan.md, docs/dynamic-module-spec.md)"""
import json

import pytest

from conjure.dynamics import (DynamicModuleDef, list_modules, load_all, load_module, load_modules,
                              module_names, resolve_module_dir)


def _write_module(tmp_path, name, data, files=None):
    """Create a `<tmp_path>/<name>/module.json` def (+ any extra files: {relpath: text}). Defaults an
    `entry` file so the existence check passes unless the test overrides it."""
    d = tmp_path / name
    d.mkdir()
    (d / "module.json").write_text(json.dumps(data))
    extra = dict(files or {})
    for f in ([data["entry"]] if isinstance(data.get("entry"), str) else (data.get("entry") or [])):
        extra.setdefault(f, "/* stub */")
    for rel, text in extra.items():
        (d / rel).write_text(text)
    return tmp_path


# ── the bundled modules load + describe themselves ────────────────────────────────────────────────
def test_bundled_modules_load():
    names = module_names()
    assert "fireflies" in names and "water" in names
    water = load_module("water")
    assert water.component == "water" and water.entry == ["water.js"] and water.face_user is True
    assert water.anchor == "free" and water.default_pos == [0.0, 1.4, -1.2]
    fireflies = load_module("fireflies")
    assert fireflies.anchor == "volume" and fireflies.face_user is False
    grab = load_module("grab")   # tier-C ambient singleton manipulation module
    assert grab.tier == "C" and grab.anchor == "ambient" and grab.singleton is True


def test_catalog_line_renders_params_with_defaults():
    line = load_module("fireflies").catalog_line()
    assert line.startswith("fireflies —")
    assert "count(40)" in line and "seed(1)" in line
    # a param with no default (water's `image`) is shown bare
    assert "image" in load_module("water").catalog_line()


# ── search path: user shadows bundled (mirror agents) ─────────────────────────────────────────────
def test_search_path_user_shadows_bundled(tmp_path):
    user = tmp_path / "user"; bundled = tmp_path / "bundled"
    user.mkdir(); bundled.mkdir()
    _write_module(user, "water", {"component": "water", "entry": "w.js", "description": "USER water"})
    _write_module(bundled, "water", {"component": "water", "entry": "w.js", "description": "bundled water"})
    _write_module(bundled, "fog", {"component": "fog", "entry": "f.js", "description": "bundled fog"})
    path = [user, bundled]
    assert load_module("water", dynamics_path=path).description == "USER water"
    assert load_module("fog", dynamics_path=path).description == "bundled fog"
    assert resolve_module_dir("water", path) == user / "water"


def test_list_modules_annotates_source(tmp_path, monkeypatch):
    from conjure import config
    user = tmp_path / "user"; bundled = tmp_path / "bundled"
    user.mkdir(); bundled.mkdir()
    _write_module(user, "mine", {"component": "mine", "entry": "m.js"})
    _write_module(user, "water", {"component": "water", "entry": "w.js"})
    _write_module(bundled, "water", {"component": "water", "entry": "w.js"})
    monkeypatch.setattr(config, "BUNDLED_DYNAMICS_DIR", bundled)
    monkeypatch.setattr(config, "DYNAMICS_PATH", [user, bundled])
    assert list_modules() == [("mine", "user"), ("water", "user")]
    assert module_names() == ["mine", "water"]


def test_resolve_module_dir_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found in search path"):
        resolve_module_dir("ghost", [tmp_path])


# ── validation ────────────────────────────────────────────────────────────────────────────────────
def test_missing_component_is_rejected(tmp_path):
    _write_module(tmp_path, "bad", {"entry": "b.js"})
    with pytest.raises(ValueError, match="component"):
        load_module("bad", dynamics_dir=tmp_path)


def test_missing_entry_is_rejected(tmp_path):
    _write_module(tmp_path, "bad", {"component": "bad"})
    with pytest.raises(ValueError, match="entry"):
        load_module("bad", dynamics_dir=tmp_path)


def test_entry_file_must_exist(tmp_path):
    # manifest references a script that isn't on disk → fail loud (catches a typo'd/forgotten file)
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "module.json").write_text(json.dumps({"component": "bad", "entry": "missing.js"}))
    with pytest.raises(ValueError, match="not found"):
        load_module("bad", dynamics_dir=tmp_path)


def test_unknown_anchor_is_rejected(tmp_path):
    _write_module(tmp_path, "bad", {"component": "bad", "entry": "b.js", "anchor": "sideways"})
    with pytest.raises(ValueError, match="anchor"):
        load_module("bad", dynamics_dir=tmp_path)


def test_name_field_must_match_the_directory(tmp_path):
    _write_module(tmp_path, "x", {"name": "y", "component": "x", "entry": "x.js"})
    with pytest.raises(ValueError, match="name"):
        load_module("x", dynamics_dir=tmp_path)


def test_entry_string_or_list_normalizes_to_list(tmp_path):
    _write_module(tmp_path, "s", {"component": "s", "entry": "one.js"})
    _write_module(tmp_path, "l", {"component": "l", "entry": ["a.js", "b.js"]})
    assert load_module("s", dynamics_dir=tmp_path).entry == ["one.js"]
    assert load_module("l", dynamics_dir=tmp_path).entry == ["a.js", "b.js"]


# ── bulk loaders ────────────────────────────────────────────────────────────────────────────────
def test_load_modules_requires_every_name(tmp_path):
    _write_module(tmp_path, "here", {"component": "here", "entry": "h.js"})
    got = load_modules(["here"], dynamics_path=[tmp_path])
    assert list(got) == ["here"] and isinstance(got["here"], DynamicModuleDef)
    with pytest.raises(FileNotFoundError):
        load_modules(["here", "gone"], dynamics_path=[tmp_path])


def test_load_all_discovers_everything(tmp_path):
    _write_module(tmp_path, "a", {"component": "a", "entry": "a.js"})
    _write_module(tmp_path, "b", {"component": "b", "entry": "b.js"})
    got = load_all(dynamics_path=[tmp_path])
    assert set(got) == {"a", "b"}
