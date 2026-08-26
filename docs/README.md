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
6. **[setup.md](./setup.md)** — prerequisites & onboarding: what installs automatically vs. by hand, the doctor check.
7. **[testing-on-quest.md](./testing-on-quest.md)** — exact steps to run it in the Quest 3 headset (USB).
8. **[https-setup.md](./https-setup.md)** — go wireless: serve over HTTPS (cloudflared / Caddy / Tailscale).
9. **[testing.md](./testing.md)** — automated testing strategy (proposal, for review).

**Four tiers, four names.** `vision.md` is intent; `architecture.md` is the cross-cutting design and
how much of it is real; [`specs/`](./specs/) is what is built; [`backlogs/`](./backlogs/) is what is
not. `decisions.md` records the forks. Nothing is called "spec" except the living specs.

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
  shared clock/bus/pointer runtime, XR actions and pointer arbitration, placement, and the tier-C commit
  path. ([backlog](./backlogs/dynamics.md))
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
- **[specs/occlusion.md](./specs/occlusion.md)** — real-world depth occlusion: the global depth pre-pass
  and the `off`/`hands`/`hands-solid` modes. ([backlog](./backlogs/occlusion.md))

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
`investigations/`, on 2026-08-26.)*

> For current project status / what works today, see the top-level [README](../README.md) — it's
> the single source of truth for status, so these docs don't drift.
