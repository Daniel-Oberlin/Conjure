# Real-world occlusion — the depth pre-pass

**Living spec.** Describes what is built and how it behaves today. Unfinished work and shelved
directions live in [`docs/backlogs/occlusion.md`](../backlogs/occlusion.md); the reasoning behind the
shelved `full` mode is [`docs/decisions.md`](../decisions.md) §19.

---

## 1. The problem

In AR passthrough the compositor draws the real world, then our opaque virtual layer on top — with **no
knowledge of real-world depth**. A virtual wall therefore covers your real hand, and everything virtual
floats in front of everything real.

## 2. The fix: one depth pre-pass, not per-material shaders

Once per frame, write real-world depth into the Z-buffer with **colour write off**, then render the scene
normally with depth testing on. A virtual fragment behind a real surface fails the depth test, writes no
colour, stays alpha 0 — and the compositor fills the hole with passthrough.

We seed **depth only**; the OS supplies real-world *colour* for free. There is no need to paint
passthrough ourselves.

**One integration site, not per material.** Every material — including all dynamic-content modules and
transparent content — occludes for free via ordinary depth testing. This is the load-bearing property:
**modules never opt in, and occlusion is not a per-module capability**
([`docs/specs/dynamics.md`](./dynamics.md) §1).

**Tradeoff, accepted:** a hard z-write gives hard-edged occlusion. Feathered edges would require exactly
the per-material path being avoided, so hard edges are accepted globally.

## 3. Modes

`off | hands | hands-solid`, selected by `--occlusion` → `Settings.occlusion` →
`window.CONJURE_OCCLUSION`, with a per-client URL override `?occlusion=off|hands|hands-solid` (consistent
with the existing `?stereodebug=` toggles). Resolution order is **URL override → injected default → off**;
a junk override is ignored rather than fatal.

| Mode | What the pre-pass writes |
|---|---|
| **`off`** | nothing. Virtual always over passthrough. Default. |
| **`hands`** | a filled, depth-only **hand mesh** per tracked hand. Sharp and cheap; hands occlude virtual content, nothing else does. Does not depend on the environment-depth map. |
| **`hands-solid`** | the **same** mesh drawn as opaque white — a white-glove avatar that also occludes (opaque geometry writes depth anyway). |

`hands`/`hands-solid` need `hand-tracking` requested **and** the headset actually producing hand input
sources — put the controllers down, or rely on auto-switch.

## 4. The hand mesh

One `BufferGeometry` per hand with **fixed topology**; only vertex positions are rewritten each frame, so
it costs one draw call and yields a true silhouette rather than a union of spheres.

- **Fingers** are palm-plane *ribbons*: two vertices per joint, offset ± half-width along the in-plane
  perpendicular to the bone.
- **The palm** is a triangle fan from the wrist across the thumb base and the four proximal knuckles.
- Material is `colorWrite: false, side: DoubleSide` for `hands`; opaque white for `hands-solid`.
- `renderOrder = -1000` — lay occluder depth before any content.

Two implementation constraints that are easy to get wrong:

- **Joints come from `frame.getJointPose`, not three's `getHand`** — the latter never populated joints
  under A-Frame. The scene root is pinned to the XR reference space, so a joint pose in that space maps
  1:1 into scene coordinates.
- **The mesh is added to the scene graph**, not rendered in a separate pass. three renders its own depth
  content *before* A-Frame's scene render, which clears depth (`autoClearDepth = true`) — anything drawn
  earlier is wiped. Living in the scene graph means it renders inside A-Frame's own pass, after the clear.

Verified on Quest 3.
