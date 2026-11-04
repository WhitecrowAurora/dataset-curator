@echo off
rem ===========================================================================
rem  Build a .zip that can be handed to another machine.
rem
rem  This is a WHITELIST packer: only the things listed below go in. Anything
rem  new in the repo is ignored until it is added here -- that is deliberate.
rem  models\ is 42GB and work\ run\ node_modules are junk for the receiver, and
rem  a blacklist would need updating every time one of those grows a new folder.
rem
rem  Usage:
rem    pack-dist.bat                    zip without env\python (~60MB)
rem    pack-dist.bat -WithEnv           zip with a pruned env\python
rem    pack-dist.bat -NoTests           skip tests\ (smaller, no self-check)
rem    pack-dist.bat -Out D:\x.zip      explicit output path
rem
rem  -WithEnv refuses to run while env\python still holds torch: that would
rem  produce a 4GB zip. Run prune-env.bat -Apply first, or drop -WithEnv.
rem
rem  NOTE: keep this file pure ASCII. cmd.exe reads batch files with the active
rem  code page; Chinese text here turns into mis-aligned lines and fragments of
rem  comments get executed as commands. Chinese belongs in the docs.
rem ===========================================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"
set "ROOT=%CD%"

set "WITHENV=0"
set "TESTS=1"
set "OUT="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="-WithEnv" (
    set "WITHENV=1"
    shift
    goto parse
)
if /i "%~1"=="-NoTests" (
    set "TESTS=0"
    shift
    goto parse
)
if /i "%~1"=="-Out" (
    shift
    if "%~1"=="" (
        echo [ERROR] -Out needs a path.
        exit /b 1
    )
    set "OUT=%~1"
    shift
    goto parse
)
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
echo [ERROR] unknown argument: %~1
echo         Run "pack-dist.bat -h" for usage.
exit /b 1
:parsed

rem ---------------------------------------------------------------- preflight
if not exist "%ROOT%\tagger\cli.py" (
    echo [ERROR] %ROOT% does not look like the repo ^(tagger\cli.py missing^).
    exit /b 1
)
if not exist "%ROOT%\install.bat" (
    echo [ERROR] install.bat missing - that file is what the receiver runs.
    exit /b 1
)
rem No UI dist means the receiver gets a blank "frontend not built" page, and
rem they probably have no Node to build it with. Stop instead of shipping that.
if not exist "%ROOT%\tagger\web\ui\dist\index.html" (
    echo [ERROR] tagger\web\ui\dist\index.html is missing.
    echo         Build the frontend first:  cd tagger\web\ui ^&^& npm run build
    exit /b 1
)
rem And the reverse: a full node_modules is 121MB of build-time junk.
if "%WITHENV%"=="1" (
    if exist "%ROOT%\env\python\Lib\site-packages\torch" (
        echo [ERROR] env\python still contains torch - the zip would be ~4GB.
        echo         Prune it first:  prune-env.bat -Apply -StripBytecode
        echo         Or pack without it:  pack-dist.bat
        exit /b 1
    )
)

rem ---------------------------------------------------------------- output path
if "%OUT%"=="" (
    for /f "delims=" %%t in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmm"') do set "STAMP=%%t"
    if not exist "%ROOT%\dist" mkdir "%ROOT%\dist"
    set "OUT=%ROOT%\dist\tagger-!STAMP!.zip"
)
set "NAME=tagger"
for %%F in ("%OUT%") do (
    set "OUTDIR=%%~dpF"
    set "NAME=%%~nF"
)
if not exist "!OUTDIR!" mkdir "!OUTDIR!"

rem ---------------------------------------------------------------- staging
set "TMPBASE=%TEMP%\tagger-pack-%RANDOM%%RANDOM%"
set "STAGE=%TMPBASE%\%NAME%"
mkdir "%STAGE%" 2>nul
if not exist "%STAGE%" (
    echo [ERROR] cannot create staging dir %STAGE%
    exit /b 1
)

echo.
echo  [pack] repo : %ROOT%
echo  [pack] zip  : %OUT%
echo.

rem ---------------------------------------------------------------- copy
rem robocopy returns 0-7 on success (1 just means "files copied"), 8+ is an
rem error. Each call is checked for the 8+ case only.
set "RC=0"

robocopy "%ROOT%\tagger" "%STAGE%\tagger" /E /XD node_modules __pycache__ dist.bak .vite /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto copy_failed

robocopy "%ROOT%\devtools" "%STAGE%\devtools" /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto copy_failed

if exist "%ROOT%\configs" (
    robocopy "%ROOT%\configs" "%STAGE%\configs" /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copy_failed
)

rem desktop\ is source only: bin\ and obj\ hold last build's intermediate
rem output and would add tens of MB for nothing.
robocopy "%ROOT%\desktop" "%STAGE%\desktop" /E /XD bin obj .vs /XF *.user /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto copy_failed

