# New-space initialization — the "new person, new place" flow

**Status:** DESIGN. Reworks how a physical **space** is claimed, selected, shared, and joined — fixing a
confirmed gap in `spaces-and-users-plan.md §5/§7`: a new user at a new location gets an anonymous,
un-located space via a fallback path, after rendering/capturing against someone else's room.

## 1. The model (target)

- **The server has no location.** It's a shared brain any user connects to. The **active space** is
  established by the **first user to connect** to an unclaimed (empty) server, from *their* reported
  location (browser or headset).
- **A space is a shared physical resource, not a per-user thing.** It's owned by whoever first captured it,
  but **any user can build their own worlds in it.** Space ownership (first capturer) is **decoupled** from
  world ownership (world creator).
- **Spaces are public by default; can be made private.** A *public* space lets any admitted (co-located)
  user create their **own** worlds in it; a *private* space restricts world-creation to the **owner**.
  Making a space private is **not retroactive** — existing worlds stay; it only blocks *new* ones by others.
- **The physical space is established by the first *AR* user** (they alone have both a location and captured
  surfaces). Voice/CLI/desktop connections have no surfaces, so they don't establish a space — they operate
  on whatever's active, or a void world.
- **Selecting/identifying a space is two-stage:** **(1) geolocation** filters to nearby candidates (coarse,
  ~hundreds of feet — excludes far-away spaces); **(2) surface/registration match** picks the *exact* space
  among the geo-near ones. Geolocation alone can't tell apart two rooms at the same address.
- **Admission is tiered by how you connect** (§4): only an **AR headset** (real presence) must be
  physically in the space; control (voice/CLI) and virtual (desktop) connections aren't.
- **A space unlocks when everyone leaves.** While occupied, joiners must match the active space; once the
  server is unclaimed again, the next user can establish a *different* active space from wherever they are.

## 2. What actually happened (grounded in the Harold's-house session)

daniel's laptop was carried to Harold's house; `--user harold` (new user) logged in. Traced from the data:

| Thread | Active world | `active_space` → file | Notes |
|---|---|---|---|
| 1 boot | `daniel/new-room` | `home` → **daniel/home** (45 surfaces) | booted daniel's world; harold not active |
| 2 guest | `daniel/default` | **daniel/home** | harold's headset registered Harold's house against **daniel's** 45-surface room → no lock |
| 3 create | `harold/harolds-world` | `home` → **harold/home ← created** | seeded EMPTY by `_activate`, owner=harold, **geo ✗** |
| 4 capture | `harold/harolds-world` | **harold/home** | 3 surfaces landed |

A space *was* created (`harold/home`) but via the **wrong path**, with **no geolocation** — so a return
visit can't match it by GPS and would mint `space-2`, `space-3`, … The geolocation flow **never ran** for
harold, because he wasn't the *active* user when his headset reported.

## 3. How it works today (mechanics + the gaps)

**Two disjoint space-creation paths:**
- **Path A — geolocation** (`/geolocation`): first report, case (3) "somewhere new" mints
  `_unique_space_name` (`space-N`), owner = the **active** user, **geo-stamped**, + a fresh world. Gated to
  the active user; a report where `req.user != active_user` is **ignored**. Selection is **GPS-only** (no
  surface disambiguation). Runs once per session.
- **Path B — world creation** (`_activate`): its real job *was* **legacy migration** (extracting geometry
  embedded in an old world doc into a space) — now **dead code** (nothing left to port; safe to delete). Its
  *fallback* seeds an anonymous **`home`** space (no geolocation) whenever a new world has no space — which
  is what wrongly won for harold. **Path B is deprecated:** the legacy-migration machinery is removed; the
  empty-`home` fallback is *replaced* by the geo-driven creation (§7), not kept.

**Root gaps:**
1. **"Logging in" doesn't set the active user** — the one global active world is flipped by world
   *switches*, not by who connects. So a new user's geolocation is dropped.
2. **Geolocation is GPS-only and single-user-scoped** — `_nearest_space(user,…)` searches only the
   requester's spaces; no surface-match disambiguation of co-located rooms.
3. **Worlds can only reference a space in their *own owner's* scope** — `_activate` does
   `spaces.load(world_owner, space_name)`. So you can't put your world in someone else's space.
4. **Path B's empty-`home` fallback wins** for a new user → anonymous, un-located space.
5. **`docSurfaces` never clears** (`applySnapshot`: `if (reals.length) docSurfaces = reals`) → switching to
   an empty world keeps the previous room's surfaces, so a capture can seed from the wrong room. Isolated
   defect, independent of the flow.

