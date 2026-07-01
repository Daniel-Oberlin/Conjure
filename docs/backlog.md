# Backlog — misc fixes & rough edges

A running shelf for small fixes, papercuts, and known rough edges that don't belong to a specific
plan doc. Each entry: what's wrong, why, a proposed fix, and any open decision. Move items out (and
delete them here) when done.

---

## Void/outdoor worlds — canonical-frame refinements (core is shipped)

**Status:** open (refinements) · noted 2026-07-01 · **core shipped same day**

**Shipped:** outdoor/void worlds (`new_world(outdoor=True)` → `environment.space == "<void>"`) are live —
no room geometry, skybox + objects, geolocation won't yank them into a physical space. In AR, `room-capture`
derives the frame on the fly with `RoomSnap.canonicalFrame` (gravity-up + wall-grid axis + largest-wall
forward + centroid origin), never captures/posts, and `#world-root` + the skybox ride that frame → the same
physical room canonicalizes to the same orientation each visit (invariance unit-tested).

**Refinements left:**
1. **Symmetric-room ambiguity (inherent):** no unique largest wall ⇒ no unique canonical orientation
   (same 180° flip as `register()`). Low-stakes for a void world (only the skybox yaw moves, nothing pinned
   to real walls), but worth a tiebreaker (a door/opening, an L-shape corner) when one exists.
