# Surfaces drop out and come back without their colour

**Outcome:** cause found and fixed, 2026-08-31. Entering AR before the Quest has finished restoring the
room produced a frame solved from a fraction of the geometry, ~17 cm out in x/z — past `matchWall`'s
identity tolerance. Walls were not recognised, new ids were minted, and the originals were pruned **with
their materials**. Fixed by the load gate ([`specs/spaces-geometry.md` §4.0a](../specs/spaces-geometry.md)).

Also recorded here: a design assumption this falsified, and one thing still open.

---

## Symptom

> "I've noticed occasional surfaces being dropped and added (losing their coloring)." — 2026-08-30

Occasional, unreproducible on demand, and with no idea which of several mechanisms was responsible. It went
uninvestigated for a day because there was nothing in any log to look at.

## Why it needed instrumenting first

Three causes produce an identical appearance in the headset, and the code could not distinguish them:

| Cause | What happened | Where a fix would live |
|---|---|---|
| **device miss** | the Quest never emitted the plane for 3+ captures | the debounce count, or nothing |
| **matcher miss** | the plane *was* emitted; `matchWall`/`matchInset` rejected it → fresh id | the identity tolerances |
| **style orphan** | identity held — `_ref` outlives the seed — but the seed entity was already pruned | protect styling from the prune |

The third is the trap: it is not an identity bug at all, and chasing it as one costs a week.

So the first move was a discriminator, not a fix. For each `_ref` entry unmatched this capture, ask whether
any detected plane could plausibly *be* it: none within reach ⇒ `device`; one right there ⇒ `matcher`, plus
**the gate that rejected it and its margin** (`explainNoMatch`).

## What the instrumentation said, on its first real firing

Walls 116 and 114 were replaced by 119 and 120.

```
15:38:19  churn.mint  wall  why=matcher  gate=perp  val=0.171  tol=0.15  near=real_wall_116
15:40:07  churn.mint  door  why=matcher  gate=perp  val=0.164  tol=0.15  near=real_door_117
15:40:07  churn.mint  wall  why=matcher  gate=gap   val=3.074  tol=0.3   near=real_wall_37
15:38:23  churn.prune real_wall_116  color=#4B0082
15:40:11  churn.prune real_wall_114  color=#4f4f4f
15:40:11  churn.prune real_door_117  color=#000000
```

**`why: matcher`** — the Quest emitted the planes; our own matcher rejected them. Two of three missed on
*perpendicular offset* by **14–21 mm** against the 150 mm `--wall-perp-tol`. Three director-set colours were
destroyed.

That single field is the whole value of the probe: it converts "a wall vanished" into "we rejected a wall
that was there, by 21 mm, on this gate".

## Experiments and what each proved

| # | Experiment | Result | Conclusion |
|---|---|---|---|
| 1 | Log every `churn.*` with a device-vs-matcher discriminator | all three mints read `matcher`, with gate and margin | Not the headset. Our matcher. Rules out the debounce and the device entirely |
| 2 | Order events by the **client's** clock (`ct`), not the server's batch-receive time | every mint lands ~330 ms after `space.enter` — one fast-retry interval | It happens in the *first moments* of a session, not at random |
| 3 | Read `planes` on `space.enter` for each session | **4** and **16** of 58 on the two sessions that churned; **58** on the one that did not | Room load. `detectedPlanes` is the persisted Room Setup delivered wholesale, so a small count means the Quest has not finished restoring it |
| 4 | Check whether the floating-room fault could explain it | `matchWall`'s `perp` is purely horizontal; that fault is vertical | Two separate faults. Do not conflate them |
| 5 | Check what registration accepts | a lock at `cov ≥ 0.3 × |ref|` — **a third of the room** | A frame is solved from partial geometry, and that frame is ~17 cm out in x/z. Identity is then assigned from it |

## The fix

**Hold identity until the room has loaded.** A capture below `LOAD_FRAC` (0.6) of the seed's surface count
holds — no identity, no render, no post — exactly as the trust gate already holds a tilted capture. Verified
live at 16:16:45: `planes: 2` → `space.loading` → `space.loaded planes: 58, held: 1`, no churn.

