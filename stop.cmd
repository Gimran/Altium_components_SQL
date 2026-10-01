@echo off
rem Stop the web UI. start.cmd does this by itself before starting, so it doubles as restart.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "app\stop_server.ps1"
pause
