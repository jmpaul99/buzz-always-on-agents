#!/usr/bin/env bash
# Single MCP slot for buzz-acp: multiplexer that proxies buzz-dev-mcp.
set -euo pipefail
export PATH="/opt/sprig:/usr/local/bin:${PATH:-/usr/bin:/bin}"
exec /opt/buzz-listener/.venv/bin/python /opt/buzz/local-mcp/mcp_manager.py
