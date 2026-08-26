#!/usr/bin/env bash
# Push Desktop agent settings to GCP once (instructions, permissions, teams).
# Prefer the always-on xyz.block.buzz.cloud-sync LaunchAgent from install-path.sh.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/.." && pwd)"

load_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ -z "$line" || "$line" == \#* ]] && continue
    if [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      local key="${BASH_REMATCH[1]}"
      local val="${BASH_REMATCH[2]}"
      val="${val#\"}"
      val="${val%\"}"
      val="${val#\'}"
      val="${val%\'}"
      if [[ -z "${!key:-}" ]]; then
        export "$key=$val"
      fi
    fi
  done < "$f"
}

load_env_file "$root/.env"
load_env_file "$root/infra/config.env"

export BUZZ_GCP_PROJECT="${BUZZ_GCP_PROJECT:-${GCP_PROJECT:-your-gcp-project}}"
export BUZZ_GCP_ZONE="${BUZZ_GCP_ZONE:-${GCP_ZONE:-us-central1-a}}"
export BUZZ_GCP_INSTANCE="${BUZZ_GCP_INSTANCE:-${LISTENER_INSTANCE:-buzz-listener}}"

sync=""
for candidate in \
    "${HOME}/.local/bin/buzz_cloud_sync.py" \
    "${here}/buzz-cloud-sync.py"; do
  if [[ -f "$candidate" ]]; then
    sync="$candidate"
    break
  fi
done
if [[ -z "$sync" ]]; then
  echo "buzz-cloud-sync.py not found. Run macos/install-path.sh first." >&2
  exit 1
fi

python=""
for candidate in \
    "$(command -v python3 2>/dev/null || true)" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3; do
  [[ -n "$candidate" && -x "$candidate" ]] || continue
  if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    python="$candidate"
    break
  fi
done
if [[ -z "$python" ]]; then
  echo "python3 >= 3.10 not found" >&2
  exit 1
fi

"$python" "$sync" --once
echo "Synced Desktop agent settings to ${BUZZ_GCP_INSTANCE}."
