# Listener

Thin control API on the GCP `e2-micro` plus one systemd `buzz-acp@<slug>` per `/etc/buzz/*.env` agent. Mentions are handled by **stock buzz-acp** (native standing prompt, ACP Activity). This process only serves Desktop roster/nsec sync on `:8743`.

Installed to `/opt/buzz-listener` by [`infra/deploy-listener.ps1`](../infra/deploy-listener.ps1).

## Layout on the VM

| Unit | Role |
| --- | --- |
| `buzz-listener.service` | Control API (`0.0.0.0:8743`) |
| `buzz-litellm-proxy.service` | Localhost IAM proxy to Cloud Run LiteLLM (`127.0.0.1:4000`) |
| `buzz-acp@<slug>.service` | One WSS + ACP client per agent; child is `buzz-agent` (sprig) |
| `buzz-keepalive.timer` | Daily idle-reclaim bump for the free e2-micro |

Sprig multicall links (`buzz`, `buzz-acp`, `buzz-agent`, `buzz-dev-mcp`) live in `/opt/sprig` and `/usr/local/bin`. `buzz-cloud-agents` is `/opt/buzz-listener/cloud_agents.py`.

## Agent files

Written by the control API, `add-agent.sh`, or Desktop sync. Mode `700` on `/etc/buzz`, `600` on files.

`/etc/buzz/<slug>.env`:

| Key | Meaning |
| --- | --- |
| `BUZZ_RELAY_URL` | Community WSS URL |
| `BUZZ_PRIVATE_KEY` | `nsec1…` or 64-char hex. Never logged. |
| `BUZZ_AUTH_TAG` | Raw JSON tags; owner pubkey is the `auth` tag |
| `BUZZ_ACP_DISPLAY_NAME` | Display name |
| `BUZZ_PUBKEY` | Declared hex pubkey (mismatch → key-derived pubkey wins) |
| `BUZZ_ACP_RESPOND_TO` | `owner-only` (default), `allowlist`, or `anyone` |
| `BUZZ_ACP_RESPOND_TO_ALLOWLIST` | Comma-separated pubkeys |
| `BUZZ_TEAM_ID` | Desktop team id |
| `BUZZ_UPDATED_AT` | RFC3339; last-write-wins with Desktop |
| `BUZZ_CHANNEL_ALLOWLIST` | Optional comma-separated channel ids |

`/etc/buzz/<slug>.instructions` is the system prompt (max 8000 chars when loaded). Concatenated with `.team` into `BUZZ_AGENT_SYSTEM_PROMPT_FILE` when `buzz-acp@` starts. Core / huddle / canvas / thread-or-DM standing context is **inside buzz-acp**, not cloned here.

Slug: `[a-z0-9][a-z0-9-]{0,31}`. If the display-name slug is already another pubkey, the API suffixes `-{pubkey[:8]}`.

Shared runtime (`/etc/buzz/_runtime.env`, not an agent): LiteLLM URL + master key, `OPENAI_COMPAT_*`, `APPLY_SA`, `LISTENER_CONTROL_URL=http://127.0.0.1:8743`.

## Control API (`0.0.0.0:8743`)

Firewall: IAP range `35.235.240.0/20` (`allow-iap-8743`). Sidecar token: `/etc/buzz/_sync.token` (`Authorization: Bearer …` or `X-Buzz-Sync-Token`). Chat apply (`buzz-cloud-agents` on this VM): Google ID token from `APPLY_SA` (listener SA) with audience `LISTENER_CONTROL_URL`. Unauthenticated `/health` and `/healthz` return `{"ok":true}`.

| Method | Path | Body / result |
| --- | --- | --- |
| `GET` | `/health`, `/healthz` | `{"ok":true}` (no auth) |
| `GET` | `/agents` | Roster for Desktop sync: slug, pubkey, display, prompt, permissions, `owner`, `updated_at`, `nsec`, `auth_tag`, `team_instructions`. **Sidecar token only**. Never log the body. |
| `GET` | `/agents/index` | Apply-only names: `{pubkey, slug, name}` (no nsec). |
| `POST` | `/agents` | Chat apply create. Mints nsec, owner-only. Apply ID token. Body: `author_pubkey`, `actor_slug` or `actor_pubkey`, `name`, `system_prompt`. Returns `{ok, agent_id, pubkey}` (no nsec). |
| `PUT` | `/agents/{pubkey}` | Sidecar upsert **or** apply update. Sidecar: `nsec` required on create; omitted on update keeps the existing key. Apply: owner-gated; cannot set `nsec`. PUT/DELETE start or stop `buzz-acp@<slug>`. |
| `DELETE` | `/agents/{pubkey}` | Sidecar token. Removes `.env`, `.instructions`, `.team`, and disables the unit. Idempotent. |

