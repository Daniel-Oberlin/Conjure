# Backlog — misc fixes & rough edges

A running shelf for small fixes, papercuts, and known rough edges that don't belong to a specific
plan doc. Each entry: what's wrong, why, a proposed fix, and any open decision. Move items out (and
delete them here) when done.

---

## Models placed "facing me" come out 180° backwards

**Status:** open · noticed 2026-06-25 during live director testing · **sign needs Quest confirm**

**Symptom:** "lay out models of people in a circle around me, facing me" placed the circle correctly
but rotated every figure 180° so they faced *away*. Consistent 180° (not random per-model) ⇒ a single
convention error, not noise.

**Cause:** `place_asset`/`place_cached_asset` take an LLM-computed `rotation` (server.py:464), so the
director freehand-computes the yaw to face center — and the forward axis is inverted. The prompt says
"session forward is −Z," but a GLB character at rotation [0,0,0] faces +Z, so "rotate to face center"
flips sign and everyone turns their back. Images never hit this: `place_image` has **no rotation
param** — it plants the plane at a fixed server-side orientation, so the LLM does no facing trig.

**Proposed fix:** mirror the `on_surface` pattern (server computes orientation, LLM doesn't). Add a
`face` option to `place_asset`/`place_cached_asset` — `face_toward: [x,y,z]` or `face: "user"` — and
compute the yaw server-side so the model's forward points at the target. Then "facing me" needs zero
LLM trig and the convention lives in one function (a one-line flip to correct once verified on device).
Consistent with the prompt's existing "DON'T hand-compute a position or rotation" rule, which currently
only covers images-on-surfaces.

**Open decision:** the exact yaw **sign** is orientation math — confirm on a Quest before trusting it
(same caveat as the window-upside-down item).

## Orphaned cache files after asset deletion — need a prune/GC sweep

**Status:** open · noticed 2026-06-25

**Symptom:** deleting an asset removes its catalog row (+ FTS/aliases/relations/vector) but **leaves the
file** in `.cache/assets/`. So deleting assets accumulates orphaned bytes on disk (live: 2 files left
after 2 deletions).

**Cause (by design, not a bug):** `library.delete()` and `/delete_asset` are catalog-only — the
docstring spells it out ("bytes kept"). The cache is content-addressed (filename = sha256 of bytes),
and a placed entity references `/assets/<hash>` directly in the world doc, *independent* of the catalog
row. Unlinking on delete would 404 a texture still used by the live scene.

**Proposed fix:** a separate, deliberate **prune/GC sweep** (NOT coupled into delete_asset). Remove
cache files that have **no catalog row AND no reference in the world doc** (scan entity material src /
gltf-model paths). **Dry-run by default** (list what it would delete); `--apply` to actually unlink.
Would also mop up the already-orphaned files. Expose as a maintenance command/endpoint alongside
reindex / retag-skyboxes / caption.

**Open decision:** should it also consider OTHER worlds/scopes' references once multi-agent lands? For
now a single live world doc is the only reference set; revisit when scopes hold separate worlds.

## Director re-queries for ids it already has in context

**Status:** open · noticed 2026-06-25 during live director testing

**Symptom:** the director re-runs `query_assets`/`search_library` for data it retrieved a turn or two
earlier and still has in context. Live: it listed the 3 transparent images *with ids*, then on "place
them left to right" announced "let me look those up properly first!" and ran the identical query again
to get ids it already had. Cheap and correct (fast local SQL, right result) — a papercut, not a defect.

**Cause:** the reuse nudge exists in the prompt ("REUSE ids you already retrieved; don't re-run
query_assets for something you just listed") but doesn't hold reliably. Two reasons: (1) it's one
clause buried in a single ~600-word run-on paragraph, so it gets diluted; (2) the model defaults to
"verify before acting" — describing felt low-stakes, *placing* felt like a commit, so it re-confirmed.
Suppressing a cheap idempotent re-lookup is inherently soft for a prompt nudge.

**Options:** (a) leave it — cheap and correct; (b) hoist the reuse rule into a prominent standalone
line — low risk, diminishing returns (the nudge already exists once); (c) **restructure the whole
builder prompt** from one wall-of-text paragraph into scannable sections / a "Rules" block — the real
fix, since right now every behavioral rule competes inside one paragraph. (c) is behavioral (can't be
unit-tested) and risks nudging other behaviors, so it needs a live test pass.

**Lean:** (c) is the high-leverage move if these "nudge didn't stick" papercuts keep recurring;
otherwise (a) is defensible.

## `search_library` is unscoped while the maintenance tools are scoped

**Status:** open · noticed 2026-06-25

**Symptom:** `search_library` (reuse) returns assets from *all* scopes, but `query_assets`/`update_asset`/
`delete_asset` see only the caller's scope. So the two can disagree on what exists (live: search found
2 apples, query found 1), which confused the director. Today it's masked because everything ends up in
`private/builder` (single agent + the `adopt_unscoped` heal), but it's a real inconsistency.

**Fix:** scope `library.find()` too — thread the agent's scope through `/library/search` →
`find()`/`search()`/`vector_search()` (add `AND scope=?` to each stage) and the `search_library` tool
(carry `SCOPE` like the maintenance tools). Then reuse and maintenance see the same per-agent set.
Matters for multi-agent; deferred until then.

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

**Partial fix shipped:** manual re-tagging is now possible — `update_asset(id, kind='grounded_skybox')`
(or "re-tag this skybox as grounded" to the agent). So the existing two are fixable by hand.

**Remaining nice-to-have:** make **`set_grounded_skybox(image_id)` re-tag** the asset's kind →
`grounded_skybox` automatically (usage informs the catalog) — so applying one grounded keeps the
catalog truthful without manual marking. Forward generation already tags correctly.
