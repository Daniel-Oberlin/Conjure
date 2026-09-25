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

A space is **user-owned and agent-agnostic**: the physical space does not belong to the builder or to the
outdoor agent, it belongs to the person who first captured it. Worlds, assets and state are
agent-owned *under* a user. Every one of them carries a `public` flag.

**Space ownership is decoupled from world ownership.** You may build your own world inside someone
else's space. That is the point of the fully-qualified space reference (§4).

A space usually contains **more than one room**. `detectedPlanes` reports the whole dwelling, so a
single space routinely holds several rooms joined by doors — the reference capture in
`tests/js/fixtures/golden-space.json` is 45 surfaces across two rooms.

**Rooms are modelled** (§1.1). Every real surface carries `meta.room`, and the space records each room
under `rooms`. The partition is the device's own: the Quest reports one floor plane per room, and the
raised-floor fault displaces exactly those groups rigidly, so the grouping that moves is the grouping it
already drew. A space is still a flat *set* of surfaces — rooms are a labelling over them, not a
containment hierarchy — and `boundary` is still one polygon for the whole space regardless of how many
rooms it holds (see [`backlogs/spaces.md`](../backlogs/spaces.md)).

### 1.1 Rooms

A **room** is a group of surfaces within a space, keyed by the floor plane that defines it.

| Where | What |
|---|---|
| `meta.room` on each real surface | which room it belongs to — **derived**, recomputed from geometry each capture |
| `space.rooms[<id>]` | `{n, name?, floor}` — **user data**, and the only place a name is stored |
| `meta.leads_to` on a door or window | the room on the far side, or absent for outside |

**Membership** is decided client-side (`SpaceSnap.segmentRooms`): a room is a floor plus its *covering*
ceiling; a wall joins when that floor covers it in plan **and** the floor's centre lies on the wall's
−normal side. That second half is what separates adjacent rooms — a partition is captured as two
near-coincident, anti-parallel planes, one per room, and the interior is the −normal side. Insets ride
their recorded `host_wall` rather than their own normal, because wall-art can arrive facing inward. A wall
too edge-on to place is left **unassigned** rather than guessed.

**Identity** is decided server-side (`conjure/rooms.py`) in three tiers: surface-set overlap against the
previous membership, then the recorded defining floor, then mint. Overlap outranks the floor because a
room is ~20 surfaces and survives its own floor being re-minted, where keying on the floor alone would
lose the name with it. Ordinals are monotonic and never reused.

**The name is stored once**, on the room record; surfaces carry only the id. The default display name is
**derived, never stored** — `room #2` until someone sets one — so a rename leaves no stale copy. Naming
is a space write, so it is **space-owner-gated**: a guest building in your space cannot rename your
kitchen. Reserved words (the semantic labels, `all`, `room`, `space`) are refused, and names are unique
case-insensitively.

The record lives on the **space**, not the world, for the same reason `boundary` does: a room name is a
fact about the physical place, so the same kitchen cannot be "kitchen" in one world and "room #2" in
another. It rides the boundary's path exactly — composed in on load, stripped by `_decompose`.

**Which room the user is in** comes from the presence tick: the AR client reports the *floor* it is
standing over (it cannot know the room id) and the server maps it. A CLI, voice or desktop connection is
standing nowhere, and is reported as such rather than guessed at.

### 1.2 Glossary

| Term | Meaning |
|---|---|
| **space** | the persistent physical-environment record (this document) |
| **`<void>`** | sentinel for a world **deliberately** without a physical tie — a pure-sky or purely-virtual world (§4.3) |
| **`UNSET`** | server-side sentinel for a world whose space is **not decided yet** — a placeholder. Renders like `<void>`; relocatable, where `<void>` is not (§4.3) |
| **active space** | the one space currently composed into the active world |
| **space owner** | the user who first captured it; the only user who may rename it, name its rooms, or change its visibility |
| **authority** | the headset whose captures the server accepts — the active world's owner |
| **holder** | an AR client that passed the admission gate and declared `hold` — occupancy |
| **claim epoch** | the interval from first hold to last release; selection is idempotent within one |
| **seed** | the space's stored surface constellation, used for registration and server-side solves |
| **guest** | a connected user who is not the active world's owner |

