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
| 3 | Read `planes` on `space.enter` for each session | **4** and **16** of 58 on the two sessions that churned; **58** on the one that did not | Space load. `detectedPlanes` is the persisted Room Setup delivered wholesale, so a small count means the Quest has not finished restoring it |
| 4 | Check whether the floating-room fault could explain it | `matchWall`'s `perp` is purely horizontal; that fault is vertical | Two separate faults. Do not conflate them |
| 5 | Check what registration accepts | a lock at `cov ≥ 0.3 × |ref|` — **a third of the space** | A frame is solved from partial geometry, and that frame is ~17 cm out in x/z. Identity is then assigned from it |

## The fix

**Hold identity until the space has loaded.** A capture below `LOAD_FRAC` (0.6) of the seed's surface count
holds — no identity, no render, no post — exactly as the trust gate already holds a tilted capture. Verified
live at 16:16:45: `planes: 2` → `space.loading` → `space.loaded planes: 58, held: 1`, no churn.

`WM.loadGate` returns `hold` / `go` / **`forced`**. The third is a deadlock escape, not a tuning outcome: a
space that has genuinely *shrunk* could never reach the threshold, and holding forever would also block
posting the removal — the wall-less-seed deadlock in a new costume. After `LOAD_PATIENCE` captures it
proceeds and records that it was forced.

## Tried and rejected

**Raising `--wall-perp-tol`.** The obvious response to "missed by 21 mm". Rejected: the two faces of a
partition sit ~0.4 m apart, so a 0.2 m tolerance starts risking the id-swap catastrophe the tolerance exists
to prevent — content on the wrong wall. And it treats a bad *transform* as if it were a tolerance problem.
*What would justify revisiting:* misses that persist after the load gate, on captures known to be complete.

**Raising `MIN_COV_FRAC` so a partial space cannot lock.** Plausible, and arguably the more principled fix.
Rejected for now because it changes registration behaviour for **every** space and every user to solve a
post-gate problem, and the load gate is strictly narrower. *What would justify revisiting:* evidence that a
partial-space lock causes harm somewhere the load gate does not cover.

## A design assumption this falsified

`matchWall` carries this justification for its tight tolerance:

> *Conservative by design — a wrong wall match is the §10 catastrophe (content on the wrong wall); a missed
> match only mints a recoverable duplicate.*

**A missed match is not recoverable.** The old id goes absent, the three-capture debounce prunes it, and the
server deletes the entity *and its material*. The asymmetry that made a tight tolerance safe does not exist.

## Still open

~~**Should styling survive a prune?**~~ **Demoted 2026-09-24 — see the census below.** It is still true
that a miss costs more than the matcher assumes, but storage is third in line: 83% of churn is the
`claimed` gate, which no storage fix touches, and the re-association key (`near`) is already computed on
every mint. A narrow version of this was built and reverted; it sits on `fix/styling-survives-prune`.

**The 2026-09-09 root cause is still open.** None of the four candidate fixes below has shipped: as of
2026-09-24 `ingest_capture` still gates on `captureAuthority`/`client_id` only, with no check that the
client has voted, no merge-until-voted flag on `_unclaim`, and no wholesale-prune guard. That incident cost
every one of 59 surfaces its styling.

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

## The census, 2026-09-24 — it is not the tolerances, it is the assignment

Everything above reasons from individual incidents. Counting **every** `churn.mint` and `churn.miss` in
every geometry log to date changes the conclusion:

| gate | events | of which doors | dist range (m) | what it means |
|---|---|---|---|---|
| **`claimed`** | **77** | 47 | 0.001 – 1.368 | every tolerance passed; another plane took the partner first |
| `dist` | 8 | — | 0.502 – 0.605 | horizontal/furniture centroid outside `matchRef`'s 0.5 m radius |
| `gap` | 4 | — | 0.880 – 0.885 | along-line overlap |
| `perp` | 3 | — | 0.179 – 0.496 | the wall gate the whole 08-31 campaign was about |
| `dyaw` | 0 | — | — | never once |

