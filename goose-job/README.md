# Goose worker image

Playwright + Goose + sprig (`buzz`) image for Cloud Run **service** `goose-worker`. The listener `POST`s `/run`; `entrypoint.sh` starts the LiteLLM sidecar then `worker.py`.

The HTTP worker stays up so a follow-up DM can reuse the container (min instances 0, max 1, concurrency 16).

Image: `linux/amd64` only (`mcr.microsoft.com/playwright:v1.55.1-noble`). Built by [`infra/cloudbuild-goose.yaml`](../infra/cloudbuild-goose.yaml) from the repo root (`-f goose-job/Dockerfile`).

## Container layout

- User `goose` (uid 10001), `HOME=/home/goose`
- `/opt/goose/goose` and `/opt/sprig/sprig` (symlinked as `buzz`)
- `/app/entrypoint.sh` starts a localhost LiteLLM sidecar (`litellm_proxy.py` on `:4000`), waits for `/health/liveliness`, then execs `worker.py`
- Goose config/hints/guardrails copied from [`goose/`](../goose/README.md)
- Recipes generated at build: `python3 generate_recipes.py /home/goose/.config/goose/config.yaml /home/goose/recipes` (image only; `listener/task-mcps.json` is a separate committed catalog)
- Google Workspace MCP: `/opt/buzz/local-mcp/google_adc_mcp.py`

Goose talks to `http://127.0.0.1:4000` (non-streaming). The sidecar adds a Cloud Run identity token and forwards to `LITELLM_URL`.

## Worker HTTP API (`worker.py`)

Bind `0.0.0.0:$PORT` (Cloud Run injects `PORT`, default 8080). Invoker IAM only (listener SA).

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/`, `/health`, `/healthz` | Queue snapshot: `max_parallel`, `running`, `queued` |
| `POST` | `/run` | One Goose turn. Waits until that turn finishes (up to `GOOSE_TIMEOUT_SECS + 30`). |

POST JSON:

```json
{
  "agent_name": "slug",
  "prompt": "…",
  "recipe": "playwright",
  "env": {
    "AGENT_NAME": "slug",
    "BUZZ_PRIVATE_KEY": "nsec1…",
    "BUZZ_AUTH_TAG": "…",
    "BUZZ_RELAY_URL": "wss://…",
    "BUZZ_CHANNEL_ID": "…",
    "BUZZ_EVENT_ID": "…",
    "REPLY_TO": "",
    "PROMPT": "…",
    "BUZZ_OWNER_PUBKEY": "…",
    "BUZZ_AUTHOR_PUBKEY": "…",
    "BUZZ_MESSAGE": "…",
    "BUZZ_IDENTITY": "…",
    "BUZZ_SEND_CMD": "buzz messages send --channel … --content '<your-reply>'",
    "BUZZ_TEAM_INSTRUCTIONS": "…",
    "BUZZ_WORKSPACE": "/mnt/buzz",
    "GOOSE_RECIPE": "playwright"
  }
}
```

Only the `PASS_ENV` keys are copied. Prompt cap 20 000. Payload cap 512 KiB. Returns `200` / `500` / `504` with `{ok, agent, returncode, error}`. Never logs nsecs or prompts.

## Scheduling

- At most `GOOSE_MAX_PARALLEL` (default 2) Goose processes.
- **One turn at a time per agent.** Extra mentions for the same agent queue FIFO. Other agents can run in parallel.
- Cloud Run concurrency is 16 so a multi-mention can POST `/run` for every tagged agent; extras wait in the worker queue, not at Cloud Run's 429 boundary.
- Relay URLs are rewritten `wss://` → `https://` for `buzz` HTTP.

Isolation: each agent gets `/tmp/goose-<slug>` with its own `HOME`, `XDG_CONFIG_HOME`, and npm cache. `agenthome.sync_agent_home` copies config, `.goosehints`, `guardrails.md`, and gcloud ADC into that HOME. The worker then writes `tom.md` (guardrails plus standing sections from `buzz mem` / canvas / huddle / recent channel or thread / team / workspace) and points `GOOSE_MOIM_MESSAGE_FILE` at it. cwd is `/mnt/buzz/agents/<slug>` when the GCS volume is mounted; HOME stays under `/tmp`. Channel work goes in `/mnt/buzz/channels/<id>/` (created on the first turn in that huddle). `shared/` is for files every agent in every channel should see.

## Timeouts

| Knob | Default | Behavior |
| --- | --- | --- |
| `GOOSE_TIMEOUT_SECS` | 1500 | Hard kill of the Goose process |
| `GOOSE_IDLE_TIMEOUT_SECS` | 180 | Kill if no parser/LLM/tool activity (hang). Success if a channel send already happened. |
| Fallback send | after Goose exits | If no channel send was seen, worker posts Goose's last text (or a short notice) |
| Cloud Run request timeout | 3600s | Service deploy |

