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
