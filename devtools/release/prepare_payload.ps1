param(
    [Parameter(Mandatory = $true)][string]$Stage,
    [switch]$WithEnv
)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$python = Join-Path $taskRoot 'env\python\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    $python = (Get-Command python -CommandType Application -ErrorAction Stop).Source
}
$workerBuilder = Join-Path $Stage 'devtools\release\build_worker.py'
& $python -B $workerBuilder --out (Join-Path $Stage 'worker.py') | Out-Null
if ($LASTEXITCODE -ne 0) { throw "独立 worker 生成失败，退出码 $LASTEXITCODE" }

if ($WithEnv) {
    # .pth 的相对路径由 site-packages 解析；向上四级就是解压后的包根。
    # 仅修改分发暂存副本，保留开发机环境的原始路径。
    $repoPathFile = Join-Path $Stage 'env\python\Lib\site-packages\_tagger_repo.pth'
    Set-Content -LiteralPath $repoPathFile -Value '..\..\..\..' -Encoding ASCII
}
