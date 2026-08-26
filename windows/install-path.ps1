#Requires -Version 5.1
# Put buzz-backend-cloud.exe on the user PATH so Desktop shows Run on → cloud.
# Desktop stages PATH plugins as %TEMP%\buzz-provider-*\provider.exe and
# CreateProcess that copy, so a .cmd/.bat shim cannot be the discovered file.
$ErrorActionPreference = "Stop"
$bin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$here = $PSScriptRoot
# Desktop globs PATH for buzz-backend-*. A .py next to the shim becomes Run on → cloud.py
# (Windows PATHEXT includes .PY). Keep the implementation under a non-matching name.
$impl = Join-Path $bin "buzz_cloud_impl.py"
Copy-Item -Force (Join-Path $here "buzz-backend-cloud.py") $impl
Remove-Item -Force (Join-Path $bin "buzz-backend-cloud.py") -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $bin "buzz-backend-cloud.cmd") -ErrorAction SilentlyContinue

$listenerUtil = Join-Path (Split-Path -Parent $here) "listener\agentutil.py"
if (-not (Test-Path -LiteralPath $listenerUtil)) {
    $listenerUtil = Join-Path $here "agentutil.py"
}
if (Test-Path -LiteralPath $listenerUtil) {
    Copy-Item -Force $listenerUtil (Join-Path $bin "agentutil.py")
}
$listenerNostr = Join-Path (Split-Path -Parent $here) "listener\nostrutil.py"
if (-not (Test-Path -LiteralPath $listenerNostr)) {
    $listenerNostr = Join-Path $here "nostrutil.py"
}
if (Test-Path -LiteralPath $listenerNostr) {
    Copy-Item -Force $listenerNostr (Join-Path $bin "nostrutil.py")
}
$syncSrc = Join-Path $here "buzz-cloud-sync.py"
$syncImpl = Join-Path $bin "buzz_cloud_sync.py"
Copy-Item -Force $syncSrc $syncImpl

$python = $null
foreach ($candidate in @(
        (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "C:\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python313\python.exe"
    )) {
    if ($candidate -and ($candidate -notlike "*\WindowsApps\*") -and (Test-Path -LiteralPath $candidate)) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    throw "python.exe not found (Windows Store alias does not count). Install CPython and re-run."
}
$pythonw = Join-Path (Split-Path -Parent $python) "pythonw.exe"
if (-not (Test-Path -LiteralPath $pythonw)) {
    $pythonw = $python
}

$gcloudDir = $null
foreach ($candidate in @(
        (Get-Command gcloud.cmd -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        (Get-Command gcloud.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source)
    )) {
    if ($candidate -and (Test-Path -LiteralPath $candidate)) {
        $gcloudDir = Split-Path -Parent $candidate
        break
    }
}
$pathExtra = @(
    (Split-Path -Parent $python),
    $bin,
    $gcloudDir,
    (Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin")
) | Where-Object { $_ } | Select-Object -Unique

$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if (-not (Test-Path -LiteralPath $csc)) {
    throw "csc.exe not found at $csc (needed to build buzz-backend-cloud.exe)"
}

$csTemplate = Join-Path $here "buzz-backend-cloud.cs"
$cs = Join-Path $env:TEMP "buzz-backend-cloud.cs"
$exe = Join-Path $bin "buzz-backend-cloud.exe"
$csText = [System.IO.File]::ReadAllText($csTemplate)
$csText = $csText.Replace("__PYTHON__", $python.Replace("\", "\\"))
$csText = $csText.Replace("__IMPL__", $impl.Replace("\", "\\"))
$csText = $csText.Replace("__PATH_EXTRA__", ($pathExtra -join ";").Replace("\", "\\"))
[System.IO.File]::WriteAllText($cs, $csText)
& $csc /nologo /optimize /target:exe /out:$exe $cs
if ($LASTEXITCODE -ne 0) {
    throw "csc.exe failed to build $exe"
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$bin*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$bin", "User")
    $env:Path = "$env:Path;$bin"
    Write-Host "Added $bin to the user PATH. Restart Buzz Desktop so Run on → cloud appears."
} else {
    Write-Host "Already on PATH: $bin"
    Write-Host "Restart Buzz Desktop so it picks up buzz-backend-cloud.exe instead of the old .cmd shim."
}
Write-Host "Provider id: cloud (default). New agents should Run on → cloud; stop any local Goose copy."

function Stop-BuzzCloudLeftovers {
    & schtasks.exe /End /TN "BuzzCloudSync" 2>$null | Out-Null
    Get-CimInstance Win32_Process | ForEach-Object {
        $cl = [string]$_.CommandLine
        $nm = ([string]$_.Name).ToLowerInvariant()
        $procId = $_.ProcessId
        if ($procId -eq $PID) { return }
        $hit = $cl -match 'buzz_cloud_sync|buzz-cloud-sync' -or
            ($cl -match 'start-iap-tunnel' -and $cl -match 'buzz-listener') -or
            ($cl -match 'buzz-listener' -and $cl -match '8743') -or
            ($nm -match '^(putty|plink)\.exe$' -and $cl -match '8743')
        if ($hit) {
            & taskkill.exe /PID $procId /T /F 2>$null | Out-Null
        }
    }
}

Stop-BuzzCloudLeftovers

$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$syncImpl`"" -WorkingDirectory $bin
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -StartWhenAvailable -Hidden
    $settings.ExecutionTimeLimit = "PT0S"
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "BuzzCloudSync" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Buzz Desktop to GCP agent sync (silent)" -Force | Out-Null
    $registered = $true
} catch {
    Write-Host "Could not replace BuzzCloudSync task from this session ($($_.Exception.Message)). The existing logon task will hand off to pythonw."
}

if ($registered) {
    Start-ScheduledTask -TaskName "BuzzCloudSync" -ErrorAction SilentlyContinue
    if (-not $?) {
        & schtasks.exe /Run /TN "BuzzCloudSync" 2>$null | Out-Null
    }
}

Start-Process -FilePath $pythonw -ArgumentList "`"$syncImpl`"" -WorkingDirectory $bin -WindowStyle Hidden
Write-Host "BuzzCloudSync is running silently ($pythonw). One instance; no extra consoles or PuTTY windows."
