> **Private notes from your human collaborator — LLMs disregard.**

# Space stuff
- Figure out how to eliminate pop/jitter
  - Every 2s (when viewer location is moving) sudden world jitter - moves a short distance then returns to essentially the same position (noticable pop reverting to a much smaller move)
  - Doesn't happen much when just moving your head around but not walking
  - Is it because we are setting translating the whole world and adjusting coordinates in two separate steps during the update (resulting in smaller net visible move)?
  - If ^ is true, should we collapse translation into the coordinates?
- Should we lerp towards a windowed time-average rather than the current value
- Can we update much more frequently now since everything is local?
  - How often is quest model updated?
  - Could we achieve frame-rate updates in headset JS?
  - Tradeoff of smoothness vs. compute power
- Set command line arguments for all these things and optimize?

# Agent refactor
- agent prompt refactor (towards multi-agent)
  - roster line -> optional per-agent injection
  - identity line -> put into builder prompt (seems builder specific)
  - context -> optional per-agent injection
- dir paths /user/agent/worlds/..., /user/spaces...
- do we need a persistent store for hierarchy now, mongodb?
- agent initializer/constructor, setup state in persistent store?
  - agent store is segregated by user
  - agent has global store /agents/builder/
  - agent has user store /user/agents/builder/
  - do we need /system, /users, /agents as top level?
   - /system/globalsettings (default info color)
   - /users/daniel/agents/builder/worlds
   - /users/daniel/spaces/home
   - /agents/builder/prompt, default settings (world constructor)
- agent semantic versioning, upgrade path (default purge)?
- maintain context persistence?
- space visibility (public access, public create worlds)

# Outdoor agent

# Graphics
- Lighting

# Dynamic content
- Fireflies
- Point clouds
- Water picture

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