`WM.loadGate` returns `hold` / `go` / **`forced`**. The third is a deadlock escape, not a tuning outcome: a
room that has genuinely *shrunk* could never reach the threshold, and holding forever would also block
posting the removal — the wall-less-seed deadlock in a new costume. After `LOAD_PATIENCE` captures it
proceeds and records that it was forced.

## Tried and rejected

**Raising `--wall-perp-tol`.** The obvious response to "missed by 21 mm". Rejected: the two faces of a
partition sit ~0.4 m apart, so a 0.2 m tolerance starts risking the id-swap catastrophe the tolerance exists
to prevent — content on the wrong wall. And it treats a bad *transform* as if it were a tolerance problem.
*What would justify revisiting:* misses that persist after the load gate, on captures known to be complete.

**Raising `MIN_COV_FRAC` so a partial room cannot lock.** Plausible, and arguably the more principled fix.
Rejected for now because it changes registration behaviour for **every** space and every user to solve a
post-gate problem, and the load gate is strictly narrower. *What would justify revisiting:* evidence that a
partial-room lock causes harm somewhere the load gate does not cover.

## A design assumption this falsified

`matchWall` carries this justification for its tight tolerance:

> *Conservative by design — a wrong wall match is the §10 catastrophe (content on the wrong wall); a missed
> match only mints a recoverable duplicate.*

**A missed match is not recoverable.** The old id goes absent, the three-capture debounce prunes it, and the
server deletes the entity *and its material*. The asymmetry that made a tight tolerance safe does not exist.

## Still open

**Should styling survive a prune?** Keyed by id, so a returning or re-minted surface inherits it. That is
the deeper fix for "loses its colouring" whatever the reason the id churned, and it would have made this
symptom cosmetic instead of destructive. The load gate removes the most common *cause* of a missed match; it
does not change what a miss costs.

~~**Was this ever observed away from session start?**~~ **Answered 2026-09-09: yes, and the load gate does
not close it.** See *Recurrence* below — the destructive captures arrived **4 s before `space.enter` and
23 s before selection completed**, so the gate never had a chance to hold them, and it recurred twice more
mid-session. The tolerance question is back.

## Recurrence 2026-09-09 — a capture accepted before the space was selected

Same symptom, new path, and it cost every stored surface its styling: **all 59 are `#888`** — the 28
re-minted ones *and* the 31 that survived. Pink walls, cream tables, a maroon bed, green couches,
lightyellow shelves, a `#3B1A08` floor, black doors, darkblue windows, all gone to default grey.

**The load gate did its job.** That is the first thing to establish, because it is the obvious suspect:

```
07:24:53.635 space.enter    {"role":"owner","ref":57,"seed":57,"planes":4}
07:24:53.635 space.loading  {"planes":4,"expect":57}
07:24:53.636 space.loaded   {"planes":57,"expect":57,"held":1}
```

Entered on 4 of 57 planes, `held:1`, loaded 57/57. Exactly as designed. **It was simply too late**, and
the reason is the ordering:

| Time | Event |
|---|---|
| 07:15:35 | `[select] space unclaimed (last AR holder left) — re-selection re-opened` |
| **07:24:49** | `[space] accept surfaces=86 changed=38 seed_ops=38` — 29 NEW ids minted beside the existing 57 |
| 07:24:51 | `[space] accept surfaces=86 changed=1` |
| **07:24:53** | `[space] accept surfaces=59 seed_ops=25` — the 25 originals pruned, every one `styled:true` |
| 07:24:53 | `space.enter` → the load gate holds → loaded 57/57 |
| **07:25:12** | `[select] user='daniel' MATCHED daniel/space-3` — selection completes, 23 s after the damage |

So a client that had **not yet selected a space** was accepted as capture authority and allowed to rewrite
the seed. With no established space at that moment there was nothing to match against in the right
frame, so the matcher missed everything, minted a parallel set of ids (`real_ceiling_57` …
`real_door_85`), and pruned the originals with their materials.

**The `dist` values prove it is not alignment.** A frame solved slightly wrong would miss everything by a
similar margin. Instead:

```
churn.miss real_wall_21     why=matcher dist=1.23   ← 1.2 m out
churn.miss real_wall_art_83 why=matcher dist=0.003  ← 3 mm out, and still a miss
```

