# Running Conjure

Everything between a fresh clone and a world you can walk around in, in the order you hit it:
**install → run → into the headset on a cable → untethered over HTTPS.**

The top-level [README](../README.md) is the quickstart and the **source of truth for API keys** — this
guide deliberately doesn't restate them. Provider and model choices live in
[providers.md](./providers.md).

*(Consolidated from `setup.md`, `testing-on-quest.md` and `https-setup.md` on 2026-08-26 — one audience,
one task, and they already chained into each other.)*

---

# Part 1 — Install

## TL;DR

```bash
./scripts/setup.sh                 # system deps + venv + voice extras + JS deps + git hooks + .env + doctor
#   then edit .env with your keys (see the README), and:
python -m conjure.doctor           # until all required checks pass
```

World-server-only (no voice) needs no system deps and no key:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .                   # base: world server + MCP, no voice
```

## What `setup.sh` actually does

In order, all idempotent:

1. **System deps** — `portaudio` + `espeak-ng` via Homebrew (macOS) or apt (Linux). Bails on macOS if
   Homebrew is absent; warns and continues on any other OS.
2. **venv** — creates `.venv` if missing, activates it.
3. **`pip install -e ".[voice]"`** — the heavy one (pulls torch).
4. **`npm install`** — JS test deps, skipped with a notice if Node isn't present.
5. **Git hooks** — `git config core.hooksPath scripts/git-hooks`, so pre-push runs the fast suite.
6. **`.env`** — copied from `.env.example` if absent.
7. **`python -m conjure.doctor`** — the preflight, non-fatal here.

## What's automatic vs. manual

| Prerequisite | How it's handled |
|---|---|
| **Python packages** | Automatic via `pyproject.toml` — see the extras below. |
| **Model weights** (Whisper, Kokoro) | Automatic — downloaded from Hugging Face on first run. |
| **System libraries** (portaudio, espeak-ng) | Not pip-installable → `setup.sh` installs them, or do it by hand (below). |
| **API keys** | You provide. Listed in `.env.example` and the [README](../README.md); validated by the doctor. |
| **`adb`** (Quest testing) | `brew install android-platform-tools` — Part 3. |
| **Node** (JS tests) | Optional. `brew install node`; without it the pre-push hook skips the JS suite. |

Single source of truth for each: **`pyproject.toml`** (Python deps), **`.env.example`** (keys/config),
this doc + **`scripts/setup.sh`** (system libs), **`conjure/doctor.py`** (the runtime checklist).

## Dependency extras

```bash
pip install -e .                 # base — world server, MCP, CLI. No ML, no voice.
pip install -e ".[voice]"        # + local STT/TTS (Whisper, Kokoro) and the voice loop. Pulls torch.
pip install -e ".[embed]"        # + SigLIP embeddings: semantic + image-similarity asset search.
pip install -e ".[dev]"          # + pytest, pytest-asyncio, respx.
```

`[embed]` is separate on purpose: it's multi-GB, and the catalog runs fine without it on FTS5 + exact
match. Install it to turn on `search_library`'s semantic half. On a machine with no GPU, use the CPU
torch wheel to avoid the CUDA download.

## Manual system-dependency install

- **macOS:** `brew install portaudio espeak-ng`
- **Debian/Ubuntu/Raspberry Pi:** `sudo apt-get install portaudio19-dev espeak-ng`

`portaudio` drives the host microphone/speaker; `espeak-ng` does phonemization for the local TTS.

## Keys

Speech runs **locally** (Whisper STT, Kokoro TTS) — no key. Everything else is per-feature, all in
`.env` (git-ignored). **The current table is in the [README](../README.md#quickstart)**; in short you
need at least one director key (`ANTHROPIC` / `GOOGLE` / `OPENAI` / `XAI`), plus `POLY_PIZZA_API_KEY`
for 3D models and any of `GOOGLE`/`OPENAI`/`XAI` for image generation.

> The doctor checks the voice stack and the *active* director's key. The asset/image keys surface as a
> clear error from `place_asset` / `place_image` when missing.

## The doctor (preflight)

`python -m conjure.doctor` verifies the prerequisites for your selected stack and prints a checklist
with fixes:

```
Conjure preflight  (stack: STT=whisper, TTS=kokoro, LLM=claude)

  ✓ pipecat-ai installed
  ✓ local audio (pyaudio + portaudio)
  ✓ Whisper backend present (model downloads on first run)
  ✓ Kokoro TTS (model downloads on first run)
  ✓ ANTHROPIC_API_KEY set
  ⚠ world server not reachable at http://localhost:8080
      → start it: python -m conjure

