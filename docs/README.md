# Conjure Docs

Conjure is a voice-driven "holodeck": describe a scene aloud and an LLM builds an
interactive WebXR world you experience on a Meta Quest 3 — with persistent memory,
dynamic behavior, and the ability to pull in generated or pre-existing web content.

## Reading order

1. **[vision.md](./vision.md)** — the north star: what Conjure is, who it's for, goals & non-goals —
   then *Capabilities in detail*, the same ambition at requirements resolution.
2. **[architecture.md](./architecture.md)** — the v1 design: concrete contracts, runtime, channels, trust model.
3. **[roadmap.md](./roadmap.md)** — phased path from prototype to full system.
4. **[decisions.md](./decisions.md)** — log of consequential forks and what we chose & why.
5. **[providers.md](./providers.md)** — provider & module registry: chosen defaults + future options per swappable slot.
6. **[running.md](./running.md)** — the runbook, in the order you hit it: install → run → into the
   headset over USB → untethered over HTTPS.
7. **[testing.md](./testing.md)** — automated testing strategy (proposal, for review).

**Four tiers, four names.** `vision.md` is intent; `architecture.md` is the cross-cutting design and
how much of it is real; [`specs/`](./specs/) is what is built; [`backlogs/`](./backlogs/) is what is
not. `decisions.md` records the forks. Nothing is called "spec" except the living specs.

**And one that is meant to go away.** [`plans/`](./plans/) holds a plan while it is being executed —
a single sequence across areas the specs and backlogs deliberately keep apart. A plan names its
dissolution target on the way in and is deleted when its last phase settles: finished work to
`specs/`, abandoned or deferred work to `backlogs/`, forks to `decisions.md`. Ten `*-plan.md` files
lived at this level before and were consolidated away in August 2026 (see the note below); they
earned their keep while live, and what went wrong was only that nothing said when they should end.
A plan that outlives its phases has become a backlog with worse organisation — dissolve it
([decisions.md §25](./decisions.md)).

## Specs and backlogs

Going forward, each area gets a **pure living spec** in [`specs/`](./specs/) — what is built and how it
behaves today, kept accurate against the code — and a matching **backlog** in
[`backlogs/`](./backlogs/) for unfinished work, future directions, and known problems. Rejected
alternatives and the reasoning behind consequential forks stay in [decisions.md](./decisions.md). The
split exists so a spec can be trusted: nothing in it is a plan, an intention, or a maybe.

- **[specs/agents.md](./specs/agents.md)** — the orchestration layer: the agent definition and two-layer
  tool scoping, the `Director` turn loop and prompt injection, the shell's command registry and
  namespace, sessions (disk layout, state store, constructor), the agent server's protocol and its
  follow loop, and the one-shared-reality permission model. ([backlog](./backlogs/agents.md))
- **[specs/dynamics.md](./specs/dynamics.md)** — dynamic modules: the manifest, the client contract, the
  shared clock and event bus, placement, and the tier-C commit path. Input moved out to `specs/input.md`.
  ([backlog](./backlogs/dynamics.md))
- **[specs/input.md](./specs/input.md)** — **XR input, as actions**: the one reader of the XR frame,
  the control vocabulary and the binding table that keeps buttons out of module code, what a pointer
  carries, `armed()` as the single definition of "in use", and the capture/reservation arbitration that
  lets two consumers share a trigger. Its own area because its consumers are not all modules — the
  beams, the gaze picker and the surface overlay read it too ([decisions.md](./decisions.md) §29).
  ([backlog](./backlogs/input.md))
- **[specs/spaces.md](./specs/spaces.md)** — the **space**: the persistent record of a real physical
  environment (surfaces, boundary, geolocation, owner), how a headset decides which space it is standing
  in, admission tiers, the occupancy claim, authority vs. edit rights, and co-location.
  ([backlog](./backlogs/spaces.md))
- **[specs/spaces-geometry.md](./specs/spaces-geometry.md)** — where a surface **is** and **which** it is:
  the non-rigid map and why geometry is never shared, frames and normal conventions, consensus
  registration, plane-relative anchors and placement modes, shell geometry, and keeping the capture off
  the frame budget. ([backlog](./backlogs/spaces-geometry.md))