Sidecar PUT JSON fields: `nsec` / `private_key_nsec`, `name`, `slug`, `system_prompt`, `respond_to`, `respond_to_allowlist`, `team_id`, `team_instructions`, `auth_tag`, `relay_url` / `relay`, `updated_at`, `channel_allowlist`. Payload cap 512 KiB.

## MCP catalog

[`mcp-catalog.json`](mcp-catalog.json) is not in Buzz Desktop. Native buzz-acp has one stdio MCP slot, so [`run-mcp.sh`](run-mcp.sh) attaches [`local-mcp/mcp_manager.py`](local-mcp/mcp_manager.py). That process proxies **always-on** `buzz-dev-mcp` (shell / files / `buzz` CLI) and can spawn extras.

**Dropped:** `playwright`, `chromedevtools`. No Chromium on the micro.

**Extras** (github, stripe, tavilywebsearch, googleadc, containeruse, linuxmcpserver, repomix, youtubetranscript) ship **disabled** in the committed catalog. Do not flip `enabled: true` there (LiteLLM keyword merge skips enabled extras). Agents call `mcp_list` / `mcp_enable` / `mcp_disable` / `mcp_register` / `mcp_tools`. Enablement is **per agent**, cap **2** extras (`MAX_ENABLED`; always-on is not in the cap), persisted in `/var/lib/buzz-listener/agents/<slug>/mcp-enabled.json` **only after spawn succeeds**. Failed extras are unpinned and show `status: failed` plus `last_error`. `mcp_register` appends `/etc/buzz/_mcp-overlay.json` (survives listener redeploy; the shipped catalog is overwritten). Extra tool names are `{slug}_{tool}` (a single underscore — buzz-agent rejects bare names containing `__`). `mcp_enable` spawns in the background so `shell` / `buzz messages send` still work this turn. `tools/list` advertises manager + always-on + one extra page (12); off-page extra names stay callable. `mcp_tools` pages the rest. Extra tokens are not in `shell` env.

HTTP Stripe/containeruse use `npx mcp-remote` and need a current Node LTS (undici). GitHub is the official `github-mcp-server` binary (`stdio`, `GITHUB_TOOLSETS=repos`). Node.js 24 LTS and `uv` are installed on the micro so those spawn specs can run. Google Workspace spawn spec is [`local-mcp/`](local-mcp/README.md) with `--suite gmail` by default. Overlay slugs do **not** update Cloud Run COMPLEX keywords until the next LiteLLM image build (keywords come from the committed catalog only).