---

## 2. The record

`SpaceStore` (`conjure/world.py:929`) persists one JSON document per space:

```jsonc
{
  "owner": "daniel",                  // first capturer; the write gate
  "name": "space-1",
  "public": true,                     // gates world-CREATION by others (§5)
  "geolocation": { "lat": …, "lon": … },   // coarse; may be null
  "surfaces": [ /* real-surface entities, geometry + DEFAULT material */ ],
  "boundary": { "floorPolygon": [[x,z], …], "height": 2.6 },
  "rooms": { "room_1": { "n": 1, "name": "kitchen", "floor": "real_floor_10" },
             "room_2": { "n": 2, "floor": "real_floor_8" } },   // §1.1
  "recent": [ ["daniel/agents/builder", "wld_ab12…"], … ],  // what was open HERE, newest first
  "last_scope": "daniel/agents/builder/…",  // the head of `recent`, kept for readers that want just one
  "last_world": "bladerunner1"
}
```

- **Geometry and default materials only.** Per-world styling is not here; it lives in the world doc as
  `surfaceStyles` (§6).
- **`boundary` is one polygon and one height** for the whole space, regardless of how many rooms it
  contains. It is derived from the largest captured floor (`conjure-client.js:3049`, by `_area`), so in
  a multi-room space the smaller rooms are not represented in it. The height is the constant `2.6`, not
  measured.
- **One consumer reads it: the space summary**, which prints it as a line of text for the director
  (`mcp_server.py:269`); the admin listing reports only *whether* there is one. Nothing clamps placement
  against it and nothing renders it — despite `query_space`'s docstring saying models land inside the
  space, that is advice to the model, not an enforced invariant. See [`backlogs/spaces.md`](../backlogs/spaces.md).
- **`recent` is the return-visit history** — `[[scope, world_id], …]`, newest first, capped at
  `_MRU_CAP`. Match this space again and you land back in the newest entry that **still exists**; the
  scope rides along with each world because the agent it belongs to is half of *put me back where I was*.
  `last_scope`/`last_world` are its head, written at the same moment and kept because several readers
  want only "the last one" (the admin listing, public-space discovery) — and because every space doc on
  disk predates `recent`, so the pair is the fallback when the history is absent. A world **rename**
  rewrites nothing here: every entry holds the permanent `wld_…` id, not the name (`server.py:2606`).

  Reading it filters on **two** things, not one: the world must still exist, and the caller must be able
  to enter it (`_may_join_world_in` — you own the scope, or its live session is public). The second
  filter matters because the history is genuinely **cross-user**: it is written under the space's owner
  using whichever scope is live, so your own space remembers a guest's worlds alongside your own. Without
  it, matching your space could switch you into someone else's private world and `_regate_clients` would
  then evict you — from your own space — to passthrough, with the switch already committed.

  It is a history rather than a single pointer for the reason given in
  [architecture.md §1](../architecture.md): a lone pointer has no answer to *the thing it names was
  deleted*, so walking back into your own space after a cleanup minted a new world instead of opening the
  one you had there before it. The world and session pointers are MRU lists for the same reason.

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

The root resolves to the user home (`config.USERS_DIR` — `DATA_DIR/users`, so
`~/.local/share/conjure/users/`), not the in-project `.cache/`.

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

- **Fully qualified** (`_space_ref`, `server.py:3403`) so a world can reference a space in another
  user's scope. A bare `<name>` still resolves, to the world owner's scope, for documents written
  before qualification (`_resolve_space_ref`, `server.py:3389`); it is rewritten to the qualified form
  on the next save.
- **`<void>`** marks a world with no physical tie. A void world drops any stray inline real surfaces and
  renders pure-virtual.
- The active world's owner and the active space's owner **may differ**, and routinely do.

### 4.1 Compose / decompose

