# Wall-art lands *behind* its wall

**Status:** partial fix (A) landed; the root fix is NOT fully solved. The on-device symptom is intermittent,
so this note preserves the full context — including a fix (B) we tried and **rejected** — for when it recurs.

## The observation (2026-07-23)

- Placement generally looked good (grounded/free models, `--drop-surface` recovery, avatars, persistence).
- **One symptom:** the image on **`real_wall_art_45`** rendered *behind* the wall-art surface — correct
  distance, wrong side.
- **Headset-only.** In the desktop/laptop browser the same world showed wall-art 45 **correctly**.
- **Intermittent.** Seen once, during a `--drop-surface "door,window,wall art"` session (so 45 was
  *recovered*, not live-captured); then NOT reproduced in two later runs (with and without `--drop-surface`).

## The surfaces (from `a captured space record` — 59 surfaces, two rooms)

| surface | role | perp to 45 | normal (x,z) | note |
|---|---|---|---|---|
| `real_wall_art_45` | the art | — | (−0.10, −0.99) | recorded `host_wall = real_wall_36` |
| `real_wall_36` | true host | 0.00 m | (−0.12, −0.99) | co-faces the *stored* art normal |
| `real_wall_27` | partition twin | **0.14 m** | (+0.12, +0.99) | anti-parallel; legitimately hosts `real_door_54` (the OTHER room) |

So 36/27 are a **back-to-back partition between the two rooms**, 0.14 m apart, opposite normals, each hosting
insets on its own side.

## Why it happens (mechanism)

`snapInsets` (client/room-snap.js) associates each inset with a wall, then the inset adopts that wall's
orientation and the on-surface image rides it. If the association picks **27** instead of **36**, the art
adopts the opposite normal → flips 180° → the 2 cm "in front" image lands behind. The association is
vulnerable because:

- The derive-by-proximity fallback picks the **nearest ~parallel wall the inset is within**. Between 36
  (0.00 m) and 27 (0.14 m) the midpoint is ~0.07 m, so a few cm of per-capture jitter can make 27 nearer →
  wrong pick. **Intermittent** = this coin-flip.
- **Headset-only:** the desktop viewer never captures, so it never runs `snapInsets`; it renders the
  server's surface at its F_ref pose, which carries the correctly-recorded association. Only the headset
  re-derives locally.

## Fix A — landed

**Captured insets now carry the seed's recorded `host_wall`** (looked up from `docSurfaces` by shared id in
the client's Pass B), so `snapInsets` snaps them by id instead of re-deriving by proximity — aligning with
decision #1 ("the inset→wall association is a recorded fact"). Previously only *recovered* insets did this.
Test: "snapInsets honors a RECORDED hostWall over proximity."

**A does not, by itself, explain the observed symptom.** The case we saw was a *recovered* 45
(`--drop-surface`), and recovered insets *already* carried `host_wall = real_wall_36`. For it to still flip,
the by-id snap must have failed — i.e. **no local wall with id `real_wall_36` existed in that capture** (see
open question 2), so it fell through to the proximity coin-flip → 27.

## Fix B — TRIED AND REJECTED (do not retry without new evidence)

We tried making the proximity fallback require the host to **co-face** the inset (`normal·normal > +0.9`
instead of `|·|`), reasoning the inset's own normal would match only its true host. **This is wrong** and was
reverted:

- `matchRef` (client/room-snap.js) and the memory [[quest-normals-not-outward]] both state an inset's **live
  captured normal can be INWARD, ~180° from its host wall's outward normal**. `snapInsets` runs on the raw
  captured `_lq`, so `sn` may be anti-parallel to the true host.
- A co-facing test would then **reject the true host** and pick the anti-parallel partition wall —
  deterministically wrong for such insets. The existing `Math.abs(...)` is **load-bearing** for exactly this.
- Our confidence was falsely propped up by fixtures: the golden-room capture *and* the stored space both
  showed insets co-facing their host (dot +1.00) — but those are post-processing/stored normals, and the
  unit tests build insets with outward normals, so none of them exercise the inward-live case the memory
  warns about. **Unresolved contradiction:** stored data says co-face, code+memory say live-can-be-inward.

Conclusion: the two anti-parallel partition faces **cannot** be disambiguated from the inset's normal. The
**recorded `host_wall` (A) is the only reliable disambiguator.**

## Open questions / next steps if it recurs

1. **Confirm the pick (instrumentation REMOVED — re-add if it recurs).** A temporary `[host45]` log block
   previously sat right after the local `snapInsets` call in `client/conjure-client.js`; it was removed once
   the standoff/coplanar scare turned out to be a misread (a nearby door seen edge-on, not the wall-art). To
   re-instrument if the flip actually recurs: under `--debug-registration`, log each inset's `recorded=<seed
   host_wall> picked=<host snapInsets chose> recordedInCapture=<yes|NO>` right after that `snapInsets` call.
   - `recordedInCapture=NO` → the recorded wall id didn't resolve this capture → proximity fallback (the
     leading theory for the recovered symptom).
   - `picked≠recorded` with `recordedInCapture=yes` → a by-id bug in `snapInsets`.
   - all `=yes`, `picked=recorded`, and 45 still visually behind → look elsewhere (the on-surface ride, §5a).
2. **Resolve the by-id failure (most likely real cause).** Why would `real_wall_36` be absent from a
   capture's local walls so the recorded id doesn't resolve? Candidates: matchRef didn't map a captured wall
   to `real_wall_36` that frame (id churn on the partition), or wall_36 genuinely wasn't in `detectedPlanes`
   that capture. Instrument matchRef output for the partition walls. This — not the proximity fallback — is
   the likely trigger for the *recovered* symptom we saw.
3. **Settle the inset-normal-facing question empirically.** Log the raw `sn` vs the true host `wn` for
   wall-art in a live headset capture. If live wall-art is reliably OUTWARD (co-facing), the memory is stale
   and a co-facing fallback becomes viable; if it's INWARD, `|dot|` must stay. Don't assume either.
4. **If ambiguity persists**, persist the association more defensively (store it against both partition
   faces, or resolve at ingest with the whole room in hand) rather than relying on any per-capture derivation.
