#!/usr/bin/env bash
# Defeat e2-micro idle reclaim: a little CPU + outbound traffic once a day.
set -euo pipefail
dd if=/dev/urandom of=/dev/null bs=1M count=32 status=none || true
curl -fsS --max-time 10 https://www.google.com >/dev/null || true
date -u +%FT%TZ > /var/lib/buzz-listener/keepalive.stamp