## 4. Connection tiers & admission

"Admission to the active world" depends on whether the connection claims **physical presence** in a real room:

| Connection | Presence | Aligns to a real room? | Co-location gate (geo + surface) |
|---|---|---|---|
| **AR headset** (immersive-ar, planes, passthrough) | physical | yes | **Required** — refuse if not in the space |
| **Voice / CLI** (director over HTTP, no render) | none (control) | no | **N/A** — admit; edits still owner-gated |
| **Desktop VR** (browser, no AR, wasd/look) | virtual | no (world-root = identity) | **N/A** — admit; virtual avatar near owner |

So the co-location gate governs **only AR headsets** — the ones that render aligned to passthrough and would
show content floating over the wrong room if mismatched. A voice/CLI user at the house and a desktop user
across the country can both drive/inhabit the active world; only a *headset* must physically be there.

**First-connection edge:** only an **AR** capture yields surfaces, so "first user establishes the active
space" means the *first AR user*. A voice/CLI/desktop user who connects first has no surfaces — they build
in a void/virtual context until a headset establishes the real space. (We already report geolocation only
on `enter-vr`, so a desktop browser doesn't try to establish a space on load.)

## 5. Design decisions

- **D1 — The active space is set by the first *AR* user's location + capture; the server has none.** An
  empty (unclaimed) server can be claimed from anywhere. **Boot is provisional:** the startup world is a
  placeholder that doesn't fix the active space — the first connection (re)establishes it, so `_boot_world`'s
  global-active pointer becomes unnecessary.
- **D2 — Two-stage space selection: geo filter → surface match.** Geolocation narrows to nearby candidates;
  the registration vote (`RoomSnap.register` against each candidate's stored geometry) picks the exact one.
  New work: candidate-set registration ("which of these near me do my walls match?").
- **D3 — Spaces are shared; space-owner ≠ world-owner.** `environment.space` becomes a **fully-qualified
  reference** (`<space-owner>/<space-name>`), so any user's world can be tied to any space. A space is owned
  by its first capturer; worlds by their creators. "In someone else's space, build your own world" works.
- **D4 — Admission is tiered (§4).** AR = geo + surface required; voice/CLI/desktop admitted without
  co-location. All edits remain owner-only.
- **D5 — Unify creation on the space; drop Path B.** A new world **adopts the active, geo+surface-selected
  space**. If there's **no active space yet** (no AR user has established one), the world is created **void**
  (`space="<void>"`) — the honest "no room yet" answer — instead of the anonymous `home`. The legacy-
  migration machinery is deleted. A void world can later be **re-homed** to a real space once one exists.
- **D8 — Space visibility (public by default).** A *public* space lets any admitted user create their own
  worlds in it; a *private* space restricts world-creation to the owner. Switching to private only affects
  **future** world-creation — existing worlds in the space are untouched. Orthogonal to admission (joining/
  viewing is governed by co-location + the *world's* visibility). `set_space_visibility` mirrors the
  world/asset toggles.
- **D6 — Space stays locked while occupied; unlocks when empty.** While any AR user holds the space, joiners
  must match it; when the last user leaves, the next connection may establish a new active space.
- **D7 — Geolocation acts for the *connecting* AR user, not the pre-booted active user.** Drop the
  active-user gate; search candidate spaces **across all users**; join an existing one on a surface match,
  else mint a geo-stamped space owned by the connecting user.

## 6. Intended flows

- **New person, new place** (harold, first time at Harold's house, empty server): harold connects in AR →
  no space within geo range → **mint a geo-stamped space owned by harold** + a fresh world, activate it →
  harold captures the room into *his* space.
- **Return to a known place:** geo narrows to harold's space(s) here → surface match confirms which →
  activate its last-active world.
- **Two rooms, one address:** geo returns both; **surface match** disambiguates → the right room activates.
- **Build in someone else's space:** daniel established this space; harold connects, is admitted (geo +
  surface match), then **creates his own world tied to daniel's space** (fully-qualified ref, D3).
- **Co-location:** second AR user passes geo + surface → **joins** the active world (no duplicate space).
  Voice/CLI/desktop users join as control/virtual without the gate.
- **Re-claim when empty:** everyone logs out → server unclaimed → next user establishes a new active space
  from their location.

## 7. Implementation plan (incremental, testable)

0. **✅ DONE — Delete the legacy-migration machinery** (`_space_from_world_doc` extraction + `_decompose`-
   on-load in `_activate`) — dead code, nothing left to port. `_activate` is now **read-only** (resolve +
   compose, no world-doc rewrite); persistence of a freshly-built world moved to `_switch_to`, where it
   belongs. The `absent → home` Path B fallback is kept as a labelled bridge (removed in step 5).
1. **✅ DONE — `docSurfaces` staleness fix** (client) — `applySnapshot` now clears `docSurfaces` when a
   snapshot carries no real surfaces, so switching into an empty/void/other room can't seed the next
   capture from the previous room's geometry. Isolated defect.

   > ⚠️ Steps 0–1 shipped covered by the unit/JS suites but **not yet headset-regression-tested** — verify
   > the "room shared across worlds / styling per-world" and "void→empty capture" paths on-device later.
2. **✅ DONE — Fully-qualified space references** (server) — `environment.space` = `<owner>/<name>` (D3).
   New `_resolve_space_ref` / `_space_ref` helpers; a new global `active_space_owner` pairs with
   `active_space` to identify the live space's file. `_activate` returns `(owner, name, store)` and loads
   the space from its OWNER's scope; `_save_active` persists captured geometry back to that owner's scope
   and writes the qualified ref; `/reset` + the admin `*active*`/delete guards resolve by owner too.
   **Back-compat:** a bare `<name>` ref resolves to the world-owner's scope, so existing docs still load;
   they're rewritten to the qualified form on the next save. Creation still can't *target* another user's
   space yet — that's step 5 (world-creation adopts the active space).
3. **✅ DONE — Two-stage space selection** (client-side match). Split into discovery + commit:
   `POST /geolocation` is now **read-only** — it returns every geo-near candidate space **across all users**
   (`_geo_candidates`, within `_GEO_RANGE_M`), each with its surface constellation. The client votes its
   live capture against them (`RoomSnap.selectSpace` → the `register` coverage vote, immune to the sparse-
   capture bug), then commits via `POST /space/select`: **matched** → join that space's last world (or mint
   a world in it, D3); **no match** → stamp the still-un-located active space, else mint a fresh geo-stamped
   `space-N` + world for the connecting user (D2/D7). Commits **once per session**. §9 resolved: matching
   runs **client-side**, reusing `register`. (`_nearest_space`/`_NEAR_M` removed — superseded by
   `_geo_candidates`.) The "connecting user establishes when first in / space unlocks when empty" lifecycle
   is still step 7; admission (who may commit) is step 4.
4. **Admission gate** (server `/ws` + `/geolocation`) — AR joiners must match the active space (geo +
   surface); voice/CLI/desktop admitted without it (D4). Refused AR joiners get an info message.
5. **World-creation adopts the active space, else void; Path B fallback removed** (D5) — no more anonymous
   `home`.
6. **Space visibility** (D8) — a `public` flag enforced at world-creation-in-a-space; `set_space_visibility`
   tool, mirroring the world/asset toggles.
7. **Boot & lifecycle** — provisional boot; establish/unlock the active space on first-connect / last-leave
   (D1/D6). Supersede the `sparse-room-relock` global-active pointer.
8. **Migration/cleanup** — geo-stamp or retire existing anonymous spaces (`harold/home`, `daniel/space-2`).

## 8. Rolled back / held in reserve

The `sparse-room-relock` branch (unmerged) is rolled back — capture was fine before Harold's house.
**Preserved for cherry-pick:** `a705271` **establish-while-sparse** (real capture-robustness; re-apply only
if a sparse first capture still freezes *after* the flow is correct). `4fdd860` register-`min` (minor) and
`1b7fb23` global-active (superseded by D1/D6) likely dropped.

## 9. Open questions / risks

- **Where does the surface match run?** ✅ RESOLVED (step 3): **client-side** — `/geolocation` ships the
  geo-near candidate constellations and the client votes with `RoomSnap.selectSpace` (reusing `register`),
  then commits the verdict via `/space/select`. Reuses the existing, tested matcher; no Python duplicate.
- **Cross-user candidate search** is a filesystem walk over every user's spaces — fine at small scale;
  index later (same note as the world-index backlog).
- **Fully-qualified space refs** touch persisted world docs — need a migration/back-compat path for the
  bare-name refs that exist today.
- **"Locked while occupied"** needs a definition of *occupied* (any AR client on `/ws`? presence within N
  seconds?) and what a mismatched AR joiner sees (info message + passthrough, like a private-world refusal).
- **Who owns a space captured by a guest** in another user's active world — the guest, or the world's space
  owner? (Ties to D3 — likely the capturer, but confirm.)
- **Server-at-one-brain, users-anywhere:** voice/desktop users can be remote while an AR user holds the
  space — confirm the tiers behave (e.g. a remote desktop user shouldn't be able to *re-home* the space).
