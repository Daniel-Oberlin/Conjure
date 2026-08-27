> **Private notes from your human collaborator — LLMs disregard.**

# Bugs

# Surfaces
- Still see occasional disappearance of some surfaces
- The bedroom floor is occasionally elevated for periods of time

# CLI and Shell
- Set variables with shell
  - General rework of variable scoping
    - Settable via command-line, env, shell
    - Scope: user, session, world?

# Graphics
  - Lighting
  - Water reflection lighting
  - Panoramic photo
  - What does "mesh detection" look like? (see occlusion backlog)

# Dynamic content
- Swarm
- Model animations via glTF/VRM
- Milkdrop style animations on ceiling, in stereo, planetarium style or in front of you
- Album covers and art
- Solar system animation
- Further out:
- Live webcam video
  - Terminal shell with voice
  - X11 window
  - Point clouds
  - Keep store of created content, version controlled?

# Audio
  - Special filter rule module for audio from LLM
    - Never emit asterisk (emoji's ok)
    - say "emoji" after an emoji
  - Audit shell for usability when voice is enabled
    - Never emit asterisk from shell (do something useful instead)
  - Can we reduce latency by starting speech before generation is finished?
  - Quest streaming
    - User audio separation in same room
  - Get better clarity for speech-to-text
  - Support SSML

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

# 3D Models
- Smithsonian and other sources
- Point clouds
- Gaussian splats