**Discount the doors** — see *The door events are diagnosed by the wrong model* below; nothing can be
concluded from them. Of the **46** events whose explanation matches the code path that produced them,
**30 are `claimed` — 65%** — and `claimed` is not a tolerance failure. `explainNoMatch` returns it
only after every gate has passed, and says so itself: *"this candidate WAS matchable — it must already have
been claimed by another plane this capture. That is the id-swap shape."* The closest one in the log missed
by **1 mm**. Meanwhile `perp` — the gate two campaigns and a shipped load gate were spent on — accounts for
three events, and `dyaw` for none.

### Root cause: matching is greedy in plane-emission order

`conjure-client.js:3042–3067` walks the Quest's planes in whatever order `detectedPlanes` hands them over.
Each takes the nearest free reference, and the `claimed` Set is mutated as it goes. There is no global
assignment, so:

1. a plane that happens to arrive early takes a ref that a **later** plane fits better;
2. the later plane finds it taken → `gate: claimed` → mints a fresh id;
3. the stolen ref's true partner now has nothing, misses three captures, and is **pruned with its material**.

Step 3 is the damage, and it is visible in the log. On 2026-09-22 `real_wall_148` missed at `dist` 0.085,
0.072, 0.073 — **7 cm from a perfectly good candidate** — and was pruned. It failed nothing. It lost a race.
`real_wall_141` (5.8–9.1 cm) and `real_wall_art_149` (5.9–9.3 cm) went the same way in the same session.

This is an assignment problem being solved first-come. The fix is to score all plausible (plane, ref) pairs
and assign globally rather than letting emission order decide — and **the cost function already exists**:
`explainNoMatch` computes, per unmatched ref, the best candidate and how far through the gates it got. It is
computed today only to write a log line.

### Re-association is already computed, and thrown away

**23 mints in the logs. All 23 carry a `near`.** Every time the matcher mints a stranger it has already
written down which surface it believes the stranger replaces, and the distance to it. That link is logged
and discarded. Whatever is done about storage, this is the succession key and it costs nothing to keep.

### The door events are diagnosed by the wrong model — and the cascade theory is dead

Doors dominate `claimed` (47, against 16 wall art and 14 wall), which suggested a cascade: insets are keyed
on `host_wall` + slot, so a wall that re-mints should invalidate every door in it. **Both halves of that are
wrong.**

*The cascade is unproven, and a better explanation is available.* A first pass bucketed churn events by
capture (2 s) and found 33 of 47 door events with no wall churn alongside — but that test is invalid: a wall
re-mints **once**, and its doors then miss for many captures afterwards, which no same-capture bucket can
see. Session-scoped, **45 of 47** door events are preceded by wall churn in the same session. That is a weak
result in the other direction, since wall churn is common enough to precede almost anything, so causation is
not established either way.

*What the trace actually shows is a pair oscillating.* **A physical doorway is captured as two door planes,
one from each room**, and they sit a wall's thickness apart — 2–40 mm in this log:

```
ct=18739  miss door_86  dist=0.004
ct=20769  miss door_43            regain door_86
ct=77463  miss door_86  dist=0.002
ct=79473  miss door_43            regain door_86
```

`real_door_86` misses while `real_door_43` holds; then 86 regains as 43 begins missing. The two faces of one
doorway are **trading a single observation between captures**, and the loser reports `claimed` each time.

This matters because it is not the same fault as the walls, and **global assignment cannot fix it**: if the
Quest emits one plane for a doorway and two refs want it, no assignment is correct — one ref loses every
time. The pair has to be recognised *as* a pair.

#### How to recognise the pair — measured, not guessed

The two faces are **not** interchangeable, and the obvious tests fail. From the stored geometry of
`space-1` (the only space captured since the rooms work, so the only one with `meta.room` populated):

