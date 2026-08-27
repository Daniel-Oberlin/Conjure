# Configuration — backlog

Unfinished work, future directions, and known problems for the user home, `settings.json`, and the
definition search paths. The current state is [`docs/specs/config.md`](../specs/config.md); the reasoning
behind rejected alternatives is [`docs/decisions.md`](../decisions.md).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems — verified against the code

### `default_user` is written but never read

`DEFAULT_SETTINGS` ships `"default_user": "daniel"`, `ensure_settings_file` writes it, and
`load_settings` returns it — but **nothing consumes it**. `config.DEFAULT_USER` is a module constant, and
every caller (`--user` defaults, the `/tunnel/<user>` route, `DEFAULT_SCOPE`) reads the constant.

An inert key is worse than a missing one: it invites someone to set it and quietly do nothing. Either
wire it (`DEFAULT_USER = settings.get("default_user") or "daniel"`, resolved like every other key in
[spec §2](../specs/config.md)) or drop it from the template. Wiring it is the small job and the obviously
right one — a user's own name is exactly the sort of thing that belongs in their settings.

### `CACHE_DIR` names the data root

`config.CACHE_DIR` is an alias for `DATA_DIR` — the **precious** tree — while `CACHE_ROOT` is the
disposable one. It is a leftover from before the split, kept because other modules import it, and it is
a live trap: a reader who assumes `CACHE_DIR` is a cache will conclude the wrong thing about what is safe
to delete. Rename to `DATA_DIR` at the import sites and retire the alias.

---

## Deferred by design

### Fold `.env` provider/model prefs into `settings.json`

Today provider selection and tuning live in a git-ignored `.env` (`CONJURE_LLM`, `CONJURE_IMAGE_PROVIDER`,
`CONJURE_SKYBOX_MODEL`, the co-location knobs) alongside the API-key **secrets**. The *prefs* are the same
category as `settings.json` and belong there; the *secrets* must stay in `.env`, because `settings.json`
is meant to be syncable with dotfiles.

So this is a **split, not a move** — which is why it wasn't done with the relocation
([decisions.md #21](../decisions.md)). The shape when it happens: prefs → `settings.json` under the
existing precedence ladder, secrets stay in `.env`, and `Settings` reads from both.

### Retiring the project-cache migration

[Spec §7](../specs/config.md) exists only to carry pre-2026-08-17 installs across. It is breadcrumb-guarded
and cheap, but it is also the only reason `PROJECT_CACHE` and the legacy `worlds/`/`spaces/` item names
survive in the code. Once no install predating the move is plausible, delete
`migrate_project_cache_to_home`, `_HOME_DATA_ITEMS`, `_HOME_CACHE_ITEMS` and the `PROJECT_CACHE` constant
together. No deprecation dance is needed — the failure mode of removing it too early is "an ancient
checkout keeps its data in `.cache` and appears empty", not data loss, because the migration never
deletes.

### A user overlay for `servers.json`

The MCP registry is bundled and single ([spec §4](../specs/config.md)). A user agent can name any server
in it but cannot add one, so a user-authored agent is capped at the bundled tool surface. The natural
shape mirrors the search paths: resolve `<config>/agents/servers.json` over the bundled one, merged by
server name with the user entry winning. Wanted as soon as someone writes an agent that needs a tool we
don't ship.

---

## Future directions

### The shared-server case

The layout is already the local instance of the multi-user model ([spec §8](../specs/config.md)): the home
becomes the **operator's**, `config/agents/` is what the operator offers everyone, and
`data/users/<user>/` already splits per-user data. The missing piece is per-remote-user *definitions* — a
guest uploading their own agent dir — which needs a per-user config root and a trust story that doesn't
exist yet ([decisions.md #9](../decisions.md)). Not precluded; not designed.

### Config reload without a restart

`resolve_paths()` is snapshotted into module constants at import, so editing `settings.json` requires a
restart. That's correct for path keys — moving `DATA_DIR` under a running server is not something to
support — but wrong for the cheap ones: adding a wake-word alias you just heard go wrong shouldn't cost
a session. A `reload settings` shell command re-resolving only the non-path keys would cover the real
case.

### `settings.json` has no schema

An unknown key is silently ignored and a typo'd one (`wake_word`, `agent_path`) reads as absent — the
setting simply doesn't take effect, with no complaint. Validating against the `DEFAULT_SETTINGS` key set
at `load_settings` time and warning on unknown keys would turn a silent no-op into one line of output.
