# Stops the Token Monitor server (by port) and the desktop pet (by command line).
$root = Split-Path -Parent $PSScriptRoot
$port = 8787
$cfg = Join-Path $root "config.json"
if (Test-Path $cfg) {
    try { $j = Get-Content $cfg -Raw | ConvertFrom-Json; if ($j.port) { $port = [int]$j.port } } catch {}
}

$pets = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -match 'tokmon\.pet' }
foreach ($p in $pets) {
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Write-Host "Stopped pet (PID $($p.ProcessId))." } catch {}
}

$conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) { Write-Host "Server is not running on port $port."; exit 0 }
foreach ($c in $conns) {
    try {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop
        Write-Host "Stopped server (PID $($c.OwningProcess))."
    } catch { Write-Host "Could not stop PID $($c.OwningProcess): $_" }
}
