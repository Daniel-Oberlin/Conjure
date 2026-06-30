# Co-location — Phase 4 design

**Status:** DESIGN (Phase 4 of `docs/spaces-and-users-plan.md` §8). Two users inhabit the same space.
Builds on spaces (Phase 2) + the registration vote (`room-model.md` §8a/§8b). Also formalizes the one
remaining loose end: **room authority = the space owner**.

## 1. What it delivers

- A **second user joins** a public world owned by another user and is **co-located** — they see the same
  content in the same physical spot — via **shared-geometry registration**, no platform "Shared Spaces".
- **Presence avatars** so they can see each other (the sphere-on-box from spaces-plan §8).
- A **desktop-browser guest mode** (no AR) for *testing without a second headset*: the guest is dropped
  in **to the right of the owner** and can move around with mouse/keyboard.

## 2. Per-connection user identity

Today `/ws` connections are anonymous. Phase 4 tags each connection with a **user**:
- The web client's user comes from the tunnel (`/tunnel/<user>` → `?user=<user>`); it passes it on the
  socket: `…/ws?user=<user>` (default user if absent).
- The server records `user` per `WebSocket` in `clients`.
- The **owner** = the active space's owner (`active_scope`'s user). A connection whose `user == owner`
  is the **owner**; anyone else is a **guest**.

(The CLI/voice *director* also has a user, but co-location is about the rendering clients on `/ws`.)

## 3. Guest join + the public gate

**Worlds gain a `public` flag** (default `true`, per spaces-plan §4), stored under `environment.public`
(like `space` — the `World` model ignores unknown top-level keys, so environment is the safe home).

**Why that's fine for querying, and how queries work.** After Phase 2 a world doc is *tiny* — placed
objects + style overrides + prefs + the `space`/`public` refs (the 45-surface geometry moved to the
space). So reading docs is cheap, and the two queries you'd want are simple:
- **"my worlds"** → `worlds.list(<my-scope>)` — a directory listing of `.cache/worlds/<my-scope>/`. *(done)*
- **"worlds available to me"** → *my* worlds ∪ *other users'* **public** worlds. **Implemented** as
  `WorldRepository.list_public()` (walk `<root>/*/agents/*`, read each doc, keep `environment.public` ≠
  false), surfaced on `/worlds/list` as `available`, tagged by owner. `switch_world(name, owner=…)` then
  enters another user's public world (everyone comes along; you can inhabit but not edit it). Cheap at
  small scale; a derived index replaces the walk when it grows (backlog).

- On `/ws` connect:
  - **owner** → send the snapshot as today.
  - **guest + active world public** → send the snapshot (they join).
  - **guest + active world private** → send an **info message** (rendered in the info color: "'<world>'
    is private — ask <owner> to make it public"), and **no** world. Keep the socket open so the owner
    making it public can push a snapshot later.

## 4. Authority = space owner, and **edit-rights follow ownership** (folds in loose-end #1)

Edit-rights follow **world ownership**: only the **active world's owner** (`active_scope`'s user) may
change that world's scene content; everyone else is read-only *on it*. But **world navigation is open to
all** — anyone may create or switch worlds and everyone present comes along — because a created/
switched-into world lives in the *caller's own scope*, so the caller becomes its owner and only *then*
can edit it. Net: a guest can spin up and build **their own** worlds with everyone present, while another
user's curated world stays protected. **Enforced server-side** — *no prompt changes* (it's a capability
boundary; the director is merely *told its username* so it can answer "who am I?" and relay a refusal
honestly instead of inventing a name collision). A consent/permission model to relax further — letting a
guest co-edit *someone else's* world — is a later tightening.

- **Geometry capture.** `ingest_room` accepts a `/room` post **only from the active world's owner**. A
  guest's capture is rejected — guests **register against** the geometry, never re-capture it. Supersedes
  the per-client-id authority (the client-id becomes a within-owner continuity detail).
