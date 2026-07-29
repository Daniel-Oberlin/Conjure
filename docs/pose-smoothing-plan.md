# Pose smoothing — easing the drift-correction step (per-surface slew)

**Status:** DESIGN (proposed, not built). A follow-on to `docs/local-first-geometry.md` §14 (render
performance). §14 stopped the capture from **dropping a frame**; this addresses the *other* half of the
"pop": the visible **step** when a drift correction is applied. The mechanism is a per-surface **slew** —
ease each surface's rendered pose toward its newly-captured target over a few frames instead of snapping —
gated by the existing apply-tolerance deadband and defaulting **off** so it can be A/B'd on-headset like
`--geo-slice-ms`.

Everything here is for the **captured-room (AR) regime**. The void/outdoor regime is different and is
covered in §7.

---

## 1. The artifact this targets

Two independent things could make a surface jump when a capture lands:

1. **A dropped frame** — the capture's synchronous work overran the 90 Hz budget, the compositor reprojected
   an old frame, and during walking that reads as a spatial flick. This is what §14 fixed (off-thread solve,
   pose/shape split, sliced geometry). **Smoothing does not address this** and is not a substitute for it.
2. **The correction step itself** — even with a perfectly-paced frame, when a new capture moves a surface
   past tolerance, today it is *snapped* to the new pose in one frame (`applyEntity`, the `poseMoved` branch).
   At the default ~0.5 Hz capture cadence that is a discrete jump every ~2 s. **This is what smoothing
   targets.**

The two efforts are complementary. §14 removed the dropped frame; this converts the remaining ~2 s *step*
into a short *ease*.

### Why the step exists at all

By design (`docs/local-first-geometry.md` §1–§2), the Quest's map is **locally non-rigid**, so `#world-root`
is held at **identity** and every surface renders at its **own** raw `F_track` pose. Content "rides F_track
drift for free" (§2): when tracking re-localizes, the detected walls move and each surface's pose is
re-read, so content stays glued to passthrough. The cost of that free glue is that a re-localization arrives
as an instantaneous per-surface pose change. Smoothing spreads that change over time **without** reintroducing
a shared frame.

---

## 2. Three clocks (the framing)

The whole design is: run three things at three different rates and stop conflating them.

| Clock | Rate | Work | Status |
|---|---|---|---|
| **Solve** | ~0.5 Hz (or slower) | `register` → id correspondence (`matchRef`) | off-thread already (§14 fix 1) |
| **Shape** | event-driven | mesh re-triangulation on a genuine shape change | sliced already (§14 fix 4) |
| **Pose-follow** | 90 Hz | ease each surface's transform toward its latest target | **this doc** |

Pose-follow is the cheap clock (a `lerp` + `slerp` for a few tens of entities). It is deliberately decoupled
from the solve: the solve fixes the *target* occasionally; the follow renders *toward* that target every
frame.

---

## 3. Core mechanism — split "adopt a target" from "move to it"

Today one event does both jobs at once: `poseMoved` firing both *decides* the pose changed and *snaps* the
entity there in the same statement (`client/conjure-client.js` `applyEntity`, ~`:388–392`).

Split them:

- **Adopt** (capture rate): the apply-gate deadband decides *whether* the new captured pose is a real change
  (`WM.surfacePoseMoved`). If yes, store it as the entity's **target pose** — do **not** move the transform
  yet.
- **Slew** (frame rate): a per-frame pump eases each entity's `object3D` toward its stored target, and stops
  the moment it arrives.

The deadband is retained unchanged and does real work: it is the **free noise floor**. Sub-tolerance jitter
(`< --apply-tol-pos`, `< --apply-tol-rot`) never becomes a target, so **an idle room does zero slew work** —
no target adopted, nothing in the slew set, pump is a no-op. This is the key efficiency property: cost is
paid only while a surface is actively settling into a real correction.

