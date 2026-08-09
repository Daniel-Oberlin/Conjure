# Pops & Jitters — investigation journey, fixes, instrumentation, and open theories

**Branch:** `fix/pops-and-jitters` — **fully merged into `main`** (final merge `789f2bb`; the branch is done).
**Status:** all fixes and instrumentation shipped and merged; the final "walking micro-stutter" **diagnosed**
as a platform-level WebXR/Quest characteristic (dropped-frame positional reprojection during translation),
**not our code**. This document is the durable record so the investigation doesn't have to be re-run.

The render model this all sits on is **local-first geometry** (`docs/local-first-geometry.md`): `#world-root`
is held at identity, every surface renders at its own raw `F_track` pose, and a per-surface **apply-gate**
skips re-laying anything that hasn't moved past tolerance. "Pops/jitters" are visible motion that shouldn't
be there.

---

## 1. Fixes shipped

Each entry: symptom → cause → fix → knob → **commit** (all now merged to `main`; the merge each landed in is
in §7).

### 1.1 Junction-seam runaway — `advanceSig` pose/shape baseline hold — **merged** (`1b25a5f`)
- **Symptom:** wall∩ceiling and wall∩wall seams slowly opening over a session ("outside shows through").
- **Cause:** the apply-gate advanced the *whole* signature baseline (`el._geoSig`) on a **pose-only** re-lay,
  silently absorbing sub-tolerance **extent** drift. `surfaceShapeChanged` then measured against a shape the
  mesh had never drawn → the rebuild never fired → the rendered mesh ran away from the true shape. For
  `joinCorners` walls (whose centre + width move as a matched pair) this split the two across render epochs,
  so wall ends missed the shared corners.
- **Fix:** `advanceSig(prev, sig, poseMoved, shapeChanged)` in `client/world-model.js` — on a pose-only
  relay, advance `p/r` but **hold `ext/holes`** at the last-rendered value; a real shape change advances the
  whole thing. Bounds the lag to the extent tolerance and self-heals. Unit-tested.

### 1.2 Wall sealing to the shell — `sealWalls` — **merged** (`6569eca`)
- **Symptom:** a thin open slit at the wall/ceiling (and wall/floor) line once fills were made solid.
- **Cause:** the Quest fits walls a few mm–cm short of the ceiling/floor.
- **Fix:** `client/room-snap.js` `sealWalls` snaps a wall's **top→ceiling / bottom→floor** when the edge is
  already within `--wall-seal-tol` (0.15 m) of the plane. Vertical-only (plane/width/registration untouched).
  Guards: a **footprint `covers()` test** + seal to the **highest covering ceiling** (fixed a shared boundary
  wall sealing to the wrong room's ceiling → a 4 mm slit). Unit-tested (pipeline guards).
- **Knob:** `--wall-seal-tol` (`CONJURE_WALL_SEAL_TOL`, default 0.15; 0 disables).

### 1.3 Float-rounding hairline cracks — fill weld — **merged** (`757df6d`)
- **Symptom:** "noisy static" see-through along abutting fill edges — sub-pixel gaps the passthrough flickers
  through.
- **Fix:** inflate each surface **fill** by `--surface-weld` (default **2 mm**, split per side) so abutting
  fills overlap; the **wireframe outline stays true size**.
- **Knob:** `--surface-weld` (`CONJURE_SURFACE_WELD`, default 0.002; 0 disables).

### 1.4 Junction epoch coherence — group-surface-relay (part B) — **merged** (`6569eca`)
- **What:** when *any* real surface crosses tolerance, re-lay **all** of them (pose **and** geometry) at one
  epoch, so wall↔floor/ceiling junctions and inset↔cutout share a render epoch and can't drift apart.
- **Later finding:** this relay is also an **amplifier of wall jitter while walking** (§4) — it re-lays every
  wall to its *raw* (noisy) pose whenever any one crosses. Turning it off (`--group-surface-relay off`) was
  tested against the walking stutter and **made no difference** (§3), so it is *not* the stutter's cause, but
  it remains a suspect for the separate ~1 cm wall-hunting jitter. Kept on (seam protection).
- **Knob:** `--group-surface-relay on|off` (default on).

### 1.5 Pose-smoothing Phase 1 — per-surface slew — **merged** (`04cc058`)
- **What:** splits "adopt a target" (capture rate, gated by the deadband) from "move to it" (frame rate). A
  per-frame pump eases each surface's `object3D` toward its captured target with a frame-rate-independent
  `a = 1 - exp(-dt/τ)` and an epsilon snap, then drops it from the working set (zero steady-state cost).
  Content glued to a surface eases in lock-step (composes with the content gate below). Design:
  `docs/pose-smoothing-plan.md`. Math (`slewAlpha`/`slewSettled`) is pure + unit-tested.
- **Knob:** `--pose-tau` (`CONJURE_POSE_TAU`, **default 0 = off/snap**). A/B like `--geo-slice-ms`.
- **What it does / doesn't:** smooths a genuine correction *step* into a short ease. It does **not** reduce
  the *rate* of corrections and cannot fix a **noisy target** — if the target itself jitters, slew just turns
  a sharp pop into a gentle swim.

### 1.6 Content shimmer — content apply-gate deadband — **merged** (`c11d92b`)
- **Symptom:** placed content (esp. free-standing props like the dog) shimmered while the gated walls sat
  still.
- **Cause (measured):** `_placeContent` re-solves each content anchor **every capture** against the **raw,
  ungated** plane basis (`_lp/_lq`), with **no deadband of its own** — so the solved pose wandered a few mm
  each capture (measured: the sampled content's world pos drifted in a ~5–6 mm envelope while the sampled
  wall was frozen to 4 dp).
- **Fix:** gate `placeContent` with the **same `CONJURE_APPLY_TOL`** the walls use — hold content unless its
  newly-solved pose moves past pos/rot tolerance from the last committed pose. Content and walls now share one
  deadband. Confirmed by measurement: the sampled content's world pos went **frozen**.
- **Note:** the cleaner long-term form (raised in-session) is to solve free content against the **rendered
  (gated) wall poses** instead of the raw basis, so it inherits wall stability — deferred because the walls
  themselves still chase raw noise.

### 1.7 GPU headroom — runtime foveation knob — **merged** (`fc24b01`)
- **What:** `--foveation LEVEL` (0..1) applied once at runtime via `renderer.xr.setFoveation()`, **overriding**
  `client/index.html`'s hard-coded `foveationLevel: 0`. `index.html` ships 0 deliberately (full-res kills
  moiré/fuzz on the grid + surface edges); this lets you A/B without an edit.
