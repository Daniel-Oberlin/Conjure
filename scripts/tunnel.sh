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
TUNNEL_FILE=".cache/tunnel_url"
mkdir -p .cache

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install it (e.g. 'brew install cloudflared') — see docs/https-setup.md." >&2
  exit 1
fi

cleanup() { rm -f "$TUNNEL_FILE"; }
trap cleanup EXIT INT TERM

# Best-effort LAN address to type on the Quest (same Wi-Fi). Fall back to the hostname.
LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname)"

echo "==> Starting cloudflared tunnel to http://localhost:$PORT  (Ctrl+C to stop)"
captured=""
# Stream cloudflared's output; the first trycloudflare URL it prints gets published to $TUNNEL_FILE.
cloudflared tunnel --url "http://localhost:$PORT" 2>&1 | while IFS= read -r line; do
  printf '%s\n' "$line"
  if [ -z "$captured" ]; then
    url="$(printf '%s' "$line" | grep -oE 'https://[a-z0-9.-]+\.trycloudflare\.com' | head -1)"
    if [ -n "$url" ]; then
      captured=1
      printf '%s' "$url" > "$TUNNEL_FILE"
      echo
      echo "==> Tunnel ready: $url"
      echo "==> On the Quest, just open:  http://$LAN_IP:$PORT/tunnel"
      echo
    fi
  fi
done