Activity is Goose stdout **or** LiteLLM sidecar `/activity` (`in_flight > 0`). Liveness frames go to the observer every 10s while a turn is running.

## Recipes vs generic path

`build_goose_cmd`:

- If `GOOSE_RECIPE` matches `/home/goose/recipes/<slug>/recipe.yaml`, run `goose run --recipe … --params identity=… message=… send_cmd=…` (task MCP already enabled in that recipe).
- Else the generated `reply` recipe (always-on extensions + send-required instructions). `-t` is only a last resort if that file is missing.

`recipe_params` builds `send_cmd` as `buzz messages send --channel … --content '<your-reply>'` (plus `--reply-to` when set). Goose must replace `<your-reply>`; it must not send that placeholder, `...`, or an empty message. `BUZZ_IDENTITY` already includes the listener turn hint (multi-mention: reply as yourself this turn).

Always `--no-session --quiet --output-format stream-json`. Quiet drops recipe-load TUI; stream-json still carries thoughts/tools. Goose is attached to a PTY so JSON is not block-buffered (a pipe left `json=0 bytes=0` until exit, which is why activity arrived after the channel reply and Goose sent twice). The worker does **not** stop on the first or second channel send. It waits until Goose exits, idle-timeouts (no LLM/tools/parser activity), or `GOOSE_TIMEOUT_SECS`. `GOOSE_CLI_SHOW_THINKING=1` and `GOOSE_THINKING_EFFORT=low`.

## Observer (kind 24200)

`observer.py` keeps **one WSS per agent** (kind 24200 is ephemeral; HTTP publish is rejected). TLS + NIP-42 AUTH start as soon as `/run` arrives (before the per-agent queue). Goose waits until AUTH so activity is live before the channel reply; that wait is usually ~0 because AUTH overlapped the queue + home sync, and follow-up DMs reuse the socket. Idle sockets (Cloud Run pauses CPU between requests) reconnect on the next `/run` instead of failing mid-turn. Logs `tls_ms`, `auth_ms`, `ready_ms`, `wait_ms`, and `first_flush lag_ms`. Frames are NIP-44 encrypted to the owner (`auth` tag), tagged `agent=<pubkey> frame=telemetry p=<owner>`.

`activity.py` turns Goose CLI / `stream-json` into compact thought + tool_call events. nsecs and `BUZZ_PRIVATE_KEY=…` style assignments are redacted. Desktop Agent Activity decrypts these.

## LiteLLM sidecar (`litellm_proxy.py`)

Goose’s LiteLLM client is non-streaming: it POSTs and waits for one JSON body. The sidecar:

- Mints `identity?audience=LITELLM_AUDIENCE` (defaults to `LITELLM_URL`)
- Sends `Authorization: Bearer <master> ` plus the Google token as Cloud Run requires
- Forces `stream: false` and **close-delimits** the buffered JSON (hop-by-hop `Transfer-Encoding: chunked` would hang Goose after a tool call)
- Rewrites model-emitted tool names back onto the names Goose offered (prefix / uniqueness)

`GET /activity` reports `{in_flight}` so the worker does not idle-timeout mid-completion.

## Modules

| File | Role |
| --- | --- |
| `entrypoint.sh` | Sidecar + worker. Never echoes nsecs. |
| `worker.py` | HTTP server, per-agent queues, Goose spawn |
| `agenthome.py` | Copy config into isolated HOME |
| `activity.py` | Stdout → observer events + redact (`--help` is shown; env dumps are not) |
| `memory.py` | `buzz mem get core` plus canvas/huddle/recent channel or thread/team/workspace → `tom.md` |
| `cloud_agents.py` | `buzz-cloud-agents` propose/apply/cancel (chat confirm → listener mint/update) |
| `observer.py` | Kind 24200 WSS publisher |
| `nip44.py` | NIP-44 v2 encrypt for observer payloads |
| `litellm_proxy.py` | Localhost → Cloud Run LiteLLM |

`listener/nostrutil.py` is copied into `/app` for observer AUTH/sign.

## Deploy knobs

Set in [`infra/deploy-goose-job.ps1`](../infra/deploy-goose-job.ps1): 2 vCPU / 4 Gi, min 0, max 1, concurrency 16, `--cpu-boost`, `--no-allow-unauthenticated`, `--ingress all`, Direct VPC to the default subnet. GCS workspace bucket mounted at `/mnt/buzz`. `LISTENER_CONTROL_URL` is set once the listener VM exists. Secrets: LiteLLM master, Gemini/Groq/NIM, optional GitHub/Tavily/Stripe, optional ADC JSON at `/secrets/adc.json`.

## Tests

From the repo root:

```powershell
python -m unittest discover -s tests/goose-job
```

No Docker/GCP. Cover HOME sync, recipe argv, activity parsing/redaction, guardrail copy, proxy stream/tool-name rewrites, and `buzz-cloud-agents` propose/apply.
