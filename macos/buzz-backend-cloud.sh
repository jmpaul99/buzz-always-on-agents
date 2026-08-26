#!/bin/bash
# Source-tree helper. Desktop PATH discovery is buzz-backend-*; do not put this
# folder (or buzz-backend-cloud.py) on PATH or Run on shows cloud.py.
# Use macos/install-path.sh — it installs ~/.local/bin/buzz-backend-cloud with
# baked-in python3 / Homebrew / gcloud PATH (Finder-launched Desktop is minimal).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${PATH}"
exec python3 "${here}/buzz-backend-cloud.py" "$@"
