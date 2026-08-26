# Spaces — the physical-environment record, and who may enter one

**Living spec.** Describes what is built and how it behaves today. Unfinished work, future directions,
and known problems live in [`docs/backlogs/spaces.md`](../backlogs/spaces.md); rejected alternatives and
the reasoning behind consequential forks live in [`docs/decisions.md`](../decisions.md).

A **space** is a persistent record of a real physical environment — its surfaces, its boundary, its
approximate location, and who owns it. This spec covers what that record is, how a headset decides
*which* space it is standing in, who is admitted, and who may write. It does **not** cover the geometry
that decides where a wall *is* — that is [`docs/specs/spaces-geometry.md`](./spaces-geometry.md) — nor
how a world styles a space's surfaces, which is
[`docs/specs/worlds-surfaces.md`](./worlds-surfaces.md).

---

## 1. What a space is

Three things are separate and stay separate:

| Concept | Owned by | Holds | Lives at |
|---|---|---|---|
| **space** | its first capturer | real surfaces, boundary, geolocation, visibility, last-used world | `<user>/spaces/<name>` |
| **world** | its creator | placed entities, per-surface style overrides, display prefs, a ref to one space | `<user>/agents/<agent>/sessions/<id>/worlds/<name>` |
| **session** | its creator | transcript, state, worlds | see [`specs/agents.md §7`](./agents.md) |

A space is **user-owned and agent-agnostic**: the physical room does not belong to the builder or to the
outdoor agent, it belongs to the person who first captured it. Worlds, assets and state are
agent-owned *under* a user. Every one of them carries a `public` flag.

**Space ownership is decoupled from world ownership.** You may build your own world inside someone
else's space. That is the point of the fully-qualified space reference (§4).

A space usually contains **more than one room**. `detectedPlanes` reports the whole dwelling, so a
single space routinely holds several rooms joined by doors — the reference capture in
`tests/js/fixtures/golden-room.json` is 45 surfaces across two rooms. Nothing in the record models a
"room" as a unit; a space is a flat set of surfaces plus one boundary polygon. Code that reasons about
"the room" as a single convex volume is making an assumption the record does not support (see
[`backlogs/spaces.md`](../backlogs/spaces.md)).

### 1.1 Glossary

| Term | Meaning |
|---|---|
| **space** | the persistent physical-environment record (this document) |
| **`<void>`** | sentinel space for a world with no physical tie — a pure-sky or purely-virtual world |
| **active space** | the one space currently composed into the active world |
| **space owner** | the user who first captured it; the only user who may write its geometry |
| **authority** | the headset whose captures the server accepts — the active world's owner |
| **holder** | an AR client that passed the admission gate and declared `hold` — occupancy |
| **claim epoch** | the interval from first hold to last release; selection is idempotent within one |
| **seed** | the space's stored surface constellation, used for registration and server-side solves |
| **guest** | a connected user who is not the active world's owner |

---

## 2. The record

`SpaceStore` (`conjure/world.py:837`) persists one JSON document per space:

```jsonc
{
  "owner": "daniel",                  // first capturer; the write gate
  "name": "space-1",
  "public": true,                     // gates world-CREATION by others (§5)
  "geolocation": { "lat": …, "lon": … },   // coarse; may be null
  "surfaces": [ /* real-surface entities, geometry + DEFAULT material */ ],
  "boundary": { "floorPolygon": [[x,z], …], "height": 2.6 },
  "last_scope": "daniel/agents/builder/…",  // return-visit pointer
  "last_world": "bladerunner1"
}
```

- **Geometry and default materials only.** Per-world styling is not here; it lives in the world doc as
  `surfaceStyles` (§6).
- **`boundary` is one polygon and one height** for the whole space, regardless of how many rooms it
  contains. It is derived from the largest captured floor (`conjure-client.js:2195`, by `_area`), so in
  a multi-room space the smaller rooms are not represented in it.
- **`last_scope` / `last_world`** are the return-visit pointer: match this space again and you land back
  in the world you left. Rewritten by world renames (`world.py:1015`).

---

## 3. Namespace, scope, and ownership

```
/<user>/[agents/<agent>/]<category>/<name>

  /daniel/spaces/space-1                                  ← spaces are USER-owned
  /daniel/agents/builder/worlds/bladerunner1               ← worlds are AGENT-owned under a user
  /daniel/agents/builder/assets/<hash>.glb
```