The live in-memory document is always **fully composed**; only persistence splits.

**`_compose(world_doc, space)`** (`server.py:3309`) — on load:

1. Drop the persistence-only `space` ref and the `surfaceStyles` map from the live doc.
2. Take the space's surfaces as the real entities; overlay each one's
   `environment.spacePresentation.surfaceStyles[<id>]` onto its `material`.
3. Copy the space's `boundary` — and its `rooms` record — into `environment` (live only — `_decompose`
   strips both, since the space owns them).
4. If the world inherited a non-empty space's geometry, default `spacePresentation.active = true` — a
   world that has real surfaces genuinely has something to work with, even with no live capture this
   session. An explicit `false` (an immersion mode) is respected.
5. Re-pin on-surface images to their (possibly moved) hosts.

**`_decompose(composed, space)`** (`server.py:3343`) — on save, the exact inverse: placed entities plus
the per-surface material deltas as `surfaceStyles`. Real-surface **geometry**, the boundary and the room
record are the space's job and are stripped from the world doc.

The consequence worth stating plainly: **the same physical space, styled two different ways in two
different worlds, is one space record and two `surfaceStyles` maps.** Switching worlds restyles the space
without recapturing it.

### 4.2 Where a new world's space ref comes from

Six paths mint a world: `/worlds/new`, an agent switch (`_activate_scope`), a session mint
(`/session/new`), a session switch into a session with no world yet, `/space/select` establishing one
(`_establish_world_in`), and boot with nothing to restore (`_boot_world`). **They all mint through
`_new_world_store` (`server.py:203`), and it stamps the ref** — `_space_for_new_world`
(`server.py:3422`) returns the live space, so a world born while a headset is standing in a space
composes that space. The two exceptions are explicit: `_establish_world_in` overwrites the stamp with the
space it was told to establish, and `_boot_world` opts out (below).

`<void>` in three cases, and only these:

| Case | Why |
|---|---|
| the world is `outdoor` | a sky world has no space to tie to. Either the request said so (`new_world(outdoor=True)`) **or the agent did** (`world.outdoor` — [`specs/agents.md §3`](./agents.md)); they OR together, so an agent whose point is to put you elsewhere doesn't inherit whatever space you were standing in |
| `active_space == VOID` | nothing is live — an unclaimed server, or the current world is itself void |
| the creator may not build in the live space | someone else's **private** space (§5) |

That last one splits by how the world was asked for. `/worlds/new` **refuses with an error** — an explicit
request deserves one. The implicit paths **degrade to `<void>`**, because an agent switch is navigation and
must not hard-fail; adopting a space the creator has no right to build in would be worse than a void world.

Two things this deliberately does *not* do. It never gates on the space existing **on disk** — a live space
is persisted lazily by `_save_active`, so it is routinely real-but-unflushed at mint time, and an
existence check would silently void every world created before the first autosave. And it never invents a
space: `_boot_world` runs before any space is resolved and is the one caller passing `adopt_space=False`,
leaving the ref absent, which `_activate` reads as the honest "no space chosen yet".

> The stamp lives at the shared chokepoint rather than at each call site because it was previously only at
> `/worlds/new`. Every other path minted a space-less world, so switching agents inside your own captured
> space dropped you into a void world and the incoming agent reported, correctly, that it had no surfaces.

### 4.3 Space-less has two meanings: `UNSET` and `VOID`

A world with no space is either **not decided yet** or **decided to have none**, and those want opposite
treatment when a headset works out which space you are standing in.

| Server state | On disk | Renders | A space selection… |
|---|---|---|---|
| **`UNSET`** | the `space` key is **absent** | space-less | **relocates you** — it's a placeholder, not a choice |
| **`VOID`** (`"<void>"`) | `"space": "<void>"` | space-less | **claims the space and leaves you put** |
| a reference | `"space": "<owner>/<name>"` | that space's geometry | admitted on match, refused otherwise |