All required prerequisites look good.
```

A failing check prints its fix inline (`✗ pyaudio present but portaudio missing → brew install
portaudio`). It exits non-zero while any **required** check is unmet, so it's safe in scripts.

## Notes

- **Piper TTS** (alt to Kokoro) needs a separately-run Piper HTTP server + a downloaded voice model —
  Kokoro is more self-contained, which is why it's the default.
- **Heavy install:** `[voice]` pulls large deps. First install and first model download take a while;
  later runs are fast.
- **Raspberry Pi:** prefer Piper for TTS and a small/streaming STT (Moonshine) — see providers.md.

---

# Part 2 — Run it

```bash
source .venv/bin/activate
python -m conjure          # terminal 1: world server → http://localhost:8080
conjure-agent              # terminal 2: agent server (holds the director + keys)
conjure-cli                # terminal 3: the REPL   (or: python -m conjure.voice)
```

The world server runs **standalone** — it boots from disk and renders to headsets with no agent server
present, so you can walk a world with no AI in the loop. Voice and CLI are thin clients of the *agent*
server; start that before them.

**What you should see** at `http://localhost:8080` before any world-building: the starter world is
`Holodeck` — a **black room drawn as a white wireframe grid**: a 20 × 20 floor grid and four wall grids
against a black sky. That is what "working" looks like on a cold start; it is not a blank page.

Drive it without an LLM:

```bash
python scripts/send_patch.py examples/patches/add_cube.json        # a red cube appears live
python scripts/send_patch.py examples/patches/recolor_ground.json  # ground and sky change
python scripts/mcp_smoke.py                                        # exercise the MCP tool surface
```

Entry points (all installed by `pip install -e .`): `conjure`, `conjure-agent`, `conjure-cli`,
`conjure-ctl`, `conjure-voice`, `conjure-mcp`, `conjure-doctor`, `conjure-import`.

---

# Part 3 — Into the headset, on a cable

