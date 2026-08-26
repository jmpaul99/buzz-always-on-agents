#Requires -Version 5.1
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_common.ps1")
$image = "${Ar}/goose-buzz:latest"
$gooseSa = "$($C.GOOSE_SA)@$Project.iam.gserviceaccount.com"
$cb = Join-Path $PSScriptRoot "cloudbuild-goose.yaml"
$litellmUrl = (& gcloud run services describe $C.LITELLM_SERVICE --project $Project --region $Region --format="value(status.url)").Trim()
if (-not $litellmUrl) { throw "deploy LiteLLM first" }

Invoke-Gcloud config set project $Project --quiet
Write-Host "building $image (Playwright + Goose; several minutes)"
Invoke-Gcloud builds submit $Root --project $Project --config $cb --substitutions="_IMAGE=$image" --quiet --timeout=1800

$secretParts = @(
    "LITELLM_MASTER_KEY=litellm-master-key:latest",
    "GEMINI_API_KEY=gemini-api-key:latest",
    "GROQ_API_KEY=groq-api-key:latest",
    "NVIDIA_NIM_API_KEY=nvidia-nim-api-key:latest"
)
function Test-Secret([string]$Name) {
    & gcloud secrets describe $Name --project $Project 1>$null 2>$null
    return ($LASTEXITCODE -eq 0)
}
if (Test-Secret "github-pat") { $secretParts += "GITHUB_PERSONAL_ACCESS_TOKEN=github-pat:latest" }
if (Test-Secret "tavily-api-key") { $secretParts += "TAVILY_API_KEY=tavily-api-key:latest" }
if (Test-Secret "stripe-api-key") { $secretParts += "STRIPE_API_KEY=stripe-api-key:latest" }
$envVars = "LITELLM_URL=$litellmUrl,LITELLM_AUDIENCE=$litellmUrl,GOOSE_PROVIDER=litellm,GOOSE_MODEL=goose,GOOSE_DISABLE_KEYRING=1,GOOSE_DISABLE_SESSION_NAMING=true,GOOGLE_CLOUD_PROJECT=$Project,GOOSE_MAX_PARALLEL=2,GOOSE_TIMEOUT_SECS=1500,GOOSE_IDLE_TIMEOUT_SECS=180"
if (Test-Secret "gcloud-adc") {
    $secretParts += "/secrets/adc.json=gcloud-adc:latest"
    $envVars += ",GOOGLE_APPLICATION_CREDENTIALS=/secrets/adc.json"
}

$listenerEmail = "$($C.LISTENER_SA)@$Project.iam.gserviceaccount.com"
$svc = $C.GOOSE_SERVICE
Write-Host "deploying Cloud Run service $svc (min 0; one instance so per-agent queues work)"
Invoke-Gcloud run deploy $svc `
    --project $Project `
    --region $Region `
    --image $image `
    --service-account $gooseSa `
    --cpu 2 `
    --memory 4Gi `
    --min-instances 0 `
    --max-instances 1 `
    --concurrency 2 `
    --timeout 3600 `
    --cpu-boost `
    --no-allow-unauthenticated `
    --ingress all `
    --port 8080 `
    --set-env-vars $envVars `
    --set-secrets ($secretParts -join ",")
Invoke-Gcloud run services add-iam-policy-binding $svc --project $Project --region $Region `
    --member="serviceAccount:$listenerEmail" --role="roles/run.invoker" --quiet
$workerUrl = (& gcloud run services describe $svc --project $Project --region $Region --format="value(status.url)").Trim()
Write-Host "GOOSE_WORKER_URL=$workerUrl"
