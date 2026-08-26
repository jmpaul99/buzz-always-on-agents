#Requires -Version 5.1
$ErrorActionPreference = "Continue"
$script:Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_env.ps1")
Import-DotEnvToProcess (Join-Path $Root ".env")
$cfgPath = Join-Path $PSScriptRoot "config.env"
Import-DotEnvToProcess $cfgPath
$script:C = @{
    GCP_REGION        = "us-central1"
    GCP_ZONE          = "us-central1-a"
    AR_REPO           = "buzz"
    LITELLM_SERVICE   = "litellm-goose"
    GOOSE_SERVICE     = "goose-worker"
    LISTENER_INSTANCE = "buzz-listener"
    LISTENER_SA       = "buzz-listener"
    GOOSE_SA          = "goose-job"
    LITELLM_SA        = "litellm-goose"
    IAP_TAG           = "iap-ssh"
}

if (Test-Path -LiteralPath $cfgPath) {
    $cfg = Get-Content $cfgPath | Where-Object { $_ -match "=" -and $_ -notmatch "^\s*#" }
    foreach ($line in $cfg) {
        $k, $v = $line.Split("=", 2)
        if ($k -and -not (Test-BuzzPlaceholder $v)) { $C[$k.Trim()] = $v.Trim() }
    }
}

function Set-FromEnv([string]$Key, [string[]]$Names) {
    foreach ($n in $Names) {
        $v = [Environment]::GetEnvironmentVariable($n)
        if (-not (Test-BuzzPlaceholder $v)) {
            $C[$Key] = $v.Trim()
            return
        }
    }
}

Set-FromEnv "GCP_PROJECT" @("GCP_PROJECT", "BUZZ_GCP_PROJECT", "GOOGLE_CLOUD_PROJECT")
Set-FromEnv "GCP_REGION" @("GCP_REGION", "BUZZ_GCP_REGION")
Set-FromEnv "GCP_ZONE" @("GCP_ZONE", "BUZZ_GCP_ZONE")
Set-FromEnv "LISTENER_INSTANCE" @("BUZZ_GCP_INSTANCE")
Set-FromEnv "RELAY_URL" @("BUZZ_RELAY_URL", "RELAY_URL")

$script:Project = $C.GCP_PROJECT
$script:Region = $C.GCP_REGION
$script:Zone = $C.GCP_ZONE
$script:Ar = "{0}-docker.pkg.dev/{1}/{2}" -f $Region, $Project, $C.AR_REPO

if (Test-BuzzPlaceholder $Project) {
    throw "Set GCP_PROJECT or BUZZ_GCP_PROJECT (or run .\deploy.ps1 from the repo root)."
}

function Invoke-Gcloud {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Args)
    & gcloud @Args
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($Args -join ' ') failed ($LASTEXITCODE)" }
}
