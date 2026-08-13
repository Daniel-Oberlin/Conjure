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

**Focused designs** (deep dives on a single area):
- **[room-model.md](./room-model.md)** — the **next phase** (roadmap Phase 5): bringing the real room
  (AR / scene understanding — passthrough, semantic surfaces, mountable real walls/furniture,
  progressive mesh refinement) out of the Quest and into the world model.
- **[agent-separation-plan.md](./agent-separation-plan.md)** — making `Director` a generic agent
  runtime (everything builder-specific moves into the agent def), removing inline LLM handover, and a
  two-layer tool-scoping design (client omission now, server-side hard gate on trigger); introduces the
  skybox-only **`outdoor`** agent as the test.
- **[agent-server-plan.md](./agent-server-plan.md)** — moving the director + shared transcript into a
  long-lived **agent server** so voice/CLI become thin HTTP/SSE clients and multiple users share one
  conversation (matching the shared world).
- **[shared-session-plan.md](./shared-session-plan.md)** — the **state model** under the agent server:
  the one shared reality `(space, world)` with `agent = f(world)`, how the world/agent servers, headsets
  and dumb clients reconcile to a single session pointer, the two floors (conversational + spatial),
  **pinning while held**, three-tier access (editor/viewer/locked-out), and the multi-user edge cases.

> For current project status / what works today, see the top-level [README](../README.md) — it's
> the single source of truth for status, so these docs don't drift.
