# buzz-always-on-agents (GCP)

Cloud-hosted Buzz agents. **Buzz Desktop is the human client and identity store only** — Goose, Chromium, and the LLM router run on GCP. Mentions on the community relay (`BUZZ_RELAY_URL`) wake a Cloud Run worker; the worker replies through `buzz` on that same relay.

Do not commit nsecs, gcloud tokens, Stripe/GitHub/Google secrets, ADC JSON, `.env`, or `infra/config.env`.

## How it works

```
Buzz Desktop / phone  →  community relay (WSS)
e2-micro listener     →  one outbound WSS per agent nsec
                         on @mention or DM → Cloud Run goose-worker (min 0)
                                              → localhost LiteLLM proxy
                                                 → Cloud Run LiteLLM (min 0)
                                                    → NIM / Groq / Gemini
                                                    → OpenRouter last-resort
                                              → buzz messages send → relay
```

| Piece | Where | Always on? |
| --- | --- | --- |
| Relay | Buzz community WSS (`BUZZ_RELAY_URL`) | Host uptime |
| Listener | `e2-micro`, 30 GB standard PD, ephemeral IPv4 | Yes (IPv4 ~$3.65/mo) |
| Goose + Chromium | Cloud Run **service** `goose-worker`, 2 vCPU / 4 Gi, min 0, max 1 | Per mention (container can stay warm for follow-up DMs; concurrency 16 so every tagged agent can enqueue) |
| LiteLLM | Cloud Run `litellm-goose`, 1 vCPU / 2 Gi, min 0 | Per LLM call |

SSH to the micro is **IAP only**:

```powershell
gcloud compute ssh buzz-listener --zone us-central1-a --tunnel-through-iap
```

Do not open `0.0.0.0/0:22`. The listener control API binds `0.0.0.0:8743`, but the firewall allows **IAP (`35.235.240.0/20`) only** — never `0.0.0.0/0:8743`.

## Mention path

1. Listener AUTH (NIP-42) on the agent's WSS, then HTTP-discovers channel membership and `REQ`s each channel.
2. Public/stream channels require a `#p` mention of the agent. DMs do not. A message that `#p`-tags several agents wakes **each** of them (dedup is per agent, not per event).
3. Listener posts 👀 then 💬 reactions and a typing heartbeat (kind 20002 every 3s).
4. It POSTs `{agent_name, prompt, recipe, env}` to `GOOSE_WORKER_URL/run` with a Google identity token. One in-flight turn per agent on the listener; Cloud Run concurrency 16 lets every tagged agent enqueue instead of 429.
5. Worker isolates Goose under `/tmp/goose-<agent>` (`HOME`), runs the `reply` recipe (or a task-MCP recipe), and streams observer events (kind 24200) so Desktop Agent Activity can show thoughts/tools.
6. Goose replies with `buzz messages send`, replacing `<your-reply>` in the send command. If other agents were mentioned too, it still replies as itself this turn (does not wait, does not speak for them). Listener sees the agent's own chat event, retracts the reactions, and stops typing.

Schedules are ordinary Buzz YAML `on: schedule` posts that `@` an agent. The listener treats them like any other mention.

## Desktop ↔ cloud sync

Agent **identity** (`nsec`) is created in Buzz Desktop. While this computer is online, a silent sidecar keeps Desktop cards and `/etc/buzz/*.env` in sync. **Every Desktop with the sidecar shares an access-filtered roster:** create on one machine is imported (card + nsec) on others where this Buzz user owns the agent, is on its allowlist, or anyone can message it. Delete on one machine undeploys GCP and drops the card everywhere it was synced.

| Syncs (sidecar PUT/GET) | Relay-native (no sidecar JSON) | Does not sync |
| --- | --- | --- |
| Create / delete (Desktop card ↔ `/etc/buzz/*.env` ↔ other Desktops) | Core + cold memory (`buzz mem`, NIP-AE) | Phone Buzz (no sidecar) |
| `system_prompt` | Channel canvas (`buzz canvas`) | Card `avatar_url`, `persona_id` |
| `respond_to` and `respond_to_allowlist` | Huddle instructions (owner-signed, per channel) | Goose `skills` / `orchestrator` / `summon` |
| `team_id`, team instruction text, display name, `channel_allowlist` | Thread / DM / channel history (`buzz messages get` or `thread`) | Desktop/phone FUSE of the GCS workspace |
| Cloud runtime labels on the card (`model=goose`, `provider=litellm`, provider backend) | Channel membership (listener join/leave live) | `is_active` / `runtime_pid` as a stop/start signal (cloud keeps listening) |

