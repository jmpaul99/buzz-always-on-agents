#Requires -Version 5.1
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_common.ps1")
$image = "${Ar}/litellm:latest"
$litellmSa = "$($C.LITELLM_SA)@$Project.iam.gserviceaccount.com"
$gooseSa = "$($C.GOOSE_SA)@$Project.iam.gserviceaccount.com"
$cb = Join-Path $PSScriptRoot "cloudbuild-litellm.yaml"

Invoke-Gcloud config set project $Project --quiet
Write-Host "building $image"
Invoke-Gcloud builds submit $Root --project $Project --config $cb --substitutions="_IMAGE=$image" --quiet --timeout=1200

Write-Host "deploying Cloud Run $($C.LITELLM_SERVICE)"
Invoke-Gcloud run deploy $C.LITELLM_SERVICE `
    --project $Project `
    --region $Region `
    --image $image `
    --service-account $litellmSa `
    --memory 2Gi `
    --cpu 1 `
    --min-instances 0 `
    --max-instances 3 `
    --timeout 300 `
    --concurrency 8 `
    --cpu-boost `
    --no-allow-unauthenticated `
    --ingress all `
    --port 8080 `
    --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,GROQ_API_KEY=groq-api-key:latest,NVIDIA_NIM_API_KEY=nvidia-nim-api-key:latest,OPENROUTER_API_KEY=openrouter-api-key:latest,LITELLM_MASTER_KEY=litellm-master-key:latest"

Invoke-Gcloud run services add-iam-policy-binding $C.LITELLM_SERVICE --project $Project --region $Region `
    --member="serviceAccount:$gooseSa" --role="roles/run.invoker" --quiet

$url = (& gcloud run services describe $C.LITELLM_SERVICE --project $Project --region $Region --format="value(status.url)").Trim()
Write-Host "LITELLM_URL=$url"
