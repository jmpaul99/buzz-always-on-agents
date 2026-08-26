#!/usr/bin/env bash
# Remove one agent env + instructions. Listener hot-reloads and drops the
# WSS loop. Never echo nsecs.
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

rm -f "/etc/buzz/${NAME}.env" "/etc/buzz/${NAME}.instructions" "/etc/buzz/${NAME}.team"
echo "removed ${NAME}"