```
                       past deadband?                 arrived?
 new capture ──► surfacePoseMoved ──yes──► set target ──► [slew set] ──frame──► object3D→target ──► settle, drop
                       │                                                              ▲
                       └──no──► keep old target, no motion, no cost ──────────────────┘
```

---

## 4. The slew math (frame-rate independent)

Per unsettled entity, each frame:

```
a = 1 - exp(-dt / tau)                       // dt = tick timeDelta, seconds
object3D.position.lerp(target.pos, a)
object3D.quaternion.slerp(target.quat, a)
if (position gap < POS_EPS && angle gap < ANG_EPS) {
    object3D.position.copy(target.pos)       // snap the last sliver exactly
    object3D.quaternion.copy(target.quat)
    settled = true                           // drop from the slew set
}
```

- **`tau`** (seconds) is the single tuning knob — the smoothing time constant. `tau = 0.1` ⇒ ~63% of the gap
  closed in 0.1 s, ~95% in `3·tau = 0.3 s`. Reads as a quick "ease into place": not a snap, not a float.
- **`1 - exp(-dt/tau)` rather than a fixed per-frame `a`.** A fixed `a` makes the settle duration depend on
  frame rate (72 Hz vs 90 Hz would feel different) and on frame-time jitter. The exponential form makes the
  *wall-clock* settle time depend only on `tau`, independent of frame rate and robust to a variable `dt`.
- This is exactly the **EMA/slew equivalence**: `x ← x + a·(target − x)` is one exponential-moving-average
  step; here `a` is derived from a time constant instead of chosen directly.
- **`POS_EPS` ≈ 1 mm, `ANG_EPS` ≈ 0.1°.** The exponential never reaches the target exactly; the epsilon snap
  guarantees termination (entity leaves the slew set) so steady-state cost returns to zero.

Rotation uses `slerp` (shortest-arc, handles quaternion double-cover); do **not** lerp Euler angles (yaw
wrap). Position uses `lerp`.

---

## 5. Implementation

Mirror the existing `geoQueue` / `pumpGeo` pair (§14 fix 4) — same module-level shape, same "called every
frame from `tick`" placement.

### 5.1 Per-entity state (stashed on `el`, which persists by id)

| Field | Meaning |
|---|---|
| `el._geoSig` | **already exists** — the last *adopted* signature the apply-gate compares against. Keep advancing it at adopt time (`:394`). The gate reads this JS state, **not** the DOM attribute, so moving the transform by any means does not disturb it. |
| `el._tgtPos` (`Vector3`) | target position to ease toward |
| `el._tgtQuat` (`Quaternion`) | target orientation to ease toward |
| `el._settled` (bool) | `object3D ≈ target`; skipped by the pump |

### 5.2 Module-level registry

`slewSet` — the set of entities currently easing (a `Set` or array), so the pump iterates only unsettled
entities instead of walking the DOM every frame. Cleared on world switch alongside `geoQueue`/`geoPending`.

### 5.3 Hook 1 — adopt (in `applyEntity`, the `poseMoved` branch ~`:388`)

Replace the immediate `el.setAttribute("position"/"rotation", …)` with: set `el._tgtPos`/`el._tgtQuat` from
`t.position`/`t.rotation`, clear `el._settled`, add `el` to `slewSet`. Leave the `shapeChanged →
enqueueGeo` path and the `el._geoSig = sig` advance (`:393–394`) exactly as-is — **pose and shape stay
split**; only the *pose* application changes from snap to slew.

When `tau <= 0` (default), skip the split and snap as today (`setAttribute`), so the feature is inert unless
enabled.

**Transform write path.** For the per-frame slew, write `el.object3D.position` / `.quaternion` directly, not
`setAttribute` — a 90 Hz `setAttribute` would parse strings and emit change events per frame. Direct
`object3D` writes are safe here because (a) nothing else sets these entities' position/rotation once
local-first render owns them, and (b) the apply-gate keys off `el._geoSig`, not the DOM attribute. (Content
already writes `object3D` directly at `:1069–1095`, so this is consistent with existing code.)

