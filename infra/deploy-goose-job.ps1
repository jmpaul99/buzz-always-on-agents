#Requires -Version 5.1
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_common.ps1")
$image = "${Ar}/goose-buzz:latest"
$gooseSa = "$($C.GOOSE_SA)@$Project.iam.gserviceaccount.com"
$cb = Join-Path $PSScriptRoot "cloudbuild-goose.yaml"
$litellmUrl = (& gcloud run services describe $C.LITELLM_SERVICE --project $Project --region $Region --format="value(status.url)").Trim()
if (-not $litellmUrl) { throw "deploy LiteLLM first" }

function Set-GooseGcsVolume([string]$Service, [string]$Bucket) {
    $export = & gcloud run services describe $Service --project $Project --region $Region --format=export
    if ($LASTEXITCODE -ne 0) { throw "gcloud run services describe $Service failed" }
    $py = Join-Path $env:TEMP "buzz-attach-gcs.py"
    @'
import sys
text = sys.stdin.read()
bucket = sys.argv[1]
if "name: buzz-workspace" not in text:
    text = text.replace(
        "        run.googleapis.com/startup-cpu-boost: 'true'\n",
        "        run.googleapis.com/startup-cpu-boost: 'true'\n"
        "        run.googleapis.com/execution-environment: gen2\n",
        1,
    )
    if "run.googleapis.com/execution-environment: gen2" not in text:
        raise SystemExit("could not set execution-environment gen2")
    if "name: BUZZ_WORKSPACE" not in text:
        text = text.replace(
            "        - name: GOOSE_IDLE_TIMEOUT_SECS\n          value: '180'\n",
            "        - name: GOOSE_IDLE_TIMEOUT_SECS\n          value: '180'\n"
            "        - name: BUZZ_WORKSPACE\n          value: /mnt/buzz\n",
            1,
        )
    if "mountPath: /mnt/buzz" not in text:
        if "        volumeMounts:\n" in text:
            text = text.replace(
                "        volumeMounts:\n",
                "        volumeMounts:\n        - mountPath: /mnt/buzz\n          name: buzz-workspace\n",
                1,
            )
        else:
            raise SystemExit("could not add /mnt/buzz volumeMount")
    csi = (
        "      - name: buzz-workspace\n"
        "        csi:\n"
        "          driver: gcsfuse.run.googleapis.com\n"
        "          volumeAttributes:\n"
        f"            bucketName: {bucket}\n"
    )
    if "      volumes:\n" in text:
        text = text.replace("      volumes:\n", "      volumes:\n" + csi, 1)
    else:
        text = text.replace(
            "      timeoutSeconds:",
            "      volumes:\n" + csi + "      timeoutSeconds:",
            1,
        )
sys.stdout.write(text)
'@ | Set-Content -LiteralPath $py -Encoding utf8
    $patched = $export | python $py $Bucket
    if ($LASTEXITCODE -ne 0) { throw "failed to patch goose-worker YAML for GCS" }
    $yaml = Join-Path $env:TEMP "buzz-goose-worker.yaml"
    Set-Content -LiteralPath $yaml -Value $patched -Encoding utf8
    Invoke-Gcloud run services replace $yaml --project $Project --region $Region --quiet
}

