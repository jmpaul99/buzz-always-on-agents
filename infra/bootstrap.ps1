#Requires -Version 5.1
# Enable APIs, Artifact Registry, service accounts, IAP firewall.
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_common.ps1")

Write-Host "project=$Project region=$Region"
Invoke-Gcloud config set project $Project
Invoke-Gcloud config set compute/region $Region
Invoke-Gcloud config set compute/zone $Zone

$apis = @(
    "compute.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "iap.googleapis.com",
    "iam.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com"
)
Invoke-Gcloud services enable @apis --project $Project --quiet

$repoExists = $false
$arList = & gcloud artifacts repositories list --location $Region --project $Project --format="value(name)" 2>$null
if ($arList -match "(^|/)$($C.AR_REPO)$") { $repoExists = $true }
if (-not $repoExists) {
    Invoke-Gcloud artifacts repositories create $C.AR_REPO --repository-format=docker --location=$Region --project=$Project --quiet
}

foreach ($sa in @($C.LISTENER_SA, $C.LITELLM_SA)) {
    $email = "${sa}@${Project}.iam.gserviceaccount.com"
    $exists = & gcloud iam service-accounts describe $email --project $Project 2>$null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Gcloud iam service-accounts create $sa --display-name $sa --project $Project
    }
}

$listenerEmail = "$($C.LISTENER_SA)@$Project.iam.gserviceaccount.com"
$litellmEmail = "$($C.LITELLM_SA)@$Project.iam.gserviceaccount.com"
$user = (& gcloud config get-value account).Trim()

Invoke-Gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$listenerEmail" --role="roles/run.invoker" --quiet --condition=None
Invoke-Gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$listenerEmail" --role="roles/secretmanager.secretAccessor" --quiet --condition=None
Invoke-Gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$litellmEmail" --role="roles/secretmanager.secretAccessor" --quiet --condition=None
Invoke-Gcloud projects add-iam-policy-binding $Project --member="user:$user" --role="roles/iap.tunnelResourceAccessor" --quiet --condition=None
Invoke-Gcloud projects add-iam-policy-binding $Project --member="user:$user" --role="roles/compute.osLogin" --quiet --condition=None

& gcloud compute firewall-rules describe allow-iap-ssh --project $Project 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Invoke-Gcloud compute firewall-rules create allow-iap-ssh --project $Project `
        --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=$($C.IAP_TAG) `
        --description="IAP SSH only"
}
& gcloud compute firewall-rules describe allow-iap-8743 --project $Project 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    Invoke-Gcloud compute firewall-rules create allow-iap-8743 --project $Project `
        --allow=tcp:8743 --source-ranges=35.235.240.0/20 --target-tags=$($C.IAP_TAG) `
        --description="IAP TCP tunnel to listener control API"
}
& gcloud compute firewall-rules describe default-allow-ssh --project $Project 1>$null 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Disabling default-allow-ssh (0.0.0.0/22) if present"
    Invoke-Gcloud compute firewall-rules delete default-allow-ssh --project $Project --quiet
}

$projectNumber = (& gcloud projects describe $Project --format="value(projectNumber)").Trim()
if (-not $projectNumber) { throw "could not resolve project number for $Project" }
$cbSa = "${projectNumber}@cloudbuild.gserviceaccount.com"
Invoke-Gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$cbSa" --role="roles/artifactregistry.writer" --quiet --condition=None
Invoke-Gcloud projects add-iam-policy-binding $Project --member="serviceAccount:$cbSa" --role="roles/logging.logWriter" --quiet --condition=None
