# Spaces — backlog

Unfinished work, future directions, and known problems for the space record, selection, admission and
co-location. The current state is [`docs/specs/spaces.md`](../specs/spaces.md); the reasoning behind
rejected alternatives is [`docs/decisions.md`](../decisions.md). Geometry items live in
[`docs/backlogs/spaces-geometry.md`](./spaces-geometry.md).

Items are grouped by what they block, roughly most-actionable first.

---

## Known problems — verified against the code

### The boundary is one polygon, from the largest floor, at a hard-coded height

`conjure-client.js:2194` keeps only the single largest floor by area:

```js
if (c.sem === "floor" && (!floor || c.ext[0] * c.ext[1] > floor._area)) {
  floor = { floorPolygon: …, height: 2.6, _area: c.ext[0] * c.ext[1] };
}
```

Two distinct defects:

1. **Multi-room spaces are under-represented.** A space routinely spans several rooms (the golden
   capture is two). Every room but the largest is absent from `boundary`, so stand in the smaller room
   and the director is handed the *other* room's footprint.
2. **`height` is the constant `2.6`,** never measured — even though ceilings are captured as surfaces
   and `sealWalls` already finds the covering ceiling per wall. The schema in the old room-model doc
   presented this as measured; it never was.

**First establish how much this matters — every consumer, traced.** Almost all of `boundary`'s
footprint is plumbing (build it, POST it, store it, compose in, strip out, persist to the space).
Strip that away and there are exactly two readers:

| Consumer | What it does with it |
|---|---|
| `mcp_server.py:267` `_room_summary` | prints one line of text: `boundary: height 2.6m, floor polygon [[x,z],…]` |
| `server.py:2372` shell `status` | a yes/no presence indicator in an inspection table |

So the boundary today is **a hint to the LLM, delivered as raw coordinates, that nothing verifies.**

**There is no in-bounds clamp.** The design promised models would land inside the boundary and never
through a wall; no such check exists on any placement path, and `floorPolygon` is read in exactly one
place — the text formatter above. `query_room`'s own docstring still promises the enforcement ("Read
this before placing things (so models land INSIDE the room, not through a wall)"), and the builder
prompt names the boundary in its Live-context bullet, so the raw polygon reaches the model every turn.
Both are advice, not a guarantee.

That reframes the severity rather than dismissing it: nothing breaks *geometrically*, because nothing
consumes it geometrically — but the director is told something false, and silently-wrong is worse than
absent when nothing checks.

Worth noting the codebase already solves this correctly elsewhere. `sealWalls` faces the identical
question — which floor/ceiling belongs to this wall? — and answers it per-wall with a footprint
`covers()` test plus a margin, so a wall on a shared boundary counts under both adjoining rooms
(`room-snap.js:539`). The multi-room-correct machinery exists a few hundred lines from the global
`if (area > best)`.

**Three options, in increasing cost. They are not alternatives — 3 depends on 1.**

1. **Make it honest.** Per-room boundaries: floor polygon plus its own covering ceiling height, keyed by
   the floor surface id; the summary reports the one the user is standing in. Small, and it makes the
   existing hint correct rather than misleading.
2. **Make it useful.** A raw polygon (`[[1.2,-0.3],[4.5,-0.3],…]`) is a poor input for an LLM deciding
   where to put a dragon. *"You're in a room roughly 4 × 5 m; you are near the north wall"* is something
   it can act on. Pure summary-formatting — no schema change — and arguably higher value than (1) alone.
3. **Make it enforced.** A real server-side in-bounds clamp on placement, which is what the design
   promised. Biggest change, and it **needs (1) first**: clamping to the wrong room's polygon is worse
   than not clamping at all.

**Recommendation: do 1 and 2 together; leave 3 until something actually places through a wall.** If
only one, do 2 — the director's behaviour is the only thing downstream of this today.

Fixing (1) also unblocks director-authored replacement geometry, which needs a safe footprint to
extrude — see [`backlogs/worlds-surfaces.md`](./worlds-surfaces.md).