Memories, canvas, huddle, and recent channel or thread messages are fetched by the Goose worker at turn start (`tom.md`) and read/written through the Buzz CLI. Team instruction text is denormalized onto each cloud agent (`/etc/buzz/<slug>.team`) so a `teams.json` save PUTs every agent on that team.

**Cloud Goose + LiteLLM are source of truth for model and harness.** The sidecar overwrites Desktop cards for cloud-tracked agents: provider backend, empty local `agent_command` / `acp_command` / `mcp_command`, `model=goose`, `provider=litellm`, `is_active=false`. Those fields are not sent to GCP (the worker is already pinned to `GOOSE_MODEL=goose`). Switching **Run on → this computer** is reverted on the next sidecar cycle. Stopping the card is not undeploy.

Cloud agents share a ~3 GB `us-central1` GCS bucket mounted at `/mnt/buzz` (`agents/<slug>/`, `channels/<id>/`, and `shared/` for cross-channel files only). Desktop and phone do not mount it. Goose `HOME` stays under `/tmp`. Per-agent and per-channel trees are mkdir'd on that agent’s first turn in that channel.

On Windows, `windows/install-path.ps1` installs the PATH plugin **and** a hidden logon task `BuzzCloudSync` (`pythonw`, `IgnoreNew`). On macOS, `macos/install-path.sh` installs the same plugin as `~/.local/bin/buzz-backend-cloud` and LaunchAgent `xyz.block.buzz.cloud-sync`. One silent sidecar per machine. Logs: `%APPDATA%\xyz.block.buzz.app\agents\cloud-sync.log` (Windows) or `~/Library/Application Support/xyz.block.buzz.app/agents/cloud-sync.log` (macOS).

That sidecar:

1. Forwards `127.0.0.1:8743` with `gcloud compute start-iap-tunnel` (TCP — no PuTTY)
2. Fetches `/etc/buzz/_sync.token` over IAP SSH
3. Watches Desktop `managed-agents.json` and `teams.json` (`%APPDATA%\xyz.block.buzz.app\agents` on Windows, `~/Library/Application Support/xyz.block.buzz.app/agents` on macOS)
4. Pulls the cloud roster first (import missing cards + nsecs; drop cards deleted elsewhere)
5. PUTs local creates/edits and DELETEs pubkeys that vanished from this Desktop

Deleting a Desktop card undeploys that agent on GCP **and** removes it from other Desktops. Stopping the Desktop card does **not**. The Desktop create wizard still asks for a local harness; cloud Goose does not use it — the sidecar overwrites the card to **Run on → cloud** / Goose+LiteLLM and leaves the local process stopped. Imported cards arrive already set to cloud and stopped.

One-shot fallback: `.\windows\sync-agent-instructions.ps1` or `./macos/sync-agent-instructions.sh` (`buzz-cloud-sync.py --once`).

Open agent cards may not redraw until you reopen them (or restart Desktop); the JSON and OS secret store are still updated.

## Layout

