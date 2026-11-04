@echo off
rem ===========================================================================
rem  Prune env\python so the repo can be packed and sent to another machine.
rem
rem  env\python is ~4.4GB and almost all of it is torch, which the web UI, the
rem  CLI tagging path and the parquet packing path never touch: every torch /
rem  timm / datasets / pandas import in tagger\ (cpu_tagger.py,
rem  quantize_worker.py, hf_source.py) sits inside a function body, i.e. lazy.
rem
rem  Default is a DRY RUN. Nothing is deleted unless -Apply is given.
rem
rem  Usage:
rem    prune-env.bat                          list what would go
rem    prune-env.bat -Apply                   delete it
rem    prune-env.bat -Apply -StripBytecode    also drop __pycache__ / *.pyc
rem    prune-env.bat -KeepTaggerStack         keep torch/timm (no size win)
rem
rem  The script also rewrites env\python\Lib\site-packages\_tagger_repo.pth.
rem  That file holds an ABSOLUTE path to the repo, so it has to be repointed on
rem  every new machine -- otherwise "import tagger" fails and the shell window
rem  only says the local service exited right after starting.
rem
rem  NOTE: keep this file pure ASCII. cmd.exe reads batch files with the active
rem  code page; Chinese text here turns into mis-aligned lines and fragments of
rem  comments get executed as commands. Chinese belongs in the docs, not here.
rem ===========================================================================
setlocal EnableDelayedExpansion

cd /d "%~dp0"

set "APPLY=0"
set "KEEP=0"
set "STRIP=0"

:parse
if "%~1"=="" goto parsed
if /i "%~1"=="-Apply" (
    set "APPLY=1"
    shift
    goto parse
)
if /i "%~1"=="-KeepTaggerStack" (
    set "KEEP=1"
    shift
    goto parse
)
if /i "%~1"=="-StripBytecode" (
    set "STRIP=1"
    shift
    goto parse
)
if /i "%~1"=="-h" goto usage
if /i "%~1"=="--help" goto usage
echo [ERROR] unknown argument: %~1
goto usage_fail
:parsed

set "ROOT=%CD%"
set "PYD=%ROOT%\env\python"
set "PY=%PYD%\python.exe"
set "SITE=%PYD%\Lib\site-packages"

if not exist "%PY%" (
    echo [ERROR] %PY% not found.
    echo         Run install.bat once to build the embedded environment.
    exit /b 1
)
if not exist "%SITE%" (
    echo [ERROR] %SITE% not found. The environment looks broken.
    exit /b 1
)

rem ---------------------------------------------------------------- groups
rem Matched by prefix, so one entry covers both "torch" and
rem "torch-2.14.1+cu126.dist-info".
rem "torch" already covers torchvision / torchgen / torch-*.dist-info; "attr"
rem covers attrs. Listing both again would make the loop visit a directory twice.
set "G_TAG=torch functorch timm safetensors"
set "G_MATH=sympy mpmath networkx"
set "G_HF=huggingface_hub hf_xet datasets truststore opentelemetry dill multiprocess _multiprocess xxhash fsspec filelock tqdm"
set "G_PD=pandas dateutil python_dateutil tzdata"
set "G_AIO=aiohttp aiosignal frozenlist yarl multidict propcache aiohappyeyeballs attr"

set "PREFIXES="
if "%KEEP%"=="0" set "PREFIXES=%G_TAG% %G_MATH%"
set "PREFIXES=%PREFIXES% %G_HF% %G_PD% %G_AIO%"

rem Belt and braces: the real main-chain deps are never removed, even if a name
rem above is ever mistyped.
set "KEEPNAMES=anyio certifi click colorama h11 httpcore httpx idna socksio typing_extensions yaml _yaml PIL pyarrow pyarrow.libs numpy numpy.libs fastapi starlette uvicorn websockets pydantic pydantic_core annotated_types annotated_doc typing_inspection pip setuptools pkg_resources _distutils_hack pytest _pytest pluggy iniconfig packaging pygments jinja2 markupsafe"

echo.
echo  [prune] repo: %ROOT%
echo  [prune] env : %SITE%
echo.

set "FOUND=0"
set "DONE=0"
set "FAIL=0"
rem Prefixes overlap by design (torch* also catches torchvision), so a name can
rem be visited twice. Keep a comma-wrapped seen-list and skip the repeat -
rem otherwise -Apply would "fail" on an already-deleted directory.
set "SEEN=,"

