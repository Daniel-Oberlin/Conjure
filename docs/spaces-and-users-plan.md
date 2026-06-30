# Spaces & Users — plan

**Status:** PLAN (design agreed 2026-06-26; not yet built). Realizes the **shared room layer** as a
first-class, user-owned concept (it absorbed the earlier shared-room-layer design) and reworks the
namespace in `persistence-model.md` §1. No security — users are identity only.

## 1. What's new

Three concepts and a namespace rework:

- **Users** — primitive identity (a username, no auth). The system has one "logged-in" user per
  connection. Drives ownership and the public/private split.
- **Spaces** — a *physical* environment (the captured real surfaces + boundary), **owned by a user**,
  with an approximate **geolocation**. This is the shared room layer realized: geometry stored once,
  not per-world. The owner's headset is always the space's capture **authority**.
- **Worlds are associated with a space** (or the **`<void>`** space, for purely-virtual worlds with no
  physical tie). A world = a space's geometry + the world's own placed objects, surface-style overlay,
  and display prefs.
- **Co-location** — a second user may **join a public world** owned by another user and inhabit the
  same space, co-located via shared-geometry registration (no platform anchors — see §8).

## 2. The model

```
User (username)
  ├── spaces/<name>            ← physical env: surfaces + boundary + geolocation; authority = owner
  └── agents/<agent>/
        ├── worlds/<name>      ← placed objects + surfaceStyles overlay + prefs; → references a space
        ├── assets/<hash>      ← content-addressed media
        └── state/<name>       ← agent KV/doc state
```

Spaces are **user-owned** (physical, agent-agnostic). Worlds/assets/state are **agent-owned** under a
user. Every item carries a `public: bool` flag (§4).

## 3. Namespace & scope

```
/<user>/[agents/<agent>/]<category>/<name>

  /daniel/spaces/home
  /daniel/agents/builder/worlds/bladerunner1
  /daniel/agents/builder/assets/<hash>.glb
```

- **User-first.** The owner is the top path segment.
- **Visibility is a FLAG, not a path segment** (decided). `public: bool` lives on each item, so:
  publishing/unpublishing is a metadata flip that never relocates an item or breaks references;
  content-addressed assets keep their hash id; access is a predicate, not path math. (Path-encoded
  visibility was considered and rejected for these reasons.)