A 3 mm miss cannot be a tolerance problem. Those surfaces were not *compared and rejected*; there was no
reference to compare them against.

**It then recurred twice**, both mid-session and both following the same `space unclaimed` line — 07:25:20
→ churn at 07:26:43, and again at 07:30:45 — each cycle costing a couple more surfaces as `wall_21`,
`wall_39` and `wall_art_83` bounced through prune/regain. Three `churn.restyle_lost` events name the
survivors that came back grey.

**Root cause: `/space/capture` has no selection gate.** It is in `_OWNER_ONLY_PATHS`, so *who* is asking
is checked; nothing checks whether they have established *where they are*.

Precisely what reset at 07:15:35 was **occupancy**, not the space — `active_space` was still
`daniel/space-3`. `_unclaim` cleared the per-client commit guard so a returning headset must vote again.
So the returning client had not yet aligned itself to the stored geometry, and its capture arrived in an
unaligned frame. A check of the form "is a space established?" would **not** have caught this: one was. The load gate is client-side and
guards entering AR, which is only one of the ways in.

Candidate fixes, cheapest first:

- **Merge, don't replace, until someone has voted.** Server-only, and the smallest thing that would have
  prevented every deletion here. `_unclaim` already clears `_selected_cids` when the last holder leaves
  (`server.py:911`) — set a flag in the same place, and while it is set treat `/space/capture` as a merge:
  add new surfaces, delete none. The capture still lands, nothing is lost, and the flag clears on the
  first `/space/select` commit.

- **Gate the capture per client** — the complete version, and **not** the "few lines" it first looks
  like. The two endpoints identify the client differently: `/space/select` sends `cid: "c_…"` (a
  page-level id, `conjure-client.js:894`) while `/space/capture` sends `client_id: "hs_…"`
  (`conjure-client.js:1563`), which is why the log reads `client=hs_3y06g2`. They cannot be compared, so
  this needs the capture to carry the same id the vote used — a client change plus a server check,
  shipping together, with a **"missing id ⇒ merge, don't replace"** fallback so a stale page is degraded
  rather than permanently blocked. Worth it eventually because co-location needs per-client admission:
  one headset can be admitted while another is refused.

  *Not* worth reaching for first. Note also that the client hard-codes `replace: true` on every capture
  (`conjure-client.js:3109`), so every capture is destructive by default — which is why the guard below
  earns its place independently of either of these.
- **Refuse a wholesale prune** — the first guard proposed under [*An empty capture wipes a space's
  geometry*](../backlogs/spaces.md): a post that would remove >50% of the stored set is far more likely a
  bug than a fact. Here it would have rejected the 25-surface prune outright.
- **Make styling survive a prune** — the deeper fix in *Still open* above, and the one that turns this
  class of event from destructive into cosmetic. This recurrence is the second time it has been the
  difference between an annoyance and lost work.

**Recovery, partial.** There are no `users.bak*` snapshots, but the geometry log recorded the colour on
every prune, so **37 old ids have a non-default colour on record** (9 pink walls, 3 darkblue windows, 3
black doors, a `#3B1A08` floor, and so on). Re-applying them needs an old-id → new-id pairing, which is
derivable from semantic plus position since the surfaces are physically unchanged. Whether that beats
re-styling by voice is a judgement call — the styling was authored conversationally in the first place.

## Fixes shipped

| Symptom | Cause | Fix | Commit |
|---|---|---|---|
| No way to tell why a surface vanished | three causes, one appearance | `churn.*` events with a device-vs-matcher discriminator naming the gate and its margin | `371ff83` |
| Walls replaced and colours destroyed on entering AR | identity assigned from a frame solved on a partial room | the load gate — hold until ≥60% of the seed is present (`WM.loadGate`) | `43bca11` |
| `[post]` logged only the first changed id; server `add`/`remove` silent | first-hit `reason`; only `update` logged | full reason list; `seed.add`/`seed.prune` named by id | `371ff83` |

## Related

- [`raised-floor.md`](./raised-floor.md) — the other symptom instrumented in the same pass. Different fault:
  that one is vertical and device-side, this one horizontal and ours.
- [`specs/spaces-geometry.md` §4.0a](../specs/spaces-geometry.md) — the load gate; §10 the event log.
