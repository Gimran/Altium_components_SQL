@echo off
rem ASCII only: a non-ASCII byte after "chcp" corrupts how cmd.exe parses the rest of the file.
rem Also works as restart: a web UI already running on the port is stopped first.
chcp 65001 >nul
setlocal
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
rem "python" may be missing or be the Microsoft Store stub; the "py" launcher is the fallback.
set "PY="
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY goto nopython
powershell -NoProfile -ExecutionPolicy Bypass -File "app\stop_server.ps1"
if errorlevel 1 goto busy
rem pip only when something is missing, so a machine without internet still starts.
%PY% -c "import importlib.metadata as m; [m.version(n.strip()) for n in open('app/requirements.txt') if n.strip()]" >nul 2>&1
if errorlevel 1 (
    %PY% -m pip install -q -r "app\requirements.txt"
    if errorlevel 1 goto nodeps
)
rem The .DbLib connection string must hold an absolute path (ODBC resolves relative ones
rem against Altium's working folder), so point it at wherever this folder is now.
if exist "GH_DB_LIB.sqlite" %PY% "app\make_dblib.py"
%PY% "app\main.py"
goto end
:busy
echo.
echo The port is taken by another program - change "port" in app\config.json.
goto end
:nopython
echo.
echo Python 3.10+ not found. Install it from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH" in the installer.
goto end
:nodeps
echo.
echo Dependency install failed - check the internet connection, then run start.cmd again.
:end
pause
