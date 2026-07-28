# Known issues & shelved fixes

Running log of problems we've diagnosed but deliberately did **not** land on `main`, plus the branches
where a fix is parked. Each entry says what the problem is, whether it's actually been observed, and how to
pick the work back up.

---

## Shelved: wall-less-seed registration deadlock

**Status:** anticipated; observed **once** (during the inset-churn era), **not reproduced** since the churn
was fixed. Fix parked on branch **`deadlock-breaker`** (commit `f94dbd6`, branched off `main` @ `4027e9f`).
Abandoned on the mainline pending an actual recurrence.

**The problem.** `RoomSnap.register()` needs a wall basis (vertical plane pairs) to lock at all. If a room's
persisted *seed* ends up with **no walls**, it can never be registered against — and because a fresh
establish is gated on an **empty** `_ref` (`conjure-client.js`, the `canEstablish` line), an owner that has
already adopted such a seed is stranded in permanent `relocalizing`, with no path to rebuild the reference.

**How it happened (the once).** Before corner-relative inset identity was resolved against `_ref` (see
`docs/local-first-geometry.md` §5.3), an inset-identity churn re-minted ids every capture; over a session the
churn pruned the architectural surfaces out of the seed until it decayed to *furniture-only* → wall-less →
deadlock. The churn fix removed that mechanism, so the decay — and thus the deadlock — no longer occurs on a
healthy seed. That's why this is shelved rather than merged: it guards a route that's currently unreachable.

**What the shelved fix does** (all keyed off `MIN_SEED_WALLS = 3`, matching `register`'s `ref<3` floor):
1. **Establish gate** — the owner only establishes a fresh reference from a capture that has ≥3 walls (never
   seed a wall-less room).
2. **Adopt gate (recovery)** — the owner only adopts a persisted seed that has ≥3 walls; otherwise it leaves
   `_ref` empty and establishes fresh, whose `replace`-POST then overwrites the bad seed.
3. **POST guard (prevention)** — never persist a wall-less surface set.
4. **Server backstop** — a wall-less `replace` post can't wipe a walled seed (`server._MIN_SEED_WALLS`);
   protects the persisted seed from any client. Unit-tested on the branch
   (`pytest -k wall_less` → `test_wall_less_replace_post_cannot_wipe_a_walled_seed`).

**Reproduce / verify (if it recurs).** With the server stopped, strip the walls from a persisted space:
`python3 -c "import json; f='.cache/spaces/<user>/<space>.json'; d=json.load(open(f)); d['surfaces']=[s for s in d['surfaces'] if (s.get('meta') or {}).get('semantic')!='wall']; json.dump(d, open(f,'w'))"`
then re-enter. **Without** the fix: hangs in `relocalizing` (`ref=<n> … dlt=0 … hold`). **With** it (the
branch): refuses the seed, establishes fresh, and the space file gets its walls back.

**To revive:** `git merge deadlock-breaker` (it's exactly `main` + the one commit).

---

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
