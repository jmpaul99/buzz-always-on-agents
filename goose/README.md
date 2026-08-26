# Goose (retired)

The Cloud Run Goose + Playwright worker is gone. Runtime is **stock buzz-acp + buzz-agent** on the e2-micro. MCP spawn specs live in [`listener/mcp-catalog.json`](../listener/mcp-catalog.json) (always-on `buzz-dev-mcp`; extras disabled; no `playwright` / `chromedevtools` / `goosedocs`).

[`local-mcp/`](local-mcp/README.md) is the optional Google Workspace extra (`googleadc`), still off by default.
