#!/usr/bin/env bash
# Conjure setup: install system deps + Python env + .env, then run the preflight doctor.
# Automates the common path; see docs/setup.md for per-platform details.
set -euo pipefail
cd "$(dirname "$0")/.."

OS="$(uname -s)"
echo "==> Detected OS: $OS"

install_system_deps() {
  case "$OS" in
    Darwin)
      if ! command -v brew >/dev/null 2>&1; then
        echo "!! Homebrew not found. Install it from https://brew.sh and re-run." >&2
        exit 1
      fi
      echo "==> System deps via Homebrew (portaudio, espeak-ng)"
      brew list portaudio >/dev/null 2>&1 || brew install portaudio
      brew list espeak-ng >/dev/null 2>&1 || brew install espeak-ng
      ;;
    Linux)
      echo "==> System deps via apt (needs sudo): portaudio19-dev, espeak-ng"
      sudo apt-get update
      sudo apt-get install -y portaudio19-dev espeak-ng
      ;;
    *)
      echo "!! Unsupported OS '$OS' — install portaudio + espeak-ng manually (see docs/setup.md)." >&2
      ;;
  esac
}

install_system_deps

if [ ! -d .venv ]; then
  echo "==> Creating virtualenv (.venv)"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing Conjure with voice extras (this can take a while — pulls torch, etc.)"
pip install -q --upgrade pip
pip install -e ".[voice]"

if [ ! -f .env ]; then
  echo "==> Creating .env from .env.example  (remember to add ANTHROPIC_API_KEY)"
  cp .env.example .env
fi

echo "==> Preflight doctor"
python -m conjure.doctor || true

echo
echo "==> Setup complete. Next:"
echo "    1) Edit .env and set ANTHROPIC_API_KEY (https://console.anthropic.com)"
echo "    2) Re-run: python -m conjure.doctor   (until all required checks pass)"
echo "    See docs/setup.md for details."
