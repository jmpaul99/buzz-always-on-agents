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
SKILL_SRC="/opt/buzz-listener/skills/mcp-manager/SKILL.md"
SKILL_DST="${WORKSPACE}/agents/${NAME}/.agents/skills/mcp-manager/SKILL.md"
if [[ -f "${SKILL_SRC}" ]]; then
  mkdir -p "$(dirname "${SKILL_DST}")"
  cp "${SKILL_SRC}" "${SKILL_DST}"
fi
PROMPT="${WORKSPACE}/agents/${NAME}/system-prompt.txt"
INST="/etc/buzz/${NAME}.instructions"
TEAM="/etc/buzz/${NAME}.team"
: >"${PROMPT}"
cat >>"${PROMPT}" <<'EOF'
## Channel delivery

Desktop Agent Activity is ACP session text. It is not a channel or thread post. Phone users never see it.

The always-on shell tool is registered as `run-mcp__shell` (also `run-mcp__read_file`, `run-mcp__mcp_list`, `run-mcp__mcp_enable`, …). Bare `shell` / `read_file` fail with unknown tool.

If this turn produced an answer a human asked for, you MUST publish it before ending:

  run-mcp__shell  command: buzz messages send --channel <uuid from <context>> --content '...'

Use the reply destination from `<context>`. For multiline content, pass real newlines on stdin:

  printf 'line1\n\nline2\n' | buzz messages send --channel <uuid> --content -

Do not end the turn with only assistant text.

EOF
if [[ -f "${TEAM}" ]]; then
  cat "${TEAM}" >>"${PROMPT}"
  printf '\n' >>"${PROMPT}"
fi
if [[ -f "${INST}" ]]; then
  cat "${INST}" >>"${PROMPT}"
fi
chmod 600 "${PROMPT}"
export BUZZ_AGENT_SYSTEM_PROMPT_FILE="${PROMPT}"

export BUZZ_ACP_AGENT_COMMAND="${BUZZ_ACP_AGENT_COMMAND:-buzz-agent}"
export BUZZ_ACP_AGENT_ARGS="${BUZZ_ACP_AGENT_ARGS:-}"
export BUZZ_ACP_MCP_COMMAND="${BUZZ_ACP_MCP_COMMAND:-/opt/buzz-listener/run-mcp.sh}"
export BUZZ_AGENT_PROVIDER="${BUZZ_AGENT_PROVIDER:-openai}"
export OPENAI_COMPAT_BASE_URL="${OPENAI_COMPAT_BASE_URL:-http://127.0.0.1:4000/v1}"
export OPENAI_COMPAT_MODEL="${OPENAI_COMPAT_MODEL:-cloud}"
export OPENAI_COMPAT_API="${OPENAI_COMPAT_API:-chat}"
export BUZZ_AGENT_REQUIRE_REPLY="${BUZZ_AGENT_REQUIRE_REPLY:-1}"
# buzz-agent only invokes _Stop when this allowlist is set. The multiplexer
# objects if the turn never ran `buzz messages send` / `reactions add`.
export MCP_HOOK_SERVERS="${MCP_HOOK_SERVERS:-*}"
export LISTENER_CONTROL_URL="${LISTENER_CONTROL_URL:-http://127.0.0.1:8743}"
export BUZZ_WORKSPACE="${WORKSPACE}"
export AGENT_NAME="${NAME}"
# Match Desktop remote launch.policy_env (block/buzz agents_deploy.rs).
export BUZZ_ACP_RELAY_OBSERVER="${BUZZ_ACP_RELAY_OBSERVER:-true}"
export BUZZ_ACP_LAZY_POOL="${BUZZ_ACP_LAZY_POOL:-true}"
export BUZZ_ACP_IDLE_POOL_SLEEP="${BUZZ_ACP_IDLE_POOL_SLEEP:-900}"
export BUZZ_ACP_AGENTS="${BUZZ_ACP_AGENTS:-1}"
export RUST_LOG="${RUST_LOG:-info,buzz_acp=info}"
if [[ -n "${BUZZ_ACP_DISPLAY_NAME:-}" ]]; then
  export BUZZ_ACP_SESSION_TITLE="${BUZZ_ACP_SESSION_TITLE:-${BUZZ_ACP_DISPLAY_NAME}}"
fi
unset BUZZ_ACP_NO_PRESENCE || true

cd "${WORKSPACE}/agents/${NAME}"
exec buzz-acp
