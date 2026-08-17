# User home, settings & agent search-path — plan

**Status:** PLAN (design agreed 2026-08-17; not yet built). Moves runtime state and agent definitions
out of the project directory into a user-owned home, adds a user settings file, and makes agent
definitions a **search path** (user + bundled) instead of a single project dir. No security — users
are identity only. Extends `spaces-and-users-plan.md` (namespace) and `sessions-plan.md` (the
`.cache/users/<user>/…` tree that this doc relocates).

## 1. Why

Three things live in the project directory today that shouldn't:

- **Agent definitions** — `<project>/agents/<name>/` (agent.json + prompt.md + seed/schema). A user
  can't add their own agent without editing the checkout. We want *their own agents ⊕ the bundled
  ones*.
- **Runtime data** — `<project>/.cache/users/<user>/…` holds every session, transcript, world, space,
  and generated **asset** (images/skyboxes). This is **precious, unregenerable data** — but it's named
  "cache" and sits in the repo, so it reads as disposable (invites `rm -rf`, caught by `git clean`,
  excluded by backup tools).
- **Settings** — don't exist yet, but are coming; they belong in the user area from day one.

## 2. The two "agents" (disambiguation)

The word is overloaded on purpose; they are genuinely different and only one side changes here.

- **Agent *definition*** — `agents/<name>/agent.json` — the template/"class". Authored, versioned.
  **This doc makes it user-ownable** via a search path.
- **Agent *runtime namespace*** — `…/users/<user>/agents/<agent>/…` — a user's instance data (sessions,
  worlds, assets, state). Already user-first (sessions-plan §3); this doc only *relocates* the tree, it
  does not reshape it.

## 3. Layout — XDG by default, one-dir escape hatch

Adopt the **XDG Base Directory** split so the precious/disposable distinction is enforced by *where*
things live, not by discipline. macOS uses the same `~/.config`-style paths in practice.

```
$XDG_CONFIG_HOME/conjure/          (default ~/.config/conjure/)      ← authored config
  settings.json                    user preferences (§4)
  agents/<name>/…                  the user's OWN agent definitions (§5)

$XDG_DATA_HOME/conjure/            (default ~/.local/share/conjure/) ← precious data — BACK THIS UP
  users/<user>/agents/<agent>/sessions/<id>/{session.json,transcript.jsonl,state/,worlds/}
  users/<user>/spaces/<name>/…
  _session.txt                     global session pointer
  assets/<hash>                    content-addressed media (images/skyboxes)
  library.db                       asset catalog — curation, NOT rebuildable (see §3.1)

$XDG_CACHE_HOME/conjure/           (default ~/.cache/conjure/)       ← genuinely disposable
  tunnel_url                       current run's public tunnel URL (ephemeral)
  (thumbnails/ … )                 future regenerable derived artifacts
```

**The `.cache` → `data` split is the single most important correctness change.** Sessions/worlds/
assets/catalog move to `data/` (precious); only genuinely regenerable artifacts may live in `cache/`.

### 3.1 Current `<project>/.cache` inventory → new home

Everything on disk today, classified explicitly. **`library.db` is DATA, not cache** — server.py:349:
"a lost catalog is NOT rebuilt from the cache files" (labels, captions, public flags, embeddings are
curation). It's an index but a *precious* one.

