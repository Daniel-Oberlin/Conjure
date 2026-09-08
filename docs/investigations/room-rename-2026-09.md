# The "room" rename — inventory

**Status:** inventory taken 2026-09-07, **nothing renamed yet.** Branch `feat/rooms`.

`room` is used throughout the code, tools and prompts to mean *the whole space*, which
[`specs/spaces.md` §1](../specs/spaces.md) explicitly disclaims: *"Nothing in the record models a 'room' as
a unit."* Making rooms real ([`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md), *rooms as a
first-class unit*) turns that usage from loose into **wrong**, so it is cleared first — otherwise the word
means both things at once, which is the confusion being removed.

This is the inventory, written before any edit, because a blind `s/room/space/` would corrupt at least four
distinct things (below). The sweep found **57 distinct identifiers** containing `room` across **49 code
files**, plus prose.

---

## The rule

Every occurrence answers one question — **which of three things did this `room` mean?**

| It meant | It becomes |
|---|---|
| the whole space (a dwelling that routinely spans several rooms) | `space` |
| a captured surface, or the set of them | `surface` / `capture` |
| a genuine single physical room | **keep — it is now correct** |

The third column is why this is an inventory and not a script.

---

## Do not rename — verified, and a blind sweep breaks these

**1. The legacy on-disk key.** `world.py:1080` and `:1092`:

```python
old = env.get("room") if "spacePresentation" not in env else None
doc["environment"] = {("spacePresentation" if k == "room" else k): v for k, v in env.items()}
```

These read the **pre-2026-08-26 world-doc key**. The string `"room"` here names historical data, not a
concept — rename it and every world doc written before that migration silently stops migrating.
`users.bak1` still contains such docs, so this path is not hypothetical.

**2. `migrate_env_room_to_space_presentation`.** The function name contains `room` *correctly*: it names
the key it migrates **from**. Renaming it would make it lie about its own job.

**3. Genuine physical rooms.** These are correct today and become more correct after the rooms work:

`rectRoom`, `roomA`, `roomBsmall`, `asymRoom`, `moveRoom`, `floatingRoom`, `bedroom`, `room_check14`,
`golden-room.json` (*"two rooms via connecting doors"*), and every prose use of "multi-room", "one room's
floor floats", "the room translates as a rigid unit".

**4. `roomy`.** `director.py:372` — *"roomy enough to see a full `query_world`"*. Ordinary English. A
substring sweep turns it into `spacey`. This one is the argument for the whole document.

---

## Tier 1 — persisted keys

**Empty.** Verified 2026-09-07 against the live tree under `config.DATA_DIR`:

| Checked | Result |
|---|---|
| `room`-named keys in live space / world / session / state docs | **zero** (only `users.bak1`) |
| `query_room` / `room://current` / `realign_room` in live transcripts | **zero** |
| agent definitions and prompts | in the **repo** (`agents/*/agent.json`, `agents/builder/prompt.md`) |

So there is no migration to write, and `main` and this branch can keep sharing live data throughout.

---

## Tier 2 — wire and director-facing

These are what the model, the client and the prompts see. Each must move **atomically** with
`agents/builder/agent.json`, `agents/builder/prompt.md`, the specs, and
`tests/test_{server,director,mcp}.py`, all of which name them. All in-repo, so one commit can be atomic.

| Now | Meant | Becomes | Notes |
|---|---|---|---|
| `query_room` | the whole space | `query_space` | 14 occurrences incl. `agent.json` |
| `room://current` | the whole space | `space://current` | 16 occurrences; the per-turn injected resource |
| `room_resource` (`mcp_server.py:291`) | that resource's handler | `space_resource` | |
| `realign_room` | the whole space | `realign_space` | |
| `virtual_room` (immersion mode) | a virtual copy of the space | `virtual_space` | **judgement call** — a director-facing enum value; its own docstring says "the room's surfaces… a virtual copy of the room" |
| context-budget slice label `"room"` | that resource's share | `"space"` | `agent_client.py:57`, `director.py:211,551` — display/telemetry only |
| `_slog("room", …)`, `origin="room"` | the capture subsystem | `"space"` or `"capture"` | safe: the client only **logs** `patch.origin` (`conjure-client.js:1071`), never switches on it |
| `room-capture` (A-Frame component) | space capture | `space-capture` | 15 occurrences |

---

## Tier 3 — internals

No wire or data exposure. Free to rename, but still needs the which-meaning judgement.

| Now | Meant | Becomes |
|---|---|---|
| `_room_summary` | the whole space | `_space_summary` |
| `_room_targets` | **surfaces**, for the whole space — misnamed twice | `_surface_targets` |
| `ingest_room` | a capture of the space | `ingest_capture` |
| `RoomUpdate` / `RoomSurface` (`server.py:2776`) | the capture payload | `CaptureUpdate` / `CapturedSurface` |
| `_face_room` | the space's interior | `_face_interior` |
| `_reset_room_authority` | capture authority | `_reset_capture_authority` |
| `inRoom` (`conjure-client.js:369`) | `presentation.active` — is a space in effect | `inSpace` |
| `room-less` (23 prose uses) | space-less | `space-less` |
| `RoomSnap` / `room-snap.js` / `room-worker.js` / `room_snap_js` | space geometry | `SpaceSnap` / `space-snap.js` / `space-worker.js` |
| `scripts/dump_room.py`, `scripts/send_room.py` | the space | `dump_space.py`, `send_space.py` |
| `_room_doc` (`test_mcp.py:62`) | a space doc | `_space_doc` |
| `_anchored_room` (`test_server.py:3026`) | a space with anchored content | `_anchored_space` |
| ~25 `test_room_*` / `test_*_room_*` names | mixed — each needs reading | per the rule |

**Unbuilt, docs-only:** `build_room`, `show_room_labels`, `refine_room_scan` exist only in a docstring and
the backlogs. `show_room_labels` actually meant **surface** labels. Renaming them is a docs edit; decide
deliberately whether to rename things that do not exist yet.

---

## Found on the way — three stale comments

Not renames, but the sweep turned them up and they are wrong today:

- `conjure-client.js:1557` — *"POSTs them to `/room`"*
- `server.py:674` — *"`/room` accept vs 403"*
- `server.py:3063` — *"`/room` is already owner-only"*

There is **no `/room` route**; it is `POST /space/capture`. Fix in the same pass.

Also `tests/test_director.py:567` passes `world://room` as an agent resource — an arbitrary fixture string,
not a real resource. Harmless; worth making `room://current` so the test reads as what it models.

---

## Method

1. **Tier 3 first**, in one mechanical commit per cluster (geometry files, server internals, tests). Cheap
   to review, nothing user-visible.
2. **Tier 2 second**, as a single atomic commit per tool so a bisect lands on a working tree — tool name,
   `agent.json`, prompt, docs and tests move together.
3. **Do-not-rename list re-checked** after each commit: `git grep -n 'roomy\|env.get("room")\|migrate_env_room'`
   must be unchanged.

## Verification

- `pytest` and the JS suite — `tests/test_{server,director,mcp}.py` and `tests/js/room-snap.test.js` name
  most of the renamed symbols, so they are the real net.
- `python -m conjure.cli` boot, and one `query_space` round-trip, since the MCP tool names are only
  exercised at runtime — the `bindings`-in-two-places bug in
  [`backlogs/spaces-geometry.md`](../backlogs/spaces-geometry.md) was caught only by curling the running
  server, and a renamed tool that no agent can call would fail the same way.
- A final sweep: every remaining `room` must be justifiable under the rule.
