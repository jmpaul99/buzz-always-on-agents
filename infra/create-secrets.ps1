#Requires -Version 5.1
# Create Secret Manager secrets from this process env. Never prints values.
$ErrorActionPreference = "Continue"
. (Join-Path $PSScriptRoot "_common.ps1")
Invoke-Gcloud config set project $Project --quiet

function Set-SecretValue {
    param([string]$Name, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        Write-Host "skip $Name (empty)"
        return
    }
    $tmp = Join-Path $env:TEMP "buzz-sec-$Name.txt"
    [IO.File]::WriteAllText($tmp, $Value)
    try {
        & gcloud secrets describe $Name --project $Project 1>$null 2>$null
        if ($LASTEXITCODE -ne 0) {
            Invoke-Gcloud secrets create $Name --project $Project --replication-policy=automatic
        }
        Invoke-Gcloud secrets versions add $Name --project $Project --data-file=$tmp
        Write-Host "upserted $Name (len=$($Value.Length))"
    } finally {
        Remove-Item -Force $tmp -ErrorAction SilentlyContinue
    }
}

function Set-SecretFile {
    param([string]$Name, [string]$Path)
    if (-not (Test-Path $Path)) {
        Write-Host "skip $Name (no file)"
        return
    }
    & gcloud secrets describe $Name --project $Project 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Invoke-Gcloud secrets create $Name --project $Project --replication-policy=automatic
    }
    Invoke-Gcloud secrets versions add $Name --project $Project --data-file=$Path
    Write-Host "upserted $Name from file (bytes=$((Get-Item $Path).Length))"
}

if (-not $env:LITELLM_MASTER_KEY) {
    & gcloud secrets describe litellm-master-key --project $Project 1>$null 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "LITELLM_MASTER_KEY unset; keeping existing Secret Manager value"
    } else {
        $bytes = New-Object byte[] 32
        [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $env:LITELLM_MASTER_KEY = "sk-" + ([BitConverter]::ToString($bytes).Replace("-", "").ToLower())
        Write-Host "generated LITELLM_MASTER_KEY (not printed)."
        [Environment]::SetEnvironmentVariable("LITELLM_MASTER_KEY", $env:LITELLM_MASTER_KEY, "User")
    }
}

Set-SecretValue "gemini-api-key" $env:GEMINI_API_KEY
Set-SecretValue "groq-api-key" $env:GROQ_API_KEY
Set-SecretValue "nvidia-nim-api-key" $env:NVIDIA_NIM_API_KEY
Set-SecretValue "openrouter-api-key" $env:OPENROUTER_API_KEY
Set-SecretValue "litellm-master-key" $env:LITELLM_MASTER_KEY
Set-SecretValue "github-pat" $env:GITHUB_PERSONAL_ACCESS_TOKEN
Set-SecretValue "tavily-api-key" $env:TAVILY_API_KEY
Set-SecretValue "stripe-api-key" $env:STRIPE_API_KEY
Set-SecretFile "gcloud-adc" (Join-Path $env:APPDATA "gcloud\application_default_credentials.json")
Write-Host "secrets upserted. Empty GEMINI/GROQ/NIM/OpenRouter/GitHub/Tavily/Stripe are skipped (never overwrite with '-')."
