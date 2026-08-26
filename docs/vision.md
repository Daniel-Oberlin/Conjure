# Vision

## The one-liner

Conjure is a personal holodeck: you *talk* to it, and it *builds* an interactive VR
world around you in real time. You describe a scene; it appears. You ask for changes;
it edits. You give the world rules and behaviors; it makes them happen. It remembers
every world you've made and can weave them together.

## What it feels like to use

1. You put on the Quest 3 and open a Conjure world (a WebXR page) in the headset.
2. You speak: *"Put me on a beach at sunset, with a campfire and gentle waves."*
3. The world assembles — progressively, narrating as it goes ("fetching driftwood,
   lighting the fire...") — placeholders first, refined as assets arrive.
4. You iterate by voice: *"Make the fire bigger. Add a hammock between those two palms.
   When I clap, launch fireworks."*
5. Later: *"Take me back to the mountain cabin I made last week,"* or *"Connect this
   beach to the cabin with a door."*

## Experiences (agents) & personas

Conjure isn't a single experience — it's a **host for many**, each a different way of being in the
headset, with its own toolset and its own world. We call an experience an **agent** — less a
"do-my-tasks" AI agent, more a **mode of play** you step into:

- **Builder** — the foundational agent: conversational world-building and editing (everything in *What
  it feels like to use*, above). Its job is to let you customize worlds for yourself.
- **Immersive DJ** — plays music and builds the room around it: album art, lyrics, fun facts, and an
  environment that suits the track.
- **Dungeon Master** — runs a tabletop-style role-playing game, generating immersive content and
  environment live as the story unfolds.
- **Planetarium operator** — moves the sky view, narrates the night, and shows immersive stellar visuals.

Within an experience there can be **personas** — characters who *participate* rather than operate. A
persona has its own voice (prompt) and brain (LLM) and sees the shared context when it's given a turn,
but it holds **no tools**: it speaks and intends, and the agent acts on its behalf with the tools at
hand. An NPC in the Dungeon Master's game is a persona; the DM gives it a turn, then realizes whatever it
does in the world.

Each agent gets its **own world**, kept separate from the others, and you can **switch between
experiences mid-session** — DJ, then a quick world-build, then back into the campaign — without losing
any of them. (Saving worlds *across* sessions comes later — see *Persistent, connectable memory* below.)
The deterministic plumbing that makes switching reliable, and the scoping that gives each experience
exactly the powers it needs, are specified in [specs/agents.md](./specs/agents.md).

## Who it's for

Primarily a **single creator/explorer** (you), experimenting with conversational world-
building. Multi-user/shared presence is a possible future, called out as a decision so
the architecture doesn't foreclose it.

## Goals

- **Voice-first creation.** Natural, conversational building and editing — not menus.
- **Real-time, in-headset feedback.** Changes appear live in the world you're standing in.
- **Persistent, connectable memory.** Worlds are saved, searchable by description, and
  linkable to each other (portals).
- **Dynamic behavior.** Worlds can have rules and reactive logic, authored by voice and
  run live ("when X, do Y").
- **Multimodal & resourceful.** Generate images/audio/3D via other models; **generate meshes**
  when nothing fits; find and reuse free existing content from the web; convert formats as needed.
- **Many AIs in one conversation.** Several LLMs available in a single session, each with a casual
  name you give it ("Gemini", "Chat"); ask to talk to a different one and it switches. The
  conversation remembers **who said what**, so the AIs can react to and build on each other.
- **Image up-res & outpainting.** Enhance images (super-resolution) and **paint beyond their
  edges** to turn an ordinary photo into an immersive skybox or wrap-around panorama.
- **Extensible audio engine.** A plugin-based audio engine — play audio files, **synthesize sound
  programmatically**, spatialize it — extensible with new audio sources and effects.
- **VR *and* AR.** Build immersive VR worlds or place content into your real room via passthrough.
- **Embodiment & vehicles.** Occupy an avatar, or climb into a vehicle — car, tank, plane, hot-air
  balloon — each with its own way of moving and being driven.
- **Bring your own controls.** Pluggable input devices — Quest controllers and hands, Bluetooth
  gamepads, and USB peripherals on the computer (flight yokes, rudder pedals, trackballs) — mapped
  to in-world actions.
- **Co-located multi-user.** Several people in one room, each in their own headset, share a world
  and see the same things in the same physical spots.
- **Extensible by MCP modules.** New powers plug in as modules — a NAS photo library, an
  interactive-fiction engine, web asset sources — without touching the core.
- **Cross-platform & modest hardware.** Server runs on Raspberry Pi, Mac, or Linux.
- **Works on any WebXR device.** Quest-specific features are extensions, never requirements.
- **Comfortable & safe.** Respects scale, comfort, and the play boundary.

## Non-goals (at least initially)

- Not a AAA game engine or a photorealistic renderer. WebXR-browser fidelity.
- Not a hand-modeling tool — we *generate, assemble, and place* content, we don't offer a
  sculpting UI.
- Not *remote* multiplayer yet (co-located is in; the remote voice bridge is a kept-open future).
- Not an app-store native app — it's WebXR delivered through the browser.

## Future possibilities (kept open, not excluded by the architecture)

Not v1 commitments — but the architecture is deliberately built so these stay reachable
(see *Forward-compatibility* in [architecture.md](./architecture.md)):

- **AR in your actual home.** Mount **portals on your real walls** and place objects in your real
  rooms **persistently** — still there next session, anchored to the physical space. (Rests on
  persistent real-world anchors as an AR extension, and anchor-relative placement in the world
  model.)
- **Live video on portals.** A portal — or any surface — showing a **live video feed** instead of
  a rendered world, via a surface that can bind to a streaming media source.
- **Remote sessions as modules.** A module that streams a **remote computer's screen** onto a
  surface in the world (and could forward your input back) — an experience module handing back a
  live stream + input channel.
- **Remote multiplayer.** The voice-bridge future from the multi-user design (off-site
  participants joining a shared world).

## Guiding principles

- **Declarative world state is the source of truth.** Everything — LLM, server, headset —
  agrees on one serializable scene representation.
- **The LLM proposes; the system constrains.** Performance budgets, content safety, and
  sandboxing are guardrails the LLM operates within, not afterthoughts.
- **Degrade gracefully.** Slow asset fetches show placeholders; a missing model falls back
  to a primitive; the experience never hard-stalls.
- **Everything is recoverable.** Voice editing is fuzzy, so undo/redo and world versioning
  are first-class.
- **A host for modules.** Conjure's power grows by adding MCP modules, not by growing a
  monolith. Agents client into scoped MCP servers (above a deterministic shell); the core stays small.
- **Vendor-neutral baseline, extensions on top.** It runs on any WebXR device; device-specific
  features light up when present and fall back gracefully when not.
