# Registers a per-user Scheduled Task that starts Token Monitor (hidden) at logon.
# Runs as the current user so it can read ~/.claude and ~/.codex.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "TokenMonitor"

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
$fallback = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\pythonw.exe"
if (-not $pythonw -and (Test-Path $fallback)) { $pythonw = $fallback }
if (-not $pythonw) { throw "pythonw.exe not found on PATH. Install Python 3.11+ and tick 'Add to PATH'." }

# The pet starts the server itself when the port is not answering, so one task covers both.
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m tokmon.pet" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description "Claude / Codex usage monitor" | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Installed and started scheduled task '$taskName'. Dashboard: http://127.0.0.1:8787/"
