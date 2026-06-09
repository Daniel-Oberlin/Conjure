# Provider & module registry

Conjure is built on a **provider abstraction** (every model role is swappable, decision #1) and a
**module system** (capabilities plug in as MCP servers, spec §13). This is the catalog of each
swappable slot: the **chosen v1 default**, plus **alternatives and future possibilities**.

Defaults are selected by config (`.env` / provider factory) and can be changed without code
changes. This complements [decisions.md](./decisions.md) (the *why* behind forks) and the §13
module taxonomy — this doc is the *what's available per slot*.

**Legend:** ✅ v1 default (chosen) · 🟢 included alt (easy swap) · 💡 alternative / future · 🔜 planned, not yet chosen

---

## Voice loop (Phase 2)

Recommended stack: **local speech + cloud director** — audio stays on the Mac, near-zero ongoing
cost, strong tool-calling where it matters, one API key (Anthropic). Maps onto the #1 topologies
(Mac does speech locally; cloud or a home box does the heavy reasoning).

### Speech-to-text (STT)
| Option | Hosting | Notes |
|---|---|---|
| ✅ **Whisper** (MLX / faster-whisper) | local | PipeCat-native; great on Apple Silicon. Utterance-based (VAD-segmented), not true streaming — fine for commands. |
| 💡 Moonshine | local | Built for **streaming on edge / Raspberry Pi**; low latency, low hallucination. Needs custom wiring. |
| 💡 NVIDIA Parakeet (`parakeet.cpp`) | local | Very fast, runs on Apple Silicon via Metal. Needs custom wiring. |
| 💡 Deepgram / AssemblyAI | cloud | True low-latency streaming; snappiest feel. Per-minute cost + keys. |

### Director LLM (roster — spec §3, arch §7a)
Multiple named LLMs may be active in one session; this is the **default director** + likely roster
members. The roster + switching are **built** (`conjure/llm.py`, `conjure/director.py`): one shared
director serves both voice and CLI; add a provider by registering it in `build_roster` and it's
usable everywhere with no other code change. Switch mid-conversation ("let me talk to Gemini") or
address one for a single turn ("Gemini, make a picture of a cat"); the transcript is attributed so
each LLM sees who said what.
| Option | Hosting | Notes |
|---|---|---|
| ✅ **Claude** (Anthropic) | cloud | Default director — strongest, most reliable **tool-calling** (it drives the MCP edits). |
| ✅ **Google Gemini** | cloud | Built roster member (casual name "Gemini"); `CONJURE_GEMINI_MODEL`, default `gemini-2.5-flash`. |
| 💡 OpenAI (GPT) | cloud | Roster member (e.g. casual name "Chat"). |
| 💡 Groq | cloud | Very fast inference; good for snappy turns. |
| 💡 Local Ollama (Qwen / Llama w/ tool-calling) | local | Zero-key / offline director. Works, but tool-calling is less reliable — experimentation, not the robust default. |

### Text-to-speech (TTS)
| Option | Hosting | Notes |
|---|---|---|
| ✅ **Kokoro** | local | 82M, Apache-2.0, studio-grade for its size; real-time on Apple Silicon (MLX). PipeCat-native. **Best voice quality (local).** |
| 🟢 Piper | local | Sub-50ms first-audio; runs Pi → Mac; PipeCat-native. **Lowest latency / most Pi-friendly.** No voice cloning. |
| 💡 Cartesia | cloud | Very low latency, high quality. |
| 💡 ElevenLabs | cloud | Top quality, slightly more latency. |

### Voice activity / turn-taking
| Option | Hosting | Notes |
|---|---|---|
| ✅ **Silero VAD** | local | Standard PipeCat VAD for endpointing + barge-in. |

### Audio transport (Phase 2)
| Option | Notes |
|---|---|
| ✅ **Host-local audio** (room mic + speaker) | Matches decision #5's shared-room-device default; no audio piped through the Quest. |
| 💡 Quest-mic over WebRTC | Later enhancement (per-headset audio, remote bridge). |

---

## Asset & media generation

### Image generation (pluggable registry — `imagegen.py`)
| Option | Hosting | Notes |
|---|---|---|
| ✅ **Gemini "Nano Banana"** (`gemini-2.5-flash-image`) | cloud | Default; great at editing + outpainting; ~4¢/image (needs billing). |
| 💡 OpenAI gpt-image-1 | cloud | Strong prompt adherence + text-in-image; ~15 lines to add as a generator. |
| 💡 FLUX (fal/Replicate) · Stable Diffusion (local) | cloud/local | Plug in as further generators. |

### Image processing — up-res & outpainting (spec §5)
| Option | Hosting | Notes |
|---|---|---|
| ✅/💡 Gemini "Nano Banana" (editing) | cloud | Best-in-class **layout-aware outpainting** → photo to skybox/panorama; image-in→image-out path already wired (generate_content). |
| 💡 Super-resolution (e.g. Real-ESRGAN-class) | local/cloud | Up-res images/textures. |

### 3D generation (spec §5, #10)
| Option | Hosting | Notes |
|---|---|---|
| 💡 Meshy / Luma / Tripo | cloud | text/image → 3D. |
| 💡 Hunyuan3D / Trellis | both | Strong; some self-hostable on a GPU box. |
| ✅ Procedural mesh-gen (in-sandbox code) | local | Always-available; runs in the QuickJS sandbox (#7, #10). |

### Audio sources (extensible audio engine, spec §7)
| Option | Hosting | Notes |
|---|---|---|
| ✅ File playback | client | Positional / ambient audio assets. |
| ✅ Programmatic / procedural | client | Web Audio / `AudioWorklet` synthesis (synths, tones, generative soundscapes). |
| 💡 Generated audio (SFX / music models) | both | Model-generated audio as assets. |

---

## Content sources & inputs

### Free / CC asset libraries (spec §5)
Poly Pizza · Quaternius · Kenney · Sketchfab (CC filter) · Objaverse · Smithsonian 3D · Poly Haven
(HDRIs/textures/models). Each tracked with **license + attribution**. New sources plug in as
content-source modules (§13).

### Content-source modules (examples)
| Module | Notes |
|---|---|
| 💡 NAS photo library | `search_photos` / `get_photo`, incl. stereoscopic & 360° media. |
| 💡 Interactive-fiction (Z-machine) | Director renders IF state as VR/AR scenes. |
| 💡 Remote-session (streaming) | Remote screen onto a surface (forward-compat). |

### Input drivers (spec §11b)
| Source | Hosting | Notes |
|---|---|---|
| ✅ WebXR controllers / hands | client | Baseline. |
| ✅ Gamepad API (incl. BT controllers paired to Quest) | client | Baseline. |
| 🟢 WebHID / WebUSB | client | Where supported. |
| 🟢 SDL / evdev / hidapi | host | USB/BT yokes, pedals, throttles, joysticks, trackballs. |
| 💡 Input-provider modules | anywhere | New device families plug in. |

---

## How a default is chosen / swapped

- **Config-driven:** a provider factory reads `.env` (e.g. `CONJURE_STT=whisper`, `CONJURE_TTS=kokoro`,
  `CONJURE_LLM=claude`) and instantiates the matching service behind a uniform interface.
- **Secrets:** API keys live in `.env` (git-ignored); a committed `.env.example` documents the vars.
- **Topologies (#1):** the same code runs all-cloud (Pi orchestrator), part-local (Mac), or with a
  self-hosted home GPU box serving local model endpoints.
- **Modules:** non-core capabilities (content sources, engines, audio/input/processing plugins) are
  added as MCP servers without touching the core (§13).
