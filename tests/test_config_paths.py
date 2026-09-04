"""User-home path resolution (docs/specs/config.md §1/§2). Pure over injected env/settings, so no
real home is touched: every case passes an explicit `env` dict (and `settings` where relevant)."""

import json
import os
from pathlib import Path

import pytest

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


# --------------------------------------------------------------------------- wake words
#
# "conjure" is not in most STT vocabularies, so Whisper guesses at something phonetically near. A
# mis-heard wake word doesn't fail loudly — it silently sends a COMMAND to the agent as content — so the
# aliases are configuration, amendable as new mis-hearings turn up.

def test_wake_words_default_to_the_shipped_list():
    from conjure.config import DEFAULT_WAKE_WORDS, resolve_wake_words
    assert resolve_wake_words({}, {}) == list(DEFAULT_WAKE_WORDS)
    assert DEFAULT_WAKE_WORDS[0] == "conjure"          # [0] is canonical — what a user is told to say
    assert "coinjure" in DEFAULT_WAKE_WORDS            # the observed mis-hearing


def test_wake_words_resolve_env_over_settings_over_default():
    from conjure.config import resolve_wake_words
    assert resolve_wake_words({"CONJURE_WAKE_WORDS": "hey,yo"}, {"wake_words": ["ignored"]}) == ["hey", "yo"]
    assert resolve_wake_words({}, {"wake_words": ["Abra", "Cadabra"]}) == ["abra", "cadabra"]


def test_wake_words_are_normalised_and_never_empty():
    from conjure.config import DEFAULT_WAKE_WORDS, resolve_wake_words
    assert resolve_wake_words({"CONJURE_WAKE_WORDS": " Hey , HEY,hey ,  "}, {}) == ["hey"]   # dedupe + fold
    assert resolve_wake_words({"CONJURE_WAKE_WORDS": " , , "}, {}) == list(DEFAULT_WAKE_WORDS)  # never none


def test_wake_aliases_expands_the_canonical_word_only():
    """`--wake-word conjure` should also catch its mis-hearings; `--wake-word banana` means banana."""
    from conjure.config import WAKE_WORDS, wake_aliases
    assert wake_aliases() == WAKE_WORDS
    assert wake_aliases("Conjure") == WAKE_WORDS       # case-insensitive, expands
    assert wake_aliases("banana") == ["banana"]


def test_the_mic_gate_ships_no_word_at_all():
    """The gate is opt-in — with nothing configured every utterance passes through — and which word
    suits a room is the user's call. Naming one is what `--wake-word` is for. (The shell's escape is
    the opposite: it must work out of the box, so it ships its word and the mis-hearings.)"""
    from conjure.config import DEFAULT_VOICE_WAKE_WORDS, VOICE_WAKE_WORDS, WAKE_WORDS, wake_word_conflict
    assert DEFAULT_VOICE_WAKE_WORDS == () and VOICE_WAKE_WORDS == []
    assert WAKE_WORDS[0] == "conjure"
    assert wake_word_conflict(WAKE_WORDS, VOICE_WAKE_WORDS) == []      # nothing to collide with


def test_wake_word_conflict_names_every_overlap():
    from conjure.config import wake_word_conflict
    assert wake_word_conflict(["conjure", "coinjure"], ["computer"]) == []
    assert wake_word_conflict(["conjure", "coinjure"], ["coinjure", "x"]) == ["coinjure"]


def test_voice_wake_words_resolve_independently_of_the_shell_list():
    from conjure.config import DEFAULT_VOICE_WAKE_WORDS, resolve_voice_wake_words, voice_wake_aliases
    assert resolve_voice_wake_words({}, {}) == []                      # no shipped word
    assert resolve_voice_wake_words({"CONJURE_VOICE_WAKE_WORDS": "hey,HEY"}, {}) == ["hey"]
    assert resolve_voice_wake_words({}, {"voice_wake_words": ["Ahoy"]}) == ["ahoy"]
    assert voice_wake_aliases("banana") == ["banana"]      # only the canonical expands