| pair | widths | ratio | through the wall | along the wall | centroid apart | rooms |
|---|---|---|---|---|---|---|
| `real_door_45` / `real_door_84` | 1.36 / 0.71 m | **1.91×** | 0.158 m | 0.287 m | 0.329 m | room_2 / room_1 |
| `real_door_48` / `real_door_99` | 0.71 / 0.81 m | 1.13× | 0.077 m | 0.040 m | 0.092 m | room_3 / room_2 |

One face of a doorway can be **nearly twice as wide as the other** and still be the same doorway. That
1.91× case is **not a legitimately wide door** — it is the *frame* around the opening being segmented as the
door, with the actual door the narrower 0.71 m. So the width disagreement is a **capture artifact**, not
ground truth, which matters twice over: the pair test has to survive it, and it is a signal in its own
right. So:

- **Do not gate on extent** — not because widths legitimately vary that much, but because one side can be
  mis-segmented to the frame and the test must still pair them.
- **Do not gate on centroid distance.** Those two faces are 0.329 m apart, and the offset is almost entirely
  *along* the wall (0.287 m) — which the half-width difference explains exactly (0.326 m). Two faces that
  share one edge but differ in width have centroids that slide apart in proportion to that difference. This
  is `matchWall`'s lesson a second time: **the centroid is an artifact; use the plane.**
- **Do gate on the plane relation:** antiparallel normals, a *through-wall* offset within a wall thickness
  (0.077 and 0.158 m here), and overlapping along-wall spans.

The through-wall component is the stable one — it is the wall's thickness, and it is small in both cases
while the along component is not.

**A false positive the naive test would take:** doors near a corner sit close together but at ~90°, not 180°
— `real_door_53`/`real_door_76` at 0.997 m and `real_door_79`/`real_door_82` at 0.765 m, both `dyaw ≈ 90°`.
Proximity alone pairs them; the antiparallel requirement excludes them.

#### A second suspect for the oscillation: an unstable extent

If one side of a doorway is sometimes read as the door and sometimes as its frame, the captured extent
**flips between captures** — and with it the centroid, by the half-width difference, ~0.29 m on
`real_door_45`. That would shift `matchInset`'s `along` slot key from one capture to the next and could
produce the observed alternation on its own, with no competition between the two faces involved.

So there are now two candidate mechanisms for the door churn, and the log cannot separate them:

| | mechanism | signature |
|---|---|---|
| **pair competition** | two refs, one detected plane, traded between captures | the *other* face regains as this one misses |
| **unstable extent** | one face flips between the door and its frame | the same face's width changes between captures |

The traced 09-17 events show the first signature (`real_door_86` regains as `real_door_43` starts missing),
which is evidence for competition — but the two are not exclusive and an unstable extent would make
competition worse by moving a centroid in and out of range.

**This needs one line of instrumentation to settle:** `churn.mint`/`churn.miss` record `dist` but not
`ext`. Recording the candidate's extent on door events would separate the two immediately. Worth doing
before either fix, since they lead to different places — pairing for the first, and for the second, either
preferring the narrower of two nested door candidates or keying insets on something the frame cannot move.

**Room membership confirms it**: both true pairs straddle two rooms (room_2/room_1 and room_3/room_2), which
is what a doorway *is*. That is the check that distinguishes a doorway pair from any other near-coincident
antiparallel arrangement, and it only became available with the rooms work.

Note the configuration is precisely the one `matchRef`'s flip rule exists to **merge**: near-coincident
(`dFlip = 0.15`) and antiparallel, built so wall art facing off its wall keeps its id. For a doorway the two
antiparallel near-coincident faces are genuinely **distinct surfaces**, so that rule is actively wrong here.
The disambiguator now exists — the two faces belong to **different rooms**, which the rooms work records,
with `leads_to` as the other half.

