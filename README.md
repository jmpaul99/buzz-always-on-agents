# buzz-always-on-agents (GCP)

Cloud-hosted Buzz agents. **Buzz Desktop is the human client**; identity (`nsec`) is minted in Desktop Save or on the listener after a chat confirm. Stock **buzz-acp + buzz-agent** run on the e2-micro; LiteLLM stays on Cloud Run. Mentions on the community relay (`BUZZ_RELAY_URL`) are handled by buzz-acp; replies go through `buzz` on that same relay.

Do not commit nsecs, gcloud tokens, Stripe/GitHub/Google secrets, ADC JSON, `.env`, or `infra/config.env`.

## How it works

```
Buzz Desktop / phone  →  community relay (WSS)
e2-micro              →  buzz-acp@<slug> (one WSS + ACP client per agent)
                         → stdio buzz-agent + buzz-dev-mcp
                         → localhost IAM proxy :4000
                            → Cloud Run LiteLLM (min 0)
                               → NIM / Groq / Gemini
                               → OpenRouter last-resort
                         → buzz messages send → relay
thin control API :8743  → Desktop sidecar (roster / nsec only)
```

| Piece | Where | Always on? |
| --- | --- | --- |
| Relay | Buzz community WSS (`BUZZ_RELAY_URL`) | Host uptime |
| Control API + buzz-acp | `e2-micro`, 30 GB standard PD, ephemeral IPv4 | Yes (IPv4 ~$3.65/mo) |
| LiteLLM | Cloud Run `litellm-goose`, 1 vCPU / 2 Gi, min 0 | Per LLM call |

No Chromium/Playwright on this pass (RAM). 1 GiB is tight with many agents or several Node MCPs; bump to **e2-small (2 GiB)** if RSS OOMs. Still no browser.

SSH to the micro is **IAP only**:

```powershell
gcloud compute ssh buzz-listener --zone us-central1-a --tunnel-through-iap
```

Do not open `0.0.0.0/0:22`. The control API binds `0.0.0.0:8743`, but the firewall allows **IAP (`35.235.240.0/20`)** — never `0.0.0.0/0:8743`.

## Mention path

1. `buzz-acp@<slug>` AUTH (NIP-42) on the agent's WSS and builds the native standing prompt (core / huddle / canvas / thread-or-DM).
2. Public/stream channels require a `#p` mention. DMs do not. A message that `#p`-tags several agents wakes **each** of them (one systemd unit per nsec).
3. buzz-acp prompts `buzz-agent` over ACP. Desktop Agent Activity is `session/update` (thoughts, tool cards). LLM token SSE is off (`buzz-agent` hardcodes `stream: false`).
4. Channel delivery is the native prompt plus `BUZZ_AGENT_REQUIRE_REPLY=1`. The agent posts with `buzz messages send`. Phone users only see channel posts; Desktop also sees ACP Activity.

Schedules are ordinary Buzz YAML `on: schedule` posts that `@` an agent.

## Desktop ↔ cloud sync

Agent **identity** (`nsec`) is minted in Buzz Desktop (Save) **or** on the listener after a chat confirm (`buzz-cloud-agents apply`). While this computer is online, a silent sidecar keeps Desktop cards and `/etc/buzz/*.env` in sync. Chat-created agents are live on GCP immediately; the sidecar imports the card + nsec (no Desktop Save). **Every Desktop with the sidecar shares an access-filtered roster:** create on one machine (or in chat) is imported on others where this Buzz user owns the agent, is on its allowlist, or anyone can message it. Delete on one machine undeploys GCP and drops the card everywhere it was synced.

The sidecars never talk to LiteLLM or Cloud Run. They IAP-tunnel to the micro **control API** (`127.0.0.1:8743`) and sync roster / nsec / prompt / permissions. Logon task, IAP tunnel, PATH plugin (`buzz-backend-cloud`), and `managed-agents.json` stay as they are. No Windows/Mac reinstall unless the GCE instance or port 8743 moves.

| Syncs (sidecar PUT/GET) | Relay-native (no sidecar JSON) | Does not sync |
| --- | --- | --- |
| Create / delete (Desktop card ↔ `/etc/buzz/*.env` ↔ other Desktops) | Core + cold memory (`buzz mem`, NIP-AE) | Phone Buzz (no sidecar) |
| `system_prompt` | Channel canvas (`buzz canvas`) | Card `avatar_url`, `persona_id` |
| `respond_to` and `respond_to_allowlist` | Huddle instructions (owner-signed, per channel) | Extra MCPs (catalog file on the VM, not Desktop UI) |
| `team_id`, team instruction text, display name, `channel_allowlist` | Thread / DM / channel history (`buzz messages get` or `thread`) | Local harness fields (cleared so this PC does not spawn a second buzz-acp) |
| Cloud runtime labels on the card (`model=goose`, `provider=litellm`, provider backend) | Channel membership (buzz-acp join/leave live) | `is_active` / `runtime_pid` as a stop/start signal (cloud keeps listening) |

Memories, canvas, huddle, and recent channel or thread messages are **relay-native** (fetched by buzz-acp, not sidecar JSON). Team instruction text is denormalized onto each cloud agent (`/etc/buzz/<slug>.team`) so a `teams.json` save PUTs every agent on that team.