`UNSET` is purely in-memory (`server.py`); on disk it is the *absence* of the key, which `_activate`
reads back as `UNSET`, so it round-trips with no new persisted sentinel. The **live document reports
`<void>` for both**, so the client's two-state contract — `isVoidWorld` ⇒ derive a canonical frame — is
untouched. Only the server holds the third state, in `active_space`; `_no_space()` is the predicate for
"no space to work with", which is what almost every caller means.

**Why a deliberately-void world is not relocated.** The client votes its live capture against the geo
candidates *even in a void world*, and must: gating that on non-void is what once left an outdoor
re-entry stuck on "finding" forever. But **resolving which space you are in** and **moving you to that
space's last world** are two different things, and only the first is wanted when you chose to be nowhere.
Previously they were fused, so leaving and restarting inside an outdoor world and then putting the
headset on pulled you into whichever agent last used your living room.

**Why that needs `UNSET` to be safe.** A boot placeholder is space-less too, and relocating it is exactly
right. Before this, `_save_active` stamped `<void>` on any space-less world, so a placeholder became
*deliberately* outdoor within one autosave (~1 s) — after which the rule above would refuse to relocate
it and strand a headset user in a blank world. Persisting the absence is what keeps the two apart.

The existing principle survives and gets sharper: **spatial truth supersedes the temporal guess** — it
applies to a *guess*. A void world is a decision.

---

## 5. Visibility and access

- **Default public.** New items are `public: true` — except a **session**, which is born with whatever
  its agent's `session.public` declares (default `true`). An agent that is private by nature says so
  once in its definition rather than instructing its model to flip the flag on the first turn
  ([`specs/agents.md §3`](./agents.md)).
- **Visibility inherits the active world.** An asset or state doc created while a *private* world is
  active defaults private; while a *public* world is active, public.
- **Read** = `owner == caller OR public`. **Write** = `owner == caller`.
- **Public-uses-public invariant.** A public world may reference only public assets, so a visitor sees
  the whole scene. Placing your own private asset into a public world — or making a world public —
  auto-publishes the assets it uses, and the tool says so.

A **space's** `public` flag governs one specific thing: **who may create a world in it.**
`_may_create_world_in(user, owner, name)` (`server.py:3416`) allows it iff the caller owns the space or
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
within `_GEO_RANGE_M` (300 m, `server.py:2249`) **across all users**, each with its surface
constellation (`_geo_candidates`, `server.py:2270`). Coarse — it separates home from office, and cannot
separate two rooms at one address.

**Stage 2 — surface vote, client-side.** The client votes its live capture against those candidate
constellations (`SpaceSnap.selectSpace` → the coverage vote of §7 in
[`spaces-geometry.md`](./spaces-geometry.md)) and commits the verdict via `POST /space/select`. The
match runs client-side deliberately: it reuses the same tested matcher registration uses, rather than a
second Python implementation.

### 6.1 What `/space/select` does

`select_space` (`server.py:2355`) branches on whether the active space is already **claimed**:

**Unclaimed** — this AR user establishes:

| Vote | Outcome |
|---|---|
| matched an existing space, whose newest remembered world is **there and joinable** | join that world (return visit), silently |
| matched, but that world is **gone** or **not yours to enter** | join the newest entry of `recent` that is both — a world you had *in this space* — and say which of the two it was |
| matched, but **nothing** in its history is joinable, or it has none | mint the connecting user a world tied to it, **in the agent that space was last used from** (below), and say so |
| matched, private, no joinable world, not the owner | refused |
| **the live world is deliberately `<void>`** | claim the space, **do not relocate** (§4.3) |
| no match | "somewhere new": mint a fresh geo-stamped `space-N` + world, owned by the connecting user |

A space is born **with** its location, at mint time.

**Where a replacement world lands** (`_entry_scope_for`). Your intent — *put me back with the agent I was
using* — survives a missing world, so the chain preserves the **agent** and lets you own the world as
yourself:

> the space's `last_scope` → the live scope → the default agent

