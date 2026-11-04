@echo off
rem ===========================================================================
rem  One-click installer (Windows).
rem
rem  Builds the embedded Python environment in env\python and installs the
rem  small runtime packages. Torch / vLLM are NOT installed here - those are
rem  big and each member installs them on demand into the same environment.
rem
rem  All real logic lives in devtools\setup_embedded_env.ps1. This file only
rem  forwards arguments, so there is exactly one implementation to maintain.
rem
rem  Usage:
rem    install.bat                                        normal (idempotent)
rem    install.bat -Force                                 rebuild from scratch
rem    install.bat -WheelDir env\initialization\wheels     offline install
rem
rem  Linux / macOS: use install.sh instead.
rem
rem  NOTE: keep this file pure ASCII. cmd.exe reads batch files with the active
rem  code page and, once chcp changes mid-file, it mis-aligns lines and starts
rem  executing fragments of comments as commands. Chinese text belongs in the
rem  PowerShell script (which has a BOM) or in the docs, not here.
rem ===========================================================================
setlocal

cd /d "%~dp0"

echo.
echo  ===========================================
echo   tagger - environment setup
echo   repo: %CD%
echo  ===========================================
echo.

where powershell >nul 2>nul
if errorlevel 1 (
    echo  [ERROR] powershell not found.
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "devtools\setup_embedded_env.ps1" %*
if errorlevel 1 (
    echo.
    echo  [ERROR] setup failed. Common causes:
    echo    1. env\initialization\ has no python-*-embed-amd64.zip
    echo    2. no network and no -WheelDir given
    echo    3. antivirus blocked the PowerShell script
    exit /b 1
)

rem The import check already ran inside the PowerShell script; duplicating it
rem here only adds a second, slightly different block of output.

echo.
echo  Done. Next steps:
echo    tagger.cmd --help
echo    tagger.cmd report --help
echo    env\python\python.exe -m pytest
echo.
echo  Big packages (optional, install into the same environment):
echo    env\python\python.exe -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124
echo.
endlocal
exit /b 0
