#!/usr/bin/env bash
# Run a cloudflared quick tunnel to the world server and publish its URL so the server's /tunnel
# endpoint can redirect to it. Then on the Quest you type ONE fixed address —
#   http://<this-machine-ip>:<port>/tunnel
# — and it bounces you to the current https://…trycloudflare.com (which changes every run).
#
# Usage:  ./scripts/tunnel.sh         (serves CONJURE_PORT, default 8080)
# Leave it running; Ctrl+C stops the tunnel and clears the published URL.
set -uo pipefail
cd "$(dirname "$0")/.."

PORT="${CONJURE_PORT:-8080}"
# Resolve the disposable cache root from config (the SAME CACHE_ROOT the server reads TUNNEL_FILE from),
# so the URL lands in the user-home cache, not the in-project .cache. Fall back to .cache if conjure isn't
# importable (e.g. venv not active) — the server's one-time migration then relocates it.
CACHE_DIR="$(python -c 'from conjure.config import CACHE_ROOT; print(CACHE_ROOT)' 2>/dev/null || echo .cache)"
TUNNEL_FILE="$CACHE_DIR/tunnel_url"
LOG="$(mktemp -t conjure-tunnel)"
mkdir -p "$CACHE_DIR"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install it ('brew install cloudflared') — see docs/https-setup.md." >&2
  exit 1
fi

CFPID=""; TAILPID=""
cleanup() {
  [ -n "$TAILPID" ] && kill "$TAILPID" 2>/dev/null
  [ -n "$CFPID" ] && kill "$CFPID" 2>/dev/null
  rm -f "$TUNNEL_FILE" "$LOG"
}
trap cleanup EXIT INT TERM

# Best-effort LAN address to type on the Quest (same Wi-Fi); fall back to the hostname.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname)"

# Only the real quick-tunnel host (random-words.trycloudflare.com). Crucially exclude
# api.trycloudflare.com, which appears in cloudflared's info/error lines and is NOT a tunnel.
grab_url() {
  grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" 2>/dev/null | grep -v '://api\.' | head -1
}

URL=""
for attempt in 1 2 3; do
  : > "$LOG"
  echo "==> Starting cloudflared tunnel to http://localhost:$PORT  (attempt $attempt/3, Ctrl+C to stop)"
  # 'context deadline exceeded' is usually a broken IPv6 path to Cloudflare — force IPv4 on retries.
  # (No bash arrays: macOS ships bash 3.2 where an empty array under `set -u` errors.)
  if [ "$attempt" -gt 1 ]; then
    echo "    (forcing IPv4)"
    cloudflared tunnel --url "http://localhost:$PORT" --edge-ip-version 4 >>"$LOG" 2>&1 &
  else
    cloudflared tunnel --url "http://localhost:$PORT" >>"$LOG" 2>&1 &
  fi
  CFPID=$!
  tail -f "$LOG" & TAILPID=$!         # stream cloudflared output live
  for _ in $(seq 1 30); do            # wait up to ~30s for a real URL (or cloudflared to die)
    kill -0 "$CFPID" 2>/dev/null || break
    URL="$(grab_url)"; [ -n "$URL" ] && break
    sleep 1
  done
  [ -n "$URL" ] && break
  kill "$TAILPID" 2>/dev/null; TAILPID=""
  kill "$CFPID" 2>/dev/null; wait "$CFPID" 2>/dev/null; CFPID=""
  echo "!! cloudflared didn't come up (network/timeout reaching trycloudflare.com). Retrying…" >&2
  sleep 2
done

if [ -z "$URL" ]; then
  echo >&2
  echo "!! Could not establish a cloudflared tunnel after 3 tries." >&2
  echo "   That timeout ('context deadline exceeded') is a connectivity issue reaching Cloudflare —" >&2
  echo "   not this script. Try again in a moment, check your internet/VPN/DNS, or use another" >&2
  echo "   option in docs/https-setup.md (Tailscale, Caddy, or USB adb-reverse to localhost)." >&2
  exit 1
fi

printf '%s' "$URL" > "$TUNNEL_FILE"
echo
echo "==> Tunnel ready: $URL"
echo "==> On the Quest (same Wi-Fi), open:  http://$LAN_IP:$PORT/tunnel"
echo
wait "$CFPID"   # block (streaming logs) until cloudflared exits or Ctrl+C
