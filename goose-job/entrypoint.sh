#!/usr/bin/env bash
# Start LiteLLM sidecar, then the long-lived HTTP worker. Never echo nsecs.
set -euo pipefail
export GOOSE_PROVIDER="${GOOSE_PROVIDER:-litellm}"
export GOOSE_MODEL="${GOOSE_MODEL:-goose}"
export GOOSE_MODE="${GOOSE_MODE:-auto}"
export GOOSE_DISABLE_KEYRING=1
export GOOSE_TELEMETRY_ENABLED=false
export LITELLM_HOST="http://127.0.0.1:4000"
export LITELLM_BASE_PATH="${LITELLM_BASE_PATH:-v1/chat/completions}"
export LITELLM_TIMEOUT="${LITELLM_TIMEOUT:-120}"
if [[ "${BUZZ_RELAY_URL:-}" == wss://* ]]; then
  export BUZZ_RELAY_URL="https://${BUZZ_RELAY_URL#wss://}"
elif [[ "${BUZZ_RELAY_URL:-}" == ws://* ]]; then
  export BUZZ_RELAY_URL="http://${BUZZ_RELAY_URL#ws://}"
fi
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PLAYWRIGHT_CHROMIUM_SANDBOX=0

python3 /app/litellm_proxy.py &
proxy_pid=$!
trap 'kill ${proxy_pid} 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do
  if curl -fsS --max-time 1 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done
if ! curl -fsS --max-time 1 http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then
  echo "litellm sidecar failed to listen on 127.0.0.1:4000" >&2
  exit 1
fi

export GOOSE_DISABLE_SESSION_NAMING=true
export GOOSE_RECIPE_PATH="${GOOSE_RECIPE_PATH:-/home/goose/recipes}"
if [[ -z "${GOOSE_MOIM_MESSAGE_FILE:-}" && -f "${HOME}/.config/goose/guardrails.md" ]]; then
  export GOOSE_MOIM_MESSAGE_FILE="${HOME}/.config/goose/guardrails.md"
fi
exec python3 /app/worker.py
