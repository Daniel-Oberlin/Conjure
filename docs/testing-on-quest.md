# Testing on the Meta Quest 3

How to run Conjure and view/drive it live inside the headset. This is the **Phase-0 path**
(`adb reverse` over USB — decision #3, tier 1): it needs no TLS because the Quest browser treats
`localhost` as a secure context, which WebXR requires. Multi-headset / LAN serving moves to
Caddy + TLS later (decision #3, tiers 2–3).

> Verified working: a static A-Frame scene renders in VR and live patches appear in-headset.

## Prerequisites

- A Mac (these commands assume macOS) with **Homebrew**.
- Python 3.11+ and the project installed in a virtualenv (see the top-level [README](../README.md)
  quickstart: `python3 -m venv .venv && source .venv/bin/activate && pip install -e .`).
- A **Meta Quest 3** and the **Meta Horizon** companion app on your phone.
- A **USB-C data cable** (the one bundled with the Quest works).

---

## Part A — One-time setup

You only do this once per machine + headset.

### A1. Install `adb` (Android platform tools)

```bash
brew install android-platform-tools
adb version          # should print "Android Debug Bridge version ..."
```

### A2. Enable Developer Mode on the Quest

Done from the **Meta Horizon phone app**, not the headset:

1. Open **Meta Horizon** on your phone with the Quest powered on.
2. **Menu** (bottom-right) → **Devices** → select your **Quest 3**.
3. **Headset settings** → **Developer Mode** → toggle **ON**.
   - If prompted to register as a developer / create an organization first, do it (it's free),
     then toggle Developer Mode on.
4. **Reboot the headset** (hold power → Restart) so it takes effect.

### A3. Connect by USB and authorize

1. Plug the Quest into the Mac with the USB-C cable.
2. **Put the headset on.** Accept the prompts:
   - **"Allow USB debugging?"** → check **"Always allow from this computer"** → **Allow**.
   - **"Allow access to data"** (if shown) → **Allow**.

### A4. Confirm the Mac sees the headset

```bash
adb devices
```

Expected:

```
List of devices attached
1WMHHxxxxxxxxx   device
```

- `unauthorized` → the debugging prompt is still waiting in the headset (A3).
- nothing listed → try another cable/port; re-check A2/A3.

---

## Part B — Run the test (repeat each session)

Use **two terminals**. In both, start from the repo root with the venv active:

```bash
cd <path-to>/Conjure
source .venv/bin/activate
```

### B1. Terminal 1 — start the world server

```bash
python -m conjure
```

Leave it running; uvicorn will report it's serving on `http://0.0.0.0:8080`.

### B2. Sanity-check on the Mac first

Open <http://localhost:8080> in a Mac browser — you should see a **green ground plane and an
orange pillar**. This isolates server problems from headset/USB problems.

### B3. Terminal 2 — forward the port to the headset

```bash
adb reverse tcp:8080 tcp:8080
```

No output = success. This points the headset's own `localhost:8080` at your Mac.
**Re-run it any time you unplug/replug the headset.**

> ⚠️ The Quest also needs **Wi-Fi internet** — the page loads the A-Frame library from a public
> CDN over the headset's own network. USB only forwards the page itself. (To go fully offline,
> vendor A-Frame locally — a later improvement.)

### B4. Open it in the headset and enter VR

1. In the Quest, open the **Meta Quest Browser**.
2. Address bar: **`http://localhost:8080`** (note: `http`, not `https`).
3. You should see the ground + pillar in front of you.
4. Tap the **VR goggles icon** (bottom-right) → **Enter VR**.

### B5. Live-edit test

Keep the headset on; in **Terminal 2**:

```bash
python scripts/send_patch.py examples/patches/add_cube.json        # red cube pops in to your right
python scripts/send_patch.py examples/patches/recolor_pillar.json  # pillar turns blue & rises, sky darkens
```

The changes appear **live while you're wearing the headset**. You can also drive it through the
MCP server (`python scripts/mcp_smoke.py`, with the world server running) — see the README.

---

## Stopping / re-running

- Stop the server: **Ctrl+C** in Terminal 1.
- Next session: redo **B1 → B3 → B4** (server, `adb reverse`, open browser). Part A is permanent.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `adb devices` shows `unauthorized` | Accept the debugging prompt inside the headset (A3) |
| `adb devices` shows nothing | Charge-only cable, or Developer Mode/reboot not done (A2) |
| Works in Mac browser, **not** in Quest | `adb reverse` not active (re-run B3), or device not authorized |
| Page loads but **blank / no 3D** | Quest has no Wi-Fi internet → can't fetch A-Frame from the CDN |
| Cube doesn't appear on `send_patch` | Terminal 1 server not running, or venv not activated in Terminal 2 |
