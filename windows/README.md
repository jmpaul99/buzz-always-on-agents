# Windows (Buzz Desktop)

This PC is the **identity store and control plane**, not the agent runtime. Goose runs on Cloud Run. These scripts put a PATH plugin in Desktop and keep agent cards in sync with the GCP listener.

Requires CPython (`python.exe` not the Windows Store alias), `gcloud` on PATH, and Buzz Desktop. On a Mac, use [`macos/`](../macos/README.md) instead.

## Install

`.\deploy.ps1` at the repo root already runs this after the GCP stack. To install only the Desktop plugin:

```powershell
.\windows\install-path.ps1
```

Then **restart Buzz Desktop**. New agents: **Run on → cloud**. If create still starts as This computer, switch immediately and stop the local Goose copy.

`install-path.ps1`:

1. Copies `buzz-backend-cloud.py` → `%USERPROFILE%\.local\bin\buzz_cloud_impl.py` (name must **not** match `buzz-backend-*` because Windows `PATHEXT` includes `.PY` and Desktop would show **Run on → cloud.py**).
2. Copies `listener/agentutil.py` and `buzz-cloud-sync.py` next to it (`buzz_cloud_sync.py`).
3. Compiles `buzz-backend-cloud.cs` with `csc.exe` into `%USERPROFILE%\.local\bin\buzz-backend-cloud.exe`, baking in the Python path and extra PATH (Python dir, `.local\bin`, Cloud SDK).
4. Adds `.local\bin` to the **user** PATH if needed.
5. Registers hidden logon task `BuzzCloudSync` (`pythonw`, `IgnoreNew`, no time limit) and starts it.
6. Kills leftover sync/IAP/PuTTY 8743 processes so only one sidecar remains.

Do **not** put this `windows\` folder on PATH. `buzz-backend-cloud.cmd` is a source-tree helper only; Desktop copies PATH plugins to `%TEMP%\buzz-provider-*\provider.exe` and `CreateProcess` that copy — a `.cmd` becomes “Unsupported 16-Bit Application”.

## PATH plugin (`buzz-backend-cloud`)

Wire: one JSON object on stdin, one JSON object on stdout, exit 0. `protocol_version: 1`. Version `0.3.0`. Provider id: `cloud`. Never logs nsecs.

| Method | Effect |
| --- | --- |
| `info` | Name, version, description, config schema (`project`, `zone`, `instance`, `remote_script`) |
| `deploy` | PUT `/agents/{pubkey}` via the IAP-forwarded control API; SSH fallback runs `sudo -E /opt/buzz-listener/add-agent.sh <slug>` |
| `delete` | DELETE `/agents/{pubkey}` or SSH `remove-agent.sh` |

Config defaults: `BUZZ_GCP_PROJECT` / `BUZZ_GCP_ZONE` / `BUZZ_GCP_INSTANCE` / `BUZZ_RELAY_URL`. Sync URL `http://127.0.0.1:8743` (the sidecar’s tunnel). Token is read from `%APPDATA%\xyz.block.buzz.app\agents\cloud-sync-state.json` when the sidecar has already fetched it.

Deploy still asks Desktop for a local harness; cloud Goose ignores it.

## Sync sidecar (`buzz-cloud-sync.py`)

Scheduled as `BuzzCloudSync`. Single-instance mutex `Local\BuzzOracleCloudSync`. All child processes use `CREATE_NO_WINDOW` (gcloud is invoked via bundled `python.exe -S lib/gcloud.py` when `gcloud.cmd` would flash a console).

Loop:

1. Start `gcloud compute start-iap-tunnel buzz-listener 8743 --local-host-port=127.0.0.1:8743` (TCP). If that fails, fall back to `gcloud compute ssh … -- -N -L 8743:127.0.0.1:8743`.
2. Wait until `GET /health` succeeds (45s).
3. `gcloud compute ssh … --command="sudo cat /etc/buzz/_sync.token"` once; cache in `cloud-sync-state.json`.
4. Read `%APPDATA%\xyz.block.buzz.app\agents\managed-agents.json` and Windows Credential Manager (`secrets.buzz-desktop`) for `agent:{pubkey}` nsecs.
5. **Pull first:** GET `/agents` (includes nsecs). Import missing cards this Desktop user can access (owner, allowlist, or anyone-can-message); drop cards that this machine previously synced and that vanished from GCP; merge settings when cloud `updated_at` is newer (`agentutil.cloud_wins`).
6. **Push:** fingerprint name / prompt / permissions / relay / channel allowlist; PUT changed agents; DELETE pubkeys that vanished from this Desktop.
7. Compact duplicate display-name drafts; rewrite `backend` to `{type: provider, id: cloud, config: …}` so Desktop does not treat them as local.
8. Debounce file writes 0.4s; pull every 5s.

`--once` (used by `sync-agent-instructions.ps1`) does a single pull/push and exits.

Logs: `%APPDATA%\xyz.block.buzz.app\agents\cloud-sync.log`. State: `cloud-sync-state.json`. Never logs nsecs, auth tags, or allowlist dumps.

### Sync vs not

| Syncs | Relay-native / not synced |
| --- | --- |
| Create / delete (this Desktop and other sidecars) | Memories, canvas, huddle, thread (relay; worker fetches via `buzz`) |
| `system_prompt`, team instruction text | Channel membership (Block relay) |
| `respond_to`, allowlist, `channel_allowlist` | `is_active`, `runtime_pid`, start/stop timestamps |
| `team_id`, display name, relay URL | Phone Buzz; GCS workspace (cloud agents only) |
| Card overwritten to cloud Goose/LiteLLM (`model=goose`, `provider=litellm`) | Switching Run on → this computer is reverted next sidecar cycle |

Deleting a Desktop card undeploys the GCP agent **and** removes the card on other Desktops. Stopping the card does **not**. Imported cards arrive as **Run on → cloud** and stopped.

Open agent cards may not redraw until you reopen them (or restart Desktop); the JSON and Credential Manager are still updated.

## One-shot sync

```powershell
.\windows\sync-agent-instructions.ps1
# optional: -Project -Zone -Instance (or BUZZ_GCP_* env)
```

Runs `%USERPROFILE%\.local\bin\buzz_cloud_sync.py --once` (falls back to this folder). Prefer the always-on task.

## Environment

| Variable | Default |
| --- | --- |
| `BUZZ_GCP_PROJECT` | `your-gcp-project` |
| `BUZZ_GCP_ZONE` | `us-central1-a` |
| `BUZZ_GCP_INSTANCE` | `buzz-listener` |
| `BUZZ_RELAY_URL` | community WSS |
| `BUZZ_SYNC_URL` | `http://127.0.0.1:8743` |

`gcloud` must already be able to IAP-SSH the micro (`roles/iap.tunnelResourceAccessor`, `roles/compute.osLogin` from `infra/bootstrap.ps1`).