| Path | Purpose |
| --- | --- |
| [`deploy.ps1`](deploy.ps1) | One command: auth, secrets, GCP stack, Windows Desktop plugin |
| [`listener/`](listener/README.md) | Always-on WSS client, hot-reload, IAP-only control API, `add-agent.sh` / `remove-agent.sh` |
| [`goose-job/`](goose-job/README.md) | Goose + sprig/`buzz` + Playwright image for the Cloud Run worker |
| [`litellm/`](litellm/README.md) | Complexity auto-router image (NIM / Groq / Gemini / OpenRouter) |
| [`goose/`](goose/README.md) | Goose `config.yaml`, hints, guardrails, recipes, [extension logins](goose/README.md#extension-logins) |
| [`infra/`](infra/README.md) | `gcloud` bootstrap, secrets, and deploy scripts |
| [`windows/`](windows/README.md) | Desktop PATH plugin and Desktop ↔ listener sync sidecar (Windows) |
| [`macos/`](macos/README.md) | Same PATH plugin and sync sidecar for Buzz Desktop on macOS |
| [`tests/`](tests/) | Unit tests; folders mirror the packages (`tests/listener`, `tests/goose-job`, `tests/litellm`) |

## Prerequisites

- Windows or macOS computer with Buzz Desktop, Python 3.10+ (CPython; not the Microsoft Store alias or the macOS Xcode stub), and the Google Cloud SDK
- A GCP project you can enable APIs in
- A Buzz community relay URL (`wss://…`). Block-hosted `*.communities.buzz.xyz` is the usual case; any host that speaks the Buzz community protocol (WSS + HTTPS `/query`) works. A generic Nostr relay does not.
- Provider keys you actually use (empty keys are skipped; do not overwrite existing secrets with `-`)

## Deploy

GCP stack from a Windows machine (PowerShell + gcloud):

```powershell
.\deploy.ps1
```

(`.\deploy.cmd` is the same command if ExecutionPolicy blocks `.ps1` files.)

That single command:

1. Creates gitignored `.env` and `infra/config.env` from the examples if they are missing
2. Logs into `gcloud` (user + Application Default Credentials) when needed
3. Prompts for GCP project, community relay URL, and any empty provider keys
4. Deploys APIs/IAM/AR → Secret Manager → LiteLLM → Goose worker → e2-micro listener
5. Installs the Windows Desktop PATH plugin and starts `BuzzCloudSync`

Re-runs are safe (create-or-update). Pass `-SkipDesktop` for GCP only, `-SkipAuth` if you are already logged in, `-NonInteractive` to fail instead of prompting (fill `.env` first).

On a **Mac**, after the GCP stack exists, install the Desktop plugin:

```bash
chmod +x macos/*.sh
./macos/install-path.sh
```

Then **restart Buzz Desktop** so **Run on → cloud** appears. If create still starts as This computer, immediately Run on → cloud and stop the local copy. On Windows the PATH plugin must be `buzz-backend-cloud.exe` — Desktop copies it to `provider.exe` and cannot run a `.cmd` shim. On macOS it is `~/.local/bin/buzz-backend-cloud` (no extension); Desktop execs that path in place.

To fill keys ahead of time instead of prompting:

```powershell
Copy-Item .env.example .env
# set BUZZ_GCP_PROJECT, BUZZ_RELAY_URL, and provider keys, then:
.\deploy.ps1
```

Redeploy a single piece without the full stack: `infra/deploy-litellm.ps1`, `infra/deploy-goose-job.ps1`, or `infra/deploy-listener.ps1`. Details in [`infra/README.md`](infra/README.md).

## Tests (no GCP)

```powershell
python -m unittest discover -s tests/listener
python -m unittest discover -s tests/goose-job
python -m unittest discover -s tests/litellm
```

Smoke LiteLLM without making the service public:

```powershell
gcloud run services proxy litellm-goose --region us-central1 --project your-gcp-project
# other terminal:
curl http://127.0.0.1:8080/v1/chat/completions ...
```

## Cost

- e2-micro compute: $0 on the free tier. Ephemeral IPv4: ~$3.65/mo.
- Cloud Run goose-worker + LiteLLM: $0 until you exceed the free allowances (Goose image is 2 vCPU / 4 Gi; LiteLLM is 1 vCPU / 2 Gi).
- Tokens: NIM / Groq / Gemini / OpenRouter bill their own accounts. All have rate limited free tiers. LiteLLM will switch between them so once one API hits its limit, it will switch you to another. Openrouter is configured to use it's lowest cost models as an absolute fall back if all are rate limited.

A daily systemd timer (`buzz-keepalive`) burns a little CPU and outbound traffic so the free e2-micro is not idle-reclaimed.

## Security

- nsecs live in Desktop Credential Manager (Windows) or Keychain (macOS), and in `/etc/buzz/*.env` (mode 600). Logs redact nsecs and provider keys.
- Control API and SSH are IAP-range only. Auth is a bearer token in `/etc/buzz/_sync.token`.
- Goose worker and LiteLLM are `--no-allow-unauthenticated`. The listener mints an identity token for `GOOSE_WORKER_URL`; the worker sidecar mints one for LiteLLM.
- Files named `_*.env` (including `_sync.token`) are not loaded as agents.

## Operations cheatsheet

```powershell
# Listener logs
gcloud compute ssh buzz-listener --zone us-central1-a --tunnel-through-iap --command "sudo journalctl -u buzz-listener -n 80 --no-pager"

# Goose worker logs
gcloud run services logs read goose-worker --region us-central1 --limit 50

# LiteLLM logs
gcloud run services logs read litellm-goose --region us-central1 --limit 50

# Worker health (from a box that can invoke it)
gcloud run services proxy goose-worker --region us-central1
curl http://127.0.0.1:8080/health
```