2. **Partial-capture stability:** a sparse capture (few walls) can pick a different frame than a full one.
   Prefer a fuller view (weight by covered wall area / require ≥N walls before locking; hysteresis so it
   doesn't hop once locked).
3. **Optional space tie:** let an outdoor world *optionally* bind to a stored space (robust registration)
   instead of canonicalizing — for rooms you revisit a lot and want rock-solid.
4. **Immersion polish:** a void world currently shows whatever skybox is set (or the void color until one
   is). Consider a sensible default / an explicit "outdoor" immersion that always occludes passthrough.

## `view_relative` can't tell you're looking at a placed OBJECT (only room surfaces)

**Status:** open · noted 2026-07-01 (diagnosed from a live session — "the LLM couldn't tell I was
looking at the tree")

**Symptom:** "what am I looking at / what model am I looking at" reliably names walls/doors but misses
placed models. In one session the director could tell the user was looking at the **dog** and the
**shell** but NOT the **tree** — same tool, opposite results.

**Diagnosis (two-part):**
1. **`view_relative` never ray-tests objects — only real surfaces.** `surface = _ray_surface(origin, vec)`
   skips anything without `meta.real`, so the "what you're looking at" result can only ever be a
   wall/door/floor. Placed models appear ONLY via `nearby = _nearby_entities(point, 1.5)` — things within
   1.5 m of a SINGLE probe point (`origin + forward·distance`, where the director guesses `distance`).
2. **`nearby` measures distance to the raw `transform.position` = the model's ORIGIN**, which is only a
   good proxy for "where the object is" when the origin sits at the visible object. It worked for the dog
   (`[1.5, 0.20, -2]`) and shell (`[0, 0, -2]`) — their origins are right where they visually are, ~2 m
   ahead near gaze level, so the probe sphere caught them. It failed for the tree (`[0.08, 5.01, 2.8]`,
   scale 4): that's the tree's GLB **origin ~5 m up** (the tree visually stands on the floor — NOT a
   placement bug, confirmed with the user), so the eye-level probe point is never within 1.5 m of it. The
   director then fell back to eyeballing coords — unreliable.

**Proposed fix:** replace the origin-point-sphere with a **gaze-ray vs. each object's world bounding box**
(position + bbox × scale) test; the nearest of {surface hit, object hit} is "what you're looking at".
Finds an object by its BODY regardless of where the GLB pivot sits, and along the true 3-D ray (so a tree
you tilt up to see is hit). Add an `object` field (id, title, distance) alongside `surface`.

**Prerequisite (why it's not a one-liner):** placed entities **don't currently store bbox** —
`meta.bbox_min`/`bbox_max` are `None` on the placed models (the catalog has them; `_model_entity_op`
takes them but doesn't write them onto the entity). So first thread the model extents onto the placed
entity (from the catalog at place time, or compute the world AABB client-side), then ray-test against it.

**Also noticed (separate):** the Beagle entity has `scale=0.00` — a degenerate/near-zero scale worth a
look on its own (may be a normalize/scale bug at placement).

**Open decision:** ray-vs-AABB (needs oriented handling) vs. ray-vs-bounding-sphere (simpler, looser);
and whether "looking at" should prefer the nearest hit or the smallest angular offset from gaze center.

## Multi-observer room fusion — refine the shared model from every headset (server-side)

**Status:** future feature · noted 2026-06-30 (deferred while building register-only guests, co-location §5)

**Idea:** today the room geometry has a single writer — the authority (space owner) captures + posts; a
guest **localizes against a frozen copy** of that geometry and never contributes (register-only — see
`room-model.md` §8b). That's correct for co-location: the shared `_ref` constellation *defines* the shared
frame, so a guest mutating it locally would only desync (its `/room` posts are 403'd, so the change never
reaches the authority) and feed a drift loop. But a guest is *also* observing the same real room, so its
observations could legitimately **improve** the one model (better extents, corrected drift, "the room
changed since capture").

**Why it must be server-side (not local `_ref` mutation):** to stay co-located there must remain exactly
**one** authoritative model, **one** frame, and **one** id-owner. So fusion flows through the single
writer: guest sends observations → **server** fuses them into the one model → re-broadcasts → *both*
headsets re-seed `_ref` from the updated geometry. Local mutation isn't a cheap version of this — it's
silent divergence + feedback drift (the exact bug register-only fixes).

**Sketch:** a guest posts its registered observations to a NON-authoritative endpoint (e.g.
`/room/observe`, not `/room`); the server fuses (weighted update of surface poses/extents, conservative
mint of genuinely-new surfaces) into the authoritative doc and broadcasts. Needs: confidence weighting,
guard against a mis-registered guest corrupting the model, and the authority's right to override.

**Open decisions:** trust model (does a guest need the owner's consent to refine?); fuse continuously vs.
on explicit "rescan together"; how to reconcile a genuinely *changed* room (furniture moved) vs. noise.
Defer until register-only co-location is solid and the need is real.

## On-surface placed content is stranded by a room re-registration

**Status:** open · noted 2026-06-30 (diagnosed from daniel's `new-room`)

**Symptom:** images "disappeared" from wall-art frames 5 and 12 while the one on 39 stayed. The
director correctly insisted they were still there.

**Diagnosis (data intact — render/anchor bug, NOT data loss):** the three wall images are placed
**floating image planes** (`place_image on_surface` → a separate `ent_image_*` entity ~2 cm in front of
the surface), and all three are still in the world doc with valid bytes + catalog rows. But each plane is
now offset **0.12–0.31 m** from its `wall_art` frame — far more than the 2 cm placement applies. So the
**space geometry was re-registered/re-captured after placement**: the surfaces moved, but the planes
(stored in **absolute reference-frame coordinates**) stayed put and drifted off their frames (some onto/
behind a wall face → read as "gone"). Same family as the boundary-frame-flip issue: placed content is
anchored to absolute coords, so a re-registration shifts the room out from under it.

**Why some survive:** offset magnitude doesn't predict visibility (the surviving calf was *more* off than
the vanished bear), so the live render detail (drifted behind a wall face / off the visible side / a
texture-load hiccup) varies per plane; not determinable from the static doc.

**Proposed fix:** anchor on-surface content to its surface instead of to absolute coords — either (a)
**parent** the `ent_image` to its `real_*` surface entity (child transform, so it rides re-capture moves),
or (b) on re-registration, **re-anchor** placed-on-surface entities by snapping each to its (nearest /
recorded) surface's new pose. (a) is cleaner if we record which surface an on-surface image belongs to
(store `meta.on_surface = <surface id>` at place time). Free-floating `place_image` (no `on_surface`) stays
absolute — only on-surface placements re-anchor.

**Immediate remediation (one-off):** a re-snap pass that moves each on-surface `ent_image` to
`surface_pos + 2 cm·normal` with the surface's rotation — fixes already-stranded images like new-room's.

**Open decision:** parenting (a) vs re-anchor-on-event (b); and whether to record `meta.on_surface` going
forward so we know each image's home surface.

## Semantic (embedding) matches are capped at "weak" — calibrate distance → tier

**Status:** open · noted 2026-06-30

**Symptom:** "I made an image of a key, but reuse shows it as a **weak** match." Embedding search
already *surfaces* the right asset (cross-modal text→image), but it can never be a confident/strong hit.

**Why (by design, today):** `library.find()` decides the confidence tier from **match TYPE**, not the
embedding distance: `strong = any(match in ("alias","exact"))`. A `vector` hit (like an `fts` hit) is
never in that set, so semantic similarity is structurally capped at **weak**, however close it is. The
`distance` is computed and returned but never thresholded. This is intentional — strong = "safe to
auto-reuse without asking," reserved for *intent* signals (you typed the stored description verbatim, or
you pinned the phrase via `default_for`) — semantic "near" doesn't reliably mean "the asset you meant" (a
"key" query is also near a padlock / door handle / keyboard). A generated image stores `label = prompt =`
the full generation prompt, so a short later query ("key") only ever hits via FTS/vector ⇒ weak.

Note the doc/impl wrinkle: `vector_search`'s docstring claims "the reuse-tier layer maps distance →
strong/weak/none," but `find()` never maps distance — only match-type. The calibrated mapping was never
built.

**Proposed fix (future):** a **calibrated** distance→tier rule — e.g. "a `vector` hit with distance < X
⇒ strong" (and a mid band ⇒ weak). Needs empirical tuning of X on real catalog data (and likely
per-kind, since distances aren't calibrated across categories), so treat it as **opt-in** until it's seen
to behave. Until then, the workaround is to give an asset real intent: `update_asset(id, default_for="key")`
(pin → alias/strong) or `update_asset(id, label="key")` (exact/strong). Possible adjacent nudge: when the
user clearly *names* what they're generating ("make me a key"), auto-pin `default_for` so it's instantly
strong-reusable (behavioral — needs a live test).

**Open decision:** calibrate a global X vs. per-kind thresholds; and whether to auto-pin on naming.

## World index for cross-user public discovery (scale the existing walk)

**Status:** open (perf only) · noted 2026-06-29 · **walk shipped 2026-06-30**

**Shipped:** cross-user discovery now works — `WorldRepository.list_public()` scans every
`<root>/*/agents/*` dir, reads each doc, and returns the public ones tagged by owner; `/worlds/list`
returns them as `available`, and `switch_world(name, owner=…)` enters another user's public world. This
is the "filesystem walk + read each" approach.

**Remaining (perf):** the walk reads *every* world doc on each `list_worlds` call — fine at small scale,
won't scale. When discovery gets heavy, add a derived **world index** — a catalog table of
`owner / name / public / space` from the docs (docs stay source of truth), kept in sync on world
save/delete — turning the walk into one indexed lookup. Defer until it actually hurts.

## Director claims a surface restyle is done without calling the tool ("the couch")

**Status:** open · noticed 2026-06-26 · **CONFIRMED hallucination** (repro'd both ways)

**Confirmation (clean session, same couch):** "surface 41 green" → director called `show_surface(real_couch_41)`
then `style_surface(real_couch_41, green)` → `Styled 1 surface(s)` → couch turned green. Same surface,
same world — works when the tool is actually called. So the failing turn was purely the director
emitting "Done" without calling `style_surface`. Likely contributing factor: the failing turn was in a
DEGRADED-tracking session with the every-2s re-ingest flood (noisy context); the successful one was a
clean restart with no flood. So the prompt guardrail is the fix; reducing context noise may also help.

**Symptom:** "Make the couch green" → director replies "Done — the couch is now green!" but nothing
changes. Reported as couch-specific and reproducible; other surfaces (tables, walls) restyle fine.

**Evidence (decisive):** the saved world `new-room` (rev 273) shows the 4 tables `color=blue
visible=True` (styled) but `real_couch_41` still `color=#888 visible=None` — **never touched**. The log
for that turn has **no `style_surface` tool call and no `material.color` patch** — just the final
"Done". So the director hallucinated completion without calling the tool.

**Not a surface bug:** matching (`target="couch"` → semantic match), material defaults (couch = normal
opaque panel; only doors/windows are special), and recapture (updates in place, preserves style) treat
the couch *identically* to the tables that worked. If `style_surface(target="couch")` had run it would
have worked. No couch-specific code path exists — this is LLM behavior (assert-done-without-acting),
same class as the re-query papercut.

**Possible trigger (unconfirmed):** an unstyled surface shows in the director's room summary as
`visible=False` (styled ones flip to `visible=True`), so the model may treat the couch as "not active"
and skip to a confirmation.

**Proposed fix:** (1) prompt guardrail — never report a change as done unless a tool was actually
called this turn; (2) clarify that every real surface, **including furniture (couch/shelf/table)**, is
a valid `style_surface` target. Both are soft (prompt-level).

**To confirm on repro (the user will retry in a fresh world):** watch the log on "make the couch X" —
- **no `style_surface` call** → confirmed hallucination → the prompt guardrail is the fix;
- **`style_surface(target="couch")` fires but the couch still doesn't change** → flips to a CLIENT
  rendering bug (couch `material.color` not applied), a different investigation.

**Side note:** the same log shows the room re-ingesting all ~45 surfaces every ~2s continuously — heavy
and noisy (recapture never touches `material.color`, so not the couch cause); may be amplified by the
shared-room layer in the multi-world code. Worth watching.

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