### Void worlds cannot be re-homed

The design says a world created `<void>` (because no AR user had established a space yet) can later be
re-homed to a real space once one exists. **No such code exists** — no re-home endpoint, tool, or
handler. Today a void world stays void; the user has to create a new world once a space is established,
and anything they built in the void world does not follow.

### A missing caller header is treated as the owner

The owner-gate middleware (`server.py:609`) reads `X-Conjure-User` and 403s a non-owner, but a
**missing** header is admitted as the owner. That was deliberate convenience for the direct dev CLI. It
means the gate is not deny-by-default: anything that reaches the HTTP surface without the header has
full edit rights. Tighten to require the header once the dev CLI attaches one.

### An empty capture wipes a space's geometry, with no floor under it

`RoomUpdate.replace` defaults to **`True`** (`server.py:2579`), and under `replace` the server prunes
every stored surface absent from the post. So a single `POST /space/capture` carrying
`surfaces: []` deletes the whole seed — 59 surfaces down to the handful that happen to be `anchored`
(photo-pinned, and protected only for that reason).

**This is not hypothetical: it happened during this work.** A throwaway smoke-test POST with an empty
surface list against the live server took `space-1` from 59 surfaces to 4. Recovered from
`users.bak1`, but only because a day-old copy existed — the styling survived independently, since
`surfaceStyles` lives in the world docs rather than the space.

The design reasoning is sound as far as it goes: the *client* owns removal confidence (a 3-capture
debounce), so a surface missing from a post is genuinely gone and the server can prune at once with no
server-side absence counter. But that trusts the client completely, and nothing distinguishes "I
carefully confirmed this room is empty" from "I sent you a malformed or empty frame".

Cheap guards, roughly in order of value:

- **Refuse a wholesale prune.** Reject (or downgrade to merge) a `replace` post that would remove more
  than some fraction of the stored set — say >50% — unless it carries an explicit
  `confirm_empty`/`force` flag. A real room does not lose 90% of its surfaces in one capture.
- **Never prune to empty.** A `replace` post with zero surfaces is far more likely a bug than a fact;
  treat it as a no-op and log loudly.
- **Snapshot before a destructive ingest**, so recovery does not depend on an unrelated backup being
  lucky.

Worth weighing against the current property that a settled room sends no traffic at all — a guard must
not reintroduce per-capture churn.

### The desktop guest is spawned, then stranded

`maybeSpawnGuest` drops a desktop guest 1.2 m to the owner's right, and its own comment says *"then let
wasd/mouse take over"*. Nothing does: there are **no `wasd-controls` or `look-controls`** anywhere in the
client. So the guest is teleported once and then cannot move or look around — half of the feature the
spawn exists to serve.

That also means the browser-only co-location demo the design promised (two tabs, presence, move around)
does not actually work end to end; the guest is a fixed camera.

Adding them is small, but note the interaction: the components must not be attached on an AR-capable
client, for the same reason the spawn must not fire there — the rig has to stay at the origin in a
session. Attaching them off the same `arCapable` check the spawn now uses is the natural shape.

### Cross-user candidate search is a filesystem walk

`_geo_candidates` walks every user's spaces on each `/geolocation`. Fine at present scale; the same
indexing note as the **world index for cross-user public discovery** item below, harvested from the old
flat backlog — the two are one piece of work.

---

## Not yet validated on device

### AR co-location with two live headsets

Everything else in the co-location path is browser-testable and tested: per-connection identity, the
public join gate, presence relay and avatars, desktop-guest mode, the owner gate. The **two live AR
headsets** case is the one path never exercised on real hardware. It is also where the matcher
robustness work (see [`backlogs/spaces-geometry.md`](./spaces-geometry.md)) would actually be proven —
the thresholds are named for exactly this tuning.

### Multiple guests

The presence relay is written for N and has only ever run with one guest.

---

## Open questions needing a decision

### Who owns a space captured by a guest?

