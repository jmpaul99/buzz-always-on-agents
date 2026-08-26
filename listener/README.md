# Listener

Always-on WSS client on the GCP `e2-micro`. One outbound socket per `/etc/buzz/*.env` agent. On a matching mention or DM it posts to the Goose Cloud Run service.

Installed to `/opt/buzz-listener` by [`infra/deploy-listener.ps1`](../infra/deploy-listener.ps1). systemd unit: `buzz-listener.service`.

## What it does

1. Loads every `*.env` in `BUZZ_AGENTS_DIR` (default `/etc/buzz`) except names starting with `_`.
2. Supervises one `agent_loop` per pubkey. Changing an env file changes its fingerprint; the supervisor cancels and restarts that loop within ~1s. No process bounce.
3. After NIP-42 AUTH, HTTP-queries membership (kind 39002) and channel metadata (kind 39000), then `REQ`s each live channel.
4. Stream/private channels filter `#p` to the agent pubkey (mentions). DMs subscribe without `#p`.
5. Live join/leave: kinds 44100 / 44101 / 39002 add or CLOSE channel subs without reconnecting.
6. Matching events get 👀 + 💬 reactions and a typing heartbeat (kind 20002, every 3s).
7. `POST {GOOSE_WORKER_URL}/run` with a metadata-server identity token. One in-flight worker call per agent (`threading.Lock`). A multi-`#p` mention POSTs once per tagged agent; Cloud Run concurrency 16 lets those land in the worker queue.
8. When the agent’s own chat event arrives (or the worker returns), reactions are deleted (kind 5) and typing stops.

Dedup is `/var/lib/buzz-listener/seen.json` (last 4000 `{agent_pubkey}:{event_id}` keys). The same mention can still wake every agent who was `#p`-tagged. Presence kind 20001 (`online`) is published after AUTH.

## Mention rules (`agentutil.should_handle`)

Handled kinds: `9`, `46010`, `40007`.

Skipped when:

- the author is the agent itself
- content is exactly `!shutdown`, `!cancel`, or `!rotate`
- `respond_to` is `owner-only` / `owner` and the author is not the `auth` tag owner
- `respond_to` is `allowlist` and the author is not on `BUZZ_ACP_RESPOND_TO_ALLOWLIST`
- `BUZZ_CHANNEL_ALLOWLIST` is set and the event’s channel is not on it
- the channel is not a DM and the agent is not `#p`-mentioned

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
| `BUZZ_ACP_RESPOND_TO` | `owner-only` (default), `allowlist`, or anything else = everyone |
| `BUZZ_ACP_RESPOND_TO_ALLOWLIST` | Comma-separated pubkeys |
| `BUZZ_TEAM_ID` | Desktop team id |
| `BUZZ_UPDATED_AT` | RFC3339; last-write-wins with Desktop |
| `BUZZ_CHANNEL_ALLOWLIST` | Optional comma-separated channel ids |

`/etc/buzz/<slug>.instructions` is the system prompt (max 8000 chars when loaded).

`/etc/buzz/<slug>.team` is denormalized team instruction text (max 8000). Deleted when instructions are empty so a cleared team does not leave stale text.

Slug: `[a-z0-9][a-z0-9-]{0,31}`. If the display-name slug is already another pubkey, the API suffixes `-{pubkey[:8]}`.

## Control API (`0.0.0.0:8743`)

Firewall: IAP range `35.235.240.0/20` only (`allow-iap-8743`). Token: `/etc/buzz/_sync.token` (`Authorization: Bearer …` or `X-Buzz-Sync-Token`). Unauthenticated `/health` and `/healthz` return `{"ok":true}`.

| Method | Path | Body / result |
| --- | --- | --- |
| `GET` | `/health`, `/healthz` | `{"ok":true}` (no auth) |
| `GET` | `/agents` | Roster for Desktop sync: slug, pubkey, display, prompt, permissions, `owner`, `updated_at`, `nsec`, `auth_tag`, `team_instructions`. Auth required. Never log the body. Sidecars import only agents this Desktop user can access. |
| `PUT` | `/agents/{pubkey}` | Upsert. `nsec` required on create; omitted on update keeps the existing key. `nsec` must match `{pubkey}`. |
| `DELETE` | `/agents/{pubkey}` | Removes `.env`, `.instructions`, and `.team`. Idempotent. |