The **Phase-0 path** (`adb reverse` over USB — decision #3, tier 1). No TLS needed: the Quest browser
treats `localhost` as a secure context, which is WebXR's requirement. To drop the cable, skip to Part 4.

## Prerequisites

- A Mac with **Homebrew** (these commands assume macOS), and the project installed (Part 1).
- A **Meta Quest 3** and the **Meta Horizon** companion app on your phone.
- A **USB-C data cable** — the one bundled with the Quest works. A charge-only cable will not.

## 3A. One-time setup

Once per machine + headset.

### Install `adb`

```bash
brew install android-platform-tools
adb version          # should print "Android Debug Bridge version ..."
```

### Enable Developer Mode

From the **Meta Horizon phone app**, not the headset:

1. Open **Meta Horizon** with the Quest powered on.
2. **Menu** (bottom-right) → **Devices** → your **Quest 3**.
3. **Headset settings** → **Developer Mode** → **ON**.
   - If prompted to register as a developer / create an organization first, do it (free), then toggle.
4. **Reboot the headset** (hold power → Restart) so it takes effect.

### Connect and authorize

1. Plug the Quest into the Mac.
2. **Put the headset on** and accept the prompts:
   - **"Allow USB debugging?"** → check **"Always allow from this computer"** → **Allow**.
   - **"Allow access to data"** (if shown) → **Allow**.

### Confirm

```bash
adb devices
```

```
List of devices attached
1WMHHxxxxxxxxx   device
```

`unauthorized` means the prompt is still waiting inside the headset. Nothing listed means a charge-only
cable, or Developer Mode / the reboot didn't take.

## 3B. Each session

1. **Start the world server** (Part 2) and check `http://localhost:8080` in a Mac browser first — this
   isolates server problems from headset/USB ones.
2. **Forward the port:**
   ```bash
   adb reverse tcp:8080 tcp:8080
   ```
   No output means success; it points the headset's own `localhost:8080` at your Mac. **Re-run it after
   any unplug/replug.**
3. **In the Quest**, open the **Meta Quest Browser** → `http://localhost:8080` (note `http`, not
   `https`) → tap the **VR goggles icon** → **Enter VR**.
4. **Live-edit while wearing it** — run the `send_patch.py` commands from Part 2 and watch them land.

> ⚠️ The Quest still needs **Wi-Fi internet**: the page loads A-Frame from a public CDN
> (`aframe.io/releases/1.5.0/aframe.min.js`). USB forwards only the page itself. To go fully offline,
> vendor A-Frame locally — not done yet.

Next session: server → `adb reverse` → browser. 3A is permanent.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `adb devices` shows `unauthorized` | Accept the debugging prompt inside the headset |
| `adb devices` shows nothing | Charge-only cable, or Developer Mode / reboot not done |
| Works in the Mac browser, **not** in the Quest | `adb reverse` not active (re-run it), or device not authorized |
| Page loads but **blank / no 3D** | Quest has no Wi-Fi internet → can't fetch A-Frame from the CDN |
| A black grid room and nothing else | That's the starter world rendering correctly — see Part 2 |
| Cube doesn't appear on `send_patch` | Server not running, or venv not activated in that terminal |

---

# Part 4 — Untethered, over HTTPS

WebXR refuses to run in a non-secure context off `localhost`, so going wireless means the Quest has to
load Conjure over **HTTPS**.

The Conjure server stays plain **HTTP on `:8080`**. You put a **TLS-terminating reverse proxy** in front
of it that the Quest trusts; the proxy forwards to `localhost:8080`. The client **auto-upgrades the
WebSocket to `wss://`** when the page is HTTPS, so nothing in the app changes.

```
Quest browser ──HTTPS──▶  TLS reverse proxy  ──HTTP──▶  python -m conjure  (:8080)
              (wss://)     (cloudflared / Caddy / Tailscale / mkcert)
```

Pick by what you have — a domain? want it private? offline? Start the server first.

## Option A — Cloudflare quick tunnel (no domain, ~2 min) ⭐ start here

Best for getting untethered *right now* and for demos. Public ephemeral URL, routed through Cloudflare
(slight latency), no certs, no domain, no router config.

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8080
```

It prints `https://random-words.trycloudflare.com`. Open that in the Quest browser (any network with
internet) and enter VR.

### Avoid retyping the URL: `scripts/tunnel.sh` + `/tunnel`

The quick-tunnel URL changes every run, which is miserable to retype on a headset. The helper publishes
the current URL so the server can redirect to it:

```bash
./scripts/tunnel.sh          # runs cloudflared, prints a short LAN address to use
```

On the Quest (same Wi-Fi as the Mac) always open the **fixed** address it prints —
`http://<your-mac-ip>:8080/tunnel` — and the server 307-redirects to the current `trycloudflare.com`
URL. Type it once; it never changes. The redirect page is plain HTTP on your LAN and bounces you to the
HTTPS tunnel where WebXR actually runs.

The script writes the URL to **`$CACHE_ROOT/tunnel_url`** — the user-home cache
(`~/.cache/conjure/tunnel_url` by default), the same path `server.py`'s `TUNNEL_FILE` reads. It resolves
that from `conjure.config` at run time and only falls back to the in-project `.cache/` if `conjure`
isn't importable. `Ctrl+C` stops the tunnel and deletes the file.

