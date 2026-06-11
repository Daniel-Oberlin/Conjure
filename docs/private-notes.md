> **Private notes from your human collaborator — LLMs disregard.**

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

## Visual

- Animation for pending box (sending dynamic code to the headset)
- Skyboxes should be set at ground level, not in mid-air
- Dynamic code should be deterministic and anchored to a precise timestamp for consistency across multiple headsets
- Sky dome
- Panorama photo walls with optional sky
- Floor

## AR

- Replace holodeck walls with actual walls from the device
- Hide / show / decorate walls with images, etc.

## Web

- Perform web requests
- Fetch images from the web

## Host Modes

- Drone (use drone controls)
- Walker (movement bound to floor, maybe use second stick for view)
- Fixed Camera
- Vehicle
- etc.

## Models

- Abstract model provider with capabilities
