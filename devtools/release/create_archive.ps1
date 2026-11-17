param(
    [Parameter(Mandatory = $true)][string]$Stage,
    [Parameter(Mandatory = $true)][string]$OutputPath
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$temporaryArchive = Join-Path (Split-Path -Parent $Stage) 'payload.zip'
# ZipFile writes the UTF-8 flag for non-ASCII names, including Chinese presets.
[IO.Compression.ZipFile]::CreateFromDirectory(
    $Stage, $temporaryArchive, [IO.Compression.CompressionLevel]::Optimal,
    $true, [Text.Encoding]::UTF8)
if (Test-Path -LiteralPath $resolvedOutput) {
    $previousRoot = Join-Path (Split-Path -Parent $resolvedOutput) (
        '.delete\pack-output-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $previousRoot | Out-Null
    Move-Item -LiteralPath $resolvedOutput -Destination $previousRoot
}
Move-Item -LiteralPath $temporaryArchive -Destination $resolvedOutput
