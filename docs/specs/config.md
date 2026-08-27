# Configuration — the user home, settings, and search paths

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/config.md`](../backlogs/config.md); rejected alternatives and
the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

This is the **installation layer**: where an installation keeps its files, what a user may set, and how
user-authored agents and dynamic modules are found. It is implemented almost entirely by
[`conjure/config.py`](../../conjure/config.py) — the one module every other one imports for a path.

Two rules run through all of it:

- **Precious and disposable never share a root.** Where a file lives is what says whether losing it
  matters, so the distinction survives a `rm -rf` by someone who didn't read this.
- **Everything user-authored is a *path*, not a directory.** Agents, dynamic modules — a user adds
  their own or shadows a bundled one without editing the checkout.

---

## 1. The three roots

Conjure follows the **XDG Base Directory** layout, so the precious/disposable split is enforced by
*where* a thing lives rather than by discipline. macOS uses the same `~/.config`-style paths in practice.

```
$XDG_CONFIG_HOME/conjure/          (default ~/.config/conjure/)      ← authored config
  settings.json                    user preferences (§3)
  agents/<name>/…                  the user's OWN agent definitions (§4)
  dynamics/<name>/…                the user's OWN dynamic modules (§4)

$XDG_DATA_HOME/conjure/            (default ~/.local/share/conjure/) ← precious — BACK THIS UP
  users/<user>/agents/<agent>/sessions/<id>/{session.json,transcript.jsonl,state/,worlds/}
  users/<user>/spaces/<name>.json
  _session.txt                     global session pointer (scope<TAB>session-id)
  assets/<hash>                    content-addressed media — images, skyboxes, models
  library.db (+ -shm, -wal)        the asset catalog

$XDG_CACHE_HOME/conjure/           (default ~/.cache/conjure/)       ← genuinely disposable
  tunnel_url                       the current run's public tunnel URL
```

The shape of the `users/` tree belongs to [`specs/agents.md §7.1`](./agents.md); this spec only says
which root it hangs off.

### 1.1 Why `library.db` is data, not cache

It is an index, but not a *derived* one. Labels, captions, public flags and embeddings are **curation** —
a lost catalog is not rebuilt by rescanning `assets/` (`server.py`, `_init_state`). It moves and is
backed up with the precious tree, WAL sidecars alongside it.

### 1.2 What is deliberately not ours

- **`<project>/.cache/backups/`** — manual backups, created by hand, never written or moved by our code.
- **`<project>/agents/`, `<project>/dynamics/`** — the *bundled* definitions shipped with the repo
  (`BUNDLED_AGENTS_DIR`, `BUNDLED_DYNAMICS_DIR`). They stay in the checkout: they are code, versioned
  with it, and they are the tail of every search path in §4.
- **`<project>/.cache/`** itself — a migration input only (§6). Nothing is written there anymore.

### 1.3 `CONJURE_HOME` — the one-dir escape hatch

If `CONJURE_HOME` is set, all three roots consolidate under it:

```
$CONJURE_HOME/config/   $CONJURE_HOME/data/   $CONJURE_HOME/cache/
```

This is the portable single-directory install (the `~/.conjure` feel), and the handle a subprocess test
uses to move an entire home onto a tmpdir in one variable.

---

## 2. Resolution — highest wins

Every location resolves the same way, following the 12-factor convention (git, docker, aws):

```
env var  >  key in settings.json  >  $CONJURE_HOME/<sub>  >  XDG default
```

| Root / path | Env var | `settings.json` key | Default |
|---|---|---|---|
| config dir | `CONJURE_CONFIG_DIR` | *(none — see below)* | `$XDG_CONFIG_HOME/conjure` |
| data dir | `CONJURE_DATA_DIR` | `data_dir` | `$XDG_DATA_HOME/conjure` |
| cache dir | `CONJURE_CACHE_DIR` | `cache_dir` | `$XDG_CACHE_HOME/conjure` |
| agent search path | `CONJURE_AGENTS_PATH` | `agents_path` | `[<config>/agents, <bundled>]` |
| dynamics search path | `CONJURE_DYNAMICS_PATH` | `dynamics_path` | `[<config>/dynamics, <bundled>]` |
| shell wake words | `CONJURE_WAKE_WORDS` | `wake_words` | `DEFAULT_WAKE_WORDS` (§5) |
| voice wake words | `CONJURE_VOICE_WAKE_WORDS` | `voice_wake_words` | *empty* (§5) |

**The config dir is the one exception** — it cannot be set by `settings.json`, because `settings.json`
lives in it. Only env and XDG feed `resolve_config_dir`.

**Env beats the file on purpose.** It is what lets an ad-hoc run or a test override a user's persisted
`settings.json` without editing it. Were the file to win, a real `data_dir` in someone's home would beat
a test's env var and the test could touch live data.

The path lists are `os.pathsep`-separated (`:` on POSIX); the wake-word lists are comma-separated. Every
value is `~`-expanded. A falsy value in `settings.json` — `null`, `""`, `[]` — reads as *absent*, so the
key falls through to the default.

### 2.1 The resolved surface

`resolve_paths(env, settings)` does the whole home in one shot and is **pure over its two arguments**, so
tests can drive it without touching a real home. Its result is snapshotted at import into module
constants, which is the form the rest of the app consumes:

| Constant | Is |
|---|---|
| `CONFIG_DIR` | the authored-config root |
| `DATA_DIR` | the precious root |
| `CACHE_ROOT` | the disposable root |
| `AGENTS_PATH`, `DYNAMICS_PATH` | ordered search paths, user-first |
| `WAKE_WORDS`, `VOICE_WAKE_WORDS` | resolved word lists, `[0]` canonical |
| `CACHE_DIR`, `USERS_DIR`, `SESSION_PTR` | aliases into `DATA_DIR` |

`CACHE_DIR` is a **historical alias for the DATA root**, not for the cache — it predates the split and is
kept because other modules import it. `CACHE_ROOT` is the disposable one. The names are a trap; the
comment in `config.py` says so at the definition.

They are module-level constants rather than a resolver behind a function specifically so tests can
monkeypatch them (§6).

---

## 3. `settings.json`

Created from a template on first run by `ensure_settings_file(CONFIG_DIR)`, called at startup — the one
place this layer writes to a real home. Idempotent: an existing file is never touched, so the template
can gain keys without disturbing anyone.

```json
{ "data_dir": null, "cache_dir": null,
  "agents_path": null, "dynamics_path": null,
  "wake_words": null, "voice_wake_words": null,
  "default_user": "daniel" }
