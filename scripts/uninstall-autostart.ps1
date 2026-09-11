# Removes the TokenMonitor logon task and stops the running server.
$taskName = "TokenMonitor"
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
& (Join-Path $PSScriptRoot "stop.ps1")
Write-Host "Removed scheduled task '$taskName'."
