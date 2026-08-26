# Infra (GCP)

PowerShell 5.1 scripts. **Never commit `config.env` or the repo-root `.env`.** `_common.ps1` loads `.env` then [`config.env`](config.env.example); process env wins. Aliases: `BUZZ_GCP_PROJECT`, `BUZZ_GCP_REGION`, `BUZZ_GCP_ZONE`, `BUZZ_GCP_INSTANCE`, `BUZZ_RELAY_URL`. Throws if `GCP_PROJECT` is still missing after that.

## Default names

| Key | Default |
| --- | --- |
| Region / zone | `us-central1` / `us-central1-a` |
| Artifact Registry | `buzz` (`us-central1-docker.pkg.dev/<project>/buzz`) |
| LiteLLM service | `litellm-goose` |
| Goose **service** (mention path) | `goose-worker` |
| Listener VM | `buzz-listener` |
| Service accounts | `buzz-listener`, `goose-job`, `litellm-goose` |
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
4. `deploy-goose-job.ps1` (needs LiteLLM URL)
5. `deploy-listener.ps1` (needs `goose-worker` URL)

Each script is idempotent enough to re-run (create-or-update). `$ErrorActionPreference = Continue` plus `Invoke-Gcloud` throws on nonzero gcloud.

## `bootstrap.ps1`

- Enables: Compute, Cloud Run, Secret Manager, Artifact Registry, IAP, IAM, Cloud Build, Cloud Resource Manager
- Creates AR docker repo `buzz` if missing
- Creates the three service accounts
- IAM:
  - Goose SA → `roles/run.invoker` (LiteLLM + later goose-worker binding)
  - LiteLLM SA and Goose SA → `secretmanager.secretAccessor`
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

If `GITHUB_PERSONAL_ACCESS_TOKEN` is unset, the script may import a `github_pat_…` Bearer from Desktop Goose `config.yaml`. Move that token out of YAML when you can.

## `deploy-litellm.ps1`

Cloud Build (`cloudbuild-litellm.yaml`) → `…/buzz/litellm:latest` → Cloud Run `litellm-goose`. Then `roles/run.invoker` for the Goose SA. Prints `LITELLM_URL`.

## `deploy-goose-job.ps1`

Cloud Build (`cloudbuild-goose.yaml`, 30 min, `E2_HIGHCPU_8`) → `…/buzz/goose-buzz:latest`.

Then:

- Deploy service `goose-worker` (same image, min 0, max 1, concurrency 16, timeout 3600s, cpu-boost, unauthenticated off)
- `roles/run.invoker` on `goose-worker` for the **listener** SA

Optional secrets are attached only if they exist (`github-pat`, `tavily-api-key`, `stripe-api-key`, `gcloud-adc` → `/secrets/adc.json`).

Env on the service includes `LITELLM_URL`, `LITELLM_AUDIENCE`, `GOOSE_MAX_PARALLEL=2`, `GOOSE_TIMEOUT_SECS=1500`, `GOOSE_IDLE_TIMEOUT_SECS=180`.

## `deploy-listener.ps1`

- Creates `e2-micro` `buzz-listener` if missing: Ubuntu 24.04, 30 GB pd-standard, PREMIUM IPv4, listener SA, tag `iap-ssh`, OS Login metadata off (SSH via IAP + project keys)
- Ensures `allow-iap-8743`
- `gcloud compute scp --tunnel-through-iap` of listener sources (including `seen.py`)
- Remote install: venv, pip, systemd units, keepalive timer
- systemd drop-in `/etc/systemd/system/buzz-listener.service.d/worker.conf` with `GOOSE_WORKER_URL` from `goose-worker`
- Listener is enabled but not started until `/etc/buzz/*.env` exists

SSH:

```powershell
gcloud compute ssh buzz-listener --zone us-central1-a --tunnel-through-iap
```

## Cloud Build YAMLs

Context is the **repo root** so Dockerfiles can `COPY goose/…`, `COPY listener/…`. `.dockerignore` excludes `windows/`, `macos/`, `tests/`, `.git`, env files. Goose build timeout 1800s.

## Typical partial redeploys

| You changed | Run |
| --- | --- |
| `litellm/config.yaml` or merge script | `.\deploy-litellm.ps1` |
| Goose config, worker, Dockerfile, recipes | `.\deploy-goose-job.ps1` |
| Listener Python/scripts/units | `.\deploy-listener.ps1` |
| Provider keys | `.\create-secrets.ps1` then the service that mounts them |
| IAM / firewall / APIs | `.\bootstrap.ps1` |
