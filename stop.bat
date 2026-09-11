@echo off
rem Stop the Token Monitor server (whatever is listening on the configured port).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop.ps1"