### 5.4 Hook 2 — the pump (`slewPoses(dt)`, called each frame next to `pumpGeo`)

Iterate `slewSet`, apply the §4 update, drop settled entities. `dt` comes from the room-capture `tick`'s
second argument (A-Frame passes `tick(time, timeDelta)`; `timeDelta` is ms — divide by 1000). The current
signature only names `time` (`:1384`); add `timeDelta`.

### 5.5 Content follows for free

Director content and on-surface images are anchored against the **captured** (target) surface poses — the
`cur`/`localSurfaces` data, not a read-back of a mid-slew `object3D` — and are re-placed each capture
(`:1043–1112`). Apply the **same** adopt-target + slew treatment to content (replace the `object3D.copy(...)`
at `:1069–1095` / `:1095` with target-set + `slewSet` insert). Because a surface and the content glued to it
adopt targets on the **same capture epoch** and share the same `tau`, they ease **together** — a wall-art
inset does not visibly separate from its wall during the transition.

**Correctness note:** content anchoring must continue to solve against the *target* poses (the capture), not
the live transform. It already does (`localSurfaces` is built from the capture data), so no change is needed
there — but do not "simplify" it into reading `hostEl.object3D`, which would be mid-slew.

---

## 6. Junction / seam coherence

`--group-surface-relay` (default on, `docs/local-first-geometry.md` §14 / `conjure/__main__.py`) already
forces **all** real surfaces to re-lay on **one** epoch when any crosses tolerance, so wall↔floor/ceiling
junctions and inset↔cutout share a render epoch. Slew preserves this: all surfaces adopt their targets on the
same epoch and share one `tau`, so they start and finish easing on the same trajectory shape — junction
alignment is held through the transition, not just at the endpoints. Slew therefore composes with group-relay
rather than fighting it. (Mid-ease, surfaces with different gap sizes are at slightly different absolute
offsets but the same *fraction* of their gap; over a ~0.1 s `tau` this is well below the seam-visibility
threshold and resolves exactly on settle.)

---

## 7. Where NOT to smooth — the world-root correction

An earlier sketch proposed smoothing a single "room frame" (`#world-root` / `_Tmat`). **That is wrong for a
captured room** and is recorded here so it is not re-proposed:

- In a captured room `#world-root` is held at **identity by design**, precisely because the map is locally
  non-rigid and no single rigid transform reconciles it (`_updateWorldFrame`, `:971–985`; §1). There is no
  single frame whose smoothing would move the surfaces — each surface carries its own `F_track` pose.
- `register`'s `_Tmat` in a captured room drives **no render transform**. It is used only for id
  correspondence, for converting *head/camera* poses to the shared `F_ref` frame for co-location/avatars
  (`:814–827`), and for the void-world fallback. Smoothing it would smooth none of the visible surfaces.

**Therefore smoothing must be per-surface** (each surface eases its own pose), which is what §3–§5 specify.

The **void/outdoor** regime is the exception: it has no real surfaces, so `#world-root` *is* parked at
`_Tmat⁻¹` (`:987–991`). If drift smoothing is ever wanted there, that single transform is the correct thing to
slew — a separate, simpler case. Not in scope for Phase 1.

---

## 8. Knobs / opt-in

One knob, plumbed the same way as `geo_slice_ms` (config → CLI → server injection → `window`):

| Surface | Name | Default | Meaning |
|---|---|---|---|
| `conjure/config.py` | `pose_tau: float` | `0.0` | smoothing time constant, seconds |
| `conjure/__main__.py` | `--pose-tau` | `0.0` | CLI override |
| `conjure/server.py` | `window.CONJURE_POSE_TAU` | injected | read by the client |

