> **Private notes from your human collaborator — LLMs disregard.**

## AR

- Windows, art, doors are fighting with the wall planes.  We need to handle them gracefully.
 - Snap the doors and windows to the walls that they are almost a part of, for a door, embed it as an opening and make it transparent.  Windows are not transparent by default. Hide the additional lines from subdividing by default (we can show them later).
 - Wall art should be pushed away from the wall by a centimeter so that it doesn't fight with the wall.
 - We will do similar things with meshes in the future (hide show the internal lines of the polygons), so account for this in the design.
- Make a bounding box around the outside of the rooms so that there is no leakage from the real world.  This can be hidden or colored separately.
- Hide / show / decorate walls with images, etc.
- Show compass somehow - place a compass on the ceiling above me - renders with lines, arrow towards north.

## Performance

- Can we somehow inject the world view into the prompt before each query to the LLM so that we save a call to get the world?  Would that make a difference?

## Web

- Perform web requests
- Fetch images or other content from the web

## Visual

- Animation for pending box (sending dynamic code to the headset)
- Skyboxes should be set at ground level, not in mid-air
- Dynamic code should be deterministic and anchored to a precise timestamp for consistency across multiple headsets
- Sky dome
- Panorama photo walls with optional sky
- Floor

## Host Modes

- Headset
- Drone (use drone controls)
- Walker (movement bound to floor, maybe use second stick for view)
- Fixed Camera
- Vehicle
- etc.

## Dynamic content
- Keep store of created content, version controlled?
- Water picture
- Solar system animation
- Live webcam video
- Terminal shell with voice
- X11 window

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

## Utility

- Logging and instrumentation so that we can profile and see what's taking the time
- Maybe this is done in the CLI, maybe you can query the logs in the CLI with natural language