- **User-first.** The owner is the top path segment.
- **Visibility is a flag, never a path segment.** `public: bool` lives on the item, so publishing is a
  metadata flip that relocates nothing and breaks no reference; content-addressed assets keep their
  hash. Access is a predicate, not path arithmetic.
- **Scope is a capability.** The `<user>/agents/<agent>` prefix is injected by the runtime, never an LLM
  argument. See [`specs/agents.md §2`](./agents.md) for the enforcement model — it is the same substrate
  for spaces, worlds, assets and state.

The root resolves to the user home (`config.DATA_DIR`, `~/.local/share/conjure/`), not the in-project
`.cache/`.

---

## 4. Worlds ↔ spaces

A world points at its space through `environment.space`:

```jsonc
"environment": {
  "space": "daniel/space-1",   // "<owner>/<name>" — fully qualified, so ANY user's world can use it
  "public": true,
  "sky":   { … },              // how this world presents the sky
  "spacePresentation": { … }   // how this world presents the space  → worlds-surfaces.md
}
```

- **Fully qualified** (`_space_ref`, `server.py:2715`) so a world can reference a space in another
  user's scope. A bare `<name>` still resolves, to the world owner's scope, for documents written
  before qualification (`_resolve_space_ref`, `server.py:2701`); it is rewritten to the qualified form
  on the next save.
- **`<void>`** marks a world with no physical tie. A void world drops any stray inline real surfaces and
  renders pure-virtual.
- The active world's owner and the active space's owner **may differ**, and routinely do.

### 4.1 Compose / decompose

The live in-memory document is always **fully composed**; only persistence splits.

**`_compose(world_doc, space)`** (`server.py:2627`) — on load:

1. Drop the persistence-only `space` ref and the `surfaceStyles` map from the live doc.
2. Take the space's surfaces as the real entities; overlay each one's
   `environment.spacePresentation.surfaceStyles[<id>]` onto its `material`.
3. Copy the space's `boundary` into `environment.spacePresentation.boundary`.
4. If the world inherited a non-empty space's geometry, default `spacePresentation.active = true` — a world that has
   real surfaces genuinely has something to work with, even with no live capture this session. An
   explicit `false` (an immersion mode) is respected.
5. Re-pin on-surface images to their (possibly moved) hosts.

**`_decompose(composed, space)`** (`server.py:2659`) — on save, the exact inverse: placed entities plus
the per-surface material deltas as `surfaceStyles`. Real-surface **geometry** and the boundary are the
space's job and are stripped from the world doc.

The consequence worth stating plainly: **the same physical room, styled two different ways in two
different worlds, is one space record and two `surfaceStyles` maps.** Switching worlds restyles the room
without recapturing it.

---

## 5. Visibility and access

- **Default public.** New items are `public: true`.
- **Visibility inherits the active world.** An asset or state doc created while a *private* world is
  active defaults private; while a *public* world is active, public.
- **Read** = `owner == caller OR public`. **Write** = `owner == caller`.
- **Public-uses-public invariant.** A public world may reference only public assets, so a visitor sees
  the whole scene. Placing your own private asset into a public world — or making a world public —
  auto-publishes the assets it uses, and the tool says so.

A **space's** `public` flag governs one specific thing: **who may create a world in it.**
`_may_create_world_in(user, owner, name)` (`server.py:2728`) allows it iff the caller owns the space or
the space is public. It is **not retroactive** — making a space private leaves existing worlds alone and
only blocks new ones by others. It is also orthogonal to admission: whether you can *enter* is governed
by co-location plus the *world's* visibility. Toggled by `POST /space/visibility` and the
`set_space_visibility` tool.

---

## 6. Establishing and selecting a space

**The server has no location.** It is a shared brain that anyone connects to. The active space is
established by the first AR user to connect to an unclaimed server, from *their* reported location.
Only an AR session yields surfaces, so only an AR user can establish; a voice, CLI or desktop
connection that arrives first operates on a void world until a headset establishes something real.

Selection is **two-stage**, because neither stage suffices alone:

**Stage 1 — geolocation prefilter.** `POST /geolocation` is read-only. It returns every candidate space
within `_GEO_RANGE_M` (300 m, `server.py:1617`) **across all users**, each with its surface
constellation (`_geo_candidates`, `server.py:1638`). Coarse — it separates home from office, and cannot
separate two rooms at one address.

