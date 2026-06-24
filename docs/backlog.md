# Backlog — misc fixes & rough edges

A running shelf for small fixes, papercuts, and known rough edges that don't belong to a specific
plan doc. Each entry: what's wrong, why, a proposed fix, and any open decision. Move items out (and
delete them here) when done.

---

## Rotated/placed objects clip through the floor

**Status:** open · noticed 2026-06-23 during live director testing

**Symptom:** "Turn the woman upside down" flipped the model but her **feet stayed on the floor and her
body went below ground**. More generally, rotating (or scaling) a floor-placed model can push part of
it through the floor.

**Cause:** the model's pivot is at its **base** (the GLB origin ≈ the feet, which is where we seat it
on the floor via `_normalize` in `conjure/server.py`). A rotation is applied about that pivot, so a
180° X-flip swings the body *down* through the floor while the feet stay at the pivot. Nothing
re-seats the object after the rotation.

**Proposed fix:** a client-side **`grounded` A-Frame component** (opt-in, flagged on objects that
auto-sit on the floor — `place_asset` / `place_cached_asset` with no explicit height). On a
transform change it computes the *rotated* model's world AABB (`THREE.Box3().setFromObject(mesh)`) and
offsets `position.y` so `box.min.y === 0` (floor). Notes:
- Ground on **rotation/scale**, but let **explicit height** placements win (don't yank "raise her 1 m"
  back to the floor).
- Guard the re-seat against re-triggering itself (one-shot flag).
- Floor = y=0 in the local-floor frame (rig at origin).
- Server-side alt (recompute the rotated AABB from the catalog bbox and emit a corrected position) is
  viable but bakes geometry math into the generic `update_entity` path — client component is cleaner.

**Open decision:** "flip upside down" → **stand on head** (re-seated on the floor, lean) vs. **hover
inverted** where she was (head down at original head height). Grounding gives the former.

---

## Image upside-down when framed in a window (wall art is fine)

**Status:** open · noticed 2026-06-24 · **needs Quest testing**

**Symptom:** `place_image(on_surface=<window>)` hangs the image **upside down**; on a wall-art surface
it's correct and quick.

**Cause:** `place_image` (`server.py:1060`) **adopts the captured surface's `rotation` verbatim** for
the image plane. Wall-art planes are captured upright/inward-facing; **window** planes come back with
a flipped orientation (the headset's plane detection inverts their up/normal), so the image inherits
the flip. Capture-side quirk, not the placement math per se.

**Proposed fix:** don't trust the captured rotation for image orientation — compute an **upright,
room-inward-facing** mounting rotation (normal toward the room interior, zero roll) from the surface
position + room center, used for *all* on-surface placements. Alt: normalize window/door surface
rotations at ingest so "up" is consistent. Either way, **verify on a Quest** (window orientation is
device/capture-dependent; can't confirm blind).

## Grounded skyboxes indistinguishable from regular in the library

**Status:** open · noticed 2026-06-24

**Symptom:** generated two grounded skyboxes, but the director finds none — `search_library(
kind="grounded_skybox")` returns nothing; all 15 skyboxes are kind `skybox`.

**Cause:** the **forward path is correct** (`generate_grounded_skybox_image` → op `grounded_skybox` →
kind `grounded_skybox`). But the two existing ones are **data-lost**: backfill records no `op` (→ all
`image`), then `retag-skyboxes` flipped wide images → `skybox`, and the aspect heuristic can't tell
grounded from regular. So they're buried as plain `skybox`. (Grounded-ness *does* matter — applying a
regular city skybox as grounded would smear the ground — so we can't just collapse it into "an
application choice".)

**Proposed fix:** make **`set_grounded_skybox(image_id)` re-tag** that asset's catalog kind →
`grounded_skybox` (usage informs the catalog) — auto-recovers the existing two the moment they're
applied grounded, and keeps the catalog truthful. Optionally also a way to set kind directly
(extend `correct_asset` with a `kind` field) for manual marking. Forward generation already tags
correctly, so this is mainly about the backfilled/retagged history.
