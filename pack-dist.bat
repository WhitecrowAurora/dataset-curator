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
rem    pack-dist.bat -WithLlama         include runtime\llama.cpp (~743MB)
rem    pack-dist.bat -NoTests           skip tests\ (smaller, no self-check)
rem    pack-dist.bat -Out D:\x.zip      explicit output path
rem
rem  Why llama.cpp is NOT included by default: it is downloaded by install.bat
rem  (same code as the web button) and it is 743MB, of which 570MB would be
rem  wasted -- the CUDA build is tied to the receiver's driver. Ship it only
rem  when the target machine has no internet, or when you must pin the exact
rem  build. runtime\llama.cpp\.download\ (the raw zips) is never copied.
rem
rem  What IS always included: vendor\llama-convert (~2MB, pure Python). That is
rem  llama.cpp's convert_hf_to_gguf.py + conversion\ + gguf-py\, i.e. the script
rem  behind "local quantization". Bundling it means the receiver never has to
rem  pull a 37MB source tarball from GitHub just to turn safetensors into GGUF.
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
set "WITHLLAMA=0"
set "TESTS=1"
set "OUT="

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="-WithEnv" (
    set "WITHENV=1"
    shift
    goto parse
)
if /i "%~1"=="-WithLlama" (
    set "WITHLLAMA=1"
    shift
    goto parse
)
if /i "%~1"=="-NoTests" (
    set "TESTS=0"
    shift
    goto parse
)
if /i "%~1"=="-Out" goto parse_out
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
echo [ERROR] unknown argument: %~1
echo         Run "pack-dist.bat -h" for usage.
exit /b 1

