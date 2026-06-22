> **Private notes from your human collaborator — LLMs disregard.**

# Architecture
- Namespace for assets, collections, data, etc: "/scoped/builder", "/global/music-collection/"

# Graphics
- Lighting

# Utility
- Show "Loading image..." at top of page, pulsing between transparent and info color
- Logging everything (with switch) for debugging
- Turn on optional timestamps to CLI messages
- Logging and instrumentation so that we can profile and see what's taking the time
- Maybe this is done in the CLI, maybe you can query the logs in the CLI with natural language
- Compass?

## Performance & Bugs
- I can sometimes hear internal thinking "let me check the room data", "

## Web
- Perform web requests
- Fetch images or other content from the web

## Host Modes
- How to reconcile host movement with real movement?
 - in host mode, the host is attached to your headset?
- Headset
- Drone (use drone controls)
- Walker (movement bound to floor, maybe use second stick for view)
- Fixed Camera
- Vehicle
- etc.

## Dynamic content
- Fireflies
- What do we call dynamic modules?
- Animation for pending box sending dynamic code to the headset- Dynamic code should be deterministic and anchored to a precise timestamp for consistency across multiple headsets
- Keep store of created content, version controlled?
- Water picture
- Milkdrop style animations on ceiling, in stereo, planetarium style or in front of you
- Solar system animation
- Live webcam video
- Terminal shell with voice
- X11 window
- Point clouds
- Album covers and art

## Environment
- Generalize under "Environment Map" concept
- Implement skydome, panorama, holodeck, containing box

## Applications
- What are applications?
- LLM app (agent), world app (world module), headset app (headset module) (can share a namespace)
- Infocom Zmachine
- namespace for worlds: "/worlds/", agents, builders can be scoped to subdirectories
- "persona" is like an agent, but only a prompt and access to context, like a participant in a role playing game

## Persistance
- Preferences
 - colors
- Asset store
- Worlds store
 - Different worlds can map onto the same location
- Dymamic module store

## CLI Client
- Don't hard-code deterministic commands — query the MCP server to build the list dynamically
- Add a `help` command that displays available commands (decoupling)

## 3D Models
- Abstract model provider with capabilities
- Smithsonian and other sources
- Point clouds

## Depolyment and infrastructure
- Put into container?
- Discovery for MCP servers?
