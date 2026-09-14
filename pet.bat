@echo off
rem Start only the desktop pet (it starts the server itself if needed).
cd /d "%~dp0"
set "TOKMON_PYTHONW=pythonw"
where pythonw >nul 2>nul
if %errorlevel% neq 0 set "TOKMON_PYTHONW=%LocalAppData%\Programs\Python\Python313\pythonw.exe"
start "" "%TOKMON_PYTHONW%" -m tokmon.pet
