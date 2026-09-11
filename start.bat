@echo off
rem Start Token Monitor: background server (no console), the desktop pet, and open the dashboard.
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel% neq 0 (
  echo pythonw.exe not found. Install Python 3.11+ from python.org and tick "Add to PATH".
  pause
  exit /b 1
)
start "" pythonw -m tokmon.server --open
start "" pythonw -m tokmon.pet
