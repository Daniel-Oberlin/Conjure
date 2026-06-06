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

## Status

🏗️ **Design stage.** Nothing built yet, but the big architectural decisions (10 of 12)
are settled and `architecture.md` is now a real v1 design. The remaining open questions
are all phase-time and non-blocking (marked `❓ OPEN` in the spec, tracked in `decisions.md`).
Next: lock the Phase-0 contracts (world-document schema, patch protocol, state channel) and
get a static A-Frame scene onto the Quest over TLS.
