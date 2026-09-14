@echo off
rem Start Token Monitor: background server (no console), the desktop pet, and open the dashboard.
cd /d "%~dp0"
set "TOKMON_PYTHONW=pythonw"
where pythonw >nul 2>nul
if %errorlevel% neq 0 set "TOKMON_PYTHONW=%LocalAppData%\Programs\Python\Python313\pythonw.exe"
if not exist "%TOKMON_PYTHONW%" if "%TOKMON_PYTHONW%" neq "pythonw" (
  echo pythonw.exe not found. Install Python 3.11+ from python.org and tick "Add to PATH".
  pause
  exit /b 1
)
start "" "%TOKMON_PYTHONW%" -m tokmon.server --open
start "" "%TOKMON_PYTHONW%" -m tokmon.pet
