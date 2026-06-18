> **Private notes from your human collaborator — LLMs disregard.**

## Environment

- Need a name for skybox, skydone, panorama, containing box, holodeck
 - Environment?  Theater?  Boundary?  Enclosure? Holodeck?
- Problem with Skyboxes, the floor is way below us
- Implement skydome, panorama, holodeck, containing box

## AR
- Make a bounding box around the outside of the rooms so that there is no leakage from the real world.  This can be hidden or colored separately. (maybe don't need this now that walls are tight?)

## Performance & Bugs

- Can we somehow inject the world view into the prompt before each query to the LLM so that we save a call to get the world?  Would that make a difference?
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

# Utility
- Compass?

## Dynamic content
- What do we call dynamic modules?
- Animation for pending box (sending dynamic code to the headset- Dynamic code should be deterministic and anchored to a precise timestamp for consistency across multiple headsets
- Keep store of created content, version controlled?
- Water picture
- Milkdrop style animations on ceiling, in stereo, planetarium style or in front of you
- Solar system animation
- Live webcam video
- Terminal shell with voice
- X11 window
- Point clouds
- Album covers and art

## Appllications
- What are applications?  Prompt + MCP application?
- LLM app (agent), world app (world module), headset app (headset module) (can share a namespace)
- Infocom Zmachine
- LLM apps can be scoped with respect to a specific world they own, or MCP access, or LLM
- conjure:claude.zork>
- conjure:gemini.builder> (this is the default app we've been working with)
- Graphic wraparound
- LLM companion
- namespace for worlds: "/worlds/", agents, builders can be scoped to subdirectories

-

## Persistance
- Preferences
 - colors
- Asset store
- Worlds store
 - Different worlds can map onto the same location
- Dymamic module store

## CLI Client

- Prompt format: `conjure:claude>`
- Don't hard-code deterministic commands — query the MCP server to build the list dynamically
- Add a `help` command that displays available commands (decoupling)

## Shell Mode

- Speaking or typing "conjure open shell" starts a non-LLM shell
- Deterministic parsing — use existing handoff logic as a model
- Extend to: limiting LLM access, removing LLM from roster, other system changes
- "exit", "close", "leave", etc. returns to normal mode
- CLI prompt: `conjure>` or `conjure:shell>`

## Models

- Abstract model provider with capabilities
- Smithsonian and other sources
- Point clouds

## Utility

- Logging and instrumentation so that we can profile and see what's taking the time
- Maybe this is done in the CLI, maybe you can query the logs in the CLI with natural language