A guest is admitted to the owner's active world and captures geometry. Today `/space/capture` is owner-only, so
the question does not arise — the capture is rejected. But if guest-proposes-surface ever lands, the
space's `owner` is the *first capturer*, and it is unclear whether a guest's contribution makes them a
co-owner, leaves ownership with the space owner, or mints something new. Likely "the capturer keeps
nothing; the space owner owns it", but it needs deciding rather than falling out of an implementation.

### Can a remote user re-home the active space?

Voice, CLI and desktop connections are admitted without the co-location gate, by design — a remote user
can drive the active world. It has not been confirmed that such a user cannot *re-home* or re-establish
the space they are not physically in. The tiers say they shouldn't; nothing verifies it.

### Guest agency — a guest's own director on a shared world

Today a guest inhabits (renders + presence) and may create and build their **own** worlds, with everyone
present coming along. Letting a guest **co-edit someone else's** world needs a consent/permission
handshake. Deliberately deferred: capability-over-lockdown was the right first call, but co-editing is a
real want.

### Private-asset references into a public world

Current behaviour is auto-publish-and-tell for the owner's own assets. The alternatives (forbid, or warn
and proceed) were never fully closed out; auto-publish is reachable only for assets the caller owns, so
there is no leak, but the policy deserves an explicit decision.

---

## Future directions

### Per-agent world spaces

An agent could declare how it wants a space presented — a `room_view` block with visibility and style
rules over the base, targeted by semantic, id, or `all`. See
[`backlogs/agents.md`](./agents.md), where this is tracked against the agent definition.

### Cross-machine federation

The `public` share is currently single-server: bytes are content-addressed and global on one disk, and
catalog reads return caller-scope ∪ public-same-agent. Sharing across *machines* is a separate feature
and needs a transport, not a predicate.

### Server multi-tenancy

One active space and a handful of connections is the design point. More than that — several
simultaneously-active spaces on one server — is out of scope and would touch the global
`active_space` / `active_world` pointers directly.

### Desktop-guest spawn tuning

Offset (1.2 m) and facing are constants. Fine for testing; a real multi-user session would want them
configurable, and probably wants the guest not to spawn inside the owner.

---

## Harvested from the old flat `docs/backlog.md` (2026-08-26)

*Items filed against this subsystem before the per-area backlogs existed. Status lines
and dates are as originally written; none has been re-verified against today's code.*

## World index for cross-user public discovery (scale the existing walk)

**Status:** open (perf only) · noted 2026-06-29 · **walk shipped 2026-06-30**

**Shipped:** cross-user discovery now works — `WorldRepository.list_public()` scans every
`<root>/*/agents/*` dir, reads each doc, and returns the public ones tagged by owner; `/worlds/list`
returns them as `available`, and `switch_world(name, owner=…)` enters another user's public world. This
is the "filesystem walk + read each" approach.

**Remaining (perf):** the walk reads *every* world doc on each `list_worlds` call — fine at small scale,
won't scale. When discovery gets heavy, add a derived **world index** — a catalog table of
`owner / name / public / space` from the docs (docs stay source of truth), kept in sync on world
save/delete — turning the walk into one indexed lookup. Defer until it actually hurts.

## "Private space" gates world-creation, not joining — revisit the semantics

**Status:** shelved (design question) · raised 2026-07-07 · from specs/spaces step 6 (D8)

**The concern:** `space.public` currently gates only **world-creation-in-the-space** (a private space =
only the owner may anchor new worlds there). "Private" colloquially means "keep others out," so the label
mismatches the behavior. Three orthogonal concerns share too few words: (1) **co-location** — are you
physically in the room (a fact, not a permission; the admission gate); (2) **content visibility** — can you
enter a given world (`world.public` + the `/ws` refusal); (3) **authoring control** — can you anchor a NEW
world here (`space.public`, D8). We labeled #3 "private/public," but users reach for "private" expecting #2
at the room level.

**Concrete smell:** a "private" space still mints **public, joinable** worlds by default — flip your space
private, create a world, and it comes up public and any co-located person can join it. The privacy doesn't
flow through to what you'd expect.

