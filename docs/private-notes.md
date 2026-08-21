> **Private notes from your human collaborator — LLMs disregard.**

# CLI refactor
- Sytax
  - new (private) session (name)
  - change/switch agent/session/llm to (something)
  - rename (current session/world) or session "name" to "new name"
  - list/ls agents/sessions/llms/spaces
- dir paths /user/agent/worlds/..., /user/spaces...
- Up/down arrows for history (goes in CLI tool only?)

# Other
  - Global settings /system/globalsettings (default info color)
  - space visibility (public access, public create worlds)

# Dynamic content
- Fireflies
- Point clouds
- Model animations via glTF/VRM
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
  - Water reflection lighting
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
