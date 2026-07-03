# New-space initialization — the "new person, new place" flow

**Status:** DESIGN. Fixes a confirmed gap in `spaces-and-users-plan.md §5/§7`: when a *new user* arrives
at a *new physical location*, the space that gets created is the wrong one — anonymous, un-located, via a
fallback path — and the user was rendering/capturing against someone else's room the whole time before that.

## 1. What actually happened (grounded in the Harold's-house session)

daniel's laptop was carried to Harold's house; `--user harold` (new user) logged in. Traced from the data:

| Thread | Active world | `active_space` → file | Notes |
|---|---|---|---|
| 1 boot | `daniel/new-room` | `home` → **daniel/home** (45 surfaces) | booted daniel's world; harold not active |
| 2 guest | `daniel/default` | **daniel/home** | harold's headset registered Harold's house against **daniel's** 45-surface room → no lock |
| 3 create | `harold/harolds-world` | `home` → **harold/home ← created** | seeded EMPTY by `_activate`, owner=harold, **geo ✗** |
| 4 capture | `harold/harolds-world` | **harold/home** | 3 surfaces landed |

A space *was* created (`harold/home`), but through the **wrong path** and with **no geolocation** — so a
return visit can't match it by GPS and would mint `space-2`, `space-3`, … (daniel already has a stray
`space-2`). The geolocation flow **never ran for harold**.

## 2. How it works today (the mechanics + the gaps)

**Two disjoint space-creation paths that don't agree:**
- **Path A — geolocation** (`/geolocation`): on the first report, case (3) "somewhere new" mints
  `_unique_space_name` (`space-N`), owner = the **active** user, **geo-stamped**, + a fresh world. Gated:
  `user = active_scope`'s user; a report where `req.user != user` is **ignored** (`server.py`).
- **Path B — world creation** (`_activate`): a new user's world with no space seeds a space literally named
  **`home`**, owner = user, **no geolocation**, no `space-N`.

**Root gaps:**
1. **"Logging in" doesn't set the active user.** The single global active world/scope is flipped by world
   *switches* (via the director), not by who connects a headset/director. At boot it's daniel (or the
   global-active pointer). So harold's geolocation is dropped (thread 2) because daniel is active.
2. **Geolocation is scoped to the active user, once per session.** `_nearest_space(user, …)` searches only
   the requesting user's spaces; and it's gated to the active user — so a *new* user's report does nothing.
3. **Path B silently wins** for a new user, producing an anonymous, un-located `home` instead of a
   geo-stamped, discoverable space.
4. **`docSurfaces` never clears** (`applySnapshot`: `if (reals.length) docSurfaces = reals`) — switching
   from a room world (many reals) to an empty world keeps the *previous* room's surfaces, so a capture can
   seed its reference from the wrong room. A real defect, independent of the flow.

## 3. Design decisions (the forks — with recommendations)

- **D1 — What determines the active space? → PHYSICAL LOCATION (geolocation), not who logged in.** The
  server is at one physical place (the laptop). Everyone connected is co-located there. Geolocation picks
  the one space for that place. *(Recommend.)*
- **D2 — Whom does geolocation act for? → the CONNECTING AR user (`req.user`), not the pre-booted active
  user.** Only AR/`enter-vr` reports geolocation (desktop guests never do), so a report means "this user is
  physically at the laptop." Let it establish *their* space. *(Recommend; supersedes the active-user gate.)*
- **D3 — Search scope for "am I here already?" → across ALL users, not just the requester.** So a second
  person at the same place joins the existing space (co-location) instead of minting a duplicate. Create a
  new space *only* when no user's space is nearby. *(Recommend.)*
- **D4 — Ownership of a newly-minted space → the user whose report created it** (first present). Correct by
  construction; matches "edit-rights follow ownership." *(Recommend.)*
- **D5 — Unify creation on the geo path.** World creation must NOT silently seed an anonymous `home`. A new
  user's first world should adopt the geolocation-selected space (creating a geo-stamped one if none). The
  `_activate` `home` fallback becomes a last resort only when there's genuinely no location. *(Recommend.)*