**Options when revisited:**
- **Collapse toward intuition (preferred):** private space = fully yours — only the owner authors worlds
  there *and* only the owner joins worlds anchored there; public = shared. One rule ("my room, my worlds,
  nobody else"); composes cleanly with the admission gate (a non-owner in a private space is just refused);
  kills the "private space spawns public worlds" surprise.
- **Rename, keep the feature:** stop calling #3 "privacy" — e.g. space "authoring: open vs. owner-only" —
  and leave joining to per-world visibility.

**Also missing (either way):** an owner-controlled restriction on *who may be present in worlds here*,
distinct from co-location (today co-location admits anyone physically in the room). Not a bug — a framing
misstep worth fixing before it ossifies. See the full discussion in the session where step 6 landed.

---

---

## Harvested from the old `docs/known-issues.md` (2026-08-26)

*Field-observed problems and shelved work, moved here when the flat known-issues file was
retired. A parked branch is a property of the item, not a reason for a separate document.
Status lines are as originally written; not re-verified against today's code.*

## Observed (unfixed): world switching & active-world preservation

Two rough edges seen on-device, **not yet fixed**. Captured here so they're not lost.

> **Notes (Daniel):** switching worlds is not always successful, and the currently-active world is not always
> preserved correctly between sessions.

Three distinct mechanisms were found behind these, in order of impact:

1. **Duplicate-space roulette (main driver of "wrong world on re-entry").** `_geo_candidates` returns *every*
   space within GPS range, and the client's `RoomSnap.selectSpace` picks by best registration coverage. When
   several **geo-overlapping** spaces exist at one physical location (leftovers accumulated during the
   churn/deadlock era, when garbage seeds couldn't be re-matched so each re-entry minted a fresh `space-N` +
   a world named after it), the vote lands on a *different* space each re-entry — and each space carries its
   own `last_world` — so you pop into a different world. Non-deterministic by nature (vote noise decides).
   *Mitigation today:* keep only one space per location (a clean `.cache/spaces` avoids it). *Not built:* a
   guard that refuses to mint a new space when a geo-overlapping one already registers well enough.

2. **`_switch_to` `last_world` lag (sub-second, self-correcting).** On a world switch, `_switch_to` calls
   `_save_active()` for the **outgoing** world (stamping the outgoing space's `last_world`) but never stamps
   the **incoming** space's `last_world`. It's corrected on the next autosave, which fires within
   `_AUTOSAVE_INTERVAL` (~1 s) because `store` rebinds to a new rev. So only if you exit within ~1 s of a
   switch (with no edit) does re-entry land in the pre-switch world. *Fix:* stamp `last_world` at the **end**
   of `_switch_to` (after `_activate` rebinds the globals) so it's correct-by-construction. Small; not landed.

3. **Geo-timeout hang ("Getting your world… working out what space you're in" forever).** When the Quest GPS
   fix times out (`code=3`), the space-selection overlay never dismisses: the give-up fallback
   (`endAwaitingSpace()` after `GEO_MAX_TRIES`) never fires because `geoTries` is reset to 0 on every
   `onEnterAR` (`conjure-client.js`), so with the 20 s GPS timeout it can't accumulate to the limit before a
   re-entry resets it (`grep 'giving up after' temp/conjure.log` → 0 hits). The room actually locks fine
   underneath (`[coloc] … LOCK`, `[room] accept …`); only the overlay is stuck. *Workaround:* run with
   `--force-geo /<user>/spaces/<name>` to bypass the flaky Quest GPS. *Fix:* give "awaiting a space" a
   **wall-clock deadline** independent of `geoTries`, so a dropped fix falls back to the active world.

**Relevant code.** Server: `_switch_to` / `_save_active` (`last_world`), `select_space` / `_geo_candidates`
(selection + minting). Client: `warmGeo` / `onEnterAR` / `beginSpaceSelection` / `commitSelect` (the geo
state machine).