- **Effect (measured):** raising 0 → 0.3 → 0.5 cut the dropped-frame **rate monotonically**; sustained
  GPU-bound drop *bursts* were gone by 0.5. It did **not** eliminate the residual isolated drops (those are
  external — §5).
- **Knob:** `--foveation` (`CONJURE_FOVEATION`, default 0 = today's behaviour). **Default choice left open** —
  a visual trade (smoothness vs peripheral sharpness) for the human to make.

---

## 2. Instrumentation — the `--debug-jitter` toolkit (keep for later)

All gated on `--debug-jitter` (`window.CONJURE_DEBUG_JITTER`); logs via `debugLog("jitter", …)` to
`temp/conjure.log`. Designed so the probe never causes the hitch it measures: **per-frame data is buffered in
memory and flushed once per ~2 s window** (a per-event `fetch` storm was shown to itself drop frames).

| Line | When | Fields / meaning |
|---|---|---|
| **RATE** | once | `current=<Hz> ideal=<ms> supported=[…]` — the **actual** display rate/budget (Quest is often 72, was **90** in our runs; supports up to 120). |
| **PACE** | every ~2 s | `mean`, **`jit(sd)`** (frame-interval stddev — the real smoothness metric), `p95`, `max`, `late(>1.2×)`, `drop(>1.5×ideal)` (missed ≥1 vsync), `slew` (peak slewSet size), **`rebuilds`** (mesh rebuilds this window), **`maxjerk` mm**, `heap` MB. |
| **LATE** | per window | one token per late frame: `dt(prevCap):dW/dO/heapKB/selfMs` — wall/obj world-move mm, heap Δ, and **tick self-time** (our JS). *Flat move + tiny self ⇒ compositor reprojection, outside our JS.* |
| **JERK** | per window | per-frame camera 2nd-difference events: `jerk-mm(on\|late/dt)`. Samples the **WebXR head pose**; jump-then-revert = spike. `on` = on-time frame, `late` = dropped. |
| **SPIKE** | on a >1.5×ideal frame (throttled) | frame-interval ring + sampled wall/obj world-pose rings — the deep-dive dump. |
| **COST** | per capture | sub-phase breakdown of the render continuation: `passB / prepL / renderL / placeC / authO`. |
| `[render]` | once | `foveation=<level>` + framebuffer/viewport (also logs `foveation applied=…` when the knob overrides). |

**Reading guide (how to attribute a stutter):**
- `dW/dO ≈ 0` on a late frame → **our transforms held** → the shift was **compositor reprojection**, not us.
- `selfMs` small (≪ dt) → the stall was **outside our JS** (render/compositor/GC-between-frames).
- `selfMs ≈ dt` → our JS was on the critical path that frame (optimize it).
- `rebuilds` high while walking → mesh churn; `rebuilds=0` closes that out by number.
- `JERK` on **on-time** frames → a **view/tracking** stutter (not a dropped frame); **only on late** frames →
  dropped-frame reprojection. (Caveat: a single drop-pop shows as *both* a `late` jerk (the shift) and an
  `on` jerk (the revert) — count drop-coincident events, not raw `on` counts.)

**Known probe limits:**
- `performance.memory` is **frozen/quantized on Oculus Browser** (constant heap, all deltas 0) → **GC is not
  testable via heap sampling** on Quest. The `heapKB`/`heap` fields are effectively dead there.
- All LATE/SPIKE/JERK forensics fire relative to `dt`; a defect on a perfectly on-time frame with no view
  jerk would still be invisible.

**Not built (the missing tool):** a **controller-button MARKER** — press the instant you see a pop, log
`MARK t=… lastDt=… lastJerk=… rebuilds=…` + the recent ring. This is the one thing that gives **frame-exact
correlation between perception and data**; every round we instead inferred correspondence from counts. Build
this first if the investigation resumes.

---

## 3. Experiments run, and what each proved

| Experiment | Result | Conclusion |
|---|---|---|
| Baseline `--debug-jitter`, standing/looking | `mean ≈ 11.1 ms @ 90 Hz`, `jit(sd) ~1 ms`, `drop ≈ 0` | Frame **delivery is clean**; steady state is not the problem. |
| Content shimmer, pose rings | wall world-pos frozen to 4 dp; content wandered ~5–6 mm | Content re-solve (raw basis, no deadband) → fixed by §1.6; content froze. |
| `--foveation` 0 → 0.3 → 0.5, walking | drop rate fell monotonically; bursts gone by 0.5 | Some drops are **GPU-bound** (full-res render); foveation is a real lever, not a full fix. |
| Tick self-time on dropped frames | a 66 ms frame with **0.2 ms** of our JS | The stall is **entirely outside our JavaScript**. |
| `--group-surface-relay off`, walking | **no difference** to the walking stutter | Whole-room mesh re-lay is **not** the stutter's cause. |
| `rebuilds` counter, walking | **`rebuilds=0`** during a run where pops were seen | Mesh rebuilds are **not** the cause (closed by number). |
| Camera JERK + head sampling, walk-then-rotate | jerks present; **rotation-only walk → clean** (user tested ~1 min) | The effect is **translation-only** — the signature of positional reprojection. |
| Count correlation | one run: **23 drops**, **6** coincided with a view-jerk >2 mm; user saw **5–10 pops** | **6 ≈ 5–10** → the pops are the **subset of drops that land while the head is translating**. |

**What we learned, distilled:**
1. Frame delivery is essentially perfect in steady state; the stutter is **rare dropped frames**, not chronic overrun.
2. **Our code is exonerated** — transforms flat, tick self-time ~0.2 ms, render continuation bounded (~5 ms), `rebuilds=0`. Repeatedly, across multiple hypotheses.
3. The visible pop is **`dropped frame × head translation at that instant`**: rotation reprojects cleanly, translation doesn't (needs parallax/depth), so only drops-during-translation are seen.

---

## 4. The two distinct residual artifacts

1. **Walking micro-stutter ("flick out and back").** Dropped-frame **positional reprojection** during
   translation. Platform/WebXR characteristic; not our code (§3). This is the main open item.
2. **~1 cm wall-hunting jitter** (separate). While walking, the group-relay re-lays every wall to its **raw**
   (sensor-noisy) pose whenever one crosses the gate → walls ease between values ~1 cm apart (below the 2 cm
   gate). The slew smooths each hop but the **target is noise**, so walls gently swim. Attacking this means
   **denoising the raw wall pose** (temporal EMA per surface) so the target is stable, and/or solving content
   against the rendered (not raw) poses. Not attempted.

---

## 5. Remaining theories and likelihood

For the walking micro-stutter (the drops themselves; `self ≈ 0.2 ms`, so external to our JS):

| Theory | Likelihood | Notes / how to test |
|---|---|---|
| **Inherent WebXR translation reprojection** (a dropped frame *always* pops during translation because WebXR gives rotation-only reprojection) | **High** — best fit for all data | Confirmed indirectly by rotation-clean + count match. The *symptom* fix (positional reprojection) needs depth/motion-vector submission — see below. |
| **GC pauses** causing the drops | **Medium** | Plausible (capture body allocates heavily every 0.5 s), but **unconfirmable on Quest** (`performance.memory` frozen). Lever: reduce per-capture allocation churn (pool `THREE` temporaries) and **fix-and-measure vs the drop count**. Not attempted. |
| **OS / compositor / thermal single-event stalls** | **Medium** | Some residual drops are one-off 40–50 ms frames unrelated to captures. Largely outside our control. |
| **GPU draw cost** (full-res, `foveationLevel:0`) | **Low–Medium (partly real)** | Foveation demonstrably cut the rate, so this contributed to the *bursts* — but 0.5 didn't eliminate residual isolated drops. |
| **Our capture / slew / content / render-transform code** | **Very low — effectively ruled out** | Exonerated by tick self-time, flat pose rings, `rebuilds=0`, bounded COST. |
| Mesh rebuilds / group-relay churn | **Ruled out** | `rebuilds=0`; `--group-surface-relay off` made no difference. |

**The symptom-fix that would actually help (option "depth/positional reprojection"):** give the compositor
per-pixel **depth** (or motion vectors) so a dropped frame reprojects **translation** correctly instead of
popping. Native Quest apps get this via **Application SpaceWarp** (`XR_FB_space_warp`, OpenXR). **In WebXR
this is very likely not exposed** — default WebXR is color-only (rotational reprojection); the `depth-sensing`
we already request is real-world depth for *occlusion*, not reprojection; and there's no WebXR motion-vector
path. The only maybe is the **WebXR Layers API** (`XRWebGLBinding` projection layer with a depth attachment)
*if* Oculus Browser consumes it for reprojection — undocumented/unverified. **Verify before promising it.**

---

## 6. If picking this up again — recommended order

1. **Build the MARKER probe** (§2) — press-when-you-see-it. Ends all correlation-by-inference.
2. **Verify WebXR positional-reprojection availability** — probe `XRWebGLBinding`/projection-layer depth,
   read Oculus Browser release notes for spacewarp/depth-reprojection, and empirically test whether a
   depth-attached layer collapses the **jerk magnitude on dropped frames** (drop count unchanged). This
   decides whether the *symptom* is fixable at all in-browser.
3. **Allocation-churn pass** — pool the per-capture `THREE` temporaries; re-measure the **drop count** (was
   23/run) to see if GC-induced drops fall. The one lever that's purely in our hands.
4. **Decide the `--foveation` default** — a human visual call (smoothness vs peripheral sharpness); data says
   0.5 is meaningfully smoother, 0.3 a balance, 0 sharpest.
5. **Wall-hunting jitter (§4.2)** — temporal EMA on raw wall poses, or solve content against rendered poses.

---

## 7. Commit map

**All merged to `main`** across three merges — the branch is fully integrated and done.

- `da1931e` (merge): `advanceSig` seam fix (`1b25a5f`), `sealWalls` + group-relay B + `--wall-seal-tol`
  (`6569eca`), 2 mm fill weld + `--surface-weld` (`757df6d`), pipeline-guard tests (`cfa5328`), `[geoslow]`
  probe (`d2b68c3`), pose-smoothing design doc (`a769497`).
- `ecae610` (merge): pose-smoothing Phase 1 slew + `--pose-tau` (`04cc058`), rolling frame-pacing report
  (`23d3c19`).
- `789f2bb` (merge): content apply-gate deadband (`c11d92b`), per-late-frame forensics + JS heap sampling
  (`3207a24`), tick self-time (`e0cb34f`), `--foveation` knob (`fc24b01`), camera-jerk probe + mesh-rebuild
  counter (`abe7a18`), and this journey doc (`0159f38`).

**Tests added:** `advanceSig` regression tests + slew-math tests (`tests/js/world-model.test.js`);
`joinCorners`/`sealWalls`/`snapInsets` pipeline-composition guards (`tests/js/room-snap.test.js`). Full suite
green at merge (97 JS + 318 Python).

---

## 8. One-line takeaway

We fixed the real, fixable pops (seams, cracks, content shimmer) and built a thorough frame-diagnostic
toolkit; the remaining "walking micro-stutter" is **dropped-frame positional reprojection during translation
— a WebXR/Quest platform limit, not our code** — and the only clean symptom-fix (positional reprojection via
depth) is probably not reachable from the browser.
