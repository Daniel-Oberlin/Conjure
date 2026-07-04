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
- **Path B — world creation** (`_activate`): its real job is **legacy migration** (extracting geometry
  embedded in an old world doc into a space). But its *fallback* seeds an anonymous **`home`** space (no
  geolocation) whenever a new world has no space — which is what wrongly won for harold.

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

**First-connection edge:** a non-AR first connection can set the coarse **location** but can't establish a
capturable **space** (surfaces come only from an AR capture). So "first user establishes the active space"
means *first AR user*; a non-AR first user builds in a void/virtual context until a headset fills the room.

## 5. Design decisions

- **D1 — The active space is set by the first connecting user's location; the server has none.** An empty
  (unclaimed) server can be claimed from anywhere; boot is provisional.
- **D2 — Two-stage space selection: geo filter → surface match.** Geolocation narrows to nearby candidates;
  the registration vote (`RoomSnap.register` against each candidate's stored geometry) picks the exact one.
  New work: candidate-set registration ("which of these near me do my walls match?").
- **D3 — Spaces are shared; space-owner ≠ world-owner.** `environment.space` becomes a **fully-qualified
  reference** (`<space-owner>/<space-name>`), so any user's world can be tied to any space. A space is owned
  by its first capturer; worlds by their creators. "In someone else's space, build your own world" works.
- **D4 — Admission is tiered (§4).** AR = geo + surface required; voice/CLI/desktop admitted without
  co-location. All edits remain owner-only.
- **D5 — Unify creation on the space.** World-creation adopts the **active, geo+surface-selected** space
  instead of seeding an anonymous `home`. Path B's empty fallback is reserved for genuinely-no-location
  (desktop-before-capture) worlds, which **re-home** to the real space once an AR capture establishes it.
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

1. **`docSurfaces` staleness fix** (client) — clear it when a snapshot has no real surfaces. Isolated
   defect; do first.
2. **Fully-qualified space references** (server) — `environment.space` = `<owner>/<name>`; `_activate`/
   `_compose`/`_save_active`/`SpaceStore` resolve a space by its own owner, not the world owner (D3). Keep
   backward-compat for existing bare-name refs (assume world-owner's scope).
3. **Two-stage space selection** — a server helper: given the connecting user's location + the client's
   detected surfaces, geo-filter candidate spaces **across all users**, then pick the best **registration**
   match; activate its last world, or mint a new geo-stamped space (D2/D7). Needs the client to send its
   detected surfaces to the server for the match (or the server broadcasts candidates for the client to
   vote — decide in §9).
4. **Admission gate** (server `/ws` + `/geolocation`) — AR joiners must match the active space (geo +
   surface); voice/CLI/desktop admitted without it (D4). Refused AR joiners get an info message.
5. **Unify world-creation with the active space** — new worlds adopt it; retire the anonymous-`home`
   fallback except for no-location worlds, which re-home on capture (D5).
6. **Boot & lifecycle** — provisional boot; establish/unlock the active space on first-connect / last-leave
   (D1/D6). Supersede the `sparse-room-relock` global-active pointer.
7. **Migration/cleanup** — geo-stamp or retire existing anonymous spaces (`harold/home`, `daniel/space-2`).

## 8. Rolled back / held in reserve

The `sparse-room-relock` branch (unmerged) is rolled back — capture was fine before Harold's house.
**Preserved for cherry-pick:** `a705271` **establish-while-sparse** (real capture-robustness; re-apply only
if a sparse first capture still freezes *after* the flow is correct). `4fdd860` register-`min` (minor) and
`1b7fb23` global-active (superseded by D1/D6) likely dropped.

## 9. Open questions / risks

- **Where does the surface match run?** Client-side (server sends candidate geometries, client votes with
  `RoomSnap.register`) vs. server-side (client sends detected planes, server runs the vote). Client-side
  reuses existing code but needs the server to ship candidate constellations; server-side centralizes but
  duplicates the matcher in Python. *Lean client-side.*
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
