#Requires -Version 5.1
# Put buzz-backend-cloud.exe on the user PATH so Desktop shows Run on → cloud.
# Desktop stages PATH plugins as %TEMP%\buzz-provider-*\provider.exe and
# CreateProcess that copy, so a .cmd/.bat shim cannot be the discovered file.
$ErrorActionPreference = "Stop"
$bin = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $bin | Out-Null
$here = $PSScriptRoot
$root = Split-Path -Parent $here
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

function Import-BuzzDotEnv([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    Get-Content -LiteralPath $Path | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") { return }
        $k, $v = $line.Split("=", 2)
        $k = $k.Trim()
        $v = $v.Trim().Trim('"').Trim("'")
        if (-not $k) { return }
        $cur = [Environment]::GetEnvironmentVariable($k)
        if ([string]::IsNullOrWhiteSpace($cur)) {
            Set-Item -Path "Env:$k" -Value $v
        }
    }
}
Import-BuzzDotEnv (Join-Path $root ".env")
Import-BuzzDotEnv (Join-Path $root "infra\config.env")
$project = $env:BUZZ_GCP_PROJECT
if ([string]::IsNullOrWhiteSpace($project) -or $project -eq "your-gcp-project") { $project = $env:GCP_PROJECT }
if ([string]::IsNullOrWhiteSpace($project) -or $project -eq "your-gcp-project") { $project = $env:GOOGLE_CLOUD_PROJECT }
if ([string]::IsNullOrWhiteSpace($project) -or $project -eq "your-gcp-project") {
    $gc = & gcloud config get-value project 2>$null
    if ($gc) { $project = "$gc".Trim() }
}
if ([string]::IsNullOrWhiteSpace($project)) { $project = "your-gcp-project" }
$zone = $env:BUZZ_GCP_ZONE
if ([string]::IsNullOrWhiteSpace($zone)) { $zone = $env:GCP_ZONE }
if ([string]::IsNullOrWhiteSpace($zone)) { $zone = "us-central1-a" }
$instance = $env:BUZZ_GCP_INSTANCE
if ([string]::IsNullOrWhiteSpace($instance)) { $instance = $env:LISTENER_INSTANCE }
if ([string]::IsNullOrWhiteSpace($instance)) { $instance = "buzz-listener" }
$env:BUZZ_GCP_PROJECT = $project
$env:BUZZ_GCP_ZONE = $zone
$env:BUZZ_GCP_INSTANCE = $instance
@(
    "BUZZ_GCP_PROJECT=$project",
    "BUZZ_GCP_ZONE=$zone",
    "BUZZ_GCP_INSTANCE=$instance"
) | Set-Content -Encoding ascii -LiteralPath (Join-Path $bin "buzz-cloud.env")
foreach ($pair in @(
        @{ K = "BUZZ_GCP_PROJECT"; V = $project },
        @{ K = "BUZZ_GCP_ZONE"; V = $zone },
        @{ K = "BUZZ_GCP_INSTANCE"; V = $instance }
    )) {
    if (-not [Environment]::GetEnvironmentVariable($pair.K, "User")) {
        [Environment]::SetEnvironmentVariable($pair.K, $pair.V, "User")
    }
}

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
Write-Host "Provider id: cloud (default). New agents should Run on → cloud; stop any local agent copy."

function Stop-BuzzCloudLeftovers {
    cmd.exe /c "schtasks /End /TN BuzzCloudSync >nul 2>&1" | Out-Null
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
            cmd.exe /c "taskkill /PID $procId /T /F >nul 2>&1" | Out-Null
        }
    }
}

Stop-BuzzCloudLeftovers

$registered = $false
try {
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$syncImpl`"" -WorkingDirectory $bin
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd -StartWhenAvailable -Hidden -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $settings.ExecutionTimeLimit = "PT0S"
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName "BuzzCloudSync" -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Buzz Desktop to GCP agent sync (silent)" -Force -ErrorAction Stop | Out-Null
    $registered = $true
} catch {
    Write-Host "Could not replace BuzzCloudSync task ($($_.Exception.Message)). Trying restart-on-failure on the existing task."
    try {
        $existing = Get-ScheduledTask -TaskName "BuzzCloudSync" -ErrorAction Stop
        $existing.Settings.RestartCount = 3
        $existing.Settings.RestartInterval = "PT1M"
        $existing.Settings.ExecutionTimeLimit = "PT0S"
        $existing.Settings.MultipleInstances = "IgnoreNew"
        Set-ScheduledTask -InputObject $existing -ErrorAction Stop | Out-Null
        $registered = $true
        Write-Host "Updated BuzzCloudSync restart-on-failure (3 retries, 1 minute)."
    } catch {
        Write-Host "Could not update BuzzCloudSync settings ($($_.Exception.Message)). Trying schtasks XML."
        try {
            $xmlPath = Join-Path $env:TEMP "BuzzCloudSync.xml"
            $userId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
            $xmlUser = [System.Security.SecurityElement]::Escape($userId)
            $xmlPy = [System.Security.SecurityElement]::Escape($pythonw)
            $xmlSync = [System.Security.SecurityElement]::Escape($syncImpl)
            $xmlBin = [System.Security.SecurityElement]::Escape($bin)
            $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Buzz Desktop to GCP agent sync (silent)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$xmlUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$xmlPy</Command>
      <Arguments>"$xmlSync"</Arguments>
      <WorkingDirectory>$xmlBin</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
            [System.IO.File]::WriteAllText($xmlPath, $xml, [System.Text.Encoding]::Unicode)
            $prevPref = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            cmd.exe /c "schtasks /Create /TN BuzzCloudSync /XML `"$xmlPath`" /F"
            $xmlOk = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = $prevPref
            if ($xmlOk) {
                $registered = $true
                Write-Host "Registered BuzzCloudSync via schtasks (restart on failure)."
            } else {
                Write-Host "Could not replace BuzzCloudSync via schtasks. Sidecar retry loop still runs in pythonw."
            }
        } catch {
            Write-Host "Could not replace BuzzCloudSync via schtasks ($($_.Exception.Message)). Sidecar retry loop still runs in pythonw."
        }
    }
}

if ($registered) {
    Start-ScheduledTask -TaskName "BuzzCloudSync" -ErrorAction SilentlyContinue
    if (-not $?) {
        & schtasks.exe /Run /TN "BuzzCloudSync" 2>$null | Out-Null
    }
}

Start-Process -FilePath $pythonw -ArgumentList "`"$syncImpl`"" -WorkingDirectory $bin -WindowStyle Hidden
Write-Host "BuzzCloudSync is running silently ($pythonw). One instance; no extra consoles or PuTTY windows."
