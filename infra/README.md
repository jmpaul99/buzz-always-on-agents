# Infra (GCP)

PowerShell 5.1 scripts. **Never commit `config.env` or the repo-root `.env`.** `_common.ps1` loads `.env` then [`config.env`](config.env.example); process env wins. Aliases: `BUZZ_GCP_PROJECT`, `BUZZ_GCP_REGION`, `BUZZ_GCP_ZONE`, `BUZZ_GCP_INSTANCE`, `BUZZ_RELAY_URL`. Throws if `GCP_PROJECT` is still missing after that.

## Default names

| Key | Default |
| --- | --- |
| Region / zone | `us-central1` / `us-central1-a` |
| Artifact Registry | `buzz` (`us-central1-docker.pkg.dev/<project>/buzz`) |
| LiteLLM service | `litellm-cloud` |
| Listener VM | `buzz-listener` |
| Service accounts | `buzz-listener`, `litellm-cloud` |
| IAP network tag | `iap-ssh` |

## Full deploy

From the **repo root** (auth, prompts, GCP stack, Desktop plugin):

```powershell
.\deploy.ps1
```

`deploy.ps1` writes `.env` / `config.env`, then runs this directory's `deploy-all.ps1`. Partial scripts still work from `infra/` when `gcloud` is already authenticated. After the stack exists, a Mac running Buzz Desktop only needs [`macos/install-path.sh`](../macos/README.md).

To run only the GCP stack (no Desktop plugin):

```powershell
.\deploy-all.ps1
```

Order:

1. `bootstrap.ps1`
2. `create-secrets.ps1`
3. `deploy-litellm.ps1`
4. `deploy-listener.ps1` (needs LiteLLM URL + `litellm-master-key`)

Each script is idempotent enough to re-run (create-or-update). `$ErrorActionPreference = Continue` plus `Invoke-Gcloud` throws on nonzero gcloud.

## `bootstrap.ps1`

- Enables: Compute, Cloud Run, Secret Manager, Artifact Registry, IAP, IAM, Cloud Build, Cloud Resource Manager
- Creates AR docker repo `buzz` if missing
- Creates the listener and LiteLLM service accounts
- IAM:
  - Listener SA → `roles/run.invoker` (Cloud Run LiteLLM)
  - Listener SA and LiteLLM SA → `secretmanager.secretAccessor`
  - **Your user** → `iap.tunnelResourceAccessor` and `compute.osLogin`
  - Cloud Build SA → Artifact Registry writer + log writer
- Firewall:
  - `allow-iap-ssh` tcp/22 from `35.235.240.0/20`, target tag `iap-ssh`
  - `allow-iap-8743` tcp/8743 from the same range
  - Deletes `default-allow-ssh` if present (`0.0.0.0/0:22`)

## `create-secrets.ps1`

Upserts Secret Manager versions from **this process env**. Never prints values. Empty strings are skipped (will not overwrite with `-`).

| Env | Secret id |
| --- | --- |
| `GEMINI_API_KEY` | `gemini-api-key` |
| `GROQ_API_KEY` | `groq-api-key` |
| `NVIDIA_NIM_API_KEY` | `nvidia-nim-api-key` |
| `OPENROUTER_API_KEY` | `openrouter-api-key` |
| `LITELLM_MASTER_KEY` | `litellm-master-key` |
| `GITHUB_PERSONAL_ACCESS_TOKEN` | `github-pat` |
| `TAVILY_API_KEY` | `tavily-api-key` |
| `STRIPE_API_KEY` | `stripe-api-key` |
| `%APPDATA%\gcloud\application_default_credentials.json` | `gcloud-adc` (file) |

If `LITELLM_MASTER_KEY` is unset: keep the existing secret, or generate `sk-` + 32 random bytes and store it in the User environment (value not printed).

## `deploy-litellm.ps1`

Cloud Build (`cloudbuild-litellm.yaml`) → `…/buzz/litellm:latest` → Cloud Run `litellm-cloud`. Then `roles/run.invoker` for the **listener** SA. Prints `LITELLM_URL`.

## `deploy-listener.ps1`

- Creates `e2-micro` `buzz-listener` if missing: Ubuntu 24.04, 30 GB pd-standard, PREMIUM IPv4, listener SA, tag `iap-ssh`, OS Login metadata off (SSH via IAP + project keys)
- Ensures `allow-iap-8743`
- `gcloud compute scp --tunnel-through-iap` of listener sources, systemd units, sprig install
- Remote install: venv, pip, Node.js/`uv`, sprig (`buzz` / `buzz-acp` / `buzz-agent` / `buzz-dev-mcp`), MCP multiplexer (`run-mcp.sh`), control API, LiteLLM proxy, `buzz-acp@` template
- Writes `/etc/buzz/_runtime.env` (LiteLLM URL + master key, `OPENAI_COMPAT_*`, apply SA). Never printed.
- Enables `buzz-listener` and `buzz-litellm-proxy` always; enables `buzz-acp@<slug>` for each existing env file

SSH:

```powershell
gcloud compute ssh buzz-listener --zone us-central1-a --tunnel-through-iap
```

## Cloud Build YAMLs

Context is the **repo root** so Dockerfiles can `COPY listener/…`. `.dockerignore` excludes `windows/`, `macos/`, `tests/`, `.git`, env files.

## Typical partial redeploys

| You changed | Run |
| --- | --- |
| `litellm/config.yaml` or merge script | `.\deploy-litellm.ps1` |
| Listener Python/scripts/units, MCP catalog, sprig | `.\deploy-listener.ps1` |
| Provider keys | `.\create-secrets.ps1` then the service that mounts them |
| IAM / firewall / APIs | `.\bootstrap.ps1` |
