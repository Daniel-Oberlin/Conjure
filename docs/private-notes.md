> **Private notes from your human collaborator — LLMs disregard.**

# Agent refactor
 - Are contexts persistent?
 - sessions (have context, worlds, public/private)
 - Do we want timestamps for entries?
 - Persistence for other info
 - Constructor
  - agent initializer/constructor, setup state in persistent store?
    - agent store is segregated by user
    - agent has global store /agents/builder/
    - agent has user store /user/agents/builder/
    - do we need /system, /users, /agents as top level?
- /users/daniel/agents/builder/worlds
- /users/daniel/spaces/home
- agent semantic versioning, upgrade path (default purge)?

# CLI refactor
- Sytax
  - new (private) session (name)
  - change/switch agent/session/llm to (something)
  - rename (current session/world) or session "name" to "new name"
  - list/ls agents/sessions/llms/spaces
- dir paths /user/agent/worlds/..., /user/spaces...
- Multi-paned CLI tool
  - Launch servers, restart
  - Launch clients via name (easy select previosu)
  - up/down arrows go to different panes
  - expand/collapse
- Up/down arrows for history?

# Other
- Global settings /system/globalsettings (default info color)
- space visibility (public access, public create worlds)

# Outdoor agent

# Dynamic content
- Dynamic modules
  - User provided
  - LLM provided
- Events
  - From user (click, etc.)
  - From other sources (music player - change key, tempo, etc.)
- State variables accessible by LLM (per dynamic module)
- Agent can emit events to affect modules
- Maybe events only (event driven architecture)?
- Fireflies
- Point clouds
- Water picture
- Model animations via glTF/VRM

# Graphics
- Lighting
- billboards

# Web
- Perform web requests
- Fetch images or other content from the web

# Utility
- General status area to top - clock on right with adjustable widgets:
 - Clock (right default)
 - Date (right default)
 - Agent:LLM (right)
 - Status: Loading skybox, Loading image, Idle (left)
 - compass (center)
 - world "/scoped/builder/firstroom"
 - show or hide
 - conjure show status, conjure hide clock, conjure clock center

# Host Modes
- How to reconcile host movement with real movement?
 - in host mode, the host is attached to your headset?
- Headset
- Drone (use drone controls)
- Walker (movement bound to floor, maybe use second stick for view)
- Fixed Camera
- Vehicle
- etc.

# Front end bugs
- Pops you out of VR when starting cold (headset restart, new domain name?)
- Test for new domain name trigger (retstart tunnel and see if it happens)
- Related to permissions?  Ask permissions at the beginning?
- Can we catch errors in JS, post them to server for logging, then exit (for visiblity)?

# Dynamic content
- What do we call dynamic modules?
- Animation for pending box sending dynamic code to the headset- Dynamic code should be deterministic and anchored to a precise timestamp for consistency across multiple headsets
- Keep store of created content, version controlled?
- Milkdrop style animations on ceiling, in stereo, planetarium style or in front of you
- Solar system animation
- Live webcam video
- Terminal shell with voice
- X11 window
- Album covers and art

# Environment
- Generalize under "Environment Map" concept
- Implement skydome, panorama, holodeck, containing box

# Applications
- What are applications?
- LLM app (agent), world app (world module), headset app (headset module) (can share a namespace)
- Infocom Zmachine
- "persona" is like an agent, but only a prompt and access to context, like a participant in a role playing game

# Persistance
- Preferences
 - colors
- Dymamic module store

# CLI Client
- Don't hard-code deterministic commands — query the MCP server to build the list dynamically
- Add a `help` command that displays available commands (decoupling)

# 3D Models
- Abstract model provider with capabilities
- Smithsonian and other sources
- Point clouds

# Deployment and infrastructure
- Put into container?
- Discovery for MCP servers?