- **Scene mutations (owner-gated).** Every scene-changing endpoint (`/patch`, `/place_asset`,
  `/place_image`, `/style_surface`, `/show_surface`, `/texture_surface`, `/edit_image`, `/outpaint_image`,
  `/set_skybox`, `/set_grounded_skybox`, `/reset`, `/room`) requires the requester to be the **active
  world's owner**.
- **World navigation (open).** `/worlds/new` and `/worlds/switch` are **not** gated — anyone creates/
  switches (in their own scope) and everyone comes along. Read endpoints (`/world`, `/worlds/list`,
  `/geolocation`, `/client_log`, presence) are open to guests.
- **How identity reaches the API (no prompt, no per-tool churn).** The MCP client `_post` attaches the
  caller's user (from `CONJURE_SCOPE`) as a header (`X-Conjure-User`) on *every* request; a FastAPI
  middleware on the **scene-mutation** routes checks `header-user == active-world-owner` and returns
  **403** otherwise (a guest *director* runs with `--user <guest>`; the headset/browser attaches the
  header on `/room`). **Interim policy:** a *missing* header (the direct dev `conjure` CLI) is treated as
  the owner — convenience now; tighten to "require it" once we want strict deny-by-default.
- The director is **told its username** (system prompt) so it answers "who am I logged in as?" and, when
  a scene edit is refused, relays the refusal plainly instead of inventing a cause (e.g. a name
  collision). The CLI prompt shows it too: `conjure:<user>.<agent>.<llm>>`.
- This landed with the authority work (build step 4); the world-navigation relaxation (open create/
  switch) followed once a guest director was real and we chose capability over strict lock-down.

## 5. Co-location for an AR guest (no platform anchors)

The shared world/space lives in **one reference frame** (the owner's, anchored to their physical room).
A guest's AR headset **registers its own detected planes onto the same persistent space geometry** (the
Hough/RANSAC vote, `room-model.md` §8a) → solves its *own* `_Tmat` into that shared frame → content
lands at the same physical spot for both. The only requirement: the guest detects **enough of the same
surfaces** (≥4 inliers / 40%). The **matcher robustness for partial/extra planes is the real work**
(§8).

## 6. Desktop-guest test mode (no AR) — the new requirement

A guest in a *desktop browser* has no XR session, no tracking, no plane detection — so it can't register.
Instead it joins as a **virtual occupant** in the world's reference frame directly:

- Detect "not immersive AR" → **desktop-guest mode**.
- Render the world in the reference frame **directly** (`#world-root` at identity — no registration; the
  space geometry + placed objects are at their reference-frame coordinates).
- **Spawn to the right of the owner:** take the owner's latest presence pose `P`, compute the owner's
  local **right** vector, place the guest camera at `P.position + right · offset` (≈1.2 m) at standing
  height, facing the owner's direction. (If the owner hasn't broadcast a pose yet, spawn at a sensible
  default and stay put once placed.)
- Enable **desktop navigation** (A-Frame `look-controls` + `wasd-controls`) so the guest can move and
  look around.
- Broadcast the guest camera's pose as presence.

Result: owner (AR) and guest (desktop) share the space frame; **each sees the other's avatar; both move
freely.** A complete co-location loop with one headset.

## 7. Presence avatars

- A new `/ws` message — **presence**: each client sends its head/camera pose **in the reference frame**
  at ~10 Hz; the server **relays** it to the other clients tagged by user. On disconnect, a `presence`
  with the user removed.
- Frame: the **AR** client transforms its headset pose by `_Tmat` into the reference frame; the
  **desktop** client's camera is already in the reference frame.
- Each client renders the *other* users as the **avatar**: a vertical box (square footprint, side 2·R)
  on the floor + a sphere radius R at the head, sphere floating a few cm above the box (R≈0.13, gap≈0.03;
  tunable), optionally labelled with the username.

## 8. Matcher robustness (the hard part)