PUT JSON fields: `nsec` / `private_key_nsec`, `name`, `slug`, `system_prompt`, `respond_to`, `respond_to_allowlist`, `team_id`, `team_instructions`, `auth_tag`, `relay_url` / `relay`, `updated_at`, `channel_allowlist`. Payload cap 512 KiB.

Hot-reload watches the env files; PUT/DELETE do not restart systemd. Redeploy the listener for Desktop roster import (`nsec` on GET `/agents`).

## Worker enqueue

Requires `GOOSE_WORKER_URL` (set by deploy as a systemd drop-in). Timeout default 1620s (`GOOSE_WORKER_TIMEOUT`). Retries 429/502/503 and transport errors up to 3 times.

Recipe: `taskmcp.match_task_recipe` against `task-mcps.json`. A recipe is sent only when **exactly one** catalog slug’s keywords appear in the mention. Ambiguous or no hit → generic Goose prompt (Extension Manager can still enable MCPs).

The prompt and `BUZZ_SEND_CMD` tell Goose to `buzz messages send --channel … --content '<your-reply>'` (and `--reply-to` when the mention has an `e` tag). Replace `<your-reply>` with the real reply; never send that placeholder, `...`, or an empty message. Recipe `identity` also gets `agentutil.with_turn_hint` so a multi-mention still replies as this agent (do not wait, do not speak for others). Prompt cap 20 000 chars; message body 8 000. `BUZZ_TEAM_INSTRUCTIONS` is passed from `/etc/buzz/<slug>.team` when present.

## systemd

`buzz-listener.service` runs as root, `Restart=always`. It stays enabled-but-inactive until at least one `*.env` exists; `add-agent.sh` starts it.

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
| `listener.py` | WSS loops, reactions/typing, worker POST, control API |
| `seen.py` | Per-agent mention dedup (`seen.json`) |
| `agentutil.py` | Env records, permissions, membership, Goose prompt, `with_turn_hint`, Desktop merge helpers |
| `nostrutil.py` | nsec decode, schnorr sign, NIP-42 AUTH, NIP-98 HTTP auth |
| `taskmcp.py` | Parse `goose/config.yaml` extensions, keyword catalog, recipe match |
| `task-mcps.json` | Committed keyword catalog. Regenerate with `python goose/generate_recipes.py goose/config.yaml <recipes-dir> listener/task-mcps.json` when adding an MCP, then redeploy the listener. |

`agentutil.py` is also copied next to the Windows and macOS sync sidecars so Desktop and the VM share slug/allowlist/roster merge rules.

## Environment

| Variable | Default | Meaning |
| --- | --- | --- |
| `BUZZ_AGENTS_DIR` | `/etc/buzz` | Agent env + instructions |
| `BUZZ_STATE_DIR` | `/var/lib/buzz-listener` | `seen.json` |
| `BUZZ_RELAY_URL` | from env files | Fallback relay |
| `GOOSE_WORKER_URL` | empty (required) | Cloud Run `goose-worker` URL |
| `GOOSE_WORKER_TIMEOUT` | `1620` | urllib timeout for `/run` |
| `BUZZ_CONTROL_HOST` | `0.0.0.0` | Control API bind |
| `BUZZ_CONTROL_PORT` | `8743` | Control API port |

## Tests

From the repo root:

```powershell
python -m unittest discover -s tests/listener
```

No GCP. `test_agentutil.py` covers mention filters, reactions, membership, Desktop compact/merge, and multi-Desktop roster import/delete. `test_seen.py` covers per-agent mention dedup (one event must still wake every mentioned agent). `test_taskmcp.py` covers config parse, recipe generation, and keyword routing.
