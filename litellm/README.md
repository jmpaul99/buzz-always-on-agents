# LiteLLM router

Cloud Run service `litellm-goose`. Goose never talks to providers directly: the worker sidecar (`goose-job/litellm_proxy.py`) forwards `127.0.0.1:4000` here with IAM + `LITELLM_MASTER_KEY`.

Image: `ghcr.io/berriai/litellm:main-stable` plus this repo’s `config.yaml` after keyword merge. Built by [`infra/cloudbuild-litellm.yaml`](../infra/cloudbuild-litellm.yaml).

## Virtual model `goose`

Goose is configured with `GOOSE_PROVIDER=litellm` and `GOOSE_MODEL=goose`. That alias is LiteLLM’s complexity auto-router (`auto_router/complexity_router`). The LLM classifier is **off**; routing is heuristic + keyword rules.

| Tier | Models (order = preference / shuffle pool) |
| --- | --- |
| SIMPLE | `groq-qwen` (qwen3.6-27b, parallel tools), `groq-fast` (gpt-oss-120b), `gemini-flash` |
| MEDIUM | `groq-qwen`, `gemini-flash`, `glm` (GLM-5.2), `kimi` (Kimi K2.6) |
| COMPLEX | `glm`, `kimi`, `deepseek-pro`, `minimax-m27` |
| REASONING | `glm`, `kimi`, `gemini-flash` |

Keyword shortcuts:

- SIMPLE: `hi`, `hello`, `hey`, `thanks`, `thank you`, `ping`, `ok`
- COMPLEX: `refactor`, `implement`, `debug`, `traceback`, `compile`, `function`
- REASONING: `step by step`, `reason`, `architecture`, `tradeoff`, `prove`

Score 0 does **not** fall through to SIMPLE (`simple_medium: 0`) so short mixed asks like “write a poem and react” stay MEDIUM. Token threshold `complex: 400`. Adaptive routing + session affinity (1h) are on. Default model: `groq-qwen` (`qwen3.6-27b`) so greetings still emit `buzz messages send`. SIMPLE stays on Groq/Gemini so it does not burn the shared NIM ~40 RPM wallet. `gemini-lite`, `groq-20b`, `openrouter-free`, and `openrouter-cheap` stay in `model_list` for manual curls but are **not** on the Goose fallback chain — they skip tool calls too often.

`custom_technical_keywords` starts with Buzz/infra terms (`buzz`, `nostr`, `nsec`, `relay`, …). **Disabled Goose extension names are appended at image build** by `merge_extension_keywords.py` so adding an MCP in `goose/config.yaml` automatically steers those mentions toward COMPLEX without a hand-maintained list.

## Fallback chain

`router_settings.default_fallbacks` after allowed_fails=1 / 30s cooldown / 2 retries:

`groq-qwen` → `gemini-flash` → `glm` → `kimi` → `groq-fast` → `deepseek-pro` → `minimax-m27` → `laguna` → `nemotron` → `step-flash` → `deepseek-flash` → `minimax` → `or-ox` → `or-ultra` → `or-laguna-s`

OpenRouter is last-resort only (not in complexity tiers). Pinned free slugs: `stealth/ox-alpha`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `poolside/laguna-s-2.1:free`. NIM Flash/XS/Lightning/M3/Step stay as named fallbacks, not tier leaders.

## Providers and secrets

| Secret Manager | Env in the container | Used for |
| --- | --- | --- |
| `nvidia-nim-api-key` | `NVIDIA_NIM_API_KEY` | Shared ~40 RPM NIM wallet |
| `groq-api-key` | `GROQ_API_KEY` | Groq org limits (~30 RPM in config) |
| `gemini-api-key` | `GEMINI_API_KEY` | AI Studio |
| `openrouter-api-key` | `OPENROUTER_API_KEY` | Last-resort |
| `litellm-master-key` | `LITELLM_MASTER_KEY` | LiteLLM `master_key` (generated if unset) |

Empty process-env values are skipped by `infra/create-secrets.ps1` so you never overwrite a live secret with `-`.

## Deploy

[`infra/deploy-litellm.ps1`](../infra/deploy-litellm.ps1): 1 vCPU / 2 Gi, min 0, max 3, timeout 300s, concurrency 8, `--cpu-boost`, `--no-allow-unauthenticated`, `--ingress all`. Invoker: Goose job SA (`goose-job@…`).

Goose worker must be able to mint an identity token whose audience is this service URL (`LITELLM_URL` / `LITELLM_AUDIENCE`).

## Build-time keyword merge

Dockerfile:

```
COPY goose/config.yaml
COPY litellm/config.yaml
RUN python merge_extension_keywords.py goose-config litellm-config /app/config.yaml
```

`disabled_extension_keywords` walks `extensions:` and, for each `enabled: false` slug, adds the slug, `name`, `display_name`, and ≥3-char tokens. Those are merged into `custom_technical_keywords`.

From the repo root:

```powershell
python -m unittest discover -s tests/litellm
```

## Smoke (does not make the service public)

```powershell
gcloud run services proxy litellm-goose --region us-central1 --project your-gcp-project
# other terminal — include the master key header LiteLLM expects
curl http://127.0.0.1:8080/v1/chat/completions ^
  -H "Authorization: Bearer $env:LITELLM_MASTER_KEY" ^
  -H "Content-Type: application/json" ^
  -d "{\"model\":\"goose\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}]}"
```

Timeout for completions is 240s (`router_settings.timeout` and Goose `LITELLM_TIMEOUT`).
