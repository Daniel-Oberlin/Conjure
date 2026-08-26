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
| `mcp_server.py:258` `_room_summary` | prints one line of text: `boundary: height 2.6m, floor polygon [[x,z],…]` |
| `server.py:2363` shell `status` | a yes/no presence indicator in an inspection table |

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

The owner-gate middleware (`server.py:600`) reads `X-Conjure-User` and 403s a non-owner, but a
**missing** header is admitted as the owner. That was deliberate convenience for the direct dev CLI. It
means the gate is not deny-by-default: anything that reaches the HTTP surface without the header has
full edit rights. Tighten to require the header once the dev CLI attaches one.

### Cross-user candidate search is a filesystem walk

`_geo_candidates` walks every user's spaces on each `/geolocation`. Fine at present scale; the same
indexing note as the world-index item in [`backlogs/agents.md`](./agents.md).

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
