#Requires -Version 5.1
<#
.SYNOPSIS
  Push Desktop agent settings to GCP once (instructions, permissions, teams).
  Prefer the always-on BuzzCloudSync task installed by install-path.ps1.
#>
param(
    [string]$Project = $(if ($env:BUZZ_GCP_PROJECT) { $env:BUZZ_GCP_PROJECT } else { "your-gcp-project" }),
    [string]$Zone = $(if ($env:BUZZ_GCP_ZONE) { $env:BUZZ_GCP_ZONE } else { "us-central1-a" }),
    [string]$Instance = $(if ($env:BUZZ_GCP_INSTANCE) { $env:BUZZ_GCP_INSTANCE } else { "buzz-listener" })
)

$ErrorActionPreference = "Stop"
$env:BUZZ_GCP_PROJECT = $Project
$env:BUZZ_GCP_ZONE = $Zone
$env:BUZZ_GCP_INSTANCE = $Instance

$sync = $null
foreach ($candidate in @(
        (Join-Path $env:USERPROFILE ".local\bin\buzz_cloud_sync.py"),
        (Join-Path $PSScriptRoot "buzz-cloud-sync.py")
    )) {
    if (Test-Path -LiteralPath $candidate) {
        $sync = $candidate
        break
    }
}
if (-not $sync) {
    throw "buzz-cloud-sync.py not found. Run windows\install-path.ps1 first."
}

$python = $null
$cmd = Get-Command python.exe -ErrorAction SilentlyContinue
if ($cmd -and $cmd.Source -notlike "*\WindowsApps\*") {
    $python = $cmd.Source
}
if (-not $python) {
    foreach ($p in @("C:\Python313\python.exe", "C:\Python312\python.exe", "C:\Python311\python.exe")) {
        if (Test-Path $p) { $python = $p; break }
    }
}
if (-not $python) { throw "python.exe not found" }

& $python $sync --once
if ($LASTEXITCODE -ne 0) { throw "cloud sync --once failed ($LASTEXITCODE)" }
Write-Host "Synced Desktop agent settings to $Instance."
