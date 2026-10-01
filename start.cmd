@echo off
rem ASCII only: a non-ASCII byte after "chcp" corrupts how cmd.exe parses the rest of the file.
rem Also works as restart: a web UI already running on the port is stopped first.
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "app\stop_server.ps1"
if errorlevel 1 goto busy
python -m pip install -q -r "app\requirements.txt"
if errorlevel 1 goto nodeps
rem The .DbLib connection string must hold an absolute path (ODBC resolves relative ones
rem against Altium's working folder), so point it at wherever this folder is now.
if exist "GH_DB_LIB.sqlite" python "app\make_dblib.py"
python "app\main.py"
goto end
:busy
echo.
echo The port is taken by another program - change "port" in app\config.json.
goto end
:nodeps
echo.
echo Dependency install failed. Check that Python 3.10+ is on PATH as "python".
:end
pause
