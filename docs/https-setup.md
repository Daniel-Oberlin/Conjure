# Going wireless: serving Conjure over HTTPS

The USB `adb reverse` path ([testing-on-quest.md](./testing-on-quest.md)) tethers you to a cable.
To go **wireless**, the Quest has to load Conjure over **HTTPS** — WebXR refuses to run in a
non-secure context off `localhost`. This guide gets you there.

## How it works (the one thing to understand)

The Conjure server stays plain **HTTP on `:8080`**. You put a **TLS-terminating reverse proxy** in
front of it that the Quest trusts; the proxy forwards to `localhost:8080`. The Conjure client
**auto-upgrades the WebSocket to `wss://`** when the page is HTTPS, so nothing in the app changes.

```
Quest browser ──HTTPS──▶  TLS reverse proxy  ──HTTP──▶  python -m conjure  (:8080)
              (wss://)     (cloudflared / Caddy / Tailscale / mkcert)
```

Pick an option below by what you have (a domain? want it private? offline?). Start the server first:

```bash
python -m conjure        # serves http://localhost:8080
```

---

## Option A — Fastest: Cloudflare quick tunnel (no domain, ~2 min) ⭐ start here

Best for getting untethered *right now* and for demos. Public, ephemeral URL; routed through
Cloudflare (slight latency); no certs, no domain, no router config.

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8080
```

It prints a URL like `https://random-words.trycloudflare.com`. Open **that** in the Quest browser
(the Quest just needs internet — any network) and enter VR.

### Tired of typing the long URL? Use `scripts/tunnel.sh` + `/tunnel`

The quick-tunnel URL changes every run, which is tedious to retype on the Quest. Instead, run the
tunnel via the helper, which publishes the current URL so the server can redirect to it:

```bash
./scripts/tunnel.sh          # runs cloudflared, prints a short LAN address to use
```

Then on the Quest (same Wi-Fi as the Mac) always open the **fixed, short** address it prints —
`http://<your-mac-ip>:8080/tunnel` — and the server 307-redirects you to the current
`trycloudflare.com` URL. Type it once; it never changes. (The redirect page is plain HTTP on your
LAN; it bounces you to the HTTPS tunnel where WebXR actually runs.) `Ctrl+C` stops the tunnel and
clears the published URL. The script writes it to `.cache/tunnel_url`, which the `/tunnel` route reads.

- ⚠️ The URL is **public** while running (anyone with it can reach your server) and **changes every
  run**. Fine for solo testing; for a stable, access-controlled URL, set up a *named* tunnel +
  Cloudflare Access (more involved — see Cloudflare's docs).

---

## Option B — Best home setup: Caddy + Let's Encrypt (you own a domain)

Wireless on your LAN, **low latency, private, trusted cert, auto-renewing**. Needs a domain and a
DNS provider with an API (e.g. Cloudflare DNS). Uses the **DNS-01 challenge**, so it works for a
**LAN IP with no inbound ports opened**.

1. **Point a hostname at your Mac's LAN IP.** Add a DNS `A` record, e.g.
   `conjure.example.com → 192.168.1.50` (use a **reserved/static** LAN IP so it doesn't change).
2. **Get a DNS API token** from your provider (Cloudflare: a token scoped to edit that zone).
3. **Install Caddy *with* the DNS plugin.** Homebrew's `caddy` lacks DNS plugins, so build one:
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
   Caddy fetches a real Let's Encrypt cert (via DNS) and proxies to Conjure. On the Quest, open
   **`https://conjure.example.com`** — it resolves to your Mac on the LAN, the cert is trusted
   automatically (real CA → **zero per-headset setup**), and WebXR works.

This is the recommended permanent setup for one or more headsets at home.

---

## Option C — Private mesh, no domain: Tailscale (works at home *and* remotely)

A private network with valid HTTPS and **no domain, no public exposure**. The catch: Tailscale has
to run on the Quest (sideload its Android app).

1. **Mac:** `brew install tailscale` (or the app), `tailscale up`, sign in.
2. **Quest:** sideload the **Tailscale** Android APK (via `adb install` or SideQuest) and sign into
   the same tailnet.
3. **Serve Conjure over the tailnet with HTTPS** (Tailscale provisions a trusted cert for your
   `*.ts.net` MagicDNS name):
   ```bash
   tailscale serve --bg 8080
   ```
   It prints your `https://<machine>.<tailnet>.ts.net` URL. Open it in the Quest browser.

Bonus: because it's a mesh, this also works when the Quest is **off your LAN** (remote) — aligning
with Conjure's future remote-multiplayer direction.

---

## Option D — Fully offline / no internet: self-signed + trust it on the Quest

For an air-gapped/local-only setup. No domain, no internet — but you must install a root cert on
**each** headset (fiddly).

1. **Mac:** `brew install mkcert && mkcert -install`, then mint a cert for your LAN host:
   ```bash
   mkcert conjure.local 192.168.1.50
   ```
2. Run a TLS proxy with that cert in front of `:8080` (e.g. Caddy with `tls cert.pem key.pem`, or
   any reverse proxy).
3. **Install mkcert's root CA on the Quest:** copy `"$(mkcert -CAROOT)/rootCA.pem"` to the headset
   and install it via **Settings → Security → install a certificate → CA certificate**. Then open
   `https://192.168.1.50:<port>` in the Quest browser.

---

## Notes & troubleshooting

- **WebSocket:** no action needed — the client uses `wss://` automatically on HTTPS pages. The
  proxies above all forward WebSockets by default.
- **Enter VR greyed out / "requires HTTPS":** you're still on `http://` or `localhost` — use the
  HTTPS URL from your chosen option.
- **"Connection not private" / untrusted cert:** only happens with self-signed (Option D) before
  the CA is installed on the headset. Options A/B/C use publicly-trusted certs.
- **LAN IP changed (Option B/D):** reserve a static IP for the Mac in your router's DHCP settings.
- **Server binding:** Conjure already listens on `0.0.0.0:8080`, so the proxy reaching it on
  `localhost:8080` works out of the box.

When you've got HTTPS working, you no longer need `adb reverse` or the USB cable — open the HTTPS
URL in the Quest browser over Wi-Fi and you're untethered.