function Set-GooseDirectVpc([string]$Service) {
    $export = & gcloud run services describe $Service --project $Project --region $Region --format=export
    if ($LASTEXITCODE -ne 0) { throw "gcloud run services describe $Service failed" }
    $py = Join-Path $env:TEMP "buzz-attach-vpc.py"
    @'
import sys
text = sys.stdin.read()
project, region = sys.argv[1], sys.argv[2]
needle = "run.googleapis.com/network-interfaces:"
if needle not in text:
    iface = (
        '[{"network":"projects/%s/global/networks/default",'
        '"subnetwork":"projects/%s/regions/%s/subnetworks/default"}]'
        % (project, project, region)
    )
    insert = (
        "        run.googleapis.com/network-interfaces: '%s'\n"
        "        run.googleapis.com/vpc-access-egress: private-ranges-only\n"
        % iface
    )
    key = "        run.googleapis.com/execution-environment: gen2\n"
    if key in text:
        text = text.replace(key, key + insert, 1)
    else:
        boost = "        run.googleapis.com/startup-cpu-boost: 'true'\n"
        if boost not in text:
            raise SystemExit("could not set Direct VPC annotations")
        text = text.replace(boost, boost + insert, 1)
sys.stdout.write(text)
'@ | Set-Content -LiteralPath $py -Encoding utf8
    $patched = $export | python $py $Project $Region
    if ($LASTEXITCODE -ne 0) { throw "failed to patch goose-worker YAML for Direct VPC" }
    $yaml = Join-Path $env:TEMP "buzz-goose-worker-vpc.yaml"
    Set-Content -LiteralPath $yaml -Value $patched -Encoding utf8
    Invoke-Gcloud run services replace $yaml --project $Project --region $Region --quiet
}

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
$envVars = "LITELLM_URL=$litellmUrl,LITELLM_AUDIENCE=$litellmUrl,GOOSE_PROVIDER=litellm,GOOSE_MODEL=goose,GOOSE_DISABLE_KEYRING=1,GOOSE_DISABLE_SESSION_NAMING=true,GOOGLE_CLOUD_PROJECT=$Project,GOOSE_MAX_PARALLEL=2,GOOSE_TIMEOUT_SECS=1500,GOOSE_IDLE_TIMEOUT_SECS=180,BUZZ_WORKSPACE=/mnt/buzz"
$internalIp = (& gcloud compute instances describe $C.LISTENER_INSTANCE --project $Project --zone $Zone --format="value(networkInterfaces[0].networkIP)" 2>$null).Trim()
if ($internalIp) {
    $envVars += ",LISTENER_CONTROL_URL=http://${internalIp}:8743"
}
if (Test-Secret "gcloud-adc") {
    $secretParts += "/secrets/adc.json=gcloud-adc:latest"
    $envVars += ",GOOGLE_APPLICATION_CREDENTIALS=/secrets/adc.json"
}

$bucket = $C.WORKSPACE_BUCKET
if ([string]::IsNullOrWhiteSpace($bucket)) {
    $bucket = "buzz-goose-workspace-$Project"
}

$listenerEmail = "$($C.LISTENER_SA)@$Project.iam.gserviceaccount.com"
$svc = $C.GOOSE_SERVICE
Write-Host "deploying Cloud Run service $svc (min 0; one instance so per-agent queues work; concurrency 16 so multi-mention /run POSTs enqueue; GCS $bucket at /mnt/buzz)"
$help = & gcloud run deploy --help 2>&1 | Out-String
if ($help -match "--add-volume") {
    Invoke-Gcloud run deploy $svc `
        --project $Project `
        --region $Region `
        --image $image `
        --service-account $gooseSa `
        --cpu 2 `
        --memory 4Gi `
        --min-instances 0 `
        --max-instances 1 `
        --concurrency 16 `
        --timeout 3600 `
        --cpu-boost `
        --no-allow-unauthenticated `
        --ingress all `
        --port 8080 `
        --update-env-vars $envVars `
        --update-secrets ($secretParts -join ",") `
        --add-volume="name=buzz-workspace,type=cloud-storage,bucket=$bucket" `
        --add-volume-mount="volume=buzz-workspace,mount-path=/mnt/buzz"
} else {
    Write-Host "gcloud has no --add-volume; deploying image then attaching GCS via services replace"
    Invoke-Gcloud run deploy $svc `
        --project $Project `
        --region $Region `
        --image $image `
        --service-account $gooseSa `
        --cpu 2 `
        --memory 4Gi `
        --min-instances 0 `
        --max-instances 1 `
        --concurrency 16 `
        --timeout 3600 `
        --cpu-boost `
        --no-allow-unauthenticated `
        --ingress all `
        --port 8080 `
        --set-env-vars $envVars `
        --set-secrets ($secretParts -join ",")
    Set-GooseGcsVolume $svc $bucket
}
if ($help -match "--network") {
    Write-Host "attaching Direct VPC egress to default subnet"
    Invoke-Gcloud run services update $svc --project $Project --region $Region `
        --network=default --subnet=default --vpc-egress=private-ranges-only --quiet
} else {
    Write-Host "gcloud has no --network; attaching Direct VPC via services replace"
    Set-GooseDirectVpc $svc
}
Invoke-Gcloud run services add-iam-policy-binding $svc --project $Project --region $Region `
    --member="serviceAccount:$listenerEmail" --role="roles/run.invoker" --quiet
$workerUrl = (& gcloud run services describe $svc --project $Project --region $Region --format="value(status.url)").Trim()
Write-Host "GOOSE_WORKER_URL=$workerUrl"
