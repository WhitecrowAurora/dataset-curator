@echo off
rem Keep this batch file ASCII: cmd.exe uses the active console code page.
rem Browser entry; the desktop shell is a separate launcher.
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=27100"

set "PY=%ROOT%env\python\python.exe"
if not exist "%PY%" set "PY=python"

echo [web] Starting local service http://127.0.0.1:%PORT%/
echo [web] Close this window to stop the service.
start "" "http://127.0.0.1:%PORT%/"
call "%ROOT%tagger.cmd" web --host 127.0.0.1 --port %PORT% --no-open