It retries up to three times, forcing IPv4 after the first attempt — a `context deadline exceeded` from
cloudflared is usually a broken IPv6 path to Cloudflare, not a Conjure problem.

- ⚠️ The URL is **public** while running and **changes every run**. Fine for solo testing; for a stable,
  access-controlled URL you want a *named* tunnel + Cloudflare Access.

## Option B — Caddy + Let's Encrypt (you own a domain)

The best permanent home setup: wireless on your LAN, low latency, private, trusted auto-renewing cert.
Needs a domain and a DNS provider with an API. Uses the **DNS-01 challenge**, so it works for a LAN IP
with **no inbound ports opened**.

1. **Point a hostname at your Mac's LAN IP** — a DNS `A` record, e.g.
   `conjure.example.com → 192.168.1.50`. Use a reserved/static LAN IP.
2. **Get a DNS API token** (Cloudflare: scoped to edit that zone).
3. **Install Caddy *with* the DNS plugin.** Homebrew's `caddy` has no DNS plugins, so build one:
   ```bash
   brew install xcaddy
   xcaddy build --with github.com/caddy-dns/cloudflare   # → ./caddy
   ```
4. **Caddyfile:**
   ```
   conjure.example.com {
       reverse_proxy localhost:8080
       tls { dns cloudflare {env.CF_API_TOKEN} }
   }
   ```
5. **Run it:**
   ```bash
   CF_API_TOKEN=your_token ./caddy run
   ```

On the Quest, open `https://conjure.example.com` — it resolves to your Mac on the LAN, the cert is
trusted automatically (real CA → **zero per-headset setup**), and WebXR works.

## Option C — Tailscale (private mesh, no domain, works remotely)

Valid HTTPS with no domain and no public exposure. The catch: Tailscale has to run on the Quest.

1. **Mac:** `brew install tailscale` (or the app), `tailscale up`, sign in.
2. **Quest:** sideload the Tailscale Android APK (`adb install` or SideQuest), sign into the same tailnet.
3. **Serve over the tailnet** — Tailscale provisions a trusted cert for your MagicDNS name:
   ```bash
   tailscale serve --bg 8080
   ```
   Open the printed `https://<machine>.<tailnet>.ts.net` in the Quest browser.

Because it's a mesh this also works with the Quest **off your LAN**, which aligns with the
remote-multiplayer direction in [vision.md §12](./vision.md).

## Option D — Fully offline: self-signed + trust it on the Quest

Air-gapped/local-only. No domain, no internet — but you install a root cert on **each** headset.

1. **Mac:** `brew install mkcert && mkcert -install`, then mint a cert for your LAN host:
   ```bash
   mkcert conjure.local 192.168.1.50
   ```
2. Run a TLS proxy with that cert in front of `:8080` (Caddy with `tls cert.pem key.pem`, or any proxy).
3. **Install mkcert's root CA on the Quest:** copy `"$(mkcert -CAROOT)/rootCA.pem"` to the headset and
   install via **Settings → Security → install a certificate → CA certificate**. Then open
   `https://192.168.1.50:<port>`.

## Notes & troubleshooting

- **WebSocket:** nothing to do — the client uses `wss://` automatically on HTTPS pages, and all the
  proxies above forward WebSockets by default.
- **Enter VR greyed out / "requires HTTPS":** you're still on `http://` off `localhost` — use the HTTPS
  URL from your chosen option.
- **"Connection not private":** only Option D before the CA is installed. A/B/C use trusted certs.
- **LAN IP changed (B/D):** reserve a static IP for the Mac in your router's DHCP settings.
- **Server binding:** Conjure listens on `0.0.0.0:8080`, so a proxy reaching it on `localhost:8080`
  works out of the box.

Once HTTPS works you no longer need `adb reverse` or the cable.
