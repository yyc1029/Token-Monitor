@echo off
rem Start only the desktop pet (it starts the server itself if needed).
cd /d "%~dp0"
start "" pythonw -m tokmon.pet