**Default `0.0` = disabled** → adopt path snaps immediately (today's behavior). Non-zero enables slew. This
mirrors `--geo-slice-ms`'s opt-in shape so it can be toggled and A/B'd on-headset with no code change.
`POS_EPS`/`ANG_EPS` are constants (not exposed) unless tuning proves otherwise.

---

## 9. Phasing

### Phase 1 (this spec)
Keep the ~0.5 Hz capture. The slew converts each ~2 s drift **step** into a short settle. Cheap (a `lerp` +
`slerp` for tens of entities, off the heavy path), contained (two hooks + the `slewSet`/`slewPoses` pair +
the config/CLI/server triple), inert by default. Ship it, A/B it against `--pose-tau 0`.

### Phase 2 (only if Phase 1 feels stale during fast walking)
The 0.5 Hz capture means the *target* itself is up to ~2 s stale; the slew smooths transitions but cannot make
tracking more current than the last capture. If fast walking exposes that lag, add a **per-frame target
refresh**: re-read `frame.getPose(plane.planeSpace, refSpace)` every frame for the surfaces we already know,
feeding a fresh target each frame that the slew low-passes → true live tracking, not 2 s-stale.

This needs a **plane→`el` map** built at solve time. `XRPlane` instances are stable across frames while the
plane persists, so the map from the last capture's planes to their entities is reusable between solves; the
heavy `register`/`matchRef` still runs only at the capture cadence. More moving parts (map lifetime across
plane add/remove, entities with no live plane this frame) — hold unless Phase 1 proves insufficient.

---

## 10. Caveats

- **AR lag ceiling (the real tuning limit).** In passthrough the *real* wall is visible, so if `tau` is too
  large the virtual surface visibly trails the real one during a correction. This bounds `tau` from above
  (~0.15 s ballpark). Steady state has no lag (deadband + settle), so the ceiling only bites during the
  transition — keep the settle short. This is the thing to watch in the A/B, not frame cost.
- **Not a dropped-frame fix.** See §1. If a heavy pass ever returns to the frame, slew will *mask* it as a
  smooth glide rather than a flick, which could hide a regression — keep the `--debug-jitter` COST/SPIKE
  probes (§14.1) authoritative for pacing, independent of whether slew is on.
- **Interaction with prune/debounce.** A surface being pruned (absent several captures, `:996`) must be
  removed from `slewSet` when its entity is removed, or the pump dereferences a dead entity.
- **Compute is not the tradeoff.** Per-frame slew for a room's worth of surfaces is negligible; the expensive
  passes stay off the frame. The genuine tradeoff is **smoothness vs. responsiveness** (larger `tau` = calmer
  but laggier to a real correction), bounded by the AR ceiling above.

---

## 11. Testing

- **Unit (`tests/js/`, `node --test`).** The alpha helper and settle predicate are pure and DOM-free:
  - `a = 1 - exp(-dt/tau)` is frame-rate independent — e.g. two 8 ms steps close the same fraction of the gap
    as one 16 ms step (to tolerance).
  - `tau <= 0` ⇒ `a = 1` (snap) — the disabled path.
  - settle predicate flips true within `~3·tau` of simulated stepping and false before.
- **On-headset A/B.** `--pose-tau 0` vs `--pose-tau 0.1`, walking, with `--debug-jitter` on. Watch (a) the
  correction reads as a settle not a step, (b) no virtual/real lag on the walls (the §10 ceiling), (c)
  junctions and wall-art insets stay put through the ease.

---

## 12. Cross-references

- `docs/local-first-geometry.md` §1–§2 (non-rigid map, identity world-root — *why* the step exists), §14
  (render performance — *dropped-frame* half of the pop).
- `docs/decisions.md` #16 (capture off the render thread; gated + sliced render).
- Code: `client/conjure-client.js` `applyEntity` (~`:367–408`), `_renderLocal` (~`:999`), `_updateWorldFrame`
  (`:971`), content placement (`:1043–1112`); `client/world-model.js` `surfacePoseMoved`/`surfaceShapeChanged`.