- **Scope is still a capability**, now `<user>/agents/<agent>`, injected by the runtime
  (`CONJURE_USER` + `CONJURE_AGENT` at MCP launch; the web client's user comes from the tunnel, §8) —
  never an LLM argument. Visibility is enforced server-side by predicate.

## 4. Access rules

- **Default public.** New items are `public: true` by default.
- **Visibility inherits the active world.** A new asset/state created while a *private* world is active
  defaults **private**; while a *public* world is active, **public**. "Make a private world" sets the
  world private, and everything subsequently created in it is private by default.
- **Read:** `can_read(u, item) = item.owner == u OR item.public`. **Write:** `item.owner == u`.
- **Public-uses-public invariant:** a public world may reference only public assets/state — so a guest
  can load the whole scene. Pulling a private asset into a public world is **forbidden/warned**.
- **Guest join:** another user may join **only a public** world. Joining a **private** world is refused
  with an **info message** (rendered in the info color) — no world data is sent.

## 5. Spaces (= the shared room layer, owned + geolocated)

- **Record:** `Space { id, owner, name, public, geolocation {lat, lon, accuracy}, surfaces[], boundary }`.
  Stored at `/<user>/spaces/<name>` → `.cache/spaces/<user>/<name>.json`. Geometry + default materials
  only (per-world styling lives in the world's overlay — §6).
- **Authority = owner's headset.** Only the owner may report/update a space's geometry (`ingest_room`
  checks the connection's user == space owner). Guests register *against* it, never re-capture it.
- **One active space per server** (agreed). The owner's session has exactly one space live.
- **`<void>`** — a sentinel space with no surfaces / no geolocation, for worlds with no physical tie
  (e.g. a grounded-skybox landscape). Worlds in `<void>` render pure-virtual.
- **Compose/decompose:** the in-memory `store.doc` stays fully composed
  (space geometry + the active world's `surfaceStyles` + placed entities + prefs); only persistence
  splits — geometry → the space file, styling/placed/prefs → the world file. Client/patch/director
  paths unchanged.

## 6. Worlds ↔ spaces

- `World { …, space: "<user>/spaces/<name>" | "<void>", public, surfaceStyles, entities (placed), env(prefs) }`.
- Loading a world: resolve its space → compose. If the world's space isn't the currently-active
  physical space (geolocation/geometry mismatch, §7), warn/offer to re-home (deferred detail).

## 7. Geolocation & nearest-space-on-load

- **Confirmed:** the Quest browser returns coarse geolocation (~hundreds of feet) via
  `navigator.geolocation` after permission. Good enough as a **prefilter**, not a fine discriminator.
- **On space creation:** stamp the space with the headset's current `{lat, lon, accuracy}`.
- **On session start:** get current location → shortlist the owner's spaces within range → **confirm by
  geometry registration** (the §register vote: does the live capture match a space's surfaces?). Match →
  that space is active; no match nearby → offer to create a new space. (Geolocation separates *distant*
  spaces — home vs. office; the registration vote separates *nearby* ones — two rooms in a house.)

## 8. Co-location

> **Detailed Phase-4 design: `co-location-plan.md`** — per-connection user identity, the public-join
> gate, authority = space owner, presence avatars, the matcher-robustness work, **and a desktop-browser
> guest mode** (no AR) for testing co-location with a single headset.

- **Join flow.** A guest opens the **owner's** running server via the tunnel under their *own* name
  (`…/tunnel/bob` on Alice's server) → Alice's server now holds two connections: owner `alice`
  (authority) + guest `bob`. If the active world is **public**, Bob receives the snapshot (space
  geometry + world) and **registers locally** to the space geometry → co-located. If **private**, Bob
  gets an **info-colored message** and no world.
- **No platform anchors.** Both headsets register to the **same shared space geometry** (the reference
  constellation) via the registration vote; each solves its own `_Tmat` into the same reference frame,
  so content lands at the same physical spot for both. The only requirement is the guest detects enough
  of the same surfaces to register (≥4 inliers / 40%).
- **Presence.** Each connected client broadcasts its headset pose; the server relays; each client
  renders the *other* users as an avatar:
  > a **vertical box** (square footprint, side = 2·R) standing on the floor (base at y=0) + a **sphere**
  > of radius **R** centered at the headset position. The box top sits a few cm **below** the sphere, so
  > the head "floats" just above the body. R and the gap are tunable defaults (≈ R=0.13 m, gap=0.03 m).
- **Robustness work (the real cost).** The guest's headset sees a *different* plane set (missing/extra),
  so the matcher must register on partial overlap. Likely: looser size tolerances, more candidate yaws,
  graceful behavior when inliers are marginal. This is the one genuinely hard engineering piece.

## 9. Migration (existing data → user `daniel`)

One-time, idempotent:
- **Assets:** catalog `scope` `private/builder` → `daniel/agents/builder`; set `public = true`.
- **Worlds:** `.cache/worlds/private/builder/<name>.json` → `.cache/worlds/daniel/agents/builder/<name>.json`.
- **Spaces from worlds:** extract the embedded real surfaces (e.g. `new-room`'s 45) into a new
  **space** owned by `daniel` (e.g. `/daniel/spaces/home`); point the migrated worlds at it; strip the
  geometry out of the per-world docs (it now lives in the space). Sparse captures (`default`'s 2) merge
  into the same space or are dropped in favor of the fuller capture.
- Geolocation backfilled on the next owner capture (no stored location for legacy spaces).

## 10. Phases

1. **Users + namespace + `public` flag + migration.** `--user` (CLI/voice), `/tunnel/<user>` (web),
   per-connection user identity, scope → `/<user>/agents/<agent>`, `public` column/flag, migrate to
   `daniel`. Re-addressing only — no new behavior. *Touches:* cli, server, mcp launch, library, world
   repo, conftest, migration.
2. **Spaces first-class** (the shared room layer). Space store; `ingest_room` → active space; authority =
   owner; worlds reference a space (+ `<void>`); compose/decompose. *Touches:* server, world/space
   stores, persistence; migration creates `daniel`'s first space.
3. **Geolocation + nearest-space-on-load.** Capture on creation; nearest + geometry-confirm on start.
   *Touches:* client (geolocation), server (space matching).
4. **Co-location.** Guest join (public-only, else info message); guest registers to served geometry;
   presence avatars; matcher robustness. *Touches:* server (multi-connection users, relay), client
   (presence render, robustness).
5. **Visibility polish.** ✅ DONE. Default-public + inheritance (new assets inherit the active world's
   visibility, first-insert only) + "make a private world" (set_world_visibility / new_world public=…) +
   per-asset toggle (update_asset public=…) + the **public-uses-public guard**: a public world may
   reference only public assets, enforced by auto-publishing the owner's private assets when they're
   placed into a public world OR when a world is made public (the director relays the notice). Reachable
   only for the owner's own assets (another user's private asset can't be read to place), so no privacy
   leak. *Touched:* server (`_ensure_referenced_public` / `_publish_world_assets`), MCP tools, prompt.

## 11. Open questions / risks

- **Matcher robustness** for the guest's partial/extra planes is the main technical risk (Phase 4).
- **Guest agency:** initially a guest *inhabits* (renders + presence); the owner's director edits. Can a
  guest run their own director on the shared world? Deferred.
- **Geolocation** is coarse — fine for distant spaces, useless for adjacent rooms; the registration vote
  is the fine discriminator. (Already accounted for in §7.)
- **Public→private reference** handling (forbid vs. warn vs. auto-publish) — default forbid/warn.
- **Server multi-tenancy** beyond one active space + a few connections is out of scope for now.