**Cloud buzz-agent + LiteLLM are source of truth for model and harness.** The sidecar overwrites Desktop cards for cloud-tracked agents: provider backend, empty local `agent_command` / `acp_command` / `mcp_command`, `model=goose`, `provider=litellm`, `is_active=false`. Card labels `model=goose` / `provider=litellm` are the LiteLLM virtual model name (cosmetic; sync does not start the LLM). Switching **Run on → this computer** is reverted on the next sidecar cycle. Stopping the card is not undeploy.

On Windows, `windows/install-path.ps1` installs the PATH plugin **and** a hidden logon task `BuzzCloudSync` (`pythonw`, `IgnoreNew`). On macOS, `macos/install-path.sh` installs the same plugin as `~/.local/bin/buzz-backend-cloud` and LaunchAgent `xyz.block.buzz.cloud-sync`. One silent sidecar per machine. Logs: `%APPDATA%\xyz.block.buzz.app\agents\cloud-sync.log` (Windows) or `~/Library/Application Support/xyz.block.buzz.app/agents/cloud-sync.log` (macOS).

That sidecar:

1. Forwards `127.0.0.1:8743` with `gcloud compute start-iap-tunnel` (TCP — no PuTTY)
2. Fetches `/etc/buzz/_sync.token` over IAP SSH
3. Watches Desktop `managed-agents.json` and `teams.json` (`%APPDATA%\xyz.block.buzz.app\agents` on Windows, `~/Library/Application Support/xyz.block.buzz.app/agents` on macOS)
4. Pulls the cloud roster first (import missing cards + nsecs; drop cards deleted elsewhere)
5. PUTs local creates/edits and DELETEs pubkeys that vanished from this Desktop

Deleting a Desktop card undeploys that agent on GCP **and** removes it from other Desktops. Stopping the Desktop card does **not**. The Desktop create wizard still asks for a local harness; cloud buzz-acp does not use it — the sidecar overwrites the card to **Run on → cloud** / LiteLLM and leaves the local process stopped. Imported cards arrive already set to cloud and stopped.

One-shot fallback: `.\windows\sync-agent-instructions.ps1` or `./macos/sync-agent-instructions.sh` (`buzz-cloud-sync.py --once`).

Open agent cards may not redraw until you reopen them (or restart Desktop); the JSON and OS secret store are still updated.

## Layout

| Path | Purpose |
| --- | --- |
| [`deploy.ps1`](deploy.ps1) | One command: auth, secrets, GCP stack, Windows Desktop plugin |
| [`listener/`](listener/README.md) | Control API, systemd `buzz-acp@`, LiteLLM proxy, MCP catalog, `add-agent.sh` / `remove-agent.sh` |
| [`litellm/`](litellm/README.md) | Complexity auto-router image (NIM / Groq / Gemini / OpenRouter) |
| [`goose/local-mcp/`](goose/local-mcp/README.md) | Optional Google Workspace MCP (disabled extra) |
| [`infra/`](infra/README.md) | `gcloud` bootstrap, secrets, and deploy scripts |
| [`windows/`](windows/README.md) | Desktop PATH plugin and Desktop ↔ listener sync sidecar (Windows) |
| [`macos/`](macos/README.md) | Same PATH plugin and sync sidecar for Buzz Desktop on macOS |
| [`tests/`](tests/) | Unit tests; folders mirror the packages (`tests/listener`, `tests/litellm`) |

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
4. Deploys APIs/IAM/AR → Secret Manager → LiteLLM → e2-micro (control API + buzz-acp + proxy)
5. Installs the Windows Desktop PATH plugin and starts `BuzzCloudSync`

Re-runs are safe (create-or-update). Pass `-SkipDesktop` for GCP only, `-SkipAuth` if you are already logged in, `-NonInteractive` to fail instead of prompting (fill `.env` first). A leftover Cloud Run `goose-worker` is deleted on listener deploy.

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

Redeploy a single piece without the full stack: `infra/deploy-litellm.ps1` or `infra/deploy-listener.ps1`. Details in [`infra/README.md`](infra/README.md).

## Tests (no GCP)

```powershell
python -m unittest discover -s tests/listener
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
- Cloud Run LiteLLM: $0 until you exceed the free allowances (1 vCPU / 2 Gi, min 0).
- Tokens: NIM / Groq / Gemini / OpenRouter bill their own accounts. All have rate limited free tiers. LiteLLM will switch between them so once one API hits its limit, it will switch you to another. OpenRouter last-resort is pinned free agent models, not the random free router.

A daily systemd timer (`buzz-keepalive`) burns a little CPU and outbound traffic so the free e2-micro is not idle-reclaimed.

## Security

- nsecs live in Desktop Credential Manager (Windows) or Keychain (macOS), and in `/etc/buzz/*.env` (mode 600). Logs redact nsecs and provider keys.
- Control API and SSH are not public (`0.0.0.0/0`). Auth is a sidecar bearer token in `/etc/buzz/_sync.token` or a listener-SA ID token for chat apply (POST/PUT only).
- LiteLLM is `--no-allow-unauthenticated`. The GCE proxy mints an identity token for `LITELLM_URL`.
- Files named `_*.env` (including `_sync.token` and `_runtime.env`) are not loaded as agents.

## Operations cheatsheet

```powershell
# Control API logs
gcloud compute ssh buzz-listener --zone us-central1-a --tunnel-through-iap --command "sudo journalctl -u buzz-listener -n 80 --no-pager"

# One agent
gcloud compute ssh buzz-listener --zone us-central1-a --tunnel-through-iap --command "sudo journalctl -u buzz-acp@YOURSLUG -n 80 --no-pager"

# LiteLLM logs
gcloud run services logs read litellm-goose --region us-central1 --limit 50
```
