> **Private notes from your human collaborator — LLMs disregard.**

# Move this file underneath config-conjure

# Bugs
  - Surfaces
    - Still see occasional disappearance of some surfaces
    - The bedroom floor is occasionally elevated for periods of time
      - This is a quest thing!  How to compensate for it?
    - Add a debug capability for seeing the quest surfaces
  - Pressing home in void world gives unexpected behavior
  - Should grab be able to orient with sticks without holding grab button (which moves also)?

# CLI and Shell
  - shell: we are inconsistent when showing names vs. slugs in the shell, for example, the worlds command shows the space of a world as the slug, but spaces shows the names of the spaces without the slugs.  IMO, slugs should be hidden from the user, only the names which can be changed should be shown - these should also be in the paths which are shown and navigated.  If someone needs to know the actual filename/path of a world, session, space, etc., there should be a file directive or command or something that makes the disk file visible.  Otherwise, it should be all names.
  - Set variables with shell
    - General rework of variable scoping
      - Settable via command-line, env, shell
      - Scope: user, session, world?

# Big ideas
- Separation of concerns architecturally
  - World server and dynamic modules can be used by other apps besides agent server
    - World server written in TS
  - Agent server can be used without world server (non-VR)
    - Have a replaceable session-resolver that decouples world-server from agent server
- Allow for Claude Code to "inhabit" Conjure
  - Tooling and awareness to allow devloping dynamic and server modules using repos

# Architectural decoupling
  - Can the agent server be formally separated from the world server so it could serve other purposes?
    - Do we need a session mediator to decouple from the world server?
  - VOX
    - Decoupled from Conjure specifically; separate project, works with other projects too
    - Different wake words for different connections
      - Connections persist until terminated with another wake word "all set" or "we're done", etc.
  - Can the world server operate separately with another app to facilitate shared experience?

# Graphics
  - Lighting
    - Water reflection lighting
    - Torch flickering lighting
  - Panoramic photo
  - What does "mesh detection" look like? (see occlusion backlog)
  - Picture frames and groups of frames that can be repopulated
  - Thumbnailwha

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
  - Can we reduce latency by starting speech before generation is finished?
  - Quest streaming
    - User audio separation in same room
  - Get better clarity for speech-to-text
  - Support SSML

# VOX
- Decouppled from Conjure specifically; separate project, works with other projects too
- Different wake words for different connections
  - Connections persist until terminated with another wake word "all set" or "we're done", etc.

# Agent & Director
- LLM routing - tool scoping to different LLMs

# LLM consensus
  - Post same thing to all LLMs, record anwsers
  - Have each LLM evaluate best response
  - Look at distribution

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

# Model Asset Sources

This document organizes practical sources for 3D models and environment assets, with notes on licensing, API access, and cost tradeoffs.

## Free and Open Sources

### 1. Khronos Sample Models (Reference glTF Assets)

Best for: testing import pipelines, materials, and rendering correctness.

- What you get: canonical glTF assets such as DamagedHelmet, FlightHelmet, and Duck.
- How it works: you can list available asset folders, then select one to retrieve a direct model download URL.
- Why use it: these are standard reference assets and are excellent for validating engine/toolchain behavior.

### 2. Smithsonian Open Access API (Real-World Objects and Historical Props)

Best for: scanned artifacts, fossils, cultural objects, and historical references.

- Licensing: Smithsonian Open Access content is broadly released under CC0/public-domain style terms.
- Access model: free official REST API with a free developer key.
- Typical workflow:
  - Search with 3D-related filters (for example, media/source fields indicating 3D content).
  - Read metadata records to locate geometry, textures, and downloadable asset descriptors.
  - Import assets commonly provided in formats such as OBJ or glTF.
- Example API pattern:
  - https://api.si.edu/openaccess/api/v1.0/search?api_key=YOUR_KEY&q=online_visual_material:true+AND+media_type:3d

### 3. OpenGameArt.org (Low-Poly and Indie-Friendly Assets)

Best for: stylized and low-poly assets that keep VR frame times stable.

- Licensing: mixed open licenses (for example CC-BY, CC0, GPL), so verify each asset's terms.
- Access model: no official JSON API, but feeds/pages are structured and script-friendly.
- Typical workflow:
  - Crawl RSS/search feeds for 3D entries.
  - Download archives and parse descriptions/license details.
  - Normalize models and metadata into your local catalog.
- Example feed pattern:
  - https://opengameart.org/art-search-rss?keys=&field_art_type_value%5B3D%5D=3D

### 4. Sketchfab Search/Download API (Free Assets via Filters)

Best for: broad catalog discovery with programmatic filtering for free assets.

- Access model: API is queryable; filter for free/downloadable assets.
- Key filters: is_downloadable=true, is_free=true.
- Important limitation: OAuth/account requirements and monthly download limits on free usage tiers can constrain large-scale production use.

## Paid and Commercial Sources

## 1. Traditional Asset Libraries

### Sketchfab (Platform Subscriptions)

- Basic tier: free browsing and access to assets explicitly published under free licenses.
- Pro tier: around $15/month (annual billing), with larger limits/caps.
- Premium tier: around $79/month (annual billing), aimed at commercial integrations and branding control.

### Matterport (Real-World Space API)

- Hosting entry: typically around $10 to $40/month depending on active spaces.
- Model export/API usage: often flat per-space fees (commonly around $20 to $50+) depending on scan size/output.

## 2. Pay-As-You-Go Inference Providers

These providers bill by usage (GPU time or per generation) instead of fixed subscriptions.

### Fal.ai

- Trellis image-to-3D: roughly $0.02 per generation.
- TripoSR image-to-3D: roughly $0.07 per generation.
- Custom GPU/serverless usage: billed per compute time (for example, A100 usage rates by second).

## 3. Dedicated AI Asset Pipelines

These tools combine generation with production features such as rigging/texturing dashboards and credit systems.

### Meshy AI

- Free tier: no API access, public outputs, monthly credit cap.
- Pro tier: around $20/month ($16/month annual), API access plus higher private/commercial limits.
- Studio tier: around $60/month ($48/month annual), higher credit volume and faster queues.

### Tripo AI

- Free tier: limited credits for web testing, public outputs.
- Pro tier: around $19.90/month (lower with annual billing), private commercial model generation and higher-res exports.
- API model: usage-based/volume credit purchasing for production traffic.

## Practical Selection Guide

- Need free reference assets and format validation: start with Khronos sample models.
- Need real-world scanned objects with permissive rights: use Smithsonian Open Access.
- Need low-poly performance-oriented content: prioritize OpenGameArt.
- Need broad marketplace discovery with filters: use Sketchfab API with free-only constraints.
- Need generated custom assets at scale: compare Fal.ai usage pricing versus dedicated platforms like Meshy/Tripo.

## Notes and Cautions

- Prices and limits change frequently; treat figures above as directional and re-verify at purchase time.
- Always store license metadata with each downloaded/generated asset.
- For production pipelines, enforce format validation, polygon budgets, and texture-size limits during ingestion.