*And their diagnosis is invalid.* `_logChurn` (`conjure-client.js:2046`) calls `explainNoMatch` for **every**
unmatched ref, and `explainNoMatch` routes anything vertical through **`matchWall`'s** gate model. A door is
vertical, so it is explained in terms of `dyaw`/`perp`/`gap`/`claimed` — but a door is actually matched by
**`matchInset`**, on `host_wall` + slot with a separate `claimedInset` set. So `gate: "claimed"` on a door
means only *"it would have passed matchWall's gates"*, which says nothing whatever about why `matchInset`
rejected it. The horizontal branch does not have this problem: it models `matchRef`, which is what
horizontals actually use.

**So we have 47 door churn events and no valid explanation for any of them.** That is its own finding, and
it needs fixing before doors can be reasoned about: `explainNoMatch` should dispatch on the same rule
`conjure-client.js` uses to choose a matcher, and report `matchInset`'s own gates for an inset.

What the raw numbers do show is that doors sit at **1–40 mm** — `real_door_86` misses repeatedly at 0.001,
0.002, 0.004 — so whatever is happening to them is a decision between near-identical alternatives, which is
where an assignment is most ambiguous and a swap most dangerous.

### Two corrections to the record

**The "3 mm shelf" is a misreading.** The backlog reads *"a shelf re-minting over three millimetres is the
sharpest statement yet that the tolerances are not the lever."* Three millimetres is the **margin**
(`val: 0.503` against `tol: 0.5`); the shelf was **503 mm** from its stored position. The conclusion happens
to be right, for an entirely different reason.

**The `dist` values are censored, not clustered.** They look systematic at 0.502–0.605, but nothing below
0.5 can ever be logged — that is the threshold. Reading a common offset into them is reading the tolerance
back out of the data. The likelier story is that furniture suffers the **same centroid-slide artifact
`matchWall` was built to defeat**: a partly-captured bed's bounding-box centre moves by half a bed between
scans. Walls were given a plane-based identity for exactly this; `matchRef` — still used for horizontals and
furniture — was never given the equivalent.

### Checked and rejected

**The floating-room fault leaking into identity.** Tempting: `matchRef` measures a 3D centroid distance, so
a vertically displaced room would break horizontals while leaving walls (matched on a purely horizontal
`perp`) untouched — which is the observed pattern. Refuted by the log: `level.anomaly` deviations on
2026-09-22/24 run **5–8 cm**, an order of magnitude short of the ~0.5 m needed. The 08-31 note that these
are two separate faults still stands.

### Where this leaves the ranking

1. **Global assignment** in place of emission-order greedy — 65% of the validly-diagnosed churn.
2. **Succession via `near`** — carry identity across the mints that remain, instead of logging the link.
3. **Styling storage** — with 1 and 2, there is rarely anything left to rescue. It was ranked first on the
   strength of the 09-24 incident; the census does not support that.

A narrow "carry the colour across a prune" change was built and **reverted** on this evidence; it survives on
`fix/styling-survives-prune`. See the backlog entry for what is keyed to a surface id besides colour —
`meta.image_id` and the derived `meta.friendly_id` are both exposed, and the latter silently renumbers.

## Fixes shipped

| Symptom | Cause | Fix | Commit |
|---|---|---|---|
| No way to tell why a surface vanished | three causes, one appearance | `churn.*` events with a device-vs-matcher discriminator naming the gate and its margin | `371ff83` |
| Walls replaced and colours destroyed on entering AR | identity assigned from a frame solved on a partial space | the load gate — hold until ≥60% of the seed is present (`WM.loadGate`) | `43bca11` |
| `[post]` logged only the first changed id; server `add`/`remove` silent | first-hit `reason`; only `update` logged | full reason list; `seed.add`/`seed.prune` named by id | `371ff83` |

## Related

- [`raised-floor.md`](./raised-floor.md) — the other symptom instrumented in the same pass. Different fault:
  that one is vertical and device-side, this one horizontal and ours.
- [`specs/spaces-geometry.md` §4.0a](../specs/spaces-geometry.md) — the load gate; §10 the event log.
