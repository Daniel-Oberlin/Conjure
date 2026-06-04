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

## Status

🏗️ **Design stage.** Nothing built yet, but the big architectural decisions (10 of 12)
are settled and `architecture.md` is now a real v1 design. The remaining open questions
are all phase-time and non-blocking (marked `❓ OPEN` in the spec, tracked in `decisions.md`).
Next: lock the Phase-0 contracts (world-document schema, patch protocol, state channel) and
get a static A-Frame scene onto the Quest over TLS.
