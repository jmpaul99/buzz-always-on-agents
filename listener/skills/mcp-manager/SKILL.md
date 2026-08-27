---
name: mcp-manager
description: Enable, disable, or register extra MCP servers for this cloud Buzz agent. Use when a mention needs GitHub, Stripe, Tavily, Google Workspace, or another catalog extra.
---

# MCP extras

This agent has one attached MCP (the multiplexer). Always-on tools come from `buzz-dev-mcp` (shell, files, `buzz` CLI). Extra servers are off until you enable them.

Cap: **2 extras** per agent (e2-micro RAM). Do not invent tool names.

## Workflow

1. Call `mcp_list` to see shipped + overlay extras, enabled state, and missing env keys.
2. If the extra exists, `mcp_enable` with its `slug`. Use that result's tool names (`slug__tool`).
3. If it is not in the list, `mcp_register` with a spawn spec (`npx` / `uv` / `uvx` / `python` / `python3`), then `mcp_enable`.
4. `mcp_disable` when done if you need the slot. You cannot disable `buzz-dev-mcp`.

Enable persists for this agent. Tools are guaranteed on the **next** mention; this turn may still call names returned by `mcp_enable`.

Missing env keys mean the operator must add the secret (`infra/create-secrets.ps1` + `deploy-listener.ps1`). Do not dump env or secrets.