def test_an_alias_spec_may_name_several_words():
    """`--wake-word hey,hay,hei`. Before this the flag looked like it took a list and instead matched the
    literal string "hey,hay,hei" — the silent-wrong failure, not a loud one."""
    from conjure.config import voice_wake_aliases, wake_aliases
    assert voice_wake_aliases("hey,hay,hei") == ["hey", "hay", "hei"]
    assert voice_wake_aliases(" Hey , HAY ,hey") == ["hey", "hay"]     # folded, trimmed, de-duplicated
    assert voice_wake_aliases("banana") == ["banana"]                  # a lone word is still literal
    # a single word that IS the configured canonical still expands to its list (the shell's case)
    assert wake_aliases("conjure") == wake_aliases()


# --------------------------------------------------------------------------- runtime settings (`set`)

def test_a_settings_key_knows_its_tier_and_its_env_var():
    from conjure import config
    assert config.tier_of("foveation") == "client"      # baked into the page at load
    assert config.tier_of("history_cap") == "live"      # read per turn
    assert config.tier_of("port") == "boot"             # read once, at startup
    assert config.tier_of("openai_api_key") == "secret"
    assert config.tier_of("no_such_knob") == "unknown"
    assert config.env_var("foveation") == "CONJURE_FOVEATION"
    assert config.env_var("world_url") == "CONJURE_URL"          # one of the six that don't follow


def test_a_value_outside_its_range_is_refused_not_clamped():
    from conjure import config
    assert config.parse_value("foveation", "0.3") == 0.3
    for key, bad in [("foveation", "3"), ("occlusion", "sideways"), ("history_cap", "lots")]:
        with pytest.raises(ValueError):
            config.parse_value(key, bad)


def test_preferences_sit_below_env_and_above_the_default(tmp_path, monkeypatch):
    # The whole precedence ladder of spec §2, and the reason `--save` writes the file rather than .env:
    # the machine-managed file sits BELOW the hand-authored variable, so a save can never fight you.
    from conjure import config
    monkeypatch.setattr(config, "_seeded", {})
    (tmp_path / "settings.json").write_text(json.dumps(
        {"preferences": {"foveation": 0.3, "pose_tau": 0.5}}))
    monkeypatch.setenv("CONJURE_POSE_TAU", "0.9")            # already set by hand
    monkeypatch.delenv("CONJURE_FOVEATION", raising=False)
    config.seed_preferences(tmp_path)
    assert os.environ["CONJURE_FOVEATION"] == "0.3" and config.source_of("foveation") == "settings.json"
    assert os.environ["CONJURE_POSE_TAU"] == "0.9" and config.source_of("pose_tau") == "env"

    config.apply_override("foveation", 0.7)                  # `set` for this run
    assert config.source_of("foveation") == "env"
    config.clear_override("foveation")                       # `unset` falls to the saved value, not away
    assert os.environ["CONJURE_FOVEATION"] == "0.3" and config.source_of("foveation") == "settings.json"


def test_a_secret_never_enters_settings_json(tmp_path, monkeypatch):
    # settings.json is meant to be syncable with dotfiles, so it must never carry a credential — not
    # written by us, and not kept if someone hand-edits one in.
    from conjure import config
    monkeypatch.setattr(config, "_seeded", {})
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "settings.json").write_text(json.dumps(
        {"preferences": {"openai_api_key": "sk-nope", "foveation": 0.3}}))
    config.seed_preferences(tmp_path)
    assert "OPENAI_API_KEY" not in os.environ             # a secret in the file is never honoured
    with pytest.raises(ValueError):
        config.save_preference("openai_api_key", "sk-nope", tmp_path)
    config.save_preference("pose_tau", 0.25, tmp_path)    # …and any already there is stripped on write
    prefs = json.loads((tmp_path / "settings.json").read_text())["preferences"]
    assert sorted(prefs) == ["foveation", "pose_tau"]