for %%N in (%PREFIXES%) do (
    for /d %%D in ("%SITE%\%%N*") do (
        call :is_kept "%%~nxD"
        if "!KEPT!"=="0" (
            echo !SEEN!| findstr /l /c:",%%~nxD," >nul
            if errorlevel 1 (
                set "SEEN=!SEEN!%%~nxD,"
                set /a FOUND+=1
                if "%APPLY%"=="1" (
                    rd /s /q "%%~fD" 2>nul
                    if exist "%%~fD" (
                        echo    [busy] %%~nxD
                        set /a FAIL+=1
                    ) else (
                        echo    [gone] %%~nxD
                        set /a DONE+=1
                    )
                ) else (
                    echo    [plan] %%~nxD
                )
            )
        )
    )
)

if "%STRIP%"=="1" (
    echo.
    echo  [prune] dropping bytecode caches
    for /f "delims=" %%D in ('dir /s /b /ad "%PYD%\__pycache__" 2^>nul') do rd /s /q "%%D" 2>nul
    del /s /q "%PYD%\*.pyc" >nul 2>nul
)

echo.
if "%FOUND%"=="0" (
    echo  [prune] nothing to remove, the environment is already clean.
) else (
    if "%APPLY%"=="1" (
        echo  [prune] removed !DONE! of %FOUND% entries.
    ) else (
        echo  [prune] %FOUND% entries would be removed.
    )
)
if not "%FAIL%"=="0" (
    echo  [prune] !FAIL! entries could not be removed - close the shell window
    echo          and any open terminal, then run again.
)

if not "%APPLY%"=="1" (
    echo.
    echo  [prune] DRY RUN - nothing changed. To do it for real:
    echo            prune-env.bat -Apply -StripBytecode
    echo.
    goto end
)

rem ---------------------------------------------------------------- repo path
rem Written by python, not by "echo >": the repo path can contain non-ASCII
rem characters, and "echo" would emit them in the ANSI code page while the site
rem module reads .pth as UTF-8. That mismatch silently breaks "import tagger".
"%PY%" -c "import os,sys;open(os.path.join(sys.argv[1],'_tagger_repo.pth'),'w',encoding='utf-8').write(os.getcwd())" "%SITE%"
echo  [prune] _tagger_repo.pth -> %ROOT%

rem ---------------------------------------------------------------- self-check
echo.
echo  [prune] self-check:
"%PY%" -c "import fastapi, uvicorn, websockets, httpx, yaml, PIL, pyarrow, numpy; print('   web + pack deps OK')"
set "RC1=%ERRORLEVEL%"
"%PY%" -m tagger.cli --help >nul 2>&1
set "RC2=%ERRORLEVEL%"
if "%RC2%"=="0" echo    tagger.cli --help: OK

echo.
if not "%RC1%"=="0" goto broken
if not "%RC2%"=="0" goto broken

echo  [prune] Done. Pack the repo, but skip: models\ work\ run\ .pytest_cache
echo          node_modules dist.bak dist\desktop
echo  [prune] On the target machine run install.bat ONCE before starting
echo          Tagger.Shell.exe - it repoints .pth and installs missing deps.
if "%KEEP%"=="0" (
    echo  [prune] torch was removed. To get the tag backends back:
    echo            env\python\python.exe -m pip install torch torchvision timm safetensors
)
echo.
goto end

:broken
echo  [prune] self-check FAILED - the main chain lost a package. Restore with:
echo            env\python\python.exe -m pip install -r requirements-embedded.txt
echo.
exit /b 1

rem ---------------------------------------------------------------- helpers
:is_kept
rem %1 = directory name; sets KEPT=1 when it is on the never-remove list.
set "KEPT=0"
for %%K in (%KEEPNAMES%) do if /i "%~1"=="%%K" set "KEPT=1"
goto :eof

:usage
echo.
echo  Prune env\python for packaging.
echo.
echo    prune-env.bat                          list what would go
echo    prune-env.bat -Apply                   delete it
echo    prune-env.bat -Apply -StripBytecode    also drop __pycache__ / *.pyc
echo    prune-env.bat -KeepTaggerStack         keep torch/timm
echo.
exit /b 0

:usage_fail
echo.
echo  Run "prune-env.bat -h" for usage.
echo.
exit /b 1

:end
endlocal
exit /b 0