- **[specs/worlds-surfaces.md](./specs/worlds-surfaces.md)** — how a **world presents** a space: real
  surfaces as ordinary entities, the base-plus-override styling split, the two immersion axes, the
  director's surface tools, and openings and edges. ([backlog](./backlogs/worlds-surfaces.md))
- **[specs/library.md](./specs/library.md)** — the **asset catalog**: keying on intent rather than
  output bytes, the core+attributes record, one write-through for every ingest path, staged search
  (alias → exact → FTS → vector) and its confidence tier, the scope predicate that walls agents off from
  each other, and the embedder. ([backlog](./backlogs/library.md))
- **[specs/figures.md](./specs/figures.md)** — **figures**: rigged humanoids, and the per-model
  vocabulary that makes them posable — what import extracts from a GLB, the discovery layers that
  recover a semantic bone map and the validator that gates them, the anatomical frame and joint limits,
  life-size placement, the catalog revision stamp, and the runtime `figure`, `figure-parts` and
  `figure-clip` components. ([backlog](./backlogs/figures.md))
- **[specs/captures.md](./specs/captures.md)** — **captures**: the pipeline from someone else's 3D web
  app to a row in the catalog — the browser extension that pulls a published build, the reconstruction
  that rejoins materials a PlayCanvas export split apart, the import that files the result with its
  relations, and the headless Blender path beside it. Each stage fails in ways the next cannot detect,
  which is why they are specified together. ([backlog](./backlogs/captures.md))
- **[specs/occlusion.md](./specs/occlusion.md)** — real-world depth occlusion: the global depth pre-pass
  and the `off`/`hands`/`hands-solid` modes. ([backlog](./backlogs/occlusion.md))
- **[specs/voice.md](./specs/voice.md)** — the **ears-and-mouth front-end**: the thin-client contract
  that puts voice and the CLI in one conversation, the PipeCat pipeline, VAD and the two silence
  thresholds, the Whisper model and why it was chosen by measurement, Kokoro TTS, why barge-in is off,
  and the mic-activation gate and its enforced separation from the shell's wake word.
  ([backlog](./backlogs/voice.md))
- **[specs/config.md](./specs/config.md)** — the **installation layer**: the three XDG roots and the
  precious/disposable split, how every location resolves (env > `settings.json` > default), what
  `settings.json` owns versus `.env`, the user-first search paths that let a user add or shadow an agent
  or a dynamic module, and how tests stay off a real home. ([backlog](./backlogs/config.md))

The three space specs form a stack, and the prefixes say which layer you are in: `spaces` is the
*record*, `spaces-geometry` treats a surface as *geometry to locate*, `worlds-surfaces` treats it as
*content to style*.

## Investigations

[`investigations/`](./investigations/) holds the durable record of a debugging campaign — the symptom,
the experiments, what each proved, and above all what was **tried and rejected**. A spec deliberately
drops that and a backlog won't hold it, but it is the knowledge that stops a dead end being re-proposed.

*(Consolidated: `agents.md`, `agent-separation-plan.md`, `agent-server-plan.md`, `sessions-plan.md`,
`session-scoping-plan.md`, `shared-session-plan.md` → [specs/agents.md](./specs/agents.md) on
2026-08-25. `room-model.md`, `space-model.md`, `spaces-and-users-plan.md`, `new-space-flow.md`,
`local-first-geometry.md`, `co-location-plan.md`, `pose-smoothing-plan.md` and `persistence-model.md` →
the three space specs above, with `pops-and-jitters-journey.md` and `wall-art-45-flip.md` moving to
`investigations/`, on 2026-08-26. `spec.md` → [vision.md](./vision.md); `setup.md`,
`testing-on-quest.md`, `https-setup.md` → [running.md](./running.md); `asset-library-plan.md` →
[specs/library.md](./specs/library.md) + its backlog; and the flat `backlog.md` + `known-issues.md`
distributed into the per-area backlogs — all 2026-08-26. `user-home-plan.md` →
[specs/config.md](./specs/config.md) + its backlog, with the layout forks to
[decisions.md](./decisions.md) §21, on 2026-08-27. `specs/dynamics.md` §6's XR-input half →
[specs/input.md](./specs/input.md) + its backlog, with the fork at
[decisions.md](./decisions.md) §29, on 2026-09-17 — a split rather than a consolidation, and the
first one: the layer's consumers had stopped being modules.)*

> For current project status / what works today, see the top-level [README](../README.md) — it's
> the single source of truth for status, so these docs don't drift.
