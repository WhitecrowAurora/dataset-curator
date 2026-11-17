param([Parameter(Mandatory = $true)][string]$Stage)
$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$resolvedStage = [IO.Path]::GetFullPath($Stage)
$tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
if (-not $resolvedStage.StartsWith($tempRoot, [StringComparison]::OrdinalIgnoreCase) -or
    (Split-Path -Leaf $resolvedStage) -notlike 'tagger-pack-*') {
    throw "打包暂存路径不在预期临时目录：$resolvedStage"
}
if (Test-Path -LiteralPath $resolvedStage) {
    $archiveRoot = Join-Path $taskRoot '.delete\pack-staging'
    New-Item -ItemType Directory -Force -Path $archiveRoot | Out-Null
    $destination = Join-Path $archiveRoot (Split-Path -Leaf $resolvedStage)
    if (Test-Path -LiteralPath $destination) { $destination += '-' + [guid]::NewGuid().ToString('N') }
    Move-Item -LiteralPath $resolvedStage -Destination $destination
    Write-Host " [pack] staging archived: $destination"
}
