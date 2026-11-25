@echo off
rem 便携入口：优先用仓库自带的嵌入式 Python，没有才退回系统 python。
rem 这样"装了包的人"和"没配过环境的人"敲的是同一条命令，输出也一致。
setlocal
rem 切到脚本所在目录：configs\tiers.yaml 与 prompt\*.json 都按相对路径解析，
rem 在别的目录敲 tagger.cmd 会找不到它们（tiers 报 ConfigError，预设下拉静默变空）。
rem 代价是相对路径参数（--out out）相对包目录，而不是敲命令时所在的目录。
cd /d "%~dp0"
set "PY=%~dp0env\python\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" -m tagger.cli %*
