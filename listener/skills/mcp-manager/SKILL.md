---
name: mcp-manager
description: Enable, disable, or register extra MCP servers for this cloud Buzz agent. Use when a mention needs GitHub, Stripe, Tavily, Google Workspace, or another catalog extra.
---

# MCP extras

This agent has one attached MCP (the multiplexer). **Always-on** tools come from `buzz-dev-mcp` (`shell`, files, `todo`, `buzz` CLI). They are not in the extra cap and cannot be disabled. Extra servers are off until you enable them.

Cap: **2 extras** per agent (e2-micro RAM). Do not invent tool names.

Replies use the registered tool `run-mcp__shell` running `buzz messages send`. Bare `shell` is unknown and fails. ACP Activity is not a channel post — never skip `buzz messages send` after enabling an extra.

Extra API tokens are **not** in `shell` env. Do not `printenv`, `grep` tokens, or read `/etc/buzz/_runtime.env` / ADC files. `mcp_list` `missing_env_keys` is the only credential signal.

## Workflow

1. Call `mcp_list`. Read `always_on` plus each extra's `status` (`off` / `starting` / `running` / `failed`), `tool_count`, and `last_error`.
2. If the extra exists and `status` is `off`, `mcp_enable` with its `slug`. Enable returns immediately (`starting: true`) so `run-mcp__shell` still works this turn.
3. Call `mcp_tools` with that `slug`. If it returns `status: failed` or `tools: []` with an `error`, the extra did not start — tell the human `last_error` and keep using `run-mcp__shell`. Do not debug env.
4. Prefixed names (`slug_tool`) stay callable when `status` is `running`, even off the current `tools/list` page. Page with `cursor` from `next_cursor`.
5. If it is not in the list, `mcp_register` with a spawn spec (`npx` / `uv` / `uvx` / `python` / `python3`), then `mcp_enable`.
6. `mcp_disable` when done if you need the slot. You cannot disable `buzz-dev-mcp`.

Enable persists only after spawn succeeds. Failed extras are not left "enabled".