A guest's headset sees a *different* plane set (missing/extra) than the owner captured. The register vote
must lock on partial overlap: looser size tolerances, more candidate yaws, graceful behavior at marginal
inliers, and not polluting the reference with a guest's stray planes. This is the one piece that needs
real on-device iteration; everything else is testable in the browser first.

## 8a. Cross-scope public assets (shared catalog on one server) — DONE (step 6)

Scenario: you bring your **laptop** to a friend's; the friend logs in as a *separate user*
(`--user friend`), creates his own space + world, and wants to use **your public assets**. This is the
intended use of the asset `public` flag — and on one machine it's mostly already there:

- **Bytes are shared.** The cache is content-addressed and global on disk (`persistence-model.md` §2):
  `/assets/<hash>` serves any hash regardless of scope. The friend's world referencing your asset by
  hash already resolves — same laptop.
- **The gap is catalog *visibility*.** The library *reads* (`find` / `search_library` / `query_assets`)
  are scoped to the caller's `<user>/agents/<agent>`, so the friend can't *discover* your public assets.

**The feature (shipped):** reads now return **caller's scope ∪ `public=1`** (referenced in place;
copy-to-private to pin). `library.find/search/vector_search/query` all take a `scope` and apply the
`(scope=? OR public=1)` predicate; `/library/search` carries `scope`, and `search_library` sends `SCOPE`
like the maintenance tools — so it's no longer unscoped (fixes the old search-vs-query inconsistency).
`place_cached_asset` resolves by hash and bytes are global, so a referenced public asset already places.
Writes stay own-scope only; no prompt changes. **Not** cross-machine federation (a separate future item).

## 9. What changes

**Server:** per-connection `user` on `/ws`; world `public` flag + the join gate; `ingest_room` authority
= space owner; a **presence relay** (receive a pose, fan out to the others).
**Client:** pass `user` on the socket; **presence** (broadcast own pose in the reference frame, render
other-user avatars); **desktop-guest mode** (detect no-XR, spawn right-of-owner, desktop nav).

## 10. Open questions / risks

- **Matcher robustness** (AR co-location) — the main risk; deferred to last so the rest lands first.
- **Edit-rights follow ownership** (§4): a guest's director can create/switch/build **its own** worlds
  (everyone comes along) but can't edit another user's world. Letting a guest **co-edit someone else's**
  world (a consent/permission handshake) is the later tightening.
- **`public` on worlds** — DECIDED: kept in `environment.public` (no schema churn, survives validation
  like `space`; default true). Toggleable from the director via `set_world_visibility(public=…)` and at
  creation via `new_world(name, public=False)`; it drives both `list_public` discovery and the `/ws`
  join gate. Scope-bound (you only change your own worlds), so it's not middleware-gated on the active
  owner.
- **Multiple guests** — the relay supports N; we test with 1 first.
- **Desktop-guest spawn** — offset/facing are tunables.

## 11. Build plan (front-load the browser-testable pieces)

1. ✅ **Per-connection user on `/ws` + world `public` flag + the join gate** (public → snapshot; private →
   info message). Server-testable.
2. ✅ **Presence** — broadcast + relay + avatar render. Testable with **two browser tabs**.
3. ✅ **Desktop-guest mode** — spawn-right-of-owner + desktop nav. Testable: owner tab + guest tab (the
   thing you asked for).
4. ✅ **Authority = world owner; edit-rights follow ownership** — scene-mutation 403 + `ingest_room` gate;
   world create/switch open to all (everyone comes along); director knows its username (§4).
5. **AR co-location + matcher robustness** — on-device, the hard part, last. ◻️ remaining
6. ✅ **Cross-scope public asset reads** (§8a) — done. Reads = own scope ∪ public; a friend on your
   laptop builds with your public assets.

Steps 1–3 give a fully working, **browser-only** co-location demo (two tabs, presence, move around)
before any headset work — and they're the foundation the AR path (4–5) sits on.