Spawn follows the same trusted/untrusted boundary as [block/buzz#6651](https://github.com/block/buzz/pull/6651): `buzz-agent` only forwards Buzz identity into the multiplexer; extra keys (`GITHUB_PERSONAL_ACCESS_TOKEN`, `GOOGLE_CLOUD_PROJECT`, …) are reloaded from `/etc/buzz/_runtime.env`. Only the proxied `buzz-dev-mcp` child receives `BUZZ_PRIVATE_KEY` / `BUZZ_RELAY_URL` / `BUZZ_AUTH_TAG`. Extras get declared API keys only. Child start failures fail closed and do not echo argv (commands may embed keys). This repo cannot use `BUZZ_ACP_EXTRA_MCP_COMMANDS` until that PR is in `sprig-latest`.

A short skill is copied to `$WORKSPACE/agents/<slug>/.agents/skills/mcp-manager/SKILL.md` on `buzz-acp@` start.

## LiteLLM

`buzz-agent` uses `BUZZ_AGENT_PROVIDER=openai` and `OPENAI_COMPAT_*` against `http://127.0.0.1:4000/v1` (`model=cloud`, `OPENAI_COMPAT_API=chat`). The proxy mints a GCE identity token and forwards a buffered JSON body (`Content-Length`). `buzz-agent` hardcodes `stream: false`. Desktop Agent Activity is the native relay observer (`BUZZ_ACP_RELAY_OBSERVER=true` in `run-acp.sh`, matching Desktop remote `launch.policy_env`), not a local ACP stdio wrap.

`BUZZ_AGENT_REQUIRE_REPLY=1` so a turn with no `buzz messages send` / `reactions add` gets a reminder. `MCP_HOOK_SERVERS=*` so the multiplexer `_Stop` hook can object with the exact tool name (`run-mcp__shell` — bare `shell` is unknown). ACP Activity is not a channel post; `run-acp.sh` prepends that to the system prompt.

## systemd

`buzz-listener.service` always runs (Desktop sync needs `:8743` even with zero agents). `add-agent.sh` writes the env and `systemctl enable --now buzz-acp@<slug>`. `remove-agent.sh` disables the unit then deletes files.

`buzz-keepalive.timer` (daily, 1h jitter) runs `keepalive.sh`: 32 MiB `/dev/urandom` → `/dev/null` plus a GET to google.com, so the free e2-micro is not idle-reclaimed.

## Scripts on the VM

```bash
# Env vars in the process; never echo nsecs
sudo -E /opt/buzz-listener/add-agent.sh <slug>
sudo /opt/buzz-listener/remove-agent.sh <slug>
```

`add-agent.sh` requires `BUZZ_PRIVATE_KEY` and `BUZZ_RELAY_URL`. Optional: `BUZZ_AUTH_TAG`, `BUZZ_ACP_DISPLAY_NAME`, `BUZZ_PUBKEY`, `BUZZ_ACP_RESPOND_TO`, `BUZZ_ACP_RESPOND_TO_ALLOWLIST`, `BUZZ_TEAM_ID`, `BUZZ_UPDATED_AT`, `BUZZ_CHANNEL_ALLOWLIST`, `BUZZ_ACP_SYSTEM_PROMPT`.

## Modules

| File | Role |
| --- | --- |
| `listener.py` | Control API; starts/stops `buzz-acp@` on PUT/DELETE |
| `run-acp.sh` | Env mapping then `exec buzz-acp` (stock `buzz-agent`) |
| `run-mcp.sh` | Stdio multiplexer (`mcp_manager.py`) |
| `litellm_proxy.py` | Localhost IAM proxy to Cloud Run LiteLLM |
| `cloud_agents.py` | Chat confirm create/update (`buzz-cloud-agents`) |
| `agentutil.py` | Env records, permissions, Desktop merge helpers |
| `nostrutil.py` | nsec decode, schnorr sign |
| `mcp_catalog.py` / `mcp-catalog.json` | Shipped always-on + extras; overlay merge; enable-set helpers |
| `local-mcp/mcp_manager.py` | Proxies `buzz-dev-mcp`; list/enable/disable/register/page extras |
| `local-mcp/google_adc_mcp.py` | Optional Google Workspace extra (`googleadc`) |
| `skills/mcp-manager/SKILL.md` | Copied into each agent cwd for `load_skill` |

`agentutil.py` is also copied next to the Windows and macOS sync sidecars so Desktop and the VM share slug/allowlist/roster merge rules.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `BUZZ_AGENTS_DIR` | `/etc/buzz` | Agent env + instructions |
| `BUZZ_STATE_DIR` | `/var/lib/buzz-listener` | Workspace + generated prompts |
| `BUZZ_RELAY_URL` | from env files | Fallback relay |
| `BUZZ_CONTROL_HOST` | `0.0.0.0` | Control API bind |
| `BUZZ_CONTROL_PORT` | `8743` | Control API port |
| `APPLY_SA` | listener SA email | Google ID token email for chat apply |
| `LISTENER_CONTROL_URL` | `http://127.0.0.1:8743` | Apply audience + `buzz-cloud-agents` |

## Tests

From the repo root:

```powershell
python -m unittest discover -s tests/listener
```

No GCP. `test_agentutil.py` covers Desktop compact/merge and multi-Desktop roster import/delete. `test_control.py` covers sidecar vs apply tokens. `test_mcp_catalog.py` asserts no browser slugs, disabled extras, overlay merge, register guards, and extra paging. `test_mcp_manager.py` drives a fake stdio child for list/enable/disable/register, always-on pinning, and extra tool pages.
