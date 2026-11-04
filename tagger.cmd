@echo off
rem 便携入口：优先用仓库自带的嵌入式 Python，没有才退回系统 python。
rem 这样"装了包的人"和"没配过环境的人"敲的是同一条命令，输出也一致。
setlocal
set "PY=%~dp0env\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m tagger.cli %*
