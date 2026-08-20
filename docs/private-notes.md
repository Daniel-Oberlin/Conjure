> **Private notes from your human collaborator — LLMs disregard.**

# CLI refactor
- Sytax
  - new (private) session (name)
  - change/switch agent/session/llm to (something)
  - rename (current session/world) or session "name" to "new name"
  - list/ls agents/sessions/llms/spaces
- dir paths /user/agent/worlds/..., /user/spaces...
- Up/down arrows for history (goes in CLI tool only?)

# Menu-driven CLI tool
  - Completely agnostic of Conjure
  - Top level: launch servers, restart, shutdown/exit
  - Add launch modules which are persisted
    - Name and CLI launch string
    - Launch by default (toggle on/off in submenu)
    - Submenu (open with RETURN, show text window, startup/shutdown, toggle default on/off, edit, delete)
    - Pressing ENTER on launch module enters the interactive CLI window for the module with highlighted top line identifying the module/session
    - ON -> GREEN, OFF -> RED, OFF (not on by default) -> Yellow
  - ESC goes back to home menu
  - Store config in .runner file (search up from current directory until home directory)

# Other
  - Global settings /system/globalsettings (default info color)
  - space visibility (public access, public create worlds)

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
- Animation for pending box sending dynamic code to the headset
- Dynamic code should be deterministic and anchored to a precise timestamp for consistency across multiple headsets
- Keep store of created content, version controlled?
- Milkdrop style animations on ceiling, in stereo, planetarium style or in front of you
- Solar system animation
- Live webcam video
- Terminal shell with voice
- X11 window
- Album covers and art

# Graphics
  - Lighting
  - Panoramic photo

# Audio
  - Quest streaming
    - User audio separation in same room
  - Get better clarity for speech-to-text

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

# 3D Models
- Smithsonian and other sources
- Point clouds
- Gaussian splats
