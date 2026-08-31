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

**Was this ever observed away from session start?** Every instance in the record is within ~350 ms of
`space.enter`. If it only ever happens at entry, the load gate closes it completely. If it recurs
mid-session, the tolerance question comes back and the evidence will be cleaner for having eliminated this.

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
