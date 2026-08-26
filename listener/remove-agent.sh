#!/usr/bin/env bash
# Stop buzz-acp@<slug> and remove env + instructions. Never echo nsecs.
# Usage: sudo /opt/buzz-listener/remove-agent.sh <slug>
set -euo pipefail

NAME="${1:-}"
if [[ -z "${NAME}" ]]; then
  echo "usage: remove-agent.sh <slug>" >&2
  exit 2
fi
if [[ ! "${NAME}" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]]; then
  echo "invalid agent name" >&2
  exit 2
fi

systemctl disable --now "buzz-acp@${NAME}.service" >/dev/null 2>&1 || true
rm -f "/etc/buzz/${NAME}.env" "/etc/buzz/${NAME}.instructions" "/etc/buzz/${NAME}.team"
echo "removed ${NAME}"
