@echo off
rem Keep this batch file ASCII: cmd.exe uses the active console code page.
rem Prefer embedded Python; use system Python when it is not installed.
setlocal
rem Resolve configs, prompts and relative arguments from the package directory.
cd /d "%~dp0"
set "PY=%~dp0env\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -u -c "import os,runpy,sys;sys.path.insert(0,os.getcwd());runpy.run_module('tagger.cli',run_name='__main__')" %*
