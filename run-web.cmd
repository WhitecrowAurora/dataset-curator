@echo off
rem Windows 的"浏览器模式"入口：不开 WPF 壳子，直接起服务再开默认浏览器。
rem 与 run-web.sh 对齐；日常用 dist\desktop\Tagger.Shell.exe 更省事。
setlocal
set "ROOT=%~dp0"
set "PORT=%~1"
if "%PORT%"=="" set "PORT=27100"

set "PY=%ROOT%env\python\python.exe"
if not exist "%PY%" set "PY=python"

echo [web] 启动本地服务 http://127.0.0.1:%PORT%/
echo [web] 关闭本窗口即停止服务
start "" "http://127.0.0.1:%PORT%/"
"%PY%" -m tagger.cli web --host 127.0.0.1 --port %PORT% --no-open
