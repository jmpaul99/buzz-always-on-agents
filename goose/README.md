# Goose config (cloud image)

Files here are copied into the Goose worker image at `/home/goose/.config/goose/` (and recipes under `/home/goose/recipes`). Desktop Goose config is **not** used in the cloud.

Stay within **5 enabled extensions and ~50 tools**. Extra MCPs eat the context window and make tool choice worse. See [`guardrails.md`](guardrails.md) (injected every turn via Top of Mind / `GOOSE_MOIM_MESSAGE_FILE`, concatenated with standing context in `tom.md`).

## Always-on vs task MCPs

Always-on in `config.yaml` (`enabled: true`): `developer`, Extension Manager, `tom`. Generated recipes bake **`developer` and `tom` only** (`todo` is in `ALWAYS_ON` but skipped so Goose cannot add a second LLM turn after send). Extension Manager stays available from config so Goose can still `manage_extensions` on the generic path.

| Config name | Goose name | Role |
| --- | --- | --- |
| `developer` | developer | Files + shell (`buzz messages send` lives here) |
| `extensionmanager` | Extension Manager | Enable/disable the rest |
| `tom` | Top of Mind | Injects `tom.md` (guardrails + standing context) every turn |

Intentionally **removed** from this image (not merely disabled — Extension Manager cannot turn them on):

- Goose `memory` (builtin)
- `chatrecall` (platform)
- `knowledgegraphmemory` (stdio `@modelcontextprotocol/server-memory`)

Durable facts go through `buzz mem`. Core is injected as `[Agent Memory — core]` when the worker can fetch it.

Intentionally **off** in cloud:

- `todo` — extra LLM turn after send; duplicates the channel reply
- `code_execution` (Code Mode) — was emitting shell calls without a `command` field; cloud has no approval UI
- `scheduler` — Buzz YAML `on: schedule` is the scheduler
- `skills`, `orchestrator`, `summon`, Computer Controller, Apps, etc.

`SECURITY_PROMPT_ENABLED: false` — cloud has no approval UI; that flag would park shell tools (including send) forever.

Provider: `litellm` / model `goose` / `LITELLM_HOST=http://127.0.0.1:4000`. Telemetry off. Thinking effort `low` (so Agent Activity gets reasoning). Mode `auto`. Keyring disabled. `GOOSE_CLI_SHOW_THINKING=1`.

Generic mentions use the generated `reply` recipe (`instructions` = send contract + Buzz CLI table, `prompt` = `{{ message }}`, `settings.max_turns: 25`). Task-MCP recipes share that contract and add one extra extension. Do not use `goose run -t` on the mention path.

The send contract: post with `{{ send_cmd }}` (`buzz messages send --channel … --content '<your-reply>'`, plus `--reply-to` when the mention has an `e` tag). Replace `<your-reply>` with the actual text; never send that placeholder, `...`, or an empty message. If other agents are also `#p`-tagged, still reply as yourself this turn — do not wait for them and do not speak for them. The listener also puts that turn hint on recipe `identity` (`agentutil.with_turn_hint`), because the recipe path only sees identity + mention body.

Goose is a Buzz CLI power user (`buzz --help` allowed): `mem`, `canvas`, `channels`, `dms`, `users`, `huddle`, `messages get/thread/search`, `buzz-cloud-agents propose` / `apply` / `cancel` (two-turn chat confirm; do not use `draft-create` / `draft-update`), plus the rest. Post user-visible updates with `buzz messages send`. Stop when the work is finished.

Create or edit agent instructions in chat: propose the full text, ask the owner to reply `confirm` (or `cancel`), then apply. The listener mints identity on create. The agent is live without a Desktop Save; the sidecar imports the card when this computer is online.

## Task MCPs (default off)

Listed in `config.yaml` so Extension Manager can enable them, and so [`generate_recipes.py`](generate_recipes.py) can bake a recipe that starts with that MCP already on. Image build writes recipes only; mention keywords live in committed [`listener/task-mcps.json`](../listener/task-mcps.json).

| Slug | Transport | Needs secret / note |
| --- | --- | --- |
| `chromedevtools` | stdio `npx chrome-devtools-mcp` | Live Chrome |
| `containeruse` | stdio mcp-remote | container-use.com |
| `github` | streamable HTTP | `GITHUB_PERSONAL_ACCESS_TOKEN` |
| `goosedocs` | stdio mcp-remote | block.gitmcp.io/goose |
| `linuxmcpserver` | stdio `uvx linux-mcp-server` | |
| `playwright` | stdio `@playwright/mcp` | Chromium already in the image. **Not for Google login.** |
| `repomix` | stdio | |
| `youtubetranscript` | stdio uvx from git | |
| `tavilywebsearch` | stdio | `TAVILY_API_KEY` |
| `stripe` | streamable HTTP mcp.stripe.com | `STRIPE_API_KEY` |
| `googleadc` | stdio `uv run … google_adc_mcp.py` | ADC JSON + `GOOGLE_CLOUD_PROJECT` |

