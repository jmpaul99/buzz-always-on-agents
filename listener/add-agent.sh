#!/usr/bin/env bash
# Write one agent env and start buzz-acp@<slug>. Never echo nsecs.
# Usage: sudo /opt/buzz-listener/add-agent.sh <slug>
set -euo pipefail

NAME="${1:-}"
if [[ -z "${NAME}" ]]; then
  echo "usage: add-agent.sh <slug>" >&2
  exit 2
fi
if [[ ! "${NAME}" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
  echo "invalid agent name" >&2
  exit 2
fi
if [[ -z "${BUZZ_PRIVATE_KEY:-}" ]]; then
  echo "BUZZ_PRIVATE_KEY is required" >&2
  exit 2
fi
if [[ ! "${BUZZ_PRIVATE_KEY}" =~ ^nsec1 ]] && [[ ${#BUZZ_PRIVATE_KEY} -ne 64 ]]; then
  echo "BUZZ_PRIVATE_KEY must be nsec1 or 64-char hex" >&2
  exit 2
fi
if [[ -z "${BUZZ_RELAY_URL:-}" ]]; then
  echo "BUZZ_RELAY_URL is required" >&2
  exit 2
fi

RELAY_URL="${BUZZ_RELAY_URL}"
AUTH_TAG="${BUZZ_AUTH_TAG:-}"
DISPLAY="${BUZZ_ACP_DISPLAY_NAME:-${NAME}}"
PUBKEY="${BUZZ_PUBKEY:-}"
RESPOND_TO="${BUZZ_ACP_RESPOND_TO:-owner-only}"
ALLOWLIST="${BUZZ_ACP_RESPOND_TO_ALLOWLIST:-}"
TEAM_ID="${BUZZ_TEAM_ID:-}"
UPDATED_AT="${BUZZ_UPDATED_AT:-}"
CHANNEL_ALLOWLIST="${BUZZ_CHANNEL_ALLOWLIST:-}"
if [[ -z "${UPDATED_AT}" ]]; then
  UPDATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
fi
ENV_FILE="/etc/buzz/${NAME}.env"
INSTRUCTIONS_FILE="/etc/buzz/${NAME}.instructions"
install -d -m 700 /etc/buzz
umask 077
cat >"${ENV_FILE}" <<EOF
BUZZ_RELAY_URL=${RELAY_URL}
BUZZ_PRIVATE_KEY=${BUZZ_PRIVATE_KEY}
BUZZ_AUTH_TAG=${AUTH_TAG}
BUZZ_ACP_DISPLAY_NAME=${DISPLAY}
BUZZ_PUBKEY=${PUBKEY}
BUZZ_ACP_RESPOND_TO=${RESPOND_TO}
BUZZ_ACP_RESPOND_TO_ALLOWLIST=${ALLOWLIST}
BUZZ_TEAM_ID=${TEAM_ID}
BUZZ_UPDATED_AT=${UPDATED_AT}
BUZZ_CHANNEL_ALLOWLIST=${CHANNEL_ALLOWLIST}
EOF
chmod 600 "${ENV_FILE}"
chown root:root "${ENV_FILE}"
if [[ -n "${BUZZ_ACP_SYSTEM_PROMPT+x}" ]]; then
  printf '%s\n' "${BUZZ_ACP_SYSTEM_PROMPT}" >"${INSTRUCTIONS_FILE}"
  chmod 600 "${INSTRUCTIONS_FILE}"
  chown root:root "${INSTRUCTIONS_FILE}"
fi
TEAM_FILE="/etc/buzz/${NAME}.team"
if [[ -n "${BUZZ_ACP_TEAM_INSTRUCTIONS+x}" ]]; then
  if [[ -z "${BUZZ_ACP_TEAM_INSTRUCTIONS}" ]]; then
    rm -f "${TEAM_FILE}"
  else
    printf '%s\n' "${BUZZ_ACP_TEAM_INSTRUCTIONS}" >"${TEAM_FILE}"
    chmod 600 "${TEAM_FILE}"
    chown root:root "${TEAM_FILE}"
  fi
fi

systemctl enable --now buzz-litellm-proxy.service >/dev/null 2>&1 || true
systemctl enable --now buzz-listener.service
systemctl enable "buzz-acp@${NAME}.service"
systemctl restart "buzz-acp@${NAME}.service"
echo "started buzz-acp@${NAME}"
