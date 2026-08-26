#Requires -Version 5.1
# Dotenv helpers shared by deploy.ps1 and _common.ps1. Never prints secret values.

function Test-BuzzPlaceholder {
    param([string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { return $true }
    $v = $Value.Trim()
    if ($v -eq "-" -or $v -eq "your-gcp-project") { return $true }
    if ($v -match "your-community\.communities\.buzz\.xyz") { return $true }
    return $false
}

function Read-DotEnvFile {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") { return }
        $k, $v = $line.Split("=", 2)
        if (-not $k) { return }
        $k = $k.Trim()
        $v = if ($null -eq $v) { "" } else { $v.Trim() }
        if ($v.Length -ge 2) {
            $q = $v[0]
            if (($q -eq [char]'"' -or $q -eq [char]"'") -and $v[-1] -eq $q) {
                $v = $v.Substring(1, $v.Length - 2)
            }
        }
        $map[$k] = $v
    }
    return $map
}

function Import-DotEnvToProcess {
    param([string]$Path)
    $map = Read-DotEnvFile $Path
    foreach ($k in @($map.Keys)) {
        $existing = [Environment]::GetEnvironmentVariable($k, "Process")
        if (-not [string]::IsNullOrWhiteSpace($existing)) { continue }
        $v = [string]$map[$k]
        if (Test-BuzzPlaceholder $v) { continue }
        [Environment]::SetEnvironmentVariable($k, $v, "Process")
    }
}

function Set-DotEnvKey {
    param([string]$Path, [string]$Key, [string]$Value)
    if ([string]::IsNullOrWhiteSpace($Key)) { return }
    $lines = @()
    if (Test-Path -LiteralPath $Path) {
        $lines = @(Get-Content -LiteralPath $Path)
    }
    $out = New-Object System.Collections.Generic.List[string]
    $found = $false
    $pattern = "^\s*" + [regex]::Escape($Key) + "\s*="
    foreach ($line in $lines) {
        if ($line -match $pattern) {
            [void]$out.Add("$Key=$Value")
            $found = $true
        } else {
            [void]$out.Add($line)
        }
    }
    if (-not $found) {
        [void]$out.Add("$Key=$Value")
    }
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    [IO.File]::WriteAllLines($Path, $out.ToArray())
}

function Get-FirstFilled {
    param([string[]]$Names)
    foreach ($n in $Names) {
        $v = [Environment]::GetEnvironmentVariable($n, "Process")
        if (-not (Test-BuzzPlaceholder $v)) { return $v.Trim() }
    }
    return $null
}

function Read-SecretOrSkip {
    param([string]$Label)
    $secure = Read-Host $Label -AsSecureString
    if ($null -eq $secure -or $secure.Length -eq 0) { return "" }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
        if ($null -eq $plain) { return "" }
        return $plain.Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}