A candidate is skipped when its agent no longer resolves on the search path, or when it declares
`world.outdoor` (§4.3) — an outdoor agent's worlds are space-less by declaration, so it cannot host one
tied to a space. This scope was previously **hard-coded to `builder`**, so a space whose remembered world
had been deleted handed you to a general-purpose agent regardless of who you had been talking to.

**There is deliberately no "any world in that session" rung here**, though the agent-switch ladder
([specs/agents.md §7.6](./agents.md)) has one. The candidate sets are not the same shape: a session's
worlds are siblings in one working context, whereas a world carries its own `environment.space` and
`_activate` composes it against *that* space. Reaching sideways into the session would hand a headset
standing in one space a world built for another and render its walls on top of the real ones — a wrong
answer, not a degradation. When a space's own history is spent, the correct move is to build a world
**bound to this space**, which is what `_establish_world_in` does.

**The fallback is announced, and says which kind it was.** A degradation that fires silently is
indistinguishable from a bug, which is how this went unnoticed: the notice names what happened and which
agent you landed in. *Gone* and *private* are reported differently on purpose — one is your data lost,
the other is someone else's door shut, and they call for different reactions. Resuming the *head* of the
history is not a fallback and stays silent.

**So is being moved by the space.** A match relocates you to that space's world in whatever scope owns it
— your space can hand you a different world, and a different agent, which is the design (decision #20).
An agent change is narrated by the agent server (`_agent_change_notice`); a relocation that keeps the
same agent — restart the server in a different space — used to be silent, the same surprise minus the
attribution. `/space/select` now says *"You're in daniel/office now — opened 'workshop'."* when it moved
you and the agent notice will not fire, so the event is announced exactly once.

**Claimed** — the admission gate:

| Vote | Outcome |
|---|---|
| matched the active space | **admitted** — co-location join, no world change |
| matched a different space, or no match | **refused** — nothing minted, nothing switched |

A refusal is an info message plus a **blanked world**: the client shows passthrough only, so content
never floats over the wrong space. The refused user never becomes a holder.

Selection is **idempotent per claim epoch**, keyed by a page-load `cid` in `_selected_cids`, so GPS
jitter cannot re-vote and thrash the choice.

### 6.2 Admission tiers

Admission depends on whether a connection claims *physical presence*:

| Connection | Presence | Aligned to a real space? | Gate |
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
  (`server.py:826`).
- `_occupied()` ⇔ any holder is present.
- It holds until it `release`s (leaves AR) or its socket closes.
- When the **last** holder leaves, `_unclaim()` frees the space and resets `_selected_cids`, so the next
  connection may establish a different space from wherever it is.
- **Boot is provisional.** `_space_holders` starts empty (`server.py:472`), so the booted-active world
  is a placeholder and the first AR user establishes from their own location rather than being refused
  against a stale one.

Only AR clients hold. Desktop, voice and CLI never do.

---

## 7. Authority and edit rights

Two different gates, often confused:

**Geometry authority — the active world's owner.** `/space/capture` sits in the same owner-only
middleware as the scene edits below, so the writer is the *world's* owner, not the space's: build your own
world in someone else's space and you are the capturer there, writing back into their space record. The
client makes the same call (`WM.isCaptureAuthority(me, worldOwner)`). A guest's capture is rejected; guests
*register against* the stored geometry and never re-author it (§8). Within one owner, an
`environment.captureAuthority` records which headset is live, and an idle authority is taken over after `_AUTH_TTL`
so a reconnecting owner is not locked out (`server.py:3632`).

**Authority is not alignment**, and the two were conflated. Being the authority says nothing about whether
this headset has worked out *where it is* — a returning client is capture authority the moment it posts,
which may be seconds before it has selected a space and registered against the stored geometry. A capture
from that window is destructive by default (the client sends `replace: true`), so it prunes everything it
failed to recognise. Until a client has voted, therefore, a capture may **add but not remove**; see
[`spaces-geometry.md` §7.4](./spaces-geometry.md) for that guard and the wholesale-prune backstop beside
it.

**Scene edit rights — the world owner.** Every scene-mutating endpoint requires the caller to be the
active world's owner. Enforced by middleware (`server.py:771`): the MCP client attaches
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
  (`#world-root` at identity) and spawns 1.2 m to the owner's right on the owner's first presence pose
  (`maybeSpawnGuest`). It cannot then move — the desktop navigation the design calls for is not built
  (see [`backlogs/spaces.md`](../backlogs/spaces.md)).

  **The spawn must never reach a headset.** `#rig` carries the camera and has to sit at the origin in a
  session — that is what aligns the A-Frame world frame with the headset's reference space — while world
  content and the raw-XR controller beams hang off `#world-root` and the scene root respectively. Move
  the rig and you view the scene from beside your own hands. Two guards, because either alone leaks:

  - `WM.shouldSpawnGuest` asks **capability**, not current state: a device that can do `immersive-ar` is
    a headset and is never spawned, even before it enters a session. Gating on "am I presenting *now*"
    is false for the first seconds after every page load, headsets included — and the spawn latches for
    the page, so a hit in that window survives into the session.
  - `enter-vr` re-asserts the origin (`resetRigForSession`), so any displacement self-corrects rather
    than persisting.

### 8.1 Presence

A `presence` message on `/ws` at ~10 Hz; the server relays it to the other clients tagged by user
(`server.py:6224`), and broadcasts `presence_leave` on disconnect so avatars drop
(`server.py:6235`).

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
| `POST /space/rename` | retitle in place (a shell action, **space-owner gated**) — the file key is the id, so nothing is re-pointed |
| `POST /room/name` | space-owner-gated; name a room in the active space (§1.1) |
| `GET /room/whereami` | which room each present user is standing in, from the presence tick |
| `POST /space/capture` | owner-only geometry ingest → updates the seed |
| `POST /space/realign` | owner-only; ask headsets to re-capture at the current tracking origin |
| `POST /worlds/new` \| `/worlds/switch` | open to all; every mint path stamps the active space ref or `<void>` (§4.2) |
| `POST /scope/activate` \| `/session/new` | agent / session switch — mints a world, same stamp (§4.2) |

**MCP tools:** `set_space_visibility`, `realign_space`, `query_space`, `name_room`,
`set_world_visibility`, `switch_world(name)`, `list_worlds`.

**Code**

| Concern | Where |
|---|---|
| space persistence | `conjure/world.py:929` `SpaceStore` |
| compose / decompose | `conjure/server.py:3309` / `:3343` |
| space ref resolution | `conjure/server.py:3389` `_resolve_space_ref`, `:3403` `_space_ref` |
| geo candidates | `conjure/server.py:2270` `_geo_candidates`, `_GEO_RANGE_M` `:2249` |
| selection + gate | `conjure/server.py:2355` `select_space` |
| occupancy | `conjure/server.py:826` `_space_holders`, `_occupied`, `_unclaim` |
| world-creation gate | `conjure/server.py:3416` `_may_create_world_in` |
| owner middleware | `conjure/server.py:771` `_owner_only_writes`, `_OWNER_ONLY_PATHS` |
| geometry ingest | `conjure/server.py:3614` `ingest_capture` |
| room segmentation / identity | `client/space-snap.js` `segmentRooms`, `roomLinks` / `conjure/rooms.py` `resolve_rooms` |
| presence relay | `conjure/server.py:6224` |
| desktop-guest spawn | `client/conjure-client.js` `maybeSpawnGuest` + `WM.shouldSpawnGuest` |
| rig-origin invariant | `client/conjure-client.js` `resetRigForSession` (on `enter-vr`) |

---

## 11. Related specs

- [`specs/spaces-geometry.md`](./spaces-geometry.md) — how a surface's position is decided, registration,
  identity correspondence, tracking stability.
- [`specs/worlds-surfaces.md`](./worlds-surfaces.md) — how a world presents a space's surfaces:
  styling, visibility, immersion, the director's surface tools.
- [`specs/agents.md`](./agents.md) — scope as a capability, sessions, worlds, the asset library.
- [`docs/architecture.md`](../architecture.md) — where this sits in the whole system.
