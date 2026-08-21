"""User-home path resolution (docs/user-home-plan.md §3/§4). Pure over injected env/settings, so no
real home is touched: every case passes an explicit `env` dict (and `settings` where relevant)."""

import os
from pathlib import Path

from conjure import config


# ── XDG defaults ────────────────────────────────────────────────────────────
def test_xdg_defaults_when_no_env(monkeypatch):
    # No CONJURE_HOME, no XDG_* → the three roots fall back to ~/.config, ~/.local/share, ~/.cache.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    p = config.resolve_paths(env={})
    assert p["config_dir"] == Path("/home/tester/.config/conjure")
    assert p["data_dir"] == Path("/home/tester/.local/share/conjure")
    assert p["cache_dir"] == Path("/home/tester/.cache/conjure")
    # Agent search path defaults to [<config>/agents, bundled], user first.
    assert p["agents_path"] == [Path("/home/tester/.config/conjure/agents"), config.BUNDLED_AGENTS_DIR]
    # Dynamic-module search path mirrors agents: [<config>/dynamics, bundled], user first.
    assert p["dynamics_path"] == [Path("/home/tester/.config/conjure/dynamics"), config.BUNDLED_DYNAMICS_DIR]


def test_xdg_env_vars_honored(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    env = {"XDG_CONFIG_HOME": "/x/cfg", "XDG_DATA_HOME": "/x/data", "XDG_CACHE_HOME": "/x/cache"}
    p = config.resolve_paths(env=env)
    assert p["config_dir"] == Path("/x/cfg/conjure")
    assert p["data_dir"] == Path("/x/data/conjure")
    assert p["cache_dir"] == Path("/x/cache/conjure")


# ── CONJURE_HOME consolidation ───────────────────────────────────────────────
def test_conjure_home_consolidates_all_three(monkeypatch):
    p = config.resolve_paths(env={"CONJURE_HOME": "/srv/conjure"})
    assert p["config_dir"] == Path("/srv/conjure/config")
    assert p["data_dir"] == Path("/srv/conjure/data")
    assert p["cache_dir"] == Path("/srv/conjure/cache")
    assert p["agents_path"] == [Path("/srv/conjure/config/agents"), config.BUNDLED_AGENTS_DIR]


def test_conjure_home_beats_xdg(monkeypatch):
    env = {"CONJURE_HOME": "/srv/conjure", "XDG_DATA_HOME": "/x/data"}
    assert config.resolve_paths(env=env)["data_dir"] == Path("/srv/conjure/data")


# ── precedence: env > settings.json > default ────────────────────────────────
def test_settings_overrides_default(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    p = config.resolve_paths(env={}, settings={"data_dir": "/mnt/big/conjure"})
    assert p["data_dir"] == Path("/mnt/big/conjure")
    # cache untouched by that setting → still the XDG default
    assert p["cache_dir"] == Path("/home/tester/.cache/conjure")


def test_env_beats_settings(monkeypatch):
    # The isolation guarantee (§4.1): an env override wins over a persisted settings.json value.
    p = config.resolve_paths(env={"CONJURE_DATA_DIR": "/tmp/test-data"},
                             settings={"data_dir": "/mnt/big/conjure"})
    assert p["data_dir"] == Path("/tmp/test-data")


def test_agents_path_from_settings_then_env(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    # settings list is honored verbatim (user order preserved)
    s = config.resolve_paths(env={}, settings={"agents_path": ["/a", "/b"]})
    assert s["agents_path"] == [Path("/a"), Path("/b")]
    # env (os.pathsep-joined) beats settings
    joined = os.pathsep.join(["/env/one", "/env/two"])
    e = config.resolve_paths(env={"CONJURE_AGENTS_PATH": joined}, settings={"agents_path": ["/a"]})
    assert e["agents_path"] == [Path("/env/one"), Path("/env/two")]


def test_dynamics_path_from_settings_then_env(monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/home/tester")))
    s = config.resolve_paths(env={}, settings={"dynamics_path": ["/a", "/b"]})
    assert s["dynamics_path"] == [Path("/a"), Path("/b")]
    joined = os.pathsep.join(["/env/one", "/env/two"])
    e = config.resolve_paths(env={"CONJURE_DYNAMICS_PATH": joined}, settings={"dynamics_path": ["/a"]})
    assert e["dynamics_path"] == [Path("/env/one"), Path("/env/two")]


# ── settings.json loader ─────────────────────────────────────────────────────
def test_load_settings_missing_is_empty(tmp_path):
    assert config.load_settings(tmp_path) == {}


def test_load_settings_reads_json(tmp_path):
    (tmp_path / "settings.json").write_text('{"data_dir": "/d", "default_user": "alice"}')
    assert config.load_settings(tmp_path) == {"data_dir": "/d", "default_user": "alice"}


def test_load_settings_broken_json_is_empty(tmp_path):
    # A corrupt settings file must not stop the app booting on defaults.
    (tmp_path / "settings.json").write_text("{ not json")
    assert config.load_settings(tmp_path) == {}


def test_load_settings_non_object_is_empty(tmp_path):
    (tmp_path / "settings.json").write_text('["a", "b"]')
    assert config.load_settings(tmp_path) == {}


def test_config_dir_env_beats_home_and_xdg():
    env = {"CONJURE_CONFIG_DIR": "/explicit/cfg", "CONJURE_HOME": "/srv/c", "XDG_CONFIG_HOME": "/x"}
    assert config.resolve_config_dir(env) == Path("/explicit/cfg")


# ── settings.json create-on-first-run ────────────────────────────────────────
def test_ensure_settings_file_creates_template(tmp_path):
    cfg = tmp_path / "conjure"
    path = config.ensure_settings_file(cfg)
    assert path == cfg / "settings.json"
    assert path.exists()
    data = config.load_settings(cfg)
    assert data["data_dir"] is None and data["agents_path"] is None
    assert data["default_user"] == config.DEFAULT_USER
    # A fresh template forces nothing: resolving with it == resolving with empty settings (same env).
    env = {"XDG_DATA_HOME": "/x/data", "XDG_CONFIG_HOME": "/x/cfg", "XDG_CACHE_HOME": "/x/cache"}
    assert config.resolve_paths(env=env, settings=data) == config.resolve_paths(env=env, settings={})


def test_ensure_settings_file_is_idempotent(tmp_path):
    cfg = tmp_path / "conjure"
    config.ensure_settings_file(cfg)
    (cfg / "settings.json").write_text('{"data_dir": "/mine"}')   # user edits it
    config.ensure_settings_file(cfg)                             # must NOT clobber
    assert config.load_settings(cfg) == {"data_dir": "/mine"}
