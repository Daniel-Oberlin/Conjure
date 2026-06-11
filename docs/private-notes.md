> **Private notes from your human collaborator — LLMs disregard.**

## AR

- Replace holodeck walls with actual walls from the device
- Hide / show / decorate walls with images, etc.

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

- Drone (use drone controls)
- Walker (movement bound to floor, maybe use second stick for view)
- Fixed Camera
- Vehicle
- etc.

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