- **D6 — Boot behavior → provisional, then geolocation re-anchors.** Boot into last-active (or a neutral
  holding state) as a placeholder; the first geolocation report switches to the correct space for wherever
  the laptop physically is. This makes the `sparse-room-relock` global-active pointer unnecessary as a
  primary mechanism (keep at most as a provisional pre-geolocation placeholder, or drop it). *(Recommend.)*
- **D7 — Guest yank guard.** Because only AR reports geolocation and everyone in AR is at the laptop, a
  *remote desktop* guest can't yank the active space (they never report). Keep desktop out of the geo path.

## 4. Intended flow

**New person, new place** (harold, Harold's house):
1. Server running (booted into *some* provisional world). `harold` logs in (director `--user harold` +
   headset `?user=harold`), enters AR.
2. Headset reports geolocation. No space of **any** user is within ~150 m → **create a geo-stamped space
   owned by harold** + a fresh `harold` world in it, and make it **active**.
3. harold's headset captures the real room into *his* space (he's now the owner of the active world).

**Returning to a known place** (harold, back at Harold's house next week):
1. Geolocation matches harold's existing space → **activate its last-active world** (Path A case 2, already
   implemented) → harold picks up where he left off; the stored geometry is the registration reference.

**Co-location** (daniel + harold, same room):
1. First AR report mints/activates the space (owned by whoever reported first).
2. The second person's report is within range of that space → **join it** (activate the same world), not a
   duplicate. They co-locate; owner-only-writes decides who can edit.

## 5. Implementation plan (incremental, testable)

1. **`docSurfaces` staleness fix** (client): clear it when a snapshot has no real surfaces, so a capture in
   a fresh/empty world never seeds from the previous room. *Isolated defect; do first.*
2. **Geolocation for the connecting user** (server `/geolocation`): drop the "ignore non-active-user"
   gate for AR reports; search **across all users' spaces** for a nearby one (D3); on a hit for another
   user's space, activate it (join); on no hit, mint a **geo-stamped space owned by `req.user`** + a fresh
   world and switch into it (D2/D4).
3. **Unify world-creation with the space** (server `_activate` / `new_world`): a new user's first world
   adopts the active geo-stamped space instead of seeding anonymous `home` (D5). `_unique_space_name`
   becomes the single naming path.
4. **Boot** (server `_boot_world`): provisional resume only; rely on geolocation to re-anchor (D6). Decide
   whether to keep or drop the global-active pointer.
5. **Migration/cleanup**: geo-stamp or retire the existing anonymous spaces (`harold/home`,
   `daniel/space-2`) — a one-off, or absorb on next geolocation.

## 6. Rolled back / held in reserve

The `sparse-room-relock` branch (3 commits, unmerged) is **rolled back** — capture was fine before Harold's
house, so those were speculative. **Preserved for cherry-pick:**
- `a705271` **establish-while-sparse** — a genuine capture-robustness fix (a partial first capture can
  freeze the reference). Re-apply *only if*, after the flow is correct, a sparse first capture still
  freezes at Harold's house.
- `4fdd860` register-acceptance `min(MIN_COV, ref.length)` — minor; re-evaluate.
- `1b7fb23` global-active resume — likely superseded by D6; drop unless kept as a provisional placeholder.

## 7. Open questions / risks

- **Cross-user nearest-space search (D3):** searching every user's spaces on each boot is a filesystem walk
  — fine at small scale (same note as the world-index backlog); index later if needed.
- **Whose world activates when joining another user's space?** The space's `last_world`/`last_scope` points
  at a world+owner; the joiner may be a guest there. Confirm that's the desired default vs. the joiner's own
  world in that space.
- **Server-at-one-location assumption:** the model assumes the laptop is the single physical anchor. A
  future remote/multi-location deployment breaks D1/D2 and needs revisiting.
- **`_geo_selected` once-per-session:** with geolocation now able to *create* the active space, confirm the
  once-per-session guard still prevents mid-session GPS jitter from re-selecting.
