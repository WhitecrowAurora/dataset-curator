$ErrorActionPreference = 'Continue'
$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8
New-Item -ItemType Directory -Force -Path 'K:\anlia\_artist' | Out-Null
"DIR_OK=" + (Test-Path 'K:\anlia\_artist')
"FREE_K_GB=" + [Math]::Round((Get-PSDrive K).Free / 1GB, 1)