**Stage 2 — surface vote, client-side.** The client votes its live capture against those candidate
constellations (`RoomSnap.selectSpace` → the coverage vote of §7 in
[`spaces-geometry.md`](./spaces-geometry.md)) and commits the verdict via `POST /space/select`. The
match runs client-side deliberately: it reuses the same tested matcher registration uses, rather than a
second Python implementation.

### 6.1 What `/space/select` does

`select_space` (`server.py:1722`) branches on whether the active space is already **claimed**:

**Unclaimed** — this AR user establishes:

| Vote | Outcome |
|---|---|
| matched an existing space, which has a last world | join that world (return visit) |
| matched, but it has no world yet | mint the connecting user a world tied to it |
| matched, private, no joinable world, not the owner | refused |
| no match | "somewhere new": mint a fresh geo-stamped `space-N` + world, owned by the connecting user |

A space is born **with** its location, at mint time.

**Claimed** — the admission gate:

| Vote | Outcome |
|---|---|
| matched the active space | **admitted** — co-location join, no world change |
| matched a different space, or no match | **refused** — nothing minted, nothing switched |

A refusal is an info message plus a **blanked world**: the client shows passthrough only, so content
never floats over the wrong room. The refused user never becomes a holder.

Selection is **idempotent per claim epoch**, keyed by a page-load `cid` in `_selected_cids`, so GPS
jitter cannot re-vote and thrash the choice.

### 6.2 Admission tiers

Admission depends on whether a connection claims *physical presence*:

| Connection | Presence | Aligned to a real room? | Gate |
|---|---|---|---|
| **AR headset** | physical | yes | **required** — geo + surface match, else refused |
| **Voice / CLI** | none (control) | no | admitted; edits still owner-gated |
| **Desktop** | virtual | no (world-root identity) | admitted; spawns near the owner |

The gate governs **only AR headsets** — the ones rendering against passthrough, where a mismatch would
put content over the wrong walls. `/space/select` is the only endpoint an AR client reaches for this, so
the other tiers are admitted by never running selection at all. A voice user in the room and a desktop
user on another continent can both inhabit the active world.

### 6.3 Occupancy — claim, hold, release

The active space is a claimable resource tracked by occupancy, not by a boot-time pointer:

- An AR client declares `hold` over `/ws` after a successful select, joining `_space_holders`
  (`server.py:655`).
- `_occupied()` ⇔ any holder is present.
- It holds until it `release`s (leaves AR) or its socket closes.
- When the **last** holder leaves, `_unclaim()` frees the space and resets `_selected_cids`, so the next
  connection may establish a different space from wherever it is.
- **Boot is provisional.** `_space_holders` starts empty (`server.py:359`), so the booted-active world
  is a placeholder and the first AR user establishes from their own location rather than being refused
  against a stale one.

Only AR clients hold. Desktop, voice and CLI never do.

---

## 7. Authority and edit rights

Two different gates, often confused:

**Geometry authority — the space owner.** `/room` is owner-only. A guest's capture is rejected; guests
*register against* the stored geometry and never re-author it (§8). Within one owner, an
`authorityClientId` records which headset is live, and an idle authority is taken over after `_AUTH_TTL`
so a reconnecting owner is not locked out (`server.py:2889`).

**Scene edit rights — the world owner.** Every scene-mutating endpoint requires the caller to be the
active world's owner. Enforced by middleware (`server.py:600`): the MCP client attaches
`X-Conjure-User` on every request, and a non-owner gets 403. A *missing* header is treated as the owner
— a convenience for the direct dev CLI, not a security posture (see
[`backlogs/spaces.md`](../backlogs/spaces.md)).

**World navigation is open to everyone.** `/worlds/new` and `/worlds/switch` are not gated: anyone may
create or switch, and everyone present comes along. This is coherent because a created or
switched-into world lives in the *caller's own* scope, so the caller becomes its owner and only then can
edit it. Net effect: a guest can build their own worlds with everyone watching, while another user's
curated world stays protected.

The director is **told its username** so it can answer "who am I?" and relay a refusal honestly rather
than inventing a cause. There are no prompt-level permissions — this is a capability boundary.

---

## 8. Co-location

Two users inhabit one space with **no platform shared-anchor**. There is no Quest "Shared Spaces"
dependency.

- **AR guest.** Registers its own detected planes against the space's stored constellation, deriving its
  own transform into the shared identity frame. Content lands on the same *physical* walls for both,
  even though the coordinate numbers differ by centimetres. The mechanism, and why coordinate agreement
  is neither achieved nor needed, is [`spaces-geometry.md §4`](./spaces-geometry.md).