if "%TESTS%"=="1" (
    if exist "%ROOT%\tests" (
        robocopy "%ROOT%\tests" "%STAGE%\tests" /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
        if errorlevel 8 goto copy_failed
    )
)

rem Initialization payload: the embeddable interpreter + pip wheel. Without
rem these install.bat has nothing to unzip, and the receiver cannot install.
if exist "%ROOT%\env\initialization" (
    robocopy "%ROOT%\env\initialization" "%STAGE%\env\initialization" /E /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copy_failed
) else (
    echo  [pack] WARNING: env\initialization is missing. The receiver will not
    echo         be able to run install.bat without a python embed zip.
)

if "%WITHENV%"=="1" (
    echo  [pack] including env\python ^(already pruned^)
    robocopy "%ROOT%\env\python" "%STAGE%\env\python" /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copy_failed
)

for %%F in (install.bat install.sh tagger.cmd tagger.sh run-web.cmd run-web.sh) do (
    if exist "%ROOT%\%%F" copy /y "%ROOT%\%%F" "%STAGE%\%%F" >nul
)
for %%F in (requirements-embedded.txt pyproject.toml HANDOFF.md prune-env.bat pack-dist.bat) do (
    if exist "%ROOT%\%%F" copy /y "%ROOT%\%%F" "%STAGE%\%%F" >nul
)

rem The single-file shell is the one people double-click. Prefer the freshest
rem build over whatever copy happens to sit in the repo root.
set "SHELL="
if exist "%ROOT%\dist\desktop-single\Tagger.Shell.exe" set "SHELL=%ROOT%\dist\desktop-single\Tagger.Shell.exe"
if not defined SHELL if exist "%ROOT%\Tagger.Shell.exe" set "SHELL=%ROOT%\Tagger.Shell.exe"
if defined SHELL (
    copy /y "!SHELL!" "%STAGE%\Tagger.Shell.exe" >nul
) else (
    echo  [pack] WARNING: no Tagger.Shell.exe found. Build it with:
    echo         powershell -File desktop\build.ps1 -SingleFile
)

rem Read-me for the receiver; renamed so it sorts to the top in Explorer.
if exist "%ROOT%\pack-readme.txt" (
    copy /y "%ROOT%\pack-readme.txt" "%STAGE%\README-FIRST.txt" >nul
)

rem ---------------------------------------------------------------- zip
where tar >nul 2>nul
if errorlevel 1 goto zip_via_ps

echo  [pack] compressing...
tar -a -c -f "!OUT!" -C "%TMPBASE%" "%NAME%"
if errorlevel 1 goto zip_failed
goto zipped

:zip_via_ps
echo  [pack] tar not found, falling back to Compress-Archive...
powershell -NoProfile -Command "Compress-Archive -Path '%STAGE%' -DestinationPath '%OUT%' -Force -CompressionLevel Optimal"
if errorlevel 1 goto zip_failed

:zipped
for %%F in ("!OUT!") do set "ZBYTES=%%~zF"
set /a ZMB=%ZBYTES% / 1048576

echo.
echo  [pack] top level:
for /d %%D in ("%STAGE%\*") do echo         %%~nxD\
for %%F in ("%STAGE%\*") do echo         %%~nxF

echo.
echo  [pack] done: %OUT%
echo  [pack] size: %ZMB% MB

if "%WITHENV%"=="0" (
    echo.
    echo  [pack] No env\python in this zip: the receiver runs install.bat first,
    echo         which downloads dependencies ^(needs a network connection^).
)

rd /s /q "%TMPBASE%" 2>nul
echo.
echo  [pack] hand this file to the other machine, then:
echo            1. unzip anywhere outside Program Files
echo            2. run install.bat once
echo            3. double-click Tagger.Shell.exe
echo.
endlocal
exit /b 0

rem ---------------------------------------------------------------- errors
:copy_failed
echo.
echo  [ERROR] copying files into the staging dir failed.
echo          Check free space on %TEMP%, or an antivirus blocking robocopy.
rd /s /q "%TMPBASE%" 2>nul
endlocal
exit /b 1

:zip_failed
echo.
echo  [ERROR] compression failed.
rd /s /q "%TMPBASE%" 2>nul
endlocal
exit /b 1

:usage
echo.
echo  Build a .zip for another machine.
echo.
echo    pack-dist.bat                    zip without env\python (~60MB)
echo    pack-dist.bat -WithEnv           zip with a pruned env\python
echo    pack-dist.bat -NoTests           skip tests\
echo    pack-dist.bat -Out D:\x.zip      explicit output path
echo.
echo  Always excluded: models\ work\ run\ .pytest_cache node_modules
echo                   env\python\Lib\site-packages (unless pruned and -WithEnv)
echo.
endlocal
exit /b 0
