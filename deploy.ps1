#Requires -Version 5.1
<#
.SYNOPSIS
    One command to deploy the Buzz always-on GCP stack and (on Windows) the Desktop plugin.

.EXAMPLE
    .\deploy.ps1
#>
param(
    [switch]$SkipDesktop,
    [switch]$SkipAuth,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
if (Test-Path variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}
$Root = $PSScriptRoot
if (-not $Root) { $Root = (Get-Location).Path }
$Infra = Join-Path $Root "infra"
. (Join-Path $Infra "_env.ps1")

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message"
}

function Test-Exe {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    return [bool]$cmd
}

function Assert-Gcloud {
    if (-not (Test-Exe "gcloud") -and -not (Test-Exe "gcloud.cmd")) {
        throw "gcloud is not on PATH. Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install then re-run .\deploy.ps1"
    }
}

function Get-CPython {
    foreach ($candidate in @(
            (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
            "C:\Python313\python.exe",
            "C:\Python312\python.exe",
            "C:\Python311\python.exe"
        )) {
        if ($candidate -and ($candidate -notlike "*\WindowsApps\*") -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }
    return $null
}

function Ensure-EnvFiles {
    $dotenv = Join-Path $Root ".env"
    $dotenvEx = Join-Path $Root ".env.example"
    $cfg = Join-Path $Infra "config.env"
    $cfgEx = Join-Path $Infra "config.env.example"
    if (-not (Test-Path -LiteralPath $dotenv)) {
        if (-not (Test-Path -LiteralPath $dotenvEx)) { throw "missing .env.example" }
        Copy-Item -LiteralPath $dotenvEx -Destination $dotenv
        Write-Host "created .env from .env.example (gitignored)"
    }
    if (-not (Test-Path -LiteralPath $cfg)) {
        if (-not (Test-Path -LiteralPath $cfgEx)) { throw "missing infra/config.env.example" }
        Copy-Item -LiteralPath $cfgEx -Destination $cfg
        Write-Host "created infra/config.env from example (gitignored)"
    }
}

function Save-Config {
    param([string]$Project, [string]$Region, [string]$Zone, [string]$Instance, [string]$Relay)
    $dotenv = Join-Path $Root ".env"
    $cfg = Join-Path $Infra "config.env"
    Set-DotEnvKey $dotenv "BUZZ_GCP_PROJECT" $Project
    Set-DotEnvKey $dotenv "GOOGLE_CLOUD_PROJECT" $Project
    Set-DotEnvKey $dotenv "BUZZ_GCP_ZONE" $Zone
    Set-DotEnvKey $dotenv "BUZZ_GCP_INSTANCE" $Instance
    Set-DotEnvKey $dotenv "BUZZ_RELAY_URL" $Relay
    Set-DotEnvKey $cfg "GCP_PROJECT" $Project
    Set-DotEnvKey $cfg "GCP_REGION" $Region
    Set-DotEnvKey $cfg "GCP_ZONE" $Zone
    Set-DotEnvKey $cfg "LISTENER_INSTANCE" $Instance
    Set-DotEnvKey $cfg "RELAY_URL" $Relay
    foreach ($pair in @(
            @{ K = "BUZZ_GCP_PROJECT"; V = $Project },
            @{ K = "GCP_PROJECT"; V = $Project },
            @{ K = "GOOGLE_CLOUD_PROJECT"; V = $Project },
            @{ K = "BUZZ_GCP_REGION"; V = $Region },
            @{ K = "GCP_REGION"; V = $Region },
            @{ K = "BUZZ_GCP_ZONE"; V = $Zone },
            @{ K = "GCP_ZONE"; V = $Zone },
            @{ K = "BUZZ_GCP_INSTANCE"; V = $Instance },
            @{ K = "BUZZ_RELAY_URL"; V = $Relay },
            @{ K = "RELAY_URL"; V = $Relay }
        )) {
        [Environment]::SetEnvironmentVariable($pair.K, $pair.V, "Process")
        [Environment]::SetEnvironmentVariable($pair.K, $pair.V, "User")
    }
}

function Invoke-GcloudAuth {
    $account = (& gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>$null | Select-Object -First 1)
    if ([string]::IsNullOrWhiteSpace($account)) {
        if ($NonInteractive) { throw "No active gcloud account. Run 'gcloud auth login' and re-run." }
        Write-Host "No active gcloud account. Opening browser for gcloud auth login..."
        & gcloud auth login
        if ($LASTEXITCODE -ne 0) { throw "gcloud auth login failed ($LASTEXITCODE)" }
        $account = (& gcloud auth list --filter="status:ACTIVE" --format="value(account)" 2>$null | Select-Object -First 1)
        if ([string]::IsNullOrWhiteSpace($account)) { throw "gcloud auth login did not produce an active account." }
    }
    Write-Host "gcloud account: $account"

    & gcloud auth application-default print-access-token 1>$null 2>$null
    if ($LASTEXITCODE -ne 0) {
        if ($NonInteractive) { throw "Application Default Credentials missing. Run 'gcloud auth application-default login' and re-run." }
        Write-Host "Application Default Credentials missing. Opening browser for ADC login..."
        & gcloud auth application-default login
        if ($LASTEXITCODE -ne 0) { throw "gcloud auth application-default login failed ($LASTEXITCODE)" }
    }
    Write-Host "Application Default Credentials: ok"
    return $account.Trim()
}

function Resolve-ProjectId {
    param([string]$Current)
    if (-not (Test-BuzzPlaceholder $Current)) { return $Current }
    if ($NonInteractive) {
        throw "GCP project is not set. Fill BUZZ_GCP_PROJECT in .env (or GCP_PROJECT in infra/config.env) and re-run."
    }
    Write-Host ""
    Write-Host "Your GCP projects:"
    & gcloud projects list --format="table(projectId,name,projectNumber)" --limit 25
    Write-Host "Create one at https://console.cloud.google.com/projectcreate if you need a new project (billing must be enabled)."
    $entered = (Read-Host "GCP project id").Trim()
    if (Test-BuzzPlaceholder $entered) { throw "A real GCP project id is required." }
    return $entered
}

function Resolve-RelayUrl {
    param([string]$Current)
    if (-not (Test-BuzzPlaceholder $Current)) { return $Current }
    if ($NonInteractive) {
        throw "Relay URL is not set. Fill BUZZ_RELAY_URL in .env (or RELAY_URL in infra/config.env) and re-run."
    }
    $entered = (Read-Host "Block community relay URL (wss://....communities.buzz.xyz)").Trim()
    if (Test-BuzzPlaceholder $entered) { throw "A real wss:// relay URL is required." }
    if ($entered -notmatch "^wss://") { throw "Relay URL must start with wss://" }
    return $entered
}

function Import-ProviderKeys {
    $dotenv = Join-Path $Root ".env"
    $keys = @(
        @{ Name = "GEMINI_API_KEY"; Optional = $false },
        @{ Name = "GROQ_API_KEY"; Optional = $false },
        @{ Name = "NVIDIA_NIM_API_KEY"; Optional = $false },
        @{ Name = "OPENROUTER_API_KEY"; Optional = $false },
        @{ Name = "GITHUB_PERSONAL_ACCESS_TOKEN"; Optional = $true },
        @{ Name = "TAVILY_API_KEY"; Optional = $true },
        @{ Name = "STRIPE_API_KEY"; Optional = $true }
    )
    $missing = @($keys | Where-Object { Test-BuzzPlaceholder ([Environment]::GetEnvironmentVariable($_.Name, "Process")) })
    $missingRequired = @($missing | Where-Object { -not $_.Optional })
    if ($missing.Count -eq 0) { return }

    if ($NonInteractive) {
        if ($missingRequired.Count -eq 4) {
            Write-Host "warning: no LLM provider keys in the environment; LiteLLM will start but model calls will fail until you add keys and re-run."
        }
        return
    }

    Write-Host ""
    Write-Host "Provider keys (typed values are hidden; Enter skips). Empty keys are not written and will not overwrite Secret Manager."
    Write-Host "At least one of Gemini / Groq / NIM / OpenRouter is recommended."
    foreach ($k in $missing) {
        $hint = if ($k.Optional) { "optional" } else { "LLM" }
        $value = Read-SecretOrSkip "$($k.Name) [$hint]"
        if (-not (Test-BuzzPlaceholder $value)) {
            [Environment]::SetEnvironmentVariable($k.Name, $value, "Process")
            Set-DotEnvKey $dotenv $k.Name $value
        }
    }
}

Write-Step "Checking prerequisites"
Assert-Gcloud
if (-not $SkipDesktop) {
    if (-not (Get-CPython)) {
        throw "CPython python.exe not found (the Microsoft Store alias does not count). Install Python from python.org and re-run, or pass -SkipDesktop."
    }
    $csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
    if (-not (Test-Path -LiteralPath $csc)) {
        throw "csc.exe not found at $csc (needed to build the Desktop PATH plugin). Pass -SkipDesktop to deploy GCP only."
    }
}
Write-Host "gcloud: ok"

Write-Step "Loading config"
Ensure-EnvFiles
Import-DotEnvToProcess (Join-Path $Root ".env")
Import-DotEnvToProcess (Join-Path $Infra "config.env")

$region = Get-FirstFilled @("GCP_REGION", "BUZZ_GCP_REGION")
if (-not $region) { $region = "us-central1" }
$zone = Get-FirstFilled @("GCP_ZONE", "BUZZ_GCP_ZONE")
if (-not $zone) { $zone = "us-central1-a" }
$instance = Get-FirstFilled @("LISTENER_INSTANCE", "BUZZ_GCP_INSTANCE")
if (-not $instance) { $instance = "buzz-listener" }

if (-not $SkipAuth) {
    Write-Step "Google Cloud authentication"
    [void](Invoke-GcloudAuth)
} else {
    Write-Host "skipping gcloud auth (-SkipAuth)"
}

Write-Step "Project and relay"
$project = Resolve-ProjectId (Get-FirstFilled @("GCP_PROJECT", "BUZZ_GCP_PROJECT", "GOOGLE_CLOUD_PROJECT"))
& gcloud projects describe $project --format="value(projectId)" 1>$null 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "GCP project '$project' was not found (or you cannot access it). Create it in the console and re-run."
}
& gcloud config set project $project --quiet
if ($LASTEXITCODE -ne 0) { throw "gcloud config set project failed" }

$billing = (& gcloud billing projects describe $project --format="value(billingEnabled)" 2>$null)
if ($billing -and $billing.Trim().ToLowerInvariant() -eq "false") {
    throw "Billing is not enabled on '$project'. Link a billing account at https://console.cloud.google.com/billing/linkedaccount?project=$project"
}

$relay = Resolve-RelayUrl (Get-FirstFilled @("BUZZ_RELAY_URL", "RELAY_URL"))
Save-Config -Project $project -Region $region -Zone $zone -Instance $instance -Relay $relay
Write-Host "project=$project region=$region zone=$zone instance=$instance"
Write-Host "relay=$relay"

Write-Step "Provider keys"
Import-ProviderKeys
$llmPresent = @("GEMINI_API_KEY", "GROQ_API_KEY", "NVIDIA_NIM_API_KEY", "OPENROUTER_API_KEY") | Where-Object {
    -not (Test-BuzzPlaceholder ([Environment]::GetEnvironmentVariable($_, "Process")))
}
if ($llmPresent.Count -eq 0) {
    Write-Host "warning: no LLM provider keys set. Deploy will continue; model calls will fail until you add keys to .env and re-run."
} else {
    Write-Host ("LLM keys present: " + ($llmPresent -join ", "))
}

if (-not $NonInteractive) {
    Write-Host ""
    $go = Read-Host "Deploy the GCP stack to '$project' now? [Y/n]"
    if ($go -and $go.Trim() -match "^(n|no)$") { throw "Aborted." }
}

Write-Step "Deploying GCP stack (APIs, secrets, LiteLLM, listener)"
& (Join-Path $Infra "deploy-all.ps1")
if ($LASTEXITCODE -ne 0) { throw "infra/deploy-all.ps1 failed ($LASTEXITCODE)" }

if (-not $SkipDesktop) {
    Write-Step "Installing Buzz Desktop PATH plugin and cloud sync"
    & (Join-Path $Root "windows\install-path.ps1")
    if ($LASTEXITCODE -ne 0) { throw "windows/install-path.ps1 failed ($LASTEXITCODE)" }
}

Write-Host ""
Write-Host "Done. Cloud stack is in project $project."
Write-Host "Next:"
Write-Host "  1. Restart Buzz Desktop so Run on → cloud appears."
Write-Host "  2. Stop any local agent copies. Identity stays on this PC; buzz-acp + LiteLLM run on GCP."
Write-Host "  3. Create or switch agents to Run on → cloud. BuzzCloudSync pushes nsecs to the listener over IAP."
if ($SkipDesktop) {
    Write-Host "Desktop plugin was skipped. On the Buzz Desktop PC run: .\windows\install-path.ps1 (or ./macos/install-path.sh on a Mac)"
}