- **Register-only guests.** A guest re-seeds its reference wholesale from the authoritative broadcast
  each capture and never establishes, mutates, mints or posts geometry. This removed a feedback drift
  where a guest evolving its local copy of the shared reference made the world drift over a session.
- **Desktop guest.** No XR session, so nothing to register. It renders in the reference frame directly
  (`#world-root` at identity), spawns 1.2 m to the owner's right on the owner's first presence pose
  (`maybeSpawnGuest`, `conjure-client.js:1118`), and gets `look-controls` + `wasd-controls`. This is a
  complete co-location loop with one headset.

### 8.1 Presence

A `presence` message on `/ws` at ~10 Hz; the server relays it to the other clients tagged by user
(`server.py:4407`), and broadcasts `presence_leave` on disconnect so avatars drop
(`server.py:4425`).

Each client renders the *others* as a vertical box on the floor (square footprint, side 2·R) plus a
sphere of radius R at the head, the sphere floating a few cm above the box — R ≈ 0.13 m, gap ≈ 0.03 m,
both tunable.

Poses stream as **plane-relative anchors** rather than raw coordinates, so an avatar lands against the
receiver's own walls with no shared-frame error. That mechanism is
[`spaces-geometry.md §5`](./spaces-geometry.md).

---

## 9. Testing without travelling

| Flag | Effect |
|---|---|
| `--force-geo zero` | pins the reported location to (0,0) — a "somewhere else" that drives the new-place mint path |
| `--force-geo /daniel/spaces/space-0` | pins you at that space's stored location, driving the return-visit / candidate path |
| `--force-occupied` | pins the active space CLAIMED via a phantom holder, so a single headset hits the admission gate |
| `--drop-surface SEMANTIC\|ID[,…]` | the client pretends it did not capture matching surfaces, exercising recovery with one headset |

Surface matching still runs under `--force-geo` — geolocation only narrows the candidate set. Match the
active space ⇒ admitted; miss it ⇒ refused with a blanked world. True co-location with two live AR users
still needs a second headset.

---

## 10. Surface reference

**Endpoints**

| Route | Purpose |
|---|---|
| `POST /geolocation` | read-only; geo-near candidate spaces across all users, with constellations |
| `POST /space/select` | commit the surface vote; establish or admit/refuse |
| `POST /space/visibility` | toggle a space's `public` flag |
| `POST /space/rename` | rename, fixing up back-references |
| `POST /room` | owner-only geometry ingest → updates the seed |
| `POST /room/realign` | re-capture at the current tracking origin |
| `POST /worlds/new` \| `/worlds/switch` | open to all; stamps the active space ref or `<void>` |

**MCP tools:** `set_space_visibility`, `realign_room`, `query_room`, `set_world_visibility`,
`switch_world(name, owner=…)`, `list_worlds`.

**Code**

| Concern | Where |
|---|---|
| space persistence | `conjure/world.py:837` `SpaceStore` |
| compose / decompose | `conjure/server.py:2627` / `:2659` |
| space ref resolution | `conjure/server.py:2701` `_resolve_space_ref`, `:2715` `_space_ref` |
| geo candidates | `conjure/server.py:1638` `_geo_candidates`, `_GEO_RANGE_M` `:1617` |
| selection + gate | `conjure/server.py:1722` `select_space` |
| occupancy | `conjure/server.py:655` `_space_holders`, `_occupied`, `_unclaim` |
| world-creation gate | `conjure/server.py:2728` `_may_create_world_in` |
| owner middleware | `conjure/server.py:600` |
| geometry ingest | `conjure/server.py:2877` `ingest_room` |
| presence relay | `conjure/server.py:4407` |
| desktop-guest spawn | `client/conjure-client.js:1118` `maybeSpawnGuest` |

---

## 11. Related specs

- [`specs/spaces-geometry.md`](./spaces-geometry.md) — how a surface's position is decided, registration,
  identity correspondence, tracking stability.
- [`specs/worlds-surfaces.md`](./worlds-surfaces.md) — how a world presents a space's surfaces:
  styling, visibility, immersion, the director's surface tools.
- [`specs/agents.md`](./agents.md) — scope as a capability, sessions, worlds, the asset library.
- [`docs/architecture.md`](../architecture.md) — where this sits in the whole system.