| Today (`.cache/…`)              | What it is                                             | New home |
|---------------------------------|--------------------------------------------------------|----------|
| `users/`                        | session/world/space tree (sessions-plan §3)            | **data/**`users/` |
| `_session.txt`                  | global session pointer                                 | **data/**`_session.txt` |
| `assets/`                       | content-addressed media — images, skyboxes             | **data/**`assets/` |
| `library.db` (+ `-shm`,`-wal`)  | asset catalog: curation + captions + embeddings; **not** regenerable | **data/**`library.db` (WAL sidecars move with it) |
| `tunnel_url`                    | current run's public tunnel URL — ephemeral            | **cache/**`tunnel_url` |
| `backups/`                      | manual backups — **NOT created or managed by our code**| **left in place / untouched by migration** (user-owned; §6) |

So today the only genuinely disposable item is `tunnel_url`; the `cache/` root starts nearly empty and
grows only as we externalize regenerable artifacts (thumbnails, etc.). `backups/` is not ours to move —
the migration must skip it and never write there.

**Escape hatch — `CONJURE_HOME`.** If set, all three roots consolidate under it, for a portable /
single-dir install (the `~/.conjure` feel) and for tests pointing at a tmpdir:

```
$CONJURE_HOME/config/   $CONJURE_HOME/data/   $CONJURE_HOME/cache/
```

## 4. Settings — every location is a setting

`settings.json` (created with defaults on first run). Anything path-shaped is overridable so the data
tree can move to another volume (assets grow large) by flipping one key. Resolution order — highest
wins, following the 12-factor convention (env overrides the config file, same as git/docker/aws) and
mirroring the existing env-driven `Settings` (config.py):

```
env var  >  explicit key in settings.json  >  XDG default (or $CONJURE_HOME/…)
```

**Env beats the file on purpose** — it's what lets an ad-hoc run (or a test) override a user's
persisted `settings.json` without editing it. If the file won, a real `data_dir` in your home would
beat a test's env var and tests could touch live data.

### 4.1 How tests override — two paths, belt-and-suspenders

Resolution must emit **monkeypatchable module-level path constants** (`CACHE_DIR`/`DATA_DIR`/
`AGENTS_PATH`), not hide them behind a resolver tests can't reach — because that's how isolation
already works:

- **Unit tests** patch the resolved globals directly to a `tmp_path`
  (today's `conftest.py` — `server.ASSET_CACHE`, `server.SESSION_PTR`, `AssetLibrary(tmp_path/…)`).
  This **bypasses settings resolution entirely**, so a real `settings.json` in the dev's home can
  never leak in, regardless of precedence. Unchanged by this plan.
- **Subprocess / integration runs** set `CONJURE_HOME=<tmpdir>`. Because env > file it wins, and it
  relocates the *config* dir too → no real `settings.json` is present there → clean defaults under the
  tmpdir.

Env vars: `CONJURE_HOME`, `CONJURE_CONFIG_DIR`, `CONJURE_DATA_DIR`, `CONJURE_CACHE_DIR`,
`CONJURE_AGENTS_PATH` (`:`-separated). Settings shape (illustrative):

```json
{ "data_dir": null, "cache_dir": null,
  "agents_path": ["<config>/agents", "<bundled>"],
  "default_user": "daniel" }
```

`null` = "use the resolved default." Provider/model prefs (today's `.env`) can migrate into this file
later; out of scope for the first cut.

## 5. Agent definitions become a search PATH

Instead of one `AGENTS_DIR`, resolve an ordered list — **user first, bundled last**:

```
agents_path = [ <config>/agents , <project>/agents ]
```

- `<project>/agents` stays as the **bundled / example** set shipped with the repo.
- `<config>/agents` is where a user drops their own — satisfying "my own ⊕ the project's."
- **Precedence:** first match wins → a user can *shadow* a bundled `builder` with their own, or purely
  add new ones. `shell.agents()` unions both and annotates the source; name collisions resolve to the
  user copy.
- **`servers.json` registry** (the shared MCP registry, today `agents/servers.json`) stays bundled;
  a user agent references the same registry. (A user-supplied registry overlay is a later step.)

## 6. Migration (one-time, idempotent)

Like `migrate_cache_to_users` (world.py): on startup, if `<project>/.cache` exists and the new
`data/` root is empty, relocate per the §3.1 table — **move** `users/`, `_session.txt`, `assets/`, and
`library.db` (+ its `-shm`/`-wal` sidecars) into `$XDG_DATA_HOME/conjure/`, and `tunnel_url` into
`$XDG_CACHE_HOME/conjure/`. Back up first (the existing `pre-sessions-*` pattern). Leave a
`.cache/MOVED.txt` breadcrumb pointing at the new location.

**Skip `backups/`** — it is not created or managed by our code (user-owned manual backups); the
migration must never move, read, or write it. `<project>/agents` is likewise **not** moved (it stays
bundled). Move `library.db` while the server is stopped (WAL checkpointed) so the sidecars are
consistent.

## 7. Local vs. server (scope of "user")

Resolved: **build the local model now, don't preclude the server one.**

- **Local (now):** `~/.config/conjure/agents/` = *your* agents; `~/.local/share/conjure/users/<you>/`
  = your data. Done.
- **Shared server (later):** the home belongs to the **operator**; `config/agents/` = agents the
  operator offers to everyone; `data/users/<user>/` already splits per-connecting-user *data*.
  Per-remote-user custom *definitions* (guests uploading their own agent dirs) is the only piece this
  layout doesn't yet cover — and it isn't precluded.

## 8. Build order (proposed)

1. Central path resolver in `config.py` — `CONFIG_DIR`/`DATA_DIR`/`CACHE_DIR`/`AGENTS_PATH` from
   settings > env > XDG (with `CONJURE_HOME` consolidation). Everything downstream reads these.
2. `settings.json` load/create + defaults.
3. Repoint `CACHE_DIR`/`USERS_DIR`/`SESSION_PTR` consumers (server.py, world.py, agent_server.py) at
   `DATA_DIR`. Rename the tree `.cache` → `data`.
4. Agent search path — `load_agent` + `shell.agents()` iterate `AGENTS_PATH`; user shadows bundled.
5. Migration §6 + backup + breadcrumb.
6. Docs: README, sessions-plan §3, spaces-and-users-plan §3 updated to the new roots.

Each step tested (pytest + `node --test`) and committed; env override keeps tests on a tmpdir.

## 9. Decisions (resolved 2026-08-17)

- **XDG vs single `~/.conjure`** — ✅ XDG-by-default + `CONJURE_HOME` consolidation escape hatch (§3).
- **Agent defs: config or data?** — ✅ **config** (authored, backed-up-with-your-dotfiles), not data.
- **Fold the existing `.env` provider/model prefs into `settings.json` now, or later?** — ✅ **later.** Today all
  provider/model config lives in a git-ignored `.env` read by `config.py` (`CONJURE_LLM`,
  `CONJURE_IMAGE_PROVIDER`, `CONJURE_SKYBOX_MODEL`, the tuning knobs, plus the API-key **secrets**).
  Those prefs are the same category as `settings.json`, so consolidating is tempting — but (a) secrets
  should stay in `.env` (git-ignored, not synced with dotfiles), so a merge is a *split* not a move,
  and (b) it's orthogonal to this plan (relocation + agent path). **Recommending later:** this plan's
  `settings.json` carries **locations only** (`data_dir`, `agents_path`, `default_user`); migrate the
  provider/model prefs in a separate focused change (prefs → `settings.json`, secrets stay in `.env`).
