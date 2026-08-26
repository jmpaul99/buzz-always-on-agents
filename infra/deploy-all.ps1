#Requires -Version 5.1
# Full GCP stack: IAM/AR, secrets, LiteLLM, Goose worker, e2-micro listener.
$ErrorActionPreference = "Continue"
$here = $PSScriptRoot
& (Join-Path $here "bootstrap.ps1")
& (Join-Path $here "create-secrets.ps1")
& (Join-Path $here "deploy-litellm.ps1")
& (Join-Path $here "deploy-goose-job.ps1")
& (Join-Path $here "deploy-listener.ps1")
Write-Host "GCP stack deployed. If you used .\deploy.ps1, the Desktop plugin is next; otherwise run windows\install-path.ps1 or macos/install-path.sh"
