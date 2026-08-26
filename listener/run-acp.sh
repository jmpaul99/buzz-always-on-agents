#!/usr/bin/env bash
# Spawn stock buzz-acp for one /etc/buzz/<slug>.env agent. Never echo nsecs.
set -euo pipefail

NAME="${1:-}"
if [[ -z "${NAME}" ]]; then
  echo "usage: run-acp.sh <slug>" >&2
  exit 2
fi
if [[ ! "${NAME}" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
  echo "invalid agent name" >&2
  exit 2
fi

export PATH="/opt/sprig:/usr/local/bin:${PATH:-/usr/bin:/bin}"

case "${BUZZ_ACP_RESPOND_TO:-owner-only}" in
  everyone|all) export BUZZ_ACP_RESPOND_TO=anyone ;;
  owner) export BUZZ_ACP_RESPOND_TO=owner-only ;;
esac

WORKSPACE="${BUZZ_WORKSPACE:-/var/lib/buzz-listener}"
mkdir -p "${WORKSPACE}/agents/${NAME}"
PROMPT="${WORKSPACE}/agents/${NAME}/system-prompt.txt"
INST="/etc/buzz/${NAME}.instructions"
TEAM="/etc/buzz/${NAME}.team"
if [[ -f "${INST}" || -f "${TEAM}" ]]; then
  : >"${PROMPT}"
  if [[ -f "${TEAM}" ]]; then
    cat "${TEAM}" >>"${PROMPT}"
    printf '\n' >>"${PROMPT}"
  fi
  if [[ -f "${INST}" ]]; then
    cat "${INST}" >>"${PROMPT}"
  fi
  chmod 600 "${PROMPT}"
  export BUZZ_AGENT_SYSTEM_PROMPT_FILE="${PROMPT}"
fi

export BUZZ_ACP_AGENT_COMMAND="${BUZZ_ACP_AGENT_COMMAND:-buzz-agent}"
export BUZZ_ACP_AGENT_ARGS="${BUZZ_ACP_AGENT_ARGS:-}"
export BUZZ_ACP_MCP_COMMAND="${BUZZ_ACP_MCP_COMMAND:-buzz-dev-mcp}"
export BUZZ_AGENT_PROVIDER="${BUZZ_AGENT_PROVIDER:-openai}"
export OPENAI_COMPAT_BASE_URL="${OPENAI_COMPAT_BASE_URL:-http://127.0.0.1:4000/v1}"
export OPENAI_COMPAT_MODEL="${OPENAI_COMPAT_MODEL:-goose}"
export OPENAI_COMPAT_API="${OPENAI_COMPAT_API:-chat}"
export BUZZ_AGENT_REQUIRE_REPLY="${BUZZ_AGENT_REQUIRE_REPLY:-1}"
export LISTENER_CONTROL_URL="${LISTENER_CONTROL_URL:-http://127.0.0.1:8743}"
export BUZZ_WORKSPACE="${WORKSPACE}"
export AGENT_NAME="${NAME}"

cd "${WORKSPACE}/agents/${NAME}"
exec buzz-acp