Generic mention path: Goose enables by config **name** with `manage_extensions`, or `search_available_extensions` once if unknown. After enable, discover tools (`list_functions` / Available tools on `-32002`). Goose names tools `extension__tool`. Disable the task MCP before the channel reply.

Recipe path: listener keyword match (exactly one catalog hit) → `goose run --recipe /home/goose/recipes/<slug>/recipe.yaml`. Keywords come from slug + extension name tokens (see `listener/taskmcp.py`).

## Extension logins

Cloud Goose has no browser, no approval UI, and `GOOSE_DISABLE_KEYRING=1`. Do not log in from a mention, and do not use Playwright (or Chrome DevTools) for Google / GitHub / Stripe. Authenticate on the PC, store the credential in Secret Manager, then redeploy the worker so Cloud Run mounts it.

| Extension | What to mint on the PC | `.env` / process env | Secret Manager | In the worker |
| --- | --- | --- | --- | --- |
| `github` | Fine-grained or classic PAT | `GITHUB_PERSONAL_ACCESS_TOKEN` | `github-pat` | env (Bearer to `api.githubcopilot.com/mcp/`) |
| `tavilywebsearch` | Tavily API key | `TAVILY_API_KEY` | `tavily-api-key` | env |
| `stripe` | Restricted Stripe secret key | `STRIPE_API_KEY` | `stripe-api-key` | env (Bearer to `mcp.stripe.com`) |
| `googleadc` | User ADC **with Workspace scopes** | (file, not a key) | `gcloud-adc` | `/secrets/adc.json` + `GOOGLE_APPLICATION_CREDENTIALS` |

Everything else in the task-MCP table needs no login. Optional secrets are attached **only if they already exist**; adding a new one always needs a Goose redeploy.

### API keys (GitHub, Tavily, Stripe)

1. Put the value in repo-root [`.env`](../.env.example) (gitignored). `.\deploy.ps1` will prompt for these if they are empty.
2. Upload: `.\infra\create-secrets.ps1` — empty strings are skipped (will not overwrite a live secret with `-`).
3. Mount: `.\infra\deploy-goose-job.ps1`.

If `GITHUB_PERSONAL_ACCESS_TOKEN` is unset, `create-secrets.ps1` may import a `github_pat_…` Bearer from Desktop Goose `config.yaml`. Move that token out of YAML when you can.

Rotate a key the same way: new value in `.env` → `create-secrets.ps1` → `deploy-goose-job.ps1`.

### Google Workspace

Default `gcloud auth application-default login` (what `deploy.ps1` runs) is **not** enough — it lacks Gmail / Drive / Sheets scopes. Login steps, scopes, and refresh: [`local-mcp/README.md`](local-mcp/README.md).

## Adding an MCP

1. Add the extension block under `extensions:` in `config.yaml` with `enabled: false` and `type: stdio` or `streamable_http`.
2. Regenerate the mention catalog (committed file the listener ships):

   ```powershell
   python goose/generate_recipes.py goose/config.yaml $env:TEMP\buzz-recipes listener/task-mcps.json
   ```

3. Rebuild the Goose image (`infra/deploy-goose-job.ps1`). Build runs `generate_recipes.py` with config + recipes dir only — it writes `/home/goose/recipes/<slug>/recipe.yaml` inside the image, not the listener catalog.
4. Rebuild LiteLLM too if you want the new name in complexity keywords (`merge_extension_keywords.py`).
5. Redeploy the listener (`infra/deploy-listener.ps1`) so the VM gets the updated `task-mcps.json`.

Do not hand-write task recipes unless you are debugging generation. The default `reply` recipe is also generated.

## `.goosehints`

Short standing instructions: GCS workspace at `/mnt/buzz` (`agents/`, `channels/`, `shared/`); reply with `buzz messages send` (replace `<your-reply>`; never send `...` or an empty message); if other agents are mentioned, still reply as yourself this turn; full Buzz CLI including `--help`; `buzz-cloud-agents` for instruction create/edit after chat confirm (no Desktop Save); Playwright is for public pages, not Google login; reactions go through `buzz reactions` on the mention event.

## Guardrails (Top of Mind)

[`guardrails.md`](guardrails.md) is copied into every-turn `tom.md` with standing sections (core memory, team instructions, huddle, canvas metadata, recent channel or thread, workspace). Reminder: 5 extensions / 50 tools, always-on set, enable-then-discover, never invent tool names, never dump env, never enable Code Mode, `buzz mem` only for durable facts.