```

Every key is `null` on purpose. A fresh file therefore changes *nothing*; it exists so the keys are
discoverable — you fill one in rather than having to learn it exists.

A broken or unreadable `settings.json` yields `{}` and never raises. Booting on defaults beats refusing
to boot over a stray comma.

> `default_user` is written into the template but **not yet read** — `DEFAULT_USER` is still a constant
> in `config.py`. Tracked in the [backlog](../backlogs/config.md).

### 3.1 What belongs here vs. in `.env`

Two files, one line between them:

- **`settings.json`** — *locations and preferences.* Authored, safe to sync with your dotfiles.
- **`.env`** (git-ignored, `.env.example` is the template) — *provider selection, connectivity, tuning
  knobs, and every **secret*** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`,
  `POLY_PIZZA_API_KEY`, `GOOGLE_API_KEY`). Read into the `Settings` dataclass; see
  [`docs/providers.md`](../providers.md).

The provider/model *preferences* in `.env` are the same category as `settings.json` and could move; the
secrets can't, because `settings.json` is meant to be syncable. So consolidating is a **split**, not a
move — deferred, and in the [backlog](../backlogs/config.md).

---

## 4. Agent and dynamics definitions are search PATHs

Two words named "agent" collide here, and only one of them is in scope:

- **Agent *definition*** — `agents/<name>/{agent.json,prompt.md,…}` — the authored template. **This is
  what the search path finds.**
- **Agent *runtime namespace*** — `users/<user>/agents/<agent>/…` — a user's instance data. That tree is
  [`specs/agents.md §7.1`](./agents.md); §1 above only relocates it.

Definitions resolve against an ordered list, **user first, bundled last**:

```
AGENTS_PATH   = [ <config>/agents ,   <project>/agents   ]
DYNAMICS_PATH = [ <config>/dynamics , <project>/dynamics ]
```

- **First match wins**, keyed on the *directory name* — which is the definition's identity. A user
  `builder` shadows the bundled `builder`; a user `erotic` simply adds one.
- `resolve_agent_dir(name)` returns the first `<dir>/<name>/agent.json` on the path, and raises naming
  every directory it tried — a missing agent says where it looked.
- `list_agents()` unions the path first-match-wins and tags each entry `user` or `bundled`; the shell's
  `agents` listing marks the user ones `(user)`, so a shadow is visible rather than mysterious. (The
  spoken form drops the tag, like every other typographic marker — [`specs/agents.md`](./agents.md).)
- The path is read **live** from `config.AGENTS_PATH` at each call (not captured at import), which is
  what lets a test monkeypatch it.
- Dynamic modules resolve identically (`resolve_dynamics_path`, [`specs/dynamics.md §3`](./dynamics.md)),
  and an agent's declared `dynamics` are validated against that path at load — a module that doesn't
  resolve fails the agent loudly instead of vanishing at runtime.
- **`servers.json`** — the shared MCP registry — stays **bundled** (`<project>/agents/servers.json`). A
  user agent references the same registry; a user-supplied overlay is [backlog](../backlogs/config.md).

