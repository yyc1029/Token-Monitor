# Securely stores the Discord bot token for the current user, then restarts Token Monitor.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

Write-Host "Discord Token Monitor setup" -ForegroundColor Cyan
Write-Host "Paste the Bot Token below. The token will not be displayed."
$secure = Read-Host "Bot Token" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    if ([string]::IsNullOrWhiteSpace($token) -or $token.Length -lt 20) {
        throw "The token is empty or too short. Nothing was changed."
    }
    [Environment]::SetEnvironmentVariable("TOKMON_DISCORD_TOKEN", $token, "User")
    $env:TOKMON_DISCORD_TOKEN = $token
} finally {
    if ($ptr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
    $token = $null
    $secure.Dispose()
}

& (Join-Path $PSScriptRoot "stop.ps1")

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
$fallback = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\pythonw.exe"
if (-not $pythonw -and (Test-Path $fallback)) { $pythonw = $fallback }
if (-not $pythonw) { throw "pythonw.exe was not found." }

Start-Process -FilePath $pythonw -ArgumentList "-m", "tokmon.server", "--open" `
    -WorkingDirectory $root -WindowStyle Hidden
Start-Process -FilePath $pythonw -ArgumentList "-m", "tokmon.pet" `
    -WorkingDirectory $root -WindowStyle Hidden

Write-Host ""
Write-Host "Discord token saved. Token Monitor has been restarted." -ForegroundColor Green
Write-Host "Try /status in your Discord server after the bot appears online."
