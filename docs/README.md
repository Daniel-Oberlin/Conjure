# Conjure Docs

Conjure is a voice-driven "holodeck": describe a scene aloud and an LLM builds an
interactive WebXR world you experience on a Meta Quest 3 — with persistent memory,
dynamic behavior, and the ability to pull in generated or pre-existing web content.

## Reading order

1. **[vision.md](./vision.md)** — the north star: what Conjure is, who it's for, goals & non-goals.
2. **[spec.md](./spec.md)** — functional spec: capabilities, requirements, and open questions.
3. **[architecture.md](./architecture.md)** — the v1 design: concrete contracts, runtime, channels, trust model.
4. **[roadmap.md](./roadmap.md)** — phased path from prototype to full system.
5. **[decisions.md](./decisions.md)** — log of consequential forks and what we chose & why.
6. **[providers.md](./providers.md)** — provider & module registry: chosen defaults + future options per swappable slot.
7. **[setup.md](./setup.md)** — prerequisites & onboarding: what installs automatically vs. by hand, the doctor check.
8. **[testing-on-quest.md](./testing-on-quest.md)** — exact steps to run it in the Quest 3 headset (USB).
9. **[https-setup.md](./https-setup.md)** — go wireless: serve over HTTPS (cloudflared / Caddy / Tailscale).
10. **[testing.md](./testing.md)** — automated testing strategy (proposal, for review).

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
- **[specs/occlusion.md](./specs/occlusion.md)** — real-world depth occlusion: the global depth pre-pass
  and the `off`/`hands`/`hands-solid` modes. ([backlog](./backlogs/occlusion.md))

**Focused designs** (deep dives on a single area — older plan-shaped docs, migrating to the above):
- **[room-model.md](./room-model.md)** — the **next phase** (roadmap Phase 5): bringing the real room
  (AR / scene understanding — passthrough, semantic surfaces, mountable real walls/furniture,
  progressive mesh refinement) out of the Quest and into the world model.
*(`agents.md`, `agent-separation-plan.md`, `agent-server-plan.md`, `sessions-plan.md`,
`session-scoping-plan.md` and `shared-session-plan.md` were consolidated into
[specs/agents.md](./specs/agents.md) + [backlogs/agents.md](./backlogs/agents.md) on 2026-08-25.)*

> For current project status / what works today, see the top-level [README](../README.md) — it's
> the single source of truth for status, so these docs don't drift.