---

## 5. Wake words

Two distinct gates, two settings keys. What the words *do* is
[`specs/agents.md`](./agents.md) — the shell's command escape and the voice client's mic gate. What this
layer owns is that they are **configuration, resolved like everything else in §2**, plus one invariant.

- **`wake_words`** — the shell's escape word and its STT mis-hearings. Ships with a real list
  (`conjure`, `coinjure`, `conjur`, …) because "conjure" isn't in most STT vocabularies and a mis-hear
  doesn't fail loudly — it delivers a *command* to the LLM as if it were conversation. Entries must be
  non-words; a real word here would swallow ordinary speech.
- **`voice_wake_words`** — the mic-activation gate. **Ships empty**: the gate is opt-in, and which word
  suits a room is the user's call. With none set, every utterance passes through.
- **The two lists must be disjoint.** The mic gate *consumes* its word before anything downstream sees
  the line, so a shared word makes shell commands unreachable by voice — you would have to say it twice.
  `wake_word_conflict(shell, voice)` returns every overlap; the voice client refuses to start on a
  non-empty result.

Both lists are lowercased, stripped and de-duplicated with order preserved. **Entry `[0]` is canonical** —
the one a user is told to say; the rest exist only to be forgiving. A list that resolves to nothing
usable falls back to the shipped default rather than to silence.

Amend by observation, not imagination: add the mis-hearing you actually saw in the log, via env or
`settings.json` — not by editing the constant.

---

## 6. How tests stay off a real home

Two independent mechanisms, deliberately belt-and-braces:

- **Unit tests monkeypatch the resolved constants** (`server.ASSET_CACHE`, `server.SESSION_PTR`,
  `config.AGENTS_PATH`, …) straight to a `tmp_path`. This **bypasses resolution entirely**, so a real
  `settings.json` in the developer's home can never leak in regardless of precedence. This is why §2.1
  exposes constants and not just a resolver.
- **Subprocess / integration runs set `CONJURE_HOME=<tmpdir>`.** Env beats the file, and it relocates the
  *config* root too — so no real `settings.json` is even present there, and the run gets clean defaults
  under the tmpdir.

`resolve_paths` and every `resolve_*` beneath it take `env` and `settings` as arguments, so the precedence
ladder itself is testable without any filesystem at all (`tests/test_config_paths.py`).

---

## 7. The one-time migration out of the project

Conjure used to keep all of this in `<project>/.cache/`. `migrate_project_cache_to_home(project_cache,
data_dir, cache_dir)` (`conjure/world.py`) relocates it, and runs in `_init_state` **before anything opens
a path under the home** — before the catalog, the repositories, or the asset dir.

- **A move, never a copy or a delete.** Content is preserved and content-addressed assets stay valid.
  `Path.replace` where possible (atomic within a filesystem), `shutil.move` across devices.
- **What moves to `data/`:** `users/`, `_session.txt`, `assets/`, `library.db` + its `-shm`/`-wal`
  sidecars, and the pre-user legacy `worlds/`/`spaces/` trees if still present.
- **What moves to `cache/`:** `tunnel_url`. It is written by an external shell script
  (`scripts/tunnel.sh`), which resolves the same `CACHE_ROOT` from config so both ends agree.
- **What is skipped:** `backups/` (§1.2), and `<project>/agents`, which is bundled, not data.
- **Idempotent** via a `<project>/.cache/MOVED.txt` breadcrumb — once written, re-runs no-op. An
  interrupted run resumes, because each item-move skips a missing source and a move into a pre-created
  empty destination merges rather than failing.
- **A guard refuses to migrate the home onto itself** (a test pointing `.cache` *at* the data root).

**The tree kept its `.cache`-era relative shape; only the root moved.** The children are still `users/`,
`assets/`, `library.db`, `_session.txt`. The rename that matters — disposable vs precious — is expressed
by the root, not by renaming everything under it.

**Two processes, one migration.** Only the world server runs it. The agent server reads `USERS_DIR`
lazily, per request, so if it happens to boot first on the very first migrating run it simply sees no
sessions until the world server relocates them — no crash, self-healing. Normal launch starts the world
server first anyway.

---

## 8. Users are identity, not security

`default_user`, `--user`, the `/tunnel/<user>` route: a user name selects a namespace and nothing more.
There is no authentication, no authorization, and no attempt at either. The consequences of that for
spaces and sessions are [`specs/spaces.md`](./spaces.md); here it is just the reason nothing in this
layer is a permission check.

The layout is nonetheless the local case of a model that generalises: on a shared server the home would
belong to the **operator** — `config/agents/` being the agents the operator offers everyone, and
`data/users/<user>/` already splitting per-user data. Per-remote-user *definitions* are the one piece
this layout doesn't yet reach, and are not precluded by it.