rem -Out is handled at a label, NOT inside an "if (...)" block: inside a block
rem `%~1` is expanded once when the block is parsed, so the `shift` above has
rem no effect and OUT ends up being the literal string "-Out". Symptom: the
rem zip is named "-Out" and tar fails with "Can't specify both -u and -c".
:parse_out
shift
if "%~1"=="" (
    echo [ERROR] -Out needs a path.
    exit /b 1
)
set "OUT=%~1"
shift
goto parse

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
rem vendor\llama-convert is the built-in GGUF convert script (llama.cpp's
rem convert_hf_to_gguf.py + conversion\ + gguf-py\, ~2MB of pure Python). It is
rem what makes "local quantization" work offline: without it the receiver's
rem first convert goes to GitHub for a 37MB source tarball, and that download is
rem exactly the step that stalls at 8/37 MB on many networks. 2MB is nothing --
rem refuse to ship a package where it silently went missing.
if not exist "%ROOT%\vendor\llama-convert\convert_hf_to_gguf.py" (
    echo [ERROR] vendor\llama-convert\convert_hf_to_gguf.py is missing.
    echo         Local quantization would fall back to downloading 37MB.
    echo         Restore that folder from the repo, then pack again.
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

rem MCP server for the receiver's own AI assistant. Pure stdlib Python, so it
rem needs no extra wheels in install.bat -- just ship the folder.
if exist "%ROOT%\mcp" (
    robocopy "%ROOT%\mcp" "%STAGE%\mcp" /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copy_failed
)

if exist "%ROOT%\configs" (
    robocopy "%ROOT%\configs" "%STAGE%\configs" /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copy_failed
    rem models.json is the receiver's OWN overlay: models they added, custom
    rem download dirs, proxy, notes, and the gguf_quants entries written by
    rem local quantization. Shipping ours means "upgrade once, lose all of it"
    rem (ours is usually just {}). Ship tiers.yaml, never this file.
    if exist "%STAGE%\configs\models.json" del /q "%STAGE%\configs\models.json"
    rem ui-form.json is the "last used form" memory: material paths, VRAM
    rem knobs, concurrency. Same reasoning -- never ship ours over theirs.
    if exist "%STAGE%\configs\ui-form.json" del /q "%STAGE%\configs\ui-form.json"
)

rem NL prompt presets: prompt\*.json, one file per preset. The FILE NAME is what
rem the "new job" page shows in its dropdown (Chinese names included). Ships with
rem OUR files -- unlike configs\models.json this is not per-machine state: the
rem built-in snapshot is the only thing a fresh install has to pick besides the
rem generated prompt, so shipping it is what makes the feature discoverable.
rem A receiver who edited a preset keeps the edits until a new package lands.
if exist "%ROOT%\prompt" (
    robocopy "%ROOT%\prompt" "%STAGE%\prompt" /E /XF *.tmp *.log /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 goto copy_failed
    if not exist "%STAGE%\prompt\*.json" (
        echo  [pack] WARNING: prompt\ has no .json preset, so the dropdown will
        echo         only offer the built-in generated prompt.
    )
) else (
    echo  [pack] WARNING: prompt\ is missing, the receiver gets no presets.
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

rem Built-in GGUF convert script: independent of -WithLlama, because this is
rem three .py folders (~2MB), not the 743MB CUDA build. It lets "local
rem quantization" run on first click without fetching llama.cpp sources.
robocopy "%ROOT%\vendor\llama-convert" "%STAGE%\vendor\llama-convert" /E /XD __pycache__ /XF *.pyc /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto copy_failed

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

rem Optional: the llama.cpp build. Off by default because install.bat normally
rem fetches it and because the CUDA DLLs are driver-specific. .download\ holds
rem the raw zips (~570MB of pure waste) so it is always excluded.
if "%WITHLLAMA%"=="1" (
    if exist "%ROOT%\runtime\llama.cpp" (
        echo  [pack] including runtime\llama.cpp
        robocopy "%ROOT%\runtime\llama.cpp" "%STAGE%\runtime\llama.cpp" /E /XD .download /NFL /NDL /NJH /NJS /NP >nul
        if errorlevel 8 goto copy_failed
    ) else (
        echo  [pack] WARNING: -WithLlama given but runtime\llama.cpp is missing.
        echo         Install it first:  tagger llama install
    )
)

for %%F in (install.bat install.sh tagger.cmd tagger.sh run-web.cmd run-web.sh) do (
    if exist "%ROOT%\%%F" copy /y "%ROOT%\%%F" "%STAGE%\%%F" >nul
)
rem HANDOFF.md lives in devtools\ (it is shipped there by the devtools copy
rem above). It used to be listed here as a root file, which silently did
rem nothing -- "if exist" hides a moved file, so that entry was dead.
rem agent.md is the AI-facing guide that goes with the mcp\ folder above.
for %%F in (requirements-embedded.txt requirements-tag.txt pyproject.toml prune-env.bat pack-dist.bat pack-readme.txt agent.md) do (
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

rem tmp\ is this machine's scratch (extracted archives, sqlite spills, child
rem process caches -- see tagger\tmpdir.py). It is recreated on first use, so
rem shipping it only adds whatever junk happened to be lying around.
if exist "%STAGE%\tmp" rd /s /q "%STAGE%\tmp" 2>nul

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
echo    pack-dist.bat -WithLlama         include runtime\llama.cpp (~743MB)
echo    pack-dist.bat -NoTests           skip tests\
echo    pack-dist.bat -Out D:\x.zip      explicit output path
echo.
echo   Always included: vendor\llama-convert (GGUF convert script, ~2MB)
echo                    prompt\ (NL prompt presets, one .json per preset)
echo.
echo  Always excluded: models\ work\ run\ tmp\ .pytest_cache node_modules
echo                   env\python\Lib\site-packages (unless pruned and -WithEnv)
echo                   runtime\llama.cpp\.download (raw zips, 570MB)
echo.
echo  llama.cpp is normally fetched by install.bat on the receiver.
echo  Use -WithLlama only for offline targets or to pin an exact build.
echo.
endlocal
exit /b 0
